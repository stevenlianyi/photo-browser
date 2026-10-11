#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_era_sampler.py
#Description: 修正步骤 R11 单测 —— 年代抽样（DR-45 ~ DR-50）
#
# 对应 plan/年代抽样规划.md 第九节验收要点。分两段：
#   A **纯函数**（不碰库）：口径 SQL、代表脸去重、权重、每年独立子种子、可复现
#   B **落库**（temp_db 临时库）：候选集口径、批次/明细、复现、只读性
#
# 全部跑在 pytest 的 tmp 临时库上，**绝不动 d:\PhotoLib 正式库**
#（conftest.guard_production_db 会话闸会当场判失败）。
#
# ⚠️ 为什么这个文件里的「反例」比「正例」多
#   抽样本身不会失败 —— 它只会**抽错**。所以每组口径都要有一条
#   「把那一项去掉就会抽到不该抽的那张」的断言，否则用例只证明了「跑得通」。

import pytest

from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from engine.sample import eraSampler as eraSampler

#: 纯函数用例用的候选行工厂（字段名与 candidateRows 的 SELECT 一一对应）
def faceRow(photoCode, faceCode=None, year=2000, quality=0.8, detScore=0.9,
            isConfirmed=0, bbox="0.1,0.1,0.3,0.3", faceCount=1):
    return {
        "photoCode": photoCode,
        "faceCode": faceCode or ("FC_%s" % photoCode),
        "quality": quality,
        "detScore": detScore,
        "bbox": bbox,
        "isConfirmed": isConfirmed,
        "effYear": year,
        "takenAt": None,
        "relPath": "%s.jpg" % photoCode,
        "faceCount": faceCount,
    }


# ============================================================
# 一、口径 SQL（§二 的 ①②③ 必须逐条钉在文本上）
# ============================================================

def test_candidateSql_hasAllFourClauses():
    sql = eraSampler.candidateSql(onlyConfirmed=False, excludeDuplicate=False)
    assert "f.personCode = %s" in sql
    assert "f.delFlag = %s" in sql and "p.delFlag = %s" in sql
    # 陌生人 = 永久排除（DR-16①），任何开关下都不许放进候选
    assert "f.isStranger = 0" in sql
    # 原图不在磁盘上的记录不抽（抽出来既不能打包也无法保证可信）
    assert "p.isMissing = 0" in sql


def test_candidateSql_yearIsEffectiveShotYear():
    """DR-42 同源：年份必须是 COALESCE(shotYearOverride, shotYear)，不是裸 shotYear。"""
    sql = eraSampler.candidateSql(False, False)
    assert "COALESCE(p.shotYearOverride, p.shotYear)" in sql
    assert " AS effYear" in sql


def test_candidateSql_switches():
    plain = eraSampler.candidateSql(onlyConfirmed=False, excludeDuplicate=False)
    assert "f.isConfirmed = %s" not in plain
    assert "p.isDuplicate = %s" not in plain
    strict = eraSampler.candidateSql(onlyConfirmed=True, excludeDuplicate=True)
    assert "f.isConfirmed = %s" in strict
    assert "p.isDuplicate = %s" in strict


# ============================================================
# 二、纯计算：年份标签 / bbox 面积 / 权重 / 代表脸
# ============================================================

@pytest.mark.parametrize("value,expect", [
    (1958, "1958"), ("2013", "2013"), (None, "NOYEAR"), ("", "NOYEAR"),
    # 越界 = 与「无年份」同档（与分桶口径一致：bucket.validShotYear 判越界 -> 空桶键）
    (1200, "NOYEAR"), (9999, "NOYEAR"), ("1900", "1900"),
])
def test_yearLabelOf(value, expect):
    assert eraSampler.yearLabelOf(value) == expect


@pytest.mark.parametrize("bbox,expect", [
    ("0.1,0.1,0.5,0.4", 0.2),
    ("", 0.0), ("junk", 0.0), ("0.1,0.1", 0.0),
    ("0,0,0,0", 0.0),                     # 零面积（坏框）不能算「小脸」以外的东西
    ("0,0,2,2", 1.0),                      # 越界要夹住，否则权重会被放大 4 倍
])
def test_bboxArea(bbox, expect):
    assert eraSampler.bboxArea(bbox) == pytest.approx(expect)


