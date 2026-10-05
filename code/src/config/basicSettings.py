#! /usr/bin/env python3
#encoding: utf-8

#Filename: basicSettings.py
#Description: photo-browser 全局业务参数集中配置（入库）
#
# 强约束：所有阈值 / 批大小 / 白名单一律在此声明，
#        **禁止散落在 processor / engine / api 等业务模块里硬编码**
#        （见 plan/开发计划.md §6.4 与本文件头部的"配置项集中"原则）。
#
# 分工：
#   basicSettings.py   ← 本文件：与机器无关的业务参数（入库）
#   local_settings.py  ← 跟本机有关的三个路径（不入库，有 .example 模板）
#   sqliteSettings.py  ← SQLite PRAGMA 常量与库文件装配

_VERSION = "20261004"


# ============================================================
# 一、路径默认值（local_settings.py 缺失时的兜底）
# ============================================================
# local_settings.py 不入库。新机器 clone 后若忘了复制 .example，
# paths.py 会退回下面三个默认值，保证程序仍能启动（不静默失败，打印 warning）。
# 命名规则固定为 DEFAULT_<配置项名>，供 paths.readSetting() 统一回退查找。

DEFAULT_PHOTO_ROOT: str = r"d:\PhotoLib"
DEFAULT_THUMB_ROOT: str = ""      # 空 = 派生 <PHOTO_ROOT>\thumb
DEFAULT_DB_FILE: str = ""         # 空 = 派生 <PHOTO_ROOT>\db\photolib.db


# ============================================================
# 二、扫描批次与限流
# ============================================================

# 一次扫描任务处理到该张数即 jobStatus=PAUSED 停止，等待用户「继续下一批」
BATCH_SIZE: int = 100

# 批内写库的事务提交粒度（每 N 行 executemany 提交一次）
COMMIT_ROWS_PER_TXN: int = 500


# ============================================================
# 三、人脸匹配阈值（S0 实测起点，勿信默认 0.65）
# ============================================================
# S0 结论：T_high=0.55 / T_low=0.35；FR 32.75% → 分桶后 ~19%
# 步骤 12 的设置页会暴露这两个值给用户调整，改动后需重算质心。
#
# 阈值**成对**给，单独调一个会出事
# --------------------------------
#   只降 T_HIGH 不动 T_LOW：灰区 [T_LOW, T_HIGH) 变宽，本该"库里没有这个人"的
#   脸被扣在待确认队列里等人工 —— 队列越堆越长，人就不看了，最后整个功能作废。
#   只升 T_LOW 不动 T_HIGH：更多脸被判成"陌生人"进聚类，pb_person 里会长出一堆
#   重复的"未命名人物"，比错分更烦人。
#   所以下面按**预设**成对切换，切换粒度是"一套"，不是"一个数"。

#: 阈值预设表。三套都满足 T_LOW < T_HIGH（灰区非空）。
#:
#: s0          —— S0 实测基线，唯一有实测数据背书的一套（步骤 5/6 的回归都按它跑）
#: conservative —— 保守：两档同时上移。自动归属门槛最高（误归属最少），
#:                 灰区最宽（模糊的脸交人工，不硬猜），落到"陌生人"的也最少
#:                 （不会凭空长出一堆未命名人物）。
#: aggressive   —— 激进：两档同时下移。自动归属门槛最低（人工量最少），
#:                 灰区最窄，代价是 FA 会明显上升。
#:
#: 档位取值依据：S0 扫描给出的推荐阈值区间约 0.43~0.64（见 tools/backtest_s0.py
#: 的阈值扫描输出），aggressive 的 T_HIGH=0.48 与 conservative 的 T_HIGH=0.62
#: 正好夹住这个区间两端，s0 的 0.55 落在中间。三档的灰区互有重叠，
#: 所以「切到激进再切回保守」是可回退的（验收第 5 条）。
MATCH_THRESHOLD_PRESETS: dict = {
    "s0":          {"tLow": 0.35, "tHigh": 0.55},
    "conservative": {"tLow": 0.42, "tHigh": 0.62},
    "aggressive":   {"tLow": 0.30, "tHigh": 0.48},
}

#: 当前生效的预设。**缺省保守**：
#:   家庭照片库里「把 A 的脸认成 B」的代价远高于「多让人点几下确认」——
#:   错分会让用户对整套识别失去信任，而多确认几张只是慢一点。
#:   激进档留给「人脸已经确认过一大半、想少点几下」的场景。
MATCH_THRESHOLD_PRESET: str = "conservative"

# 下面两个是**当前生效值**（由 matchThresholds() 从预设解析而来，不是独立真相）。
# 保留这两个名字是为了兼容既有调用与日志口径；**业务代码请一律调 matchThresholds()**，
# 不要直接读 T_HIGH / T_LOW —— 否则切预设时这些常量不会跟着变，
# 会出现「换了预设但行为没变」这种静默失效。
#
# 相似度 >= T_HIGH → 自动归属（无需人工确认）
T_HIGH: float = 0.55
# 相似度 <= T_LOW → 判为"库里没有这个人"，进聚类
# T_LOW < 相似度 < T_HIGH → 灰区，记 Top-5 候选进待确认队列（人工兜底）
T_LOW: float = 0.35


