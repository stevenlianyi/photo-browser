#! /usr/bin/env python3
#encoding: utf-8

#Filename: placeFix.py
#Description: photo-browser 单张照片地点修正（DR-52）—— pb_photo.placeNameOverride 的唯一写入口
#
# 要解决的现象：逆地理 / 目录名给的地点**不一定对**（GPS 漂了几十公里、目录名只是
#   "大概那一片"）。用户看着一张照片能一眼说出"这是外婆家"，而库里没有一处能记下它。
#
# 为什么另存一列而不是改 `placeName` / `placeNameDir`
# -------------------------------------------------
#   · `placeName` 是**扫描器写的派生值**（重扫整列覆盖）；`placeNameDir` 是目录名线索。
#     人工判断混进这两列，下一次重扫 / 目录名填空都会把它**静默冲掉**（不报错）。
#   · 人工修正的生命周期与它们都不同：由用户显式改、显式撤销，不跟随任何自动流程。
#   ⇒ 另存 `placeNameOverride`，并让它进**聚合键的最优先一段**
#     （`placeStore.PLACE_KEY_SQL`）—— 地点列表 / 筛选 / 照片摘要自动跟着变。
#
# ⚠️ 「自由输入一个新地点名」还牵出第二列：`pb_place.nameSource`（DR-53）
# ---------------------------------------------------------------------
#   用户输入一个地理数据集不认识的词（「外婆家」）时，`rebuildPlaces()` 会**新建**
#   一行地点 —— 而它的 `nameZh` 是空的，下一次 `rebuildNameZh()` 就会按坐标把它
#   填成区县名，用户输入的名字**在显示层消失**（数据层却完全正常）。
#   ⇒ `applyFix()` 在 rebuild 之后把**本次新建**的那一行标成
#     `nameSource='manual'`（人工命名）→ 人工行不进 `rebuildNameZh()` 的候选集。
#   ⚠️ **只标新建的行**：已有地点一律不碰 `nameZh`/`nameSource`
#      （它可能已经有人工名，或该由地理数据集命名）。判据 = 改之前 `exists=False`。
#
# ⚠️ R13（DR-54）：「从行政区词表里选一个地名」= 口径乙，多写一列
# ------------------------------------------------------------
#   用户在词表里选『北京市 · 朝阳区』时，那一项在库里可能是 **8 个街道级 `placeName`**
#   的归并组 —— **一个字符串表达不了 8 个键**。所以：
#     · `placeNameOverride` = 规范名（`validateName` 里做归一，`北京市·朝阳区` 也认）
#     · 该行 `nameZh`       = **同一个规范名** + `nameSource='manual'`
#   ⇒ 这一行与同区县的 GPS 行在 `groupByNameZh` 下**归并成同一项**，
#     列表里「北京市 · 朝阳区」从 1501 变 1502 —— 那才是"把这张挂到整个朝阳区"。
#   ⚠️ 两个字符串必须**逐字相同**（含 `PLACE_ZH_JOINER` 的空格），否则归并不上，
#      症状是列表里出现两条看起来完全同名的项，而**哪儿都不报错**。
#   ⚠️ 预览里的数字因此是**项级**（归并后），不是键级 —— 见 `previewFix` 的说明。
#
# ⚠️ 三条「不许碰它」的纪律（与 placeNameDir / rotateDeg 之所以安全同一条理由：
#   **它是非派生列，任何自动流程都只读不写**。本步 runner.py 因此**零改动**）：
#   ① **不进** `runner._META_FULL_COLUMNS`（重扫白名单）-> 否则重扫一次全部清零；
#   ② **不进** `placeStore.REBUILD_COLUMNS`（复算列）-> 否则每轮 rebuild 覆盖；
#   ③ **不被** `dirNamePlace.scanDirNames()` 的填空逻辑碰到 -> 否则扫描收尾覆盖。
#
# ⚠️ 改完地点**必须立刻** `rebuildPlaces()`（用户明确要求，本步的核心体验）：
#   `pb_place.photoCount` 是**派生缓存**，只由 `rebuildPlaces()` 复算。不 rebuild 就会
#   出现「照片详情说华盛顿、地点列表说纽约 98 张」的自相矛盾（接口 200、两处各自自洽）。
#   所以 `applyFix()` 落库后**同步**调一次全量复算，让"提交后立刻正确"可验收
#   （**刻意不做异步后台 rebuild** —— 那会让接口先返回、列表稍后才变）。
#
# ⚠️ 不做批量（用户明确）；**不落 pb_review_log**：地点修改不改变任何识别事实
#   （不动 bbox / 质心 / shotBucket / 归属 / faceCount）且完全可逆，与 DR-43 旋转同口径。

