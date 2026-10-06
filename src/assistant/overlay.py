"""The window the user actually looks at. tkinter, because it is in the stdlib.

Why tkinter and not PySide6
--------------------------
The same reasoning that made the workspace manifest TOML rather than YAML: a
window that guards your filesystem should not need a third-party parser. PySide6
is a ~100 MB install for a frameless window, a text widget and a topmost flag -
all of which tkinter does natively. (PLAN.md still says PySide6 and is wrong.)

What this window is allowed to do
---------------------------------
Everything goes through `daemon.handle`, exactly as the tray and the pipes do
(D31). There is no path from a widget to the filesystem, and no way to widen
authority by reaching a different object. A widget that could approve on its own
would be a second, unaudited front door to a T3 action.

The rules this implements, and why each one is here
--------------------------------------------------
D32 makes a confirmation a foreground, payload-bound, single-use human act.

* **The resolved path is shown, not the one the model proposed.** A model that
  says `Downloads/tmp` when `tmp` is a junction to your thesis produces a dialog
  that is entirely accurate about what it says and wrong about what will happen.
  So the preview's `resolved` is displayed, alongside the `proposed` string when
  they differ. Nothing is silently redirected, and the audit line cannot say "you
  approved a.txt" when what was approved was a symlink.
* **The files are enumerated, never the pattern.** `*.tmp` matching 3 files now
  and 11,000 at execute time is the difference between an informed decision and a
  rubber stamp, so the count and the byte total are shown even when the list is
  truncated.
* **A timeout denies.** A countdown that expires into *approval* is a footgun;
  one that expires into *denial* is merely annoying. So the timer runs down to a
  deny, and there is no timer at all while the session is unattended - a
  countdown the user cannot see is worse than no countdown.
* **There is no "always allow".** Two scopes: this call, or this class for 600 s.
  A permanent grant over every approved root is a standing delete capability, and
  "delete 40 files" should not be 40 dialogs either - which is what trains a user
  to hit Enter without reading.
* **Only this window approves.** Not the tray, not Telegram, not the model. If
  the dialog is not on screen, nothing is approved.

The stream
----------
`render_delta` receives the real accumulated pieces the child returns
(`brain.stream` hands back `pieces` and the joined `text`). It is fed in order,
so a partial answer appears as it is produced rather than after the fact. There
is no simulated stream anywhere: a mechanism that silently does nothing is worse
than one that is absent (D36).
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable

from . import ipc

__all__ = ["Overlay", "OVERLAY_IDLE_TTL_S"]

# How long a T3 dialog waits before denying. Long enough to read a list of
# files, short enough that walking away does not leave a live dialog holding a
# destructive action. The user may pick a duration in the UI.
OVERLAY_IDLE_TTL_S = 60.0

# D32's class grant. The user chose 600s; the daemon enforces it too, and this
# copy exists only so the dialog can say what it is about to grant.
CLASS_GRANT_SECONDS = 600.0

BG = "#14161a"
FG = "#e6e6e6"
MUTED = "#8b8b8b"
ACCENT = "#6aa9ff"
DANGER = "#ff6a5a"
OK = "#6ac47f"
PANEL = "#1e2128"


class Overlay:
    """The prompt window and the confirmation dialog.

    `call` is a callable taking `(op, **args)` and returning the daemon's
    envelope. It is injected rather than constructed from a Daemon so the tests
    can drive this whole module with no tkinter display and no daemon at all.
    """

    def __init__(
        self,
        call: Callable[..., dict],
        *,
        title: str = "Assistant",
        ttl_s: float = OVERLAY_IDLE_TTL_S,
    ) -> None:
        self._call = call
        self._title = title
        self._ttl_s = ttl_s
        self._root: tk.Tk | None = None
        self._closing = False
        # Confirmation ids already answered by this window. Keyed on the id the
        # daemon issued, so a re-polled pending list cannot raise a dialog twice.
        self._answered: set[str] = set()
        # (0, "model", text) - the daemon's envelope is unwrapped by the caller.
        self._events: queue.Queue = queue.Queue()
        self._busy = False
        # --- the Tk thread, and the three things it owns ---------------------
        # All three were previously created lazily inside `_build`, which meant
        # they did not exist until a window had been built - and every method
        # that touched one before then raised AttributeError. `_dlg` was read by
        # `_poll` on its very first call, `_poll_job` by anything inspecting the
        # schedule. Present from construction instead.
        self._dlg: "_Confirm | None" = None
        self._poll_job: str | None = None
        self._tk: threading.Thread | None = None
        self._root_ready = threading.Event()

    # --- lifecycle ---------------------------------------------------------

    def _tk_main(self) -> None:
        """The one thread permitted to touch Tk.

        tkinter binds a root to the thread that created it: every later call from
        any other thread is undefined, and `root.update()` from the wrong thread
        raises `RuntimeError: main thread is not in main loop` - measured, not
        remembered. So the thread that builds the root runs the event loop and
        every widget call is funnelled here through the queue.

        The event loop is the part that was missing entirely, and its absence is
        why the window used to open blank. A `Tk()` that is never pumped still
        maps a window with its title and geometry - `winfo_viewable` is 1 - and
        paints nothing, because painting *is* event processing. It looks like a
        working window to every check that is not a screenshot.
        """
        try:
            root = self._build()
            self._root = root
            # Set before anything can wait on it, including the failure path
            # below, so a dead Tk thread cannot leave a caller blocked forever.
            self._root_ready.set()
            # Two loops, both on this thread. `_pump` drains the cross-thread
            # queue (brain output, show/hide requests); `_poll` raises pending T3
            # confirmations. Both are `after` callbacks, so the event loop is what
            # drives them and neither needs a thread of its own.
            root.after(50, self._pump)
            self._poll()
            root.mainloop()
        except BaseException as exc:  # noqa: BLE001 - report, never hang a waiter
            ipc.note(f"overlay tk thread died: {type(exc).__name__}: {exc}")
            self._root_ready.set()
        finally:
            # Release every Tk object *here*, on the thread that created them.
            # `self.transcript`, `self.entry`, `self.status` and `_dlg` are widget
            # objects and each holds `.tk` - the Tcl interpreter - which is why
            # simply dropping `self._root` was not enough: the interpreter stayed
            # alive, held by attributes that live on the Overlay, and was finally
            # released at *process* shutdown, on the main thread. Tcl then deletes
            # a per-thread async handler from the wrong thread and says so:
            #   Tcl_AsyncDelete: async handler deleted by the wrong thread
            # On Windows that raises a debug break, so the daemon exited
            # -2147483645 instead of 0 after a perfectly clean shutdown - which a
            # scheduled task or any supervising script reads as failure.
            self._root = None
            self.transcript = None      # type: ignore[assignment]
            self.entry = None           # type: ignore[assignment]
            self.status = None          # type: ignore[assignment]
            self._dlg = None

    def _ensure_tk(self) -> bool:
        """Start the Tk thread if it is not already running. Returns readiness."""
        thread = self._tk
        if thread is not None and thread.is_alive() and self._root is not None:
            return True
        if self._closing:
            return False
        self._root_ready.clear()
        self._tk = threading.Thread(target=self._tk_main, name="overlay-tk",
                                   daemon=True)
        self._tk.start()
        # Bounded, because "wait forever" here means a dead Tk thread hangs the
        # hotkey thread, which hangs the tray, which looks like a dead assistant.
        return self._root_ready.wait(timeout=10.0)

    def _invoke(self, fn: Any, *args: Any, timeout: float = 10.0) -> Any:
        """Run `fn` on the Tk thread and wait for its result.

        Re-entrant by necessity: `_ask` calls `self.show()` while already on the
        Tk thread, and an unconditional post-and-wait there would wait for
        itself forever. That is not hypothetical - it is the path taken every
        time a prompt arrives from Telegram while the window is already open.
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
                f"the overlay Tk thread did not run {fn!r} within {timeout}s"
            )
        if "error" in slot:
            raise slot["error"]
        return slot.get("value")

    def show(self) -> None:
        """Raise the window. Safe to call from any thread."""
        if not self._ensure_tk():
            return
        self._invoke(self._show_now)

    def _show_now(self) -> None:
        if self._root is None:
            return
        try:
            self._root.deiconify()
            self._root.lift()
            self._root.focus_force()
        except tk.TclError:
            pass

    def hide(self) -> None:
        if self._root is None:
            return
        self._invoke(self._hide_now)

    def _hide_now(self) -> None:
        if self._root is None:
            return
        try:
            self._root.withdraw()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        """Stop the window and its thread.

        Safe to call more than once, and safe on an overlay that was never shown.
        The sentinel goes in *before* `_closing` is set, because the pump treats
        `_closing` as "drop everything queued" - and the sentinel is the one thing
        it must not drop.
        """
        thread = self._tk
        if self._root is None and (thread is None or not thread.is_alive()):
            self._closing = True
            return
        self._events.put(None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)
        self._closing = True
        self._tk = None
        # `self._root` is cleared by the Tk thread on its way out, so the Tk object
        # is finalised by the thread that created it. Only clear it here when no
        # thread ever owned it - if the join timed out, the thread is still running
        # and pretending otherwise would leave the window alive with nothing
        # pointing at it.
        if thread is None or not thread.is_alive():
            self._root = None

    def is_visible(self) -> bool:
        return self._root is not None and bool(self._root.state() != "withdrawn")

    def _pump(self) -> None:
        """Drain queued events. Runs *on* the Tk thread, as an `after` callback.

        tkinter is not thread-safe, so nothing here may touch a widget directly;
        everything arrives through the queue and is applied here. It used to be a
        thread of its own, which meant widget calls from a thread that did not own
        the root - the other half of the blank-window defect, and the one that
        would have surfaced as soon as the event loop was fixed.

        Re-arms itself through `after` rather than looping and sleeping, so the
        event loop stays the single scheduler and there is no timer to leak.
        """
        if self._root is None:
            return
        try:
            while True:
                try:
                    ev = self._events.get_nowait()
                except queue.Empty:
                    break
                # The sentinel is handled *before* any _closing check. destroy()
                # has to set _closing, and gating on it here meant the sentinel
                # was never read: the pump returned at its guard, the root was
                # never destroyed, mainloop() never returned, and destroy() sat
                # in its join for the whole timeout on every single shutdown.
                if ev is None:
                    self._closing = True
                    try:
                        self._root.destroy()
                    except tk.TclError:
                        pass
                    return
                if self._closing:
                    # Drop whatever else is queued; we are on the way out.
                    continue
                self._handle_event(*ev)
        finally:
            if not self._closing and self._root is not None:
                try:
                    self._root.after(50, self._pump)
                except tk.TclError:
                    pass

    def _post(self, *ev: Any) -> None:
        if not self._closing:
            self._events.put(ev)

    # --- construction ------------------------------------------------------

    def _build(self) -> tk.Tk:
        root = tk.Tk()
        root.title(self._title)
        root.configure(bg=BG)
        root.geometry("760x520+80+80")
        root.minsize(560, 380)
        # NOT always-on-top, unlike the T3 dialog below. This window is the
        # conversation, and pinning it above everything means it covers the
        # desktop for as long as it is open - which on a machine with your own
        # work on the screen is an obstacle, not a feature. `show()` already
        # raises it on demand with `lift()` and `focus_force()`, so summoned is
        # foreground; left alone, it is out of the way.

        mono = tkfont.nametofont("TkFixedFont")

        head = tk.Label(
            root, text="Ask", fg=ACCENT, bg=BG,
            font=("Segoe UI", 11, "bold"), anchor="w",
        )
        head.pack(fill="x", padx=18, pady=(14, 0))

        self.transcript = tk.Text(
            root, fg=FG, bg=PANEL, insertbackground=FG, wrap="word",
            font=mono, height=18, relief="flat", padx=12, pady=10,
            state="disabled",
        )
        self.transcript.pack(fill="both", expand=True, padx=18, pady=(8, 8))
        self.transcript.tag_configure("muted", foreground=MUTED)
        self.transcript.tag_configure("good", foreground=OK)
        self.transcript.tag_configure("bad", foreground=DANGER)
        self.transcript.tag_configure("accent", foreground=ACCENT)

        entry_row = tk.Frame(root, bg=BG)
        entry_row.pack(fill="x", padx=18, pady=(0, 12))
        self.entry = tk.Entry(
            entry_row, fg=FG, bg=PANEL, insertbackground=FG,
            font=mono, relief="flat",
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, ipadx=8)
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.entry.bind("<Escape>", lambda _e: self.hide())

        self.status = tk.Label(root, text="", fg=MUTED, bg=BG, anchor="w")
        self.status.pack(fill="x", padx=18, pady=(0, 10))

        # Deliberately does NOT start `_poll` or `_pump` here. Both need
        # `self._root` to exist, and `self._root` is not assigned until `_build`
        # returns - so calling them from in here ran them against `None`. `_poll`
        # opens with `if self._closing or self._root is None: return`, so it
        # returned at once and the one-second confirmation poll was never armed:
        # a destructive action queued from Telegram would silently never raise a
        # dialog. `_tk_main` starts them, on this thread, after the root is set.
        return root

    def _poll(self) -> None:
        """Raise the next pending confirmation, one at a time, every second.

        Polled rather than pushed because a confirmation can also arrive from
        Telegram or the scheduler, and a dialog that only appeared because the
        local user happened to type would be a dialog that silently did not
        appear.

        The state kept is `_answered`, not `_seen`. Marking an item as *seen* the
        moment it is offered is the bug this replaced: the second queued
        confirmation was recorded, refused a dialog because one was already up,
        and was then skipped by every later poll - so it never appeared at all,
        and the user had a pending destructive action with no way to reach it.
        """
        if self._closing or self._root is None:
            return
        if self._dlg is None:
            try:
                res = self._call("confirm_list")
                pending = (res or {}).get("result", {}).get("pending", []) or []
            except Exception:
                pending = []
            for entry in pending:
                key = entry.get("id")
                if key and key not in self._answered:
                    self._present(entry)
                    break
        try:
            self._poll_job = self._root.after(1000, self._poll)
        except tk.TclError:
            pass

    # --- transcript --------------------------------------------------------

    def _append(self, text: str, tag: str = "") -> None:
        if self._root is None:
            return
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text + "\n", tag)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def say(self, text: str, tag: str = "") -> None:
        self._post("say", text, tag)

    def render_delta(self, piece: str) -> None:
        """Append one streamed fragment.

        Called with the real pieces the child produced, in order. Streaming a
        half-written JSON object is fine here: this is prose, not a tool call.
        A tool call's text is never streamed, because a tool call that appears
        mid-stream looks like something that has already happened.
        """
        self._post("delta", piece)

    def set_status(self, text: str) -> None:
        self._post("status", text)

    # --- events ------------------------------------------------------------

    def _handle_event(self, kind: str, *rest: Any) -> None:
        if kind == "say":
            self._append(str(rest[0]), str(rest[1]) if len(rest) > 1 else "")
        elif kind == "delta":
            self._append(str(rest[0]))
        elif kind == "status":
            if self._root is not None:
                try:
                    self._root.after(0, lambda: self.status.configure(text=str(rest[0])))
                except tk.TclError:
                    pass
        elif kind == "prompt":
            self._ask(str(rest[0]))
        elif kind == "call":
            # A `_invoke` request: run it here, on the Tk thread, so the caller
            # on another thread can wait for the result.
            rest[0]()

    def _submit(self) -> None:
        prompt = self.entry.get().strip()
        if not prompt or self._busy:
            return
        self.entry.delete(0, "end")
        self._busy = True
        self.set_status("thinking...")
        threading.Thread(target=self._run_turn, args=(prompt,), daemon=True).start()

    def _run_turn(self, prompt: str) -> None:
        """Ask the daemon for a turn, off the Tk thread.

        The turn costs seconds - it may load a 4 GB model first - so it cannot
        run on the Tk thread without freezing the window the user is looking at,
        which for a confirmation dialog would be its own kind of unsafe.

        A turn also does not end at the proposal. `brain_turn` returns a
        *proposed* call and nothing else - that is its contract, and it is the
        right one - so a front door that stops there leaves the user looking at a
        plan with nothing behind it. See `_carry_through`.
        """
        try:
            res = self._call("brain_turn", prompt=prompt, channel="pc", actor="overlay")
        except Exception as exc:
            self._post("say", f"the brain is not answering: {exc}", "bad")
            self._post("status", "")
            self._busy = False
            return
        try:
            body = (res or {}).get("result", {}) or {}
            kind = body.get("kind")
            if kind == "answer":
                self._post("say", body.get("text", ""), "")
            elif kind == "tool":
                self._carry_through(body)
            else:
                self._post("say", "the brain returned nothing usable", "bad")
        finally:
            # Never leave the window permanently deaf. `_busy` gates `_submit`,
            # so a raise anywhere above that skipped the reset would leave a
            # window that looks entirely normal and refuses every later question
            # - no exception on screen, no log line, nothing to report.
            self._busy = False
            self._post("status", "")

    def _carry_through(self, body: dict) -> None:
        """Send a proposed call back through the gate, and show what came of it.

        `brain_turn` proposes and `tool_call` is the only path to an action. The
        window is a front door like Telegram and voice, so it gets no private
        route: the proposal goes back through `tool_call` and the gate rules on
        it exactly as it would for anyone else. That is also why this is safe to
        do without asking - the gate is still upstream, and a T3 action still
        comes back as a confirmation id that this window then has to be asked
        about.

        This used to stop one line after printing `proposing <tool> (<args>)`.
        The consequences were worse than an unimplemented feature, because
        nothing said so:

        * the transcript read as though the assistant were working, so the
          failure looked like a slow answer rather than a dead end;
        * the audit chain verified, because the proposal *was* audited - D68;
        * and a T0 read like `system.gpu_state` needs no approval at all, so the
          one category of tool that should have worked most obviously did not.

        The four outcomes below are all of the things `tool_call` can legitimately return,
        and each is reported rather than swallowed. An unbuilt tool is named as
        unbuilt: "I will not do this" and "I have not built this yet" are different
        answers, and conflating them is how an assistant ends up reporting
        unfinished work as finished (D58).

        One turn, one tool, on purpose. Feeding the result back for a second model
        turn would be a genuine improvement and it is deliberately not here: that is
        a loop with its own budget, its own cancellation and its own way of
        surprising the user with four minutes of silence, and it deserves to be a
        decision rather than a side effect of fixing a dead end.
        """
        parse_error = body.get("parse_error")
        if parse_error:
            self._post("say", f"the model tried to call a tool but the call "
                              f"did not parse: {parse_error}", "bad")
            return

        tool = body.get("tool")
        args = body.get("arguments") or {}
        self._post("say", f"proposing {tool} {_fmt_args(args)}", "accent")

        res = self._call("tool_call", tool=tool, args=args,
                         channel="pc", actor="overlay")
        res = res or {}
        if not res.get("ok"):
            # A refusal and a build gap are different facts about the world.
            if res.get("not_implemented"):
                self._post("say", f"{tool} is registered but has no executor "
                                  f"yet: {res.get('error', '')}", "muted")
            else:
                self._post("say", f"refused: {res.get('error', 'no reason given')}",
                           "bad")
            return

        done = res.get("result") or {}
        if done.get("needs_confirmation"):
            # The armed poll raises the dialog within a second; naming the id
            # here means the window already shows what it is waiting for when
            # the dialog appears over it. It is also the only place the user
            # learns a confirmation id at all from this front door - Telegram
            # reports one and the window is where it gets approved.
            # The status line is NOT set here: `_run_turn`'s `finally` clears it
            # unconditionally, so a status set inside this method would be
            # overwritten a moment later and appear to flicker.
            self._post("say", f"waiting for your approval as "
                              f"{done.get('confirmation_id')}", "muted")
            return
        if done.get("queued"):
            # T2/T3 from a channel that does not assert presence. Not an error.
            self._post("say", f"queued until you are back at the keyboard: "
                              f"{done.get('reason', '')}", "muted")
            return
        self._post("say", _render_result(tool, done), "")

    def _ask(self, prompt: str) -> None:
        if self._root is not None and not self.is_visible():
            self.show()
        self._append(f"> {prompt}", "muted")
        self.entry.focus_set()

    # --- the T3 dialog (D32) ----------------------------------------------

    def _present(self, item: dict) -> "_Confirm | None":
        """Show one confirmation. Never two at once.

        Two stacked modals is how the wrong thing gets approved, so a second
        request waits for the first to be answered - the daemon still holds it in
        `_pending` and this polls for it. The new item is remembered even when it
        cannot be shown yet, which is what makes it appear afterwards.

        Returns the dialog, or None if one is already up. Callers that need the
        dialog afterwards must hold this reference: `_settle` clears `self._dlg`
        on the way out, so reading it back a moment later yields None.
        """
        if self._dlg is not None:
            return None
        tool = item.get("tool", "?")
        preview = item.get("preview", {}) or {}
        return _Confirm(self, item, tool, preview, ttl_s=self._ttl_s)


