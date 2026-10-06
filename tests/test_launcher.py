"""The one command that starts the product.

`daemon.py`, `tray.py`, `overlay.py` and `brain_server.py` are all built and all
tested, and between them nothing was connected: `Tray` and `Overlay` were
referenced from tests only, so the repository could pass 1025 tests while the
assistant had no way to be *started*. Phase 2's gate is "hotkey opens the
overlay with no terminal visible", and that gate was unreachable because no
module owned the process.

This file is the seam. It asserts on composition - what gets built, in what
order, and what happens when a piece cannot be built - rather than on pystray or
tkinter, which are not importable in a headless assertion. The factory seam is
the project's own convention: `Overlay` takes its `call` injected precisely "so
the tests can drive this whole module with no tkinter display and no daemon at
all".

The behaviours that matter are the product's promises, so those are what is
asserted: the chord opens the window, a taken chord does not take the assistant
down with it, and nothing appears on screen before the daemon is actually
serving.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant import ipc
from assistant.daemon import Daemon
from assistant.launcher import App
from assistant.tray import HotkeyUnavailable

# Named pipes vanish when the last handle closes, but a crashed run can leave one
# behind briefly, so every test binds a name nothing else will ask for.
_COUNTER = iter(range(1, 10_000))


def unique_pipe(tag: str) -> str:
    return rf"\\.\pipe\locala.test.launcher.{tag}.{next(_COUNTER)}"


# --------------------------------------------------------------------------- #
# stand-ins
# --------------------------------------------------------------------------- #


class FakeTray:
    """Records what the launcher asked of it, in order."""

    instances: list["FakeTray"] = []

    def __init__(self, daemon, *, hotkey=None, **kw):
        self.daemon = daemon
        self.hotkey = hotkey
        self.events: list[str] = []
        self.opened = 0
        self.started = False
        self.stopped = False
        self.notices: list[str] = []
        FakeTray.instances.append(self)

    def start(self):
        self.started = True
        self.events.append("start")
        return self

    def stop(self):
        self.stopped = True
        self.events.append("stop")

    def open_prompt(self, *a, **kw):
        self.opened += 1

    def notify(self, message, color=None):
        self.notices.append(str(message))


class FakeHotkey:
    """Captures the callback so a test can press the chord."""

    def __init__(self, callback, **kw):
        self.callback = callback
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.stopped = True

    def press(self):
        return self.callback()


class FakePresence:
    def __init__(self, daemon, **kw):
        self.daemon = daemon
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.stopped = True


def taken_hotkey(callback, **kw):
    """A factory standing in for Ctrl+Alt+Space already owned by an IME.

    This is not hypothetical: `tray.HotkeyUnavailable` documents the chord as a
    popular IME toggle on several East Asian editors, and the documented failure
    mode of getting this wrong is a daemon that starts and can never be summoned.
    """
    raise HotkeyUnavailable("Ctrl+Alt+Space is already registered")


@pytest.fixture
def tray_factory():
    FakeTray.instances = []
    return FakeTray


# --------------------------------------------------------------------------- #
# the chord opens the window
# --------------------------------------------------------------------------- #


def test_the_hotkey_opens_the_prompt_window(tmp_path: Path, tray_factory) -> None:
    """The Phase 2 gate, stated as behaviour: Ctrl+Alt+Space opens the overlay.

    Asserted by pressing the chord through the callback the launcher registered,
    not by checking that an object was constructed. A launcher that built a hotkey
    and wired it to nothing would pass a construction test.
    """
    hotkeys: list[FakeHotkey] = []

    def hotkey_factory(callback, **kw):
        hk = FakeHotkey(callback, **kw)
        hotkeys.append(hk)
        return hk

    app = App(
        Daemon(state=tmp_path / "state"),
        pipe=unique_pipe("chord"),
        tray_factory=tray_factory,
        hotkey_factory=hotkey_factory,
        presence_factory=FakePresence,
    )
    app.start()
    try:
        assert hotkeys and hotkeys[0].started, "the chord must be registered"
        hotkeys[0].press()
        assert tray_factory.instances[0].opened == 1
    finally:
        app.stop()


# --------------------------------------------------------------------------- #
# a chord we cannot have is not a reason to be absent
# --------------------------------------------------------------------------- #


def test_a_taken_chord_does_not_stop_the_assistant_from_starting(
    tmp_path: Path, tray_factory
) -> None:
    """The tray must still come up, and must say why the chord is missing.

    Losing the chord is an inconvenience; losing the tray is an assistant the
    user cannot see at all. The user has to be told which of the two happened -
    a daemon that silently starts with no way to summon it reads as broken.
    """
    app = App(
        Daemon(state=tmp_path / "state"),
        pipe=unique_pipe("taken"),
        tray_factory=tray_factory,
        hotkey_factory=taken_hotkey,
        presence_factory=FakePresence,
    )
    app.start()
    try:
        tray = tray_factory.instances[0]
        assert tray.started, "the tray must come up without the chord"
        assert tray.hotkey is None
        assert any("Ctrl+Alt+Space" in n for n in tray.notices), (
            "the user must be told the chord is unavailable and why"
        )
    finally:
        app.stop()


# --------------------------------------------------------------------------- #
# ordering: nothing appears before the daemon is serving
# --------------------------------------------------------------------------- #


def test_the_daemon_is_bound_before_the_tray_appears(
    tmp_path: Path, tray_factory
) -> None:
    """A tray icon for a daemon that failed to bind is a lie about a running
    service.

    If the pipe bind fails - another daemon already owns this state directory -
    the user must get a failure, not an icon that answers menu clicks against a
    process that is not serving.
    """
    state = tmp_path / "state"
    pipe = unique_pipe("order")
    incumbent = Daemon(state=state)
    incumbent.start_serving(pipe)
    try:
        FakeTray.instances = []
        app = App(
            Daemon(state=state),
            pipe=pipe,
            tray_factory=tray_factory,
            hotkey_factory=FakeHotkey,
            presence_factory=FakePresence,
        )
        with pytest.raises(OSError):
            app.start()
        assert FakeTray.instances == [], (
            "no tray may appear for a daemon that could not bind"
        )
    finally:
        incumbent.shutdown()


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #


def test_every_component_starts_and_stops_with_the_app(
    tmp_path: Path, tray_factory
) -> None:
    app = App(
        Daemon(state=tmp_path / "state"),
        pipe=unique_pipe("life"),
        tray_factory=tray_factory,
        hotkey_factory=FakeHotkey,
        presence_factory=FakePresence,
    )
    app.start()
    tray = tray_factory.instances[0]
    presence = app.presence
    hotkey = app.hotkey
    assert tray.started and presence.started and hotkey.started

    app.stop()

    assert tray.stopped, "a stopped tray must leave no icon behind"
    assert presence.stopped
    assert hotkey.stopped


def test_stopping_twice_is_safe(tmp_path: Path, tray_factory) -> None:
    """The tray's Exit item and the hotkey thread can both ask.

    `Tray._quit` calls `daemon.request_shutdown` and stops the icon, and the
    daemon's own serve loop calls `shutdown` in a finally. Shutdown therefore
    runs more than once on a normal exit, and a second `stop()` that raised
    would surface as a traceback on the way out of a process that was working.
    """
    app = App(
        Daemon(state=tmp_path / "state"),
        pipe=unique_pipe("twice"),
        tray_factory=tray_factory,
        hotkey_factory=FakeHotkey,
        presence_factory=FakePresence,
    )
    app.start()
    app.stop()
    app.stop()  # must not raise


def test_a_stopped_app_leaves_no_credential_behind(
    tmp_path: Path, tray_factory
) -> None:
    """The D64 invariant, held at the level the user actually starts from.

    Stopping the assistant should look like the assistant is not running, and the
    test that proves it is the one a user runs by pressing Exit.
    """
    state = tmp_path / "state"
    app = App(
        Daemon(state=state),
        pipe=unique_pipe("cred"),
        tray_factory=tray_factory,
        hotkey_factory=FakeHotkey,
        presence_factory=FakePresence,
    )
    app.start()
    assert (state / "authkey.bin").exists()

    app.stop()

    assert not (state / "authkey.bin").exists()


def test_stopping_the_app_tears_down_the_window_it_opened(
    tmp_path: Path, tray_factory
) -> None:
    """The prompt window must be destroyed on the way out, not merely abandoned.

    `Tray._open_prompt` lazily builds an `Overlay`, and an `Overlay` owns a Tk
    thread and a Tcl interpreter. `Tray.stop()` stopped the pystray icon and
    nothing else, so every shutdown - and with D64 fixed, a restart is now a
    normal event rather than a one-off - left a live Tk thread and a live
    interpreter behind. Threads that outlive their owner are how a 24/7 daemon
    accumulates.

    Asserted through the *real* `Tray`, because the leak is in `Tray.stop()` and
    a stand-in tray would stop having the bug the moment the fixture changed.
    """
    from assistant import overlay as overlay_mod
    from assistant import tray as tray_mod

    built: list = []

    class RecordingOverlay:
        def __init__(self, call, *, title=""):
            built.append(self)
            self.shown = False
            self.destroyed = False

        def show(self):
            self.shown = True

        def destroy(self):
            self.destroyed = True

    # `tray._open_prompt` does `from . import overlay as overlay_mod` at call time,
    # so the patch has to land on the module itself rather than on tray's namespace.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(overlay_mod, "Overlay", RecordingOverlay)
    try:
        real_tray = tray_mod.Tray(Daemon(state=tmp_path / "state"))
        real_tray.open_prompt()          # the chord's path
        assert len(built) == 1 and built[0].shown

        real_tray.stop()

        assert built[0].destroyed, (
            "the overlay must be destroyed when the tray stops; otherwise its Tk "
            "thread and Tcl interpreter outlive the daemon"
        )
    finally:
        monkey.undo()


def test_the_daemon_serves_its_pipe_while_the_ui_runs(
    tmp_path: Path, tray_factory
) -> None:
    """The product promise in one test: the UI is up *and* the pipe answers.

    Assembled from the real daemon, the real pipe and the real authkey on disk -
    only the tray and the chord are stand-ins. Before this the pipe was served by
    `python -m assistant.daemon` and the UI was reachable by no route at all,
    which is why both halves are asserted together.
    """
    state = tmp_path / "state"
    app = App(
        Daemon(state=state),
        pipe=unique_pipe("serves"),
        tray_factory=tray_factory,
        hotkey_factory=FakeHotkey,
        presence_factory=FakePresence,
    )
    app.start()
    try:
        client = ipc.PipeClient(
            app.pipe, authkey=ipc.load_authkey(state / "authkey.bin"), timeout=20
        )
        assert client.call("ping").get("result") == "pong"
    finally:
        app.stop()
