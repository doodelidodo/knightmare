; Windows-Installer fuer Knightmare (Inno Setup 6).
;   iscc /DAppVersion=1.6.0 desktop\installer.iss
; Erwartet den PyInstaller-Ordner in dist\Knightmare.
; Installiert pro Benutzer - braucht keine Administratorrechte.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1C0E9A-3B1E-4D7C-9A52-7E1B2C4D8F10}
AppName=Knightmare
AppVersion={#AppVersion}
AppPublisher=aBetterDodo
AppPublisherURL=https://abetterdodo.ch
AppSupportURL=https://github.com/doodelidodo/knightmare/issues
DefaultDirName={autopf}\Knightmare
DefaultGroupName=Knightmare
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Knightmare-Windows-Setup
SetupIconFile=icons\icon.ico
UninstallDisplayIcon={app}\Knightmare.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Knightmare\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Knightmare"; Filename: "{app}\Knightmare.exe"
Name: "{autodesktop}\Knightmare"; Filename: "{app}\Knightmare.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Knightmare.exe"; Description: "{cm:LaunchProgram,Knightmare}"; Flags: nowait postinstall skipifsilent

; Die Daten (%LOCALAPPDATA%\Knightmare) bleiben bei der Deinstallation
; bewusst liegen - eine Neuinstallation soll die Analysen nicht verlieren.