def matchThresholds(preset: str = None) -> tuple:
    """取一套 (tLow, tHigh) 阈值。**全项目读阈值的唯一入口。**

    参数
    ----
      preset : None = 用 MATCH_THRESHOLD_PRESET；给键名则取该预设

    未知预设名**不静默回落**到 s0 —— 那正是"配错了但看不出来"的来源。
    这里直接抛 KeyError，让调用方在启动时就炸（阈值配错必须立刻可见，
    不能等到跑了三万张脸才发现用的是哪一套）。
    """
    name = str(preset or MATCH_THRESHOLD_PRESET)
    if name not in MATCH_THRESHOLD_PRESETS:
        raise KeyError("未知的阈值预设 %r，可选：%s"
                       % (name, sorted(MATCH_THRESHOLD_PRESETS.keys())))
    one = MATCH_THRESHOLD_PRESETS[name]
    tLow, tHigh = float(one["tLow"]), float(one["tHigh"])
    if tLow >= tHigh:
        raise ValueError("阈值预设 %r 非法：T_LOW(%s) 必须 < T_HIGH(%s)"
                         % (name, tLow, tHigh))
    return tLow, tHigh


# 相邻年代桶候选数：取「本桶 + 前后各一桶」共三桶
MATCH_NEIGHBOR_BUCKETS: int = 1

# 桶内样本数下限：<该值不启用该桶质心（样本太少不可信）
MIN_CENTROID_SAMPLES: int = 3

# 质心**只统计人工确认样本**（isConfirmed=1）—— 防污染开关（DR-16③ / D-9）
# ------------------------------------------------------------------
# True（缺省）= 质心只用 isConfirmed=1 的样本。
#   理由：自动归属里必然混着误认样本，一张误认的脸拉偏质心 ->
#         后续更多脸被误认 -> **越错越错**，而且没有任何迹象提示它坏了。
# False = 退回修正前的旧口径（桶内**全部**样本都进质心）。
#   **只用于两件事**：① 回归对比（验收第 21 条：确认本次改动没有意外改变匹配能力）；
#   ② 冷启动 —— 用户还没人工确认过任何脸时，True 会让**所有**质心不可用
#      -> 自动归属数为 0 -> 所有人脸进待确认队列。
#      这是**正确行为**（没有干净样本可用就不该猜），但会让 S0 回归跑不出结果，
#      所以留这个逃生口。日常跑 True。
CENTROID_CONFIRMED_ONLY: bool = True

# 待确认时记下的候选人数（Top-5）
MATCH_TOP_CANDIDATES: int = 5

# pb_review_log.logCode 前缀（格式 RL_<yyyymmddHHMMSS>_<6位随机>，VARCHAR(64) 绰绰有余）
REVIEW_LOG_CODE_PREFIX: str = "RL"

# 纠错日志的 opUser（本机单用户，固定值；留着将来接多用户）
REVIEW_LOG_USER: str = "local"


# ============================================================
# 三之二、年代分桶规则（步骤 6）
# ============================================================
# 为什么按年龄自适应，而不是一律等宽 5 年
# ----------------------------------------
#   ArcFace 的 embedding 随年龄漂移（婴儿→成人那一段的漂移远大于成人之间），
#   S0 已实测：不分桶 FR 32.75%，分桶后 ~19%（降幅 42%，见 数据库设计.md D-5）。
#   而「同一个人在 3 岁和 30 岁」这种跨度，等宽桶必须放到 30 年宽才装得下，
#   一装得宽就把不同年龄的人混进同一个质心 —— 正是要避免的事。
#   所以：童年细（同龄人脸变化快）、成年粗（变化慢）。分界 18 岁。

#: 童年/成年分界（岁）。<= 该岁走童年桶，> 该岁走成年桶
BUCKET_CHILD_MAX_AGE: int = 18
#: 童年段桶宽（年）：从出生年起每 3 年一桶
BUCKET_CHILD_WIDTH: int = 3
#: 成年段桶宽（年）：从出生年+18 起每 10 年一桶
BUCKET_ADULT_WIDTH: int = 10
#: 出生年未知时的降级桶宽（年）：等宽 5 年（S0 的方案 A，零前置条件）
BUCKET_EQUAL_WIDTH: int = 5
#: bucketKey 形如 "1995-1999"，必须放得进 pb_*.txt 里的 VARCHAR(16)
BUCKET_KEY_MAX_LEN: int = 16

#: 质心全量加载时的分页行数（与步骤 3 的 SCAN_INDEX_PAGE 同一个理由）
#: 3 万个 2048 字节的 BLOB 一次性取回 = 61MB 字节对象 + 61MB 矩阵 = 122MB，
#: 直接把「内存 < 100MB」这条验收指标顶爆。分页后峰值只有一页（2000 × 2KB = 4MB）。
CENTROID_PAGE_ROWS: int = 2000


