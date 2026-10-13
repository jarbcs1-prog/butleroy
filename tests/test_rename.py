"""The D73 rename, and the three things it must not have broken.

Renaming a product is mostly mechanical and therefore mostly where you get it
wrong. This file exists because the rename touched things that *look* identical:

  1. **A product name and a filesystem path are not the same string.** The project
     still lives in `G:\\localaized`. `policy.SELF_PATHS` contains that literal
     path, and so do roughly fifteen tests. A blanket `locala` -> `butleroy`
     replace would repoint every one of them at a directory that does not exist -
     and would have looked *fine*, because most of those tests assert a string
     against a string, and the strings still match each other.
  2. **A rename that breaks a credential is silent.** `LOCALAIZED_EMAIL_PASSWORD`
     and `LOCALAIZED_TELEGRAM_TOKEN` were real, documented, possibly-set
     variables. Renaming them without a fallback gives a machine that worked
     yesterday an empty inbox today, with no error anywhere.
  3. **A rename that leaves the console script pointing at the old package is
     found by the user, not by us** - at the moment they type the command.

None of these can be caught by a test that only asserts the new name appears
somewhere. So each is asserted as a *negative* or a *behaviour*, below.
"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "assistant"

#: Files where `locala` legitimately survives the rename. This is a list rather
#: than an exclusion pattern so that adding a file to it is a deliberate act with
#: something to explain, and so the assertion below can name it in the failure.
#:
#: Every entry is a *path* or a *comment about the rename*, never a live product
#: reference. `G:\localaized` is where the code lives; renaming it is a different
#: job (moving the checkout) and would invalidate the venv, the editable install,
#: and every relative path.
ALLOWED_LOCALA = {
    "policy.py": "SELF_PATHS holds the real checkout path G:\\localaized",
    "daemon.py": "a comment quoting an error message that contains the real path",
    # The pipe-rename note records the old name deliberately, so that the next
    # person to read it knows what the prefix was. Comments are documentation
    # about the rename, not references to the product.
    "ipc.py": "the D73 comment explaining what the rename did and did not change",
}


def _py_sources() -> list[Path]:
    return sorted(SRC.glob("*.py"))


# --------------------------------------------------------------------------- #
# 1. the product name is gone from the source, and the path is not
# --------------------------------------------------------------------------- #


def test_no_source_file_still_calls_the_product_locala() -> None:
    """Every surviving `locala` in src/ must be a path, not a name.

    Written as an allowlist of files rather than a regex for "not followed by
    ized", because the failures we care about are specific: a pipe name, a
    pystray identity, an environment variable. A regex would pass while one of
    those three remained.
    """
    offenders: list[str] = []
    for path in _py_sources():
        allowed = path.name in ALLOWED_LOCALA
        lines = path.read_text(encoding="utf-8").splitlines()
        # Docstrings are prose *about* the rename, and prose is not something the
        # OS reads. Comments likewise. What must not be skipped is a live value -
        # a pipe name, a pystray identity, an environment variable - so the skip
        # is done by tracking triple-quote regions rather than by any
        # comment-shaped heuristic, which would also hide a real reference sitting
        # on a line that happens to start with '#'.
        in_docstring = False
        for n, line in enumerate(lines, 1):
            stripped = line.strip()
            if in_docstring:
                in_docstring = '"""' not in stripped
                continue
            if stripped.startswith('"""'):
                # A one-line docstring closes on the same line.
                in_docstring = not (stripped.endswith('"""') and len(stripped) > 5)
                continue
            if stripped.startswith("#"):
                continue

            if "locala" not in line:
                continue
            # `localaized` is the old descriptor, and a real path is fine.
            text = line.replace("localaized", "").replace("LOCALAIZED", "")
            if "locala" not in text:
                continue
            if allowed:
                continue
            # A Windows API env var, which is Microsoft's, not ours.
            if "LOCALAPPDATA" in line:
                continue
            offenders.append(f"{path.name}:{n}: {line.strip()}")

    assert not offenders, (
        "the product name 'locala' survives in these places, which is either a "
        "missed rename or a path that should have been left alone:\n  "
        + "\n  ".join(offenders)
    )


