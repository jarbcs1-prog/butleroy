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

**This module runs in-process.** It used to be run inside a subprocess by
`test_settings_isolated.py`, for a good reason: in this Python/Tk build, a worker-thread
Tk root that has been created and destroyed can leave Tcl's async handler to be deleted
at interpreter shutdown, on the main thread — `Tcl_AsyncDelete: async handler deleted by
the wrong thread`, and the process exits `-2147483645` *after every test has passed*. It
was not a leak: the release tests below assert nothing is retained, bare tkinter handles
forty such roots without complaint, and the count at which it appears is not stable.

That containment is **gone as of D80 (6687475)**, which deleted
`test_settings_isolated.py` along with the import-time display probe that had motivated
it. These tests are no longer skipped and no longer run in a child — all 40 run in the
parent suite. The abort is therefore not contained any more, and D81 recorded one
reproduction on 2026-10-13 (`0x80000003` in `settings.py:1513 _release_widgets`). It is
intermittent and not root-caused; see HANDOFF §5 structural claim 6.
"""

from __future__ import annotations

import pytest

from assistant import settings as S

tk = pytest.importorskip("tkinter")


def _display_available() -> bool:
    """Whether tkinter is usable, WITHOUT creating a root.

    This probe used to create and destroy a `tk.Tk()` at import time. That turned out
    to be the cause of the `-2147483645` aborts this project spent a session working
    around: a Tcl interpreter created and dropped on the **main** thread leaves the
    main thread's Tcl state unusable, and the interpreter is then torn down at
    shutdown by the wrong thread.

    Measured: with the probe, `tests/test_widgets_real.py` produced 26 errors and this
    file aborted the process; without it, both pass in-process with exit 0.

    A capability check instead. On the target platform tkinter implies a display.
    """
    try:
        return tk.Tcl is not None
    except Exception:  # noqa: BLE001
        return False


requires_display = pytest.mark.skipif(
    not _display_available(), reason="no interactive session for Tk"
)


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

# --------------------------------------------------------------------------- #
# the authority section
# --------------------------------------------------------------------------- #


class FakeAuthorityDaemon(FakeDaemon):
    """A daemon that answers `status`, `confirm_list`, `tool_check`, set_attended."""

    def __init__(self) -> None:
        super().__init__()
        self.attended = True
        self.ceiling = "T3"
        self.waiting: list[dict] = []
        self.checked: list[str] = []

    def __call__(self, op: str, /, **args) -> dict:
        self.calls.append((op, dict(args)))
        if op == "status":
            return {"ok": True, "result": {
                "attended": self.attended, "session_ceiling": self.ceiling,
                "tools_registered": 61, "audit_records": 7}}
        if op == "confirm_list":
            return {"ok": True, "result": {"pending": list(self.waiting)}}
        if op == "tool_check":
            self.checked.append(args.get("tool", ""))
            return {"ok": True, "result": {
                "tool": args.get("tool"), "would": "confirm", "tier": "T3",
                "reason": "file.delete is T3 (destructive)",
                "executable": True}}
        if op == "session_set_attended":
            self.attended = bool(args.get("attended"))
            return {"ok": True, "result": {"attended": self.attended}}
        if op == "session_set_ceiling":
            want = str(args.get("ceiling", ""))
            order = ["T0", "T1", "T2", "T3"]
            if want not in order:
                return {"ok": False, "error": f"unknown ceiling {want!r}"}
            if order.index(want) > order.index(self.ceiling) and not self.attended:
                return {"ok": False, "refused": True,
                        "error": "raise the ceiling while attended"}
            changed = want != self.ceiling
            self.ceiling = want
            return {"ok": True, "result": {"ceiling": want, "changed": changed}}
        return super().__call__(op, **args)


@requires_display
def test_authority_shows_the_ceiling_and_the_attended_state(win) -> None:
    """The facts the user could not previously see anywhere.

    Until this section the tiers and the attended flag existed only inside the
    daemon: someone could be refused at T3 with no way to see why, or what the
    ceiling even was.
    """
    w = win(FakeAuthorityDaemon())
    w._select("authority")

    headline = w._sections["authority"].headline.cget("text")
    assert "Attended" in headline, headline
    assert "T3" in headline, headline


@requires_display
def test_authority_spells_out_every_tier_with_examples(win) -> None:
    """The ladder is shown as *examples*, not as numbers.

    "T2" means nothing to someone who has not read DECISIONS.md. "move, archive,
    bulk rename, commit, build" does.
    """
    w = win(FakeAuthorityDaemon())
    w._select("authority")

    section = w._sections["authority"]

    def texts(widget) -> list[str]:
        """Every label under `widget`, recursively.

        Recursive because the ladder is nested two frames deep - a row per tier,
        inside a ladder frame. The first version of this test looked one level down,
        found nothing, and failed for a reason that had nothing to do with the
        rendering.
        """
        out: list[str] = []
        try:
            value = widget.cget("text")
        except Exception:  # noqa: BLE001 - not every widget has text
            value = ""
        if value:
            out.append(str(value))
        for child in widget.winfo_children():
            out.extend(texts(child))
        return out

    rendered = " ".join(texts(w._root))
    for name, _label, examples, _colour in S.AuthoritySection.TIERS:
        assert name in rendered, f"{name} is not shown"
        assert examples.split(",")[0] in rendered, (
            f"{name}'s examples are not shown; the ladder is numbers without them"
        )


@requires_display
def test_authority_lists_pending_actions_and_says_where_to_answer(win) -> None:
    """It lists them; it does not answer them.

    Two windows able to answer the same dialog means one of them is the one the user
    was not looking at. The section has to say where the answer goes, or a user
    staring at a pending T3 with no button will reasonably conclude it is stuck.
    """
    daemon = FakeAuthorityDaemon()
    daemon.waiting = [{"id": "c1", "tool": "file.delete",
                       "reason": "T3 destructive", "payload_hash": "a" * 64}]

    w = win(daemon)
    w._select("authority")

    text = w._sections["authority"].pending.cget("text")
    assert "file.delete" in text, text
    assert "prompt window" in text, (
        f"the user is not told where to answer it: {text!r}"
    )


@requires_display
def test_authority_says_plainly_when_nothing_is_waiting(win) -> None:
    w = win(FakeAuthorityDaemon())
    w._select("authority")
    assert "Nothing is waiting" in w._sections["authority"].pending.cget("text")


@requires_display
def test_stepping_back_goes_unattended_and_says_what_that_costs(win) -> None:
    """The consequence, not the state change.

    "Unattended" alone is a flag. What the user needs is that T2 and T3 now queue
    until they are back - because that is the thing that will surprise them.
    """
    daemon = FakeAuthorityDaemon()
    w = win(daemon)
    w._select("authority")
    section = w._sections["authority"]

    section._step_back()

    assert daemon.attended is False, "the daemon was not told"
    status = section.status.cget("text")
    assert "queue" in status, f"the consequence is not stated: {status!r}"
    assert "Unattended" in section.headline.cget("text")


@requires_display
def test_authority_offers_no_way_to_claim_presence(win) -> None:
    """The button narrows and never widens.

    Going the other way is the presence detector's job. A button that claimed the
    user was present would be a button that lies about the one fact the whole tier
    model rests on - and the daemon refuses it anyway, which is why the window must
    not offer it.
    """
    daemon = FakeAuthorityDaemon()
    daemon.attended = False
    w = win(daemon)
    w._select("authority")

    section = w._sections["authority"]
    assert section.attended_button.cget("state") == "disabled", (
        "the button is enabled while already unattended, so it can only be there "
        "to go the other way"
    )
    assert "unattended" in section.attended_button.cget("text").lower()

    # And nothing in the section offers to attend.
    for op, args in daemon.calls:
        if op == "session_set_attended":
            assert args.get("attended") is not True, (
                "the window asked to become attended"
            )


@requires_display
def test_the_checker_predicts_and_never_executes(win) -> None:
    """`tool_check` is a prediction. Nothing may be run from this box.

    Asserted on the ops the window actually called: a `tool_call` here would be the
    settings window becoming a second execution path, which is D59's defect.
    """
    daemon = FakeAuthorityDaemon()
    w = win(daemon)
    w._select("authority")
    section = w._sections["authority"]

    section.check.insert(0, "file.delete")
    section._explain()

    assert daemon.checked == ["file.delete"], "tool_check was not asked"
    assert "confirm" in section.status.cget("text")
    assert "T3" in section.status.cget("text")
    ops = [op for op, _ in daemon.calls]
    assert "tool_call" not in ops, (
        "the settings window called tool_call; it must never be a second way to "
        "run something (D59)"
    )


@requires_display
def test_the_checker_says_something_for_an_empty_box(win) -> None:
    w = win(FakeAuthorityDaemon())
    w._select("authority")
    section = w._sections["authority"]

    section._explain()

    assert "tool name" in section.status.cget("text"), (
        "an empty box produced no guidance"
    )


@requires_display
def test_the_authority_widgets_are_released(win) -> None:
    """Including the headline and the button, which are widgets like any other."""
    w = win()
    w._select("authority")
    section = w._sections["authority"]
    assert section.holds_widgets()

    w.destroy()

    assert section.holds_widgets() == [], (
        f"the section still holds {section.holds_widgets()} after disposal"
    )


def _ceiling_controls(section):
    """The ceiling Select and its Apply button, found structurally."""
    select = button = None

    def walk(widget):
        nonlocal select, button
        if type(widget).__name__ == "Select":
            select = widget
        if type(widget).__name__ == "Button" and widget.cget("text") == "Apply ceiling":
            button = widget
        for child in widget.winfo_children():
            walk(child)

    for top in section.widgets:
        walk(top)
    return select, button


@requires_display
def test_authority_stages_the_ceiling_and_applies_it(win) -> None:
    """Two gestures, like Models: choosing stages, Apply writes.

    A Select that wrote on every change would let a stray click widen
    authority, and authority deserves the more deliberate gesture (D97).
    """
    daemon = FakeAuthorityDaemon()
    w = win(daemon)
    w._select("authority")
    section = w._sections["authority"]

    select, apply = _ceiling_controls(section)
    assert select is not None, "no ceiling choice on the Authority tab"
    assert apply is not None, "no Apply ceiling button on the Authority tab"

    select.set("T1")
    assert daemon.ceiling == "T3", "choosing wrote before Apply was pressed"
    apply.invoke()
    select.widget.winfo_toplevel().update_idletasks()

    assert daemon.ceiling == "T1", "Apply did not tell the daemon"
    assert "T1" in section.headline.cget("text")
    assert "T2" in section.status.cget("text") or "refuse" in section.status.cget("text"), (
        f"the consequence is not stated: {section.status.cget('text')!r}"
    )


@requires_display
def test_authority_surfaces_a_refused_widening(win) -> None:
    """Unattended plus raise is a refusal, and the window must show the
    daemon's reason rather than going quiet or pretending it worked."""
    daemon = FakeAuthorityDaemon()
    daemon.attended = False
    daemon.ceiling = "T1"
    w = win(daemon)
    w._select("authority")
    section = w._sections["authority"]

    select, apply = _ceiling_controls(section)
    select.set("T3")
    apply.invoke()
    select.widget.winfo_toplevel().update_idletasks()

    assert daemon.ceiling == "T1", "a refused widening moved the ceiling"
    assert "T1" in section.headline.cget("text")
    assert "attended" in section.status.cget("text").lower(), (
        f"the refusal reason is not shown: {section.status.cget('text')!r}"
    )