import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/place
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from database import queryCommon as query                         # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402

from processor.place import placeNameZh as placeNameZh            # noqa: E402
from processor.place import placeStore as placeStore              # noqa: E402

_VERSION = "20261012"

_LOG = misc.setLogNew("placeFix", "placefix.log")

#: 地点名长度上限 —— 与 `pb_photo.placeNameOverride VARCHAR(128)` 逐字对齐。
#: ⚠️ 超长不截断而是 400：截断是"用户以为存了完整名字、实际被砍了"，而不报错。
MAX_NAME_LEN: int = 128

#: `current.source` 的取值（说明"现在这个地点是从哪来的"）
SOURCE_OVERRIDE: str = "override"      # 人工修正（placeNameOverride 非空）
SOURCE_DIR: str = "dir"                # 目录名（placeNameDir）
SOURCE_GPS: str = "gps"                # 逆地理（placeName）
SOURCE_NONE: str = "none"              # 三段全空（截图/人名目录类，没有地点线索）


class PlaceFixError(Exception):
    """参数非法 / 状态不允许（api 层映射成 400）。"""


# ============================================================
# 零、内部工具
# ============================================================

def _photoRow(photoCode: str) -> dict:
    """取一行 pb_photo（**含软删行** —— 修正一张已软删的照片不该 404）。查不到抛错。"""
    code = str(photoCode or "").strip()
    if not code:
        raise PlaceFixError("photoCode 不能为空")
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=code,
                                      delFlag="*", limitNum=1)
    if not rows:
        raise PlaceFixError("photoCode=%s 在 pb_photo 里不存在" % code)
    return rows[0]


def validateName(placeName):
    """外部传进来的目标地点名 -> 写入值（str 或 None）。

    None / `""`  -> None（= **恢复自动**：把 override 写回 SQL NULL）
    `"   "`（全空白）-> 抛错。⚠️ 它与 `""` 是**两件事**：前者是用户手滑，
        后者是"我就是要恢复自动"。混在一起会让"输了一串空格"变成
        "静默把用户的修正清掉"。
    超长 -> 抛错（**不截断**：截断是"用户以为存了完整名字、实际被砍了"，且不报错）。
    """
    if placeName is None:
        return None
    text = str(placeName)
    if text == "":
        return None
    stripped = text.strip()
    if not stripped:
        raise PlaceFixError("地点名不能只有空白字符（要清除修正请传空字符串或 null）")
    if len(stripped) > MAX_NAME_LEN:
        raise PlaceFixError("地点名最长 %d 个字符，收到 %d 个"
                            % (MAX_NAME_LEN, len(stripped)))
    # ⚠️ R13（DR-54）：命中**行政区规范名**时取规范写法（`北京市·朝阳区` -> `北京市 · 朝阳区`）。
    #    这个值会**同时**写进 `pb_photo.placeNameOverride` 与 `pb_place.nameZh`，
    #    两者都必须与 GPS 算出来的那份**逐字相同**，否则归并不到一起 ——
    #    症状是地点列表里出现两条看起来完全同名的项，而哪儿都不报错。
    #    归一化放在这里（而非调用点）是为了让**预览与落库**必走同一条路。
    return placeNameZh.canonicalDatasetName(stripped) or stripped


def _overrideOf(row: dict) -> str:
    """行里的 `placeNameOverride` -> 去空白后的 str 或 None（空串=未修正）。"""
    text = str((row or {}).get("placeNameOverride") or "").strip()
    return text or None


