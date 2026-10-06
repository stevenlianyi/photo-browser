# photo-browser 分步开发提示语（12 步）

> 配套：`plan/开发计划.md`（总纲）｜`plan/数据库设计.md`（表定义）｜`plan/UI/photo-browser UI 设计.md`（信息架构）
>
> **使用方法**：每一步**单独开一个新对话**，把下面对应的提示语整段复制粘贴进去。
> 前一步验收未通过，不要开始下一步。
> 每步结束统一输出：**改动文件清单 + 验收结果 + 遗留问题**。
>
> ⚠️ **当前进度：步骤 1–11 已执行完毕，下一步是步骤 12（最后一步）。**
> 进度以文末「附录 A」的状态表为准；步骤 11 的实际产出与本文件里步骤 11 提示语的
> 偏差、以及步骤 12 要接着做的事，都写在文末「进入步骤 12 时要知道的现状」里。
>
> 历史：步骤 1–6 执行后核对发现两处返工，顺序是 **R2 → R → 步骤 7**：
> **R2 = 分桶口径修复**（`pb_face.shotBucket` 从来没被重写成自适应桶，**S0 的分桶结论一直在跑对照组**）
> → **R = 纠错闭环**（DR-16）→ 步骤 7。两项均已完成并入。

---

## 全局约定（每步都适用，已写进各提示语，此处仅备查）

| 项 | 约定 |
| --- | --- |
| 代码根 | `d:/home/lianyi/git/photo-browser/code/` |
| 文档根 | `d:/home/lianyi/git/photo-browser/plan/` |
| Python | `C:\Users\NINGMEI\.workbuddy\binaries\python\versions\3.13.12\python.exe`（venv 建在 `code/.venv`） |
| pip 源 | **必须官方源** `https://pypi.org/simple`（清华镜像在本机失效） |
| photoRoot | `d:\PhotoLib`（可改，`code/src/config/local_settings.py`） |
| 原图 | **绝对只读**：不写、不删、不改名 |
| 业务层 | **禁止裸 SQL**，一律走 `sqliteCommon` |
| 表定义 | `code/src/database/pb_*.txt` 是唯一数据源，**禁止手工改 `auto_generated/`** |
| 网络 | 服务只绑 `127.0.0.1` |

---
---

# 修正步骤 R · 返工修正步骤 1–6（纠错闭环 DR-16）

> **什么时候做**：现在。步骤 7 之前必须完成。
> **为什么要返工**：新增了「用户浏览时改判认错的人脸」这条闭环（DR-16），核对现有代码发现 6 处冲突，
> 其中 1 处是**根因**，不改则后续全部白做。

```text
【photo-browser · 修正步骤 R · 返工修正步骤 1–6】

## 目标
把已完成的步骤 1–6 对齐到最新的「纠错闭环」口径（plan/开发计划.md DR-16）：
① 质心只用人工确认样本（防污染）；② 新增 ALL 兜底桶；③ 新增 isStranger 与 pb_review_log；
④ 把「自动归属」与「人工确认」真正区分开（这是根因）；⑤ 存量数据修正与质心重建。

## 前置
步骤 1–6 已完成，并且**已有真实数据**（d:\PhotoLib\photo 有真实照片、正式库有真实记录，
据开发计划 DR-12/DR-15 实测约 10 万行 pb_photo）。
本步不新增业务功能，只做口径修正 + 数据迁移。

## 必须先读的项目文档
- plan/开发计划.md 第四节 DR-16（本次要落地的全部口径）、第五节步骤 6/7 行
- plan/数据库设计.md §4.5 pb_face（**四态语义表**）、§4.6 pb_person_centroid（质心三级启用）、
  §4.9 pb_review_log（8 种 opType 与副作用表）、§五 索引清单、§六 D-4/D-9/D-10/D-11
- plan/UI/photo-browser UI 设计.md 第 4.4 / 4.5 / 4.6 节（人脸框三态描边、P-06 双 Tab、P-05 样本分两段）

## 一、先读现有代码，确认真实差距（不要凭我的描述改）
重点读这 5 个文件，**逐条核对下面的「现状 → 应为」**：
- code/src/processor/review/assigner.py
- code/src/processor/review/merger.py
- code/src/engine/match/centroid.py
- code/src/engine/match/matcher.py
- code/src/config/basicSettings.py

已知差距（我已核对过源码，但你必须自己再确认一遍再动手）：

| # | 文件 | 现状 | 应改为 |
|---|---|---|---|
| 1 | `assigner.py` 的 `assign()` | 无论人工还是自动，**都写 `isConfirmed=1`**（约 234 行硬编码） | `isConfirmed=1` **只表示经人工确认**；自动归属必须写 0 |
| 2 | `assigner.py` 文件头 + `__main__` | 写着「待确认 = personCode IS NULL **OR isConfirmed=0**」 | 改为 `personCode IS NULL AND isStranger=0` |
| 3 | `centroid.py` 的 `loadFaceVectors()` | 查回这个人的全部脸后只按 shotBucket 过滤，**无 isConfirmed 过滤** | 只取 `isConfirmed=1` 的样本（可开关，见 3.5） |
| 4 | `centroid.py` | 无 `ALL` 兜底桶；桶样本不足直接不启用（约 215 行） | 桶确认样本 <3 时退到 `ALL`；总确认样本 <3 才是真不启用 |
| 5 | `matcher.py` | 候选桶 = `bucket.neighborBucketKeys(...)`（约 407 行） | 候选集合 **∪ {ALL}**；`shotBucket` 为空的脸候选桶就是 `{ALL}` |
| 6 | 全库 | 无 `pb_review_log` 表、`pb_face` 无 `isStranger` 列 | 建表 + 加列 + 补索引 |

**第 1 条是根因**：自动归属也写 `isConfirmed=1`，导致
(a) 无法区分「机器认的」与「人工确认的」，「我不同意」列表无从表达；
(b) 质心全部由自动样本构成 → 防污染无从下手。

## 二、表结构与数据迁移

### 2.1 表定义（我已改好，你只需核对）
- `code/src/database/pb_review_log.txt` —— **新建的第 9 张表**，22 字段，按该文件写
- `code/src/database/pb_face.txt` —— 已加 `isStranger TINYINT NOT NULL DEFAULT 0`
- `code/src/database/pb_person_centroid.txt` —— 注释已写明 `ALL` 兜底桶与「只统计确认样本」
- `code/src/database/pb_photo_person.txt` —— `source` 注释已写明 0 自动 / 1 人工确认或改判

**重跑生成器**（`python code/src/database/sqliteCodeGenerator.py`）→ 产物落
`code/src/database/auto_generated/sqliteCommon.py`，**禁止手工改产物**。

### 2.2 迁移（**用 `--migrate`，不要 `--drop`**）
```
python code/src/tools/build_db.py --migrate
```
- 只 `ALTER TABLE ADD COLUMN` 补缺的（加 `pb_face.isStranger`）+ 建 `pb_review_log` + 补 4 个新索引
- **不删列、不改列类型、不动任何一行**（DR-13）
- ⚠️ **动手前先备份**：`copy d:\PhotoLib\db\photolib.db d:\PhotoLib\db\photolib.db.bak-before-R`
- 迁移后核对：`PRAGMA table_info(pb_face)` 有 `isStranger`；`sqlite_master` 有 `pb_review_log`
  与 4 个新索引（`pb_face(personCode IS NULL)`、`pb_face(personCode, isConfirmed)` 部分索引、
  `pb_review_log(isRevertible)` 部分索引、`pb_photo(movedToPhotoCode)`）
- 迁移前后各记录一次逐表行数（`SELECT COUNT(*)`），**必须完全一致**

### 2.3 存量数据修正（**必做，否则「我不同意」列表是空的**）
现有自动归属的脸被写成了 `isConfirmed=1`，要按真实语义回改：

写一次性脚本 `code/src/tools/fix_confirmed_flag.py`：
- 依据 `pb_photo_person.source`：`source=0`（自动）→ 对应 `pb_face.isConfirmed` 回改为 **0**；
  `source=1`（人工）→ 保持 1
- ⚠️ 一张照片可能有多个 `pb_photo_person` 行、一个人脸只对应一个 `faceCode`。
  **以 `pb_photo_person.faceCode` 为准**（那是判定来源那张脸）；`faceCode` 为空的行跳过并计数报告
- 先 `--dry-run` 打印将要改的行数与样例，确认后再实跑
- 跑完打印：回改行数、跳过行数、以及改后
  `SELECT COUNT(*) FROM pb_face WHERE personCode IS NOT NULL AND isConfirmed=0` 的结果

**重算全部质心**：旧质心是污染样本算出来的，必须作废。
走 `centroid.recomputePerson()` 对库里每个有脸的 personCode 重算（**不删库**，只重算）。

## 三、代码修正

### 3.1 `assigner.py` —— 拆开「人工确认」与「自动归属」
- `assign(faceCode, personCode, source, ...)` 的 `isConfirmed` 必须由 `source` 决定：
  - `source == comGD.LINK_SOURCE_MANUAL` → `isConfirmed=1`
  - `source == comGD.LINK_SOURCE_AUTO` → `isConfirmed=0`（**当前硬编码 1，要改**）
- 建议同时提供两个语义明确的入口（内部共用 `_setBelong()`），避免调用方继续传错 `source`：
  - `confirm(faceCode, personCode, confidence=None)` —— 人工确认，`isConfirmed=1`、link `source=1`
  - `autoAssign(faceCode, personCode, confidence)` —— 自动归属，`isConfirmed=0`、link `source=0`
- **`fix(faceCode, action, personCode=None)` 统一改判入口**：

  | action | 效果 | 落 pb_face | 关联 | 重算质心 |
  |---|---|---|---|---|
  | `assign` | 改判到某人 | `personCode=新, isConfirmed=1` | 删旧 linkKey + 写新 `source=1` | **原人 + 新人**全部桶 |
  | `unknown` | 置为未知 | `personCode=NULL, isConfirmed=0` | 删旧 linkKey | 原人全部桶 |
  | `stranger` | 标记陌生人 | `personCode=NULL, isStranger=1` | 删旧 linkKey | 原人全部桶 |

  - 现有 `unassign()` 保留，但**明确它 == `fix('unknown')`**，别留两套语义
- `batchFix(faceCodes, action, personCode)`：同一 `clusterCode` 批量，一个事务 + 一次重算
- 每次写操作**必须落一条 `pb_review_log`**（`logCode` 幂等键、`opType`、`faceCode`、`photoCode`、
  `fromPersonCode`、`toPersonCode`、`similarity`、`faceCount`、`opYMDHMS`）
- ⚠️ 写 `pb_review_log` 时**注意 upsert 写 NULL 的坑**（assigner 文件头已记录：`update_*` 写不进 NULL，
  `fromPersonCode` 为空时必须走 upsert + `forceColumns`）
- ⚠️ 纪律 ③（`pb_photo_person` 只在「这张照片里确实有人属于 P」时存在）**继续生效**，
  改判/陌生人/置未知都要走 `_facesInPhotoFor()` 判断后再决定删不删关联

### 3.2 `merger.py`
- `merge()` 迁移脸时写 `isConfirmed=1` —— **保持不变**（用户主动合并就是人工确认）
- `merge()` / `split()` 各自落一条 `pb_review_log`，**`isRevertible=1`**
- 新增 `undo(logCode)`：
  - 只允许撤销 `isRevertible=1 AND revertedByLogCode IS NULL` 的记录，否则抛错
  - 反向恢复 `pb_face.personCode` 与 `pb_photo_person` 关联
  - **重算涉及双方的质心**
  - 回填原记录的 `revertedByLogCode`，并写一条 `opType=UNDO` 的新日志
  - `merge` 撤销要恢复 `fromPerson`（软删的 `pb_person.delFlag` 也要恢复）

### 3.3 `centroid.py` —— 防污染 + 兜底桶
- `loadFaceVectors(personCode, bucketKey, confirmedOnly=True)`：加 `isConfirmed=1` 过滤
  - ⚠️ 生成层 `query_pb_face` **没有 `isConfirmed` 查询参数**（只有 recID/faceCode/photoCode/
    personCode + nullFields）。**不要为此改生成器加参数**（要重生成 + 全库迁移，代价与收益
    不成比例）；按现有文件头的做法：查回这个人的脸，在 Python 里过滤
- 新增 `ALL` 桶：`bucketKey = "ALL"` = 该人**全部确认样本**（不分桶）
- `computeCentroid()` / `recompute()` 支持 `ALL`
- **质心三级启用**（写进文件头）：

  | 优先级 | 桶 | 启用条件 |
  |---|---|---|
  | 1 | 相邻年代桶 | 该桶**确认样本** ≥ 3 |
  | 2 | `ALL` 兜底桶 | 总确认样本 ≥ 3 |
  | 3 | 无 | 总确认样本 < 3 → **该人不参与自动匹配** |

- `recomputePerson()` 重算时**也要算 `ALL` 桶**，并把已不存在的桶清掉（现有清僵尸桶逻辑保留）
- ⚠️ **`listBucketsOf()` 要排除 `ALL`**（它是虚拟桶，不来自任何 `pb_face.shotBucket`），
  否则 `recomputePerson` 会去算一个不存在的桶
- `dropPerson()` / `dropBucket()` / `centroidOf()` 保持可用，`ALL` 走同一套

### 3.4 `matcher.py` —— 候选桶并入 `ALL`
- 候选桶 = `bucket.neighborBucketKeys(bucketKey, neighbor)` **∪ `["ALL"]`**
- `shotBucket` 为空（截图、EXIF 缺失）的脸：`neighborBucketKeys("")` 返回 `[]`，
  候选桶就是 `["ALL"]` —— **这类脸现在也能匹配了**，别让它直接掉进聚类
- `candidateBuckets` 报告要如实反映实际参与的桶
- 三段式判定、原因码、Top-5 降序、两次跑完全一致 —— 这些既有行为**不要动**

### 3.5 `basicSettings.py` —— 冷启动开关
新增：
```
CENTROID_CONFIRMED_ONLY: bool = True   # True=只用 isConfirmed=1 样本（防污染，默认）
                                       # False=退回旧口径（全样本），仅用于回归对比与冷启动
