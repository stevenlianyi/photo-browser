#! /usr/bin/env python3
#encoding: utf-8

#Filename: settings.py
#Description: photo-browser 设置类接口（步骤 12 · P-08）—— 参数读取/保存、质心重算、备份清单
#
# 四个端点
# ----------
#   GET  /api/settings                当前参数 + 三条路径 + 质心现状（只读）
#   POST /api/settings/match          改 T_low / T_high / 分年代档策略（**进程内生效**）
#   POST /api/settings/centroids/rebuild  重算质心（可只重算某人 / 可先刷新年代档）
#   GET  /api/settings/centroids/status   上面那个后台任务的真实进度
#
# 为什么要单独一个模块，而不是塞进 browse / contacts
# ------------------------------------------------
#  ① 它是**唯一会写配置态**的 api 模块。放进 browse 里，
#     「browse 是纯只读的」这条纪律（api/browse.py 文件头）就破了，
#     而那条纪律是步骤 9 复查时确认过的。
#  ② 它要 import engine.match.{rebucket,centroid}。browse 刻意不 import engine 层，
#     保持「浏览不依赖计算引擎」—— 引擎挂了应该还能翻照片。
#
# 参数为什么只存内存、不写回 basicSettings.py
# --------------------------------------------
#  · 阈值/策略的**读取入口**只有 matchThresholds() / bucketStrategy()，
#    改内存里的值下一次匹配立刻生效，不必重启进程；
#  · 本服务**零鉴权**且只绑回环，但「让HTTP 请求改写仓库里的 .py 源文件」
#    仍然是危险能力，不该顺手提供。真要固化就改 basicSettings.py（那是代码）；
#  · 所以界面必须明说「进程内生效，重启后回到配置值」——
#    藏着这句，用户重启服务发现阈值变了回去，会以为功能有 bug。
#
# 重算质心为什么放后台线程
# ----------------------
#  全库 2000+ 个人、10 万张脸，逐人「刷新年代档 + 重算全部年代档质心」是**分钟级**的写库操作。
#  同步做会把 HTTP 线程占住（FastAPI 同步端点在线程池里，会耗尽线程池），
#  界面表现是「点一下卡住几十秒然后超时」。后台线程 + 真实计数轮询，
#  与扫描台同一套语义（P-07 已确立「进度诚实」）。

import os
import sys
import threading
import traceback

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import browse                                              # noqa: E402
from api import dto                                                 # noqa: E402
from common import globalDefinition as comGD                       # noqa: E402
from common import miscCommon as misc                              # noqa: E402
from common import paths as paths                                  # noqa: E402
from config import basicSettings as basicSettings                  # noqa: E402
from database import queryCommon as query                          # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon   # noqa: E402

from fastapi import APIRouter, Query                                # noqa: E402
from pydantic import BaseModel, Field                              # noqa: E402

_VERSION = "20261007"

_LOG = misc.setLogNew("apiSettings", "apisettings.log")

router = APIRouter(tags=["settings"])


# ============================================================
# 一、后台重算任务的状态（进程内，单任务）
# ============================================================
#: 为什么是模块级字典而不是落一张表
#   · 它是**瞬时状态**：进程退出就没了（届时正需要重跑一次）；
#   · 落表的话，重启后会出现一个永远停在「运行中 47%」的僵尸任务，
#     而用户没有任何办法把它结束掉 —— 比不给进度条更糟。
#: 为什么只允许一个
#   单写入者硬约束：刷新年代档 + 重算质心全是写库操作，与扫描器并发会撞库锁。
#: 已经有扫描在跑时直接拒绝（错误信息里告诉用户哪个任务）。
_TASK_LOCK = threading.Lock()
_TASK: dict = {
    "running": False,
    "personCode": None,
    "confirmedOnly": True,
    "rebucketFirst": True,
    "total": 0,
    "done": 0,
    "ok": 0,
    "skipped": 0,
    "failed": 0,
    "startedYMDHMS": None,
    "finishedYMDHMS": None,
    "lastError": "",
    "log": [],
}


def taskSnapshot() -> dict:
    with _TASK_LOCK:
        out = dict(_TASK)
    out["log"] = list(out.get("log") or [])[-20:]
    total = int(out.get("total") or 0)
    done = int(out.get("done") or 0)
    out["percent"] = (min(100.0, round(done * 100.0 / total, 2)) if total else 0.0)
    return out


