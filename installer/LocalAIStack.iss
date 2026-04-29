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
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startmenuicon"; Description: "Create Start Menu shortcut"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; Compiled launcher
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
; Start Menu
Name: "{group}\{#AppName}";        Filename: "{app}\{#ExeName}";  Tasks: startmenuicon
Name: "{group}\Admin Console";     Filename: "{app}\{#ExeName}";  Parameters: "-Admin"; Tasks: startmenuicon
Name: "{group}\Health Check";      Filename: "{app}\{#ExeName}";  Parameters: "-Test"; Tasks: startmenuicon
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"

; Desktop
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#ExeName}";  Tasks: desktopicon

[Run]
; Run -Setup -SkipModels as admin during install:
; creates vendor venvs, skips model downloads (done later via wizard)
Filename: "{app}\{#ExeName}"; Parameters: "-Setup -SkipModels"; \
    Description: "Create Python environments"; \
    Flags: runhidden waituntilterminated

; First launch: wizard detects no .env / no admin user → opens setup wizard
Filename: "{app}\{#ExeName}"; \
    Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

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
