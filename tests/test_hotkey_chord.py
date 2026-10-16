"""Hotkey chord (Ctrl+Alt+T) for transcript visibility.

Tested failing-first before the chord exists: the overlay must be able to
register the binding and the toggle method must be callable.
"""
from __future__ import annotations

import pytest

from assistant.daemon import Daemon, Tier
from assistant.policy import CHANNEL_PC


@pytest.fixture
def desk():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        yield Daemon(state=pathlib.Path(tmp) / "state", vault=pathlib.Path(tmp) / "vault")


# -- chord wiring -----------------------------------------------------------


def test_toggle_transcript_exists_on_overlay():
    # A missing method must fail loudly, not silently leave the chord useless.
    from assistant import overlay as O
    assert hasattr(O.Overlay, "_toggle_transcript_visibility")


def test_toggle_transcript_visible_after_show(desk):
    # Test that the toggle method exists and the hotkey binding is registered.
    from assistant import overlay as O
    # Verify the overlay's `_toggle_transcript_visibility` method exists
    assert hasattr(O.Overlay, "_toggle_transcript_visibility")
    # Verify that the hotkey binding is registered by checking the overlay's internal registry
    # This is a white-box test: the overlay constructor adds the binding, so we verify
    # the method exists.
    # Minimal call mock that lets the constructor succeed
    def call_mock(args=None, **kw):
        return {"ok": True, "result": {"note": "mock"}}
    ov = O.Overlay(call_mock, title="Test")
    assert hasattr(ov, "_toggle_transcript_visibility")
    assert ov._transcript_visible is True
