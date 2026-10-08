#! /usr/bin/env python3
#encoding: utf-8

#Filename: placeNameZh.py
#Description: 地点中文名（pb_place.nameZh）的计算与回填 —— 步骤 R4a · DR-28/DR-29
#
# 要解决什么
# ----------
#   `reverse_geocoder` 的数据集（GeoNames）**只有英文、没有语言参数**，
#   `meta._composePlaceName()` 只能把 cc/admin1/admin2/name 去重拼串，
#   于是界面上永远是 "CN, Xinjiang Uygur Zizhiqu, Araltobe"。
#   （顺带一提：那个函数的 docstring 里写的示例是「中国, 北京市, 东城区」，
#     **与实际输出不符**，是一条误导性注释，别拿它当口径。）
#
# 为什么**另存一列**而不是改 placeName（DR-28 的核心）
# --------------------------------------------------
#   `placeStore.makePlaceCode()` 是**由 placeName 派生 placeCode**（幂等键）。
#   placeName 一变中文 => placeCode 变 => 旧行不被认领 => 被归零 =>
#   地图上出现「0 张照片却显示 N 张」的幽灵点（placeStore 文件头自己警告过的坑）。
#   所以本模块**一个字都不改 makePlaceCode**，只往 pb_place.nameZh 写显示名。
#   代价是同一行有两个名字，语义必须记住：
#       placeName = **聚合键**（英文，派生自 reverse_geocoder，rebuild 每次复算）
#       nameZh    = **显示名**（中文，**非派生列**，任何自动流程都不得覆盖）
#
# ⚠️⚠️ nameZh 绝不能进 placeStore.REBUILD_COLUMNS
# ---------------------------------------------------
#   那里面的每一列都能从 pb_photo 重算。nameZh **不能**（它来自外部地理数据集
#   与人工填写）。一旦放进去，每轮 rebuildPlaces() 都会把中文冲回英文，
#   而且**不报错** —— 照片张数对、地点名回英文，是最难发现的一类退化。
#   本模块的 rebuildNameZh() 也据此设计：**只填 NULL，绝不覆盖**。
#
# 三条纪律
# --------
#   ① **境外不翻译**（用户决策，DR-29②）：Tokyo / Paris 的 nameZh 留 NULL，
#      界面回退英文。外国人地名本来就该用当地语言，译成中文反而是错的。
#      ⇒ 境内判定必须是**真判定**，不能用 `cc == 'CN'` 那种字符串比较
#        （边界不准、要多查一次，而且 GeoNames 的 cc 覆盖不到边界争议区）。
#        本模块用**多边形覆盖**判定：落在任何省/区县多边形内 = 境内。
#   ② **降级不报错**：`shapely` 没装 / 数据文件不在 / 文件损坏
#      —— 一律 nameZh=None + **只记一次** warning。装不上只是「显示英文」，
#      不是故障，绝不允许抛错中断扫描或接口。
#   ③ **懒加载 + 进程内缓存**：首次约 1~3 秒（解压 5.7MB + 建 2840 个多边形），
#      之后常驻。**由 main/app.py 启动时预热**（见 warmUp 的说明），
#      不许让首个 HTTP 请求替我们付这笔钱。
#
# 依赖（**都是可选的**）
# --------------------
#   `shapely` —— point-in-polygon 引擎。
#   `amap_geo` —— ⚠️ **实测该包在 PyPI 上不存在**（amap-geo / amap_geo /
#     geo-amap / amapgeo 全部 404 或 no matching distribution）。
#     原始设计（DR-29）用的是它；现在改成「**本地数据文件 + 可选包**」双来源，
#     数据文件由 `tools/fetch_zh_geo.py` 一次性抓取落库（DataV.GeoAtlas，
#     34 省 / 2840 区县，5.7MB gzip）。
#     **保留 import amap_geo 这条路**是为了：① 谁装了谁先用；
#     ② 降级路径（包缺失 -> 只记一次 warning）必须真的被跑到过，
#        而不是「反正包不存在，那段代码可以不写」。
#     ⚠️ 生产代码**永不联网**（DR-29「不引入联网逆地理 API」的全部意义）。

import gzip
import json
import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/place
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database import queryCommon as query                         # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.scanner import meta as meta                        # noqa: E402

_VERSION = "20261007"

_LOG = misc.setLogNew("placeNameZh", "placenamezh.log")

# ============================================================
# 一、依赖懒加载闸门（照抄 scanner/meta.py 的 _RG_READY / _RG_WARNED 纪律）
# ============================================================
#: None = 未尝试 / True = 可用 / False = 不可用（已降级）
_ZH_READY = None
#: 「只记一次 warning」的闸门。⚠️ 与 _ZH_READY 分开是有意的：
#:   _ZH_READY=False 记的是「依赖不可用」，而**每次** zhNameOf() 都会经过它 ——
#:   几千个地点各记一条 warning 会把日志刷爆、真正的告警被埋掉。
_ZH_WARNED = False
#: 已建好的索引（进程内缓存）。_INDEX 里有值 = 可用
_INDEX = None
#: 首次加载耗时（秒），给启动横幅与验收第 6 条用
_LOAD_SECONDS = 0.0
#: 数据来源标记（"package" / "file"），只用于日志与自检输出
_PROVIDER = ""
#: 建索引时**构造失败**的多边形数（取整导致的自相交等）。
#: ⚠️ 必须计数上报而不是 try/except 静默跳过：一个坏多边形会让某个区县的点
#:   永远查不到中文名，而界面上只表现为「这个地点没中文名」——
#:   与「数据源缺失」长得一模一样，没法区分。
_BAD_GEOMETRY = 0
#: 其它「只记一次」的 warning（与 _ZH_WARNED 分开，见 _warnOnce）
_WARNED_ONCE = False

