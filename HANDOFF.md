# HANDOFF

**Purpose:** resume this project without re-deriving anything. Written at the end
of the session that fixed D64–D67.

**If you are resuming from a prompt like** *"inspect `G:\localaized` to determine
the current progress status, find out where it stopped and resume"* — read this
whole file, then `PLAN.md` → "Immediate next action", then `DECISIONS.md` §D67.
Do not re-read the other 4,600 lines before starting work.

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
| `DECISIONS.md` §D64–D67 | the four defects fixed in the last session, and their causes |
| `ARCHITECTURE.md` | four planes, the VRAM arbiter, authority tiers |
| `INSTALL.md` | how to install it. **§3 is mandatory and counter-intuitive** |
| `AI_Assistant_Local.md` | the original brief. **Superseded** — provenance only |

## 3. Verified state

Every number below was measured, not remembered.

```
tests        1080 passed, 3 skipped, 47 subtests   (~2m35s, no PYTHONPATH needed)
verify_install.py  7 passed, 0 failed, 0 skipped  exit 0
tools        61 registered · 51 with executors · 10 unimplemented
             (6 email.*, 4 reflect.* — the only two groups left)
decisions    D1..D67, contiguous, none missing
core deps    pystray, pillow. llama-cpp-python is in the [brain] extra ONLY,
             and tests/test_install.py asserts it never becomes a core dep (D66)
HEAD         2ea267b  D67: the window was mapped, titled, and completely dead
tree         clean
```

The 3 skips are speech tests gated on `LOCALAIZED_SLOW_TESTS=1`. Run the suite
with plain `.\.venv\Scripts\python.exe -m pytest` from the repo root —
`pythonpath = ["src"]` in `pyproject.toml` means no environment setup is needed.

## 4. What happened last session

Four phases of work. The project's test suite passed 1025 tests while three
production-blocking defects were live, which is the most important thing to carry
forward: **this codebase's tests do not cover lifecycle, threading, or the real
GUI.** See §6.

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

## 5. Do this next

**Ask the window something that requires a tool.** That is the remaining gap, and
it is a small, cheap one — but it is the one that can fail *silently*.

A `Hello` turn came back correctly, which proves the spine (see §4). It did not
exercise the grammar-constrained decision, because a greeting correctly yields no
tool call. D34 says the 4B cannot be *made* to decide to call a tool under
`tool_choice="auto"` — it narrates the call in prose and returns nothing, which
renders as a normal-looking answer. So the failure mode of this next check is a
window that says "I'll list that directory for you" and then does nothing, with
every test in the repository still green.

Ask for something concrete — *list the files in C:\Users\Public\Documents*, or
*how much VRAM am I using* — and check two things:

- the window shows a **tool proposal**, not prose describing one, and
- the tool actually **runs** and its real output comes back.

Note the policy gate is expected to refuse an unapproved path (§ D30/D33 era), so
prefer a `system.*` read, which needs no approved workspace. `system.gpu_state`
over the pipe returns live data today; it has simply never been driven *from the
window through a model turn*.

Then, in order:

1. **`email.*`** — six tools, IMAP, the last transport in scope. Three T0 reads,
   `draft` as REVERSIBLE_WRITE, `send` T3 external, `delete` T3 destructive.
   Two traps: `email.delete` is currently the destructive tool two tests use as
   their *example* of one, so building it makes that example vanish — repoint
   those deliberately. And D63 applies: an argument name is part of the rule, so
   check `folder` / `mailbox` against the path screen before shipping.
2. **`reflect.*`** — four tools, Phase 6. Build it last and on purpose. It is the
   only place the assistant reasons about improving itself, and D21 forbids it
   writing its own code or policy *permanently, at every phase*. That constraint
   belongs in the tests from the first line, not retrofitted.

Deliberately **not** done, and why:

- **The 9B deep tier** is downloaded (5.96 GB) and has never been measured;
  `phase1_measure.py` does not reference it. Kill criterion already written: if it
  misses the gate, drop the deep tier and let Plane D do heavy reasoning.
- **`daemon.py`** is 4,667 lines in one class — the largest single point of
  failure left. It absorbed three changes without complaint, which is a good sign.
  Split only along the `_OPS` seams, only opportunistically, never as a standalone
  refactor with no behaviour change to justify it.
- **Autostart** is documented (`INSTALL.md` §9) but not registered. Registering a
  scheduled task is a persistent change to the user's machine — ask first.
- **The Telegram token** is still plaintext at `state/telegram.json`.
  `LOCALAIZED_TELEGRAM_TOKEN` works if moved; rotation is @BotFather plus one edit.

## 6. How to work in this repo

This is the part that matters most for a new session.

**This suite does not cover lifecycle, threading, or the real GUI.** 1025 tests
passed while the daemon could start once, the tray was unreachable, and the window
was blank. The blind spot is consistent: *tests that assert a property of a
component in isolation cannot find a defect living in the transition between two
instances of it.* So:

- **A lifecycle bug needs a lifecycle test.** Two real things against one piece of
  shared state. `tests/test_authkey_restart.py` is the pattern.
- **Mutate your tests.** If a mutation of the code does not make the test fail,
  the test proves nothing. Twice this session a first mutation only moved a line
  and the test still passed — which would have left a worthless test behind. Do
  not trust a green test you have not tried to break.
- **Fakes must fail on behaviour, not on method coverage.** The first
  `test_overlay_lifecycle.py` used a fake root that only *recorded* `after()`
  callbacks; it would have passed against a Tk with no event loop, because the
  test supplied the pumping production does not. It would have "fixed" a blank
  window by writing a second blank window. Write the sentence *"this test fails if
  and only if ___ is broken"* before writing the fake; if the blank is not obvious,
  the fake is not specified yet.
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
hypotheses and what they cost. The numbering is load-bearing: all 67 are
contiguous. When a plan line turns out to be wrong on the record, correct the line
and say so — do not quietly overwrite it.

## 7. Quick commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q        # 1080 tests, ~2m35s
.\.venv\Scripts\python.exe verify_install.py   # exit 0 = install is real
.\.venv\Scripts\python.exe -m assistant        # start the product
.\.venv\Scripts\python.exe -m assistant --check # preflight, no display
.\.venv\Scripts\python.exe phase0_gate.py      # CUDA + Qwen3.5 gate (needs the model)
.\.venv\Scripts\python.exe probe_isa.py        # D25 standing smoke test: CPU can execute
```

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
