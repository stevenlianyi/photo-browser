#! /usr/bin/env python3
#encoding: utf-8

#Filename: verify_bucket_gain.py
#Description: photo-browser S0 回归（步骤 6 验收 9）—— 分桶 vs 不分桶的 FR/FA 对比
#
# 用法
# ----
#   # ① 真实 S0 验证集（需要 onnxruntime 或 insightface + D:\PhotoLib\verify）
#   python code\src\tools\verify_bucket_gain.py --root D:\PhotoLib\verify
#   python code\src\tools\verify_bucket_gain.py --root D:\PhotoLib\verify --csv out.csv
#
#   # ② 合成对照（**不需要模型**，跑的是同一套分桶/质心/匹配代码）
#   python code\src\tools\verify_bucket_gain.py --synthetic
#
#   # ③ 直接吃已有的 embedding（.npz，见 loadNpzSamples 的键名）
#   python code\src\tools\verify_bucket_gain.py --npz emb.npz
#
# 要回答的问题
# ------------
#   「分桶后的 FR 明显低于不分桶」这句话，能不能**在本项目代码上**复现？
#
#   三种口径（都跑同一套 centroid / 邻桶 / max 的代码）
#     A 全局不分桶：每人**一个全局质心**（跨全部年代混在一起）
#     B 等宽 5 年  ：**S0 的基线**（方案 A，出生年未知时的降级口径）
#     C 自适应分桶：0~18 岁每 3 年 / 18+ 每 10 年（本项目口径）
#   把 B 也放进来，是为了能与 S0 的 32.75% **直接对照** ——
#   只跟"完全不分桶"比是跟一个比 S0 更弱的基线比，降幅会显得很好看但没意义。
#
#   指标与**生产的三段式决策**同口径（不是自定义的）
#     T_HIGH / T_LOW 取自 basicSettings（同一套阈值）
#     分数 = 该人在候选桶里的**最大**余弦（与 matcher 一致）
#     FR    = 真人的最高分 <  T_LOW  -> 判成"陌生人"（进聚类）
#     FA    = 错人的最高分 >= T_HIGH  -> 错误自动归属（**最贵的错**）
#     自动对 = 真人最高分 >= T_HIGH 且确实是 top-1
#
#   探针（probe）只取**最早/最晚**年代的照片，训练集只取**中间**年代 ——
#   这正是真实场景：你要用一堆 2010 年的照片去认一张 1995 年的照片。
#   若训练集里含探针同年代的样本，任何方法都一眼能认出来，对比失去意义。
#
# ⚠️ 关于 --synthetic 的诚实声明
# ----------------------------
#   合成模式**只能证明"机制成立"**，不能替代真实 S0 的绝对数值。
#   它用的是一个人为构造的人脸模型（参数与三次踩坑记录见下方常量与
#   syntheticSamples），而模型的合理性只能靠"量出来的相似度分布与 S0 同量级"
#   来间接支撑 —— 所以本工具**强制打印相似度自检**。
#   若那一行与 S0 量级不符，下面的 FR/FA 就不该被采信，更不该被拿去汇报。
#   要真实数字就跑 --root。

import argparse
import csv
import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import numpy as np                                                # noqa: E402

from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from engine.face import engine as faceEngine                      # noqa: E402
from engine.match import bucket as bucket                         # noqa: E402
from engine.match import centroid as centroid                     # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("verifyBucket", "verifybucket.log")

# ============================================================
# 一、合成模式的规模与年代安排
# ============================================================
SYNTH_PEOPLE: int = 30
#: 训练（建质心）用中间三个成年桶（k=1/2/3），每桶 3 张
SYNTH_TRAIN_AGES: tuple = (30, 33, 36, 41, 44, 47, 52, 55, 57)
#: 探针只取训练区间**外**的最近两桶：k=0（18~27 岁）与 k=4（58~67 岁），各 2 张
SYNTH_PROBE_AGES: tuple = (20, 24, 60, 64)