@requires_display
def test_loading_the_tab_does_not_move_the_ceiling(win) -> None:
    """Populating the dropdown during load must not register as a user edit.

    The D92 shape: `Select.set_options` fires `on_change` through the
    StringVar trace, so a load without a guard would stage (and on Apply,
    write) a ceiling nobody chose.
    """
    daemon = FakeAuthorityDaemon()
    w = win(daemon)
    w._select("authority")

    assert ("session_set_ceiling",) not in [
        (op,) for op, _ in daemon.calls
    ], "opening the tab moved the ceiling"
    select, _apply = _ceiling_controls(w._sections["authority"])
    assert select.get() == "T3", (
        f"the choice does not show the current ceiling: {select.get()!r}"
    )

# --------------------------------------------------------------------------- #
# the advanced section
# --------------------------------------------------------------------------- #


class FakeAdvancedDaemon(FakeDaemon):
    """A daemon that answers the six diagnostics ops, with one deliberately absent."""

    def __init__(self) -> None:
        super().__init__()
        self.records = [
            {"seq": 1, "timestamp": "2026-10-07T12:00:00.000000+00:00",
             "actor": "tray", "channel": "pc", "tool": "voice_config.set",
             "tier": "T1", "decision": "allow", "reason": "x", "args": {}},
            {"seq": 2, "timestamp": "2026-10-07T12:01:00.000000+00:00",
             "actor": "user", "channel": "pc", "tool": "workspace.add",
             "tier": "T1", "decision": "allow", "reason": "y", "args": {}},
        ]

    def __call__(self, op: str, /, **args) -> dict:
        self.calls.append((op, dict(args)))
        if op == "status":
            return {"ok": True, "result": {
                "version": "0.3.0", "uptime_s": 12.5, "attended": True,
                "session_ceiling": "T3", "requests": 3, "tools_registered": 61,
                "audit_records": 2, "vram": {"used_bytes": 0, "ceiling_bytes": 0}}}
        if op == "brain_status":
            return {"ok": True, "result": {
                "running": False, "tier": None, "model_loaded": False,
                "idle_s": None, "ttl_s": 90.0, "note": "no child is running"}}
        if op == "mail_status":
            return {"ok": True, "result": {"configured": False,
                                           "note": "no state/email.json"}}
        if op == "vault_status":
            return {"ok": True, "result": {"available": False,
                                           "error": "vault not configured"}}
        if op == "reflect_overlay":
            return {"ok": True, "result": {"chars": 0}}
        if op == "audit_tail":
            return {"ok": True, "result": list(self.records)}
        return super().__call__(op, **args)


