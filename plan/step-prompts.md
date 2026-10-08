# photo-browser 分步开发提示语（12 步）

> 配套：`plan/开发计划.md`（总纲）｜`plan/数据库设计.md`（表定义）｜`plan/UI/photo-browser UI 设计.md`（信息架构）
>
> **使用方法**：每一步**单独开一个新对话**，把下面对应的提示语整段复制粘贴进去。
> 前一步验收未通过，不要开始下一步。
> 每步结束统一输出：**改动文件清单 + 验收结果 + 遗留问题**。
>
> ⚠️ **当前进度：12 步编码全部完成。M1~M3 已过；M4 = 自己真正用一周（步骤 12 已交付 2026-10-07）。**
> 进度以文末「附录 A」的状态表为准。**M4 剩下的不是继续写代码，而是真实使用一周** ——
> 一周自测清单见 `开发计划.md` 步骤 12 的输出与 §八 里程碑。
> 唯一还欠的技术性报告是 **R2 的验收证据**（见文末提醒），它不影响 M4 的使用。
>
> 历史：步骤 1–6 执行后核对发现两处返工，顺序是 **R2 → R → 步骤 7**：
> **R2 = 分桶口径修复**（`pb_face.shotBucket` 从来没被重写成自适应桶，**S0 的分桶结论一直在跑对照组**）
> → **R = 纠错闭环**（DR-16）→ 步骤 7。两项均已完成并入。
>
> **新增（2026-10-07）：地点维度**。用户提出「按人物选择去过地点的界面（人物 → 地点列表 → 时间线）」，
> 评估后拆成三步，**顺序 R3 → R4 → R5**：
> **R3 = 修 `(0,0)` 占位坐标**（DR-25，小步，独立，**必须最先做** —— 26 张照片的地点是错的"加纳"）
> → **R4 = 地点数据层**（DR-26/27/28/29：geohash 归并 + 目录名抽取 + `/api/places`，**不做界面**）
> → **R5 = 地点界面**（人物 → 地点列表 → 时间线，缓一步）。
>
> **新增（2026-10-08）：人物头像**。用户看 P-04 截图后提出两句 ——「人物库**最好中间显示照片**」
> 「在人脸样本可以**选择一个作为人物的默认图片**」，评估后合成一步 **R7**（DR-40/41）：
> **R7 = 人物头像**（卡片显示照片 + 人脸样本设默认）。**与地点线无依赖**，可任选顺序：
> 前半（卡片显示照片，DR-40）是**纯读取回退、零迁移**，单独做就已解决截图里的问题。

---

# 修正步骤 R3 · 修 `(0,0)` 占位坐标（DR-25）

> **什么时候做**：现在。地点功能的第一步，**也独立有价值**（26 张照片的地点现在是错的）。
> **为什么必须最先做**：错的 `placeName` 已经落库了，不清掉的话后面所有地点视图都被污染。

```text
【photo-browser · 修正步骤 R3 · 修 (0,0) 占位坐标】

## 目标
相机未定位时写的 `(0,0)` 占位坐标被当成真实 GPS，逆地理查到了几内亚亚，
导致 26 张照片的 `placeName` 落成「加纳」。本步修代码 + 清已落库的错数据。

## 前置
步骤 1–12 已完成。真实库：`d:\PhotoLib\db\photolib.db`（2137 张照片）。

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-25**
- plan/照片管理方案_开源调研与自研设计.md（`pb_photo.lat` / `lon` / `placeName` 字段语义）

## 必须先读现有代码
`code/src/processor/scanner/meta.py` 的 `reverseGeocode()`：
- 坐标合法性校验只有一条（约 331 行）：
  `if not (-90.0 <= latValue <= 90.0 and -180.0 <= lonValue <= 180.0): return None`
  → **`(0,0)` 完美通过**（它确实在合法范围内，只是不是真实位置）
- `readMeta()` 只在 `lat/lon` 非空时调 `reverseGeocode()`

## 实测事实（我已查过正式库，可复核）
- 照片总数 2137；有 GPS 91 张（4.3%）；有 placeName 91 张
- **恰好 `(0,0)` 的有 26 张**，它们的 placeName 全是 `GH, Western, Takoradi`
- 有效 GPS（剔除 `(0,0)`）**只剩 65 张（3.0%）**
- 这 26 张**已经落库**，后续不清就会被地点视图当成"加纳"

## 一、修代码（`meta.py`）

新增一个独立的判据函数（**不要把判断塞进 if 里，要能单测、要能复用**）：

```python
#: 占位坐标判据：(0,0) 及 |lat|<EPS / |lon|<EPS 一律视为"无定位"
PLACEHOLDER_EPS: float = 1e-4

def isRealCoordinate(lat, lon) -> bool:
    """真实 GPS 坐标？范围合法 **且** 不是占位值。
    ⚠️ (0,0) 在范围内但不是真实位置 —— 相机无定位时写的就是它。
    判据：lat/lon 任一为 None、任一非有限数、任一 |v| < PLACEHOLDER_EPS -> False"""
```

`reverseGeocode()` 与 `readMeta()` 都改用它。**`(0,0)` 一律返回 None（`placeName` 留空）**，
不要去猜"可能是哪个地方的 0"。

⚠️ 同时检查 `meta.py` 里还有没有别处也做了坐标合法性判断（grep `90.0` / `180.0`），
有的一并改，否则会出现"一条路径修了另一条没修"。

## 二、清已落库的错数据（**必做，否则修了代码库里还是错的**）

写一次性脚本 `code/src/tools/fix_placeholder_geo.py`：

```
--dry-run   只打印将要改的行（默认）
--apply     实跑
```

行为：
1. `SELECT photoCode, relPath, lat, lon, placeName FROM pb_photo WHERE <占位判据>`
2. **打印清单**（照片数 + relPath 前若干条），让人能确认这些确实是无定位的
3. `--apply` 时：**`placeName = NULL`**（⚠️ 走 upsert + `forceColumns`；
   `update_pb_photo` 写不进 NULL —— 这是 DR-16 里已踩过的坑，assigner 文件头有记录）
4. **`lat` / `lon` 是否也要清？请你给结论并说明理由** ——
   建议**保留** lat/lon（它们是 EXIF 里的原始事实，清了等于丢信息），
   只清 `placeName`（那是**推断出来的**结果）。但请你复核后给最终结论。
5. 跑完打印：清掉的行数、清理后 `placeName IS NOT NULL` 的总数

## 三、要不要顺带加一个「无定位」的可见性

真实库还有 **65 张**照片有真实 GPS 但它们在库里 `placeName` 正常；可另有大量照片
**根本没有 GPS**（`lat IS NULL`）。请在验收报告里给出这三个数字，方便后续判断地点层能覆盖多少：
- `lat IS NULL`（无 GPS 坐标）多少张
- 有 GPS 且非占位多少张
- 有 `placeName` 多少张

## 四、验收清单（逐条实际运行）
1. `isRealCoordinate()` 单测：`(0,0)` / `(1e-5, 1e-5)` / `(None, 120)` / `("abc", 120)` /
   `(39.9, 116.4)` 全部给出预期判定
2. **跑一个真实坐标**：`reverseGeocode(39.9, 116.4)` 能查出北京的 `admin2`（证明没改坏正常路径）
3. **`reverseGeocode(0, 0)` 返回 None**（改前会返回加纳 —— 请把改前的对照结果贴出来）
4. `fix_placeholder_geo.py --dry-run` 输出的行数 = **26**（或你复核后的实际数）
5. 清单里抽样 5 条贴出 `relPath` 与 `placeName`，人工确认确实是无定位
6. `--apply` 后：`placeName LIKE '%Takoradi%'` 或 `placeName LIKE 'GH%'` 的行数 = **0**
7. 清理后**逐表行数不变**（`pb_photo` 行数与清理前一致 —— 只改列不改行）
8. **照片数与字节数前后比对**（原图只读硬约束）
9. `pytest code/src/test` 全绿
10. 给出第三节那三个数字（无 GPS / 有 GPS非占位 / 有 placeName）

## 五、硬约束
- **原图绝对只读**（`d:\PhotoLib\photo`）
- 业务层禁止裸 SQL（一律经 sqliteCommon）
- **只改 `placeName`（与 lat/lon 的取舍按你的结论）**，不要顺手改别的列
- 不要顺手重构 `meta.py` 里与本步无关的代码
- 现有代码风格（文件头纪律说明、`_VERSION`、日志、`_RG_CACHE` 等）保持一致

## 六、完成后必须输出
1. 改动文件清单
2. 验收结果（10 条逐条给命令与实际输出）
3. **lat/lon 是否一并清理的结论与理由**
4. 第三节三个数字
5. 遗留问题与需要我决策的点
```

---
---

# 修正步骤 R4 · 地点数据层（geohash 归并 + 目录名抽取 + API）

> **什么时候做**：R3 验收通过后。
> **做什么**：把「地点」变成可验证的数据。本步**不做界面**。

```text
【photo-browser · 修正步骤 R4 · 地点数据层】

## 目标
建立可用的地点事实层：给照片打 geohash → 归并成地点 `pb_place` → 地点名优先取目录名，
并提供只读 API 供人工核对。**本步不做任何界面**。

## 前置
R3 已完成（`(0,0)` 已修、26 张错 placeName 已清）。

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-26（geohash 方案与三个局限）/ DR-27（目录名优先）/ DR-28（不建事件表）/ DR-29（先数据层）**
- plan/数据库设计.md §1.3 命名规范、§1.4 尾部七字段、§五 索引清单、§六 D-1/D-2（弱外键哲学）
- plan/照片管理方案_开源调研与自研设计.md 3.10（前端视图清单里的「地点」那一行）

## 必须先读现有代码
- `code/src/processor/scanner/meta.py` —— `reverseGeocode()` / `_composePlaceName()` /
  `PLACEHOLDER_EPS`（R3 加的）
- `code/src/processor/scanner/walker.py` —— `relPath` 的形态（地点线索的来源）
- `code/src/api/browse.py` —— 分页 / 筛选 / DTO 的既有写法（本步照它来）
- `code/src/api/dto.py`、`code/src/common/globalDefinition.py`
- `code/src/config/basicSettings.py` —— 配置项的加法与注释风格

## 实测事实（正式库，我已查过，可复核）
- 照片 2137 张；有效 GPS（剔占位）**65 张 / 3.0%**；`placeName` 覆盖 4.3%
- **`relPath` 目录名里带「日期 + 地点」的约 512 张**（是有效 GPS 的 8 倍）：
  `2013.07.16 东莞`(98) / `2013.07.18 大亚湾`(99) / `2013.07.20 桂林`(38) /
  `2013.07.22 凤凰`(52) / `2013.07.23 乌镇`(60) / `2013.07.26 张家界`(114) /
  `2013.07.27 南宁`(51)；另有 `2011春天`(122) / `20101218 Michael's Home`(52)
- 目录名里还有大量**按人/按组**命名的：`BaiRuiQin`(452) / `MOT Friends`(157) /
  `lianzhongwen`(116) / `Family`(24) / `Friends`(37) —— ⚠️ 这些**不是地点**，抽取时必须排除

## 一、新增 `pb_place` 表（**10 → 11 张表**）

`code/src/database/pb_place.txt`（先写 txt，再重跑生成器 + `--migrate`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `recID` | INT AUTO_INCREMENT PK | |
| `placeCode` | VARCHAR(64) NOT NULL UNIQUE | 幂等键。由「归并后的代表 geohash + 地点名」派生（**不要用随机 uuid** —— 要稳定，重算才幂等） |
| `name` | VARCHAR(128) NOT NULL | 显示名 |
| `nameSource` | TINYINT NOT NULL DEFAULT 0 | **0 未知 / 1 逆地理 / 2 目录名 / 3 手工**（手工最高，自动流程不得覆盖） |
| `admin1` | VARCHAR(64) NULL | 省/州 |
| `admin2` | VARCHAR(64) NULL | 市/区 |
| `lat` | DECIMAL(10,7) NOT NULL | 簇中心（照片加权平均） |
| `lon` | DECIMAL(10,7) NOT NULL | |
| `photoCount` | INT NOT NULL DEFAULT 0 | 冗余，列表页免 COUNT |
| `personCount` | INT NOT NULL DEFAULT 0 | 冗余，**有多少人物出现过** |
| `firstShotYear` | SMALLINT NULL | 最早年份 |
| `lastShotYear` | SMALLINT NULL | 最晚年份 |
| + 尾部七字段 | | label/memo/regID/regYMDHMS/modifyID/modifyYMDHMS/delFlag |

⚠️ **不要**给 `pb_place` 加 `isConfirmed` / `isStranger` 之类 —— 地点没有「人工确认」语义。
手工命名即最高权威，不满意就改名字，不是打标记。

## 二、新增 `code/src/common/geoHash.py`（**自己实现，不引包**）

标准 geohash 算法（字符集 `0123456789bcdefghjkmnpqrstuvwxyz`，base32，交替经纬度二分）：

```python
def encode(lat, lon, precision: int) -> str: ...       # -> 字符串
def decode(geohash: str) -> (latMin, latMax, lonMin, lonMax): ...
def neighbors(geohash: str) -> list:                   # 8 邻居格（DR-26① 的邻域归并要用）
def distanceKm(lat1, lon1, lat2, lon2) -> float: ...    # haversine
```

- **不引 `geohash` 包**（DR-26③）。约 50 行，自己写。
- `neighbors()` 是**DR-26① 格子边界问题的解法**，必须实现并单测。
- 精度对照（写进文件头注释）：4 位≈39×19km / 5 位≈4.9×4.9km / **6 位≈1.2×0.6km（默认）** / 7 位≈153×153m

## 三、新增 `code/src/processor/place/`（地点抽取与归并）

### 3.1 `dirNamePlace.py` —— 目录名地点线索（**主力来源，DR-27**）

从 `pb_photo.relPath` 的**父目录**（必要时祖父目录）解析：

```python
@dataclass
class DirPlaceHint:
    raw: str              # 原目录名，如 "2013.07.26 张家界"
    placeName: str        # "张家界"
    dateHint: str         # "2013-07-26"（可能为空）
    confidence: int       # 见下
```

| 情形 | 示例 | placeName | dateHint | confidence |
|---|---|---|---|---|
| 日期 + 中文地名 | `2013.07.26 张家界` / `2013-07-26 张家界` | 张家界 | 2013-07-26 | 高 |
| 日期 + 英文/数字 | `20101218 Michael's Home` | Michael's Home | 2010-12-18 | 中（不确定算不算地点） |
| 只有中文（无日期） | `旅行照片` | 旅行照片 | — | **低：倾向排除** |
| **纯人名/组名** | `BaiRuiQin` / `MOT Friends` / `Family` / `Friends` / `BUPT871` | — | — | **必须排除** |

⚠️ **排除人名/组名是这一步最大的难点**，请给出一条**可解释**的判据并写进文件头
（例如：与 `pb_person.displayName` / `pb_person_centroid` 里出现过的名字匹配 → 排除；
或纯 ASCII 字母且长度 ≤ 20 且不含空格 → 排除）。**光靠正则会误伤**（`Michael's Home` 就不该被排除）。
**请把你的判据与它对 2137 张照片的排除结果列出来**（哪些目录被排除了、哪些被采用了）。

### 3.2 `placeStore.py` —— geohash 归并成地点

```
输入：pb_photo（有 lat/lon 的）+ DirPlaceHint（有地点名的）
1. GPS 侧：每张有真实坐标的照片 -> geoHash(lat, lon, PLACE_GEOHASH_PRECISION)
2. 归并：同 geohash 先成簇；再枚举 8 邻居格，簇中心距离 < PLACE_MERGE_RADIUS_KM 则合并
   -> 每个合并簇一个 pb_place 行，placeCode 由「代表 geohash + 规范化地名」派生
3. 目录名侧：同名地点（规范化后）合并成一个 pb_place，placeCode 由目录名派生
   ⚠️ 同名目录地点与 GPS 簇**可能指向同一处**（例如「张家界」目录 + 武陵源 GPS）——
   给出你的合并判据（规范化名相同就合？还是靠距离？两者都试？），并说明误合并/漏合并的后果
4. nameSource 优先级：手工(3) > 目录名(2) > 逆地理(1)；**已有手工命名的一律不覆盖**
5. photoCount / personCount / firstShotYear / lastShotYear 随归并结果一并算出
```

配置项（加进 `basicSettings.py`，都要有注释说明为什么是这个默认值）：
- `PLACE_GEOHASH_PRECISION = 6`
- `PLACE_MERGE_RADIUS_KM = 1.5`（⚠️ 6 位格宽 ~1.2×0.6km，半径要略大于格宽才能把相邻格并起来）
- `PLACE_MIN_PHOTOS = 1`（低于此数不建地点，避免单张照片也占一行）

⚠️ **只重建地点、不要顺手改 `pb_photo` 的其他列**。

### 3.3 `place_cli.py`
```
--audit          只读巡检：地点清单（名称 / 张数 / 年份跨度 / 在场人数 / 代表 geohash）
--rebuild        清空 pb_place 并按当前数据重建（幂等，跑两遍结果一致）
--apply          从 --audit 的清单确认后落库
--photo <code>   单张重算
```

## 四、只读 API（**本步唯一的对外产物，界面在 R5**）

`code/src/api/place.py`，照 `browse.py` 的写法（分页统一 `page/size/total`、禁止裸 SQL）：

| 端点 | 返回 |
|---|---|
| `GET /api/places` | 分页 + 筛选（`q` 名称模糊 / `minPhotos` / `year` / `hasPerson`）。每条：`placeCode / name / nameSource / admin1 / admin2 / lat / lon / photoCount / personCount / firstShotYear / lastShotYear` |
| `GET /api/places/{placeCode}` | 详情 + 该地点的照片列表（缩略图 URL + 拍摄时间 + 在场的人） |
| `GET /api/places/{placeCode}/persons` | 该地点出现过的人 + 各自照片数 |
| `GET /api/persons/{personCode}/places` | **某个人去过的地点列表**（按 `lastShotYear` 倒序）—— 这是 DR-28 里「实时 join」的那条，**不落库** |
| `GET /api/places/stats` | 汇总：无地点照片数 / 有效 GPS 数 / 目录名命中数 / 地点总数 |