#: 身份向量所在的**共享低维子空间**维数。必须小（见 _subspace 的说明）
SYNTH_SUBSPACE: int = 8
#: 身份 = normalize(prior + ID*u_i)。prior/id = 0.283/0.50
#:   -> 不同人之间余弦 = 1/(1+(ID/prior)^2) ≈ 0.35（与 S0 同量级）
SYNTH_PRIOR_WEIGHT: float = 0.283
SYNTH_ID_WEIGHT: float = 0.500
#: 每张照片的随机噪声（角度/表情/光照），同样取自 span(E)
SYNTH_NOISE: float = 0.35
#: 年龄漂移：**共享旋转**的饱和角（弧度）、衰减常数（年）、基准年龄
#:   θ(a) = SWEEP * sign(a-REF) * (1 - exp(-|a-REF| / TAU))
#:   同人相似度 ≈ cos(θ(a1) - θ(a2))：Δ=3 年 -> 0.92、Δ=10 年 -> 0.53、Δ=20 年 -> 0.19
SYNTH_AGE_SWEEP: float = 1.60
SYNTH_AGE_TAU: float = 10.0
SYNTH_AGE_REF: float = 43.0


def _fixConsole() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ============================================================
# 二、样本：list[dict(label, shotYear, birthYear, embedding, isProbe)]
# ============================================================

def _unitOrthogonal(rng, dim, bases):
    """在 bases 张成的正交补里取一个随机单位向量（子空间**之外**的方向）"""
    v = rng.randn(1, dim).ravel()
    for _ in range(2):
        for prev in bases:
            v = v - float(v @ prev) * prev
        n = float(np.linalg.norm(v))
        v = (v / n) if n > 1e-6 else rng.randn(1, dim).ravel()
    return faceEngine.l2normalize(v)


def _randomInSubspace(rng, basis, avoid=()):
    """在 span(basis) **之内**取一个随机单位向量（可对 avoid 里的向量正交化）。

    ⚠️⚠️ 这里必须用**标量**系数：`sum(rng.randn() * b for b in basis)`。
       写成 `sum(rng.randn(1, dim).ravel() * b for b in basis)` 就完蛋了 ——
       向量乘向量是**逐元素**相乘，得到的还是一条泛泛的随机向量，
       根本没落在 span(basis) 里。
       这个错的表现极其隐蔽：prior / 个体 / 噪声全都"看起来正常"
       （单位向量、两两余弦 0），但年龄旋转作用在它们身上毫无效果 ——
       相似度分布完全不对，而代码读起来毫无破绽。
       （本工具第一版就死在这里：同人跨 20 年相似度恒为 0.89。）
    """
    v = sum(float(rng.randn()) * b for b in basis)
    for other in list(avoid) + list(basis):
        v = v - float(v @ other) * other
    n = float(np.linalg.norm(v))
    if n < 1e-6:                       # 数值退化：换一条
        return _randomInSubspace(rng, basis, avoid)
    return faceEngine.l2normalize(v)


def _subspace(rng, dim, k):
    """取一个 k 维子空间的标准正交基（单位向量列表）。

    为什么必须把整个模型**关进一个低维子空间**
    ----------------------------------------
      这是本工具最关键的一处建模。第三版踩的坑：在 512 维空间里，
      一个 2 维的"年龄平面"几乎不携带能量（随机向量的分量约 1/√512 ≈ 0.044），
      在里面转 1.6 弧度对结果的影响 < 0.2% —— 等于没转。
      真实人脸 embedding 的**有效维度**远低于 512：年龄/性别/族裔都集中在
      少数方向上。把身份放进 k≈8 维子空间，年龄旋转才真的作用在身份本身，
      全局质心才会因为"年龄被糊掉"而失效 —— 那正是分桶要解决的问题。
    """
    basis = [faceEngine.l2normalize(rng.randn(1, dim).ravel())]
    while len(basis) < k:
        cand = rng.randn(1, dim).ravel()
        for prev in basis:                          # 对全部已有基投影掉
            cand = cand - float(cand @ prev) * prev
        basis.append(faceEngine.l2normalize(cand))
    return basis


