; MyAppVersion can be overridden on the ISCC command line with /DMyAppVersion=x.y.z
; (deploy/windows/build-mpau-agent.ps1 passes the local_agent __version__ so the
; installer, the release manifest, and the agent's self-update check stay in sync).
#ifndef MyAppVersion
#define MyAppVersion "0.3.0"
#endif

#define MyAppName "MPAU 本地执行助手"
#define MyAppPublisher "MPAU"
#define MyAppExeName "MPAU-Agent.exe"

[Setup]
AppId={{D79C6F80-3CB3-4BF6-90D6-8B86FD1841D9}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\MPAU-Agent
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; The normal wizard closes the old helper before replacing its program files.
CloseApplications=yes
RestartApplications=no
OutputDir=output
OutputBaseFilename=MPAU-Agent-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\..\dist\MPAU-Agent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v ""MPAU Agent"" /f"; Flags: runhidden