# ============================================================
# 四、人脸质量过滤（任一不满足即丢弃，不入 pb_face）
# ============================================================

# SCRFD 检测置信度下限
MIN_DET_SCORE: float = 0.6
# 人脸框短边像素下限（太小则特征不可靠）
MIN_FACE_EDGE: int = 64
# 侧脸偏航角绝对值上限（度）；|yaw| > 45 的侧脸 embedding 偏移严重
MAX_YAW: int = 45

# ArcFace 特征维度与存储字节数（float32[512] 小端 = 2048 字节）
EMBEDDING_DIM: int = 512
EMBEDDING_BYTES: int = EMBEDDING_DIM * 4


# ============================================================
# 四之二、人脸提取进程池的保守上限（步骤 5）
# ============================================================
#
# 为什么要有"硬上限"这一层
# ----------------------
#   缺省进程数本来已经按 min(核数-1, 内存60%/每进程600MB) 收敛过，
#   但那只是**跟着机器长**：16 核 64GB 就会真的开 15 个进程（实测约 7GB 常驻、
#   满载时 CPU 跑满、风扇起飞）。对"整理家庭照片"这种一次性的活，
#   把整台机器吃满不是好默认值 —— 开着扫面��时电脑基本没法干别的。
#   所以再叠一层**与机器规格无关的硬上限**，把缺省值钉在
#   "5 核 / 4GB 小机器也能稳稳跑"的水平（= 4 个进程 / 4GB 预算）。
#
#   要跑得更快就**显式** `--workers N`（或设环境变量），
#   显式值不受本上限约束 —— 上限只管"没指定时别乱猜"。
#
# 每进程内存估算：buffalo_l 的 SCRFD 143MB + ArcFace 174MB = 约 300MB 权重，
#   加上 onnxruntime 的 arena 与解码缓冲，实测单进程 RSS 约 470MB。
FACE_WORKER_MEMORY_MB: int = 600
#: 进程池缺省进程数**硬上限**。
#: 取 4 是与下面 4GB 内存预算**配套**的：在 5 核 / 4GB 的参考机器上，
#: 核数(5-1=4)、内存(4096x0.6/600=4)、本上限(4) 三条路**正好都落到 4**，
#: 谁先起作用都不影响结果 —— 换机器时不会出现"我调了内存上限却不生效"的错觉。
#: 要在更大的机器上跑得更快：调大 FACE_MAX_MEMORY_MB，或直接 --workers N。
FACE_MAX_WORKERS: int = 4
#: 进程池可用内存**硬上限**（MB）。4GB 是"小机器也别把自己拖死"的预算：
#: 就算机器有 64GB，也不因为"内存充足"就开十几个进程把整台机器吃满。
FACE_MAX_MEMORY_MB: int = 4096
#: 可用环境变量临时改（不想改代码时用）
FACE_WORKERS_ENV: str = "PHOTO_BROWSER_FACE_WORKERS"


# ============================================================
# 五、聚类（DBSCAN，步骤 7）
# ============================================================
# 只对**未归类集合**聚类：personCode IS NULL AND isStranger=0 AND delFlag='0'
#
# eps=0.45 的来历：距离定义为「余弦距离 = 1 - cos」，所以 0.45 意味着
# **余弦相似度 >= 0.55** 才算邻居 —— 正好与步骤 6 的 T_LOW（0.35~0.42 档）
# 和 S0 扫描给出的推荐区间 0.43~0.64 对齐：聚类只接「连最低阈值都不够格」
# 的脸，不去抢匹配器已经够格的活。
# ⚠️ 这两个值**必须可配置**（cluster_cli.py --eps / --min-samples）：
#    不同照片库的密度差别很大，写死就等于让用户只能改代码。
DBSCAN_EPS: float = 0.45
#: min_samples=2（**由 3 改来**，2026-10-05）
#:
#: 为什么从 3 降到 2（实测依据，别改回去）
#: --------------------------------------------
#:   正式库 73 张未归类脸上，min_samples=3 时 **46 张（63%）是噪声点** ——
#:   原因是家庭相册里多数人「在合影里只露一次」，凑不出 3 个互相像的邻居。
#:   噪声点意味着这些脸在界面上**没有任何聚合提示**，用户只能一张张点。
#:   min_samples=2 之后「两张互相像」就成簇，噪声点大幅下降。
#:
#: 代价（必须知道，别只看好处）
#:   min_samples=2 时**任意两张脸相似就成簇**，
#:   于是「两个长得很像的陌生人」会被凑成一个「未命名人物」。
#:   ⇒ 簇**永远只是提示**，不是结论：确认入口仍然是逐张确认（assigner.confirm），
#:     且同一簇里**必须允许逐张否决**（步骤 11 的簇内批量改判要能拆开）。
#:   若发现假簇变多，第一反应是 `--min-samples 3` 或调小 `--eps`，不是改代码。
DBSCAN_MIN_SAMPLES: int = 2

