#! /usr/bin/env python3
#encoding: utf-8

# Filename: scanScheduler.py
# Description: photo-browser 扫描任务调度与状态流转（步骤 3）
#
# 职责
# ----
#   1. 任务生命周期：createJob（jobCode 幂等）/ getJob / listJobs
#   2. 状态机：IDLE -> RUNNING -> (PAUSED -> 继续 -> RUNNING) / DONE / FAILED
#      流转合法性一律走 globalDefinition.canTransit，**调度层不自己发明规则**
#   3. 执行：runBatch 跑一批、runUntilDone 跑到完、startBackground 后台线程跑
#   4. 异常：任何未捕获异常 -> 记 errMsg（截 512）+ 置 FAILED，**绝不留一个 RUNNING 的僵尸任务**
#   5. 完成收尾（步骤 R4b）：真的跑完（DONE）后自动做一次**地点侧收尾** ——
#      填 `pb_photo.placeNameDir` → 重建 `pb_place` →（可选）补 `nameZh`，
#      见 processor/place/placeFinalize.py。⚠️ 挂在**状态机这一处**
#      （runBatch 的 DONE 分支）而不是 onFinished 钩子：钩子只有服务端注册，
#      挂在钩子上会让 CLI 全量扫描（新增目录最多的那一次）永远不收尾。
#      收尾失败**只记日志**，绝不把 DONE 改成 FAILED（照片已经扫进去了）。
#      ⚠️ 收尾在**单写入者门闩内**执行，所以任务置 DONE 之后的这一小段里
#         `isRunning()` 仍是 True —— 此时再 `POST /api/scan/start` 会被
#         "已有扫描任务在跑" 挡下。这是**正确行为**（收尾确实在写 pb_photo），
#         不是 bug；调用方/界面要看 `GET /scan/status` 的 `running` 字段
#         （DONE + running=True = "还在整理地点"），别只看 jobStatus。
#
# 单写入者（开发计划 §3.3）
# ------------------------
#   同一时刻**只允许一个扫描任务在跑**（模块级 _ACTIVE_LOCK + _ACTIVE_JOB）。
#   两个扫描同时写 pb_photo，轻则进度互相覆盖，重则database is locked。
#   需要并行扫描请排队（runUntilDone 串行调多次），而不是开多线程。
#
# 为什么把「限流」放在调度层而不是 runner 里
# --------------------------------------
#   runner 只管「跑一批，处理了 N 张」，**不碰 jobStatus**；
#   「这批之后该置 PAUSED 还是 DONE」是任务状态机的领域。
#   分开的好处：runner 可以被单测直接反复调（不产生状态副作用），
#   调度层则把「状态流转 + 异常兜底 + 单写入者互斥」集中在一处。
#
# 崩溃恢复：认领僵尸 RUNNING
# ------------------------
#   进程被强杀时任务会停在 RUNNING。按 canTransit，RUNNING 只能去
#   PAUSED/DONE/FAILED，不能直接去 RUNNING。这里放行一条**受控**通道：
#   当本进程没有持有任何活动任务时，允许把上次遗留的 RUNNING 直接推进到
#   PAUSED/DONE/FAILED。否则一次 Ctrl+C 就得手工改库才能继续扫。

import os
import sys
import threading
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                    # noqa: E402
from common import miscCommon as misc                                  # noqa: E402
from common import paths as paths                                      # noqa: E402
from config import basicSettings as basicSettings                # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.scanner import runner as runner                      # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("scanScheduler", "scanscheduler.log")

_ERR_MAX_LEN = 512             # pb_scan_job.errMsg VARCHAR(512)

#: pb_scan_job.label 里标记「扫描完成后自动识别人脸」的记号。
#: label 是逗号分隔的**记号集合**（人脸识别任务用 REPLACE 占它，
#: 两者靠 jobType 区分，不会互相干扰）。
_AUTO_FACE_LABEL: str = "AUTO_FACE"


