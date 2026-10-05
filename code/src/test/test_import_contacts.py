#! /usr/bin/env python3
#encoding: utf-8
#Filename: test_import_contacts.py
#Description: 通讯录导入（vCard -> pb_person）单测：解析 / 幂等 / 撞车分级
#
# 重点测三件出错后**不报错**的事：
#   ① 解析：折行、参数、值里的分号、BDAY 的多种写法
#   ② 幂等：同一批 vcf 导两次**不能**建出两个人（真实导出普遍没有 UID）
#   ③ 撞车分级：同邮箱 / 同号同名 = 同一人（跳过）；同号不同名 = 共用座机（照常导）

import os
import shutil

import pytest

from common import globalDefinition as comGD
from database.auto_generated import sqliteCommon as sqliteCommon
from tools import import_contacts as ic

CARD_MIN = (u"BEGIN:VCARD\nVERSION:2.1\nN:Bai;Qing;;;\nFN:Qing Bai\n"
            u"TEL;CELL:18516181670\nEMAIL:qing_bai@hotmail.com\n"
            u"BDAY:1971-02-10\nEND:VCARD\n")

CARD_NOUID = (u"BEGIN:VCARD\nVERSION:2.1\nN:Lian;Steven;;;\nFN:Steven Lian\n"
              u"TEL;CELL:13910710766\nBDAY:1969-12-17T00:00:00Z\nEND:VCARD\n")

CARD_TZ = u"BEGIN:VCARD\nVERSION:3.0\nFN:Only Time\nBDAY:20070514\nEND:VCARD\n"
CARD_YEAR = u"BEGIN:VCARD\nVERSION:3.0\nFN:Year Only\nBDAY:1988\nEND:VCARD\n"
CARD_MD = u"BEGIN:VCARD\nVERSION:3.0\nFN:Month Day Only\nBDAY:--0210\nEND:VCARD\n"

CARD_PHOTO = (u"BEGIN:VCARD\nVERSION:2.1\nFN:With Photo\n"
               u"PHOTO;ENCODING=BASE64;JPEG:/9j/4AAQSkZJRg==\nEND:VCARD\n")

CARD_ADDR = (u"BEGIN:VCARD\nVERSION:2.1\nFN:Addr Test\n"
             u"ADR;WORK;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:"
             u";;=E4=B8=8A=E6=B5=B7\n =E6=B5=A6=E4=B8=9C;;2012;;;\n"
             u"END:VCARD\n")


@pytest.fixture
def lib(temp_db):
    sqliteCommon.dbHandle(temp_db)
    return temp_db


def writeVcf(dirPath, name, text):
    path = "%s/%s" % (dirPath, name)
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))
    return path


# ============================================================
# 一、解析
# ============================================================