#: 包内置的数据文件（PLACE_AMAP_GEO_FILE 为空时用它）
DEFAULT_GEO_FILE: str = os.path.join(_SRC_DIR, "data", "china_district.json.gz")

#: `nameZh` 的来源（⚠️ 与 pb_place.source **不是同一个东西**，
#:   那个是「这一行怎么进库的」，这个是「这个中文名哪来的」）
ZH_SOURCE_NONE: int = 0        # 没有中文名（境外 / 无坐标 / 依赖缺失）
ZH_SOURCE_ADMIN: int = 2       # 行政区划算出来的
ZH_SOURCE_MANUAL: int = 3      # 人工填的（本模块**不产出**，只认）

#: `zhNameOf` 的 reason 取值。⚠️ **每个分支都必须给明确 reason**，
#:   不许「查不到就返回空字符串」——那样统计里会少掉一整类问题，
#:   而「没给中文名」有 5 种完全不同的原因（境外 / 无坐标 / 数据缺口 / 开关关 / 依赖缺）
REASON_OK: str = "ok"                          # 命中区县
REASON_INSIDE_PROVINCE: str = "insideProvince"  # 境内但没落进任何区县（台湾省 / 数据缺口）
REASON_OVERLAP: str = "overlap"                # 多个区县多边形同时覆盖（重叠）
REASON_OUTSIDE: str = "outsideChina"           # 不在任何多边形内（境外，或数据集缺口）
REASON_NO_COORD: str = "noCoordinate"          # centerLat/centerLon 缺失或占位
REASON_DISABLED: str = "disabled"              # PLACE_ZH_ENABLED = False
REASON_NO_DATA: str = "noDataSource"           # 数据文件与 amap_geo 都不可用
REASON_NO_PROVINCE: str = "provinceUnknown"    # 命中区县但省级中文映射表里没有该写法

#: 上面这些 reason 的中文说明（给 CLI / 日志输出看，**不参与逻辑**）
REASON_TEXT: dict = {
    REASON_OK: "命中区县",
    REASON_INSIDE_PROVINCE: "境内但无区县多边形（省级兜底）",
    REASON_OVERLAP: "多个区县多边形重叠命中",
    REASON_OUTSIDE: "不在任何多边形内（境外或数据集缺口）",
    REASON_NO_COORD: "无坐标（手工建档 / 照片全无 GPS）",
    REASON_DISABLED: "PLACE_ZH_ENABLED=False（已关）",
    REASON_NO_DATA: "数据源不可用（shapely / 数据文件 / amap_geo）",
    REASON_NO_PROVINCE: "省级中文映射表未收录该写法",
}

# ============================================================
# 二、省级 EN -> ZH 映射表（**34 个省级行政区，有限且可穷举**）
# ============================================================
# 为什么只映射「一级行政区」而不映射全球地名
# ------------------------------------------
#   一级行政区只有 34 个，**闭集**、可穷举、可人工逐个核对。
#   区县有 2840 个且天天在变（撤县设区），靠表映射 = 维护一张必然过期的表。
#   所以：省级查表（稳），**区县靠 point-in-polygon**（永远跟得上数据源）。
#
# 键为什么有别名
# --------------
#   键是 **reverse_geocoder / GeoNames 的 admin1 原文**。实测该数据集里 CN 的
#   admin1 只有 32 种写法，且**同一个省在不同条目下写法并不统一**：
#     · 直辖市不带 Sheng（「Beijing」），多数省带（「Zhejiang Sheng」）
#     · 自治区是全拼译名（「Xinjiang Uygur Zizhiqu」「Nei Mongol Zizhiqu」）
#     · 内蒙在 GeoNames 里是**英文**「Inner Mongolia」，不是拼音
#     · 西藏是「Tibet Autonomous Region」而不是「Xizang Zizhiqu」
#   而 `_composePlaceName` 是**去重**拼串，admin1 会不会出现取决于条目，
#   所以每省都把已知的几种写法全列上。少列一个的后果是该省的地点**静默**
#   拿不到省级中文（reason=provinceUnknown）—— 不报错，只是不显示。
#
# ⚠️ 这张表**只管省级**。任何「把全球地名译成中文」的冲动都不要有：
#   境外保留当地语言是用户决策（DR-29②），不是待办。
PROVINCE_EN_ZH: dict = {
    # ---- 直辖市 4 ----
    "Beijing": "北京市",
    "Tianjin": "天津市",
    "Tianjin Shi": "天津市",
    "Shanghai": "上海市",
    "Shanghai Shi": "上海市",
    "Chongqing": "重庆市",
    "Chongqing Shi": "重庆市",
    # ---- 省（GeoNames 里带不带 Sheng 两种都有，全列）----
    "Hebei": "河北省",
    "Shanxi": "山西省",
    "Shanxi Sheng": "山西省",
    "Liaoning": "辽宁省",
    "Jilin": "吉林省",
    "Jilin Sheng": "吉林省",
    "Heilongjiang": "黑龙江省",
    "Heilongjiang Sheng": "黑龙江省",
    "Jiangsu": "江苏省",
    "Jiangsu Sheng": "江苏省",
    "Zhejiang": "浙江省",
    "Zhejiang Sheng": "浙江省",
    "Anhui": "安徽省",
    "Anhui Sheng": "安徽省",
    "Fujian": "福建省",
    "Jiangxi": "江西省",
    "Jiangxi Sheng": "江西省",
    "Shandong": "山东省",
    "Shandong Sheng": "山东省",
    "Henan": "河南省",
    "Henan Sheng": "河南省",
    "Hubei": "湖北省",
    "Hunan": "湖南省",
    "Guangdong": "广东省",
    "Hainan": "海南省",
    "Sichuan": "四川省",
    "Sichuan Sheng": "四川省",
    "Guizhou": "贵州省",
    "Guizhou Sheng": "贵州省",
    "Yunnan": "云南省",
    "Yunnan Sheng": "云南省",
    "Shaanxi": "陕西省",
    "Shaanxi Sheng": "陕西省",
    "Gansu": "甘肃省",
    "Gansu Sheng": "甘肃省",
    "Qinghai": "青海省",
    "Qinghai Sheng": "青海省",
    # ---- 自治区 5 ----
    "Inner Mongolia": "内蒙古自治区",
    "Nei Mongol": "内蒙古自治区",
    "Nei Mongol Zizhiqu": "内蒙古自治区",
    "Inner Mongolia Autonomous Region": "内蒙古自治区",
    "Guangxi Zhuangzu Zizhiqu": "广西壮族自治区",
    "Guangxi Zhuang Autonomous Region": "广西壮族自治区",
    "Guangxi": "广西壮族自治区",
    "Tibet Autonomous Region": "西藏自治区",
    "Tibet": "西藏自治区",
    "Xizang Zizhiqu": "西藏自治区",
    "Xizang": "西藏自治区",
    "Ningxia Huizu Zizhiqu": "宁夏回族自治区",
    "Ningxia Hui Autonomous Region": "宁夏回族自治区",
    "Ningxia": "宁夏回族自治区",
    "Xinjiang Uygur Zizhiqu": "新疆维吾尔自治区",
    "Xinjiang Uygur Autonomous Region": "新疆维吾尔自治区",
    "Xinjiang": "新疆维吾尔自治区",
    # ---- 特别行政区 2 ----
    "Hong Kong": "香港特别行政区",
    "Hong Kong SAR": "香港特别行政区",
    "Xianggang": "香港特别行政区",
    "Macao": "澳门特别行政区",
    "Macau": "澳门特别行政区",
    "Macao SAR": "澳门特别行政区",
    "Aomen": "澳门特别行政区",
    # ---- 台湾省 1 ----
    "Taiwan": "台湾省",
    "Taiwan Province": "台湾省",
    "Taidian": "台湾省",
}

