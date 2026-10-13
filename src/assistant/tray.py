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
from typing import Callable

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

    def __init__(self, callback, *, label=HOTKEY_LABEL, vk=VK_SPACE,
                 mods=MOD_CONTROL | MOD_ALT, hotkey_id=HOTKEY_ID):
        self.callback = callback
        self.label = label
        self.vk = vk
        self.mods = mods | MOD_NOREPEAT
        # A per-instance id, so a caller that legitimately wants two chords does not
        # have to fight itself, and so tests can use distinct ids. Windows scopes a
        # NULL-hWnd registration to the *current thread*, so this is not what caused
        # the intermittent tray failures - see trigger() for that.
        self.hotkey_id = hotkey_id
        self.registered = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        # How many WM_HOTKEY messages the pump has actually dispatched. `trigger`
        # waits for this to move, because posting a message is not delivering it.
        self._seen = 0
        self._error: Exception | None = None

    def trigger(self, timeout: float = 2.0) -> bool:
        """Synthesise a press of this chord, as if the user had hit the key.

        This exists because it is the only *safe* way to test the pump, and because
        the tray's own "Summon" menu item should travel the identical path a real key
        press does rather than calling the callback directly.

        Hand-rolling this in a test means posting to `thread.ident`, and Windows
        recycles thread identifiers. A message addressed to a thread that has already
        exited can land in an unrelated, newly-created thread that happens to have
        been handed the same id - which is precisely the intermittent failure seen
        in the tray tests, and it is a race, not a flake in the test's logic. Going
        through the instance removes the guesswork: if the thread is gone, this
        returns False instead of misdelivering.

        `timeout` is how long to wait for the pump to actually consume the message.
        It is a parameter because the two callers want different things. The tray's
        "Summon" item wants a short one - a menu that hangs for ten seconds is
        worse than the fallback path it takes when this returns False, which is to
        set the session attended directly. A test on a loaded machine wants a long
        one, because what it is asserting is *delivery*, not latency, and a 2s
        budget measured on an idle desktop is not a fact about delivery.
        """
        if self._thread is None or not self._thread.is_alive():
            return False
        target = self._seen
        ident = self._thread.ident
        if not ident:
            return False
        # Explicit marshalling. Without argtypes, ctypes guesses, and the failure
        # mode is not an exception: GetMessageW on the pump thread simply never
        # sees the message, so the hotkey silently does nothing.
        posted = user32.PostThreadMessageW(
            ctypes.c_ulong(ident),
            ctypes.c_uint(WM_HOTKEY),
            ctypes.c_ulong(self.hotkey_id),
            ctypes.c_ulong(0),
        )
        if not posted:
            return False
        # Wait for the pump to actually consume it. Posting is not delivery: the
        # message can be accepted by a queue that never gets read, and "posted"
        # is not a claim the caller is entitled to make. The counter is
        # incremented on the pump thread inside the same branch that runs the
        # callback, so this cannot report success for a message that was dropped.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._seen >= target + 1:
                return True
            time.sleep(0.005)
        return False

    def start(self, timeout: float = 5.0) -> "Hotkey":
        # Register from this thread. The registration is per-thread on Windows, which
        # is precisely why the message pump below has to live in the same thread: a
        # WM_HOTKEY posted to a thread that did not register it is never delivered.
        if not user32.RegisterHotKey(None, self.hotkey_id, self.mods, self.vk):
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
                if (got > 0 and msg.message == WM_HOTKEY
                        and msg.wParam == self.hotkey_id):
                    self._seen += 1
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
            user32.UnregisterHotKey(None, self.hotkey_id)
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

    def __init__(
        self,
        daemon,
        *,
        idle_after: int = 300,
        poll: float = 2.0,
        retry_after: float = 60.0,
        seconds_since: Callable[[], int] | None = None,
    ):
        self.daemon = daemon
        self.idle_after = idle_after
        self.poll = poll
        # Presence is read from the OS, which is the whole point (D30) -- and also
        # the reason the monitor is untestable by default. A test that lets it read
        # real input state passes only while a human is at the keyboard, and fails
        # for anyone else, so it asserts on who happens to be sitting there rather
        # than on the logic. Injected the same way VramArbiter takes its snapshot
        # function: the default is the real thing, tests pass a fake.
        self.seconds_since = seconds_since or seconds_since_input
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
            op="session_set_attended",
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
                idle = self.seconds_since()
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