def _sourceOf(override: str, placeNameDir: str, placeName: str) -> str:
    """「现在这个地点是从哪来的」——三段优先，与聚合键**同一顺序**。"""
    if str(override or "").strip():
        return SOURCE_OVERRIDE
    if str(placeNameDir or "").strip():
        return SOURCE_DIR
    if str(placeName or "").strip():
        return SOURCE_GPS
    return SOURCE_NONE


def _nameZhOf(placeKey: str) -> str:
    """某个聚合键的中文显示名（没有就返回 ""）。"""
    if not placeKey:
        return ""
    return placeStore.nameZhOfPlace(str(placeKey)) or ""


def _existsOf(placeKey: str) -> dict:
    """该聚合键在 `pb_place` 里是否已有行（未软删）+ 它的中文名。

    ⚠️ 只看行是否存在，不看 `photoCount` —— 张数用 `_photoCountOf` **现算**。
    """
    if not placeKey:
        return {"exists": False, "nameZh": ""}
    if not query.tableExists("pb_place"):
        return {"exists": False, "nameZh": ""}
    rows = query.selectList(
        "SELECT g.placeName AS placeName, g.nameZh AS nameZh FROM pb_place g"
        " WHERE g.delFlag = %s AND g.placeName = %s LIMIT 1",
        (comGD.DEL_FLAG_NO, str(placeKey)))
    if not rows:
        return {"exists": False, "nameZh": ""}
    return {"exists": True, "nameZh": str(rows[0].get("nameZh") or "")}


def _photoCountOf(placeKey: str) -> int:
    """某聚合键**当前**的照片数（**实时**算，不读 `pb_place.photoCount` 缓存）。

    ⚠️ 为什么不读字典缓存：`applyFix()` 落库后会立刻 `rebuildPlaces()`，
       预览里"改后会变成 N"必须与**重建之后**的值一致。缓存可能是上一次复算的
       快照（扫描新照片后没 rebuild），拿它预测会给出提交后对不上的数字。
    """
    return _photoCountOfKeys([placeKey]) if placeKey else 0


def _photoCountOfKeys(keys: list) -> int:
    """**一组**聚合键的照片数（实时，一条 `IN (...)`）。

    ⚠️ R13（DR-54）：用户从行政区词表里选『北京市 · 朝阳区』时，那一项在库里
       是 **8 个街道级键的归并组** —— 预览必须报**组内总数**（列表里显示的就是它），
       只数其中某一个键会给一个偏小的数，于是弹窗与地点列表对不上。
    """
    out = sorted({str(one).strip() for one in (keys or []) if str(one or "").strip()})
    if not out:
        return 0
    key = placeStore.placeKeySql("p")
    marks = ", ".join(["%s"] * len(out))
    return int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p"
        " WHERE p.delFlag = %s AND " + key + " IN (%s)" % marks,
        tuple([comGD.DEL_FLAG_NO] + out), default=0) or 0)


# ============================================================
# 一、预览（**只读，一行都不写**）
# ============================================================

