"""The settings window. Option A: a second window, not tabs on the chat window.

Why a second window rather than tabs on the overlay (D75). The chat window is a
*transcript* - append-only, one-directional, sized for reading. Configuration is the
opposite: many small labelled controls, two-way editing, and it needs to scroll
independently. Put them on one window and the two scrollbars fight, and every
settings bug becomes a risk to the chat window that D67 and D68 just made work.
Separate files, separate Tk threads, and a failure in one cannot take the other
down - which is the property `overlay.py` gained the hard way.

**The threading is copied deliberately, not by taste.** tkinter binds a root to the
thread that created it, so there is exactly one thread permitted to touch Tk and
every call from anywhere else is funnelled through a queue with a timeout. That
shape is `overlay.Overlay`'s, and re-deriving it would produce a second, subtly
different one. The reason it is duplicated rather than shared is stated in
`Settings` below.

**Nothing here writes anything itself.** Every mutation goes through the daemon
over the named pipe, like every other client in this process tree (D31). The tray
being a *client* of the daemon rather than a second front door is the property that
makes the whole thing auditable, and a settings window that read `models.json`
directly would be exactly the second door D59 says is the defect.

Tk is imported inside the methods that need it, so `import assistant.settings`
costs nothing on a headless machine - the same reason `tray.py` and `overlay.py`
both do it, and the same reason D1 requires.
"""

from __future__ import annotations

import gc
import queue
import threading
from typing import Any, Callable

from . import widgets as W

#: Palette, read from the brand kit rather than re-typed. `brand/tokens.json` is
#: the one definition; a hex code written out here would be the second, and would
#: drift the first time someone edited the brand.
#:
#: Read from the repository rather than imported, because `brand/tokens.json` is
#: documentation beside the artwork and not part of the installed package (D72).
#: If it is missing - an installed copy, a trimmed deployment - the fallbacks
#: below are the values the brand documents, so the window still renders.
_FALLBACK = {
    "obsidian": "#0F120E",
    "slate": "#1B2019",
    "slate-line": "#2E342A",
    "brass": "#C8A951",
    "brass-lit": "#E8CE86",
    "bone": "#EDE8DC",
    "sage": "#7C8A7C",
    "verdigris": "#6AC47F",
    "rust": "#D9694F",
    "amber": "#C79A3C",
}


