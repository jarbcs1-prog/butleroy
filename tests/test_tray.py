"""
Tests for the tray, the hotkey and presence detection.

Two of these tests do real things to the real desktop session: they register a
global hotkey and they post a message to a thread's queue. That is deliberate. A
mocked RegisterHotKey proves the mock works; it cannot prove that the chord is
free, that the message pump is on the same thread as the registration, or that the
dispatch actually reaches the callback - and each of those is exactly the kind of
thing that fails silently and only shows up as "the assistant sometimes doesn't
come when I press the key".
"""

from __future__ import annotations

import ctypes
import threading
import time

import pytest

from assistant import ipc, tray
from assistant.tray import (
    HOTKEY_ID,
    MOD_ALT,
    MOD_CONTROL,
    VK_SPACE,
    WM_HOTKEY,
    Hotkey,
    HotkeyUnavailable,
    Presence,
    Tray,
    seconds_since_input,
)


# --- the OS primitives --------------------------------------------------------


def test_seconds_since_input_is_a_sane_number():
    idle = seconds_since_input()
    assert isinstance(idle, int)
    # A test machine has been up longer than 60s. A negative or absurd value would
    # mean the struct marshalling is wrong, which is the classic way this call lies.
    assert 0 <= idle < 60 * 60 * 24 * 365


def test_seconds_since_input_does_not_advance_instantly():
    # If dwTime or GetTickCount64 were mis-marshalled we would see wild jitter. Two
    # reads microseconds apart must be within a second of each other.
    a = seconds_since_input()
    b = seconds_since_input()
    assert abs(a - b) <= 1


def test_the_hotkey_chord_is_available_in_a_normal_session():
    """Ctrl+Alt+Space must be registrable without elevation.

    This is the property that rules out the `keyboard` library. If it ever fails
    here, the cause is an IME that has taken the chord, and the daemon must say so
    rather than start unsummonable.
    """
    from assistant.tray import user32

    if not user32.RegisterHotKey(None, 0xB99, MOD_CONTROL | MOD_ALT, VK_SPACE):
        pytest.skip("Ctrl+Alt+Space is owned by another program on this machine")
    user32.UnregisterHotKey(None, 0xB99)


# --- the hotkey object --------------------------------------------------------


def test_hotkey_registers_and_unregisters_cleanly():
    fired = threading.Event()
    hk = Hotkey(lambda: fired.set())
    hk.start()
    assert hk.registered
    hk.stop()
    assert not hk.registered


def test_a_second_registration_of_the_same_chord_fails_loudly():
    """Two daemons must be a visible error, not a silently dead hotkey."""
    first = Hotkey(lambda: None)
    first.start()
    try:
        second = Hotkey(lambda: None)
        with pytest.raises(HotkeyUnavailable) as exc:
            second.start()
        assert "already owns it" in str(exc.value)
    finally:
        first.stop()


def test_a_raising_callback_does_not_kill_the_message_pump():
    """The pump is the only thing that can report the hotkey.

    If a callback exception unwound the loop, the assistant would become
    permanently unsummonable and the failure would look like "the hotkey is
    flaky" rather than a bug.
    """
    calls = []

    def bad():
        calls.append(1)
        raise RuntimeError("callback exploded")

    hk = Hotkey(bad)
    hk.start()
    try:
        _post_hotkey(hk, times=2)
        time.sleep(0.4)
    finally:
        hk.stop()
    assert len(calls) >= 2, "the pump stopped after the first exception"


def _post_hotkey(hk: Hotkey, times: int = 1) -> None:
    """Press the chord, without synthesising a keystroke.

    Delegates to `Hotkey.trigger()` rather than posting to `hk._thread.ident` by
    hand. That indirection is the point: Windows recycles thread identifiers, so a
    message addressed to a thread that has already exited can be delivered to an
    unrelated thread that was later handed the same id. Hand-posting is therefore a
    race, and it showed up as two tests that failed only in a full-suite run and
    passed in isolation. The instance knows whether its own thread is still alive,
    and returns False instead of misdelivering.

    The real registration path is covered by the tests above, which do call
    RegisterHotKey.
    """
    for _ in range(times):
        assert hk.trigger(), "the pump thread is not accepting messages"


def test_posting_a_hotkey_message_reaches_the_callback():
    fired = threading.Event()
    hk = Hotkey(lambda: fired.set())
    hk.start()
    try:
        _post_hotkey(hk)
        assert fired.wait(3.0), "the message pump never dispatched WM_HOTKEY"
    finally:
        hk.stop()