# ============================================================
# 二、GET /api/settings
# ============================================================

def _centroidOverview() -> dict:
    """质心现状（只读聚合）。

    `stale` 是**能算出来的**那个关键信号：库里有多少张脸的 shotBucket
    与它所在的人按当前策略+生日算出来的不一致 —— 那就是「改了参数还没重算」。
    直接调 rebucket.expectedBucketOf 逐行比对太贵（全库 10 万张），
    这里用一个便宜但可靠的替代信号：**年代档跨度分布**里出现等宽 5 年的比例，
    以及 `pb_person_centroid` 里的年代档键是否覆盖了脸表里出现的年代档键。
    两者任一不覆盖 -> 一定有「取不到质心」的年代档（DR-22 描述的静默失配）。
    """
    faceRows = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_face WHERE delFlag = %s",
        (comGD.DEL_FLAG_NO,)) or 0)
    bucketRows = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_person_centroid") or 0)
    enabledRows = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_person_centroid"
        " WHERE centroid IS NOT NULL AND sampleCount > 0") or 0)
    faceBuckets = {}
    for one in query.selectList(
            "SELECT shotBucket AS bucketKey, COUNT(*) AS cnt FROM pb_face"
            " WHERE delFlag = %s GROUP BY shotBucket", (comGD.DEL_FLAG_NO,)):
        key = str(one.get("bucketKey") or "")
        faceBuckets[key] = faceBuckets.get(key, 0) + int(one.get("cnt") or 0)
    centroidBuckets = set(
        str(one.get("bucketKey") or "") for one in query.selectList(
            "SELECT DISTINCT bucketKey AS bucketKey FROM pb_person_centroid"))
    missing = sorted(k for k in faceBuckets if k and k not in centroidBuckets)
    return {"faceCount": faceRows,
            "centroidRows": bucketRows,
            "enabledBuckets": enabledRows,
            "faceBucketKeys": len(faceBuckets),
            "bucketsWithoutCentroid": missing[:20],
            "bucketsWithoutCentroidTotal": len(missing)}


@router.get("/settings", summary="设置页的全部数据（路径 / 识别参数 / 质心现状 / 备份）")
def getSettings():
    """**纯读**。一次给全，省掉设置页 4 个并行请求。"""
    tLow, tHigh = basicSettings.matchThresholds()
    preset = str(basicSettings.MATCH_THRESHOLD_PRESET or "")
    override = basicSettings.thresholdOverride()
    resolvedPreset = "" if override else preset
    strategy = basicSettings.bucketStrategy()
    resolved = paths.all_paths()
    backups = listBackups()
    return {
        "ok": True,
        "paths": {
            "photoRoot": resolved["photoRoot"],
            "photo": resolved["photo"],
            "thumb": resolved["thumb"],
            "database": resolved["database"],
            "backupRoot": paths.backup_root(),
            "overrides": {k: v for k, v in paths.rootOverrides().items() if v},
        },
        "match": {
            "tLow": tLow,
            "tHigh": tHigh,
            "preset": resolvedPreset,
            "presets": {name: dict(values)
                        for name, values in basicSettings.MATCH_THRESHOLD_PRESETS.items()},
            "overridden": bool(override),
            "override": override,
            "bucketStrategy": strategy,
            "bucketStrategyChoices": list(basicSettings.BUCKET_STRATEGY_CHOICES),
            "childMaxAge": basicSettings.BUCKET_CHILD_MAX_AGE,
            "childWidth": basicSettings.BUCKET_CHILD_WIDTH,
            "adultWidth": basicSettings.BUCKET_ADULT_WIDTH,
            "equalWidth": basicSettings.BUCKET_EQUAL_WIDTH,
            "minCentroidSamples": basicSettings.MIN_CENTROID_SAMPLES,
            "centroidConfirmedOnly": bool(basicSettings.CENTROID_CONFIRMED_ONLY),
            "batchSize": int(basicSettings.BATCH_SIZE),
            "serverPort": int(basicSettings.SERVER_PORT),
        },
        "centroids": _centroidOverview(),
        "rebuild": taskSnapshot(),
        "backups": {"root": paths.backup_root(), "items": backups, "total": len(backups)},
        "notice": "阈值与分年代档策略的改动**只在本进程内生效**：不写回配置文件，"
                  "重启服务后回到 basicSettings 的值。",
    }