⚠️ **`personCount` / 某人在某地点的照片数一律实时 `DISTINCT` join 算，不冗余存储**（DR-28）。
`pb_place.photoCount/personCount` 只是**列表页免 COUNT 的冗余**，要有 `--rebuild` 能重算，
并提供一个 `verifyPlaceCounts()` 核对冗余与实时值是否一致（不一致要能报出来）。

## 五、验收清单（逐条实际运行）

### A. 表与生成器
1. `pb_place.txt` 写好，重跑生成器，`build_db.py --migrate` 建表
   （**逐表行数迁移前后一致**，只新增表、不动既有数据）
2. `PRAGMA table_info(pb_place)` 字段齐全；`placeCode` 的 UNIQUE 索引存在
3. `pb_person_centroid` 与 `pb_photo` 行数未变

### B. geohash 自实现
4. `geoHash.encode/decode/neighbors` 单测：`neighbors` 返回 8 个、
   `decode(encode(p))` 包含 p、跨带（如 39.9/116.4 与 -33.9/151.2）正确
5. **边界用例**：`39.9999,116.3999` 与 `39.9999,116.4001` 编码后**不同格**
   → 这正是 DR-26① 要靠 `neighbors()` 归并的场景，请证明归并能合上（贴归并前后）

### C. 目录名抽取（**本步最容易错的地方**）
6. 给出**完整**的目录级采纳/排除清单（2137 张照片涉及的每个二级目录 → 采用还是排除、理由）
7. `BaiRuiQin` / `MOT Friends` / `Family` / `Friends` / `BUPT871` **必须被排除**
8. `2013.07.26 张家界` / `2013.07.16 东莞` 等**必须被采用**，地点名与日期解析正确
9. `20101218 Michael's Home` 的判定请给结论并说明理由（它可能是地点也可能是人）
10. 命中率报告：多少张照片因目录名拿到了地点名、多少张仍无地点

### D. 地点归并
11. `place_cli.py --audit` 输出地点清单。**对照我上面的实测数字核对量级**
    （目录名线索约 512 张、有效 GPS 65 张）
12. **至少人工核对 5 个地点**：名字对不对？张数对不对？有没有把不同地点并成一个？
    有没有把同一地点拆成多个？
13. **手工命名不被覆盖**：手工把某地点 `name` 改掉 + `nameSource=3`，跑 `--rebuild`
    → **名字必须不变**
14. `--rebuild` 跑两遍，`pb_place` 行数与内容一致（幂等）
15. `verifyPlaceCounts()` 报告 `pb_place.photoCount` 与实时 `COUNT(*)` 是否一致

### E. API
16. 启动服务，`/docs` 能看到 5 个端点并可试调
17. `GET /api/places` 分页 / 筛选（`q` / `minPhotos` / `year` / `hasPerson`）都正确
18. `GET /api/persons/{personCode}/places` **返回的是实时 join 的结果**——
    构造一个新人物确认其脸后，该地点的 `personCount` 立即变化（证明不是落库的）
19. `GET /api/places/stats` 给出四个数字（无地点 / 有效 GPS / 目录名命中 / 地点总数）
20. `photoDir` 零风险：全程文件数与总字节数不变

## 六、硬约束
- **原图绝对只读**
- 业务层**禁止裸 SQL**，一律经 `sqliteCommon`
- **不建「事件」表**（DR-28）：地点×人的交叉一律实时 join
- **手工命名最高权威**：`nameSource=3` 的行，任何自动流程不得覆盖其 `name`
- 不引 `geohash` 包（自己实现）
- 不要顺手做界面（R5 的事）；不要顺手改 `pb_photo` 的其他列
- 现有代码风格（文件头纪律说明、`_VERSION`、日志、分页/筛选写法）保持一致

## 七、完成后必须输出
1. 改动文件清单
2. 验收结果（20 条逐条给命令与实际输出）
3. **地点清单（`--audit` 全量输出）** —— 这是 R5 界面设计的依据
4. **目录名采纳/排除清单 + 判据说明**
5. 归并误判/漏判的自查结论（哪些地点你看着不对、为什么）
6. 遗留问题与需要我决策的点
```

---
---

# 修正步骤 R5 · 地点界面（人物 → 地点列表 → 时间线）

> **什么时候做**：R4 验收通过、**地点清单人工核对过之后**。
> 本步的提示语**暂未编写** —— 界面形态要在看过真实地点清单之后再定（现在定会返工）。
> 届时按 R4 输出的「地点清单」决定：地点怎么分组（按省/市？按时间跨度？）、要不要地图、
> 人物视角与地点视角的入口放哪。

---

# 修正步骤 R4a · 地点中文名（DR-28/29）

> **什么时候做**：现在。地点界面（R5）之前必须做完，否则界面出来还是英文。
> **前置**：R3 已完成；`pb_place` + `placeStore.py` + `/api/places` 已落地（本步是**增量**，不是重写）。

```text
【photo-browser · 修正步骤 R4a · 地点中文名】

## 目标
照片详情/列表里的地点在**中国境内显示中文**。根因是 `reverse_geocoder` 的数据集只有英文。
本步给 `pb_place` 加一列中文名，并把中文名接到接口与筛选上。**不做界面**。

## 前置（已核实，请自己再确认一遍）
- `code/src/database/pb_place.txt` —— 表已存在（`placeCode` / `placeName` / `source` /
  `photoCount` / `firstShotYear` / `lastShotYear` / `centerLat` / `centerLon` + 尾部七字段）
- `code/src/processor/place/placeStore.py` —— `makePlaceCode()` / `rebuildPlaces()` /
  `listPlaces()` / `countPlaces()` / `liveAggregatePlaces()`
- `code/src/api/browse.py` —— `GET /api/places`（约 1285 行）、`POST /api/places/rebuild`（约 1380 行）
- `code/src/test/test_api_places.py`
- `code/src/tools/fix_placeholder_geo.py`（R3 的产出，已完成）

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-28 / DR-29**（本步的全部口径）
- plan/数据库设计.md §1.4 尾部七字段、§五 索引清单

## 必须先读现有代码（重点这几处）
### 1. `placeStore.py` 的派生纪律（本步最大的坑在��）
```python
REBUILD_COLUMNS = ("placeName", "photoCount", "firstShotYear",
                   "lastShotYear", "centerLat", "centerLon")
```
以及文件头这段：
> `photoCount` / `firstShotYear` / `lastShotYear` / `centerLat` / `centerLon` / **`placeName` 全部可从 `pb_photo` 重算**

⚠️ **`nameZh` 绝不能进 `REBUILD_COLUMNS`** —— 它是**人工/外部来源**，不是派生值。
进了就会每轮 `rebuildPlaces()` 把中文冲回英文，**而且不报错**（照片张数对、地点名回英文，
最难发现的一类退化）。请把这条写进 `REBUILD_COLUMNS` 的注释里。

### 2. `placeStore.makePlaceCode(placeName)` —— 幂等键派生
**本步一个字都不改它。** `placeName` 保持英文做聚合键 ⇒ `placeCode` 不漂移 ⇒
已有的行、归零逻辑、`/api/places` 的排序筛选全部不受影响。

### 3. `browse.py` 里地点相关的三处
- `photoSummary()`（约 182 行）—— 列表用的 `placeName`
- `GET /api/photos` 的 `placeName` 筛选（约 523 行）—— **精确匹配**
- `photoDetail` 的 `out["gps"]["placeName"]`（约 1214 行）—— **详情页「地点」栏读的就是它**
⚠️ 还有 `getPerson` 里的 `photosByBucket`（约 991 行）也 SELECT 了 `p.placeName`。

## 一、表结构改动

`code/src/database/pb_place.txt` **在尾部七字段之前**插入：
```
nameZh VARCHAR(128) NULL COMMENT '地点中文名 境内区县级 境外为空回退placeName 非派生列rebuild绝不覆盖'
```
- 重跑生成器 → `build_db.py --migrate`（**逐表行数迁移前后一致**，只加列不动数据）

## 二、新增 `code/src/processor/place/placeNameZh.py`

```python
#: 依赖懒加载闸门（照抄 meta.py 的 _RG_READY / _RG_WARNED 纪律）
_ZH_READY = None       # None=未尝试 / True=可用 / False=不可用
_ZH_WARNED = False    # 「只记一次 warning」

def isInChina(lat, lon) -> bool: ...
    """境内判定。⚠️ **不要用 reverse_geocoder 的 cc == 'CN'** —— 边界不准且要多查一次。
    用 amap-geo 的多边形集合做 point-in-polygon（覆盖即境内）。"""

def zhNameOf(placeCode, placeName, centerLat, centerLon) -> dict:
    """给一个地点算中文名。返回 {nameZh, source, reason}
    source: 0=无 2=行政区 3=手工
    ⚠️ centerLat/centerLon 为空的地点（如手工建的）→ nameZh=None
    ⚠️ 境外 → nameZh=None（**保留当地语言**，不翻译）
    ⚠️ amap-geo 不可用 → nameZh=None 并**只记一次 warning**，绝不抛错"""

def rebuildNameZh(dryRun=False) -> dict:
    """遍历 pb_place，为**还没有 nameZh** 的行补中文名。
    ⚠️ **绝不覆盖已有的 nameZh** —— 手工填的（source=1）与已算出的都不动。
    dry-run 只报告将改多少行。"""
```

### 中文名的组装
```
nameZh = "<省级中文> · <区县中文>"
例：「新疆维吾尔自治区 · 阿勒泰市」、「北京市 · 海淀区」
```
- **省级中文**：一级行政区（`Xinjiang Uygur Zizhiqu` / `Beijing` / `Zhejiang Sheng`…）
  用一张**小型固定映射表**（全国 34 个省级行政区，**有限且可穷举**，不要去映射全球）
- **区县中文**：`amap-geo` 的 point-in-polygon 结果
- ⚠️ 边界情形：落在两个多边形上（重叠）/ 都不在（境外或数据集缺口）
  → **都要给明确 reason 并计数报告**，不要静默给一个空字符串

## 三、依赖与开关（`basicSettings.py` + `requirements.txt`）

```
#: 地点中文名总开关（关掉后 nameZh 全为 NULL，界面回退英文）
PLACE_ZH_ENABLED: bool = True
#: amap-geo 数据文件路径（默认用包内置）
PLACE_AMAP_GEO_FILE: str = ""
```

`requirements.txt` 新增（**标注为可选**）：
```
# ---- 地点中文名（可选）----
# amap-geo 含全国 ~3500 个区县多边形（纯离线）；需 shapely 做 point-in-polygon
amap-geo>=0.0.7
shapely>=2.0.0
```

⚠️ **依赖纪律**（照抄 `meta.py` 的 `_RG_READY` / `_RG_WARNED`）：
- `import amap_geo` / `import shapely` 放进函数内，**模块顶层不许 import**
- 首次调用才加载（约 1~2 秒），进程内缓存
- 失败 → `_ZH_READY=False` + **只记一次 warning** + `nameZh=None`
- **绝对不许抛错中断**：装不上只是「显示英文」，不是故障
- ⚠️ 首次加载**不许在请求路径上重复发生** —— 要么在 `main/app.py` 启动时预热，
  要么在第一次 `/api/places` 调用时加载后缓存（请给出结论）

## 四、接口改动

### 4.1 地点字典端点
`GET /api/places` 的每条**增加 `nameZh`**（`SELECT g.nameZh AS nameZh`）。
`keyword` 筛选**同时匹配 `placeName` 与 `nameZh`**。

### 4.2 照片列表/详情端点 —— ⚠️ 这一步是「界面能显示中文」的关键
**`pb_photo` 没有 `placeCode` 列**，所以照片 → 地点的关联只能走
`JOIN pb_place g ON g.placeName = p.placeName`（B 方案的代价：用字符串 join）。
⚠️ 这个 join **只对 `placeName` 非空的照片成立**，无 GPS 的照片拿不到中文名（回退 NULL，可接受）。

| 位置 | 改动 |
|---|---|
| `photoSummary()` | 增加 `placeZh` 字段（`p.placeName` 存在时 JOIN 取 `g.nameZh`，否则 NULL） |
| `photoDetail` 的 `out["gps"]` | 增加 `placeZh`；⚠️ **保留 `placeName`**（英文原值作为回退与排障依据） |
| `getPerson` 的 `photosByBucket` | 同上增加 `placeZh` |

**回退契约（前端要照此实现）**：
```
显示名 = placeZh ?? placeName      # placeZh 为空就用英文原值
```
⚠️ **不要**用 `placeZh` 覆盖 `placeName` 返回 —— 保留英文原值，排障时要看它，
而且界面「地点」的 tooltip 可以显示两者。

### 4.3 地点筛选两套都支持（DR-29③）
`GET /api/photos?placeName=X` 的 `X` 现在可能是中文名（前端下拉给中文），
内部翻译回英文再精确匹配：

```python
def resolvePlaceFilter(value) -> tuple:
    """筛选值 -> (sql 片段, 参数)。
    X 命中 pb_place.nameZh -> 用该行的 placeName 做精确匹配
    X 直接是 placeName      -> 直接精确匹配（回退兼容旧调用）
    两边都没命中            -> 原样精确匹配（返回空集，不报错）"""
```
⚠️ **仍然是精确匹配，不要改成 LIKE** —— `browse.py` 现有注释已说明理由
（「北京」与「北京市」在库里是两个不同字符串，模糊匹配会让用户以为自己筛错了）。
- 筛选下拉的数据源要**优先给 `nameZh`**（`nameZh IS NOT NULL` 的行），英文值作为回退项补充
- `test_api_places.py` 里现有的筛选用例**不能挂**

## 五、验收清单（逐条实际运行）

### A. 表与迁移
1. `pb_place.txt` 加了 `nameZh`，重跑生成器，`build_db.py --migrate` 成功
   （**逐表行数迁移前后一致**，`pb_photo` / `pb_face` / `pb_person_centroid` 行数不变）
2. `PRAGMA table_info(pb_place)` 含 `nameZh`

### B. 边界纪律（**本步最关键**）
3. **`rebuildPlaces()` 跑两遍后 `nameZh` 不变** —— 证明 `REBUILD_COLUMNS` 没把它当派生列
4. **手工设一个 `nameZh`（模拟用户填的），再跑 `rebuildPlaces()` → 该值不变**
5. 手工行的 `source=1` 不被复算改回 0（现有代码已处理，本步别破坏）

### C. 中文名正确性
6. **境内地点的中文名**：列出改后全部 `nameZh`，逐个人工核对（至少 5 个）
7. **境外地点 `nameZh` 全部为 NULL**（现有库有 `GH, Western, Takoradi`（(0,0) 那批，
   R3 已清）与真实境外地点；若R3 清干净了，就说明当前库里没有境外真实地点，
   请用构造数据验证境外返回 NULL）
8. 省级映射表**覆盖全国 34 个省级行政区**，请给出这份表的完整内容
9. `zhNameOf` 的每个边界情形都有 reason 与计数（重叠多边形 / 不在任何多边形 / 无坐标）
10. **你截图那张**：`mmexport17832182934679.jpg`（`CN, Xinjiang Uygur Zizhiqu, Araltobe`）
    → `placeZh` 应该是中文（阿勒泰市一带）。请贴出实际值

### D. 接口
11. `GET /api/places` 每条含 `nameZh`；`keyword` 能用中文名搜到
12. `GET /api/photos/{photoCode}` 的 `placeZh` 有值、`placeName` **仍是英文原值**
13. `GET /api/photos?placeName=阿勒泰市` 与 `?placeName=CN, Xinjiang Uygur Zizhiqu, Araltobe`
    **返回同一批照片**（两套都支持的验收）
14. 无地点的照片：`placeZh = None`、`placeName = NULL`，前端按契约回退
15. `GET /api/persons/{personCode}` 里的照片也带 `placeZh`
16. `test_api_places.py` 原有用例全绿（筛选没被改坏）

### E. 依赖与回归
17. **`amap-geo` 缺失时的降级**：临时把它从环境里屏蔽（或用一个假的 import 失败桩），
    确认 `nameZh` 全为 NULL、**只记一次 warning**、接口仍 200、**不抛异常**
18. `PLACE_ZH_ENABLED=False` 时全部回退英文
19. `pytest code/src/test -q` 全绿（当前基线 **1237 passed**）
20. `photoDir` 零风险：全程文件数与总字节数不变

## 六、硬约束
- **原图绝对只读**
- 业务层**禁止裸 SQL**，一律经 `sqliteCommon`
- **`placeCode` 的派生逻辑一字不改**（`makePlaceCode` / `REBUILD_COLUMNS` 的既有成员不动）
- **`nameZh` 绝不进 `REBUILD_COLUMNS`**，绝不被任何自动流程覆盖
- **`amap-geo` / `shapely` 是可选依赖**：缺失只降级不报错
- **境外地点不翻译**（保留当地语言）
- 筛选仍用**精确匹配**，不要改成 LIKE
- 本步**不做界面**（R5 的事）；不顺手做目录名抽取（DR-30 是独立一步）
- 现有代码风格（文件头纪律说明、`_VERSION`、日志、可选依赖的懒加载纪律）保持一致

## 七、完成后必须输出
1. 改动文件清单
2. 验收结果（20 条逐条给命令与实际输出）
3. **改后 `pb_place` 全表**（placeCode / placeName / nameZh / photoCount / source）
4. **省级 EN→ZH 映射表全文**
5. **你截图那张照片的 `placeZh` 实际值**
6. `amap-geo` 的首次加载耗时实测
7. 遗留问题与需要我决策的点
```

---
---

# 修正步骤 R4b · 目录名线索接入（DR-25/30）

> ⚠️ **暂未编写**。R4a 完成后决定要不要做 —— 前提是回答：
> 「地点字典只有 65 张 GPS 照片，而目录名线索有 512 张。值得为它扩表吗？」
> 若做，涉及 `pb_photo` 加列 + `rebuildPlaces` 的聚合键变更，会影响 `placeCode` 派生，
> 需重新评估 DR-28 的 B 方案是否还成立。

---

# 修正步骤 R5 · 地点界面（人物 → 地点列表 → 时间线）

> **什么时候做**：R4a 完成、`pb_place` 清单人工核对过之后。
> 本步的提示语**暂未编写** —— 界面形态要在看过真实地点清单之后再定。
> 届时需要落实的**显示契约**（本步已定）：
> `显示名 = placeZh ?? placeName`；地点筛选两套值都认；`placeZh` 为空即回退英文。

---

# 修正步骤 R6 · 照片详情左右翻页（A 档 · DR-31）

> **什么时候做**：R4a 之后（或随时，与地点线无依赖）。
> **做什么**：P-03 照片详情页支持左右箭头翻看上一张/下一张。**只做前端，后端零改动。**

```text
【photo-browser · 修正步骤 R6 · 照片详情左右翻页（A 档）】

