# HANDOFF

**Purpose:** resume this project without re-deriving anything. Last rewritten at the
end of the session that built the brand kit (D72).

**If you are resuming from a prompt like** *"inspect `G:\localaized` to determine
the current progress status, find out where it stopped and resume"* — read this
whole file, then `PLAN.md` → "Immediate next action", then `DECISIONS.md` §D72.
Do not re-read the other 4,600 lines before starting work.

**The product is Butleroy** (LocalAIzed Concierge for Windows). The brand kit is
`brand/`; the mark geometry the product actually draws is `src/assistant/brand.py`,
and the tray renders from it at runtime rather than loading a file. The Python
package, wheel and named pipes are still `locala` — deliberately; see
`brand/README.md` and D72.

---

## 1. What this project is

A 24/7 local AI assistant for **one** Windows 11 machine: RTX 3060 12 GB, Ryzen 5
5600G (6c/6t), 32 GB DDR4, Python 3.11, CUDA 13.3. All reasoning and all memory
on-device. No listening ports anywhere — all inter-process transport is Windows
named pipes. Safety is deterministic code, never a prompt.

The governing constraint is the user's own, and it is an invariant rather than a
preference: **the assistant never takes resources it needs from you.** The daemon
is always resident (~300 MB); only the model cycles and is killed after 90 s
idle, which on Windows is the only reliable way to return VRAM.

**Hard boundaries you must not erode:**

- No cloud reasoning. Telegram and IMAP are transports, not reasoning. Cloud model
  providers are permitted in exactly one place — delegated workers (Plane D).
- No listening sockets, no local HTTP servers, ever.
- No coupling to any other project on `F:` or `E:`. External directories are
  reached only through an explicit, user-approved workspace manifest.
- The assistant never rewrites its own code or policy. Permanently, at every
  phase (D21). This is not a phase-6 item.

## 2. Read in this order

| File | What it is |
|---|---|
| `HANDOFF.md` | this file |
| `PLAN.md` → "Immediate next action" | what to do next, and why |
| `DECISIONS.md` §D70-D72 | `email.*`, `reflect.*`, and the brand kit - the last tool groups plus the most recent session |
| `brand/README.md` | the Butleroy brand kit: mark, palette, glyphs, voice, widget rules |
| `ARCHITECTURE.md` | four planes, the VRAM arbiter, authority tiers |
| `INSTALL.md` | how to install it. **§3 is mandatory and counter-intuitive** |
| `AI_Assistant_Local.md` | the original brief. **Superseded** — provenance only |

## 3. Verified state

Every number below was measured, not remembered.

```
tests        1167 passed, 3 skipped, 47 subtests   (~2m52s, no PYTHONPATH needed)
verify_install.py  7 passed, 0 failed, 0 skipped  exit 0
tools        61 registered · 61 with executors · 0 unimplemented
             (every registered tool can run; reflect.* closed it, D71)
decisions    D1..D71, contiguous, none missing
core deps    pystray, pillow. llama-cpp-python is in the [brain] extra ONLY,
             and tests/test_install.py asserts it never becomes a core dep (D66)
tree         clean at the last commit; see §4 for what the last session changed
```

The 3 skips are speech tests gated on `LOCALAIZED_SLOW_TESTS=1`. Run the suite
with plain `.\.venv\Scripts\python.exe -m pytest` from the repo root —
`pythonpath = ["src"]` in `pyproject.toml` means no environment setup is needed.

## 4. What happened last session

The previous session fixed D64–D67 and proved the spine with a `Hello` turn. **This
session closed the gap that handoff left open, and the gap was not where the
handoff predicted.**

- **D68 — the window proposed the tool and then dropped it.** The handoff predicted
  the tool turn would fail because of D34: the 4B narrates the call in prose instead
  of committing. The model committed — `tool:\n"system.gpu_state"` in 1.50 s, then a
  clean constrained call. `Overlay._run_turn` printed `proposing system.gpu_state ()`
  and returned, never sending the proposal back through `tool_call`. Its comment
  claimed "the daemon has already gated it"; `op_brain_turn` gates nothing and says
  so in its own docstring. **The window could never run any tool at all**, including
  a T0 read needing no approval.

  It failed silently in four ways at once: nothing on screen said so, the audit
  chain verified (the proposal genuinely was audited), `system.gpu_state` needs no
  approval so no policy edge was near it, and — the trap — *voice carries its
  proposal through while Telegram deliberately does not*, so "one works, one does
  not" pointed at the model instead of at a missing line in one method.

  Fixed with one method, `_carry_through`, doing what `_voice_turn` already did, and
  reporting all four of `tool_call`'s outcomes (ran / needs confirmation / queued /
  refused-or-unbuilt). Verified on the real model through the real arbiter, real
  grammar, real gate, real executor: `vram_used_bytes: 6,390,022,144` on screen.
  **The spine is closed for a tool turn, which `Hello` never was.**

