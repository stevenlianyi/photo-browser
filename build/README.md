# photo-browser 打包与分发（R10）

本文只讲**怎么把项目打成 Windows 上可分发的形态**、以及打不通时怎么查。
产品本身的用法见仓库根 `README.md`。

---

## 一、一句话

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build\package.ps1
```

产出：

```
dist\PhotoBrowser-Setup-0.2.0.exe      ← 给用户的双击安装包（Inno Setup 6，LZMA2）
dist\photo-browser\photo-browser.exe   ← 免安装的独立 exe（PyInstaller --onedir）
```

> **版本号口径（2026-10-16 起）**：默认版本 = **`0.2.0`**，三处**必须同步**改，
> 否则会出现「安装包叫 0.1.0、界面/前端是 0.2.0」这种不报错的错位：
> `build\package.ps1` 的 `$Version` 默认值、`build\installer.iss` 的 `PbVer` 默认值、
> `code\webserver\package.json` 的 `version`。
> R10 的历史产物 `PhotoBrowser-Setup-0.1.0.exe` **保留留档，不要删**。

常用变体：

```powershell
... -Version 0.3.0                 # 换版本号（决定安装包文件名）；默认是 0.2.0
... -SkipFrontend                  # 不重跑 npm run build（用上一次的快照，**有风险**）
... -SkipInstaller                 # 只要 exe 目录，不要安装包
... -NoClean                       # 不删 dist\photo-browser（增量重打）
... -KeepWork                      # 保留 build\work（排障 PyInstaller 时用）
```

---

## 二、前置（三样，都不是运行时依赖）

| 东西 | 用途 | 检查 | 装 |
|---|---|---|---|
| `code\.venv` | 项目依赖 | `code\.venv\Scripts\python.exe -V` | 见仓库根 `README.md` |
| PyInstaller | 冻结成 exe | `code\.venv\Scripts\python.exe -m PyInstaller --version` | `code\.venv\Scripts\python.exe -m pip install -i https://pypi.org/simple pyinstaller` |
| Inno Setup 6 | 打安装包 | `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe` | `winget install --id JRSoftware.InnoSetup -e` |
| Node / npm | 构建前端 | `node -v` | 装 Node LTS |

> ⚠️ **PyInstaller / Inno Setup 故意不写进 `requirements.txt`**：
> 它们只在**打包时**用，不是运行时依赖。把它们写进去会让"只想跑源码"的人
> 也装一堆编译工具。

`package.ps1` 会自己检查前三样，缺哪样就给出**可直接照抄**的安装命令。

---

## 三、打包时到底做了什么

```
① 前端构建      code\webserver  ->  npm run build  ->  code\webserver\dist
② PyInstaller   build\photo-browser.spec            ->  dist\photo-browser\
③ 布局整理      _internal\web / _internal\models    ->  搬到 exe 同级
④ Inno Setup    build\installer.iss                 ->  dist\PhotoBrowser-Setup-<版本>.exe
```

体积参考（本机实测，R11 之后）：