#: 聚类后端。auto = 装了 scikit-learn 就用它，没装用自实现 numpy 版。
#:
#: 为什么不把 sklearn 写进 requirements（关键取舍，别改回去）
#: ------------------------------------------------------
#:   ① 本项目**没有装** sklearn（也不该为此装）：它会拖进 scipy，
#:      而步骤 6 已经定下「不引 FAISS / sqlite-vec」的同一套理由 ——
#:      10 万 × 512 float32 = 205MB 规模，numpy 一次矩阵乘就够，
#:      引向量库的收益在这个规模是负的（多依赖、多一套状态要与库同步）。
#:   ② sklearn.cluster.DBSCAN 对本项目这种「几千到几万行」的规模
#:      并不比「分块矩阵乘算邻接 + 队列扩展」快（小规模下它反而更慢）。
#:   ③ 但**代码里留这条后端开关**：万一将来装上了、或者有人拿去比
#:      大规模数据，自实现与 sklearn 走的是同一套参数语义（eps/min_samples），
#:      切过去不需要改调用方。两种后端的**簇划分必须一致**（见 test_dbscan.py
#:      的后端对照用例），否则 clusterCode 会随后端变 —— 那是最难查的一类问题。
DBSCAN_BACKEND: str = "auto"
DBSCAN_BACKEND_ALL: tuple = ("auto", "numpy", "sklearn")

#: clusterCode 前缀与摘要长度。格式 cluster_<12位十六进制>，
#: 拼起来 20 字符，pb_face.clusterCode 是 VARCHAR(64)，绰绰有余。
CLUSTER_CODE_PREFIX: str = "cluster_"
CLUSTER_CODE_HASH_LEN: int = 12

#: 算邻接矩阵时一次处理多少张脸。
#: DBSCAN 是 O(N²) 的算法，峰值内存 = 该值 × N × 4 字节。
#: dbscan.py 会按 N 自动收缩（上限 DBSCAN_SIM_CELLS 个 float32），
#: 所以这个值是**上限**而不是定值 —— 1 万张脸时它自己会降下来。
DBSCAN_CHUNK_ROWS: int = 256
#: 临时相似度矩阵的元素数上限（838 万 × 4B ≈ 32MB）。
#: 定这个上限是为了「内存峰值不随库规模漂移」：N 涨到 10 万时
#: 分片自动从 256 降到 83，而不是老老实实开 256 吃 100MB。
DBSCAN_SIM_CELLS: int = 8 * 1024 * 1024

#: 邻接边数上限（超过就中止并报错，不硬撑）。
#: O(N²) 的真正危险不是时间而是内存：库里若塞了一大批互相都近似的
#: 重复向量，边数会从 O(N) 涨到 O(N²)，10 万张脸就是 10^10 条边 —— 那时
#: **明确报错让人来调 eps** 远好过把内存吃光换一堆垃圾簇。
DBSCAN_MAX_EDGES: int = 8 * 1024 * 1024

#: 写 clusterCode 的批量行数（单事务 executemany，与步骤 3/5 同口径）
CLUSTER_WRITE_BATCH: int = 500
#: 装载未归类人脸时的分页行数（10 万行一次全取 = embedding 就200MB）
CLUSTER_PAGE_ROWS: int = 2000


