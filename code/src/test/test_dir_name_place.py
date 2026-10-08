#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_dir_name_place.py
#Description: 目录名地点（pb_photo.placeNameDir）与聚合键的测试（步骤 R4b · DR-30/32/33/34）
#
# 覆盖
#   * 判据（`basicSettings.DIR_PLACE_PATTERN`）：日期前缀 + 非空地名 才采纳；
#     人名/组名目录、只有日期的目录一律排除 —— 且**每条都给得出 reason**
#   * 判据与黑名单**是配置**（monkeypatch 后行为立刻变，不用改代码）
#   * `placeNameDirOf` 的上溯与**终止条件**（不许一路爬到 photo 根）
#   * `scanDirNames` **只填空不覆盖**（用户手工改过的值必须活下来）
#   * 目录改名的两个动作：**报漂移**（永远）+ `refresh` 才覆盖
#   * `rebuildPlaces` 的聚合键 = 目录名优先（DR-32）：张数 / 中心点 NULL / nameZh NULL
#   * **降级路径 `liveAggregatePlaces` 与筛选 `resolvePlaceFilter` 用同一条键**
#     （不一致会表现成「列表说 3 张、点进去 0 张」，而两处都不报错）
#   * 手工行（source=1）永不归零；孤儿 nameZh / 重名 nameZh 两个报告
#   * Python 口径 `placeKeyOf` 与 SQL 口径逐行一致（防两份口径漂移）

import pytest

#: 判据的「正样本」（正式库实测 11 个目录，598 张）
ADOPTED = {
    "2013.07.26 华盛顿": "华盛顿",
    "2013.07.18 大都会博物馆": "大都会博物馆",
    "2013.07.16 纽约": "纽约",
    "2013.07.23 尼亚加拉大瀑布": "尼亚加拉大瀑布",
    "2013.07.22 千岛湖": "千岛湖",
    "2013.07.27 国家艺术馆": "国家艺术馆",
    "2013.07.20 波士顿": "波士顿",
    "2013.07.21 Watertown": "Watertown",
    "2013.07.19 罗德岛": "罗德岛",
    "2013.07.24～25 康宁及赫尔希": "康宁及赫尔希",
    "20101218 Michael's Home": "Michael's Home",
}

#: 「负样本」：人名 / 组名 / 学校 / 活动名 / 家族名（正式库实测 41 个目录，1539 张）
EXCLUDED = [
    "BaiRuiQin", "MOT Friends", "lianzhongwen", "BUPT871", "LianZhongWen",
    "LianYi", "lc", "Friends", "Family", "others source", "LiuChang",
    "Photo", "MengLi", "LvZhenhua", "Wang", "shiyu", "Lian Family",
    "xiaoyun", "毕业照", "lianzhongming", "mengli", "老照片", "DengGang",
    "廉政甫", "Liu Family", "xiaomao", "豆豆家", "Friends Photo",
    "WangRong", "Zhuhong", "XieWanHe", "GouQiMing", "Shiyan", "YuBo",
    "XiaoYun", "DDQ", "聚会", "廉家老照片", "2011聚会", "2013美国游",
]