class TestParse:
    def test_basic_fields(self, tmp_path):
        got = ic.readContact(writeVcf(str(tmp_path), "0154_Qing Bai.vcf",
                                      CARD_MIN))
        assert got["displayName"] == "Qing Bai"
        assert got["familyName"] == "Bai"
        assert got["phone"] == "18516181670"
        assert got["email"] == "qing_bai@hotmail.com"
        assert got["birthday"] == "1971-02-10"
        assert got["hasBirthYear"] is True
        assert got["uidFromFile"] is True, "没有 UID 时要用文件名兜底"
        assert got["vcardUid"] == "file:0154_Qing Bai"

    def test_continuation_line_is_joined(self, tmp_path):
        """折行必须接上，否则地址只剩半截（而解析不报错）"""
        adr = ic.collect(ic.parseCards(CARD_ADDR)[0])["ADR"][0][1]
        assert "=E6=B5=A6=E4=B8=9C" in adr, "折行没接上：%r" % adr

    def test_semicolon_in_value_is_not_a_param(self):
        """值里的分号不能被当成参数分隔符（ADR 街道/城市/邮编就是分号隔的）"""
        keys, value = ic.splitValue(u"ADR;WORK;CHARSET=UTF-8:;;b;;c;;d")
        assert [k[0] for k in keys] == ["ADR", "WORK", "CHARSET"]
        assert keys[2][1] == "UTF-8"
        assert value == ";;b;;c;;d", "值被截断了：%r" % value

    def test_bday_formats(self, tmp_path):
        cases = [("t.vcf", CARD_TZ, "2007-05-14", True),
                 ("y.vcf", CARD_YEAR, "1988-01-01", True),
                 ("m.vcf", CARD_MD, "", False)]
        for name, text, wantBday, wantYear in cases:
            got = ic.readContact(writeVcf(str(tmp_path), name, text))
            assert got["birthday"] == wantBday, text[:24]
            assert got["hasBirthYear"] is wantYear, text[:24]

    def test_month_day_only_keeps_raw(self, tmp_path):
        """只有月日时年份未知：**不能**把 '--0210' 塞进 birthday。

        塞进去会让 birthYearOf 拿到非法年份 -> 静默走等宽降级，
        而用户以为走的是自适应分桶。
        """
        got = ic.readContact(writeVcf(str(tmp_path), "md.vcf", CARD_MD))
        assert got["birthday"] == "" and got["bdayRaw"] == "--0210"
        assert "无年份" in ic._memoOf(got)

    def test_multiple_cards_in_one_file(self, lib, tmp_path):
        """一个文件两张卡 = 两个联系人，都要导（只导第一张会静默丢人）

        ⚠️⚠️ `lib` 夹具不是可选的，**去掉它这个用例会静默读到正式库**
        ------------------------------------------------------------
          本用例原来只写 `tmp_path`，没要`lib` —— 于是 plan() 里的
          loadIndex() 通过 dbHandle() **懒加载了 d:\\PhotoLib\\db\\photolib.db**。
          那库里已经有从同一份通讯录导进去的 Qing Bai(18516181670 /
          qing_bai@hotmail.com) 与 Steven Lian(13910710766)，
          于是 _planOne 正确地判成「疑似同一人，已跳过」-> actions == 0。
          ⇒ 失败的不是产品，是**单测在读生产数据**。
            更糟的是它只差一步就是**单测往生产库里写人**（plan 后面接 apply）。
          本类其余用例（test_basic_fields 等）只解析不落库，不受污染 ——
          但只要哪天给它们也加一句 plan()/apply()，同一个坑就会再踩一次。
        """
        writeVcf(str(tmp_path), "two.vcf", CARD_MIN + CARD_NOUID)
        got = ic.readContacts(writeVcf(str(tmp_path), "x.vcf",
                                       CARD_MIN + CARD_NOUID))
        assert [c["displayName"] for c in got] == ["Qing Bai", "Steven Lian"]
        assert got[0]["vcardUid"] != got[1]["vcardUid"], \
            "同文件第二张卡不能与第一张撞幂等键"
        writeVcf(str(tmp_path), "two.vcf", CARD_MIN + CARD_NOUID)
        planned = ic.plan(str(tmp_path))
        assert [a["op"] for a in planned["actions"]] == ["create", "create"]
        assert len(planned["actions"]) == 2

    def test_card_without_name_is_skipped_with_warning(self, lib, tmp_path):
        """同样必须带 `lib`：否则这条断言会因为「库里正好有同名同号的人」
        而**碰巧**通过 —— 绿灯不代表它在测想测的东西。"""
        writeVcf(str(tmp_path), "bad.vcf",
                 u"BEGIN:VCARD\nVERSION:2.1\nEND:VCARD\n")
        planned = ic.plan(str(tmp_path))
        assert not planned["actions"]
        assert any("没有可用" in w for w in planned["warnings"])


# ============================================================
# 二、幂等（真实导出普遍没有 UID，这是最要紧的一条）
# ============================================================

