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

        row = tk.Frame(body, bg=c["slate"])
        row.pack(fill="x", padx=16, pady=(12, 14))
        self.widgets.append(row)

        self.status = tk.Label(row, text="", fg=c["sage"], bg=c["slate"], anchor="w",
                               font=("Segoe UI", 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.widgets.append(self.status)

        for label, command in (("Reload", self._reload),
                               ("Save", self._save)):
            button = tk.Button(
                row, text=label, command=command, relief="flat", bd=0,
                bg=c["slate"], fg=c["brass"], activebackground=c["slate-line"],
                activeforeground=c["brass-lit"], font=("Segoe UI", 9),
                padx=14, pady=4, cursor="hand2",
            )
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
        self._order = ["models"]
        self._sections = {"models": ModelsSection()}

        for key in self._order:
            section = self._sections[key]
            button = tk.Button(
                head, text=section.title, relief="flat", bd=0,
                bg=c["slate"], fg=c["sage"], activebackground=c["slate-line"],
                activeforeground=c["bone"], font=("Segoe UI", 9),
                padx=16, pady=6, cursor="hand2",
                command=lambda k=key: self._select(k),
            )
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