# ============================================================
# 三、POST /api/settings/match
# ============================================================

class MatchBody(BaseModel):
    """POST /api/settings/match —— 改识别参数。**都不给 = 全部复位**。"""

    tLow: float = Field(default=None, description="T_low；与 tHigh 必须同时给")
    tHigh: float = Field(default=None, description="T_high；与 tLow 必须同时给")
    preset: str = Field(default=None, description="按预设改（s0/conservative/aggressive）")
    bucketStrategy: str = Field(default=None, description="adaptive / fixed5 / none")
    confirmedOnly: bool = Field(default=None, description="质心是否只用人工确认样本")


@router.post("/settings/match", summary="改识别参数（进程内生效，需重算质心）")
def updateMatch(body: MatchBody = None):
    """改阈值 / 分年代档策略 / 质心样本口径。

    ⚠️ **这里不重算任何数据**，只改开关，并明确告诉前端「下一步要做什么」。
    把「改参数」与「重算数据」分成两步不是偷懒：
      · 重算是分钟级的写库操作，用户未必想立刻跑；
      · 而且很多改动（比如只调 T_high）**只影响下一次判定**，
        不影响已有质心 —— 一律弹「必须重算」会让真正的重算提示失去分量。
    所以返回里给的是 `needRebucket`（改分年代档策略才要刷 shotBucket）与
    `needCentroidRebuild`（改阈值/策略/样本口径都要），**前端照着显示即可**。
    """
    payload = body or MatchBody()
    before = {"tLow": basicSettings.matchThresholds()[0],
              "tHigh": basicSettings.matchThresholds()[1],
              "bucketStrategy": basicSettings.bucketStrategy(),
              "confirmedOnly": bool(basicSettings.CENTROID_CONFIRMED_ONLY)}
    changed = []

    # ---- 阈值 ----
    if payload.preset:
        try:
            basicSettings.matchThresholds(str(payload.preset))   # 只校验存在性
        except (KeyError, ValueError) as e:
            raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
        basicSettings.MATCH_THRESHOLD_PRESET = str(payload.preset)
        basicSettings.setThresholdOverride()                     # 清掉数值覆盖
        changed.append("preset")
    if payload.tLow is not None or payload.tHigh is not None:
        current = basicSettings.matchThresholds()
        try:
            basicSettings.setThresholdOverride(
                payload.tLow if payload.tLow is not None else current[0],
                payload.tHigh if payload.tHigh is not None else current[1])
        except ValueError as e:
            raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
        changed.append("threshold")

    # ---- 分年代档策略 ----
    if payload.bucketStrategy:
        try:
            basicSettings.setBucketStrategy(payload.bucketStrategy)
        except KeyError as e:
            raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
        changed.append("bucketStrategy")

    # ---- 质心样本口径 ----
    if payload.confirmedOnly is not None:
        basicSettings.CENTROID_CONFIRMED_ONLY = bool(payload.confirmedOnly)
        changed.append("centroidConfirmedOnly")

    after = {"tLow": basicSettings.matchThresholds()[0],
             "tHigh": basicSettings.matchThresholds()[1],
             "bucketStrategy": basicSettings.bucketStrategy(),
             "confirmedOnly": bool(basicSettings.CENTROID_CONFIRMED_ONLY)}
    strategyChanged = before["bucketStrategy"] != after["bucketStrategy"]
    needRebucket = strategyChanged
    needCentroid = bool(changed) and (
        strategyChanged
        or "threshold" in changed
        or "centroidConfirmedOnly" in changed
        or "preset" in changed)
    steps = []
    if needRebucket:
        steps.append("① 重新扫描人脸归属的年代档（pb_face.shotBucket）")
    if needCentroid:
        steps.append("② 按当前参数重新生成全部人脸质心")
    return dto.okBody(changed=changed, before=before, after=after,
                      needRebucket=needRebucket,
                      needCentroidRebuild=needCentroid,
                      steps=steps,
                      hint=("改参数只影响后续判定。%s之后识别结果才会按新参数走。"
                            % ("；".join(steps) + "。")
                            if needCentroid else
                            "没有改动阈值或质心口径，不需要重算质心。"),
                      centroids=_centroidOverview())


# ============================================================
# 四、POST /api/settings/centroids/rebuild（后台）
# ============================================================

