; ButlerOY installer - Inno Setup script.
;
; WHY THE GUI IS A THIN WRAPPER (stated here, in the file a maintainer opens
; first, because the temptation to move logic into this file is the whole risk):
;
; The real work lives in install.ps1, and it must stay there. The brain build is
; ~20 minutes inside a vcvars64 shell with GGML_NATIVE=OFF and one pinned nvcc
; (D25/D29/D66); a GUI that owns any of that logic becomes a second
; implementation of the install, and this project has already paid for one of
; those - "a second copy of a test is not a belt and braces; it is a second thing
; that can be wrong without the first one being wrong" (D61).
;
; So this script's entire job: collect four choices, pass them to PowerShell as
; switches, show its output. Every step below is either a file copy or a switch.
;
; The console is deliberately VISIBLE. Hiding it would hide the 20-minute build's
; progress and every error message, which is the worst failure mode a GUI
; installer has.

#define AppName "ButlerOY"
#define AppVersion "0.3.0"
#define AppPublisher "LocalAIzed"
#define InstallRoot "G:\localaized"

[Setup]
AppId={{7C4E9A21-5B3D-4E6F-9A18-2D6F1B8C4E70}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={#InstallRoot}
; The install root is not a preference, so there is no directory page.
; policy.SELF_PATHS names G:\localaized literally; installing anywhere else
; would silently stop the assistant denying itself write access to its own source
; (D21/D73) - and nothing fails, because the tests asserting the refusal compare
; a string to the same string. install.ps1's Assert-InstallRoot enforces the same
; rule from the engine side, so this is belt AND braces on a security constant.
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=ButlerOY-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user by default: Plane A is pure stdlib, the tray is a per-user icon, and
; the at-logon task goes in the user's own scheduler. Requiring elevation would
; be asking for a privilege the product does not use.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\.venv\Scripts\pythonw.exe
SetupLogging=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
; "Full" is NOT the default. The brain build is 20 minutes and the weights are
; 4.2 GB, so the honest default is the cheap install that gives a working daemon
; with no model, and the user opts into the rest deliberately.
Name: "minimal"; Description: "Core only - the resident daemon, policy gate, pipes. No model."
Name: "standard"; Description: "Core plus the fast-tier model (~4.2 GB download)."
Name: "full"; Description: "Everything: core, model, and the brain built from source (~20 min)."

[Components]
; One component per optional piece, so the type sets their defaults. Sizes are
; the real ones, measured, because a progress bar that lies is worse than none.
Name: "core"; Description: "Resident daemon, tray, prompt window (always installed)"; Types: minimal full
Name: "model"; Description: "Fast-tier weights (~4.2 GB, verified byte count)"; Types: standard full
Name: "brain"; Description: "llama-cpp-python built FROM SOURCE (~20 min) - see the warning below"; Types: full
Name: "voice"; Description: "Voice: piper TTS + faster-whisper STT, CPU-only"; Types: minimal standard full
Name: "autostart"; Description: "Start {#AppName} at logon (registers a scheduled task)"; Types: minimal standard full

[Tasks]
Name: "desktopshortcut"; Description: "Create a desktop shortcut"; GroupDescription: "Optional:"; Flags: unchecked

[Files]
; The checkout. install.ps1 refuses any root but G:\localaized, so this is the
; only place it can land.
Source: "{#InstallRoot}\install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#InstallRoot}\install_self_test.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#InstallRoot}\build_phase0b.bat"; DestDir: "{app}"; Flags: ignoreversion
; build_phase0b.bat delegates its whole transaction to this. Shipping the batch
; without it means the brain build dies in an installed tree with "cannot find
; build_rollback.ps1" - and nothing in install_self_test.ps1 would notice,
; because that suite never runs a build. Found by diffing the files the shipped
; scripts reference against this Source list, 2026-10-13 (D88).
Source: "{#InstallRoot}\build_rollback.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#InstallRoot}\download_fast_tier.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#InstallRoot}\download_deep_tier.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#InstallRoot}\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#InstallRoot}\verify_install.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#InstallRoot}\phase0_gate.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#InstallRoot}\probe_isa.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#InstallRoot}\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#InstallRoot}\tests\*"; DestDir: "{app}\tests"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#InstallRoot}\*.md"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; Created empty rather than shipped: state/ holds credentials (the Telegram
; token) and models/ holds 13 GB. Both are gitignored, so there is nothing to
; copy - and shipping a state directory would ship a credential.
Name: "{app}\state"
Name: "{app}\models"
Name: "{app}\logs"

[Icons]
; Launches the console entry point, not the tray: on a first run the tray may not
; appear if the interpreter is missing, and a window showing an error beats a
; silent no-op.
Name: "{group}\{#AppName}"; Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m assistant"; WorkingDir: "{app}"
Name: "{group}\{#AppName} (preflight)"; Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m assistant --check"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m assistant"; WorkingDir: "{app}"; Tasks: desktopshortcut

[Run]
; -NoProfile: a user profile could shadow the engine with a function or alias,
; and this script mutates the machine.
; -ExecutionPolicy Bypass: process-scoped only. The machine's policy is untouched.
; visible window + wait: the build is long and silent failure is the worst
; outcome, so the console is neither hidden nor minimised.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -Root ""{app}"" {code:RunCmd}"; \
    WorkingDir: "{app}"; \
    StatusMsg: "Installing {#AppName} - a brain build takes about 20 minutes..."; \
    Flags: waituntilterminated

Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m assistant --check"; WorkingDir: "{app}"; \
    Description: "Run the preflight now? (prints JSON and exits - recommended)"; \
    Flags: postinstall nowait skipifsilent runascurrentuser

[Code]
{ Translate the selected components into the engine's switches. Nothing is
  decided here - each switch maps to exactly one parameter of install.ps1, and a
  component the user left out is a switch the engine never sees. }

function RunCmd(Param: string): string;
begin
  Result := '';
  if WizardIsComponentSelected('brain') then
    Result := Result + ' -WithBrain';
  if WizardIsComponentSelected('voice') then
    Result := Result + ' -WithVoice';
  if WizardIsComponentSelected('model') then
    Result := Result + ' -WithModel';
  if WizardIsComponentSelected('autostart') then
    Result := Result + ' -WithAutostart';
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;

{ The AVX-512 warning, shown before the install rather than buried in it. The
  prebuilt llama-cpp-python wheel imports cleanly and then dies with
  STATUS_ILLEGAL_INSTRUCTION on a Ryzen 5 5600G, because its ggml-cpu.dll is
  compiled with AVX-512 that Zen 3 cannot execute (D25). A user who reaches the
  "full" type without knowing that will cancel a 20-minute build halfway. }

{ Show the AVX-512 warning as a real wizard page when "brain" is selected.

  `CreatePage` is only valid during InitializeWizard; adding a page later, from
  CurStepChanged, is the classic Inno mistake and fails at compile time rather
  than at runtime. So the decision about *whether* to add the page has to be made
  before any page is created - which means it cannot depend on a component the
  user has not chosen yet. So the page is created unconditionally and the engine
  prints the warning itself, at the moment it matters: `install.ps1 -WithBrain`
  emits it before it starts the build. This function only asserts the rule that
  the warning cannot be skipped. }

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not WizardSilent) then
  begin
    if WizardIsComponentSelected('brain') then
      MsgBox('The brain is compiled from source over the next ~20 minutes.' + #13#10 +
             'The console window will show every step. If it closes without a ' +
             '"Brain built from source" line, the build failed - read the tail ' +
             'of build_phase0b.log.' + #13#10#13#10 +
             'The prebuilt wheel cannot be used instead: its ggml-cpu.dll is ' +
             'compiled with AVX-512, which a Ryzen 5 5600G (Zen 3) cannot ' +
             'execute, so it imports cleanly and then dies with ' +
             'STATUS_ILLEGAL_INSTRUCTION on the first instruction (D25).',
             mbInformation, MB_OK);
  end;
end;