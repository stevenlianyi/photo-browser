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
| 1 | 工程基线与配置骨架 | ⬜ 未开始 |
| 2 | SQLite 运行层 + 代码生成器 + 建库 | ⬜ 未开始 |
| 3 | 扫描器 | ⬜ 未开始 |
| 4 | 缩略图与原图文件服务 | ⬜ 未开始 |
| 5 | 人脸引擎 | ⬜ 未开始 |
| 6 | 分桶 + 质心 + 匹配决策 | ⬜ 未开始 |
| 7 | 聚类与待确认数据 | ⬜ 未开始 |
| 8 | 联系人导入 | ⬜ 未开始 |
| 9 | 后端 API 全量 | ⬜ 未开始 |
| 10 | 前端骨架 + 双主题 | ⬜ 未开始 |
| 11 | 照片流 + 详情 + 待确认队列 | ⬜ 未开始 |
| 12 | 人物库/详情 + 扫描台 + 设置 + 打磨 | ⬜ 未开始 |

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
│   ├── 数据库设计.md             ★ 8 张表定义（pb_*.txt 的说明）+ 三层结构 + 索引清单
│   ├── 照片管理方案_开源调研与自研设计.md   上游主方案（v3）
│   ├── MVP_plan.md              S0–S7 阶段划分（做什么）
│   └── UI/photo-browser UI 设计.md         信息架构 + 8 页面 + 双主题 Token
│
└── code/
    ├── requirements → 见根目录 requirements.txt
    ├── src/
    │   ├── main/                程序入口（app.py FastAPI / cli.py 命令行）
    │   ├── api/                 路由层（scan / browse / review / contacts / static）
    │   ├── processor/           业务编排（scanner / media / contact / review）
    │   ├── engine/              计算引擎（face / match / cluster）
    │   ├── schedule/            扫描任务调度与状态流转
    │   ├── common/              sqliteHandle / sqliteCommon(生成) / miscCommon / paths
    │   ├── config/              basicSettings / sqliteSettings / local_settings(不入库)
    │   ├── database/            pb_*.txt（唯一数据源）/ sqliteCodeGenerator / auto_generated
    │   ├── tools/               build_db / gen_thumbs / backtest_s0 / backup
    │   └── test/                单元测试
    ├── webserver/               前端（Vue3 + Vite + Pinia + Tailwind + Element Plus）
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

- **8 张表**：`pb_family` / `pb_person` / `pb_person_category` / `pb_photo` / `pb_face` / `pb_person_centroid` / `pb_photo_person` / `pb_scan_job`。
- **唯一数据源**：`code/src/database/pb_*.txt`（单字段一行）→ `sqliteCodeGenerator.py` → `common/sqliteCommon.py`。**业务层禁止裸 SQL**。
- **不用 ORM**：原生 `sqlite3` 读写双连接 + PRAGMA（`journal_mode=WAL` / `foreign_keys=ON` / `busy_timeout=5000` …）。
- **主键**：`recID INT AUTO_INCREMENT`（SQLite 侧 `INTEGER PRIMARY KEY AUTOINCREMENT`），不用 BIGINT。
- **规范**：业务幂等键 `xxxCode UNIQUE`；关联用业务编码（**不建物理外键**）；软删 `delFlag`；尾部固定 7 字段（label/memo/regID/regYMDHMS/modifyID/modifyYMDHMS/delFlag）。
- **单写入者**：SQLite 硬约束，子进程只做 CPU 计算，主进程单线程批量写库。

详见 `plan/数据库设计.md`。

---

## 六、界面主题

**浅色淡雅 + 跟随系统**，另提供手动三态开关（浅色 / 深色 / 跟随系统），偏好写入 localStorage。

- 浅色（默认）：底 `#F7F8FA`、卡片 `#FFFFFF`、边框 `#E8EBF0`、主色 `#5B8DEF`
- 深色：底 `#14161A`、卡片 `#1E2128`、边框 `#2B2F38`、主色 `#7BA6F5`
- 语义色双套（成功/警告/危险/信息），两套主题下均达 WCAG AA
- 状态识别一律「颜色 + 图标 + 文字」三重编码
- **照片区不叠加任何滤镜**

> ⚠️ 这推翻了原 UI 稿 v0.1 的「暗色单一主题」。

---

## 七、关键业务流程

1. **扫描限流**：一次扫描处理到 `batchSize`（**默认 100**）张即 `PAUSED` 停止，等待指示后**断点续扫**（`lastCursor`）。
2. **人工确认队列**：相似度落在灰区（`T_low`–`T_high`）的人脸进「待确认队列」，由人工确认归属，**不静默归类**。
3. **年龄分桶**：**分桶是刚需**（S0 实测 FR 32.75% → ~19%，降幅 42%）。策略：自适应分桶（0–18 岁 3 年 / 18 岁以上 10 年），相邻三桶取 **max**，每桶 ≥3 样本才启用质心。
4. **年份识别**：`EXIF` → `文件名`（含 `mmexport*` 毫秒时间戳）→ `mtime`；`mtime` 不可靠；截图归 `UNK`。
5. **原图只读**：所有操作只动数据库与 `thumb\`，不改写/删除原图。

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
| Node.js / npm | ⏳ **尚未核实**（步骤 10 首要动作） |

### 已知风险

1. `insightface` 在 Python 3.13 可能编译失败（Cython）→ 降到 **Python 3.12** 重建 venv。
2. 若仍失败 → 改用裸 `onnxruntime` 加载 SCRFD + ArcFace（零编译，多写约 80 行）。
3. `reverse_geocoder` 为**可选**依赖，缺失时地点字段留空并降级，不报错。

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
