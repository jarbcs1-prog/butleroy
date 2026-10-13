"""Overlay typography storage: validated, headless, no Tk anywhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant import fontconfig as F


def test_no_file_means_no_change(tmp_path: Path):
    assert F.load(tmp_path / "state") is None


def test_a_broken_file_means_no_change(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "fonts.json").write_text("not json{", encoding="utf-8")
    assert F.load(state) is None


def test_any_bad_field_falls_back_whole(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    for bad in ({"family": "", "size": 12, "align": "left"},
                {"family": "Consolas", "size": 7, "align": "left"},
                {"family": "Consolas", "size": 49, "align": "left"},
                {"family": "Consolas", "size": "12", "align": "left"},
                {"family": "Consolas", "size": True, "align": "left"},
                {"family": "Consolas", "size": 12, "align": "justify"},
                ["not", "a", "dict"]):
        (state / "fonts.json").write_text(json.dumps(bad), encoding="utf-8")
        assert F.load(state) is None, f"{bad!r} should not apply"


def test_save_round_trips_and_returns_what_was_written(tmp_path: Path):
    saved = F.save(tmp_path / "state", "Cascadia Code", 12, "center")
    assert saved == {"family": "Cascadia Code", "size": 12, "align": "center"}
    assert F.load(tmp_path / "state") == saved


def test_save_refuses_loudly(tmp_path: Path):
    with pytest.raises(ValueError, match="family"):
        F.save(tmp_path / "state", "  ", 12, "left")
    with pytest.raises(ValueError, match="8\\.\\.48"):
        F.save(tmp_path / "state", "Consolas", 100, "left")
    with pytest.raises(ValueError, match="alignment"):
        F.save(tmp_path / "state", "Consolas", 12, "justify")
    assert not (tmp_path / "state" / "fonts.json").exists(), (
        "a refused save left a file behind")
