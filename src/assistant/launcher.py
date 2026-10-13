"""The one command that starts the product.

Everything in Plane A already existed and none of it was connected. ``daemon.py``
served a named pipe, ``tray.py`` built a tray icon, a chord and a presence
monitor, ``overlay.py`` built the prompt window and the T3 dialog - and no module
imported any of them from another. ``Tray`` and ``Overlay`` appeared in
``tests/`` only. So the repository could pass 1025 green tests while the
assistant had no route to being started, which is the specific failure this
module exists to close.

It is deliberately thin. There is no new logic here: the daemon still holds the
gate, the leases and the chain, and the tray is still a *client* of the daemon
rather than a second front door (``tray.Tray`` says so, and that property is the
one worth protecting). What this file does is decide the order, and hold the one
invariant the order exists for.

The order, and why it is this order
-----------------------------------
1. **Bind the pipe first.** The daemon must be genuinely serving before anything
   appears on screen. A tray icon for a daemon that lost the race to another
   instance is a lie about a running service: the menu would answer, and the
   answers would be nothing.
2. **Then the chord, the presence monitor, the tray.** The chord is registered
   before the tray so that the icon cannot appear without the summons that
   belongs to it.
3. **Then park**, and let whoever asked - the tray's Exit item, a signal, a
   second instance giving up - stop the daemon.

Why the chord is allowed to fail
--------------------------------
``tray.HotkeyUnavailable`` is not hypothetical: Ctrl+Alt+Space is a popular IME
toggle on several East Asian input method editors, and the documented failure
mode of treating that as fatal is a daemon that starts and can never be summoned
- which the user experiences as a broken assistant rather than as a conflict. So
losing the chord costs the chord, says so, and keeps everything else running.

Nothing here imports a model runtime. Plane A stays at a few hundred MB with no
VRAM (D1), and a broken model build still cannot take the assistant down.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from . import ipc
from .daemon import STATE, Daemon
from .manifest import TIER_BY_NAME
from .obsidian import resolve_vault_root
from .tray import Hotkey, HotkeyUnavailable, Presence, Tray

__all__ = ["App", "main", "build_parser"]


def _ceiling(name: str | None):
    """Turn ``--ceiling T2`` into a Tier, refusing anything else by name.

    argparse cannot express "one of these four" as a type without reimplementing
    the check, and a typo here would otherwise be silently ignored - which for a
    *ceiling on what the assistant may do unattended* is the wrong way to be
    wrong. The valid set is printed on failure.
    """
    if name is None:
        return TIER_BY_NAME["T3"]
    if name not in TIER_BY_NAME:
        raise SystemExit(
            f"--ceiling must be one of {sorted(TIER_BY_NAME)}; got {name!r}"
        )
    return TIER_BY_NAME[name]


class App:
    """One resident process: the daemon, and the desktop furniture around it.

    The three GUI/OS pieces are injected as factories whose defaults are the real
    ones. That is the project's convention rather than a testing convenience -
    ``overlay.Overlay`` takes its ``call`` injected for exactly the same reason,
    "so the tests can drive this whole module with no tkinter display and no
    daemon at all" - and it is what makes the composition assertable without a
    display, a registered chord, or a tray.
    """

    def __init__(
        self,
        daemon: Daemon,
        *,
        pipe: str = ipc.PIPE_BRAIN_REQ,
        tray_factory: Callable[..., Any] = Tray,
        hotkey_factory: Callable[..., Any] = Hotkey,
        presence_factory: Callable[..., Any] = Presence,
        headless: bool = False,
    ) -> None:
        self.daemon = daemon
        self.pipe = pipe
        self._tray_factory = tray_factory
        self._hotkey_factory = hotkey_factory
        self._presence_factory = presence_factory
        self.headless = headless
        self.tray: Any = None
        self.hotkey: Any = None
        self.presence: Any = None
        self._lock = threading.Lock()
        self._stopped = False

    # -- lifecycle ---------------------------------------------------------

    def open_prompt(self) -> None:
        """Raise the prompt window. The chord's whole job."""
        if self.tray is not None:
            self.tray.open_prompt()

    def start(self) -> None:
        """Bind the daemon, then bring up the desktop furniture.

        Raises whatever binding raised, and raises it *before* any component is
        built, so a refused start leaves no icon, no chord and no credential
        behind - the same rule D64 established for the state directory.
        """
        self.daemon.start_serving(self.pipe)

        hotkey = None
        try:
            hotkey = self._hotkey_factory(self.open_prompt).start()
        except HotkeyUnavailable as exc:
            # An assistant the user cannot see is worse than one they can see but
            # cannot summon, so this is survivable. It is also *visible*, because
            # "why did my shortcut stop working" has a different answer from
            # "why did the assistant stop working" and the user cannot tell which
            # one they are looking at otherwise.
            hotkey_error: str | None = str(exc)
        else:
            hotkey_error = None

        presence = self._presence_factory(self.daemon).start()

        if self.headless:
            # For a machine with no interactive session, and for `--check`. The
            # daemon is the product; the furniture is not.
            self.presence = presence
            self.hotkey = hotkey
            return

        tray = self._tray_factory(self.daemon, hotkey=hotkey)
        try:
            tray.start()
        except BaseException:
            # Never leave a chord registered and a presence thread polling for a
            # tray that is not there.
            hotkey.stop()
            presence.stop()
            self.daemon.shutdown()
            raise
        if hotkey_error:
            tray.notify(
                f"{hotkey.label if hotkey else 'The shortcut'} could not be "
                f"registered: {hotkey_error}. The assistant is running, but the "
                f"chord will not summon it - open the prompt from the tray icon.",
                color="red",
            )

        self.tray = tray
        self.hotkey = hotkey
        self.presence = presence

    def stop(self, why: str = "stopped") -> None:
        """Tear everything down. Safe to call more than once.

        The tray's Exit item and the daemon's serve loop both ask, so a normal
        shutdown runs this twice. A second call that raised would surface as a
        traceback on the way out of a process that was working fine.
        """
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            # The daemon first: it owns the gate and the chain, and the tray is
            # its client, so it must not outlive the thing it talks to.
            self.daemon.request_shutdown(why)
            self.daemon.shutdown()
        if self.tray is not None:
            self.tray.stop()
        if self.hotkey is not None:
            self.hotkey.stop()
        if self.presence is not None:
            self.presence.stop()

    def run(self) -> None:
        """Start, then park until something asks the daemon to stop."""
        self.start()
        try:
            while not self.daemon._halt.wait(3600):  # noqa: SLF001
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop("interrupted" if sys.stdin.isatty() else "exited")