def previewFix(photoCode: str, placeName=None) -> dict:
    """这张照片的地点改成 `placeName` 之后会变成什么样。**纯读**。

    参数 placeName：目标地点名；**None / "" = 恢复自动**（回到目录名/GPS 的自动值）。

    ⚠️⚠️ 「键」与「项」是两个层级（R13 · DR-54），本函数的数字一律是**项级**
    -----------------------------------------------------------------
      用户在地点列表里看到的**一项** = 同一个 `nameZh` 的若干**键**归并后的结果
      （实测「北京市 · 朝阳区」= 8 个街道级 `placeName`）。所以：
        · `target` 描述**键**（行是否存在、这个键自己有多少张）
        · `counts` 描述**项**（归并后那一项现在多少张、改完变多少）
      只报键级的后果：弹窗说「0 → 1」，地点列表说「1501 → 1502」——
      两处各自都自洽、都不报错，用户只会觉得哪里坏了。

    字段分工：
      current.source     : override / dir / gps / none —— 现在这个地点从哪来
      autoPlaceKey       : 清掉 override 后会回到哪里（前端显示「自动：纽约」）
      target.isDistrict  : 目标是**行政区规范名**（R13）-> 落库时会同时写 `nameZh`，
                           于是它与同区县的行**归并成一项**
      target.groupKeys   : 那一项当前的成员键（>1 时前端可提示"将并入 N 处"）
      counts.from/to     : 两个**项**的张数与"改后"张数
      sameKey            : 目标键 == 当前键（打开弹窗预填时的第一眼状态）——
                           张数不变，语义是"把当前地点**记为人工修正**"
      sameGroup          : 键变了、但**项没变**（从朝阳区的某个街道键改成
                           『北京市 · 朝阳区』）-> 张数一个都不动，不能报 -1/+1
      fromBecomesEmpty   : from 那一项改完会变成 0 张（前端据此提示一句）
    """
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    relPath = str(row.get("relPath") or "")
    placeNameNow = str(row.get("placeName") or "")
    placeDirNow = str(row.get("placeNameDir") or "")
    overrideNow = _overrideOf(row)

    # ⚠️ 两个口径都从**同一把键**算出来（placeStore.placeKeyOf），别在这里另写一遍
    #    优先级：那正是"接口返回的地点"与"聚合/筛选用的地点"分叉的起点。
    autoKey = placeStore.placeKeyOf(placeNameNow, placeDirNow)
    currentKey = placeStore.placeKeyOf(placeNameNow, placeDirNow, overrideNow)

    writeValue = validateName(placeName)                     # None = 恢复自动
    targetKey = autoKey if writeValue is None else writeValue
    changed = (writeValue != overrideNow)

    # ---- 归并项：目标与当前各自属于"哪一项"（见 docstring 的键/项分层）----
    # ⚠️ 目标若是行政区规范名，落库时 `nameZh` 会被写成这个规范名（口径乙）
    #    => 它进的就是"这个区县那一项"，哪怕那一项当前的成员里没有这个键。
    targetCanonical = placeNameZh.canonicalDatasetName(targetKey)
    targetKeys = _groupKeysOf(targetKey, targetCanonical or _nameZhOf(targetKey))
    currentKeys = _groupKeysOf(currentKey, _nameZhOf(currentKey))

    exists = _existsOf(targetKey)
    currentCount = _photoCountOfKeys(currentKeys)
    targetCount = _photoCountOfKeys(targetKeys)
    # ⚠️ "同一项"不等于"同一个键"，也**不是**"两个键集相等"。
    #    从朝阳区的某个街道键改成『北京市 · 朝阳区』时：键集确实多了一个（那个键
    #    在改之前还不存在，是这次才建出来的），但**用户看到的那一项没变** ——
    #    两张照片改前改后都在「北京市 · 朝阳区」这一项里。所以判据只能是
    #    「两侧的**组名**（= 界面上的那一项）是否相同」；用键集判会得出
    #    "不同项 -> 报 -1/+1"，于是列表里那一项纹丝不动、弹窗却说掉了一张。
    currentGroupName = _nameZhOf(currentKey)
    targetGroupName = (targetCanonical or _nameZhOf(targetKey)) or ""
    sameGroup = bool(str(currentGroupName or "").strip()) \
        and str(currentGroupName).strip() == str(targetGroupName).strip()
    sameKey = bool(currentKey) and currentKey == targetKey
    # ⚠️ "这张照片会不会换项"= `sameKey or sameGroup`：
    #    · 同一个键 -> 当然不换项（⚠️ 它**不蕴含** `sameGroup`：无 nameZh 的键
    #      `currentGroupName` 是空串，`sameGroup` 判定为假 —— 单看 `sameGroup`
    #      会让"预填当前键"这种第一眼状态报出 from 0 / to +1 的假变化）
    #    · 键变了但组名相同 -> 也不换项（从街道键换成区县规范名）
    keepsItem = sameKey or sameGroup
    if keepsItem:
        fromAfter = toAfter = currentCount
    else:
        fromAfter = max(0, currentCount - 1)
        toAfter = targetCount + 1

    return {
        "photoCode": code,
        "relPath": relPath,
        "changed": bool(changed),
        "restoreAuto": bool(writeValue is None),
        #: 目标键 == 当前键：**什么都没挪**，只是要把这个键显式写进 `placeNameOverride`
        #: （语义 = "这张就是这个地点，以后重扫 / 逆地理别再改它"）。
        #: 打开弹窗时会预填当前键，所以这是**第一眼**看到的状态 —— 必须与
        #: "并进同一项"（`sameGroup`，键变了项没变）分开说，否则用户读到一句
        #: 莫名其妙的"两边是同一项"（而他根本没改任何东西）。
        "sameKey": bool(sameKey),
        "sameGroup": bool(sameGroup),
        "current": {"placeKey": currentKey or None,
                    "nameZh": _nameZhOf(currentKey) or None,
                    "source": _sourceOf(overrideNow, placeDirNow, placeNameNow)},
        "target": {"placeKey": targetKey or None,
                   "nameZh": (exists.get("nameZh") or None) or None,
                   "exists": bool(exists.get("exists")),
                   #: 这个**键**自己有多少张（排障用；界面上的数字请看 counts）
                   "ownPhotoCount": _photoCountOf(targetKey),
                   #: 这一**项**（归并组）现在有多少张
                   "photoCount": int(targetCount),
                   "isDistrict": bool(targetCanonical),
                   #: 改完之后这一项会有多少个键（含即将新建的那个）
                   "groupKeys": list(targetKeys),
                   "groupRowCount": len(targetKeys),
                   #: 这一项**当前**有多少个键（不含还没建出来的目标键）——
                   #: 界面文案要说"当前由 N 处归并而成"时用这个，别用上面那个
                   #: （差 1 会让用户数不上：列表里明明 8 处，弹窗说 9 处）
                   "groupExistingRowCount": len([one for one in targetKeys
                                                 if one != targetKey])},
        "autoPlaceKey": autoKey or None,
        #: 自动值的**显示名**（「自动：华盛顿」那一行）—— 后端给，前端别再自己查表：
        #: 候选词表在前端只覆盖"库里有行 + 数据集"，两者都不含的键会被显示成英文，
        #: 而那一行恰恰是用户判断"清空会回到哪里"的唯一依据。
        "autoNameZh": _nameZhOf(autoKey) or None,
        #: ⚠️ 每一侧都带 `nameZh`：`photoCount` 是**项级**（归并组），
        #:    那么这一项的**显示名**也必须是组级的那一个（= GPS 行算出来的区县名），
        #:    否则界面会出现「北京市 · 西城区 2566 → 2565」这种"名字与数字不同源"的读法。
        "counts": {"from": {"placeKey": currentKey or None,
                            "nameZh": _nameZhOf(currentKey) or None,
                            "photoCount": int(currentCount), "after": int(fromAfter)},
                   "to": {"placeKey": targetKey or None,
                          "nameZh": (targetCanonical or _nameZhOf(targetKey)) or None,
                          "photoCount": int(targetCount), "after": int(toAfter)}},
        # ⚠️ 只对"真的会掉到 0"报 True：不换项时张数不动，绝不误报。
        "fromBecomesEmpty": bool(changed and currentKey and not keepsItem
                                 and fromAfter <= 0),
        "note": ("修正的是**这张照片的地点**（进 `placeNameOverride`）：原值都不改写，"
                 "随时可「恢复自动」；提交后会自动重建地点字典，让张数立刻正确。"),
    }


