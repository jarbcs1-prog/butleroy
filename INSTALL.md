# Installing

The assistant runs on **one Windows 11 machine**: RTX 3060 12 GB, Ryzen 5 5600G
(6c/6t), 32 GB DDR4, Python 3.11.9, CUDA 13.4. Everything below has been run on
that machine and the numbers are from it.

There is one step here that is genuinely unusual, and it is not optional. Read
[Why the source build is mandatory](#why-the-source-build-is-mandatory) before you
skip anything.

---

## 1. Prerequisites

| | |
|---|---|
| Python | **3.11.x**, 64-bit (`tomllib` and the tier dataclasses need it) |
| CUDA | 13.3, toolkit installed, `nvcc` on PATH |
| Build tools | Visual Studio Build Tools with the **Ninja** generator and MSVC |
| Disk | ~20 GB (13 GB of weights, ~7 GB of build) |
| `uv` | [astral.sh/uv](https://docs.astral.sh/uv/) — the venv here has no `pip` |

Confirm the build tools before starting. Two failures in this project's history
were both "the toolchain looked installed":

```powershell
& "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
where cl
where nvcc
nvcc --version
```

`cl` and `nvcc` both have to resolve. `nvcc` merely being on PATH is **not**
sufficient for the Visual Studio CMake generator — it looks for a CUDA toolset
registered *inside* the VS install, which is why CMake printed
`Found CUDAToolkit ... 13.3.73` and then died with `No CUDA toolset found`. The
build script pins Ninja for exactly this reason (D29).

## 2. The venv

```powershell
cd G:\localaized
uv venv --python 3.11 .venv
```

**The base interpreter, for the record.** This venv was created by a uv-managed
CPython and its `pyvenv.cfg` was later repointed at the system install:

```
home = C:\Program Files\Python311
version_info = 3.11.14      <- stale; see below
```

`C:\Program Files\Python311\python.exe` is **3.11.9**, and so is the venv's
interpreter (`sys.version` and `sys.base_prefix` both confirm it). The
`version_info` line is leftover metadata from the original creation and is **not**
what runs — `verify_install.py` reads the live `sys.version_info`, and nothing in
`install.ps1` or `verify_install.py` parses `pyvenv.cfg`. So the discrepancy is
cosmetic; the number to trust is `3.11.9`.

Do not "fix" it by editing `pyvenv.cfg` while the build is running. If you ever do,
`home` is the field that decides where the base interpreter is found.

## 3. Why the source build is mandatory

**The prebuilt `llama-cpp-python` wheel cannot execute on this CPU.** Do not
install it, and do not "fix" a working runtime by reinstalling.

The wheel ships a `ggml-cpu.dll` compiled with **AVX-512**. A Ryzen 5 5600G is
Zen 3 and has none. The library imports perfectly and then dies with
`STATUS_ILLEGAL_INSTRUCTION` (`0xc000001d`) on the first real instruction — on
CPU *and* CUDA, which is what makes it so confusing to diagnose. Measured:

| Build | EVEX-shaped sites / `0x62` bytes |
|---|---|
| Prebuilt wheel | **9,786 (76.7 %)** |
| Our source build | 10 (1.02 %) |
| Control DLL, known AVX-512-free | 2.74 % |

`pyproject.toml` therefore does **not** list `llama-cpp-python` as a dependency,
and a test asserts it stays that way. It is in the `brain` extra, because it must
be built, not downloaded.

## 4. Build llama-cpp-python from source

```powershell
.\build_phase0b.bat
```

About **20 minutes** at `-j3`. It sets `GGML_NATIVE=OFF` (stops `-march=native`
baking this machine's instruction set in), `GGML_AVX512=OFF` (explicit
belt-and-braces), `AVX2=ON` (Zen 3 really has it), and
`CMAKE_CUDA_ARCHITECTURES=86` — one architecture instead of the wheel's eight,
which is the single biggest lever on a 6-core build.

It must end with **`exit code 0`**. Logs land in `build_phase0b.log`;
`build_phase0b_attempt1.log` is kept deliberately, because the first attempt
failing is the more useful record.

> **Exit code 0 does not mean it compiled.** uv caches the *built wheel*, so a
> second identical run resolves and installs in about **four seconds** and exits 0
> without invoking a compiler once. Measured 2026-10-13: `Prepared 1 package in
> 11ms`, and every DLL byte-identical (sha256) afterwards. If you need a genuine
> rebuild:
>
> ```powershell
> $env:BUTLEROY_FORCE_REBUILD = '1'
> .\build_phase0b.bat
> ```
>
> That sets `UV_NO_CACHE=1` for the build. Treat anything finishing in seconds as
> a cache hit, and check the clock — a real build is ~20 minutes.
>
> One caveat worth knowing: uv's cache key comes from the source and its build
> requirements. The CMake flags above are *ours*, not llama-cpp-python's, so
> whether they participate in that key is **unverified**. If they do not, a cached
> wheel can be reused across a change of build flags — which would quietly
> reintroduce exactly the AVX-512 failure a flags change exists to prevent. This is
> the strongest practical argument for `BUTLEROY_FORCE_REBUILD=1` after changing any
> flag.

The build is a **transaction**: `build_rollback.ps1` snapshots `llama_cpp`, and any
non-zero exit restores it and re-propagates the code. A failed build costs ~20
minutes, never a working brain (D84).

## 5. Install the package

```powershell
uv pip install --python .venv\Scripts\python.exe -e .
```

Core is `pystray` and `pillow` only. **Plane A is pure stdlib** — that is what
keeps the resident daemon at a few hundred MB with no VRAM (D1) — so this is
small and fast, and it cannot pull in a GPU stack behind your back.

Extras, each optional and each independently skippable:

| Extra | Brings | Needed for |
|---|---|---|
| `brain` | `llama-cpp-python` | inference — **build step 4 still applies** |
| `voice` | `numpy`, `sounddevice`, `faster-whisper`, `piper-tts` | TTS and STT on CPU |
| `wakeword` | `openwakeword`, `onnxruntime` | the wake word (abandoned on this machine, D57) |
| `test` | `pytest` | the suite |

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[brain,voice,test]"
```

## 6. Models

Weights are **not** in the repository — 13 GB of `.gguf` has no business in git.
Put them in `models/`:

| File | Role | Size |
|---|---|---|
| `Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf` | **fast tier** — the one that must be good | 4.17 GB |
| `Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-NEO-MAX-IQ4_XS.gguf` | deep tier | 5.96 GB |
| `Qwen3.5-0.8B-Q4_0.gguf` | used by `verify_install.py` as the cheap execution probe | 0.52 GB |
| `mmproj-F16.gguf` | vision projector | 0.86 GB |

The two tiers **cannot co-reside** — 4.17 + 5.96 GB does not fit under the 7.0 GiB
ceiling (D1), and neither does either plus the rest. Download one at a time and
confirm with `nvidia-smi` that the previous one is gone before fetching the next.
`download_fast_tier.ps1` and `download_deep_tier.ps1` do this; `Phase 1` in
`PLAN.md` has the measured numbers.

Piper voices (`.onnx`) go in `models/` too.

## 7. Prove the install

```powershell
.\.venv\Scripts\python.exe verify_install.py
```

```
[ ok ] python     3.11.9, 64-bit
[ ok ] imports    assistant.* imports; no model runtime loaded
[ ok ] no_ports   no TCP listener; transport is named pipes only
[ ok ] isa        ggml-cpu.dll: 10/984 EVEX-shaped (1.02%), under the 10% limit
[ ok ] brain_surface  llama_cpp exposes Llama and LlamaGrammar, as brain.py imports them
[ ok ] daemon     preflight ok: 42 audit records verify, no listening ports
[ ok ] models     5 model file(s), 13.04 GB: Qwen3.5-0.8B-Q4_0.gguf, ...
[ ok ] inference  Qwen3.5-0.8B-Q4_0.gguf: generated 8 chars in 1.0s, 25/25 layers on GPU
```

Exit code 0 only if all eight pass. Two of them are the ones that matter:

- **`isa`** reads the built `ggml-cpu.dll` and fails if it was compiled for
  AVX-512. The threshold is 10 % of EVEX-shaped sites; the gap is enormous
  (76.7 % broken against 2.74 % clean) so the threshold is not a guess about
  someone else's build. It is a *ratio*, not a byte count — a `0x62` byte is also
  the `BOUND` opcode and appears in ordinary code (D55).
- **`inference` loads a model and generates a token.** It cannot be faked, and
  it is the only check that would have caught the wheel: the AVX-512 build
  imports cleanly and dies on first execution. It also asserts on llama.cpp's
  *own* `offloaded N/N layers to GPU` line and fails if that line is missing,
  because partial offload means silent CPU spill on six cores.

## 8. Run it

```powershell
.\.venv\Scripts\python.exe -m assistant
```

Tray icon, `Ctrl+Alt+Space`, prompt window. `--check` prints the preflight and
exits without starting anything, which is what to run when something is wrong and
you do not yet know what.

If you installed the package (`-e .`), `butleroy` is on PATH and does the same
thing.

## 9. Start it at logon

Task Scheduler, at logon, not at startup — the tray and the tkinter window need
an interactive session, and a service session has none.

```powershell
$action  = New-ScheduledTaskAction `
  -Execute "G:\localaized\.venv\Scripts\pythonw.exe" `
  -Argument "-m assistant" `
  -WorkingDirectory "G:\localaized"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "Butleroy" -Action $action `
  -Trigger $trigger -Settings $set
```

Use **`pythonw.exe`**, not `python.exe` — the console window is the "no terminal
visible" part of the Phase 2 gate, and `python.exe` opens one.

Two settings matter for a 24/7 resident daemon:

- `MultipleInstances = IgnoreNew` (the default here). Two daemons on one state
  directory is refused at startup by design (D64) — the second one loses the pipe
  race and exits rather than sharing the credential.
- `ExecutionTimeLimit = 0`. The default is 3 days, and a daemon that is killed at
  the limit is exactly the silent-death mode D64 was about.

## 10. Telegram (optional)

The bridge is transport only — no model on that path (D11) — and it is off unless
configured. Put `state/telegram.json`:

```json
{
  "telegram_bot_token": "123456:ABC...",
  "telegram_allowed_chat_ids": [123456789]
}
```

`state/` is gitignored, so the token is never committed. It is still a plaintext
credential on disk, so there is a way to keep it off disk entirely.

**Prefer the environment variable.** `BUTLEROY_TELEGRAM_TOKEN` takes precedence
over the file, so the token need never be written down. (The pre-rename spelling
`LOCALAIZED_TELEGRAM_TOKEN` still works, with a deprecation warning — nothing you
already have configured has stopped working.)

```powershell
$env:BUTLEROY_TELEGRAM_TOKEN = "123456:ABC..."
python -m assistant
```

The allowlist always comes from the file, because it is configuration rather than
a credential and belongs somewhere a human edits it. `status` reports which of the
two is in use, so you can tell at a glance whether this machine holds a token in
plaintext. A malformed environment token is **refused** rather than silently
falling back to the file — a typo there would otherwise look exactly like it
worked, and the old credential would keep being the one in use.

To have it survive a reboot, set it for your user rather than the session:

```powershell
[Environment]::SetEnvironmentVariable("BUTLEROY_TELEGRAM_TOKEN", "123456:ABC...", "User")
```

Either way:

- **the allowlist is the security boundary, not the token.** A chat id that is
  not listed is silently ignored. Never add an id you do not control.
- **Rotate by revoking the token with @BotFather** and replacing the old value.
  There is deliberately no command that writes the token: a feature that writes a
  credential is a feature that can leak one.
- Telegram may **never authorise a T3 action** (D13). It is a notification
  channel; confirmation happens at the PC.

## 11. Mail (optional)

The six `email.*` tools speak IMAP and SMTP over stdlib `imaplib`/`smtplib` — no
extra package is installed and nothing in the resident process opens a connection
until a mail tool is actually called. It is off unless configured.

Put `state/email.json`:

```json
{
  "email_host": "imap.example.com",
  "email_username": "you@example.com",
  "email_password": "your-app-specific-password"
}
```

| key | required | meaning |
|---|---|---|
| `email_host` | yes | IMAP server |
| `email_username` | yes | the account |
| `email_password` | yes, unless the variable below is set | credential |
| `email_port` | no (993) | IMAP port |
| `email_ssl` | no (`true`) | implicit TLS. `false` means STARTTLS, **not** plaintext — see below |
| `email_mailbox` | no (`INBOX`) | the default folder |

**Use an app-specific password.** Every mainstream provider asks for one and will
refuse the account password over IMAP. It is also revocable on its own, which is
the property worth having in a credential that a 24/7 process holds.

**Prefer the environment variable,** as with Telegram:

```powershell
$env:BUTLEROY_EMAIL_PASSWORD = "your-app-specific-password"
python -m assistant
```

`BUTLEROY_EMAIL_PASSWORD` takes precedence, so the credential need never be
written down. (`LOCALAIZED_EMAIL_PASSWORD` still works, with a deprecation
warning.) The host, username and default folder always come from the file —
those are configuration rather than secrets. `mail_status` reports which of the two
is in use. A password set in the environment to the **empty string** falls back to
the file, because a cleared variable is a recoverable misconfiguration and
refusing it would break a machine that was working an hour ago.

Worth knowing before you use it:

- **The password is never a tool argument.** No `email.*` tool takes one, because
  an argument reaches the append-only audit chain. It is read from configuration
  and held in memory. `mail_status` and every `email.*` result omit it.
- **`email.send` is T3 and external** — a real message leaves your machine and
  cannot be recalled. Its confirmation shows the recipient and the whole body.
- **`email.delete` is T3 destructive**, and it files the message in
  `state/quarantine/mail-*` *before* expunging it, so it can be recovered from
  this machine. It is still gone from the server: another mail client will no
  longer see it.
- **`email_ssl: false` is refused if STARTTLS fails.** The password is never sent
  in the clear, even on a server that offers no way to avoid it.
- **Rotate by revoking the app password at your provider.** As with Telegram there
  is deliberately no command that writes a credential.

## 12. Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

1412 tests, ~3 minutes. No `PYTHONPATH` needed — `pythonpath = ["src"]` is in
`pyproject.toml`. Three speech tests are skipped unless `BUTLEROY_SLOW_TESTS=1`.

A full run can instead abort at ~67% with a Tcl `0x80000003` from
`settings.py:1513`. That one is **unresolved** — measured at ~9% of runs, with
two hypotheses disproved (D83). It is not fixed, so do not read three clean runs
as a fix: at 9%, `0.91³ ≈ 75%` happens by chance alone.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `STATUS_ILLEGAL_INSTRUCTION` / `0xc000001d` | prebuilt AVX-512 wheel | rebuild from source, step 4. `verify_install.py` `isa` names it |
| `No CUDA toolset found` | CMake picked the VS generator | use Ninja; `build_phase0b.bat` pins it (D29) |
| `CMAKE_BUILD_TYPE=` empty, slow build | VS generator is multi-config | use Ninja **and** pin `-DCMAKE_BUILD_TYPE=Release` |
| `import llama_cpp` fails outright | CUDA runtime not on PATH | add `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4\bin\x64` — **not** `bin`, which on this toolkit holds zero runtime DLLs (`cudart64_13.dll` and 25 others live in `bin\x64`) |
| `FileExistsError: authkey.bin` | a daemon is already running | expected; it refuses to share a state directory (D64) |
| chord does nothing, tray is fine | `Ctrl+Alt+Space` is an IME toggle | the tray says so in red; open the prompt from the icon |
| audit chain does not verify | state was tampered with, or two daemons raced | **read `state\audit.jsonl` before doing anything else** |
| `No module named assistant` | not installed and no `pythonpath` | run from the repo root, or `uv pip install -e .` |
| voice input silent, `-9999 Blocking API` | PortAudio host-API limit | not another program holding the mic (D57) |