## 目标
P-03 照片详情页支持「上一张 / 下一张」：**键盘 ← →** + **左右浮动箭头按钮**。
**只做 P-03；网格页不动；后端零改动。**

## 前置
步骤 1–12 已完成。P-03（`PhotoDetailView.vue`，35KB）已上线，`store/photos.js` 已实现列表。

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-31**（本步全部口径）

## 必须先读现有代码
### 1. `code/webserver/src/views/PhotoDetailView.vue`
- `onMounted` 现在只有 `review.ensurePersonDirectory()` / `reloadPhoto()` / `probeRange()`
  —— **没有任何键盘监听**，本步要加
- 已有 `const zoomPercent = ref(100)` 与 `zoomStyle` —— ⚠️ 箭头不能与缩放交互打架
- 已有 `FixFaceDialog`（改判入口，DR-16）—— ⚠️ 见「四、与改判的顺序」

### 2. `code/webserver/src/store/photos.js`
- `items`（已加载列表，**累积上限 `MAX_APPENDED_CELLS = 1800`**）、`total`、`page`、`size`
- `current`（当前详情对象）、`currentLoading`
- `hasMore` / `canAppendMore` / `appendCapped` / `rangeText`
- `applyFilters()` / `resetFilters()` —— **两者都会把 `items` 清空并回到第 1 页**

### 3. `code/webserver/src/views/ReviewView.vue` 的键盘纪律（本步要照抄）
```js
function onKeydown(event) {
  if (activeTab.value !== 'pending') return
  if (event.metaKey || event.ctrlKey || event.altKey) return
  if (isTypingTarget(event.target)) return
  if (overlayOpen()) return
  ...
  event.preventDefault()
}
onMounted(async () => { window.addEventListener('keydown', onKeydown); ... })
onBeforeUnmount(() => { window.removeEventListener('keydown', onKeydown); ... })
```
⚠️ `isTypingTarget` / `overlayOpen` 这两个判据**复用或照抄**，不要另写一套。

## 一、新增 `code/webserver/src/components/photo/PhotoPager.vue`

单一职责组件：**左右箭头 + 位置提示**。P-03 用它，将来 `PersonDetailView` 的时间轴详情也能复用。

```vue
<template>
  <div class="photo-pager">
    <button class="pager-btn" :disabled="!canPrev" aria-label="上一张"
            @click="$emit('nav', -1)"> <ChevronLeft /> </button>
    <div class="pager-info">
      <span class="pager-index">{{ index }}</span>
      <span class="pager-total">/ {{ total }}</span>
    </div>
    <button class="pager-btn" :disabled="!canNext" aria-label="下一张"
            @click="$emit('nav', 1)"> <ChevronRight /> </button>
  </div>
  <p v-if="loadedHint" class="pager-hint">{{ loadedHint }}</p>
</template>
```

| prop | 说明 |
|---|---|
| `index` | 当前在 `items` 里的**下标 + 1**（1 基）；不在列表里时为 0 |
| `total` | **`items.length`**（不是 store 的 `total`！见 DR-31 边界纪律③） |
| `loadedHint` | `items.length < store.total` 时的补充提示（如「已加载 60 / 2137，滚动可继续加载」） |

⚠️ **禁止循环**：到第一张左箭头灰、到最后一张右箭头灰，**不要**「到底跳回第一张」。
⚠️ 箭头**必须是真 `<button>`**（不用 `div`），带 `aria-label`、`:disabled`、`focus-visible` 样式。

## 二、`PhotoDetailView.vue` 的改动

### 2.1 计算前后张（**不要新写一个接口**）

```js
const photoStore = usePhotosStore()

/** 当前 photoCode 在已加载列表里的下标；-1 = 不在列表里 */
const currentIndex = computed(() =>
  photoStore.items.findIndex((p) => p.photoCode === current?.photoCode))

const canPrev = computed(() => currentIndex.value > 0)
const canNext = computed(() =>
  currentIndex.value >= 0 && currentIndex.value < photoStore.items.length - 1)
/** ⚠️ -1 时（不在列表里）两个都 false —— DR-31 边界纪律① */
const pagerTotal = computed(() => photoStore.items.length)
```

### 2.2 翻页动作

```js
async function gotoOffset(delta) {
  if (busy.value) return                      // ⚠️ 改判进行中禁翻（见四）
  const target = photoStore.items[currentIndex.value + delta]
  if (!target) return
  busy.value = true
  try {
    await router.push({ name: 'photo-detail', params: { photoCode: target.photoCode } })
    // ⚠️ 不要预取上一张，只预取下一张（DR-31）
    prefetchNext()
  } finally {
    busy.value = false
  }
}
```

- 路由参数用 `photoCode`（`/photos/:photoCode`），保持「可分享 URL、可刷新」
- ⚠️ **`currentIndex` 依赖 `photoStore.items`** ⇒ `applyFilters()` / `resetFilters()` 清空
  `items` 后，`currentIndex` 会变成 -1 ⇒ 箭头自动禁用。**这是期望行为**，不要额外去「记住旧索引」

### 2.3 键盘监听（照抄 ReviewView 三条纪律 + 第四条）

```js
function onKeydown(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return
  if (isTypingTarget(event.target)) return
  if (overlayOpen()) return
  if (event.key === 'ArrowLeft'  && canPrev.value) { event.preventDefault(); gotoOffset(-1) }
  if (event.key === 'ArrowRight' && canNext.value) { event.preventDefault(); gotoOffset(+1) }
}
```

⚠️ **`preventDefault()` 必须调**，否则 `←`/`→` 会触发横向滚动。
⚠️ `onMounted` 里 `addEventListener`、`onBeforeUnmount` 里 `removeEventListener`（ReviewView 已有这个范式）。

**不要**占用其他键（`Esc` 关闭、`F` 全屏留给将来）。**不要**加 `↑`/`↓`（会与缩放/滚动打架）。

### 2.4 预取下一张（只做 1 张）

```js
function prefetchNext() {
  const next = photoStore.items[currentIndex.value + 1]
  if (!next) return
  // ⚠️ 用 requestIdleCallback 或 setTimeout 延后；连按 → 时不要堆成一片请求
  idle(() => { fetch(`${API_BASE}/api/original/${next.photoCode}`, { headers: { Range: 'bytes=0-65535' } }) })
}
```
⚠️ `/api/original` **已支持 Range**（步骤 4 已交付），所以预取首块即可，别整张拉。
⚠️ 预取失败**静默忽略**，不许弹错误提示（用户没要求看下一张）。

## 三、样式与可访问性

| 项 | 要求 |
|---|---|
| 位置 | 大图**左右两侧垂直居中**，浮动；`hover` 才显形（`opacity-0 → 1`），避免干扰看图 |
| 圆角/阴影 | 圆形按钮 + 轻阴影（沿用 `--radius` 与浅色 token） |
| 响应式 | `<768` 时缩小按钮（避免遮挡）；**不要**引入手势滑动 |
| 深色主题 | 两套主题下都要可辨（箭头背景用 `bg-card` / `border-card` 类，不用硬编码色） |
| 无障碍 | `<button>` + `aria-label="上一张/下一张"` + `focus-visible` 环；`disabled` 用 `disabled:` 变体而非 JS 控制 `opacity` 硬改 |
| 位置提示 | 箭头之间的 `12 / 60`，小号 `text-secondary`；`loadedHint` 放在下方、更弱的颜色 |

## 四、⚠️ 与改判（DR-16）的顺序 —— 本步最容易漏的一条

P-03 上有「✗ 不是他」改判（`FixFaceDialog`）。改判成功后：
- 该脸的 `personCode` / `isStranger` 变了 ⇒ **人脸框状态要重画**（`FaceBox` 的描边三态）
- `pb_photo_person` 变了 ⇒ 侧栏「出现的人」列表要重画

⚠️ **若改判请求未完成就按 `→` 跳走**，回来时状态陈旧，**且不报错** —— 表现是「我明明改判了，人脸框还是绿的」。

**做法**：
1. `busy` 标志位覆盖「改判请求进行中」与「翻页进行中」
2. `busy` 为真时 **两个箭头禁用 + 键盘不响应**
3. 改判成功后**先 `reloadPhoto()` 刷当前图，再解禁**

⚠️ 验收第 6 条专门测这个：改判中途点箭头 → 应无反应；改判成功后当前图的人脸框与侧栏都已更新。

## 五、验收清单（逐条实际运行）

1. P-03 打开时左右箭头**正确显示/禁用**：第一张左箭头灰、最后一张右箭头灰
2. 点箭头 / 按 `←` `→` 都能翻到相邻照片，**URL 的 `photoCode` 同步变化**（可分享、可刷新）
3. **不循环**：第一张连按 `←` 停在原地；最后一张连按 `→` 停在原地
4. 位置提示 `12 / 60` 正确；**分母是 `items.length` 不是 `total`**
5. `items.length < total` 时出现补充提示（如「已加载 60 / 2137，滚动可继续加载」）
6. **⚠️ 改判顺序**：改判进行中点箭头 → **无反应**；改判成功后 → 人脸框与侧栏「出现的人」都已更新，然后才能翻
7. **不在列表里**：从 P-06 待确认队列点开一张照片 → **两个箭头都禁用**，且有说明「不在当前列表中，无法连续翻页」
8. **换筛选后**：在 P-02 改筛选 → 点进详情 → 索引正确、`items.length` 正确
9. **三条忽略纪律逐条验**：① 按住 Ctrl/Alt/Shift + `←` 不响应 ② 光标在输入框内按 `←` 不响应
   （输入框光标移动） ③ `FixFaceDialog` 打开时按 `←` 不响应（不误翻）
10. `preventDefault` 生效：按 `←`/`→` **页面不横向滚动**
11. **预取生效**：Network 里能看到下一张的 `/api/original` Range 请求，**且只有 1 张**（不堆成一片）
12. 预取失败**不弹错误提示**
13. **网格页 P-02 未受影响**：方向键仍是移动焦点（Tab / 方向键能走到每张缩略图）
14. `<768` 断点下箭头不遮挡大图；`<1024` 正常
15. 深色主题下箭头可辨（两套主题都看一遍）
16. 无障碍：箭头是真 `<button>`、有 `aria-label`、`focus-visible` 环清晰、`disabled` 有视觉态
17. `npm run build` 通过；`npm run dev` 手动过一遍
18. **后端零改动**：本步**不改任何 `.py`** —— 用 `git status` 证明（只应有 `.vue` / 可能的 `.js` 改动）
19. **photoDir 零风险**：全程文件数与总字节数不变
20. `pytest code/src/test -q` 仍全绿（当前基线 **1237 passed**）

## 六、硬约束
- **后端零改动**（A 档的核心）：不新增、不修改任何 `.py` / `pb_*.txt`
- **网格页不动**：P-02 的方向键保持移动焦点（WCAG 2.1 AA 基线）
- **只占用 `←` `→` 两个键**，不抢其他键
- 一切读写经既有 store/api 封装，**不要在组件里直接 axios**
- 现有代码风格（`store` / `api` / `components` 分层、`<script setup>`、Element Plus + Tailwind、主题 token）保持一致
- 单文件不超过 300 行（`PhotoDetailView.vue` 现有 35KB，若逼近上限请把翻页逻辑拆到 `PhotoPager.vue`）
- 不要顺手加手势滑动、全屏、缩略图条等本步未要求的功能

## 七、完成后必须输出
1. 改动文件清单（应只有前端文件）
2. 验收结果（20 条逐条说明实测情况）
3. `git status` 证明后端零改动
4. 第 7 条的实测：从队列点开照片时箭头的实际状态与提示文案
5. 遗留问题与需要我决策的点
```

---

# 修正步骤 R7 · 人物头像（卡片显示照片 + 样本设默认 · DR-40/41）

> **什么时候做**：随时（与 R5 地点界面**无依赖**，两边都要改人物相关文件时建议串行）。
> **做什么**：① 人物库卡片中间显示**本人照片**（人脸裁剪图）；② 人脸样本里**任选一张设为该人的默认头像**，可清除。
> 拆两半：**A 半（①）零迁移、纯读取回退**；**B 半（②）只多一列写入**。先做 A 单独就已解决截图里的问题。

```text
【photo-browser · 修正步骤 R7 · 人物头像（DR-40/41）】

## 目标
① P-04 人物库卡片中间显示**本人照片**（人脸裁剪图），不再是首字母占位。
② P-05 人物详情「人脸样本」里，**人工确认段与自动归属段任选一张 → 设为该人的默认头像**；可清除（回到自动代表脸）。
拆两半：**A 半（①）纯读取回退、零迁移**；**B 半（②）只多一列写入**。A 单独做即可先上线。

## 前置
步骤 1–12 与 R3 / R4 / R4a / R4b / R6 均已完成。
真实库 `d:\PhotoLib\db\photolib.db`：`pb_person` **2029** 行、`pb_face` **3647** 张（但**仅 67 张有归属**），
`pb_person.avatarFaceCode` 列**已存在**、且**全库几乎为空**（这就是卡片全是字母的原因）。

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-40 / DR-41**（本步全部口径）
- plan/UI/photo-browser UI 设计.md §4.8（P-04 人物库）/ P-05 人物详情 —— **「改头像」本来就在设计稿的操作行里**

## 必须先读现有代码
### 1. `code/webserver/src/components/common/PersonCard.vue`
- 第 40–42 行 `avatarSrc` **只读** `person.avatarFaceCode` ⇒ 空就退到第 45 行的 `initial`（首字母）
- 第 12–16 行文件头**已经把取舍写死**：头像永远是人脸裁剪图，**不是照片缩略图**（合影里脸太小认不出）—— 本步照此执行
- 第 95–116 行是头像那块（圆形遮罩 `h-20 w-20`），第 109–115 行是首字母占位

### 2. `code/webserver/src/views/PersonDetailView.vue`
- 第 76–81 行 `avatarSrc`：`avatarFaceCode` → 否则「第一张确认样本」。⚠️ 与列表页口径**不一致**，本步统一到后端 `coverFaceCode`
- 第 379–385 行 `setAvatar()` 是**死代码**（模板里没有任何按钮调它，函数体只弹一条「不提供编辑入口」的提示）⇒ **删掉**，换成真的实现
- 第 554–571 行（人工确认段）/ 第 593–632 行（自动归属段）是样本列表；自动段每张已有「确认 / ✕ 移除」

### 3. `code/src/api/browse.py`
- 第 234–276 行 `personSummary()`（第 271 行的 `thumbUrl` 就是头像 URL）
- 第 605 行 `personStatsOf()` —— **本步新函数的样板**：一次 `IN` 查询算一批人，禁止逐人查库
- 第 298 行 `_inClause()`；第 750–757 行 `listPersons` 的 items 组装；第 784–786 行 `getPerson`
- 第 871 行 `_faceSummary()`（Tab2 的样本形状，`thumbUrl` = `/api/face/<faceCode>`）

### 4. `code/src/api/contacts.py`
- 第 415–416 行 `CONTACT_PATCH_COLUMNS`；第 455–515 行 `patchContact` 主流程
- ⚠️ 第 463–471 行「未知字段优先报错」与「没有任何字段要改」的**顺序不要改**
- 第 301 行（contacts 列表）与 `code/src/api/place.py` 第 715–720 行也要同步传代表脸

### 5. `code/src/api/dto.py`
- 第 352–380 行 `ContactPatchBody`（`extra="allow"`：白名单外的键由路由 400 拒掉）

## 一、A 半：卡片显示照片（DR-40）

### 1.1 后端：新增 `personCoverOf(codes)`（放在 `personStatsOf` 旁边）

对每人取一张**代表脸**，优先级：
`isConfirmed DESC`（用户核对过的优先）→ `detScore DESC` → `quality DESC` → `regYMDHMS ASC`（稳定）。

- 一次 `IN` 查询拿一批人（⚠️ **必须批量**；正式库 2029 人，逐人查就是 N+1）
- 只取 `delFlag='0'` 且 `personCode IS NOT NULL` 的脸
- 返回 `{personCode: faceCode}`；**查不到**的人不出现在字典里（调用方 `.get()` 拿 `None`）

⚠️ **不要做 `thumbStore.exists()` 探测**：列表接口不该为每行去碰文件系统（一页 24 次 `stat`，磁盘异常会把列表拖死）。裁剪图缺失由前端 `@error` 兜底。

### 1.2 `personSummary()` 增 `coverFaceCode`

- 新参数 `coverFaceCode=None`；值 = **用户指定的默认（且那张脸还在他名下）→ 否则代表脸**
  （⚠️ 由 `personCoversOf()` 解析好后传进来，**不要在 `personSummary` 里写 `avatarFaceCode or ...`** ——
  见文末「R7 落地时的偏差」第 1 条）
- `thumbUrl`（第 271 行）指向 `coverFaceCode`
- ⚠️ `avatarFaceCode` 字段**语义不变**（用户指定的默认）；`coverFaceCode` 才是「实际展示的那张」
- ⚠️ **代表脸绝不写库**：`avatarFaceCode` 为空是**合法状态**，不能变成「浏览一次就写一次库」

接线四处：`browse.listPersons`（750）、`browse.getPerson`（784）、`contacts.py` 列表（301）、`place.py`（715–720 —— 该处已有 `thumbUrl` 字段，一并改口径）。

### 1.3 前端：`PersonCard.vue`