def test_an_unrelated_message_is_ignored():
    """A message we did not register for must not trigger the callback."""
    from assistant.tray import user32

    fired = threading.Event()
    hk = Hotkey(lambda: fired.set())
    hk.start()
    try:
        assert hk._thread is not None
        user32.PostThreadMessageW(hk._thread.ident, 0x0001, HOTKEY_ID, 0)  # WM_CREATE
        user32.PostThreadMessageW(hk._thread.ident, WM_HOTKEY, HOTKEY_ID + 99, 0)
        assert not fired.wait(0.8)
    finally:
        hk.stop()


def test_stopping_twice_is_not_an_error():
    hk = Hotkey(lambda: None)
    hk.start()
    hk.stop()
    hk.stop()


# --- presence -----------------------------------------------------------------


class FakeDaemon:
    """Records what the presence monitor asked for.

    Deliberately not the real Daemon: this test is about *when* the monitor speaks,
    and the real daemon has a whole file of opinions about sessions.
    """

    def __init__(self, *, ok=True):
        self.ok = ok
        self.calls = []
        self._lock = threading.Lock()

    def handle(self, request):
        with self._lock:
            self.calls.append(request)
        return {"ok": self.ok, "result": {}}


def test_presence_marks_attended_when_someone_is_typing():
    d = FakeDaemon()
    p = Presence(d, idle_after=300, poll=0.05)
    p.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not d.calls:
            time.sleep(0.05)
    finally:
        p.stop()
    assert d.calls, "presence never spoke"
    first = d.calls[0]
    assert first.op == "session.set_attended"
    assert first.args["attended"] is True
    assert first.args["channel"] == "pc"


def test_presence_does_not_repeat_itself_while_nothing_changes():
    """A monitor that calls every 2s forever would flood the audit chain."""
    d = FakeDaemon()
    p = Presence(d, idle_after=300, poll=0.02)
    p.start()
    time.sleep(0.5)
    p.stop()
    assert len(d.calls) == 1, f"expected one transition, got {len(d.calls)}"


def test_presence_ignores_its_own_result():
    """If the daemon refuses, the monitor must not retry into a hot loop.

    Retrying a refused transition every poll interval is how a bug turns into a
    busy loop against a subsystem that is already unhappy.
    """
    d = FakeDaemon(ok=False)
    p = Presence(d, idle_after=300, poll=0.02)
    p.start()
    time.sleep(0.4)
    p.stop()
    assert len(d.calls) == 1, "a refused transition was retried"


def test_presence_reports_absence_after_the_idle_window(monkeypatch):
    idle = {"value": 10_000}
    monkeypatch.setattr(tray, "seconds_since_input", lambda: idle["value"])
    d = FakeDaemon()
    p = Presence(d, idle_after=300, poll=0.02)
    idle["value"] = 10_000  # nobody has touched the keyboard
    p.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not d.calls:
            time.sleep(0.05)
    finally:
        p.stop()
    assert d.calls
    assert d.calls[0].args["attended"] is False


def test_presence_uses_the_daemons_audited_path(monkeypatch):
    """It must go through handle(), not reach into the daemon.

    A presence monitor that flips `daemon.attended` directly would be invisible in
    the audit chain, which is the one place this is supposed to be provable.
    """
    seen = []

    class Recorder(FakeDaemon):
        def handle(self, request):
            seen.append(request.op)
            return {"ok": True, "result": {}}

    p = Presence(Recorder(), idle_after=300, poll=0.02)
    p.start()
    time.sleep(0.3)
    p.stop()
    assert seen == ["session.set_attended"]


# --- the tray -----------------------------------------------------------------


class StubDaemon:
    def __init__(self):
        self.requests = []
        self.shutdown_why = None

    def handle(self, request):
        self.requests.append(request)
        if request.op == "status":
            return {
                "ok": True,
                "result": {
                    "version": "9.9.9",
                    "attended": True,
                    "tools_registered": 60,
                    "tools_executable": ["file.read"],
                },
            }
        if request.op == "audit.verify":
            return {"ok": True, "result": {"ok": True, "checked": 12}}
        if request.op == "arbiter.summary":
            return {"ok": True, "result": "no leases; 1.5 GiB reserved for you"}
        if request.op == "workspace.list":
            return {"ok": True, "result": {"workspaces": []}}
        return {"ok": True, "result": {}}

    def request_shutdown(self, why):
        self.shutdown_why = why


