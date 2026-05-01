; Inno Setup 6 script for Local AI Stack
; Build with: ISCC.exe installer\LocalAIStack.iss
; Or: .\LocalAIStack.ps1 -BuildInstaller
;
; Ship model: ONE installer EXE (LocalAIStackInstaller-<ver>.exe). After
; install, the only EXE on disk is the runtime LocalAIStack.exe — the
; installer is not redistributed alongside the app. To reconfigure or
; repair, the user re-runs the installer (Apps & Features → Modify, or
; double-clicks the same EXE again); the installer detects the existing
; install via {#AppId} and edits it in place.
;
; Layout after install:
;   %PROGRAMFILES%\LocalAIStack\   — code + binaries (read-only, per-machine)
;   %LOCALAPPDATA%\LocalAIStack\   — .env, database, logs, models (per-user, preserved on uninstall)

#define AppName      "Local AI Stack"
#define AppVersion   "1.0.0"
#define AppPublisher "Local AI Stack"
#define AppURL       "https://github.com/kitisathreat/local-ai-stack"
#define AppId        "{E7B3A1D2-4F8C-4E9A-B012-3C7D5E6F1A2B}"
#define ExeName      "LocalAIStack.exe"
; AppUserModelID — opaque, version-stable taskbar identity. Stamped on
; every shortcut and set at runtime by the launcher and the Qt GUI so
; (a) the pinned shortcut, (b) the launcher process, and (c) the Qt
; window all share one taskbar slot. Don't bump on minor upgrades or
; users lose their pinned shortcut.
#define AppAUMID     "LocalAIStack.App"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\LocalAIStack
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename=LocalAIStackInstaller-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Registry flag so the launcher knows it is running in installed mode
ChangesEnvironment=yes
; Repair-in-place: when the same AppId is detected, reuse the prior
; install dir and shut the running app down before overwriting files.
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
CloseApplications=force
RestartApplications=no
; Same-version upgrades: prevent two side-by-side installs by routing
; everything through the standard upgrade flow keyed off {#AppId}.
DisableDirPage=auto
DisableProgramGroupPage=auto

[Registry]
Root: HKLM; Subkey: "SOFTWARE\LocalAIStack"; ValueType: string; ValueName: "InstallDir"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "SOFTWARE\LocalAIStack"; ValueType: string; ValueName: "Installed"; ValueData: "1"

; ── Proper-app registration (Windows "Apps" recognition + taskbar pin) ──
; HKCR\Applications\LocalAIStack.exe gives the EXE a friendly name and
; binds the AUMID. Without this, "Pin to taskbar" / "Open with" treat
; the EXE as anonymous and the taskbar icon doesn't group with the
; pinned shortcut.
Root: HKLM; Subkey: "SOFTWARE\Classes\Applications\{#ExeName}"; \
    ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "SOFTWARE\Classes\Applications\{#ExeName}"; \
    ValueType: string; ValueName: "AppUserModelID"; ValueData: "{#AppAUMID}"
Root: HKLM; Subkey: "SOFTWARE\Classes\Applications\{#ExeName}\DefaultIcon"; \
    ValueType: string; ValueData: "{app}\{#ExeName},0"
Root: HKLM; Subkey: "SOFTWARE\Classes\Applications\{#ExeName}\shell\open\command"; \
    ValueType: string; ValueData: """{app}\{#ExeName}"" ""%1"""

; "Registered Applications" entry — surfaces the app in
; Settings → Apps → Default apps, and in the modern taskbar pin UX.
Root: HKLM; Subkey: "SOFTWARE\RegisteredApplications"; \
    ValueType: string; ValueName: "{#AppName}"; ValueData: "SOFTWARE\LocalAIStack\Capabilities"; Flags: uninsdeletevalue
Root: HKLM; Subkey: "SOFTWARE\LocalAIStack\Capabilities"; \
    ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"
Root: HKLM; Subkey: "SOFTWARE\LocalAIStack\Capabilities"; \
    ValueType: string; ValueName: "ApplicationDescription"; \
    ValueData: "Local AI assistant: chat, admin console, model resolver, Cloudflare tunnel."
Root: HKLM; Subkey: "SOFTWARE\LocalAIStack\Capabilities"; \
    ValueType: string; ValueName: "ApplicationIcon"; ValueData: "{app}\{#ExeName},0"

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Both default to OFF — the user must opt in to either. Inno's
; "checkedonce" flag means "checked the first time the user reaches
; this page, but only once per machine"; we use plain unchecked so
; they're a deliberate choice on every install.
Name: "desktopicon";   Description: "Create a &desktop shortcut";   GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startmenuicon"; Description: "Create &Start menu entries";   GroupDescription: "Additional shortcuts:"

[Files]
; Compiled launcher — the ONLY EXE installed on disk. Handles every
; mode the app needs (start, stop, setup, reconfigure, test) via
; switches. The installer EXE itself is not bundled into {app}; users
; who want to reconfigure re-run the installer from Apps & Features.
Source: "..\LocalAIStack.exe";         DestDir: "{app}";                    Flags: ignoreversion

; Frozen GUI (PyInstaller one-folder)
Source: "..\dist\gui\*";               DestDir: "{app}\gui";                Flags: ignoreversion recursesubdirs createallsubdirs

; Backend source
Source: "..\backend\*";                DestDir: "{app}\backend";            Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\tools\*";                  DestDir: "{app}\tools";              Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\config\*";                 DestDir: "{app}\config";             Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\*";               DestDir: "{app}\scripts";            Flags: ignoreversion recursesubdirs createallsubdirs

; Embedded Python for bootstrap (used by -Setup to create venvs)
Source: "..\vendor\python-3.12-embed\*"; DestDir: "{app}\vendor\python-3.12-embed"; Flags: ignoreversion recursesubdirs createallsubdirs

; Native binaries
Source: "..\vendor\qdrant\*";          DestDir: "{app}\vendor\qdrant";      Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\vendor\llama-server\*";    DestDir: "{app}\vendor\llama-server"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\vendor\cloudflared\*";     DestDir: "{app}\vendor\cloudflared"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\vendor\inno-setup\*";      DestDir: "{app}\vendor\inno-setup";  Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

; Assets
Source: "..\assets\*";                 DestDir: "{app}\assets";             Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
; Start Menu — every entry targets LocalAIStack.exe (the only EXE on
; disk). Reconfigure / Repair are switches, not separate binaries.
; AppUserModelID stamps the shortcut so its taskbar slot matches the
; runtime EXE — that's what makes "Pin to taskbar" work cleanly and
; keeps the running app in the same slot as the pinned shortcut.
Name: "{group}\{#AppName}";             Filename: "{app}\{#ExeName}"; \
    AppUserModelID: "{#AppAUMID}"; Tasks: startmenuicon
Name: "{group}\Admin Console";          Filename: "{app}\{#ExeName}"; \
    Parameters: "-Admin"; AppUserModelID: "{#AppAUMID}"; Tasks: startmenuicon
Name: "{group}\Health Check";           Filename: "{app}\{#ExeName}"; \
    Parameters: "-Test"; AppUserModelID: "{#AppAUMID}"; Tasks: startmenuicon
Name: "{group}\Reconfigure {#AppName}"; Filename: "{app}\{#ExeName}"; \
    Parameters: "-SetupGui"; AppUserModelID: "{#AppAUMID}"; Tasks: startmenuicon
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"

; Desktop (single shortcut for the runtime EXE).
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#ExeName}"; \
    AppUserModelID: "{#AppAUMID}"; Tasks: desktopicon

[Run]
; Phase 1 (silent, hidden, always runs): create the Python venvs and
; download non-model vendor binaries. Models are skipped here and
; pulled later in phase 2 via the wizard. Runs through the runtime
; EXE so we don't ship a second binary.
Filename: "{app}\{#ExeName}"; Parameters: "-Setup -SkipModels"; \
    Description: "Create Python environments"; \
    Flags: runhidden waituntilterminated; \
    StatusMsg: "Creating Python environments and downloading binaries…"

; Phase 2 (post-install, optional): launch the setup wizard via the
; runtime EXE's -SetupGui mode. Kicks off the model pull in the
; background and exits. The user can deselect this checkbox if they
; prefer to run setup later from the Start menu.
Filename: "{app}\{#ExeName}"; Parameters: "-SetupGui"; \
    Description: "Run the setup wizard now (admin user, Cloudflare, model download)"; \
    Flags: nowait postinstall skipifsilent

; Phase 3 (post-install, optional): launch the runtime EXE. Disabled
; by default — the wizard from phase 2 needs to finish first or the
; runtime's preflight will block on the missing admin user.
Filename: "{app}\{#ExeName}"; \
    Description: "Start Local AI Stack"; \
    Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
; Stop all services before uninstalling
Filename: "{app}\{#ExeName}"; Parameters: "-Stop"; Flags: runhidden skipifdoesntexist

; Uninstall cloudflared service (if registered)
Filename: "{app}\vendor\cloudflared\cloudflared.exe"; Parameters: "service uninstall"; \
    Flags: runhidden skipifdoesntexist

[UninstallDelete]
; Remove compiled .pyc caches (written at runtime)
Type: filesandordirs; Name: "{app}\backend\__pycache__"
Type: filesandordirs; Name: "{app}\tools\__pycache__"

; NOTE: %LOCALAPPDATA%\LocalAIStack is intentionally NOT removed on uninstall.
; It contains the user's database, conversation history, model files, and .env.
; The user must delete it manually if they want a clean slate.

[Code]
// Detect a previous installation. We never run side-by-side — the same
// {#AppId} ensures Inno reuses the prior install dir and overwrites in
// place. Show a single confirmation so the user knows they're editing
// an existing installation rather than creating a new one.
function InitializeSetup(): Boolean;
var
  PrevVersion, PrevDir: String;
  Msg: String;
begin
  Result := True;
  if RegQueryStringValue(HKLM, 'SOFTWARE\LocalAIStack', 'InstallDir', PrevDir) then begin
    RegQueryStringValue(HKLM, 'SOFTWARE\LocalAIStack', 'AppVersion', PrevVersion);
    if PrevVersion = '' then PrevVersion := '(unknown)';
    Msg := 'An existing Local AI Stack installation was detected:' + #13#10 + #13#10 +
           '    Location: ' + PrevDir + #13#10 +
           '    Version:  ' + PrevVersion + #13#10 + #13#10 +
           'Setup will repair / upgrade this installation in place.' + #13#10 +
           'Your data in %LOCALAPPDATA%\LocalAIStack (database, models, .env)' + #13#10 +
           'will be preserved. Continue?';
    if MsgBox(Msg, mbConfirmation, MB_YESNO) = IDNO then begin
      Result := False;
    end;
  end;
end;

// Write AppVersion to registry after install so we can detect upgrades.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    RegWriteStringValue(HKLM, 'SOFTWARE\LocalAIStack', 'AppVersion', '{#AppVersion}');
  end;
end;
