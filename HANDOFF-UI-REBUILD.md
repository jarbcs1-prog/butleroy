# Resume: the settings/tray UI rebuild

**Written 2026-10-13, end of a session that ended on a clean tree at `767b404`.**
If you are starting cold, read this file first, then `HANDOFF.md` for the
project-wide state and `DECISIONS.md` for why things are as they are (D83–D93
are this session's).

A new agent or session with no memory of the above can pick up at
**Step 0 of "Where to start"** below and continue without guessing.

---

## 1. What this session was about

The user installed the app and found the settings window was effectively a
viewer: tabs existed, contents rendered, and almost nothing could be changed.
The brief became: make the taskbar app usable, overhauling menus and settings,
**using the brand kit in `brand/`** as the design contract.

That produced four phases, all committed:

| Phase | Result |
|---|---|
| **A** — intermittent Tcl abort | **attempted, not solved.** ~9% per run, two hypotheses disproved. D83 |
| **B** — trustworthy install chain | build is a transaction; verifier sees `llama_cpp`'s Python surface. D84 |
| **C** — from-scratch install | **found a real bug two test suites were enforcing.** D87 |
| **D** — UI rebuild, started | brand spec corrected; widget kit verified; **Models made operable**. D89–D92 |

Phase A is closed-as-unresolved, not fixed. Do not re-open it without a
reproduction first (see §6).

---

## 2. Environment facts — set these before anything

```
OS            Windows 11, x64
Shell         PowerShell (pwsh)
Repo          G:\localaized   (in-place install; policy.SELF_PATHS pins this literal path)
Python        3.11.9, system install at C:\Program Files\Python311
CUDA          v13.4.2  ->  C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4
git identity  John B. <jarbcs1@gmail.com>   (set globally on 2026-10-13)
git           always pass -c safe.directory=G:/localaized
ISCC          C:\Users\jarbr\AppData\Local\Programs\Inno Setup 7\ISCC.exe
```

**`CUDA_PATH` is not in the process environment by default and everything fails
without it.** `test_brain.py`, `verify_install.py` and the app all fail to
import `llama_cpp` if it is missing. Preamble for any shell:

```powershell
$cp = [Environment]::GetEnvironmentVariable('CUDA_PATH','Machine')
$env:CUDA_PATH = $cp
$env:PATH = "$cp\bin;$cp\bin\x64;" + $env:PATH
```

Note `bin` holds **zero** CUDA runtime DLLs on this toolkit; they are in
`bin\x64`. Getting this wrong looks like a broken install.

---

## 3. Verified current state

| check | command | expected |
|---|---|---|
| suite | `.\.venv\Scripts\python.exe -m pytest -q --no-header -p no:cacheprovider` | **1423 passed, 3 skipped**, exit 0 (~3 min) |
| install verify | `.\.venv\Scripts\python.exe verify_install.py` | **8 passed, 0 failed**, exit 0 (loads a model on the GPU) |
| installer self-test | `powershell -File .\install_self_test.ps1` | **18 passed**, exit 0 |
| ISA probe | `.\.venv\Scripts\python.exe probe_isa.py` | `ggml-cpu.dll` **1.73%** EVEX, under the 10% limit |

**Never pipe pytest.** `pytest ... | Select-Object -Last N` reports the
*pipeline's* exit code and truncates output. Redirect to a file and read
`$LASTEXITCODE`.

Never read a file while appending to it. Doing so caused a 4 GB / 2.6 M-line
feedback loop earlier in this project.

---

## 4. The UI rebuild: exactly where it stopped

### Measured inventory — **derive this, do not reuse these line numbers**

They were correct at `767b404` and this session had already got them wrong once
(see §6). Re-derive before quoting:

```powershell
# section starts come from the class markers in settings.py
Select-String -Path src\assistant\settings.py -Pattern 'super\(\).__init__\("(\w+)"' |
  ForEach-Object { "  $($_.LineNumber): $($_.Matches[0].Groups[1].Value)" }
```

Then for each start, the section runs to the line before the next start.

At `767b404`:

| Section | lines | widgets | raw `tk` | daemon ops reached | verdict |
|---|---|---|---|---|---|
| models | L140-447 | Button, **Select** | Listbox | `model_config`, `model_config_set` | ✅ operable, built this session |
| voices | L448-745 | Button | Listbox, OptionMenu | `voice_config`, `voice_import`, `voice_install`, `voice_remove` | ✅ **user confirmed usable** |
| workspaces | L746-997 | Button | Listbox, OptionMenu | `workspace_add/list/remove/roots` | ✅ **user confirmed usable** |
| authority | L998-1210 | Badge, Button | Entry | `confirm_list`, `session_set_attended`, `status`, `tool_check` | ⚠️ one gap (below) |
| **advanced** | **L1211-1775** | Button | **none** | **none** | ❌ **redesign — next** |

### Done this session

- **Models is operable.** One `W.Select` per tier offering the models in
  `models/`, staged on change, written via the existing `model_config_set`.
  Commit `d733b08`. Test: `tests/test_settings_models.py`.
  It is deliberately **not** a path field — see §5.
- **Widget kit verified.** `tests/test_widgets_render.py`, 7 tests,
  mutation-gated. All nine widgets in `src/assistant/widgets.py` construct, pack
  and render to the brand spec.
- **`brand/README.md` §5 corrected.** It had claimed four sections were "built"
  with affordances that did not exist. Now separates built from specified.

### Authority — one real gap

The session **ceiling** is displayed (`headline` reads `ceiling T3` from
`status`) but cannot be changed, and **the daemon has no op to set one**.

This needs care and is deliberately last. `op_session_set_attended` is documented
as *narrowing-only*: it **refuses** `attended=True` rather than ignoring it,
because a caller able to grant itself unattended execution is itself the bug
(D-something, in `daemon.py`). A ceiling op **widens** what may run without a
human, so it needs the same scrutiny: write the constraint down before writing
the code.

---

## 5. Constraints a new session will otherwise rediscover the hard way

**`llama-cpp-python` 0.3.35 publishes NO wheel on PyPI.** One sdist, zero
wheels (verified against the PyPI JSON API). So naming it as a dependency makes
the package manager **compile** it with llama-cpp-python's defaults, not ours —
which yields an `AVX-512` `ggml-cpu.dll` this Ryzen 5 5600G cannot execute
(76.73% EVEX measured). It is named in **no dependency list at all**; `brain` is
an intentionally empty extra because `install.ps1` passes `.[brain]` and a
missing extra is a hard error. `build_phase0b.bat` is the single flag-controlled
path (calls `vcvars64` itself, passes `GGML_NATIVE=OFF`, `GGML_AVX512=OFF`,
`AVX2=ON`, `CMAKE_CUDA_ARCHITECTURES=86`). D87.

**`uv` caches the built wheel, so a build can exit 0 without compiling.** Use
`$env:BUTLEROY_FORCE_REBUILD = '1'` (sets `UV_NO_CACHE=1`) for a genuine
rebuild. A real one takes 20–26 minutes at `-j3`; a cache hit takes four
seconds. D86.

**The models config stores a filename *relative to* `models/`**, never an
absolute path, so a moved checkout keeps working. `_SAFE_NAME` rejects anything
outside `^[A-Za-z0-9][A-Za-z0-9._\- ]{0,180}\.gguf$`. **Do not build a path text
box** — the daemon would refuse it on every keystroke.

**Any `.bat`/`.cmd` written by an agent must be CRLF-normalised and byte-checked
before it is trusted.** The `write` tool emits LF-only, and `cmd` cannot locate
`goto :label` targets in an LF file — the script falls off the end with no
error. This cost several rounds of misdiagnosis. D84.

**`Panel` owns an internal `.body` frame**; children belong on `panel.body`.
Packing on the Panel itself renders but is a sibling of `body`, not a child.
`Badge.for_tier` is a **classmethod on `Badge`** — `Badge.for_tier(parent, tier)`,
not a module-level `W.for_tier`. D89.

**`Select.set_options` fires `on_change`** when the current value is not offered,
because it sets the `StringVar`, which is traced. So populating a dropdown during
a load can register as a user edit. `ModelsSection` guards with `_loading`. D92.

**The brand kit's rules that are real, not stylistic:** brass is reserved for
things *acting*, never for danger — destructive is `rust`; corners are sharp
rectangles on purpose (tkinter has no radius and a `Canvas` costs focus handling);
1 px borders via `highlightthickness`, never `bd`; a 4 px grid; nothing
interactive under 32 px. Read `brand/README.md` before designing anything.

---

## 6. The lesson from this session — please do not skip it

Three inventories were reported to the user as product defects and were **all
wrong**:

1. Regex `W\.Badge\(` could not match `W.Badge.for_tier(` → "no badges anywhere".
2. Regex `W\.(Entry|Checkbutton|…)` missed `W.Field`, `W.Select`, `W.Toggle` →
   "no input widgets anywhere".
3. Hard-coded section line ranges went stale after ~60 lines were added above
   them → "Voices and Workspaces are unwired".

In each case the code was right and the measurement was wrong, and the user had
to absorb a confident incorrect report.

**General form: an inventory derived by hand from a moving file is not an
inventory.** Derive it from structure, or re-derive it every time. When a grep
returns nothing, that is more often a wrong pattern than a missing feature.

Related, from D83–D84: a green suite is not proof. In this project the suite was
green while `install.ps1` was structurally broken on any cold uv cache (D87), and
while eight of nine widgets were used nowhere (D89). Write the failing test
**before** the product change, and gate-check it by confirming it fails for the
right reason — this is how Models was built.

---

## 7. Where to start

### Step 0 — establish state, do not assume it

```powershell
cd G:\localaized
$cp = [Environment]::GetEnvironmentVariable('CUDA_PATH','Machine')
$env:CUDA_PATH = $cp; $env:PATH = "$cp\bin;$cp\bin\x64;" + $env:PATH

git -c safe.directory=G:/localaized log --oneline -1      # expect 767b404
git -c safe.directory=G:/localaized status --short        # expect empty
.\.venv\Scripts\python.exe verify_install.py --quiet      # expect 8 passed, exit 0
```

If the tree is dirty or HEAD has moved, re-read `DECISIONS.md` D93 onward first.

### Step 1 — Advanced (the redesign)

Advanced is 565 lines, one button, and **calls no daemon op at all** — every
figure it shows is read locally from files. There is nothing to wire; it has to
be designed. The user was explicit: *"Advanced should be redesigned properly."*

Do this, in this order:

1. **Read what it currently shows** and write down what each block is for.
2. **Decide what should become reachable from it.** `brand/README.md` §5 lists
   27 daemon ops with no way in, grouped, with a note on what a window would do
   with each. Several belong in a diagnostics surface — e.g. `brain_*` load/unload
   ("free VRAM now", "see what the model was given"), `arbiter_summary`
   ("what holds VRAM and for how long"), `workspace_covers`/`workspace_resolve`
   ("is this path approved, and what may happen in it"). Confirm each op really
   exists in `daemon.py` before designing a control for it.
3. **Write the failing test first** — one that fails because the affordance is
   absent, not one written afterwards to agree with the code.
4. **Build**, then render and screenshot it. `brand/README.md` records that two
   visual defects were only findable that way (a `Meter` that never stretched,
   and `ImageGrab` being physical pixels while Tk reports logical ones).
5. **Gate-check** the new test: confirm it fails when the affordance is removed.

Screenshots: pump the Tk toplevel (`update_idletasks()` + `update()`) before
grabbing, `lift()` and set `-topmost`, and multiply the bbox by
`winfo_fpixels("1i")/96` or the crop lands on the wrong window.

### Step 2 — Authority's session ceiling

Only after Step 1. See §4. Write the "cannot widen" constraint into
`DECISIONS.md` **before** adding the op.

### Step 3 — decide whether to revisit Models

The user has seen Models and has not complained about it. It was built quickly
and its layout may be plain; if they want it revisited, `tests/test_settings_models.py`
already pins the behaviour and the change should stay inside that.

---

## 8. Do not re-open these

- **The Tcl abort (Phase A).** Measured ~9% per run; two hypotheses disproved by
  reproduction. `gc.collect()` at `settings.py:1513` is the only process-wide
  collect on a worker thread; `overlay.py` never collects. Do not add another
  `gc.collect()` anywhere without reading D83.
- **"Three clean runs" is not evidence of a fix.** At 9%, `0.91³ ≈ 75%` happens
  by chance three times in four.
- **The 56 `OpenCode <opencode@local>` commits** were left attributed as they
  were made, deliberately. Rewriting them would assert something untrue. The 32
  that were rewritten to `jarbcs1-prog` can be rolled back with
  `git reset --hard refs/original/refs/heads/master` (D85).

---

## 9. Still open beyond the UI

- **Phase C** — `dist\Butleroy-Setup-0.3.0.exe` was recompiled and run once by
  the user; the install passed. The compiled *payload's* contents are still
  unverified (7-Zip cannot read Inno 6+ archives, `innounp` not installed), so
  someone should run it and look.
- **The app has never been asked a question** on a rebuilt tree. Preflight
  (`python -m assistant --check`) passes — exit 0, audit chain verifies, no
  listening ports — but no real answer has been produced since the venv was
  rebuilt from nothing.
- **`state/telegram.json` holds a token in plaintext.**
- **The 9B deep tier** is downloaded but has never been measured.
- **A stale `locala-0.3.0` editable install** sits in `.venv` alongside
  `butleroy-0.3.0`.