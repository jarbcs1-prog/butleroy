"""The settings window, driven on a real Tk.

D67's lesson, applied: a `Tk()` that is never pumped still maps a window with the
right title and geometry, `winfo_viewable` is 1, and it paints nothing — because
painting *is* event processing. 1025 tests once passed against exactly that. So
none of the assertions here are "the window was created"; they are about what ended
up in the widget tree and what the daemon was actually asked.

No `TkFake` here on purpose. A hand-built fake for tkinter is a week of work that
passes everything except the parts that break, and the parts that break are
`padding`, `highlightthickness`, `relief` and the event loop.
`test_overlay_real_dialog.py` is the same trade, and is what caught D69.

**One window per test.** A shared module-scoped window was tried and abandoned: a
shared fixture has to guarantee its own preconditions, and the ways it failed were
`KeyError: 'models'` in whichever test happened to run next — an error naming the
fixture rather than the cause. Isolated windows are boring and deterministic.

**This module is run inside a subprocess by `test_settings_isolated.py`.** Not for
speed. In this Python/Tk build, a worker-thread Tk root that has been created and
destroyed can leave Tcl's async handler to be deleted at interpreter shutdown, on
the main thread — `Tcl_AsyncDelete: async handler deleted by the wrong thread`, and
the process exits `-2147483645` *after every test has passed*. It is not a leak: the
release tests below assert nothing is retained, bare tkinter handles forty such
roots without complaint, and the count at which it appears is not stable. What is
stable is that a Tcl interpreter outliving its owning thread is this environment's
problem, not a property of the product. Containing it in a subprocess means it is
*observable* (a non-zero exit code, reported) instead of silently turning a green run
into a red one.
"""

from __future__ import annotations

import os

import pytest

from assistant import settings as S

tk = pytest.importorskip("tkinter")


def _display_available() -> bool:
    """Whether a Tk root can actually be created here.

    Probed rather than assumed, so an unavailable display skips instead of erroring
    halfway through a test that has already built half a window.

    In-process runs are for development and for this module being run directly.
    The normal suite runs it through `test_settings_isolated.py`, in a subprocess —
    see that file, and this module's docstring, for why.
    """
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        return False
    root.destroy()
    return True


requires_display = pytest.mark.skipif(
    not _display_available(), reason="no interactive session for Tk"
)

#: This module is not collected by the normal suite. `test_settings_isolated.py` runs
#: it in a subprocess, because a Tcl interpreter that outlives its owning thread
#: aborts at interpreter *shutdown* in this build - after every test has passed -
#: and turns a green run red with no summary at all. Running in-process here is for
#: development; set `SETTINGS_TK_INPROCESS=1` only if you are debugging these tests.
if not os.environ.get("SETTINGS_TK_INPROCESS"):
    pytestmark = pytest.mark.skip(reason="run by test_settings_isolated.py")


class FakeDaemon:
    """A daemon-shaped `call`, recording what it was asked.

    Real enough that the window's two ops are exercised end to end, and recording
    because the interesting failure is a window that never asks — which looks
    exactly like a window that is still loading.
    """

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._fail = fail
        self.tiers = {
            "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * 1024 ** 3},
            "deep": {"model": "large-IQ4_XS.gguf", "need_bytes": 7 * 1024 ** 3},
        }
        self.models = [
            {"name": "small-Q4_0.gguf", "bytes": 512, "est_vram_bytes": 604},
            {"name": "large-IQ4_XS.gguf", "bytes": 4096, "est_vram_bytes": 4833},
        ]
        self.provenance = "file"
        self.saved: dict | None = None

    def __call__(self, op: str, /, **args) -> dict:
        self.calls.append((op, dict(args)))
        if self._fail is not None:
            raise self._fail
        if op == "model_config":
            return {"ok": True, "result": {
                "tiers": self.tiers,
                "models": self.models,
                "provenance": self.provenance,
                "resolved": {n: f"G:/localaized/models/{t['model']}"
                             for n, t in self.tiers.items()},
            }}
        if op == "model_config_set":
            self.saved = dict(args)
            return {"ok": True, "result": {
                "saved": r"G:\localaized\state\models.json",
                "tiers": args.get("tiers"),
                "provenance": "file",
                "note": "a tier already loaded keeps its model until the daemon "
                        "next starts a child; nothing was restarted",
                "brain_running": False,
            }}
        return {"ok": True, "result": {}}