def isAutoFaceJob(job: dict) -> bool:
    """该任务是否要求「扫描完成后自动识别人脸」。

    落点是 pb_scan_job.label 而不是内存里的标志位 ——
    断点续扫（resume）可能发生在**另一个进程**里（CLI 续跑、
    服务重启后接着跑），标志位在内存里早就没了，而用户当初勾过的
    那个「扫完自动认脸」必须还在。

    模块级函数而非实例方法：调用方（api 层的后台线程）手里只有一行
    pb_scan_job，拿不到调度器实例，也不该为此去拿一把。
    """
    return _AUTO_FACE_LABEL in str((job or {}).get("label") or "").split(",")


# 模块级「当前活动任务」：单写入者的互斥门闩
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_STATE = {"jobCode": None}


# 门闩的**公开取用口**（扫描 / 人脸提取 / 人脸匹配三方共用）
# ----------------------------------------------------------
#   为什么要有这三个模块级函数，而不是让调用方直接读写 _ACTIVE_STATE：
#     · 「匹配」（api/match.py）**不是** pb_scan_job 里的任务 —— 它没有 jobCode、
#       没有批次、也不断点续跑；但它落库写的是同一批 pb_face /
#       pb_photo_person / pb_person_centroid，与人脸提取是同一个写入面。
#       所以它必须抢**同一把**闩。各造一把闩 = 两个写入者，而
#       `database is locked` 会以「偶发」的形式出现，是最难排查的一类 bug。
#     · 用函数包一层，调用方不必 import 私有变量名，也不必知道锁的类型
#       （将来若换成 RLock / 加超时，只有这一处要改）。
def acquireWriteLock(owner: str) -> bool:
    """抢占单写入者门闩。抢不到（别人在跑）返回 False。"""
    with _ACTIVE_LOCK:
        if _ACTIVE_STATE["jobCode"] is not None:
            return False
        _ACTIVE_STATE["jobCode"] = str(owner or "?")
        return True


def releaseWriteLock() -> None:
    """释放单写入者门闩。

    ⚠️ **必须在 finally 里调**：漏一次释放，整个服务后续的扫描 / 人脸提取 /
    匹配都会被「已有任务在跑」永久挡下，只能重启进程。
    """
    with _ACTIVE_LOCK:
        _ACTIVE_STATE["jobCode"] = None


def writeLockOwner():
    """当前持闩者（jobCode，或 "MATCH" 这类非任务记号）；None = 空闲。"""
    return _ACTIVE_STATE["jobCode"]