def test_the_tray_is_a_client_of_the_daemon_not_a_second_door():
    d = StubDaemon()
    t = Tray(d)
    t.call("status")
    assert [r.op for r in d.requests] == ["status"]
    assert d.requests[0].args["actor"] == "tray"
    assert d.requests[0].args["channel"] == "pc"


def test_every_tray_action_went_through_handle():
    """No menu item may touch the filesystem or the gate directly."""
    d = StubDaemon()
    t = Tray(d)
    t._set_attended()
    t._workspaces()
    t._vram()
    t._snapshot()
    ops = {r.op for r in d.requests}
    assert ops == {
        "session.set_attended",
        "workspace.list",
        "arbiter.summary",
        "audit.verify",
    }


def test_the_summon_item_marks_attended_with_a_reason():
    d = StubDaemon()
    Tray(d)._set_attended()
    req = d.requests[0]
    assert req.args["attended"] is True
    assert "tray" in req.args["why"]


def test_an_empty_workspace_list_says_so_instead_of_looking_healthy():
    """Zero approved workspaces is the safe state and should read as empty, not ok."""
    d = StubDaemon()
    t = Tray(d)
    t._workspaces()
    assert "No workspace approved" in t._notice


def test_a_broken_audit_chain_is_reported_in_red():
    class Broken(StubDaemon):
        def handle(self, request):
            if request.op == "audit.verify":
                return {"ok": True, "result": {"ok": False, "checked": 9}}
            return super().handle(request)

    t = Tray(Broken())
    t._snapshot()
    assert "BROKEN" in t._notice
    assert t._color == (255, 90, 90)


def test_a_healthy_chain_is_reported_plainly():
    t = Tray(StubDaemon())
    t._snapshot()
    assert "VERIFIES" in t._notice
    assert "12 records" in t._notice


def test_status_line_reflects_attended_state():
    class Unattended(StubDaemon):
        def handle(self, request):
            if request.op == "status":
                return {
                    "ok": True,
                    "result": {"version": "9.9.9", "attended": False},
                }
            return super().handle(request)

    assert "unattended" in Tray(Unattended())._status()
    assert "attended" in Tray(StubDaemon())._status()


def test_the_open_prompt_item_admits_the_overlay_is_not_built():
    """It must not fake success. A menu item that lies is undebuggable."""
    t = Tray(StubDaemon())
    t._open_prompt()
    assert "Phase 3" in t._notice
    assert "not built yet" in t._notice.lower() or "not built" in t._notice.lower()


def test_quit_stops_the_icon_then_asks_the_daemon():
    class Icon:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    d = StubDaemon()
    t = Tray(d)
    icon = Icon()
    t._quit(icon)
    assert icon.stopped
    assert d.shutdown_why == "tray exit"


def test_the_icon_image_is_a_real_image():
    from PIL import Image

    img = tray._icon_image(64)
    assert isinstance(img, Image.Image)
    assert img.size == (64, 64)
    assert img.mode == "RGBA"
    # An all-transparent icon is invisible, which looks exactly like a broken tray.
    assert img.getbbox() is not None, "the icon is entirely transparent"
    flat = img.convert("RGB")
    colours = {flat.getpixel((x, y)) for x in range(0, 64, 2) for y in range(0, 64, 2)}
    assert len(colours) >= 3, f"the icon is a flat colour: {colours}"


def test_the_menu_actually_builds():
    """Constructing the real pystray menu catches the (icon, item) arity mistake.

    A callback written as `lambda: ...` builds fine and fails only when clicked.
    """
    t = Tray(StubDaemon())
    icon = t.build()
    assert icon is not None
    # Walk the menu and assert every non-separator item is clickable with the
    # (icon, item) signature pystray uses.
    def walk(items, depth=0):
        for it in items:
            if getattr(it, "sub_menu", None):
                walk(it.sub_menu.items if hasattr(it.sub_menu, "items") else [], depth + 1)
            elif getattr(it, "action", None) is not None and it.text != "":
                import inspect

                sig = inspect.signature(it.action)
                assert len(sig.parameters) >= 2 or any(
                    p.kind is inspect.Parameter.VAR_POSITIONAL
                    for p in sig.parameters.values()
                ), f"menu item {it.text!r} cannot accept (icon, item)"

    walk(icon.menu.items)
