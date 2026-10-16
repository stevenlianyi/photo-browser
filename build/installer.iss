; ==========================================================================
;  photo-browser 安装包（Inno Setup 6）
;
;  编译：
;     ISCC.exe /DPbVer=0.2.0 build\installer.iss
;   （或直接跑 build\package.ps1，它会连 PyInstaller 一起做完）
;   ⚠️ 参数名是 **PbVer**（不是 Ver）—— 与下面 #ifndef 里那个名字必须逐字一致；
;      写错不会报错，只会静默用默认版本号。build\package.ps1 传的就是 /DPbVer=。
;
;  产出：dist\PhotoBrowser-Setup-<版本>.exe
;
;  ⚠️ 本文件是**安装器**，不是"另一个打包器"：
;     它只负责把 dist\photo-browser\ 铺到用户选的目录、写一份 app.ini、
;     建快捷方式、并在卸载时**绝不碰用户数据**。
;
;  三条设计决定（每一条都对应一个曾经踩过的坑）
;  ------------------------------------------
;   ① 「安装目录」与「照片库根目录」**不是同一个东西**，但默认必须一致：
;        DefaultDirName 只是**默认值**（首次安装可在向导里改）；
;        而 app.ini 里的 photo_root 由安装器写成 **{app}**（用户选的目录），
;        **绝不写死 d:\PhotoLib** —— 否则程序装到 E:、数据跑去 D:，
;        用户毫不知情。
;   ② app.ini **只在不存在时生成**（[Code] + FileExists，**不许用 [INI] 段**）：
;        [INI] 段每次安装都会写，升级时会把用户改过的 photo_root 覆盖回默认
;        ⇒ 用户的照片还在老路径上，界面里却"全没了"。
;   ③ 卸载**只删 {app}\app**（程序本体）+ 快捷方式：
;        photo\ / thumb\ / db\ / backup\ 是**用户资产**，
;        绝不许出现 `{app}\*` 这种通配 —— 那会删掉全家照片。
; ==========================================================================

#ifndef PbVer
  #define PbVer "0.2.0"
#endif

#define AppName "Photo Browser"
#define AppExe  "photo-browser.exe"
#define AppPublisher "photo-browser project"