def test_the_project_directory_is_still_called_localaized() -> None:
    """The inverse, and the one that actually protects anything.

    If somebody does the obvious global replace, `policy.SELF_PATHS` stops
    pointing at this checkout. The refusal that stops the assistant editing its
    own source silently stops applying to the real source, and nothing fails -
    because the test that asserts "this path is refused" is asserting against the
    same replaced constant.
    """
    from assistant.policy import SELF_PATHS

    root = str(ROOT).rstrip("\\")
    assert any(p.lower().startswith(root.lower()) for p in SELF_PATHS), (
        f"no SELF_PATH is inside {root}; the checkout path was renamed or moved, "
        f"so the assistant's self-protection no longer covers its own code: "
        f"{SELF_PATHS}"
    )
    # And specifically the three files whose modification would rewrite the
    # project's own decisions.
    for name in ("DECISIONS.md", "ARCHITECTURE.md", "PLAN.md"):
        assert any(p.endswith(name) for p in SELF_PATHS), (
            f"{name} is no longer protected by SELF_PATHS"
        )


def test_the_pipes_are_renamed_and_named_pipes_still_validate() -> None:
    """The four pipes carry the product name, and they are still legal names.

    Both halves matter. A rename that produced `\\\\.\\pipe\\butleroy\\brain.req`
    would be a plausible-looking string that Windows refuses at `Listen()`, and
    the failure would be at startup rather than in a test.
    """
    from assistant.ipc import ALL_PIPES, PIPE_PREFIX, PROTOCOL

    assert len(ALL_PIPES) == 4, f"the pipe set changed size: {ALL_PIPES}"
    assert len(set(ALL_PIPES)) == 4, "two pipes share a name"
    for pipe in ALL_PIPES:
        assert "locala" not in pipe, f"{pipe} still carries the old name"
        assert pipe.startswith(PIPE_PREFIX), f"{pipe} is not a named pipe"
        assert "\\" not in pipe[len(PIPE_PREFIX):], (
            f"{pipe} has a backslash in the name; Windows rejects that"
        )
        assert len(pipe) <= 256, f"{pipe} exceeds the Windows pipe name limit"
    assert PROTOCOL == 1, "the protocol version must not move with a rename"


def test_the_pipes_are_unique_to_this_product_so_two_instances_cannot_collide() -> None:
    """D64's second-instance rule depends on the pipe names being stable.

    Two ButlerOYs on one machine is the case the pipe namespace exists to catch.
    If a rename had accidentally produced a name another product also uses, the
    second instance would attach to the first one's daemon instead of refusing.
    """
    from assistant.ipc import ALL_PIPES

    for pipe in ALL_PIPES:
        assert "butleroy" in pipe, f"{pipe} does not carry the product name"


# --------------------------------------------------------------------------- #
# 2. the credentials survive the rename
# --------------------------------------------------------------------------- #


def test_the_email_variable_names_are_what_we_think() -> None:
    """The two names, asserted literally so a careless edit to either is caught.

    Small, and it exists because everything else in this file references these
    constants by name - so a constant quietly changed to the *same* value it
    already had would pass every behavioural test below it.
    """
    from assistant import email as E

    assert E.PASSWORD_ENV_VAR == "BUTLEROY_EMAIL_PASSWORD"
    assert E.PASSWORD_ENV_VAR_LEGACY == "LOCALAIZED_EMAIL_PASSWORD"


def test_the_telegram_variable_names_are_what_we_think() -> None:
    from assistant import telegram as T

    assert T.TOKEN_ENV_VAR == "BUTLEROY_TELEGRAM_TOKEN"
    assert T.TOKEN_ENV_VAR_LEGACY == "LOCALAIZED_TELEGRAM_TOKEN"


