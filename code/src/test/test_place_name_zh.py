#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_place_name_zh.py
#Description: 地点中文名（pb_place.nameZh · R4a / DR-28 / DR-29）的测试
#
# 覆盖
# ----
#   * **边界纪律（本步最关键）**
#       - `nameZh` 不在 `placeStore.REBUILD_COLUMNS` 里（静态断言，最便宜的守卫）
#       - `rebuildPlaces()` 跑两遍后 `nameZh` 不变
#       - 手工设的 `nameZh` 再跑 `rebuildPlaces()` 仍然不变
#       - `rebuildNameZh()` 只填 NULL，绝不覆盖已有值
#   * **中文名正确性**：省级映射表覆盖 34 个省级行政区；真实坐标判定
#   * **降级纪律**：`PLACE_ZH_ENABLED=False` / 数据源缺失 -> nameZh=None、
#     不抛错、**只记一次** warning
#   * **境外不翻译**：境外坐标 nameZh 必须是 None
#   * **筛选两套都支持**（DR-29③）：中文名与英文 placeName 返回同一批照片
#   * **回退契约**：`placeName` 保留英文原值，界面按 `placeZh ?? placeName` 显示
#   * **重���**：两个区县多边形同时覆盖 -> reason=overlap（单独一类，可计数）

import pytest


# ============================================================
# 零、装置
# ============================================================