- `avatarSrc` 改成 `faceUrl(person.avatarFaceCode || person.coverFaceCode)`（任一为空 → 退首字母）
- 头像尺寸 **`h-20 w-20` → `h-24 w-24`（96px）**（用户要的是「中间显示照片」，80px 太小）
- `<img>` 加 **`@error`** ⇒ 加载失败切回首字母占位（**不允许出现破图**）
- 首字母占位（109–115 行）**保留**：没有任何脸的人（刚导入的联系人）仍然靠它

### 1.4 前端：`PersonDetailView.vue` 头部

第 76–81 行的 `avatarSrc` 改为优先用后端 `coverFaceCode`，**与列表页口径统一**（不要再自己取 `facesConfirmed[0]`）。

## 二、B 半：人脸样本设默认头像（DR-41）

### 2.1 后端：`avatarFaceCode` 进 PATCH 白名单

- `dto.ContactPatchBody` 加 `avatarFaceCode: Optional[str]`
- `CONTACT_PATCH_COLUMNS` 加 `"avatarFaceCode"`
- `patchContact` 里加**写库前**的校验：
  - 非空 ⇒ `browse.faceRow(faceCode)` 必须存在、未软删、且 `face.personCode == personCode`；
    **不存在/已软删 → 404 `NOT_FOUND`**，**属于别人 → 400 `PARAM_INVALID`**（见文末偏差第 2 条）
  - 空串 / `None` ⇒ 清空（回退代表脸），**合法**
- ⚠️ **不写 `pb_review_log`**、**不重算质心**、**不 rebucket**：头像是展示，不是归属
- 响应：`changedFields` 天然含它；`personSummary` 已返回 `avatarFaceCode`

### 2.2 前端：Tab2 就地设默认

- ⚠️ 走既有封装：`store/persons.js` 加 `setAvatar(personCode, faceCode)`（内部调 `api/contacts.js` 的 `patchContact`，成功后重取 `fetchPerson` + `fetchFaces`）。**组件里不要直接 axios**
- 两段样本（554–571 / 593–632）每张加「设为默认」
- **当前默认那张**：加实心描边 + 「默认」角标（⚠️ 描边语义沿用 `utils/faceState.js` 那套，**不要另写配色**）
- 头部接「改头像」按钮（设计稿 P-05 操作行里本来就有）：打开「选择默认头像」弹窗 —— 列出全部样本、点选即设、内含「清除默认」
- 选中**自动归属段**的样本时给一句说明：「这张是机器认的、还没确认；**头像只影响展示，不影响匹配**」
- 成功 Toast「已设为默认头像」；清除后 Toast「已清除默认头像，已回到自动代表脸」

### 2.3 边界

| 情况 | 行为 |
| --- | --- |
| 默认那张脸被「移除」/「合并走」 | `avatarFaceCode` **留着不动**，展示层自动降级到代表脸（DR-41 ④ 的刻意选择） |
| 库里没有任何脸 | 首字母占位；「设为默认」入口**隐藏或禁用并说明原因** |
| 已停用的人 | 头像照旧（`opacity-60`），不改行为 |
| 传别人的 `faceCode` | 400，且库里一个字不变 |

## 三、样式与可访问性

| 项 | 要求 |
| --- | --- |
| 卡片头像 | 圆形（沿用现有 `rounded-full`），96px，`object-cover` **铺满圆框**。⚠️ **不要**在圆框内留一圈底色环把照片缩小 —— 2026-10-08 试过（`p-2` + 内层 `rounded-full`，照片缩到 ~75%），观感成了两个同心圈，用户否决并已回退 |
| 「默认」角标 | **不只靠颜色**（DR-16② 的三重编码纪律）：图标 + 文字「默认」+ 描边 |
| 可点区域 | 「设为默认」是真 `<button>`，带 `aria-label`（含年份），有 `focus-visible` 环 |
| 深色主题 | 两套主题都过一遍（用 `bg-card` / `border-line` 类，不硬编码色） |
| 图片加载 | 一律 `loading="lazy"` + `decoding="async"`；`/api/face` 已是 160px 方图，前端不再裁 |

## 四、验收清单（逐条实际运行）

1. P-04 一页 24 人：**所有「有人脸」的人卡片中间都显示本人照片**，不再全是字母
2. **没有任何脸**的人仍显示首字母，且接口 200、无报错、无破图
3. 手动把某人的 `avatarFaceCode` 改成一个**已软删的 faceCode** → 卡片自动降级到代表脸（**不出现破图**）
4. P-05 Tab2 **人工确认段**任一样本可设默认：设置后**详情头部与 P-04 卡片同时更新**
5. P-05 Tab2 **自动归属段**任一样本可设默认（并出现「只影响展示」的说明）
6. 「清除默认」后回到代表脸（`avatarFaceCode` 变 NULL）
7. 传**别人的** `faceCode` → **400**，且 `pb_person` 该行一个字不变（连 `modifyYMDHMS` 都不变）
8. 传不存在的 `faceCode` → **404**（不是 500，也不是静默成功）
9. 设默认**不影响匹配**：`pb_person_centroid` 的 `modifyYMDHMS` 不变；`pb_face` 零变动
10. 设默认**不产生 `pb_review_log`** 记录（前后 `COUNT(*)` 一致）
11. 失效场景：把默认那张脸用「✕ 移除」（`fix('unknown')`）掉 → 卡片与头部**自动降级**，无破图
12. **N+1 检查**：`/api/persons?size=24` 的 SQL 日志里 `pb_face` 查询**只有 1 条**（不是 24 条）
13. 停用的人仍正常显示头像（`opacity-60`），不报错
14. 深色主题下卡片头像与「默认」角标均可辨
15. 无障碍：「设为默认」是真 `<button>` + `aria-label`；「默认」角标有文字、不只靠颜色
16. `npm run build` 通过；`npm run dev` 手动过一遍
17. `pytest code/src/test -q` 全绿（R7 完成后基线 **1371 passed / 3 skipped**；改动前为 1237）
18. **原图零风险**：`d:\PhotoLib\photo` 的文件数与总字节数全程不变
19. `git status` 里**没有任何 `pb_*.txt` 改动**（本步**不改表**：`avatarFaceCode` 列早就存在）

## 五、硬约束
- **只写 `pb_person.avatarFaceCode` 一列**：不碰 `pb_face`、不碰 `pb_person_centroid`、不写 `pb_review_log`
- **不改表、不重跑生成器、不需要 `build_db --migrate`**（`avatarFaceCode` 列已存在）
- **不新增任何图片编辑/覆盖/删除入口**（原图只读红线）：设头像只写一个 faceCode，**不生成任何图片文件**
- 代表脸**必须批量 IN**；列表接口**不碰文件系统**
- 一切读写经既有 `store` / `api` 封装；业务层禁止裸 SQL
- 复用既有分层与双主题 token；`<script setup>` + Element Plus + Tailwind 风格保持一致
- 单文件不超过 300 行（`PersonDetailView.vue` 已接近上限；让文件超标就抽 `components/common/AvatarPicker.vue`）
- 不要顺手接通讯录头像 `avatarFile`，不要顺手做「头像上传/裁剪」—— 都不是本步

## 六、完成后必须输出
1. 改动文件清单（后端 / 前端分开列）
2. 验收结果（19 条逐条说明实测情况）
3. 第 12 条的实测证据：`/api/persons?size=24` 的 SQL 条数
4. 一张 P-04 的实际截图（卡片中间出现照片之后的样子）
5. `SELECT COUNT(*) FROM pb_review_log` 在设置头像前后的对比值
6. 遗留问题与需要我决策的点
```

---

## ⚠️ R7 落地时的偏差（执行后回填，供以后回看）

| # | 提示语原样 | 落地成什么 | 为什么 |
|---|---|---|---|
| 1 | `avatarFaceCode` 失效时「展示层自动降级到代表脸」 | **降级由服务端 `personCoversOf()` 做**，不是前端；前端**只认 `coverFaceCode`** | 第一版把口径写成 `coverFaceCode = avatarFaceCode or 代表脸`，**没校验那张脸还在不在他名下** ⇒ 默认失效后卡片仍指向已删除的脸（`/api/face` 404 = 破图），DR-41④ 承诺的回退**没有发生**。用例 `test_defaultFaceRemovedDoesNotClearAvatar` 当场抓出。修法：新增 `_liveFaceOwnersOf()` 一次 IN 判「还活着且还属于他」，`personSummary` 不再自己 `or`（详见 DR-40 落地补充） |
| 2 | 提示语写「不存在的 faceCode → **400**」 | **404 `NOT_FOUND`** | 本项目已有约定：`personCode` / `photoCode` / `faceCode` 查不到一律 404（`dto.CODE_NOT_FOUND` 的说明里就列了 faceCode）。为单个字段改全局错误码映射会让前端多记一个特例。「这张脸属于**别人**」仍是 400（请求值不对） |
| 3 | 四处接线（persons / person detail / contacts / place） | **六处**（多了 PATCH 响应与家庭成员） | 那两个接口也在返回 `personSummary`。少接线的一处症状是「同一张脸在 A 页有照片、B 页是首字母」，接口全 200、不报错 |
| 4 | 「每张样本加『设为默认』」 | 人工确认段用**实心按钮**、自动段并进原有的 `确认 / 移除` 行；另加头部「改头像」弹窗 | 64px 宽的样本格塞第三个按钮会换行；自动段本来就有动作行，就地并列比新开一行短。弹窗（`AvatarPicker`）解决「样本上百张时要在 Tab2 里滚很久」的问题 |
| 5 | 「加实心描边」标出当前默认 | 改成**品牌色环（`ring`）+ ★ 图标 + 「默认」文字**，**不动描边样式** | 描边样式承担「归属来源」这一维（实线=人工确认 / 虚线=机器认的，`utils/faceState.js`）。拿它表示「这是默认」会把两种语义混成一种 |
| 6 | 未提 | 报错文案带**双方姓名**、且**不含 Markdown 星号** | message 是给人看的一句话，会被原样 Toast；只报「不支持」用户不知道下一步做什么 |

---

# 修正步骤 R8 · 照片年代修正（人工修正拍摄年 · DR-42）

> **什么时候做**：随时（与 R5 地点界面**无依赖**）。用户看完 P-03 截图提出「这个照片的年代桶是错误的」。
> **做什么**：给照片加一个「人工修正拍摄年」，并在照片详情页提供**编辑入口** —— 让这张照片里每个人脸的**年代桶**、时间筛选、年代跨度、地点年份一起变正确。
> **核心判断**：这**不是**「桶算错了」，而是喂给桶公式的**拍摄年不可信** —— 所以修照片的年份，不是改人的桶。

```text
【photo-browser · 修正步骤 R8 · 照片年代修正（DR-42）】

## 目标
① `pb_photo` 新增「人工修正拍摄年」；**有效拍摄年 = override 优先于 shotYear**。
② P-03 照片详情「拍摄信息 › 年代」行提供**编辑入口**（先看影响面，再落库）。
③ 落库后这张照片**全部**人脸重刷年代桶 + 涉及人物质心重算（DR-22 的硬顺序）。
④ 可撤销（pb_review_log opType=BUCKET_FIX）。

## 前置
步骤 1–12 与 R3 / R4a / R4b / R6 / R7 均已完成。
真实库现象：`Len Family/董家老相册/相片纸(2).jpg` 读出来是 **2019-07-19**（翻拍时间），
而照片本身拍于 1960 年代 —— 一位 1938-12-30 生的人因此落在「2016-2025」，
按真实年代应当是「1956-1965」。**公式没错，输入错了。**

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-42** 与 **DR-42 落地补充**（本步全部口径）
- plan/数据库设计.md §4.4 `pb_photo` 的「有效拍摄年」段（含为什么表达式不走索引）
- plan/开发计划.md「⚠️ 修正步骤 R2」（DR-20/21/22：桶是派生值、刷桶与重算的硬顺序）

## 必须先读现有代码
### 1. `code/src/engine/match/` —— 分桶与刷桶
- `bucket.py` 第 235–271 行 `bucketKeyAdaptive(shotYear, birthYear)`：桶 = f(年, 生日)，**规则只在这一处**
- `rebucket.py` 第 200–218 行 `shotYearOf()`：**全项目唯一**的 photoCode → 年份回查点（改这一处即覆盖 `rebucketFace/Photo/Person/All` 全部路径）
- `rebucket.py` 第 357–390 行 `rebucketPhoto()`：一张照片的全部人脸重刷（只改 `shotBucket` 一列）
- `centroid.py` 第 389–419 行 `recomputePerson()`：会 `dropBucket` 掉没样本的旧桶，**执行前**跑 DR-22 前置检查
### 2. `code/src/api/photoAction.py` —— 照片级写的家（两段式范式照抄 `soft-delete`）
### 3. `code/src/processor/review/assigner.py` 第 108–129 行（opType 登记表）+ `merger.py` 的 `undo()`（**唯一**逆操作入口）
### 4. `code/webserver/src/views/PhotoDetailView.vue` 第 702–760 行「拍摄信息」区块

## 一、数据层
1. `pb_photo.txt` 加 `shotYearOverride SMALLINT NULL`，注释写明「人工修正拍摄年 分桶优先 空=未修正」。
2. 重跑 `python code/src/database/sqliteCodeGenerator.py`（⚠️ **全量，不要用 `-i`**），
   再跑 `python code/src/tools/build_db.py --migrate`（只加列，不动数据）。
3. ⚠️ **不要把新列加进 `runner._META_FULL_COLUMNS`**：重扫是"整列替换"语义
   （`updateColumns` + `forceColumns`），加进去 = 每次重扫都把人工修正洗回 EXIF 年份。

## 二、口径：有效拍摄年（只有两个出口）
- SQL 侧 `comGD.sqlEffectiveShotYear(alias)`；Python 侧 `rebucket.effectiveShotYear()`。
- 要替换的读点（**缺一处 = 某处仍按 2019 算，且两处各自看着都对**）：
  `browse.py`（列表 / 详情 / `shotYearFrom/shotYearTo` 筛选 / 排序白名单 / 人物年代跨度）、
  `place.py`（年筛选 / 年代直方图 / 地点人物跨度）、
  `placeStore.py`（字典 `rebuildPlaces` + 实时聚合 `liveAggregatePlaces` 两路的 `MIN/MAX`）。

## 三、写入口（新建 `processor/photoTimeFix.py`）
- `previewFix()` **纯读**，必须返回 `persons[]`：谁、从哪个桶、到哪个桶
  （写入的是**照片级**年份，而桶按各人生日现算 —— 一张合影里三个不同生日的人会朝三个方向变）。
- `applyFix()` 顺序：① 写 override → ② `rebucket.rebucketPhoto` → ③ `centroid.recomputePerson`。
- `revertFromLog()`：撤销 = 把 override 写回原值 + 重刷桶 + 重算质心。
- 日志：**主日志 + 每个受影响人一条成员日志**。成员日志必须带 `toPersonCode` ——
  `/review/log?personCode=` 是**等值**查，不带就查不到，用户的操作历史里看不见这次修正。

## 四、API
- `GET /photos/{photoCode}/shot-year-fix`（纯读预览；**省略 shotYear = 预览「恢复自动」**）
- `POST /photos/{photoCode}/shot-year-fix`（不带 `confirm=1` 只返回影响面，带了才执行）
- ⚠️ `shotYear` 必须**显式给**：不传 → 400；传 `null` → 恢复自动。两者在 JSON 里都是 None，
  只能靠 `model_dump(exclude_unset=True)` 区分 —— 混在一起会让"前端漏传"变成"静默清掉用户的修正"。

## 五、前端
- 新建 `components/photo/ShotYearFixDialog.vue`：年份输入 + 影响面清单 + 「恢复自动」。
- `PhotoDetailView.vue`：拍摄信息加「年代」行（值 + 「人工修正」角标 + 「修正」按钮）。
- `api/photoAction.js`：`getShotYearImpact` / `fixShotYear`（恢复自动要**显式**传 `{shotYear: null}`）。
- 撤销侧：`PersonDetailView.vue` 的撤销按钮文案与成功提示必须按 `opType` 区分 ——
  `/review/revertible` 是**通用**可撤销列表，最新一条可能是 BUCKET_FIX。

## 验收
见 plan/开发计划.md「修正步骤 R8」的验收栏（8 条），新建 `test/test_photo_time_fix.py` 逐条钉住。
**最容易漏的一条**是 `test_rescanUpsertKeepsOverride`（重扫 upsert 之后修正还在）——
它守的是"新列没被顺手加进扫描器的列白名单"这个边界。
```

## 落地与原提示语的差异（R8 实测）

| # | 原提示语 | 落地成什么 | 为什么 |
|---|---|---|---|
| 1 | 用户提「**拖拽**方式或者编辑方式」 | 本轮**只做编辑入口**（P1 再做拖拽） | 拖拽的落点是「**这个人**的桶」，而写入的是**照片级**年份 ⇒ 反解有歧义（10 年桶只能保证落在桶内）；且 `BucketTimeline` 得先有一个「可选桶清单」（含 0 张的空桶）才能当放置区，那是新增 API + 组件改造。编辑入口不依赖这些，且语义更贴近真相（"这张照片是 1960 年拍的"） |
| 2 | 修「年代桶」 | 只修**到年**（`takenAt` 一个字不动） | 桶只由年决定（用户明确要的也是桶）；改 `takenAt` 会覆盖掉「扫描器读到的 EXIF 时间」这个事实，还引入一个本需求不需要的第二真相。代价是照片流的「年月」分组里它仍在 2019 那一格 —— 已记在 R8 的「已知取舍」 |
| 3 | 撤销 | **复用** `POST /api/review/undo`（`merger.undo` 分流委托），不新开端点 | 撤销入口只能有一个：两套入口 = 两套闸门（`isRevertible` / `revertedByLogCode`），迟早分叉。⚠️ 委托时要把 `PhotoTimeFixError` **转成 `MergeError`** —— `api/review.py` 的 `_codeOf()` 是按**错误文本**映射状态码的，直接抛新异常会变成 500（"撤销两次"本该是 409） |
| 4 | 日志 | **主日志 + 每人一条成员日志** | 主日志的 `from/to` 都是空的（一次修正可能牵动多人，写谁都不对），而 `/review/log?personCode=` 是等值查 `toPersonCode` ⇒ 只写主日志的话，**用户在这个人的操作历史里什么都看不到** |
| 5 | 落库后重算质心 | 重算失败**不当作整体失败**，转成 `warnings[]` 返回 | `recomputePerson` 会先跑 DR-22 前置检查；被修正的人**别的照片**若还残留旧口径桶键，它会抛 `BucketStaleError` —— 而本次这张照片的桶**已经刷对了**。「静默吞掉」与「整次操作失败」都不对，所以把建议命令交给界面显示 |
| 6 | 未提 | 生成器**必须全量跑**（`sqliteCodeGenerator.py` 不带 `-i`） | 实测踩到：`-i pb_photo.txt` 只生成这一张表的区块，把产物里其余 9 张表的 CRUD **全抹掉了**（2600+ 行 → 997 行）。它是"只处理指定表"的语义，不是"只更新指定表" |

---

# 修正步骤 R4b · 目录名线索接入（DR-25/30/32/33/34/35）

> **什么时候做**：R4a 已完成，随时可做。
> **做什么**：把「目录名里的地点」接进地点字典。**这是地点维度最大的一块数据** ——
> 目录名线索约 **598 张**，是有效 GPS（65 张）的 **9 倍**。

```text
【photo-browser · 修正步骤 R4b · 目录名线索接入】