| 项 | 大小 |
|---|---|
| `dist\photo-browser\`（onedir 全量） | **656.4 MB** |
| └ `models\buffalo_l\`（3 个 onnx） | 319.4 MB |
| └ `web\`（前端） | 1.0 MB |
| └ `_internal\`（Python/依赖 DLL） | 336 MB |
| 安装包（LZMA2/max） | 见 `package.ps1` 末尾输出 |

> 为什么比"`.venv` 498.7MB + 模型 325.5MB = 824MB"的粗略估算小：
> ① 只收 `buffalo_l` 里**真正加载的 3 个**模型（不含 `2d106det` / `genderage`，
> 共 6MB）；② PyInstaller 只带**被 import 到的**模块，测试套件 / pytest / pip /
> pygments / tkinter / 未用到的 scipy 子模块都不会进包。

---

## 四、安装后的布局（**装到哪，数据就在哪**）

```
{你选的安装目录}                    ← 默认 D:\PhotoLib，安装向导里可改
├── app\                            ← ★ 程序本体（升级只覆盖这里）
│   ├── photo-browser.exe
│   ├── _internal\
│   ├── models\buffalo_l\*.onnx     ← 随包模型（离线可用）
│   └── web\                        ← 前端
├── photo\                          ← 你的照片（**只读**，卸载绝不删）
├── thumb\                          ← 缩略图 / 人脸裁剪图（卸载绝不删）
├── db\                             ← SQLite 库 + imports\ + exports\（卸载绝不删）
│   └── exports\era-sample\         ← R11 年代抽样导出的 zip
├── backup\                         ← 备份落点（卸载绝不删）
├── app.ini                         ← ★ 外置配置（用户可改；升级不覆盖、卸载不删除）
└── unins000.exe
```

**两个目录不是同一个东西**（这是最容易设计错的一处）：

| | 谁决定 | 默认 | 用户能改吗 |
|---|---|---|---|
| ① 程序安装目录 | 安装向导「选择目标位置」 | `D:\PhotoLib` | ✅ 首次安装时自选 |
| ② 照片库根目录 `photo_root` | `app.ini` 的 `[paths] photo_root` | **= ①**（安装器写 `{app}`） | ✅ 改 `app.ini` 后重启 |

② **默认跟随 ①**：装到 `E:\apps\photobrowser`，`app.ini` 里就是
`photo_root = E:\apps\photobrowser`，数据也在那里。**没有任何地方写死 `d:\PhotoLib`。**

⚠️ 为什么默认不是 `C:\Program Files\...`：程序要往 `photo_root` 里写
`thumb\` / `db\` / `backup\`，而 Program Files 是受保护目录。
用户仍然可以自选到那里 —— 所以**首次运行会做可写性检查**，不可写时给可读提示
并指出 `app.ini` 的完整路径（不是抛堆栈）。

---

## 五、`app.ini`（用户唯一能改的配置出口）

读取优先级：

```
--config <path>  →  环境变量 PHOTO_BROWSER_INI  →  <exe目录>\app.ini  →  <exe目录>\..\app.ini
```

首次运行都没有时**自动生成**一份带注释的 `app.ini`（`photo_root` = 实际安装目录）。
**只在文件不存在时生成** —— 安装器的 `[Code]` 与程序自己的 `ensureIni()` 都是这条纪律。

⚠️ 源码模式（未冻结）下**不走 app.ini 这条隐式通道**，仍然认
`code/src/config/local_settings.py`（行为与 R10 之前逐字相同）。
想在源码模式下用 INI：显式 `--config` 或设 `PHOTO_BROWSER_INI`。

要用什么就改什么：

```ini
[paths]
photo_root = D:\PhotoLib      ; 照片必须放在 <photo_root>\photo\ 之下（原图区只读）

[server]
port = 8765                   ; 被占用就改这里
open_browser = 1              ; 0 = 启动后不自动开浏览器

[models]
model_root =                  ; 空 = 随包 -> %USERPROFILE%\.insightface -> 联网下载
```

---

## 六、exe 的子命令（打包用户没有 Python，所有 CLI 都得有 exe 形态）

| 命令 | 用途 |
|---|---|
| （不带参数） | 起服务 + 自动开浏览器 |
| `--init-db` | **建库 / 补表**（幂等）。自动建表失败、或需要人工确认时用 |
| `--check-only` | 只做启动自检（路径 / 可写性 / 建库 / 端口），不起服务 |
| `--backup` | 停服务后备份 `db\` + `thumb\`（**不含 `photo\` 原图**） |
| `--list-backups` | 列出已有备份 |
| `--restore <编号>` | 从备份还原（会先自动另存现状） |
| `--gen-thumbs` | 批量预生成缩略图（**进程池**路径） |
| `--config <path>` | 指到另一份 `app.ini`（多库并存 / 排障） |
| `--port` `--host` `--db` `--root` `--thumb` | 本次运行的覆盖（不写回 `app.ini`） |
| `--no-browser` `--no-auto-db` | 排障开关 |

启动时会**自动建库 / 补表**（`main.cli.ensureDb`，幂等）：
老库升级上来时，R11 新增的 `pb_era_sample` / `pb_era_sample_item` 会被自动补建，
**旧数据一行不动**。所以正常用户永远不需要手动跑 `--init-db`。

---

## 七、排障（每一条都是实测踩过的）

### 7.1 `无法加载文件 ...，因为在此系统上禁止运行脚本`
PowerShell 的执行策略。**不要**去 `Set-ExecutionPolicy` 改机器设置，加参数即可：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build\package.ps1
```

