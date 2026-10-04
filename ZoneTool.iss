; ZoneTool Windows Installer script (Inno Setup)
; Builds: installer\ZoneTool-Setup-v1.0.0.exe
; Prerequisite: dist\ZoneTool.exe already built via PyInstaller, e.g.:
;   python -m PyInstaller --noconfirm --onefile --windowed --icon="resources\Xml-tool_37095.ico" --name "ZoneTool" main.py

#define MyAppName "ZoneTool"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Syncronic IT Solutions Pvt Ltd"
#define MyAppExeName "ZoneTool.exe"
#define MyAppIcon "resources\Xml-tool_37095.ico"

[Setup]
AppId={{AA7337D2-B0F3-4B2B-954A-7CE76164BBBA}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Fixed at the drive root (not {autopf}\ZoneTool / Program Files) - the
; app writes its own projects/output/assets folders BESIDE the EXE at
; runtime (see gui/main_window.py's frozen-aware APP_ROOT), which must
; work for a standard user with no elevation on every normal launch.
DefaultDirName=C:\ZoneTool
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Normal Windows "Apps & features" / uninstall entry is created automatically
; by Inno Setup - no extra config needed for that.
OutputDir=installer
OutputBaseFilename=ZoneTool-Setup-v{#MyAppVersion}
SetupIconFile={#MyAppIcon}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; Installs without requiring administrator rights - a standard user
; can create/write C:\ZoneTool directly on typical Windows systems,
; and [Dirs] below explicitly grants standard users full read/write on
; the installed folder regardless, so every normal launch afterward
; (creating/updating projects, output, assets) never needs elevation.
; If a particular target system's C:\ ACL is more restrictive, the
; wizard still offers to elevate (PrivilegesRequiredOverridesAllowed).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Only the built application and its own icon - no .py source, no
; __pycache__, no build cache, no test files.
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyAppIcon}"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; Pre-created with explicit standard-user read/write permissions, so
; normal day-to-day use (auto-save, Generate XML) never needs
; elevation regardless of the install-time ACLs inherited from C:\ -
; the app's own startup mkdir (main_window.py) is then just a no-op
; confirmation that these already exist.
Name: "{app}"; Permissions: users-modify
Name: "{app}\projects"; Permissions: users-modify
Name: "{app}\output"; Permissions: users-modify
Name: "{app}\assets"; Permissions: users-modify

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Xml-tool_37095.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Xml-tool_37095.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

; Deliberately no [UninstallDelete] entries for projects/output/assets -
; the installer must never delete the user's own project/output data
; during an uninstall (or an update reinstall), even though that data
; happens to live inside {app} in this fixed-path layout. Normal
; uninstall only removes the files this installer itself placed
; (the exe + icon, tracked automatically by Inno Setup), leaving any
; projects/output/assets content the user created completely untouched.