```
⚠️ **为什么需要这个开关**：改成「只用确认样本」后，**在用户还没人工确认过任何脸之前，
所有质心都不可用 → 自动归属数为 0 → 所有人脸进待确认队列**。
这是**正确行为**（没有干净样本可用），但会让 S0 回归验证跑不出结果。
所以留一个逃生口：验收第 21 条与 S0 回归对比用 `False`，日常跑 `True`。

## 四、验收清单（逐条实际运行，不要只写代码就宣称通过）

### A. 迁移
1. 迁移前已备份 `photolib.db.bak-before-R`
2. `build_db.py --migrate` 成功；**逐表行数迁移前后完全一致**（贴出前后对照）
3. `PRAGMA table_info(pb_face)` 含 `isStranger`（默认 0）；`sqlite_master` 含 `pb_review_log`
   与 4 个新索引
4. `PRAGMA integrity_check` 返回 ok

### B. 语义修正
5. `fix_confirmed_flag.py --dry-run` 输出合理 → 实跑后：
   `SELECT COUNT(*) FROM pb_face WHERE personCode IS NOT NULL AND isConfirmed=0` **> 0**
   （这就是「我不同意」列表的条数，必须不为 0，否则说明回改没生效）
6. 四态互斥性检查（写 SQL 验证）：
   - 待确认 = `personCode IS NULL AND isStranger=0`
   - 我不同意 = `personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0`
   - 人工确认 = `isConfirmed=1`
   - 陌生人 = `isStranger=1`
   - **四者之和 == `pb_face` 总行数**（不重不漏）

### C. 质心防污染（**本步最核心的三条**）
7. 造测试数据：某 person 某桶放 2 张 `isConfirmed=1` + 1 张 `isConfirmed=0`（属于别人的脸），
   `recomputePerson` 后该桶 **`sampleCount == 2` 且 `centroid` 与只有那 2 张时逐字节相同**
   —— 证明自动样本没进质心
8. `ALL` 兜底：某 person 桶确认样本只有 1 张，但总确认样本 4 张 → 该桶不启用、
   **`ALL` 桶启用且 sampleCount=4**，且该人能被匹配到
9. 总确认样本 2 张的人 → **不启用任何质心**，`loadAllCentroids` 的索引里没有他
10. `shotBucket` 为空的脸 → 候选桶 == `["ALL"]`，能拿到分数（不再直接掉聚类）
11. 旧质心已全部重算（贴出重算前后 `pb_person_centroid` 行数与 `sampleCount` 变化）

### D. 纠错链路
12. `fix(action='assign')`：**原人与新人的质心都重算**（查两人 `modifyYMDHMS` 或质心内容，
    只重算一边算不合格）；旧 `linkKey` 已删；新行 `source=1`
13. `fix(action='unknown')` → 该脸进待确认队列；关联行按纪律 ③ 正确存废
14. `fix(action='stranger')` → 该脸**既不在待确认、也不在「我不同意」、也不参与聚类**
15. `merge` 后 `undo` 能完整还原（人脸归属 + `pb_photo_person` + 双方质心 + `fromPerson` 的
    `delFlag`），且 `revertedByLogCode` 已回填
16. `undo` 对 `isRevertible=0` 的记录（普通确认）**必须报错拒绝**
17. 每次写操作都新增了一条 `pb_review_log`，`opType`/`fromPersonCode`/`toPersonCode` 正确
18. `verifyLinks()` 仍然 `clean=True`（改判/陌生人之后不能留下幽灵关联）
19. **纪律 ③ 回归**：同一张合影里 P 有 2 张脸，把其中 1 张改判给别人 →
    `pb_photo_person` 里 P 的那行**必须还在**（还有 1 张脸属于 P）

### E. 回归
20. `pytest code/src/test` 全绿（含新增的防污染、兜底桶、改判、撤销单测）
21. `CENTROID_CONFIRMED_ONLY=False` 时行为与修正前一致（**用旧口径跑一遍 S0 验证集，
    给出 FR 数字**，与之前基线对比，确认代码改动没有意外改变匹配能力）
22. `tools/backtest_s0.py` 跑通，给出准确率与耗时
23. `tools/scan_cli.py --db <临时库>` 指向临时库跑一小批，确认**正式库行数不变**
    （`--db` 是 DR-10 强调过的坑，务必验证）
24. **photoDir 零风险**：扫描前后 `photoDir` 的文件数与总字节数完全一致

## 五、硬约束
- **原图绝对只读**：`d:\PhotoLib\photo` 一律只读，扫描前后文件数与字节数必须一致
- **迁移只加不删**：`--migrate` 不删列不改类型不动数据；不删任何行
- **业务层禁止裸 SQL**：一切读写经 `sqliteCommon`；新表走生成的
  `query_pb_review_log` / `insert_pb_review_log` / `update_pb_review_log` / `delete_pb_review_log`
- **禁止手工改 `auto_generated/`**：改表一律回 `pb_*.txt` 再重跑生成器
- **单写入者**：所有写库在主进程；本步不引入子进程
- 现有代码风格（文件头纪律说明、错误码、`_VERSION`、日志）保持一致，别把注释删了
- 不要顺手重构与本步无关的代码

## 六、完成后必须输出
1. 改动文件清单（新增 / 修改，逐个列路径）
2. 验收结果（上面 24 条**逐条**给命令与实际输出 / 数值）
3. 遗留问题与需要我决策的点
4. **存量数据修正后的统计**：四态各多少条、质心重算前后对比
5. 步骤 7 可以开始的判断：以上 24 条是否全部通过
```

---
---

# 修正步骤 R2 · 分桶口径修复（自适应分桶从未生效 · DR-20/21/22）

> **什么时候做**：**先于修正步骤 R**（R 已经假设质心口径正确了；R2 修的是桶键来源）。
> **为什么必须做**：`faceStore.makeShotBucket()` 写的是等宽 5 年**占位**桶，
> 注释说「步骤 6 会用自适应规则重算覆盖」，**但步骤 6 从来没做这个覆盖**。
> 全库 `bucketKeyAdaptive()` 只在验证脚本、CLI 报告与测试里被调用，**没有任何生产路径写回
> `pb_face.shotBucket`**。而 `matcher.bucketKeyOfFace()` 明确「只认 `shotBucket` 这一列」。
> 结果：生产库里所有脸都是等宽 5 年桶 → **S0 的「自适应分桶让 FR 32.75%→19%」一直在跑对照组**，
> `bucket.py` 的自适应逻辑是死代码，**且 DR-18「改生日重算质心」完全无效**（桶键不依赖生日）。

```text
【photo-browser · 修正步骤 R2 · 分桶口径修复】

## 目标
三件事，缺一不可：
① **补「重刷 shotBucket」过程**（方案 A，用户已定）：让 `pb_face.shotBucket` 真正按
   「拍摄年 + 出生年」算出自适应桶（0–18 岁 3 年 / 18+ 10 年），而不是等宽 5 年占位；
② **放宽未归属脸的候选桶**（DR-21）：否则跨口径对不上，等宽桶的脸一把质心都取不到；
③ **确立「先刷桶，再重算质心」的硬顺序**（DR-22），并加前置检查。

## 前置
步骤 1–6 已完成，**真实数据已在库里**（10 万行 pb_photo、真实人脸与质心）。
修正步骤 R 尚未开始（若已开始的，先停下做完 R2）。

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-20 / DR-21 / DR-22**（本次要落地的全部口径）
- plan/数据库设计.md §4.5 pb_face.shotBucket、§4.6 pb_person_centroid
- plan/MVP_plan.md S3 的分桶规则与匹配决策

## 必须先读���现有代码（逐条核对「现状 → 应为」，不要凭我的描述改）
| 文件 | 关键点 |
|---|---|
| `code/src/engine/face/faceStore.py` | `makeShotBucket()` 返回**等宽 5 年**（约 72–93 行），文件头与函数注释都写着「步骤 6 会重算覆盖」，**但没有任何代码做这件事** |
| `code/src/engine/match/bucket.py` | `bucketKeyAdaptive(shotYear, birthYear)` / `bucketKeyOf(shotYear, birthday)` / `birthYearOf(birthday)` —— **已实现且正确**，但生产路径没调用 |
| `code/src/engine/match/matcher.py` | `bucketKeyOfFace()`（约 293 行）**只认 `shotBucket` 列**，注释解释了「质心按 shotBucket 建、匹配必须按同一列取」——这个纪律是对的，**不要改成实时算** |
| `code/src/engine/match/centroid.py` | `listBucketsOf()` 从脸表读 `shotBucket`；`loadFaceVectors()` 按桶过滤；`recomputePerson()` 重算各桶 |
| `code/src/processor/review/assigner.py` | `assign()` 读 `face["shotBucket"]` 用于重算质心（约 397 行），**但从不重写它** |
| `code/src/processor/review/merger.py` | `merge()` 用 `update_pb_face` 迁移 `personCode`，**没管 `shotBucket`** |
| `code/src/tools/verify_bucket_gain.py` | 已能对比「等宽 vs 自适应」的收益，**R2 要用它给出真实数字** |

## 一、新增 `code/src/engine/match/rebucket.py`（本步核心）

```python
SHOT_BUCKET_WIDTH_FALLBACK = 5      # 无生日时的降级等宽（沿用 faceStore 口径）

def shotBucketFor(shotYear, birthday) -> str:
    """(拍摄年, pb_person.birthday 原文) -> 自适应桶键；生日不可用 -> 降级等宽 5 年。
    ⚠️ 薄封装，最终一律调 bucket.bucketKeyAdaptive()，**不在这里重写规则**"""

def rebucketFace(faceRow, personRow=None) -> dict:
    """单张脸：按 (pb_photo.shotYear, 该脸所属人的 birthday) 重算并写回 pb_face.shotBucket。
    - personRow 为 None（未归属）→ 降级等宽桶
    - **只改 shotBucket 一列**，走 upsert + forceColumns（注意 faceStore 已有 FACE_IDENTITY_COLUMNS 纪律）
    - ⚠️ **绝不碰 personCode / isConfirmed / isStranger / clusterCode / embedding**
    - 返回 {'faceCode','oldBucket','newBucket','changed'}"""

def rebucketPerson(personCode) -> dict:      # 该人全部脸（分页，别一次取全）
def rebucketPhoto(photoCode) -> dict:        # 该照片全部脸
def rebucketAll(batchRows=None, progress=None, onlyAdaptive=False) -> dict:
    """全库。**未归属的脸保持等宽桶**（没有生日可用），
    所以 onlyAdaptive=True 时只刷「已归属 + 目标人有合法生日」的那些"""
def auditBuckets() -> dict:
    """一致性巡检（只读，不写）：
    - bucketWidth 分布：宽 3 / 宽 10 = 自适应，宽 5 = 等宽降级
    - 孤儿质心：pb_person_centroid 里的 bucketKey 在该人脸表中**没有任何脸**
    - 失配脸：该人脸表的 shotBucket 集合与该人质心的 bucketKey 集合不相交
    - 无主质心：bucketKey='ALL' 之外的桶，sampleCount>0 但该桶已无脸"""
```

**巡检是本步最有价值的产出** —— 它能把「桶口径不一致」这类静默失配变成一条明确结论。
请把 `auditBuckets()` 的输出做成一目了然的表格（宽度分布用计数 + 举例）。

## 二、把刷桶接进写入路径（**根治点：不要靠人记得跑脚本**）

| 触发点 | 做什么 |
|---|---|
| **`assigner.assign()`** | 归属那一刻**生日才确定** → 先按新主人的 birthday 重刷这张脸的 `shotBucket`，**再**重算质心。⚠️ 这是最关键的一处 |
| `assigner.unassign()` / `fix('unknown')` | 退回未归属 → 刷回**等宽降级桶**（生日不再是这个人的） |
| `fix('stranger')` | 同上（该脸永远不会再匹配，刷成等宽即可） |
| `merger.merge()` | 迁移到目标人后，按**目标人**的 birthday 重刷全部迁移的脸（源与目标的 birthday 可能不同 → 桶键不同） |
| `merger.split()` | 拆出的人（新建档案）生日可能为空 → 刷成等宽降级桶 |
| **联系人导入后** | `import_contacts` 落库完成 → 对**本次新建/更新的人**跑 `rebucketPerson` + `recomputePerson`（生日到位了，桶键才该变） |
| 扫描提取后（`faceStore`） | 保持现状：仍写等宽占位桶（此时还不知道这张脸是谁），由 `assign` 负责刷。**但要在 faceStore 的函数注释里把「谁负责重刷」写清楚**，别再留「步骤 6 会覆盖」这种已经失效的承诺 |

## 三、DR-21：未归属脸的候选桶放宽到「全部已启用桶」

`matcher.py` 现在对所有脸都用 `bucket.neighborBucketKeys(bucketKey, neighbor)`。
问题：**未归属脸的桶是等宽 5 年，而别人的质心是自适应桶（宽 3 / 宽 10），键根本不对齐**
（等宽 `"2000-2004"` 的邻居是 `"1995-1999"`/`"2005-2009"`，而自适应童年桶可能是 `"1997-1999"`）
→ **未归属脸一把质心都取不到，只能靠 `ALL` 兜底 → 等于退化成不分桶**，
而这恰恰是最需要匹配的阶段。

改法：
```python
def candidateBucketsOf(faceRow, index=None):
    """faceRow['personCode'] 为空 -> 返回 None（表示"全部已启用的桶"）
    已归属 -> bucket.neighborBucketKeys(shotBucket, neighbor)"""
```
调用侧：候选为 `None` 时用 `index.subset(index.全部桶键)`（**`CentroidSubset` 的按人连续段与
`np.maximum.reduceat` 优化完整保留，不用重写**），并把 `ALL` 一并加入。

**成本核算要给出实测数字**（别只说「可接受」）：
- 质心行数 = Σ(人 × 每人启用桶数)，行数 × 512 × 4B = 内存
- 单脸一次矩阵乘的耗时（ms）
- 10 万张脸全量重匹配的 wall-clock

**注意与 DR-12 的关系**：DR-12 说「按候选桶惰性加载，10 万 × 512 = 205MB 已超预算」。
本条放宽后，未归属脸要全量 —— 但**质心行数远小于人脸行数**（质心 = 人数 × 桶数，几百到几千行，
几 MB），人脸的 embedding 才是 205MB 那一项，而那部分**没有变化**。请在报告里把
「质心矩阵」与「人脸向量矩阵」两笔内存分开列，别混成一个数字。

## 四、DR-22：硬顺序 + 前置检查

- `centroid.recomputePerson()` / `recompute()` 执行前**先做一致性检查**：
  脸表实际 `shotBucket` 集合 vs 质心表 `bucketKey` 集合，不一致 → **抛错并提示先跑 rebucket**，
  **不要默默按旧口径算**（那会造出僵尸质心 + 新桶无质心 → 匹配率归零且库里看不出异常）
- 提供 `tools/rebucket_cli.py`：
  `--all` / `--person <code>` / `--photo <code>` / `--audit` / `--dry-run` / `--recompute`
  ⚠️ `--recompute` 必须是**刷完桶之后**才重算，不要提供一个「只重算」的路径让人跳过刷桶

## 五、验收清单（逐条实际运行，不要只写代码就宣称通过）

### A. 桶口径修好了
1. `auditBuckets()` 改前 vs 改后的 **bucketWidth 分布**：改前宽 5 占比 ~100%；改后
   宽 3 / 宽 10 出现（有生日的人），宽 5 只剩「无生日 + 未归属」那些
2. 抽 3 个人，各挑一张有 `shotYear` 的脸，贴出 `shotBucket` 改前 → 改后的值，
   并手算 `bucketKeyAdaptive(shotYear, birthYear)` 验证与库里的值一致
3. `rebucketPerson` 对**已归属 + 有生日**的人：桶键集合与 `bucket.bucketKeyAdaptive` 逐条重算一致
4. 未归属的脸：桶宽仍是 5（降级），**不被误改成自适应桶**

### B. 写入路径已接上（**这是根治点，逐个测**）
5. 新建一个人（有生日）→ `assign()` 一张脸 → **该脸 `shotBucket` 立即变成自适应桶**（不是等宽）
6. `fix('unknown')` / `unassign()` → 该脸刷回等宽降级桶
7. `fix('stranger')` → 同上
8. `merge()`（源与目标 birthday 不同）→ **迁移后的脸按目标人生日重刷**，与目标人其他脸同口径
9. `split()` 拆出的人 birthday 为空 → 脸刷成等宽降级桶
10. 走查确认：`assign()` 里「先刷桶、后重算质心」的顺序不可颠倒

### C. DR-21 生效
11. 构造一张**未归属**脸 + 一个**有自适应桶质心**的人 → 该脸能拿到分数（改前拿不到，改前请先贴出对照）
12. 已归属脸仍走相邻三桶（**不要被 R2 改成全量**）：贴出候选桶列表证明仍是 3 个桶 + `ALL`
13. `candidateBucketsOf()` 对未归属脸返回 `None` → 调用侧正确转成「全部桶」
14. `CentroidSubset` 的 reduceat 路径仍被使用（代码走查 + 与改前相同的打分结果对照）

### D. 顺序纪律
15. 故意先 `recompute` 再 `rebucket`（用 `--recompute` 单独跑一次制造）→ 确认
    **前置检查抛错并提示先跑 rebucket**，而不是默默产出错误的质心

### E. 一致性与回归
16. `auditBuckets()` 改后：**孤儿质心 0 条、失配脸 0 条、无主质心 0 条**（贴出完整报告）
17. `pytest code/src/test` 全绿
18. **`tools/verify_bucket_gain.py` 给出自适应 vs 等宽的真实 FR 对比数字** ← 本步最硬的证据，
    之前 S0 的结论是 32.75% → 19%，请给出**生产库口径下**的实测值
19. `tools/run_match.py` 在真实库上跑一批脸，给出三段式分布（auto/review/cluster）与耗时
20. 修完后**全库重算质心**（`--all --recompute`），给出重算前后 `pb_person_centroid` 行数变化
21. `photoDir` 零风险：全程文件数与总字节数不变
22. 迁移/备份口径不变：`backup.py` 仍可整库拷贝（本步不得让 `photo\` 变成可写）

## 六、硬约束
- **原图绝对只读**：`d:\PhotoLib\photo` 一律只读
- **业务层禁止裸 SQL**：一律经 `sqliteCommon`
- **不要改成「匹配时实时算桶键」**（方案 B）：`CentroidSubset` 的 reduceat 优化会被推翻、
  匹配侧要重写。用户已定方案 A
- **不要用 `update_pb_face` 写 `shotBucket`**：它写不进 NULL/变化不可靠，走 upsert +
  `forceColumns`（faceStore/assigner 文件头已有该纪律的记录）
- **不要动 `pb_face` 的这些列**：`personCode` / `isConfirmed` / `isStranger` / `clusterCode` /
  `embedding` / `bbox` / 各种 score。R2 **只动 `shotBucket` 一列**
- 现有代码风格（文件头纪律说明、`_VERSION`、日志、`FACE_IDENTITY_COLUMNS` 等常量）保持一致
- 不要顺手重构与本步无关的代码

## 七、完成后必须输出
1. 改动文件清单（新增 / 修改，逐个列路径）
2. 验收结果（上面 22 条**逐条**给命令与实际输出 / 数值）
3. **`auditBuckets()` 改前 vs 改后的完整报告**
4. **内存两笔账分开列**：质心矩阵（MB）与人脸向量矩阵（MB）
5. **`verify_bucket_gain.py` 的自适应 vs 等宽 FR 实测对比**
6. 遗留问题与需要我决策的点
```