### 7.2 `Missing closing '}'` / 脚本里的中文变成乱码
`package.ps1` **必须**是 **UTF-8 with BOM**。PowerShell 5.1 对没有 BOM 的 `.ps1`
按系统 ANSI 代码页（本机 936）读，中文乱码里出现的引号/大括号会直接把解析器搞崩。
重存成带 BOM：

```powershell
$p = (Resolve-Path build\package.ps1).Path
$t = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
[IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding($true)))
```

（`start.cmd` 是**相反**的规矩：它必须**纯 ASCII**，因为 cmd.exe 没有 BOM 机制。）

### 7.3 `[spec] 缺少必需文件：... code\webserver\dist`
前端没构建。`cd code\webserver; npm install; npm run build`。
spec 在缺文件时**直接报错退出**（而不是打一个静默残缺的包）。

### 7.4 装完打开是白屏 / 任何页面 404，但 `/api/health` 是 200
这是前端产物没进包的**静默失败**形态（`main/app.py` 会安静退回"只返回 JSON"）。
查两处：

1. `dist\photo-browser\web\index.html` 在不在（`package.ps1` 会把 `web\`
   从 `_internal\` 搬出来，搬到一半失败会报错）；
2. 冻结时的路径解析只允许走 `common/resourcePath.py`。
   再出现 `os.path.dirname(__file__)` 式的相对推导就会重现这个 bug。

### 7.5 人脸提取"一张都没检出" / 子进程一声不响地退出
三个已知原因，按顺序查：

1. **`freeze_support()` 丢了**。它必须是 `tools/appLauncher.py` 里
   `if __name__ == "__main__":` 的**第一条语句**。丢了的话子进程会因为
   `unrecognized arguments: --multiprocessing-fork` 立刻退出，
   主进程只看到"子进程全部退出，只收到 0/N 块"。
2. **被 spawn 的子进程模块没进 `hiddenimports`**（`engine.face.pool` /
   `processor.scanner.runner` / `processor.media.thumbMaker`）。
3. **insightface 的均值形状没落到 `_internal\objects\`**。
   `insightface/data/pickle_object.py` 冻结时**硬编码**从 `sys._MEIPASS\objects\`
   找 `meanshape_68.pkl`；只在 `_internal\insightface\data\objects\` 里放一份
   是不够的（它只会打印一行 `[Error] File not found:`，不抛异常）。
   `photo-browser.spec` 里那一条 `datas.append((..., "objects"))` 就是为它加的。

### 7.6 一次 spawn 就卡死 / 临时目录爆盘
**用了 `--onefile`**。本项目缩略图用 `ProcessPoolExecutor`、人脸用
`multiprocessing.get_context("spawn")`，onefile 每次 spawn 都要把几百 MB
重新解包 ⇒ 必须 `--onedir`。`photo-browser.spec` 写死了 onedir，别改。

### 7.7 模型路径
三段回退（`engine.face.engine.modelBaseRoot`）：

```
① <安装目录>\app\models\buffalo_l        ← 随包（离线可用，优先）
② %USERPROFILE%\.insightface\models\buffalo_l
③ 都没有 → 由 insightface 联网下载（约 325MB）
```

想验证"确实走的随包"：把 `%USERPROFILE%\.insightface` 改名，
然后跑 `photo-browser.exe`（人脸识别能跑通 = 没联网）。
⚠️ **绝不把 `buffalo_l.zip`（275MB 下载残留）打进包**，spec 里只收 3 个 onnx。

### 7.8 SmartScreen / 杀软
未签名的 exe 会被 SmartScreen 拦：**更多信息 → 仍要运行**。
PyInstaller 的产物也常被杀软误报。想彻底解决只能**代码签名**（见 §九）。

### 7.9 静默安装 —— **必须显式带 `/CURRENTUSER`（或 `/ALLUSERS`）**
```
PhotoBrowser-Setup-0.2.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART ^
    /CURRENTUSER /DIR="E:\apps\photobrowser"