def test_weightOf_neverZero_andBoosted():
    zero = faceRow("A", quality=0, detScore=0, bbox="")
    weight = eraSampler.weightOf(zero)
    # 0 分也要有非零权重：否则「那一年唯一一张他出现过的照片」永远抽不到
    assert weight >= basicSettings.ERA_SAMPLE_MIN_WEIGHT

    confirmed = dict(zero, isConfirmed=1)
    assert eraSampler.weightOf(confirmed) == pytest.approx(
        weight * basicSettings.ERA_SAMPLE_CONFIRM_BOOST)

    # 画面占比大 -> 权重更高（压制「他站得很远的合影」）
    big = faceRow("B", quality=0.5, detScore=0.9, bbox="0.0,0.0,0.9,0.9")
    small = faceRow("C", quality=0.5, detScore=0.9, bbox="0.0,0.0,0.05,0.05")
    assert eraSampler.weightOf(big) > eraSampler.weightOf(small)


def test_representativeRows_keepsBestFacePerPhoto():
    rows = [faceRow("P1", "F_low", quality=0.2),
            faceRow("P2", "F_only", quality=0.5),
            faceRow("P1", "F_high", quality=0.9)]
    out = eraSampler.representativeRows(rows)
    assert len(out) == 2                              # 一张照片只算一次（口径 ②）
    byPhoto = {one["photoCode"]: one["faceCode"] for one in out}
    assert byPhoto["P1"] == "F_high"
    assert byPhoto["P2"] == "F_only"


def test_representativeRows_tieIsStable():
    """quality/detScore 全等时按 faceCode 兜底 —— 否则「同种子可复现」会失效。"""
    rows = [faceRow("P1", "F_b", quality=0.5), faceRow("P1", "F_a", quality=0.5)]
    assert eraSampler.representativeRows(rows)[0]["faceCode"] == "F_a"


# ============================================================
# 三、纯计算：抽样的三条性质（可复现 / 每年独立 / NOYEAR 排最后）
# ============================================================

def _multiCandidateRows():
    """2010 三年各 3 张候选 + 一个无年份 —— 够用来验证「真的在随机」。"""
    rows = []
    for year in (2008, 2009, 2010):
        for index in range(3):
            rows.append(faceRow("PH_%d_%d" % (year, index),
                                year=year, quality=0.3 + 0.1 * index))
    rows.append(faceRow("PH_NODATE", year=None))
    return rows


def test_planPicks_onePerYear_ascending_noYearLast():
    picks = eraSampler.planPicks(_multiCandidateRows(), mode="weighted",
                                 seed="s1", includeNoYear=True)
    assert [one["yearLabel"] for one in picks] == ["2008", "2009", "2010", "NOYEAR"]
    assert [one["pickOrder"] for one in picks] == [1, 2, 3, 4]
    assert all(one["candidateCount"] == 3 for one in picks[:3])
    assert picks[3]["shotYear"] is None


def test_planPicks_noYearHiddenByDefault():
    picks = eraSampler.planPicks(_multiCandidateRows(), mode="weighted", seed="s1")
    assert [one["yearLabel"] for one in picks] == ["2008", "2009", "2010"]


def test_planPicks_sameSeedReproducesExactly():
    first = eraSampler.planPicks(_multiCandidateRows(), mode="weighted", seed="seed-A")
    second = eraSampler.planPicks(_multiCandidateRows(), mode="weighted", seed="seed-A")
    assert [(one["yearLabel"], one["photoCode"]) for one in first] == \
           [(one["yearLabel"], one["photoCode"]) for one in second]


def test_planPicks_differentSeedPicksSomethingElse():
    """至少要有一年换了照片 —— 否则「重新抽」按钮是坏的（加权随机退化成 argmax）。"""
    pool = _multiCandidateRows()
    picks = {one["yearLabel"]: one["photoCode"]
             for one in eraSampler.planPicks(pool, mode="weighted", seed="s1")}
    other = {one["yearLabel"]: one["photoCode"]
             for one in eraSampler.planPicks(pool, mode="weighted", seed="s2")}
    assert picks != other


def test_planPicks_yearRangeDoesNotDisturbOtherYears():
    """**每年一个独立子种子**的直接验证。

    如果改成「一条随机流顺序取」，把范围收窄到只留 2009 会让 2010 也跟着变 ——
    用户会认为这个功能不可信（同样的 2010，凭什么换个筛选就换照片）。
    """
    pool = _multiCandidateRows()
    allYears = {one["yearLabel"]: one["photoCode"]
                for one in eraSampler.planPicks(pool, mode="weighted", seed="s1")}
    narrowed = {one["yearLabel"]: one["photoCode"]
                for one in eraSampler.planPicks(pool, mode="weighted", seed="s1",
                                                yearFrom=2009, yearTo=2010)}
    assert set(narrowed) == {"2009", "2010"}
    assert narrowed["2010"] == allYears["2010"]


