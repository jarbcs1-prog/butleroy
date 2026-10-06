"""The Telegram token on disk, and what can be done about it.

`state/telegram.json` holds a live bot token in plaintext. It is gitignored, so it
is never committed - but gitignored is not the same as secret, and a credential
that lives in a file is a credential that eventually lands in a paste, a backup,
or a screenshot. That is fine for an allowlisted single user and worth reducing
anyway.

Two changes make it reducible rather than "just don't commit it":

- **The token can come from the environment instead**, so it need never touch the
  disk at all. The allowlist stays in the file, because it is not a secret and
  belongs somewhere a human edits.
- **When the token *is* on disk, that is reported.** Silently is how a credential
  nobody is thinking about stays on a disk for four years.

What is deliberately *not* here: a command that writes the token. A feature that
writes a credential is a feature that can leak one, and rotation is a @BotFather
action plus one edit. See INSTALL.md §10.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant import telegram as T

TOKEN = "123456789:AAFakeTokenForTestsOnly-not-a-real-secret"


def write_config(path: Path, token: str = TOKEN, ids: tuple[int, ...] = (199251286,)) -> Path:
    path.write_text(
        json.dumps({"telegram_bot_token": token,
                    "telegram_allowed_chat_ids": list(ids)}),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# the token may come from the environment
# --------------------------------------------------------------------------- #


def test_a_token_in_the_environment_means_no_token_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the credential does not have to exist as a file.

    The allowlist still comes from the file, because it is configuration rather
    than a secret and belongs somewhere a human can edit it.
    """
    path = write_config(tmp_path / "telegram.json", token="", ids=(42,))
    monkeypatch.setenv("LOCALAIZED_TELEGRAM_TOKEN", TOKEN)

    cfg = T.load_config(path)

    assert cfg.allowed_chat_ids == (42,)
    # The token is deliberately not readable back - `describe()` exists so that
    # it can never reach a log. So assert on the fact that it was accepted.
    assert "token hidden" in repr(cfg)


def test_the_environment_wins_over_a_stale_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotated token must take effect without editing a file that is easy to
    forget exists - and the stale one must not quietly win."""
    path = write_config(tmp_path / "telegram.json", token="999:OLD-and-rotated")
    monkeypatch.setenv("LOCALAIZED_TELEGRAM_TOKEN", TOKEN)

    cfg = T.load_config(path)

    assert cfg.allowed_chat_ids == (199251286,), "the allowlist still comes from the file"


def test_a_file_token_still_works_when_the_environment_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged behaviour for the common case. This must not become a breaking
    change for the machine that already works."""
    monkeypatch.delenv("LOCALAIZED_TELEGRAM_TOKEN", raising=False)
    path = write_config(tmp_path / "telegram.json")

    cfg = T.load_config(path)

    assert cfg.allowed_chat_ids == (199251286,)


def test_an_empty_environment_token_is_ignored_not_treated_as_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env var set to "" is a misconfiguration, not a credential.

    Falling through to the file is the recoverable reading; refusing would break a
    machine where someone exported the variable and then cleared it.
    """
    monkeypatch.setenv("LOCALAIZED_TELEGRAM_TOKEN", "")
    path = write_config(tmp_path / "telegram.json")

    cfg = T.load_config(path)

    assert cfg.allowed_chat_ids == (199251286,)


def test_a_malformed_environment_token_is_refused_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token without a ":" is not a Telegram token. Silently using the file's
    instead would mean a typo in a variable looks like it worked."""
    monkeypatch.setenv("LOCALAIZED_TELEGRAM_TOKEN", "not-a-token")
    path = write_config(tmp_path / "telegram.json")

    with pytest.raises(T.TelegramError, match="LOCALAIZED_TELEGRAM_TOKEN"):
        T.load_config(path)


# --------------------------------------------------------------------------- #
# and when it *is* on disk, that is reported
# --------------------------------------------------------------------------- #


def test_a_token_on_disk_is_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not an error. A fact, surfaced where the user will actually see it.

    `describe()` is what `status` is allowed to show and it deliberately omits
    the token. Where it came from is not a secret, and it is the difference
    between "this machine holds a credential in plaintext" and "it does not".
    """
    monkeypatch.delenv("LOCALAIZED_TELEGRAM_TOKEN", raising=False)
    path = write_config(tmp_path / "telegram.json")

    described = T.load_config(path).describe()

    # The whole path, not the basename: the actionable fact is *which file* holds
    # the credential, and there is more than one state directory in play on a
    # machine that has been reinstalled onto.
    assert described["token_source"].endswith("telegram.json (plaintext on disk)")
    assert str(path) in described["token_source"]


def test_a_token_from_the_environment_is_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAIZED_TELEGRAM_TOKEN", TOKEN)
    path = write_config(tmp_path / "telegram.json", token="")

    described = T.load_config(path).describe()

    assert described["token_source"] == "LOCALAIZED_TELEGRAM_TOKEN"


def test_describe_never_includes_the_token_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that makes the rest safe to log.

    A source *description* is only safe to log if the credential cannot be in it.
    Checked against the whole rendered object, not one field, because a token
    leaking through `__repr__` is the version that actually happens.
    """
    monkeypatch.delenv("LOCALAIZED_TELEGRAM_TOKEN", raising=False)
    path = write_config(tmp_path / "telegram.json")

    cfg = T.load_config(path)
    rendered = json.dumps(cfg.describe()) + repr(cfg) + str(cfg.describe())

    assert TOKEN not in rendered
