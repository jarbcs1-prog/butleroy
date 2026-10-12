# Completion plan

**Written 2026-10-13, after D82.** Four phases, then a definition of done. Every
number below was measured on this machine on that date; where a figure is not
stable, it says so.

---

## 0. What "completion" means here

The product is **feature-complete by its own plan** — `PLAN.md` phases 0–6 closed,
61 tools registered with 61 executors, nothing half-built. **There is no feature
work left**, and any proposal that adds some should be refused on those grounds.

What remains is closing defects and earning a defensible *"this works"* claim.
The honest position today:

| | State |
|---|---|
| Known test failures | **zero** |
| Suite trustworthiness | **poor** — aborts intermittently at ~67% |
| Installer artifact | **compiled, never executed** |
| Product exercised since venv rebuild | **no** |
| venv provenance | **patched** — DLLs from the original build, `.py` restored from uv's cache |

So: zero known failures is *not* the same as done, and this plan is about the
difference.

**The governing constraint is unchanged and is the reason several items below
cannot be shortcut:** the assistant never takes resources from you, safety is
deterministic code rather than a prompt, no listening ports ever, and the
assistant never rewrites its own code or policy (D21, permanently).

---

## Phase A — Make the suite trustworthy

**The intermittent Tcl abort is the blocker.** A suite that aborts roughly one run
in two undermines every other number in this repository, and it is the single item
between the project and "all green."

### What is measured

Four reproductions in this project's history, all `0x80000003` (STATUS_BREAKPOINT)
from `src/assistant/settings.py:1513` inside `_release_widgets`, during
`Garbage-collecting`, on the Tk thread. Recorded as `Garbage-collecting` in the
fault report, with `_tk_main` immediately above it.

Subset runs, **each exit 0**, none reproducing:

| Subset | Result |
|---|---|
| `test_settings_real.py` alone | 40 passed |
| the four real-Tk files together | 86 passed |
| all 35 files preceding `test_settings_real.py`, plus it | 1041 passed |
| the two real-Tk files alone, three consecutive runs | 70 passed each |
| all Tk-touching files (`overlay`, `overlay_lifecycle`, `overlay_real_dialog`, `settings_real`, `widgets_real`) | 110 passed |

`test_settings_real.py` sits at **file-index 35 of 49** in collection order, and no
subset *containing* it reproduces. The trigger is therefore **after** it in run
order, not in it.

### The hypothesis, stated so it can be falsified

`settings.py:1513` calls `gc.collect()` in `finally`, on the Tk thread, to break
reference cycles that hold the Tcl interpreter — D80's fix, and the correct place
for it.

**`gc.collect()` collects all cyclic garbage process-wide, not only the settings
widgets it intends to release.** So it can reach a cycle belonging to a *different*
thread, and deallocating a Tcl object from a thread that does not own its
interpreter is precisely what produces `STATUS_BREAKPOINT` during collection.

**The `gc.collect()` that was added to fix a wrong-thread release may itself be
causing one.** That is a hypothesis, not a finding.

### Steps

1. **Instrument, do not guess.** Log, inside `_release_widgets`, what `gc.collect()`
   actually collected and which thread owned each unreachable Tcl object. If the
   foreign-cycle hypothesis holds, this shows it directly.
2. **If it holds:** replace the blanket collect with targeted disposal — destroy
   each section's widgets in order, drop the section's own references, and collect
   only after the interpreter's own cycles are gone. Keep a final `gc.collect()`
   solely as a last-resort sweep, and make it conditional on this window being the
   only live Tk interpreter.
3. **If it does not hold:** say so in `DECISIONS.md`, keep narrowing, and do not
   claim a fix. A wrong fix here is worse than the open item, because the next
   session will trust it.

**Gate: three consecutive clean full-suite runs, exit 0.** Not one — the whole
point is that a single clean run is not evidence of anything (see Phase D).

---

### STATUS: ATTEMPTED 2026-10-13, NOT SOLVED (D83)

Steps 1–3 were carried out under `systematic-debugging` and the fault was **not
root-caused**. The durable outcome is the measurement and the disproofs:

- **Rate is ~9 %, not 20 %** — 1 crash in 11 full-suite runs. The first 1-in-5 was
  taken under concurrent load and a GC-timing fault shifts with load.