class TestIdempotent:
    def _root(self, tmp_path):
        writeVcf(str(tmp_path), "0154_Qing Bai.vcf", CARD_MIN)
        writeVcf(str(tmp_path), "0702_Steven Lian.vcf", CARD_NOUID)
        return str(tmp_path)

    def test_second_run_updates_instead_of_duplicating(self, lib, tmp_path):
        root = self._root(tmp_path)
        first = ic.plan(root)
        assert [a["op"] for a in first["actions"]] == ["create", "create"]
        ic.apply(first)
        assert sqliteCommon.countTableGeneral("pb_person", delFlag="*") == 2

        second = ic.plan(root)
        assert [a["op"] for a in second["actions"]] == ["update", "update"], \
            "第二次导入必须全是更新"
        result = ic.apply(second)
        assert not result["created"], "又建了新档案"
        assert len(result["updated"]) == 2
        assert sqliteCommon.countTableGeneral("pb_person", delFlag="*") == 2

    def test_update_does_not_wipe_edited_fields(self, lib, tmp_path):
        """只补空、不覆盖：用户手工填的内容不能被一次缺字段的导入冲掉"""
        root = self._root(tmp_path)
        ic.apply(ic.plan(root))
        code = "VC_0154_Qing_Bai"
        sqliteCommon.insertManyTableGeneral(
            "pb_person",
            [{"personCode": code, "displayName": "Qing Bai",
              "memo": "用户自己写的备注"}],
            conflictColumns=("personCode",), updateColumns=("memo",),
            fillStandard=True)
        writeVcf(root, "0154_Qing Bai.vcf",
                 u"BEGIN:VCARD\nVERSION:2.1\nFN:Qing Bai\n"
                 u"BDAY:1971-02-10\nEND:VCARD\n")
        ic.apply(ic.plan(root))
        row = sqliteCommon.query_pb_person("pb_person", personCode=code)[0]
        assert row["memo"] == "用户自己写的备注", "memo 被空值覆盖了"
        assert row["phone"] == "18516181670", "已有电话被覆盖成空了"

    def test_renamed_file_does_not_silently_duplicate(self, lib, tmp_path):
        """改名后再导：文件名兜底键失效，但同邮箱+同名仍要认出来并跳过"""
        root = self._root(tmp_path)
        ic.apply(ic.plan(root))
        os.remove("%s/0154_Qing Bai.vcf" % root)
        writeVcf(root, "0154_白晴.vcf", CARD_MIN)
        planned = ic.plan(root)
        assert not [a for a in planned["actions"]
                    if a["contact"]["displayName"] == "Qing Bai"], \
            "同邮箱+同名被当成了新的人"
        assert any("疑似同一人" in w for w in planned["warnings"]), \
            "撞车却没有提示：%s" % planned["warnings"]


    def test_failed_write_lands_in_failed_not_updated(self, lib, tmp_path,
                                                       monkeypatch):
        """写失败**必须**进 failed，绝不能进 updated。

        为什么这条比它看起来重要
        ----------------------
          insertManyTableGeneral 返回的是 **(rtn, columnNames) 元组**，而
          apply() 里写的是 `rtn = sqliteCommon.insertManyTableGeneral(...)`
          —— 没解包。于是 `rtn == -2` 永远不成立，
          哪怕 SQLite 报了 `NOT NULL constraint failed` 也照样
          `updated.append(code)`。对调用方（CLI）来说就是：
          「导入完成，更新 2027 个人」，而库里**一条都没变**。
          用户改了通讯录里的手机号重新导入 -> 库里的号码永远不变，
          而且工具还在报成功。**这是本项目最该防的那类静默错误。**

          对照：processor/review/assigner._patchFace 写的是
          `rtn, _cols = sqliteCommon.insertManyTableGeneral(...)` —— 正确。
        """
        writeVcf(str(tmp_path), "0154_Qing Bai.vcf", CARD_MIN)
        ic.apply(ic.plan(str(tmp_path)))
        real = sqliteCommon.insertManyTableGeneral

        def fakeFail(*args, **kwargs):
            real(*args, **kwargs)
            return (-2, ())                 # 冒充一次失败（RET_ERROR）
        monkeypatch.setattr(ic.sqliteCommon, "insertManyTableGeneral", fakeFail)
        res = ic.apply(ic.plan(str(tmp_path)))
        assert res["failed"], "写失败却没进 failed（返回值没解包）"
        assert not res["updated"], \
            "写失败却进了 updated —— CLI 会显示「更新成功」，实际一条没写"


# ============================================================
# 三、撞车分级
# ============================================================

class TestCollisionGrading:
    def test_phone_key_normalizes_formats(self):
        assert ic.phoneKey("+862885187018") == ic.phoneKey("02885187018")
        assert ic.phoneKey("185 1618 1670") == ic.phoneKey("18516181670")
        assert ic.phoneKey("") == ""
        # 只能归一化前缀，不能把不同号段归成同一个。
        # ⚠️ 真实数据里就有一个反例：+86285187018 与 02885187018 **不是**同一个号
        #   （前者少一位），差点被当成「同一人」而丢掉一个真实的人。
        assert ic.phoneKey("+86285187018") != ic.phoneKey("02885187018")

    def test_same_phone_same_name_is_skipped(self, lib, tmp_path):
        root = str(tmp_path)
        writeVcf(root, "a.vcf", CARD_MIN)
        ic.apply(ic.plan(root))
        os.remove("%s/a.vcf" % root)
        writeVcf(root, "b.vcf", CARD_MIN)          # 同人换了文件名
        planned = ic.plan(root)
        assert not [a for a in planned["actions"] if a["op"] == "create"]
        assert any("疑似同一人" in w for w in planned["warnings"])

    def test_same_phone_different_name_is_kept(self, lib, tmp_path):
        """一家人共用座机很常见：同号不同名必须**照常导入**，
        否则会悄悄丢掉一个真实的人。"""
        root = str(tmp_path)
        writeVcf(root, "a.vcf", CARD_MIN)
        ic.apply(ic.plan(root))
        os.remove("%s/a.vcf" % root)
        writeVcf(root, "c.vcf",
                 u"BEGIN:VCARD\nVERSION:2.1\nFN:Other Xu\n"
                 u"TEL;CELL:18516181670\nEND:VCARD\n")
        planned = ic.plan(root)
        assert [a["op"] for a in planned["actions"]] == ["create"], \
            "同号不同名被当成了同一人"
        assert any("共用座机" in w for w in planned["warnings"])


