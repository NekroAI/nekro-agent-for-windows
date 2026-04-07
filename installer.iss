; =========================
; Nekro-Agent 稳定安装脚本（工业级）
; =========================

#define MyAppName "Nekro Agent"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Nekro AI"
#define MyAppExeName "NekroAgent.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=installer
OutputBaseFilename=NekroAgent-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UsePreviousAppDir=yes
CloseApplications=yes
RestartApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

; ✅ 文件详细信息版本号
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer
VersionInfoProductName={#MyAppName}
VersionInfoCopyright=Copyright (C) 2024 {#MyAppPublisher}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Files]
Source: "dist\NekroAgent\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\NekroAgent\_internal\*"; DestDir: "{app}\_internal"; \
    Flags: ignoreversion recursesubdirs createallsubdirs uninsnosharedfileprompt

[Icons]
Name: "{group}\{#MyAppName}";          Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}";     Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";    Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

; =========================
; ✅ 卸载逻辑全部在 [Code] 中处理，彻底规避 PathRedir Bug
; =========================
[Code]
procedure DeleteFileIfExists(const FilePath: String);
begin
  if FileExists(FilePath) then
    DeleteFile(FilePath);
end;

procedure DeleteDirIfExists(const DirPath: String);
begin
  if DirExists(DirPath) then
    DelTree(DirPath, True, True, True);
end;

procedure CleanupLauncherState();
var
  AppDir: String;
  LocalDataDir: String;
begin
  AppDir := AddBackslash(ExpandConstant('{app}'));
  LocalDataDir := ExpandConstant('{localappdata}\NekroAgent');

  DeleteFileIfExists(AppDir + 'config.json');
  DeleteFileIfExists(AppDir + 'image_config.json');
  DeleteFileIfExists(AppDir + 'debug.log');

  DeleteDirIfExists(AppDir + '_internal');
  DeleteDirIfExists(AppDir + 'browser_profile');
  DeleteDirIfExists(AppDir + 'runtime_cache');
  DeleteDirIfExists(AppDir + 'shared');
  DeleteDirIfExists(AppDir + 'logs');
  DeleteDirIfExists(LocalDataDir);
  RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'NekroAgentLauncher');

  if DirExists(ExpandConstant('{app}')) then
    RemoveDir(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  { ─── 卸载前：强制杀进程 ─── }
  if CurUninstallStep = usUninstall then
  begin
    Exec(
      ExpandConstant('{sys}\taskkill.exe'),
      '/F /IM {#MyAppExeName}',
      '',
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    );
    { ResultCode 不判断，进程不存在时 taskkill 返回非零但无需报错 }
  end;

  { ─── 卸载后：清理启动器本地残留 ─── }
  if CurUninstallStep = usPostUninstall then
    CleanupLauncherState();
end;
