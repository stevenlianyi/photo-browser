#! /usr/bin/env python3
#encoding: utf-8
#Filename: backtest_s0.py
#Description: photo-browser S0 口径回测（步骤 5 验收 1/2）
#
# 用法
# ----
#   python code\src\tools\backtest_s0.py --root D:\PhotoLib\verify
#   python code\src\tools\backtest_s0.py --root D:\PhotoLib\verify --workers 8 --csv out.csv
#
# 验证集格式与 S0 脚本完全一致：每人一个子目录，目录名 = 姓名
#     root\张三\1.jpg ... 10.jpg
#
# 干什么
# ------
#   用本项目的 engine/face（不是 insightface 原始 API）跑一遍 S0 的验证集，
#   输出组内/组间余弦分布、全阈值扫描（FA/FR）、推荐阈值、单张耗时 p50/p95。
#   目的是与 S0 实测（photoapp/tools/verify_accuracy.py）逐项对齐：
#   阈值取自 basicSettings（detScore 0.6 / 短边 64px / yaw 45 度），
#   每张取面积最大的脸（S0 的取法），连丢弃原因码的名字都一样。
#   只有口径一致，"准确率一致 2% 以内" 才是可比的；否则是在比两把不同的尺子。
#
# 不做的事
#   * 不写库、不裁人脸图（wantCrop=False）：纯统计工具，别把验证集混进正式库
#   * 不做分桶匹配（步骤 6 的事）。这里只出单阈值基线，与 S0 同口径。

import argparse
import csv
import itertools
import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import numpy as np

from common import miscCommon as misc
from config import basicSettings as basicSettings
from engine.face import engine as faceEngine
from engine.face import pool as facePool

_VERSION = "20261004"

_LOG = misc.setLogNew("backtestS0", "backtests0.log")

#: 只扫这些扩展名（与 S0 脚本一致；HEIC 需额外插件，暂不处理）
IMG_EXT = {"jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff"}

#: 全阈值扫描区间（与 S0 脚本逐字一致：0.30~0.70 步长 0.01）
THRESHOLDS = np.round(np.arange(0.30, 0.71, 0.01), 2)

#: 默认验证集根（**只作缺省**，可用 --root 覆盖；不写死绝对路径）
DEFAULT_ROOT = r"D:\PhotoLib\verify"


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def listPeople(root: str) -> list:
    """验证集里的人员子目录（目录名 = 姓名），按名称排序保证可复现"""
    out = []
    for name in sorted(os.listdir(root)):
        one = os.path.join(root, name)
        if os.path.isdir(one):
            out.append((name, one))
    return out


def listPhotos(personDir: str) -> list:
    files = []
    for name in sorted(os.listdir(personDir)):
        if os.path.splitext(name)[1].lower().lstrip(".") in IMG_EXT:
            files.append(os.path.join(personDir, name))
    return files


def collectEmbeddings(root: str, engineKwargs: dict = None, workers: int = 0):
    """跑验证集，返回 (embeddings, stats, timing, engineInfo)

    embeddings : dict[人名] -> list[np.float32[512]]（已 L2 归一化）
    stats      : dict[人名] -> {files, ok, 丢弃原因码...}
    timing     : 单张耗时统计 {count, mean, p50, p95, max}

    为什么走 **pool.extractFaces** 而不是循环调 engine
    --------------------------------------------------
      ① 走的是生产同一条路径（含分阶段过滤、进程池、中文路径读图），
         统计出来的就是「上线后真会发生的数」；
      ② workers>0 时多进程跑，100 张从 ~90 秒降到 ~20 秒；
         workers=0 时退化为单进程内联，便于单测直接调（不 spawn 子进程）。
    """
    people = listPeople(root)
    if len(people) < 2:
        raise SystemExit("%s 下至少需要 2 个人员子目录，当前找到 %d 个。"
                         % (root, len(people)))
    tasks = []
    owner = {}
    for name, personDir in people:
        for path in listPhotos(personDir):
            tasks.append((path, name))
            owner[path] = name

    kwargs = dict(engineKwargs or {})
    kwargs["wantCrop"] = False            # 纯统计，不裁图
    kwargs["reportRejected"] = True       # 复现 S0 的取脸口径（见 _pickS0Face）
    batchRows = []

    def _onBatch(batch):
        batchRows.extend(batch)

    summary = facePool.extractFaces(tasks, workers=workers or None,
                                    engineKwargs=kwargs, onBatch=_onBatch,
                                    batchSize=64)
    embeddings = dict((name, []) for name, _dir in people)
    stats = dict((name, dict(files=0, ok=0)) for name, _dir in people)
    for _index, absPath, _photoCode, result in batchRows:
        name = owner.get(absPath)
        if name is None:
            continue
        if result.get("engineInfo"):
            summary["engineInfo"] = result["engineInfo"]
        stats[name]["files"] += 1
        reason, vec = _pickS0Face(result)
        if vec is None:
            # 与 S0 的 reasons 同口径记「为什么这张图没留下样本」
            stats[name][reason] = stats[name].get(reason, 0) + 1
            continue
        embeddings[name].append(vec)
        stats[name]["ok"] += 1
    timing = facePool.timingStats(summary["elapsedList"])
    return embeddings, stats, timing, summary