@requires_display
def test_advanced_shows_the_facts_that_had_no_home(win) -> None:
    """Six things that were previously invisible anywhere in the product.

    Which model is loaded, whether mail is configured, whether the vault bridge is
    up, what the assistant has promoted, the paths, and the audit record.
    """
    w = win(FakeAdvancedDaemon())
    w._select("advanced")

    text = w._sections["advanced"].facts.cget("text")
    for needle in ("brain running", "mail configured", "vault available",
                   "reflect overlay", "models dir", "voices dir"):
        assert needle in text, f"{needle!r} is not shown: {text!r}"


@requires_display
def test_advanced_says_never_used_rather_than_none_s_of_90s(win) -> None:
    """`idle_s` is None when no child has ever run.

    Rendered naively that reads "Nones of 90.0s", which is not a sentence and looks
    like a bug in the window rather than a fact about the daemon. Asserted because it
    is exactly the kind of thing that ships.
    """
    w = win(FakeAdvancedDaemon())
    w._select("advanced")

    text = w._sections["advanced"].facts.cget("text")
    assert "None" not in text, f"a raw None reached the screen: {text!r}"
    assert "never used" in text, text


@requires_display
def test_advanced_survives_a_subsystem_being_absent(win) -> None:
    """The vault is not installed on every machine.

    A diagnostics panel that dies because one subsystem is missing is useless exactly
    when it is most wanted. Each fact is asked for independently and a failure becomes
    a line, not an exception.
    """
    w = win(FakeAdvancedDaemon())
    w._select("advanced")

    text = w._sections["advanced"].facts.cget("text")
    # Booleans render as yes/no, so the absence reads as a fact rather than as a
    # Python repr. The point of the assertion is that the panel is *complete* despite
    # one subsystem being missing.
    assert "vault available  no" in text, text
    assert "vault error      vault not configured" in text, text