# ============================================================
# 五之二、待确认 / 「我不同意」两个队列（步骤 7）
# ============================================================
# ⚠️⚠️ 这里的两个口径是**互斥的四态**，绝不能写成 `OR isConfirmed=0`
# ------------------------------------------------------------
#   未归属（待确认队列）   = personCode IS NULL  AND isStranger=0
#   自动归属（「我不同意」） = personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0
#   人工确认                = isConfirmed=1
#   陌生人（永久排除）      = isStranger=1（既不进队列也不进聚类）
#
#   写成 `personCode IS NULL OR isConfirmed=0` 会把**全部自动归属**卷进待确认队列
#   （DR-16：10 万张 ≈ 4~8 万条，队列爆炸，用户看一眼就放弃）。
#   自动归属的 isConfirmed 本来就是 0（DR-16③），这是设计而非缺陷。
#
# ⚠️ pendingCount 口径必须拆开（DR-11）
#   pb_scan_job.pendingCount = 「疑似移动/重命名待确认**张数**」（步骤 3 的
#   movedToPhotoCode），与「待确认**人脸**条数」是两个不同的计数。
#   **不要用同一个字段**，否则侧栏角标会把「移动 12 张」显示成「12 张脸待确认」。
PENDING_FACE_NOTE: str = "personCode IS NULL AND isStranger=0 AND delFlag='0'"
#: 单次装载待确认人脸的分页行数（10 万行一次全取 = 峰值数百 MB）
QUEUE_PAGE_ROWS: int = 2000
#: **待确认队列的分页排序键**（必须是 pb_face 已落库、且在生成层 ORDER BY
#: 白名单里的列，否则全局分页根本排不了 —— 详见 queue.pendingQueue 的说明）。
#:
#: 为什么是 quality 而不是 similarity：
#:   · similarity 是**算出来的**（步骤 6 明确 review 结果不落库），
#:     生成层没有可排序的列 -> 用它分页只能每次重扫全量（10 万张约 40 秒）。
#:   · quality 是步骤 5 落下的综合质量分，**永不变化**（除非重跑提取）。
#:     分页键必须稳定：否则翻页时条目会在眼前挪位，那比分页不准更让人烦。
#:   · 实测正式库 73 行 quality 全部非空（0.206~0.911，均值 0.590），
#:     没有 NULL 排序的坑。
#:   · 语义上也说得通：脸清楚 -> 用户点开能认；脸糊 -> 认了也是白认。
#:
#: 候选白名单只有 recID / shotBucket / quality / faceCode 四个（见
#: sqliteCommon.ORDER_FIELDS['pb_face']），别写别的。
QUEUE_ORDER_BY: str = "quality"
#: 队列概览里超过这个规模的簇**不列**全部 faceCode/photoCodes
#: （大簇列明细会把响应撑爆，而 UI 本来就要分页才画得下）
CLUSTER_DETAIL_MAX: int = 60
#: contact sheet（簇拼图）的落点子目录名，在 <thumb> 之下。
#:
#: ⚠️ 这个目录里的东西**不是库产物**，是给人眼抽查的临时拼图：
#:   · 不进 pb_* 任何表，没有 fileHash/faceCode 索引，删掉不影响任何功能；
#:   · **绝不能**被当成"缩略图/���脸图"统计进去（那会让 thumbStats 的
#:     文件数与体积对不上，也会让人以为库里多了几千张图）；
#:   · clusterCode 是内容指纹（见 queue.py「四之二」），簇一变编码就变，
#:     所以旧拼图会**静默过期** -> 写完必须清掉不再对应任何簇的那些，
#:     否则看图的人会对着一个已经不存在的簇下结论。
CLUSTER_SHEET_SUBDIR: str = "cluster"
#: <thumb> 之下**非库产物**的派生目录（统计/清理/巡检一律跳过）
DERIVED_THUMB_SUBDIRS: tuple = (CLUSTER_SHEET_SUBDIR,)
#: 「我不同意」列表单张照片最多展开多少张脸（同一张照片里可能认错好几个人）
DISPUTED_MAX_FACES_PER_PHOTO: int = 20


# ============================================================
# 六、照片扩展名白名单（扫描器只认这些，命中才入库）
# ============================================================
# 全部小写、含点。is_photo_file() 做判断，业务层不要自己写后缀匹配。
#
# ⚠️ **只管静态照片，不含视频**（wmv/mp4/mov/... 已刻意移除）。
#    理由：全项目12 步路线里没有任何视频环节——8 张表无视频字段、缩略图走 Pillow
#    解码、8 个 UI 页面无播放器。放进白名单只会让步骤 3 把视频塞进 pb_photo，
#    步骤 4 生成缩略图时再因解不出帧而炸，属自找麻烦。
#    将来真要支持视频，**另建VIDEO_EXTS 并同步改表/改 UI**，不要往这里加。
#
# 关于 .arf：疑为 .arw(Sony RAW) 的笔误，但**仍予保留** ——
#    白名单多一项的代价是零（永不匹配而已），漏一项的代价是照片被静默跳过。

PHOTO_EXTS: tuple = (
    # 常规位图
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".heic", ".heif", ".avif", ".jxl",
    # RAW（只读不改写，绝不回写相机格式）
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".arf", ".raf", ".orf",
    ".rw2", ".pef", ".srw", ".3fr", ".erf", ".kdc", ".mos", ".mrw",
)

# frozenset 供 O(1) 查表；PHOTO_EXTS 供文档展示与遍历
PHOTO_EXT_SET: frozenset = frozenset(PHOTO_EXTS)

# 明确排除的目录名（扫描器跳过，不因扩展名误判）
EXCLUDED_DIR_NAMES: frozenset = frozenset({
    "@eaDir",          # 群晖缩略图目录
    ".thumbnails",
    "thumbs",
    "faces",
    "$RECYCLE.BIN",
    "System Volume Information",
})


def is_photo_file(fileName: str) -> bool:
    """按扩展名判断是否为受支持的照片文件（大小写不敏感）"""
    if not fileName:
        return False
    dot = fileName.rfind(".")
    if dot < 0:
        return False
    return fileName[dot:].lower() in PHOTO_EXT_SET


# ============================================================
# 七、文件 hash
# ============================================================

# 分块大小 8MB —— 绝不允许整读原图（单张 RAW 可达 60MB+）
HASH_CHUNK_SIZE: int = 8 * 1024 * 1024          # 8 * 1024 * 1024