@pytest.fixture
def zh_env(api_env):
    """api_env + 地点字典已 rebuild + **nameZh 已回填** + 补上真实中国坐标。

    ⚠️ 为什么要坐标：`nameZh` 靠 point-in-polygon 算，**没有坐标就没有中文名**。
       而 api_env 的夹具照片全是 lat/lon 为空的（原来只测 placeName 字符串），
       所以这里给两张补上北京的坐标，让中文名链路真的被跑到。
    ⚠️ 为什么不手工回填 nameZh（R4b 起）：`POST /api/places/rebuild` 现在跑的是
       **地点侧全链路**（`placeFinalize`：填目录名 → 重建字典 → **补 nameZh**），
       所以第③步已经在接口里做完了。这里再显式跑一次是**幂等自检**：
       `filled` 必须是 0（已经填过），而不是"必须 >= 1"。
       ⚠️ 纪律**没有变**：`nameZh` 仍不在 `REBUILD_COLUMNS` 里
       （`rebuildPlaces()` 一个字都不碰它，见那里的警告）；
       补它的是**另一个函数** `rebuildNameZh()`，只填 NULL、绝不覆盖。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import placeNameZh as placeNameZh

    # ⚠️ 两张照片都放在**朝阳区**（望京一带），而不是一个朝阳一个东城。
    #   原因：中文名是**按地点**算的（`pb_place` 一行 = 一个 placeName），
    #   同一 placeName 下的所有照片必然共享同一个 placeZh。
    #   早先这里给两张分别放朝阳/东城，于是 place 的中心点（两者的均值）
    #   落在第三个区县上，而用例却按「每张照片各自的区县」断言 ——
    #   测的是一个**根本不存在**的口径。
    #   要验「不同地点不同中文名」，正确做法是**再造一个东城区的 placeName**。
    for code in ("PH_2013_01", "PH_2013_07"):
        sqliteCommon.updateTableGeneral(
            "pb_photo", "photoCode = %s", (code,),
            {"lat": 39.9842, "lon": 116.4007})
    _resetZhModule()
    assert api_env["client"].post("/api/places/rebuild").json()["ok"] is True
    # 第③步已由接口完成（R4b：POST 走全链路）
    assert _nameZhOf("北京") == "北京市 · 朝阳区", _nameZhOf("北京")
    # 显式再补一次 ⇒ **幂等**（没有可填的了）
    report = placeNameZh.rebuildNameZh(dryRun=False)
    assert report["filled"] == 0, report
    assert _nameZhOf("北京") == "北京市 · 朝阳区", _nameZhOf("北京")
    return api_env


def _setNameZh(placeName: str, nameZh: str) -> int:
    from database.auto_generated import sqliteCommon as sqliteCommon
    return sqliteCommon.updateTableGeneral(
        "pb_place", "placeName = %s", (str(placeName),),
        {"nameZh": str(nameZh)})


def _nameZhOf(placeName: str):
    from database import queryCommon as query
    rows = query.selectList(
        "SELECT g.nameZh AS nameZh FROM pb_place g WHERE g.placeName = %s",
        (str(placeName),))
    return rows[0]["nameZh"] if rows else None


def byNameOf(body: dict, placeName: str):
    """从 /api/places 响应里按 placeName 取 nameZh（取不到返回 None）。"""
    for one in body.get("items") or []:
        if one.get("placeName") == placeName:
            return one.get("nameZh")
    return None


def _resetZhModule() -> None:
    """把 placeNameZh 的**进程内缓存**清干净（数据源索引 + 降级闸门）。

    ⚠️ 必须清：`_INDEX` / `_ZH_READY` 都是模块级全局，
       一个用例把它设成「不可用」之后，后面的用例会**继承**那个降级状态 ——
       症状是「单跑绿、一起跑红」，而报错信息与真因毫无关系。
    """
    from processor.place import placeNameZh as placeNameZh
    placeNameZh._INDEX = None
    placeNameZh._ZH_READY = None
    placeNameZh._ZH_WARNED = False
    placeNameZh._WARNED_ONCE = False
    placeNameZh._PROVIDER = ""
    placeNameZh._LOAD_SECONDS = 0.0


# ============================================================
# 一、边界纪律：**nameZh 绝不能被自动流程覆盖**
# ============================================================

def test_nameZhIsNotADerivedColumn():
    """`nameZh` **绝不能**在 `REBUILD_COLUMNS` 里。

    ⚠️ 这是本步**最便宜也最值钱**的一条守卫：一旦有人（可能是半年后的自己）
       觉得「中文名也是算出来的呀」而把它加进 REBUILD_COLUMNS，
       后果是每轮 rebuildPlaces() 把中文冲回英文，**而且不报错** ——
       照片张数对、接口 200、字段在，只有值变了。
       用例不依赖任何数据，纯静态断言，跑得飞快。
    """
    from processor.place import placeStore

    assert "nameZh" not in placeStore.REBUILD_COLUMNS
    # 反面也要钉住：派生集合里那六个**确实**还在（别为了加 nameZh 顺手删了别的）
    for column in ("placeName", "photoCount", "firstShotYear",
                   "lastShotYear", "centerLat", "centerLon"):
        assert column in placeStore.REBUILD_COLUMNS, column


def test_rebuildPlacesDoesNotTouchNameZh(zh_env):
    """`rebuildPlaces()` 跑两遍，`nameZh` **逐字不变**。

    ⚠️ 先手工填一个 nameZh（模拟用户填的），再跑两遍 rebuild ——
       如果 nameZh 进了派生集合，第一遍就把它冲掉了。
    """
    from processor.place import placeStore

    _setNameZh("北京", "北京市 · 我家")
    before = _nameZhOf("北京")
    assert before == "北京市 · 我家"

    placeStore.rebuildPlaces()
    assert _nameZhOf("北京") == "北京市 · 我家", "rebuild 把手工 nameZh 冲掉了"
    placeStore.rebuildPlaces()
    assert _nameZhOf("北京") == "北京市 · 我家", "第二遍 rebuild 又冲了一次"


def test_rebuildPlacesKeepsManualSourceWithNameZh(zh_env):
    """手工行（source=1）的 `source` 与 `nameZh` 都必须活过 rebuild。"""
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import placeStore

    assert sqliteCommon.updateTableGeneral(
        "pb_place", "placeName = %s", ("北京",),
        {"source": 1, "nameZh": "北京市 · 手工填的"}) > 0
    placeStore.rebuildPlaces()
    row = sqliteCommon.query_pb_place("pb_place", placeName="北京")[0]
    assert int(row["source"]) == 1, "手工行的 source 被复算打回了 0"
    assert row["nameZh"] == "北京市 · 手工填的"


def test_rebuildNameZhNeverOverwritesExisting(zh_env):
    """`rebuildNameZh()` **只填 NULL**：已算出的与手工填的都原样保留。"""
    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    _setNameZh("北京", "北京市 · 人工改过的")

    report = placeNameZh.rebuildNameZh(dryRun=True)
    # 「北京」已有 nameZh -> 不该出现在候选里
    assert report["candidates"] >= 1
    assert _nameZhOf("北京") == "北京市 · 人工改过的"
    assert "北京市 · 人工改过的" not in [
        one.get("nameZh") for one in report["samples"]]

    placeNameZh.rebuildNameZh(dryRun=False)
    assert _nameZhOf("北京") == "北京市 · 人工改过的", "rebuildNameZh 覆盖了已有值"


def test_rebuildNameZhIsIdempotent(zh_env):
    """第二遍 `rebuildNameZh()` **一条都不该再写**（不重复劳动）。

    ⚠️ 注意「候选数」不等于 0：算不出中文名的行（无坐标的上海/广州）
       **每遍都会被重新评估一遍** —— 它们永远是候选，这没问题
       （代价是几十次字典查找）。要盯的是 `filled == 0`：
       一旦第二遍又写了行，说明「只填 NULL」这条纪律破了。
    """
    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    first = placeNameZh.rebuildNameZh(dryRun=False)
    # zh_env 夹具已经填过了，所以本用例要先**清空**才能观察到「第一遍写了 N 行」
    assert first["filled"] == 0, \
        "夹具已回填过，第一遍不该再写；若这里不为 0 说明「只填 NULL」破了"
    # 无坐标的地点必须**每遍**都如实报成 noCoordinate，而不是悄悄给个空串
    assert first["byReason"].get(placeNameZh.REASON_NO_COORD, 0) >= 1, first
    assert first["filled"] == 0 and first["candidates"] >= 1, first

    # 清掉已算出的值，再跑两遍：第一遍写 N，第二遍写 0。
    # ⚠️ 用 `updateTableGeneral` 而不是一条 UPDATE —— `queryCommon` 是**只读出口**
    #   （没有 executeUpdate，这是刻意的），而 `normalizeDataSet` 会把
    #   `{"nameZh": None}` 整条丢掉，所以清空要写**空串**（`_placesWithoutNameZh`
    #   把 `nameZh = ''` 与 NULL 同等看待，正是为此）。
    from database.auto_generated import sqliteCommon as sqliteCommon
    for one in sqliteCommon.query_pb_place("pb_place", mode="light"):
        sqliteCommon.updateTableGeneral(
            "pb_place", "placeCode = %s", (str(one["placeCode"]),),
            {"nameZh": ""})
    second = placeNameZh.rebuildNameZh(dryRun=False)
    assert second["filled"] >= 1, second
    third = placeNameZh.rebuildNameZh(dryRun=False)
    assert third["filled"] == 0, third


# ============================================================
# 二、中文名正确性
# ============================================================

def test_provinceTableCoversAll34():
    """省级映射表必须覆盖全国 **34 个**省级行政区，一个不缺。"""
    from processor.place import placeNameZh as placeNameZh

    assert len(placeNameZh.PROVINCE_ZH_ALL) == 34
    covered = set(placeNameZh.PROVINCE_EN_ZH.values())
    missing = [one for one in placeNameZh.PROVINCE_ZH_ALL if one not in covered]
    assert not missing, "映射表缺 %s" % missing
    # 别把表里混进不存在的「省」（那会让「表覆盖了 34 个」变成假通过）
    unknown = sorted(covered - set(placeNameZh.PROVINCE_ZH_ALL))
    assert not unknown, "映射表里有不在 34 个之内的值：%s" % unknown


def test_provinceTableCoversRealGeoNamesSpellings():
    """映射表必须认得 **reverse_geocoder 实际在用**的每一种 admin1 写法。

    ⚠️ 为什么这条要有：`placeName` 的 admin1 段直接来自 GeoNames，
       而那个数据集里「直辖市不带 Sheng、部分省带」是不统一的。
       少认一种写法，那个省的所有地点就**静默**退回英文（reason=provinceUnknown），
       而接口不会报任何错。
       下面这组写法是**从正式库的 GeoNames 数据集里实测抄出来的**。
    """
    from processor.place import placeNameZh as placeNameZh

    for english, chinese in (
            ("Beijing", "北京市"),
            ("Xinjiang Uygur Zizhiqu", "新疆维吾尔自治区"),
            ("Zhejiang Sheng", "浙江省"),
            ("Gansu Sheng", "甘肃省"),
            ("Guangdong", "广东省"),
            ("Inner Mongolia", "内蒙古自治区"),
            ("Tibet Autonomous Region", "西藏自治区"),
            ("Guangxi Zhuangzu Zizhiqu", "广西壮族自治区"),
            ("Ningxia Huizu Zizhiqu", "宁夏回族自治区"),
            ("Chongqing Shi", "重庆市"),
            ("Jiangsu Sheng", "江苏省"),
            ("Jilin Sheng", "吉林省")):
        assert placeNameZh.provinceZhOfPlaceName("CN, %s, Somewhere" % english) \
            == chinese, english


def test_zhNameOfRealCoordinates():
    """真实坐标 -> 正确的中文名（每个断言都写明**为什么是它**）。"""
    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    if not placeNameZh.ready():
        pytest.skip("中文名数据源不可用（shapely 或数据文件缺失）")

    # 望京（朝阳区内）—— 来自正式库的实测坐标
    got = placeNameZh.zhNameOf("PL_T1", "CN, Beijing, Wangjing", 39.99710, 116.47683)
    assert got["nameZh"] == "北京市 · 朝阳区", got
    assert got["reason"] == placeNameZh.REASON_OK
    assert got["source"] == placeNameZh.ZH_SOURCE_ADMIN

    # 天安门（东城区）
    got = placeNameZh.zhNameOf("PL_T2", "CN, Beijing, Longtan", 39.89139, 116.41019)
    assert got["nameZh"] == "北京市 · 东城区", got

    # 新疆阿勒泰地区一带（正式库实测坐标落在和静县）
    got = placeNameZh.zhNameOf("PL_T3", "CN, Xinjiang Uygur Zizhiqu, Araltobe",
                               42.90810, 84.36478)
    assert got["nameZh"] == "新疆维吾尔自治区 · 和静县", got


def test_zhNameOfNoCoordinateGivesNoneWithReason():
    """无坐标 -> nameZh=None 且 reason=noCoordinate（**不猜**）。"""
    from processor.place import placeNameZh as placeNameZh

    got = placeNameZh.zhNameOf("PL_T4", "外婆家", None, None)
    assert got["nameZh"] is None
    assert got["reason"] == placeNameZh.REASON_NO_COORD
    # ⚠️ (0,0) 占位坐标同样判「无坐标」—— 复用 meta.isRealCoordinate，
    #   与扫描器、fix_placeholder_geo 同一个判据（DR-25）
    got = placeNameZh.zhNameOf("PL_T5", "CN, Beijing, X", 0, 0)
    assert got["nameZh"] is None
    assert got["reason"] == placeNameZh.REASON_NO_COORD


def test_overseasPlacesAreNotTranslated():
    """**境外不翻译**（用户决策 DR-29②）：nameZh 必须是 None。"""
    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    if not placeNameZh.ready():
        pytest.skip("中文名数据源不可用")

    for lat, lon, name in ((35.6895, 139.6917, "JP, Tokyo, Tokyo"),      # 东京
                           (48.8566, 2.3522, "FR, Ile-de-France, Paris"),  # 巴黎
                           (5.6037, -0.1870, "GH, Greater Accra, Accra"),  # 加纳
                           (-33.8688, 151.2093, "AU, New South Wales, Sydney"),
                           (37.7749, -122.4194, "US, California, San Francisco")):
        got = placeNameZh.zhNameOf("PL_OS", name, lat, lon)
        assert got["nameZh"] is None, "%s 不该被翻译（得到 %r）" % (name, got["nameZh"])
        assert got["reason"] == placeNameZh.REASON_OUTSIDE, got
        assert placeNameZh.isInChina(lat, lon) is False


def test_isInChinaUsesPolygonsNotCcString():
    """`isInChina` 判的是**多边形覆盖**，不是 `cc == 'CN'`。"""
    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    if not placeNameZh.ready():
        pytest.skip("中文名数据源不可用")
    assert placeNameZh.isInChina(39.9842, 116.4007) is True     # 北京
    assert placeNameZh.isInChina(43.8256, 87.6168) is True     # 乌鲁木齐
    assert placeNameZh.isInChina(22.3193, 114.1694) is True    # 香港
    assert placeNameZh.isInChina(35.6895, 139.6917) is False   # 东京
    assert placeNameZh.isInChina(0, 0) is False                # 占位坐标
    assert placeNameZh.isInChina(None, None) is False
    assert placeNameZh.isInChina("abc", "def") is False        # 垃圾输入不抛错


def test_overlapIsItsOwnReasonAndStillCounted():
    """两个区县多边形同时覆盖 -> `reason=overlap`（**单独一类，可计数**）。

    ⚠️ 这里不打桩真实数据（那要让 5.7MB 的索引变形），而是塞一个**合成索引**：
       两个完全重叠的小方块。真实数据里这情况来自「飞地 / 沿海岛屿」，
       概率低但确实存在，而它一旦被静默取第一个就再也查不出来了。
    """
    from shapely.geometry import box

    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    if not placeNameZh.ready():
        pytest.skip("中文名数据源不可用")
    from shapely.strtree import STRtree

    big = box(0, 0, 10, 10)
    # ⚠️ 两个盒子必须**相交但互不包含**。用「小的套在大的里面」是错的：
    #    那样 STRtree 的 bbox 过滤本来就只会返回大的那个，
    #    根本走不到「多命中」这条分支 —— 用例会**假通过**，
    #    而真正需要守的那条代码路径一次都没被执行。
    mid = box(5, 5, 15, 15)          # 与 big 相交、面积更小
    items = [("测试省", "甲区", big), ("测试省", "乙区", mid)]
    saved = placeNameZh._INDEX
    try:
        from shapely.geometry import Point
        placeNameZh._INDEX = {
            "districts": items,
            "provinces": [("测试省", None, big)],
            "tree": STRtree([one[2] for one in items]),
            "pTree": STRtree([big]),
            "pointOf": Point,
        }
        # (7,7) 同时落在两个盒子里
        got = placeNameZh.zhNameOf("PL_OV", "CN, Beijing, Overlap", 7.0, 7.0)
        assert got["reason"] == placeNameZh.REASON_OVERLAP, got
        assert got["overlap"] is True
        assert got["nameZh"] == "测试省 · 甲区", \
            "重叠时应取面积最大的那个；且省级以多边形为准（合成索引的省是「测试省」，" \
            "而 placeName 的 admin1 映射成「北京市」—— 不一致时听多边形的）"
        # 不在重叠区的点只命中一个 -> reason 回到 ok（别把重叠当常态）
        got = placeNameZh.zhNameOf("PL_OV2", "CN, Beijing, Overlap", 1.0, 1.0)
        assert got["reason"] == placeNameZh.REASON_OK, got
        assert got["nameZh"] == "测试省 · 甲区"
    finally:
        placeNameZh._INDEX = saved
        _resetZhModule()


def test_provinceMismatchPrefersThePolygon():
    """admin1 与坐标所在省**不一致时以多边形为准**（不许拼出不可能的组合）。

    ⚠️ 真实触发场景：`placeName` 的 admin1 是 reverse_geocoder 按「最近的城市点」
       推的，本来就可能跨县；而多边形是按**真实边界**判的。
       早先的实现以映射表为准，于是 placeName="CN, Beijing, X" 而坐标在新疆时
       会输出「北京市 · 奇台县」—— 一个地理上不可能、但**看起来完全正常**
       的中文名，界面上没人会发现，导出给别的工具才会露馅。
    """
    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    if not placeNameZh.ready():
        pytest.skip("中文名数据源不可用")
    # 望京（北京朝阳区）的坐标，却声称 admin1 是广东 -> 必须听多边形的
    got = placeNameZh.zhNameOf("PL_MM", "CN, Guangdong, Wangjing", 39.9971, 116.4768)
    assert got["nameZh"] == "北京市 · 朝阳区", got
    # 一致时不受影响
    got = placeNameZh.zhNameOf("PL_MM2", "CN, Beijing, Wangjing", 39.9971, 116.4768)
    assert got["nameZh"] == "北京市 · 朝阳区", got


def test_everyReasonIsRegisteredInReasonText():
    """**所有 reason 都必须在 REASON_TEXT 里有中文说明**。

    ⚠️ 这条守卫的是「统计报告的可读性」：byReason 打出来的每一类，
       人都要能看懂它意味着什么。新增 reason 忘了登记说明，
       报告里就会出现一行 `unregistered` —— 那正是「看不清」的起点。
    """
    from processor.place import placeNameZh as placeNameZh

    for name in dir(placeNameZh):
        if name.startswith("REASON_") and isinstance(
                getattr(placeNameZh, name), str):
            reason = getattr(placeNameZh, name)
            if reason == "REASON_OK":            # 常量名本身，不是 reason 值
                continue
            assert reason in placeNameZh.REASON_TEXT, reason


# ============================================================
# 三、降级纪律（**装不上只是显示英文，不是故障**）
# ============================================================

def test_disabledSwitchFallsBackToEnglish(zh_env, monkeypatch):
    """`PLACE_ZH_ENABLED=False` -> **不再算新的**中文名。

    ⚠️ 关开关**不是「删掉已算出的 nameZh」**：那一列是非派生数据，
       开关只管「还能不能算出新的」。所以本用例断言的是
       ① 关掉后 `rebuildNameZh()` 一行都不写；
       ② 接口仍然 200、库里原值仍在。
       若把开关实现成「启动时清空 nameZh」，用户会发现自己填的
       「外婆家」在某次重启后消失了 —— 那比不生效严重得多。
    """
    from config import basicSettings as basicSettings
    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    monkeypatch.setattr(basicSettings, "PLACE_ZH_ENABLED", False)
    report = placeNameZh.rebuildNameZh(dryRun=False)
    assert report["byReason"] == {placeNameZh.REASON_DISABLED: report["candidates"]}
    assert report["filled"] == 0, report

    body = zh_env["client"].get("/api/places").json()
    # ⚠️ `/api/places` 走的是 dto.pageBody，**没有 `ok` 字段**（那是 okBody 的）。
    #   断言一个不存在的键会得到 `KeyError: 'ok'` —— 与本用例要验的东西毫无关系。
    assert "items" in body and body["source"] == "dictionary", body
    # ⚠️ 关掉开关**不会抹掉库里已有的 nameZh**（它是非派生列）。
    #    所以响应里 nameZh **仍有值**；关开关影响的是「还能不能算出新的」。
    assert _nameZhOf("北京") == "北京市 · 朝阳区"
    assert any(item["nameZh"] for item in body["items"]), body["items"]
    got = zh_env["client"].get("/api/photos", params={"placeName": "北京"}).json()
    assert all(item["placeZh"] == "北京市 · 朝阳区" for item in got["items"])
    assert zh_env["client"].get("/api/places").status_code == 200
    # 彻底没算过的地方（无坐标）在关开关后依然是 None -> 界面回退英文
    assert byNameOf(body, "上海") is None


def test_missingDataSourceDegradesWithoutRaising(zh_env, monkeypatch):
    """数据源缺失（包没有 + 文件指向不存在的路径）-> **不抛错**、全 NULL、只记一次 warning。

    ⚠️ 这是「可选依赖」纪律的**核心**用例：装不上 shapely / 换机器忘了拷数据文件
       都走这条路。症状必须是「界面显示英文」，**不是** 500、不是启动失败。
    """
    from config import basicSettings as basicSettings
    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    monkeypatch.setattr(basicSettings, "PLACE_AMAP_GEO_FILE",
                        "d:\\不存在的路径\\china_district.json.gz")
    try:
        assert placeNameZh.ready() is False
        got = placeNameZh.zhNameOf("PL_X", "CN, Beijing, Datun", 39.98, 116.40)
        assert got["nameZh"] is None
        assert got["reason"] == placeNameZh.REASON_NO_DATA
        # ⚠️ 「只记一次」：ready() 再调 5 次，_ZH_WARNED 必须仍是 True 且
        #   不得把闸门复位（否则几千个地点会刷出几千条 warning）
        for _ in range(5):
            assert placeNameZh.ready() is False
        assert placeNameZh._ZH_WARNED is True
        # 接口仍然 200
        assert zh_env["client"].get("/api/places").status_code == 200
        assert zh_env["client"].get("/api/photos").status_code == 200
    finally:
        monkeypatch.undo()
        _resetZhModule()


def test_amapGeoImportFailureIsCaught(zh_env, monkeypatch):
    """`import amap_geo` 失败必须被吞掉并降级（**不许冒到接口层**）。

    ⚠️ `amap-geo` 在 PyPI 上根本不存在（2026-10-07 实测），所以这条
    **不是「万一」而是「每天」**：如果这里没兜住，服务第一次启动就 ImportError。
    """
    import builtins

    from processor.place import placeNameZh as placeNameZh

    _resetZhModule()
    realImport = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "amap_geo":
            raise ImportError("No module named 'amap_geo'（模拟）")
        return realImport(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    try:
        # 数据文件是好的 -> 包失败后应当**自动落到文件**，而不是整体降级
        assert placeNameZh.ready() is True, "包失败后没有落到本地数据文件"
        assert placeNameZh._PROVIDER.startswith("file("), placeNameZh._PROVIDER
        got = placeNameZh.zhNameOf("PL_Y", "CN, Beijing, Wangjing", 39.9971, 116.4768)
        assert got["nameZh"] == "北京市 · 朝阳区", got
    finally:
        monkeypatch.undo()
        _resetZhModule()


def test_moduleTopLevelDoesNotImportOptionalDeps():
    """模块顶层**不许** import shapely / amap_geo（懒加载纪律）。

    ⚠️ 这条用「源码文本」断言而不是「sys.modules 检查」：后者会因为
       **别的模块**已经 import 过 shapely 而恒真 —— 守卫就白设了。
    """
    import os

    from processor.place import placeNameZh as placeNameZh

    source = open(placeNameZh.__file__, "r", encoding="utf-8").read()
    # 只看 import 区（顶层 import 都在文件头那一段）
    head = source[:source.index("_VERSION =")]
    for line in head.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("import ", "from ")):
            continue
        assert "shapely" not in stripped, stripped
        assert "amap_geo" not in stripped, stripped
    # 文件必须存在（顺带守住「别把数据文件挪走」）
    assert os.path.isfile(placeNameZh.DEFAULT_GEO_FILE), placeNameZh.DEFAULT_GEO_FILE


# ============================================================
# 四、接口：显示名 / 回退契约 / 筛选两套
# ============================================================

def test_placesEndpointCarriesNameZh(zh_env):
    """`GET /api/places` 每条含 `nameZh`。"""
    body = zh_env["client"].get("/api/places").json()
    assert body["source"] == "dictionary"
    byName = {one["placeName"]: one for one in body["items"]}
    assert byName["北京"]["nameZh"] == "北京市 · 朝阳区"
    # ⚠️ `placeName` 仍是英文原值（前端要拿它排障、placeCode 由它派生）
    assert byName["北京"]["placeName"] == "北京"
    # 无坐标的地点（上海）-> nameZh 为 null，界面按契约回退英文
    assert byName["上海"]["nameZh"] is None


def test_placesKeywordMatchesChineseName(zh_env):
    """`keyword` 能用**中文名**搜到（DR-29③）。"""
    got = zh_env["client"].get("/api/places", params={"keyword": "朝阳区"}).json()
    assert [one["placeName"] for one in got["items"]] == ["北京"]
    # 英文仍然能搜（回退兼容）
    got = zh_env["client"].get("/api/places", params={"keyword": "广州"}).json()
    assert [one["placeName"] for one in got["items"]] == ["广州"]


def test_photoListCarriesPlaceZhAndKeepsEnglishPlaceName(zh_env):
    """列表项带 `placeZh`，而 `placeName` **保留英文原值**。"""
    got = zh_env["client"].get("/api/photos", params={"placeName": "北京"}).json()
    assert got["total"] == 3
    for item in got["items"]:
        assert item["placeName"] == "北京", item
        assert item["placeZh"] == "北京市 · 朝阳区", item
    # ⚠️ 不许用 placeZh 覆盖 placeName
    assert all(item["placeName"] != item["placeZh"] for item in got["items"])


def test_photoWithoutPlaceGetsNullPlaceZh(zh_env):
    """无地点的照片：`placeZh=None` 且 `placeName=None`（前端按契约回退）。"""
    got = zh_env["client"].get("/api/photos", params={"size": 100}).json()
    target = [one for one in got["items"] if one["photoCode"] == "PH_2024_06"]
    assert target, "夹具照片 PH_2024_06 不在列表里"
    assert target[0]["placeName"] is None
    assert target[0]["placeZh"] is None
    # ⚠️ 详情页同样为空（gps.placeZh 走的是 photoSummary 的同一个字段）
    detail = zh_env["client"].get("/api/photos/PH_2024_06").json()
    assert detail["gps"]["placeName"] is None
    assert detail["gps"]["placeZh"] is None


def test_photoDetailGpsCarriesPlaceZh(zh_env):
    """详情页 `gps` 里 placeZh 有值、placeName 仍是英文原值（验收第 12 条）。"""
    got = zh_env["client"].get("/api/photos/PH_2013_01").json()
    assert got["gps"]["placeZh"] == "北京市 · 朝阳区"
    assert got["gps"]["placeName"] == "北京"
    # 列表项与详情项口径必须一致（同一个 photoSummary）
    assert got["placeZh"] == got["gps"]["placeZh"]


def test_twoPlacesGetTwoDifferentChineseNames(zh_env):
    """**不同地点得到不同中文名** —— 证明关联真的是「按 placeName 取」。

    ⚠️ 这条必须单独测：placeZh 是**地点级**字段，同一 placeName 下的照片
       共享同一个值（这是设计，不是缺陷）。要证明「没有把所有地点都写成
       同一个中文名」，只能造**两个不同的 placeName**。
       早先的用例把两张照片分别放在朝阳/东城，然后按「每张照片各自的区县」
       断言 —— 那是把**地点级字段当照片级**测，跑出来的失败会把人引向
       「中心点取了均值」这个错误方向，而真因是口径搞错了。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import placeNameZh as placeNameZh

    # 东城区（天安门一带）：**另一个 placeName** + 自己的坐标
    sqliteCommon.insertManyTableGeneral(
        "pb_photo",
        [{"photoCode": "PH_DC1", "relPath": "2015/01/dc1.jpg",
          "relPathHash": "rh_dc1", "fileHash": "fh_dc1", "fileSize": 100,
          "takenAt": "2015-01-05T03:00:00Z", "shotYear": 2015,
          "placeName": "CN, Beijing, Dongcheng", "lat": 39.9088,
          "lon": 116.3975, "faceCount": 0, "isDuplicate": 0,
          "isMissing": 0, "scanState": 1}],
        conflictColumns=("photoCode",),
        updateColumns=("placeName", "lat", "lon"),
        fillStandard=True, forceColumns=("placeName", "lat", "lon"))
    zh_env["client"].post("/api/places/rebuild")
    placeNameZh.rebuildNameZh(dryRun=False)

    assert _nameZhOf("北京") == "北京市 · 朝阳区"
    assert _nameZhOf("CN, Beijing, Dongcheng") == "北京市 · 东城区"
    body = zh_env["client"].get("/api/places").json()
    got = {one["placeName"]: one["nameZh"] for one in body["items"]}
    assert got["北京"] == "北京市 · 朝阳区"
    assert got["CN, Beijing, Dongcheng"] == "北京市 · 东城区"