def _blockRotation(rng, dim, basis):
    """在 span(E) 的**每一对**基向量上造一个同角的**共享**旋转 R(θ(age))。

    为什么必须对**所有**维度对都转同样的角
    ------------------------------------
      只在前两维上转的话，子空间里另外 6 维的分量一动不动，
      而向量的能量有 6/8 在那几维里 —— "年龄漂移"于是被稀释成
      cos ≈ 0.75 + 0.25·cos(Δθ)，Δθ 再大都掉不下去（实测跨 20 年仍 0.88）。
      对每一对都转同一个角 θ，则对子空间里**任意** u 都有
          cos(R(θ1)u, R(θ2)u) = Σ_i w_i²·cos(θ1-θ2) = cos(θ1-θ2)
      「同人相似度 = cos(年龄差对应的 θ 差)」成为一条干净可核对的关系。
    """
    pairs = [(basis[i], basis[i + 1]) for i in range(0, len(basis) - 1, 2)]
    refAge = float(SYNTH_AGE_REF)

    def rotate(vec, age):
        span = float(age) - refAge
        theta = SYNTH_AGE_SWEEP * (1.0 if span >= 0 else -1.0) \
            * (1.0 - float(np.exp(-abs(span) / SYNTH_AGE_TAU)))
        c, s = float(np.cos(theta)), float(np.sin(theta))
        out = vec.copy()
        for P, Q in pairs:
            p, q = float(vec @ P), float(vec @ Q)
            out = out + (c - 1.0) * p * P + (c - 1.0) * q * Q + s * q * P - s * p * Q
        return faceEngine.l2normalize(out)
    return rotate


def syntheticSamples(seed: int = 20261005) -> list:
    """合成一个跨年代的"家庭照片库"（每人 9 张训练 + 4 张探针）。

    人脸模型
    --------
      所有向量都活在**同一个 k 维子空间** span(E) 里（k = SYNTH_SUBSPACE）：
        ① 身份  ident_i = normalize(prior + ID*u_i)，prior 与 u_i 都取自 span(E)
           -> 不同人之间 ~0.35 的余弦（与 S0 同量级）
        ② 年龄  照片 = normalize( R(θ(age))·ident_i + NOISE·噪声 )
           ⇒ 同人相似度**随年龄差单调下降**（Δ=10 年 ~0.53、Δ=20 年 ~0.19），
           而**陌生人之间几乎不受年龄影响**（两人转同样的角度），恒在 ~0.35 ——
           于是跨年代时"像自己"与"像别人"真的会混淆，FR/FA 才会出现。
        ③ 噪声  span(E) 内的随机扰动（角度/表情/光照）

    ⚠️ 三次踩坑记录（原因都写在上面，别再改回去）
      ① 身份之间做成正交             -> FR 恒为 0（漂移了也还是最像自己）
      ② 年龄做成"叠加一个分量"       -> 取平均时个体成分不被削弱，
                                       全局质心永不失效，FR 仍为 0
      ③ 年龄做成 512 维里的 2 维旋转  -> 平面能量太低，转了等于没转，FR 仍为 0
      只有「身份住在低维子空间 + 年龄在其中做共享旋转」这一种组合会产出真实的 FR。

    训练/探针的年代安排（**对比公平性的关键**）
    ------------------------------------------
      训练：30/33/36、41/44/47、52/55/57 岁  -> 成年桶 k=1,2,3 各 3 张
      探针：20/24 岁（桶 k=0）、60/64 岁（桶 k=4）
      · 分桶侧：探针的**相邻桶**恰好是训练桶（k=0 的邻居含 k=1；k=4 的邻居含 k=3），
        于是**每一张探针都判得了** —— 不会因为"那边没数据"被跳过。
        那不是小瑕疵：第一版正因为大部分探针无候选而只剩 67/180 参与判定，
        分桶组被强行缩小，比出来的数字没有意义。
      · 不分桶侧：训练集是 30~57 岁的一团，均值被中间年龄拉平，
        20 岁与 64 岁的探针都得去和这团"平均脸"比。
      两侧**判定样本集合完全相同**，FR 才可比。
    """
    rng = np.random.RandomState(seed)
    dim = basicSettings.EMBEDDING_DIM
    basis = _subspace(rng, dim, SYNTH_SUBSPACE)
    rotate = _blockRotation(rng, dim, basis)
    prior = SYNTH_PRIOR_WEIGHT * _randomInSubspace(rng, basis)
    out = []
    for p in range(SYNTH_PEOPLE):
        birth = int(rng.randint(1955, 1990))
        ident = faceEngine.l2normalize(
            prior + SYNTH_ID_WEIGHT * _randomInSubspace(rng, basis, avoid=(prior,)))
        for age in list(SYNTH_TRAIN_AGES) + list(SYNTH_PROBE_AGES):
            noise = SYNTH_NOISE * _randomInSubspace(rng, basis)
            vec = faceEngine.l2normalize(rotate(ident, age) + noise)
            out.append({"label": "S%02d" % p, "shotYear": birth + age,
                        "birthYear": birth, "embedding": vec,
                        "isProbe": age in SYNTH_PROBE_AGES})
    return out


