# photo-browser

> 本地个人/家庭**照片浏览与人脸归类**工具 —— 把磁盘上散落的照片，变成可按人浏览、可跨年代追溯的私人相册。
> 存储：**SQLite（单文件、零安装）** ｜ 后端：Python + FastAPI + 原生 sqlite3 ｜ 前端：Vue3 + Vite + Tailwind + Element Plus

**当前状态**：✅ **12 步编码全部完成**（2026-10-07）。S0 准确度验证**已通过**；M1~M3 已过，**M4 = 自己真正用一周**。
> 📍 **新增需求（2026-10-07）**：地点维度（人物 → 地点列表 → 时间线）。拆为 **R3 → R4 → R5** 三步，见下表。

---

## 一、这是什么

一个本地优先的照片浏览 + 人脸归类系统，对标 Picasa 3 但更轻。核心约束：

- **纯浏览、不编辑**原图（原文件只读，绝不写/删/改名）。
- **人脸识别 + 归类**是核心，且要求跨年龄准确度。
- **人员数据来自通讯录**（CSV / vCard 导入）—— 这是所有开源方案都满足不了的部分，必须自研。
- 规模：**约 3 万张**照片；单机；**无需 GPU、无需装数据库服务**。

---

## 二、怎么启动

### 一句话版（已经装好依赖 + 已有照片库时）

```powershell
.\start.cmd
```

然后打开 **http://127.0.0.1:8765** 。`Ctrl+C` 停止。

> 也可以**直接双击 `start.cmd`**。启动失败时窗口会停住等你按键 —— 不会一闪而过、
> 让你对着一个空窗口发呆。

> ⚠️ **为什么是 `.cmd` 不是 `.ps1`**：PowerShell 5.1 在执行策略为
> `Restricted` / `AllSigned` 时**拒绝运行 `.ps1`**，报错
> `无法加载文件…因为在此系统上禁止运行脚本`（`UnauthorizedAccess`）。
> `.cmd` 由 cmd.exe 执行、不受该策略约束，双击也能跑。
> ⚠️ **不要为了跑本项目去改执行策略**（`Set-ExecutionPolicy`）—— 那是机器/用户的安全设置，
> 项目没理由为了自己的方便让人把它调低。若你本来就想放开，那是你自己的决定：
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。

### 第一次跑（换机器 / 新克隆仓库）

```powershell
# ① 建venv（**官方源必须**，本机默认镜像查不到 vobject）
py -3.13 -m venv code\.venv
code\.venv\Scripts\python.exe -m pip install -i https://pypi.org/simple -r requirements.txt
code\.venv\Scripts\python.exe -m pip uninstall -y opencv-python      # 只留 headless

# ② 前端依赖 + 构建（**必须 >= 22**）
cd code\webserver; npm install; npm run build; cd ..\..

# ③ 本机配置（该文件 .gitignore，不入库）
copy code\src\config\local_settings.py.example code\src\config\local_settings.py
notepad code\src\config\local_settings.py                           # 改 PHOTO_ROOT 指向你的照片目录

# ④ 建库 + 扫描（只需一次）
code\.venv\Scripts\python.exe code\src\tools\build_db.py
code\.venv\Scripts\python.exe code\src\tools\scan_cli.py --root d:\PhotoLib\photo

# ⑤ 起服务
.\start.cmd
```

### `start.cmd` 参数（直接透传给 `serve.py`，只有一套 `--` 语法）

| 参数 | 作用 |
|---|---|
| （无） | 起服务，同源提供前端 → 开 **8765** |
| `--dev` | 另起 Vite dev server → 开 **5173**（改前端代码热更时用这个） |
| `--check-only` | 只做体检、不启动。**改配置后先跑这个** |
| `--port 8799` | 换端口（8765 被占时） |
| `--db <路径>` | 换库文件（对着另一个库试调时） |
| `--root <路径>` / `--thumb <路径>` | 覆盖原图根 / 缩略图根（**本进程有效**，退出即失效） |

### 不想用脚本的话，等价的原始命令

