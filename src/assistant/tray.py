"""
Plane A's human interface: the tray icon, the hotkey, and presence detection.

Three things live here, and they are grouped because they are the only parts of the
assistant that touch the desktop at all.

Why the hotkey is RegisterHotKey and not the `keyboard` library
---------------------------------------------------------------
`keyboard` is installed (0.13.5) because Phase 5 needs a genuine global key listener
for the offline wake word. It is deliberately NOT used for the hotkey, for three
reasons that are worth stating rather than leaving for someone to rediscover:

1. **It wants administrator rights.** An assistant that must be elevated to be
   summoned is an assistant you cannot summon from a normal session. Silent elevation
   is explicitly out of scope in PLAN.md, and a hotkey that only works in an
   elevated prompt is not a hotkey.

2. **It sees every keystroke.** The library installs a low-level hook, so the process
   observes the entire keyboard, all the time. For a 24/7 resident daemon that is a
   standing liability, and it buys nothing here: we want exactly one chord.

3. **It is a dependency for one integer.** `RegisterHotKey` is a single Win32 call.
   Verified on this machine: `RegisterHotKey(None, 1, MOD_CONTROL|MOD_ALT, VK_SPACE)`
   returns 1 in a non-elevated session, no library, no hook, no extra thread pumping
   every scancode.

The consequence is a nice property. A process that can only ever see one specific
chord has a much smaller blast radius than one that can see everything, and that
difference is not incidental - it is the reason the whole project can hold the line
that safety is deterministic code rather than prompt discipline.

Presence detection
------------------
D13's unattended cap needs to know whether a human is at the keyboard. Asking the
caller over the transport would be worthless - whoever sends the request could simply
claim to be present. So presence is read from the OS (`GetLastInputInfo`) and can
only ever be *narrowed* from a remote channel, never asserted by one.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

from . import __version__, ipc
from .policy import CHANNEL_PC

# --- Win32 constants -----------------------------------------------------------
# Only the values actually used. Virtual keys that matter: space.
VK_SPACE = 0x20
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000  # Windows 7+. Without it, holding the chord fires repeatedly.
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

HOTKEY_ID = 0xB01
HOTKEY_LABEL = "Ctrl+Alt+Space"


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwTime", wt.DWORD)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Tell the OS our structures are the right size. Without this, the 64-bit marshalling
# of GetMessageW is wrong and messages come back with garbage in them - a failure that
# would look like "the hotkey randomly does not fire".
user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
kernel32.GetTickCount64.restype = ctypes.c_ulonglong


def seconds_since_input() -> int:
    """Seconds since the last keyboard or mouse input anywhere in the session.

    This is the ground truth for presence. It cannot be forged over the transport,
    which is the only property that makes it usable for the D13 unattended cap.
    """
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise OSError("GetLastInputInfo failed - presence cannot be determined")
    return int((kernel32.GetTickCount64() - info.dwTime) / 1000)


class HotkeyUnavailable(RuntimeError):
    """The chord is already taken by someone else.

    This is a real possibility, not a theoretical one: Ctrl+Alt+Space is a popular
    IME toggle on several East Asian input method editors. If the registration fails
    we say so plainly instead of silently starting a daemon that can never be
    summoned, which is the failure mode where the user concludes the assistant is
    broken rather than that it is in a conflict.
    """


class Hotkey:
    """A single global chord, delivered to a callback on its own thread."""

    def __init__(self, callback, *, label=HOTKEY_LABEL, vk=VK_SPACE, mods=MOD_CONTROL | MOD_ALT):
        self.callback = callback
        self.label = label
        self.vk = vk
        self.mods = mods | MOD_NOREPEAT
        self.registered = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def start(self, timeout: float = 5.0) -> "Hotkey":
        # Register from this thread. The registration is per-thread on Windows, which
        # is precisely why the message pump below has to live in the same thread: a
        # WM_HOTKEY posted to a thread that did not register it is never delivered.
        if not user32.RegisterHotKey(None, HOTKEY_ID, self.mods, self.vk):
            raise HotkeyUnavailable(
                f"could not register {self.label} - another program already owns it. "
                "Common culprits: an IME (Ctrl+Alt+Space toggles several), or another "
                "instance of this assistant."
            )
        self.registered = True
        self._thread = threading.Thread(
            target=self._pump, name="hotkey", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise HotkeyUnavailable(f"{self.label} thread did not start within {timeout}s")
        if self._error is not None:
            self.stop()
            raise HotkeyUnavailable(f"{self.label} message pump failed: {self._error}")
        return self

    def _pump(self) -> None:
        msg = _MSG()
        try:
            self._ready.set()
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):  # WM_QUIT, or an outright error
                    break
                if got > 0 and msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    # A failure inside the callback must not take the pump down. This
                    # thread is the only thing that can report the hotkey, so if it
                    # dies the assistant silently becomes unsummonable - the exact
                    # failure HotkeyUnavailable exists to make loud.
                    try:
                        self.callback()
                    except Exception as exc:  # noqa: BLE001 - deliberate
                        ipc.note(f"hotkey callback raised: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - deliberate
            self._error = exc
            self._ready.set()

    def post_quit(self) -> None:
        user32.PostThreadMessageW(self._thread.ident if self._thread else 0, WM_QUIT, 0, 0)

    def stop(self) -> None:
        if self.registered:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self.registered = False
        self.post_quit()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)


class Presence:
    """Keeps the daemon's attended flag honest.

    The rule: presence is *observed*, and can only be *narrowed* remotely. This
    mirrors the daemon's own `session.set_attended`, but it does not replace it -
    it calls it, through the same audited path, so the audit log records every
    transition and a reader can reconstruct who was at the keyboard and when.
    """

    def __init__(self, daemon, *, idle_after: int = 300, poll: float = 2.0, retry_after: float = 60.0):
        self.daemon = daemon
        self.idle_after = idle_after
        self.poll = poll
        self.retry_after = retry_after
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # None, not False. The daemon starts attended, so a monitor that assumes
        # "not held" would see an already-correct state and never speak - and the
        # very first transition it misses is the one where a real transition to
        # unattended would go unrecorded. "I have not looked yet" is the honest
        # starting value.
        self._held: bool | None = None
        self._attempted_at: float = 0.0
        self._attempted: bool | None = None

    def _call(self, want: bool, why: str) -> bool:
        req = ipc.Request(
            op="session.set_attended",
            args={"attended": want, "channel": CHANNEL_PC, "why": why},
            id=ipc.next_id(),
        )
        reply = self.daemon.handle(req)
        return bool(reply.get("ok"))

    def _should_speak(self, present: bool, now: float) -> bool:
        """Attempt on every transition, and re-attempt a failed one rarely.

        The order of these tests is the whole implementation. A failed call leaves
        `_held` at its old value, so asking "did we change since we last *know*"
        first would answer "yes" forever, and the monitor would hammer a subsystem
        that is already refusing. The question that matters is whether the observed
        state differs from what we last *tried*; the held value only tells us
        whether we are currently correct.
        """
        if present != self._attempted:
            return True                      # a genuine transition
        if present == self._held:
            return False                     # already right, stay quiet
        return (now - self._attempted_at) >= self.retry_after

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                idle = seconds_since_input()
                present = idle < self.idle_after
                if self._should_speak(present, now):
                    self._attempted = present
                    self._attempted_at = now
                    if self._call(present, f"idle for {idle}s"):
                        self._held = present
            except Exception as exc:  # noqa: BLE001 - deliberate
                # A failure here must not mark us unattended. Defaulting to
                # "nobody is home" would silently pause the assistant's work, and
                # the honest response to not knowing is to keep the current state.
                ipc.note(f"presence check failed: {type(exc).__name__}: {exc}")
            self._stop.wait(self.poll)

    def start(self) -> "Presence":
        self._thread = threading.Thread(target=self._run, name="presence", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)


def _icon_image(size: int = 64):
    """A tray icon drawn here rather than shipped as a binary.

    An .ico in the repo would be an opaque asset nobody can review. Three
    ellipses and two connecting lines is the whole idea - the assistant, linking
    the planes - and it is inspectable in a diff.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    line = max(2, size // 16)
    r = size // 6
    nodes = [
        (size // 2, r + 2),                      # the brain
        (r + 2, size - r - 2),                   # tools
        (size - r - 2, size - r - 2),            # supervisor
    ]
    d.line([nodes[1], nodes[0]], fill=(120, 200, 255, 255), width=line)
    d.line([nodes[2], nodes[0]], fill=(120, 200, 255, 255), width=line)
    colours = [(235, 235, 245, 255), (255, 170, 60, 255), (120, 220, 140, 255)]
    for (cx, cy), c in zip(nodes, colours):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
    return img


class Tray:
    """The tray icon.

    Every menu item goes through `daemon.handle`, the same path a pipe peer uses.
    There is no privileged shortcut from the icon to the filesystem: the tray is a
    *client* of the daemon, not a second front door. That is the property worth
    holding, because a tray that can do more than the pipe is a tray that nobody
    audits.
    """

    def __init__(self, daemon, *, title=f"Local assistant {__version__}"):
        self.daemon = daemon
        self.title = title
        self.icon = None
        self._notice: str | None = None
        self._color: tuple | None = None
        self._lock = threading.Lock()

    # -- the only way out of the icon -----------------------------------------
    def call(self, op: str, /, **args) -> dict:
        args.setdefault("channel", CHANNEL_PC)
        args["actor"] = "tray"
        return self.daemon.handle(
            ipc.Request(op=op, args=args, id=ipc.next_id())
        )

    def _status(self, icon=None, item=None) -> str:
        res = self.call("status")
        if not res.get("ok"):
            return f"daemon: {res.get('error', 'unreachable')}"
        s = res["result"]
        mode = "attended" if s["attended"] else "unattended"
        return f"assistant {s['version']}  |  {mode}"

    def _snapshot(self, icon=None, item=None) -> None:
        res = self.call("audit.verify")
        ok = bool(res.get("ok")) and res["result"].get("ok")
        n = res["result"].get("checked", 0) if res.get("ok") else 0
        self.notify(
            f"Audit chain {'VERIFIES' if ok else 'BROKEN'} - {n} records",
            None if ok else (255, 90, 90),
        )

    def _vram(self, icon=None, item=None) -> None:
        res = self.call("arbiter.summary")
        self.notify(res.get("result") if res.get("ok") else str(res.get("error")),
                    None if res.get("ok") else (255, 90, 90))

    def _workspaces(self, icon=None, item=None) -> None:
        res = self.call("workspace.list")
        if not res.get("ok"):
            self.notify(str(res.get("error")), (255, 90, 90))
            return
        names = res["result"].get("workspaces") or []
        self.notify(
            f"{len(names)} workspace(s) approved: {', '.join(names)}"
            if names
            else "No workspace approved. The assistant cannot read any project yet.",
        )

    def _set_attended(self, icon=None, item=None) -> None:
        self.call("session.set_attended", attended=True, why="summoned from the tray")
        self.notify("Marked attended. Remote channels still cannot authorise T3.")

    def notify(self, message, color=None) -> None:
        """Show a message, and remember it whether or not an icon exists yet.

        The last line is not an optimisation. `self._notice` and `self._color` are
        how a test (and the status line) can see what the user was told without a
        toast having to succeed, and a Windows notification that silently fails is
        a routine event, not an exception.
        """
        with self._lock:
            self._notice = str(message)
            self._color = color
        if not self.icon:
            return
        try:
            self.icon.notify(str(message), title=self.title)
        except Exception as exc:  # noqa: BLE001 - a toast failure is not fatal
            ipc.note(f"tray notify failed: {type(exc).__name__}: {exc}")

    def _open_prompt(self, icon=None, item=None) -> None:
        """The real prompt is the Phase 3 overlay, which does not exist yet.

        This says so rather than doing something else. A menu item that appears to
        work and quietly shows stale information is worse than one that admits the
        feature is not built - the user cannot debug a lie.
        """
        self.notify(
            "The prompt window is not built yet - that is Phase 3. "
            f"The hotkey {HOTKEY_LABEL} is live and marks you attended."
        )

    def build(self):
        import pystray

        image = _icon_image()
        # pystray invokes every action with (icon, item), so every callback must
        # accept those. Lambdas written as `lambda: ...` raise TypeError here, and
        # only when someone actually clicks, which is the worst time to find out.
        menu = pystray.Menu(
            pystray.MenuItem("Open prompt", self._open_prompt, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Summon ({HOTKEY_LABEL})", self._set_attended),
            pystray.MenuItem("Workspaces", self._workspaces),
            pystray.MenuItem("VRAM leases", self._vram),
            pystray.MenuItem("Verify audit chain", self._snapshot),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._quit),
        )
        self.icon = pystray.Icon(
            "locala", icon=image, title=self.title, menu=menu
        )
        return self.icon

    def _quit(self, icon=None, item=None) -> None:
        # Stop the icon first so the tray disappears immediately, then ask the
        # daemon to stop. Asking the daemon to stop while the icon is still running
        # would leave an icon on screen that no longer answers.
        try:
            if icon:
                icon.stop()
        finally:
            self.daemon.request_shutdown("tray exit")

    def start(self) -> "Tray":
        """Run pystray on a background thread.

        pystray's Windows backend runs a per-thread message loop, so a background
        thread gets its own pump and does not contend with the main thread's sleep
        loop. Calling `icon.run()` on the main thread would work too, but it would
        make the main thread the tray's, and then a tray crash would be a daemon
        crash. Keeping them separate is the whole point of the thread.
        """
        if self.icon is None:
            self.build()
        t = threading.Thread(
            target=self.icon.run, name="tray", daemon=True
        )
        t.start()
        self._thread = t
        return self

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001 - stopping a dead tray is fine
                pass
        if getattr(self, "_thread", None) and self._thread.is_alive():
            self._thread.join(timeout=2.0)