def _pending_count(res) -> int:
    """How many confirmations are waiting. Never raises, never lies up.

    A malformed reply is 0 with no toast: a watcher that cries wolf about a
    shape it did not understand trains the user to ignore the real one.
    """
    try:
        result = (res or {}).get("result") or {}
        pending = result.get("pending") or []
        return len(pending) if isinstance(pending, list) else 0
    except Exception:  # noqa: BLE001 - a watcher must not raise
        return 0


class WatchPending:
    """Toast when confirmations start waiting on the user. Read-only polling.

    The overlay interrupts with the dialog itself; this is for when it is
    hidden. Only a rising edge toasts: a steady or falling count is not news,
    and a dead daemon stays quiet rather than alarming about being unreached.
    Approval stays in the overlay - this announces, it never approves.
    """

    def __init__(self, call, on_rise, *, poll_s: float = 15.0) -> None:
        # `call` is the tray's pipe call, so every poll goes through
        # `daemon.handle` with actor=tray and lands in the audit chain like
        # any other client read. A watcher with a daemon reference instead
        # would be the second front door the module docstring forbids.
        self._call = call
        self._on_rise = on_rise
        # Human timescale: a confirmation waits minutes, not milliseconds.
        # Fifteen seconds notices promptly without chattering the pipe, and
        # `confirm_list` is a read, so no audit record is written per poll.
        self.poll_s = poll_s
        self._last: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _check(self) -> int:
        try:
            res = self._call("confirm_list")
        except Exception:  # noqa: BLE001 - down daemon, stay quiet
            return self._last if self._last is not None else 0
        if not isinstance(res, dict) or not res.get("ok"):
            return self._last if self._last is not None else 0
        n = _pending_count(res)
        if self._last is not None and n > self._last:
            self._on_rise(
                f"{n} confirmation{'s' if n != 1 else ''} waiting — "
                f"answer in the prompt window."
            )
        self._last = n
        return n

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            self._check()

    def start(self) -> "WatchPending":
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="watch-pending", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)


def _icon_image(size: int = 64):
    """The tray icon: the Butleroy bell, drawn rather than shipped as a binary.

    Drawn rather than loaded from `brand/icons/tray.png`, and that is not a
    preference for reviewability over convenience - it is that `src/` is the only
    directory that gets packaged, so a PNG in `brand/` would simply not exist for
    an installed user and the tray would fall over or silently show nothing. The
    geometry is therefore `assistant.brand`, in the package, and this file and
    `brand/tools/render_icons.py` are two views of one shape.

    Mono, because Windows composes tray icons against an unpredictable
    background and a filled shape with a hard edge survives that where a lit
    gradient turns to mud.
    """
    from .brand import tray_image

    return tray_image(size)