## 目标
让「目录名里写着地点」的照片也能进地点字典。做法：`pb_photo` 存目录名解析结果，
`rebuildPlaces()` 的聚合键改成「目录名优先」，并重跑一次（顺带清掉 26 张幽灵行）。
**不做界面。**

## 前置（已核实，请自己再确认一遍）
- R4a 已完成：`pb_place.nameZh` 已加、`processor/place/placeNameZh.py` 已交付（45KB）
- `pb_place` 现有 **17 行**，16 行有 `nameZh`（唯一 NULL 是 `(0,0)` 那批，判定正确）
- `pb_photo` 现有 2137 行；`placeName` 非空 **65** 张；`lat/lon` 非空 **91** 张
- ⚠️ `pb_place.photoCount` **合计 91**，比实际 65 多 **26** —— 见 DR-33（R3 清数据后没重跑 rebuild）

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-25 / DR-30 / DR-32 / DR-33 / DR-34 / DR-35 / DR-36**
- plan/数据库设计.md §1.3 命名规范、§1.4 尾部七字段、§五 索引清单

## 必须先读现有代码
### 1. `code/src/processor/place/placeStore.py`（27KB，本步主要改动面）
- `makePlaceCode(placeName)` —— **幂等键派生，本步要改它的输入语义**（见第三节）
- `REBUILD_COLUMNS = ("placeName", "photoCount", "firstShotYear", "lastShotYear",
  "centerLat", "centerLon")` —— ⚠️ `nameZh` **刻意不在内**，本步**别把它加进去**
- `rebuildPlaces()` 的聚合（`GROUP BY placeName`）+ 归零逻辑（「本次没出现在 pb_photo 里的行
  `photoCount` 置 0，**不删行**」）+ `source` 刻意不进 `updateColumns`/`forceColumns`
- `listPlaces()` / `countPlaces()` / `liveAggregatePlaces()` / `nameZhMap()` / `resolvePlaceFilter()`

### 2. `code/src/database/pb_photo.txt`（31 行）
- `placeName VARCHAR(256) NULL COMMENT '逆地理地点'`
- ⚠️ **没有** `placeNameDir`

### 3. `code/src/processor/scanner/walker.py` / `meta.py`
- `relPath` 的形态（相对 photo 根、正斜杠）
- ⚠️ **不要改扫描器去写 `placeNameDir`** —— 见第五节「谁来填这一列」的说明

## 一、表结构改动

### 1.1 `pb_photo` 增列
在 `placeName` **之后**插入：
```
placeNameDir VARCHAR(128) NULL COMMENT '目录名解析出的地点名 非派生列 扫描与rebuild均只填空不覆盖'
```
- 重跑生成器 → `build_db.py --migrate`（**逐表行数迁移前后一致**，只加列不动数据）

### 1.2 `pb_place.txt` 的注释要更新（DR-34）
`placeName` 的语义在 R4b 后变成「**聚合键兼显示名**」，来源可能是 GPS 英文、也可能是目录名中文：
```
placeName VARCHAR(256) NOT NULL COMMENT '聚合键兼显示名 来源=目录名(优先)或GPS逆地理 目录名地点已是中文故nameZh可空'
```

## 二、新增 `code/src/processor/place/dirNamePlace.py`

```python
#: 判据正则（**做成配置，见 basicSettings**）—— DR-32 实测得出的口径
#: 「日期前缀 + 非空地名」：A 类全带日期，人名/组名全不带
DIR_PLACE_PATTERN_DEFAULT = r"^(?P<y>\d{4})[.\-/]?(?P<m>\d{2})[.\-/]?(?P<d>\d{2})\s*(?P<name>\S.*)$"

def parseDirName(dirName: str) -> dict:
    """目录名 -> {isPlace, placeName, dateHint, reason}
    ⚠️ 判据要**可解释**：返回 reason 说明为什么采纳/排除，
       便于输出「采纳/排除清单」让人核对（验收第 2 条）"""

def placeNameDirOf(relPath: str, depth: int = 1) -> str:
    """relPath -> 目录名地点名（空串 = 无线索）。
    depth=1 先看父目录；父目录无名时**可考虑**上溯祖父目录
    —— 但要注意 `2013.07.26 华盛顿/xxx/` 这种，祖父目录就是它本身。
    ⚠️ 上溯要有**明确终止条件**，不要一路爬到 photo 根目录。"""

def scanDirNames(dryRun=True) -> dict:
    """遍历 pb_photo，算出每张照片的 placeNameDir。
    ⚠️ **只填空不覆盖**：已有非空值的行不动（这是它敢做非派生列的前提）。
    dryRun 报告将填多少行 + 完整采纳/排除清单。"""

def rebuildDirPlaces(dryRun=False) -> dict:
    """落库 + 报告。落库后提示调用 placeStore.rebuildPlaces()（见第三节的硬顺序）"""
```

### 判据必须做成配置（`basicSettings.py`）
```
#: 目录名 -> 地点名的判据（DR-32）。默认口径 = 「日期前缀 + 非空地名」，
#: 实测能干净分开「日期+地名」的 A 类与「人名/组名」的 B 类。
#: ⚠️ 这是**用户的命名习惯**，不是普适规律 —— 改库里的目录风格时同步调这里。
DIR_PLACE_PATTERN: str = r"^(?P<y>\d{4})[.\-/]?(?P<m>\d{2})[.\-/]?(?P<d>\d{2})\s*(?P<name>\S.*)$"
#: 明确排除的目录名（黑名单，逗号分隔）。用于正则拦不住的特例。
DIR_PLACE_BLACKLIST: tuple = ()
```

### 实测的目录清单（**请以此为验收基准，逐个给判定**）

**必须采纳**（日期 + 地名，约 598 张）：
```
114  2013.07.26 华盛顿          60  2013.07.23 尼亚加拉大瀑布
 99  2013.07.18 大都会博物馆     52  2013.07.22 千岛湖
 98  2013.07.16 纽约            51  2013.07.27 国家艺术馆
 38  2013.07.20 波士顿          15  2013.07.21 Watertown
 13  2013.07.19 罗德岛           6  2013.07.24～25 康宁及赫尔希
 52  20101218 Michael's Home
```

**必须排除**（人名 / 组名 / 学校 / 无地点）：
```
452 BaiRuiQin   157 MOT Friends   122 2011聚会    116 lianzhongwen
 98 BUPT871      75 廉家老照片      63 聚会         58 LianZhongWen
 52 DDQ          45 LianYi          37 lc           34 Friends
 24 Family       24 others source   18 LiuChang     16 Photo / MengLi
 14 LvZhenhua    14 Wang            14 shiyu         9 Lian Family
  8 xiaoyun       5 毕业照           4 lianzhongming 4 mengli / DengGang
  4 老照片
```

**需要你给结论的特例**（**必须在报告里给理由**）：
| 目录 | 张数 | 待判 |
|---|---|---|
| `2011聚会` | 122 | 有年份 + 事件名，**无地点** → 我的倾向：**排除**（"聚会"不是地点） |
| `20051229` | 22 | **只有日期、地名部分为空** → 排除（没有地点信息） |
| `201105` | 8 | 同上 → 排除 |
| `20101218 Michael's Home` | 52 | 日期 + `Michael's Home`。"xx 的家"**算不算地点**？→ 我的倾向：**采纳**（它是具体地点，比它没有强），但请你判定 |
| `廉家老照片` | 75 | 无日期，`廉家` 是家族名 → 排除 |
| `2013.07.24～25 康宁及赫尔希` | 6 | 日期含**波浪号区间** + 两个地名 → 请给结论（我的倾向：采纳，取整串作名字） |

## 三、`rebuildPlaces()` 的聚合键改动（本步最需要小心的改动）

### 3.1 聚合键
```sql
-- 改前
SELECT placeName, COUNT(*) ... FROM pb_photo GROUP BY placeName
-- 改后（DR-32：目录名优先，DR-25）
SELECT COALESCE(NULLIF(placeNameDir, ''), placeName) AS placeKey, COUNT(*) ...
  FROM pb_photo
 WHERE COALESCE(NULLIF(placeNameDir, ''), placeName) IS NOT NULL
 GROUP BY placeKey
```
- `placeKey` 就是新的 `pb_place.placeName`，并据此走既有的 `makePlaceCode()` 派生 `placeCode`
  —— ⚠️ **`makePlaceCode` 本身不改**（`placeStore` 文件头明确「一个字都不改」的纪律）
- `centerLat/centerLon`：目录名地点**没有坐标** ⇒ 留 NULL（DR-34）。
  ⚠️ 聚合时**不能**因为 `AVG(lat)` 全 NULL 就把整列塞 0 或不写，要能正确留 NULL

### 3.2 ⚠️ 硬顺序（写进 `rebuildDirPlaces` 的注释与 CLI 提示）
```
① dirNamePlace.scanDirNames()   填 pb_photo.placeNameDir
② placeStore.rebuildPlaces()    按新聚合键重建地点字典
③ placeNameZh.rebuildNameZh()   （可选）补 GPS 地点的中文名
```
反了会怎样：先 rebuild 再填 `placeNameDir` ⇒ 字典里全是旧的 GPS 地名，
新填的目录名**这一轮完全不生效**，而 `rebuildPlaces` 会打印「更新 N 个地点」看着很正常。

### 3.3 幂等键漂移的连带处理（**DR-32 明确要求，别省**）
目录名会被用户改动（实测：两天内 `张家界` → `华盛顿`）。改一次名 ⇒ 新 `placeCode` + 旧行归零。必须：
1. **旧行只归零、不删除** —— 现有逻辑已支持，别破坏
2. **手工行（`source=1`）永不归零**
3. **新增「孤儿 `nameZh` 报告」**：`photoCount = 0` 但 `nameZh` 非空的行走一遍并输出
   —— 手工填过的中文名会因改名变成孤儿，**必须让用户看见而不是静默丢**
4. **map 陈旧检查**：`liveAggregatePlaces()` 走了实时聚合（降级路径），
   它的数据源是 `pb_photo` 的 GROUP BY —— 本步改了聚合键后，**这条降级路径也要跟着改**，
   否则「字典表未 rebuild 时」会给出与字典表不一致的地点名

## 四、谁来填 `placeNameDir`（**明确写清，避免又出现「承诺了没人做」**）

| 时机 | 做什么 |
|---|---|
| **本步的 `scanDirNames()`** | 一次性全量填（首次接入） |
| **扫描器（步骤 3）** | ⚠️ **本轮不改扫描器** —— 理由：改扫描器要动 `runner.py` 的写库批次与 `forceColumns`，风险高于收益。改为在**文档与注释里写明**：「`placeNameDir` 由 `dirNamePlace.scanDirNames()` 填，扫描器不负责」，并提供一个 CLI 便于改名后重跑 |
| **目录改名后** | 用户手动跑 `scanDirNames()` + `rebuildPlaces()`（重扫会更新 `relPath`，但**不会**自动更新 `placeNameDir`）⚠️ 这个限制必须写进 CLI 的输出提示里 |

⚠️ 这是一个**已知取舍**，请把它写进 `dirNamePlace.py` 的文件头，并在完成后**主动报告**：
「目录改名后需要手动跑两个命令」——不要让用户自己发现。

## 五、CLI：`code/src/tools/place_cli.py`

```
--scan-dir       从 relPath 解析并填 pb_photo.placeNameDir（--dry-run 只报告）
--audit          巡检报告（见下）
--rebuild        全链路：scan-dir -> rebuildPlaces ->（可选）rebuildNameZh
--photo <code>   单张照片的地点解析过程（排障用）
```

`--audit` 必须输出（这是本步的**主要验收产物**）：
1. **字段级总数**：照片总数 / `placeNameDir` 非空 / `placeName` 非空 / 两者都空
2. **采纳/排除清单**：每个不同目录名 → 采纳还是排除 + 理由 + 张数（**全量，不是 top N**）
3. **地点清单**：`placeCode / placeName / nameZh / source / photoCount / firstShotYear / lastShotYear / centerLat / centerLon`
4. **幽灵行**：`photoCount = 0` 的行（含 `(0,0)` 那批的 26 张）
5. **孤儿 `nameZh`**：`photoCount = 0` 但 `nameZh` 非空
6. **重名 `nameZh`**：同一个 `nameZh` 出现多行（DR-36，如「北京市 · 朝阳区」两行）
7. **无中心点地点**：`centerLat` 为 NULL 的地点（目录名地点全是）

## 六、验收清单（逐条实际运行）

### A. 表与迁移
1. `pb_photo.placeNameDir` 已加，重跑生成器，`build_db.py --migrate` 成功
2. **逐表行数迁移前后一致**（`pb_photo` 2137 行、`pb_place` 17 行 等）
3. `PRAGMA table_info(pb_photo)` 含 `placeNameDir`；`pb_place.txt` 的 `placeName` 注释已更新

### B. 判据（**本步最关键的第二条**）
4. `--scan-dir --dry-run` 输出**完整**的采纳/排除清单（52 个不同目录逐个给判定与理由）
5. **必须采纳的 11 个目录全部采纳**（上面列的那批，约 598 张）
6. **必须排除的全部排除**（`BaiRuiQin` / `MOT Friends` / `lianzhongwen` / `BUPT871` /
   `Family` / `Friends` / `廉家老照片` / 各类人名拼音…）
7. 六个特例（`2011聚会` / `20051229` / `201105` / `20101218 Michael's Home` /
   `廉家老照片` / `2013.07.24～25 康宁及赫尔希`）**逐个给结论与理由**
8. 判据正则与黑名单**已做成配置**，不是硬编码在函数里
9. 实跑后 `placeNameDir` 非空的行数 = 预期（约 598，请给出精确数）
10. **只填空不覆盖**：手工把某一行的 `placeNameDir` 改成别的值 → 再跑 `--scan-dir` → **该值不变**

### C. 聚合与字典
11. 重跑 `rebuildPlaces()` 后：地点数、`photoCount` 合计 == `placeNameDir` 非空 + `placeName` 非空
    （**请给出精确对照**）
12. **26 张幽灵清零**：`PL_GH_Western_Takoradi` 的 `photoCount` 变为 0（DR-33）
13. **目录名地点的 `placeName` 是中文、`nameZh` 是 NULL、`centerLat/centerLon` 是 NULL**（DR-34）
14. **GPS 地点的 `nameZh` 未被改动**（R4a 的成果不回归）
15. **展示契约验证**：`nameZh ?? placeName` 对两类地点都给出正确显示名（逐类举 3 例）
16. 手工行（`source=1`）在 rebuild 后**仍为手工行**且 `photoCount` 未被归零
17. `liveAggregatePlaces()`（降级路径）**也用了新聚合键** —— 与字典表的结果一致
18. **孤儿 `nameZh` 报告**能报出（构造：手工填一个 `nameZh`，然后把该行的照片改到另一个目录 → 重跑）
19. **重名 `nameZh` 报告**能报出「北京市 · 朝阳区」两行（DR-36）

### D. 顺序与幂等
20. `--rebuild` 全链路（scan-dir → rebuildPlaces）跑通；**跑两遍结果一致**（幂等）
21. 故意**反序**（先 rebuild 再 scan-dir）→ 明确提示「必须先 scan-dir」而不是静默给出旧结果
22. 改名演练：把某个目录改名（构造数据，**不要动真实 photo 目录**）+ 重扫 + `--rebuild` →
    新 `placeCode` 生成、旧行归零、孤儿 `nameZh` 报告出现

### E. 回归
23. `pytest code/src/test -q` 全绿（当前基线 **1237 passed**）
24. 前端构建通过（`npm run build`）—— 本步应**不含任何前端改动**，仅证明未被影响
25. **photoDir 零风险**：全程文件数与总字节数不变
26. **不改扫描器**：`git status` 里不应有 `processor/scanner/*` 的改动

## 七、硬约束
- **原图绝对只读**
- 业务层**禁止裸 SQL**，一律经 `sqliteCommon`（`query` 模块的 `selectList` 用于复杂聚合，
  现有 `rebuildPlaces` 已在用，照它来）
- **`makePlaceCode()` 一个字都不改**（`placeStore` 文件头的纪律）
- **`nameZh` 绝不进 `REBUILD_COLUMNS`**，`placeNameZh.py` **一个字都不改**（DR-34）
- **`placeNameDir` 只填空不覆盖** —— 这是它敢做非派生列的前提
- **旧行只归零不删除**；手工行永不归零
- **不引入 geohash**（DR-35：目录名无坐标，收益已被 `nameZh` 替代）
- **不改扫描器**（第四节）
- **不对 DR-36（重名 `nameZh`）擅自实现归并** —— 只报告，等用户定
- 现有代码风格（文件头纪律说明、`_VERSION`、日志、`REBUILD_COLUMNS` 的警告注释）保持一致