# hash 算法：relPathHash（路径级去重）/ fileHash（内容级去重）统一用 SHA-256
HASH_ALGORITHM: str = "sha256"
# 库中 hash 字段的十六进制长度（CHAR(64)）
HASH_HEX_LEN: int = 64


# ============================================================
# 七之二、扫描器与 EXIF（步骤 3）
# ============================================================

# EXIF 的 DateTimeOriginal **不带时区**（EXIF 规范里就没这玩意儿），
# 只能按「拍摄地当时的本地时间」理解，再换算成 UTC 落 pb_photo.takenAt。
# 东八区 = 8；本机若不在东八区，改这一处即可。
EXIF_LOCAL_UTC_OFFSET_HOURS: float = 8.0

# shotYear 的可信区间：超出即视为「文件名误命中」，不采信
#（1990 年前的胶片扫件与 2100 的未来文件都不该出现在正常照片库里）
SHOT_YEAR_MIN: int = 1900
SHOT_YEAR_MAX: int = 2100

# 截图类文件名前缀（对 stem 做前缀匹配，大小写不敏感）
# 命中即 shotYear = NULL，**不参与跨年代桶比对**（截图的拍摄年份没有意义）
SCREENSHOT_NAME_PREFIXES: tuple = (
    "screenshot", "screen shot", "screencap", "screen recording",
    "截图", "截屏", "屏幕截图", "屏幕录制",
)

# 截图名是否连 EXIF 也不认。
#   False（**默认**）= EXIF 优先：只有「EXIF 也没有」时才当截图处理，shotYear=NULL。
#       理由：EXIF 的 DateTimeOriginal 是相机写的真实拍摄时间，比文件名可信；
#       误命名成 Screenshot_xxx.jpg 的真实照片不该丢掉EXIF 年份。
#   True  = 截图名压过 EXIF，一律 shotYear=NULL。
#       代价：少数带 EXIF 的截图（截图软件顺手写入了 EXIF）会把保存时间当年份。
SCREENSHOT_OVERRIDES_EXIF: bool = False

# 扫描索引加载的分页大小（**10 万行规模实测定的**，别随手改大）
#
# 为什么必须分页（实测数据，10 万行 pb_photo）
# ------------------------------------------
#   一次性 query_pb_photo 全量取行：峰值 **157.5 MB**，耗时 1.57 s
#   每页 2000 行分页取：  峰值 **  3.2 MB**，耗时 1.55 s   <- 内存 50 倍差，速度一样
#   （分页后紧凑索引本身常驻约 54 MB，那是判定必需的，不在优化范围）
# 一次性全取的那 150 MB 纯属「dict 列表的中间态」，白给。
# 2000 是实测拐点：再大内存线性涨，再小 SQLite 往返次数变多。
SCAN_INDEX_PAGE: int = 2000

# 每次扫描结束是否顺带做「库中存在但磁盘找不到」的缺失判定（默认开）。
# 关掉它可省掉每条库记录一次 stat（10 万条约 1 秒，实测 0.92 s），代价是 isMissing 永远不更新。
SCAN_MISSING_SWEEP: bool = True

# 每处理多少个文件写一次 pb_scan_job 进度（避免每张都写库）
PROGRESS_EVERY: int = 50

# 扫描任务默认归属人（pb_photo.ownerID / pb_scan_job.regID）。
# 步骤 9接API 后由登录态覆盖。
DEFAULT_OWNER_ID: str = "local"

# 扫描任务 jobCode 前缀（格式 SJ_<yyyymmddHHMMSS>_<6位随机>，VARCHAR(64) 内绰绰有余）
SCAN_JOB_CODE_PREFIX: str = "SJ"



# ============================================================
# 八、缩略图与人脸裁剪图（DR-1：落thumb\，路径可推导、不入库）
# ============================================================

# 缩略图宽度多尺寸（WebP）
THUMB_SIZES: tuple = (200, 400, 800)
# 缩略图格式与质量
THUMB_FORMAT: str = "WEBP"
THUMB_QUALITY: int = 82
# 人脸裁剪图边长与格式
FACE_CROP_SIZE: int = 160
FACE_CROP_FORMAT: str = "JPEG"
FACE_CROP_QUALITY: int = 85
# 人脸图裁成**正方形**（长=宽=FACE_CROP_SIZE）还是保持 bbox 长宽比
# 选 True：人员头像位按圆形遮罩渲染，方形图不必再由前端裁；斜脸/侧脸也不会
#         被拉成"人脑袋被压扁"的观感。选 False 则输出长边=160 的等比图。
FACE_CROP_SQUARE: bool = True

