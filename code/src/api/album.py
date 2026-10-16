#! /usr/bin/env python3
#encoding: utf-8

#Filename: album.py
#Description: 文件夹浏览接口（R16 · DR-59~63）—— /api/albums* 整个命名空间
#
# 一个端点（**纯读，本模块一个字都不写**）
# ----------------------------------------
#   GET /api/albums/path    目录树 + 当前位置（面包屑 / 直属张数 / 递归张数）
#
# 为什么单独成模块（照 api/place.py 的先例）
# -----------------------------------------
#   ① 「同一命名空间只在一个模块注册」：避免 FastAPI 的静默遮蔽
#      （后注册的同路径路由永远不会被调用，而 /openapi.json 里两条都「在」）；
#   ② `browse.py` 的文件头纪律是「**整个模块一个字都不写**」—— 本端点只读，
#      与那条纪律相容，但「文件夹」是一个自成一体的命名空间，
#      收在一个模块里才好一起读（同一个理由已经把 `/api/places*` 搬去了 place.py）；
#   ③ `test_api_smoke` 会逐条扫 OpenAPI 断言「无 export / 无 DELETE」，
#      本模块不提供任何写端点，那条闸门照常通过。
#
# ⚠️ 图片列表**不在这里**：它挂在既有的 `GET /api/photos?dirPath=`（api/browse.py）
#    -----------------------------------------------------------------
#   这样直接复用 `photoSummary()` 的统一形状、排序白名单与全部筛选，
#   **尤其是锚点定位 `anchorOffsetOf()`** —— 而锚点定位正是「从目录里点开一张
#   **老**照片后左右翻页」必需的东西（那一批按 `takenAt DESC` 分页，
#   老照片可能排在第 2 页之后；不带锚点会让两个箭头全禁用且不报错）。
#   本模块只回答「有哪些目录、各有多少张、我在哪」。
#
# ⚠️ 目录与张数**全部现推自 `pb_photo.relPath`**（DR-60）
# --------------------------------------------------------
#   不落表、不新增列、不需要迁移 ⇒ 没有「派生缓存与真值不一致」这类问题
#   （对照 `pb_place.photoCount` 与 DR-33 的幽灵点）。
#   连带好处：本模块**对 `photo/` 目录的访问次数为 0** —— 既不 listdir 也不 stat，
#   与「原图只读」铁律天然相容（不是"记得别写"，是没有写的入口）。
#
# ⚠️ 一次聚合，不逐层查询
# ----------------------
#   `relPath` 上**没有索引**（pb_photo 的 6 个索引不含它），而目录键是**列上的
#   函数** ⇒ 有索引也用不上。所以整棵树由**一条 `GROUP BY`** 拿回「不同目录 ->
#   张数」这一小批行（几十~几百行），再在 Python 里一次性组装父子关系与各层张数；
#   请求内部不再二次查库。复杂度 O(N)，内存 O(不同目录数)。

import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import dto                                               # noqa: E402
from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as comBS                         # noqa: E402
from database import queryCommon as query                         # noqa: E402
from processor.album import dirPath as dirPath                    # noqa: E402

from fastapi import APIRouter, Query                              # noqa: E402

_VERSION = "20261016"

_LOG = misc.setLogNew("apiAlbum", "apialbum.log")

router = APIRouter(tags=["album"])

#: 根目录在界面上的**中性显示名**。
#: ⚠️ 刻意不用磁盘绝对路径（`d:\PhotoLib\photo`）：那是**本机实现细节**，
#:   写进响应契约就会让前端的展示分支跟着环境变；换机器/换盘符时
#:   界面上会出现一个"看起来像配置错误"的字符串。
#:   —— 与 `placeStore` 那边「`placeName` 是聚合键、`nameZh` 是显示名」同一个思路：
#:   对外只给**人看**的那一层。
ROOT_DISPLAY_NAME: str = "照片库"


# ============================================================
# 一、一次聚合（唯一一次查库）
# ============================================================

def dirCountsOf() -> dict:
    """全库「目录键 -> **直属**张数」（只统计未软删）。

    ⚠️ `direct` 是关键：本功能的口径是「仅当前目录直属」（用户确认过），
       子目录的照片由树上的下一层负责 —— 这也与资源管理器一致。
    ⚠️ `delFlag` 走**参数绑定**（不拼字面量）；软删照片不进任何一层计数。
    ⚠️ `GROUP BY` 用的是**别名** `dirKey`：`queryCommon.checkColumns()` 会先把
       `AS xxx` 抽成别名再放行（`_aliasesOf`），所以别名可用 ——
       `browse.py` 的 `_yearMonthIndex`（`... AS ym ... GROUP BY ym`）是既有先例。
       这样表达式只写一遍，比在 `GROUP BY` 里再抄一次 `rtrim(...)` 安全
       （抄错一处就是「树说 45 张、网格说 44 张」那种两边各自自洽的错）。
    """
    key = dirPath.dirKeySql("p")
    out = {}
    for row in query.selectList(
            "SELECT " + key + " AS dirKey, COUNT(*) AS cnt FROM pb_photo p"
            " WHERE p.delFlag = %s GROUP BY dirKey", (comGD.DEL_FLAG_NO,)):
        out[str(row.get("dirKey") or "")] = int(row.get("cnt") or 0)
    return out


