; Tetra 的 Windows 安装程序（Inno Setup 6）。由 packaging/build_windows.py 在打包完以后调用：
;   ISCC /DAppVersion=0.1.2 /DSourceDir=<dist\Tetra> /DOutputDir=<dist> packaging\tetra.iss
; 生成 dist\Tetra-Setup-<版本>.exe。安装时可以选：安装位置、只为自己装还是为所有用户装、桌面快捷方式、开机自启。
; 默认只为当前用户安装（不用管理员权限），装在 %LOCALAPPDATA%\Programs\Tetra。
; 用户数据（题库、设置、Key）不在安装目录里，在 %LOCALAPPDATA%\Tetra；卸载时会问要不要一起删。

#ifndef AppVersion
  #define AppVersion "0.1.2"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Tetra"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{6F1C3A52-6B7E-4F0E-9C2D-5A8B1E4D7C93}
AppName=Tetra
AppVersion={#AppVersion}
AppVerName=Tetra {#AppVersion}
AppPublisher=Tetra
AppComments=Tetra 命题智能体：高中数学出题、组卷
DefaultDirName={autopf}\Tetra
DisableDirPage=no
DisableProgramGroupPage=yes
DisableWelcomePage=no
UsePreviousAppDir=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=tetra.ico
UninstallDisplayIcon={app}\Tetra.exe
UninstallDisplayName=Tetra 命题智能体
; Tetra 开着时（desktop.py 拿着这个互斥量）安装、卸载都会先请用户关掉它
AppMutex=TetraDesktopApp
CloseApplications=yes
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=Tetra-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ShowLanguageDialog=no

[Languages]
Name: "zh"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面创建快捷方式"; GroupDescription: "快捷方式："
Name: "autostart"; Description: "开机时自动启动 Tetra（窗口最小化，以后可以在设置里改）"; GroupDescription: "开机启动："; Flags: unchecked

[InstallDelete]
; 升级时先删掉旧版本的依赖（版本可能换了）；用户数据不在这里
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Tetra"; Filename: "{app}\Tetra.exe"; Comment: "Tetra 命题智能体"
Name: "{autodesktop}\Tetra"; Filename: "{app}\Tetra.exe"; Comment: "Tetra 命题智能体"; Tasks: desktopicon

[Registry]
; 和设置里的"开机时自动启动"是同一项（mingti/desktop_os.py）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Tetra"; ValueData: """{app}\Tetra.exe"" --autostart"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\Tetra.exe"; Description: "现在打开 Tetra"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 安装以后在设置里建的快捷方式（安装程序自己建的会自动删）
Type: files; Name: "{userdesktop}\Tetra.lnk"
Type: files; Name: "{userprograms}\Tetra.lnk"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
    { 安装以后在设置里打开的开机自启，安装时没勾也要去掉 }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Tetra');
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\Tetra');
    if DirExists(DataDir) and not UninstallSilent then
      if MsgBox('要同时删除 Tetra 的数据吗？' + #13#10#13#10 +
                '题库、试卷、对话、设置和 API Key 都在：' + #13#10 + DataDir + #13#10#13#10 +
                '选“否”会保留这些数据，以后重新安装还能接着用。' + #13#10 +
                '（在设置里把数据文件夹换到别处的，那个文件夹不会被删。）',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