- **Two of the ten new tests passed against broken code first**, both found by
  trying to break them rather than by reading them: one asserted a flag it never set,
  one asserted a substring the payload happened to contain. D67's file records the
  same failure a generation earlier. Seven mutations were run against `overlay.py`
  to prove the other eight tests are load-bearing.

- **D69 — the T3 confirmation dialog could not be built, and 24 tests said it was
  fine.** Driving the T3 chain next found the entire destructive path dead.
  `activebackground="#3182ce6"` is seven hex digits, so `tk.Button` raised
  `TclError: invalid color name` *while constructing the dialog*. Every T3
  confirmation in the product died on that line, and **silently** — `_poll` calls
  `_present` from an `after` callback, where Tk prints the traceback and carries on,
  so the dialog never appeared and the pending action could be neither approved nor
  refused. That is D67 defect 2's exact failure mode, reached by a different route
  in the same method.

  `test_overlay.py`'s `NoButton` ignored every colour it was handed, so all 24 tests
  on the confirmation rules passed against a dialog that cannot exist. **The fake
  agreed with the code about everything the fake did not check** — D67's lesson one
  level too high: it made the *root* real and left the widgets imaginary, and the
  part that was imaginary is the only part Tk validates.

  The same three lines carried a second defect: `_settle` destroyed the Toplevel but
  never cleared `top`/`body`/`clock` and the three buttons, so a settled dialog kept
  the Tcl interpreter alive and the process exited `-2147483645` after a clean
  shutdown — D67's exit-code bug one level down. Order matters: cleared on the Tk
  thread gives exit 0, held past shutdown gives the debug break.

- **The chain is now closed end to end.** Real model, real arbiter, real gate, real
  executor, against a real 34-byte file in a real approved workspace: propose
  `file.delete` → gate confirms `c000001` → **the armed poll raises the dialog by
  itself** → the real button approves, bound to the payload hash → the file is gone
  and nothing is left pending. D69 would have made every step after the second
  impossible.

- **D71 — `reflect.*`, and self-improvement that cannot do damage.** The last tool
  group, and the one D21 says to build last and on purpose. The interesting question
  was never how to make reflection work but how to make it *unable* to hurt anything,
  and the answer was structural rather than promised: **no `reflect.*` tool accepts a
  path**, so every file is derived from the state directory and "the assistant
  modified its own code" is an operation with no representation. The gate denies
  `src/` independently, and the overlay reaches the system prompt and nothing else —
  measured, not asserted: the probe promoted a lesson, then asked the gate to
  overwrite `daemon.py`, and watched it refuse.

  The gate now **records its own refusals**, which is what makes reflection possible
  rather than merely safe — a lesson the model derived from its own account of a
  conversation is a lesson about what it thinks happened. And the four anti-patterns
  D21 names each got a rule and each rule got a mutation: one observation is never a
  pattern, a proposal without a falsifiable `check` is refused, a candidate the daemon
  did not witness is never promoted, and one change per cycle.

  Twelve mutations, twelve caught — after the run found **two defects in my own
  gate**: a threshold of 60 on a 0–10,000 scale, so every candidate passed it, and a
  score term that no mutation could detect because the gate already refused every
  candidate it applied to.

  **Two bugs only loading the real 4B could find**: every unit test constructs a
  `Brain` with a roots reader, so the branch that never refreshed the overlay was one
  nobody took — the whole feature was inert for every construction that did not — and
  the constructor then raised `AttributeError` because it read the overlay before
  assigning it. Proven end to end: 4 real refusals → a candidate → promoted → the
  overlay present in a **resident** model's prompt with no reload, and the gate still
  refusing `src/`.