# ============================================================
# 二、扁平计数 -> 嵌套树
# ============================================================

def buildTree(counts: dict) -> tuple:
    """`{目录键: 直属张数}` -> `(rootNode, nodesByKey)`。

    两步，都在内存里：
      ① **补中间层**：某目录的键可能**没有直属照片**（照片全在更深一层），
         于是它自己不在聚合结果里 —— 不补祖先，`a/b/c` 就会在树上**凭空消失**
         （父 `a/b` 不存在），用户看到的是「有些目录点不到」，而接口 200、数据全在。
      ② **自底向上累计** `totalCount`（递归张数）。

    每个节点给**两个**张数，界面上要同时能说清（这是本功能最容易让人误判的地方）：
      · `count`      = **直属**张数（树节点上显示的就是它）
      · `totalCount` = 该目录**含所有子目录**的张数
    只给 `count` 时，`2013美国游` 显示 3（而它下面三个日期子目录各几十张），
    用户会以为「少报了」—— 所以两个数都要有，界面负责把「直属」说出来。
    """
    direct = {str(k): int(v) for k, v in (counts or {}).items()}
    direct.setdefault("", 0)

    keys = set()
    for one in direct:
        keys.update(dirPath.ancestorsOf(one))
    keys.add("")

    nodes = {}
    for key in keys:
        own = int(direct.get(key, 0))
        nodes[key] = {"name": dirPath.dirNameOf(key) or ROOT_DISPLAY_NAME,
                      "path": key,
                      "count": own,
                      "totalCount": own,
                      "hasChildren": False,
                      "children": []}

    # 由深到浅：这样在处理某个键时，它所有后代的 totalCount 都已经算完了。
    for key in sorted(keys, key=lambda one: len(dirPath.ancestorsOf(one)),
                      reverse=True):
        if not key:
            continue
        parent = dirPath.parentKeyOf(key)
        parentNode = nodes.get(parent)
        if parentNode is None:                       # 不可能：ancestorsOf 保证了祖先都在
            _LOG.warning("目录树组装异常：%r 的父键 %r 不在节点集里", key, parent)
            continue
        parentNode["hasChildren"] = True
        parentNode["totalCount"] += nodes[key]["totalCount"]
        parentNode["children"].append(nodes[key])

    # ⚠️ 排序只在 Python 侧做（**不写进 SQL 的 ORDER BY**）：目录名大量以日期开头
    #    （`2013.07.26 华盛顿`），纯字符串序会把 `2013.10.01` 排在 `2013.7.1` 前面。
    #    口径与实现都在 `dirPath.nameSortKey`（本项目唯一一份）。
    for one in nodes.values():
        one["children"].sort(key=lambda node: dirPath.nameSortKey(node["name"]))
    return nodes[""], nodes


# ============================================================
# 三、GET /api/albums/path
# ============================================================

def _crumbNodesOf(current: str, nodes: dict) -> list:
    """当前位置 -> 面包屑（由外向内，含根目录）。

    ⚠️ 路径不存在时**照样给完整面包屑**（各层张数为 0），而不是返回空数组：
       用户在旧链接/目录改名后点进来，看到的应该是「照片库 / 2013美国游 / 老名字
       （0 张）」+ 一句说明，而不是一个只剩标题的空页面 —— 后者会被读成"页面坏了"。
    """
    out = []
    for key in dirPath.ancestorsOf(current):
        node = nodes.get(key)
        out.append({"name": (node or {}).get("name")
                    or dirPath.dirNameOf(key) or ROOT_DISPLAY_NAME,
                    "path": key,
                    "count": int((node or {}).get("count", 0)),
                    "totalCount": int((node or {}).get("totalCount", 0))})
    return out