def palette() -> dict[str, str]:
    """The brand colours, from the kit if it is reachable, else the same values."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    tokens = root / "brand" / "tokens.json"
    try:
        import json

        raw = json.loads(tokens.read_text(encoding="utf-8"))["colour"]
        return {k: v for k, v in raw.items() if not k.startswith("$")}
    except Exception:  # noqa: BLE001 - a missing kit must not stop the window
        return dict(_FALLBACK)


class Section:
    """One page of the window: a title, a build method, and nothing else.

    A class rather than a dict of lambdas because each page owns real widgets and
    needs to release them, and a lambda cannot. The `Section` contract is small on
    purpose: build widgets into `parent`, and return any widget references you need
    later in `self.widgets` so `dispose()` can let go of them.

    The disposal is not optional. A `Label` holds `.tk`, which is the Tcl
    interpreter, and an interpreter leaked to process shutdown gets its async
    handler deleted from the wrong thread - which on Windows is a debug break and a
    process exit of `-2147483645` instead of 0. That is D69's exit code, measured.
    """

    def __init__(self, key: str, title: str, glyph: str) -> None:
        self.key = key
        self.title = title
        self.glyph = glyph
        self.widgets: list[Any] = []
        # Populated by `build` implementations for the widgets they keep a named
        # reference to. `dispose()` walks it, and the walk is what makes clearing
        # the list sufficient.
        self.named: tuple[str, ...] = ()

    def build(self, parent: Any, call: Callable[..., dict]) -> None:
        raise NotImplementedError

    def dispose(self) -> None:
        """Drop every widget this section created, on the Tk thread.

        Both halves, and the first one is the one that bit. Clearing `widgets`
        while leaving `self.listbox` and friends still pointing at the objects does
        **not** release anything: the attributes are the references that matter. That
        is D69's bug reproduced verbatim in a new module, and it presented as the
        same exit code — `-2147483645`, Tcl's async handler deleted from the wrong
        thread — which is why it is worth a comment rather than a tidy-up.

        So: clear the list, *and* null the attributes, *and* check that nothing is
        left holding a `.tk`.
        """
        self.widgets.clear()
        for name in self.named:
            setattr(self, name, None)

    def holds_widgets(self) -> list[str]:
        """Names of any attribute still holding a widget.

        Used by the test that asserts teardown is complete. A test cannot reach into
        `__dict__` and check for `.tk` without this, because the attributes are
        named differently per section.
        """
        return [name for name, value in vars(self).items()
                if hasattr(value, "tk")]


class ModelsSection(Section):
    """Which model answers each tier, and what it may lease.

    The first surface that can change anything, because it is the first one that
    could not be done at all before D74: the two tiers were Python literals at
    `brain_server.py:43-72`, and the only thing that could change one was a
    programmer with a text editor.
    """

    def __init__(self) -> None:
        super().__init__("models", "Models", "models")
        # The three widgets kept by name. `dispose()` nulls exactly these, and the
        # `holds_widgets()` test would catch a fourth being added and forgotten.
        self.named = ("listbox", "detail", "status")
        self.listbox: Any = None
        self.detail: Any = None
        self.status: Any = None

    def build(self, parent: Any, call: Callable[..., dict]) -> None:
        import tkinter as tk

        c = palette()
        body = tk.Frame(parent, bg=c["slate"], bd=0)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 16))
        self.widgets.append(body)

        note = tk.Label(
            body,
            text="The fast tier answers ordinary turns. The deep tier is asked for "
                 "when a turn is hard enough; the two cannot both be resident, so "
                 "asking for deep gives fast back.",
            fg=c["sage"], bg=c["slate"], wraplength=640, justify="left",
            font=("Segoe UI", 9),
        )
        note.pack(fill="x", padx=16, pady=(14, 10))
        self.widgets.append(note)

        self.listbox = tk.Listbox(
            body, fg=c["bone"], bg=c["obsidian"], selectbackground=c["brass"],
            selectforeground=c["obsidian"], relief="flat", highlightthickness=0,
            font=("Cascadia Code", 9), activestyle="none",
        )
        self.listbox.pack(fill="both", expand=True, padx=16)
        self.widgets.append(self.listbox)

        self.detail = tk.Label(body, text="", fg=c["sage"], bg=c["slate"],
                               anchor="w", justify="left",
                               font=("Segoe UI", 9))
        self.detail.pack(fill="x", padx=16, pady=(10, 0))
        self.widgets.append(self.detail)

        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        # The choice controls. Until these existed the section was a viewer: it
        # showed which model answered `fast` and which answered `deep`, and Save
        # re-persisted exactly what Load had read, so neither could be changed.
        #
        # The options are the models **already in `models/`**, not a text box for
        # an arbitrary path. `modelconfig` stores a filename relative to
        # `models/` on purpose - a checkout that moves must not invalidate the
        # config - and `_SAFE_NAME` rejects anything else. A path field would be
        # a control the daemon refuses on every keystroke. Copying a new file
        # into `models/` is a different operation and needs an op that does not
        # exist yet.
        self._selectors: dict[str, Any] = {}
        for tier in ("fast", "deep"):
            line = tk.Frame(body, bg=c["slate"])
            line.pack(fill="x", padx=16, pady=(4, 0))
            self.widgets.append(line)
            tk.Label(line, text=f"{tier} tier", fg=c["sage"], bg=c["slate"],
                     font=("Segoe UI", 9), width=10, anchor="w").pack(side="left")
            self.widgets.append(line.winfo_children()[-1])
            picker = W.Select(
                line, "", options=("",), width=52,
                on_change=lambda value, t=tier: self._choose(t, value),
            )
            picker.pack(side="left", fill="x", expand=True)
            self.widgets.append(picker)
            self._selectors[tier] = picker

        row = tk.Frame(body, bg=c["slate"])
        row.pack(fill="x", padx=16, pady=(12, 14))
        self.widgets.append(row)

        self.status = tk.Label(row, text="", fg=c["sage"], bg=c["slate"], anchor="w",
                               font=("Segoe UI", 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.widgets.append(self.status)

        for label, command in (("Reload", self._reload),
                               ("Save", self._save)):
            button = W.Button(row, label, command,
                               variant="ghost")
            button.pack(side="right", padx=(8, 0))
            self.widgets.append(button)

        self._models: list[dict[str, Any]] = []
        self._tiers: dict[str, dict[str, Any]] = {}
        self._provenance = ""
        # Kept, because `_save` needs it and re-reading it from the daemon would be
        # a race: the value on screen and the value sent would be two different
        # reads of a file the user may be editing in a text editor.
        #
        # This attribute was *missing* in the first version, and `_save` then
        # raised AttributeError, which its own `except Exception` swallowed into
        # "Could not reach the daemon" - so Save reported a connection failure for
        # a bug in the window, and the window had never been able to save at all.
        # Found by `test_saving_sends_the_whole_table`, which asserts the daemon
        # was reached rather than what the status line says.
        self._call = call
        self._load(call)

    # -- data ---------------------------------------------------------------

    def _choose(self, tier: str, name: str) -> None:
        """A tier was re-assigned in the window. Update the table Save will send.

        Deliberately does not write on its own. The brand kit reserves brass for
        things that are *acting*, and a config edit is audited at T1 - so it is
        the Save button that puts it on the wire, and this only stages it. It also
        refreshes the list, or the list would keep showing the old pairing beside a
        control that already says otherwise.
        """
        if not name or tier not in self._tiers:
            return
        # A load must never register as a user edit. `Select.set_options` falls
        # back to the first option when the current one is not offered, and that
        # fires `on_change` through the StringVar trace - so populating the
        # dropdowns during a load silently rewrote `fast` to the alphabetically
        # first model on disk. It surfaced as the list showing the *deep* model
        # for both tiers. Loading is not choosing.
        if getattr(self, "_loading", False):
            return
        current = self._tiers[tier]
        if current.get("model") == name:
            return
        # need_bytes is the session's lease ceiling, not a property of the model,
        # so it is carried across untouched. Losing it would silently change what
        # the arbiter permits.
        self._tiers[tier] = {**current, "model": name}
        self._say(f"{tier} tier set to {name} - press Save to apply", "brass")
        self._refresh_list()

    def _refresh_list(self) -> None:
        """Redraw the list from `self._tiers`.

        Split out so a staged change and a reload cannot become two different
        renderings of the same state.
        """
        self.listbox.delete(0, "end")
        for name in sorted(self._tiers):
            tier = self._tiers[name]
            size = self._size_of(tier.get("model", ""))
            self.listbox.insert(
                "end",
                f"  {name:<6} {tier.get('model','?')}\n"
                f"         lease {tier.get('need_bytes', 0) / 1024 ** 3:.1f} GiB"
                f"   on disk {size / 1024 ** 3:.1f} GiB",
            )

    def _offered(self) -> list[str]:
        """Every model on disk, as `model_config` reports them.

        Tolerates both shapes the payload has taken - a list of names, and a list
        of dicts with a name. Assuming one and being wrong empties the dropdown,
        which reads as "there are no models" rather than as a bug.
        """
        names: list[str] = []
        for entry in self._models or []:
            if isinstance(entry, str):
                name = entry
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("model") or ""
            else:
                name = str(entry)
            if name and name not in names:
                names.append(name)
        return sorted(names)

    def _load(self, call: Callable[..., dict]) -> None:
        """Ask the daemon. Never touch the file from here.

        The daemon is the only writer of `state/models.json` (D75 adds the ops for
        it). A window that read the file directly would be a second front door, and
        it would also not see the daemon's own view of whether a tier fell back to
        a default - which is the one fact the user most needs.
        """
        try:
            res = call("model_config") or {}
        except Exception as exc:  # noqa: BLE001 - a dead daemon must not kill the UI
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return

        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return

        result = res.get("result") or {}
        self._models = result.get("models") or []
        self._tiers = result.get("tiers") or {}
        self._provenance = result.get("provenance", "")

        self._refresh_list()

        # Fill the two pickers. `set_options` keeps the current value when it is
        # still offered, so a reload after a staged-but-unsaved change does not
        # silently discard it.
        offered = self._offered()
        self._loading = True
        try:
            for tier, picker in getattr(self, "_selectors", {}).items():
                picker.set_options(offered or ("",))
                assigned = (self._tiers.get(tier) or {}).get("model", "")
                if assigned and assigned in offered:
                    picker.set(assigned)
        finally:
            self._loading = False

        self._say(self._provenance_note(), "sage" if self._provenance == "file"
                  else "amber")

    def _save(self) -> None:
        """Write through the daemon, and say plainly whether it took.

        A settings window that silently discards what you typed is worse than one
        that refuses to save, because you believe it took - so the success and
        failure paths are visually different and neither is silent.

        The **whole** table is sent, always. The daemon refuses a partial one
        (`test_a_partial_table_is_refused_rather_than_merged`), and that refusal is
        the design rather than pedantry: a patch would delete any tier this window
        had not rendered, and the failure would surface later as "unknown tier"
        on a request about something the user never touched.

        `tiers=` as a **keyword**, not a positional dict. `Tray.call` is
        `call(op, /, **args)` — args are keyword-only — and the first version
        passed the table positionally. Every call then raised `TypeError`, which the
        `except` below turned into "Could not reach the daemon", so Save reported a
        connection failure for a window bug and the window had never once been able
        to save. Found by `test_saving_sends_the_whole_table`, which asserts the
        daemon *was reached* rather than reading the status line.
        """
        if not self._tiers:
            self._say("Nothing loaded, so there is nothing to save.", "amber")
            return

        try:
            res = self._call("model_config_set", tiers=self._tiers) or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return

        if res.get("ok"):
            saved_to = (res.get("result") or {}).get("saved", "models.json")
            note = (res.get("result") or {}).get("note", "")
            # Reload FIRST, then report. The other order says "Saved" and then
            # immediately overwrites it with the provenance line, so the user presses
            # Save and nothing on screen changes - which is precisely the
            # "settings window that silently discards what you typed" failure this
            # whole method was written to avoid, reached by a different route.
            self._load(self._call)
            self._say(f"Saved to {saved_to}. {note}".strip(), "verdigris")
        else:
            self._say(res.get("error", "unknown error"), "rust")

    def _reload(self) -> None:
        self._load(self._call)

    # -- presentation -------------------------------------------------------

    def _size_of(self, model: str) -> int:
        for row in self._models:
            if row.get("name") == model:
                return int(row.get("bytes", 0))
        return 0

    def _provenance_note(self) -> str:
        """Always visible, and never just "OK".

        `provenance` is the difference between "these are your settings" and "these
        are the built-in defaults because your settings file could not be read, and
        here is why". A user who cannot see that will believe the window is showing
        what they configured.
        """
        if self._provenance == "file":
            return f"Read from state/models.json ({len(self._tiers)} tiers)."
        if self._provenance == "defaults":
            return "No models.json yet - showing the built-in defaults. Save to create one."
        return f"Built-in defaults in use. {self._provenance}"

    def _on_select(self, _event: Any = None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        name = sorted(self._tiers)[selection[0]]
        tier = self._tiers[name]
        models = [m["name"] for m in self._models]
        self.detail.configure(text=(
            f"{name}: {tier.get('model')}\n"
            f"  lease ceiling {tier.get('need_bytes', 0):,} bytes "
            f"({tier.get('need_bytes', 0) / 1024 ** 3:.1f} GiB)\n"
            f"  {len(models)} model(s) available on disk"
        ))

    def _say(self, message: str, tone: str) -> None:
        self.status.configure(text=message, fg=palette().get(tone, "#EDE8DC"))


class VoicesSection(Section):
    """Which voice speaks, what is installed, and importing your own.

    The second surface, and the first with a *file* in it. Three groups: the
    installed voices, the default, and the transcription model. Import is a Tk file
    dialog feeding an op, which is the one place the window hands the daemon a path
    the user chose - see `voiceconfig.import_voice`.
    """

    def __init__(self) -> None:
        super().__init__("voices", "Voices", "voices")
        self.named = ("listbox", "detail", "status", "whisper", "whisper_picker")
        self.listbox: Any = None
        self.detail: Any = None
        self.status: Any = None
        self.whisper: Any = None
        self.whisper_picker: Any = None

    def build(self, parent: Any, call: Callable[..., dict]) -> None:
        import tkinter as tk

        c = palette()
        self._call = call
        body = tk.Frame(parent, bg=c["slate"], bd=0)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 16))
        self.widgets.append(body)

        note = tk.Label(
            body,
            text="The voice speaks your replies. Transcription turns your speech "
                 "into text, and runs on the CPU - it finishes before you do, so it "
                 "never touches the graphics card.",
            fg=c["sage"], bg=c["slate"], wraplength=640, justify="left",
            font=("Segoe UI", 9),
        )
        note.pack(fill="x", padx=16, pady=(14, 10))
        self.widgets.append(note)

        self.listbox = tk.Listbox(
            body, fg=c["bone"], bg=c["obsidian"], selectbackground=c["brass"],
            selectforeground=c["obsidian"], relief="flat", highlightthickness=0,
            font=("Cascadia Code", 9), activestyle="none", height=8,
        )
        self.listbox.pack(fill="both", expand=True, padx=16)
        self.widgets.append(self.listbox)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        self.detail = tk.Label(body, text="", fg=c["sage"], bg=c["slate"],
                               anchor="w", justify="left", font=("Segoe UI", 9))
        self.detail.pack(fill="x", padx=16, pady=(10, 0))
        self.widgets.append(self.detail)

        row = tk.Frame(body, bg=c["slate"])
        row.pack(fill="x", padx=16, pady=(10, 0))
        self.widgets.append(row)

        tk.Label(row, text="Transcription model", fg=c["sage"], bg=c["slate"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.widgets.append(row.winfo_children()[-1])

        self.whisper = tk.StringVar()
        self.whisper_picker = tk.OptionMenu(row, self.whisper, "")
        self.whisper_picker.configure(
            bg=c["obsidian"], fg=c["bone"], relief="flat", bd=0,
            highlightthickness=0, activebackground=c["slate-line"],
            font=("Cascadia Code", 9))
        self.whisper_picker["menu"].configure(bg=c["obsidian"], fg=c["bone"])
        self.whisper_picker.pack(side="left", padx=10)
        self.widgets.append(self.whisper_picker)

        actions = tk.Frame(body, bg=c["slate"])
        actions.pack(fill="x", padx=16, pady=(12, 14))
        self.widgets.append(actions)

        self.status = tk.Label(actions, text="", fg=c["sage"], bg=c["slate"],
                               anchor="w", justify="left", wraplength=420,
                               font=("Segoe UI", 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.widgets.append(self.status)

        for label, command in (
            ("Remove", self._remove),
            ("Import…", self._import),
            ("Download", self._download),
            ("Use this voice", self._make_default),
            ("Save", self._save),
        ):
            button = W.Button(actions, label, command,
                               variant="ghost")
            button.pack(side="right", padx=(6, 0))
            self.widgets.append(button)

        self._data: dict[str, Any] = {}
        self._load(call)

    # -- data ---------------------------------------------------------------

    def _load(self, call: Callable[..., dict]) -> None:
        try:
            res = call("voice_config") or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return
        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return

        self._data = res.get("result") or {}
        voices = self._data.get("voices") or {}
        default = self._data.get("default")

        self.listbox.delete(0, "end")
        self._names = sorted(voices)
        for name in self._names:
            v = voices[name]
            mark = "*" if name == default else " "
            state = "installed" if v.get("complete") else (
                "incomplete" if v.get("installed") else "not downloaded")
            size = v.get("model_bytes", 0) / 1024 ** 2
            self.listbox.insert(
                "end",
                f"{mark} {name}\n      {state}"
                + (f"   {size:.0f} MB" if size else ""),
            )

        models = [m["name"] for m in (self._data.get("whisper_models") or [])]
        # The OptionMenu's own `menu`, held directly. The first version reached for
        # `self.widgets[-2]["menu"]`, which is both the wrong widget (a Label) and
        # the wrong accessor (`["menu"]` is `cget`, which raises for a widget that
        # has no such option). It raised *mid-`_load`*, after the list was populated
        # and before the status line was set - so the pane looked half-built and the
        # status looked empty, two symptoms from one wrong lookup.
        menu = self.whisper_picker["menu"]
        menu.delete(0, "end")
        for m in models:
            menu.add_command(label=m, command=lambda v=m: self.whisper.set(v))
        self.whisper.set(self._data.get("whisper") or "")

        provenance = self._data.get("provenance", "")
        if provenance == "file":
            self._say(f"* marks the voice that speaks. Read from "
                      f"state/voices.json.", "sage")
        elif provenance == "defaults":
            self._say("No voices.json yet - using the built-in default. Save to "
                      "create one.", "amber")
        else:
            self._say(f"Built-in default in use. {provenance}", "amber")

    def _selected(self) -> str | None:
        selection = self.listbox.curselection()
        if not selection:
            return None
        return self._names[selection[0]]

    def _on_select(self, _event: Any = None) -> None:
        name = self._selected()
        if name is None:
            return
        v = (self._data.get("voices") or {}).get(name) or {}
        self.detail.configure(text=(
            f"{name}\n"
            f"  {v.get('source', '?')}   model "
            f"{v.get('model_bytes', 0) / 1024 ** 2:.1f} MB, config "
            f"{v.get('config_bytes', 0) / 1024} KB\n"
            f"  {v.get('note') or 'ready'}"
        ))

    # -- actions ------------------------------------------------------------

    def _make_default(self) -> None:
        """Mark the selected voice, and save immediately.

        Saving on this button rather than only on Save is deliberate: choosing a
        voice is a single, reversible decision with an obvious result, and making the
        user choose *and then* remember to press Save is how someone ends up
        believing they changed something they did not.
        """
        name = self._selected()
        if name is None:
            self._say("Select a voice first.", "amber")
            return
        self._data["default"] = name
        self._save(message=f"{name} is now the voice that speaks.")

    def _save(self, message: str | None = None) -> None:
        try:
            res = self._call(
                "voice_config_set",
                default=self._data.get("default"),
                whisper=self.whisper.get(),
            ) or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return

        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return

        result = res.get("result") or {}
        self._load(self._call)
        if message:
            self._say(f"{message} {result.get('note', '')}".strip(), "verdigris")
        elif result.get("note"):
            self._say(f"Saved. {result['note']}", "amber")
        else:
            self._say(f"Saved. {result.get('default')} speaks, "
                      f"{result.get('whisper')} transcribes.", "verdigris")

    def _download(self) -> None:
        """Fetch a known voice. Blocks, and says so first.

        The status is set *before* the call, because a 60 MB download with no
        message on screen for several seconds reads as a hung window.
        """
        name = self._selected()
        if name is None:
            self._say("Select a voice first.", "amber")
            return
        self._say(f"Downloading {name}…", "amber")
        try:
            res = self._call("voice_install", voice=name) or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return

        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return
        size = (res.get("result") or {}).get("bytes", 0) / 1024 ** 2
        self._load(self._call)
        self._say(f"Downloaded {name} ({size:.0f} MB).", "verdigris")

    def _import(self) -> None:
        """Pick a `.onnx` with a Tk dialog, and hand the path to the daemon.

        The dialog is Tk's, so the *user* chooses; the window never enumerates the
        filesystem. The daemon validates and copies, and audits the source path.
        """
        import tkinter as tk
        from tkinter import filedialog

        # Parented to the root so the dialog is modal to this window rather than
        # floating free behind it.
        parent = self.listbox.winfo_toplevel() if self.listbox is not None else None
        chosen = filedialog.askopenfilename(
            parent=parent,
            title="Choose a piper voice model",
            filetypes=[("piper voice", "*.onnx"), ("all files", "*.*")],
        )
        if not chosen:
            self._say("Import cancelled.", "sage")
            return

        try:
            res = self._call("voice_import", source=chosen) or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return

        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return
        self._load(self._call)
        self._say(f"Imported {(res.get('result') or {}).get('voice', '?')}.",
                  "verdigris")

    def _remove(self) -> None:
        name = self._selected()
        if name is None:
            self._say("Select a voice first.", "amber")
            return
        try:
            res = self._call("voice_remove", voice=name) or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return

        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return
        removed = (res.get("result") or {}).get("removed") or []
        self._load(self._call)
        self._say(f"Removed {name} ({len(removed)} file(s)). It can be downloaded "
                  f"again.", "verdigris")

    def _say(self, message: str, tone: str) -> None:
        if self.status is not None:
            self.status.configure(text=message, fg=palette().get(tone, "#EDE8DC"))


class WorkspacesSection(Section):
    """Which directories the assistant may touch at all.

    The most important surface in the window, and the reason is structural: nothing
    outside `models/` is reachable until a root is approved here. A workspace manifest
    is the *only* source of "approved" that `policy.Gate` has - D13 says paths are
    checked against approved roots, and this is where approved comes from.

    **This screen cannot widen authority, and the code makes sure of that rather than
    this comment.** `allow_tiers` is a per-workspace ceiling that is always
    *intersected* with the session ceiling at resolve time, and `DangerLevel` caps the
    tiers a manifest may ask for at all - `high` danger permits no more than T1. So
    the tier pickers here can only ever narrow. A window that could raise the ceiling
    would be a window that hands the assistant authority the session did not have.
    """

    def __init__(self) -> None:
        super().__init__("workspaces", "Workspaces", "workspaces")
        self.named = ("listbox", "detail", "status", "danger", "tiers",
                      "_danger_menu", "_tiers_menu")
        self.listbox: Any = None
        self.detail: Any = None
        self.status: Any = None
        self.danger: Any = None
        self.tiers: Any = None
        self._danger_menu: Any = None
        self._tiers_menu: Any = None

    def build(self, parent: Any, call: Callable[..., dict]) -> None:
        import tkinter as tk

        c = palette()
        self._call = call
        body = tk.Frame(parent, bg=c["slate"], bd=0)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 16))
        self.widgets.append(body)

        note = tk.Label(
            body,
            text="The assistant can only read and write inside a directory you "
                 "approve here. Nothing else on the machine is reachable, however "
                 "it is asked. A workspace is a claim you make about a folder - it "
                 "does not make the folder safe, it makes it reachable.",
            fg=c["sage"], bg=c["slate"], wraplength=660, justify="left",
            font=("Segoe UI", 9),
        )
        note.pack(fill="x", padx=16, pady=(14, 10))
        self.widgets.append(note)

        self.listbox = tk.Listbox(
            body, fg=c["bone"], bg=c["obsidian"], selectbackground=c["brass"],
            selectforeground=c["obsidian"], relief="flat", highlightthickness=0,
            font=("Cascadia Code", 9), activestyle="none", height=8,
        )
        self.listbox.pack(fill="both", expand=True, padx=16)
        self.widgets.append(self.listbox)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        self.detail = tk.Label(body, text="", fg=c["sage"], bg=c["slate"],
                               anchor="w", justify="left", font=("Segoe UI", 9))
        self.detail.pack(fill="x", padx=16, pady=(10, 0))
        self.widgets.append(self.detail)

        row = tk.Frame(body, bg=c["slate"])
        row.pack(fill="x", padx=16, pady=(10, 0))
        self.widgets.append(row)

        tk.Label(row, text="Danger", fg=c["sage"], bg=c["slate"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.widgets.append(row.winfo_children()[-1])
        self.danger = tk.StringVar(value="low")
        danger_menu = tk.OptionMenu(row, self.danger, "low", "medium", "high")
        danger_menu.configure(bg=c["obsidian"], fg=c["bone"], relief="flat", bd=0,
                              highlightthickness=0, activebackground=c["slate-line"],
                              font=("Cascadia Code", 9))
        danger_menu["menu"].configure(bg=c["obsidian"], fg=c["bone"])
        danger_menu.pack(side="left", padx=(8, 18))
        self.widgets.append(danger_menu)
        self._danger_menu = danger_menu

        tk.Label(row, text="Allowed tiers", fg=c["sage"], bg=c["slate"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.widgets.append(row.winfo_children()[-1])
        self.tiers = tk.StringVar(value="T0")
        tiers_menu = tk.OptionMenu(row, self.tiers,
                                   "T0", "T0 T1", "T0 T1 T2", "T0 T1 T2 T3")
        tiers_menu.configure(bg=c["obsidian"], fg=c["bone"], relief="flat", bd=0,
                             highlightthickness=0, activebackground=c["slate-line"],
                             font=("Cascadia Code", 9))
        tiers_menu["menu"].configure(bg=c["obsidian"], fg=c["bone"])
        tiers_menu.pack(side="left", padx=8)
        self.widgets.append(tiers_menu)
        self._tiers_menu = tiers_menu

        actions = tk.Frame(body, bg=c["slate"])
        actions.pack(fill="x", padx=16, pady=(12, 14))
        self.widgets.append(actions)

        self.status = tk.Label(actions, text="", fg=c["sage"], bg=c["slate"],
                               anchor="w", justify="left", wraplength=420,
                               font=("Segoe UI", 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.widgets.append(self.status)

        for label, command in (
            ("Remove", self._remove),
            ("Approve a folder…", self._add),
            ("Reload", self._reload),
        ):
            button = W.Button(actions, label, command,
                               variant="ghost")
            button.pack(side="right", padx=(6, 0))
            self.widgets.append(button)

        self._rows: list[dict[str, Any]] = []
        self._load(call)

    # -- data ---------------------------------------------------------------

    def _load(self, call: Callable[..., dict]) -> None:
        try:
            res = call("workspace_list") or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return
        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return

        # `workspace_list` returns a list of **strings**, formatted for a person to
        # read - the op's own docstring says `workspace_roots` is the one for
        # machines. The first version of this filtered for dicts, which discarded
        # every row and left the screen saying "No workspace approved" while a
        # workspace demonstrably existed. That is a plausible and completely wrong
        # answer, which is the worst kind.
        #
        # So the list is used only to *count*, and the structured facts come from
        # `workspace_roots` - never by parsing the human string, because a caller
        # that parses prose is one wording change away from believing the wrong thing.
        described = res.get("result") or []
        self._rows = [line for line in described if isinstance(line, str)]

        roots_res = call("workspace_roots") or {}
        roots = (roots_res.get("result") or {}).get("roots") or []

        self.listbox.delete(0, "end")
        for line in self._rows:
            self.listbox.insert("end", "  " + str(line).replace("\n", "\n  "))

        if not self._rows:
            self._say("No workspace approved. The assistant cannot read any project "
                      "yet - approve a folder to change that.", "amber")
        else:
            self._say(f"{len(self._rows)} workspace(s) approved, covering "
                      f"{len(roots)} root(s).", "sage")
        self._roots = roots

    def _selected(self) -> str | None:
        """The selected row's workspace name, taken from the human string's first
        token - which is safe *only* because `workspace_list`'s format starts with
        the name and this is used for a name, not for a path or a permission.

        Anything that decides what is allowed comes from `workspace_roots`."""
        selection = self.listbox.curselection()
        if not selection:
            return None
        line = self._rows[selection[0]]
        return str(line).split(" ", 1)[0] or None

    def _on_select(self, _event: Any = None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        self.detail.configure(text=str(self._rows[selection[0]]))

    # -- actions ------------------------------------------------------------

    def _add(self) -> None:
        """Pick a folder, then approve it.

        The folder chooser is Tk's, so the *user* names the directory; the window
        never enumerates the filesystem. The name is derived from the folder and can
        be overridden by the daemon's own validation, which refuses a name that is
        not a plain identifier.
        """
        from tkinter import filedialog

        parent = self.listbox.winfo_toplevel() if self.listbox is not None else None
        root = filedialog.askdirectory(
            parent=parent, title="Choose a folder to approve", mustexist=True)
        if not root:
            self._say("Cancelled.", "sage")
            return

        name = root.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        tiers = [t for t in self.tiers.get().split() if t]

        try:
            res = self._call("workspace_add", name=name, root=root,
                             danger_level=self.danger.get(),
                             allow_tiers=tiers) or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return

        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return
        self._load(self._call)
        self._say(f"Approved {root} as {name!r}, {self.danger.get()} danger, "
                  f"allowing {' '.join(tiers)}.", "verdigris")

    def _remove(self) -> None:
        name = self._selected()
        if name is None:
            self._say("Select a workspace first.", "amber")
            return
        try:
            res = self._call("workspace_remove", name=name) or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return

        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return
        self._load(self._call)
        self._say(f"Revoked access to {name!r}. Nothing under it is reachable "
                  f"any more.", "verdigris")

    def _reload(self) -> None:
        self._load(self._call)

    def _say(self, message: str, tone: str) -> None:
        if self.status is not None:
            self.status.configure(text=message, fg=palette().get(tone, "#EDE8DC"))


class AuthoritySection(Section):
    """What the assistant is allowed to do, and what it is waiting on you for.

    The section that makes the safety model *visible* rather than only enforced. Until
    now the tiers, the session ceiling and the attended flag existed entirely inside
    the daemon: the user could be refused at T3 and have no way to see why, or to see
    what the ceiling even was.

    **It cannot approve anything, and that is deliberate.** Confirmations are answered
    in the chat window and only there - `tray.py` says so about its own window, and the
    same argument applies here: two windows able to answer the same dialog means one
    of them is the one the user was not looking at. This section *lists* what is
    pending and says where to answer it.

    **It can narrow, not widen.** `session_set_attended` is the one op that may change
    the attended flag and it may only ever narrow - asking for attended=True from a
    non-present channel is refused by the daemon, not by this window. Dropping to
    unattended is always allowed, because that is the safe direction.
    """

    #: The ladder, spelled out for a person. Kept here rather than derived from the
    #: enum because the point of showing it is the *examples*, and those are a
    #: judgement about this product rather than a property of the numbers.
    TIERS = (
        ("T0", "read", "list, read, search, look at the machine", "verdigris"),
        ("T1", "reversible write", "drafts, scratch files, edits under git", "sage"),
        ("T2", "consequential", "move, archive, bulk rename, commit, build", "amber"),
        ("T3", "irreversible", "delete, send, install, admin change", "rust"),
    )

    def __init__(self) -> None:
        super().__init__("authority", "Authority", "shield")
        self.named = ("listbox", "detail", "status", "pending", "check",
                      "headline", "attended_button")
        self.listbox: Any = None
        self.detail: Any = None
        self.status: Any = None
        self.pending: Any = None
        self.check: Any = None
        self.headline: Any = None
        self.attended_button: Any = None

    def build(self, parent: Any, call: Callable[..., dict]) -> None:
        import tkinter as tk

        c = palette()
        self._call = call
        body = tk.Frame(parent, bg=c["slate"], bd=0)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 16))
        self.widgets.append(body)

        self.headline = tk.Label(body, text="", fg=c["bone"], bg=c["slate"],
                                 anchor="w", font=("Segoe UI", 11, "bold"))
        self.headline.pack(fill="x", padx=16, pady=(14, 2))
        self.widgets.append(self.headline)

        note = tk.Label(
            body,
            text="Every action is placed on this ladder before it runs, and the "
                 "highest tier you are attending for is the session ceiling. Nothing "
                 "the assistant decides can raise either one.",
            fg=c["sage"], bg=c["slate"], wraplength=660, justify="left",
            font=("Segoe UI", 9),
        )
        note.pack(fill="x", padx=16, pady=(0, 10))
        self.widgets.append(note)

        ladder = tk.Frame(body, bg=c["slate"])
        ladder.pack(fill="x", padx=16)
        self.widgets.append(ladder)
        for name, label, examples, colour in self.TIERS:
            row = tk.Frame(ladder, bg=c["slate"])
            row.pack(fill="x", pady=1)
            self.widgets.append(row)
            # The badge, from the kit. This is the one place the widget set carries
            # real meaning rather than style: the tier colour is how the authority
            # model becomes visible, and `Badge.for_tier` is the single mapping from a
            # tier name to a colour. Spelling it out here was the second copy of that
            # mapping, and the second copy is the one that drifts.
            badge = W.Badge.for_tier(row, name)
            badge.pack(side="left")
            self.widgets.append(badge)
            tk.Label(row, text=f"  {label}", fg=c["bone"], bg=c["slate"],
                     font=("Segoe UI", 9), width=18, anchor="w").pack(side="left")
            self.widgets.append(row.winfo_children()[-1])
            tk.Label(row, text=examples, fg=c["sage"], bg=c["slate"],
                     font=("Segoe UI", 9), anchor="w").pack(side="left")
            self.widgets.append(row.winfo_children()[-1])

        tk.Label(body, text="Waiting on you", fg=c["brass"], bg=c["slate"],
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(
            fill="x", padx=16, pady=(14, 2))
        self.widgets.append(body.winfo_children()[-1])

        self.pending = tk.Label(body, text="", fg=c["sage"], bg=c["slate"],
                                anchor="w", justify="left", wraplength=660,
                                font=("Segoe UI", 9))
        self.pending.pack(fill="x", padx=16)
        self.widgets.append(self.pending)

        # The checker. A prediction, not an action - `tool_check`'s own docstring is
        # emphatic about that, and so is the label.
        row = tk.Frame(body, bg=c["slate"])
        row.pack(fill="x", padx=16, pady=(14, 0))
        self.widgets.append(row)
        tk.Label(row, text="What would happen if…", fg=c["brass"], bg=c["slate"],
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self.widgets.append(row.winfo_children()[-1])
        self.check = tk.Entry(row, fg=c["bone"], bg=c["obsidian"],
                              insertbackground=c["bone"], relief="flat",
                              font=("Cascadia Code", 9))
        self.check.pack(side="left", fill="x", expand=True, padx=10, ipady=4, ipadx=6)
        self.widgets.append(self.check)
        self.check.bind("<Return>", lambda _e: self._explain())

        actions = tk.Frame(body, bg=c["slate"])
        actions.pack(fill="x", padx=16, pady=(12, 14))
        self.widgets.append(actions)

        self.status = tk.Label(actions, text="", fg=c["sage"], bg=c["slate"],
                               anchor="w", justify="left", wraplength=440,
                               font=("Segoe UI", 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.widgets.append(self.status)

        self.attended_button = W.Button(
            actions, "Step back (unattended)", self._step_back, variant="ghost")
        self.attended_button.pack(side="right", padx=(6, 0))
        self.widgets.append(self.attended_button)

        reload_button = W.Button(actions, "Reload", self._reload, variant="ghost")
        reload_button.pack(side="right", padx=(6, 0))
        self.widgets.append(reload_button)

        self._load(call)

    def _load(self, call: Callable[..., dict]) -> None:
        try:
            res = call("status") or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return
        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return

        s = res.get("result") or {}
        attended = bool(s.get("attended"))
        ceiling = s.get("session_ceiling", "?")
        self.headline.configure(
            text=f"{'Attended' if attended else 'Unattended'}   |   "
                 f"ceiling {ceiling}",
            fg=palette()["bone"],
        )
        self.attended_button.configure(
            text="Step back (unattended)" if attended else "Already unattended")
        self.attended_button.configure(
            state="normal" if attended else "disabled")

        pending = call("confirm_list") or {}
        items = (pending.get("result") or {}).get("pending") or []
        if items:
            lines = [f"{len(items)} action(s) waiting for a decision:"]
            for item in items:
                lines.append(f"  {item.get('tool', '?')} - {item.get('reason', '')}")
            lines.append("")
            lines.append("Answer them in the prompt window. This window cannot: two "
                         "windows able to answer the same dialog means one of them is "
                         "the one you were not looking at.")
            self.pending.configure(text="\n".join(lines), fg=palette()["amber"])
        else:
            self.pending.configure(text="Nothing is waiting on you.",
                                   fg=palette()["sage"])

        self._say(f"{s.get('tools_registered', '?')} tools registered, "
                  f"{s.get('audit_records', '?')} audit records.", "sage")

    def _step_back(self) -> None:
        """Drop to unattended.

        The only direction this window offers, and the only direction the daemon
        accepts without a present channel. Going the other way is the presence
        detector's job: a button that claimed the user was present would be a button
        that lies about the one fact the whole tier model rests on.
        """
        try:
            res = self._call("session_set_attended", attended=False,
                             why="stepped back from the settings window") or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return
        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return
        self._load(self._call)
        self._say("Unattended. T2 and T3 now queue until you are back at the "
                  "keyboard.", "amber")

    def _explain(self) -> None:
        """Ask what a tool call would do. Never executes it.

        `tool_check` predicts; it does not act. The label above the box says so, and
        the reply says so in its own words, because a window that looked like it ran
        things would be a window people were afraid to type into.
        """
        wanted = (self.check.get() or "").strip()
        if not wanted:
            self._say("Type a tool name, such as file.delete.", "amber")
            return
        try:
            res = self._call("tool_check", tool=wanted, args={}) or {}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not reach the daemon: {exc}", "rust")
            return
        if not res.get("ok"):
            self._say(res.get("error", "unknown error"), "rust")
            return
        r = res.get("result") or {}
        self._say(f"{wanted}: {r.get('would')} at {r.get('tier')} - "
                  f"{r.get('reason')}", "sage")

    def _reload(self) -> None:
        self._load(self._call)

    def _say(self, message: str, tone: str) -> None:
        if self.status is not None:
            self.status.configure(text=message, fg=palette().get(tone, "#EDE8DC"))


class AdvancedSection(Section):
    """Everything that did not warrant a screen of its own, and the record.

    A read-only diagnostics panel plus the audit tail. Most of what is here has never
    had a way to be seen: which model is loaded, whether mail is configured, whether
    the vault bridge is up, what the assistant has taught itself, and the paths
    everything lives under.

    **Read-only on purpose.** Every *setting* has a screen where it can be changed
    with its consequences next to it; this is the one place that answers "what is the
    state of things", and a panel that mixed the two would be a panel where a stray
    click changes something. The one action is Reload.
    """

    def __init__(self) -> None:
        super().__init__("advanced", "Advanced", "settings")
        # The widgets kept by name. `dispose()` nulls exactly these, and the
        # `holds_widgets()` test would catch another being added and forgotten.
        self.named = ("facts", "record", "status", "vram", "path_field",
                      "path_verdict", "describe_out")
        self.facts: Any = None
        self.record: Any = None
        self.status: Any = None
        self.vram: Any = None
        self.path_field: Any = None
        self.path_verdict: Any = None
        self.describe_out: Any = None
        self._brain_running = False

    def build(self, parent: Any, call: Callable[..., dict]) -> None:
        import tkinter as tk

        c = palette()
        self._call = call
        body = tk.Frame(parent, bg=c["slate"], bd=0)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 16))
        self.widgets.append(body)

        # The tab is taller than the window: facts, three panels and the
        # audit tail do not fit 600 px, and did not fit before the panels
        # either. So the content scrolls. A plain viewport, not a drawn
        # control: the kit's Canvas objection is about replacing controls
        # with shapes, and a scrolling container keeps every control native.
        scroll = tk.Scrollbar(body, orient="vertical", relief="flat", bd=0,
                              width=12, bg=c["slate-line"],
                              troughcolor=c["obsidian"],
                              activebackground=c["brass"],
                              highlightthickness=0)
        scroll.pack(side="right", fill="y")
        self.widgets.append(scroll)
        view = tk.Canvas(body, bg=c["slate"], relief="flat", bd=0,
                         highlightthickness=0, yscrollcommand=scroll.set)
        view.pack(side="left", fill="both", expand=True)
        self.widgets.append(view)
        scroll.configure(command=view.yview)
        inner = tk.Frame(view, bg=c["slate"])
        self.widgets.append(inner)
        window_id = view.create_window((0, 0), window=inner, anchor="nw",
                                       tags=("advanced-inner",))
        inner.bind("<Configure>",
                   lambda _e: view.configure(scrollregion=view.bbox("all")))
        view.bind("<Configure>",
                  lambda e: view.itemconfigure(window_id, width=e.width))

        def _wheel(event: Any) -> None:
            view.yview_scroll(-1 * (event.delta // 120), "units")

        view.bind("<Enter>", lambda _e: view.bind_all("<MouseWheel>", _wheel))
        view.bind("<Leave>", lambda _e: view.unbind_all("<MouseWheel>"))

        self.facts = tk.Label(inner, text="", fg=c["bone"], bg=c["slate"],
                              anchor="w", justify="left", font=("Cascadia Code", 9))
        self.facts.pack(fill="x", padx=16, pady=(14, 10))
        self.widgets.append(self.facts)

        # What holds VRAM, in the daemon's own words. `status` carries `vram`
        # as a one-line string, which the facts block below cannot render (it
        # only prints dicts), so the holders line was silently absent with a
        # real daemon. Asked directly, read-only.
        vram_panel = W.Panel(inner, title="Video memory")
        vram_panel.pack(fill="x", padx=16, pady=(0, 8))
        self.widgets.append(vram_panel)
        self.vram = tk.Label(vram_panel.body, text="", fg=c["sage"],
                             bg=c["slate"], anchor="w", justify="left",
                             wraplength=520, font=("Segoe UI", 9))
        self.vram.pack(fill="x")
        self.widgets.append(self.vram)

        # What the model was given. `brain_describe` asks the child rather
        # than re-deriving locally, so on a cold daemon this starts the child
        # to ask it — the status line says which happened. Read-only either way.
        model_panel = W.Panel(inner, title="The model")
        model_panel.pack(fill="x", padx=16, pady=(0, 8))
        self.widgets.append(model_panel)
        model_row = tk.Frame(model_panel.body, bg=c["slate"])
        model_row.pack(fill="x")
        self.widgets.append(model_row)
        self.describe_out = tk.Label(model_row, text="not asked yet.",
                                     fg=c["sage"], bg=c["slate"], anchor="w",
                                     justify="left", wraplength=400,
                                     font=("Segoe UI", 9))
        self.describe_out.pack(side="left", fill="x", expand=True)
        self.widgets.append(self.describe_out)
        describe_button = W.Button(model_row, "What the model was given",
                                   self._describe, variant="ghost")
        describe_button.pack(side="right")
        self.widgets.append(describe_button)

        # Is this path approved, and what may happen in it. `workspace_covers`
        # only reads the manifests. An empty field asks nothing.
        path_panel = W.Panel(inner, title="Is this path approved")
        path_panel.pack(fill="x", padx=16, pady=(0, 8))
        self.widgets.append(path_panel)
        self.path_field = W.Field(path_panel.body, "Path",
                                  hint="a folder or file, then Check path",
                                  on_submit=lambda _v: self._check_path())
        self.path_field.pack(fill="x")
        self.widgets.append(self.path_field)
        path_row = tk.Frame(path_panel.body, bg=c["slate"])
        path_row.pack(fill="x", pady=(4, 0))
        self.widgets.append(path_row)
        self.path_verdict = tk.Label(path_row, text="no path checked yet.",
                                     fg=c["sage"], bg=c["slate"], anchor="w",
                                     justify="left", wraplength=400,
                                     font=("Segoe UI", 9))
        self.path_verdict.pack(side="left", fill="x", expand=True)
        self.widgets.append(self.path_verdict)
        check_button = W.Button(path_row, "Check path", self._check_path,
                                variant="ghost")
        check_button.pack(side="right")
        self.widgets.append(check_button)

        tk.Label(inner, text="Audit tail", fg=c["brass"], bg=c["slate"],
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(
            fill="x", padx=16, pady=(4, 2))
        self.widgets.append(inner.winfo_children()[-1])

        self.record = tk.Text(
            inner, fg=c["sage"], bg=c["obsidian"], relief="flat", wrap="none",
            height=12, font=("Cascadia Code", 8), padx=10, pady=8,
            insertbackground=c["bone"],
        )
        self.record.pack(fill="both", expand=True, padx=16)
        self.record.configure(state="disabled")
        self.widgets.append(self.record)

        row = tk.Frame(inner, bg=c["slate"])
        row.pack(fill="x", padx=16, pady=(10, 14))
        self.widgets.append(row)

        self.status = tk.Label(row, text="", fg=c["sage"], bg=c["slate"],
                               anchor="w", justify="left", wraplength=520,
                               font=("Segoe UI", 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.widgets.append(self.status)

        button = W.Button(row, "Reload", self._reload, variant="ghost")
        button.pack(side="right")
        self.widgets.append(button)

        self._load(call)

    def dispose(self) -> None:
        # The viewport binds MouseWheel at `all` level while hovered. That
        # binding outlives the widgets, so it is removed here, on the Tk
        # thread like the rest of teardown — otherwise a wheel event after
        # disposal fires into a destroyed canvas.
        for top in self.widgets:
            try:
                top.unbind_all("<MouseWheel>")
            except Exception:  # noqa: BLE001 - teardown must not mask
                pass
        super().dispose()

    # -- data ---------------------------------------------------------------

    @staticmethod
    def _fmt(value: Any) -> str:
        """A value fit for a person to read.

        `None` becomes "unknown" rather than the string "None". The first version
        interpolated the value directly, so a daemon reply missing a field rendered
        as `version          None` - which reads as a bug in the window rather than
        as a fact about the daemon, and the whole point of this panel is to be
        trusted when something *is* wrong.
        """
        if value is None:
            return "unknown"
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)

    def _ask(self, op: str, **args: Any) -> dict:
        """One op, never raising. A panel of six facts must survive one of them
        being unavailable - the vault is not installed on every machine, and a
        diagnostics screen that dies because one subsystem is absent is useless
        exactly when it is most wanted.
        """
        try:
            res = self._call(op, **args) or {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return res

    def _load(self, call: Callable[..., dict]) -> None:
        lines: list[str] = []
        self._brain_running = False

        status = self._ask("status")
        if status.get("ok"):
            s = status["result"]
            f = self._fmt
            lines.append(f"version          {f(s.get('version'))}")
            lines.append(f"uptime           {f(s.get('uptime_s'))}s")
            lines.append(f"attended         {f(s.get('attended'))}")
            lines.append(f"session ceiling  {f(s.get('session_ceiling'))}")
            lines.append(f"requests         {f(s.get('requests'))}")
            lines.append(f"tools            {f(s.get('tools_registered'))} registered")
            lines.append(f"audit records    {f(s.get('audit_records'))}")
            vram = s.get("vram") or {}
            if isinstance(vram, dict):
                for key in ("used_bytes", "ceiling_bytes", "holders"):
                    if key in vram:
                        lines.append(f"vram {key:<11} {vram[key]}")
        else:
            lines.append(f"status           unavailable: {status.get('error')}")

        brain = self._ask("brain_status")
        if brain.get("ok"):
            b = brain["result"]
            self._brain_running = bool(b.get("running"))
            lines.append("")
            lines.append(f"brain running    {self._fmt(b.get('running'))}")
            lines.append(f"brain tier       {self._fmt(b.get('tier'))}")
            lines.append(f"model loaded     {self._fmt(b.get('model_loaded'))}")
            idle = b.get("idle_s")
            lines.append(
                "idle             never used yet"
                if idle is None
                else f"idle             {idle}s of {b.get('ttl_s')}s"
            )
            lines.append(f"note             {b.get('note')}")

        mail = self._ask("mail_status")
        lines.append("")
        if mail.get("ok"):
            m = mail["result"]
            lines.append(f"mail configured  {self._fmt(m.get('configured'))}")
            for key in ("host", "username", "mailbox", "note"):
                if m.get(key):
                    lines.append(f"mail {key:<11} {m[key]}")
        else:
            lines.append(f"mail             unavailable: {mail.get('error')}")

        vault = self._ask("vault_status")
        lines.append("")
        if vault.get("ok"):
            v = vault["result"]
            lines.append(f"vault available  {self._fmt(v.get('available'))}")
            if v.get("error"):
                lines.append(f"vault error      {v['error']}")
            described = v.get("describe")
            if isinstance(described, dict):
                for key in ("path", "notes", "last_index"):
                    if described.get(key) is not None:
                        lines.append(f"vault {key:<11} {described[key]}")
        else:
            lines.append(f"vault            unavailable: {vault.get('error')}")

        reflect = self._ask("reflect_overlay")
        lines.append("")
        if reflect.get("ok"):
            r = reflect["result"] or {}
            lines.append(
                f"reflect overlay  {self._fmt(r.get('chars'))} chars promoted")
            for key in ("ledger", "candidates", "note"):
                if r.get(key) is not None:
                    lines.append(f"reflect {key:<9} {r[key]}")
        else:
            lines.append(f"reflect          unavailable: {reflect.get('error')}")

        # Paths, from the modules that own them rather than re-derived here.
        from . import modelconfig, voiceconfig

        lines.append("")
        lines.append(f"models dir       {modelconfig.MODELS_DIR}")
        lines.append(f"voices dir       {voiceconfig.VOICES_DIR}")
        lines.append(f"models config    {modelconfig.DEFAULT_CONFIG_PATH}")
        lines.append(f"voices config    {voiceconfig.DEFAULT_CONFIG_PATH}")

        self.vram.configure(text=self._vram_line(self._ask("arbiter_summary")))

        self.facts.configure(text="\n".join(lines))

        tail = self._ask("audit_tail", limit=40)
        self.record.configure(state="normal")
        self.record.delete("1.0", "end")
        if tail.get("ok"):
            records = tail.get("result") or []
            for r in records:
                if not isinstance(r, dict):
                    continue
                self.record.insert(
                    "end",
                    f"{str(r.get('timestamp', ''))[:19]:<20}"
                    f"{r.get('decision', ''):<8}"
                    f"{r.get('tier', ''):<4} {r.get('actor', ''):<12}"
                    f"{r.get('tool', '')}\n",
                )
            self._say(f"{len(records)} of {len(records)} record(s) shown. "
                      f"Read-only.", "sage")
        else:
            self.record.insert("end", f"unavailable: {tail.get('error')}\n")
            self._say(f"the audit tail is unavailable: {tail.get('error')}", "rust")
        self.record.configure(state="disabled")

    def _reload(self) -> None:
        self._load(self._call)

    @staticmethod
    def _vram_line(res: dict) -> str:
        """The holders line, or a sentence rather than a repr.

        Older fakes (and any daemon that answers otherwise) return a dict or
        nothing here; the real daemon returns the one-line string. None of
        those may reach the screen as `None` or `{}`.
        """
        if not res.get("ok"):
            return "unavailable."
        summary = res.get("result")
        if isinstance(summary, str) and summary:
            return summary
        return "no summary reported."

    def _describe(self) -> None:
        """Ask the child what the model was actually given, and show it."""
        res = self._ask("brain_describe")
        if not res.get("ok"):
            self.describe_out.configure(
                text=f"unavailable: {self._fmt(res.get('error'))}")
            self._say(
                f"the description is unavailable: "
                f"{self._fmt(res.get('error'))}", "rust")
            return
        result = res.get("result") or {}
        if isinstance(result, dict):
            shown = [f"{k:<16} {self._fmt(v)}" for k, v in result.items()][:12]
            if len(result) > 12:
                shown.append(f"... ({len(result) - 12} more)")
            self.describe_out.configure(text="\n".join(shown) or "empty")
            count = len(result)
        else:
            self.describe_out.configure(text=self._fmt(result))
            count = 1
        started = "" if self._brain_running else "the brain child was started to ask. "
        self._say(f"{started}{count} field(s) shown. Read-only.", "sage")

    def _check_path(self) -> None:
        """Is this path approved, and what may happen in it. Read-only."""
        path = self.path_field.get().strip()
        if not path:
            self.path_verdict.configure(text="type a path first.")
            return
        res = self._ask("workspace_covers", path=path)
        if not res.get("ok"):
            self.path_verdict.configure(
                text=f"unavailable: {self._fmt(res.get('error'))}")
            self._say(
                f"the path check is unavailable: "
                f"{self._fmt(res.get('error'))}", "rust")
            return
        hit = res.get("result")
        if hit is None:
            self.path_verdict.configure(
                text="outside every approved workspace.")
            self._say("that path is outside every approved workspace.", "amber")
            return
        if not isinstance(hit, dict):
            self.path_verdict.configure(text=self._fmt(hit))
            return
        tiers = hit.get("allow_tiers")
        if isinstance(tiers, list):
            tiers_s = ", ".join(str(t) for t in tiers) or "none"
        else:
            tiers_s = self._fmt(tiers)
        self.path_verdict.configure(
            text=f"approved in {hit.get('name', 'unknown')} "
                 f"[{hit.get('danger_level', 'unknown')}] "
                 f"{hit.get('root', 'unknown')}\nallowed here: {tiers_s}")
        self._say(f"{path} is approved in {hit.get('name', 'unknown')}.", "sage")

    def _say(self, message: str, tone: str) -> None:
        if self.status is not None:
            self.status.configure(text=message, fg=palette().get(tone, "#EDE8DC"))


class Settings:
    """The window. One Tk thread, one queue, built like `overlay.Overlay`.

    The threading is duplicated from `Overlay` rather than shared with it, and the
    reason is worth stating because "why not refactor" is normally the wrong answer.
    `Overlay` has 1200 lines and the scar tissue of D67 (blank window), D68 (dropped
    tool) and D69 (undialogable confirm) on it. Sharing its machinery would mean
    either a base class whose behaviour both windows must satisfy - and a settings
    bug would then be a chat-window bug - or a generic thread-pump helper extracted
    from code that is known-fragile. The queue-and-timeout pattern is 30 lines and
    is genuinely easier to write twice than to abstract safely.

    `call` is the daemon pipe client, the same callable `overlay.Overlay` takes. It
    is a `client` rather than a daemon reference, which is the property that makes
    this a client too.
    """

    #: Every attribute that may hold a widget, and must therefore be nulled in
    #: `_release_widgets`.
    #:
    #: Declared as data and asserted against the live instance by
    #: `test_every_widget_attribute_is_released`, so a widget attribute added
    #: without adding it here fails a test rather than leaking a Tcl interpreter at
    #: shutdown. That is not hypothetical: the first version of this class leaked
    #: `_head` and `_title_label`, and pytest exited `-2147483645` - D69's exact
    #: failure, reproduced in a new module, from the fix I was writing.
    _WIDGET_ATTRS = ("_root", "_body", "_head", "_title_label")

    def widget_attrs_still_held(self) -> list[str]:
        """Widget attributes that survived teardown. Empty is the only good answer."""
        return [
            name for name in self._WIDGET_ATTRS
            if getattr(self, name, None) is not None
        ]

    def __init__(
        self,
        call: Callable[..., dict],
        *,
        title: str = "Butleroy Settings",
        state_path: Any = None,
        models_dir: Any = None,
    ) -> None:
        self._call = call
        self._title = title
        self._state_path = state_path
        self._models_dir = models_dir

        self._root: Any = None
        self._closing = False
        self._events: queue.Queue = queue.Queue()
        self._tk: threading.Thread | None = None
        self._root_ready = threading.Event()

        self._sections: dict[str, Section] = {}
        self._buttons: list[Any] = []
        self._current: str | None = None
        self._body: Any = None
        # Two more widget references, and they are the ones that leaked the first
        # time. Every attribute here that can hold a widget must be nulled in
        # `_release_widgets`; `test_every_widget_attribute_is_released` checks the
        # list against the instance so a new one cannot be added and forgotten.
        self._head: Any = None
        self._title_label: Any = None
        # Whatever killed the Tk thread, if something did. Set in `_tk_main`'s
        # except and read by `show()`, so a window that cannot open says why rather
        # than leaving the caller with a timeout and no cause.
        self._tk_error: str | None = None

    # -- lifecycle ----------------------------------------------------------

    def show(self) -> None:
        """Open the window. Safe to call from any thread, more than once.

        Idempotent because a tray menu item is a click, and a user who clicks
        "Settings" three times should get one window rather than three.
        """
        if not self._ensure_tk():
            return
        self._invoke(self._show_now)

    def _show_now(self) -> None:
        if self._root is None:
            return
        self._root.deiconify()
        self._root.lift()
        try:
            self._root.focus_force()
        except Exception:  # noqa: BLE001 - focus is a nicety, not a requirement
            pass

    def hide(self) -> None:
        if self._closing or self._root is None:
            return
        self._invoke(self._root.withdraw)

    def destroy(self) -> None:
        """Tear down. Idempotent, and safe to call twice (D69's lesson)."""
        if self._root is not None and not self._closing:
            try:
                self._invoke(self._destroy_now, timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        self._closing = True
        thread = self._tk
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    @property
    def is_open(self) -> bool:
        return self._root is not None

    def _ensure_tk(self) -> bool:
        thread = self._tk
        if thread is not None and thread.is_alive() and self._root is not None:
            return True
        if self._closing:
            return False
        self._root_ready.clear()
        self._tk_error = None
        self._tk = threading.Thread(target=self._tk_main, name="settings-tk",
                                   daemon=True)
        self._tk.start()
        ready = self._root_ready.wait(timeout=10.0)
        if not ready or self._root is None:
            # Say why. A caller that only learns "the window did not open" cannot
            # report anything useful, and the whole reason this attribute exists is
            # that the first version of `_tk_main` discarded exactly this.
            detail = self._tk_error or "the Tk thread did not become ready"
            raise RuntimeError(f"Butleroy settings window: {detail}")
        return True

    def _tk_main(self) -> None:
        """The one thread permitted to touch Tk, and the place failures are kept.

        The `except` **records** rather than swallowing. The first version set
        `_root_ready` and discarded the exception, which is why a defect that made
        the window fail to build after a dozen open/close cycles presented as an
        unexplained ten-second `_invoke` timeout in an unrelated test - the one
        piece of evidence that would have identified it was thrown away. Same reason
        `Overlay` calls `ipc.note` here.
        """
        try:
            root = self._build()
            self._root = root
            self._root_ready.set()
            root.after(50, self._pump)
            root.mainloop()
        except BaseException as exc:  # noqa: BLE001 - never leave a waiter blocked
            self._tk_error = f"{type(exc).__name__}: {exc}"
            self._root_ready.set()
        finally:
            # D69's exit code, by the same route and for the same reason: widget
            # references outlive `self._root`, and a Tcl interpreter released at
            # process shutdown from the wrong thread is `-2147483645` on Windows.
            self._release_widgets()

    def _release_widgets(self) -> None:
        """Let go of every widget. On the Tk thread, in `finally`, always.

        This function is the whole of D69's fix and it is the reason the settings
        window can exist at all. Widget attributes outlive `self._root`; each one
        holds `.tk`, which is the Tcl interpreter; and an interpreter still alive at
        *process* shutdown has its per-thread async handler deleted from the wrong
        thread, which on Windows is a debug break. The process then exits
        `-2147483645` after a perfectly clean shutdown, which every scheduled task
        and supervising script reads as failure.

        The first version of this cleared `self._sections` and left `self._head`,
        `self._title_label` and each section's own attributes pointing at live
        widgets. It reproduced the exact bug it was written to prevent, and showed
        up as the exact exit code above. Every attribute that can hold a widget is
        named here explicitly, because "clear the collections" is not the same thing
        and looks identical from the outside.
        """
        for section in self._sections.values():
            try:
                section.dispose()
            except Exception:  # noqa: BLE001
                pass
        self._sections.clear()
        self._buttons.clear()
        # Named rather than iterated: an attribute added later must be added here
        # too, and a test asserts the list is complete.
        self._root = None
        self._body = None
        self._current = None
        self._head = None
        self._title_label = None

        # Collect *here*, on the Tk thread. Nulling the attributes above is
        # necessary but not sufficient: the `after` callbacks and the event queue
        # leave reference cycles, and a cycle holding the Tcl interpreter is only
        # broken by the cyclic collector. Left to the next collection - which in a
        # long-lived tray may be at *process* exit, on the main thread - the
        # interpreter is destroyed by a thread that did not create it, and Tcl
        # aborts with `Tcl_AsyncDelete: async handler deleted by the wrong thread`.
        #
        # Measured, not assumed: with this line the settings window and the overlay
        # coexist under one pytest process and the exit code is 0; without it the
        # same pair exits `-2147483645`. The symptom is identical to the leak it
        # sits next to, which is exactly why it was worth separating the two cases
        # rather than assuming one fix covered both.
        gc.collect()

    def _destroy_now(self) -> None:
        if self._root is not None:
            self._root.quit()
            self._root.destroy()

    # -- threading ----------------------------------------------------------

    def _invoke(self, fn: Any, *args: Any, timeout: float = 10.0) -> Any:
        """Run `fn` on the Tk thread and wait, exactly as `Overlay._invoke` does.

        Including the re-entrancy branch: `_select` calls `show()`, and an
        unconditional post-and-wait there would wait for itself forever. Not
        hypothetical - it is the path taken every time the window is opened while
        another call is in flight.
        """
        if threading.current_thread() is self._tk:
            return fn(*args)
        if self._tk is None or not self._tk.is_alive():
            return None
        slot: dict = {}
        finished = threading.Event()

        def runner() -> None:
            try:
                slot["value"] = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                slot["error"] = exc
            finally:
                finished.set()

        self._events.put(("call", runner))
        if not finished.wait(timeout=timeout):
            raise RuntimeError(
                f"the settings Tk thread did not run {fn!r} within {timeout}s"
            )
        if "error" in slot:
            raise slot["error"]
        return slot.get("value")

    def _pump(self) -> None:
        """Drain the queue. An `after` callback, so the event loop drives it."""
        if self._closing or self._root is None:
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "call":
                    payload()
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001 - one bad call must not stop the loop
            pass
        finally:
            if not self._closing and self._root is not None:
                self._root.after(50, self._pump)

    # -- construction -------------------------------------------------------

    def _build(self) -> Any:
        import tkinter as tk

        c = palette()
        root = tk.Tk()
        root.title(self._title)
        root.configure(bg=c["obsidian"])
        root.geometry("900x600+120+100")
        root.minsize(720, 480)
        # NOT always-on-top, for the same reason the chat window is not: a pinned
        # settings window covers the desktop for as long as it is open.

        head = tk.Frame(root, bg=c["slate"], height=52)
        head.pack(fill="x")
        head.pack_propagate(False)
        self._head = head

        title = tk.Label(head, text="Butleroy", fg=c["brass"], bg=c["slate"],
                         font=("Segoe UI", 13, "bold"), anchor="w")
        title.pack(side="left", padx=18)
        self._title_label = title

        tk.Label(head, text="LOCALAIZED CONCIERGE", fg=c["sage"], bg=c["slate"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 18))

        # Sections are listed in this order rather than discovered, because a
        # settings window whose navigation depends on a registry lookup is a window
        # that can come up empty with nothing wrong anywhere.
        self._order = ["models", "voices", "workspaces", "authority", "advanced"]
        self._sections = {
            "models": ModelsSection(),
            "voices": VoicesSection(),
            "workspaces": WorkspacesSection(),
            "authority": AuthoritySection(),
            "advanced": AdvancedSection(),
        }

        for key in self._order:
            section = self._sections[key]
            # A ghost button whose text colour marks the active page. Configured after
            # construction rather than being a variant of its own, because "active tab"
            # is a property of the *set* of buttons rather than of one button.
            button = W.Button(head, section.title, lambda k=key: self._select(k),
                              variant="ghost")
            button.configure(fg=c["sage"], activeforeground=c["bone"], padx=16)
            button.pack(side="left")
            self._buttons.append(button)

        self._body = tk.Frame(root, bg=c["obsidian"])
        self._body.pack(fill="both", expand=True)

        self._select("models")
        return root

    def _select(self, key: str) -> None:
        """Swap the right-hand pane. Built lazily, so a broken section is one
        red row rather than a window that will not open.

        Which matters: a settings window whose *first* section raises leaves the
        user with no way to reach the others, and "fix the thing that is broken"
        stops being possible from inside the broken window.
        """
        section = self._sections.get(key)
        if section is None or self._body is None:
            return

        try:
            self._invoke(self._select_now, key, section)
        except Exception as exc:  # noqa: BLE001
            self._note(f"could not open {section.title}: {exc}")

    def _select_now(self, key: str, section: Section) -> None:
        import tkinter as tk

        for child in self._body.winfo_children():
            child.destroy()

        try:
            section.build(self._body, self._call)
        except Exception as exc:  # noqa: BLE001 - one section, not the window
            c = palette()
            self._body.configure(bg=c["obsidian"])
            tk.Label(self._body, text=f"{section.title}: {exc}", fg=c["rust"],
                     bg=c["obsidian"], wraplength=600, justify="left",
                     font=("Segoe UI", 10)).pack(padx=24, pady=24, anchor="w")

        self._current = key
        c = palette()
        for button, bkey in zip(self._buttons, self._order):
            button.configure(fg=c["brass"] if bkey == key else c["sage"])

    def _note(self, message: str) -> None:
        """Surface a failure where the user is already looking."""
        if self._body is None:
            return
        try:
            self._invoke(self._note_now, message)
        except Exception:  # noqa: BLE001
            pass

    def _note_now(self, message: str) -> None:
        import tkinter as tk

        c = palette()
        tk.Label(self._body, text=message, fg=c["rust"], bg=c["obsidian"],
                 font=("Segoe UI", 9)).pack(padx=24, pady=12, anchor="w")