[Setup]
; AppId 一旦发布就**不许再改**：升级靠它识别"这是同一个程序的另一个版本"
AppId={{9F2C4B7E-5A31-4D6E-9C0B-7E4A8D3F1C02}
AppName={#AppName}
AppVersion={#PbVer}
AppVerName={#AppName} {#PbVer}
AppPublisher={#AppPublisher}
VersionInfoVersion={#PbVer}

; ---- 目标目录：默认 D:\PhotoLib，但**用户可改** ----
; 为什么不默认 C:\Program Files：程序要往 photo_root 里写 thumb\ db\ backup\，
; 那是受保护目录，非管理员进程写不进去。
; 依然会有人手选到受保护目录 —— 所以首次运行有可写性检查（见 build/README.md §四）。
DefaultDirName=D:\PhotoLib
; ---- 下面三行是「让用户选目录」这件事的全部配置（**别改**）----
;   DisableDirPage=auto（**默认值，不要改成 yes/no**）：
;       首次安装**显示**「选择目标位置」页（用户可改目录）；
;       升级**跳过**该页并沿用旧目录。
;       改成 no 会让升级也弹目录页 -> 用户误改 -> 新旧文件分散、老数据成孤儿。
DisableDirPage=auto
;   升级时目录页被跳过，但「准备安装」页仍把目标目录显示出来（看得见 = 不会误以为装到别处）
AlwaysShowDirOnReadyPage=yes
;   升级沿用上一次的安装目录（默认值，写出来是为了表意）
UsePreviousAppDir=yes

DisableProgramGroupPage=yes
DefaultGroupName={#AppName}
AllowNoIcons=yes
WizardStyle=modern

; ---- 权限 ----
; lowest = 普通用户即可安装（装到 D:\PhotoLib 这种用户可写的位置不需要管理员）。
; OverridesAllowed=dialog 允许在向导首页切到「为所有人安装」（会提权），
; 这样想装到 Program Files 的高级用户也有路走，同时**不强迫所有人提权**。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; ---- 平台 ----
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

; ---- 压缩与输出 ----
Compression=lzma2/max
SolidCompression=yes
; OutputDir 相对于**本脚本所在目录**（build\）=> 仓库根的 dist\
OutputDir=..\dist
OutputBaseFilename=PhotoBrowser-Setup-{#PbVer}
; 安装日志（排障时让用户把这个文件发过来）
SetupLogging=yes

UninstallDisplayName={#AppName} {#PbVer}
UninstallDisplayIcon={app}\app\{#AppExe}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; ⚠️ Inno Setup 6 的**官方安装包不含简体中文** .isl
;    （实测 6.7.3 的 Languages\ 目录里只有 30 个语种，没有 ChineseSimplified.isl）。
;    写一行不存在的语言文件会让**整次编译失败**，所以这里保持注释。
;    想要中文安装界面：把社区版 ChineseSimplified.isl 放进 Inno 的 Languages\ 目录，
;    再取消下面这行的注释。
;Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

; ==========================================================================
[Files]
; ⚠️ 全部程序本体都进 {app}\app\（**子目录**）：
;    photo/thumb/db 是**数据**，app\ 是**程序**。升级只覆盖 app\（数据零风险），
;    卸载只删 app\。把 exe 直接铺在 {app} 根会让升级/卸载与用户照片搅在一起。
;
; ⚠️ `Excludes` 是**文件级的第二道防线**，对应一个实测踩到的坑：
;    payload 目录被"试跑过一次"就会多出 `app.ini` 与 `log\`，一旦进包就会落到
;    `{app}\app\app.ini`，而读取顺序里 `<exe目录>\app.ini` 优先于 `<exe目录>\..\app.ini`
;    ⇒ 它会**遮蔽安装器写的 {app}\app.ini**，用户改配置不生效且**不报错**。
;    第一道防线是 build\package.ps1 里的 Remove-RuntimeLeftovers（PyInstaller 后 + ISCC 前各一次）；
;    这里再挡一层，是为了"有人绕过 package.ps1、手敲 ISCC"的情况。
;    反过来**绝不能**排除 _internal / web / models —— 那是程序本体。
Source: "..\dist\photo-browser\*"; DestDir: "{app}\app"; \
    Excludes: "app.ini,log,log\*,db,db\*,thumb,thumb\*,photo,photo\*,backup,backup\*"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

; ==========================================================================
[Dirs]
; 数据目录先建出来（空的），有两个好处：
;   ① 用户一眼能看到"照片该放哪"、"备份在哪"；
;   ② uninsneveruninstall 让它们在卸载后依然存在 ——
;      「照片库没被卸载删掉」这条红线因此**即使一次都没扫描过也能被验证**。
; ⚠️ 只建目录，**不往 photo\ 里放任何东西**（原图区只读是应用层的硬约束 1）。
Name: "{app}\photo";   Flags: uninsneveruninstall
Name: "{app}\thumb";   Flags: uninsneveruninstall
Name: "{app}\db";      Flags: uninsneveruninstall
Name: "{app}\backup";  Flags: uninsneveruninstall

; ==========================================================================
[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\app\{#AppExe}"; WorkingDir: "{app}\app"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\app\{#AppExe}"; WorkingDir: "{app}\app"; Tasks: desktopicon
; 配置文件也放一个快捷方式：用户要改 photo_root / port 时**找得到**它
Name: "{autoprograms}\{#AppName} 配置 (app.ini)"; Filename: "{app}\app.ini"

; ==========================================================================
[Run]
; postinstall：安装完勾选即启动。nowait = 不阻塞向导退出；
; skipifsilent = 静默安装时不弹窗（脚本化场景）。
Filename: "{app}\app\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
    Flags: postinstall nowait skipifsilent

; ==========================================================================
[UninstallDelete]
; ⚠️⚠️ **本步最大红线**：这里绝不许出现 `{app}\*` 或 `{app}\photo` 之类的通配。
;     只删**程序本体**目录。photo\ / thumb\ / db\ / backup\ / app.ini 全部保留。
Type: filesandordirs; Name: "{app}\app"

; ==========================================================================
[Code]

const
  CRLF = #13#10;

{ 首次安装时生成的 app.ini（**带注释** —— 用户第一次打开要知道每一项是什么）。
  逐项说明里的路径都写成通用说法，不写死任何盘符。 }
function BuildIniText(const PhotoRoot: String): String;
var
  S: String;
begin
  S :=
    '; ============================================================' + CRLF +
    '; photo-browser 外置配置（app.ini）' + CRLF +
    ';' + CRLF +
    ';   本文件是**用户可改**的配置出口。改完**重启 Photo Browser 生效**。' + CRLF +
    ';   它在程序安装目录下；exe 在 <安装目录>\app\ 里。' + CRLF +
    ';' + CRLF +
    ';   * 升级**不会覆盖**本文件（安装器只在文件不存在时生成）。' + CRLF +
    ';   * 卸载**不会删除**本文件，也不会删除 photo / thumb / db / backup。' + CRLF +
    ';   * 路径可以用正斜杠或反斜杠；不要用引号把路径包起来。' + CRLF +
    '; ============================================================' + CRLF +
    CRLF +
    '[paths]' + CRLF +
    '; 照片库根目录。**照片必须放在 <photo_root>\photo\ 之下**（原图只读）。' + CRLF +
    '; 想看别处的照片：让照片位于 <那个目录>\photo\ 之内，再把这里指到它的上一级。' + CRLF +
    'photo_root = ' + PhotoRoot + CRLF +
    '; 缩略图 / 人脸裁剪图根目录。留空 = <photo_root>\thumb' + CRLF +
    'thumb_root =' + CRLF +
    '; SQLite 主库文件。留空 = <photo_root>\db\photolib.db' + CRLF +
    'db_file =' + CRLF +
    CRLF +
    '[server]' + CRLF +
    '; 只允许回环地址（127.0.0.1 / localhost / ::1）。填别的会被拒绝启动 ——' + CRLF +
    '; 本服务零鉴权，开 0.0.0.0 等于把全家照片发到局域网。' + CRLF +
    'host = 127.0.0.1' + CRLF +
    '; 端口被占用时改这里（或启动时加 --port 8799）' + CRLF +
    'port = 8765' + CRLF +
    '; 1 = 启动后自动打开浏览器；0 = 不打开' + CRLF +
    'open_browser = 1' + CRLF +
    CRLF +
    '[models]' + CRLF +
    '; 人脸模型基目录。留空 = 依次找' + CRLF +
    ';   1) <安装目录>\app\models\buffalo_l   （随包，优先，离线可用）' + CRLF +
    ';   2) %USERPROFILE%\.insightface\models\buffalo_l （你以前下过）' + CRLF +
    ';   3) 都没有才联网下载（约 325MB，离线机请把模型拷进 1）' + CRLF +
    'model_root =' + CRLF +
    CRLF +
    '[scan]' + CRLF +
    '; 一次扫描任务处理到该张数即暂停，等用户「继续下一批」' + CRLF +
    'batch_size = 100' + CRLF;
  Result := S;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  IniPath: String;
begin
  if CurStep = ssPostInstall then
  begin
    IniPath := ExpandConstant('{app}\app.ini');

    { ★ 关键：**只在文件不存在时**写。
        升级时若覆盖，用户自定义的 photo_root 会被改回默认 ——
        而他的照片还在老路径上，表现为「升级完照片全没了」。 }
    if not FileExists(IniPath) then
    begin
      if not SaveStringToFile(IniPath, BuildIniText(ExpandConstant('{app}')), False) then
        MsgBox('无法写入配置文件：' + IniPath + CRLF + CRLF +
               '程序仍可启动（会用默认值），但请手工创建该文件并把 photo_root 指向有写权限的目录。',
               mbError, MB_OK);
    end;
  end;
end;

{ 卸载前提示：**动态拼路径**（跟着用户选的安装目录 / 他改过的 photo_root 走），
  绝不写死 d:\PhotoLib —— 写死的话，装到 E: 的用户会以为照片被删了。

  ⚠️ 必须用 UninstallSilent() 把提示挡在静默模式之外：
     否则静默卸载会**卡在一个没人点的消息框上**（实测：进程占着、目录不删、
     看起来像死锁）。同时用 Log() 把同样的内容写进卸载日志 ——
     静默卸载下"提示确实显示了、路径确实是动态的"这件事，只能靠日志取证。 }
function InitializeUninstall(): Boolean;
var
  IniPath, PhotoRoot, Text: String;
begin
  IniPath := ExpandConstant('{app}\app.ini');
  PhotoRoot := ExpandConstant('{app}');
  if FileExists(IniPath) then
    PhotoRoot := GetIniString('paths', 'photo_root', PhotoRoot, IniPath);

  Text := '即将卸载 Photo Browser 的**程序本体**。' + CRLF + CRLF +
          '下面这些**都会保留**（它们是你的数据，卸载绝不删除）：' + CRLF +
          '    照片    ： ' + PhotoRoot + '\photo' + CRLF +
          '    缩略图  ： ' + PhotoRoot + '\thumb' + CRLF +
          '    数据库  ： ' + PhotoRoot + '\db' + CRLF +
          '    备份    ： ' + PhotoRoot + '\backup' + CRLF +
          '    配置    ： ' + IniPath + CRLF + CRLF +
          '确认已经不需要这些数据时，请手工删除上面的目录。';

  Log('=========================================================');
  Log(Text);
  Log('=========================================================');

  if not UninstallSilent then
    MsgBox(Text, mbInformation, MB_OK);
  Result := True;
end;
