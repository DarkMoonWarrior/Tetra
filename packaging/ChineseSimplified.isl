; *** Inno Setup 6.5.0+ 简体中文消息（Tetra 安装程序用）***
; 按 Inno Setup 自带的 Default.isl 逐条翻译；%1、%n、[name] 这些占位符原样保留。
; 文件必须存成带 BOM 的 UTF-8（packaging/build_windows.py 打包前会检查）。

[LangOptions]
LanguageName=简体中文
LanguageID=$0804
LanguageCodePage=936
DialogFontName=Microsoft YaHei UI
DialogFontSize=9
WelcomeFontName=Microsoft YaHei UI
WelcomeFontSize=14

[Messages]

; *** Application titles
SetupAppTitle=安装
SetupWindowTitle=安装 - %1
UninstallAppTitle=卸载
UninstallAppFullTitle=卸载 %1

; *** Misc. common
InformationTitle=提示
ConfirmTitle=确认
ErrorTitle=错误

; *** SetupLdr messages
SetupLdrStartupMessage=将要安装 %1。要继续吗？
LdrCannotCreateTemp=无法创建临时文件，安装已中止
LdrCannotExecTemp=无法运行临时文件夹里的文件，安装已中止
HelpTextNote=

; *** Startup error messages
LastErrorMessage=%1。%n%n错误 %2：%3
SetupFileMissing=安装目录里缺少文件 %1。请修复这个问题，或者重新下载一份安装程序。
SetupFileCorrupt=安装文件已损坏，请重新下载一份安装程序。
SetupFileCorruptOrWrongVer=安装文件已损坏，或者和这个版本的安装程序不兼容。请修复这个问题，或者重新下载一份安装程序。
InvalidParameter=命令行参数无效：%n%n%1
SetupAlreadyRunning=安装程序已经在运行。
WindowsVersionNotSupported=这个程序不支持你电脑上的 Windows 版本。
WindowsServicePackRequired=这个程序需要 %1 Service Pack %2 或更新的版本。
NotOnThisPlatform=这个程序不能在 %1 上运行。
OnlyOnThisPlatform=这个程序只能在 %1 上运行。
OnlyOnTheseArchitectures=这个程序只能安装在为下面这些处理器架构设计的 Windows 上：%n%n%1
WinVersionTooLowError=这个程序需要 %1 %2 或更新的版本。
WinVersionTooHighError=这个程序不能安装在 %1 %2 或更新的版本上。
AdminPrivilegesRequired=安装这个程序需要用管理员账户登录。
PowerUserPrivilegesRequired=安装这个程序需要用管理员账户或 Power Users 组的成员登录。
SetupAppRunningError=安装程序发现 %1 正在运行。%n%n请先关掉它的所有窗口，再点“确定”继续；或者点“取消”退出。
UninstallAppRunningError=卸载程序发现 %1 正在运行。%n%n请先关掉它的所有窗口，再点“确定”继续；或者点“取消”退出。

; *** Startup questions
PrivilegesRequiredOverrideTitle=选择安装方式
PrivilegesRequiredOverrideInstruction=选择安装方式
PrivilegesRequiredOverrideText1=%1 可以为这台电脑上的所有用户安装（需要管理员权限），也可以只为你自己安装。
PrivilegesRequiredOverrideText2=%1 可以只为你自己安装，也可以为这台电脑上的所有用户安装（需要管理员权限）。
PrivilegesRequiredOverrideAllUsers=为所有用户安装(&A)
PrivilegesRequiredOverrideAllUsersRecommended=为所有用户安装(&A)（推荐）
PrivilegesRequiredOverrideCurrentUser=只为我安装(&M)
PrivilegesRequiredOverrideCurrentUserRecommended=只为我安装(&M)（推荐）

; *** Misc. errors
ErrorCreatingDir=安装程序没能创建文件夹“%1”
ErrorTooManyFilesInDir=文件夹“%1”里的文件太多，没法再创建文件

; *** Setup common messages
ExitSetupTitle=退出安装
ExitSetupMessage=安装还没完成。现在退出的话，程序不会被安装。%n%n以后可以再运行安装程序完成安装。%n%n要退出安装吗？
AboutSetupMenuItem=关于安装程序(&A)...
AboutSetupTitle=关于安装程序
AboutSetupMessage=%1 版本 %2%n%3%n%n%1 主页：%n%4
AboutSetupNote=
TranslatorNote=