The project's test suite had passed 1025 tests while three production-blocking
defects were live, 1080 while a fourth was, and 1090 while a fifth was. That is the
thing to carry forward: **this codebase's tests do not cover lifecycle, threading,
the real GUI, call sequences between two ops, whether its widgets can be
constructed, or how its objects are constructed in practice.** See §6.

<details><summary>The previous session, for provenance</summary>

Four phases of work, ending with D64–D67.

- **D64 — the daemon could start exactly once.** `Daemon.__init__` minted a fresh
  authkey per construction, `store_authkey` opened the file `O_EXCL`, and nothing
  removed it. Every reboot or crash after that killed it permanently. Fixed by
  binding the pipe first (the OS refuses a rebind — that is the real proof of
  ownership), then adopting or minting the key; clean shutdown removes it.
  Exposing a second defect: `start_serving` wrote its audit record *before* the
  bind, so a refused second daemon corrupted the shared hash chain and stopped the
  incumbent from restarting.

- **D65 — four complete modules, composed by nothing.** `Tray` and `Overlay` were
  referenced from **zero** modules in `src/` — 36 call sites in tests only. Added
  `launcher.py` and `__main__.py`. The only thing it decides is the order: bind
  the pipe, *then* chord + presence + tray, then park.

- **D66 — an install path that structurally cannot fetch a runtime this CPU cannot
  run.** `llama-cpp-python` is deliberately **not** a plain dependency and a test
  enforces it; the prebuilt wheel is compiled with AVX-512 and dies with
  `STATUS_ILLEGAL_INSTRUCTION` on this Ryzen 5 5600G. `pyproject.toml`,
  `INSTALL.md`, `verify_install.py` (which *executes* a model, because the bad
  wheel imports cleanly). Core deps are `pystray` and `pillow` only — Plane A is
  pure stdlib, which is *why* the daemon is cheap (D1).

- **D67 — the prompt window was mapped, titled, and completely dead.** Found from
  the user's report of a blank white window. Four causes: no Tk event loop
  anywhere; the T3 confirmation poll never armed because `_poll()` ran while
  `_root` was still `None` (so **a destructive action queued from Telegram would
  silently never raise a dialog**); Tk touched from two threads; and
  `Tray.stop()` never destroying the overlay. Fixed with one named Tk thread
  (`overlay-tk`) that owns the root and runs `mainloop()`. The prompt window is no
  longer permanently always-on-top; **the T3 dialog still is**, deliberately.

- **The full spine is proven end to end.** After D67 the user typed `Hello` into
  the window and got an answer back. That single turn crosses
  `overlay._submit` → `brain_turn` → the arbiter's lease → spawning the
  `brain_server` child → real llama.cpp on the GPU → a streamed answer → the
  transcript. **The vertical slice works.** The last proof before that had been
  Phase 2, on 2026-10-01.

  **What it does not prove, and the reason is not pedantry:** a greeting
  correctly produces *no tool call*, so this turn never exercised the
  grammar-constrained decision. D34's load-bearing claim is that under
  `tool_choice="auto"` the 4B **narrates** the call in prose and returns no call —
  a silent failure that renders as a perfectly normal-looking answer. A prose turn
  and a tool turn therefore fail *differently*, and only one of them has been
  tested.

</details>

That gap is now closed — see D68 at the top of this section. The `Hello` turn's
blind spot was real, and the thing behind it was not D34.

## 5. Do this next

**The tool turn is done — it failed, and it was D68, not D34.** This section used
to open by asking for a `system.*` read through the window. That check has been run
on the real model; it found the window was proposing a tool and dropping it. It is
fixed and verified. Do not re-run it as a manual sanity check expecting a new
answer — the interesting next steps are the two build gaps below.

**Both of last session's open items are now closed.** D68 made the window carry a
proposal through the gate; D69 made the confirmation dialog buildable and released
its Tcl references correctly. The T3 chain has been driven end to end on the real
model, so there is no instruction here to re-run it — the interesting next step is
the first build gap.

