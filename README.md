# photo-browser 使用手册

> 本地个人 / 家庭**照片浏览与人脸归类**工具 —— 把磁盘上散落的照片，变成可按人浏览、可跨年代追溯的私人相册。
> 存储：**SQLite（单文件、零安装）** ｜ 后端：Python + FastAPI + 原生 sqlite3 ｜ 前端：Vue3 + Vite + Tailwind + Element Plus

---

## 一、这是什么

一个**本地优先**的照片浏览 + 人脸归类系统，对标 Picasa 3 但更轻。把磁盘上散落的照片，变成可按人浏览、可跨年代追溯的私人相册。

- **纯浏览、不编辑**原图（原文件只读，绝不写 / 删 / 改名）。
- **人脸识别 + 归类**是核心优势：按联系人**出生年月**把每个人按**年代分档**识别，跨年龄也能保持**高准确率**（实测误拒率从 32.75% 降到约 19%）。
- **人员数据来自通讯录**（CSV / vCard 导入）—— 这是多数开源方案满足不了的部分。
- 规模约 **10 万张**照片；单机；**无需 GPU、无需装数据库服务**。

| 项 | 说明 |
|---|---|
| 存储 | SQLite（单文件、零安装） |
| 后端 | Python + FastAPI + 原生 sqlite3 |
| 前端 | Vue3 + Vite + Tailwind + Element Plus |

---

## 二、快速开始

### 一句话版（已装好依赖 + 已有照片库时）

在仓库根目录执行：

```powershell
.\start.cmd
```

然后打开 **http://127.0.0.1:8765** 。`Ctrl+C` 停止。也可以**直接双击 `start.cmd`**。

> 启动失败时窗口会停住等你按键 —— 不会一闪而过、让你对着空窗口发呆。

> ⚠️ **为什么是 `.cmd` 不是 `.ps1`**：PowerShell 在执行策略为 `Restricted` / `AllSigned` 时会拒绝运行 `.ps1`。`.cmd` 由 cmd.exe 执行、不受该策略约束，双击也能跑。
> ⚠️ **不要为了跑本项目去改 PowerShell 执行策略**（`Set-ExecutionPolicy`）—— 那是机器 / 用户的安全设置。

### 第一次跑（换机器 / 新克隆仓库）

```powershell
# ① 建 venv（必须用官方源，默认镜像查不到 vobject）
py -3.13 -m venv code\.venv
code\.venv\Scripts\python.exe -m pip install -i https://pypi.org/simple -r requirements.txt
code\.venv\Scripts\python.exe -m pip uninstall -y opencv-python      # 只留 headless

# ② 前端依赖 + 构建（Node 必须 >= 22）
cd code\webserver; npm install; npm run build; cd ..\..

# ③ 本机配置（该文件 .gitignore，不入库）
copy code\src\config\local_settings.py.example code\src\config\local_settings.py
notepad code\src\config\local_settings.py          # 改 PHOTO_ROOT 指向你的照片目录

# ④ 起服务
.\start.cmd
```

### `start.cmd` 参数

| 参数 | 作用 |
|---|---|
| （无） | 起服务，同源提供前端 → 开 **8765** |
| `--dev` | 另起 Vite dev server → 开 **5173**（改前端代码热更时用这个） |
| `--check-only` | 只做体检、不启动。**改配置后先跑这个** |
| `--port 8799` | 换端口（8765 被占时） |
| `--db <路径>` | 换库文件（对着另一个库试调时） |
| `--root <路径>` / `--thumb <路径>` | 覆盖原图根 / 缩略图根（本进程有效，退出即失效） |

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

---

## 三、首次使用须知

1. **照片要先放进图片目录、再扫描才看得到。** 增加照片需要先复制到所选择的图片目录（默认 `d:\photoLib\photo`），然后在「扫描任务」里启动一个新的扫描任务才会被收录。库里 0 张照片时界面照样起得来，只是列表是空的。
2. **「待确认」需要你先人工确认几张脸才有内容。** 自动归属只用人工确认过的样本算质心（防污染），所以刚上手时「我不同意」与自动归属都是空的，**这是正确行为不是故障**。

---

## 四、主要功能与界面