```powershell
# ⚠️ 工作目录**必须是 code\src** —— `-m main.app` 靠 sys.path[0]（= 当前目录）
#    找main 包。从 code\ 或仓库根跑会直接 ModuleNotFoundError: No module named 'main'。
#    （本README 之前写的就是错的，已改。）
cd code\src
..\.venv\Scripts\python.exe -m main.app --host 127.0.0.1 --port 8765
```

### 为什么要有启动脚本（不是「顺手加个」）

照着手敲命令有**六个坑**，其中三个是**静默**的 —— 服务起来了、页面是空的、什么都不报错：

| # | 坑 | 静默吗 |
|---|---|---|
| ① | 工作目录必须是 `code\src`，否则 `ModuleNotFoundError` | 否（直接报错，还算友好） |
| ② | 必须用 `code\.venv` 的解释器。裸敲 `python` 会命中 WindowsApps 基础解释器，依赖不同 | **是**（能跑，但行为诡异） |
| ③ | `local_settings.py` 不入库，缺它时回落默认 `d:\PhotoLib` | **是**（服务正常、列表全空，像「扫不出照片」） |
| ④ | `dist/index.html` 不存在时退化成「只返回 JSON 的根路由」 | **是**（`/api/health` 回 ok:true，任何页面都 404） |
| ⑤ | 端口被占时 8765 上可能跑着**另一个库**的服务 | **是**（你在浏览器里看着另一个库的数据） |
| ⑥ | `.ps1` 可能被执行策略直接拦住（上一轮实测撞到） | 否（报错明确，但会让人以为项目坏了） |

`code/src/tools/serve.py` 把① ② ③ ④ ⑤逐条查完再启；查不过就**不给启动**，并打印可照抄的修复命令。
`start.cmd` 负责⑥（用 cmd.exe 而不是 PowerShell）。另外还做两件原始命令不会做的事：

- **打印「已就绪」之前先轮询 `/api/health`**。否则会出现「端口空闲 → 打印地址 → 实际因路径异常退出」，
  用户点开一个连不上的链接，还要回来读日志才知道刚才已经挂了。
- **控制台与日志文件的编码分开处理**：控制台走 Windows 代码页（不处理就是乱码），
  重定向到文件时强制 UTF-8（不处理 `Get-Content` / `git diff` 全乱码）。

### 只想体检配置（改完 `local_settings.py` 后）

```powershell
.\start.cmd --check-only
```

```
== 体检 ==
  [ok] 前端产物：...\code\webserver\dist\index.html
  [ok] 本机配置：...\code\src\config\local_settings.py
  [ok] 原图  ：d:\PhotoLib\photo
  [ok] 缩略图 ：d:\PhotoLib\thumb
  [ok] 库   ：d:\PhotoLib\db\photolib.db
  [ok] 库中照片 2137 张
  [ok] 端口 8765 空闲

== 体检通过（--check-only，未启动） ==
```

### 首次使用前必须知道的两件事

1. **照片要先扫描才看得到**。库里 0 张照片时界面照样起得来，只是列表是空的 ——
   体检会提醒你跑 `scan_cli.py`。
2. **「待确认」需要你先人工确认几张脸才有内容**。自动归属**只用人工确认过的样本**算质心
   （防质心污染，见 §八-5），所以刚上手时 `我不同意` 与自动归属都是空的，**这是正确行为不是故障**。

---

## 三、怎么开始开发

>⚠️ 12 步已经全部执行完毕。下面是**历史与当前状态**，新功能请直接改代码，不要再按步骤推进。

打开 **`plan/step-prompts.md`** 可以看到每一步的原始提示语与执行后的偏差记录。

### 当前进度

