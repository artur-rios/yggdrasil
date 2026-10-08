; Windows installer for the yggdrasil console (Inno Setup 6).
;
;     flutter build windows --release --build-name=<version>
;     iscc /DAppVersion=<version> windows\installer\yggdrasil.iss      (from console\)
;
; Writes build\windows\installer\yggdrasil-console-<version>-setup.exe. CI builds it on every run
; (.github/workflows/ci.yml) and attaches it to each release (release.yml).
;
; Installs for the current user by default, so no administrator rights are needed; the first page
; offers "install for all users" to someone who has them. Upgrades in place: the AppId below
; identifies the application across versions, so NEVER change it.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Yggdrasil"
#define AppExe "yggdrasil.exe"
#define BuildDir "..\..\build\windows\x64\runner\Release"

[Setup]
AppId={{406345E9-F9D8-4A4F-8193-0CDD5E703F68}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Artur Rios
AppPublisherURL=https://github.com/artur-rios/yggdrasil
AppComments=Deploy console: the status of every system and application on each host, in every environment.
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\..\build\windows\installer
OutputBaseFilename=yggdrasil-console-{#AppVersion}-setup
SetupIconFile=..\runner\resources\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole Flutter bundle: the executable, flutter_windows.dll, the plugins' DLLs and data\.
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