- **照片流 / 时间线**：按拍摄时间浏览全部照片，支持左右翻页（`←` `→`）。
- **人物库**：按人归类照片，显示人物头像，可查看某人跨年代的时间轴。
- **待确认队列**：相似度落在灰区的人脸进入队列，由你确认归属，不静默归类。
- **我不同意**：自动归属但未经确认的脸在此，支持整张照片一键否决。
- **纠错闭环**：浏览时发现认错了，一次点击可改判 / 置未知 / 新建 / 标陌生人，且可撤销。
- **地点维度**：人物 → 地点列表 → 时间线，查看某人「去过的地方」（含境内中文名）。
- **联系人导入**：CSV / vCard 导入人员名单，作为归类依据。
- **扫描台**：后台扫描、断点续扫、限流。
- **双主题**：浅色淡雅 / 深色 / 跟随系统，偏好写入 localStorage。

---

## 五、关键业务流程

1. **扫描限流**：一次扫描处理到 `batchSize`（默认 100）张即 `PAUSED` 停止，等待指示后断点续扫（`lastCursor`）。
2. **待确认队列**：相似度落在灰区（`T_low`–`T_high`）的人脸进队列，由人工确认，不静默归类。
3. **纠错闭环**：人脸框悬停「✗ 不是他」→ 改判到别人 / 置为未知 / 新建人物 / 标记陌生人，一次点击可达。自动归属的脸进「我不同意」，支持整张照片一键否决。合并 / 拆分写审计日志，**可撤销**。
4. **年龄分档**：按联系人出生年月把每个人按年代分档识别（0–18 岁 3 年 / 18 岁以上 10 年），相邻三档取 max。分档是刚需（实测误拒率 32.75% → 约 19%，降幅 42%）。
5. **质心防污染**：质心只用人工确认样本计算；某档确认样本 <3 时退到兜底档；总确认样本 <3 则不参与自动匹配。
6. **年份识别**：EXIF → 文件名（含毫秒时间戳）→ mtime；mtime 不可靠；截图归 UNK。
7. **原图只读**：所有操作只动数据库与 `thumb\`，不改写 / 删除原图。

### 人脸四态

| 状态 | 条件 | 进「待确认」 | 进「我不同意」 |
|---|---|---|---|
| 未归属 | `personCode IS NULL AND isStranger=0` | ✅ | — |
| 自动归属（未人工确认） | `personCode NOT NULL AND isConfirmed=0 AND isStranger=0` | — | ✅ |
| 人工确认 | `isConfirmed=1` | — | — |
| 陌生人 | `isStranger=1` | — | — |

---

## 六、照片库布局

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
2. 缩略图**不进 `photo\`**（保持原图区干净）。
3. 缩略图**不入库** —— 路径由 `fileHash` + size 推导。

**备份** = 停服务 → 拷贝 `db\` + `thumb\`。**迁移** = 拷贝三个目录 → 改 `PHOTO_ROOT` → 启动。

---

## 七、配置与常用工具

### 本机配置 `local_settings.py`

复制示例文件后修改（该文件不入库）：

```powershell
copy code\src\config\local_settings.py.example code\src\config\local_settings.py
notepad code\src\config\local_settings.py
```

其中最重要的是 `PHOTO_ROOT`，指向你的照片目录。

### 常用工具命令（在 `code\src` 下用 venv 解释器）

| 命令 | 用途 |
|---|---|
| `tools\build_db.py` | 建库（幂等，老库升级补建缺的表） |
| `tools\scan_cli.py --root <目录>` | 扫描原图目录（也可在界面「扫描任务」里启动） |
| `tools\gen_thumbs.py` | 缩略图补建 |
| `tools\import_contacts.py` | 联系人导入（CSV / vCard） |
| `tools\backup.py ...` | 备份 / 恢复 / 列清单（须停服务） |

> ⚠️ 所有 Python 命令务必用 `code\.venv\Scripts\python.exe`，裸敲 `python` 会命中 WindowsApps 基础解释器，依赖不同导致行为诡异。

---

## 八、备份与恢复（必须停服务）

```powershell
# ⚠️ 先停掉 photo-browser 服务（Ctrl+C）。服务开着时拷走的是 SQLite 的 WAL 中间态，
#    备份会「成功」、零报错，但恢复后少最近几分钟的确认记录。
# ⚠️ 用 .venv 的解释器，别用裸 python。