def test_planPicks_uniformMeansEqualProbability():
    picks = eraSampler.planPicks(_multiCandidateRows(), mode="uniform", seed="s1")
    for one in picks[:3]:
        assert one["weight"] == pytest.approx(1.0)
        assert one["weightSum"] == pytest.approx(3.0)
        assert one["weight"] / one["weightSum"] == pytest.approx(1.0 / 3)


def test_planPicks_singleCandidateProbabilityIsOne():
    picks = eraSampler.planPicks([faceRow("PH_ONLY", year=1999)], mode="weighted",
                                 seed="s1")
    assert len(picks) == 1
    assert picks[0]["candidateCount"] == 1
    # 唯一候选：weight == weightSum -> 概率 1.0（**不是 NULL** —— 含义不含糊）
    assert picks[0]["weight"] == pytest.approx(picks[0]["weightSum"])


def test_planPicks_refusesAbsurdYearCount():
    """年档数超上限要**抛错**，不许静默少抽（那会给出一个「少了几年的队列」）。"""
    rows = [faceRow("PH_%d" % y, year=1900 + y) for y in range(5)]
    with pytest.raises(eraSampler.EraSampleError):
        eraSampler.planPicks(rows, mode="weighted", seed="s1", maxYears=3)


def test_candidateYears_ignoresYearRange():
    years = eraSampler.candidateYears(_multiCandidateRows(), includeNoYear=False)
    assert years == ["2008", "2009", "2010"]
    assert eraSampler.candidateYears(_multiCandidateRows(), includeNoYear=True)[-1] == "NOYEAR"


# ============================================================
# 四、参数归一（非法值必须抛，不许静默回落）
# ============================================================

def test_normalizeMode_rejectsUnknown():
    assert eraSampler.normalizeMode("") == basicSettings.ERA_SAMPLE_MODE_DEFAULT
    assert eraSampler.normalizeMode("UNIFORM") == "uniform"
    with pytest.raises(eraSampler.EraSampleError):
        eraSampler.normalizeMode("random")


def test_normalizeGrain_rejectsBucket():
    """bucket 是 DR-45 的预留位：**接受但按 year 做**会让界面撒谎。"""
    assert eraSampler.normalizeGrain("") == "year"
    with pytest.raises(eraSampler.EraSampleError):
        eraSampler.normalizeGrain("bucket")


def test_normalizeYearBound():
    assert eraSampler.normalizeYearBound(None, "yearFrom") is None
    assert eraSampler.normalizeYearBound("", "yearFrom") is None
    assert eraSampler.normalizeYearBound("1970", "yearFrom") == 1970
    with pytest.raises(eraSampler.EraSampleError):
        eraSampler.normalizeYearBound("很久以前", "yearFrom")


# ============================================================
# 五、落库（temp_db 临时库）
# ============================================================

ERAS = ["PH_1998", "PH_2003", "PH_2011"]


def addPerson(code="P_era", name="年代人", birthday=None):
    sqliteCommon.insertManyTableGeneral(
        "pb_person", [{"personCode": code, "displayName": name, "birthday": birthday,
                       "source": 0, "isConfirmed": 0}],
        conflictColumns=("personCode",), fillStandard=True)
    return code


def addPhoto(code, shotYear=None, override=None, isDuplicate=0, isMissing=0,
             delFlag="0", faceCount=0):
    sqliteCommon.insertManyTableGeneral(
        "pb_photo", [{"photoCode": code, "relPath": "%s.jpg" % code,
                      "relPathHash": (code + "_path").ljust(64, "0")[:64],
                      "fileHash": (code + "_file").ljust(64, "1")[:64],
                      "fileSize": 1024, "shotYear": shotYear,
                      "shotYearOverride": override, "isDuplicate": isDuplicate,
                      "isMissing": isMissing, "faceCount": faceCount,
                      "delFlag": delFlag, "takenAt": None}],
        fillStandard=True, forceColumns=("shotYear", "shotYearOverride"))