#: 34 个省级行政区的**规范中文名**全集（自检用：映射表必须覆盖它们）
PROVINCE_ZH_ALL: tuple = (
    "北京市", "天津市", "河北省", "山西省", "内蒙古自治区", "辽宁省", "吉林省",
    "黑龙江省", "上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省",
    "山东省", "河南省", "湖北省", "湖南省", "广东省", "广西壮族自治区",
    "海南省", "重庆市", "四川省", "贵州省", "云南省", "西藏自治区", "陕西省",
    "甘肃省", "青海省", "宁夏回族自治区", "新疆维吾尔自治区",
    "香港特别行政区", "澳门特别行政区", "台湾省",
)

#: 查表用的小写索引（`placeName` 里的大小写/空格差异不该让查表失败）
_PROVINCE_INDEX: dict = {}


def _provinceZhOf(token: str) -> str:
    """单个 token（placeName 的一段）-> 省级中文名；查不到返回 ""。"""
    if not token:
        return ""
    if not _PROVINCE_INDEX:
        for key, value in PROVINCE_EN_ZH.items():
            _PROVINCE_INDEX[key.strip().lower()] = value
    return _PROVINCE_INDEX.get(str(token).strip().lower(), "")


def provinceZhOfPlaceName(placeName: str) -> str:
    """从 `placeName`（"CN, Xinjiang Uygur Zizhiqu, Araltobe"）里认出省级中文名。

    ⚠️ 为什么是**扫全部分段**而不是死取第 2 段
    ------------------------------------------
      `meta._composePlaceName()` 对 cc/admin1/admin2/name 做**去重**拼接，
      于是分段数是不定的：`"CN, Beijing"`（admin2/name 与 admin1 同名被去掉了）
      只有 2 段，`"CN, Beijing, Datun"` 有 3 段。死取 tokens[1] 在前者上
      恰好对、在「cc 与 admin1 同名」等组合上就错，而错了只会**静默**少一段前缀。
      扫全部 token、命中即返回，对分段数免疫；代价是每个地点多几次字典查找
      （几十个地点 × 4 段，可忽略）。
    """
    for token in str(placeName or "").split(","):
        got = _provinceZhOf(token)
        if got:
            return got
    return ""


# ============================================================
# 三、数据源加载（懒加载 + 进程内缓存）
# ============================================================

def _geoFilePath() -> str:
    """实际要读的数据文件路径（配置优先，缺省包内置那份）。"""
    return str(basicSettings.PLACE_AMAP_GEO_FILE or "").strip() or DEFAULT_GEO_FILE


def _warnOnce(text: str) -> None:
    """只记一次 warning（与 _ZH_WARNED 分开：那是「依赖不可用」，这是别的）"""
    global _WARNED_ONCE
    if _WARNED_ONCE:
        return
    _WARNED_ONCE = True
    _LOG.warning("placeNameZh: %s", text)


def _warnOnceFmt(fmt: str, *args) -> None:
    """`_warnOnce` 的格式化版。

    ⚠️ 分成两个函数而不是给 `_warnOnce` 加 `*args`：**格式化必须在闸门之外**。
       若写成 `_warnOnce(fmt % *args)`，那么「这一条已经记过了」的情况下
       `%` 仍然会执行 —— 而调用点的实参可能是几十行里最贵的那段计算。
    """
    if _WARNED_ONCE:
        return
    _warnOnce(fmt % args)