code\.venv\Scripts\python.exe code\src\tools\backup.py backup                            # 备份 db\ + thumb\
code\.venv\Scripts\python.exe code\src\tools\backup.py list                              # 列已有备份
code\.venv\Scripts\python.exe code\src\tools\backup.py restore --code pb_20261007153012  # 恢复（先自动另存现状）
```

- **不备份 `photo\`**（原图只读且不会坏）—— 所以「恢复」= 让索引回到某个时刻，**不找回删掉的照片**。
- 设置页只有**只读清单 + 可照抄的命令**，**故意没有备份按钮**。

---

## 九、常见问题（FAQ）

**Q：页面打开了但照片列表是空的？**
照片要先放进图片目录、再在「扫描任务」里启动扫描才会被收录。库为空时界面照样起得来，请用 `.\start.cmd --check-only` 体检。

**Q：「待确认」「我不同意」都是空的？**
这是**正确行为**。自动归属只用人工确认过的样本算质心，刚上手时还没有确认样本，需要先人工确认几张脸。

**Q：双击 `start.cmd` 一闪而过 / 报脚本禁止运行？**
脚本本身是 `.cmd`，不受 PowerShell 执行策略约束。若仍异常，请在仓库根目录的命令行里手动执行 `.\start.cmd` 查看报错。

**Q：端口 8765 被占了？**
换端口：`.\start.cmd --port 8799`。注意：端口被占时，8765 上可能跑着**另一个库**的服务，请在浏览器确认数据是否一致。

**Q：改了前端代码没生效？**
改完前端要重新 `npm run build`；本地热改用 `.\start.cmd --dev`（开 5173）。并确保 `dist/` 已构建（否则根路由只返回 JSON，页面全 404）。

**Q：怎么恢复误删的照片？**
软件**不删除原图**，也不备份 `photo\`。原图删除只能靠你自己的文件备份找回；`backup.py` 只恢复索引（数据库 + 缩略图）。

---

## 十、技术参考（面向开发者 / 排障）

> 以下内容偏内部实现，日常使用无需阅读。完整设计见 `plan/` 目录。

### 技术栈与本机环境事实（已验证）

| 项 | 值 |
|---|---|
| 开发主机 | Windows，项目在 `d:\home\lianyi\git\photo-browser` |
| Python | 3.13（venv 在 `code\.venv`） |
| pip 源 | **必须用官方** `https://pypi.org/simple`（默认镜像查不到 vobject） |
| 人脸模型 | InsightFace `buffalo_l`（CPU 0.42–0.50s/张） |
| onnxruntime | **CPU 版**，不要 `onnxruntime-gpu` |
| Node.js / npm | **v22+**（低于 22 时 EP / Vite 7 依赖不满足） |
| `httpx` | 测试必需，`requirements.txt` 已声明 |

### 数据库要点

- **10 张表**：`pb_family` / `pb_person` / `pb_person_category` / `pb_place` / `pb_photo` / `pb_face` / `pb_person_centroid` / `pb_photo_person` / `pb_scan_job` / `pb_review_log`。
- **唯一数据源**：`code/src/database/pb_*.txt` → `sqliteCodeGenerator.py` → `common/sqliteCommon.py`。**业务层禁止裸 SQL**。
- **不用 ORM**：原生 `sqlite3` 读写双连接 + PRAGMA（`journal_mode=WAL` / `foreign_keys=ON` / `busy_timeout=5000` …）。
- **单写入者**：SQLite 硬约束，子进程只做 CPU 计算，主进程单线程批量写库；每线程一个只读连接（正确性要求，非优化）。
- 老库升级：跑一次 `tools\build_db.py`（幂等，只建缺的表）。

### 启用脚本为什么必要

照手敲命令有六个坑（其中三个静默）：工作目录必须是 `code\src`、必须用 `code\.venv` 解释器、`local_settings.py` 缺失回落默认、缺 `dist/index.html` 退化为只返 JSON、端口被占可能跑着另一个库、`.ps1` 可能被策略拦。`serve.py` 逐条查完再启，查不过就不给启动。

### S0 结论（已通过）

| 指标 | 结果 |
|---|---|
| False Accept | **0** ✅ |
| 误拒率 | 32.75% → **分档后约 19%**（降幅 42%） |
| 阈值 | 起点 `T_high=0.55` / `T_low=0.35`（以实测为准） |
| 单张耗时 | 0.42–0.50s（CPU） |

### 测试

```powershell
cd code
.\.venv\Scripts\python.exe -m pytest src/test -q --junitxml=.jr.xml   # 看 failures/errors 是否为 0
```

> ⚠️ IDE 注入的 `sitecustomize.py` 安全删除守卫可能让 pytest 以退出码 1 结束却打不出摘要（与项目代码无关），权威判据用 junitxml。

---

## 十一、许可

MIT
