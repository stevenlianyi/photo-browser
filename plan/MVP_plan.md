# MVP 实施计划 — 照片浏览与人脸归类系统

> 配套文档：`照片管理方案_开源调研与自研设计.md`（v3 · SQLite）、`数据库设计.md`（表定义）、`开发计划.md`（**执行总纲 · 12 步**）、`step-prompts.md`（分步提示语）
> 技术栈：Python 3.13 + FastAPI + **原生 sqlite3 运行层与代码生成器** + InsightFace + Vue 3
> 规模前提：**3 万 ~ 10 万张照片（按 10 万设计）** · 目录 4–5 级 · 单机 · 无需 GPU · 无需安装数据库

---

## 0. 执行路线说明（先读这段）

本文的 S0–S7 是**阶段划分**（做什么）；实际动手按 **`plan/开发计划.md` 的 12 步**（怎么做），每步单独开一次对话，提示语见 `plan/step-prompts.md`。

| 本文章节 | 对应 12 步 |
|---|---|
| S1 环境 + 骨架 + 数据库 | 步骤 1 工程基线与配置骨架、步骤 2 SQLite 运行层 + 生成器 + 建库 |
| S2 扫描器 | 步骤 3 扫描器 |
| S2（缩略图部分，原写在 S5） | 步骤 4 缩略图与原图文件服务 |
| S3 人脸引擎 + 分桶 + 匹配 | 步骤 5 人脸引擎、步骤 6 分桶质心匹配、步骤 7 聚类与待确认数据 |
| S4 联系人导入 | 步骤 8 联系人导入 |
| S5 后端 API | 步骤 9 后端 API 全量 |
| S6 前端视图 | 步骤 10 前端骨架 + 双主题、步骤 11 照片流/详情/待确认队列 |
| S7 打磨 | 步骤 12 人物库/详情 + 扫描台 + 设置 + 打磨 |