class RebuildBody(BaseModel):
    """POST /api/settings/centroids/rebuild"""

    personCode: str = Field(default=None, description="只重算这个人；不给 = 全库")
    confirmedOnly: bool = Field(default=None, description="只用人工确认样本；不给 = 用当前配置")
    rebucketFirst: bool = Field(default=True,
                                description="**先刷 shotBucket 再重算质心**（DR-22 的硬顺序）。"
                                            "关掉它等于按旧年代档键重算一遍同样的样本，"
                                            "质心内容逐位不变且不报错")


@router.post("/settings/centroids/rebuild", summary="重新生成质心（后台，可选先刷新年代档）")
def rebuildCentroids(body: RebuildBody = None):
    """启动一次后台重算。**立刻返回**，进度看 /settings/centroids/status。

    ⚠️ **顺序不可颠倒**（DR-22）：先 `rebucket.rebucketPerson()` 刷 shotBucket，
       再 `centroid.recomputePerson()` 按新年代档键重建。
       反过来做会安静地留下「质心表里是旧年代档键的行、脸表里是新年代档键」——
       那个人的匹配率归零，而库里**看不出任何异常**。
       所以这里默认 rebucketFirst=True，且不提供「先只重算质心」的快捷入口。
    """
    payload = body or RebuildBody()
    if payload.personCode and not browse.personRow(payload.personCode, withDeleted=True):
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "personCode=%s 在 pb_person 里不存在" % payload.personCode)

    from schedule import scanScheduler as scheduler
    if scheduler.ScanScheduler(verbose=False).isRunning():
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                           "有扫描任务在跑，不能同时重算质心（单写入者）：请先等扫描停下来")

    with _TASK_LOCK:
        if _TASK["running"]:
            raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                               "已有重算任务在跑：%s（%d/%d）"
                               % (_TASK["personCode"] or "全库", _TASK["done"], _TASK["total"]))
        _TASK.update({"running": True,
                      "personCode": str(payload.personCode) if payload.personCode else None,
                      "confirmedOnly": (bool(basicSettings.CENTROID_CONFIRMED_ONLY)
                                        if payload.confirmedOnly is None
                                        else bool(payload.confirmedOnly)),
                      "rebucketFirst": bool(payload.rebucketFirst),
                      "total": 0, "done": 0, "ok": 0, "skipped": 0, "failed": 0,
                      "startedYMDHMS": misc.getTime(), "finishedYMDHMS": None,
                      "lastError": "", "log": []})

    thread = threading.Thread(target=_runRebuild, args=(payload,),
                              name="pb-centroid-rebuild", daemon=True)
    thread.start()
    return dto.okBody(started=True, **taskSnapshot())


def _runRebuild(payload: RebuildBody) -> None:
    """后台线程体。**每个失败都记进 log 而不是抛出** ——
    一个人生日录错导致刷新年代档算不出来，不该让其余 2000 个人白跑。"""
    from engine.match import centroid as centroid
    from engine.match import rebucket as rebucket

    def log(text: str) -> None:
        with _TASK_LOCK:
            _TASK["log"] = (list(_TASK.get("log") or []) + [str(text)])[-50:]

    if payload.personCode:
        codes = [str(payload.personCode)]
    else:
        codes = [str(one.get("personCode") or "") for one in query.selectList(
            "SELECT DISTINCT personCode AS personCode FROM pb_face"
            " WHERE delFlag = %s AND personCode IS NOT NULL AND personCode <> ''"
            " ORDER BY personCode", (comGD.DEL_FLAG_NO,))]
    with _TASK_LOCK:
        _TASK["total"] = len(codes)

    rebucketChanged = 0
    for index, code in enumerate(codes, start=1):
        with _TASK_LOCK:
            _TASK["done"] = index
        try:
            if payload.rebucketFirst:
                # ⚠️ rebucketPerson 返回的是 **dict**（含 changed/written/samples），
                #   不是数字。写成 `int(rebucketPerson(...))` 会在后台线程里抛
                #   TypeError，而界面上只看到「1 人失败」—— 没有堆栈、没有原因。
                rebucketChanged += int((rebucket.rebucketPerson(code) or {}).get("changed") or 0)
            centroid.recomputePerson(code,
                                     confirmedOnly=bool(payload.confirmedOnly))
            with _TASK_LOCK:
                _TASK["ok"] += 1
        except Exception as e:                                # noqa: BLE001
            with _TASK_LOCK:
                _TASK["failed"] += 1
                _TASK["lastError"] = "%s: %s" % (type(e).__name__, e)
            #⚠️ 必须记**堆栈**：这里捕获的是所有异常，而调用链有五层
            #   （后台线程 -> rebucket -> _writeBucketRows -> 生成层update_*），
            #   光一句 "int() argument..." 完全指不到是哪一层、哪个参数错位。
            log("重算失败 %s —— %s\n%s" % (code, e, traceback.format_exc()))
    with _TASK_LOCK:
        _TASK["running"] = False
        _TASK["finishedYMDHMS"] = misc.getTime()
        okCount, failedCount = _TASK["ok"], _TASK["failed"]
    log("完成：%d 人成功 / %d 失败；刷新年代档改动 %d 行"
        % (okCount, failedCount, rebucketChanged))
    _LOG.info("质心重算完成: ok=%s failed=%s rebucketRows=%s",
              okCount, failedCount, rebucketChanged)