@pytest.fixture
def win():
    """A real window for one test, torn down on the Tk thread whatever happens."""
    made: list[S.Settings] = []

    def build(daemon: FakeDaemon | None = None) -> S.Settings:
        w = S.Settings(daemon or FakeDaemon(), title="Butleroy Settings")
        made.append(w)
        w.show()
        return w

    yield build

    for w in made:
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# it opens, and it is painted
# --------------------------------------------------------------------------- #


@requires_display
def test_the_window_opens_and_is_pumped(win) -> None:
    """Mapped *and* rendered, which are different claims.

    `winfo_viewable` alone is the D67 shape. So this also asserts the event loop is
    actually running by requiring a round trip through it — a window that is mapped
    but never pumped cannot answer `_invoke`.
    """
    w = win()

    assert w.is_open
    assert w._root is not None
    assert w._root.winfo_viewable() == 1, "the window is not mapped"
    assert w._root.title() == "Butleroy Settings"
    assert w._invoke(lambda: "pumped") == "pumped"


@requires_display
def test_the_window_is_sized_for_the_content(win) -> None:
    """Geometry is a string, so a typo in it is silent.

    `update_idletasks()` first, and the values are read *once*. Without the settle,
    the first `winfo_reqwidth()` returned a stale 33 while a second call moments
    later returned the real 669 — and an assertion whose message re-reads the value
    prints the second number while failing on the first, which reads like the test
    contradicting itself.
    """
    w = win()
    w._root.update_idletasks()
    width = w._root.winfo_reqwidth()
    height = w._root.winfo_reqheight()

    assert "Butleroy" in w._root.title()
    assert width > 400, (
        f"the window requested only {width}px wide; a mis-specified geometry "
        "string is silent"
    )
    assert height > 300, f"the window requested only {height}px high"


@requires_display
def test_opening_it_twice_gives_one_window(win) -> None:
    """A tray menu item is a click, and people click things twice.

    Three windows stacked at the same coordinates is a bug report nobody can
    screenshot usefully, and the fix is one idempotency check.
    """
    w = win()
    first = w._root
    w.show()
    w.show()
    assert w._root is first, "show() built a second window"


# --------------------------------------------------------------------------- #
# it asks the daemon for the configuration
# --------------------------------------------------------------------------- #


@requires_display
def test_opening_asks_the_daemon_for_the_configuration(win) -> None:
    """The window does not read the file. It cannot — it has no path to it.

    Asserted because a window that read `state/models.json` itself would be a second
    front door (D59) *and* would not see the daemon's degraded view, which is the one
    fact the user most needs.
    """
    w = win()
    ops = [op for op, _ in w._call.calls]
    assert "model_config" in ops, f"the window opened without asking: {ops}"


@requires_display
def test_the_tiers_are_rendered_into_the_list(win) -> None:
    w = win()
    text = "\n".join(w._sections["models"].listbox.get(0, "end"))

    assert "fast" in text and "deep" in text, text
    assert "small-Q4_0.gguf" in text, text
    # The lease is what the user is actually choosing, so it has to be on screen.
    assert "1.0 GiB" in text, (
        f"the lease ceiling is not shown, so the row does not say what it costs: "
        f"{text!r}"
    )
    # And so does the model's real size, or the row cannot answer "will this fit".
    assert "on disk" in text, f"the row does not say how big the model is: {text!r}"


@requires_display
def test_provenance_is_always_visible_not_only_on_trouble(win) -> None:
    """The user must be able to tell their settings from the built-in defaults.

    This is the whole reason `provenance` is on every response rather than only on
    error. A window that showed defaults silently would let someone believe they had
    configured something they had not.
    """
    w = win()
    assert "models.json" in w._sections["models"].status.cget("text")


@requires_display
def test_defaults_are_labelled_as_defaults(win) -> None:
    daemon = FakeDaemon()
    daemon.provenance = "defaults"
    w = win(daemon)

    status = w._sections["models"].status.cget("text")
    assert "default" in status.lower(), (
        f"with provenance='defaults' the status does not say so: {status!r}"
    )