---
---

# 步骤 1 · 工程基线与配置骨架

```text
【photo-browser · 步骤 1/12 · 工程基线与配置骨架】

## 目标
建立可运行的 Python 后端工程骨架、配置体系与路径解析模块。本步不碰数据库、不碰扫描、不碰前端。

## 前置
无。这是第 1 步。

## 必须先读的项目文档
- d:/home/lianyi/git/photo-browser/plan/开发计划.md          ← 总纲，务必读完
- d:/home/lianyi/git/photo-browser/plan/数据库设计.md        ← 表定义与三层结构
- d:/home/lianyi/git/photo-browser/README.md

## 本步产出文件
1. d:/home/lianyi/git/photo-browser/requirements.txt
   - 后端依赖清单，注释里写明：必须用官方源 -i https://pypi.org/simple
   - 基础：fastapi, uvicorn[standard], pydantic-settings, python-multipart
   - 图像：pillow, opencv-python-headless, numpy
   - 元数据：reverse-geocoder（标注为可选依赖）
   - 人脸：insightface, onnxruntime（CPU 版，禁止 onnxruntime-gpu）
   - 联系人：vobject
   - 末尾注释：insightface 若在 Python 3.13 装不上，降级 Python 3.12 重建 venv
2. code/src/config/local_settings.py
   - PHOTO_ROOT: str = r"d:\PhotoLib"      # 默认值，可改
   - THUMB_ROOT: str = ""                   # 空 = 派生 <PHOTO_ROOT>\thumb
   - DB_FILE: str = ""                      # 空 = 派生 <PHOTO_ROOT>\db\photolib.db
3. code/src/config/local_settings.py.example   （同结构，仅作模板，入库）
4. code/src/config/basicSettings.py
   - 批大小 BATCH_SIZE=100；阈值 T_HIGH=0.55、T_LOW=0.35（以 S0 实测为准）
   - 质量过滤：MIN_DET_SCORE=0.6、MIN_FACE_EDGE=64、MAX_YAW=45
   - 支持的照片扩展名白名单（jpg/jpeg/png/webp/heic/bmp/tif/tiff/dng/cr2/nef/arf/wmv…）
   - 文件 hash 分块大小 8MB
5. code/src/config/sqliteSettings.py
   - 承载 PRAGMA 常量列表与 sqlite 文件装配入口（建库逻辑留到步骤 2，本步只放常量与装配函数签名）
6. code/src/common/miscCommon.py
   - setLogNew(...) 日志函数（复用 contentHub 风格：控制台 + 文件）
   - 当前时间 YYYYMMDDHHMMSS 字符串、ISO8601 UTC 字符串
   - 字符串/字节常用工具
7. code/src/common/globalDefinition.py
   - 错误码常量、任务状态机常量（IDLE/RUNNING/PAUSED/DONE/FAILED）、scanState 常量（0/1/2/3）
8. code/src/common/paths.py
   - photo_dir() / thumb_dir() / db_file()：空值时从 PHOTO_ROOT 派生，返回规范化绝对路径
   - validate_layout(photo, thumb, db)：三者互不嵌套、不得等于 PHOTO_ROOT，违规抛异常
   - ensure_dirs()：创建 thumb/db/imports/exports 目录（**绝不创建或写入 photo/**）
   - normalize_relpath(p)：把相对路径统一为正斜杠、去 ./、去首尾空白、Unicode NFC —— **仅用于算 hash，不得改写库中 relPath 原值**
   - dump_paths()：打印解析后的全部绝对路径，启动时调用
9. code/src/test/conftest.py 与 code/src/test/test_paths.py
   - 路径派生、嵌套校验（合法/非法各一组）、normalize_relpath 的单测

## 硬约束
- 禁止连接数据库、禁止建表、禁止写 pb_*.txt
- 禁止在任何情况下写入 photoRoot 下的 photo 目录
- local_settings.py 不入库（.gitignore 已忽略），必须提供 .example
- 不引入 SQLAlchemy（本项目用原生 sqlite3 + 代码生成器）
- 不引入 pymysql / cryptography（那是 MySQL 才需要）
- 配置项集中，禁止散落在业务模块里硬编码

## 验收清单（请逐条实际运行验证，不要只写代码就宣称通过）
1. python -m venv code/.venv 并按 requirements.txt 装依赖成功
2. python -c "import" 全部新增模块无报错
3. 用默认配置运行，能打印出：
   photo     = d:\PhotoLib\photo
   thumb     = d:\PhotoLib\thumb
   database  = d:\PhotoLib\db\photolib.db
4. 改 local_settings.py 的 PHOTO_ROOT 后再跑，路径随之变化，无需改任何业务代码
5. 把 THUMB_ROOT 故意设成 photo 的子目录，validate_layout 能抛异常
6. pytest code/src/test 通过

## 输出格式（本步结束后必须给出）
1. 改动文件清单（新增/修改，逐个列路径）
2. 验收结果（上面 6 条逐条：命令 + 实际输出或结论）
3. 遗留问题与需要我决策的点

有问题随时提，不要自行假设需求。
```

---
---

# 步骤 2 · SQLite 运行层 + 代码生成器 + 建库

```text
【photo-browser · 步骤 2/12 · SQLite 运行层 + 代码生成器 + 建库】

## 目标
打通「表定义 txt → 代码生成器 → 运行层 sqliteHandle → 真实建库」全链路。

## 前置
步骤 1 已完成并验收通过（配置与路径解析可用）。

## 必须先读的项目文档
- plan/开发计划.md 第二节（三层结构）、第 6.1 节（类型映射）、第 6.2 节（PRAGMA）
- plan/数据库设计.md（8 张表字段、§1.5 类型映射、§五 索引清单、§八 生成器工作流）
- 参考工程（**只读参考，不要直接复制粘贴）：
  - d:/home/lianyi/git/stock_rotation_strategy/src/common/sqliteHandle.py   ← SQLite 运行层参照
  - d:/home/lianyi/git/stock_rotation_strategy/src/database/mysqlCodeGenerator.py  ← 生成器结构参照
  - d:/home/lianyi/git/contentHub/code/src/database/*.txt  ← 表定义书写规范参照

## 第一件事：确认表定义已定稿（9 张表）
表定义**已是最终态**，跑生成器前逐条核对，不一致先改 `.txt`（唯一数据源），**禁止只改生成产物**：
- 9 个文件：pb_family / pb_person / pb_person_category / pb_photo / pb_face / pb_person_centroid / pb_photo_person / pb_scan_job / **pb_review_log**
- 每个文件首行必须是 `recID INT AUTO_INCREMENT PRIMARY KEY COMMENT '记录ID'`（9 个都是 INT，**没有 BIGINT**）
- `pb_face` 含 `isConfirmed`（归属是否经人工确认）+ **`isStranger`**（是否标记为陌生人）两个独立字段
- `pb_person_centroid.bucketKey` 允许特殊值 **`ALL`**（兜底桶，不分桶）；`sampleCount` 只统计**人工确认**样本
- `pb_photo_person.source`：`0` 自动归属未确认 / `1` 人工确认或改判
- `pb_review_log` 是第 9 张表（纠错审计与撤销依据），字段见 `plan/数据库设计.md` §4.9

## 本步产出文件
1. code/src/common/sqliteHandle.py
   - open_db(db_path, read_only=False)：row_factory=sqlite3.Row、check_same_thread=False
   - 读连接与写连接**各自**执行同一套 PRAGMA：
     journal_mode=WAL / foreign_keys=ON / synchronous=NORMAL / busy_timeout=5000 / temp_store=MEMORY / cache_size=-64000
   - executeRead / executeWrite / fetchAll / fetchMany(2000) / fetchOne
   - 占位符转换：SQL 里 %s → ?（防注入）；禁止字符串拼接值
   - 事务：executemany 批量写 + 显式 commit/rollback
2. code/src/database/sqliteCodeGenerator.py
   - 读 pb_*.txt（单字段一行：名称 类型 [NOT NULL|UNIQUE|NULL|DEFAULT x] COMMENT '...'）
   - 类型映射（**顺序敏感，INT 规则必须排在 BIGINT 之前**）：
     INT AUTO_INCREMENT PRIMARY KEY -> INTEGER PRIMARY KEY AUTOINCREMENT
     BIGINT/INT/SMALLINT/TINYINT     -> INTEGER
     VARCHAR/CHAR/MEDIUMTEXT/TEXT    -> TEXT
     DECIMAL/FLOAT/DOUBLE            -> NUMERIC
     MEDIUMBLOB                      -> BLOB
   - 保留 #common begin/end 区段（通用 insertTableGeneral / updateTableGeneral / chkTableExist）
   - 各表生成：create_pb_xxx / query_pb_xxx / insert_pb_xxx / update_pb_xxx / delete_pb_xxx
   - 建表时一并输出索引（清单照 plan/数据库设计.md §五，命名 idx_<表>_<字段>，**含 4 个部分索引**：
     `pb_face(personCode IS NULL)` 待确认队列、`pb_face(personCode, isConfirmed) WHERE isConfirmed=0 AND isStranger=0` 「我不同意」列表、
     `pb_review_log(isRevertible) WHERE isRevertible=1 AND revertedByLogCode IS NULL` 撤销候选、`pb_photo(movedToPhotoCode)`）
   - 产物落 code/src/database/auto_generated/sqliteCommon.py
   - 注意：SQLite 无 VARCHAR 类型，VARCHAR(n) 的长度 (n) 不被强制，业务层自行校验
   - **`.txt` 里的 `UNIQUE` 关键字不写进列定义**，由生成器按 §五 转成命名唯一索引 `idx_<表>_<字段>`（避免同一份唯一性生成两次）
3. code/src/database/auto_generated/sqliteCommon.py   ← 生成产物，禁止手工改
4. code/src/tools/build_db.py
   - 按 pb_*.txt 建库建表建索引；缺目录自动创建（db 目录）
   - 支持重复执行（幂等）
   - 支持 `--migrate`：比对 .txt 与实际列，只 `ALTER TABLE ADD COLUMN` 补缺的（**不动任何一行**），不删列不改类型
   - 支持 `--db <路径>` 指向临时库（**校验用**，不碰正式库）

## 硬约束
- 业务层禁止裸 SQL（本步只生成数据访问层，不写业务）
- 不建物理外键；关联一律用业务编码 photoCode / personCode
- 不引入 SQLAlchemy；本项目以 数据库设计.md 为准（MVP_plan.md 里的 SQLAlchemy 片段已废弃）
- 生成物只能由生成器产生，禁止手工编辑 auto_generated/
- 不写真实照片数据，本步只建空表结构

## 验收清单（逐条实际运行验证）
1. 运行生成器，auto_generated/sqliteCommon.py 成功产出，且文件头标注「自动生成，请勿手改」
2. python code/src/tools/build_db.py 建库成功，d:\PhotoLib\db\photolib.db 存在
3. PRAGMA 查询确认 **9 张表**齐全，索引齐全（含 4 个部分索引）
4. PRAGMA table_info(pb_photo) 显示 recID 类型为 INTEGER 且 pk=1
5. PRAGMA table_info(pb_face) 确认存在 `isConfirmed` 与 `isStranger` 两列，默认值均 0
6. PRAGMA journal_mode 返回 wal；PRAGMA foreign_keys 返回 1
7. 重复执行 build_db.py 不报错、不重复建表（chkTableExist 幂等）
8. `--migrate` 在一个缺列的旧库副本上跑通：只加列、不删数据（前后行数一致）
9. 写一条测试记录走通用 insert → query → update → 删除，验证 %s→? 转换与 blob 读写正常
10. PRAGMA integrity_check 返回 ok

## 输出格式
1. 改动文件清单
2. 验收结果（8 条逐条给命令与实际输出）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 3 · 扫描器（遍历 / hash / EXIF / 去重 / 批次限流）

```text
【photo-browser · 步骤 3/12 · 扫描器】

## 目标
把 photo 目录变成库里的结构化事实数据：遍历、双 hash、EXIF/年份、去重判定、增量扫描、批次限流与断点续扫。

## 前置
步骤 2 已完成（8 张表已建、sqliteCommon 可用）。

## 必须先读的项目文档
- plan/开发计划.md 第 3.1 节（扫描链路）与第 3.3 节（单写入者）
- plan/数据库设计.md §4.4 pb_photo 字段含义
- plan/MVP_plan.md 的 S2 章节

## 本步产出文件
1. code/src/processor/scanner/walker.py
   - 递归遍历 photoDir，扩展名白名单过滤
   - relPath（相对 photo 根，正斜杠，**保留磁盘原值不改写**）
   - relPathHash = sha256(规范化后的相对路径)；规范化 = 统一 /、去 ./、Unicode NFC，仅用于算 hash
   - fileHash = 流式 SHA-256，**分块 8MB**，禁止整文件读入