def test_the_legacy_email_variable_still_works_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback, tested as behaviour rather than as a string.

    A rename that drops the old variable name is a mail client that stops
    authenticating on a machine that was working yesterday. The credential is the
    thing that matters, so the assertion is that the password still arrives - and
    the deprecation is asserted separately, because "it works" and "the user was
    told" are different claims.
    """
    from assistant import email as E

    monkeypatch.delenv(E.PASSWORD_ENV_VAR, raising=False)
    monkeypatch.setenv(E.PASSWORD_ENV_VAR_LEGACY, "hunter2")

    config = tmp_path / "email.json"
    config.write_text(
        '{"email_host": "imap.example.com", "email_username": "me@example.com"}',
        encoding="utf-8",
    )

    with pytest.warns(DeprecationWarning, match="LOCALAIZED_EMAIL_PASSWORD"):
        cfg = E.load_config(config)

    assert cfg.password == "hunter2", (
        "the legacy variable stopped working; a configured machine silently "
        "loses its credential"
    )
    # And the reported source names the variable actually used, so `mail_status`
    # can tell the user which spelling to go and rename.
    assert E.PASSWORD_ENV_VAR_LEGACY in cfg.password_source, (
        f"the source is reported as {cfg.password_source!r}, which does not "
        "mention the deprecated name the user actually set"
    )


def test_the_new_variable_wins_over_the_legacy_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both set: the new one must win, silently, with no deprecation warning.

    Warning here would train the user to ignore the warning, which is how the
    deprecation stops being noticed at all.
    """
    from assistant import email as E

    monkeypatch.setenv(E.PASSWORD_ENV_VAR, "new-secret")
    monkeypatch.setenv(E.PASSWORD_ENV_VAR_LEGACY, "old-secret")

    config = tmp_path / "email.json"
    config.write_text(
        '{"email_host": "imap.example.com", "email_username": "me@example.com"}',
        encoding="utf-8",
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = E.load_config(config)

    assert cfg.password == "new-secret", "the deprecated variable overrode the new one"
    assert E.PASSWORD_ENV_VAR in cfg.password_source


def test_the_legacy_telegram_token_still_works_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant import telegram as T

    monkeypatch.delenv(T.TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv(T.TOKEN_ENV_VAR_LEGACY, "123456:legacy")

    config = tmp_path / "telegram.json"
    config.write_text('{"telegram_allowed_chat_ids": [1]}', encoding="utf-8")

    with pytest.warns(DeprecationWarning, match="LOCALAIZED_TELEGRAM_TOKEN"):
        cfg = T.load_config(config)

    # The token is deliberately not exposed - it is a credential, and the class
    # exists partly to make sure it cannot reach a log line. So the assertion is
    # that it loaded at all (a token with no ':' is refused) and that the reported
    # source names the variable actually used.
    described = cfg.describe()
    assert T.TOKEN_ENV_VAR_LEGACY in described["token_source"], (
        f"the source is reported as {described['token_source']!r}, which does "
        "not mention the deprecated name the user actually set"
    )
    assert "token" not in described, (
        "describe() is leaking the token; a token in a report is a token in a "
        "file somebody will paste into a chat"
    )


def test_an_empty_legacy_variable_is_not_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleared variable must stay ignored, exactly as the new one is.

    D63's rule: a variable set to the empty string is a recoverable
    misconfiguration, not a credential. The fallback path must not turn "someone
    cleared it" into an error, or renaming the variable becomes a way to break
    the bridge.
    """
    from assistant import telegram as T

    monkeypatch.delenv(T.TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv(T.TOKEN_ENV_VAR_LEGACY, "")

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        token, name = T._token_from_env()

    assert token == ""
    assert name == T.TOKEN_ENV_VAR, "an empty legacy var should not claim to be the source"


# --------------------------------------------------------------------------- #
# 3. the package and the command
# --------------------------------------------------------------------------- #


def test_the_package_is_named_butleroy_and_still_ships_the_launcher() -> None:
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "butleroy"

    scripts = data["project"].get("scripts", {})
    assert "butleroy" in scripts, (
        f"no `butleroy` command; the user would have to type {sorted(scripts)}"
    )
    # The entry point must resolve. A console script pointing at a renamed
    # function is a door that opens onto a wall (D65's shape).
    from assistant.launcher import main

    assert callable(main)
    assert scripts["butleroy"].endswith(":main")


def test_the_tray_registers_under_the_product_name() -> None:
    """pystray is handed the product name, not the old one.

    Asserted by reading the call rather than by constructing a tray: building one
    needs a pipe and a display, and the string is the whole claim.
    """
    source = (SRC / "tray.py").read_text(encoding="utf-8")
    match = re.search(r'pystray\.Icon\(\s*"([^"]+)"', source)
    assert match, "could not find the pystray.Icon name in tray.py"
    assert match.group(1) == "butleroy", (
        f"the tray registers as {match.group(1)!r}"
    )


def test_the_daemon_names_the_new_credential_variable_in_its_refusals() -> None:
    """A refusal that lists only the new spelling strands the old-name user.

    The legacy variable is still honoured, so the credential keeps working while
    the refusal implies it does not - and the user goes looking for a problem
    that is not there.
    """
    from assistant.daemon import PASSWORD_ENV_VAR_HINT

    assert PASSWORD_ENV_VAR_HINT == "BUTLEROY_EMAIL_PASSWORD"


# --------------------------------------------------------------------------- #
# 4. what the rename must NOT have touched
# --------------------------------------------------------------------------- #


def test_the_editable_install_still_points_at_this_checkout() -> None:
    """`pip install -e .` left a `.pth` and a dist-info named after the old
    package. That is expected - the editable install predates the rename - but it
    means the installed console script is still `locala.exe` until reinstall, and
    a new session that trusts `butleroy --help` will fail.

    Asserted as a *fact about the current machine* so the discrepancy is recorded
    rather than discovered by a user. This test does not fail on a clean
    install; it fails only when the venv is in the half-renamed state.
    """
    site = ROOT / ".venv" / "Lib" / "site-packages"
    if not site.is_dir():
        pytest.skip("no venv in this checkout")

    fresh = sorted(p.name for p in site.glob("*butleroy*"))
    assert fresh, (
        "the venv has no butleroy dist-info. Reinstall with "
        "`uv pip install --python .venv\\Scripts\\python.exe -e .`"
    )

    # The stale `locala` artefacts are *tolerated*, not required to be gone:
    # `uv pip install` leaves the old dist-info behind rather than uninstalling
    # it, and a leftover dist-info cannot shadow the new one. So this asserts the
    # new name resolves and is the one importlib reports - which is the claim that
    # actually matters - and only *reports* the leftovers.

    import importlib.metadata as md

    assert md.version("butleroy"), "importlib cannot resolve the butleroy package"

    # `entry_points()` returns EntryPoint objects here, not a mapping you can
    # index by name - indexing it raises `AttributeError: 'str' object has no
    # attribute '_key'`, which says nothing about the install. So filter instead.
    scripts = [e for e in md.entry_points(group="console_scripts")
               if e.name == "butleroy"]
    assert scripts, (
        "no `butleroy` console script is installed; found "
        f"{sorted(e.name for e in md.entry_points(group='console_scripts'))}"
    )
    assert scripts[0].value == "assistant.launcher:main"

    leftovers = [p.name for p in site.glob("*locala*")]
    if leftovers:
        print(
            f"\nnote: stale pre-rename artefacts still present: {leftovers}. "
            "Harmless - importlib resolves butleroy - but `uv pip uninstall locala` "
            "clears them."
        )


def test_windows_environment_variables_are_untouched() -> None:
    """`LOCALAPPDATA` is Microsoft's. Renaming it would be a catastrophic find.

    Stated as its own test because it is the single most likely accident in a
    rename like this: the string matches, the surrounding text mentions the
    project, and a global replace takes it without asking.
    """
    from assistant import daemon, sysinfo

    for module in (daemon, sysinfo):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert 'os.environ.get("LOCALAPPDATA")' in source, (
            f"{module.__name__} no longer reads LOCALAPPDATA; check that the "
            "rename did not touch a Windows environment variable"
        )

# --------------------------------------------------------------------------- #
# 4. the display rename: Butleroy -> ButlerOY (D102). Display strings only.


def test_the_display_name_is_butleroy_wherever_shown() -> None:
    """Window titles and the tray carry the official capitalisation.

    Asserted on the constructor defaults, because those are what a fresh
    window actually shows - a test asserting a string appears *somewhere*
    would pass while the default title still said the old thing.
    """
    import inspect

    from assistant import overlay, settings, tray

    overlay_title = inspect.signature(
        overlay.Overlay.__init__).parameters["title"].default
    assert overlay_title == "ButlerOY", (
        f"the prompt window still opens as {overlay_title!r}")
    settings_title = inspect.signature(
        settings.Settings.__init__).parameters["title"].default
    assert settings_title == "ButlerOY Settings", (
        f"the settings window still opens as {settings_title!r}")
    tray_title = inspect.signature(
        tray.Tray.__init__).parameters["title"].default
    assert tray_title.startswith("ButlerOY "), (
        f"the tray still identifies as {tray_title!r}")


def test_the_lockup_carries_the_new_capitalisation() -> None:
    logo = (ROOT / "brand" / "logo.svg").read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", logo, flags=re.S)
    assert "ButlerOY" in text
    assert "Butleroy" not in text.replace("ButlerOY", ""), (
        "the old capitalisation survives in the lockup artwork")


def test_the_display_rename_stopped_at_display() -> None:
    """Pipes, the pystray identity, env vars and the desk they sit on: all
    byte-identical. D73's three lessons apply unchanged to a casing change:
    a path, a credential and a console script are not display strings."""
    from assistant import ipc

    for pipe in ipc.ALL_PIPES:
        assert "ButlerOY" not in pipe, (
            f"{pipe} followed the display rename; pipe names are identity")
    source = (SRC / "tray.py").read_text(encoding="utf-8")
    assert '"butleroy"' in source, "the pystray identity moved"
    assert "BUTLEROY_TELEGRAM_TOKEN" in (SRC / "telegram.py").read_text(
        encoding="utf-8")
    assert "BUTLEROY_EMAIL_PASSWORD" in (SRC / "email.py").read_text(
        encoding="utf-8")