def addFace(code, photoCode, person=None, quality=0.8, detScore=0.9,
            confirmed=0, stranger=0, bbox="0.1,0.1,0.3,0.3", delFlag="0"):
    sqliteCommon.insertManyTableGeneral(
        "pb_face", [{"faceCode": code, "photoCode": photoCode, "personCode": person,
                     "quality": quality, "detScore": detScore, "bbox": bbox,
                     "isConfirmed": confirmed, "isStranger": stranger,
                     "delFlag": delFlag, "shotBucket": "2000-2004"}],
        fillStandard=True, forceColumns=("personCode", "bbox", "shotBucket"))


def seedLibrary(person="P_era"):
    """一份最小但覆盖每条口径的库：三年各一张 + 各类该被排除的记录。"""
    addPerson(person)
    for year, code in zip([1998, 2003, 2011], ERAS):
        addPhoto(code, shotYear=year, faceCount=1)
        addFace("FC_%s" % code, code, person=person, confirmed=1)

    # 该被排除的四种
    addPhoto("PH_STRANGER", shotYear=1999, faceCount=1)
    addFace("FC_STRANGER", "PH_STRANGER", person=None, stranger=1)
    addPhoto("PH_DUP", shotYear=2004, isDuplicate=1, faceCount=1)
    addFace("FC_DUP", "PH_DUP", person=person)
    addPhoto("PH_MISSING", shotYear=2005, isMissing=1, faceCount=1)
    addFace("FC_MISSING", "PH_MISSING", person=person)
    addPhoto("PH_DELETED", shotYear=2006, delFlag="1", faceCount=1)
    addFace("FC_DELETED", "PH_DELETED", person=person)
    # 别人的脸不能进他的抽样
    addPhoto("PH_OTHER", shotYear=2007, faceCount=1)
    addFace("FC_OTHER", "PH_OTHER", person="P_other")
    # 同一年两张候选（用来验证「一年只留一张」）
    addPhoto("PH_2003_B", shotYear=2003, faceCount=1)
    addFace("FC_2003_B", "PH_2003_B", person=person, quality=0.95)


def yearsOf(batchCode):
    return [str(one["yearLabel"]) for one in eraSampler.getBatch(batchCode)["items"]]


def test_generate_writesBatchAndItems(temp_db):
    seedLibrary()
    info = eraSampler.generate("P_era", mode="weighted", seed="unit-seed")
    assert info["batchCode"].startswith(basicSettings.ERA_SAMPLE_CODE_PREFIX + "_")
    assert info["photoCount"] == 3

    got = eraSampler.getBatch(info["batchCode"])
    batch, items = got["batch"], got["items"]
    assert int(batch["photoCount"]) == 3
    assert int(batch["candidatePhotoCount"]) == 4          # 三年各一张 + 2003 的第二张
    assert int(batch["candidateYearCount"]) == 3
    assert str(batch["randomSeed"]) == "unit-seed"
    assert str(batch["personName"]) == "年代人"             # 名字快照
    assert [str(one["yearLabel"]) for one in items] == ["1998", "2003", "2011"]
    assert [int(one["pickOrder"]) for one in items] == [1, 2, 3]
    # 明细里带上了 pb_photo 的展示字段（JOIN 出来，不冗余进表）
    assert str(items[0]["relPath"]) == "PH_1998.jpg"
    assert int(items[0]["candidateCount"]) == 1            # 1998 年只有这一张，没得选
    assert int(items[1]["candidateCount"]) == 2            # 2003 年两张（验证真的在抽）
    # 抽中概率 = weight / weightSum（两种模式同一套解释，见 planPicks 注释）：
    # 唯一候选恒为 1.0，多候选落在 (0, 1) —— 且**权重从不缺失**，
    # 于是不会出现「NULL 到底是唯一候选还是没记」的歧义。
    for one in items:
        probability = float(one["weight"]) / float(one["weightSum"])
        assert 0.0 < probability <= 1.0
        if int(one["candidateCount"]) == 1:
            assert probability == pytest.approx(1.0)


def test_itemKey_isBatchCodeColonYear(temp_db):
    """幂等键的形状：`<batchCode>:<yearLabel>`（重跑同一批靠它去重）。"""
    seedLibrary()
    code = eraSampler.generate("P_era", seed="s")["batchCode"]
    rows = sqliteCommon.query_pb_era_sample_item(
        "pb_era_sample_item", batchCode=code, orderBy="pickOrder")
    assert [str(one["itemKey"]) for one in rows] == [
        "%s:1998" % code, "%s:2003" % code, "%s:2011" % code]