2. code/src/processor/scanner/meta.py
   - Pillow 读 EXIF：takenAt(DateTimeOriginal, UTC)、width/height/orientation、cameraModel、lat/lon
   - shotYear 识别优先级：EXIF → 文件名（识别 mmexport* 13 位毫秒时间戳、常见日期格式）→ mtime（最不可靠）
   - 截图类文件名（Screenshot* / 截图*）→ shotYear = NULL（不参与跨桶比对）
   - GPS 逆地理：reverse_geocoder 若导入失败或无数据，placeName 留空并记录一次 warning，**不得报错中断**
3. code/src/processor/scanner/runner.py
   - 增量三路判定：
     relPathHash 未命中                       → 新增
     relPathHash 命中且 fileHash 相同          → 跳过（幂等）
     relPathHash 命中但 fileHash 不同          → 更新元数据，人脸需重提取
     fileHash 命中但 relPathHash 未命中        → 标记「移动/重命名」，**不自动改路径**
     fileHash 命中且另一条 relPathHash 不同    → isDuplicate=1 + dupOfPhotoCode
   - 库中存在但磁盘找不到 → isMissing=1，**不删记录**（可能只是移动硬盘没插）
   - 批量 upsert：executemany，每 500 条提交一次
   - 进度写 pb_scan_job：processedCount/addedCount/skippedCount/duplicateCount/pendingCount/lastCursor
   - **批次限流**：累计处理到 batchSize（默认 100）即 jobStatus=PAUSED 并停下，等待「继续下一批」，从 lastCursor 续扫
4. code/src/schedule/scanScheduler.py
   - 后台任务调度与状态流转：IDLE → RUNNING → (PAUSED → 继续 → RUNNING) / DONE / FAILED
   - jobCode 幂等；异常写 errMsg 并置 FAILED
5. code/src/tools/scan_cli.py（或并入 main/cli.py）
   - 命令行入口：--root 指定扫描根（缺省用 photo_dir()）、--batch-size、--resume <jobCode>
6. code/src/test/：normalize_relpath、relPathHash 幂等、fileHash 分块一致性、增量三路判定的单测

## 硬约束
- **单写入者**：本步可以单进程；涉及 CPU 密集的读图时，子进程绝对不能连数据库，结果经 Queue 回主进程（步骤 5 才真正用到进程池）
- photo 目录**绝对只读**：不写、不删、不改名
- 事务粒度 500 条；中断后可续扫，不重复入库
- 不要在本步做人脸识别或缩略图生成（步骤 4、5 负责）

## 验收清单（准备一个小测试集：约 300 张，含子目录 4–5 级、含重命名、含重复内容、含缺 EXIF 的图）
1. 首次全量扫描：无遗漏、无重复入库
2. 立即第二次扫描：addedCount = 0（幂等性）
3. 新增 100 张：只处理这 100 张，其余跳过
4. 把某个文件改名：识别为「移动」而非「新增+删除」，且不自动改 relPath
5. 同一张图复制到另一个路径：被标记 isDuplicate=1 且 dupOfPhotoCode 正确
6. 临时移走一个子目录再扫描：对应记录被标 isMissing=1 而**没有被删除**
7. batchSize=100：处理到 100 张自动 PAUSED，点「继续」从 lastCursor 接着扫，最终 DONE，计数正确
8. EXIF 缺失的图走文件名/mtime 兜底；截图类 shotYear 为 NULL
9. 库中的 relPath 与磁盘实际路径逐字一致（未被规范化改写）
10. 单测全部通过

## 输出格式
1. 改动文件清单
2. 验收结果（10 条逐条给命令与实际输出/数据）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 4 · 缩略图与原图文件服务

```text
【photo-browser · 步骤 4/12 · 缩略图与原图文件服务】

## 目标
让前端能快速看图：缩略图按需生成 + 落盘缓存到 d:\PhotoLib\thumb\，原图接口支持 Range，并搭起最小 FastAPI 实例。

## 前置
步骤 3 已完成（库里已有照片记录与 fileHash）。

## 必须先读的项目文档
- plan/开发计划.md 第 3 节（数据流）、第 6.3 节（性能）、第 6.4 节（原子写）
- plan/数据库设计.md Q-5（缩略图存文件系统不入库）
- plan/照片管理方案_开源调研与自研设计.md 3.9（API 清单）

## 目录约定（已定稿，不得改动）
photoDir\   只读原图
thumbDir\   生成物，可随时重建
  ├─ thumbs\<fileHash[:2]>\<fileHash>_<size>.webp     size ∈ {200, 400, 800}
  └─ faces\<faceCode[:2]>\<faceCode>.jpg              160px（步骤 5 用）
路径必须**可推导**（pb_photo 没有 thumbPath 字段），由 fileHash + size 计算。

## 本步产出文件
1. code/src/processor/media/thumbStore.py
   - THUMB_SIZES = (200, 400, 800)
   - thumb_relpath(file_hash, size=400) -> "thumbs/<xx>/<hash>_<size>.webp"
   - face_relpath(face_code) -> "faces/<xx>/<faceCode>.jpg"
   - write_atomic(abs_path, data)：先写 <name>.tmp，再 os.replace；失败清理 tmp，**绝不留下半文件**
   - ensure_bucket_dir(relpath)：按需创建分桶目录
2. code/src/processor/media/thumbMaker.py
   - Pillow 打开原图 → 应用 EXIF orientation → 缩放到目标宽 → 存 WebP（质量 80 左右）
   - 保持宽高比；不改动原图任何字节
   - make_thumb(photoRow, size)：命中磁盘直接返回，未命中则生成后原子写
   - make_thumbs_bulk(photoRows, workers)：批量生成，**用进程池**（CPU 解码密集）
3. code/src/processor/media/faceCropper.py
   - crop_from_bbox(原图路径, bbox, outSize=160) -> bytes：按归一化 bbox 裁剪并保存 JPEG
   - bbox 越界要夹紧，不能抛异常导致整张照片失败
4. code/src/main/app.py（最小实例）
   - FastAPI()，挂载 /api 路由与静态资源
   - **只绑 127.0.0.1**（uvicorn host="127.0.0.1"）
5. code/src/api/static.py
   - GET /api/thumb/{photoCode}?size=400
     - 命中磁盘直接返回 FileResponse；未命中按需生成
     - 带 ETag（用 fileHash+size）与 Cache-Control: max-age
     - 支持 If-None-Match 返回 304
   - GET /api/original/{photoCode}
     - **必须支持 Range 请求**（解析 Range: bytes=start-end，返回 206 + Content-Range / Accept-Ranges: bytes）
     - MIME 按扩展名；支持 HEAD
   - GET /api/face/{faceCode}：人脸裁剪图（步骤 5 才有数据，本步先留好接口）
6. code/src/tools/gen_thumbs.py
   - 批量生成缩略图：--size 400 --workers 8 --limit N --resume
7. code/src/test/test_thumb_store.py：路径可推导性、原子写、分桶目录

## 硬约束
- photoDir 绝对只读
- 缩略图**不入库**（pb_photo 无 thumbPath 字段，路径必须能算出来）
- 缩略图必须原子写，禁止半文件被前端读到
- 批量生成用进程池；单张按需生成用线程池
- 服务只绑 127.0.0.1，不开 0.0.0.0
- 本步不做人脸识别

## 验收清单
1. 对测试集批量生成 400px 缩略图成功，文件落在 thumbDir\thumbs\<xx>\ 下，目录分桶生效
2. 二次请求同一张缩略图，响应头含 ETag，带 If-None-Match 返回 304，且**不重复解码原图**
3. GET /api/original 带 Range: bytes=0-1023 → 返回 206 与正确 Content-Range / Content-Length
4. GET /api/original 无 Range → 200 完整文件
5. 生成过程中强制中断，检查磁盘上没有残留 .tmp 半文件
6. 手动删除某个缩略图后再请求 → 能自动重新生成
7. 单张 400px WebP 体积约 25KB 量级（抽样 20 张统计）
8. 全程只读 photoDir：校验 photoDir 内文件数与总字节数扫描前后完全一致

## 输出格式
1. 改动文件清单
2. 验收结果（8 条逐条给命令与实际输出）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 5 · 人脸引擎（检测 + 特征 + 质量过滤）

```text
【photo-browser · 步骤 5/12 · 人脸引擎】

## 目标
从照片中稳定抽出人脸与 512 维特征，写入 pb_face，并做人脸裁剪图。

## 前置
步骤 4 已完成（thumb 目录与文件服务可用）。

## 必须先读的项目文档
- plan/开发计划.md 第 3.2 节（识别链路）、第 3.3 节（单写入者）
- plan/数据库设计.md §4.5 pb_face 字段
- plan/MVP_plan.md 的 S3 章节
- 参考（可选，只读）：C:/Users/NINGMEI/WorkBuddy/selfDevelop/photoapp/tools/verify_accuracy.py ← S0 实测脚本，质量过滤阈值与统计口径以它为准

## 本步产出文件
1. code/src/engine/face/engine.py
   - 封装检测 + 提取，统一输出 (faceCode, bbox归一化x,y,w,h, detScore, poseYaw, posePitch, embedding)
   - insightface FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])，prepare(ctx_id=-1, det_size=(640,640))
   - **中文路径安全读图**：cv2.imread 在 Windows 读不了非 ASCII 路径 → 必须 np.fromfile + cv2.imdecode（这是 S0 脚本已踩过的坑）
   - embedding 存 float32[512] 小端 2048 字节，已 L2 归一化
   - 若 insightface 装不上：降级为裸 onnxruntime 加载 SCRFD + ArcFace，接口保持一致
2. code/src/engine/face/pool.py
   - ProcessPoolExecutor 封装
   - **子进程绝对不能连数据库**：只接收 (图片路径, ...) 返回特征数组，结果经 Queue 回主进程
   - 模型在子进程内懒加载一次（不要每张都重新加载）
3. code/src/engine/face/faceStore.py
   - 写 pb_face：faceCode 幂等键、photoCode、bbox、detScore、poseYaw、posePitch、quality、embedding(BLOB)、shotBucket、isConfirmed=0
   - 同步更新 pb_photo.faceCount 与 scanState（0→1）
   - 裁剪图落 thumbDir\faces\<xx>\<faceCode>.jpg（复用步骤 4 的 faceCropper + write_atomic）
4. code/src/tools/backtest_s0.py
   - 用 S0 的 100 张验证集（10 人 × 10 张，目录名 = 人名）跑本引擎，输出准确率/耗时统计
   - --root 参数指定验证集目录（不写死路径）

## 质量过滤（提取阶段做掉，丢弃不入库）
- detScore < 0.6
- 人脸框短边 < 64 px
- |poseYaw| > 45°
- 一张图内取面积最大的人脸为主脸，其余按 detScore 降序保留（全部存库）

## 硬约束
- **单写入者**：特征提取在子进程，写库只在主进程单线程批量提交（否则 database is locked）
- onnxruntime 必须 CPU 版，禁止 onnxruntime-gpu
- photoDir 绝对只读
- embedding 必须 float32[512] 小端，落 MEDIUMBLOB
- 单张处理耗时需统计（验收 <1s CPU）
- 本步不做分桶匹配（步骤 6）

## 验收清单
1. backtest_s0.py 在 100 张验证集上跑通，识别准确率与 S0 结果一致（±2%）
2. 单张平均耗时 <1s（CPU），输出 p50/p95
3. 质量过滤生效：构造小脸(<64px)、大角度侧脸、低分检测样本，确认被丢弃且不入库
4. pb_face 落库正确，embedding 长度 = 2048 字节
5. pb_photo.faceCount 与 pb_face 实际条数一致，scanState 由 0 变 1
6. 人脸裁剪图落在 thumbDir\faces\<xx>\ 下且肉眼位置正确
7. 代码走查确认：子进程内没有任何数据库连接/导入
8. 中文路径照片能正常处理（用中文文件名 + 中文目录各测一张）

## 输出格式
1. 改动文件清单
2. 验收结果（8 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 6 · 分桶 + 质心 + 匹配决策