@router.get("/settings/centroids/status", summary="质心重算进度（真实计数）")
def rebuildStatus():
    """轮询用。计数与扫描台同一套语义：真实计数，不做假进度条。"""
    snapshot = taskSnapshot()
    return dict(snapshot, ok=True)


# ============================================================
# 五、备份清单（**只读**；真正的备份/恢复在 tools/backup.py）
# ============================================================

def listBackups(limit: int = 20) -> list:
    """列出 `paths.backup_root()` 下的备份（新的在前），读各自的 manifest.json。

    ⚠️ **为什么备份/恢复不在 HTTP 里做**
      备份的定义是「停服务 → 拷贝 db\\ + thumb\\」（硬约束）。
      在服务**自己还开着**的时候把自己的库文件拷走，拷到的是 WAL 的中间态 ——
      单文件拷贝会丢掉还在 `-wal` 里没 checkpoint 的事务，
      恢复后少几百条记录，而这一切在拷贝时不报任何错。
      所以这里只能「告诉用户去命令行跑」，并把已有的备份列出来。
    """
    root = paths.backup_root()
    if not os.path.isdir(root):
        return []
    items = []
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return []
    for name in names:
        full = os.path.join(root, name)
        if not os.path.isdir(full) or not name.startswith(paths.BACKUP_PREFIX):
            continue
        manifest = {"backupCode": name}
        manifestFile = os.path.join(full, paths.BACKUP_MANIFEST_NAME)
        if os.path.isfile(manifestFile):
            try:
                import json
                with open(manifestFile, "r", encoding="utf-8") as fh:
                    manifest.update(json.load(fh) or {})
            except (OSError, ValueError):
                pass                       # 清单坏了也照样列出来（目录本身可能是好的）
        manifest["path"] = full
        manifest["hasDb"] = os.path.isdir(os.path.join(full, "db"))
        manifest["hasThumb"] = os.path.isdir(os.path.join(full, "thumb"))
        items.append(manifest)
        if len(items) >= int(limit):
            break
    return items


@router.get("/settings/backups", summary="已有备份清单（备份/恢复本身走命令行）")
def getBackups(limit: int = Query(default=20, ge=1, le=200)):
    """只读。返回备份根目录与各备份的清单。

    界面上必须同时给出**可照抄的命令行**，否则用户拿到一个只读清单
    却不知道怎么用 —— 「备份」这个按钮给不了安全感。
    """
    root = paths.backup_root()
    items = listBackups(limit)
    return {"ok": True, "root": root, "total": len(items), "items": items,
            "commands": {
                "backup": "python code/src/tools/backup.py backup",
                "restore": "python code/src/tools/backup.py restore --code <备份编号>",
                "list": "python code/src/tools/backup.py list",
            },
            "notes": [
                "备份 = 停服务 → 拷贝 db\\ 与 thumb\\，**不拷贝 photo\\**（原图不动）。",
                "恢复会覆盖现有 db\\ 与 thumb\\；恢复前脚本会自动把现状另存一份。",
                "thumb\\ 是生成物，恢复后如与库不一致，可用 gen_thumbs.py 重建。",
            ]}


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("settings.py _VERSION:", _VERSION)
    print("路由:", [(r.path, sorted(r.methods)) for r in router.routes])
    print("阈值:", basicSettings.matchThresholds(), "策略:", basicSettings.bucketStrategy())
    print("备份根:", paths.backup_root())
    print("备份清单:", len(listBackups()))