| 步骤 | 主题 | 状态 |
|---|---|---|
| **R3** | **修 `(0,0)` 占位坐标**（DR-23） | ✅ 已完成（`tools/fix_placeholder_geo.py`） |
| **R4** | **地点字典基础层**（`pb_place` + `placeStore` + `/api/places`） | ✅ 已完成 |
| **R5** | **地点界面**（地点 → 照片流 + 人物详情时间轴后的「去过的地方」· DR-37/38/39） | ✅ 已完成（`api/place.py` / `PlacesView` / `PlaceDetailView` / `PersonPlaces` + 路由与侧栏接线） |
| **R9** | **照片旋转**（左右转 90° · 显示层 CSS + `pb_photo.rotateDeg` · DR-43） | ⬜ **待做（提示语已写）** |
| **R10** | **打包分发**（独立 exe + 安装包 · `d:\PhotoLib` 默认目录 · DR-44） | ⬜ **待做（提示语已写）** |
| **R7** | **人物头像**（人物库卡片显示照片 + 人脸样本设默认头像 · DR-40/41） | ✅ 已完成（2026-10-08；`AvatarPicker` + `personCoversOf`） |
| **R8** | **照片年代修正**（人工修正拍摄年 → 年代桶跟着对 · DR-42） | ✅ 已完成（2026-10-08；`pb_photo.shotYearOverride` + 照片详情「年代」修正入口；拖拽与 `takenAt` 修正留作 P1/P2） |
| **R2** | **分桶口径修复**（自适应分桶从未生效 · DR-20/21/22） | ✅ 代码已落地（`test_rebucket.py` 36 用例全绿）；⚠️ **验收证据仍欠**（见下） |
| R6 | 照片详情左右翻页（← → + 浮动箭头 · DR-31） | ✅ 已完成 |
| R4a | 地点中文名（境内显示中文 · DR-28/29） | ✅ 已完成 |
| R4b | 目录名线索接入（598 张 · DR-32/33/34/35/36） | ✅ 已完成（`pb_place` 28 行、663 张、幽灵清零） |
| R | 返工修正 1–6（纠错闭环 DR-16） | ✅ 已完成 |
| 1 | 工程基线与配置骨架 | ✅ 已完成 |
| 2 | SQLite 运行层 + 代码生成器 + 建库 | ✅ 已完成 |
| 3 | 扫描器 | ✅ 已完成 |
| 4 | 缩略图与原图文件服务 | ✅ 已完成 |
| 5 | 人脸引擎 | ✅ 已完成 |
| 6 | 分桶 + 质心 + 匹配决策 | ✅ 已完成 |
| 7 | 聚类与待确认数据 | ✅ 已完成 |
| 8 | 联系人导入 | ✅ 已完成 |
| 9 | 后端 API 全量（含 **contacts CRUD**） | ✅ 已完成（`/docs` 可试调） |
| 10 | 前端骨架 + 双主题 | ✅ 已完成（Node 22 + Vite 7 + Tailwind 3.4；主题三态；构建门禁） |
| 11 | 照片流 + 详情 + 待确认队列 | ✅ 已完成（DR-16 纠错闭环全部落地） |
| 12 | 人物库/详情 + 扫描台 + 设置 + 打磨 | ✅ 已完成（2026-10-07） |

**测试规模**：`code\.venv\Scripts\python.exe -m pytest code\src\test -q` → **1354 passed / 6 skipped / 1 xfailed**（共 1383 条，约 1.5 分钟）。

> ⚠️ 另有 `test/test_place_name_zh.py` 的 **22 条**（1 failed + 21 errors）在**本机**红，原因是环境**缺 `shapely`**
> （该模块的 `point-in-polygon` 拿不到中文名 ⇒ `nameZh` 恒为空），与业务代码无关；
> `pip install shapely` 后即可全绿。

> ⚠️ **R2唯一还欠的东西**：代码已落地且有 36 条用例兜着，但三项报告没出 ——
> `auditBuckets()` 改前/改后完整对照、`verify_bucket_gain.py` 的生产库 FR 实测、
> 全库重算质心前后行数变化。它们是 **M2「识别复现 S0」** 的前提
> （桶口径说了算，不能只凭「代码写完了」就认为 S0 的结论能在生产库复现）。

---

## 四、目录结构