def build_parser() -> argparse.ArgumentParser:
    """Flags for `python -m assistant`.

    argparse rather than the hand-rolled loop in ``daemon.main``: that one is
    four flags for a module that is not a general-purpose CLI, and this one is the
    user-facing command, where a typo should print usage rather than be ignored.
    """
    p = argparse.ArgumentParser(
        prog="python -m assistant",
        description="The resident assistant: daemon, tray icon, "
        f"Ctrl+Alt+Space, and the prompt window.",
    )
    p.add_argument("--state", type=Path, default=STATE,
                   help="state directory (default: %(default)s)")
    p.add_argument("--vault", type=Path, default=None,
                   help="Obsidian vault root")
    p.add_argument("--pipe", default=ipc.PIPE_BRAIN_REQ,
                   help="named pipe to serve (default: %(default)s)")
    p.add_argument("--check", action="store_true",
                   help="print the preflight and exit without starting anything")
    p.add_argument("--headless", action="store_true",
                   help="serve the pipe with no tray, chord or prompt window")
    p.add_argument("--unattended", action="store_true",
                   help="start in the unattended state")
    p.add_argument("--ceiling", default=None,
                   help="session ceiling, T0..T3 (default: T3)")
    p.add_argument("--idle-after", type=int, default=300,
                   help="seconds of no input before the session is unattended")
    return p


def main(argv: list[str] | None = None) -> int:
    opts = build_parser().parse_args(argv)

    d = Daemon(
        state=opts.state,
        vault=resolve_vault_root(opts.state, opts.vault),
        attended=not opts.unattended,
        session_ceiling=_ceiling(opts.ceiling),
    )
    if opts.check:
        # Nothing is started, so nothing must be left behind - in particular no
        # credential, because a key on disk means a daemon is running (D64).
        print(json.dumps(d.preflight(), indent=2, default=str))
        return 0

    app = App(d, pipe=opts.pipe, headless=opts.headless)
    app.start()
    try:
        while not d._halt.wait(3600):  # noqa: SLF001
            pass
    except KeyboardInterrupt:
        pass
    finally:
        app.stop("exited")
    return 0
