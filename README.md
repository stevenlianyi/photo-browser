# photo-browser

> 本地个人/家庭**照片浏览与人脸归类**工具 —— 把磁盘上散落的照片，变成可按人浏览、可跨年代追溯的私人相册。
> 存储：**SQLite（单文件、零安装）** ｜ 后端：Python + FastAPI + 原生 sqlite3 ｜ 前端：Vue3 + Vite + Tailwind + Element Plus

**当前状态**：🚧 **编码中**（按 `plan/开发计划.md` 的 12 步推进）。S0 准确度验证**已通过**。

---

## 一、这是什么

一个本地优先的照片浏览 + 人脸归类系统，对标 Picasa 3 但更轻。核心约束：

- **纯浏览、不编辑**原图（原文件只读，绝不写/删/改名）。
- **人脸识别 + 归类**是核心，且要求跨年龄准确度。
- **人员数据来自通讯录**（CSV / vCard 导入）—— 这是所有开源方案都满足不了的部分，必须自研。
- 规模：**约 3 万张**照片；单机；**无需 GPU、无需装数据库服务**。

---

## 二、怎么开始开发

### 第 1 步：复制提示语，新开对话

打开 **`plan/step-prompts.md`**，把「步骤 1」那一整段提示语复制，**新开一个对话**粘贴执行。
每步验收通过后，再复制下一步的提示语开新对话。

> ⚠️ 不要把 12 步一起粘进同一个对话 —— 上下文过长会导致约束被忽略。

### 当前进度

| 步骤 | 主题 | 状态 |
|---|---|---|
| **R2** | **分桶口径修复**（自适应分桶从未生效 · DR-20/21/22） | ⬜ **待做，最优先** |
| R | 返工修正 1–6（纠错闭环 DR-16） | ✅ 已完成 |
| 1 | 工程基线与配置骨架 | ✅ 已完成 |
| 2 | SQLite 运行层 + 代码生成器 + 建库 | ✅ 已完成 |
| 3 | 扫描器 | ✅ 已完成 |
| 4 | 缩略图与原图文件服务 | ✅ 已完成 |
| 5 | 人脸引擎 | ✅ 已完成 |
| 6 | 分桶 + 质心 + 匹配决策 | ✅ 已完成 |
| 7 | 聚类与待确认数据 | ✅ 已完成 |
| 8 | 联系人导入 | ✅ 已完成 |
| 9 | 后端 API 全量（含 **contacts CRUD**） | ✅ 已完成（**41 个端点**；`/docs` 可试调；118 个新增用例） |
| 10 | 前端骨架 + 双主题 | ✅ 已完成（Node 22 + Vite 7 + Tailwind 3.4；主题三态；EP 按需引入；构建门禁） |
| 11 | 照片流 + 详情 + 待确认队列 | ⬜ 未开始 |
| 12 | 人物库/详情 + 扫描台 + 设置 + 打磨（含 **PersonForm / 停用**） | ⬜ 未开始 |

> ⚠️ **R2 为什么最优先**：`pb_face.shotBucket` 一直是从未重写过的**等宽 5 年占位桶**，
> 自适应分桶（0–18 岁 3 年 / 18+ 10 年）在生产库**从未生效** ——
> S0 的「分桶让 FR 32.75% → 19%」一直在跑**对照组**，M2「识别复现 S0」因此卡住。

---

## 三、目录结构

```
photo-browser/
├── README.md                    本文件
├── requirements.txt             后端依赖
├── .gitignore
├── LICENSE                      MIT
│
├── plan/                        设计与计划文档（唯一权威）
│   ├── 开发计划.md               ★ 执行总纲：架构 / 数据流 / 12 步路线图 / 决策记录 DR-1~DR-9
│   ├── step-prompts.md          ★ 分步提示语（12 条，可直接复制）
│   ├── 数据库设计.md             ★ 9 张表定义（pb_*.txt 的说明）+ 三层结构 + 索引清单
│   ├── 照片管理方案_开源调研与自研设计.md   上游主方案（v3）
│   ├── MVP_plan.md              S0–S7 阶段划分（做什么）
│   └── UI/photo-browser UI 设计.md         信息架构 + 8 页面 + 双主题 Token
│
└── code/
    ├── requirements → 见根目录 requirements.txt
    ├── src/
    │   ├── main/                程序入口（app.py FastAPI / cli.py 命令行）
    │   ├── api/                 路由层（dto / scan / browse / review / contacts / static）
    │   ├── processor/place/     地点字典（pb_place 的rebuild 与查询，步骤 9）
    │   ├── processor/           业务编排（scanner / media / contact / review）
    │   ├── engine/              计算引擎（face / match / cluster）
    │   ├── schedule/            扫描任务调度与状态流转
    │   ├── common/              sqliteHandle / sqliteCommon(生成) / miscCommon / paths
    │   ├── config/              basicSettings / sqliteSettings / local_settings(不入库)
    │   ├── database/            pb_*.txt（唯一数据源）/ sqliteCodeGenerator / auto_generated
    │   ├── tools/               build_db / gen_thumbs / backtest_s0 / backup
    │   └── test/                单元测试
    ├── webserver/               前端（Vue3 + Vite + Pinia + Tailwind + Element Plus）
    │   ├── dist/                构建产物（不入库；由后端挂在 8765，自带 SPA 回退）
    │   └── scripts/             构建门禁（样式覆盖顺序检查，postbuild 自动跑）
    └── .venv/                   Python 虚拟环境（不入库）
```