@requires_display
def test_a_degraded_config_is_shown_to_the_user_not_swallowed(win) -> None:
    """The corrupt-file case, end to end through the window.

    The daemon degrades to the defaults and says why in `provenance`; the window has
    to put that in front of the user. A window that showed the defaults without the
    reason is a window where the user believes their settings took.
    """
    daemon = FakeDaemon()
    daemon.provenance = (
        "defaults+unreadable: JSONDecodeError: Expecting property name")
    w = win(daemon)

    status = w._sections["models"].status.cget("text")
    assert "JSONDecodeError" in status, (
        f"the reason the config was ignored is not shown: {status!r}"
    )


# --------------------------------------------------------------------------- #
# saving
# --------------------------------------------------------------------------- #


@requires_display
def test_saving_sends_the_whole_table(win) -> None:
    """Not a patch. The daemon refuses a partial one, and for a reason.

    A window that sent only what it rendered would delete a tier it had not built,
    and the failure would surface later as "unknown tier" about something the user
    never touched.
    """
    w = win()
    w._sections["models"]._save()

    assert w._call.saved is not None, "Save did not reach the daemon"
    assert set(w._call.saved["tiers"]) == {"fast", "deep"}, (
        f"Save sent {sorted(w._call.saved['tiers'])}, not the whole table"
    )


@requires_display
def test_a_successful_save_says_so_and_says_when_it_takes_effect(win) -> None:
    w = win()
    w._sections["models"]._save()

    status = w._sections["models"].status.cget("text")
    assert "Saved" in status, status
    assert "until the daemon next starts a child" in status, (
        f"the user is not told the change is not live yet: {status!r}"
    )


@requires_display
def test_a_successful_save_is_not_overwritten_by_its_own_reload(win) -> None:
    """The specific bug that made Save look broken.

    `_save` reported "Saved" and then reloaded, and the reload overwrote the status
    with the provenance line — so the user pressed Save and nothing on screen
    changed. Asserted separately from the message content because the failure is the
    *ordering*, and a test that only looked for the word "Saved" could pass on the
    broken version if the reload happened to include it.
    """
    w = win()
    w._sections["models"]._save()

    status = w._sections["models"].status.cget("text")
    assert "Read from state/models.json" not in status, (
        f"the reload overwrote the save confirmation: {status!r}"
    )


@requires_display
def test_a_refused_save_shows_the_daemons_reason(win) -> None:
    """The refusal text is the useful part and must not be replaced by "error"."""

    class Refusing(FakeDaemon):
        def __call__(self, op: str, /, **args) -> dict:
            if op == "model_config":
                return FakeDaemon.__call__(self, op, **args)
            return {"ok": False, "refused": True,
                    "error": "tier 'deep': 'typo.gguf' is not in models/."}

    w = win(Refusing())
    w._sections["models"]._save()

    status = w._sections["models"].status.cget("text")
    assert "typo.gguf" in status, (
        f"the refusal lost the model name that caused it: {status!r}"
    )
    assert "Saved" not in status, f"a refused save reported success: {status!r}"


@requires_display
def test_a_dead_daemon_is_reported_rather_than_shown_as_saving(win) -> None:
    """A Save against a dead pipe says so, and does not claim success."""

    class Broken:
        def __call__(self, op: str, /, **args) -> dict:
            raise ConnectionError("pipe is gone")

    w = win()
    section = w._sections["models"]
    section._call = Broken()
    section._save()

    status = section.status.cget("text")
    assert "daemon" in status.lower(), (
        f"a dead daemon produced {status!r} rather than an explanation"
    )
    assert "Saved" not in status, f"a failed save reported success: {status!r}"


# --------------------------------------------------------------------------- #
# a daemon that cannot be reached must not stop the window
# --------------------------------------------------------------------------- #


@requires_display
def test_a_daemon_that_is_down_still_produces_a_window(win) -> None:
    """The window opening is not conditional on the daemon answering.

    This is the failure mode worth guarding: a settings window that raises on open
    because the daemon is restarting leaves the user with no way to see *that* the
    daemon is restarting.
    """
    w = win(FakeDaemon(fail=ConnectionError("pipe is gone")))

    assert w.is_open
    assert w._root.winfo_viewable() == 1
    status = w._sections["models"].status.cget("text")
    assert "pipe is gone" in status, (
        f"the window opened but did not say why it is empty: {status!r}"
    )