## 八、完成后必须输出
1. 改动文件清单
2. 验收结果（26 条逐条给命令与实际输出）
3. **完整的采纳/排除清单**（52 个目录逐个 + 理由 + 张数）← R5 界面设计的依据
4. **改后 `pb_place` 全表**（placeCode / placeName / nameZh / source / photoCount / 年份 / 中心点）
5. 第 11 条的精确对照：`photoCount` 合计 vs `placeNameDir` 非空 + `placeName` 非空
6. 六个特例的判定与理由
7. `--audit` 的七项输出（幽灵行 / 孤儿 nameZh / 重名 nameZh / 无中心点地点）
8. 遗留问题与需要我决策的点（**含目录改名后需手动跑两个命令这个已知取舍**）
```

---

# 修正步骤 R5 · 地点界面（地点 → 照片流 + 人物「去过的地方」）

> **什么时候做**：R4a / R4b 已完成，随时可做。
> **形态已按 R4b 实测数据调整**（见 DR-37）：主路径是**地点 → 照片流**，不是「人物 → 地点列表」。
> **本轮不做地图**（11/28 个地点无坐标）。

```text
【photo-browser · 修正步骤 R5 · 地点界面】

## 目标
把已有的地点数据做成可浏览的界面：
① **地点列表**（`/places`）→ ② **地点详情**（照片流 + 在场的人）
③ **人物详情 Tab1「时间轴」后面**加「去过的地方」区块 ← **用户点名要的位置**（见 §3.3）
④ 照片各处的地点显示改成中文。

## 前置（已核实，请自己再确认一遍）
- R4a 已完成：`pb_place.nameZh`（中文行政区名）+ 照片接口的 `placeZh` + 筛选两套值
- R4b 已完成：`pb_photo.placeNameDir` + 聚合键 `COALESCE(placeNameDir, placeName)`
- **实测数据**：`pb_place` **28 行**（11 个目录名地点 + 17 个 GPS 地点）；`photoCount` 合计 **663**
  （598 目录名 + 65 GPS）；`pb_person` 2029；`pb_face` 3647 但**只有 67 个有归属**；
  `pb_photo_person` 仅 66 行

## ⚠️ 必读：两个实测结论决定了界面形态（DR-37）

### ① 人物 × 地点交叉几乎为空
有地点的照片 663 张里，**已关联到人物的只有 1 张**；`pb_face` 有归属的只有 67/3647。
⇒ **按「先选人物 → 再看地点」做，点进去是空界面。**
⇒ 主路径改为 **地点 → 照片流**；人物维度做成人物详情页里的一个区块。

### ② 11 个目录名地点没有坐标
目录名地点占 663 张里的 **598 张（90%）**，`centerLat/centerLon` 全为 NULL。
⇒ **地图做不了**（只有 17 个 GPS 点可画，照片最多的 11 个地点会在地图上消失）。
⇒ **本轮不做地图**；后续要做得先解决「目录名地点无坐标」这个前提。

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-25 / DR-28 / DR-34 / DR-36 / DR-37 / DR-38**（地点口径与形态决策）
- plan/UI/photo-browser UI 设计.md 第四节（P-02 照片流、P-04 人物库、P-05 人物详情）、第七节（双主题 token）
- plan/照片管理方案_开源调研与自研设计.md 3.10（前端视图清单里「地点」那行 —— 原设想含地图，本轮不采纳）

## 必须先读现有代码
- `code/src/processor/place/placeStore.py`（43KB）—— `listPlaces` / `countPlaces` /
  `nameZhMap` / `resolvePlaceFilter` / `liveAggregatePlaces`，**本步的接口都基于它**
- `code/src/processor/place/placeNameZh.py`（45KB）、`dirNamePlace.py`（48KB）—— 只读参考，**不改**
- `code/src/api/browse.py` —— `GET /api/places`（约 1285 行）、`GET /api/photos/{photoCode}`
  （`out["gps"]["placeZh"]`）、`photoSummary()`、`resolvePlaceFilter()` 的用法；
  **新增端点要照 `GET /persons/{personCode}/timeline`（第 998 行）的写法摆在同一文件里**
- `code/src/api/browse.py` 的 `GET /photos`（约 460 行）—— **确认它同时吃 `personCode` 与
  `placeName`**（§3.3 的点击跳转依赖这个，已核实存在，但要自己再看一眼参数校验）
- `code/webserver/src/views/PersonDetailView.vue`（33KB）—— **重点看第 515–532 行 Tab1 时间轴**，
  §3.3 就要插在这一段里；同时看第 34–42 行的 import 与 `store/persons` 的 `fetchTimeline` 用法
  （新的 `fetchPlaces` 照它写）
- `code/webserver/src/views/PhotosView.vue` / `PhotoDetailView.vue`
- `code/webserver/src/components/photo/{PhotoThumb,BucketTimeline,PhotoPager}.vue`
- `code/webserver/src/components/common/{PersonCard,PersonForm}.vue` —— 新组件同目录同风格
- `code/webserver/src/router/index.js`、`store/`、`api/` —— **照既有分层写，不要新起一套**

## 一、显示契约（**先定这个，全步都依赖它**）

```js
// 地点显示名的唯一入口，所有视图都调它，不要各写各的
export function placeDisplayName(place) {
  return place.nameZh || place.placeName || '未知地点'
}
```
| 地点类型 | `placeName` | `nameZh` | 显示 |
|---|---|---|---|
| GPS 地点 | `CN, Beijing, Datun`（英文） | `北京市 · 朝阳区` | **中文** |
| 目录名地点 | `华盛顿`（已是中文） | `NULL` | **华盛顿** |
| 幽灵行（`photoCount=0`） | `GH, Western, Takoradi` | `NULL` | **不展示**（过滤掉） |

⚠️ **后端列表要过滤 `photoCount > 0`**（幽灵行不该出现在界面）；
若接口不支持，前端过滤并**在报告里说明**。

## 二、后端：补 **5** 个只读接口（**不是纯前端改动**）

- **`code/src/api/place.py`（新建）**：4 个 `/api/places/*` 端点，照 `browse.py` 的分页/筛选/DTO 写法
- **`code/src/api/browse.py`（改）**：1 个 `/api/persons/{personCode}/places`
  —— ⚠️ **必须放这里**，与第 998 行 `/persons/{personCode}/timeline`、第 917 行
  `/persons/{personCode}/faces` 同模块（那两条已存在，本条是它们缺失的兄弟端点；
  R4a/R4b 的提示语里登记过但**实测未实现**，本步补上）
- 两边都**禁止裸 SQL**（走 `database/queryCommon.py` 查询出口，步骤 9 已建立）

| 端点 | 所在文件 | 返回 | 关键点 |
|---|---|---|---|
| `GET /api/places?groupByNameZh=1` | `place.py` | 地点列表（分页 + `q` 模糊 / `minPhotos` / `year` / `hasPerson`） | **按 `nameZh` 分组归并**（DR-36 方案①） |
| `GET /api/places/{placeCode}` | `place.py` | 地点详情（含 `photoCount` / 年份跨度 / 坐标 / `placeCodes` 同组列表） | 支持 `?placeCodes=a,b,c` 表示「已归并的同组」 |
| `GET /api/places/{placeCode}/photos` | `place.py` | 该地点的照片（分页，按年分组） | 复用 `photoSummary()`，带 `placeZh` / `thumb` / `shotYear` |
| `GET /api/places/{placeCode}/persons` | `place.py` | 出现在该地点的人 + 各自照片数 | **实时 `DISTINCT` join `pb_photo_person`**（DR-26，不落库） |
| `GET /api/persons/{personCode}/places` | **`browse.py`** | 某人去过的地方（按 `lastShotYear` 倒序）+ `photoTotal` / `locatedPhotoTotal` | 同上，实时 join；**3.3.1 有完整契约** |

### 2.1 重名归并（DR-36 方案①）—— 本步最容易做错的一处
```
实测重名 3 组：
  北京市 · 朝阳区       PL_CN_Beijing_Datun(8) + PL_CN_Beijing_Wangjing(7)  → 归并后 15
  新疆维吾尔自治区 · 特克斯县  Hujirti(6) + Tekes(1)                        → 归并后 7
  新疆维吾尔自治区 · 伊宁市    Yengiyar(8) + Huiyuan(1)                     → 归并后 9
```
- **`placeCode` 一个字都不改**（R4b 刚确立的纪律，改了幂等键会漂移）
- 归并只发生在**展示层**：同 `nameZh` 的多行合并成一行，`photoCount` 求和，
  `firstShotYear`/`lastShotYear` 取并集的最值
- 组内多个 `placeCode` 要**能下钻**：列表项点击后带 `placeCodes` 进详情，
  详情里可用 tab / 分段列出组内各点（如「大屯 8 张 / 望京 7 张」）
- ⚠️ **归并是「展示分组」不是「数据合并」**：不要把同组的照片混成一堆而丢失来源，
  下钻时仍要能看出「这 15 张里哪 8 张来自大屯」

## 三、前端：2 个新视图 + 1 个新区块 + 3 处改造

### 3.1 `views/PlacesView.vue`（`/places`）
```
┌──────────────────────────────────────────────────────────────┐
│ 地点 28 处      [🔍 搜索地点]  [年份▾] [仅有人物的]           │
├──────────────────────────────────────────────────────────────┤
│ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ │
│ │  [封面缩略图]│ │            │ │            │ │            │ │
│ │ 华盛顿      │ │ 纽约        │ │ 大都会博物馆 │ │ 尼亚加拉... │ │
│ │ 114 张      │ │ 98 张       │ │ 99 张       │ │ 60 张       │ │
│ │ 2013        │ │ 2013        │ │ 2013        │ │ 2013        │ │
│ │ 👤 —        │ │ 👤 —        │ │ 👤 —        │ │ 👤 —        │ │
│ └────────────┘ └────────────┘ └────────────┘ └────────────┘ │
└──────────────────────────────────────────────────────────────┘
```
- 卡片：**封面用该地点第一张照片的缩略图**（`/api/thumb`）
- **重名归并项**要有视觉提示（如角标「2 处」），点进去能下钻
- 无坐标地点**不要显示坐标/地图图标**（11/28 是这样）
- `👤 n` = 该地点在场人数；现在大多是 0 或 `—`，**空状态文案要友好**（见 3.4）

### 3.2 `views/PlaceDetailView.vue`（`/places/:placeCode`）
```
┌──────────────────────────────────────────────────────────────┐
│ ‹ 地点   华盛顿 · 归并 2 处       2013     114 张            │
│           [大屯 8 张] [望京 7 张]   ← 重名组内下钻             │
├──────────────────────────────────────────────────────────────┤
│  照片(114)  │  在场的人(0)                                     │
├──────────────────────────────────────────────────────────────┤
│ 2013  ▢ ▢ ▢ ▢ ▢ ▢ ▢ ▢ ▢ ▢  ← 按年/月分组，缩略图懒加载       │
│ 2012  ▢ ▢ ▢ ▢                                                │
├──────────────────────────────────────────────────────────────┤
│ ⚠ 该地点还没有关联到人物。去「我不同意」或待确认队列确认人脸    │
│   后，这里会显示当时在场的人。                                │
└──────────────────────────────────────────────────────────────┘
```
- **照片流按年分组**（复用 `BucketTimeline` 或 `PhotoThumb` 网格），分页懒加载
- 「在场的人」**实时接口**取；**没有数据时给引导文案**（指向纠错队列），不要只显示空白
- 复用 R6 的 `PhotoPager`？—— 详情内是大图/网格，**点开照片仍走 P-03**（那里已有左右翻页）

### 3.3 `PersonDetailView.vue` · Tab1「时间轴」**后面**加「去过的地方」区块
> **用户明确要求的位置**：就在**年代桶时间轴下方**（同一个 Tab1 内，`BucketTimeline` 之后），
> **不新开 Tab**、不独立成页。（空 Tab 会比空区块更难看。）

**现状**（`views/PersonDetailView.vue` 第 515–532 行）：
```vue
<el-tab-pane :label="`时间轴（N 个年代桶）`" name="timeline">
  <div class="space-y-4 pt-2">
    <p v-if="persons.timelineLoading" class="pb-hint">正在按年代桶分组…</p>
    <BucketTimeline v-else :groups="timelineGroups" :max-per-bucket="12" @select="onTimelineSelect" />
    <p class="pb-hint">时间轴按年代桶分组（童年 3 年 / 成年 10 年）—— …</p>
    <!-- ↓↓↓ 本步在这里加「去过的地方」 ↓↓↓ -->
  </div>
</el-tab-pane>
```

**改动形态**（新组件 `components/common/PersonPlaces.vue`，与 `PersonCard.vue` / `PersonForm.vue` 同目录同风格）：
```vue
  <section class="mt-6 border-t border-line pt-4" aria-labelledby="pd-places-title">
    <h3 id="pd-places-title" class="text-body text-ink">去过的地方</h3>
    <!-- 每条：地点中文名 + 该人在此的照片数 + 年份范围；整条可点 -->
    <!-- 点击 → /photos?personCode={personCode}&placeName={placeName} -->
  </section>
```
- 每条用 `placeDisplayName()`（见 §一）显示**中文名**，禁止直接渲染英文 `placeName`
- 每条按 **`lastShotYear` 倒序**（最近去过的在前）
- 每条点击 → **`/photos?personCode={personCode}&placeName={placeName}`**
  —— ✅ 已核实 `/api/photos` **同时支持** `personCode` 与 `placeName`（`api/browse.py`），
  精确匹配且 R4a 已做「两套写法返回同一批照片」的翻译，所以这个链接能**保留人物上下文**
  （比跳 `/places/{code}` 更贴合「**这个人**去过的地方」的语义）
- 整体为空时 → **`v-if` 不渲染这个 `section`**（连标题一起隐藏），另给一行 `pb-hint`：
  「该人 **{N}** 张照片中，**{M}** 张有地点信息。」（N/M 由接口返回，见下）
  —— ⚠️ **不要只写「暂无数据」**：用户看到空白不知道是「没做」还是「真没有」。
  带出 N/M 才能让人明白「是地点线索没覆盖到，不是功能没做」。

#### 3.3.1 后端新增 `GET /api/persons/{personCode}/places`
- **放在 `api/browse.py`**（与 `/persons/{personCode}/timeline` 第 998 行、`/persons/{personCode}/faces`
  第 917 行 **同模块、同风格**；不要新建模块）
- 返回：
  ```json
  {
    "personCode": "CS_0154_Qing_Bai",
    "photoTotal": 1,          // 该人照片总数
    "locatedPhotoTotal": 1,   // 其中有地点信息的张数（空状态文案要用）
    "places": [
      { "placeCode": "PL_CN_...", "placeName": "纽约", "nameZh": null,
        "photoCount": 1, "firstShotYear": 2013, "lastShotYear": 2013 }
    ]
  }
  ```
- 实现要点：**实时 join**（DR-26），一条 SQL 搞定：
  `pb_photo_person JOIN pb_photo` → 按 `COALESCE(NULLIF(placeNameDir,''), placeName)` 分组，
  `photoCount` 用 `COUNT(DISTINCT photoCode)`，年份用 `MIN/MAX(shotYear)`；
  `nameZh` 从 `pb_place` 按同一聚合键取（**不要按 `placeCode` 取**，目录名地点的
  聚合键与 `placeCode` 可能不是一对一 —— 见 DR-36）
- **禁止裸 SQL** → 走 `database/queryCommon.py` 查询出口（步骤 9 已建立的口径）

#### 3.3.2 ⚠️ 实测数据预期（**先说清楚，免得以为做错了**）
本步实测（只读探查，非猜测）：
```
pb_photo_person 共 66 行，分布在 20 个人上
其中「照片带地点信息」的合计只有 2 行 —— 且是【同一张照片】关联了 2 个人：
  PH_ae8b0bc8e902499982fa5651919f6b37  2013 年  目录名=纽约  无 GPS
    → Qing Bai（1 张）/ RuiQin Bai（1 张）
反向：663 张有地点的照片里，只有 1 张有人物关联
```
⇒ **R5 做完后，只有 `Qing Bai` 与 `RuiQin Bai` 两人会看到「纽约 1 张」；
其余 18 人（含截图里的 Steven Lian）这个区块会隐藏，只留一行 N/M 提示。这是数据现状，不是 bug。**
⇒ 因此 §五 的验收**必须同时验证「有数据的 2 人」与「无数据的 18 人」两种情况**，
   不能因为看到空区块就判定实现失败。
⇒ 想让这个区块真正有内容，前提是**在待确认队列里给有地点的照片确认人脸**
   （现在 3647 张脸只有 67 张有归属）—— 这是 R5 之外的独立工作。

### 3.3b 侧栏「人物库」无需改动
用户说的「人物库」指**人物详情页**（截图正是 `PersonDetailView`）；`PeopleView.vue`
（人物网格）本步**不动**。

### 3.4 照片各处的地点显示（**验收重点是「中文」**）
| 位置 | 改动 |
|---|---|
| P-03 照片详情「地点」栏 | `placeZh ?? placeName`；**保留英文原值在 tooltip**（排障要看） |
| P-02 照片流角标 | `📷` 保持；有地点时 hint 显示中文名 |
| 地点筛选下拉 | **给中文名**（`nameZh`），英文值作回退项；两套都能筛（R4a 已做后端翻译） |
| 图片 `alt` | 带地点中文名（无障碍） |

### 3.5 侧栏入口
- 「地点」作为一级入口（在「人物库」之后、「待确认」之前）
- 角标：可选（地点数无「待处理」语义，**建议不加角标** —— 加了一切非待办的数字都会变成噪声）