def syntheticStats(samples: list) -> dict:
    """量出合成数据的相似度分布，**打印出来供人核对 realism**。

    没有这一步，读者只看到一个 FR 数字，无法判断这个模型像不像人脸；
    而一个不像人脸的模型报出"分桶降 42%"是毫无意义的（自欺）。
    参考区间（S0 实测）：同人同龄 0.5~0.7 / 同人跨 20 年 0.25~0.45 / 陌生人 0.2~0.45

    ⚠️ 一律按**年龄差**分组，不按"同龄"分组：合成集里每个年龄只有一张，
    根本不存在同龄对；而且真实 ArcFace 的统计也是按年代组做的。
    """
    def _age(one):
        return one["shotYear"] - one["birthYear"]

    def _q(values, q=0.50):
        if not values:
            return None
        arr = np.sort(np.asarray(values, dtype=np.float64))
        return round(float(arr[min(len(arr) - 1, int(len(arr) * q))]), 3)

    near, mid, far, between = [], [], [], []
    byLabel = {}
    for one in samples:
        byLabel.setdefault(one["label"], []).append(one)
    labels = sorted(byLabel)
    for i, la in enumerate(labels):
        for x in byLabel[la]:
            for y in byLabel[la]:
                if x is y:
                    continue
                gap = abs(_age(x) - _age(y))
                cos = float(x["embedding"] @ y["embedding"])
                (near if gap <= 6 else (mid if gap < 20 else far)).append(cos)
        for lb in labels[i + 1:]:
            for x in byLabel[la]:
                for y in byLabel[lb]:
                    between.append(float(x["embedding"] @ y["embedding"]))
    return {"nearP50": _q(near), "midP50": _q(mid), "farP50": _q(far),
            "betweenP50": _q(between), "betweenP95": _q(between, 0.95),
            "pairs": {"near": len(near), "mid": len(mid), "far": len(far),
                      "between": len(between)}}


def verifySetSamples(root: str, workers: int = 0) -> list:
    """跑真实 S0 验证集：root\\<姓名>\\*.jpg，每张取面积最大的脸。

    验证集目录名只有姓名，**没有出生年** -> 走**等宽降级**（真实的 birthYear 未知情形）。
    拍摄年只从文件名前 4 位取；取不到的那些 shotYear=None -> **不参与分桶判定**，
    本工具单列出来，绝不混进 FR 统计（否则分桶组天然吃亏，那是不公平的比较）。
    """
    from engine.face import pool as facePool
    from engine.match import matcher as matcher

    people = []
    for name in sorted(os.listdir(root)):
        one = os.path.join(root, name)
        if os.path.isdir(one):
            people.append((name, one))
    if len(people) < 2:
        raise SystemExit("%s 下至少需要 2 个人员子目录，当前 %d 个。"
                         % (root, len(people)))
    tasks = []
    for name, personDir in people:
        for fileName in sorted(os.listdir(personDir)):
            if os.path.splitext(fileName)[1].lower() in (".jpg", ".jpeg", ".png",
                                                        ".bmp", ".webp"):
                tasks.append((os.path.join(personDir, fileName), name))
    batch = []

    def _onBatch(rows):
        batch.extend(rows)
    facePool.extractFaces(tasks, workers=workers or None,
                          engineKwargs={"wantCrop": False, "reportRejected": True},
                          onBatch=_onBatch)
    out = []
    for _index, absPath, label, result in batch:
        faces = result.get("faces") or []
        if not faces:
            continue
        picked = max(faces, key=lambda f: (float(f.get("areaRatio") or 0.0),
                                           -float(f.get("detScore") or 0.0)))
        vec = matcher.faceEmbedding({"embedding": picked.get("embedding")})
        if vec is None:
            continue
        stem = os.path.splitext(os.path.basename(absPath))[0]
        shotYear = int(stem[:4]) if len(stem) >= 4 and stem[:4].isdigit() else None
        out.append({"label": label, "shotYear": shotYear, "birthYear": None,
                    "embedding": vec, "isProbe": True, "path": absPath})
    return out


