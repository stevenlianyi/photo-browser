#! /usr/bin/env python3
#encoding: utf-8

#Filename: fetch_zh_geo.py
#Description: 地点中文名的**离线数据源**构建脚本（R4a · DR-28/DR-29）——
#             抓取全国省/市/县区级行政边界，合并成一个压缩文件落库到仓库
#
# 为什么需要这个脚本（依赖被换掉的原因，别改回去）
# ---------------------------------------------------
#   原始设计用的是 PyPI 的 `amap-geo`。**实测该包在 PyPI 上不存在**
#   （`pip index versions amap-geo` -> No matching distribution；
#     pypi.org/pypi/amap-geo/json -> 404；`amap_geo` / `geo-amap` / `amapgeo`
#     等候选名同样 404）。可替代的离线包里也没有一个带**区县多边形**：
#     `china-division`（31KB）与 `cpca` 都只有「6 位编码 + 中文名」，
#     没有坐标，做不了 point-in-polygon。
#   所以改成「**数据文件 + 可选包**」双来源（见 placeNameZh.py）：
#     ① `PLACE_AMAP_GEO_FILE` 指向的本地数据文件（**本脚本产出，入库**）
#     ② PyPI 的 `amap_geo` 包（仍然写进 requirements 的可选段，谁装了谁先用）
#     ③ 都没有 -> nameZh 全 NULL + 只记一次 warning（降级，不报错）
#   ⚠️ 联网只发生在这个**一次性**脚本里。生产代码（placeNameZh）**永不联网**，
#     这一点是 DR-29「不引入联网逆地理 API」的全部意义，别把它改成在线查。
#
# 数据源
# ------
#   阿里云 DataV.GeoAtlas（`geo.datav.aliyun.com/areas_v3/bound/`），公开、免费、
#   无需 key。它是**唯一**能免 key 拿到全国区县边界多边形的公开源。
#   ⚠️ 它按层级分文件：`_full.json` 只含**直接子级**（省的文件给市、
#     直辖市的文件直接给区），所以必须**逐层递归**：
#       100000_full.json            -> 34 个省级
#       <省 adcode>_full.json       -> 地级市（直辖市是区县）
#       <市 adcode>_full.json       -> 区县   <-- 本脚本要的层级
#     共约 375 次请求，只在 --apply 时跑一次。
#
# 只取 `level == 'district'`（区县级）。为什么不要市级
# ----------------------------------------------
#   DR-29 定死「精度 = 区县级」。市级多边形在直辖市/省辖市会造成
#   「同市不同区」显示成同一个名字，而家庭照片里「住的地方」恰恰是区。
#   省/市级多边形**仍然保留**（体积很小、约 0.6MB）——它们是
#   「点落在境内但没落进任何区县」时的兜底，让 placeNameZh 能报出
#   `insideProvince` 这个明确 reason，而不是笼统的「没命中」。
#
# 坐标精度：5 位小数（≈ 1.1m）
# --------------------------------
#   原样保留是 15 位小数（亚毫米），而我们只用来做「照片中心点落在哪个区县」。
#   区县级判定用 1.1m 精度绰绰有余（人站在区县界上算哪边都不影响正确性），
#   而坐标位数直接决定文件体积：5 位小数比原样小一截，gzip 后更明显。
#   ⚠️ 取整可能让极小的环自相交。真发生了由 placeNameZh 侧记 `badGeometry`
#     计数并跳过该多边形，**不在这里 try/except 吞掉**。
#
# 用法
# ----
#   python code\src\tools\fetch_zh_geo.py --dry-run
#   python code\src\tools\fetch_zh_geo.py --apply
#   python code\src\tools\fetch_zh_geo.py --dry-run --limit-province 2
#
# 硬约束：只写 `--out` 指向的那一个数据文件；**绝不碰 photo 目录**。

