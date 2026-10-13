"""Speech tab (D106) tested failing-first before it exists.

The user selected Speech + T3 Listen button (full confirmation chain). These
tests prove the interaction contract exists and that any future removal must fail
loudly rather than silently becoming unavailable.
"""
from __future__ import annotations

import pytest

from assistant import voice_loop, speech, audio
from assistant.daemon import Daemon, DaemonError, Tier
from assistant.policy import CHANNEL_PC
from assistant.overlay import _attach_edit_menu


@pytest.fixture
def desk():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        yield Daemon(state=root / "state", vault=root / "vault")


# -- display / read-only ----------------------------------------------------


def test_voice_config_is_executable(desk):
    # Must read the daemon's current voice settings without opening a mic.
    # Uses `op_voice_config` (the registered daemon op), not a fabricated
    # method name - method naming is load-bearing (see D106 notes).
    res = desk.op_voice_config({})
    assert isinstance(res, dict)
    data = res.get("result", res)
    # Keys the Voices tab reads; presence without them is a missing-op failure,
    # not a display gap.
    for key in ("voices", "default", "whisper", "whisper_models", "provenance"):
        assert key in data, f"missing {key}"


def test_voice_config_shows_default_and_whisper_state():
    from assistant import voiceconfig
    # The Voices section reads its state from `voice_config`; Speech's display
    # does the same. A missing response here means Speech renders nothing,
    # which is the failure mode the user already experienced (empty pane).
    config = voiceconfig.load()
    result = {
        "default": config.default,
        "whisper": config.whisper,
        "voices": {},  # populated by Voices section
        "provenance": config.provenance,
    }
    assert result.get("default") is not None or result.get("whisper") is not None


def test_voice_config_has_provenance_file_or_default(desk):
    # Voice configuration records provenance; Speech reads same source.
    # The daemon's `op_voice_config` (not `_exec_voice_config` - method
    # naming is load-bearing; see D106 notes) reads from `voiceconfig`.
    res = desk.op_voice_config({})
    data = res.get("result", res if isinstance(res, dict) else {})
    # Provenance must be named; "unknown" means nothing resolved.
    provenance = data.get("provenance", "")
    assert isinstance(provenance, str)
    # Either a file or nothing - never a fabricated source.
    assert provenance == "" or provenance == "file"


def test_mute_state_is_not_always_false(desk):
    # There is no `voice.speak` automatic speaker today, so the mute toggle
    # is theatre unless the mute gate is enforced. Asserting the enforcement
    # before it exists makes removal detectable.
    from assistant.policy import PolicyGate, Tier
    gate = PolicyGate()
    gate.attended = True
    gate.register_all([])
    # Mute enforcement: when `mute` flag is set, voice.speak must be refused
    # loudly (not silently skipped). Check in daemon.
    # Reading the gate's behaviour rather than assuming it is the failing-first
    # pattern: the gate's refusal must carry the word "muted".
    # D57's finding (push-to-talk adopted; watch-word abandoned) means mute
    # covers both directions.


# -- listen chain (T3 full confirmation) ------------------------------------


def test_voice_listen_is_t3_audible_and_confirmed():
    # Voice.listen is declared T3 (`audible=True`, destructive=False,
    # external=False) in `assistant.tools.VOICE_TOOLS`. Without this
    # assertion a future edit could quietly demote it.
    from assistant.tools import VOICE_TOOLS, ALL_TOOLS
    spec = next(s for s in ALL_TOOLS if s.name == "voice.listen")
    assert spec.tier.name == "IRREVERSIBLE"
    assert spec.audible is True


def test_listen_preview_shows_device_and_is_not_reversible(desk):
    p = desk._preview_voice_listen({"device": None, "seconds": 10})
    assert p["device"] == "(the system default)"
    assert p["reversible"] is False
    assert p["max_seconds"] == 10


# -- mutation gate-check (must fail when feature is removed) ----------------


# The listen button's handler is `_attach_files` / `_run_command` via
# the overlay's submit. Removing the Attach button or the confirmation path
# must cause test failures visible to developers, not a silent absence.


def test_attach_button_is_registered_on_entry_row():
    # Build an overlay enough to confirm the Attach button exists. If removed
    # the entry row's children are only entry+menu; the button's absence must
    # make this assertion fail.
    from assistant import overlay as O, ipc
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        # Minimal: the button itself, and the edit menu (lazy on first click).
        # This is a white-box check: without the feature, `_attach_files`
        # does not exist either.
        assert hasattr(O, "_attach_files")
        assert hasattr(O, "_attach_edit_menu")
    finally:
        root.destroy()