**路径约定变更（已定稿，见 DR-8）**：照片库根目录由 `code/src/config/local_settings.py` 的 `PHOTO_ROOT` 配置，**默认 `d:\PhotoLib`**；`thumb\`（缩略图/人脸裁剪图）与 `db\`（数据库）均从它派生。缩略图**不进 `photo\`**（原图严格只读），见 DR-1。

---

## 阶段总览

| 阶段 | 目标 | 估量 | 前置 |
|------|------|------|------|
| **S0** | **准确度验证** —— 决定项目可行性 | 半天 | 无 |
| S1 | 环境 + 项目骨架 + 数据库 | 1 天 | S0 通过 |
| S2 | 扫描器（遍历 / hash / EXIF / 去重） | 2–3 天 | S1 |
| S3 | 人脸引擎 + 分桶 + 匹配决策 | 3–5 天 | S2 |
| S4 | 联系人导入（CSV / vCard） | 1–2 天 | S1 |
| S5 | 后端 API | 3–4 天 | S2–S4 |
| S6 | 前端视图（时间线 / 人员 / 待确认） | 5–7 天 | S5 |
| S7 | 打磨（家庭 / 地图 / 多人 / 重复照片） | 3–4 天 | S6 |

**S0 不通过就不要往下走。** 准确度不达标的话，后面所有工程都是白干。

> ✅ **S0 已完成**（结论已并入 `README.md`：FA=0，FR 32.75% → 分桶后 ~19%，降幅 42%；单张 0.42–0.50s CPU）。诊断脚本在 `C:\Users\NINGMEI\WorkBuddy\selfDevelop\photoapp\tools\`。当前进入编码阶段。

---

## S0 · 准确度验证（半天）

### 为什么先做这个

人脸识别的**误判率**是这个项目唯一的未知数，其余全是已知工作量。花半天把它测出来，再决定要不要投入后面三周。

### 数据准备（你来做，约 30 分钟）

从照片库里挑 **10 个最常出现的人**，每人选 **10 张跨年代的照片**（尽量覆盖不同年龄段、不同光线、不同角度）：

```
d:\PhotoLib\verify\
├── 张三\  1.jpg ... 10.jpg
├── 李四\  1.jpg ... 10.jpg
└── ...    （共 10 人 × 10 张 = 100 张）
```

> 路径不写死在代码里：脚本通过 `--root` 参数接收验证集目录（步骤 5 的 `backtest_s0.py` 同理）。

### 脚本要做的事

```
1. 对 100 张图跑 SCRFD 检测 + ArcFace 提取 512 维特征
2. 组内配对：每人 10 张 → C(10,2) = 45 对，共 450 对 → 余弦相似度分布
3. 组间配对：任取不同人 → 余弦相似度分布
4. 画两张直方图，找最佳阈值切点（EER 位置）
5. 关键输出：在候选阈值下，有多少「不同的人」被误判为同一人（False Accept）
```

### 验收标准（P0 硬指标）

| 指标 | 门槛 | 说明 |
|------|------|------|
| **False Accept** | **= 0** | 不同的人被判为同一人。**这条必须为 0**——它比漏识别有害得多 |
| False Reject | < 15% | 漏识别可以靠人工确认队列兜底 |
| 最佳阈值落在 | 0.40–0.60 | 若落在极端区间，说明数据集有问题 |
| 单张处理耗时 | < 1 s（CPU） | 决定全量扫描时长 |

### 验收标准的延伸验证（同样半天内做完）

选出阈值后，再测一遍**跨年龄**的表现：把每个人的照片按年代分成两组（如「2005–2010」和「2020–2025」），看两组之间的组内相似度是否明显高于组间。

> 如果跨年代组内相似度已经掉到阈值边缘 —— **说明分桶是刚需**，更进一步可以验证「按年龄自适应分桶」是否真的比「等宽 5 年」更好。这一步的数据直接决定 S3 的桶设计。

### S0 脚本（`tools/verify_accuracy.py`）

> **已抽取为独立可执行文件，见交付包 `tools/verify_accuracy.py`，以该文件为准。**
> 相比下面这版草稿，正式脚本额外做了这些修正：
> ① **中文路径安全读图**（`cv2.imread` 在 Windows 上读不了非 ASCII 路径，而验证集目录名就是人名 → 必须走 `np.fromfile` + `cv2.imdecode`）；
> ② 质量过滤阈值外提为常量，便于按实测调整；
> ③ 单张耗时统计（对齐「< 1s/张」验收项）；
> ④ `FA=0` 前提下自动推荐阈值 + PASS/FAIL 判定；
> ⑤ 「组内 5% 分位 vs 组间 95% 分位」重叠度检测（分桶是否必需的量化证据）；
> ⑥ `--csv` 导出，便于多模型横评留档。
>
> 下面保留草稿版本，仅用于说明算法意图。

```python
"""S0：人脸识别准确度验证。用法：python verify_accuracy.py D:\\PhotoLib\\verify"""
import sys, itertools, pathlib
import numpy as np, cv2
from insightface.app import FaceAnalysis

SIM_THRESHOLD_CANDIDATES = np.arange(0.30, 0.71, 0.01)

def load_engine():
    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))
    return app

def embed_one(app, path):
    img = cv2.imread(str(path))
    if img is None:
        return None
    faces = app.get(img)
    if not faces:
        return None
    # 取面积最大的那张脸，并过滤过小/过侧
    f = max(faces, key=lambda x: (x.bbox[2]-x.bbox[0]) * (x.bbox[3]-x.bbox[1]))
    if abs(getattr(f, "pose", [0, 0, 0])[1]) > 45:
        return None
    v = f.normed_embedding.astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)

def pairwise(embs_a, embs_b):
    if not embs_a or not embs_b:
        return []
    A = np.stack(embs_a)
    B = np.stack(embs_b)
    return list((A @ B.T).ravel())