def _groupKeysOf(placeKey: str, groupName: str) -> list:
    """`placeKey` 所属的归并项成员键（`groupName` 为空时只有它自己）。

    ⚠️ 两处细节，都是"预览与列表对不上"的入口：
      ① 目标键**可能还没有行**（第一次把照片放到某个区县）-> 必须把它自己补进去，
         否则 `IN (...)` 里只有别的键，算出来的数会少这一张的归宿；
      ② 列表用的是 `api/place.groupByNameZh` 的**同一份规则**（同 `nameZh` 并一项，
         `nameZh` 为空各自成项），所以这里直接用 `placeStore.nameGroupKeys`。
    """
    key = str(placeKey or "").strip()
    keys = []
    if str(groupName or "").strip():
        keys = [one for one in placeStore.nameGroupKeys(groupName) if one]
    if key and key not in keys:
        keys.append(key)
    return keys


# ============================================================
# 二、落库 + 自动重建地点字典
# ============================================================

def _writeOverride(photoCode: str, value) -> int:
    """写 `pb_photo.placeNameOverride`。

    ⚠️ 必须走 `updateTableGeneral`：值**原样**进参数元组，None 就是 SQL NULL
       ——「恢复自动」写的就是 NULL。走 upsert 的话整批为 None 的列会被
       normalizeDataSet 丢掉，「恢复自动」就变成一次**静默空操作**（同 photoTimeFix）。
    """
    rtn = sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", (str(photoCode),),
        {"placeNameOverride": value, "modifyYMDHMS": misc.getTime()})
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise PlaceFixError("pb_photo.placeNameOverride 写入失败: %s"
                            % sqliteCommon.dbHandle().lastErrMsg)
    if rtn is not None and rtn < 0:
        raise PlaceFixError("pb_photo.placeNameOverride 写入返回 %r" % (rtn,))
    return rtn