import argparse
import gzip
import io
import json
import os
import sys
import time
import urllib.request

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
_REPO_DIR = os.path.dirname(_SRC_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                              # noqa: E402

_VERSION = "20261007"

_LOG = misc.setLogNew("fetchZhGeo", "fetchzhgeo.log")

#: DataV.GeoAtlas 的边界文件目录（_full = 含直接子级）
DATA_URL_BASE: str = "https://geo.datav.aliyun.com/areas_v3/bound"
#: 全国根节点 adcode
ROOT_ADCODE: int = 100000
#: 保留到第几层（1=省 2=市 3=区）。**3 = 区县就是 DR-29 要的精度**
MAX_LEVEL: int = 3
#: 坐标取整位数（5 位小数 ≈ 1.1m，见文件头）
COORD_DIGITS: int = 5
#: 缺省输出（入库）。放在 code/src/data 下，**不是** pb_*.txt 那套数据源
DEFAULT_OUT: str = os.path.join(_SRC_DIR, "data", "china_district.json.gz")
#: 省级 adcode -> 中文名 的**权威来源是数据文件自身**（从 100000_full.json 读），
#:   本常量只用来断言「抓到的省级数必须是 34」，防止某次源站变更被静默接受。
EXPECT_PROVINCE_NUM: int = 34
#: 单次请求超时（秒）与重试次数
HTTP_TIMEOUT: int = 120
HTTP_RETRY: int = 3
#: DataV 没有 User-Agent 会 403
_USER_AGENT: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                    " photo-browser/fetch_zh_geo")


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/路径会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _roundCoords(coords, digits: int = COORD_DIGITS):
    """递归把 [lon, lat] 取整到指定位数。输入结构原样保留（GeoJSON 嵌套数组）。"""
    if not coords:
        return coords
    head = coords[0]
    if isinstance(head, (int, float)):
        return [round(float(coords[0]), digits), round(float(coords[1]), digits)]
    return [_roundCoords(one, digits) for one in coords]