def main(root):
    root = pathlib.Path(root)
    people = sorted([d for d in root.iterdir() if d.is_dir()])
    if len(people) < 2:
        print("至少需要 2 个人的文件夹"); return
    app = load_engine()

    embs = {}
    for p in people:
        vecs = [v for v in (embed_one(app, f) for f in sorted(p.glob("*.*"))) if v is not None]
        embs[p.name] = vecs
        print(f"{p.name}: {len(vecs)} 张有效人脸")

    intra, inter = [], []
    for name, vecs in embs.items():
        for a, b in itertools.combinations(range(len(vecs)), 2):
            intra.append(float(vecs[a] @ vecs[b]))
    names = list(embs)
    for i, j in itertools.combinations(range(len(names)), 2):
        inter += [float(s) for s in pairwise(embs[names[i]], embs[names[j]])]

    intra, inter = np.array(intra), np.array(inter)
    print(f"\n组内(同一人) {intra.size} 对: 均值 {intra.mean():.3f}  5%分位 {np.percentile(intra,5):.3f}")
    print(f"组间(不同人) {inter.size} 对: 均值 {inter.mean():.3f}  95%分位 {np.percentile(inter,95):.3f}")

    print("\n阈值  误接受(FA)  误拒(FR)   FA率      FR率")
    best = None
    for t in SIM_THRESHOLD_CANDIDATES:
        fa = int((inter >= t).sum()); fr = int((intra < t).sum())
        far, frr = fa / max(inter.size, 1), fr / max(intra.size, 1)
        print(f"{t:.2f}   {fa:6d}     {fr:6d}   {far:7.2%}  {frr:7.2%}")
        if fa == 0 and (best is None or frr < best[1]):
            best = (t, frr)
    print(f"\n建议阈值: {best[0]:.2f} (FA=0, FR率 {best[1]:.2%})" if best
          else "\n警告: 不存在 FA=0 的阈值，需要调整数据或改用更强的模型")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"d:\PhotoLib\verify")
```

### S0 的三种结果与对应决策

| 结果 | 决策 |
|------|------|
| FA=0 且 FR < 15%，阈值在 0.40–0.60 | ✅ **通过，直接进 S1** |
| FA=0 但 FR 偏高（15–30%） | ⚠️ 通过，但人工确认队列要做得更好用（批量确认、Top-N 候选） |
| 存在 FA > 0 的可行阈值区间外 | ❌ 换 `antelopev2`（ResNet100，更强）重测；仍不行则考虑商业 API |

---

## S1 · 环境 + 骨架 + 数据库（1 天）

### 1. 数据库：SQLite（零安装）+ 原生 sqlite3 运行层

**不需要装任何东西。** Python 标准库自带 `sqlite3`。

> ⚠️ **本项目不使用 SQLAlchemy ORM**（决策见 `数据库设计.md` 与 `开发计划.md` DR-2）。数据访问分三层：
> `database/pb_*.txt`（唯一数据源）→ `sqliteCodeGenerator.py` → `common/sqliteCommon.py`（业务唯一入口，禁止裸 SQL）
> → `common/sqliteHandle.py`（原生 sqlite3 读写双连接）。下方 SQLAlchemy 代码块**仅作对照，实际不采用**：

```python
# 【对照用，实际不采用】SQLAlchemy 写法
from sqlalchemy import create_engine, event

engine = create_engine(
    "sqlite:///d:/PhotoLib/db/photolib.db",
    connect_args={"check_same_thread": False},   # FastAPI 多线程访问需要
    poolclass=StaticPool,                         # 单文件库用静态池
)

@event.listens_for(engine, "connect")
def _set_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA foreign_keys = ON")       # SQLite 默认关闭外键！必须开
    cur.execute("PRAGMA synchronous = NORMAL")
    cur.execute("PRAGMA busy_timeout = 5000")
    cur.execute("PRAGMA temp_store = MEMORY")
    cur.execute("PRAGMA cache_size = -64000")
    cur.close()