; *** Buttons
ButtonBack=< 上一步(&B)
ButtonNext=下一步(&N) >
ButtonInstall=安装(&I)
ButtonOK=确定
ButtonCancel=取消
ButtonYes=是(&Y)
ButtonYesToAll=全部是(&A)
ButtonNo=否(&N)
ButtonNoToAll=全部否(&O)
ButtonFinish=完成(&F)
ButtonBrowse=浏览(&B)...
ButtonWizardBrowse=浏览(&R)...
ButtonNewFolder=新建文件夹(&M)

; *** "Select Language" dialog messages
SelectLanguageTitle=选择安装语言
SelectLanguageLabel=选择安装过程中使用的语言。

; *** Common wizard text
ClickNext=点“下一步”继续，或者点“取消”退出安装。
BeveledLabel=
BrowseDialogTitle=选择文件夹
BrowseDialogLabel=在下面的列表里选一个文件夹，然后点“确定”。
NewFolderName=新建文件夹

; *** "Welcome" wizard page
WelcomeLabel1=欢迎使用 [name] 安装向导
WelcomeLabel2=将要在你的电脑上安装 [name/ver]。%n%n建议先关掉其他正在运行的程序，再继续。

; *** "Password" wizard page
WizardPassword=密码
PasswordLabel1=这个安装程序有密码保护。
PasswordLabel3=请输入密码，然后点“下一步”继续。密码区分大小写。
PasswordEditLabel=密码(&P)：
IncorrectPassword=密码不对，请再试一次。

; *** "License Agreement" wizard page
WizardLicense=许可协议
LicenseLabel=继续之前，请先阅读下面的重要信息。
LicenseLabel3=请阅读下面的许可协议。继续安装之前，必须接受协议里的条款。
LicenseAccepted=我接受协议(&A)
LicenseNotAccepted=我不接受协议(&D)

; *** "Information" wizard pages
WizardInfoBefore=信息
InfoBeforeLabel=继续之前，请先阅读下面的重要信息。
InfoBeforeClickLabel=准备好继续安装时，点“下一步”。
WizardInfoAfter=信息
InfoAfterLabel=继续之前，请先阅读下面的重要信息。
InfoAfterClickLabel=准备好继续安装时，点“下一步”。

; *** "User Information" wizard page
WizardUserInfo=用户信息
UserInfoDesc=请填写你的信息。
UserInfoName=用户名(&U)：
UserInfoOrg=单位(&O)：
UserInfoSerial=序列号(&S)：
UserInfoNameRequired=必须填写用户名。

; *** "Select Destination Location" wizard page
WizardSelectDir=选择安装位置
SelectDirDesc=把 [name] 安装到哪里？
SelectDirLabel3=安装程序会把 [name] 安装到下面这个文件夹。
SelectDirBrowseLabel=点“下一步”继续。想换一个文件夹，点“浏览”。
DiskSpaceGBLabel=至少需要 [gb] GB 的可用磁盘空间。
DiskSpaceMBLabel=至少需要 [mb] MB 的可用磁盘空间。
CannotInstallToNetworkDrive=不能安装到网络驱动器上。
CannotInstallToUNCPath=不能安装到 UNC 路径上。
InvalidPath=请输入带盘符的完整路径，比如：%n%nC:\APP%n%n或者这样的 UNC 路径：%n%n\\server\share
InvalidDrive=选的驱动器或 UNC 共享不存在，或者访问不了，请换一个。
DiskSpaceWarningTitle=磁盘空间不够
DiskSpaceWarning=安装至少需要 %1 KB 的可用空间，但选的驱动器只有 %2 KB 可用。%n%n还是要继续吗？
DirNameTooLong=文件夹名或路径太长了。
InvalidDirName=文件夹名无效。
BadDirName32=文件夹名不能包含下面这些字符：%n%n%1
DirExistsTitle=文件夹已经存在
DirExists=文件夹：%n%n%1%n%n已经存在。还是要安装到这个文件夹吗？
DirDoesntExistTitle=文件夹不存在
DirDoesntExist=文件夹：%n%n%1%n%n不存在。要新建这个文件夹吗？