# 分桶子目录名（<thumb>\thumbs\<fileHash[:2]>\...、<thumb>\faces\<faceCode[:2]>\...）
THUMB_SUBDIR: str = "thumbs"
FACE_SUBDIR: str = "faces"
# 通讯录头像子目录名（步骤 8）。
#   落点：<thumb>\vcards\<sha1(personCode)[:2]>\<sha1(personCode)>.jpg
#
# 为什么单独一个目录（而不是塞进 faces/ 或 photos/）
#   faces/ 里的文件是**从照片里裁出来的人脸**，每张都挂在某个
#   pb_photo.photoCode 之下（pb_face.photoCode 是 NOT NULL 外键），而通讯录头像**不属于任何一张**
#   照片** —— 它是 vCard 里内嵌的 base64，跟 photo 库毫无关系。
#   混进 faces/ 会让「按 photoCode 找裁剪图」的逻辑多出一批无主的文件，
#   而清理/统计脚本无法区分它们是「从照片裁的」还是「通讯录带的」。
#   ⇒ 独立目录的代价只是多一个常量，收益是**两类数据沄澄分明**。
#
# ⚠️ 它不在 DERIVED_THUMB_SUBDIRS 里：邮箱头像是**真数据**（有库行指向它），
#    而 cluster/ 里的拼图才是临时产物。两者的寿命不同，别混在一起。
VCARD_SUBDIR: str = "vcards"
#: 通讯录头像落盘扩展名。vCard 里的 PHOTO 几乎全是 JPEG，
#: 且它是**用户自己放进通讯录的图**（已经被压缩过），
#: 再编码一次只会变模糊 —— 所以直接存原始字节。
VCARD_AVATAR_EXT: str = ".jpg"
# 分桶取hash 前几位
HASH_BUCKET_LEN: int = 2

# 缩略图落盘的扩展名（**由路径推导决定，不能改**——改了等于换了一套路径规则，
# 库里又没有 thumbPath 字段，老缩略图会全部变成孤儿文件）
THUMB_EXT: str = ".webp"
FACE_EXT: str = ".jpg"

# 原子写的临时文件扩展名：先写 <name>.tmp，再 os.replace 覆盖正式名。
# 磁盘上**永远不允许**出现 *.tmp 被前端读到（验收第 5 条）。
TMP_EXT: str = ".tmp"

# 缩略图接口的缺省尺寸（?size= 不传时用它；必须是 THUMB_SIZES 成员）
THUMB_DEFAULT_SIZE: int = 400

# WebP 编码 method：0 最快最差 / 6 最慢最好。4 是「体积/速度」拐点，
# 400px 缩略图用它比 method=6 快 3-4 倍而体积只大3%~5%。
THUMB_WEBP_METHOD: int = 4


# ============================================================
# 八之二、步骤 4 服务参数（缩略图与原图文件服务）
# ============================================================

# /api/thumb 的 Cache-Control。
# 缩略图是**内容寻址**的（文件名 = fileHash + size，同名即同内容，且永不原地改写），
# 所以可以放心给足一年 + immutable：改 quality 只会换 ETag 之外的东西，
# 真要作废整批缩略图，删掉 thumb\thumbs\ 重生成即可（生成物，可随时重建）。
THUMB_CACHE_CONTROL: str = "public, max-age=31536000, immutable"

# 单张按需生成用的线程池大小。
# 为什么按需用线程池、批量用进程池：
#   * 批量是 CPU 解码密集（Pillow 解 JPEG 主体在 C 层但仍吃满一个核），
#     多进程才能真正并行 -> make_thumbs_bulk 用 ProcessPoolExecutor；
#   * 按需是「等前端要图」，同一时刻通常只有1~2 张，用线程池即可，
#     免得起进程池的冷启动（Windows spawn 一个进程 ~0.3s，比解码还慢）
#     —— 顺带把 Pillow 的 GIL 等待也错开了一些。
THUMB_ON_DEMAND_WORKERS: int = 4

# 批量生成时每个子进程任务携带的照片条数。
# 太大：单条失败就要整批重跑；太小：IPC 往返与 pickle 开销占比上升。16 是个稳的值。
THUMB_BULK_CHUNK: int = 16

# ============================================================
# 九、步骤 8 联系人导入（CSV / vCard）
# ============================================================
# 这一步只建人员档案。分批提交（不搞长事务）是硬约束：
# 一次导 2000 行如果放在一个事务里，WAL 会一直涨、期间整库对读者写锁，
# 而「导入」本来就允许用户中途 Ctrl+C —— 分批提交让已经落库的部分是有效的。

#: 分批提交的行数（每次 insertManyTableGeneral 自带一个事务 = 一次 fsync）
IMPORT_BATCH_ROWS: int = 200

#: 读 pb_person_category 建「personCode -> 分类集合」索引时的分页大小
#: （与 SCAN_INDEX_PAGE 同理：全量取行在小表上无所谓，但别写成一次性 dict 列表）
IMPORT_PAGE_ROWS: int = 2000

#: 编码嗅探顺序。注意 utf-8 必须在 gbk **前面**：
#: 纯 ASCII 文本两种编码都能解出来，而中文 GBK 字节几乎不可能恰好是合法 UTF-8，
#: 反过来（先 gbk）则会把 UTF-8 中文解成乱码且**不报错**。
IMPORT_ENCODINGS: tuple = ("utf-8-sig", "utf-8", "gbk", "gb18030")