def _markManualNamed(placeKey: str, nameZh=None) -> bool:
    """把聚合键对应的地点行标成**人工命名**（`nameSource='manual'`）。

    `nameZh`
    --------
      空（默认）—— 只写 `nameSource`。此时 `nameZh` 该显示的就是 `placeName` 本身
        （用户输入的名字已经**就是**聚合键），再抄一遍是纯冗余，而且会让
        「中文显示名」这一列混进英文键。界面的回落（`nameZh || placeName`）给出同一个字。
      非空（**R13 · DR-54 的口径乙**）—— 同时写 `nameZh`。两个场景：
        ① **行政区规范名**（用户在词表里选了『北京市 · 朝阳区』）：写进去之后，
           它与**同区县**的 GPS 行在 `groupByNameZh` 下**归并成同一项** ——
           这正是"把这张挂到整个朝阳区"的语义（一次选一个键表达不了 8 个街道键）；
        ② 自愈：库里若已有一行"中文键、但 `nameZh` 为空"的地点（手工造过），
           顺手补上，让它回到可归并状态。
        ⚠️ **必须与 `pb_photo.placeNameOverride` 里的那个字符串逐字相同**
           （含 `PLACE_ZH_JOINER` 的空格），否则 GPS 算出来的那份归并不上 ——
           所以拼串规则只有一处（`validateName` 里的 `canonicalDatasetName`）。

    为什么必须有人做这件事（DR-53）：新建的地点行只可能来自这次的手工输入，
      它的 `nameZh`/`nameSource` 都是空 —— 于是下一次 `rebuildNameZh()`
      （`POST /api/places/rebuild` 或一次扫描收尾）会按坐标把它填成区县名，
      用户输入的名字**在显示层消失**，而接口一路 200。
    """
    code = placeStore.makePlaceCode(str(placeKey or ""))
    if not code:
        return False
    values = {"nameSource": placeNameZh.NAME_SOURCE_MANUAL,
              "modifyYMDHMS": misc.getTime()}
    if str(nameZh or "").strip():
        values["nameZh"] = str(nameZh).strip()
    rtn = sqliteCommon.updateTableGeneral(
        "pb_place", "placeCode = %s", (code,), values)
    if rtn == -2 or (rtn is not None and rtn < 0):
        _LOG.warning("placeFix: 标记人工命名失败 placeCode=%s: %s",
                     code, sqliteCommon.dbHandle().lastErrMsg)
        return False
    ok = bool(rtn and rtn > 0)
    if ok and "nameZh" in values:
        # ⚠️ 显示名的进程内快照必须失效：否则"同一个进程里改完、紧接着查接口"
        #    看到的还是旧值（照片列表的 placeZh 依旧名），而库里已经是新的 ——
        #    排障时会得出「归并没生效」的错误结论。`rebuildNameZh` 也是这么做的。
        placeStore.nameZhMap(refresh=True)
    return ok


