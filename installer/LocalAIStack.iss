; Inno Setup 6 script for Local AI Stack
; Build with: ISCC.exe installer\LocalAIStack.iss
; Or: .\LocalAIStack.ps1 -BuildInstaller
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

[Registry]
Root: HKLM; Subkey: "SOFTWARE\LocalAIStack"; ValueType: string; ValueName: "InstallDir"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "SOFTWARE\LocalAIStack"; ValueType: string; ValueName: "Installed"; ValueData: "1"

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
; Compiled launcher (day-to-day runtime — what the desktop / Start
; menu shortcuts target).
Source: "..\LocalAIStack.exe";         DestDir: "{app}";                    Flags: ignoreversion

; Compiled installer (first-time setup + reconfiguration). Reachable
; from the "Reconfigure Local AI Stack" Start menu entry and from
; Apps & Features → Modify; not given a desktop shortcut.
Source: "..\LocalAIStackInstaller.exe"; DestDir: "{app}";                   Flags: ignoreversion skipifsourcedoesntexist

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
; Start Menu — runtime shortcuts (LocalAIStack.exe). The installer EXE
; only appears as a "Reconfigure" entry, never as the primary launch.
Name: "{group}\{#AppName}";        Filename: "{app}\{#ExeName}";  Tasks: startmenuicon
Name: "{group}\Admin Console";     Filename: "{app}\{#ExeName}";  Parameters: "-Admin"; Tasks: startmenuicon
Name: "{group}\Health Check";      Filename: "{app}\{#ExeName}";  Parameters: "-Test"; Tasks: startmenuicon
Name: "{group}\Reconfigure {#AppName}"; Filename: "{app}\LocalAIStackInstaller.exe"; Parameters: "-Reconfigure"; Tasks: startmenuicon
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"

; Desktop (single shortcut for the runtime EXE — no installer on
; the desktop).
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#ExeName}";  Tasks: desktopicon

[Run]
; Phase 1 (silent, hidden, always runs): create the Python venvs and
; download non-model vendor binaries. Models are skipped here and
; pulled later in phase 2 via the wizard.
Filename: "{app}\LocalAIStackInstaller.exe"; Parameters: "-RepairOnly"; \
    Description: "Create Python environments"; \
    Flags: runhidden waituntilterminated; \
    StatusMsg: "Creating Python environments and downloading binaries…"

; Phase 2 (post-install, optional): launch the setup wizard. The
; installer EXE runs the wizard, kicks off the model pull in the
; background, and exits. The user can deselect this checkbox if they
; prefer to run setup later from the Start menu.
Filename: "{app}\LocalAIStackInstaller.exe"; \
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
// Detect a previous installation and offer upgrade path.
function InitializeSetup(): Boolean;
var
  PrevVersion: String;
begin
  Result := True;
  if RegQueryStringValue(HKLM, 'SOFTWARE\LocalAIStack', 'AppVersion', PrevVersion) then begin
    if PrevVersion = '{#AppVersion}' then begin
      MsgBox('Version {#AppVersion} is already installed. Reinstalling will overwrite the application files.' + #13#10 +
             'Your data in %LOCALAPPDATA%\LocalAIStack will not be affected.', mbInformation, MB_OK);
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