# ============================================================
# 四、落库字段
# ============================================================

class TestRowShape:
    def test_written_row(self, lib, tmp_path):
        writeVcf(str(tmp_path), "0154_Qing Bai.vcf", CARD_MIN)
        ic.apply(ic.plan(str(tmp_path)))
        row = sqliteCommon.query_pb_person("pb_person",
                                           personCode="VC_0154_Qing_Bai")[0]
        assert row["displayName"] == "Qing Bai"
        assert row["birthday"] == "1971-02-10"
        assert row["familyName"] == "Bai"
        assert row["phone"] == "18516181670"
        assert row["source"] == comGD.PERSON_SOURCE_IMPORT
        assert int(row["isConfirmed"]) == 0, "批量导入不等于用户核对过"
        assert "vCard:0154_Qing Bai.vcf" in row["memo"]
        assert row["vcardUid"] == "file:0154_Qing Bai"

    def test_duplicate_display_name_gets_suffix(self, lib, tmp_path):
        """displayName 是 UNIQUE 键：撞了加后缀，**不能覆盖别人**"""
        root = str(tmp_path)
        writeVcf(root, "a.vcf", CARD_MIN)
        ic.apply(ic.plan(root))
        writeVcf(root, "b.vcf",
                 u"BEGIN:VCARD\nVERSION:2.1\nFN:Qing Bai\n"
                 u"TEL;CELL:13900000000\nBDAY:1990-01-01\nEND:VCARD\n")
        planned = ic.plan(root)
        created = [a for a in planned["actions"] if a["op"] == "create"]
        assert created, "同号不同名应当照常导入"
        assert created[0]["contact"]["name"] == "Qing Bai(2)"
        ic.apply(planned)
        assert sqliteCommon.countTableGeneral("pb_person", delFlag="*") == 2
        assert sqliteCommon.query_pb_person("pb_person",
                                            displayName="Qing Bai")

    def test_intra_batch_duplicate_is_caught(self, lib, tmp_path):
        """同一批文件里两份同一个人的名片 -> 只建一个，并报出来"""
        root = str(tmp_path)
        writeVcf(root, "a.vcf", CARD_MIN)
        writeVcf(root, "copy_of_a.vcf", CARD_MIN)
        planned = ic.plan(root)
        assert len([a for a in planned["actions"] if a["op"] == "create"]) == 1
        assert any("疑似同一人" in w for w in planned["warnings"])



# ============================================================
# 五、真实通讯录（lianyi-unique.vcf）
# ============================================================
# 为什么合成数据不够
# ------------------
#   合成卡只有 2~3 行，而真实导出（本文件 4.95MB / 2030 张卡）里有：
#     · ENCODING=QUOTED-PRINTABLE 的中文姓名（821/2027 张卡）
#     · 1012 张内嵌 base64 头像
#     · 94 张既无电话又无邮箱（幂等键只能靠「文件名+序号」）
#     · 3 张只有 EMAIL、没有 FN/N（当前被**静默丢弃**）
#   这些都不是构造得出来的 —— 它们是「真实数据专有」的坑，
#   拿 2 行的合成卡去测，测的是一个比现实干净得多的世界。
#
# ⚠️ 文件不在就跳过：这份文件在本机 D:\\home\\lianyi\\contacts\\ 下，
#    别人的机器上没有 —— 单测不该因为缺一个私人文件而红。
#
# 本类只锁「**不该悄悄坏掉**」的性质；已知缺陷用 xfail(strict=True) 标出，
# 步骤 8 修好之后它会 XPASS 并**让本用例失败**，强制摘掉标记
#（strict=True 是这里的关键：否则 xfail 永远不会提醒任何人）。

REAL_VCF: str = os.environ.get("PHOTO_BROWSER_REAL_VCF",
                               r"D:\\home\\lianyi\\contacts\\lianyi-unique.vcf")


def _realCards():
    if not os.path.isfile(REAL_VCF):
        return []
    with open(REAL_VCF, "rb") as fh:
        return ic.parseCards(fh.read().decode("utf-8", "replace"))