```
photo-browser/
├── README.md                    本文件
├── start.cmd                    ★ 启动器（体检 + 起服务，**日常只用它**）
├── requirements.txt             后端依赖
├── .gitignore
├── LICENSE                      MIT
│
├── plan/                        设计与计划文档（唯一权威）
│   ├── 开发计划.md               ★ 执行总纲：架构 / 数据流 / 12 步路线图 / 决策记录 DR-1~DR-29
│   ├── step-prompts.md          ★ 分步提示语（12 条）+ 执行后的偏差记录
│   ├── 数据库设计.md             ★ 10 张表定义（pb_*.txt 的说明）+ 三层结构 + 索引清单
│   ├── 照片管理方案_开源调研与自研设计.md   上游主方案（v3）
│   ├── MVP_plan.md              S0–S7 阶段划分（做什么）
│   └── UI/photo-browser UI 设计.md         信息架构 + 8 页面 + 双主题 Token
│
└── code/
    ├── requirements → 见根目录 requirements.txt
    ├── src/
    │   ├── main/                程序入口（app.py FastAPI / cli.py 命令行）
    │   ├── api/                 路由层（dto / scan / browse / review / contacts / photoAction / settings / static）
    │   ├── processor/place/     地点字典（pb_place 的 rebuild 与查询）
    │   ├── processor/           业务编排（scanner / media / contact / review）
    │   ├── engine/              计算引擎（face / match / cluster）
    │   ├── schedule/            扫描任务调度与状态流转
    │   ├── common/              sqliteHandle / sqliteCommon(生成) / miscCommon / paths
    │   ├── config/              basicSettings / sqliteSettings / local_settings(**不入库**)
    │   ├── database/            pb_*.txt（唯一数据源）/ sqliteCodeGenerator / queryCommon / auto_generated
    │   ├── tools/
    │   │   ├── serve.py       ★ 启动器主体（体检 + 起服务，start.cmd 转调它）
    │   │   ├── build_db.py       建库（幂等，老库升级跑它补建缺的表）
    │   │   ├── scan_cli.py       扫描（--root 指定原图目录）
    │   │   ├── gen_thumbs.py     缩略图补建
    │   │   ├── backup.py         备份/恢复/列清单（**必须停服务**）
    │   │   ├── import_contacts.py  联系人导入（CSV / vCard）
    │   │   ├── run_faces.py / run_match.py / rebucket_cli.py / cluster_cli.py
    │   │   └── backtest_s0.py / verify_bucket_gain.py   S0 复现与验证
    │   └── test/                单元测试（1237 条）
    ├── webserver/               前端（Vue3 + Vite + Pinia + Tailwind + Element Plus）
    │   ├── dist/                构建产物（不入库；由后端挂在 8765，自带 SPA 回退）
    │   └── scripts/             构建门禁（样式覆盖顺序检查，postbuild 自动跑）
    └── .venv/                   Python 虚拟环境（不入库）
```

---

## 五、照片库布局（运行期数据，不入库）

**根目录由配置决定**：`code/src/config/local_settings.py` 的 `PHOTO_ROOT`，**默认 `d:\PhotoLib`**。

```
d:\PhotoLib\
├── photo\        原图 —— 【绝对只读】，应用绝不写入
├── thumb\        生成物 —— 缩略图 + 人脸裁剪图（可随时重建）
│   ├── thumbs\<fileHash[:2]>\<fileHash>_<size>.webp     200/400/800 宽的 WebP
│   └── faces\<faceCode[:2]>\<faceCode>.jpg              160px 人脸裁剪图
└── db\           应用状态
    ├── photolib.db (+ -wal / -shm)     SQLite 主库
    └── imports\ / exports\             导入原件归档 / 导出产物
```

**三条硬约束**：

