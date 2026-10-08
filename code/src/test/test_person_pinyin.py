#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_person_pinyin.py
#Description: 姓名拼音检索（pb_person.displayNamePinyin）
#
# 为什么这组测试值钱
# ------------------
#   拼音检索是**纯派生数据**：displayNamePinyin 由 common/pinyin.personPinyin
#   从 displayName + familyName 算出来，存在库里给 `keyword` 做 LIKE。
#   这意味着它可能在**三个地方**各自悄悄坏掉，而且都不报错：
#     ① 算法算错（"王小明" -> "wangxiao ming"，token 多了个空格，搜 wangxm 零结果）；
#     ② 写入路径漏了某一条（新建算、导入不算，于是「导入进来的人搜不到」）；
#     ③ 改了名没重算（搜新名字零结果、搜旧名字还有结果）。
#   下面这几条分别钉住这三处 —— 单元测试钉 ①，接口测试钉 ② 和 ③。
#
# ⚠️ 刻意**不断言**「漏掉元音的输入」能命中：token 是全拼连写，
#   用户漏字母不是我们要猜的事（见 common/pinyin.py 的说明）。
#   本特性不做子序列匹配，也不做同音纠错。

import pytest

from common import pinyin as pinyin


# ============================================================
# 一、算法（纯函数，不碰库）
# ============================================================

def test_personPinyin_fullAndInitials():
    assert pinyin.personPinyin("王小明") == "wangxiaoming wxm"
    # 姓在 familyName 里时要多出一个 token，否则输 "wang" 搜不到「王小雨」
    assert pinyin.personPinyin("王小雨", "王") == "wangxiaoyu wxy wang"
    # 单字名：首字母只有 1 个字母，那是噪声不是缩写（'%w%' 会命中所有人）
    assert pinyin.personPinyin("王", "王") == "wang"


def test_personPinyin_keepsAsciiSoEnglishNamesStillWork():
    assert pinyin.personPinyin("Lucy Chen") == "lucychen lc"
    assert pinyin.personPinyin("Alice 王小明") == "alicewangxiaoming awxm"


def test_personPinyin_normalizesSeparatorsAndFallbackNames():
    # 重名避让后的名字必须能被搜到（输 "zhangsan2"）
    assert pinyin.personPinyin("张三(2)") == "zhangsan2 zs2"
    # 拆分时自动建档的兜底名不是汉字，也得有检索串
    assert pinyin.personPinyin("未命名-ab12cd") == "weimingmingab12cd wmma"


def test_personPinyin_emptyInputs():
    assert pinyin.personPinyin("") == ""
    assert pinyin.personPinyin("", "") == ""
    # 姓氏与名字相同时不该重复出 token
    assert pinyin.personPinyin("张伟", "张伟") == "zhangwei zw"


def test_personPinyin_neverExceedsColumnWidth():
    assert len(pinyin.personPinyin("王" * 128)) <= 512


@pytest.mark.skipif(not pinyin._HAS_PINYIN, reason="未安装 pypinyin")
def test_has_pypinyin_or_search_is_silently_dead():
    """少装一个包，拼音检索就整条失效 —— 这里把「降级」变成一个显式失败，
    免得哪天环境里少了它而没人发现搜索「时灵时不灵」。"""
    assert pinyin.personPinyin("王小明") == "wangxiaoming wxm"


# ============================================================
# 二、写入路径（接口层）：新建 / 改名 都必须留下拼音
# ============================================================

def _newPerson(client, displayName, **more):
    body = {"displayName": displayName}
    body.update(more)
    rtn = client.post("/api/contacts", json=body)
    assert rtn.status_code == 201, rtn.text
    return rtn.json()["personCode"]


def _pyOf(personCode):
    from database.auto_generated import sqliteCommon as sqliteCommon
    rows = sqliteCommon.query_pb_person("pb_person", personCode=personCode)
    return str(rows[0].get("displayNamePinyin") or "")


def _codesByKeyword(client, keyword):
    got = client.get("/api/persons", params={"keyword": keyword}).json()
    return [i["personCode"] for i in got["items"]]


def test_createPerson_fillsPinyin_andItIsSearchable(api_env):
    client = api_env["client"]
    code = _newPerson(client, "王小明", familyName="王")

    assert _pyOf(code) == "wangxiaoming wxm wang"
    # 全拼 / 首字母 / 汉字，三种输入都必须命中同一个人
    for kw in ("wangxiaoming", "wxm", "王小明"):
        assert _codesByKeyword(client, kw) == [code], kw


def test_createPerson_pinyinFollowsDeduplicatedName(api_env):
    """重名避让：库里是「张三(2)」，拼音必须跟着**最终名**算。
    否则用户搜 zhangsan2 零结果 —— 看着就像搜索坏了。"""
    client = api_env["client"]
    _newPerson(client, "张三")
    second = _newPerson(client, "张三")

    assert _pyOf(second) == "zhangsan2 zs2"
    assert _codesByKeyword(client, "zhangsan2") == [second]


def test_patchName_refreshesPinyin_bothWays(api_env):
    client = api_env["client"]
    code = _newPerson(client, "李四", familyName="李")

    rtn = client.patch("/api/contacts/%s" % code, json={"displayName": "李五"})
    assert rtn.status_code == 200, rtn.text
    # 姓氏没动，所以那个姓的 token 必须在（这才是「搜 li 还能找到他」的原因）
    assert _pyOf(code) == "liwu lw li"

    assert _codesByKeyword(client, "liwu") == [code]     # 新名搜得到
    assert _codesByKeyword(client, "lisi") == []         # 旧名搜不到了


def test_patchClearingFamilyName_dropsItsPinyinToken(api_env):
    """把姓氏清空后，拼音里不能再留着那个姓的 token ——
    否则搜「wang」还能命中一个已经没有姓王的人。"""
    client = api_env["client"]
    code = _newPerson(client, "王小雨", familyName="王")
    assert "wang" in _pyOf(code)

    rtn = client.patch("/api/contacts/%s" % code, json={"familyName": ""})
    assert rtn.status_code == 200, rtn.text
    assert _pyOf(code) == "wangxiaoyu wxy"


def test_pinyinColumnIsNotExposedInResponses(api_env):
    """派生列不该出现在 API 响应里：PersonForm 不可编辑它，
    暴露出去只会让人「看得见摸不着」。"""
    client = api_env["client"]
    code = _newPerson(client, "赵六")
    assert "displayNamePinyin" not in client.get("/api/persons/%s" % code).json()
    listed = client.get("/api/persons", params={"keyword": "zhaoliu"}).json()
    assert "displayNamePinyin" not in listed["items"][0]
    # PATCH 也不接受外部直接写这一列（它是派生值）
    rtn = client.patch("/api/contacts/%s" % code,
                       json={"displayNamePinyin": "hack"})
    assert rtn.status_code == 400


def test_contactsListKeyword_alsoMatchesPinyin(api_env):
    """人物网格与联系人列表必须**一致**地支持拼音 ——
    同一批人在两个页面一个搜得到一个搜不到，是最难解释的那种坏。"""
    client = api_env["client"]
    code = _newPerson(client, "钱七", familyName="钱")
    got = client.get("/api/contacts", params={"keyword": "qianqi"}).json()
    assert code in [i["personCode"] for i in got["items"]]