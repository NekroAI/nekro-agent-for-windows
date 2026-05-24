; =========================
; Nekro Agent 安装脚本
; =========================

#define MyAppName "Nekro Agent"
#define MyAppPublisher "Nekro AI"
#define MyAppExeName "NekroAgent.exe"

; 版本号从 version.txt 读取（由 Inno Setup 预处理器 #file 指令）
#define VersionFile FileOpen(SourcePath + "\version.txt")
#define MyAppVersion Trim(FileRead(VersionFile))
#expr FileClose(VersionFile)

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Nekro-Agent
DefaultGroupName={#MyAppName}
OutputDir=installer
OutputBaseFilename=NekroAgent-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 默认用户级安装，无 UAC 弹窗；允许用户选择全局安装（会提升权限）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

UsePreviousAppDir=yes
CloseApplications=yes
RestartApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer
VersionInfoProductName={#MyAppName}
VersionInfoCopyright=Copyright (C) 2025 {#MyAppPublisher}

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
; runasoriginaluser: 即使安装器以 admin 运行，也用原始用户身份启动程序
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent runasoriginaluser

; =========================
; 卸载逻辑
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

  { 安装目录下的数据子目录 }
  DeleteDirIfExists(AppDir + 'data');
  DeleteDirIfExists(AppDir + '_internal');

  { fallback 数据目录（用户装在 Program Files 时会用到） }
  if DirExists(LocalDataDir) then
    DelTree(LocalDataDir, True, True, True);

  { 清理开机自启注册表项 }
  RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'NekroAgentLauncher');

  { 尝试移除空的安装目录 }
  if DirExists(ExpandConstant('{app}')) then
    RemoveDir(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
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
  end;

  if CurUninstallStep = usPostUninstall then
    CleanupLauncherState();
end;

{ 当用户选择"为所有用户安装"（admin 权限）时，自动切换到 Program Files 路径 }
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if IsAdmin then
    begin
      if Pos(ExpandConstant('{localappdata}'), WizardDirValue) > 0 then
        WizardForm.DirEdit.Text := ExpandConstant('{autopf}\NekroAgent');
    end;
  end;
end;