1. `photo\` **绝对只读** —— 不写、不删、不改名。
2. 缩略图**不进 `photo\`**（保持原图区干净，扫描器无需排除目录）。
3. 缩略图**不入库** —— `pb_photo` 没有 `thumbPath` 字段，路径由 `fileHash` + size **推导**。

**备份** = 停服务 → 拷贝 `db\` + `thumb\`。**迁移** = 拷贝三个目录 → 改 `PHOTO_ROOT` → 启动。

---

## 六、数据库要点

- **10 张表**：`pb_family` / `pb_person` / `pb_person_category` / **`pb_place`**（地点字典，步骤 9 新增） / `pb_photo` / `pb_face` / `pb_person_centroid` / `pb_photo_person` / `pb_scan_job` / **`pb_review_log`**（纠错审计与撤销依据）。
- **唯一数据源**：`code/src/database/pb_*.txt`（单字段一行）→ `sqliteCodeGenerator.py` → `common/sqliteCommon.py`。**业务层禁止裸 SQL**。
  > ⚠️ **加/改表要同步 7 处**（`TABLE_ORDER` / `TABLE_CN` / `INDEX_SPEC` / `EXPECTED_INDEX_NAMES` / `CONFLICT_COLUMNS` / `QUERY_FILTER_FIELDS` / `ORDER_FIELDS`）**和** `plan/数据库设计.md` 的 §四、§五，改完**必须 diff 重生成的产物**：生成器在表元数据过期时会整个重写 `#common` 区段，不 diff 就无法确认它有没有顺带改到别的表（步骤 9 加 `pb_place` 时实测 +325/-3 行，其中 12 个差异块逐一核对过）。
  > **老库升级**：跑一次 `tools\build_db.py`（幂等，**只建缺的表**）。步骤 9 实测 `created=['pb_place']`、其余 9 张 `existed`、零丢失。
- **不用 ORM**：原生 `sqlite3` 读写双连接 + PRAGMA（`journal_mode=WAL` / `foreign_keys=ON` / `busy_timeout=5000` …）。
- **主键**：`recID INT AUTO_INCREMENT`（SQLite 侧 `INTEGER PRIMARY KEY AUTOINCREMENT`），不用 BIGINT。
- **规范**：业务幂等键 `xxxCode UNIQUE`；关联用业务编码（**不建物理外键**）；软删 `delFlag`；尾部固定 7 字段（label/memo/regID/regYMDHMS/modifyID/modifyYMDHMS/delFlag）。
- **单写入者**：SQLite 硬约束，子进程只做 CPU 计算，主进程单线程批量写库。
- **每线程一个读连接**：`sqliteHandle` 的读路径是「每条语句新建游标 + **本线程自己的只读连接**」（挂在 `threading.local()`）。**这不是优化，是正确性要求**，而且这里叠着**两个** bug：
  1. **共享游标**：`A.execute -> B.execute -> A.fetch` 会**读到 B 的结果集**；并发 `fetchall`/`execute` 更是**进程级 access violation**（Windows 直接杀进程，exit 0xC0000005）。
  2. **共享连接上的预处理语句缓存**（CPython `sqlite3` 是 per-connection 的）：同一连接上**文本相同的 SQL 复用同一个 `sqlite3_stmt`**，后 execute 的线程会 `sqlite3_reset` 掉它，**把先 execute 的那个待取的行丢掉** —— `fetchValue()` 静默返回 None，`lastErr` 为空。实测：6 线程跑同一段 SQL 文本**第 1 次就复现**；每线程文本加几个空格 -> 60 次零失败。

  ⇒ 只有「每线程一个**连接**」才治本。**代价已核算**：`cache_size=-64000`（≈64MB）是按一条主连接定的，几十条线程连接会吃上 GB，所以每线程读连接单独用 **8MB**（`THREAD_READ_CACHE_SIZE`）。实测**性能反而更好**（少了跨线程锁竞争）：10 万张首屏 `/api/timeline` p50 **91ms -> 56ms**。
  **写路径不开每线程连接**：每条写语句都在锁里一次做完（bind + step 到底，不迭代取行），不存在"待取的行被 reset"，且写受「单写入者」硬约束保护。
  ⚠️ 单线程下新旧行为逐位相同，**所以单线程测试全绿证明不了并发正确** —— 见 `src/test/test_sqlite_handle_concurrency.py`（6 个用例，且验证过"改回共享连接后 4/4 变红"）。