### 3.6 本步新增 / 修改文件清单（**动手前先照这张表核对，别漏也别多**）
| 类型 | 文件 |
|---|---|
| 新增 | `code/src/api/place.py` |
| 新增 | `code/webserver/src/views/PlacesView.vue` |
| 新增 | `code/webserver/src/views/PlaceDetailView.vue` |
| 新增 | `code/webserver/src/components/common/PersonPlaces.vue`（§3.3 的「去过的地方」） |
| 新增 | `code/webserver/src/api/place.js`（照 `api/` 既有模块的 axios 封装写） |
| 新增 | `code/src/test/test_api_person_places.py`（§3.3.1 端点单测，基准用例见验收第 6 条） |
| 修改 | `code/src/api/browse.py`（+`GET /persons/{personCode}/places`；**其余端点不动**） |
| 修改 | `code/webserver/src/views/PersonDetailView.vue`（**只动 Tab1** 第 515–532 行区间：加 import + 插区块 + 加 `fetchPlaces`） |
| 修改 | `code/webserver/src/views/PhotoDetailView.vue`（地点栏中文 + tooltip） |
| 修改 | `code/webserver/src/router/index.js`（`/places`、`/places/:placeCode` 两条） |
| 修改 | `code/webserver/src/components/layout/AppSidebar.vue`（「地点」一级入口） |
| 修改 | `code/webserver/src/store/persons.js`（+`fetchPlaces`，照 `fetchTimeline` 写） |
| **不改** | `processor/place/*.py`（三个文件**一个字都不动**）、`pb_place.txt`、`pb_photo.txt`、`PeopleView.vue`、`PhotoPager.vue` |

> ⚠️ `PersonDetailView.vue` 已有 **33KB**，改的时候**只做插入**，不要顺手重构 ——
> 这个文件已经踩过「一次重排把 Tab2 人脸样本的实线/虚线语义搞混」的坑。

## 四、样式与可访问性
- 双主题（浅色默认 + 跟随系统）都要可读（R4a 之后的既有 token，直接用）
- 卡片 hover 微交互与 `PhotoThumb` 一致（上浮 2px / 120ms）
- `<1024` 侧栏收窄；`<768` 卡片 2 列
- 地点卡片是 `<a>`/`<button>`，键盘可达，`aria-label` 带地点名与张数

## 五、验收清单（逐条实际运行）

### A. 后端接口
1. `GET /api/places` 返回 **28 行**（未归并）/ 归并后 **25 行**（3 组重名各减 1）
   —— 请给出两个数字与算法
2. `photoCount` 合计仍为 **663**（归并前后一致，不能因归并丢张数）
3. **幽灵行不出现在列表**：`PL_GH_Western_Takoradi`（`photoCount=0`）不可见
4. `GET /api/places/{code}/photos` 分页正确；带 `placeZh` / `thumb` / `shotYear`
5. `GET /api/places/{code}/persons` **实时 join**：构造一次归属（给某张有地点的照片确认一个人）
   → 该接口立刻返回这个人（**证明不是落库的**）
6. `GET /api/persons/{code}/places` **实测两条基准用例**（数据现状见 3.3.2）：
   - `CS_0154_Qing_Bai` → `photoTotal=1`、`locatedPhotoTotal=1`、`places=[纽约 × 1, 2013]`
   - `VC_0702_Steven_Lian`（截图那个人）→ `photoTotal=9`、`locatedPhotoTotal=0`、`places=[]`
   —— 两条都要给实际 JSON，**证明既有数据也对、无数据也对**
7. 重名组下钻：`?placeCodes=a,b` 能正确返回组内各点的分别张数（如 大屯 8 / 望京 7）

### B. 前端视图
8. `/places` 列表：28 个地点（归并后 25 行）**全部可见**，名称全为**中文**
9. 封面缩略图正确加载（走 `/api/thumb`，**不是 `/api/original`**，Network 面板确认）
10. 重名归并项有「2 处」类提示，点进去能下钻到组内各点
11. **无坐标地点不报错**：11 个目录名地点正常展示，无地图/坐标占位
12. `/places/:code` 照片流按年分组、缩略图懒加载、分页正确
13. **「在场的人」空状态给引导文案**（指向纠错队列），不是空白
14. 构造一次归属后，地点详情的「在场的人」**刷新即出现**（呼应第 5 条）
15. **人物详情 Tab1「时间轴」后面有「去过的地方」区块**（用户点名要的位置）：
    - 打开 `VC_0702_Steven_Lian`（截图那位，9 张照片 / 0 张有地点）
      → **区块整体隐藏**（连标题一起），只留一行
      「该人 9 张照片中，0 张有地点信息。」
      —— **截图给我看这行提示确实出现**，且不是空白、不是空表格
    - 打开 `CS_0154_Qing_Bai` → 区块**可见**，标题「去过的地方」，
      下面一条「纽约 1 张 2013」，**地点名是中文**
    - 点那条「纽约」→ 跳到 `/photos?personCode=CS_0154_Qing_Bai&placeName=纽约`
      → 照片流**只剩 1 张**（人物 + 地点**双条件都生效**，这是本条的考点）
    - 时间轴本身**未被破坏**：`BucketTimeline` 仍在原位、桶数与之前一致、
      Tab2「人脸样本」不受影响
16. **照片详情「地点」栏显示中文**（你截图那张新疆的照片应是「新疆维吾尔自治区 · 阿勒泰市」），
    tooltip 里能看到英文原值
17. 地点筛选下拉给中文名；`?placeName=阿勒泰市` 与 `?placeName=CN, Xinjiang Uygur Zizhiqu, Araltobe`
    **返回同一批照片**
18. 侧栏「地点」入口可跳转；**不加角标**
19. 深色/浅色两套主题下全部可读；`<1024` / `<768` 断点正常
20. 键盘可达：Tab 能走到每个地点卡片，`aria-label` 带地点名与张数

### C. 回归
21. `npm run build` 通过；`npm run dev` 手动过一遍（含 R6 的左右翻页未被破坏）
22. `pytest code/src/test -q` 全绿（当前基线 **1237 passed** 起）
23. **`photoDir` 零风险**：全程文件数与总字节数不变
24. **不改地点数据层**：`placeStore.py` / `dirNamePlace.py` / `placeNameZh.py` 的
    数据口径无改动（本步只加接口与视图）—— 用 `git diff --stat` 说明

## 六、硬约束
- **原图绝对只读**（所有图片走 `/api/thumb`，点开才 `/api/original`）
- 业务层**禁止裸 SQL**，一律经 `sqliteCommon`
- **`placeCode` / 聚合键 / `nameZh` 一个字都不改** —— 归并只在展示层（DR-36 方案①）
- **人物 × 地点一律实时 join**，不新增关联表（DR-26）
- **本轮不做地图**（DR-37②：11/28 无坐标）；也不做「行程/事件」推断
- **不引入 i18n 框架**：地点中文名是**数据层**的事，界面只有中文一套
- 复用既有 router / store / api / 组件分层与双主题 token，**不要新起一套**
- 现有代码风格保持一致；单文件不超过 300 行（超了就把卡片/行拆成组件）

## 七、完成后必须输出
1. 改动文件清单（后端 + 前端分开列）
2. 验收结果（**24 条逐条**给命令与实际输出，第 15 条含 3 张截图：区块隐藏 / 区块可见 / 双筛选结果）
3. **地点列表的完整截图或列表文本**（28 → 归并 25 行的对照）
4. 重名 3 组归并前后的张数对照
5. 第 5 条「实时 join」的实测过程（构造归属 → 接口即刻返回）
6. `git diff --stat` 证明地点数据层未被改动
7. 遗留问题与需要我决策的点（**特别是：地图要不要做、若要做怎么解决 11 个无坐标地点**）
```

---

# 修正步骤 R9 · 照片旋转（左右转 90°）

> **什么时候做**：R5 已完成（地点界面）。**R9 与地点无关**，随时可做。
> ⚠️ **编号说明**：`R7` 已被「人物头像」（DR-40/41）占用、`R8` 是「照片年代修正」（DR-42），
> 所以照片旋转编 **R9**。别再叫它 R7。
> **核心判断**：旋转是**显示层属性**（DB 存角度 + 前端 CSS 转），**绝不烘进图片文件、绝不服务端转码**。

```text
【photo-browser · 修正步骤 R9 · 照片旋转】

## 目标
照片浏览支持左右旋转 90°（修正方向不对的翻拍件 / 扫描件），
**只改显示，不动原图一个字节**。

## 前置
- R5 已完成（`api/place.py` / `PlacesView` / `PlaceDetailView` / `PersonPlaces` 已落地）
- `api/photoAction.py` 已存在（照片级写接口的家，含软删 / 标记重复 / 年代修正 DR-42）
- 测试基线 `pytest code/src/test -q` = **1237 passed** 起

## ⚠️ 必读：三种做法只有一种能用

| 方案 | 做法 | 判定 |
|---|---|---|
| **A（必须选）** | `pb_photo.rotateDeg` 字段 + 前端 CSS `transform` | ✅ 原图零改动；缩略图缓存不失效；`/api/original` 的 Range 不受影响 |
| B | 服务端转码后返回 | ❌ 整图编码 → **Range 失效**（`api/static.py` 模块头写明「必须支持 Range，否则滚动预加载卡死」）；且每次旋转要重生成 3 档缩略图 |
| C | 改写原图 EXIF Orientation | ❌ **违反「原图只读」铁律**（`processor/media/thumbMaker.py` 第 16 行：不改动原图任何字节）；且会覆盖相机原始方向，`thumbMaker._openOriented` 与 `faceCropper` 都还在读它 |

## 必须先读的项目文档
- plan/开发计划.md 第四节 **DR-42**（年代修正 —— 本步要照抄的「用户覆盖值」先例）、**DR-16**（纠错闭环）
- plan/UI/photo-browser UI 设计.md §五 关键组件表（`PhotoThumb` / `FaceBox` 规格）

## 必须先读现有代码（**顺序别调，前两个是本步的成败点**）
- `code/src/api/photoAction.py`（224 行，全文读）—— 模块头写明了「为什么不并进 browse.py」
  「哪些动作落 pb_review_log」，**本步的端点要加在这里并遵守同一套纪律**
- `code/src/processor/photoTimeFix.py` —— `previewFix` / `applyFix` 的分层，本步照它的结构
- `code/src/api/browse.py`
  - `photoSummary()`（**第 197 行**）
  - **5 处照片 SELECT**：L482-483、L526-527、L651-652、L1204、L1265-1267
  - `photoRow()`、`FACE_*` 无关，别动
- `code/src/api/place.py` 第 808-810 行（R5 新增的第 6 处照片 SELECT）
- `code/src/processor/scanner/runner.py`
  - `_META_FULL_COLUMNS`（**第 87 行附近**）
  - `_metaColumns()`（**第 473 行附近**）
- `code/src/database/pb_photo.txt`（31 行，字段顺序）
- `code/src/tools/build_db.py` 第 224 / 340 行 —— **已有自动加列迁移**，别手写迁移脚本
- `code/webserver/src/utils/format.js` 的 `displaySize`（**第 109-120 行**，已在折算 EXIF orientation）
- `code/webserver/src/views/PhotoDetailView.vue` 的 `frameStyle`（**第 152-160 行**）
  与主图区（**第 802-821 行**）、放大弹窗（**第 1273-1281 行**）
- `code/webserver/src/views/ReviewView.vue` 第 **654-668 行**（大图 + 人脸框）
- `code/webserver/src/components/photo/PhotoThumb.vue` 第 **158-200 行**
- `code/webserver/src/styles/main.css` 的 `.pb-photo-frame`（= `relative overflow-hidden rounded-thumb border border-line bg-photo`）

## 一、数据库字段（一处新增）

在 `code/src/database/pb_photo.txt` 的 `orientation` 后面加一行：
```
rotateDeg SMALLINT NOT NULL DEFAULT 0 COMMENT '人工旋转角度 0/90/180/270 仅影响显示 不改原图 不改EXIF (DR-43)'
```

**迁移不用手写脚本** —— `tools/build_db.py` 第 224 行已经实现了「缺列 → `ALTER TABLE ADD COLUMN`」：
```powershell
code\.venv\Scripts\python.exe src\tools\build_db.py
```

三条必须知道的细节：
1. ⚠️ SQLite 的 `ADD COLUMN` **只能追加到表末尾**（`build_db.py` 第 340 行注释）。
   所以**已存在的库**里 `rotateDeg` 会在**最后一列**，**新建的库**里它在 `orientation` 后面
   —— 列序不一致**无害**（本项目全部按列名访问），但别用 `SELECT *` 去比对新旧库。
2. ⚠️ **绝对不要把 `rotateDeg` 加进 `runner.py` 的 `_META_FULL_COLUMNS`（L87）或
   `_metaColumns()`（L473）**。那两个是重扫 upsert 的 `DO UPDATE` 白名单
   （`sqliteCodeGenerator` 的 `forceColumns`），加进去会让**重扫一次把用户旋转全部归零**。
   本步**一行都不用改 runner.py** —— 正因为不加，才安全。
3. `rotateDeg` 与 `shotYearOverride` 是**同构**的用户覆盖值：`0` = 未修正、
   「重置」有意义、重扫不覆盖。命名和语义都照 DR-42 的样子来。

## 二、后端

### 2.1 写库逻辑 → 新建 `code/src/processor/photoRotate.py`
照 `processor/photoTimeFix.py` 的结构写，但**比它简单**：
```python
class PhotoRotateError(Exception): ...

VALID_ANGLES = (0, 90, 180, 270)

def applyRotate(photoCode, rotateDeg) -> dict:   # 落库 + 返回 {photoCode, rotateDeg, changed}
def resetRotate(photoCode) -> dict:              # 等价于 applyRotate(photoCode, 0)
```
- **不需要 `previewFix`** —— 旋转**没有影响面**（见 2.2 的理由）
- 幂等：同值重复提交返回 `changed=False`，不报错
- 只接受 `0/90/180/270`，其它抛 `PhotoRotateError`
- **业务层禁止裸 SQL** → 走 `database/queryCommon.py` / 生成的 `sqliteCommon`

### 2.2 端点 → 加进 `code/src/api/photoAction.py`（**不要碰 browse.py**）
```python
POST /api/photos/{photoCode}/rotate     body: {"rotateDeg": 90}
POST /api/photos/{photoCode}/rotate-reset
```
⚠️ **本步与 `photoAction.py` 里其他动作有三处关键差别，必须在模块头注释里写清理由**：

| | 软删 / 标记重复 / 年代修正 | **旋转** |
|---|---|---|
| 两段式 `confirm=1` | 需要（有影响面 / 不可逆语义） | **不需要** |
| 落 `pb_review_log` | 软删与标记重复不落；**年代修正落**（它重算质心，动的是归属排障链上的事实） | **不落** |
| 连带重算 | 年代修正是 DR-22 三步联动（刷桶 + 重算质心） | **零连带** |

**理由（照抄进注释）**：旋转**不改变任何识别事实** —— 不动 `bbox`、不动质心、不动 `shotBucket`、
不动归属、不动 `pb_photo.faceCount`，且完全可逆（再转回去即可）。
所以它既没有"必须先让用户看见的影响面"，也不属于"归属纠错排障链"。
`photoAction.py` 的头注释把这条链讲得很清楚，**别把旋转塞进那条链**。

- 校验非法角度 → `dto.ApiError(dto.CODE_PARAM_INVALID, ...)`
- 返回 `dto.okBody(executed=True, **result)`
- 更新文件头的 `_VERSION`（当前 `"20261006"`）

### 2.3 DTO 透出（**漏了这步前端根本拿不到值**）
`rotateDeg` 必须出现在**所有返回照片摘要/详情的地方**：
| 文件 | 位置 |
|---|---|
| `api/browse.py` | `photoSummary()` L197；SELECT L482-483、L526-527、L651-652、L1204、L1265-1267 |
| `api/place.py` | SELECT L808-810（R5 新增的第 6 处） |
| `api/dto.py` | 若有照片字段白名单/文档串，一并补 |

⚠️ 这 6 处**一处都不能漏** —— 少一处就是「照片流里转了、地点详情里没转」这类
最难发现的不一致（接口 200、字段缺失、界面静默错）。

## 三、前端

### 3.1 唯一几何入口：扩展 `displaySize`（**不要另起新函数**）
`utils/format.js` 的 `displaySize(photo)` 已经在处理「EXIF orientation ∈ {5,6,7,8} 时宽高转置」。
旋转是**同一条路径上的第二次转置**，必须合并在同一个函数里：
```js
export function displaySize(photo, rotateDeg = 0) {
  // ① 先按现有逻辑折算 EXIF orientation
  // ② rotateDeg 为 90 / 270 时再转置一次
  return { width, height }
}
```
调用点全部改传角度：`PhotoDetailView.vue` **L154**（frameStyle）与 **L921**（「尺寸」行）。

> 物理含义：`displaySize` 返回的是**最终显示方向的宽高**。
> 照片详情右下角「尺寸」显示的也就该是旋转后的值。

### 3.2 ⚠️ 旋转容器结构（**本步最容易做错的一处，做错人脸框全错位**）

人脸框 `pb_face.bbox` 是**归一化 x,y,w,h**，由 `utils/faceState.js` 的 `parseBbox` →
`faceBoxStyle` 转成百分比，`FaceBox` **绝对定位**叠在图上。
**如果只给 `<img>` 加 `transform` 而 FaceBox 在外面 → 百分比基座没变 → 框全部错位。**

正确结构（旋转**容器**，不旋转 img 自己）：
```
外层 outer：吃【旋转后】的比例（90/270 时 w/h 互换）
  内层 inner：宽高用【未旋转】比例，居中 + transform: rotate(deg)
    ├── <img>          ← 不单独加 transform
    └── <FaceBox> × N  ← 跟着 inner 一起转，百分比基座不变
```
**这样 bbox 语义完全不用动，一行坐标数学都不用写。**
（另一种做法是改 `parseBbox` 做坐标变换，能行但容易在 90/180/270 上写错，
且 `FaceBox` 的「标签放哪一侧不挡脸」启发式读的还是同一个 box —— 不推荐。）

⚠️ **副作用要实测**：`FaceBox` 用 `ResizeObserver` 量 `frameW/frameH` 来定标签方向，
而 CSS transform **不改 layout box** → 旋转 90° 后这个启发式按未旋转尺寸算，
**标签可能贴到边上**。属外观问题，实机看一眼，必要时把旋转角度传给它自己换算。

### 3.3 各触点怎么改（**按「框的形状」分三类，规则只有三条**）

> **通用规则**
> · 框是**正方形** → 直接转 `<img>`，`scale(1)`（正方形转 90° 仍是正方形，**无缝**）
> · 框是**固定 4:3** → 转 `<img>` + `scale(4/3)` 重新盖满（或把框换成 `aspect-[3/4]`）
> · 框是**动态真比例** → 外层换成旋转后比例 + 内层包住 img 与脸框一起转