```text
【photo-browser · 步骤 6/12 · 分桶 + 质心 + 匹配决策】

## 目标
把「这张脸是谁」变成可解释的三段式判定：自动归属 / 待人工确认 / 进聚类。

## 前置
步骤 5 已完成（pb_face 有数据且 embedding 可读）。

## 必须先读的项目文档
- plan/开发计划.md 第 3.2 节（识别链路）
- plan/数据库设计.md §4.6 pb_person_centroid、§六 D-3/D-5
- plan/MVP_plan.md S3 的分桶规则与匹配决策

## 本步产出文件
1. code/src/engine/match/bucket.py
   - bucket_key_adaptive(shot_year, birth_year=None) -> str
     age = shot_year - birth_year
     age <= 18  → 从 birth_year 起每 3 年一桶   （0–18 岁 3 年一桶）
     age >  18  → 从 birth_year+18 起每 10 年一桶（18+ 10 年一桶）
   - birth_year 未知 → 降级 bucket_key_equal(shot_year)：等宽 5 年
   - shot_year 未知 → 返回空/NULL，不参与跨桶比对（截图类）
2. code/src/engine/match/centroid.py —— **质心防污染是本步的重点**
   - 按 (personCode, bucketKey) 归一化均值向量
   - ⚠️ **只用 `pb_face.isConfirmed=1` 的人工确认样本计算质心**，自动归属的样本一律不入（否则误认样本拉偏质心 → 越错越错）
   - `sampleCount` = 参与计算的**确认样本**数
   - **`ALL` 兜底桶**：bucketKey='ALL' 表示「该人全部确认样本、不分桶」。某年代桶确认样本 <3 时用它兜底，保证早期样本不足仍能自动归属
   - 该人总确认样本 <3 → **不启用任何质心**（不参与自动匹配，其脸全部走待确认队列）
   - `recompute_person(personCode)`：重算该人**全部**桶（含 ALL），是唯一写质心的入口；确认/改判/拆分/合并后调用
   - `load_centroids(personCode, bucketKeys)`：**按候选桶惰性加载**（DR-12：10 万 × 512 float32 = 205MB，全量加载已超内存预算；本项目本来就只取相邻三桶）
3. code/src/engine/match/matcher.py
   - match(face)：
     候选桶 = [B0-1, B0, B0+1] 中所有有样本的桶 **∪ {ALL}**
     score = **max**(cosine(face, centroid[bucket]))   ← 取 max，不取 mean
     score >= T_HIGH → 自动归属（**`isConfirmed` 保持 0**，`pb_photo_person.source=0`）
     T_LOW <= score < T_HIGH → 待人工确认，记 Top-5 候选
     score < T_LOW → 未知人脸，进聚类（步骤 7）
   - 批量比对用 numpy 矩阵乘（暴力搜索，不引 ANN 索引）
   - Top-5 候选排序稳定（同分按 displayName）
4. code/src/processor/review/assigner.py —— **改判入口（纠错核心，DR-16）**
   - `assign(faceCode, personCode)` 首次确认：写 `pb_face.personCode` + `isConfirmed=1`，写 `pb_photo_person`（linkKey 幂等，source=1），重算该人全部桶
   - `fix(faceCode, action, personCode=None)` **改判**，`action ∈ {assign / unknown / stranger}`：
     · `assign` 改到别人 → 删旧 `linkKey`、写新 `source=1`、**重算原人 *与* 新人** 全部桶
     · `unknown` 置为未知 → `personCode=NULL`、`isConfirmed=0`、删旧 `linkKey`、重算原人
     · `stranger` 标记陌生人 → `personCode=NULL`、`isStranger=1`、删旧 `linkKey`、重算原人
   - `batch_fix(faceCodes[], action, personCode)` 同一 `clusterCode` 批量（一次点击解决 N 张）
   - **每次操作必须写一条 `pb_review_log`**（`logCode` 幂等键、`opType`、`fromPersonCode`/`toPersonCode`、`similarity`、`faceCount`）
   - 每次操作后**同步刷新** `pb_photo.faceCount` 与相关计数
5. code/src/processor/review/merger.py
   - `merge(fromPerson, toPerson)`：迁移 `pb_face.personCode`、`pb_person_centroid`、`pb_photo_person`、`avatarFaceCode`；写 `pb_review_log`（`isRevertible=1`）
   - `split(faceCode, newPersonCode=None)`：从某人拆出 → 写日志（`isRevertible=1`）
   - `undo(logCode)`：只允许撤销 `isRevertible=1 AND revertedByLogCode IS NULL` 的记录；反向恢复人脸归属与关联行，**重算双方质心**，回填 `revertedByLogCode` 并写一条 `opType=UNDO`
6. code/src/test/：桶边界单测（0/3/17/18/19/70 岁、跨年、shotYear=NULL）+ 质心防污染单测 + 改判副作用单测

## 硬约束
- 相邻三桶取 **max**，不是 mean
- **质心只用人工确认样本**；`ALL` 兜底桶只在桶样本不足时启用
- **自动归属时 `isConfirmed` 保持 0** —— `isConfirmed` 只表示「经人工确认」，不参与「是否进待确认队列」的判定
- 四态互斥（由 `personCode` / `isConfirmed` / `isStranger` 推导，**不要新增 matchType 字段**）：
  未归属 = `personCode IS NULL AND isStranger=0`｜自动归属未确认 = `personCode NOT NULL AND isConfirmed=0 AND isStranger=0`｜人工确认 = `isConfirmed=1`｜陌生人 = `isStranger=1`
- 向量 BLOB 为 float32[512] 小端
- 不引入 FAISS / sqlite-vec 等向量库（暴力比对即可）
- 阈值写进 config/basicSettings.py，可切换「保守/激进」两套
- 匹配只写库不做文件 IO，不写 photoDir

## 验收清单
1. 桶边界单测全部通过（列出实际测试用例与结果）
2. 相邻三桶 max 生效：构造一个在相邻桶分数更高的人脸，确认取到较大值
3. **某桶只有 1 张确认样本时回退 `ALL` 桶并能匹配成功**（构造数据验证）
4. **总确认样本 <3 的人不参与自动匹配**（其脸全部落到待确认）
5. **质心防污染**：往某桶塞 1 张属于他人的**自动**样本（`isConfirmed=0`），`recompute_person` 后 `centroid` BLOB 与 `sampleCount` **均不变**
6. 三段式决策可复现：同一批人脸跑两次结果完全一致
7. 切换「保守/激进」阈值，结果按预期变化且可回退
8. **10 万人脸下按候选桶惰性加载，内存占用 <100MB**（不是靠全量加载）
9. `assign` 后该 person 的 `pb_person_centroid` 立即更新（`sampleCount` 变化可见）
10. **改判副作用齐全**：`fix('assign')` 后原人与新人的质心都重算、旧 `linkKey` 已删、新 `source=1` 已写、`pb_review_log` 新增一条
11. `fix('stranger')` 后该脸既不进待确认队列也不进聚类
12. `merge`/`split` 后 `pb_photo_person` 与 `pb_face` 一致无孤儿；`undo` 能完整还原
13. 用 S0 验证集做回归：分桶后的 FR 明显低于不分桶

## 输出格式
1. 改动文件清单
2. 验收结果（13 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 7 · 聚类与待确认数据

```text
【photo-browser · 步骤 7/12 · 聚类与待确认数据】

## 目标
把识别不出来的脸自动聚成「未命名人物」候选，并让待确认队列的数据口径准确（不把自动归属的脸算进来）。

## 前置
步骤 6 已完成（matcher 能输出「未知人脸」集合，assigner/merger 已就位）。

## 必须先读的项目文档
- plan/开发计划.md 第 3.2 节、DR-16（纠错闭环）
- plan/数据库设计.md §4.5 pb_face（**四态语义表**）、§4.9 pb_review_log、§四 D-4

## 本步产出文件
1. code/src/engine/cluster/dbscan.py
   - 纯 numpy 实现 DBSCAN（余弦距离 = 1 - cos），可选 sklearn 作为加速后端（若装则用 sklearn，不装则用自实现）
   - 参数默认 eps=0.45、min_samples=3（集中放 basicSettings.py）
   - **只对未归类集合聚类**：`personCode IS NULL AND isStranger=0 AND delFlag='0'`
   - clusterCode 幂等：同一簇重跑应得到相同编码（编码规则：`cluster_<hash>`，hash 基于簇内 faceCode 排序后计算）
   - 代表样本：每簇取 detScore 最高的人脸作为簇代表，便于前端展示
2. 队列查询（写入生成层或 processor）—— **两个队列，口径不同**
   - **待确认队列** = `personCode IS NULL AND isStranger=0 AND delFlag='0'`
     每个条目返回 Top-N 候选人物（复用步骤 6 的 matcher，含头像 faceCode、displayName、similarity）
   - **「我不同意」列表** = `personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0 AND delFlag='0'`
     按 `photoCode` 分组，供前端展示「机器认的，可否决」；返回该人当时的 `similarity`
   - ⚠️ **绝对不要用 `OR isConfirmed=0`**：自动归属的 `isConfirmed` 本来就是 0，用它会把**全部自动归属**扫进待确认队列（10 万张 ≈ 4~8 万条，队列爆炸）
   - ⚠️ **`pendingCount` 口径要拆开**：`pb_scan_job.pendingCount` 记的是「疑似移动/重命名待确认张数」（步骤 3 的 `movedToPhotoCode`），**与待确认人脸数是两个不同计数**，不要混用同一个字段；待确认人脸数请直接 SQL count
3. code/src/tools/cluster_cli.py：--eps 0.45 --min-samples 3 [--dry-run] [--rebuild]
4. code/src/test/test_dbscan.py：合成数据验证聚类正确性、clusterCode 幂等性

## 硬约束
- 不新建待确认表（`personCode` 可空即表达，数据库设计 D-4）
- 不新建 `pb_face_cluster` 表（用 `clusterCode`，Q-3）
- 不新建「我不同意」队列表（用 `personCode + isConfirmed` 组合表达，Q-3b）
- 已归类人脸绝不能被重新聚类；**`isStranger=1` 的脸既不进队列也不进聚类**
- 聚类只读 embedding，不写 photoDir；不写 `pb_review_log`（聚类是机器行为，不是人工操作）
- eps / min_samples 必须可配置

## 验收清单
1. 对未归类集合聚类，簇数量合理；输出每簇代表样本的 contact sheet（临时拼图即可）供抽查
2. 肉眼抽查 3 个簇，确认簇内确实是同一个人
3. 重跑聚类，`clusterCode` 完全稳定（不产生新编码）
4. 已归类人脸（`personCode` 非空）数量在聚类前后不变
5. **待确认队列条数 = `SELECT COUNT(*) FROM pb_face WHERE personCode IS NULL AND isStranger=0 AND delFlag='0'`**（直接 SQL 数，不读 `pendingCount`）
6. **构造 10 条自动归属数据（`personCode` 非空 + `isConfirmed=0`），待确认队列仍为 0 条**（DR-16 修正点，必须验证）
7. `isStranger=1` 的脸既不在待确认队列也不在「我不同意」列表
8. 「我不同意」列表条数 = `personCode IS NOT NULL AND isConfirmed=0` 的行数
9. 单测通过；聚类耗时可接受（给出实际秒数与规模）

## 输出格式
1. 改动文件清单
2. 验收结果（9 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 8 · 联系人导入（CSV / vCard）

```text
【photo-browser · 步骤 8/12 · 联系人导入（CSV / vCard）】

## 目标
把通讯录变成「可被识别归属」的已知人员档案。本步只建档案，不关联照片。

## 前置
步骤 2 已完成（sqliteCommon 可用）。本步与 3–7 无强耦合，可独立完成。

## 必须先读的项目文档
- plan/数据库设计.md §4.1 pb_family、§4.2 pb_person、§4.3 pb_person_category
- plan/MVP_plan.md S4 章节
- plan/UI/photo-browser UI 设计.md（联系人导入的幂等与「半自动」原则）

## 本步产出文件
1. code/src/processor/contact/csv_import.py（**主力通道**）
   - 支持 Outlook 导出的 CSV（列名中英文容错：DisplayName/显示名、Categories/类别、E-Mail/Email、Mobile/电话、Birthday/生日、Surname/姓氏、Title…）
   - 编码自动嗅探（utf-8-sig / gbk / utf-8），失败明确报错
   - 幂等：优先 vCardUid；无 UID 用 displayName 匹配。命中则 UPDATE，不新增
   - Categories 列按分号/逗号分隔 → 拆成多行写 pb_person_category
   - **重复导入不得残留旧分类行**（先清该 person 的旧 category 再写新的）
   - source=1（vCard/CSV 导入）
2. code/src/processor/contact/vcard_import.py
   - vobject 解析 vCard 3.0 / 4.0
   - KIND:group 识别为家庭组 → 写 pb_family，并把成员的 familyGroupCode 指向它
   - 注意 Outlook 只认单联系人 .vcf（支持批量文件目录）
3. code/src/main/cli.py
   - 命令：import-csv --file <path>；import-vcard --path <dir|file>；list-families
   - 导入原件归档复制到 dbDir\imports\（带时间戳，保留原文件不改）
   - 导入结果摘要：新增 N / 更新 M / 分类行 K / 家庭组 J
4. code/src/test/test_csv_import.py：幂等（重复导入 0 新增）、分类拆分、家庭组提示

## 硬约束
- **只建档案，绝不关联照片**（照片归属由人脸识别负责）
- FamilyName 相同 → **提示**用户是否合并家庭组，**不自动建**（半自动原则）
- 不做 Microsoft Graph API（P2，本步不做）
- 不写 photoDir
- 导入不阻塞、不做长事务（分批提交）

## 验收清单
1. 导入 200 人 CSV：姓名/姓氏/关系/生日/邮箱/电话/分类全部正确落库，在D:\temp\contacts 有真实vCard和csv文件
2. 重复导入**同一文件**：新增 0 条，已存在记录被更新，pb_person_category 无残留旧行
3. 按 category='family' 查询结果正确
4. vCard 4.0 的 KIND:group 能识别为 pb_family，并把成员挂上 familyGroupCode
5. 相同 FamilyName 的记录只给「建议合并」提示，不自动建家庭组
6. GBK 编码的 CSV 能正常导入（不乱码、不报错）
7. 导入原件已归档到 dbDir\imports\
8. 单测通过

## 输出格式
1. 改动文件清单
2. 验收结果（8 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 9 · 后端 API 全量

```text
【photo-browser · 步骤 9/12 · 后端 API 全量】

## 目标
提供前端可直接消费的完整 HTTP 接口。

## 前置
步骤 3–8 已完成（扫描/缩略图/人脸/匹配/联系人都可用）。

## 必须先读的项目文档
- plan/照片管理方案_开源调研与自研设计.md 3.9（API 清单）
- plan/MVP_plan.md S5
- plan/UI/photo-browser UI 设计.md（页面需要哪些数据）

## 本步产出文件
0. **code/src/database/queryCommon.py（落地时补，不在原清单里）**
   生成层 `sqliteCommon.query_pb_*` 只能按「主键 + 4 个业务码等值 + IS NULL」过滤，
   **表达不了区间比较 / 分组聚合 / COUNT(DISTINCT ...)**，而硬约束又写着
   「业务层禁止裸 SQL」。没有这个出口，`/api/timeline` 的
   `shotYear BETWEEN` 与 `GROUP BY` 就只能靠拼字符串实现（= 绕过纪律）。
   本模块 = **手写但受约束的只读查询出口**：只允许 SELECT/WITH、
   值一律 `%s` 占位、表名与列名对生成层白名单校验、聚合必须给别名。
1. code/src/api/dto.py：统一响应结构
   - 分页统一 { page, size, total, items }；错误统一 { code, message }
2. code/src/api/browse.py（P0）
   - GET /api/timeline：按年月分组，首屏 <500ms（3 万张）
   - GET /api/photos：分页 + 筛选（personCode / shotYear 区间 / placeName / hasFace / isDuplicate）+ 排序
   - GET /api/persons：人物列表（含 photoCount、年代跨度）
   - GET /api/persons/{personCode}：详情（含各年代桶分组统计）
   - GET /api/photos/{photoCode}：详情（含 faceCount、出现的人、EXIF 信息）
3. code/src/api/scan.py（P0）
   - POST /api/scan/start { rootPath?, batchSize? } → jobCode
   - GET /api/scan/status/{jobCode} → 真实计数（processed/added/skipped/duplicate/pending/jobStatus/lastCursor）
   - POST /api/scan/resume/{jobCode} → 继续下一批
   - POST /api/scan/stop/{jobCode}
   - GET /api/scan/jobs → 任务列表
4. code/src/api/review.py（P1 · 纠错闭环，DR-16）
   - GET  /api/review/pending?page&size            待确认队列 = `personCode IS NULL AND isStranger=0`
   - GET  /api/review/disputed?page&size&groupByPhoto  **「我不同意」列表** = `personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0`，按 photoCode 分组
   - GET  /api/review/pending/count               侧栏角标，返回 `{ pendingCount, disputedCount }` **两个数**
   - PUT  /api/review/{faceCode}/assign { personCode }        首次确认
   - POST /api/review/batch-assign { faceCodes[], personCode } 批量确认
   - POST /api/review/fix { faceCode \| faceCodes[], action, personCode? }   **改判**，action ∈ `assign` / `unknown` / `stranger`
   - POST /api/review/batch-fix { faceCodes[], action, personCode }  同一 clusterCode 批量
   - POST /api/review/merge { fromPersonCode, toPersonCode }
   - POST /api/review/split { faceCode, personCode? }
   - POST /api/review/undo { logCode }              **撤销**一条 isRevertible=1 的 SPLIT/MERGE
   - GET  /api/review/log?faceCode=&photoCode=      操作历史（排障用）
   - GET  /api/review/revertible                   当前可撤销的操作列表
   > `fix` / `batch-fix` / `undo` **只做编排**：删旧 linkKey、写新 source=1、重算原人与新人全部桶、同步 faceCount、落 `pb_review_log` 全部交给步骤 6 的 `assigner`/`merger`/`centroid`，**api 层不重复实现这套逻辑**