/LOG="%TEMP%\pb-setup.log"        ; 指定日志路径（否则是 %TEMP%\Setup Log *.txt）
```

⚠️ 少了 `/CURRENTUSER` 会**看起来像卡死**（实测：进程 CPU 只涨 0.1 秒、
内存停在 10MB、目标目录**一直不出现**、几十秒后仍然如此）。
原因在 `installer.iss` 的 `PrivilegesRequiredOverridesAllowed=dialog`：
它让向导首页能切「为所有人安装 / 只为我安装」，而**静默模式下没人点那个选择**，
Inno 就停在那里等一个选择框。交互安装（双击）没有这个问题，不必改配置。

安装日志：`[Setup]` 里开了 `SetupLogging=yes`，日志默认落在
`%TEMP%\Setup Log <日期> #<序号>.txt`。

---

## 八、验收时怎么快速手工过一遍

```powershell
# 1) 免安装形态：直接双击（会把数据落到 dist\photo-browser\ 下，属正常）
.\dist\photo-browser\photo-browser.exe

# 2) 指到一套全新的库（"空白目录自动建库"那条验收）
.\dist\photo-browser\photo-browser.exe --db D:\t\db\x.db --root D:\t\photo --thumb D:\t\thumb

# 3) 只要 exe 侧的证据，不起服务
.\dist\photo-browser\photo-browser.exe --check-only

# 4) 建库/补表兜底
.\dist\photo-browser\photo-browser.exe --init-db
```

安装包侧的必测四条：

1. **换目录**：安装向导里把目标位置改成 `E:\apps\photobrowser`，
   装完确认 `E:\apps\photobrowser\app.ini` 里 `photo_root` 就是它；
2. **升级不覆盖**：装 v0.1 → 手改 `app.ini`（例如换端口）→ 装 v0.2 →
   手改的还在（这条专测 `[Code]` 里的 `FileExists` 判断，用 `[INI]` 段必然失败）；
3. **卸载不删数据**：卸载后 `photo\` / `thumb\` / `db\` / `backup\` 全在，
   且卸载提示里的路径**跟着实际安装目录走**（不是写死的 `d:\PhotoLib`）；
4. **受保护目录**：装到 `C:\Program Files\PhotoBrowser` → 首次运行必须给出
   可读提示并指出 `app.ini` 的完整路径。

---

## 九、还没做的（需要决策）

| 事项 | 说明 |
|---|---|
| **代码签名** | 未签名 → SmartScreen 警告 + 杀软误报。OV 证书约 200–400 元/年，EV 约 1500+ 元/年；签名需要联网时间戳服务，CI 里还要保管私钥（建议放 HSM/密钥库）。当前**未签**。 |
| **模型拆成可选下载包** | 模型 319MB 占了安装包的绝大部分。拆出去能让安装包掉到 ~340MB，代价是"首次人脸识别要联网下载"（离线机装不上这个特性）。当前**随包**。 |
| **安装向导增加"照片库位置"页** | 现在 `photo_root` 默认跟随安装目录、之后改 `app.ini`。若用户想把程序装 C 盘、照片放 D 盘，需要多一页向导 + 安装器写两处。当前**没做**（用 `app.ini` 兜住）。 |