One thing worth doing first anyway, because it is cheap and it is the kind of thing
this project keeps being bitten by: **read `tests/test_overlay_real_dialog.py` and
ask what else the fakes in `test_overlay.py` are not checking.** That file exists
because 24 tests on the confirmation rules passed against a dialog that could not be
constructed. The widgets the fakes replace are `Button`, `Label`, `Text`, `Frame` and
`Toplevel`, and Tk validates options on all of them. `NoButton` now checks colours;
nothing checks `relief`, `padx`, `pady`, `wraplength`, `anchor`, `justify` or
`insertbackground`, and `_Confirm` uses most of them. Expect the same class of
finding, and treat "the fakes agree with the code" as the thing to distrust.

**Every tool group is built. `email.*` closed with D70, `reflect.*` with D71, and
`reflect.*` was the last one: 61 registered, 61 with executors, nothing half-built.**
There is no build queue left. D72 built the brand kit. What remains is the GUI
work the user has asked for, plus the four things listed as "deliberately not done"
below.

So this section no longer tells you what to build. What it does instead is name the
four claims that are now *structural* and worth knowing, because they are the ones a
future edit could quietly undo:

1. **No `reflect.*` tool takes a path.** That is what makes D21's "may not modify its
   own code or policy" impossible rather than enforced. A future argument called
   `target` or `file` in that group would reopen it.
2. **The reflection overlay reaches the system prompt and nothing else.** It cannot
   widen authority — a promoted lesson cannot make the gate permit anything, and the
   probe in D71 asks the gate to overwrite `daemon.py` and watches it refuse.
3. **The gate records its own refusals.** That is the evidence the promotion gate
   runs on. If `_note_friction` stops being called, reflection does not get more
   permissive — it stops being able to promote *anything*, silently.
4. **The mark geometry lives in `src/assistant/brand.py`, not `brand/`.** This is
   because `pyproject.toml` packages `src/` only, so the tray *cannot* import from
   `brand/tools/` without breaking every installed user. `brand/tools/render_icons.py`
   is a thin CLI over the package module. If someone "tidies" the geometry back into
   `brand/`, the tray keeps working in a checkout and fails on install — and no test
   here will run against an installed copy. Read `brand/README.md` before moving it.

### The lesson from D70 and D71, which is the one to carry

**Run the real thing as early as you can, not at the end.** `email.*` has a fake
suite and a live-server suite, and the live server found four bugs the fake suite
called green — including that the client **never authenticated at all**, and that
`MailAuthError` was unreachable in production because `imaplib` raises a bare
`IMAP4.error` where a fake returns a `(status, data)` pair. Every one of them lived
*at or above the seam*, and every one of them was invisible from below it.

The pattern, now seen six times: **the fake agrees with the code about everything it
does not check.** In D68 that was colours on a button. In D70 it was everything above
a socket — the login, the protocol, the literal framing. In D71 it was the *other*
direction: every unit test constructs a `Brain` with a roots reader, so the branch
that never refreshed the overlay was one nobody took, and loading the real model was
the only thing that found it. So when a module has a boundary a fake can stand in
for — or a construction the tests all happen to take one way — ask what it is standing
in for, and then go find out whether the part it replaced ever worked.

Deliberately **not** done, and why:

- **The 9B deep tier** is downloaded (5.96 GB) and has never been measured;
  `phase1_measure.py` does not reference it. Kill criterion already written: if it
  misses the gate, drop the deep tier and let Plane D do heavy reasoning.
- **`daemon.py`** is now ~5,000 lines in one class — still the largest single point
  of failure left. It absorbed five changes without complaint, which is a good sign.
  Split only along the `_OPS` seams, only opportunistically, never as a standalone
  refactor with no behaviour change to justify it.
- **Autostart** is documented (`INSTALL.md` §9) but not registered. Registering a
  scheduled task is a persistent change to the user's machine — ask first.
- **The Telegram token** is still plaintext at `state/telegram.json`.
  `LOCALAIZED_TELEGRAM_TOKEN` works if moved; rotation is @BotFather plus one edit.

## 6. How to work in this repo

This is the part that matters most for a new session.

**This suite does not cover lifecycle, threading, the real GUI, or call sequences.**
1025 tests passed while the daemon could start once, the tray was unreachable, and
the window was blank; 1080 passed while the window could not run a tool at all. The
blind spot is consistent: *tests that assert a property of a component in isolation
cannot find a defect living in the transition between two instances of it.* So:

- **A lifecycle bug needs a lifecycle test.** Two real things against one piece of
  shared state. `tests/test_authkey_restart.py` is the pattern.