5. code/src/api/contacts.py（P1 · 联系人维护，DR-17/18/19）
   - GET  /api/contacts?page&size&category&familyGroupCode&keyword&delFlag
         分页 + 筛选；每条返回 `photoCount` / `faceCount` / `confirmedFaceCount` / `birthday`
   - POST /api/contacts                界面新建手工档案（source=0，vcardUid 空）
   - PATCH /api/contacts/{personCode}   **部分字段**更新
         · **检测 birthday 是否变更** → 变更则 `centroid.recomputePerson()`，响应带 `centroidRebuilt: true`
         · 改 displayName 撞 UNIQUE → `409 + {"code":"DUPLICATE_DISPLAY_NAME",
           "existing":{personCode, displayName, photoCount, faceCount}}`，**不写库**
         · 改 familyGroupCode 要写进 pb_person；分类走 pb_person_category 增删
   - GET  /api/contacts/{personCode}/impact
         停用影响面预览 `{photoCount, faceCount, confirmedCount, centroidCount, pendingAfter}`
   - POST /api/contacts/{personCode}/disable?confirm=true
         **不带 confirm=true 只返回影响面、不写任何一行**（前端两段式）
         执行顺序固定：① `centroid.dropPerson()` ② 人脸全部退回未归属
         （`personCode=NULL, isConfirmed=0`）③ 关联行按纪律 ③ 存废
         ④ 落 `pb_review_log`（`opType=DISABLE`）
   - POST /api/contacts/{personCode}/enable    恢复 delFlag，响应带提示「需重新确认人脸」
   - GET  /api/contacts/duplicates             同名/疑似同人合并候选
   - POST /api/contacts/import/csv（multipart，`?dryRun=true` 只回计划不写库）
   - GET/POST/PATCH /api/families[/{familyCode}]   家庭组维护（P2 可延后）
   - GET  /api/places（供筛选与后续地图）
   > ⚠️ **不提供任何导出接口**（DR-17：不做联系人 CSV / 聚类 CSV / vCard 导出）
   > ⚠️ **不提供 DELETE**（DR-19：只允许停用。硬删会作废该人的全部确认工作）
   > 写入一律复用 `contactCommon`（`makePersonCode` / `makeDisplayName` / `splitCategories` /
   > `_syncCategories`）与 `assigner` / `centroid`，**api 层不另写一套写库路径**
6. code/src/main/app.py：路由统一注册、静态资源、CORS 仅本机
7. code/src/test/：关键接口冒烟测试 + 纠错接口副作用测试 + contacts CRUD 测试 + 地点字典测试
8. **code/src/database/pb_place.txt + 重跑生成器（落地时补）**
   `/api/places` 原来是一次全表 `GROUP BY placeName`（10 万张实测 p50 95ms，
   且随库线性增长），且同一地点的多种写法无处收敛、地图要中心点得现算。
   加 `pb_place` 地点字典表；派生逻辑落在 **code/src/processor/place/placeStore.py**
   （`rebuildPlaces` 幂等全量复算 / `listPlaces` 纯读 / `liveAggregatePlaces` 降级）。
   ⚠️ 加表要**同时**改生成器的 7 处（TABLE_ORDER / TABLE_CN / INDEX_SPEC /
   EXPECTED_INDEX_NAMES / CONFLICT_COLUMNS / QUERY_FILTER_FIELDS / ORDER_FIELDS）
   **和** `plan/数据库设计.md` §四、§五；改完**必须 diff 重生成的产物**，
   确认只多了那一张表（生成器在表元数据过期时会重写整个 `#common` 区段，
   不 diff 就不知道它有没有顺带改别的）。老库用 `tools/build_db.py` 补建
   （幂等，只建缺的表：实测 `created=['pb_place']`，其余 9 张 `existed`）。

## 硬约束
- 业务层**禁止裸 SQL**，全部经 sqliteCommon
- 分页统一 page/size/total；**不要**用 OFFSET 深分页（时间线用游标或按年月分段）
- 扫描是后台任务 + 内存状态 + jobId 轮询，**不得阻塞请求**
- 服务只绑 127.0.0.1
- 合并/拆分/软删等不可逆操作：服务端做参数复述所需的查询，前端负责二次确认
- 原图零风险：任何接口都不得写/删 photoDir
- **改判必须重算原人 *与* 新人**的质心（只重算一边 = 越改越乱）
- **每个写操作都要落 `pb_review_log`**，不允许静默改库
- **联系人字段分两类**（DR-18）：`birthday` 改 → 必须 `recomputePerson()`；
  其余（`displayName` / `familyName` / `familyGroupCode` / `relation` / `email` / `phone` / 分类）改了什么都不用做
- **停用必须先删质心再退人脸**（DR-19）：只删质心留 `personCode` 会让脸变成
  「人工确认但无质心」，既不在待确认也不在「我不同意」，**从所有队列里消失**

## 验收清单
1. uvicorn 启动后访问 /docs 能看到全部接口且可试调
2. 触发扫描 → 轮询 /api/scan/status/{jobCode} 能看到**真实计数**推进，到 batchSize 转 PAUSED
3. 10 万张照片下 GET /api/timeline 首屏响应 <500ms（给出实测毫秒数）
4. GET /api/photos 筛选 personCode / 年份区间 / hasFace 均正确
5. 待确认队列分页正确，Top-5 候选按相似度降序
6. assign 后该 person 的 photoCount 立即变化；确认后质心即时重算
7. merge 双方数据正确合并，无孤儿记录
8. **改判 `fix(action='assign')`：原人与新人的 `pb_person_centroid` 都已重算**（查 `modifyYMDHMS` 或对比质心内容，只重算一边算不合格）
9. **改判后无残留旧 `linkKey`**；`pb_photo_person` 中该照片只关联新 person
10. **`fix(action='stranger')` 后该脸不在待确认队列、也不在「我不同意」列表**
11. **`/api/review/disputed` 返回的条数 = `personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0` 的行数**（自动归属的脸确实在这里，不在待确认队列里）
12. **`/api/review/undo` 能把一次 merge 完整还原**（人脸归属 + `pb_photo_person` + 双方质心），且 `pb_review_log` 回填了 `revertedByLogCode`
13. **每次纠错调用都新增一条 `pb_review_log`**，`opType` / `fromPersonCode` / `toPersonCode` / `faceCount` 正确
14. `GET /api/review/pending/count` 返回 `{ pendingCount, disputedCount }` **两个数且不相等**（构造数据验证）
15. 确认无接口修改 photoDir（代码走查 + 文件数/字节数前后比对）

### contacts CRUD（DR-17/18/19）
16. `GET /api/contacts` 分页与筛选（category / familyGroupCode / keyword / delFlag）都正确，每条带 photoCount / faceCount / confirmedFaceCount
17. `POST /api/contacts` 新建成功后，`GET` 能查到，且 `source=0`、`vcardUid` 为空
18. **`PATCH` 改 `email`（非 birthday）→ 响应 `centroidRebuilt` 为 false/absent，且该人 `pb_person_centroid` 的 `modifyYMDHMS` 完全没变**
19. **`PATCH` 改 `birthday` → 响应 `centroidRebuilt=true`，且该人全部桶已重算**
    （构造：改生日前后 `pb_face.shotBucket` 应随新桶键变化 —— ⚠️ 注意 `shotBucket` 存在脸表上，
    改生日后**是否要重算脸的 `shotBucket`** 要给结论：要么同步刷 `pb_face.shotBucket`，
    要么在质心侧按新 birthday 重新分桶。**当前 `centroid.recomputePerson` 是按脸表里已存的
    `shotBucket` 分桶的，不重刷的话改生日等于没改** —— 这是本条最容易做漏的地方，
    请核实并给出正确做法）

    > **✅ 步骤 9 已给出结论（DR-18 补充）**：**必须刷 `pb_face.shotBucket`**，且是三步固定顺序
    > ① 写 `birthday` ② `rebucket.rebucketPerson()` 刷桶 ③ `centroid.recomputePerson()`。
    > 只做 ③ = 用旧桶键重算同样的样本 -> 质心与改前**逐位相同**。
    > ⚠️ 挑构造用的生日要挑**真会挪桶**的：成年桶起点 = `出生年+18+k×10`，
    > `1985` 与 `1965` 年生的人拍 2013 年照片**都**落在 `2003-2012`。
    > 实测可用 `1985-03-07 -> 1970-01-01`（2013/2016 的样本全挪到 `2008-2017`）。
    > ⚠️ `ALL` 兜底桶由确认样本集合决定，改生日不改变样本集合 -> 它**理应逐位不变**，
    > 不能拿它当「已重算」的证据。
20. **`PATCH` 改 `displayName` 撞已有的同名人员 → 返回 409 + `existing` 里带对方 personCode/照片数/脸数，且库里那行 `displayName` 未被改动**
21. **`GET /{personCode}/impact` 返回的四个数字与实际一致**（用 SQL 逐个核对）
22. **`POST /disable` 不带 `confirm=true` → 只返回影响面，`pb_person` / `pb_face` / `pb_person_centroid` / `pb_review_log` 四张表**行数全部不变**
23. **`POST /disable?confirm=true` 执行后**：
    - 该人 `pb_person_centroid` 行数 = 0
    - 该人所有脸变成 `personCode IS NULL AND isConfirmed=0`（**进待确认队列**）
    - **没有任何脸落在「既不在待确认、也不在「我不同意」、personCode 非空」的盲区**（写 SQL 验证）
    - `pb_person.delFlag='1'`
    - `pb_review_log` 新增一条 `opType='DISABLE'`
    - `pb_photo_person` 无孤儿行（跑 `assigner.verifyLinks()`，`clean=True`）
24. `POST /enable` 恢复后 `delFlag='0'`，响应带「需重新确认人脸才能自动匹配」的提示字段
25. `GET /api/contacts/duplicates` 能报出疑似同人（**同一中国手机号的不同写法**）

    > **⚠️ 原文「构造两个同名人员」按字面做不到，步骤 9 已改口径。**
    > `pb_person.displayName` 上有 **UNIQUE 索引**（`idx_pb_person_displayName`），
    > 而 `contactCommon.makeDisplayName()` 在导入时已在加 `(2)` 后缀避让 ——
    > **任何两个 personCode 的 displayName 不可能字面相同**，写第二个直接
    > `UNIQUE constraint failed`。所以 `duplicates` 的三类判据改为：
    > | 判据 | 强度 | 说明 |
    > |---|---|---|
    > | `nameKey()` 归一后相同、**原文不同** | 最强 | 多空格 / 全角半角 / NFKC。真正会发生且危险的「重名」：一次手敲、一次导入 |
    > | `email` 相同 | 强 | `email` 无唯一约束，可以重复 |
    > | `phone` 归一后相同 | 中 | 见下面 phoneKey 口径 |
    > 同一对可同时命中多个判据 -> 输出 `reason`（最强那个）+ `reasons`（全列表）。
    >
    > **`phoneKey` 的口径（用户确认）**：`-` 一律去掉；**不带 `+` 或 `00` 开头的默认是中国国内号（手机号 11 位）**。
    > 实测 11 种写法归一到同一个键：`13800138000` / `138-0013-8000` / `138 0013 8000` /
    > `+8613800138000` / `8613800138000` / `008613800138000` / `+86 138-0013-8000` / …
    > ⚠️ **只归一前缀、不合并号段**：`+86285187018`（少一位的截断脏数据）与 `02885187018`
    > 归一到**不同的键**，故意不配成对 —— 宁可漏一次合并提示，也不能把两个真人并成一个。
    > ⚠️ 顺手修掉一个老 bug：旧实现只判「位数 > 11 且以 `86` 开头」，`0086...` 的 `00` 前缀没被吃掉，
    > 于是 `008613800138000` 变成 `08613800138000`（14 位），**跟谁都配不上对且不报错**。
26. `POST /api/contacts/import/csv?dryRun=true` 返回计划（新建/更新/跳过数 + 警告列表）且**不写任何一行**
27. **走查确认：没有任何导出接口**（`/api/contacts/export` 之类一律不存在，返回 404 或 405）
28. 走查确认：**没有 DELETE /api/contacts/{personCode}**（404 或 405）

## 输出格式
1. 改动文件清单
2. 验收结果（上面 28 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点（**特别是第 19 条 birthday 与 `pb_face.shotBucket` 的关系**，务必给出结论）

---

## ⚠️ 步骤 9 收尾时**新增**的两类验收（原清单没有，实测发现问题后补的）

### A. 扫描的可中断性（原清单只验了"计数推进"，没验"停止真的能停"）

29. **`POST /api/scan/stop` 在**全树遍历阶段**就能中止，不必等遍历走完**
    背景：`stopEvent` 原来只在**逐文件循环**里被检查，而那段循环在
    `walker.listPhotoFiles()` **之后**才进入。10 万张库/慢盘上，遍历本身要 2~20s
    —— 用户按了停止，界面十几秒没反应。现在 `walker.listPhotoFiles(shouldStop=...)`
    每 64 个目录查一次，响应降到毫秒级。
30. **遍历中止时游标与全部计数"一律不动"，且中止**必须抛异常**、绝不返回部分清单**
    ⚠️ 部分清单会被 `runBatch` 当成全量真值：`totalCount = len(items)`（偏小 ->
    进度永远到不了 100%）、`bisect_right(relPaths, lastCursor)` 落在错位置 ->
    **续扫永久漏一段且不报错**。所以「中止」与「返回」必须互斥（`walker.WalkAborted`）。
    对照：**批内**停止（已处理了一部分）则**必须**推进游标 —— 分界是「有没有开始处理文件」。

### A′. 落地时还会踩到的四处（步骤 9 实测记录）

35. **`GET|HEAD` 同函数注册会让 `/docs` 每次启动刷 3 条 `Duplicate Operation ID`**
    三种写法只有第三种对（**踩过坑**）：
      · `api_route(methods=["GET","HEAD"])` -> HEAD 可用，但两个方法共用一个
        `operationId` -> 启动刷警告 + `/docs` 多 3 组无意义的 HEAD 条目；
      · 只写 `@router.get(...)` -> **HEAD 直接 405**。⚠️ Starlette 1.7 的 `Route`
        **不再**为 GET 自动补 HEAD（老版本会补，网上大量"只写 GET 就行"的说法
        在这个版本上是错的）。**这条最坑：`/docs` 看着完全正常，只有真发 HEAD 才暴露。**
      · **两个装饰器**（`@router.get(...)` + `@router.head(..., include_in_schema=False)`）
        -> HEAD 可用 + OpenAPI 里只有 GET + 零警告。✅
    必须留一条用例真发 HEAD 请求（判据：`!= 405` 且与 GET 状态码一致），
    因为「OpenAPI 里没有 HEAD」这件事**查不出** HEAD 到底能不能用。

36. **方法不匹配（405）不能落到 `PARAM_INVALID`(400) + 英文原文**
    实测：把 `POST /api/places/rebuild` 发成 GET，拿到
    `400 {"code":"PARAM_INVALID","message":"Method Not Allowed"}` ——
    前端会按「你参数写错了」走表单校验分支，而真相是**调用姿势**错了。
    修法：单独一个 `CODE_METHOD_NOT_ALLOWED`(405)，message 中文且**写出允许的方法**
    （从 `Allow` 头取）；5xx 归 `INTERNAL` 而不是 `PARAM_INVALID`。

37. **`insertID()` 经实测**不需要**为并发加锁（⚠️ 差点白改一轮）**
    一度怀疑 `Cursor.lastrowid` 是连接级的（= 多线程下 A 会拿到 B 的 recID，
    然后拿 B 的 id 去 update，静默改错行），并据此写了一版「锁内拍快照」的实现。
    **实测否掉了这个怀疑**（CPython 3.13.14 / sqlite3 3.53.1）：
      | 场景 | 行为 |
      |---|---|
      | 另一个游标在同一连接上 INSERT | **本游标的值不变** -> **游标级，无竞态** |
      | commit / UPDATE / DELETE 之后 | 都不变 |
      | **executemany 之后** | **`None`**（CPython 明确不更新） |
    => 快照实现被**撤掉**（修的是一个不存在的问题，给共用 DB 层加投机代码更贵）。
    保留两条**特征化钉子**（标注清楚它们不是"修了 bug 的证据"）：
    钉「每个线程拿到自己的 id」（谁把它改成读共享状态就红 —— 实测 3/3 红）
    与「非 INSERT 的写不影响上一次插入」。并把最后一行那个 `executemany` 的坑
    写进 `insertID()` 的 docstring。