| 位置 | 框的形状 | 做法 |
|---|---|---|
| `PhotoDetailView.vue` 主图 **L808-821** | **动态真比例**（`frameStyle` L152-160） | `frameStyle` 的 `aspectRatio` 与 `width` 都用**旋转后**的 `displaySize(photo, rotateDeg)`；内层包 img + FaceBox 一起转。⚠️ 该处**刻意不套 `.pb-photo-frame`**（L804 注释：脸框标签与「不是他」要溢出框外）→ 内层**也别加** `overflow-hidden` |
| `PhotoDetailView.vue` 放大弹窗 **L1273-1281** | 无框、裸 `<img>` | 只转 img（这里没有脸框） |
| `ReviewView.vue` 大图 **L654-668** | 固定 `aspect-[4/3]`，**含脸框** | 内层包 img + 那个 `border-dotted` 的框一起转；⚠️ 外层比例要跟着换成 `aspect-[3/4]`，否则 4:3 框装 3:4 内容会出现空隙（`.pb-photo-frame` 自带 `overflow-hidden`，空隙里会露出 `bg-photo` 底色） |
| `PhotoThumb.vue` **L159-180** | 固定 `aspect-square`（**实测全项目没有一处传 `ratio=`，网格恒为 square**） | 只转 `<img>`，无需 scale。⚠️ **角标（L191 起的 👤/⚠/📷）绝对不能转** —— 它们与照片内容无关 |
| `PlacesView.vue` **L228-233** | 固定 `aspect-[4/3]` 封面，无脸框 | 转 img + `scale(4/3)` |
| `DuplicateCompare.vue` **L218** 等处 | 固定 `aspect-[4/3]`，**已确认无人脸框** | 同上 |
| `PersonDetailView.vue` 时间轴 | 走 `PhotoThumb` | 无需单独改 |
| `PersonDetailView.vue` 人脸样本 L558 等 | **人脸裁剪图**（已裁好的独立小方图） | **不改** |
| 任何 `faceUrl()`（人脸裁剪图 / 头像） | 独立产物 | **一律不改** |

> 说明：`PhotoThumb` 的 `object-cover` 是**先按正方形裁、再整体旋转**，
> 与「先旋转、再按正方形裁」取到的区域**不完全相同**（正方形旋转不变，
> 所以不会有空隙，只是取景范围略有差别）。缩略图尺度上可接受，
> **不要为此去改缓存或做双份生成**。

### 3.4 交互
- **照片详情主图区**加两个图标按钮（`lucide-vue-next` 的 `RotateCcw` / `RotateCw`）：
  「↺ 左转 90°」「↻ 右转 90°」
- **「重置方向」按钮**：仅 `rotateDeg !== 0` 时出现，点了写 0
- 角度累加 mod 360。**不做单独的 180° 按钮** —— 连点两次左转就是 180°
- 快捷键 `[` 左转 / `]` 右转 —— **照 `ReviewView.vue` 的键盘纪律抄**
  （先读它怎么避免抢输入框焦点、怎么处理组合键）
- ⚠️ **翻页不能串角度（最典型的 bug）**：`PhotoPager` 切到下一张时，
  `rotateDeg` 必须取**那张照片自己的值**。写错的表现是「转完一张按 →，
  下一张也是躺着的」，而且刷新一下就好了 —— 极易漏测。**验收第 15 条专门测它。**
- 乐观更新 + 失败回滚；成功后把新值写回 store（不要整页 reload）
- ⚠️ **下载原图仍是原始方向**（原图不动）。这是**有意为之**，
  不要"顺手"给下载链接加旋转；若将来要"旋转后另存"，那是导出功能，另开一步
- 无障碍：按钮要有 `aria-label`（「向左旋转 90 度」），并在界面上可见地反映当前角度

### 3.5 前端 api
`api/photoAction.js`（已有 `softDelete` / `fixShotYear` 等 7 个导出）加：
```js
export function rotatePhoto(photoCode, rotateDeg) { ... }   // 照 fixShotYear 的写法
export function resetRotate(photoCode) { ... }
```

## 四、验收清单（逐条实际运行）

### A. 字段与迁移
1. `pb_photo` 出现 `rotateDeg`；`PRAGMA table_info(pb_photo)` 显示类型 `INTEGER`、
   `notnull=1`、`dflt_value=0`。**把那一行原文贴出来**
2. 再跑一次 `build_db.py` **幂等**（不再加列、不报错）
3. 加列**不丢数据**：已有 **2137 行**的 `rotateDeg` 全为 0，`COUNT(*)` 仍 2137

### B. 重扫不覆盖（本步最关键的一条）
4. 手工把某张的 `rotateDeg` 改成 90 → 对这张跑一次扫描 → 值**仍是 90**
   —— 证明它**没被塞进** `_META_FULL_COLUMNS`。
   请给出 `runner.py` 的 `git diff` 为**空**（本步不该改这个文件）

### C. 接口
5. `POST /api/photos/{code}/rotate` body `{"rotateDeg":90}` → 返回 `rotateDeg=90`
6. 传 `45` / `-90` / `"abc"` → 全部 **400**
7. 重复提交同值 → `changed=false`，不报错
8. `rotateDeg` **6 处全部透出**：`GET /api/photos/{code}`、`GET /api/photos`（列表）、
   `GET /api/places/{code}/photos` —— 逐个贴响应片段
9. 旋转**没有**新增 `pb_review_log` 行（对比操作前后 `COUNT(*)`）
10. 旋转**没有**改变任何识别数据：`pb_face` 的 `bbox`/`personCode`/`isConfirmed`、
    `pb_person_centroid` 的 `centroid` BLOB、`pb_photo.faceCount`
    —— 操作前后各查一次对比，**全部逐字节相同**

### D. 渲染（**本步存在的意义，必须有截图**）
11. **人脸框跟着转**：拿一张有脸、`orientation=1` 的照片，详情页转 90°
    → 人脸框仍**贴在脸上**。**贴旋转前 / 旋转后两张截图对比**
12. 同样测一张 **`orientation=6`** 的照片（EXIF 与人工旋转**叠加**：
    `orientation=6` 已让画面变竖，再人工转 90° 应变横）—— 截图
13. **ReviewView 待确认队列的大图与脸框一起转**，且外层从 4:3 变 3:4、**四周无空隙**
14. 网格（照片流）：旋转后缩略图也转了；**角标（👤/⚠/📷）没有被转**（截图指出角标位置）
15. **翻页不串角度**：转第 1 张 90° → 按 `→` → 第 2 张是**正常方向**；
    再按 `←` 回第 1 张 → **仍是 90°**
16. 照片详情「尺寸」行（L921）显示的是**旋转后**的宽高（与截图对照）
17. 放大弹窗的图与主图方向一致
18. 连点 4 次右转 → 回到原方向，与初始截图一致
19. `rotateDeg !== 0` 时「重置方向」才出现；点了回 0 且按钮消失

### E. 红线回归（**证明没走错方案**）
20. **缩略图目录文件数与总字节数不变** —— `GET /api/media/stats` 前后对照，
    或直接数 `d:\PhotoLib\thumb\thumbs\` 的文件数。**证明旋转没烘进缩略图、没触发重新生成**
21. **`photoDir` 文件数与总字节数不变**（原图零风险）
22. `Range: bytes=0-65535` 仍返回 **206** 且 `Content-Range: bytes 0-65535/<size>` 正确
    —— 贴响应头原文，**证明 `/api/original` 的直传与 Range 没被破坏**
23. 下载原图得到的文件方向 = **原始方向**（不是旋转后的）
24. 深浅主题、`<1024` / `<768` 断点正常；键盘可达（Tab 能走到两个旋转按钮，`[`/`]` 生效）
25. `npm run build` + 样式门禁通过；`pytest code/src/test -q` 全绿（基线 **1237 passed** 起）

## 五、硬约束
- **原图绝对只读**：不改写、**不改 EXIF**、不碰 mtime。
  旋转一律是「DB 里的一个角度 + 前端一次 CSS transform」
- **旋转不烘进缩略图**（缓存键是 `thumb_relpath(fileHash, size)`，不含旋转）
- **不做服务端转码**（保住 `/api/original` 的 Range 直传）
- **不改 `browse.py`**（它的纪律是"整个模块一个字都不写"）→ 端点进 `api/photoAction.py`
- **不落 `pb_review_log`**；**不做两段式 `confirm`**（理由见 §2.2）
- **不改 `runner.py`**（不加进重扫 `DO UPDATE` 白名单）
- **人脸框必须与被旋转的图在同一个旋转容器内**（否则框全错位）
- 只支持 **0 / 90 / 180 / 270**；**不做任意角度、不做镜像翻转**
- 业务层禁裸 SQL；复用既有 router / store / api / 组件分层与双主题 token
- 单文件不超过 300 行；`PhotoDetailView.vue` 已 43KB，**只做插入不要顺手重构**
- 现有代码风格保持一致（本仓库注释密度很高，新代码请同样写清"为什么"）

## 六、完成后必须输出
1. 改动文件清单（后端 / 前端分开列，标注新增还是修改）
2. 验收结果（**25 条逐条**给命令与实际输出）
3. **第 11 / 12 条的截图**（旋转前后人脸框仍贴脸；含 `orientation=6` 那张）
   —— 这是本步存在的意义，没有截图就等于没做
4. 第 20 / 21 条的缩略图与原图目录统计**前后对照**
5. 第 22 条的 **206 响应头原文**
6. `PRAGMA table_info(pb_photo)` 里 `rotateDeg` 那一行原文
7. `runner.py` / `browse.py` 的 `git diff` 为空的证明
8. 遗留问题与需要我决策的点
   （特别是：**要不要做批量旋转**、**下载要不要给"旋转后"版本**）
```

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
         · **R7 追加：`avatarFaceCode` 也可 PATCH**（DR-41）——设/清默认头像。
           它是**展示**字段：不重算质心、不写 `pb_review_log`。
           写库前必须校验「这张脸存在且属于这个人」（不存在 404 / 属于别人 400）
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
| **R5** | **地点界面**（**地点 → 照片流** + 人物「去过的地方」区块 · DR-37/38） | — | ✅ 已完成（`api/place.py` 48KB / `PlacesView` / `PlaceDetailView` / `PersonPlaces` / `test_api_person_places.py`；路由 `/places` + `/places/:placeCode`、侧栏 `MapPin`、`PersonDetailView` L684「↓↓↓ R5：去过的地方 ↓↓↓」均已接线） |
| **R9** | **照片旋转**（左右转 90° · 显示层 CSS + `rotateDeg` 字段 · DR-43） | — | ⬜ **待做（提示语已写）** |
| **R6** | **照片详情左右翻页**（DR-31，A 档，后端零改动） | — | ✅ 已完成（`components/photo/PhotoPager.vue`） |
| **R7** | **人物头像**（卡片显示照片 + 人脸样本设默认 · DR-40/41） | — | ✅ 已完成（2026-10-08：`personCoversOf` + `AvatarPicker` + 16 条用例；**正式库副本实测 24/24 卡片有封面脸**，改前 0/2030 有 `avatarFaceCode`） |
| **R8** | **照片年代修正**（人工修正拍摄年 + 编辑入口 · DR-42） | — | ✅ 已完成（2026-10-08：`pb_photo.shotYearOverride` + `comGD.sqlEffectiveShotYear` + `processor/photoTimeFix.py` + `ShotYearFixDialog.vue` + `test_photo_time_fix.py` 12 条用例全绿）；⚠️ 拖拽（P1）与 `takenAt` 修正到日（P2）未做 |
| **R3** | **修 `(0,0)` 占位坐标**（DR-23） | — | ✅ 已完成（`tools/fix_placeholder_geo.py`） |
| **R4** | **地点字典基础层**（`pb_place` + `placeStore` + `/api/places`） | — | ✅ 已完成（`pb_place.txt` / `placeStore.py` / `browse.py` `/api/places` / `test_api_places.py`） |
| **R4a** | **地点中文名**（DR-28/29：`pb_place.nameZh` + 筛选两套） | — | ✅ 已完成（`placeNameZh.py` 45KB；实测 16/17 GPS 地点有中文名） |
| **R4b** | **目录名线索接入**（DR-32/33/34/35/36） | — | ✅ 已完成（`dirNamePlace.py` 48KB + `placeFinalize.py`；实测 `placeNameDir` **598 张**、`pb_place` **28 行**、`photoCount` 合计 **663 = 598+65** 完全对上、**26 张幽灵已清零**）<br>⚠️ 遗留：`nameZh` 重名 3 组（交 R5 处理） |
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
| 11 | 照片流 + 详情 + 待确认队列 | — | ✅ 已完成（`PhotosView` / `PhotoDetailView` / `ReviewView` + 7 个新组件；**DR-16 纠错闭环全部落地**：`FaceBox` 三种描边样式 + 悬停「✗ 不是他」一次可达、`ReviewView` 双 Tab + 键盘 1/2/3/N/S/I + 批量确认、合并/拆分独立端点。`pytest` = **1175 passed / 0 failed**） |
| 12 | 人物库/详情 + 扫描台 + 设置 + 打磨 | **M4**（自己真正用一周） | ✅ **编码已完成**（2026-10-07）。撤销 UI 已接（候选集口径也修了：一次合并只出一个候选）、质心健康度提示、备份脚本 `tools/backup.py`（DR-24：故意不做进 HTTP）、重复照片对比、`/api/settings` 四端点。**修了两个既有缺陷**：人物桶内 `photoCount` 恒为 0、撤销候选集按 N 条成员行重复出现。`pytest` = **1225 passed / 0 failed**，`npm run build` + 样式门禁通过。⚠️ 未做：`GET /api/map`（可选，见该步「已知取舍」①）。**M4 剩下的是"用一周"，不是写代码** |

## 附录 B · 每步固定的输出格式

```
1. 改动文件清单（新增 / 修改，逐个列路径，标注新增还是修改）
2. 验收结果（该步验收清单逐条：执行的命令 + 实际输出或结论 + 数值）
3. 遗留问题与需要我决策的点（如果有拿不准的需求，不要自行假设，列出来问我）
```

> 提醒：新对话里请**只粘贴当前步骤的提示语**，不要把多步一起粘过去，否则上下文会过长导致遗漏约束。
>
> **当前应粘贴的是「修正步骤 R9 · 照片旋转」**（本文「修正步骤 R9」那一节）。
> 12 步主线于 2026-10-07 全部完成；R2 / R3 / R4 / R4a / R4b / R5 / R6 / R7 / R8 均已完成，
> **R9（照片旋转）是唯一待做项**。
>
> ⚠️ **编号提醒**：`R7` = 人物头像（DR-40/41，已完成）、`R8` = 照片年代修正（DR-42，已完成）、
> **`R9` = 照片旋转（DR-43，待做）**。别把 R9 叫成 R7。
>
> **R9 完成后（按优先级）**
> 1. **M4 一周自测**（最重要，不是写代码）：按 `开发计划.md` §八 的建议用法真用一周，
>    第 7 天自问「还会想去资源管理器看照片吗」。
> 2. **R2 的验收证据**（唯一还欠的技术报告）：`auditBuckets()` 改前/改后完整对照、
>    `verify_bucket_gain.py` 的生产库 FR 实测、全库重算质心前后行数变化。
>    它是 **M2「识别复现 S0」** 的前提 —— 桶口径说了算，不能只凭"代码写完了"就认为结论能复现。
>    要补就单独贴 **「修正步骤 R2」**。⚠️ 该节在本文里有**两份重复**（文件被多次追加过），
>    粘贴前先确认你拿到的是完整的那一份。
> 3. **确认人脸**：`pb_face` 3647 张里只有 **67 张有归属**，`pb_person` 2029 个里只有 **20 个有照片**。
>    「人物 × 地点」交叉目前几乎是空的（有地点的 663 张照片里仅 **1 张**关联到人）——
>    R5 做出来的「在场的人」区块会长期空着，直到你在待确认队列里把归属做起来。
> 4. 可选：**批量旋转**（R9 只做单张，见该步「完成后必须输出」第 8 条）。
> 5. 可选：地图视图（R5 明确未做，前提是解决「11/28 个地点无坐标」——
>    目录名地点占 663 张里的 598 张，没有 `lat/lon`）。
>
> ⚠️ **本文档已有重复章节**（多轮追加导致）：`R5` 出现 **3 次**、`R4b` **2 次**、`R2` **2 次**，
> 其中 `R5` 的两份旧版内容仍是**废弃的「人物 → 地点列表 → 时间线」**形态。
> 使用前请**只认最新那份**；建议在有空时清理一次（本步不做）。
>
> **步骤 12 实际落地时的偏差（供以后回看）**
> - 步骤 12 提示语里说「补齐 4 个 views」，实际情况是**补齐 5 个**（`OverviewView` 也接了真数据）
>   并新建了 **2 个**组件：`PersonCard.vue`（提示语点名的那个）**与 `DuplicateCompare.vue`**
>   —— 后者是因为「重复照片」筛选已经摆在照片流里，只给筛选不给对比就是半成品。
> - 后端**多建了 `api/settings.py`**（提示语没提）：浏览层刻意不 import 引擎层，
>   「唯一会写配置态的 api 模块」单放一处，才不破 `api/browse.py`「纯只读」那条纪律。
> - 修了一个**提示语没提到的既有缺陷**：人物桶内 `photoCount` 恒为 0
>   （`GROUP BY substr(takenAt,1,7)` 却拿桶键去查字典，键的形状不同）。这类"不报错的恒为 0"
>   是最贵的缺陷类型 —— 接口 200、结构齐全、字段在。
> - **撤销候选集的口径也修了**：一次合并会写 1 主 + N 成员日志，
>   原先 `/review/revertible` 把 N 条成员行也列成候选（合并 3 张脸 = 4 个"可撤销"）。
>   现在按「一次用户操作 = 一条」收敛。
> - **备份/恢复故意没做进 HTTP**：见 DR-24。WAL 中间态会让「备份成功、零报错、恢复后少数据」。
>   设置页给只读清单 + 可照抄的命令行。