- **The gate above is invalid at that rate.** `0.91³ ≈ 75 %`: three green runs
  happens by chance three times in four. A valid gate would need ~30 runs.
- **Two hypotheses, both disproved by reproduction** — the foreign-interpreter
  collect (8 real `Overlay` cycles + 3 `Settings` cycles, exit 0 with and without
  the collect), and the main-thread root from `test_widgets_real` (which runs at
  99–100 %, long after every settings window).
- **One lead survives, unproven:** a run observed at 67 % inside
  `test_prompt_served.py`, before `test_settings_real.py` starts, is consistent with
  a Settings Tk thread finalising long after its own test returned.
- **One finding stands regardless of this fault:** `gc.collect()` is the only
  process-wide collect on a worker thread in `src/`, `overlay.py` never collects,
  and it is the exact line every recorded abort fires on.

**This item is closed as *attempted and unresolved*, not as fixed.** Reopening it
requires a reproduction first: the fault did not appear in 10 of 11 runs, so any
hypothesis formed without one is a guess. Full detail in DECISIONS.md D83.

---

## Phase B — Make the install chain trustworthy

Three items, all small, all derived from damage caused on 2026-10-13 and recorded
in D82.

### B1 — `build_phase0b.bat` must not destroy a working runtime

It currently uninstalls `llama-cpp-python` **before** it knows the build will
succeed. When it then fails — for any reason, including a file lock from an
unrelated process — the machine is left with a half-uninstalled brain.

Observed on 2026-10-13: six `.py` files gone, every DLL intact, `import llama_cpp`
still succeeding, and `verify_install.py` still passing all seven checks.

**Fix:** build into a project-local staging directory, and swap into the venv only
on exit 0. A failed build then leaves the previous runtime untouched — which is
the entire point of having one.

**Gate:** run the build script with an induced failure (hold a lock on one DLL).
The venv must still import and still pass `verify_install.py` afterwards.

---

### STATUS: DONE 2026-10-13 (D84)

Implemented as a **snapshot/restore transaction** rather than a staging swap —
`uv pip install --reinstall-package` removes the installed package before it
builds the replacement, so the honest unit of protection is "put the old one back",
not "build somewhere else".

`build_rollback.ps1 -Mode build` snapshots, builds, restores on any non-zero exit,
cleans up, and propagates the code. `build_phase0b.bat` now makes exactly one call.

**The transaction had to leave the batch file entirely.** Measured, not assumed:
`cmd` stopped executing the instant `uv` returned, so *every* statement after the
build was skipped — no restructuring inside a `.bat` could have worked. Three
findings, all in D84.

Gate passed on the shipped files: induced failure → snapshot 51 files → restored
51 files → exit 9 propagated → all six damaged `.py` files back → `verify_install.py`
**8 passed, exit 0**.

**Standing rule now in force: any `.bat`/`.cmd` an agent writes must be normalised
to CRLF and the byte counts checked.** `write` emits LF-only, and `cmd` cannot find
`goto` labels in an LF file — the script falls off the end with no error.

### B2 — `verify_install.py` must assert the module surface

`check_imports` proves Plane A imports with no model runtime loaded. It does **not**
prove that `llama_cpp`'s Python layer is intact, because the interpreter
implementation lives in `.py` files while the verifier's `inference` check only
drives the native DLLs.

That gap is why a half-uninstalled `llama_cpp` passed all seven checks.

**Fix:** in `check_imports` (or a new `check_brain_surface`), import
`Llama`, `LlamaGrammar` and `LlamaToken` and fail with a message naming what is
missing.

**Gate:** delete `llama_cpp/__init__.py` in a scratch copy; the verifier must fail
and name that file.

---

### STATUS: DONE 2026-10-13 (D84)

Added as `check_brain_surface`, asserting the names `brain.py` actually imports.

**This check caught the author.** The first draft demanded `LlamaToken` and
`LlamaContext`, which 0.3.35 does not export at top level — so it failed a healthy
install. Guessing an API surface is the same error as the one it was written to
detect. The names now come from a grep of `src/`, not from memory: `Llama` and
`LlamaGrammar`.