@requires_display
def test_advanced_renders_the_audit_tail_with_timestamps(win) -> None:
    """The field is `timestamp`, not `at`.

    The first version read `at`, so every row rendered with a blank time and the tail
    looked like it was missing data rather than misreading it.
    """
    w = win(FakeAdvancedDaemon())
    w._select("advanced")

    rendered = w._sections["advanced"].record.get("1.0", "end")
    assert "voice_config.set" in rendered, rendered
    assert "workspace.add" in rendered, rendered
    assert "2026-10-07" in rendered, (
        f"the timestamps did not render: {rendered!r}"
    )


@requires_display
def test_the_audit_tail_is_read_only(win) -> None:
    """A diagnostics panel must not be an editor.

    Every *setting* has a screen where it can be changed with its consequences
    visible. This is the one place that answers "what is the state of things", and a
    panel mixing the two is a panel where a stray keystroke changes something.
    """
    w = win(FakeAdvancedDaemon())
    w._select("advanced")

    assert str(w._sections["advanced"].record.cget("state")) == "disabled", (
        "the audit tail is editable"
    )


@requires_display
def test_advanced_calls_nothing_that_could_change_anything(win) -> None:
    """Read-only asserted on the ops, not on the widget state.

    `audit_tail`, `brain_status`, `mail_status`, `vault_status`, `reflect_overlay` and
    `status` all read. If a future edit added a write here, the disabled text widget
    would still look right.
    """
    daemon = FakeAdvancedDaemon()
    w = win(daemon)
    w._select("advanced")

    writes = {
        "voice_config_set", "voice_install", "voice_import", "voice_remove",
        "model_config_set", "workspace_add", "workspace_remove",
        "session_set_attended", "tool_call", "confirm_approve", "confirm_deny",
        "audit_note", "brain_load", "brain_unload",
    }
    called = {op for op, _ in daemon.calls}
    assert not (called & writes), (
        f"the advanced panel called a mutating op: {sorted(called & writes)}"
    )


@requires_display
def test_the_advanced_widgets_are_released(win) -> None:
    w = win()
    w._select("advanced")
    section = w._sections["advanced"]
    assert section.holds_widgets()

    w.destroy()

    assert section.holds_widgets() == [], (
        f"the section still holds {section.holds_widgets()} after disposal"
    )