#: vCard 文件扩展名（Outlook 导出的是单联系人 .vcf）
IMPORT_VCARD_EXTS: tuple = (".vcf", ".vcard")

#: personCode 前缀。与 tools/import_contacts.py 的 "VC_" 保持一致，
#: 这样步骤 8 之前导进去的那 10 个人仍然是同一批人（编码不变= 不重复建档）
IMPORT_CODE_PREFIX_CSV: str = "CS_"
IMPORT_CODE_PREFIX_VCARD: str = "VC_"
IMPORT_CODE_PREFIX_FAMILY: str = "FM_"

#: BDAY 里 4 位数字被当成「年份」的合理窗口。
#: 低于下限的（如 vCard 的 --0210 去掉非数字正好剩 4 位）一律当「只有月日」，
#: 否则 birthYearOf 会拿到非法年份 -> 静默走等宽 5 年降级，而用户以为走的是自适应分桶。
IMPORT_MIN_YEAR: int = 1900
IMPORT_MAX_YEAR: int = 2099

#: 归档原件的子目录名前缀（落在 <dbDir>\imports\ 下，带时间戳）
IMPORT_ARCHIVE_PREFIX: str = "import"

# /api/original 流式回传原图时的读取块大小（Range 请求同样用它）。
# **绝不整读原图**（单张 RAW 可达 60MB+），也不整读缩略图。
STREAM_CHUNK_SIZE: int = 1024 * 1024          # 1 MB

# 原图 MIME 映射：mimetypes 在 Windows 注册表里对 .heic/.avif/.jxl 常查不到，
# 查不到就回退 application/octet-stream，浏览器会当下载而不是显示图 —— 必须自己兜住。
MIME_BY_EXT: dict = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".heic": "image/heic", ".heif": "image/heif",
    ".avif": "image/avif", ".jxl": "image/jxl",
    # RAW 浏览器基本都不认，给个通用二进制类型（前端走 /api/original 下载原件）
    ".dng": "application/octet-stream", ".cr2": "application/octet-stream",
    ".cr3": "application/octet-stream", ".nef": "application/octet-stream",
    ".arw": "application/octet-stream", ".arf": "application/octet-stream",
    ".raf": "application/octet-stream", ".orf": "application/octet-stream",
    ".rw2": "application/octet-stream", ".pef": "application/octet-stream",
    ".srw": "application/octet-stream", ".3fr": "application/octet-stream",
    ".erf": "application/octet-stream", ".kdc": "application/octet-stream",
    ".mos": "application/octet-stream", ".mrw": "application/octet-stream",
}


# ============================================================
# 九、服务
# ============================================================

# ⚠️ 只绑 127.0.0.1，绝不 0.0.0.0（见开发计划 §一 硬约束表）
SERVER_HOST: str = "127.0.0.1"
SERVER_PORT: int = 8765


if __name__ == "__main__":
    print("BATCH_SIZE      :", BATCH_SIZE)
    for _name in sorted(MATCH_THRESHOLD_PRESETS):
        _lo, _hi = matchThresholds(_name)
        _mark = " <- 当前" if _name == MATCH_THRESHOLD_PRESET else ""
        print("阈值预设 %-12s T_LOW=%.2f  T_HIGH=%.2f%s" % (_name, _lo, _hi, _mark))
    # s0 是唯一有实测背书的一套，T_HIGH/T_LOW 必须与它逐位一致，
    # 否则步骤 5/6 的回归结论会对不上（历史上这两个常量直接写死在代码里）
    assert (T_HIGH, T_LOW) == (MATCH_THRESHOLD_PRESETS["s0"]["tHigh"],
                               MATCH_THRESHOLD_PRESETS["s0"]["tLow"]), \
        "T_HIGH/T_LOW 必须等于 s0 预设"
    print("分桶            : <=%d 岁每 %d 年 / >%d 岁每 %d 年 / 降级等宽 %d 年"
          % (BUCKET_CHILD_MAX_AGE, BUCKET_CHILD_WIDTH, BUCKET_CHILD_MAX_AGE,
             BUCKET_ADULT_WIDTH, BUCKET_EQUAL_WIDTH))
    print("质心只用确认样本:", CENTROID_CONFIRMED_ONLY,
          "（False = 旧口径全样本，仅用于回归对比/冷启动）")
    print("MIN_DET_SCORE   :", MIN_DET_SCORE)
    print("MIN_FACE_EDGE   :", MIN_FACE_EDGE)
    print("MAX_YAW         :", MAX_YAW)
    print("PHOTO_EXTS      :", len(PHOTO_EXTS), "个")
    print("HASH_CHUNK_SIZE :", HASH_CHUNK_SIZE, "(%d MB)" % (HASH_CHUNK_SIZE // 1024 // 1024))
    print("EXIF 时区偏移   :", EXIF_LOCAL_UTC_OFFSET_HOURS, "小时")
    print("截图压过 EXIF   :", SCREENSHOT_OVERRIDES_EXIF, "（False = EXIF 优先）")