def fetchGeoJson(adcode: int) -> dict:
    """取一个 adcode 的 `_full.json`。失败抛异常（由调用方决定是跳过还是中止）。"""
    url = "%s/%d_full.json" % (DATA_URL_BASE, int(adcode))
    lastError = None
    for attempt in range(1, HTTP_RETRY + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as e:                      # noqa: BLE001 —— 逐类重试
            lastError = e
            if attempt < HTTP_RETRY:
                time.sleep(1.5 * attempt)
    raise RuntimeError("拉取 %s 失败（重试 %d 次）: %s: %s"
                       % (url, HTTP_RETRY, type(lastError).__name__, lastError))


def _featureOf(data: dict):
    return (data or {}).get("features") or []


def _adcodeOf(feature: dict) -> int:
    """要素的 adcode（取整）。**非纯数字一律返回 0**。

    ⚠️ 为什么必须防这一手：DataV 把香港拆成 `100000_JD`（九龙）/
      `100000_JR`（港岛）/`100000_MO`（澳门）这类**带后缀的伪 adcode**，
      直接 `int("100000_JD")` 会 ValueError 把整轮抓取炸掉 ——
      而那正是「已经抓完 34 个省、眼看要成功」的最后一秒。
      取不到就返回 0，由调用方按「跳过并计数」处理。
    """
    try:
        return int((feature.get("properties") or {}).get("adcode"))
    except (TypeError, ValueError):
        return 0


def build(districtOnly: bool = True, limitProvince: int = 0) -> dict:
    """递归抓取并组装数据包（**不写任何文件**）。"""
    startTime = time.time()
    root = fetchGeoJson(ROOT_ADCODE)
    provinces, skipped, empty = [], [], []
    for province in _featureOf(root):
        pAd = _adcodeOf(province)
        pName = str((province.get("properties") or {}).get("name") or "")
        if pAd == ROOT_ADCODE or pAd == 0 or not pName:
            continue                              # 南海诸岛那一条占位，不是省
        if limitProvince and len(provinces) >= limitProvince:
            break
        entry = {"adcode": pAd, "name": pName,
                 "polys": _roundCoords((province.get("geometry") or {})
                                       .get("coordinates") or []),
                 "districts": []}
        # 一级子级：省 -> 市（直辖市/特别行政区 -> 区县）
        provinceChildren = []
        try:
            provinceChildren = _featureOf(fetchGeoJson(pAd))
        except RuntimeError as e:
            _LOG.warning("fetch_zh_geo: 省 %s(%d) 子级拉取失败：%s" % (pName, pAd, e))
        for child in provinceChildren:
            cAd = _adcodeOf(child)
            cLevel = str((child.get("properties") or {}).get("level") or "")
            if cAd == 0:
                # 港澳的「九龙/港岛/澳门」等伪 adcode：它们不是可下钻的层
                skipped.append("%s/%s" % (pName, cLevel or "?"))
                continue
            # 直辖市（北京/上海/天津/重庆）的直接子级就是区县，不必再往下钻
            if cLevel == "district":
                entry["districts"].append(_oneDistrict(pAd, pName, child))
                continue
            if cLevel != "city":
                continue
            try:
                cityChildren = _featureOf(fetchGeoJson(cAd))
            except RuntimeError as e:
                _LOG.warning("fetch_zh_geo: 市 %s(%d) 子级拉取失败：%s"
                             % ((child.get("properties") or {}).get("name"), cAd, e))
                continue
            for one in cityChildren:
                if str((one.get("properties") or {}).get("level") or "") != "district":
                    continue
                entry["districts"].append(_oneDistrict(pAd, pName, one))
        if not entry["districts"]:
            # ⚠️ **必须报出来，不能静默**：实测 710000（台湾省）的 _full.json
            #   拉得到、但不含任何 district 要素，于是「台湾省的点永远查不到区县」。
            #   这类缺口靠日志一行提醒，人来决定要不要补源 —— 而不是等界面
            #   上出现「查不到地点」再去猜是数据缺还是代码错。
            empty.append(pName)
            _LOG.warning("fetch_zh_geo: 省 %s(%d) **没有任何区县要素**（源站该层级为空）",
                         pName, pAd)
        provinces.append(entry)
        print("   %-4d %-12s 区县 %4d 个" % (pAd, pName, len(entry["districts"])))

    payload = {
        "source": "DataV.GeoAtlas areas_v3 (geo.datav.aliyun.com)",
        "url": DATA_URL_BASE,
        "fetchedAt": misc.getTime(),
        "generator": "fetch_zh_geo.py _VERSION=%s" % _VERSION,
        "level": "district",
        "coordDigits": COORD_DIGITS,
        "provinces": provinces,
    }
    return {"payload": payload, "elapsed": round(time.time() - startTime, 1),
            "provinceNum": len(provinces),
            "skipped": skipped, "emptyProvinces": empty,
            "districtNum": sum(len(p["districts"]) for p in provinces)}


def _oneDistrict(pAdcode: int, pName: str, feature: dict) -> dict:
    """一个区县要素 -> 数据文件里的一项（只留判定必需的字段）。"""
    props = feature.get("properties") or {}
    return {
        "adcode": int(props.get("adcode") or 0),
        "name": str(props.get("name") or ""),
        "pa": int(pAdcode),
        "pn": str(pName),
        "g": _roundCoords((feature.get("geometry") or {}).get("coordinates") or []),
    }


def writeOut(payload: dict, outFile: str) -> dict:
    """原子落盘：先写 .tmp 再 os.replace（磁盘上不许出现半个文件）。"""
    target = os.path.abspath(outFile)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmpFile = target + ".tmp"
    # ⚠️ 压缩级别取 9：这份文件是**一次性**产出、之后只读，
    #    多花几秒 CPU 换体积是纯赚（默认 6 差不多能省 8% 体积）。
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with gzip.open(tmpFile, "wt", encoding="utf-8", compresslevel=9) as out:
        out.write(raw)
    os.replace(tmpFile, target)
    return {"outFile": target, "rawBytes": len(raw.encode("utf-8")),
            "gzBytes": os.path.getsize(target)}


def measure(payload: dict) -> dict:
    """**只算体积不落盘**（dry-run 用）。在内存里压一遍，报真实 gzip 后的大小。

    为什么 dry-run 也要压一次：这份文件是**一次性产出**，
    「到底多大」是决定「要不要入库」的唯一依据。
    而凭原始 JSON 长度估 gzip 大小会差一倍，那个数字没法用来做决定。
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as out:
        out.write(raw.encode("utf-8"))
    return {"outFile": os.path.abspath(DEFAULT_OUT),
            "rawBytes": len(raw.encode("utf-8")), "gzBytes": len(buf.getvalue())}


def main(argv=None) -> int:
    _fixConsole()
    parser = argparse.ArgumentParser(
        prog="fetch_zh_geo.py",
        description="抓取全国区县边界多边形，合并成一个压缩数据文件（地点中文名用）")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出文件（默认 %s）" % DEFAULT_OUT)
    parser.add_argument("--dry-run", action="store_true", help="只报不改（默认行为）")
    parser.add_argument("--apply", action="store_true", help="确认实跑（真正写文件）")
    parser.add_argument("--limit-province", type=int, default=0,
                        help="只抓前 N 个省（调试用；0 = 全部）")
    args = parser.parse_args(argv)
    dryRun = bool(args.dry_run) or not bool(args.apply)

    print("fetch_zh_geo _VERSION: %s" % _VERSION)
    print("数据源             : %s" % DATA_URL_BASE)
    print("精度               : level=district（第 %d 层），坐标 %d 位小数"
          % (MAX_LEVEL, COORD_DIGITS))
    print("")
    print("---- 抓取中（约 375 次请求，只在 --apply 时跑一次）----")
    report = build(limitProvince=max(0, int(args.limit_province)))
    payload = report["payload"]
    # ⚠️ dry-run **不写任何文件**（与 fix_placeholder_geo 同一纪律）：
    #   「改生产数据/落库文件必须显式 --apply」。体积照报，因为那是
    #   决定要不要入库的唯一依据，而 gzip 后的大小只能真压一遍才知道。
    size = measure(payload) if dryRun else writeOut(payload, args.out)

    print("\n---- 结果 ----")
    print("  省级 : %d 个%s" % (report["provinceNum"],
                              "（符合预期 %d）" % EXPECT_PROVINCE_NUM
                              if report["provinceNum"] == EXPECT_PROVINCE_NUM
                              else "（**预期 %d**）" % EXPECT_PROVINCE_NUM))
    print("  区县 : %d 个" % report["districtNum"])
    if report["skipped"]:
        print("  跳过 : %d 个伪 adcode（港澳下级，%s）"
              % (len(report["skipped"]), ", ".join(report["skipped"])))
    if report["emptyProvinces"]:
        print("  ⚠️ 无区县要素的省 : %s（该省点位只能给到省级中文，reason=insideProvince）"
              % ", ".join(report["emptyProvinces"]))
    print("  原始 : %.1f MB -> gzip %.1f MB" % (size["rawBytes"] / 1048576.0,
                                              size["gzBytes"] / 1048576.0))
    print("  文件 : %s" % size["outFile"])
    print("  耗时 : %.1f s" % report["elapsed"])

    if dryRun:
        print("\n[dry-run] 未写任何文件。确认无误后加 --apply 落库。")
    else:
        _LOG.info("fetch_zh_geo: 省级 %d / 区县 %d -> %s（%.1f MB -> %.1f MB）",
                  report["provinceNum"], report["districtNum"],
                  size["outFile"], size["rawBytes"] / 1048576.0,
                  size["gzBytes"] / 1048576.0)
        print("\n已落库。下一步：把 basicSettings.PLACE_AMAP_GEO_FILE 指向该文件。")
    return 0 if report["provinceNum"] == EXPECT_PROVINCE_NUM else 1


if __name__ == "__main__":
    sys.exit(main())