def test_generate_excludesStrangerDuplicateMissingDeletedAndOthers(temp_db):
    seedLibrary()
    code = eraSampler.generate("P_era", seed="s")["batchCode"]
    assert yearsOf(code) == ["1998", "2003", "2011"]
    picked = {str(one["photoCode"]) for one in eraSampler.getBatch(code)["items"]}
    # 陌生人脸 / 重复件 / 原图缺失 / 软删照片 / 别人的脸 —— 一个都不许进来
    assert picked.isdisjoint({"PH_STRANGER", "PH_DUP", "PH_MISSING",
                              "PH_DELETED", "PH_OTHER"})


def test_generate_yearRangeOnlyNarrowsTheQueue(temp_db):
    seedLibrary()
    code = eraSampler.generate("P_era", seed="s", yearFrom=2000,
                               yearTo=2020)["batchCode"]
    got = eraSampler.getBatch(code)
    assert [str(one["yearLabel"]) for one in got["items"]] == ["2003", "2011"]
    # 候选年份数**不受区间影响**（否则用户会以为他的照片变少了）
    assert int(got["batch"]["candidateYearCount"]) == 3


def test_generate_groupsByEffectiveYear(temp_db):
    """DR-42：shotYearOverride 优先 —— 否则翻拍件会落进错误的年份。"""
    addPerson()
    addPhoto("PH_REFIX", shotYear=2019, override=1958, faceCount=1)
    addFace("FC_REFIX", "PH_REFIX", person="P_era", confirmed=1)
    code = eraSampler.generate("P_era", seed="s")["batchCode"]
    got = eraSampler.getBatch(code)
    assert [str(one["yearLabel"]) for one in got["items"]] == ["1958"]
    assert int(got["items"][0]["shotYear"]) == 1958


def test_generate_countsPhotoOnceWithTwoFaces(temp_db):
    """口径 ②：一张合影里同一个人 2 张脸只算一次候选。"""
    addPerson()
    addPhoto("PH_TWO", shotYear=2001, faceCount=2)
    addFace("FC_TWO_A", "PH_TWO", person="P_era", quality=0.4)
    addFace("FC_TWO_B", "PH_TWO", person="P_era", quality=0.9)
    code = eraSampler.generate("P_era", seed="s")["batchCode"]
    batch = eraSampler.getBatch(code)["batch"]
    assert int(batch["candidatePhotoCount"]) == 1
    assert int(batch["photoCount"]) == 1


def test_generate_includeNoYear(temp_db):
    addPerson()
    addPhoto("PH_NODATE", shotYear=None, faceCount=1)
    addFace("FC_NODATE", "PH_NODATE", person="P_era")
    without = eraSampler.generate("P_era", seed="s")["batchCode"]
    assert eraSampler.getBatch(without)["items"] == []

    withNoYear = eraSampler.generate("P_era", seed="s",
                                    includeNoYear=True)["batchCode"]
    items = eraSampler.getBatch(withNoYear)["items"]
    assert [str(one["yearLabel"]) for one in items] == [basicSettings.ERA_SAMPLE_NOYEAR_LABEL]
    assert items[0]["shotYear"] is None


def test_generate_onlyConfirmed(temp_db):
    addPerson()
    addPhoto("PH_AUTO", shotYear=2001, faceCount=1)
    addFace("FC_AUTO", "PH_AUTO", person="P_era", confirmed=0)
    addPhoto("PH_OK", shotYear=2002, faceCount=1)
    addFace("FC_OK", "PH_OK", person="P_era", confirmed=1)
    loose = eraSampler.generate("P_era", seed="s")["batchCode"]
    assert yearsOf(loose) == ["2001", "2002"]
    strict = eraSampler.generate("P_era", seed="s", onlyConfirmed=True)["batchCode"]
    assert yearsOf(strict) == ["2002"]


def test_generate_noCandidate_isNotAnError(temp_db):
    """「这个人没有带年份的照片」是要如实告诉用户的事实，不是失败。"""
    addPerson()
    info = eraSampler.generate("P_era", seed="s")
    assert info["photoCount"] == 0
    got = eraSampler.getBatch(info["batchCode"])
    assert got["items"] == []
    assert int(got["batch"]["photoCount"]) == 0