```

```python
# 【实际采用】code/src/common/sqliteHandle.py 的等价实现要点
# 1) 读连接与写连接各自执行上面 6 条 PRAGMA（只设一个连接是不够的）
# 2) sqlite3.connect(db_path, check_same_thread=False)   # FastAPI 同步端点在线程池里跑
# 3) row_factory = sqlite3.Row
# 4) 占位符统一写 %s，执行前统一转 ?（防注入）
```

> **必须设对的三条**：`foreign_keys=ON`（否则删照片留孤儿人脸）、`journal_mode=WAL`（写不阻塞读，扫描时前端还能浏览）、`busy_timeout`（遇锁等待而非报错）。

**如果将来要切 MySQL**：只改生成器方言 + 连接串，业务代码零改动。

### 2. Python 环境

```powershell
# 本机实际路径（venv 建在仓库内，不放照片库里）
C:\Users\NINGMEI\.workbuddy\binaries\python\versions\3.13.12\python.exe -m venv d:\home\lianyi\git\photo-browser\code\.venv
code\.venv\Scripts\pip install -i https://pypi.org/simple fastapi "uvicorn[standard]" pydantic-settings python-multipart
code\.venv\Scripts\pip install -i https://pypi.org/simple onnxruntime opencv-python-headless numpy pillow
code\.venv\Scripts\pip install -i https://pypi.org/simple vobject reverse_geocoder
code\.venv\Scripts\pip install -i https://pypi.org/simple insightface
```

> 注意：**没有** `pymysql` / `cryptography` —— 那是 MySQL 才需要的。SQLite 走标准库，零数据库依赖。
> **没有** `sqlalchemy` —— 本项目用原生 sqlite3 + 代码生成器。
> **pip 必须用官方源** `https://pypi.org/simple`（清华镜像在本机失效，返回 `no versions`）。
> `reverse_geocoder` 为**可选**依赖，缺失时地点字段留空并降级，不报错。

> **⚠️ 两个已知风险，S1 第一天先验证：**
> 1. **`insightface` 在 Python 3.13 上可能装不上**（依赖 Cython 编译）。若失败 → 降级到 **Python 3.12** 建 venv，这是最省事的解法。
> 2. **`insightface` 在 Windows 上可能要 VC++ Build Tools**。若装不上或编译失败 → **改用备选方案**：直接用 `onnxruntime` 加载 SCRFD + ArcFace 的 ONNX 模型，跳过 `insightface` 包。多写约 80 行代码，但**零编译、零外部依赖**，反而更可控。S0 脚本里的 `FaceAnalysis` 换成自写的 `load_onnx()` 即可。
>
> 记住：装 `onnxruntime`（CPU 版），**不要** `onnxruntime-gpu`。

### 3. 代码目录结构

> 实际结构以 `plan/开发计划.md` §2.2 为准（工作区在 `d:\home\lianyi\git\photo-browser\code\`）。
> 下面是本阶段的原始设想，**已被 12 步方案取代**（关键差异：无 ORM、缩略图独立 `thumb\`、配置驱动路径）：

```
D:\PhotoLib\                      # photoRoot（配置项 PHOTO_ROOT，默认 d:\PhotoLib）
├── photo\                      # 照片（只读，绝不写入生成物）
├── thumb\                      # 缩略图 + 人脸裁剪图（可重建）
├── db\                         # 数据库 / 导入归档 / 配置
└── app\
    ├── config.py               # 配置加载（photoRoot / thumbRoot / dbFile）
    ├── db.py                   # 原生 sqlite3 读写双连接 + PRAGMA
    ├── common/sqliteCommon.py  # 代码生成器产出（业务唯一入口）
    ├── scanner\                # walker.py / meta.py / runner.py
    ├── face\                   # engine.py / bucket.py / matcher.py / cluster.py
    ├── contacts\               # csv_import.py / vcard_import.py
    ├── api\                    # scan.py  browse.py  review.py  contacts.py  static.py
    └── main.py                 # FastAPI 入口（只绑 127.0.0.1）