def _fmt_args(args: dict) -> str:
    if not args:
        return "()"
    bits = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        bits.append(f"{k}={s!r}")
    return "(" + ", ".join(bits) + ")"


# Long enough for a real sysinfo payload, short enough that a stray multi-
# megabyte result cannot wedge the transcript. Truncation always says so, because
# a silently shortened answer is a wrong answer that reads as a complete one.
_RESULT_CHARS = 1200


def _render_result(tool: str, result: Any) -> str:
    """Show what a tool actually returned.

    Not `_describe_result`, and deliberately so. That one is for a *spoken*
    answer: it prefers a human summary because a dict read aloud is a log line.
    Here the user is looking at a scrollable transcript with the question still
    on screen, and the whole point of a T0 read is the data - "6.26 GiB used of
    12.0" is the answer, and `"Done: system.gpu_state."` is not. So the value is
    rendered as-is, with scalars on their own lines rather than as Python repr,
    since `{'vram_used_bytes': 939524096.0}` answers no question a person asked.
    """
    if result is None:
        return f"{tool}: done."
    if isinstance(result, str):
        return result[:_RESULT_CHARS]
    if isinstance(result, dict):
        # A human-facing summary still wins when the tool offers one, so a tool
        # that bothers to be readable stays readable.
        for key in ("summary", "message", "answer", "transcript"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value[:_RESULT_CHARS]
        lines = []
        for key, value in result.items():
            if isinstance(value, (dict, list)):
                continue  # rendered below, at full width
            if isinstance(value, bool) or value is None:
                lines.append(f"{key}: {str(value).lower()}")
            elif isinstance(value, float):
                lines.append(f"{key}: {value:,.2f}".rstrip("0").rstrip("."))
            else:
                lines.append(f"{key}: {value}")
        nested = {k: v for k, v in result.items() if isinstance(v, (dict, list))}
        if nested:
            lines.append("")
            lines.append(_pretty(nested))
        text = "\n".join(lines)
        return text[:_RESULT_CHARS] + ("\n... (truncated)" if len(text) > _RESULT_CHARS
                                       else "")
    return str(result)[:_RESULT_CHARS]


def _pretty(value: Any, indent: int = 0) -> str:
    """Nested dicts and lists, indented, so nesting is visible.

    A GPU report arrives as `{gpus: [{...}], count: 1}` and one flat line of that
    is unreadable - which is the whole reason a person asked.
    """
    pad = "  " * indent
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}:")
                out.append(_pretty(v, indent + 1))
            else:
                out.append(f"{pad}{k}: {_scalar(v)}")
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, (dict, list)):
                out.append(_pretty(entry, indent))
            else:
                out.append(f"{pad}- {_scalar(entry)}")
    else:
        out.append(f"{pad}{_scalar(value)}")
    return "\n".join(line for line in out if line)


