#! /usr/bin/env python3
#encoding: utf-8

#Filename: miscCommon.py
#Description: photo-browser 通用杂项函数：日志 / 时间 / 字符串 / 字节
#
# 日志风格复用 contentHub 的 common/miscCommon.setLogNew（控制台 + 文件双通道），
# 日志目录锚定在仓库内 <root>/log，不随进程 cwd 漂移。
#
# 纪律（开发计划 §6.4）：
#   - 不打印原图路径全集、不打印 embedding 内容；
#   - 单张耗时只在 debug 级输出（避免 3 万张刷屏）。

import datetime
import hashlib
import json
import logging
import os
import sys
import time
import unicodedata

_VERSION = "20261004"


# 项目根目录: .../<root>/src/common/miscCommon.py -> .../<root>
# （即 photo-browser/code），日志目录固定落在 <root>/log，不受 cwd 影响
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEF_LOG_DIR = os.path.join(_ROOT_DIR, "log")


# ============================================================
# 一、日志
# ============================================================

def setLogNew(title, filebasename, homeDir=None, stdout=False):
    """取/建一个 logger，同时挂文件handler（可选挂控制台）。

    参数
    ----
    title         : logger 名（同名重复调用不会重复挂 handler）
    filebasename  : 日志文件名，如 "photolib.log"
    homeDir       : 日志目录，缺省为 <repo>/code/log
    stdout        : True 时额外输出到控制台

    设计沿用 contentHub：建目录/开文件失败**不中断调用方**，退化为 stderr 提示。
    """
    if not homeDir:
        homeDir = _DEF_LOG_DIR
    if not os.path.exists(homeDir):
        try:
            os.makedirs(homeDir, exist_ok=True)
        except OSError as e:
            sys.stderr.write("setLogNew: create log dir failed:%s,%s\n" % (homeDir, e))
    logfile = os.path.join(homeDir, filebasename)
    logger = logging.getLogger(title)
    if not logger.handlers:
        file_handler = None
        try:
            file_handler = logging.FileHandler(logfile, encoding="utf-8")
        except OSError as e:
            sys.stderr.write("setLogNew: open log file failed:%s,%s\n" % (logfile, e))
        if file_handler is not None:
            formatter = logging.Formatter("%(name)-12s %(asctime)s %(levelname)-8s %(message)s")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)
    logger.logFileName = logfile
    if stdout:
        # 去重：stdout=True 被调多次时不要叠一堆StreamHandler
        if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
            logger.addHandler(stream_handler)
    return logger


def getDefaultLogger(name="photolib", stdout=True):
    """全项目通用 logger（落<code>/log/photolib.log）"""
    return setLogNew(name, "photolib.log", stdout=stdout)


# ============================================================
# 二、时间
# ============================================================

def getTime() -> str:
    """当前本地时间 YYYYMMDDHHMMSS —— 对齐库中 regYMDHMS / modifyYMDHMS 字段"""
    return time.strftime("%Y%m%d%H%M%S", time.localtime())


def getDate() -> str:
    """当前本地日期 YYYYMMDD"""
    return time.strftime("%Y%m%d", time.localtime())