def test_generate_unknownPersonRaises(temp_db):
    with pytest.raises(eraSampler.EraSampleError):
        eraSampler.generate("P_nobody", seed="s")


def test_generate_badRangeRaises(temp_db):
    addPerson()
    with pytest.raises(eraSampler.EraSampleError):
        eraSampler.generate("P_era", seed="s", yearFrom=2010, yearTo=1990)


def test_generate_reproducibleThroughDb(temp_db):
    """同参数同种子 -> 库里两批明细逐字相同（DR-47 的可复现承诺）。"""
    seedLibrary()
    first = eraSampler.generate("P_era", mode="weighted", seed="dup-seed")["batchCode"]
    second = eraSampler.generate("P_era", mode="weighted", seed="dup-seed")["batchCode"]
    assert first != second
    assert _picksOf(first) == _picksOf(second)


def _picksOf(batchCode):
    return [(str(one["yearLabel"]), str(one["photoCode"]))
            for one in eraSampler.getBatch(batchCode)["items"]]


def test_generate_isReadOnly(temp_db):
    """验收第 14 条：抽样**一个字都不改**（含不写 pb_review_log）。"""
    seedLibrary()
    before = _faceSnapshot()
    photosBefore = _photoSnapshot()
    eraSampler.generate("P_era", seed="s")
    assert _faceSnapshot() == before
    assert _photoSnapshot() == photosBefore
    assert sqliteCommon.countTableGeneral("pb_review_log", delFlag="*") == 0


def _faceSnapshot():
    """脸表的五列快照（归属 / 两种标记 / 桶 / 软删）—— 抽样碰了任何一列都该被判红。"""
    return sorted((str(r["faceCode"]), str(r["personCode"] or ""),
                   int(r["isConfirmed"]), int(r["isStranger"]),
                   str(r["shotBucket"] or ""), str(r["delFlag"] or ""))
                  for r in sqliteCommon.query_pb_face("pb_face", delFlag="*"))


def _photoSnapshot():
    return sorted((str(r["photoCode"]), r.get("shotYear"), r.get("shotYearOverride"),
                   int(r["faceCount"]))
                  for r in sqliteCommon.query_pb_photo("pb_photo", delFlag="*"))


def test_listBatches_newestFirst(temp_db):
    seedLibrary()
    first = eraSampler.generate("P_era", seed="a")["batchCode"]
    second = eraSampler.generate("P_era", seed="b")["batchCode"]
    codes = [str(one["batchCode"]) for one in eraSampler.listBatches("P_era")]
    assert codes == [second, first]           # 最近生成的那一次在最前面


def test_generate_missingTables_saysRunMigrate(temp_db):
    """老库没跑迁移 -> **可读的一句话 + 那条命令**，不是 `no such table`。

    这不是假设的场景：建表 DDL 是 `CREATE TABLE IF NOT EXISTS`，
    老库不会因为 .txt 加了表就自动多出它（DR-13），而迁移是**手工一步**。
    手工 E2E 第一次跑就撞上了它 —— 当时的报错是一长串
    `executeWrite:no such table: pb_era_sample`，没有任何指向性。
    """
    addPerson()
    addPhoto("PH_X", shotYear=2000, faceCount=1)
    addFace("FC_X", "PH_X", person="P_era", confirmed=1)
    sqliteCommon.drop_pb_era_sample("pb_era_sample")
    sqliteCommon.drop_pb_era_sample_item("pb_era_sample_item")
    with pytest.raises(eraSampler.EraDbError) as err:
        eraSampler.generate("P_era", seed="s")
    message = str(err.value)
    assert "build_db.py" in message and "--migrate" in message
    # ⚠️ 必须是 EraDbError（api 层据此回 500），不能退化成父类的 400
    assert isinstance(err.value, eraSampler.EraSampleError)


def test_updateZipState_roundTrip(temp_db):
    seedLibrary()
    code = eraSampler.generate("P_era", seed="a")["batchCode"]
    assert eraSampler.updateZipState(code, "READY", zipRelName="era-sample/x.zip",
                                     zipSize=1234, zipPhotoCount=3,
                                     zipMissingCount=0) is True
    batch = eraSampler.batchRow(code)
    assert str(batch["zipState"]) == "READY"
    assert int(batch["zipSize"]) == 1234
    assert str(batch["zipRelName"]) == "era-sample/x.zip"
    assert batch["zipYMDHMS"]
    assert eraSampler.updateZipState("ES_nope", "READY") is False