; *** "Select Components" wizard page
WizardSelectComponents=选择组件
SelectComponentsDesc=要安装哪些组件？
SelectComponentsLabel2=勾选要安装的组件，不要的取消勾选。准备好以后点“下一步”。
FullInstallation=完整安装
CompactInstallation=简洁安装
CustomInstallation=自定义安装
NoUninstallWarningTitle=组件已经存在
NoUninstallWarning=安装程序发现电脑上已经装了下面这些组件：%n%n%1%n%n取消勾选不会卸载它们。%n%n还是要继续吗？
ComponentSize1=%1 KB
ComponentSize2=%1 MB
ComponentsDiskSpaceGBLabel=当前选择至少需要 [gb] GB 的磁盘空间。
ComponentsDiskSpaceMBLabel=当前选择至少需要 [mb] MB 的磁盘空间。

; *** "Select Additional Tasks" wizard page
WizardSelectTasks=选择附加任务
SelectTasksDesc=还要做哪些设置？
SelectTasksLabel2=选择安装 [name] 时要一起做的事，然后点“下一步”。

; *** "Select Start Menu Folder" wizard page
WizardSelectProgramGroup=选择开始菜单文件夹
SelectStartMenuFolderDesc=把程序的快捷方式放在哪里？
SelectStartMenuFolderLabel3=安装程序会在下面这个开始菜单文件夹里创建程序的快捷方式。
SelectStartMenuFolderBrowseLabel=点“下一步”继续。想换一个文件夹，点“浏览”。
MustEnterGroupName=必须填写文件夹名。
GroupNameTooLong=文件夹名或路径太长了。
InvalidGroupName=文件夹名无效。
BadGroupName=文件夹名不能包含下面这些字符：%n%n%1
NoProgramGroupCheck2=不创建开始菜单文件夹(&D)

; *** "Ready to Install" wizard page
WizardReady=准备安装
ReadyLabel1=安装程序已经准备好，可以开始在你的电脑上安装 [name] 了。
ReadyLabel2a=点“安装”开始安装；想检查或修改设置，点“上一步”。
ReadyLabel2b=点“安装”开始安装。
ReadyMemoUserInfo=用户信息：
ReadyMemoDir=安装位置：
ReadyMemoType=安装类型：
ReadyMemoComponents=选择的组件：
ReadyMemoGroup=开始菜单文件夹：
ReadyMemoTasks=附加任务：

; *** TDownloadWizardPage wizard page and DownloadTemporaryFile
DownloadingLabel2=正在下载文件...
ButtonStopDownload=停止下载(&S)
StopDownload=确定要停止下载吗？
ErrorDownloadAborted=下载已中止
ErrorDownloadFailed=下载失败：%1 %2
ErrorDownloadSizeFailed=获取大小失败：%1 %2
ErrorProgress=进度无效：%1 / %2
ErrorFileSize=文件大小不对：应该是 %1，实际是 %2

; *** TExtractionWizardPage wizard page and ExtractArchive
ExtractingLabel=正在解压文件...
ButtonStopExtraction=停止解压(&S)
StopExtraction=确定要停止解压吗？
ErrorExtractionAborted=解压已中止
ErrorExtractionFailed=解压失败：%1

; *** Archive extraction failure details
ArchiveIncorrectPassword=密码不对
ArchiveIsCorrupted=压缩包已损坏
ArchiveUnsupportedFormat=不支持这种压缩包格式

; *** "Preparing to Install" wizard page
WizardPreparing=正在准备安装
PreparingDesc=安装程序正在准备在你的电脑上安装 [name]。
PreviousInstallNotCompleted=上一个程序的安装或卸载还没完成，需要重启电脑才能完成。%n%n重启以后，请再运行安装程序完成 [name] 的安装。
CannotContinue=安装程序没法继续，请点“取消”退出。
ApplicationsFound=下面这些程序正在使用安装程序要更新的文件，建议让安装程序自动关掉它们。
ApplicationsFound2=下面这些程序正在使用安装程序要更新的文件，建议让安装程序自动关掉它们。安装完成后，安装程序会试着重新打开它们。
CloseApplications=自动关掉这些程序(&A)
DontCloseApplications=不关掉这些程序(&D)
ErrorCloseApplications=安装程序没能自动关掉所有程序。继续之前，建议先关掉正在使用这些文件的程序。
PrepareToInstallNeedsRestart=安装程序需要重启电脑。重启以后，请再运行安装程序完成 [name] 的安装。%n%n要现在重启吗？

; *** "Installing" wizard page
WizardInstalling=正在安装
InstallingLabel=请稍候，安装程序正在你的电脑上安装 [name]。