def _pickS0Face(result: dict) -> tuple:
    """按 **S0 的口径**从一张照片里挑样本脸，返回 (reason, vec|None)。

    S0 的口径（verify_accuracy.py 的 embed_one）
    ------------------------------------------
        1. 读图失败        -> decode_fail
        2. 一张脸都没有    -> no_face
        3. **先取面积最大的那张脸**
        4. 再对这一张脸判 detScore / 短边 / |yaw| -> low_det / too_small / side_face

    而本引擎是**先过滤、再在存活者里取最大**。两者在一张图里
    「最大脸是侧脸、次大脸是正脸」时会差出一张样本。
    实测 100 张验证集里恰好有 1 张这样：
        S0 口径  -> 99 个样本（该张整张丢弃）
        本引擎   -> 100 个样本（用上了次大脸）
    而这 1 张正好落在「组间相似度最大值」上，会把推荐阈值从 0.43 顶到 0.64 ——
    也就是**验收第 1 条会误判成"准确率差很多"**。
    所以回测必须显式复现 S0 的口径：拿 engine 报上来的 droppedFaces 一起参与
    「取面积最大」，再对选中的那一张判质量。

    本引擎的行为本身**不改成 S0 那样** —— 生产上"别因为最大那张是侧脸就丢整张脸"
    是更对的行为，这里只是让统计口径可比。
    """
    if not result.get("ok"):
        reason = faceEngine.REASON_NO_FILE
        for code, count in (result.get("rejected") or {}).items():
            if count:
                reason = code
                break
        return reason, None
    survivors = result.get("faces") or ()
    dropped = result.get("droppedFaces") or ()
    if not survivors and not dropped:
        return faceEngine.REASON_NO_FACE, None
    # 把被丢弃的脸也算进来参与「取面积最大」，这样选中的那张与 S0 完全一致。
    # ⚠️ 存活脸带的是 areaRatio（占画面比例），被丢弃脸带的是像素面积 area，
    #    **必须先统一到同一量纲再比**，否则两类脸的排序是错的
    #    （0.05 的比例会输给任何一块 0.05 平方像素的绝对面积）。
    px = max(1, int(result.get("imgW") or 1)) * max(1, int(result.get("imgH") or 1))
    pool = [{"area": float(d.get("areaRatio") or 0.0), "d": d, "dropped": False}
            for d in survivors]
    pool.extend({"area": float(d.get("area") or 0.0) / px, "d": d, "dropped": True}
                for d in dropped)
    best = max(pool, key=lambda item: item["area"])
    if best["dropped"]:
        # 被丢弃就没有 embedding，reason 用引擎给出的那一档
        return str(best["d"].get("reason") or faceEngine.REASON_LOW_DET), None
    vec = faceEngine.l2normalize(
        faceEngine.decodeEmbedding(best["d"].get("embedding")))
    if vec is None:
        return faceEngine.REASON_ZERO_VEC, None
    return "", vec


def computeDistributions(embeddings: dict) -> tuple:
    """组内（同一人）与组间（不同人）的余弦相似度分布（与 S0 同算法）"""
    intra, inter = [], []
    for vecs in embeddings.values():
        for a, b in itertools.combinations(range(len(vecs)), 2):
            intra.append(faceEngine.cosine(vecs[a], vecs[b]))
    names = list(embeddings)
    for i, j in itertools.combinations(range(len(names)), 2):
        A, B = embeddings[names[i]], embeddings[names[j]]
        if not A or not B:
            continue
        inter.extend(float(v) for v in (np.stack(A) @ np.stack(B).T).ravel())
    return np.asarray(intra, dtype=np.float32), np.asarray(inter, dtype=np.float32)