def loadNpzSamples(path: str) -> list:
    """读外部算好的 embedding（.npz）。

    需要的键：label[str] / shotYear[int] / embedding[2048 字节 float32]；
    可选 birthYear[int]（缺 = 未知，走等宽降级）。
    """
    from engine.match import matcher as matcher
    data = np.load(path, allow_pickle=True)
    out = []
    for i, label in enumerate(data["label"]):
        blob = data["embedding"][i]
        vec = matcher.faceEmbedding(
            {"embedding": blob.tobytes() if hasattr(blob, "tobytes") else blob})
        if vec is None:
            continue
        out.append({"label": str(label), "shotYear": int(data["shotYear"][i]),
                    "birthYear": (None if "birthYear" not in data
                                  else int(data["birthYear"][i])),
                    "embedding": vec, "isProbe": True})
    return out


# ============================================================
# 三、三种口径
# ============================================================
MODE_ALL: str = "global"      # 完全不分桶：每人一个全局质心
MODE_EQUAL: str = "equal"     # 等宽 5 年（S0 的基线）
MODE_ADAPT: str = "adaptive"  # 自适应（S0 结论、本项目口径）
MODE_TEXT = {MODE_ALL: "全局不分桶", MODE_EQUAL: "等宽5年(S0基线)",
             MODE_ADAPT: "自适应分桶"}
MODE_ORDER = (MODE_ALL, MODE_EQUAL, MODE_ADAPT)


def _bucketKeyOf(mode: str, one: dict) -> str:
    if mode == MODE_ALL:
        return "ALL"
    if mode == MODE_EQUAL:
        # 出生年**一律不用**：S0 的方案 A 就是"等宽、零前置条件"
        return bucket.bucketKeyAdaptive(one["shotYear"])
    return bucket.bucketKeyAdaptive(one["shotYear"], one["birthYear"])


def _indexOf(specs, names, minSamples):
    if not specs:
        return None, None
    matrix = np.ascontiguousarray(np.vstack([s[3] for s in specs]), dtype=np.float32)
    rows = [centroid.CentroidRow(i, i + 1, s[0], s[1], s[2])
            for i, s in enumerate(specs)]
    return matrix, centroid.CentroidIndex(matrix, rows,
                                          sorted(set(s[0] for s in specs)),
                                          names, minSamples)


def buildIndex(samples: list, mode: str, minSamples: int):
    """按某一口径建质心集合。样本数不足的桶**不启用**（与生产同一道门禁）。"""
    byKey = {}
    for one in samples:
        if one["isProbe"]:
            continue
        key = _bucketKeyOf(mode, one)
        if not key:
            continue
        byKey.setdefault((one["label"], key), []).append(one["embedding"])
    specs, names = [], {}
    for (label, key), vecs in sorted(byKey.items()):
        if len(vecs) < minSamples:
            continue
        cen = centroid.normalizedMean(vecs)
        if cen is None:
            continue
        code = "%s_%s" % (mode[0].upper(), label)
        names[code] = label
        specs.append((code, key, len(vecs), cen))
    return _indexOf(specs, names, minSamples)