def test_personTimelinePhotosCarryPlaceZh(zh_env):
    """`/api/persons/{code}/timeline` 里的照片也带 `placeZh`（验收第 15 条）。"""
    from database.auto_generated import sqliteCommon as sqliteCommon

    # ⚠️ 人物时间线的数据源是 **pb_face**（f.personCode + f.shotBucket），
    #   不是 pb_photo_person。往关联表里插一行不会让这个端点返回任何东西 ——
    #   而症状是「groups 为空」，很容易被误判成「中文名这条链路把人过滤掉了」。
    assert sqliteCommon.updateTableGeneral(
        "pb_face", "faceCode = %s", ("FC_PH_2013_01_0",),
        {"personCode": "P_alpha", "isConfirmed": 1}) > 0
    got = zh_env["client"].get("/api/persons/P_alpha/timeline").json()
    photos = [one for group in got["groups"] for one in group["photos"]]
    target = [one for one in photos if one["photoCode"] == "PH_2013_01"]
    assert target, photos
    assert target[0]["placeZh"] == "北京市 · 朝阳区"
    assert target[0]["placeName"] == "北京"


def test_placeFilterAcceptsBothChineseAndEnglish(zh_env):
    """`?placeName=` **两套都支持**（验收第 13 条：返回同一批照片）。"""
    client = zh_env["client"]
    byZh = client.get("/api/photos",
                      params={"placeName": "北京市 · 朝阳区"}).json()
    byEn = client.get("/api/photos", params={"placeName": "北京"}).json()
    assert byZh["total"] == byEn["total"] == 3
    assert [i["photoCode"] for i in byZh["items"]] == \
        [i["photoCode"] for i in byEn["items"]]