; *** "Setup Completed" wizard page
FinishedHeadingLabel=[name] 安装完成
FinishedLabelNoIcons=已经在你的电脑上装好了 [name]。
FinishedLabel=已经在你的电脑上装好了 [name]。可以通过安装好的快捷方式打开它。
ClickFinish=点“完成”退出安装。
FinishedRestartLabel=要完成 [name] 的安装，需要重启电脑。要现在重启吗？
FinishedRestartMessage=要完成 [name] 的安装，需要重启电脑。%n%n要现在重启吗？
ShowReadmeCheck=是，我要看说明文件
YesRadio=是，现在重启电脑(&Y)
NoRadio=否，我稍后自己重启(&N)
; used for example as 'Run MyProg.exe'
RunEntryExec=运行 %1
; used for example as 'View Readme.txt'
RunEntryShellExec=查看 %1

; *** "Setup Needs the Next Disk" stuff
ChangeDiskTitle=安装程序需要下一张磁盘
SelectDiskLabel2=请插入第 %1 张磁盘，然后点“确定”。%n%n如果这张磁盘上的文件在下面显示的文件夹以外的地方，请输入正确的路径，或者点“浏览”。
PathLabel=路径(&P)：
FileNotInDir2=在“%2”里找不到文件“%1”。请插入正确的磁盘，或者换一个文件夹。
SelectDirectoryLabel=请指定下一张磁盘的位置。

; *** Installation phase messages
SetupAborted=安装没有完成。%n%n请修复问题后再运行安装程序。
AbortRetryIgnoreSelectAction=选择操作
AbortRetryIgnoreRetry=重试(&T)
AbortRetryIgnoreIgnore=忽略错误并继续(&I)
AbortRetryIgnoreCancel=取消安装
RetryCancelSelectAction=选择操作
RetryCancelRetry=重试(&T)
RetryCancelCancel=取消

; *** Installation status messages
StatusClosingApplications=正在关闭程序...
StatusCreateDirs=正在创建文件夹...
StatusExtractFiles=正在释放文件...
StatusDownloadFiles=正在下载文件...
StatusCreateIcons=正在创建快捷方式...
StatusCreateIniEntries=正在写入 INI 设置...
StatusCreateRegistryEntries=正在写入注册表...
StatusRegisterFiles=正在注册文件...
StatusSavingUninstall=正在保存卸载信息...
StatusRunProgram=正在完成安装...
StatusRestartingApplications=正在重新打开程序...
StatusRollback=正在撤销更改...

; *** Misc. errors
ErrorInternal2=内部错误：%1
ErrorFunctionFailedNoCode=%1 失败
ErrorFunctionFailed=%1 失败；代码 %2
ErrorFunctionFailedWithMessage=%1 失败；代码 %2。%n%3
ErrorExecutingProgram=无法运行文件：%n%1

; *** Registry errors
ErrorRegOpenKey=打开注册表项时出错：%n%1\%2
ErrorRegCreateKey=创建注册表项时出错：%n%1\%2
ErrorRegWriteKey=写入注册表项时出错：%n%1\%2

; *** INI errors
ErrorIniEntry=在文件“%1”里写入 INI 设置时出错。