- **A call-sequence bug needs a call-sequence test.** D68 generalised the same
  sentence one level down: it lived in the gap between `brain_turn` and `tool_call`,
  and every test that covered either op was green. `tests/test_overlay_turn.py` is
  the pattern — one overlay, one daemon-shaped `call`, both ops, and the
  conversation that comes out.
- **A fake agrees with the code about everything it does not check.** D69 was this
  costing a destructive path (`NoButton` ignored colours). D70 was this costing the
  entire mail client: the fake's `connect` did its own LOGIN, so **production never
  authenticated at all** and the suite was green. When a fake stands in for a
  boundary, check whether the part it replaced ever worked. `tests/test_email_live.py`
  is the pattern — a real IMAP server on a real socket, which found four bugs no fake
  could.
- **A default that is wrong for exactly the case you construct it for.** D63's shape,
  twice more: `dict(messages or {...})` silently turns an empty mailbox into a full
  one, because an empty dict is falsy. `is None` is the only check that means what it
  looks like.
- **Mutate your tests.** If a mutation of the code does not make the test fail,
  the test proves nothing. Twice last session a first mutation only moved a line
  and the test still passed. This session **two of ten new tests passed against
  broken code**, both for the same reason: one asserted a flag it never set, one
  asserted a substring the payload happened to contain. Seven mutations were run
  against `overlay.py` to prove the other eight are load-bearing. Do not trust a
  green test you have not tried to break.
- **Assert on the payload, not on "it handled it".** The D68 window would have
  passed a test asserting the turn dealt with a tool. What caught it was asserting
  that the VRAM figure the user asked for was actually on screen. This has now
  bitten at five different heights (D36 twice, D67, D68, D72) and is the single most
  transferable lesson in this file. **All four bugs in the D72 icon generator
  produced output that looked correct.** None was found by looking at it: each was
  found by measuring the rendered pixels — where the brass is, whether a highlight
  is brighter than the surface beside it, whether the mark is centred. When the
  output is an image, the test has to look at the image.
- **A renderer and the thing it renders are two implementations. Check they
  agree.** The 50% highlight blended correctly in `mark.svg` and rendered as a
  dark hole in the PNG, because `ImageDraw` overwrites pixels and SVG `opacity`
  blends. Same source, opposite result. This is D70's "the fake did its own
  LOGIN" in a new medium: the generator agreed with the design about everything
  neither of them checked.
- **You cannot test what you cannot run.** There is no SVG rasteriser in this
  project (`cairosvg`, `svglib`, `magick` all absent, and adding one would breach
  D66). So the SVGs are only ever parsed, never rendered. One test asserting the
  lockup's optical balance was written and then **deleted** because it would have
  been asserting a number invented to match a file nobody here can look at. State
  the bound instead of inventing a check.
- **Fakes must fail on behaviour, not on method coverage.** The first
  `test_overlay_lifecycle.py` used a fake root that only *recorded* `after()`
  callbacks; it would have passed against a Tk with no event loop, because the
  test supplied the pumping production does not. It would have "fixed" a blank
  window by writing a second blank window. Write the sentence *"this test fails if
  and only if ___ is broken"* before writing the fake; if the blank is not obvious,
  the fake is not specified yet.
- **A fake agrees with the code about everything it does not check.** D69 is this
  sentence costing a destructive path: `NoButton` recorded `text` and `command` and
  ignored `bg`, `fg` and `activebackground`, so it happily agreed with a
  seven-hex-digit colour that Tk rejects outright. After D67 made the fake *root*
  real and left the *widgets* imaginary — and the imaginary part was the only part
  Tk validates. For every option a widget is handed, either check it in the fake or
  check it against a real Tk somewhere; silently ignoring it is not neutral.
- **Release Tk objects on the thread that created them, and in the right order.**
  D67 for the Overlay, D69 for the dialog. A widget object holds `.tk`, which is the
  Tcl interpreter, so a surviving reference means the interpreter is released at
  *process* shutdown on the main thread: `Tcl_AsyncDelete: async handler deleted by
  the wrong thread`, which on Windows is a debug break and an exit code of
  -2147483645 that a scheduled task reads as failure. Measured, in D69: cleared on
  the Tk thread → exit 0; held past shutdown → the break; cleared before the caller
  drops its reference → exit 0. The order is load-bearing.