def applyFix(photoCode: str, placeName=None) -> dict:
    """改成 `placeName`（None / "" = 恢复自动）并**自动重建地点字典**。

    返回 = `previewFix` 的全部字段 + `rebuildSummary`（复算统计）
         + `rebuildElapsedMs`（全量复算耗时，毫秒）。
    """
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    overrideNow = _overrideOf(row)
    writeValue = validateName(placeName)
    #: 目标聚合键 + 「改之前这个地点在库里有没有行」—— 决定③要不要标人工命名
    targetKey = writeValue if writeValue is not None else placeStore.placeKeyOf(
        row.get("placeName"), row.get("placeNameDir"), None)
    targetExisted = bool(_existsOf(targetKey).get("exists")) if targetKey else False

    # 幂等：值没变就什么都不做（**不写库、不 rebuild**）——
    # 重复点一次「修正」不该多跑一遍全量复算（那是几万行的一次 GROUP BY）。
    if writeValue == overrideNow:
        preview = previewFix(code, placeName)
        preview["changed"] = False
        preview["rebuildSummary"] = None
        preview["rebuildElapsedMs"] = 0
        preview["note"] = "目标地点与当前值相同，没有改动（幂等）"
        return preview

    # ---- ① 写 override（唯一那个写点）----
    _writeOverride(code, writeValue)

    # ---- ② 立刻全量复算地点字典（用户明确要求：提交后计数立刻正确）----
    # ⚠️ 同步、不做"异步后台 rebuild"：见文件头。幂等（不清表、按 placeCode upsert）。
    started = time.time()
    summary = placeStore.rebuildPlaces()
    elapsedMs = int(round((time.time() - started) * 1000))

    # ---- ③ 人工命名的地点行（DR-53）+ 行政区规范名同时写 nameZh（R13 · DR-54）----
    # ⚠️ 必须在 rebuild **之后**：那一行是 rebuild 刚创建出来的。
    namedManual = datasetNamed = False
    if writeValue is not None and targetKey:
        canonical = placeNameZh.canonicalDatasetName(targetKey)
        if canonical:
            # 口径乙：不区分"行是否本来就存在" —— 这个键是规范名，就该带上 nameZh，
            # 否则它永远归并不进同区县那一项（行为上是"看不见的"错误）。
            namedManual = _markManualNamed(targetKey, nameZh=canonical)
            datasetNamed = namedManual
        elif not targetExisted:
            namedManual = _markManualNamed(targetKey)

    out = previewFix(code, placeName)
    out["changed"] = True
    out["rebuildSummary"] = {"created": int(summary.get("created") or 0),
                             "updated": int(summary.get("updated") or 0),
                             "zeroed": int(summary.get("zeroed") or 0),
                             "placeCount": int(summary.get("placeCount") or 0),
                             "newPlaceNamedManual": bool(namedManual),
                             "datasetNamed": bool(datasetNamed)}
    out["rebuildElapsedMs"] = elapsedMs
    _LOG.info("地点修正 %s：override %r -> %r，重建字典 created=%d updated=%d "
              "zeroed=%d，人工命名=%s，行政区规范名=%s，耗时 %d ms",
              code, overrideNow, writeValue,
              out["rebuildSummary"]["created"], out["rebuildSummary"]["updated"],
              out["rebuildSummary"]["zeroed"], namedManual, datasetNamed, elapsedMs)
    return out


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("placeFix.py _VERSION:", _VERSION)
    print("改的列      : pb_photo.placeNameOverride（None=NULL，最长 %d 字符）"
          % MAX_NAME_LEN)
    print("聚合键      : 人工修正 > 目录名 > GPS（placeStore.placeKeySql）")
    print("联动        : ①写 override -> ②rebuildPlaces()（同步全量复算）")
    print("日志        : 不落 pb_review_log（不改变任何识别事实，见文件头）")
    print("库          :", sqliteCommon.dbFilePath())