@pytest.fixture
def placeEnv(temp_db, set_photo_root, tmp_path):
    """临时库 + 一批「按目录名带地点」与「按 GPS 带地点」的照片。

    ⚠️ 全部走 tmp 临时库（`temp_db`）、绝不碰正式库：
       `sqliteCommon.dbHandle` 是进程级单例，忘了挂夹具的用例会安静地写正式库。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon

    def _add(photoCode, relPath, placeName=None, placeNameDir=None,
             lat=None, lon=None, shotYear=2013):
        sqliteCommon.insertManyTableGeneral(
            "pb_photo",
            [{"photoCode": photoCode, "relPath": relPath,
              "relPathHash": "rh_%s" % photoCode, "fileHash": "fh_%s" % photoCode,
              "fileSize": 100, "shotYear": shotYear,
              "placeName": placeName, "placeNameDir": placeNameDir,
              "lat": lat, "lon": lon,
              "faceCount": 0, "isDuplicate": 0, "isMissing": 0, "scanState": 1}],
            conflictColumns=("photoCode",),
            updateColumns=("placeName", "placeNameDir", "lat", "lon", "shotYear",
                           "modifyYMDHMS"),
            fillStandard=True,
            forceColumns=("placeName", "placeNameDir", "lat", "lon"))

    # 目录名线索（父目录 = `2013.07.26 华盛顿`）
    for index in range(3):
        _add("PH_WASH_%d" % index,
             "2013美国游/照片/风景照/2013.07.26 华盛顿/IMG_%d.jpg" % index)
    # GPS 线索（父目录是**排除项**，所以聚合键应退回 placeName）
    for index in range(2):
        _add("PH_BJ_%d" % index, "Lian Family/BaiRuiQin/mmexport%d.jpg" % index,
             placeName="CN, Beijing, Datun", lat=39.98 + index * 0.01,
             lon=116.40 + index * 0.01)
    # 两种线索都有：**目录名优先**
    _add("PH_BOTH", "2013美国游/照片/风景照/2013.07.16 纽约/IMG_b.jpg",
         placeName="CN, Beijing, Wangjing", lat=39.99, lon=116.47)
    # 无线索（人名目录 + 截图）
    _add("PH_NONE", "Friends/lc/abc.jpg")
    return {"dbFile": temp_db, "add": _add, "root": set_photo_root}


# ============================================================
# 一、判据
# ============================================================
def test_parseDirNameAdoptsDatePlace(placeEnv):
    """「日期前缀 + 非空地名」全部采纳，且名字**剥掉日期前缀**。"""
    from processor.place import dirNamePlace

    for dirName, expect in ADOPTED.items():
        got = dirNamePlace.parseDirName(dirName)
        assert got["isPlace"] is True, dirName
        assert got["placeName"] == expect, (dirName, got)
        assert got["reason"] == dirNamePlace.REASON_ADOPTED
        assert got["dateHint"], dirName           # 日期也被记下来了（报告用）


def test_parseDirNameExcludesNonPlaces(placeEnv):
    """人名 / 组名 / 学校 / 活动名 / 家族名 / 只有日期 —— 一律排除，且给得出理由。"""
    from processor.place import dirNamePlace

    for dirName in EXCLUDED:
        got = dirNamePlace.parseDirName(dirName)
        assert got["isPlace"] is False, (dirName, got)
        assert got["placeName"] == ""
        # ⚠️ R4b 收尾起，正式库实测的 41 个已确认排除项被写进
        #    `DIR_PLACE_BLACKLIST`（**决策留痕**，也让"疑似地点"报告不重复报），
        #    所以这一批的 reason 是 `blacklisted` 而不再是机械原因。
        assert got["reason"] in (dirNamePlace.REASON_NO_DATE,
                                 dirNamePlace.REASON_DATE_ONLY,
                                 dirNamePlace.REASON_BLACKLIST), (dirName, got)
    # 只有日期（没有地名）与「根本没有日期」是**两类不同**的原因，不能混
    assert dirNamePlace.parseDirName("20051229")["reason"] == \
        dirNamePlace.REASON_DATE_ONLY
    assert dirNamePlace.parseDirName("201105")["reason"] == \
        dirNamePlace.REASON_DATE_ONLY
    # ⚠️ 已人工确认排除的目录（`DIR_PLACE_CONFIRMED_NOT_PLACE`）**不改判定**：
    #    它们的 reason 仍是**机械原因**（`noDatePrefix`）—— 那是排障时唯一的线索。
    #    "是不是地点"与"为什么被排除"是两件事，配置也刻意分开。
    assert dirNamePlace.parseDirName("2011聚会")["reason"] == \
        dirNamePlace.REASON_NO_DATE
    assert dirNamePlace.parseDirName("廉家老照片")["reason"] == \
        dirNamePlace.REASON_NO_DATE


def test_parseDirNameSixSpecialCases(placeEnv):
    """六个特例的判定（逐个给结论，与验收第 7 条一一对应）。"""
    from processor.place import dirNamePlace

    # ① 有年份 + 事件名、无地点 -> 排除（「聚会」不是地点）
    assert dirNamePlace.parseDirName("2011聚会")["isPlace"] is False
    # ② 只有日期、地名部分为空 -> 排除
    assert dirNamePlace.parseDirName("20051229")["isPlace"] is False
    # ③ 只有年月 -> 排除
    assert dirNamePlace.parseDirName("201105")["isPlace"] is False
    # ④ 日期 + 「xx 的家」-> **采纳**（它是具体地点，比不给强）
    home = dirNamePlace.parseDirName("20101218 Michael's Home")
    assert home["isPlace"] is True and home["placeName"] == "Michael's Home"
    # ⑤ 无日期、家族名 -> 排除
    assert dirNamePlace.parseDirName("廉家老照片")["isPlace"] is False
    # ⑥ 日期含波浪号区间 + 两个地名 -> **采纳**，区间残留被清理
    span = dirNamePlace.parseDirName("2013.07.24～25 康宁及赫尔希")
    assert span["isPlace"] is True and span["placeName"] == "康宁及赫尔希"


# ============================================================
# 一之二、判据的四个配置（默认 / 别名 / 黑名单 / 名字清理）
# ============================================================
def test_aliasTableAdoptsUnusualDirName(placeEnv, monkeypatch):
    """**别名表**：判据认不出的写法，人工逐条登记（优先于正则）。"""
    from config import basicSettings as basicSettings
    from processor.place import dirNamePlace

    # 现状：下划线分隔/日期在后 都认不出
    for name in ("2016_05_01_三亚", "三亚 2016.05.01", "2016.5.1 三亚"):
        assert dirNamePlace.parseDirName(name)["isPlace"] is False, name

    monkeypatch.setattr(basicSettings, "DIR_PLACE_ALIAS",
                        ("2016_05_01_三亚=三亚", "三亚 2016.05.01=三亚"),
                        raising=False)
    got = dirNamePlace.parseDirName("2016_05_01_三亚")
    assert got["isPlace"] is True
    assert got["placeName"] == "三亚"
    assert got["reason"] == dirNamePlace.REASON_ALIAS
    # 大小写不敏感（与黑名单同一口径）
    assert dirNamePlace.parseDirName("三亚 2016.05.01")["placeName"] == "三亚"
    # 没登记的照旧不采纳（别名表是**逐条**的，不是放宽全局）
    assert dirNamePlace.parseDirName("2016.5.1 三亚")["isPlace"] is False
    # 别名表**优先于黑名单之后、正则之前**：正常目录名不受影响
    assert dirNamePlace.parseDirName("2013.07.26 华盛顿")["reason"] == \
        dirNamePlace.REASON_ADOPTED
    # 畸形项被忽略（缺 `=` / 右边为空），不影响其它项
    monkeypatch.setattr(basicSettings, "DIR_PLACE_ALIAS",
                        ("没有等号", "只有左边=", "2016_05_01_三亚=三亚"),
                        raising=False)
    assert dirNamePlace.parseDirName("2016_05_01_三亚")["placeName"] == "三亚"
    assert dirNamePlace.parseDirName("2013.07.26 华盛顿")["isPlace"] is True


def test_aliasRegexCoversAFamilyOfDirNames(placeEnv, monkeypatch):
    """左侧以 `^` 开头 ⇒ **正则别名**：一条盖住一族日期目录（前缀匹配）。"""
    from config import basicSettings as basicSettings
    from processor.place import dirNamePlace

    monkeypatch.setattr(basicSettings, "DIR_PLACE_ALIAS",
                        ("^2026_05_01 喀纳斯.*=喀纳斯",),
                        raising=False)
    for name in ("2026_05_01 喀纳斯", "2026_05_01 喀纳斯 冬季",
                 "2026_05_01 喀纳斯-晨雾"):
        got = dirNamePlace.parseDirName(name)
        assert got["isPlace"] is True, name
        assert got["placeName"] == "喀纳斯", (name, got)
        assert got["reason"] == dirNamePlace.REASON_ALIAS
    # 不匹配的照旧（正则别名也是**从名字开头**匹配，与 DIR_PLACE_PATTERN 同一规则）
    assert dirNamePlace.parseDirName("喀纳斯 2026_05_01")["isPlace"] is False
    # **精确项优先于正则项**（登记顺序不影响这条）
    monkeypatch.setattr(basicSettings, "DIR_PLACE_ALIAS",
                        ("^2026_05_01 喀纳斯.*=喀纳斯湖",
                         "2026_05_01 喀纳斯=喀纳斯"), raising=False)
    assert dirNamePlace.parseDirName("2026_05_01 喀纳斯")["placeName"] == "喀纳斯"
    assert dirNamePlace.parseDirName("2026_05_01 喀纳斯 2")["placeName"] == "喀纳斯湖"
    # 非法正则只忽略那一项（其余项照常）
    monkeypatch.setattr(basicSettings, "DIR_PLACE_ALIAS",
                        ("^([=坏正则", "2026_05_01 喀纳斯=喀纳斯"), raising=False)
    assert dirNamePlace.parseDirName("2026_05_01 喀纳斯")["placeName"] == "喀纳斯"


def test_confirmedNotPlaceOnlySilencesSuspects(placeEnv, monkeypatch):
    """`DIR_PLACE_CONFIRMED_NOT_PLACE`：**不改判定**，只让疑似报告闭嘴。"""
    from config import basicSettings as basicSettings
    from processor.place import dirNamePlace

    item = {"dir": "外婆家", "count": 75, "isPlace": False,
            "reason": dirNamePlace.REASON_NO_DATE}
    assert [one["dir"] for one in dirNamePlace.suspectsOf([item])] == ["外婆家"]
    monkeypatch.setattr(basicSettings, "DIR_PLACE_CONFIRMED_NOT_PLACE",
                        ("外婆家",), raising=False)
    assert dirNamePlace.suspectsOf([item]) == []
    # ⚠️ 判定**一点没变**（reason 还是机械原因）
    assert dirNamePlace.parseDirName("外婆家")["isPlace"] is False
    assert dirNamePlace.parseDirName("外婆家")["reason"] == \
        dirNamePlace.REASON_NO_DATE
    # ⚠️ 反过来：黑名单**会**改判定（reason = blacklisted）—— 两者别混
    monkeypatch.setattr(basicSettings, "DIR_PLACE_BLACKLIST",
                        ("2016.05.01 全家福",), raising=False)
    got = dirNamePlace.parseDirName("2016.05.01 全家福")
    assert got["isPlace"] is False
    assert got["reason"] == dirNamePlace.REASON_BLACKLIST


def test_nameTrimCleansLeadingSeparator(placeEnv):
    """`2016.05.01-三亚` -> 名字不带头导分隔符（R4b 收尾顺手修的小瘸腿）。"""
    from processor.place import dirNamePlace

    got = dirNamePlace.parseDirName("2016.05.01-三亚")
    assert got["isPlace"] is True and got["placeName"] == "三亚", got
    # 空格、波浪号、顿号等开头也一并清掉
    assert dirNamePlace.parseDirName("2016.05.01 ~ 三亚")["placeName"] == "三亚"
    assert dirNamePlace.parseDirName("2016.05.01、三亚")["placeName"] == "三亚"
    # ⚠️ 底线：清完为空就**退回原值**（宁可名字难看，也不能把名字清没了）
    assert dirNamePlace.parseDirName("2016.05.01 ----")["placeName"] == "----"


def test_suspectsReportNewNamingStyle(placeEnv, monkeypatch):
    """**疑似地点**：被排除但像地名 ⇒ 报出来（只报告、不改采纳）。"""
    from config import basicSettings as basicSettings
    from processor.place import dirNamePlace

    dirList = [
        {"dir": "2013.07.26 华盛顿", "count": 114, "isPlace": True,
         "reason": dirNamePlace.REASON_ADOPTED},
        {"dir": "2016_05_01_三亚", "count": 1, "isPlace": False,
         "reason": dirNamePlace.REASON_NO_DATE},        # 含年份、**1 张也报**
        {"dir": "三亚 2016.05.01", "count": 8, "isPlace": False,
         "reason": dirNamePlace.REASON_NO_DATE},        # 日期在后 -> 报
        {"dir": "外婆家", "count": 75, "isPlace": False,
         "reason": dirNamePlace.REASON_NO_DATE},        # 含中文 >=5 -> 报
        {"dir": "廉家老照片", "count": 75, "isPlace": False,
         "reason": dirNamePlace.REASON_NO_DATE},        # 已在"已确认"清单 -> 不报
        {"dir": "20051229", "count": 22, "isPlace": False,
         "reason": dirNamePlace.REASON_DATE_ONLY},      # 只有日期 -> 不报
        {"dir": "BaiRuiQin", "count": 452, "isPlace": False,
         "reason": dirNamePlace.REASON_NO_DATE},        # 纯拼音、无年份 -> 不报
        {"dir": "新地方", "count": 2, "isPlace": False,
         "reason": dirNamePlace.REASON_NO_DATE},        # 含中文但不到 5 张 -> 不报
    ]
    got = dirNamePlace.suspectsOf(dirList)
    assert [one["dir"] for one in got] == \
        ["2016_05_01_三亚", "三亚 2016.05.01", "外婆家"], got
    assert all(one["hint"] for one in got)
    # ⚠️ 门槛**分级**：含年份那一类门槛是 1（上面 1 张的三亚就报了），
    #    含中文那一类仍是 5（`新地方` 2 张不报）—— 这是"报出来"与"吵死人"的平衡。
    monkeypatch.setattr(basicSettings, "DIR_PLACE_SUSPECT_MIN_YEAR", 99, raising=False)
    assert [one["dir"] for one in dirNamePlace.suspectsOf(dirList)] == ["外婆家"]
    monkeypatch.setattr(basicSettings, "DIR_PLACE_SUSPECT_MIN_YEAR", 1, raising=False)
    # 显式传 minPhotos 可以整体压平（排障用）
    assert "新地方" in [one["dir"] for one in
                        dirNamePlace.suspectsOf(dirList, minPhotos=1)]
    # ⚠️ 最重要的一条：报告**不改采纳结果**
    assert dirNamePlace.parseDirName("2016_05_01_三亚")["isPlace"] is False


def test_patternAndBlacklistAreConfig(placeEnv, monkeypatch):
    """判据与黑名单**是配置**：改配置立刻改行为（不是硬编码在函数里）。"""
    from config import basicSettings as basicSettings
    from processor.place import dirNamePlace

    # ① 正则换成「地名在前、日期在后」-> 原来不采纳的现在采纳
    monkeypatch.setattr(basicSettings, "DIR_PLACE_PATTERN",
                        r"^(?P<name>\S+)\s+(?P<y>\d{4})[.\-/]?"
                        r"(?P<m>\d{2})[.\-/]?(?P<d>\d{2})$", raising=False)
    got = dirNamePlace.parseDirName("华盛顿 2013.07.26")
    assert got["isPlace"] is True and got["placeName"] == "华盛顿"
    assert dirNamePlace.parseDirName("2013.07.26 华盛顿")["isPlace"] is False

    # ② 黑名单：正则拦不住的特例（这里是反过来用它排除一个"像地点"的目录）
    monkeypatch.setattr(basicSettings, "DIR_PLACE_PATTERN",
                        r"^(?P<y>\d{4})[.\-/]?(?P<m>\d{2})[.\-/]?(?P<d>\d{2})"
                        r"\s*(?P<name>\S.*)$", raising=False)
    monkeypatch.setattr(basicSettings, "DIR_PLACE_BLACKLIST",
                        ("20130101 全家福",), raising=False)
    got = dirNamePlace.parseDirName("20130101 全家福")
    assert got["isPlace"] is False
    assert got["reason"] == dirNamePlace.REASON_BLACKLIST

    # ③ 非法正则 -> 一律「无线索」+ 明确 reason（不抛错、不静默）
    monkeypatch.setattr(basicSettings, "DIR_PLACE_PATTERN", "([", raising=False)
    got = dirNamePlace.parseDirName("2013.07.26 华盛顿")
    assert got["isPlace"] is False
    assert got["reason"] == dirNamePlace.REASON_BAD_PATTERN


def test_placeNameDirOfStopsAtPhotoRoot(placeEnv):
    """上溯有**明确终止条件**：最多 DIR_PLACE_MAX_UP 层，绝不爬到 photo 根。"""
    from processor.place import dirNamePlace as dnp

    # 父目录就是地点
    assert dnp.placeNameDirOf("2013美国游/照片/风景照/2013.07.26 华盛顿/a.jpg") == "华盛顿"
    # 父目录无名、祖父目录是地点 -> 上溯一层
    assert dnp.placeNameDirOf(
        "2013美国游/照片/风景照/2013.07.26 华盛顿/子目录/a.jpg") == "华盛顿"
    # 父目录无名、祖父也无名 -> 空（**不会**把 `风景照` 当地点）
    assert dnp.placeNameDirOf("2013美国游/照片/风景照/a.jpg") == ""
    # 一级目录（`2013美国游`）**不是**地点，不许被当成线索
    assert dnp.placeNameDirOf("2013美国游/a.jpg") == ""
    # relPath 里没有父目录
    assert dnp.placeNameDirOf("a.jpg") == ""
    # depth=2：只看祖父目录（父目录明明有地点也不看）
    assert dnp.placeNameDirOf("2013.07.26 华盛顿/a.jpg", depth=2) == ""
    # level 报得出来（排障要回答「名字是从哪一层取的」）
    assert dnp.resolveDirPlace(
        "2013美国游/照片/风景照/2013.07.26 华盛顿/子/a.jpg")["level"] == 2


# ============================================================
# 二、只填空不覆盖 + 改名（漂移 / refresh）
# ============================================================
def test_scanDirNamesFillsWithoutOverwriting(placeEnv):
    """**只填空不覆盖**：手工改过的值必须活下来（这是非派生列的前提）。"""
    from database import queryCommon as query
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import dirNamePlace

    # 先全量填一遍
    first = dirNamePlace.scanDirNames(dryRun=False)
    assert first["written"] == 4        # 3 张华盛顿 + 1 张「两种线索都有」
    assert first["alreadyFilled"] == 0

    # 用户手工把某一行改成别的值
    assert sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", ("PH_WASH_0",),
        {"placeNameDir": "外婆家"}) > 0

    again = dirNamePlace.scanDirNames(dryRun=False)
    assert again["written"] == 0, "不该有任何写入"
    assert again["alreadyFilled"] == 4
    # ⚠️ 它同时会被报成**漂移**（库里存量 ≠ 现在解出来的值）——
    #    这是有意的：函数无法区分「用户手工改的」与「目录改名留下的旧值」，
    #    所以**永远报出来让人判断**，但默认绝不覆盖。
    assert len(again["drift"]) == 1 and again["drift"][0]["photoCode"] == "PH_WASH_0"
    assert str(query.selectValue(
        "SELECT p.placeNameDir AS v FROM pb_photo p WHERE p.photoCode = %s",
        ("PH_WASH_0",))) == "外婆家"


def test_scanDirNamesRefreshOverwritesOnlyDrift(placeEnv):
    """`refresh=True`：只覆盖**漂移**行，别的已有值仍然不动。"""
    from database import queryCommon as query
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import dirNamePlace

    dirNamePlace.scanDirNames(dryRun=False)
    sqliteCommon.updateTableGeneral("pb_photo", "photoCode = %s", ("PH_WASH_0",),
                                    {"placeNameDir": "外婆家"})
    sqliteCommon.updateTableGeneral("pb_photo", "photoCode = %s", ("PH_WASH_1",),
                                    {"placeNameDir": "华盛顿"})   # 与解析结果相同
    out = dirNamePlace.scanDirNames(dryRun=False, refresh=True)
    assert out["refreshed"] == 1 and out["written"] == 1
    assert str(query.selectValue(
        "SELECT p.placeNameDir AS v FROM pb_photo p WHERE p.photoCode = %s",
        ("PH_WASH_0",))) == "华盛顿", "漂移行应被刷成现在的目录名"
    assert str(query.selectValue(
        "SELECT p.placeNameDir AS v FROM pb_photo p WHERE p.photoCode = %s",
        ("PH_WASH_1",))) == "华盛顿"


# ============================================================
# 三、聚合键（rebuild / 降级 / 筛选 / Python 口径）
# ============================================================
def test_rebuildUsesDirNameAsKey(placeEnv):
    """聚合键 = 目录名优先：地点数 / 张数 / centerLat NULL / nameZh NULL（DR-34）。"""
    from database import queryCommon as query
    from processor.place import dirNamePlace, placeStore

    dirNamePlace.scanDirNames(dryRun=False)
    out = placeStore.rebuildPlaces()
    # 华盛顿 3 + 纽约 1（目录名键）+ Datun 2（GPS 键）；PH_NONE 无线索
    assert out["placeCount"] == 3 and out["groups"] == 3
    assert out["total"] == 6 and out["unnamed"] == 1
    rows = {str(one["placeName"]): one for one in query.selectList(
        "SELECT placeName, photoCount, centerLat, centerLon, nameZh, source"
        " FROM pb_place WHERE delFlag = %s", ("0",))}
    assert set(rows) == {"华盛顿", "纽约", "CN, Beijing, Datun"}
    assert int(rows["华盛顿"]["photoCount"]) == 3
    assert int(rows["纽约"]["photoCount"]) == 1
    assert int(rows["CN, Beijing, Datun"]["photoCount"]) == 2
    # 目录名地点：那一组照片没有 GPS -> 中心点 **NULL**
    # （不能兜成 0 —— (0,0) 正好是「几内亚湾」那个占位坐标，等于把地点搬到海里）
    assert rows["华盛顿"]["centerLat"] is None
    assert rows["华盛顿"]["centerLon"] is None
    # ⚠️ 而「纽约」这一组的那张照片**自己带 GPS**（PH_BOTH），所以它有中心点。
    #    这是**有意的语义**：中心点来自照片的坐标事实，与「地点名从哪来」无关。
    #    别为了「目录名地点一律没有中心点」把这一列硬 NULL 掉 ——
    #    那是在丢 EXIF 里真实存在的坐标。正式库实测 11 个目录地点**全都没 GPS**，
    #    所以它们全部落 NULL（见 placeStore.placesWithoutCenter）。
    assert rows["纽约"]["centerLat"] is not None
    # nameZh 由 rebuildNameZh 单独负责，rebuildPlaces 绝不碰（这里本来就是 NULL）
    assert rows["华盛顿"]["nameZh"] is None
    # GPS 地点仍有坐标
    assert rows["CN, Beijing, Datun"]["centerLat"] is not None
    # 两种线索都有时**目录名赢**
    assert "CN, Beijing, Wangjing" not in rows


def test_liveAggregateAndFilterUseSameKey(placeEnv):
    """降级路径与筛选**必须**用同一条键（否则「列表 3 张、点进去 0 张」）。"""
    from database import queryCommon as query
    from processor.place import dirNamePlace, placeStore

    dirNamePlace.scanDirNames(dryRun=False)
    placeStore.rebuildPlaces()

    listed = [str(one.get("placeName")) for one in
              placeStore.liveAggregatePlaces(limitNum=0)]
    assert set(listed) == {"华盛顿", "纽约", "CN, Beijing, Datun"}, listed
    live = {str(one["placeName"]): int(one["photoCount"]) for one in
            placeStore.liveAggregatePlaces(limitNum=0)}
    assert live == {"华盛顿": 3, "纽约": 1, "CN, Beijing, Datun": 2}

    # 筛选：`?placeName=华盛顿` 必须命中那 3 张（它们的 pb_photo.placeName 是**空**）
    placeSql, placeValues = placeStore.resolvePlaceFilter("华盛顿")
    got = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p WHERE p.delFlag = %s AND "
        + placeSql, ("0",) + tuple(placeValues)) or 0)
    assert got == 3, (placeSql, placeValues)
    # 另一套值（英文 GPS 键）也仍然能用
    placeSql2, placeValues2 = placeStore.resolvePlaceFilter("CN, Beijing, Datun")
    got2 = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p WHERE p.delFlag = %s AND "
        + placeSql2, ("0",) + tuple(placeValues2)) or 0)
    assert got2 == 2


def test_placeKeyOfMatchesSql(placeEnv):
    """Python 口径 `placeKeyOf` 与 SQL 口径 `placeKeySql` **逐行一致**。

    ⚠️ 两份口径漂移的表现：照片列表（Python 算）与地点筛选（SQL 判）
      给出不同的地点 —— 两处都 200、都对得上自己，就是合不上。
    """
    from database import queryCommon as query
    from processor.place import placeStore

    sql = ("SELECT p.photoCode AS photoCode,"
           " " + placeStore.placeKeySql("p") + " AS placeKey,"
           " p.placeName AS placeName, p.placeNameDir AS placeNameDir"
           " FROM pb_photo p ORDER BY p.photoCode ASC")
    for one in query.selectList(sql):
        assert str(one.get("placeKey") or "") == \
            placeStore.placeKeyOf(one.get("placeName"), one.get("placeNameDir")), \
            one.get("photoCode")


# ============================================================
# 四、归零 / 手工行 / 两个报告
# ============================================================
def test_manualRowNeverZeroedAndSystemRowIs(placeEnv):
    """归档：**系统行归零、手工行永不归零**；旧行不删。"""
    from database import queryCommon as query
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import dirNamePlace, placeStore

    dirNamePlace.scanDirNames(dryRun=False)
    placeStore.rebuildPlaces()

    # ① 「纽约」标成手工行（模拟用户手工建档），再把它的照片搬去别的目录
    assert sqliteCommon.updateTableGeneral(
        "pb_place", "placeName = %s", ("纽约",), {"source": 1, "nameZh": "纽约市"}) > 0
    sqliteCommon.updateTableGeneral("pb_photo", "photoCode = %s", ("PH_BOTH",),
                                    {"placeNameDir": "波士顿"})
    # ② 「华盛顿」是**系统行**（source=0），把它的三张也搬走 -> 应该被归零
    for index in range(3):
        sqliteCommon.updateTableGeneral("pb_photo", "photoCode = %s",
                                        ("PH_WASH_%d" % index,),
                                        {"placeNameDir": "波士顿"})
    out = placeStore.rebuildPlaces()
    assert out["zeroed"] == 1, "系统行（华盛顿）该被归零"
    assert out["manualZeroSkipped"] == 1, "手工行（纽约）不该被归零"

    rows = {str(one["placeName"]): one for one in query.selectList(
        "SELECT placeName, photoCount, source, nameZh FROM pb_place"
        " WHERE delFlag = %s", ("0",))}
    # 旧行**一个都没删**
    assert "纽约" in rows and "华盛顿" in rows
    # 手工行：source 与 photoCount 都原样保留（照片搬走了也不归零）
    assert int(rows["纽约"]["source"]) == 1, "手工行被打回系统行"
    assert int(rows["纽约"]["photoCount"]) == 1, "手工行不该被归零"
    assert rows["纽约"]["nameZh"] == "纽约市"
    # 系统行：缓存值归零（否则地图上留一个「0 张照片却显示 3 张」的幽灵点）
    assert int(rows["华盛顿"]["photoCount"]) == 0
    # 搬过去的地方是新建行，4 张
    assert int(rows["波士顿"]["photoCount"]) == 4


def test_orphanNameZhIsReported(placeEnv):
    """孤儿 `nameZh`：**系统**行照片搬走后被归零，但中文名还挂着 -> 必须报出来。"""
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import dirNamePlace, placeStore

    dirNamePlace.scanDirNames(dryRun=False)
    placeStore.rebuildPlaces()
    # 用户手工给「纽约」填中文名（source 仍是 0 = 系统聚合行）
    sqliteCommon.updateTableGeneral("pb_place", "placeName = %s", ("纽约",),
                                    {"nameZh": "纽约市"})
    # 照片改名到另一个目录（**模拟目录被改名**）
    sqliteCommon.updateTableGeneral("pb_photo", "photoCode = %s", ("PH_BOTH",),
                                    {"placeNameDir": "华盛顿"})
    out = placeStore.rebuildPlaces()
    assert out["zeroed"] == 1
    assert out["orphanCount"] == 1
    assert str(out["orphans"][0]["placeName"]) == "纽约"
    assert str(out["orphans"][0]["nameZh"]) == "纽约市"
    # 独立的巡检入口也是同一批数据
    assert [str(one["placeName"]) for one in placeStore.orphanNameZh()] == ["纽约"]


def test_orphanReportIsEmptyWhenClean(placeEnv):
    """干净库里两个报告都为空（避免"永远报同一批"的假警报）。"""
    from processor.place import dirNamePlace, placeStore

    dirNamePlace.scanDirNames(dryRun=False)
    out = placeStore.rebuildPlaces()
    assert out["orphanCount"] == 0
    assert placeStore.orphanNameZh() == []
    assert placeStore.duplicateNameZhRows() == []
    # 无中心点的地点：只有「华盛顿」（那 3 张没有 GPS）。
    # 「纽约」的那张照片自带 GPS，所以它有中心点 —— 见上一条用例的说明。
    got = [str(one["placeName"]) for one in placeStore.placesWithoutCenter()]
    assert got == ["华盛顿"], got


def test_duplicateNameZhIsReported(placeEnv):
    """重名 `nameZh`（DR-36）：同一个中文名挂多行 -> **只报告、不归并**。"""
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import dirNamePlace, placeStore

    dirNamePlace.scanDirNames(dryRun=False)
    placeStore.rebuildPlaces()
    for name, zh in (("华盛顿", "北京市 · 朝阳区"), ("纽约", "北京市 · 朝阳区")):
        sqliteCommon.updateTableGeneral("pb_place", "placeName = %s", (name,),
                                        {"nameZh": zh})
    groups = placeStore.duplicateNameZhRows()
    assert len(groups) == 1
    assert str(groups[0]["nameZh"]) == "北京市 · 朝阳区"
    assert int(groups[0]["rowNum"]) == 2
    # placeCode 一个字节都没动（归并必须等用户定，DR-36）
    assert len(sqliteCommon.query_pb_place("pb_place", delFlag="0")) == 3


# ============================================================
# 五、顺序闸
# ============================================================
def test_pendingScanCountDrivesOrderGate(placeEnv):
    """顺序闸：该填没填时 `pendingScanCount` 必须报得出来（--rebuild-only 靠它拒绝）。"""
    from processor.place import dirNamePlace

    before = dirNamePlace.pendingScanCount()
    assert before["unfilled"] == 7 and before["candidates"] == 4, before
    dirNamePlace.scanDirNames(dryRun=False)
    after = dirNamePlace.pendingScanCount()
    assert after["candidates"] == 0, after