```

### 4. 验收标准

- [ ] `d:\PhotoLib\db\photolib.db` 文件生成，`sqlite3` 命令行能打开
- [ ] **8 张表**（`pb_family` / `pb_person` / `pb_person_category` / `pb_photo` / `pb_face` / `pb_person_centroid` / `pb_photo_person` / `pb_scan_job`）由 `build_db.py` 建成，索引齐全
- [ ] `PRAGMA table_info` 显示各表 `recID` 为 `INTEGER` 且 `pk=1`（.txt 写 `INT AUTO_INCREMENT`）
- [ ] `PRAGMA foreign_keys` 返回 1，`PRAGMA journal_mode` 返回 `wal`（读、写连接都生效）
- [ ] 重复执行 `build_db.py` 幂等（`chkTableExist` 生效）
- [ ] `insightface` 或裸 ONNX 任一方案能跑通 S0 脚本
- [ ] `uvicorn main:app` 起得来，`/docs` 能看到 OpenAPI 页面

---

## S2 · 扫描器（2–3 天）

### 交付物

| 文件 | 职责 |
|------|------|
| `walker.py` | 递归遍历 `photo\`，算 `RelPathHash`（规范化后）与 `FileHash`（流式 SHA-256，避免大文件吃内存） |
| `meta.py` | EXIF 时间/GPS/尺寸/方向；GPS → 离线逆地理 → `PlaceName` |
| `runner.py` | 增量判定、批量 upsert、进度上报、断点续扫 |

### 关键实现点

0. **⚠️ 单写入者架构（最重要，别写错）**
   `ProcessPoolExecutor` 的子进程**绝对不能连数据库**，否则 `database is locked`。正确分工：
   ```
   主进程：遍历 + hash + 批量写库（单线程，executemany，500 条/事务）
   子进程：只做 CPU 密集的读图/检测/提特征，结果经 Queue 回主进程
   ```
   这个架构对 MySQL 也是最佳实践，所以不亏——只是 SQLite 下从"建议"变成"必须"。
1. **路径规范化**：统一 `/`、去除 `./`、Unicode NFC 归一化；**归一化只用于算 hash，不要改写 `RelPath` 原值**
2. **hash 大文件**：分块读取（8 MB/块），不要 `read()` 整个文件
3. **增量判定走索引**：`RelPathHash` 和 `FileHash` 都建了索引，三条路 O(1)
4. **移动/重命名检测**：`FileHash` 命中但 `RelPathHash` 未命中 → 提示用户确认，别自动改
5. **删除检测**：库里存在但磁盘上找不到 → 标记 `IsMissing`，不要直接删（可能只是移动硬盘没插）
6. **事务粒度**：每 500 条提交一次，兼顾速度和中途中断的损失

### 验收标准

- [ ] 10 万张全量扫描完成，无遗漏、无重复入库
- [ ] 第二次扫描增量为 0 条新增（幂等性）
- [ ] 新增 100 张照片，增量扫描只处理这 100 张
- [ ] 复制同一张照片到两个路径 → 被识别为重复（`FileHash` 相同）
- [ ] 重命名文件 → 被识别为「移动」而非「新增+删除」
- [ ] 含 GPS 的照片 `PlaceName` 正确填充

---

## S3 · 人脸引擎 + 分桶 + 匹配（3–5 天）

### 这是整个项目最难的部分，S0 的数据直接决定参数

| 文件 | 职责 |
|------|------|
| `engine.py` | 封装检测+提取，统一输出 `(bbox, pose, det_score, normed_embedding)` |
| `bucket.py` | `bucket_key(shot_year, birth_year=None) -> str` |
| `matcher.py` | 三段式决策 + 候选桶取 max |
| `cluster.py` | 未归类人脸的 DBSCAN 聚类，生成「未命名人物」 |

### 分桶规则（S0 验证后二选一）

```python
# 方案 A：等宽 5 年（零前置条件）
def bucket_key_equal(shot_year, birth_year=None):
    start = (shot_year // 5) * 5
    return f"{start}-{start + 4}"

# 方案 B：按年龄自适应（推荐，需要 Person.Birthday）
def bucket_key_adaptive(shot_year, birth_year=None):
    if birth_year is None:
        return bucket_key_equal(shot_year)
    age = shot_year - birth_year
    if age <= 18:
        start = birth_year + (age // 3) * 3
        return f"{start}-{start + 2}"
    start = birth_year + 18 + ((age - 18) // 10) * 10
    return f"{start}-{start + 9}"
```

### 匹配决策

```
候选桶 = [B0-1, B0, B0+1] 中所有有样本的桶
score  = max(cosine(face, PersonCentroid[bucket]))     # 取 max，不取 mean
score >= T_high → 自动归属
T_low <= score < T_high → 待人工确认（记 Top-5 候选）
score < T_low → 未知人脸 → 进聚类
```

### 关键实现点

1. **向量按候选桶加载进内存**：10 万 × 512 float32 = 205MB，**不做全量加载**（只取相邻三桶，见开发计划 DR-12），比对用 NumPy 矩阵乘
2. **质心重算**：人工确认后立即重算该 `(PersonId, BucketKey)` 的 centroid，不用全量重跑
3. **质量过滤在提取阶段做掉**：`det_score < 0.6`、人脸短边 < 64px、`|pose_yaw| > 45°` → 丢弃不入库
4. **聚类只在未归类集合上跑**：DBSCAN `eps=0.45`, `min_samples=3`（需按 S0 数据调整）

### 验收标准

- [ ] 用 S0 的 100 张验证集，识别准确率与 S0 结果一致（±2%）
- [ ] 人工确认 5 次后，同一人的新照片自动命中率明显上升（可量化：确认前 vs 确认后各测 20 张）
- [ ] 未命名人物的聚类簇，视觉上确实是同一个人（输出 contact sheet 抽查）
- [ ] 阈值切换为「保守/激进」时，结果可复现

---

## S4 · 联系人导入（1–2 天）

### 交付物

- `csv_import.py`：主力通道。Outlook 导出 CSV → 解析 → upsert `Person`
- `vcard_import.py`：`vobject` 解析（注意 Outlook 单联系人限制）
- `graph_import.py`：P2，可延后到 S7

### 关键实现点

1. **幂等**：以 `VCardUid` 为准，命中则 UPDATE；无 UID 用 `DisplayName` 匹配
2. **只建档案，不关联照片** —— 照片归属由人脸识别负责
3. **`Categories` 解析**：Outlook 的「类别」列是分号分隔 → 拆成多行写入 `PersonCategory` 表
4. **`Family` 推导**：若多人 `FamilyName` 相同 → 提示是否合并为一个家庭组（半自动，别自动建）

### 验收标准

- [ ] 导入 200 人 CSV，字段全部落库正确（姓名/类别/关系/生日/邮箱/电话）
- [ ] 重复导入同一文件 → 0 新增，字段被更新而非重复，`PersonCategory` 不残留旧行
- [ ] 按 label 查询正确：`JOIN PersonCategory WHERE Category='family'`
- [ ] vCard 4.0 的 `KIND:group` 能识别为家庭组

---

## S5 · 后端 API（3–4 天）

按方案文档 3.9 的清单实现。优先级：

**P0（S6 前端依赖）**
- `POST /api/scan/start` + `GET /api/scan/status/{jobId}`
- `GET /api/timeline` · `GET /api/photos` · `GET /api/persons`
- `GET /api/thumb/{photoId}` · `GET /api/original/{photoId}`

**P1（识别闭环）**
- `GET /api/review/pending` · `PUT /api/review/{faceId}/assign`
- `POST /api/review/merge` · `POST /api/review/split`
- `POST /api/contacts/import/csv`

**P2（完整体验）**
- `GET /api/families` · `GET /api/places` · `GET /api/map`
- 重复照片接口 · `POST /api/contacts/sync/graph`

### 关键实现点

1. **扫描是后台任务**：`BackgroundTasks` + 内存状态字典，`jobId` 轮询查进度
2. **缩略图按需生成 + 落盘缓存**：`thumb\thumbs\<fileHash前2位>\<fileHash>_<size>.webp`（size ∈ 200/400/800，WebP）；人脸裁剪图落 `thumb\faces\<faceCode前2位>\<faceCode>.jpg`。路径**可推导、不入库**（`pb_photo` 无 `thumbPath` 字段）；写入先 `*.tmp` 再 `os.replace`
3. **`/api/original` 支持 Range**：浏览器才能流畅预加载；`/api/thumb` 带 ETag + `Cache-Control: max-age`
4. **分页统一**：`page` + `size`，返回 `total`（时间线用游标或按年月分段，禁深 OFFSET）
5. **本地访问**：绑定 `127.0.0.1`，不开 `0.0.0.0`

### 验收标准

- [ ] 触发扫描后，前端能实时看到进度（已扫描/新增/重复/待确认计数）
- [ ] 10 万张的 `/api/timeline` 首屏 < 500 ms
- [ ] 缩略图二次访问命中磁盘缓存（不重复解码原图）
- [ ] 原图在浏览器里能流畅滚动查看

---

## S6 · 前端视图（5–7 天）

先做 3 个，不要一次做完：

| 优先级 | 视图 | 说明 |
|--------|------|------|
| **P0** | 时间线 | 年月分组吸顶 + 瀑布流懒加载 |
| **P0** | 人员 | 头像网格 → 点进单人时间线 |
| **P0** | 待确认队列 | **最关键**——人脸卡片 + Top-5 候选 + 「都不是」新建 + 批量确认 |
| P1 | 扫描控制台 | 触发扫描 + 实时进度 |
| P1 | 单人 / 多人 | 多人支持 AND（合影）/ OR 切换 |
| P2 | 家庭 / 地点地图 / 重复照片 | S7 |

### 待确认队列要做好，因为它是准确率的兜底

- 一张人脸卡的 Top-5 候选：**显示候选人的头像 + 相似度**，一键选中
- **批量确认**：同一聚类簇下所有人脸一次全确认
- 「都不是」→ 下拉选联系人 或 新建人员
- 「合并」/「拆分」入口要在手边，别藏三级菜单

### 验收标准

- [ ] 时间线滚动 10 万张不卡（虚拟滚动或分页）
- [ ] 确认一个人的人脸后，该人照片数立即更新
- [ ] 批量确认 50 张同类人脸 ≤ 3 次点击

---

## S7 · 打磨（3–4 天）

家庭视图 · 地点地图（Leaflet + 离线瓦片）· 多人合影筛选 · 重复照片对比 · Graph API 同步 · 备份脚本。

---

## 里程碑检查点

| 检查点 | 位置 | 通过条件 |
|--------|------|----------|
| **M0** | S0 结束 | FA=0，FR<15%，阈值合理 → **决定是否继续** |
| **M1** | S2 结束 | 10 万张扫描幂等、去重准确 |
| **M2** | S3 结束 | 识别准确率复现 S0，人工确认闭环生效 |
| **M3** | S5 结束 | API 全部可用，扫描可后台跑 |
| **M4** | S6 结束 | **自己真正用起来**——把你日常浏览照片的习惯切过来用一周 |

> M4 是最重要的验收点。如果用了三天你还想打开 Windows 资源管理器看照片，说明产品逻辑有问题，得回头改。

---

## 我建议的第一步

**就做 S0，别做别的。**

半天时间、100 张照片、60 行脚本，换一个「这个项目到底能不能成」的确定答案。确认通过后我再按 S1 → S7 逐阶段交付代码，每个阶段你验收完再进下一个。