@pytest.fixture(scope="module")
def realRoot(tmp_path_factory):
    """把真实 vcf 复制到临时目录（**只读源文件，绝不原地改**）。"""
    if not os.path.isfile(REAL_VCF):
        pytest.skip("真实通讯录不在本机: %s" % REAL_VCF)
    root = tmp_path_factory.mktemp("realvcf")
    shutil.copy2(REAL_VCF, os.path.join(str(root), "lianyi-unique.vcf"))
    return str(root)


class TestRealVCard:
    def test_card_split_loses_nothing(self, realRoot):
        """切卡不能丢：parseCards 数出来的卡数必须等于文件里的 BEGIN:VCARD 数。

        这是「一个文件 N 张卡」在**真实规模**下的版本（合成卡只有 2 张）。
        切卡丢卡是静默的：少导一个人不会有任何报错。
        """
        with open(os.path.join(realRoot, "lianyi-unique.vcf"), "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
        assert len(ic.parseCards(text)) == text.count("BEGIN:VCARD")

    def test_every_card_gets_a_unique_idempotency_key(self, realRoot):
        """本文件**没有一行 UID**，幂等键全靠「文件名+序号」兜底 ——
        2027 个联系人必须拿到 2027 个**互不相同**的 vcardUid。

        为什么这条最要紧：vcardUid 是 pb_person 的幂等键（Q-3）。
        两张卡撞了同一个键 -> 第二个人**永远导不进来**（upsert 静默覆盖），
        而界面上只是「少了个人」，没有任何报错。
        """
        contacts = ic.readContacts(os.path.join(realRoot, "lianyi-unique.vcf"))
        uids = [c["vcardUid"] for c in contacts]
        assert len(contacts) > 1000, "真实文件应解析出上千个联系人"
        assert len(set(uids)) == len(uids), \
            "vcardUid 撞车：%d 个联系人只有 %d 个键" % (len(uids), len(set(uids)))
        assert all(str(u).startswith("file:lianyi-unique") for u in uids), \
            "本文件没有 UID，键必须来自文件名兜底"

    def test_plan_creates_one_action_per_contact(self, lib, realRoot):
        """空库导入这个文件 -> 每人一条 create，且**总数与解析数一致**。

        ⚠️ 实测现在会少3 个（只有 EMAIL、没有 FN/N 的卡被丢弃，
        #1022/#1023/#1024），而 plan() **一条警告都不报** ——
        # 因为警告只在「整个文件都不可用」时才发。
        #   ⇒ 这里先锁住「actions == contacts」这个不变式（今天成立），
        #     「一张卡都不许丢」由下面那条 xfail 盯着。
        """
        contacts = ic.readContacts(os.path.join(realRoot, "lianyi-unique.vcf"))
        planned = ic.plan(realRoot)
        assert len(planned["actions"]) == len(contacts)
        assert all(a["op"] == "create" for a in planned["actions"])

    def test_avatars_land_in_vcards_dir_never_in_pb_face(self, lib, realRoot):
        """头像落<thumb>/vcards/，**绝不进 pb_face**。

        为什么不能进 pb_face：pb_face.photoCode 是 NOT NULL 外键，
        一张脸必须挂在某张 pb_photo 上；而通讯录头像是vCard 内嵌的
        base64，跟photo 库毫无关系 -> 硬塞就要虚构一行「无照片的 pb_face」。
        那是本项目最忌讳的那种数据造假。
        """
        from processor.media import thumbStore as thumbStore
        contacts = ic.readContacts(os.path.join(realRoot, "lianyi-unique.vcf"))
        wantPhoto = [c for c in contacts if c.get("photo")]
        assert len(wantPhoto) > 1000, "本文件该有 1012 个带头像的联系人"

        res = ic.apply(ic.plan(realRoot))
        assert len(res["avatars"]) == len(wantPhoto), \
            "落盘的头像数与 vCard 里的头像数对不上"
        assert not res["failed"], res["failed"][:2]
        # 头像进了库���但pb_face 仍然是空的（照片库里一个真人都没确认）
        assert sqliteCommon.countTableGeneral("pb_face", delFlag="*") == 0

        rows = [r for r in sqliteCommon.query_pb_person("pb_person", mode="light")
                if r.get("avatarFile")]
        assert len(rows) == len(wantPhoto)
        one = rows[0]
        # 存的是**相对路径**，且与推导公式一致（DR-1：路径必须可推导）
        assert one["avatarFile"] == thumbStore.vcardAvatar_relpath(one["personCode"])
        assert thumbStore.vcardAvatar_exists(one["personCode"]), \
            "avatarFile 指向的文件不在盘上"
        assert os.path.isfile(thumbStore.vcardAvatar_abspath(one["personCode"]))
        # 没有头像的人，这一列必须留空（不能拿别人的头像顶上）
        noPhoto = [r for r in sqliteCommon.query_pb_person("pb_person", mode="light")
                   if not r.get("avatarFile")]
        assert noPhoto, "本文件应当有没头像的联系人"
        assert not any(thumbStore.vcardAvatar_exists(r["personCode"])
                       for r in noPhoto[:20]), "没头像的人却有头像文件"

    def test_photo_warning_is_aggregated_not_per_card(self, lib, realRoot):
        """1012 个头像只给**一条**汇总警告，不许刷屏。

        逐卡一条会把真正值看的警告（同号不同名、只有 EMAIL 的卡、
        解不出的卡）全部淹掉 —— 而那些才是要人管的。
        """
        planned = ic.plan(realRoot)
        photoWarnings = [w for w in planned["warnings"] if "PHOTO" in w]
        assert len(photoWarnings) == 1, \
            "头像警告应当只有 1 条汇总，实际 %d 条" % len(photoWarnings)
        assert "1012" in photoWarnings[0]

    def test_reimport_overwrites_avatar_in_place(self, lib, realRoot):
        """换个头像重导：**同一个文件**被覆盖，avatarFile 不变。

        路径由 personCode 推导，所以「第一个人」不会被旧头像钉住；
        而 avatarFile 每次都被刷新，指向的永远是磁盘上那个最新的。
        """
        res = ic.apply(ic.plan(realRoot))
        code = res["avatars"][0][0]
        before = sqliteCommon.query_pb_person("pb_person", personCode=code,
                                              mode="light")[0]["avatarFile"]
        again = ic.apply(ic.plan(realRoot))
        after = sqliteCommon.query_pb_person("pb_person", personCode=code,
                                             mode="light")[0]
        assert after["avatarFile"] == before, "头像路径不该随导入次数变化"
        assert not again["created"], "第二次导入又建了新档案"

    def test_quoted_printable_names_are_decoded(self, realRoot):
        """中文姓名必须是 '史红江'，不能是 '=E5=AD=A3=E7=BA=A2=E6=B1=9F'。

        实测 2027 个联系人里有 **821 个**（40%）的名字是未解码的 QP串——
        直接导入就是 821 个乱码人名，而且**不报错**。
        """
        contacts = ic.readContacts(os.path.join(realRoot, "lianyi-unique.vcf"))
        bad = [c["displayName"] for c in contacts
               if (c.get("displayName") or "").startswith("=")]
        assert not bad, "%d/%d 个中文名未解码，例如 %r" % (len(bad), len(contacts),
                                                          bad[:1])

    def test_idempotent_second_run_on_real_file(self, lib, realRoot):
        """同一个文件导两次，**人数不许变**。

        现在会变：那 94 个既无电话又无邮箱的联系人，幂等键=「文件名+序号」，
        撞不上就再build 一个人 —— 联系人数量悄悄翻倍。
        """
        first = ic.apply(ic.plan(realRoot))
        n1 = sqliteCommon.countTableGeneral("pb_person", delFlag="*")
        second = ic.apply(ic.plan(realRoot))
        n2 = sqliteCommon.countTableGeneral("pb_person", delFlag="*")
        assert len(first["created"]) > 1000

        # ⚠️⚠️ 这条断言是本用例的**关键**，不能省
        # ------------------------------------------
        #   没有它，本用例会因为「update 路径**整条写失败**」而**假绿**：
        #   第二次 created 当然是空的（op 本来就是 update），
        #   人数当然也不变（因为一条都没写进去）。
        #   换句话说：现在这条用例的绿灯来自 BUG-1，不来自幂等做对了。
        #   必须先确认「第二次真的写成功了」，再谈人数没变。
        #   ⇒ 现状：failed 非空 -> 本用例 xfail（strict）-> 步骤 8 修完两处必 XPASS。
        assert not second["failed"], \
            "第二次导入有 %d 条写失败（%s）—— 先修写入路径，再谈幂等" \
            % (len(second["failed"]), second["failed"][:1])
        assert second["created"] == [], \
            "第二次又建了 %d 个人（人数 %d -> %d）" % (len(second["created"]), n1, n2)
        assert len(second["updated"]) == len(first["created"]), \
            "第二次应当把 %d 个人全部更新一遍，实际 updated=%d" \
            % (len(first["created"]), len(second["updated"]))
        assert n2 == n1

    @pytest.mark.xfail(strict=True, reason="步骤 8 待修：只有 EMAIL、没有 FN/N 的"
                                          "卡被丢弃且**不产生任何警告**")
    def test_no_card_is_silently_dropped(self, realRoot):
        """每张卡要么被导入，要么**明确**报警 —— 不许静默丢人。

        实测丢 3 张（只有 EMAIL、无 FN/N），而 plan() 一条警告都没发。
        """
        contacts = ic.readContacts(os.path.join(realRoot, "lianyi-unique.vcf"))
        with open(os.path.join(realRoot, "lianyi-unique.vcf"), "rb") as fh:
            total = fh.read().decode("utf-8", "replace").count("BEGIN:VCARD")
        planned = ic.plan(realRoot)
        dropped = total - len(contacts)
        if dropped:
            pytest.fail("丢了 %d 张卡，警告 %d 条：%s"
                        % (dropped, len(planned["warnings"]), planned["warnings"][:3]))
        assert len(contacts) == total


# ============================================================
# 六、ENCODING 解码（真实通讯录带来的）
# ============================================================
# ��� 821/2027 个联系人的 FN 是 QUOTED-PRINTABLE 的中文，不解码就是
# 一串 =E5=AD=A3=...；1012 张PHOTO 是 BASE64，不解码头像全丢。


class TestDecodeEncoding:
    def test_quoted_printable_chinese(self):
        assert ic.decodeValue("=E5=AD=A3=E7=BA=A2=E6=B1=9F",
                              {"ENCODING": "QUOTED-PRINTABLE"}) == u"季红江"

    def test_quoted_printable_soft_break(self):
        """软换行是「=回车」两个字符，必须整个吃掉。

        只吃掉一个字符的话 = 后面会留下回车，中文名被拆成两行，
        而后面的字节解码会解出一半个字 —— 而且**不报错**。
        """
        for br in ("\r\n", "\n", "\r"):
            raw = "=E5=AD=A3=" + br + "=E7=BA=A2=E6=B1=9F"
            assert ic.decodeValue(raw, {"ENCODING": "QUOTED-PRINTABLE"}) == u"季红江", \
                "软换行 %r 没被整个吃掉" % br

    def test_quoted_printable_trailing_equals_is_space(self):
        """行末单个 = 转义的是空格，不是把它丢掉"""
        assert ic.decodeValue("=E5=AD=A3=",
                              {"ENCODING": "QUOTED-PRINTABLE"}) == u"季 "

    def test_no_encoding_param_is_untouched(self):
        """没有 ENCODING 就**原样返回** —— 绝不猜。

        猜错的结果比不解码更糟：'Qing Bai' 里没有 '='所以没事，
        但一个本来就该原样保留的串被猜着解一遍就成了乱码。
        """
        assert ic.decodeValue("=E5=AD=A3", {}) == "=E5=AD=A3"
        assert ic.decodeValue("Qing Bai", {}) == "Qing Bai"

    def test_unknown_encoding_is_untouched(self):
        assert ic.decodeValue("xxx", {"ENCODING": "x-something"}) == "xxx"

    def test_base64_returns_latin1_str(self):
        """decodeBase64 返回 **latin-1 str**（一一对应字节）。

        为什么不是 bytes：它服务于「把值当文本用」的场景；
        真要字节时走 photoBytesOf()，那里才 encode 回 latin-1。
        两者混用会让人以为 base64 出来的是 utf-8 文本 ——
        而 base64 解出来的是**任意字节**，按 utf-8 解必然炸。
        """
        got = ic.decodeBase64("/9j/4AAQSkZJRg==")
        assert got == "\xff\xd8\xff\xe0\x00\x10JFIF", repr(got)
        assert len(got) == 10

    def test_base64_bad_input_returns_empty_not_raise(self):
        """坏 base64 返回 ""而不是抛异常。

        头像是**附赠品**：一个人头像解不出来，不该让整个通讯录导入报错
        （那才是更大的失败）。
        """
        assert ic.decodeBase64("!!!not base64!!!") == ""
        assert ic.decodeBase64("") == ""

    def test_photo_bytes_of_jpeg(self):
        data = ic.photoBytesOf("/9j/4AAQSkZJRg==",
                               {"ENCODING": "BASE64", "TYPE": "JPEG"})
        assert data[:3] == b"\xff\xd8\xff", "JPEG 头不对：%r" % data[:4]

    def test_photo_uri_is_not_downloaded(self):
        """PHOTO;VALUE=URI 是外链 —— 导入时**绝不下载**。

        一次导入 1012 个外链就是 1012 次网络请求，慢、不可控，
        而且会把通讯录里的 URL 变成对外的抓取清单。
        """
        assert ic.photoBytesOf("http://example.com/a.jpg", {"VALUE": "URI"}) == b""

    def test_photo_folded_base64_is_joined(self):
        """折行的 base64（真机就是这个形态：一张头像几十行）必须接上再解"""
        # 折行点两侧拼起来必须正好是原串 /9j/4AAQSkZJRg==
        folded = "/9j/4AAQSk\n ZJRg=="
        assert ic.photoBytesOf(folded, {"ENCODING": "BASE64"})[:3] == b"\xff\xd8\xff"

    def test_photo_data_uri_prefix_is_stripped(self):
        """有的客户端写成 data:image/jpeg;base64,... —— 前缀要去掉"""
        assert ic.photoBytesOf("data:image/jpeg;base64,/9j/4AAQSkZJRg==",
                               {"ENCODING": "BASE64"})[:3] == b"\xff\xd8\xff"

    def test_card_with_photo_yields_bytes(self, tmp_path):
        got = ic.readContacts(writeVcf(str(tmp_path), "p.vcf", CARD_PHOTO))[0]
        assert got["hasPhoto"] is True
        assert got["photo"][:3] == b"\xff\xd8\xff"
        assert ic.photoOf(ic.collect(ic.parseCards(CARD_PHOTO)[0]))[:3] == b"\xff\xd8\xff"

    def test_card_without_photo_yields_empty(self, tmp_path):
        got = ic.readContacts(writeVcf(str(tmp_path), "n.vcf", CARD_MIN))[0]
        assert got["hasPhoto"] is False
        assert got["photo"] == b""


# ============================================================
# 七、update 路径的三个陷阱（都是「不报错」的那一类）
# ============================================================


class TestUpdatePathTraps:
    def _import(self, root):
        return ic.apply(ic.plan(root))

    def test_update_row_must_carry_displayname(self, lib, tmp_path):
        """upsert 的 INSERT 必须带 displayName（NOT NULL 且无 DEFAULT）。

        SQLite 是**先按插入**满足 NOT NULL，再决定走不走冲突分支；
        一个只带 personCode+phone 的行压根进不到 DO UPDATE，
        报的是 `NOT NULL constraint failed: pb_person.displayName`。
        """
        root = str(tmp_path)
        writeVcf(root, "0154_Qing Bai.vcf", CARD_MIN)
        self._import(root)
        res = self._import(root)
        assert not res["failed"], \
            "二次导入写失败（多半是 displayName 没带）：%s" % (res["failed"][:1],)
        assert len(res["updated"]) == 1

    def test_update_does_not_rename(self, lib, tmp_path):
        """导入**无权改名**：DO UPDATE 里不许出现 displayName。"""
        root = str(tmp_path)
        writeVcf(root, "0154_Qing Bai.vcf", CARD_MIN)
        self._import(root)
        sqliteCommon.insertManyTableGeneral(
            "pb_person", [{"personCode": "VC_0154_Qing_Bai",
                           "displayName": "我自己改的名字"}],
            conflictColumns=("personCode",), updateColumns=("displayName",),
            fillStandard=True)
        self._import(root)
        row = sqliteCommon.query_pb_person("pb_person",
                                           personCode="VC_0154_Qing_Bai")[0]
        assert row["displayName"] == "我自己改的名字", "导入把用户的改名覆盖了"

    def test_update_only_fills_empty_fields(self, lib, tmp_path):
        """只补空：空字段被补上，已有字段一个都不许动。"""
        root = str(tmp_path)
        writeVcf(root, "0154_Qing Bai.vcf", CARD_MIN)
        self._import(root)
        code = "VC_0154_Qing_Bai"
        sqliteCommon.updateTableGeneral("pb_person", "personCode = %s", (code,),
                                        {"phone": None, "birthday": None})
        self._import(root)
        row = sqliteCommon.query_pb_person("pb_person", personCode=code)[0]
        assert row["phone"] == "18516181670", "空电话没被补上"
        assert row["birthday"] == "1971-02-10", "空生日没被补上"

        sqliteCommon.updateTableGeneral("pb_person", "personCode = %s", (code,),
                                        {"phone": "13900000000"})
        self._import(root)
        row2 = sqliteCommon.query_pb_person("pb_person", personCode=code)[0]
        assert row2["phone"] == "13900000000", \
            "用户手工填的号码被 vCard 覆盖了（导入只该补空）"

if __name__ == "__main__":
    raise SystemExit("请用 pytest 运行："
                     "python -m pytest code/src/test/test_import_contacts.py -v")