def evaluate(samples: list, mode: str, minSamples: int, tLow: float,
             tHigh: float) -> dict:
    """按生产的三段式决策统计 FR / FA / 自动归属。

    ⚠️ 不走 matcher.match：那条路径的候选桶是**按人脸自己的 shotBucket** 推的，
       而"桶宽"必须与质心那一侧同族。这里显式给出候选桶（全局口径给 ["ALL"]），
       免得两边的桶族悄悄错位 —— 那是这类对比实验最容易出假结论的地方。
    """
    matrix, index = buildIndex(samples, mode, minSamples)
    out = {"mode": mode, "centroids": 0 if index is None else len(index),
           "probes": 0, "judged": 0, "topCorrect": 0, "fr": None, "fa": None,
           "autoCorrect": 0, "autoWrong": 0, "review": 0,
           "skippedNoYear": 0, "correctP50": None, "wrongP50": None}
    if index is None or not len(index):
        out["note"] = "没有启用的质心（样本不足或没有拍摄年份）"
        return out
    probes = [s for s in samples if s["isProbe"]]
    correctScores, wrongScores = [], []
    for probe in probes:
        keys = ["ALL"] if mode == MODE_ALL else bucket.neighborBucketKeys(
            _bucketKeyOf(mode, probe))
        if not keys:
            out["skippedNoYear"] += 1
            continue
        subset = index.subset(keys)
        if subset.personCount == 0:
            continue
        out["probes"] += 1
        simRow = np.asarray(subset.matrix, dtype=np.float32) @ probe["embedding"]
        scores = (np.maximum.reduceat(simRow, subset.starts)
                  if subset.personCount != len(subset) else simRow)
        bestAt = int(np.argmax(scores))
        best = subset.personCodes[bestAt]
        bestLabel = subset.displayNames.get(best, best)
        # 真人自己的最高分（FR 判据用它，不是 top-1）
        mine = -2.0
        for k in range(subset.personCount):
            if subset.displayNames.get(subset.personCodes[k],
                                       subset.personCodes[k]) == probe["label"]:
                mine = max(mine, float(scores[k]))
        top1right = (bestLabel == probe["label"])
        if top1right:
            out["judged"] += 1
            out["topCorrect"] += 1
            correctScores.append(mine)
        else:
            wrongScores.append(float(scores[bestAt]))
        # ---- 生产的三段式判定 ----
        if top1right and mine >= tHigh:
            out["autoCorrect"] += 1
        elif not top1right and float(scores[bestAt]) >= tHigh:
            out["autoWrong"] += 1
        else:
            out["review"] += 1
        # ---- FR：真人最高分低于 T_LOW -> 判成陌生人 ----
        if mine < tLow:
            out["fr"] = (out["fr"] or 0.0)
            out["_frCount"] = out.get("_frCount", 0) + 1
    total = out["probes"]
    if total:
        out["fr"] = out.get("_frCount", 0) / float(total)
        out["fa"] = out["autoWrong"] / float(total)
    if correctScores:
        out["correctP50"] = round(float(np.median(correctScores)), 4)
    if wrongScores:
        out["wrongP50"] = round(float(np.median(wrongScores)), 4)
    out.pop("_frCount", None)
    return out


# ============================================================
# 四、main
# ============================================================