- **查询出口**：生成层 `sqliteCommon.query_pb_*` 只能按「主键 + 4 个业务码等值 + `IS NULL`」过滤，**表达不了区间比较 / 分组聚合 / `COUNT(DISTINCT ...)`**。这些走 `database/queryCommon.py`（手写、**只读**）：它只允许 `SELECT`/`WITH`、值一律 `%s` 占位、表名与列名对生成层白名单校验。**api 层与 processor 层没有任何一处裸 SQL**。
- **人脸四态**（由 `personCode` / `isConfirmed` / `isStranger` 推导，无冗余字段）：

  | 状态 | 条件 | 进「待确认」 | 进「我不同意」 |
  |---|---|---|---|
  | 未归属 | `personCode IS NULL AND isStranger=0` | ✅ | — |
  | 自动归属（未人工确认） | `personCode NOT NULL AND isConfirmed=0 AND isStranger=0` | — | ✅ |
  | 人工确认 | `isConfirmed=1` | — | — |
  | 陌生人 | `isStranger=1` | — | — |

详见 `plan/数据库设计.md`。

---

## 七、界面主题

**浅色淡雅 + 跟随系统**，另提供手动三态开关（浅色 / 深色 / 跟随系统），偏好写入 localStorage。

- 浅色（默认）：底 `#F7F8FA`、卡片 `#FFFFFF`、边框 `#E8EBF0`、主色 `#5B8DEF`
- 深色：底 `#14161A`、卡片 `#1E2128`、边框 `#2B2F38`、主色 `#7BA6F5`
- 语义色双套（成功/警告/危险/信息）
- 状态识别一律「颜色 + 图标 + 文字」三重编码
- **照片区不叠加任何滤镜**

> ⚠️ 这推翻了原 UI 稿 v0.1 的「暗色单一主题」。

**对比度实测（步骤 10，58 项全算）**：文字色与徽标、描边、进度条在两套主题下均达 WCAG AA。三处要记住的规则：

1. **警告色有两个**：描边/圆点/进度条用 `--pb-warning-line`（3.4–3.6:1），浅色 `--pb-warning` 只能当**徽标底色**（当描边只有 2.16:1）。
2. **深色主色按钮是「浅蓝底 + 深墨字」**（7.43:1），不是白字（白字只有 2.44:1）。
3. ⚠️ **浅色主色按钮白字 = 3.23:1，低于 AA 的 4.5** —— 品牌色 `#5B8DEF` 与 AA 冲突，**已知取舍：保留品牌色**。要改成 AA 版（`#3E63C8`，5.49:1）只需动 `element-theme.css` 的 `--el-color-primary` 一行。
4. 弱文本由 UI 稿的 `#8A93A6` 加深为 `#656D7E`（原值只有 3.09:1）。

⚠️ **改主题变量前先知道一条硬约束**：`element-theme.css` / `main.css` 必须排在 `src/main.js` 里 `App.vue` 的 import **之后**。顺序错了**不会报错**，只会让覆盖悄悄失效（界面变回 EP 蓝）。`npm run build` 结束时会自动跑 `scripts/check-style-order.mjs` 拦住这种情况。

---

## 八、关键业务流程