---

## 四、照片库布局（运行期数据，不入库）

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

## 五、数据库要点

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

## 六、界面主题

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

## 七、关键业务流程

1. **扫描限流**：一次扫描处理到 `batchSize`（**默认 100**）张即 `PAUSED` 停止，等待指示后**断点续扫**（`lastCursor`）。
2. **待确认队列**：相似度落在灰区（`T_low`–`T_high`）的人脸进「待确认队列」，由人工确认归属，**不静默归类**。口径 = `personCode IS NULL AND isStranger=0`。
3. **纠错闭环（浏览时发现认错了）**：人脸框悬停「✗ 不是他」→ 改判到别人 / 置为未知 / 新建人物 / 标记陌生人，**一次点击可达**。自动归属但未经确认的脸进「我不同意」列表，支持整张照片一键否决。合并/拆分写 `pb_review_log`，**可撤销**。
4. **年龄分桶**：**分桶是刚需**（S0 实测 FR 32.75% → ~19%，降幅 42%）。策略：自适应分桶（0–18 岁 3 年 / 18 岁以上 10 年），相邻三桶取 **max**。
5. **质心防污染**：质心**只用人工确认样本**计算；某桶确认样本 <3 时退到 `ALL` 兜底桶（不分桶）；总确认样本 <3 则不参与自动匹配。否则误认样本会拉偏质心 → **越错越错**。
6. **年份识别**：`EXIF` → `文件名`（含 `mmexport*` 毫秒时间戳）→ `mtime`；`mtime` 不可靠；截图归 `UNK`。
7. **原图只读**：所有操作只动数据库与 `thumb\`，不改写/删除原图。

---

## 八、本机环境事实（已验证）

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

### 怎么跑

```powershell
# 装依赖（venv 在仓库内，不放照片库）
code\.venv\Scripts\pip install -i https://pypi.org/simple -r requirements.txt
#   ⚠️ 装完必须清掉 opencv-python 只留 headless（见 requirements.txt 末尾）

# 全量测试（约 60s，1100+ 用例）
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

# 启动服务（**只绑回环**，非回环地址会拒绝启动）
.\.venv\Scripts\python.exe -m main.app --host 127.0.0.1 --port 8765
#   打开 http://127.0.0.1:8765/docs 逐条试调
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

## 九、S0 结论（已通过）

| 指标 | 结果 |
|---|---|
| False Accept | **0** ✅ |
| False Reject | 32.75% → **分桶后 ~19%**（降幅 42%） |
| 阈值 | 起点 `T_high=0.55` / `T_low=0.35`（以实测为准，别信默认 0.65） |
| 单张耗时 | 0.42–0.50s（CPU） |

诊断脚本：`C:\Users\NINGMEI\WorkBuddy\selfDevelop\photoapp\tools\`（`diagnose_s0.py`、`bucket_benefit.py`、`audit_years.py`、`make_review_sheets.py` 等）。

---

## 十、里程碑

| 检查点 | 位置 | 通过条件 |
|---|---|---|
| M1 | 步骤 3 | 3 万张扫描幂等、去重准确、断点续扫可用 |
| M2 | 步骤 7 | 识别准确率复现 S0，人工确认闭环生效 |
| M3 | 步骤 9 | API 全部可用，扫描可后台跑 |
| **M4** | **步骤 12** | **自己真正用起来 —— 把日常浏览照片的习惯切过来用一周** |

> M4 是最重要的验收点。若用了三天还想打开 Windows 资源管理器看照片，说明产品逻辑有问题，必须回头改。

---

## 十一、许可

MIT