def sweep(intra: np.ndarray, inter: np.ndarray) -> tuple:
    """全阈值扫描。返回 (rows, best, bestNearly)，与 S0 的 sweep 同名同义。"""
    rows = []
    for t in THRESHOLDS:
        fa = int((inter >= t).sum())
        fr = int((intra < t).sum())
        rows.append({"threshold": float(t), "fa": fa, "fr": fr,
                     "far": fa / max(inter.size, 1), "frr": fr / max(intra.size, 1)})
    safe = [r for r in rows if r["fa"] == 0]
    best = min(safe, key=lambda r: (r["frr"], -r["threshold"])) if safe else None
    nearly = [r for r in rows if r["far"] < 0.001]
    bestNearly = min(nearly, key=lambda r: (r["frr"], -r["threshold"])) if nearly else None
    return rows, best, bestNearly


def _run(args) -> int:
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("[Error] 验证集目录不存在: %s" % root)
        return 1
    # 子进程不连库的自查（与 run_faces 同一道）
    facePool.assertNoDatabaseImport()

    print("=" * 68)
    print("S0 口径回测  |  验证集 %s" % root)
    print("  阈值: detScore>=%s  短边>=%spx  |yaw|<=%s  （与 S0 脚本同一套）"
          % (args.min_det, args.min_edge, args.max_yaw))
    print("  workers=%s   模型 %s" % (args.workers or "auto", args.model))
    print("=" * 68)

    engineKwargs = {"modelPack": args.model,
                    "detSize": (args.det_size, args.det_size),
                    "minDetScore": args.min_det, "minFaceEdge": args.min_edge,
                    "maxYaw": args.max_yaw}
    t0 = time.time()
    embeddings, stats, timing, summary = collectEmbeddings(
        root, engineKwargs=engineKwargs, workers=args.workers)
    wall = time.time() - t0

    total = sum(s["files"] for s in stats.values())
    ok = sum(s["ok"] for s in stats.values())
    info = summary.get("engineInfo") or {}
    if info:
        print("\n引擎: 后端=%s  模型包=%s  已加载模块=%s  姿态可用=%s"
              % (info.get("backend"), info.get("modelPack"),
                 info.get("loadedModules"), info.get("poseReady")))
        if info.get("backendError"):
            print("  [ERROR] insightface 后端加载失败，已降级: %s" % info["backendError"])
            trace = str(info.get("backendTrace") or "").strip().splitlines()
            for line in trace[-12:]:
                print("        | %s" % line)
        if not info.get("poseReady", False):
            print("  [WARN] 姿态模型不可用 -> |yaw|<=45 这条质量过滤**本轮不生效**"
                  "（与 S0 口径不一致，数字不可直接对比）")
    print("\n每人有效人脸:")
    for name in sorted(stats):
        one = stats[name]
        extra = "".join("   丢弃 %s=%d" % (k, v)
                        for k, v in sorted(one.items())
                        if k not in ("files", "ok") and v)
        print("  %-14s %3d/%-3d 张有效人脸%s" % (name, one["ok"], one["files"], extra))
    print("\n合计 %d/%d 张产出有效特征（%.1f%%）   墙钟 %.1f 秒   进程池 %s"
          % (ok, total, (100.0 * ok / total) if total else 0.0, wall,
             summary["mode"]))
    if summary["dropped"]:
        print("过滤汇总: " + "  ".join("%s=%d" % (k, v)
                                       for k, v in sorted(summary["dropped"].items())))

    print("\n" + "=" * 68)
    print("单张耗时（含检测+特征+质量过滤，不含裁图）")
    print("=" * 68)
    print("  本次配置 workers=%s：均值 %.3fs  p50 %.3fs  p95 %.3fs  max %.3fs  (n=%d)"
          % (args.workers or "auto", timing["mean"], timing["p50"], timing["p95"],
             timing["max"], timing["count"]))
    if args.workers and args.workers > 1:
        print("  注意：workers>1 时每个子进程被钉成 1 个 ORT 线程（防 8x16 线程互相抢核），")
        print("        所以**单张延迟会随并行度线性变长**，这不代表变慢了，代表并行换吞吐。")
        print("        验收项「单张 <1s」按 **workers=1**（一张照片独占全部核心）判。")
    print("  验收项「单张 <1s」: %s（本配置均值 %.3fs）"
          % ("PASS" if timing["mean"] < 1.0 else "见上方说明", timing["mean"]))

    intra, inter = computeDistributions(embeddings)
    print("\n" + "=" * 68)
    print("相似度分布")
    print("=" * 68)
    if intra.size:
        print("组内(同一人)   %6d 对   均值 %.3f   5%% 分位 %.3f   最小 %.3f"
              % (intra.size, intra.mean(), np.percentile(intra, 5), intra.min()))
    else:
        print("组内(同一人)   0 对 —— 有效样本不足（每人至少 2 张有效人脸）")
    if inter.size:
        print("组间(不同人)   %6d 对   均值 %.3f   95%% 分位 %.3f   最大 %.3f"
              % (inter.size, inter.mean(), np.percentile(inter, 95), inter.max()))
    else:
        print("组间(不同人)   0 对")
    if not intra.size or not inter.size:
        print("\n[Error] 样本不足，无法给出阈值建议")
        return 1

    lo = float(np.percentile(intra, 5))
    hi = float(np.percentile(inter, 95))
    if lo < hi:
        print("\n!! 组内 5%% 分位(%.3f) 低于 组间 95%% 分位(%.3f) —— 两分布有重叠，"
              "FA=0 要付较高 FR 代价。这条正是「分桶是否必需」的量化证据（步骤 6）。"
              % (lo, hi))
    else:
        print("\nOK  组内 5%% 分位(%.3f) 高于 组间 95%% 分位(%.3f) —— 分离度良好。"
              % (lo, hi))

    rows, best, bestNearly = sweep(intra, inter)
    print("\n" + "=" * 68)
    print("阈值扫描（FA = 把不同的人判成同一人，必须趋近 0）")
    print("=" * 68)
    print("阈值    误接受FA   误拒FR    FA率       FR率")
    for r in rows:
        mark = "  <<<" if best and r["threshold"] == best["threshold"] else ""
        if mark or abs(r["threshold"] * 100 - round(r["threshold"] * 100)) < 1e-9:
            print("%.2f   %7d   %7d   %7.2f%%  %7.2f%%%s"
                  % (r["threshold"], r["fa"], r["fr"], r["far"] * 100.0,
                     r["frr"] * 100.0, mark))

    print("\n" + "=" * 68)
    print("结论")
    print("=" * 68)
    if best:
        t = best["threshold"]
        print("推荐阈值（FA=0）：%.2f  ->  FA率 %.2f%%  FR率 %.2f%%  "
              "（漏识别 %d/%d 对）"
              % (t, best["far"] * 100.0, best["frr"] * 100.0, best["fr"], intra.size))
        print("  [PASS] False Accept = 0")
        print("  [%s] False Reject < 15%%  (实测 %.2f%%)"
              % ("PASS" if best["frr"] < 0.15 else "WARN", best["frr"] * 100.0))
        print("  [%s] 最佳阈值落在 0.40-0.60  (实测 %.2f)"
              % ("PASS" if 0.40 <= t <= 0.60 else "WARN", t))
    else:
        print("不存在 FA=0 的阈值 —— 不要继续往下做")
    if bestNearly and (not best or bestNearly["threshold"] != best["threshold"]):
        print("备选阈值（FA率<0.1%%，换取更低 FR）：%.2f -> FA率 %.3f%%  FR率 %.2f%%"
              % (bestNearly["threshold"], bestNearly["far"] * 100.0,
                 bestNearly["frr"] * 100.0))
    print("对比 S0 脚本请用同一验证集：verify_accuracy.py %s" % root)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=["threshold", "fa", "fr", "far", "frr"])
            writer.writeheader()
            writer.writerows(rows)
        print("\n阈值扫描已导出: %s" % os.path.abspath(args.csv))
    return 0


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backtest_s0.py",
        description="photo-browser S0 口径回测（准确率 + 耗时统计）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help="验证集根目录（每人一个子目录，目录名=姓名）。缺省 %s" % DEFAULT_ROOT)
    parser.add_argument("--workers", type=int, default=0, help="进程数（0=自动 cpu-1）")
    parser.add_argument("--model", default=faceEngine.DEFAULT_MODEL_PACK,
                        help="模型包名")
    parser.add_argument("--det-size", type=int, default=640, help="检测边长")
    parser.add_argument("--min-det", type=float, default=basicSettings.MIN_DET_SCORE,
                        help="detScore 下限（S0 口径 0.6）")
    parser.add_argument("--min-edge", type=int, default=basicSettings.MIN_FACE_EDGE,
                        help="人脸框短边下限 px（S0 口径 64）")
    parser.add_argument("--max-yaw", type=float, default=basicSettings.MAX_YAW,
                        help="侧脸 yaw 绝对值上限（S0 口径 45）")
    parser.add_argument("--csv", default=None, help="把阈值扫描结果导出到 CSV")
    return parser


def main(argv=None) -> int:
    _fixConsole()
    args = buildParser().parse_args(argv)
    print("backtest_s0 _VERSION: %s" % _VERSION)
    try:
        return _run(args)
    except KeyboardInterrupt:
        return 130
    except SystemExit as e:
        print("[Error] %s" % e)
        return 1
    except Exception as e:
        print("[Error] %s: %s" % (type(e).__name__, e))
        _LOG.error("backtest_s0 失败: %s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