class ScanScheduler(object):
    """扫描任务调度器。一个进程一个实例即可（内部状态都挂在类级互斥上）。"""

    def __init__(self, dbFile: str = None, ownerID: str = None, verbose: bool = True,
                 onFinished=None):
        self.dbFile = dbFile
        self.ownerID = ownerID or basicSettings.DEFAULT_OWNER_ID
        self.verbose = bool(verbose)
        self.stopEvent = threading.Event()
        self.thread = None
        #: 本轮跑完（done=True）后的回调 onFinished(jobCode, summary, jobRow)。
        #: **只在后台线程里触发**（runUntilDone 的调用方就是后台线程），
        #: 钩子里可以安全地发起下一个任务 —— 单写入者门闩此时已释放。
        #: 唯一的用途：扫描 DONE 之后自动串起人脸识别（见 api/scan.py 的 _autoFaceHook）。
        self.onFinished = onFinished
        # 一轮扫描内复用的 ScanRunner（**只在本调度器的runUntilDone 期间有效**）
        self._runnerCache = {"key": None, "runner": None}
        if dbFile:
            sqliteCommon.dbHandle(dbFile)
        self.ensureSchema()

    def setOnFinished(self, callback) -> None:
        """换掉完成回调（api 层用它挂「扫描完 -> 启人脸识别」）。"""
        self.onFinished = callback

    def isAutoFaceJob(self, job: dict) -> bool:
        """实例转发到模块级 isAutoFaceJob（保留旧调用点的写法能跑）。"""
        return isAutoFaceJob(job)

    # ========================================================
    # 一、库与任务
    # ========================================================

    def ensureSchema(self) -> bool:
        """确认 8 张表在（扫描依赖 pb_photo + pb_scan_job）。

        刻意**不自动建表**：建表是tools/build_db.py 的职责，
        这里只给出可执行的提示，免得新机器上抛一句看不懂的 sqlite 错误。
        """
        sqliteCommon.dbHandle(self.dbFile)
        missing = [name for name in ("pb_photo", "pb_scan_job")
                   if not sqliteCommon.chkTableExist(name)]
        if missing:
            raise RuntimeError(
                "数据表缺失: %s；请先执行 python code\\src\\tools\\build_db.py --verify"
                % ", ".join(missing))
        return True

    def createJob(self, root: str = None, batchSize: int = None, jobCode: str = None,
                  countTotal: bool = True, autoFace: bool = True) -> dict:
        """建一个扫描任务（**jobCode 幂等**：已存在就原样返回，不新建）。

        参数
        ----
        root      : 扫描根，缺省 paths.photo_dir()（**只读**，绝不创建）
        batchSize : 每批处理张数，缺省 basicSettings.BATCH_SIZE（100）
        jobCode   : 指定则幂等；不指定则自动生成 SJ_<时间戳>_<随机>
        countTotal: True 时先廉价遍历一次把 totalCount 填上（只 stat，不读内容）
        autoFace  : True 时把「扫描跑完自动接着识别人脸」记进 label
                    （**缺省 True** —— 见下面「为什么默认是自动」）

        为什么 autoFace 缺省为 True
        -------------------------
          扫描的产物是 pb_photo 的行，而**待确认队列数的是 pb_face**。
          两者之间没有任何自动衔接：不识别人脸，扫一万张照片
          「待确认」也永远是 0，而界面上看不出任何异常 ——
          这正是本次要修的那个现象。所以把第二步默认接上，
          用户不该需要先知道"扫完还要手动跑一次人脸提取"。
          需要分开控制时（只想扫库存档、或批量重扫）传 autoFace=False。

        返回
        ----
        dict —— pb_scan_job 的一行
        """
        self.ensureSchema()
        if jobCode:
            exist = self.getJob(jobCode)
            if exist is not None:
                _LOG.info("createJob: jobCode 已存在，幂等返回: %s（状态 %s）"
                          % (jobCode, exist.get("jobStatus")))
                exist["created"] = False
                return exist

        scanRoot = os.path.abspath(str(root or paths.photo_dir()))
        if not os.path.isdir(scanRoot):
            raise FileNotFoundError("createJob: 扫描根不存在或不是目录: %s" % scanRoot)
        size = int(batchSize or basicSettings.BATCH_SIZE)
        if size <= 0:
            raise ValueError("createJob: batchSize 非法: %s" % size)

        newCode = jobCode or runner.makeJobCode()
        now = misc.getTime()
        dataSet = {
            "jobCode": newCode,
            "rootPath": scanRoot,
            "batchSize": size,
            "batchIndex": 0,
            "totalCount": 0,
            "jobStatus": comGD.JOB_IDLE,
            "jobType": comGD.JOB_TYPE_SCAN,
            "label": _AUTO_FACE_LABEL if autoFace else None,
            "regID": self.ownerID,
            "regYMDHMS": now,
        }
        if countTotal:
            from processor.scanner import walker as walker
            dataSet["totalCount"] = walker.countPhotoFiles(scanRoot)

        recID = sqliteCommon.insert_pb_scan_job("pb_scan_job", dataSet)
        if recID <= 0:
            raise RuntimeError("createJob: 写入 pb_scan_job 失败: %s"
                               % sqliteCommon.dbHandle().lastErrMsg)
        _LOG.info("createJob: %s root=%s batchSize=%d totalCount=%d"
                  % (newCode, scanRoot, size, dataSet["totalCount"]))
        job = self.getJob(newCode)
        job["created"] = True
        return job

    def getJob(self, jobCode: str) -> dict:
        """取任务行（含软删，幂等恢复用）；不存在返回 None"""
        rows = sqliteCommon.query_pb_scan_job("pb_scan_job", jobCode=str(jobCode),
                                            delFlag="*")
        return rows[0] if rows else None

    def listJobs(self, limit: int = 20) -> list:
        """最近的**扫描**任务列表（新的在前）

        ⚠️ 固定带 jobType=0：pb_scan_job 现在也存人脸识别任务，
        而两者的 pendingCount 口径不同（扫描=疑似移动张数 / 人脸=新入库人脸条数），
        混列出来的数字没法解释。人脸识别任务见 faceScheduler.listJobs。
        """
        rows = sqliteCommon.query_pb_scan_job("pb_scan_job", delFlag="*",
                                            jobType=str(comGD.JOB_TYPE_SCAN),
                                            orderBy="recID", descFlag=True,
                                            limitNum=int(limit))
        return rows

    # ========================================================
    # 二、状态流转
    # ========================================================

    def _canTransit(self, current: str, target: str) -> bool:
        """流转合法性 = globalDefinition.canTransit + 一条「认领僵尸 RUNNING」的受控通道"""
        if current == target:
            return True
        if comGD.canTransit(current, target):
            return True
        if (current == comGD.JOB_RUNNING
                and target in (comGD.JOB_PAUSED, comGD.JOB_DONE, comGD.JOB_FAILED)
                and _ACTIVE_STATE["jobCode"] is None):
            return True
        return False

    def setStatus(self, jobCode: str, newStatus: str, errMsg: str = None,
                  patch: dict = None) -> dict:
        """按状态机置 jobStatus（非法流转直接拒绝并记 error，不硬改）。

        patch 里可带 finishedYMDHMS / batchIndex / startedYMDHMS 等顺带更新。
        """
        job = self.getJob(jobCode)
        if job is None:
            raise ValueError("setStatus: 任务不存在: %s" % jobCode)
        current = str(job.get("jobStatus") or comGD.JOB_IDLE)
        if not self._canTransit(current, newStatus):
            _LOG.error("setStatus: 非法流转 %s: %s -> %s（拒绝）"
                       % (jobCode, current, newStatus))
            return {"ok": False, "code": comGD.ERR_TASK_STATE_ILLEGAL,
                    "from": current, "to": newStatus,
                    "errMsg": "%s -> %s 非法（%s）"
                              % (comGD.JOB_STATUS_TEXT.get(current, current),
                                 comGD.JOB_STATUS_TEXT.get(newStatus, newStatus),
                                 comGD.errText(comGD.ERR_TASK_STATE_ILLEGAL))}
        dataSet = {"jobStatus": newStatus}
        if patch:
            dataSet.update(patch)
        if errMsg:
            dataSet["errMsg"] = str(errMsg)[:_ERR_MAX_LEN]
        rtn = sqliteCommon.update_pb_scan_job("pb_scan_job", job["recID"], dataSet)
        _LOG.info("setStatus: %s %s -> %s%s"
                  % (jobCode, current, newStatus,
                     ("err=%s" % dataSet["errMsg"]) if errMsg else ""))
        return {"ok": rtn >= 0, "from": current, "to": newStatus, "code": comGD.RET_OK}

    # ========================================================
    # 三、执行
    # ========================================================

    def _acquire(self, jobCode: str) -> bool:
        """抢占「单写入者」门闩。抢不到说明别的任务在跑，直接 False。"""
        with _ACTIVE_LOCK:
            if _ACTIVE_STATE["jobCode"] is not None:
                return False
            _ACTIVE_STATE["jobCode"] = str(jobCode)
            return True

    def _release(self) -> None:
        with _ACTIVE_LOCK:
            _ACTIVE_STATE["jobCode"] = None

    def activeJobCode(self):
        """当前正在跑的任务编码；None = 空闲"""
        return _ACTIVE_STATE["jobCode"]

    def isRunning(self) -> bool:
        return _ACTIVE_STATE["jobCode"] is not None

    def _runnerFor(self, jobCode: str, root: str, batchSize: int, forceNew: bool = False):
        """取本轮可复用的 ScanRunner。

        复用意义（10 万行实测）：索引加载 2.3s/次，batchSize=100 时有 1000 批 ——
        每批新建runner 就等于每批重建一次全表索引 = 约 38 分钟纯开销（平方级劣化）。
        key 里带了 jobCode/root/batchSize，任一变化就换新实例（索引自然重建）。
        """
        key = (str(jobCode), str(root), int(batchSize))
        if not forceNew and self._runnerCache["key"] == key \
                and self._runnerCache["runner"] is not None:
            return self._runnerCache["runner"]
        scanRunner = runner.ScanRunner(jobCode=jobCode, root=root,
                                       batchSize=batchSize, dbFile=self.dbFile,
                                       ownerID=self.ownerID, stopEvent=self.stopEvent)
        self._runnerCache = {"key": key, "runner": scanRunner}
        return scanRunner

    def runBatch(self, jobCode: str, root: str = None, batchSize: int = None) -> dict:
        """跑**一批**。返回 {"ok", "done", "paused", "result", "errMsg", "code"}。

        正常路径：RUNNING -> (PAUSED | DONE)
        异常路径：任何 Exception -> errMsg 落库 + FAILED（并把异常文本带回给调用方）
        """
        jobCode = str(jobCode)
        if not self._acquire(jobCode):
            other = _ACTIVE_STATE["jobCode"]
            return {"ok": False, "code": comGD.ERR_TASK_STATE_ILLEGAL, "done": False,
                    "paused": False, "result": None,
                    "errMsg": "已有扫描任务在跑: %s（单写入者不允许并发）" % other}
        try:
            job = self.getJob(jobCode)
            if job is None:
                raise ValueError("任务不存在: %s" % jobCode)
            current = str(job.get("jobStatus") or comGD.JOB_IDLE)
            if current == comGD.JOB_DONE:
                return {"ok": True, "code": comGD.RET_OK, "done": True, "paused": False,
                        "result": None, "errMsg": ""}
            if current not in (comGD.JOB_IDLE, comGD.JOB_RUNNING, comGD.JOB_PAUSED,
                               comGD.JOB_FAILED):
                raise ValueError("任务状态异常，无法开始: %s" % current)

            startedAt = job.get("startedYMDHMS") or misc.getTime()
            transit = self.setStatus(jobCode, comGD.JOB_RUNNING,
                                     patch={"startedYMDHMS": startedAt,
                                            "errMsg": ""})
            if not transit.get("ok"):
                raise ValueError(transit.get("errMsg") or "置RUNNING 失败")

            scanRunner = self._runnerFor(jobCode, root or job.get("rootPath"),
                                        batchSize or job.get("batchSize"))
            result = scanRunner.runBatch()

            batchIndex = int(job.get("batchIndex") or 0) + 1
            if result.get("done"):
                self.setStatus(jobCode, comGD.JOB_DONE,
                               patch={"batchIndex": batchIndex,
                                      "finishedYMDHMS": misc.getTime()})
                # ---- 地点侧收尾（R4b）：填 placeNameDir → 重建 pb_place ----
                # ⚠️ 为什么在**这里**而不是 onFinished 钩子：
                #   `onFinished` 只有服务端（api/scan.py）注册，`tools/scan_cli.py`
                #   建调度器时没传 —— 挂在钩子上会让 **CLI 全量扫描**（首次建库、
                #   新增目录最多的那一次）永远不收尾。挂在状态机这一处，
                #   Web 后台 / CLI --all / CLI 交互逐批 全部自动生效。
                # ⚠️ 为什么在 `_release()`（finally）**之前**调用：
                #   收尾要写 `pb_photo.placeNameDir`，必须仍持有单写入者门闩。
                # ⚠️ 为什么放在置 DONE **之后**：任务状态与"照片已入库"这件事已经
                #   成立（收尾只是派生缓存）；收尾失败绝不能把 DONE 改成 FAILED
                #   —— 那会让用户以为照片没扫进去，而照片明明都在。
                result["placeFinalize"] = self._finalizePlaces(jobCode)
            else:
                self.setStatus(jobCode, comGD.JOB_PAUSED,
                               patch={"batchIndex": batchIndex})
            return {"ok": True, "code": comGD.RET_OK,
                    "done": bool(result.get("done")),
                    "paused": bool(result.get("paused")),
                    "result": result, "errMsg": ""}
        except Exception as e:
            errText = "%s: %s" % (type(e).__name__, e)
            _LOG.error("runBatch %s 失败: %s" % (jobCode, errText))
            try:
                self.setStatus(jobCode, comGD.JOB_FAILED, errMsg=errText,
                               patch={"finishedYMDHMS": misc.getTime()})
            except Exception as e2:
                _LOG.error("runBatch: 连置 FAILED 都失败了: %s" % e2)
            return {"ok": False, "code": comGD.ERR_UNKNOWN, "done": False,
                    "paused": False, "result": None, "errMsg": errText}
        finally:
            self._release()

    def _finalizePlaces(self, jobCode: str) -> dict:
        """扫描跑完后的地点侧收尾（R4b）：`processor/place/placeFinalize.finalizePlaces`。

        ⚠️ 三种"不触发"是**正确行为**，不是遗漏：stop / `maxBatches` 用完 /
        批次失败都走不到 DONE 分支，所以那些情况下不会收尾 —— 半截库不许
        当成完整库去建地点字典（与 onFinished 的 autoFace 同一口径）。

        ⚠️ 这里**再包一层 try/except**（`finalizePlaces` 自己已经不抛）：
        它是挂在"任务已置 DONE"之后的写库动作，一旦漏出异常会被 `runBatch`
        的 except 捕获 → 任务被改成 **FAILED**，而照片其实已经扫进去了。
        那是最坏的一种错误归因，所以这里宁可写两次 try。
        """
        if not basicSettings.PLACE_FINALIZE_AFTER_SCAN:
            _LOG.info("finalizePlaces: 开关 PLACE_FINALIZE_AFTER_SCAN 关闭，跳过（%s）",
                      jobCode)
            return {"ok": True, "skipped": True,
                    "reason": "PLACE_FINALIZE_AFTER_SCAN = False"}
        try:
            from processor.place import placeFinalize as placeFinalize
            report = placeFinalize.finalizePlaces(
                dbFile=self.dbFile, source="scan:%s" % jobCode)
        except Exception as e:                                    # noqa: BLE001
            errText = "%s: %s" % (type(e).__name__, e)
            _LOG.error("finalizePlaces: 扫描 %s 收尾失败（任务仍保持 DONE）: %s",
                       jobCode, errText)
            return {"ok": False, "errMsg": errText}
        # ⚠️ 只回**摘要**：`report` 里有 52 行目录清单与样例，塞进批次返回值会让
        #    `/api/scan/status` 这类响应急剧变胖，而它们要的是"收尾成没成"。
        drift = list(report.get("drift") or [])
        suspects = list(report.get("suspects") or [])
        scan = report.get("scan") or {}
        rebuild = report.get("rebuild") or {}
        nameZh = report.get("nameZh") or {}
        brief = {
            "ok": bool(report.get("ok")),
            "elapsed": report.get("elapsed"),
            "filled": scan.get("written"),            # 本次新填的 placeNameDir 行数
            "refreshed": scan.get("refreshed"),
            "placeCount": rebuild.get("placeCount"),
            "zeroed": rebuild.get("zeroed"),
            "orphanCount": rebuild.get("orphanCount"),
            "nameZhFilled": nameZh.get("filled"),
            "driftCount": len(drift),
            "driftTop": ["%s -> %s" % (one.get("old"), one.get("now"))
                         for one in drift[:5]],
            "suspectCount": len(suspects),
            "suspectTop": [one.get("dir") for one in suspects[:5]],
            "errors": list(report.get("errors") or []),
        }
        if drift:
            # 改名不会静默：这里主动喊一声（自动收尾**不覆盖**漂移行）
            _LOG.warning("finalizePlaces: %s 发现 %d 行 placeNameDir 漂移（目录改名）"
                         "—— 自动收尾只填空不覆盖，请跑 "
                         "`place_cli.py --scan-dir --apply --refresh` + `--rebuild-only`: %s",
                         jobCode, len(drift), brief["driftTop"])
        if suspects:
            _LOG.warning("finalizePlaces: %s 有 %d 个目录疑似地点但被排除"
                         "（新命名风格？）—— 确认后加 DIR_PLACE_ALIAS 或黑名单: %s",
                         jobCode, len(suspects), brief["suspectTop"])
        return brief

    def runUntilDone(self, jobCode: str, maxBatches: int = None,
                     root: str = None, batchSize: int = None,
                     onBatch=None) -> dict:
        """连续跑批直到 DONE（或 maxBatches 用完 / 收到停止信号）。

        onBatch : 每批结束后的回调 onBatch(batchIndex, batchResult, jobRow)
        """
        jobCode = str(jobCode)
        self.stopEvent.clear()
        summary = {"jobCode": jobCode, "batches": 0, "done": False,
                   "counts": {"added": 0, "skipped": 0, "updated": 0,
                              "duplicate": 0, "moved": 0, "moveLinked": 0,
                              "copied": 0, "missing": 0, "recovered": 0},
                   "errMsg": ""}
        started = time.time()
        try:
            while True:
                if self.stopEvent.is_set():
                    summary["errMsg"] = "收到停止信号，已在批边界停下"
                    break
                if maxBatches is not None and summary["batches"] >= int(maxBatches):
                    summary["errMsg"] = "达到 maxBatches=%d，任务保持 PAUSED" % int(maxBatches)
                    break
                batchResult = self.runBatch(jobCode, root=root, batchSize=batchSize)
                if not batchResult.get("ok"):
                    summary["errMsg"] = batchResult.get("errMsg") or "批次失败"
                    break
                summary["batches"] += 1
                result = batchResult.get("result") or {}
                for key in summary["counts"]:
                    summary["counts"][key] += int((result.get("counts") or {}).get(key, 0))
                if result.get("placeFinalize"):
                    # 地点侧收尾的结果（R4b）：只在"真的跑完"的那一批里有，
                    # 带到 summary 上，CLI/接口才看得见"收尾成没成、填了几行"。
                    summary["placeFinalize"] = result["placeFinalize"]
                if onBatch is not None:
                    try:
                        onBatch(summary["batches"], result, self.getJob(jobCode))
                    except Exception as e:              # 回调异常不许影响扫描本身
                        _LOG.warning("runUntilDone: onBatch 回调异常（已忽略）: %s" % e)
                if batchResult.get("done"):
                    summary["done"] = True
                    break
                if result.get("stopped"):
                    summary["errMsg"] = "收到停止信号，已在批边界停下"
                    break
        finally:
            # 索引缓存**只在本轮内有效**：下一轮重新建（期间库可能被别的进程改过）
            self._runnerCache = {"key": None, "runner": None}
        summary["elapsed"] = round(time.time() - started, 3)
        jobRow = self.getJob(jobCode) or {}
        summary["jobStatus"] = jobRow.get("jobStatus")
        _LOG.info("runUntilDone %s: %d 批, done=%s, 计数=%s, 用时 %.2fs"
                  % (jobCode, summary["batches"], summary["done"], summary["counts"],
                     summary["elapsed"]))
        # ---- 完成钩子：只在**真的跑完**时触发 ----
        # maxBatches 用完、收到停止信号、批次失败都**不**触发：
        #   前者任务还开着（PAUSED），此时去启人脸识别等于把没扫完的半截
        #   库当成完整的去提取；后者更不能——失败的任务没有"扫完"这回事。
        # 钩子里可以发起下一任务：此时单写入者门闩已在 runBatch 的 finally 里释放。
        if summary["done"] and self.onFinished is not None:
            try:
                self.onFinished(jobCode, summary, jobRow)
            except Exception as e:      # 钩子异常绝不许影响扫描本身
                _LOG.error("runUntilDone %s: onFinished 回调异常（已忽略）: %s: %s",
                           jobCode, type(e).__name__, e)
        return summary

    def startBackground(self, jobCode: str, maxBatches: int = None) -> dict:
        """后台线程里跑到 DONE（供 API「开始扫描」用，步骤 9 接 FastAPI）。

        立刻返回，扫描在后台推进；用 jobStatus()/progress() 查进度，用 stop() 叫停。
        """
        if self.thread is not None and self.thread.is_alive():
            return {"ok": False, "code": comGD.ERR_TASK_STATE_ILLEGAL,
                    "errMsg": "已有后台扫描线程在跑"}
        self.stopEvent.clear()

        def _target():
            try:
                self.runUntilDone(jobCode, maxBatches=maxBatches)
            except Exception as e:                  # 线程里绝不能让异常逃出去
                _LOG.error("startBackground %s 线程异常: %s: %s"
                           % (jobCode, type(e).__name__, e))

        self.thread = threading.Thread(target=_target, name="scan-%s" % jobCode,
                                       daemon=True)
        self.thread.start()
        _LOG.info("startBackground: %s 已在后台启动" % jobCode)
        return {"ok": True, "code": comGD.RET_OK, "jobCode": jobCode, "errMsg": ""}

    def stop(self) -> bool:
        """请求停止：在**批边界**生效（不打断正在提交的事务）"""
        self.stopEvent.set()
        return True

    def waitBackground(self, timeout: float = None) -> bool:
        """等后台线程结束（测试/CLI 用）"""
        if self.thread is None:
            return True
        self.thread.join(timeout)
        return not self.thread.is_alive()

    # ========================================================
    # 四、进度查询
    # ========================================================

    def progress(self, jobCode: str) -> dict:
        """任务的进度快照（给 UI 轮询用）"""
        job = self.getJob(jobCode)
        if job is None:
            return {"ok": False, "code": comGD.ERR_TASK_NOT_FOUND, "errMsg": "任务不存在"}
        total = int(job.get("totalCount") or 0)
        processed = int(job.get("processedCount") or 0)
        percent = 0.0
        if total > 0:
            percent = min(100.0, round(processed * 100.0 / total, 2))
        status = str(job.get("jobStatus") or comGD.JOB_IDLE)
        return {
            "ok": True, "code": comGD.RET_OK, "errMsg": "",
            "jobCode": job.get("jobCode"), "rootPath": job.get("rootPath"),
            "jobStatus": status, "jobStatusText": comGD.JOB_STATUS_TEXT.get(status, status),
            "batchSize": job.get("batchSize"), "batchIndex": job.get("batchIndex"),
            "totalCount": total, "processedCount": processed,
            "addedCount": job.get("addedCount"), "skippedCount": job.get("skippedCount"),
            "duplicateCount": job.get("duplicateCount"),
            "pendingCount": job.get("pendingCount"),
            "lastCursor": job.get("lastCursor"), "percent": percent,
            "startedYMDHMS": job.get("startedYMDHMS"),
            "finishedYMDHMS": job.get("finishedYMDHMS"),
            "errMsg": job.get("errMsg") or "",
            "running": self.isRunning(),
        }

    def describe(self, jobCode: str) -> dict:
        """把一行 pb_scan_job 渲染成人能看的进度文本（CLI/日志共用）"""
        info = self.progress(jobCode)
        if not info.get("ok"):
            return info
        text = ("[%s] %s | %d/%d (%s%%) | 新增 %s 跳过 %s 重复 %s 待确认 %s | 批次 %s/%s | 游标 %s"
                % (info["jobStatusText"], info["jobCode"],
                   info["processedCount"], info["totalCount"], info["percent"],
                   info["addedCount"], info["skippedCount"], info["duplicateCount"],
                   info["pendingCount"], info["batchIndex"], info["batchSize"],
                   info["lastCursor"] or "(无)"))
        if info["errMsg"]:
            text += " | err=%s" % info["errMsg"]
        info["text"] = text
        return info


if __name__ == "__main__":
    print("scanScheduler _VERSION:", _VERSION)
    print("状态机:", {k: comGD.JOB_STATUS_TEXT.get(k, k)
                     for k in comGD.JOB_STATUS_ALL})
    try:
        sched = ScanScheduler()
    except Exception as e:
        print("[未就绪] %s" % e)
        raise SystemExit(0)
    for job in sched.listJobs(5):
        print(sched.describe(job["jobCode"])["text"])