- **Run the real thing.** Probes against real Tk, real pipes, real processes found
  four defects that 1,080 unit tests did not — including two bugs in my own fix.
  A fake of the key file would have passed against the broken `store_authkey`.
- **Prove constraints are active, don't set them.** D36: a constraint silently
  ignored is the worst failure mode available. `llama-cpp-python` being absent from
  `pyproject.toml` is enforced by a test, not a comment, for exactly this reason.

**Commits.** Narrowly scoped, each message explaining what was found and why, in
the project's existing voice. Stage explicit paths — `git add -A` swept build
metadata and the user's file into a D66 commit. Read `git status --short` in full
*before* committing.

**Never remove a working-tree change you did not make.** Isolate it from your
commit and leave it in the working tree. The user edits files on this machine
without telling you; `AI_Assistant_Local.md` was changed by the user mid-session
and nearly discarded as an anomaly.

**When a report is ambiguous, ask which readings it admits before probing.** "Ctrl
+Alt+Space does nothing" means *no window*, *window behind something*, or *window
present but dead*. Those need three different fixes. This cost the user two
corrections before it was diagnosed — the specific observation ("a window titled
Local assistant 0.3.0 that is empty white") was the one that cracked it.

**Decisions.** New findings go in `DECISIONS.md` as `## Dn - <title that states
the finding, not the topic>`. Rationale and evidence, including the wrong
hypotheses and what they cost. The numbering is load-bearing: all 71 are
contiguous. When a plan line turns out to be wrong on the record, correct the line
and say so — do not quietly overwrite it.

## 7. Quick commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q        # 1095 tests, ~2m34s
.\.venv\Scripts\python.exe verify_install.py   # exit 0 = install is real
.\.venv\Scripts\python.exe -m assistant        # start the product
.\.venv\Scripts\python.exe -m assistant --check # preflight, no display
.\.venv\Scripts\python.exe phase0_gate.py      # CUDA + Qwen3.5 gate (needs the model)
.\.venv\Scripts\python.exe probe_isa.py        # D25 standing smoke test: CPU can execute
```

Two probes written this session live in the temp dir rather than the repo, because
they are one-shot diagnostics rather than standing tools like the three above. Both
are worth re-creating rather than re-deriving if the question comes up again:

- a cheap one that traces which ops the window asks for and compares all three
  front doors — no model, ~1 s, and it answers "where does a turn stop";
- a real one that loads the 4B through the real `VramArbiter` and drives a full turn
  through a real `Overlay`, separating "did the model commit" from "did the window
  run it". Those two failures are indistinguishable from outside the window, and
  conflating them is what sent the previous session looking at the model.
- a T3 chain probe that drives propose → confirm → dialog → approve → run against a
  real file in a real approved workspace, and asserts the dialog appeared *without
  anything calling `_poll`*. This is the one that found D69, and it is the shape to
  copy for any loop the suite claims to cover piece by piece.

If the daemon will not start and reports `FileExistsError: authkey.bin`, another
daemon owns that state directory — that is by design (D64). Check
`python -m assistant --check`.

## 8. Machine

RTX 3060 12 GB · Ryzen 5 5600G · 32 GB DDR4 · ~500 GB NVMe · Python 3.11 ·
CUDA 13.3 · `opencode` v2.0.23 (installed at `~/.opencode/bin`, **not on PATH** —
D61, and the reason Phase 4 abandoned `opencode run` subprocesses) · Obsidian
vault at `E:\Obsidian_Vault` (426 indexable notes).

Weights in `models/`: five `.gguf` files, **12.14 GiB** total. Note that
`verify_install.py` prints the same total as `13.04 GB`, because it divides by
10⁹ while everything else in this project uses GiB (2³⁰) — same number, different
unit, which is D55's subject exactly and worth knowing before someone reads the
two as a discrepancy. The two model tiers **cannot co-reside** — 4.17 GiB + 5.96
GiB does not fit under the 7.0 GiB ceiling (D1).

Two log files are tracked on purpose: `build_phase0b.log` and
`build_phase0b_attempt1.log`. PLAN.md cites them and attempt 1 failing is the more
useful record than attempt 2 succeeding (D29). Do not let a `*.log` ignore rule
swallow them. (`build_phase0b.bat` is tracked too, and is not a log.)