def getISO8601UTC() -> str:
    """当前 UTC 时间 ISO8601（秒级，Z 结尾）—— 对齐 pb_photo.takenAt"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ymdHMS2human(s: str) -> str:
    """20261004153045 -> 2026-10-04 15:30:45（非法输入原样返回）"""
    s = "" if s is None else str(s).strip()
    if len(s) != 14 or not s.isdigit():
        return s
    return "%s-%s-%s %s:%s:%s" % (s[0:4], s[4:6], s[6:8], s[8:10], s[10:12], s[12:14])


def human2ymdHMS(s: str) -> str:
    """2026-10-04 15:30:45 / 2026-10-04T15:30:45 -> 20261004153045"""
    s = "" if s is None else str(s).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[:14].ljust(14, "0") if digits else ""


def getTimeStamp() -> str:
    """毫秒级时间戳 YYYYMMDDHHMMSS.mmm（日志/耗时统计用）"""
    ct = time.time()
    head = time.strftime("%Y%m%d%H%M%S", time.localtime(ct))
    return "%s.%03d" % (head, (ct - int(ct)) * 1000)


# ============================================================
# 三、字符串
# ============================================================

def isNull(v) -> bool:
    """None / 空串 / 纯空白 → True"""
    return v is None or (isinstance(v, str) and v.strip() == "")


def isEmpty(v) -> bool:
    """None / 空串 / 空容器 → True"""
    if v is None:
        return True
    if isinstance(v, (str, bytes, list, tuple, dict, set)):
        return len(v) == 0
    return False


def trim(s) -> str:
    """去首尾空白；None 安全"""
    return "" if s is None else str(s).strip()


def str2nfc(s) -> str:
    """Unicode NFC 规范化（Windows 上NTFS 存NFD，macOS/Linux 存 NFC；
    同一文件名在不同系统上字节不同，算 hash 前必须先统一）"""
    return "" if s is None else unicodedata.normalize("NFC", str(s))


def subStr(s: str, start: int, length: int = -1) -> str:
    """安全子串：越界不抛异常"""
    s = "" if s is None else str(s)
    if start < 0:
        start = max(0, len(s) + start)
    if start >= len(s):
        return ""
    if length is None or length < 0:
        return s[start:]
    return s[start:start + length]


def strReplace(s: str, old: str, new: str, count: int = -1) -> str:
    return ("" if s is None else str(s)).replace(old, new, count)


def splitPath(s: str) -> str:
    """把任意分隔符统一成正斜杠（不做 NFC、不去 ./ ——那是 paths.normalize_relpath 的事）"""
    return ("" if s is None else str(s)).replace("\\", "/")


def humanSize(n) -> str:
    """字节数 -> 人类可读（1.2 MB）"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return ("%d%s" if unit == "B" else "%.1f%s") % (n, unit)
        n /= 1024.0
    return "%.1fPB" % n


def jsonDumps(data, ensure_ascii=False, indent=0) -> str:
    if indent > 0:
        return json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
    return json.dumps(data, separators=(",", ":"), ensure_ascii=ensure_ascii)


def jsonLoads(data):
    return json.loads(data)


# ============================================================
# 四、字节 / hash
# ============================================================

def toBytes(v, encoding="utf-8") -> bytes:
    """str/bytes/bytearray/memoryview -> bytes"""
    if isinstance(v, bytes):
        return v
    if isinstance(v, (bytearray, memoryview)):
        return bytes(v)
    return str(v).encode(encoding)


def toStr(v, encoding="utf-8") -> str:
    """bytes/bytearray -> str（已解码则原样返回）"""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode(encoding, errors="replace")
    return "" if v is None else str(v)


def bytes2hex(b) -> str:
    """bytes -> 小写十六进制字符串（不带 0x）"""
    return toBytes(b).hex()


def hex2bytes(s: str) -> bytes:
    """十六进制字符串 -> bytes（非法字符抛 ValueError）"""
    s = trim(s).lower()
    if s.startswith("0x"):
        s = s[2:]
    return bytes.fromhex(s)


def sha256Hex(data) -> str:
    """任意 bytes/str -> sha256 十六进制（64 位小写）"""
    h = hashlib.sha256()
    h.update(toBytes(data))
    return h.hexdigest()


def sha1Hex(data) -> str:
    h = hashlib.sha1()
    h.update(toBytes(data))
    return h.hexdigest()


def fileHashHex(filePath: str, chunkSize: int = 8 * 1024 * 1024, algorithm: str = "sha256") -> str:
    """分块流式计算文件 hash —— **绝不整读原图**（单张 RAW 可达 60MB+）。

    chunkSize 由 config/basicSettings.HASH_CHUNK_SIZE 提供（8MB）。
    本函数只读文件，不写、不改、不动mtime。
    """
    h = hashlib.new(algorithm)
    with open(filePath, "rb") as f:
        while True:
            block = f.read(chunkSize)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


if __name__ == "__main__":
    print("miscCommon _VERSION :", _VERSION)
    print("log dir            :", _DEF_LOG_DIR)
    print("getTime()          :", getTime())
    print("getISO8601UTC()    :", getISO8601UTC())
    print("ymdHMS2human()     :", ymdHMS2human(getTime()))
    print("human2ymdHMS()     :", human2ymdHMS("2026-10-04 15:30:45"))
    print("humanSize(1234567) :", humanSize(1234567))
    print("sha256Hex('abc')   :", sha256Hex("abc"))
    lg = getDefaultLogger(stdout=True)
    lg.info("miscCommon self-test ok")