1. **扫描限流**：一次扫描处理到 `batchSize`（**默认 100**）张即 `PAUSED` 停止，等待指示后**断点续扫**（`lastCursor`）。
2. **待确认队列**：相似度落在灰区（`T_low`–`T_high`）的人脸进「待确认队列」，由人工确认归属，**不静默归类**。口径 = `personCode IS NULL AND isStranger=0`。
3. **纠错闭环（浏览时发现认错了）**：人脸框悬停「✗ 不是他」→ 改判到别人 / 置为未知 / 新建人物 / 标记陌生人，**一次点击可达**。自动归属但未经确认的脸进「我不同意」列表，支持整张照片一键否决。合并/拆分写 `pb_review_log`，**可撤销**。
4. **年龄分桶**：**分桶是刚需**（S0 实测 FR 32.75% → ~19%，降幅 42%）。策略：自适应分桶（0–18 岁 3 年 / 18 岁以上 10 年），相邻三桶取 **max**。
5. **质心防污染**：质心**只用人工确认样本**计算；某桶确认样本 <3 时退到 `ALL` 兜底桶（不分桶）；总确认样本 <3 则不参与自动匹配。否则误认样本会拉偏质心 → **越错越错**。
6. **年份识别**：`EXIF` → `文件名`（含 `mmexport*` 毫秒时间戳）→ `mtime`；`mtime` 不可靠；截图归 `UNK`。
7. **原图只读**：所有操作只动数据库与 `thumb\`，不改写/删除原图。

---

## 九、本机环境事实（已验证）

| 项 | 值 |
|---|---|
| 开发主机 | Windows，项目在 `d:\home\lianyi\git\photo-browser` |
| Python | 托管解释器 `C:\Users\NINGMEI\.workbuddy\binaries\python\versions\3.13.12\python.exe`（3.13） |
| venv | `code\.venv` |
| pip 源 | **必须用官方** `https://pypi.org/simple`（清华镜像在本机失效，返回 `no versions`） |
| 人脸模型 | InsightFace `buffalo_l`（S0 实测 0.42–0.50s/张，CPU 可接受） |
| onnxruntime | **CPU 版**，不要 `onnxruntime-gpu` |
| Node.js / npm | **v22.23.3 / 10.9.9**（已核实，步骤 10）。⚠️ **不能低于 22**：EP 的传递依赖 `@vueuse/*` 要求 `>=22`，Vite 7 要求 `^20.19 \|\| >=22.12` |
| `httpx` | **测试必需**，`requirements.txt` 已声明（步骤 9 加）。它是 `fastapi.testclient.TestClient` 的底层传输 —— 不装它 `from fastapi.testclient import TestClient` 直接 `ImportError`，而 starlette 的报错只说「httpx 没装」，不说是干什么用的，很容易卡在这 |

### 怎么跑（开发者视角；日常使用见 §二）

```powershell
# 装依赖（venv 在仓库内，不放照片库）
code\.venv\Scripts\pip install -i https://pypi.org/simple -r requirements.txt
#   ⚠️ 装完必须清掉 opencv-python 只留 headless（见 requirements.txt 末尾）

# 全量测试（约 75s，1200+ 用例）
cd code
.\.venv\Scripts\python.exe -m pytest src/test -q
#   ⚠️ **用 .venv 的解释器跑**：直接敲 `python` 会解析到 WindowsApps 的基础解释器
#      （3.13.14，site-packages 在用户目录），它和 .venv 的依赖并不相同 ——
#      本机实测就因为这个让 vobject 相关的用例假红了一次。

#   ⚠️ **已知环境坑（不是本项目的 bug）**：在 CodeBuddy/VS Code 里跑测试时，
#      进程**偶尔会以退出码 1 结束、且打不出最后那行「N passed」摘要**
#      —— 但进度行上全是 `.`（全部通过）。
#      原因：IDE 注入的 `sitecustomize.py` 安全删除守卫 patch 了 `shutil.rmtree`，
#      pytest 收尾清理 `tmp_path` 时删的文件数累计超过阈值（500），
#      守卫直接 `raise SystemExit(1)`（traceback 落在
#      `.../genie/out/vendor/shim/sitecustomize.py`，与项目代码无关）。
#      **权威判据用 junitxml，别只看退出码**：
#        .\.venv\Scripts\python.exe -m pytest src/test -q --junitxml=.jr.xml
#        # 看 failures/errors 是否为 0

# 启动服务 —— 或直接用根目录的 .\start.cmd（会先做启动体检，推荐）
#   ⚠️ 工作目录必须是 code\src，解释器必须是 .venv（理由见 §二）
cd code\src
..\.venv\Scripts\python.exe -m main.app --host 127.0.0.1 --port 8765
#   打开 http://127.0.0.1:8765 逐个页面点；接口清单在 /docs
```

### 前端怎么跑

```powershell
cd code\webserver
npm install                 # Node 必须 >= 22（见上表）

# 开发：dev server 在 5173，接口指向 http://127.0.0.1:8765（.env.development）
npm run dev

# 生产构建 → dist/；**结束时会自动跑样式覆盖顺序门禁**（不通过则构建失败）
npm run build

# 本地预览产物（前端自带 SPA 回退，深链接刷新不会 404）
npm run preview
```

- **端口只有一个**：接口地址写死在 `.env.development` 的 `VITE_API_BASE=http://127.0.0.1:8765`，
  与后端 `SERVER_PORT` 必须一致。生产构建**不需要** `.env.production` ——
  变量缺失时自动回落相对路径 `/api`，由后端同源提供。