38. **收尾顺序**（`main/app.py` 的 lifespan）：先 `resetScheduler()`（等后台扫描线程
    退出）-> 再 `thumbMaker.shutdownPool(wait=True)`（等在途缩略图做完）->
    最后 `closeDb()`。原先是 `wait=False`，等于让正在用连接的工作线程撞上 `close()`；
    单线程测试看不出来，但退出时可能留半个写盘文件 / 半条记录。

### B. `sqliteHandle` 的并发隔离（步骤 9 才第一次出现"后台线程 + 请求线程"并存）

> ⚠️ **这里实际上是两个叠在一起的 bug。第一个修完，第二个才会露出来。**

31. **bug ①：两个线程交替 `execute`/`fetch`，各自必须拿到自己的结果集**
    背景：原实现 `execute` 持锁、`fetchAll/fetchMany/fetchOne/fetchValue` **不持锁**
    且共用**同一个游标**。单线程下行为与"每线程游标"逐位相同，**所以旧的单线程测试
    全绿也证明不了并发正确**。实测症状：`fetchall` 与 `execute` 并发作用于同一 cursor ->
    **进程级 access violation**（Windows 直接杀进程，exit 0xC0000005，
    traceback 只有 `runner.loadIndex <- query_pb_photo` 的片段，看不出根因）。
    修法：游标跟着线程走（`threading.local()`）。

32. **bug ②（更隐蔽）：CPython `sqlite3` 的 per-connection 预处理语句缓存**
    「每线程一个游标」只解决了一半。同一连接上**文本相同的 SQL 会被缓存并复用
    同一个 `sqlite3_stmt`**，于是两个游标共享同一个底层语句 ——
    后 execute 的那个会 `sqlite3_reset` 掉共享语句，**把先 execute 的那个待取的行丢掉**：

    ```
    线程A: executeRead("SELECT COUNT(*) ... delFlag = ?")  -> 已 step 出 1 行
    线程B: executeRead(同一段 SQL 文本)                     -> reset 掉共享语句
    线程A: fetchValue()                                     -> None（静默！无任何报错）
    ```

    **实测证据**（`code/.probe_min.py`，已删）：
    | 条件 | 结果 |
    |---|---|
    | 6 线程跑**同一段 SQL 文本** | **第 1 次尝试就复现**（`code=-1` 有结果集、`description` 非空、`rowcount=-1`、同游标两次 `fetchone()` 都 None、另起独立查询却正常、`lastErr=''`） |
    | 每线程 SQL 文本各加几个空格（= 不同缓存键） | **60 次零失败** |

    ⇒ 语句缓存是**连接级**的，所以 **只有"每线程一个连接"才治本**。
    写路径**不用**开每线程连接：每条写语句都在 `_lock` 里一次做完
    （bind + step 到底，不迭代取行），不存在"待取的行被别人 reset"；
    且写受「单写入者」硬约束保护。
    ⚠️ 代价必须核算：`PRAGMA cache_size = -64000`（≈64MB）是按"一条主连接"定的，
    几十条线程连接 × 64MB = 上 GB。所以**每线程读连接单独设 `cache_size = -8000`（8MB）**
    （常量 `sqliteHandle.THREAD_READ_CACHE_SIZE`）。
    实测（10 万张，请求线程走 8MB 缓存）性能**反而更好**：
    首屏 `/api/timeline` p50 **91.39ms -> 55.67ms**（少了跨线程锁竞争），
    `/api/places` 176ms -> 95ms。

    ⚠️ **证据要求（两个 bug 都要）**：必须有自己的用例，不能靠"跑了一遍别的用例没炸"；
    且**用例本身要被验证过有效**：
      · 把 `_readConn()` 改回共享 `dbR` -> 4/4 并发用例变红
        （含直接回归钉 `test_sameSqlTextAcrossThreadsDoesNotLoseRows`）
      · 把 `_readCursor()` 改回共享游标 -> 同样变红

### C. 接口层的三个补丁

32. **`POST /api/scan/start` 的 `rootPath` 只允许**精确等于 `paths.photo_dir()`**
    （`ScanScheduler.createJob` 只判 `os.path.isdir`，于是 `{"rootPath": "C:\\"}`
    会真的去遍历整个 C 盘，把库外图片按错误 `relPath` 写进 `pb_photo`。
    它不碰 `photo\`（原图只读没破），但会把库搞脏。子目录也不行 ——
    `makeRelPath` 相对于传入的 root，同一张照片在不同 root 下 `relPathHash` 不同
    -> **判定成两张不同的照片，重复入库**。多盘照片库请改 `PHOTO_ROOT` 配置。）
33. **`GET /api/timeline?unknown=1`**：让 `takenAt` 为空的照片（截图类）**有入口能翻到**。
    原来只给 `unknownCount` 一个数字 —— 这批照片既不在年表里、也不在按年翻页的结果里，
    等于在 UI 上**消失**。⚠️ **这不是边角数据**：本项目正式库实测
    **2137 张里有 711 张读不出年月（33%）** —— 截图、微信图片、无 EXIF 的扫描件。
    只给一个数字等于让三分之一的内容在时间线上不可达。
    与 `year`/`month` 互斥；排序只能用 `recID`（`takenAt` 是 NULL，排不了时间）。
34. **`GET /api/review/disputed` 必须给**两套**总数**：
    `total`（人脸行数，= 验收第 11 条口径）+ `photoGroupTotal`（`COUNT(DISTINCT photoCode)`，
    分页口径）。前端画分页器**只能用后者** —— 拿 `total` 算总页数会翻进空页
    （37 张脸可能只分布在 5 张照片里），而这个错不报错、只表现为"最后一页是空的"。
㉓ **`GET /api/places` 读 `pb_place` 字典表，且必须能降级**
    · 字典有数据 -> `source="dictionary"`（只扫几十~几百行）；
    · 字典空或表不存在 -> `source="lib"` 实时聚合（全表 GROUP BY，慢但拿得到数据），
      响应里说明原因 + 提示调 `POST /api/places/rebuild`；
    · **两条路径下同一地点的 `placeCode` 必须一致** ——
      否则用户在地点下拉里选中的编码在 rebuild 之后失效，
      已缓存的前端状态/URL 全部对不上，而接口不报错。
    · ⚠️ `GET` 是**纯读**：`rebuild` 必须走显式的 POST
      （在 GET 里顺手回填 = 让只读接口有副作用，代理缓存/客户端重试都会替用户写库）。
㉔ **`POST /api/places/rebuild` 幂等 + 不清表**
    第二次跑 `created=0 / updated=N`；`source=1` 的手工行**不能被复算打回 0**；
    已从 `pb_photo` 消失的地点**行要留、`photoCount` 要归零**
    （不归零就会在地图上留一个「0 张却显示 300 张」的幽灵点）。

---
---

# 步骤 10 · 前端骨架 + 双主题

```text
【photo-browser · 步骤 10/12 · 前端骨架 + 双主题】

## 目标
从零搭起可运行的 Vue3 前端工程，并建立「浅色淡雅 + 跟随系统」的主题体系。本步页面只需骨架与真实文案，不接业务数据。

## 前置
步骤 9 已完成（接口可用）。本步需先核实本机 Node.js / npm 版本。

## 必须先读的项目文档
- plan/开发计划.md「设计方向 / 主题策略 / 页面与布局 / 交互与动效 / 响应式与无障碍」五节
- plan/UI/photo-browser UI 设计.md（信息架构、页面清单、组件规范、状态机、ASCII 布局）

## 技术栈（固定，不要替换）
Vue 3 + Vite + Pinia + Vue Router + Tailwind CSS + Element Plus + axios
- Tailwind CSS 3.4.17（不要用 v4，配置方式不同）
- 图标用 lucide-vue-next
- 构建产物 dist/ 不入库

## 本步产出文件（code/webserver/ 下）
1. 工程配置：package.json、vite.config.js（含 server.allowedHosts）、tailwind.config.js、postcss.config.js、index.html、.env.development（VITE_API_BASE=http://127.0.0.1:8000）
2. src/main.js、src/App.vue
3. src/styles/tokens.css —— **双套 CSS 变量**
   浅色（默认）：底 #F7F8FA / 卡片 #FFFFFF / 边框 #E8EBF0 / 主色 #5B8DEF / 次主色 #4A76D8 / 浅主色 #EAF1FE
                文本 #1F2430 / #5A6274 / #8A93A6；功能色 成功 #2E9E6B、警告 #E8A33D、危险 #E05A5A、信息 #5B8DEF、点缀 #F4A7B9
   深色（@media prefers-color-scheme: dark）：底 #14161A / 卡片 #1E2128 / 边框 #2B2F38，语义色用同色系微调版
   字体 Inter + PingFang SC；标题 24px/600，副标题 16px/600，正文 14px/400
   圆角：卡片 12 / 按钮 8 / 缩略图 8；间距 4px 基准；阴影极轻
4. src/styles/element-theme.css —— Element Plus 在 html.dark 下的 CSS 变量覆盖
5. src/styles/main.css —— Tailwind 三层 + 基础样式
6. src/utils/theme.js —— 三态切换（浅色 / 深色 / 跟随系统），写 localStorage，首屏读取避免闪白
7. src/router/index.js —— 8 个路由：/ /photos /photos/:photoCode /people /people/:personCode /review /scan-jobs /settings
8. src/api/request.js（axios 封装：baseURL、统一错误提示、拦截器）+ src/api/{scan,browse,review,contacts,static}.js（薄封装）
9. src/store/{photos,persons,review,scan,settings}.js（Pinia，先建结构与状态，不接全部接口）
10. src/components/layout/{AppSidebar,AppTopbar}.vue
    - Sidebar 220px，6 项：概览 / 照片流 / 人物库 / 待确认（带角标①）/ 扫描任务 / 设置；<1024 收窄为图标
    - Topbar 56px：面包屑 + 页面标题 + 搜索 + 待确认角标 + **主题三态切换开关**
11. src/views/{Overview,Photos,PhotoDetail,People,PersonDetail,Review,ScanJobs,Settings}View.vue —— 8 个页面骨架
    - **写真实中文文案，禁止 Lorem/占位文案**
    - 每页含该页的关键区块占位（统计卡、筛选条、网格、候选列表、任务表格、参数表单等），并接上真实布局结构

## 硬约束
- **不要暗色单一主题**（原 UI 稿已推翻）：默认浅色，跟随系统，另提供手动开关
- Element Plus 必须在两套主题下都可读，不能出现隐形文字
- 照片呈现区不叠加任何滤镜（此步无真实照片，但布局要为「照片优先」留位：大留白、低饱和界面）
- 所有交互元素用语义标签（button / input），不用 div 模拟
- 单文件不超过 300 行；样式统一用 Tailwind 类
- 本步不接业务接口数据（骨架 + 文案即可），数据接入在步骤 11、12

## 验收清单
1. 先报 Node.js 与 npm 版本（本机此前未核实）
2. npm install 成功；npm run dev 起得来；npm run build 成功
3. 系统切换深色 → 页面自动变深色，无需刷新
4. 手动三态切换（浅/深/跟随）都生效，刷新后仍保持
5. Element Plus 的 Button/Table/Input/Dialog/Tabs/Drawer/Toast/Pagination 在浅色与深色下均可读（逐个目视检查）
6. 8 个路由都能跳转，刷新不 404
7. <1024 时 Sidebar 收窄为图标；<768 时布局可用
8. 全流程键盘可达，焦点可见
9. 语义色（成功/警告/危险/信息）在两套主题下对比度均达 WCAG AA

## 输出格式
1. 改动文件清单
2. 验收结果（9 条逐条给命令/截图说明/实际输出）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 11 · 照片流 + 照片详情 + 待确认队列

```text
【photo-browser · 步骤 11/12 · 照片流 + 照片详情 + 待确认队列】

## 目标
打通主链路：看照片 → 看人脸 → 确认归属 → **发现认错了能改**。这是全项目最关键的一步。

## 前置
步骤 10 已完成（前端骨架与主题就绪）。

## 必须先读的项目文档
- plan/UI/photo-browser UI 设计.md 第 4.3/4.4/4.6 节（三个页面的 ASCII 布局）、第 1.2 节设计原则、第 6.2 节交互流程
- plan/开发计划.md 步骤 11 行的全部验收项、**DR-16（纠错闭环）**
- plan/数据库设计.md §4.5 四态语义表

## 本步产出文件
1. src/views/PhotosView.vue
   - 筛选条：人物 / 年份区间 / 地点 / 仅含人脸 / 重复 / 排序
   - 网格视图（默认）+ 时间轴视图（一键切换）
   - 网格只加载缩略图（/api/thumb），**点击才取原图**（/api/original）
   - 角标：👤n（含 n 张脸）/ ⚠重复 / 📷有 GPS
   - 滚动懒加载 + 骨架占位；分页信息（显示 1-60 条，共 N 条）
2. src/components/photo/PhotoThumb.vue
   - 1:1 或 4:3，圆角 8；hover 轻微上浮（2px / 120ms ease-out），角标淡入
3. src/views/PhotoDetailView.vue
   - 主图（支持 Range 预加载）+ 右侧信息栏：拍摄信息（时间/相机/尺寸/地点/文件 hash）
   - 「出现的人」列表可跳转人物详情
   - 操作：确认归属 / **改判（✗ 不是他）** / 标记重复 / 软删除（软删需 danger 二次确认）
4. src/components/photo/FaceBox.vue —— **纠错入口都在这里（DR-16）**
   - 绝对定位框 + 2px 描边；**已归属绿 / 待确认橙**
   - 悬停显示人名 + 相似度
   - **颜色 + 图标 + 文字三重编码**（灰度/色盲下仍可辨）
   - **三种描边样式区分归属来源**：实线 = 人工确认（`isConfirmed=1`）、**虚线 = 机器自动认的（`isConfirmed=0`，可否决）**、点线 = 待确认
   - **悬停出「✗ 不是他」按钮**（图标 + 文字，不只靠颜色）：点击开改判浮层 → 候选人物（带相似度）/ 新建人物 / 置为未知 / 标记陌生人
   - 改判浮层里也要有「就这张照片的其他脸也否决」的入口
5. src/components/photo/BucketTimeline.vue
   - 按年代桶分组的横向缩略图带；桶数自适应，空桶不显示
6. src/views/ReviewView.vue（**待确认队列，核心页**）
   - **双 Tab**：「待确认 N」（`/api/review/pending`）+「我不同意 M」（`/api/review/disputed`）
   - 顶部：剩余 N / 总数 M、跳过、忽略此人脸
   - 左侧未知人脸大图；右侧候选人物按相似度降序（头像 + 姓名 + 相似度 + 确认按钮）
   - 灰区条目橙色提示；候选旁可显示关系提示（如「兄妹，长相接近」，取自 relation 字段）
   - 底部：无匹配 → [新建人物] [标记为陌生人]
   - **键盘快捷键**：1/2/3 选候选、N 新建、S 跳过、I 忽略
   - **批量确认**：同一聚类簇下所有人脸一次全确认
   - 「合并」/「拆分」入口在手边，不藏三级菜单
   - **「我不同意」Tab**：按照片分组，每组支持**整张一键否决**（≤2 次点击）+ 簇内批量改判；每条显示「机器认成 X，相似度 0.62」与「去改判」
7. src/components/review/DisputedList.vue（「我不同意」列表）
8. src/components/review/CandidateRow.vue、src/components/review/ConfirmMerge.vue
   - ConfirmMerge：**复述双方姓名 + 照片数**，动词按钮「确认合并」，二次确认
9. src/components/common/FixFaceDialog.vue（改判浮层，三种 action 共用）
10. 对应 store 与 api 接入（photos / review）

## 硬约束
- **网格里绝不加载原图**
- 不确定就要问：灰区（`T_LOW`–`T_HIGH`）一律进队列，**不静默归属**
- 人脸框必须永远可见
- **「✗ 不是他」必须一次点击可达**（人脸框悬停即出），不许绕到人物详情 Tab2 —— 用户在照片流里看到认错的第一反应就是就地纠正
- **自动归属但未确认的脸要能被一眼识别**（虚线描边 + 角标），否则用户根本不知道哪些可以否决
- 不可逆操作（合并/软删/**标记陌生人**）必须参数复述 + 二次确认
- 原图零风险：UI 不提供任何编辑/覆盖/删除原图的入口
- 状态识别不得单靠颜色
- 侧栏角标分**两个数**（待确认数 / 我不同意数），不要合并成一个

## 验收清单
1. 10 万张照片滚动不卡（说明用了分页还是虚拟滚动，给出实测帧率或耗时）
2. 打开照片流时，Network 面板**只看到 /api/thumb**，没有 /api/original
3. 点击缩略图 → 进入详情 → 才发起 /api/original 请求；拖动滚动原图流畅（Range 生效）
4. 人脸框在**人工确认 / 自动归属 / 待确认**三种状态下颜色、描边样式、图标、文字都正确且可辨
5. 确认一张人脸归属后：该人照片数立即更新，队列自动前进到下一条
6. 批量确认 50 张同类人脸 ≤3 次点击
7. 键盘 1/2/3/N/S/I 全部生效（逐个测）
8. 合并弹窗正确复述双方姓名与照片数，取消无副作用
9. **人脸框悬停「✗ 不是他」→ 选候选 → 改判成功**：该人照片数与待确认数同时变化，「我不同意」列表少一条
10. **「我不同意」Tab 里整张照片一键否决 ≤2 次点击**，改后这些脸进入待确认
11. 标记陌生人有二次确认，确认后该脸从所有队列与聚类中消失
12. 侧栏角标两个数字（待确认 / 我不同意）分别正确、与接口一致
13. 网格 hover 微交互、懒加载骨架、角标淡入均正常
14. 浅色与深色两套主题下本页全部可读

## 输出格式
1. 改动文件清单
2. 验收结果（14 条逐条说明实测情况）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 12 · 人物库/详情 + 扫描台 + 设置 + 打磨

```text
【photo-browser · 步骤 12/12 · 人物库/详情 + 扫描台 + 设置 + 打磨】