class Tray:
    """The tray icon.

    Every menu item goes through `daemon.handle`, the same path a pipe peer uses.
    There is no privileged shortcut from the icon to the filesystem: the tray is a
    *client* of the daemon, not a second front door. That is the property worth
    holding, because a tray that can do more than the pipe is a tray that nobody
    audits.
    """

    def __init__(self, daemon, *, title=f"Butleroy {__version__}",
                 hotkey=None):
        self.daemon = daemon
        self.title = title
        # Optional, and deliberately so: the tray must work with no hotkey at all
        # (headless, or a second instance that lost the chord to an IME), rather
        # than refusing to start.
        self.hotkey = hotkey
        self.icon = None
        # The prompt window, created on first use rather than at construction.
        # D31 again: it is a client of the daemon like everything else here, and
        # it never gets to approve a confirmation - that is the overlay's job and
        # only the overlay's.
        self._overlay = None
        # The settings window, same reasoning (D75): built on first use, owned here
        # so closing it releases its Tk interpreter, and a client of the daemon like
        # the overlay. A settings window that could approve a confirmation would
        # make two windows able to answer the same dialog, and only one of them is
        # the one the user was looking at.
        self._settings = None
        self._notice: str | None = None
        self._color: tuple | None = None
        self._lock = threading.Lock()
        # The pending-confirmation watcher, started with the icon. None until
        # `start()` so a tray that is only ever driven by tests never grows a
        # thread, and headless runs (which return before `start()`) never poll.
        self._watch: WatchPending | None = None

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
        res = self.call("audit_verify")
        ok = bool(res.get("ok")) and res["result"].get("ok")
        n = res["result"].get("checked", 0) if res.get("ok") else 0
        self.notify(
            f"Audit chain {'VERIFIES' if ok else 'BROKEN'} - {n} records",
            None if ok else (255, 90, 90),
        )

    def _vram(self, icon=None, item=None) -> None:
        res = self.call("arbiter_summary")
        self.notify(res.get("result") if res.get("ok") else str(res.get("error")),
                    None if res.get("ok") else (255, 90, 90))

    def _workspaces(self, icon=None, item=None) -> None:
        res = self.call("workspace_list")
        if not res.get("ok"):
            self.notify(str(res.get("error")), (255, 90, 90))
            return
        names = res["result"].get("workspaces") or []
        self.notify(
            f"{len(names)} workspace(s) approved: {', '.join(names)}"
            if names
            else "No workspace approved. The assistant cannot read any project yet.",
        )

    def _waiting(self, icon=None, item=None) -> None:
        """How many confirmations are waiting on the user, on demand.

        The watcher toasts on a rising edge; this answers when asked. Either
        way the tray only announces: approval is the overlay's job (see the
        D31 note on `_overlay` above), and a second window able to answer the
        same dialog is the defect the settings window was kept away from.
        """
        try:
            res = self.call("confirm_list")
        except Exception as exc:  # noqa: BLE001 - down daemon, say so plainly
            self.notify(f"Waiting list unavailable: {type(exc).__name__}: {exc}")
            return
        if not isinstance(res, dict) or not res.get("ok"):
            self.notify("Waiting list unavailable.")
            return
        n = _pending_count(res)
        if n == 0:
            self.notify("Nothing waiting on you.")
        else:
            self.notify(
                f"{n} confirmation{'s' if n != 1 else ''} waiting — "
                f"answer in the prompt window."
            )

    def _set_attended(self, icon=None, item=None) -> None:
        """Summon, from the menu.

        If a Hotkey is attached, it goes *through* it, so the menu item and the
        keyboard chord execute the same code path. A menu item with its own private
        route to the callback is a second, untested way to be summoned.
        """
        if self.hotkey is not None and self.hotkey.trigger():
            return
        self.call("session_set_attended", attended=True, why="summoned from the tray")
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

    def open_prompt(self) -> None:
        """Raise the prompt window, from anything that holds a tray.

        Public because the launcher's hotkey callback needs it, and reaching into
        ``_open_prompt`` from another module would be a second thing that can
        change without the tray's tests noticing. The pystray menu item still goes
        through ``_open_prompt`` because pystray calls every action with
        ``(icon, item)``.
        """
        self._open_prompt()

    def _open_prompt(self, icon=None, item=None) -> None:
        """Raise the prompt window.

        Built lazily, on first use, because importing tkinter and constructing a
        Tk root on a machine with no interactive session costs seconds and then
        throws them away - and this tray is supposed to be able to run headless.
        """
        try:
            if self._overlay is None:
                from . import overlay as overlay_mod

                self._overlay = overlay_mod.Overlay(self.call, title=self.title)
            self._overlay.show()
        except Exception as exc:
            # A tray that cannot open a window still has to say so, rather than
            # the menu item appearing to work and doing nothing.
            self.notify(f"The prompt window could not open: {exc}")

    def _open_settings(self, icon=None, item=None) -> None:
        """Open the settings window.

        Built lazily and owned by the tray, for the same reasons as
        `_open_prompt`: importing tkinter and constructing a root costs seconds on a
        machine with no interactive session, and this tray has to be able to run
        headless.

        It talks to the daemon through `self.call`, like everything else here. A
        settings window holding a daemon reference would be a second front door into
        the policy gate, which is the defect D59 is about.
        """
        try:
            if self._settings is None:
                from . import settings as settings_mod

                self._settings = settings_mod.Settings(self.call, title=self.title)
            self._settings.show()
        except Exception as exc:
            # A tray item that appears to work and does nothing is worse than one
            # that says why, which is the same reason as `_open_prompt`.
            self.notify(f"The settings window could not open: {exc}")

    def open_settings(self) -> None:
        """Public, for a hotkey or anything else holding a tray.

        Same reason `open_prompt` is public: reaching into `_open_settings` from
        another module is a second thing that can change without the tray's tests
        noticing.
        """
        self._open_settings()

    def build(self):
        import pystray

        image = _icon_image()
        # pystray invokes every action with (icon, item), so every callback must
        # accept those. Lambdas written as `lambda: ...` raise TypeError here, and
        # only when someone actually clicks, which is the worst time to find out.
        menu = pystray.Menu(
            pystray.MenuItem("Open prompt", self._open_prompt, default=True),
            pystray.MenuItem("Settings…", self._open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Summon ({HOTKEY_LABEL})", self._set_attended),
            pystray.MenuItem("Waiting on you", self._waiting),
            pystray.MenuItem("Workspaces", self._workspaces),
            pystray.MenuItem("VRAM leases", self._vram),
            pystray.MenuItem("Verify audit chain", self._snapshot),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._quit),
        )
        # The name pystray registers the icon under. It is the identity the
        # platform sees, so it is the product name (D73) - not the package name.
        self.icon = pystray.Icon(
            "butleroy", icon=image, title=self.title, menu=menu
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
        self._watch = WatchPending(self.call, self.notify).start()
        return self

    def stop(self) -> None:
        # The watcher goes first: it polls the daemon, and everything below
        # is about releasing what the poller might still touch.
        if self._watch is not None:
            try:
                self._watch.stop()
            except Exception:  # noqa: BLE001 - stopping is best-effort
                pass
            self._watch = None
        # The prompt window goes next. `Overlay` owns a Tk thread and a Tcl
        # interpreter, and nothing else in the process knows about either - so a
        # tray that stops its icon and leaves the window standing leaves a live
        # thread and a live interpreter behind, on every shutdown. With the daemon
        # now restartable (D64) a restart is a routine event rather than a
        # one-off, which turns that leak from rare to once a day.
        if self._overlay is not None:
            try:
                self._overlay.destroy()
            except Exception:  # noqa: BLE001 - a window that will not close is not
                pass             # worth refusing to shut the rest down over
            self._overlay = None
        # The settings window, same reason and same failure mode. It has its own Tk
        # thread and its own Tcl interpreter, so leaving it standing leaks exactly
        # as the overlay did - and D69's exit code is what a leak looks like from
        # outside: a process that shut down cleanly and reported `-2147483645`.
        if self._settings is not None:
            try:
                self._settings.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._settings = None
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001 - stopping a dead tray is fine
                pass
        if getattr(self, "_thread", None) and self._thread.is_alive():
            self._thread.join(timeout=2.0)