- **`dist/` 由后端一起提供**：`main/app.py` 挂 `code/webserver/dist`，并自带 **SPA history 回退**
  （深链接 `/photos/P-000231` 直接刷新也回 `index.html`，不会 404）。所以改完前端要重新 `npm run build`。
- ⚠️ `npm run build` 末尾的门禁会检查「EP 的 `--el-*` 默认值有没有赢过我们的覆盖」。
  这类问题**不报错、只表现为改了主题变量不生效**，所以必须在构建期拦住。
  单独跑：`npm run check:styles`。

### 已知风险

1. `insightface` 在 Python 3.13 可能编译失败（Cython）→ 降到 **Python 3.12** 重建 venv。
2. 若仍失败 → 改用裸 `onnxruntime` 加载 SCRFD + ArcFace（零编译，多写约 80 行）。
3. `reverse_geocoder` 为**可选**依赖，缺失时地点字段留空并降级，不报错。
4. ⚠️ **重装依赖必须带官方源**：默认镜像里**查不到 `vobject`**（`from versions: none`），
   缺它时 `test_contact_import.py` 有一条用例会红。装法：
   `.\.venv\Scripts\python.exe -m pip install -i https://pypi.org/simple -r requirements.txt`

---

## 十、S0 结论（已通过）

| 指标 | 结果 |
|---|---|
| False Accept | **0** ✅ |
| False Reject | 32.75% → **分桶后 ~19%**（降幅 42%） |
| 阈值 | 起点 `T_high=0.55` / `T_low=0.35`（以实测为准，别信默认 0.65） |
| 单张耗时 | 0.42–0.50s（CPU） |

诊断脚本：`C:\Users\NINGMEI\WorkBuddy\selfDevelop\photoapp\tools\`（`diagnose_s0.py`、`bucket_benefit.py`、`audit_years.py`、`make_review_sheets.py` 等）。

---

## 十一、里程碑

| 检查点 | 位置 | 通过条件 | 状态 |
|---|---|---|---|
| M1 | 步骤 3 | 3 万张扫描幂等、去重准确、断点续扫可用 | ✅ 已过 |
| M2 | 步骤 7 | 识别准确率复现 S0，人工确认闭环生效 | ⚠️ 代码与队列已实测，**「复现 S0」的报告仍欠**（R2 的验收证据，见 `plan/step-prompts.md` 提醒） |
| M3 | 步骤 9 | API 全部可用，扫描可后台跑 | ✅ 已过 |
| **M4** | **步骤 12** | **自己真正用起来 —— 把日常浏览照片的习惯切过来用一周** | 🟡 **代码已交付**（步骤 12 完成 2026-10-07）；剩下的是「用一周」这件事 |

> M4 是最重要的验收点。若用了三天还想打开 Windows 资源管理器看照片，说明产品逻辑有问题，必须回头改。

### 备份与恢复（**必须停服务**）

```powershell
# ⚠️ 先停掉 photo-browser 服务（Ctrl+C）。服务开着时拷走的是 SQLite 的 WAL 中间态——
#    备份会「成功」、零报错，但恢复后少最近几分钟的确认记录（原理见 plan/开发计划.md DR-24）
# ⚠️ 用 .venv 的解释器，别用裸 python（理由见 §二）

code\.venv\Scripts\python.exe code\src\tools\backup.py backup                            # 拷贝 db\ + thumb\ 到 <PHOTO_ROOT>\backup\pb_<时间戳>\
code\.venv\Scripts\python.exe code\src\tools\backup.py list                              # 列已有备份
code\.venv\Scripts\python.exe code\src\tools\backup.py restore --code pb_20261007153012  # 恢复（会先自动另存现状）
```

- **不备份 `photo\`**（原图只读且不会坏）—— 所以「恢复」= 让索引回到某个时刻，**不找回删掉的照片**。
- 设置页只有**只读清单 + 可照抄的命令**，**故意没有备份按钮**（理由同上）。

---

## 十二、许可

MIT