; *** File copying errors
FileAbortRetryIgnoreSkipNotRecommended=跳过这个文件（不推荐）(&S)
FileAbortRetryIgnoreIgnoreNotRecommended=忽略错误并继续（不推荐）(&I)
SourceIsCorrupted=源文件已损坏
SourceDoesntExist=源文件“%1”不存在
SourceVerificationFailed=源文件校验失败：%1
VerificationSignatureDoesntExist=签名文件“%1”不存在
VerificationSignatureInvalid=签名文件“%1”无效
VerificationKeyNotFound=签名文件“%1”用了未知的密钥
VerificationFileNameIncorrect=文件名不对
VerificationFileTagIncorrect=文件标记不对
VerificationFileSizeIncorrect=文件大小不对
VerificationFileHashIncorrect=文件哈希值不对
ExistingFileReadOnly2=现有的文件是只读的，没法替换。
ExistingFileReadOnlyRetry=去掉只读属性后重试(&R)
ExistingFileReadOnlyKeepExisting=保留现有的文件(&K)
ErrorReadingExistingDest=读取现有文件时出错：
FileExistsSelectAction=选择操作
FileExists2=文件已经存在。
FileExistsOverwriteExisting=覆盖现有的文件(&O)
FileExistsKeepExisting=保留现有的文件(&K)
FileExistsOverwriteOrKeepAll=后面的冲突也这样处理(&D)
ExistingFileNewerSelectAction=选择操作
ExistingFileNewer2=现有的文件比安装程序要装的更新。
ExistingFileNewerOverwriteExisting=覆盖现有的文件(&O)
ExistingFileNewerKeepExisting=保留现有的文件（推荐）(&K)
ExistingFileNewerOverwriteOrKeepAll=后面的冲突也这样处理(&D)
ErrorChangingAttr=修改现有文件的属性时出错：
ErrorCreatingTemp=在安装文件夹里创建文件时出错：
ErrorReadingSource=读取源文件时出错：
ErrorCopying=复制文件时出错：
ErrorDownloading=下载文件时出错：
ErrorExtracting=解压时出错：
ErrorReplacingExistingFile=替换现有文件时出错：
ErrorRestartReplace=重启后替换失败：
ErrorRenamingTemp=在安装文件夹里重命名文件时出错：
ErrorRegisterServer=无法注册 DLL/OCX：%1
ErrorRegSvr32Failed=RegSvr32 失败，退出代码 %1
ErrorRegisterTypeLib=无法注册类型库：%1

; *** Uninstall display name markings
; used for example as 'My Program (32-bit)'
UninstallDisplayNameMark=%1（%2）
; used for example as 'My Program (32-bit, All users)'
UninstallDisplayNameMarks=%1（%2，%3）
UninstallDisplayNameMark32Bit=32 位
UninstallDisplayNameMark64Bit=64 位
UninstallDisplayNameMarkAllUsers=所有用户
UninstallDisplayNameMarkCurrentUser=当前用户

; *** Post-installation errors
ErrorOpeningReadme=打开说明文件时出错。
ErrorRestartingComputer=安装程序没能重启电脑，请手动重启。

; *** Uninstaller messages
UninstallNotFound=文件“%1”不存在，没法卸载。
UninstallOpenError=打不开文件“%1”，没法卸载
UninstallUnsupportedVer=这个版本的卸载程序认不出卸载日志“%1”的格式，没法卸载
UninstallUnknownEntry=卸载日志里有未知的条目（%1）
ConfirmUninstall=确定要从电脑上完全删除 %1 和它的所有组件吗？
UninstallOnlyOnWin64=这个程序只能在 64 位 Windows 上卸载。
OnlyAdminCanUninstall=这个程序只有管理员才能卸载。
UninstallStatusLabel=请稍候，正在从你的电脑上删除 %1。
UninstalledAll=已经从你的电脑上删除了 %1。
UninstalledMost=%1 卸载完成。%n%n有些内容没能删除，可以手动删除。
UninstalledAndNeedsRestart=要完成 %1 的卸载，需要重启电脑。%n%n要现在重启吗？
UninstallDataCorrupted=文件“%1”已损坏，没法卸载

; *** Uninstallation phase messages
ConfirmDeleteSharedFileTitle=删除共享文件？
ConfirmDeleteSharedFile2=系统显示下面这个共享文件已经没有程序在用了。要让卸载程序删除它吗？%n%n如果还有程序在用这个文件，删掉以后那些程序可能没法正常运行。拿不准的话请选“否”，留着这个文件不会有任何影响。
SharedFileNameLabel=文件名：
SharedFileLocationLabel=位置：
WizardUninstalling=卸载状态
StatusUninstalling=正在卸载 %1...

; *** Shutdown block reasons
ShutdownBlockReasonInstallingApp=正在安装 %1。
ShutdownBlockReasonUninstallingApp=正在卸载 %1。

[CustomMessages]

NameAndVersion=%1 版本 %2
AdditionalIcons=附加快捷方式：
CreateDesktopIcon=在桌面创建快捷方式(&D)
CreateQuickLaunchIcon=创建快速启动栏快捷方式(&Q)
ProgramOnTheWeb=%1 的网站
UninstallProgram=卸载 %1
LaunchProgram=打开 %1
AssocFileExtension=把 %2 文件关联到 %1(&A)
AssocingFileExtension=正在把 %2 文件关联到 %1...
AutoStartProgramGroupDescription=开机启动：
AutoStartProgram=开机时自动启动 %1
AddonHostProgramNotFound=在你选的文件夹里找不到 %1。%n%n还是要继续吗？