def main(argv=None) -> int:
    _fixConsole()
    p = argparse.ArgumentParser(description="S0 回归：分桶 vs 不分桶的 FR/FA")
    p.add_argument("--root", default="", help="S0 验证集根目录（每人一个子目录）")
    p.add_argument("--npz", default="", help="外部 embedding（.npz）")
    p.add_argument("--synthetic", action="store_true", help="合成对照（不需模型）")
    p.add_argument("--csv", default="", help="把样本表导出 CSV")
    args = p.parse_args(argv)

    print("verify_bucket_gain.py _VERSION:", _VERSION)
    tLow, tHigh = basicSettings.matchThresholds()
    minSamples = basicSettings.MIN_CENTROID_SAMPLES
    print("判定阈值    : T_LOW=%.2f / T_HIGH=%.2f（预设 %s，与生产同一套）"
          % (tLow, tHigh, basicSettings.MATCH_THRESHOLD_PRESET))
    print("启用门槛    : sampleCount >= %d" % minSamples)

    if args.synthetic:
        samples = syntheticSamples()
        print("样本        : 合成 %d 人（每人 %d 张训练 + %d 张探针）"
              % (SYNTH_PEOPLE, len(SYNTH_TRAIN_AGES), len(SYNTH_PROBE_AGES)))
        print("            训练年龄 %s（成年桶 k=1,2,3 各 3 张）"
              % list(SYNTH_TRAIN_AGES))
        print("            探针年龄 %s（桶 k=0 与 k=4，各 2 张）"
              % list(SYNTH_PROBE_AGES))
        print("人脸模型    : 身份住在 %d 维共享子空间；照片 = normalize("
              "R(θ(a))·身份 + %.2f·噪声)" % (SYNTH_SUBSPACE, SYNTH_NOISE))
        print("            θ(a) = %.2f·sign·(1-exp(-|a-%.0f|/%.0f))，"
              "**所有人共享同一组旋转**"
              % (SYNTH_AGE_SWEEP, SYNTH_AGE_REF, SYNTH_AGE_TAU))
        st = syntheticStats(samples)
        print("相似度自检  : 同人差 1~6 年 p50=%.3f（%d 对）/ 差 7~19 年 p50=%.3f（%d）"
              " / 差 >=20 年 p50=%.3f（%d）"
              % (st["nearP50"], st["pairs"]["near"], st["midP50"],
                 st["pairs"]["mid"], st["farP50"], st["pairs"]["far"]))
        print("            陌生人 p50=%.3f p95=%.3f（%d 对）"
              % (st["betweenP50"], st["betweenP95"], st["pairs"]["between"]))
        print("            S0 实测参考：同人同龄 0.5~0.7 / 同人跨 20 年 0.25~0.45"
              " / 陌生人 0.2~0.45 —— 量级对不上就别信下面的数字")
    elif args.npz:
        samples = loadNpzSamples(args.npz)
        print("样本        : %s（%d 条）" % (args.npz, len(samples)))
    elif args.root:
        start = time.perf_counter()
        samples = verifySetSamples(args.root)
        print("样本        : %s（%d 条，耗时 %.1fs）"
              % (args.root, len(samples), time.perf_counter() - start))
    else:
        p.print_help()
        return 2

    results = [evaluate(samples, mode, minSamples, tLow, tHigh)
               for mode in MODE_ORDER]
    print("\n%-14s %7s %8s %9s %9s %9s %9s %9s"
          % ("口径", "质心数", "参与判定", "FR", "FA", "自动对", "自动错", "灰区"))
    for one in results:
        def _pct(key):
            return "-" if one.get(key) is None else "%.1f%%" % (one[key] * 100.0)
        print("%-14s %7d %8d %9s %9s %9d %9d %9d"
              % (MODE_TEXT[one["mode"]], one["centroids"], one["probes"],
                 _pct("fr"), _pct("fa"), one["autoCorrect"], one["autoWrong"],
                 one["review"]))
        if one.get("skippedNoYear"):
            print("              ⚠ %d 张探针没有拍摄年份，**不参与**本口径判定"
                  % one["skippedNoYear"])
        if one.get("note"):
            print("              ", one["note"])
    print("\n正确匹配的分数中位数（越高越有把握）:")
    for one in results:
        print("   %-14s %s" % (MODE_TEXT[one["mode"]],
                              "-" if one["correctP50"] is None
                              else "%.4f" % one["correctP50"]))

    base, adapt = results[0], results[-1]
    if base["fr"] is not None and adapt["fr"] is not None:
        if base["fr"] > 0:
            drop = (base["fr"] - adapt["fr"]) / base["fr"] * 100.0
            print("\n自适应 vs 全局不分桶: FR %.1f%% -> %.1f%%（相对降幅 %.1f%%）"
                  % (base["fr"] * 100.0, adapt["fr"] * 100.0, drop))
        else:
            print("\n全局不分桶的 FR 已是 0，这个模型分辨得太容易（相似度分布"
                  "没落在 S0 量级），本次对照**不足以支持结论**")
        if adapt["fr"] is not None and base["fr"] is not None and \
                adapt["fr"] < base["fr"]:
            print("结论        : **分桶后 FR 更低，机制成立**")
        elif base["fr"] is not None:
            print("结论        : 分桶没有降低 FR —— 规则或实现需要复查")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["label", "shotYear", "birthYear", "isProbe",
                             "bucketAdaptive", "bucketEqual"])
            for one in samples:
                writer.writerow([one["label"], one["shotYear"], one["birthYear"],
                                 int(bool(one["isProbe"])),
                                 bucket.bucketKeyAdaptive(one["shotYear"],
                                                          one["birthYear"]) or "",
                                 bucket.bucketKeyAdaptive(one["shotYear"]) or ""])
        print("\nCSV         :", args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