def _scalar(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


class _Confirm:
    """A single T3 confirmation, on top of the overlay.

    Owns its own Toplevel and its own countdown. It talks to the daemon only
    through the overlay's `call`, so an approve is audited exactly like any
    other, and there is no local shortcut that skips the hash check.
    """

    def __init__(self, overlay: Overlay, item: dict, tool: str, preview: dict,
                 *, ttl_s: float) -> None:
        self.ov = overlay
        self.item = item
        self.cid = item.get("id", "")
        self.phash = item.get("payload_hash", "")
        self.answer: str | None = None
        self._done = False
        self._sender: threading.Thread | None = None

        top = tk.Toplevel(overlay._root)
        self.top = top
        top.title("Confirm")
        top.configure(bg=BG)
        top.geometry("700x520+120+120")
        top.attributes("-topmost", True)
        top.transient(overlay._root)
        top.protocol("WM_DELETE_WINDOW", self._deny)

        tk.Label(top, text="Confirm this action", fg=DANGER, bg=BG,
                 font=("Segoe UI", 12, "bold"), anchor="w").pack(
            fill="x", padx=18, pady=(14, 2))
        tk.Label(top, text=tool, fg=FG, bg=BG,
                 font=("Segoe UI", 11), anchor="w").pack(
            fill="x", padx=18)
        reason = item.get("reason", "")
        if reason:
            tk.Label(top, text=reason, fg=MUTED, bg=BG, wraplength=650,
                     justify="left", anchor="w").pack(fill="x", padx=18, pady=(2, 8))

        body = tk.Text(top, fg=FG, bg=PANEL, wrap="word", height=14,
                       relief="flat", padx=12, pady=10)
        body.pack(fill="both", expand=True, padx=18)
        body.tag_configure("muted", foreground=MUTED)
        body.tag_configure("good", foreground=OK)
        body.tag_configure("bad", foreground=DANGER)
        self.body = body

        self._describe(preview)

        self.clock = tk.Label(top, text="", fg=MUTED, bg=BG, anchor="w")
        self.clock.pack(fill="x", padx=18, pady=(6, 0))

        buttons = tk.Frame(top, bg=BG)
        buttons.pack(fill="x", padx=18, pady=(8, 14))

        self.btn_once = tk.Button(
            buttons, text="Approve once", command=self._approve_once,
            bg="#2b6cb0", fg="#ffffff", activebackground="#3182ce6",
            relief="flat", padx=10, pady=5)
        self.btn_once.pack(side="left")

        self.btn_class = tk.Button(
            buttons,
            text=f"Approve this class for {int(CLASS_GRANT_SECONDS // 60)} min",
            command=self._approve_class,
            bg="#4a5568", fg="#ffffff", activebackground="#5a6678",
            relief="flat", padx=10, pady=5)
        self.btn_class.pack(side="left", padx=8)

        self.btn_deny = tk.Button(
            buttons, text="Deny", command=self._deny,
            bg="#3a1f1f", fg="#ffb3ad", activebackground="#4a2626",
            relief="flat", padx=10, pady=5)
        self.btn_deny.pack(side="right")

        self._on_close = self._deny
        top.protocol("WM_DELETE_WINDOW", self._on_close)

        self.ov._dlg = self          # register before anything can settle us
        self._remaining = ttl_s
        self._tick()

    def _describe(self, preview: dict) -> None:
        """Render what will actually happen, not what was asked for.

        Every line here is either a count, a resolved path, or the model's own
        string labelled as such. Nothing on this screen is an inference.
        """
        w = self.body
        action = preview.get("action")
        if action:
            self._line(f"Action: {action}", "good")

        proposed = preview.get("proposed")
        resolved = preview.get("resolved")
        if proposed:
            self._line(f"The model asked for: {proposed}", "muted")
        if resolved and resolved not in (proposed, "(glob resolved per match below)"):
            self._line(f"Which resolves to: {resolved}", "bad")
        elif resolved == "(glob resolved per match below)":
            self._line("Resolved per file below - the pattern is not resolved once, "
                       "so a target could move between now and execution.", "muted")

        count = preview.get("count")
        if count is not None:
            total = preview.get("total_bytes")
            size = f", {total:,} bytes" if isinstance(total, int) else ""
            self._line(f"Targets: {count}{size}")
        note = preview.get("note")
        if note:
            self._line(note, "muted")

        targets = preview.get("targets") or []
        if targets:
            self._line("")
            for t in targets:
                if isinstance(t, dict):
                    kind = t.get("kind", "file")
                    path = t.get("path", "")
                    extra = ""
                    if kind == "dir":
                        entries = t.get("entries")
                        extra = f"  (directory, {entries:,} entries)" if isinstance(entries, int) else "  (directory)"
                    elif isinstance(t.get("size"), int):
                        extra = f"  ({t['size']:,} bytes)"
                    self._line(f"  {path}{extra}")
                else:
                    self._line(f"  {t}")
        if preview.get("missing"):
            self._line(f"{preview['missing']} target(s) do not exist.", "muted")

        args = self.item.get("args") or {}
        if args:
            self._line("")
            self._line("Arguments as recorded:")
            for k, v in args.items():
                self._line(f"  {k} = {v!r}", "muted")

        self._line("")
        self._line(f"Payload hash: {self.phash[:16]}...", "muted")
        self._line("If anything changes before you approve, the approval is "
                   "refused and you will be asked again.", "muted")

    def _line(self, text: str, tag: str = "") -> None:
        self.body.insert("end", text + "\n", tag)

    def _tick(self) -> None:
        """Count down to a DENIAL.

        Not to an approval, and not to nothing. A dialog that expires into
        approval is the worst possible default; one that expires into denial only
        costs the user a moment of their attention.
        """
        if self._done:
            return
        self._remaining -= 0.5
        if self._remaining <= 0:
            self.clock.configure(text="Expired. Treating this as a denial.", fg=MUTED)
            self._settle("deny")
            return
        self.clock.configure(
            text=f"{self._remaining:.0f}s left, then this is denied.", fg=MUTED
        )
        try:
            self.top.after(500, self._tick)
        except tk.TclError:
            pass

    # --- answers -----------------------------------------------------------

    def _approve_once(self) -> None:
        self._settle("once")

    def _approve_class(self) -> None:
        self._settle("class")

    def _deny(self) -> None:
        self._settle("deny")

    def _settle(self, scope: str) -> None:
        if self._done:
            return
        self._done = True
        self.answer = scope
        if self.cid:
            self.ov._answered.add(self.cid)
        try:
            self.top.grab_set()
            self.top.destroy()
        except tk.TclError:
            pass
        # Clear the slot only if we are still the current dialog. A second
        # confirmation may already be waiting, and clearing unconditionally would
        # let a third stack on top of it.
        if self.ov._dlg is self:
            self.ov._dlg = None
        # Kept so a caller - and the tests - can wait for the answer to have
        # actually reached the daemon. Sending it on a bare daemon thread means
        # "the dialog closed" and "the daemon was told" are separate moments, and
        # only the first is visible on screen.
        self._sender = threading.Thread(
            target=self._send, args=(scope,), daemon=True
        )
        self._sender.start()

    def _send(self, scope: str) -> None:
        """Ask the daemon, off the Tk thread.

        The approve path re-runs the gate and re-checks the hash, so this is
        where a changed payload is caught - and where an expired dialog becomes
        an audited denial rather than a silent nothing.
        """
        try:
            if scope == "deny":
                res = self.ov._call("confirm_deny", confirmation_id=self.cid,
                                    channel="pc", actor="overlay")
                self.ov._post("say", f"denied {self.item.get('tool', '?')}", "muted")
            else:
                res = self.ov._call(
                    "confirm_approve", confirmation_id=self.cid,
                    payload_hash=self.phash, scope=scope,
                    channel="pc", actor="overlay",
                )
                body = (res or {}).get("result", {}) or {}
                if body.get("approved"):
                    note = "this class for 10 minutes" if scope == "class" else "once"
                    self.ov._post("say", f"approved {self.item.get('tool','?')} ({note})", "good")
                elif body.get("requeued"):
                    self.ov._post("say", "the action changed while you were reading it; "
                                         "here it is again", "muted")
                else:
                    self.ov._post("say", f"refused: {body.get('reason','no reason given')}", "bad")
        except Exception as exc:
            self.ov._post("say", f"the daemon refused the answer: {exc}", "bad")