@router.get("/albums/path", summary="文件夹浏览：目录树 + 当前位置（只读）")
def getAlbumPath(path: str = Query(
        default=None,
        description="目录键（相对照片库根，`/` 分隔）；省略或传 %r 表示根目录"
                    % comBS.ALBUM_ROOT_TOKEN)):
    """P-11「文件夹」页的全部数据源（**只读**，本端点不写任何一行）。

    返回什么
    --------
      · `path` / `pathName`：当前目录（已归一）；
      · `pathExists`：这个目录在库里**有没有照片**（含只有子目录的情形）。
        为 false 时前端给「这个目录在库里已经没有照片了」而不是白屏；
      · `breadcrumb[]`：由外向内的路径，每段可点击跳回；
      · `root`：**整棵目录树**（嵌套，每个节点带 `count` 直属 / `totalCount` 递归）。
        为什么一次给全树而不是「只给当前层的子目录」：本页的核心动作是
        **逐级下钻**，而"切换目录"是最高频操作。只给当前层的话，展开任意一层
        都要再发一次请求，且**展开状态会在重取时丢掉**（用户会看到树自己合上）。
        代价是响应体 O(不同目录数)（每个节点约 90 字节 JSON），
        本项目正式库在**几百**这一档 —— 超过 `ALBUM_TREE_MAX_DIRS` 会记 warning
        （**只警告、不截断**：静默砍掉一半树等于静默丢数据）；
      · `directCount` / `photoTotal` / `dirTotal`：三个计数，用于页脚与空状态
        （「这个目录没有直属图片」与「库里根本没有照片」是两种完全不同的空状态，
        只给一个 0 是分不出来的）；
      · `note`：有话说时的一句人话（例如路径不存在），没有则为 null。

    ⚠️ 根目录为什么要有**保留 token**
       `path` 不传（None）等价于根目录，但**根目录也会被显式传成 token** ——
       因为空串在 HTTP query 上**传不过去**（前端 `api/request.js` 的
       `paramsSerializer` 会丢弃 `''`），见 `basicSettings.ALBUM_ROOT_TOKEN`。
       两种写法这里都收，归一后都是空串。
    """
    reason = dirPath.invalidReason(path)
    if reason:
        # 格式非法的键 100% 匹配不到任何数据；返回一个"空目录"比 400 更让人困惑
        # （用户会去检查是不是照片被删了）。由 api 层决定错误码 ——
        # processor 层不许依赖 api/dto，所以那边只给 `reason`。
        raise dto.ApiError(dto.CODE_PARAM_INVALID, reason)

    current = dirPath.normalizeDirKey(path)
    started = time.perf_counter()
    counts = dirCountsOf()
    root, nodes = buildTree(counts)
    elapsedMs = round((time.perf_counter() - started) * 1000.0, 2)

    currentNode = nodes.get(current)
    photoTotal = sum(counts.values())
    dirTotal = len(counts)
    # 只在 debug 级打点：树端点会被"点目录"反复调用，info 级会把日志刷满；
    # 且**不打印路径集合**（几百上千条会淹没日志，要查树内容看接口响应即可）。
    _LOG.debug("albums/path path=%r exists=%s dirs=%d photos=%d 耗时=%.2fms",
               current, currentNode is not None, dirTotal, photoTotal, elapsedMs)
    if dirTotal > int(comBS.ALBUM_TREE_MAX_DIRS):
        _LOG.warning("目录数 %d 超过 ALBUM_TREE_MAX_DIRS(%d)：响应体会随之变大，"
                     "该考虑改成逐层懒加载了（当前仍**原样返回全部目录**，不截断）",
                     dirTotal, comBS.ALBUM_TREE_MAX_DIRS)

    note = None
    if currentNode is None:
        note = ("这个目录在库里已经没有照片了：可能是照片被软删、目录被改名，"
                "或者这批照片还没扫描入库")
    return {"ok": True,
            "path": current,
            "pathName": (currentNode or {}).get("name")
                        or dirPath.dirNameOf(current) or ROOT_DISPLAY_NAME,
            "pathExists": currentNode is not None,
            "breadcrumb": _crumbNodesOf(current, nodes),
            "root": root,
            "directCount": int((currentNode or {}).get("count", 0)),
            "subDirCount": len((currentNode or {}).get("children", [])),
            "directTotalCount": int((currentNode or {}).get("totalCount", 0)),
            "photoTotal": int(photoTotal),
            "dirTotal": int(dirTotal),
            "elapsedMs": elapsedMs,
            "note": note}


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("album.py _VERSION:", _VERSION)
    print("路由:", [(one.path, sorted(one.methods)) for one in router.routes])
    print("根目录显示名:", ROOT_DISPLAY_NAME,
          "｜保留 token:", comBS.ALBUM_ROOT_TOKEN)
    # 组装逻辑的**离线自检**（不查库）：用一批手造计数验证「补中间层 +
    # 递归张数 + 数字感知排序」三件事。
    demo = {"2013美国游": 3,
            "2013美国游/2013.07.10": 40,
            "2013美国游/2013.7.2": 7,
            "BaiRuiQin": 452}
    root, nodes = buildTree(demo)
    print("---- 组装自检 ----")
    print("  根: count=%d totalCount=%d" % (root["count"], root["totalCount"]))
    for child in root["children"]:
        print("  ├ %-24s count=%-4d totalCount=%-4d 子=%d"
              % (child["name"], child["count"], child["totalCount"],
                 len(child["children"])))
    print("  nodes:", sorted(nodes.keys()))
    assert root["totalCount"] == sum(demo.values()), "递归张数必须等于全库合计"
    assert not root["count"], "根目录没有直属照片时 count 应为 0"
    assert [one["path"] for one in root["children"]] == \
        ["2013美国游", "BaiRuiQin"], "排序应为数字感知"
    print("组装自检通过")