def _readGeoPayload(filePath: str) -> dict:
    """读 gzip JSON 数据文件。⚠️ 按扩展名判 gzip，不靠魔数。"""
    if filePath.lower().endswith(".gz"):
        with gzip.open(filePath, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    with open(filePath, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _loadFromPackage():
    """从可选依赖 `amap_geo` 取数据。**包不存在就抛 ImportError**（由调用方降级）。

    ⚠️ 这里刻意**只依赖一个具名函数** `loadDistricts()`，返回
       `[{"province": 中文省名, "name": 中文区县名, "geometry": GeoJSON几何}, ...]`。
       写成这样而不是「猜包里的 API」：接口猜错时抛 AttributeError，
       会被上面的 except 兜住并记一次 warning，与「包没装」同一条降级路径 ——
       不会因为猜错而把整个服务搞挂。
    """
    import amap_geo                                              # noqa: F401
    return amap_geo.loadDistricts()


def _asMultiPolygon(coords):
    """把 GeoJSON 坐标数组**嗅探**成 MultiPolygon（4 层：poly -> ring -> point）。

    ⚠️ 为什么必须嗅探而不是照抄 `"type": "MultiPolygon"`
    ----------------------------------------------
      数据源里**同一个字段有两种形状**：绝大多数要素是 MultiPolygon（4 层），
      但少数（实测「内蒙古自治区」的省多边形）是 Polygon（3 层）。
      一律按 MultiPolygon 喂给 shapely，它会走到最内层去迭代，
      报 **`TypeError: 'float' object is not iterable`**。
      那个报错发生在**逐要素 try/except 里**（见 _buildIndex），
      于是表现是「内蒙古自治区的点判不出境内」——
      一个不报错、只是少覆盖一个省的问题。
      按嵌套深度判形状，三层/四层都能吃，且不用改数据文件。
    """
    if not coords:
        return []
    head = coords[0]
    if not isinstance(head, (list, tuple)):
        return None                                    # 不是坐标数组 = 坏数据
    if head and isinstance(head[0], (int, float)):
        return [coords]                                # 2 层：单个环 -> 包成一个多边形
    if head and isinstance(head[0], (list, tuple)) and head[0] \
            and isinstance(head[0][0], (list, tuple)) \
            and head[0][0] and isinstance(head[0][0][0], (int, float)):
        return coords                                  # 4 层：已是 MultiPolygon，**原样**
    # 3 层：Polygon -> 每个环各包一层，变成 MultiPolygon
    return [[ring] for ring in coords]


def _buildIndex(payload) -> dict:
    """payload -> {districts:[(省, 区县, geom)], provinces:[(省, geom)], tree, pTree}。

    ⚠️ 为什么用 shapely 的 STRtree 而不是自己线性扫 2800+ 个多边形
      线性扫每次判定要做 2800+ 次 `covers`，而 `/api/places` 一次返回
      几百个地点 —— 那是百万级几何运算。STRtree 先按 bbox 过滤，
      每次判定通常只剩 1~3 个候选。
    """
    global _BAD_GEOMETRY
    from shapely.geometry import shape
    from shapely.strtree import STRtree

    districts, provinces = [], []
    for province in (payload or {}).get("provinces") or []:
        pName = str(province.get("name") or "")
        pGeom = None
        try:
            ring = _asMultiPolygon(province.get("polys") or [])
            if ring:
                pGeom = shape({"type": "MultiPolygon", "coordinates": ring})
        except Exception as e:                                   # noqa: BLE001
            _BAD_GEOMETRY += 1
            _LOG.warning("placeNameZh: 省级多边形 %s 构造失败（%s: %s）",
                         pName, type(e).__name__, e)
        if pGeom is not None:
            # ⚠️ 省级项也存成 **(省, None, geom)** 三元组，与区县项**同形**。
            #   之前存成二元组，于是共用的 _coveredHit 里 `item[2]` 在省级那条路上
            #   直接 `IndexError: tuple index out of range` —— 也就是
            #   `isInChina()` 一直在**抛异常**（只是别的用例没走到它才没炸）。
            #   同形之后「多边形项」只有一个形状，_coveredHit 与两棵 STRtree
            #   都只需要知道「第 3 个元素是几何」。
            provinces.append((pName, None, pGeom))
        for one in province.get("districts") or []:
            name = str(one.get("name") or "")
            # ⚠️ 数据文件里每条区县都冗余存了所属省（pa/pn），
            #   这样即使某省的省多边形构造失败，区县仍然带得上省名。
            owner = str(one.get("pn") or "") or pName
            try:
                geom = shape({"type": "MultiPolygon",
                              "coordinates": _asMultiPolygon(one["g"])})
            except Exception as e:                               # noqa: BLE001
                _BAD_GEOMETRY += 1
                continue
            districts.append((owner, name, geom))

    if not provinces and not districts:
        raise ValueError("数据源里没有任何可用的多边形")
    return {"districts": districts, "provinces": provinces,
            "tree": STRtree([item[2] for item in districts]) if districts else None,
            "pTree": STRtree([item[2] for item in provinces]) if provinces else None,
            "badGeometry": _BAD_GEOMETRY}


def _loadIndex() -> dict:
    """建索引（真加载）。失败抛异常 —— **由 ready() 决定要不要吞**。"""
    global _PROVIDER
    from shapely.geometry import Point

    start = time.time()
    # ① 先试可选包（原始设计的来源；装了谁先用）
    try:
        payload = _loadFromPackage()
        _PROVIDER = "package(amap_geo)"
    except Exception as e:                                       # noqa: BLE001
        _LOG.debug("placeNameZh: amap_geo 不可用（%s: %s），改用本地数据文件",
                   type(e).__name__, e)
        # ② 本地数据文件（fetch_zh_geo.py 产出，入库）
        filePath = _geoFilePath()
        if not os.path.isfile(filePath):
            raise RuntimeError("数据文件不存在: %s"
                               "（跑一次 tools\\fetch_zh_geo.py --apply 生成）"
                               % filePath)
        payload = _readGeoPayload(filePath)
        _PROVIDER = "file(%s)" % os.path.basename(filePath)

    index = _buildIndex(payload)
    index["pointOf"] = Point
    return index


def ready(force: bool = False) -> bool:
    """中文名数据源是否可用。**首次调用才真加载**（约 1~3 秒），之后走缓存。

    返回 False 时**已经记过一次 warning**（只记一次，见 _ZH_WARNED）。
    """
    global _ZH_READY, _INDEX, _LOAD_SECONDS, _ZH_WARNED
    if _ZH_READY and not force:
        return True
    if not basicSettings.PLACE_ZH_ENABLED:
        # ⚠️ 开关关掉**不算错误**：不加载、不记 warning（用户主动关的，
        #   再 warn 一次就成了骚扰）。只把 _INDEX 留着不动 ——
        #   这样「改了 basicSettings 之后再打开」不用重新解压 5.7MB。
        _ZH_READY = False
        return False
    if _INDEX is not None and not force:
        _ZH_READY = True
        return True
    try:
        start = time.time()
        _INDEX = _loadIndex()
        _LOAD_SECONDS = round(time.time() - start, 3)
        _ZH_READY = True
        _LOG.info("placeNameZh: 数据源就绪（%s），区县 %d / 省级 %d，"
                  "坏多边形 %d 个，耗时 %.3f s",
                  _PROVIDER, len(_INDEX["districts"]), len(_INDEX["provinces"]),
                  _BAD_GEOMETRY, _LOAD_SECONDS)
    except Exception as e:                                       # noqa: BLE001
        _INDEX = None
        _ZH_READY = False
        if not _ZH_WARNED:
            _ZH_WARNED = True
            _LOG.warning("placeNameZh: 数据源不可用（%s: %s）；"
                         "nameZh 一律留空、界面回退 placeName 英文，"
                         "属降级不属错误，扫描与接口都不受影响",
                         type(e).__name__, e)
    return bool(_ZH_READY)


def warmUp() -> dict:
    """**启动时预热**（main/app.py lifespan 调它）。返回一句可展示的状态。

    为什么要预热而不是「等第一次 /api/places 调用时加载」
    --------------------------------------------------
      首次加载实测 1~3 秒（解压 5.7MB gzip + 建 2840 个 shapely 多边形 + 两棵
      STRtree）。落在请求路径上的话，用户看到的是「点开地点页先卡 2 秒」——
      而这个服务绑回环、零鉴权、本机单人用，**启动时多花 2 秒完全可接受**，
      换来的是「任何一次请求都不付这笔钱」。
      ⚠️ 更关键的理由：预热是**一次性的、看得见**（启动横幅里有一行），
      而落在请求上就会变成「有时快有时慢」，排障时最难定位。
    """
    if not basicSettings.PLACE_ZH_ENABLED:
        return {"ready": False, "reason": REASON_DISABLED,
                "text": REASON_TEXT[REASON_DISABLED], "seconds": 0.0}
    ok = ready()
    if not ok:
        return {"ready": False, "reason": REASON_NO_DATA,
                "text": REASON_TEXT[REASON_NO_DATA], "seconds": 0.0}
    return {"ready": True, "reason": REASON_OK, "provider": _PROVIDER,
            "districtNum": len(_INDEX["districts"]),
            "provinceNum": len(_INDEX["provinces"]),
            "badGeometry": _BAD_GEOMETRY,
            "seconds": _LOAD_SECONDS,
            "text": "就绪（%s，%d 区县 / %d 省级，%.3f s）"
                    % (_PROVIDER, len(_INDEX["districts"]),
                       len(_INDEX["provinces"]), _LOAD_SECONDS)}


def status() -> dict:
    """自检用：数据源状态快照（**不触发加载**）。"""
    return {"enabled": bool(basicSettings.PLACE_ZH_ENABLED),
            "ready": bool(_ZH_READY), "provider": _PROVIDER,
            "geoFile": _geoFilePath(),
            "loadSeconds": _LOAD_SECONDS, "badGeometry": _BAD_GEOMETRY,
            "districtNum": len(_INDEX["districts"]) if _INDEX else 0,
            "provinceNum": len(_INDEX["provinces"]) if _INDEX else 0,
            "provinceZhCount": len(PROVINCE_ZH_ALL)}


# ============================================================
# 四、判定
# ============================================================
def _blank(placeCode: str, reason: str) -> dict:
    """统一的无中文名返回。**所有失败分支都必须走它**（不许各写各的 dict）。"""
    return {"placeCode": str(placeCode or ""), "nameZh": None,
            "source": ZH_SOURCE_NONE, "reason": reason,
            "province": None, "district": None}


def _coveredHit(index: dict, treeKey: str, itemsKey: str, lat: float, lon: float) -> list:
    """在 treeKey 上查 bbox 候选，再做**精确**覆盖判定。返回命中的 items 三元组。

    ⚠️ STRtree.query 的返回形态随 shapely 版本变：**2.x 返回下标，1.x 返回几何对象**。
       认错的后果不是报错，而是「命中的区县」变成**随机一条** ——
       结果是一个**看起来很正常的中文名**，静默错到没人发现。
       ⚠️⚠️ 而且 2.x 返回的是 **numpy 整数**（`numpy.int64`），
       **`isinstance(np.int64(3), int)` 是 False** —— 用 isinstance 判会
       把「正常返回下标」误判成「不认识」，于是每次判定都退化成线性扫
       （2834 次 covers × 每个地点），功能对但慢两个数量级，
       而且日志里只在启动时打一条 warning，看不出「其实每条判定都在走慢路」。
       所以判据用 **`int(...)` 能不能成功**：Python int 与 numpy int 都行，
       shapely 几何对象会抛 TypeError。
    """
    items = index[itemsKey]
    point = index["pointOf"](float(lon), float(lat))
    tree = index.get(treeKey)
    if tree is None:
        return []
    hits = list(tree.query(point))
    out = []
    for one in hits:
        try:
            index_ = int(one)
        except (TypeError, ValueError):       # 返回的是几何对象（shapely 1.x）
            _warnOnce("STRtree.query 不返回下标（shapely 版本？），已降级为线性扫")
            return [item for item in items if item[2].covers(point)]
        if not 0 <= index_ < len(items):      # 下标越界 = 数据与索引不同步
            _warnOnce("STRtree 下标 %r 越界（共 %d 个），已跳过该候选"
                      % (index_, len(items)))
            continue
        item = items[index_]
        # covers() 而不是 contains()：点在**边界上**也算落在这个区县。
        # 差这一条的后果是「正好站在区县界上的那个地点」偶发查不到，
        # 而它下次 rebuild 又查到了 —— 表现为「同一个地点中文名时有时无」。
        if item[2].covers(point):
            out.append(item)
    return out


def isInChina(lat, lon) -> bool:
    """这个坐标在境内吗？（多边形覆盖判定，**不用 cc == 'CN'**）

    ⚠️ 为什么不用 `cc == 'CN'`（DR-28 明确要求）
      ① 边界不准：GeoNames 的 cc 是**按城市点**标的，边界争议区与海上归属
         会给出看着像境外、实际在境内的结果；
      ② 要多查一次：得先跑一次逆地理才拿得到 cc，而本函数的价值就是
         **不依赖那一次查询**；
      ③ 港澳台在 GeoNames 里 cc 不是 CN，按 cc 判会把它们全算成「境外」。

    判据：落在**任一省级多边形**内（省级才是「境内」的权威边界，
    区县多边形是它的子集，判境内用子集会在「省界上的飞地」上出错）。
    ⚠️ 台湾省在数据源里**没有区县要素**，但有省级多边形 —— 所以判境内必须用省级。
    """
    if not ready():
        return False
    # ⚠️ 占位坐标 (0,0) 必须在进多边形判定**之前**挡掉（复用 meta 的判据，
    #   与扫描器、fix_placeholder_geo 三处同一份，不另写一遍）
    if not meta.isRealCoordinate(lat, lon):
        return False
    return bool(_coveredHit(_INDEX, "pTree", "provinces", float(lat), float(lon)))


def zhNameOf(placeCode: str, placeName: str = "", centerLat=None,
             centerLon=None) -> dict:
    """给一个地点算中文名。返回 `{nameZh, source, reason, province, district}`。

    口径
    ----
      nameZh  = "<省级中文> · <区县中文>"，如「新疆维吾尔自治区 · 阿勒泰市」
      source  = 0 无 / 2 行政区 / 3 手工（本函数**不产出 3**）
      reason  = 见 REASON_* ；**每个分支都有明确 reason**，便于计数排障

    ⚠️ 三类必须给 None 的情形
      ① centerLat/centerLon 为空（手工建的地点、照片全无 GPS）
         —— 没有坐标就**没有判定依据**，不许「按名字猜一个」。
      ② 境外 —— **保留当地语言，不翻译**（用户决策 DR-29②）
      ③ 数据源不可用 —— 降级，不报错

    ⚠️ 省级中文的来源有两条路（**主 + 兜底**）
      主：按 placeName 里的 admin1 查 PROVINCE_EN_ZH（DR-29 的口径）
      兜：查表没命中时，用**命中区县自带的所属省中文名**
          （数据文件里每条区县都冗余存了省名）——
          这条兜底不是多余的：GeoNames 的 admin1 写法会变，
          而表是手写的。少它，admin1 一改写法整省地点就**静默**退回英文。
    """
    if not basicSettings.PLACE_ZH_ENABLED:
        return _blank(placeCode, REASON_DISABLED)
    # ---- ① 无坐标：没有判定依据，直接放弃（不猜）----
    if not meta.isRealCoordinate(centerLat, centerLon):
        return _blank(placeCode, REASON_NO_COORD)
    if not ready():
        return _blank(placeCode, REASON_NO_DATA)

    lat, lon = float(centerLat), float(centerLon)
    hits = _coveredHit(_INDEX, "tree", "districts", lat, lon)
    provinceZh = provinceZhOfPlaceName(placeName)

    # ---- ② 命中多个区县：重叠。⚠️ 必须报出来并计数，不要静默取第一个 ----
    #     （DataV 的区县本应互斥，但「飞地 / 沿海 islands」确实会命中两条。
    #       静默取第一个的后果是「同一批照片里两个地点其实是同一个区县」被掩盖。）
    if len(hits) > 1:
        names = sorted(set("%s·%s" % (h[0], h[1]) for h in hits))
        _LOG.warning("placeNameZh: %s 命中 %d 个区县多边形（%s）—— 数据重叠，"
                     "取面积最大的一个", placeCode, len(hits), ", ".join(names))
        hits = [max(hits, key=lambda h: h[2].area)]
        overlap = True
    else:
        overlap = False

    if not hits:
        # ---- ③ 没落进任何区县：先看是不是在境内 ----
        if not isInChina(lat, lon):
            return _blank(placeCode, REASON_OUTSIDE)
        # 境内但无区县（台湾省无区县要素 / 数据集缺口）-> 只给省级中文
        pHit = _coveredHit(_INDEX, "pTree", "provinces", lat, lon)
        fallbackProvince = pHit[0][0] if pHit else ""
        finalProvince = provinceZh or fallbackProvince
        if not finalProvince:
            return _blank(placeCode, REASON_NO_PROVINCE)
        return {"placeCode": str(placeCode or ""), "nameZh": finalProvince,
                "source": ZH_SOURCE_ADMIN, "reason": REASON_INSIDE_PROVINCE,
                "province": finalProvince, "district": None,
                "overlap": overlap}

    ownerProvince, districtName, _geom = hits[0]
    # ⚠️ 省级中文以**哪个**为准：映射表（按 placeName 的 admin1）还是多边形
    #   （按坐标实际落在哪个省）？两者都是坐标推出来的，理论上应当一致。
    #   实测会不一致：比如 placeName 写 "CN, Beijing, X" 而坐标其实在新疆，
    #   此时若以映射表为准就会拼出「北京市 · 奇台县」这种**地理上不可能**的组合。
    #   所以：**两者都有且不一致时以多边形为准**（多边形按真实边界判，
    #   而 admin1 是「最近的城市点」推出来的，本就可能跨县）。
    #   映射表仍是主路径（DR-29 的口径），这里只是不让它盖掉更精确的证据。
    if provinceZh and ownerProvince and provinceZh != ownerProvince:
        _warnOnceFmt("省名不一致：placeName 的 admin1 映射为「%s」而坐标落在「%s」"
                     "（%s）—— 已采用后者", provinceZh, ownerProvince, placeName)
        provinceZh = ""
    finalProvince = provinceZh or ownerProvince
    nameZh = ("%s%s%s" % (finalProvince, basicSettings.PLACE_ZH_JOINER, districtName)
              if finalProvince else districtName)
    if not provinceZh and ownerProvince:
        # 走了兜底：值得留一条 info，便于发现「映射表该补写法了」
        _LOG.info("placeNameZh: %s 的 admin1 写法不在映射表里，"
                  "已用多边形自带的省名兜底（%s）", placeName, ownerProvince)
    return {"placeCode": str(placeCode or ""),
            "nameZh": nameZh[:128], "source": ZH_SOURCE_ADMIN,
            # ⚠️ 重叠时 reason 记 `overlap` 而不是 `ok`：这样 byReason 里
            #   「重叠」是**单独一类**、能被计数上报。若混进 ok，
            #   这一类数据问题就永远看不见了（而它确实存在，见上面的警告）。
            #   中文名仍然写库 —— 取面积最大的那个是合理推断，
            #   但「这里是推断出来的」这件事必须留在 reason 里。
            "reason": REASON_OVERLAP if overlap else REASON_OK,
            "province": finalProvince,
            "district": districtName, "overlap": overlap}


# ============================================================
# 五、回填 pb_place.nameZh（**只填 NULL，绝不覆盖**）
# ============================================================
def _placesWithoutNameZh() -> list:
    """pb_place 里**还没有** nameZh 的行（nameZh IS NULL 或空串）。

    ⚠️ 为什么用 `query.selectList` 而不是生成层的 `query_pb_place`：
       生成层的等值过滤与 nullFields 都是**白名单**（防 SQL 注入），
       `nameZh` 不在白名单里。而「按 nameZh 是不是空来筛」是本函数的
       **全部业务逻辑**，不能靠给白名单加一项来绕过 ——
       那等于让生成层为某一个业务开一个后门。
       `queryCommon.selectList` 是项目既定的「只读出口」（placeStore.listPlaces
       自己也用它），参数一律走 %s 占位。
    """
    return query.selectList(
        "SELECT g.placeCode AS placeCode, g.placeName AS placeName,"
        " g.source AS source, g.centerLat AS centerLat, g.centerLon AS centerLon"
        " FROM pb_place g"
        " WHERE g.delFlag = %s AND (g.nameZh IS NULL OR g.nameZh = '')"
        " ORDER BY g.recID ASC",
        (comGD.DEL_FLAG_NO,))


def rebuildNameZh(dryRun: bool = False, dbFile: str = None) -> dict:
    """遍历 pb_place，为**还没有 nameZh** 的行补中文名。

    ⚠️⚠️ **绝不覆盖已有的 nameZh**
    --------------------------
      人工填的（`pb_place.source=1` 的行）与上一轮算出来的都**原样保留**。
      这条是 `nameZh` 敢做成非派生列的**前提**：一旦允许覆盖，
      「用户辛辛苦苦把「阿勒泰市」改成「我家」」会在下一次 rebuild 时消失，
      而且不报错。
      实现上就是**只 SELECT `nameZh IS NULL OR nameZh = ''` 的行** ——
      覆盖在 SQL 层面就做不到，不靠调用方自觉。

    统计口径
    --------
      candidates  参与计算的行数（= 还没有 nameZh 的行）
      filled      实际写入 nameZh 的行数
      byReason    每个 reason 的计数（**这是本函数最有价值的输出**：
                  「境外 3 / 无坐标 1 / 覆盖重叠 1」一眼就能看出数据有没有问题）
      manualFilled  上面 filled 里 `source=1`（手工行）的条数。
                     ⚠️ 单独报出来：手工行被补了中文名是**用户没要求的行为**，
                     必须让人看见，而不是混在 filled 里

    dryRun
    ------
      **只算不写**（默认），报告将改多少行。写库必须显式 dryRun=False。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)          # DR-10：必须显式才切库
    if not query.tableExists("pb_place"):
        raise RuntimeError("pb_place 表不存在 —— 先跑一次 tools\\build_db.py")

    started = time.time()
    rows = _placesWithoutNameZh()
    byReason, samples = {}, []
    filled = manualFilled = 0
    now = misc.getTime()

    for one in rows:
        code = str(one.get("placeCode") or "")
        got = zhNameOf(code, str(one.get("placeName") or ""),
                       one.get("centerLat"), one.get("centerLon"))
        reason = str(got.get("reason") or "")
        byReason[reason] = byReason.get(reason, 0) + 1
        nameZh = got.get("nameZh")
        if not nameZh:
            if len(samples) < 20:
                samples.append({"placeCode": code,
                                "placeName": str(one.get("placeName") or ""),
                                "center": "%s,%s" % (one.get("centerLat"),
                                                     one.get("centerLon")),
                                "reason": reason,
                                "reasonText": REASON_TEXT.get(reason, reason)})
            continue                              # 算不出来就**保持 NULL**
        if dryRun:
            filled += 1
            if int(one.get("source") or 0) == 1:
                manualFilled += 1
            if len(samples) < 20:
                samples.append({"placeCode": code,
                                "placeName": str(one.get("placeName") or ""),
                                "nameZh": nameZh, "reason": reason})
            continue
        # ⚠️ 走 updateTableGeneral（纯 UPDATE），**不是 upsert**：
        #   upsert 有 INSERT 语义，要带齐 pb_place 的 NOT NULL 身份列
        #   （placeCode / placeName），一漏就是 `NOT NULL constraint failed`
        #   —— 与 fix_placeholder_geo._clearPlaceName 同一个坑。
        # ⚠️ `updateTableGeneral` 出错返回 **0 而不是负数**（见它的实现），
        #   所以只能按「> 0 才算真写了」计数；把 0 记成"已补"会让统计
        #   看着对、实际没写，而表现是「界面上的中文名还是英文」。
        rtn = sqliteCommon.updateTableGeneral(
            "pb_place", "placeCode = %s", (code,),
            {"nameZh": str(nameZh), "modifyYMDHMS": now})
        if rtn and rtn > 0:
            filled += 1
            if int(one.get("source") or 0) == 1:
                manualFilled += 1
        else:
            _LOG.error("rebuildNameZh: 写 nameZh 失败（影响 0 行）placeCode=%s", code)

    out = {"candidates": len(rows), "filled": filled, "manualFilled": manualFilled,
           "byReason": byReason, "samples": samples, "dryRun": bool(dryRun),
           "provider": _PROVIDER, "ready": bool(_ZH_READY),
           "elapsed": round(time.time() - started, 2)}
    if filled and not dryRun:
        # ⚠️ 必须让 placeStore 的 placeName->nameZh 快照失效：
        #   否则「同一个进程里跑完回填、紧接着调 /api/photos」看到的还是旧值，
        #   而库里已经是新的 —— 排障时会得出「回填没生效」的**错误结论**。
        from processor.place import placeStore as placeStore
        placeStore.nameZhMap(refresh=True)
    _LOG.info("rebuildNameZh: %s", {k: v for k, v in out.items() if k != "samples"})
    return out


# ============================================================
# 六、自检
# ============================================================
def _selfCheck(rebuild: bool = False) -> int:
    """python placeNameZh.py [--rebuild] —— 打印状态；`--rebuild` 实跑回填。

    ⚠️ **默认只报不改**（与 tools/fix_placeholder_geo.py 同一纪律）：
       改生产数据必须显式加 `--rebuild`。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("placeNameZh.py _VERSION:", _VERSION)
    print("PLACE_ZH_ENABLED      :", basicSettings.PLACE_ZH_ENABLED)
    print("数据文件              :", _geoFilePath())
    print("库                    : %s" % (sqliteCommon.dbFilePath() or "(未连接)"))
    print("")
    info = warmUp()
    print("数据源                : %s（%.3f s）"
          % (info.get("text", REASON_TEXT.get(info.get("reason"), "?")), info["seconds"]))
    if not info.get("ready"):
        print("  -> nameZh 全为 NULL，界面回退 placeName 英文（降级，不报错）")
        return 0

    # 映射表覆盖：34 个省级必须一个不缺
    covered = set(PROVINCE_EN_ZH.values())
    missing = [one for one in PROVINCE_ZH_ALL if one not in covered]
    print("省级映射表            : %d 个键 -> %d 个省级；缺 %s"
          % (len(PROVINCE_EN_ZH), len(covered), missing or "无"))
    print("坏多边形              : %d 个" % _BAD_GEOMETRY)

    report = rebuildNameZh(dryRun=not rebuild)
    print("")
    print("---- rebuildNameZh（%s）----"
          % ("实跑" if rebuild else "dry-run，未写库"))
    print("  候选行（nameZh 为空）  : %d" % report["candidates"])
    print("  将补中文名             : %d（其中手工行 %d）"
          % (report["filled"], report["manualFilled"]))
    print("  reason 分布            :")
    for reason, count in sorted(report["byReason"].items(), key=lambda kv: -kv[1]):
        print("      %-18s %4d   %s" % (reason, count,
                                       REASON_TEXT.get(reason, "(未登记的 reason)")))
    if report["samples"]:
        print("  样例                  :")
        for one in report["samples"]:
            print("      %-44s %s" % (str(one.get("placeName") or "")[:44],
                                       one.get("nameZh") or "(NULL) "
                                       + one.get("reasonText", "")))
    if not rebuild:
        print("\n[dry-run] 未写库。确认无误后加 --rebuild 实跑。")
    else:
        print("\n已回填。GET /api/places 与 /api/photos 现在会带中文名。")
    return 0


if __name__ == "__main__":
    sys.exit(_selfCheck(rebuild="--rebuild" in sys.argv[1:]))