## 目标
补齐剩余 4 个页面、备份脚本与收尾，完成里程碑 M4。

## 前置
步骤 11 已完成（主链路可用）。

⚠️ **下面 4 个视图文件步骤 10/11 已经建好并且接上了真实接口，本步是「补齐交互与打磨」，不是从零新建。**
动手前先读现有实现，再决定改哪里 —— 不要按「新建」的口径重写：
- `views/PeopleView.vue` / `PersonDetailView.vue` / `ScanJobsView.vue` / `SettingsView.vue` / `OverviewView.vue` —— 已存在，已有筛选条与列表骨架
- `components/common/PersonForm.vue` —— 已存在，`FixFaceDialog` 的「新建人物」在用（硬约束「不得有第二份表单」依然成立）
- `utils/faceState.js` —— 步骤 11 产出，**四态语义（色/描边/图标/文字）的唯一口径出口**。本步 Tab2 的「人工确认 / 自动归属」两段必须调 `faceStateOf()`，不要另写一套配色
- `components/photo/BucketTimeline.vue` —— 步骤 11 产出，人物时间轴直接复用
- `api/request.js` 的 `paramsSerializer` 已根治空 query 参数（只丢 `undefined`/`null`/`''`，`0` 与 `false` 原样发出），**不必再逐个 store 查 `''`**

## 必须先读的项目文档
- plan/UI/photo-browser UI 设计.md 第 4.2/4.5/4.7 节（概览、人物详情、扫描任务布局）、第 3.2 节页面树
- plan/开发计划.md 步骤 12 行验收项、第八节里程碑

## 本步产出文件
1. src/views/PeopleView.vue（人物库）—— **补齐，不是新建**
   - 人物卡片网格：头像 + 姓名 + 照片数 + 年代跨度
   - 筛选：分类（family/friend/colleague 芯片）/ 家庭组
   - 空状态引导（先导入联系人再扫描）
   - **质心健康度提示**（见硬约束）
2. src/components/common/PersonCard.vue —— **本步唯一需要新建的组件**。圆形头像，hover 上浮，点击进详情；**识别质量徽标**（见硬约束）
3. src/views/PersonDetailView.vue（人物详情）—— **补齐**
   - 头部：头像 + 姓名 + 家庭关系 + 分类 + 照片数
   - Tab1 时间轴：**按年代桶分组**的该人照片（复用 `BucketTimeline`）
   - Tab2 人脸样本：**分两段** —— 「人工确认 N」（实线）与「自动归属 M」（虚线，各带「✗ 移除」）；移除后该脸回到待确认队列。描边样式与文案走 `utils/faceState.js`，与照片详情页的 `FaceBox` 保持一致
   - 操作：合并到… / **撤销上次合并·拆分** / 改头像 / 编辑资料 / **操作历史**（查 `pb_review_log`，「这张脸当初怎么被认成这个人的」）
   - ⚠️ **撤销按钮只需接 UI**：`GET /api/review/revertible` 与 `POST /api/review/undo` 后端已通并实测过
     （能把 `pb_face` 归属 + `pb_photo_person` + 双方质心一起还原，`pb_review_log` 回填 `revertedByLogCode`）。
     界面上**必须**写明「撤销只能回到合并/拆分前的归属，质心的原值回不来」
4. src/views/ScanJobsView.vue（扫描任务）—— **补齐**
   - 新建扫描（选根目录 + 批大小，默认 100）
   - 任务列表：状态 / 本批进度（真实计数）/ 累计 / 操作
   - 状态色 + 图标 + 文字三重编码：RUNNING 蓝◐、PAUSED 橙⏸、DONE 绿✓、FAILED 红✕
   - 操作：开始 / **继续下一批** / 暂停 / 查看错误（展开原始 errMsg）
   - **显式展示「已暂停等待指示」**，不要假进度条
5. src/views/SettingsView.vue（设置）—— **补齐**
   - 照片根目录（只读展示）
   - 识别参数：T_high / T_low / 分桶策略（改动提示「需重新生成质心」）
   - 数据：重新生成质心（可选「仅用已确认样本」）/ 备份 / 恢复 / 关于
6. src/views/OverviewView.vue 补全（统计卡 + 最近入库缩略图行 + 扫描状态条 + 待确认入口 + **我不同意入口**）
7. 后端补齐（后端为主、前端为壳）
   - GET /api/places（**已存在**）、GET /api/map（Leaflet + 离线瓦片，可选功能）
   - 重复照片对比接口
   - 备份脚本 code/src/tools/backup.py：停服务 → 拷贝 db\ + thumb\ → 输出备份路径；另提供 restore
8. 重复照片视图（可选，若时间允许）

## 硬约束
- 人物时间轴必须**按年代桶分组**（S0 结论：分桶是刚需）
- 扫描台进度必须是真实计数，禁止假进度条
- 改阈值必须提示需重算质心，并提供触发按钮
- 备份 = 停服务 → 拷贝 `db\` + `thumb\`；**不要备份 photoDir**（原图不动）
- 不提供任何编辑/覆盖/删除原图的入口
- 移动端仅浏览 + 轻操作；扫描/合并/删除引导到桌面端
- **「撤销上次合并」只对 `isRevertible=1`（SPLIT/MERGE）开放**（DR-16）；撤销后必须重算双方质心
- **质心健康度提示**（DR-16）：某人「自动归属数 ≫ 人工确认数」时在人物卡片上提示「识别质量偏低，建议人工确认 N 张」—— 用数据引导用户去纠错，而不是等他自己发现

## 验收清单
1. 人物库卡片显示头像/姓名/照片数/年代跨度，分类与家庭组筛选正确
2. 人物详情时间轴按年代桶分组且空桶不显示
3. **Tab2 能一眼分出「人工确认」与「自动归属」两段**；移除自动样本后该脸回到待确认队列
4. **执行一次合并后可「撤销」**：人脸归属 + `pb_photo_person` + 双方质心全部还原，`pb_review_log` 回填 `revertedByLogCode`
5. **操作历史**能按人脸/照片查到历次 `ASSIGN/FIX/SPLIT/MERGE`
6. **质心健康度提示**在「自动 ≫ 确认」的人身上正确出现，在正常人物上不出现
7. 扫描任务：新建 → 运行 → 到 100 张自动 PAUSED → 点「继续下一批」→ 断点续扫 → DONE，全程计数正确
8. FAILED 任务能展开查看原始错误
9. 设置页改 T_high/T_low 有「需重算质心」提示，且能触发重算
10. 备份脚本产出完整备份（db + thumb），恢复后应用正常
11. 8 个页面在浅色/深色下全部可读，<1024 与 <768 断点正常
12. 全项目回归：主链路（扫描 → 浏览 → 确认 → **改判** → 人物时间轴）端到端走通
13. 输出 M4 自测清单：接下来一周你要用哪些功能、怎么记录问题

## 输出格式
1. 改动文件清单
2. 验收结果（13 条逐条说明实测情况）
3. 遗留问题与需要我决策的点
4. M4 一周自测建议清单
```

---
---

## 附录 A · 步骤与里程碑对照

| 步 | 主题 | 里程碑 | 状态 |
| --- | --- | --- | --- |
| **R2** | **分桶口径修复**（DR-20/21/22 自适应分桶从未生效） | — | ✅ **代码已落地**（`engine/match/rebucket.py` 的 `rebucketFace/rebucketPerson/rebucketAll`、DR-21 未归属脸候选桶放宽、DR-22「先刷桶再重算」顺序闸、`auditBuckets`；`test_rebucket.py` **36 用例全绿**）<br>⚠️ 但 **R2 的验收证据仍欠**：`auditBuckets` 改前/改后对照、`verify_bucket_gain.py` 的生产库 FR 实测、全库重算质心 —— 这三项报告没出 |
| **R** | **返工修正 1–6（纠错闭环 DR-16）** | — | ✅ 已完成（已并入步骤 6 / 7，不单列） |
| 1 | 工程基线与配置骨架 | — | ✅ 已完成 |
| 2 | SQLite 运行层 + 生成器 + 建库 | — | ✅ 已完成 |
| 3 | 扫描器 | **M1**（扫描幂等、去重准确、续扫可用） | ✅ 已完成 |
| 4 | 缩略图与原图文件服务 | — | ✅ 已完成 |
| 5 | 人脸引擎 | — | ✅ 已完成 |
| 6 | 分桶 + 质心 + 匹配 | — | ✅ 已完成 |
| 7 | 聚类与待确认数据 | **M2**（识别复现 S0、确认闭环生效） | ✅ 已完成（聚类 + 两个队列已实测；**M2 的「确认闭环」仍待人工确认 3 张脸**） |
| 8 | 联系人导入 | — | ✅ 已完成（CSV+vCard 双通道 + 家庭组 + 1012 张头像；**M2 待人工确认 3 张脸**） |
| 9 | 后端 API 全量 | **M3**（接口全可用、扫描可后台跑） | ✅ 已完成（`api/` 六个模块 + `database/queryCommon.py` 查询出口 + `processor/place/` 地点字典；**含 contacts CRUD**，DR-17/18/19。`pytest code/src/test` = **1153 passed / 0 failed**，`/docs` 可试调，扫描可后台跑）|
| 10 | 前端骨架 + 双主题 | — | ✅ 已完成（Vue3 + Vite + Tailwind + Element Plus 按需引入；`tokens.css` 单一口径出双主题，浅/深/跟随系统三态；`check-style-order.mjs` 门禁防 EP 覆盖顺序） |
| 11 | 照片流 + 详情 + 待确认队列 | — | ✅ 已完成（`PhotosView` / `PhotoDetailView` / `ReviewView` + 7 个新组件；**DR-16 纠错闭环全部落地**：`FaceBox` 三种描边样式 + 悬停「✗ 不是他」一次可达、`ReviewView` 双 Tab + 键盘 1/2/3/N/S/I + 批量确认、合并/拆分独立端点。`pytest` = **1175 passed / 0 failed**。⚠️ **撤销按钮的 UI 未接**，`/review/revertible` + `merger.undo` 后端已通） |
| 12 | 人物库/详情 + 扫描台 + 设置 + 打磨 | **M4**（自己真正用一周） | ⬜ 未开始（**下一步**）。5 个视图已存在，**本步是补齐交互不是从零新建**；需新建的只有 `PersonCard.vue`（`PersonForm.vue` 步骤 10 已产出）。**待接：撤销上次合并·拆分的 UI**（后端已通）、质心健康度提示、备份脚本 `tools/backup.py`、`/api/map`（可选）、重复照片对比 |

## 附录 B · 每步固定的输出格式

```
1. 改动文件清单（新增 / 修改，逐个列路径，标注新增还是修改）
2. 验收结果（该步验收清单逐条：执行的命令 + 实际输出或结论 + 数值）
3. 遗留问题与需要我决策的点（如果有拿不准的需求，不要自行假设，列出来问我）
```

> 提醒：新对话里请**只粘贴当前步骤的提示语**，不要把多步一起粘过去，否则上下文会过长导致遗漏约束。
> 当前应粘贴的是 **「步骤 12 · 人物库/详情 + 扫描台 + 设置 + 打磨」**（12 步里的最后一步）。
>
> **进入步骤 12 时要知道的现状**（都是步骤 10/11 实际做完后与本提示语原文有出入的地方）：
> - 步骤 12 产出清单里的 `PeopleView.vue` / `PersonDetailView.vue` / `ScanJobsView.vue` / `SettingsView.vue`
>   **步骤 10 已建好骨架、步骤 11 已接上真实接口**，本步是补齐交互与打磨，不是从零新建。
> - `PersonForm.vue` **已存在**（步骤 10 产出，`FixFaceDialog` 的「新建人物」在用）。硬约束「不得有第二份表单」仍然成立。
> - **撤销上次合并/拆分的 UI 还没做** —— 后端 `/review/revertible` 与 `merger.undo` 已通并实测（能把双方归属和质心一起还原），
>   但 `ReviewView` / `PersonDetailView` 里没有按钮。本步要接上，并且**必须在界面上说明「撤销只能回到合并/拆分前的归属，质心的原值回不来」**。
> - 照片级写操作（软删 / 恢复 / 标记重复）步骤 11 已加端点，语义是「只改库、不动磁盘、不删关联行、软删可恢复」。
> - 空 query 参数（`hasFace=''` 之类）已在 `api/request.js` 的 `paramsSerializer` 根治：
>   **只丢 `undefined`/`null`/`''`，`0` 与 `false` 原样发出**（`desc=0` 升序不能被当空值丢掉）。
>
> ⚠️ 与当前进度无关、但仍未补齐的一件事：**R2 的验收证据**。代码已落地并有 36 条用例兜着，
> 但下面三项报告一直没出，而它们是 **M2「识别复现 S0」** 的前提
> （桶口径说了算，不能只凭"代码写完了"就认为 S0 的结论能在生产库复现）：
> `auditBuckets()` 改前/改后完整对照、`verify_bucket_gain.py` 的生产库 FR 实测、
> 全库重算质心（`--all --recompute`）前后行数变化。
> 要补就单独贴 **「修正步骤 R2」**。