@requires_display
def test_a_window_that_cannot_build_says_why_rather_than_failing_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_tk_main` records its exception; the first version swallowed it.

    That is not a style point. The swallow cost real time: a defect that made the
    window fail after a number of open/close cycles presented as an unexplained
    ten-second timeout in an unrelated test, because the one piece of evidence that
    would have named it was discarded on the way out.

    Patched at `tkinter.Tk` rather than at `settings.tk`, because the module imports
    tkinter *inside* its methods — there is no module attribute to replace. A first
    attempt set `settings.tk` and failed with "module has no attribute 'tk'", which
    says nothing about window construction and would have been "fixed" by adding a
    module-level import that exists only to make a test easier.
    """

    def exploding(*_a, **_kw):
        raise RuntimeError("deliberate, for the test")

    monkeypatch.setattr(tk, "Tk", exploding)

    w = S.Settings(FakeDaemon())
    try:
        with pytest.raises(RuntimeError, match="deliberate"):
            w.show()
    finally:
        w.destroy()


# --------------------------------------------------------------------------- #
# teardown
# --------------------------------------------------------------------------- #


@requires_display
def test_every_widget_attribute_is_released(win) -> None:
    """The guard on the guard.

    `_release_widgets` names four attributes by hand. Anyone adding a fifth — a
    header, a scrollbar, a status bar — must add it there too, and the only way to
    notice without waiting for a wrong-thread Tcl error at process exit is this.

    It has already caught a real leak: the first version of this class kept `_head`
    and `_title_label`, and pytest exited `-2147483645` — D69's exact failure mode,
    reproduced by the fix for it.
    """
    w = win()
    assert w.widget_attrs_still_held(), (
        "nothing is held, so this test is checking a window that never built"
    )

    w.destroy()

    assert w.widget_attrs_still_held() == [], (
        f"still holding {w.widget_attrs_still_held()} after destroy()"
    )


@requires_display
def test_a_section_holds_no_widget_after_disposal(win) -> None:
    """The same check one level down, on the section's own attributes.

    A section that clears its `widgets` list but keeps `self.listbox` has released
    nothing at all, and it looks disposed from the outside.

    The section is taken from the window that is then destroyed. An earlier version
    destroyed a *second* window and asserted on the first window's section, which
    failed for a good reason — they are different objects — and would have been
    "fixed" by asserting nothing at all.
    """
    w = win()
    section = w._sections["models"]
    assert section.holds_widgets(), "the section holds nothing, so this proves nothing"

    w.destroy()

    assert section.holds_widgets() == [], (
        f"the section still holds {section.holds_widgets()} after disposal"
    )


@requires_display
def test_destroy_is_idempotent(win) -> None:
    """Called from `Tray.stop()`, which may run after a failed open."""
    w = win()
    w.destroy()
    w.destroy()
    assert w._root is None


@requires_display
def test_the_tray_closes_the_settings_window_with_the_overlay() -> None:
    """Both windows or neither.

    Two Tk threads and two Tcl interpreters, one `Tray.stop()`. Closing only the
    overlay leaves the settings window alive on every daemon restart (D64 makes that
    daily) and the process reports a bad exit code.
    """
    from assistant.tray import Tray

    class _Stub:
        def handle(self, request):  # noqa: ANN001
            return {"ok": True, "result": {}}

    tray = Tray(_Stub())
    tray._settings = S.Settings(FakeDaemon())
    tray._settings.show()
    assert tray._settings.is_open

    tray.stop()

    assert tray._settings is None, (
        "Tray.stop() left the settings window standing; its Tk thread and Tcl "
        "interpreter leak on every restart"
    )
# --------------------------------------------------------------------------- #
# the workspaces section
# --------------------------------------------------------------------------- #


class FakeWorkspaceDaemon(FakeDaemon):
    """A daemon whose `workspace_list` returns what the real one returns: strings.

    This matters more than it looks. The real op returns a list of **strings**
    formatted for a person, and the first version of the section filtered for dicts -
    which discarded every row and left the screen saying "No workspace approved"
    while a workspace demonstrably existed. A fake that returned dicts would have
    agreed with the bug.
    """

    def __init__(self) -> None:
        super().__init__()
        self.workspaces: list[str] = []
        self.roots: list[str] = []

    def __call__(self, op: str, /, **args) -> dict:
        self.calls.append((op, dict(args)))
        if op == "workspace_list":
            return {"ok": True, "result": list(self.workspaces)}
        if op == "workspace_roots":
            return {"ok": True, "result": {"roots": list(self.roots)}}
        if op == "workspace_add":
            name = args.get("name", "?")
            root = args.get("root", "?")
            self.workspaces.append(
                f"{name} [{args.get('danger_level')}] {root}\n"
                f"  allowed here: {', '.join(args.get('allow_tiers') or [])}"
            )
            self.roots.append(root)
            return {"ok": True, "result": {"name": name}}
        if op == "workspace_remove":
            name = args.get("name")
            self.workspaces = [w for w in self.workspaces
                               if not w.startswith(f"{name} ")]
            self.roots = [r for r in self.roots if not r.endswith(str(name))]
            return {"ok": True, "result": {"removed": name}}
        return super().__call__(op, **args)


@requires_display
def test_workspaces_are_listed_from_strings_not_dicts(win) -> None:
    """The exact bug: the op returns strings and the section must cope.

    Asserted as "the row appears", because the failure was a screen that looked
    correct and said the opposite of the truth.
    """
    daemon = FakeWorkspaceDaemon()
    daemon.workspaces = ["proj [low] G:/work/proj\n  allowed here: T0"]
    daemon.roots = ["G:/work/proj"]

    w = win(daemon)
    w._select("workspaces")

    text = "\n".join(w._sections["workspaces"].listbox.get(0, "end"))
    assert "proj" in text, f"the workspace was not rendered: {text!r}"
    assert "G:/work/proj" in text, text


@requires_display
def test_an_empty_workspace_list_says_what_that_means(win) -> None:
    """Not just "0" - the consequence.

    "No workspace approved" on its own reads as a neutral fact. The sentence that
    matters is that the assistant therefore cannot read any project, because that is
    what the user has to change.
    """
    w = win(FakeWorkspaceDaemon())
    w._select("workspaces")

    status = w._sections["workspaces"].status.cget("text")
    assert "cannot read any project" in status, status


@requires_display
def test_the_workspace_count_and_root_count_are_both_reported(win) -> None:
    """Two different numbers, from two different ops.

    A workspace and a root are not one-to-one, and reporting only the first would
    hide a manifest that resolved to nothing.
    """
    daemon = FakeWorkspaceDaemon()
    daemon.workspaces = ["a [low] G:/a\n  allowed here: T0"]
    daemon.roots = ["G:/a", "G:/a/sub"]

    w = win(daemon)
    w._select("workspaces")

    status = w._sections["workspaces"].status.cget("text")
    assert "1 workspace" in status and "2 root" in status, status


@requires_display
def test_revoking_a_workspace_says_what_it_stopped(win) -> None:
    """The consequence, not the record.

    "Revoked a" is a database report. What the user needs to know is that nothing
    under it is reachable any more - which is the thing that just changed.
    """
    daemon = FakeWorkspaceDaemon()
    daemon.workspaces = ["a [low] G:/a\n  allowed here: T0"]
    daemon.roots = ["G:/a"]

    w = win(daemon)
    w._select("workspaces")
    section = w._sections["workspaces"]
    section.listbox.selection_set(0)
    section._remove()

    status = section.status.cget("text")
    assert "reachable" in status, status
    assert not section.listbox.get(0, "end"), "the row survived the revoke"


@requires_display
def test_the_workspace_pickers_are_released(win) -> None:
    """Two OptionMenus were leaked the first time round.

    They were stored as `_danger_menu` and `_tiers_menu` and left out of `named`, so
    `dispose()` never nulled them - the same class of leak as D75's `_head`, caught
    this time by the section-level check rather than by a process exit code.
    """
    w = win()
    w._select("workspaces")
    section = w._sections["workspaces"]
    assert section.holds_widgets()

    w.destroy()

    assert section.holds_widgets() == [], (
        f"the section still holds {section.holds_widgets()} after disposal"
    )