def test_placeFilterUnknownValueReturnsEmptyNotError(zh_env):
    """两套都没命中 -> **返回空集，不报错**。"""
    got = zh_env["client"].get(
        "/api/photos", params={"placeName": "不存在的地点"}).json()
    assert got["total"] == 0 and got["items"] == []


def test_placeFilterIsStillExactMatch(zh_env):
    """筛选**仍然是精确匹配**（不许被改成 LIKE）。

    ⚠️ 「北京」与「北京市」在库里是两个不同字符串；模糊匹配会让用户
       以为自己筛错了（这是 browse.py 原有注释里写明的理由）。
       所以「北京」**不能**命中「北京市」。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon

    sqliteCommon.insertManyTableGeneral(
        "pb_photo",
        [{"photoCode": "PH_BJ2", "relPath": "2014/01/bj2.jpg",
          "relPathHash": "rh_bj2", "fileHash": "fh_bj2", "fileSize": 100,
          "takenAt": "2014-01-05T03:00:00Z", "shotYear": 2014,
          "placeName": "北京市", "faceCount": 0, "isDuplicate": 0,
          "isMissing": 0, "scanState": 1}],
        conflictColumns=("photoCode",),
        updateColumns=("placeName",), fillStandard=True, forceColumns=("placeName",))
    zh_env["client"].post("/api/places/rebuild")
    got = zh_env["client"].get("/api/photos", params={"placeName": "北京"}).json()
    assert [i["placeName"] for i in got["items"]] == ["北京", "北京", "北京"], \
        "精确匹配被改成了 LIKE：「北京」命中了「北京市」"


# ============================================================
# 五、resolvePlaceFilter（单测口径，不经 HTTP）
# ============================================================

def test_resolvePlaceFilterReturnsSqlAndValues(zh_env):
    from processor.place import placeStore

    # ⚠️ R4b 起，匹配的是**聚合键**（`COALESCE(NULLIF(placeNameDir,''), placeName)`）
    #    而不是裸的 `p.placeName` 列 —— 目录名地点的 placeName 是**空**的，
    #    按裸列筛会「列表里有 114 张、点进去 0 张」。断言因此改成**对着
    #    `placeStore.placeKeySql()`** 比（而不是再写一遍字面量）：口径只有那一处定义。
    key = placeStore.placeKeySql("p")
    assert "placeNameDir" in key
    # ① 中文名 -> 翻译回英文 placeName
    sql, values = placeStore.resolvePlaceFilter("北京市 · 朝阳区")
    assert sql == key + " = %s"
    assert values == ("北京",)
    # ② 英文原值 -> 直接精确匹配（回退兼容旧调用）
    assert placeStore.resolvePlaceFilter("北京") == (key + " = %s", ("北京",))
    # ③ 都没命中 -> 原样精确匹配（返回空集，不报错）
    assert placeStore.resolvePlaceFilter("火星") == (key + " = %s", ("火星",))
    # ④ 空值
    assert placeStore.resolvePlaceFilter("") == (key + " = %s", ("",))


def test_resolvePlaceFilterUsesInWhenOneZhMapsToSeveralNames(zh_env):
    """**一个中文名对应多个不同 placeName** 时用 IN，不能只取第一行。

    ⚠️ 真实场景：用户把一条**手工地点**命名为「北京市 · 朝阳区」（比如把
       「外婆家」归到朝阳区），而聚合行 `placeName='北京'` 也有同一个中文名。
       界面上点那个中文名时，两边的照片**都应该**出来。

    ⚠️⚠️ 相反的场景**不需要 IN**：两行 `placeName` 相同（手工 + 聚合同名）时，
       `placeName = '北京'` 本来就同时命中两行。早先的用例把这两件事搞混了
       ——它造的是「placeName 相同的两行」，而那种情况用 `=` 完全正确。
       照着错前提写出来的断言会逼着代码去写一个**多余**的 IN 分支。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import placeStore

    # ⚠️ 必须用 `insertManyTableGeneral(..., fillStandard=True)` 而不是
    #   `insertTableGeneral`：后者**不填标准尾字段**（delFlag 为 NULL），
    #   于是 `query_pb_place` 的 `delFlag = '0'` 过滤会把这行藏起来 ——
    #   症状是「明明插进去了，listPlaces 里只有一行」。
    assert sqliteCommon.insertManyTableGeneral(
        "pb_place",
        [{"placeCode": placeStore.makePlaceCode("外婆家") + "_MANUAL",
          "placeName": "外婆家", "source": 1, "photoCount": 0,
          "nameZh": "北京市 · 朝阳区"}],
        conflictColumns=("placeCode",), fillStandard=True)[0] >= 0
    rows = sqliteCommon.query_pb_place("pb_place", placeName="外婆家")
    assert len(rows) == 1, [one["placeCode"] for one in rows]

    sql, values = placeStore.resolvePlaceFilter("北京市 · 朝阳区")
    # ⚠️ R4b：IN 的前缀也换成**聚合键**（理由同上一条用例）
    assert sql.startswith(placeStore.placeKeySql("p") + " IN ("), sql
    assert set(values) == {"北京", "外婆家"}, values
    # 参数个数必须与占位符个数一致（少一个就是 SQL 绑定错位）
    assert sql.count("%s") == len(values), (sql, values)

    # 只有一个 placeName 值时仍用 `=`（IN 一个元素没意义）
    sql2, values2 = placeStore.resolvePlaceFilter("CN, Beijing, Dongcheng")
    assert sql2 == placeStore.placeKeySql("p") + " = %s" \
        and values2 == ("CN, Beijing, Dongcheng",)