Gate passed: 8/8 healthy; moving `__init__.py` aside makes it fail **by name**;
restores clean. The gate is "it catches the real damage", not "it returns green".

### B3 — A clean source rebuild, end to end

The venv is currently **patched**: native DLLs from the original source build, six
`.py` files restored from uv's archive cache after the failed uninstall (restored
only after confirming `llama_cpp.py` matched byte-for-byte between cache and
install). It works. Its provenance is a footnote, and this is the wrong basis for a
production claim.

**Do it because it closes three things at once:**

- establishes a fully-known-good baseline with no repair in its history;
- runs the D82 `CMAKE_CUDA_COMPILER` fix (`CUDA_PATH` → detected `nvcc`) for the
  **first time** — it has only ever been confirmed to *resolve*, never to complete;
- validates the "full" install path end to end, which has never been executed.

**Cost:** ~20 minutes at `-j3`, plus the prerequisites already present.

**Gate:** `build_phase0b.bat` exits 0, `probe_isa.py` reports the source build's
~1% EVEX ratio (not the wheel's 76.7%), and `verify_install.py` exits 0.

---

## Phase C — Validate the delivery

Three claims made during D82 that are **not yet verified**. Each is currently an
assertion rather than a result.

### C1 — `Setup.exe` has never been run

`butleroy.iss` compiles cleanly to a 3.24 MB `dist\Butleroy-Setup-0.3.0.exe`, and
`install.ps1` was run directly and exited 0. **The compiled artifact itself has
never been executed**, so the GUI wiring — the four switches, the component →
switch mapping, the file list — is unproven.

**Fix:** run the compiled installer. Minimal type first (fast, no download), then
confirm `-WithAutostart` registers the task and that uninstall leaves the machine
as it found it.

**Gate:** `Setup.exe` completes, `verify_install.py` exits 0 afterwards, uninstall
is clean.

### C2 — Nobody has used the product since the venv was rebuilt

`verify_install.py` passes and `--check` returns a valid preflight, but **no
question has been asked and answered through the prompt window on GPU since the
environment was rebuilt**. The D82 changes touched the install path, not the
product, so this is a low-risk claim — but it is unverified.

**Fix:** start the product, ask a question in the prompt window, confirm a streamed
answer. Then ask something requiring a tool (`system.gpu_state`) and confirm the
figure on screen. That is the D68 spine, and it is cheap to re-prove.

**Gate:** one prose answer and one tool-driven answer, both on the real model
through the real arbiter, grammar, gate and executor.

### C3 — The crash rate is unquantified

The abort is recorded as "intermittent", which is a description, not a number.

**Fix:** run the full suite five times on a quiet machine; record the pass/crash
split. If Phase A did not fix it, a measured rate is what makes the open item
actionable rather than anecdotal.

**Gate:** a written rate, in `DECISIONS.md`, even if the rate is "4 of 5".

---

## Phase D — Close the record

Documents that state something untrue are worse than documents that are silent,
because they are believed.

### D1 — `INSTALL.md` is stale in ways D81 did not fix

| Location | Says | Reality |
|---|---|---|
| §1 prerequisites | CUDA **13.3** | **13.4** is installed |
| §Troubleshooting | add `…\CUDA\v13.3\bin` to PATH | that directory does not exist; `llama_cpp` resolves `bin\x64` (D81) |
| §12 tests | **1047 tests**, ~2.5 min | **1412 collected** |
| §7 sample output | `python 3.11.14` | **3.11.9** |

**Fix:** correct all four. `README.md`, `HANDOFF.md` and `PLAN.md` were reconciled
in D81; `INSTALL.md` was not, and is the file a new user actually follows.

### D2 — Record the venv's base interpreter

The venv's base interpreter is now the system `C:\Program Files\Python311`
(3.11.9), not the uv-managed 3.11.14 it was built against — the original base
lived in a `PC Principal` profile that no longer exists. It works, and the ABI
matches (both cp311), but it is **non-standard and undocumented**, and it means
`uv pip install` into this venv behaves differently than the original setup would
have. Record it in `INSTALL.md` §2.

### D3 — Measure counts from the run they came from

`DECISIONS.md` §6 already says this. It has been violated three times in this
project's own history — D72 (`1217 passed`, from the start of a session), D81's
first draft (`1410 passed, 0 failed`, written before the run finished), and once
during D82 when a run's log was deleted before its summary was read.

**Fix:** every count in `HANDOFF.md` §3, `README.md` and `INSTALL.md` carries the
date and the run it came from. Where a figure is not stable — the suite-wide
count, because of Phase A — say so in the same line rather than in a footnote
nobody reads.

### D4 — `git config` identity

Commits in this session needed `GIT_AUTHOR_*` environment variables each time,
because the global identity was not configured for this account. One command:

```powershell
git config --global user.name "John B."
git config --global user.email "jarbcs1@gmail.com"
```

---

## Decisions required

These are judgement calls, not work. Each has a recommendation; none should be
decided by an agent.

| # | Item | Options | Recommendation |
|---|---|---|---|
| 1 | **9B deep tier** — 5.96 GB on disk, never measured, kill criterion already written in `PLAN.md` | measure it · drop it | **Ask first, then measure if wanted.** Dropping is a *planned* outcome, not failure: Plane D does the heavy reasoning and the delegation architecture does not depend on the deep tier existing. If it is not wanted, deleting 5.96 GB is a real win. |
| 2 | **Telegram token** — plaintext at `state/telegram.json` | rotate via @BotFather + move to `BUTLEROY_TELEGRAM_TOKEN` · keep as is | **Rotate.** The env var already takes precedence; the file only needs the allowlist. |
| 3 | **Autostart** — documented in `INSTALL.md` §9, never registered | register the at-logon task · leave documented-only | **Register it via the installer's opt-in checkbox.** It is a persistent machine change, which is exactly why it asks rather than doing. |
| 4 | **`locala` editable-install cruft** — both `butleroy-0.3.0` and `locala-0.3.0` present in `.venv` from the D73 rename | remove `locala` · leave both | **Remove it.** Harmless today, confusing the moment anyone reinstalls. It is inside `.venv`, so it is not a code change. |
| 5 | **`daemon.py`** — ~5,000 lines, one class, the largest remaining single point of failure | split it · leave it | **Leave it**, per `HANDOFF.md` §5: split only along the existing `_OPS` seams, only opportunistically, never as a standalone refactor with no behaviour change to justify it. Completion work is the wrong place for it. |
| 6 | **Install root** — the installer refuses anything but `G:\localaized`, because `policy.SELF_PATHS` names that literal path | keep the restriction · make `SELF_PATHS` dynamic | **Keep it.** Making it dynamic is a change to a security-critical constant and would touch ~20 tests. Do it only if the project is genuinely going somewhere else, and then as its own decision with its own evidence. |

---

## Definition of done

The project is production-ready when **all** of the following hold, measured not
assumed:

1. `Setup.exe` runs clean on this machine from a fresh state, and uninstalls clean.
2. The assistant answers a real prose question **and** a real tool question through
   the prompt window, on GPU, through the real arbiter, grammar, gate and executor.
3. **Three consecutive full-suite runs exit 0** — or Phase A's outcome is recorded
   with a measured rate and an accepted risk.
4. `verify_install.py` exits 0 with all seven checks passing.
5. No stale figure remains in `HANDOFF.md`, `README.md`, `PLAN.md` or `INSTALL.md`.
6. `DECISIONS.md` is contiguous D1..Dn, and every open item above is either closed
   or written down as an accepted risk with its reason.

**Anything short of that is not production-ready, and the gap should be named
rather than rounded up.** Reporting a partial result accurately is worth more than
a green summary, and this project has already established that twice today.

---

## What is deliberately *not* on this list

- **New features.** Phases 0–6 are closed.
- **A rewritten `daemon.py`.** See decision 5.
- **A CPU-only or multi-tier fallback.** The tiering scheme is measured, not assumed.
- **Cloud reasoning or any listening socket.** Permanently out of scope.
- **The assistant modifying its own code or policy.** Permanently out of scope, at
  every phase (D21). Not a backlog item; a property.

---

## If only one thing gets done

**Phase A.** An intermittent suite abort means no number in this repository can be
trusted, including the ones written in this file. Fixing the tests' trustworthiness
is worth more than any single feature or cleanup, because everything else is
measured by them.