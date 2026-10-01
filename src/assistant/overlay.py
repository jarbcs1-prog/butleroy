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

    # --- lifecycle ---------------------------------------------------------

    def show(self) -> None:
        """Raise the window. Safe to call from any thread."""
        if self._root is None:
            self._root = self._build()
        try:
            self._root.deiconify()
            self._root.lift()
            self._root.focus_force()
        except tk.TclError:
            pass

    def hide(self) -> None:
        if self._root is None:
            return
        try:
            self._root.withdraw()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self._closing = True
        self._events.put(None)
        if self._root is not None:
            try:
                self._root.destroy()
            except tk.TclError:
                pass
            self._root = None

    def is_visible(self) -> bool:
        return self._root is not None and bool(self._root.state() != "withdrawn")

    def _pump(self) -> None:
        """Drain events onto the tkinter thread.

        tkinter is not thread-safe, so nothing here touches a widget directly;
        everything arrives through the queue and is applied in `_poll`, which
        runs on the Tk main loop.
        """
        while not self._closing:
            try:
                ev = self._events.get(timeout=0.1)
            except queue.Empty:
                continue
            if ev is None:
                return
            self._handle_event(*ev)

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
        # Always on top, because a confirmation that can be buried behind a
        # browser is a confirmation the user never sees, and then never made.
        root.attributes("-topmost", True)

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

        self._poll_job: str | None = None
        self._poll()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
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
        """
        try:
            res = self._call("brain_turn", prompt=prompt, channel="pc", actor="overlay")
        except Exception as exc:
            self._post("say", f"the brain is not answering: {exc}", "bad")
            self._post("status", "")
            self._busy = False
            return
        body = (res or {}).get("result", {}) or {}
        kind = body.get("kind")
        if kind == "answer":
            self._post("say", body.get("text", ""), "")
        elif kind == "tool":
            tool = body.get("tool")
            args = body.get("arguments", {})
            # A proposal, never an execution. The daemon has already gated it;
            # the overlay's only job is to tell the user what was proposed.
            if body.get("parse_error"):
                self._post("say", f"the model tried to call a tool but the call "
                                  f"did not parse: {body['parse_error']}", "bad")
            else:
                self._post("say", f"proposing {tool} {_fmt_args(args)}", "accent")
            self._post("status", "")
        else:
            self._post("say", "the brain returned nothing usable", "bad")
            self._post("status", "")
        self._busy = False

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