# ============================================================
# 六、既有筛选用例不许被改坏（验收第 16 条）
# ============================================================

def test_existingPlaceFiltersStillWork(zh_env):
    """`test_api_places.py` 里的核心筛选用例在这里再跑一遍（防回归）。

    ⚠️ 期望值按 **api_env 夹具**算（北京 3 / 上海 2 / 广州 1），
       不是按 test_api_places.py 的 placesEnv（它多造了广州与丽江两张）——
       两套夹具的地点构成不同，照抄那边的数字会让这条用例变成
       「断言一个和被测行为无关的常量」。
    """
    client = zh_env["client"]
    client.post("/api/places/rebuild")
    got = client.get("/api/places", params={"keyword": "广"}).json()
    assert [i["placeName"] for i in got["items"]] == ["广州"]
    got = client.get("/api/places", params={"minPhotoCount": 2}).json()
    assert got["total"] == 2, got
    got = client.get("/api/places", params={"orderBy": "photoCount",
                                            "desc": 0}).json()
    assert [i["photoCount"] for i in got["items"]] == [1, 2, 3], got
    assert client.get("/api/places", params={"orderBy": "DROP TABLE"}).status_code == 400


def test_placesEndpointIsStillPureRead(zh_env):
    """加了 nameZh 之后，`GET /api/places` **仍然一行都不写**。"""
    from database import queryCommon as query

    def fingerprint():
        return {name: int(query.selectValue(
            "SELECT COUNT(*) AS rowNum FROM %s" % name, (), default=0) or 0)
            for name in ("pb_place", "pb_photo", "pb_review_log")}

    before = fingerprint()
    for _ in range(3):
        zh_env["client"].get("/api/places", params={"keyword": "朝阳"})
    assert fingerprint() == before, "GET /api/places 写了库"
