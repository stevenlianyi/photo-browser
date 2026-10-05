#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_contact_import.py
#Description: 步骤 8 单测 —— processor/contact（CSV / vCard 两条通道）+ 头像落盘
#
# 为什么这个文件存在（本轮它抓到了三个真 bug）
# --------------------------------------------
#   ① **unfoldLines 把 base64 padding 当 QP 软换行** -> 吞掉下一张卡的 BEGIN，
#      lianyi-unique.vcf 2030 张卡只切出 1713 张（**丢 317 人，不报错**）。
#      根因：判据只有「上一行以 = 结尾」，而 base64 行尾的 padding 恰好也是 `=`。
#   ② **csv_import.makeContact 的局部变量 contact 遮蔽了模块别名 contact**
#      -> UnboundLocalError，**CSV 通道从来没有跑通过一次**（主力通道）。
#   ③ **csv_import.report 对 int 做 join** -> TypeError，命令在打印计划时崩。
#   三个都是「不报错、只是结果不对/直接崩」的类别，而 CSV/vCard 一次都没被
#   端到端跑过 —— 所以它们只能靠**真实数据**发现，不能靠读代码发现。
#   ⇒ 这个文件的作用是：把那三次「真实数据才暴露」的坑钉成回归锁。
#
# 全部跑在 pytest 临时库上，**绝不动 d:\PhotoLib 正式库**。
# 真实通讯录（lianyi-unique.vcf / .csv）不在这儿 —— 那是别人的私人文件，
# 需要的用例用合成数据表达，真实文件的验收由 tools/ 下的脚本单独做。

import io
import os

import pytest

from common import globalDefinition as comGD
from common import paths as paths
from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from processor.contact import contactCommon as contact
from processor.contact import csv_import as csvImport
from processor.contact import vcard_import as vcardImport
from processor.media import thumbStore as thumbStore


# ============================================================
# 〇、合成数据
# ============================================================

#: 一张带头像的卡（base64 的 JPEG 头 + 一点尾巴，解出来能当字节用）
_B64 = ("/9j/4AAQSkZJRgABAQAAAQABAAD/4gIoSUNDX1BST0ZJTEUAAQI5AAAAAAAA"
        "AAAAAAAAAABCkWgAAALLAAAABAAEAAAABAAEAAABAAEAAABAAEAAABAAEAAABAA")

CARD_PLAIN = u"""BEGIN:VCARD
VERSION:2.1
FN:王蓉
N:王;蓉;;;
TEL;CELL:13900000001
EMAIL:wangrong@example.com
UID:u-wangrong
END:VCARD
"""

CARD_PHOTO = u"""BEGIN:VCARD
VERSION:2.1
FN:吴俊
N:吴;俊;;;
TEL;CELL:13900000002
PHOTO;ENCODING=BASE64;JPEG:%s
UID:u-wujun
END:VCARD
""" % _B64

#: 缺 N / 缺 BDAY 的卡：**曾经**让 one() 返回 "" 而使整条 vobject 路径崩掉
CARD_MINIMAL = u"""BEGIN:VCARD
VERSION:2.1
FN:Michael
TEL;CELL:13900000003
UID:u-michael
END:VCARD
"""

#: KIND:group —— vCard 3.0 家庭组的显式声明（验收 ④）
CARD_GROUP = u"""BEGIN:VCARD
VERSION:3.0
FN:王家人
KIND:group
MEMBER;UID=u-wangrong:
MEMBER;UID=u-wujun:
UID:u-group-wang
END:VCARD
"""

CSV_HEADER = u"姓名,电话,公司,职务,邮箱,Categories,生日"
CSV_ROWS = [
    [u"王蓉", "13900000001", u"公司A", u"经理", "wangrong@example.com",
     u"家人;family;vip", "1985-03-07"],
    [u"吴俊", "13900000002", u"公司B", u"工程师", "wujun@example.com",
     u"同事", "1990-01-02"],
    [u"李四", "13900000003", u"公司C", u"助理", "lisi@example.com",
     u"family", "1995-12-31"],
]


def writeVcf(dirPath, name, text):
    path = os.path.join(dirPath, name)
    with io.open(path, "w", encoding="utf-8", newline="") as hFile:
        hFile.write(text)
    return path


def writeCsv(dirPath, name, header=CSV_HEADER, rows=None):
    import csv as _csv
    path = os.path.join(dirPath, name)
    with io.open(path, "w", encoding="utf-8-sig", newline="") as hFile:
        writer = _csv.writer(hFile)
        writer.writerow(header.split(","))
        for one in (rows if rows is not None else CSV_ROWS):
            writer.writerow(one)
    return path


@pytest.fixture
def lib(temp_db):
    sqliteCommon.dbHandle(temp_db)
    return temp_db


def cats():
    return sqliteCommon.query_pb_person_category("pb_person_category", mode="light")


def catCount(pred=None):
    return sum(1 for r in cats() if (pred is None or pred(r)))


# ============================================================
# 一、折行（**本轮第一个真 bug：丢 317 张卡**）
# ============================================================

class TestUnfold:
    def test_base64_padding_is_not_a_soft_break(self):
        """⚠️ base64 行尾的 `=` 是 padding，**不是** QP 软换行。

        认错的后果（本轮真实发生）：PHOTO 行把下一张卡的 `BEGIN:VCARD` 吞掉，
        那张卡永远等不到 END -> 2030 张只切出 1713 张，**全程不报错**。
        """
        lines = vcardImport.unfoldLines(
            u"BEGIN:VCARD\n"
            u"FN:A\n"
            u"PHOTO;ENCODING=BASE64;JPEG:/9j/4AAQSkZJRg==\n"
            u"BEGIN:VCARD\n"
            u"FN:B\n"
            u"END:VCARD\n")
        assert any(l.strip().upper().startswith("BEGIN:VCARD") for l in lines[3:]), \
            "PHOTO 行尾的 '=' 把下一张卡的 BEGIN 吞掉了：%r" % lines

    def test_card_count_is_exact(self):
        """N 张卡必须切出 N 张 —— 用带 base64 头像的那种（最容易被误吞）"""
        parts = []
        for i in range(6):
            parts.append(u"BEGIN:VCARD\nVERSION:2.1\nFN:P%d\n"
                         u"PHOTO;ENCODING=BASE64;JPEG:/9j/4AAQSkZJRg==\n"
                         u"UID:u-%d\nEND:VCARD\n" % (i, i))
        text = u"".join(parts)
        cards, _parser, _err = vcardImport.readCardsText(text)
        assert len(cards) == 6, "切出 %d 张，应为 6 张" % len(cards)
        assert [c["fn"] for c in cards] == ["P0", "P1", "P2", "P3", "P4", "P5"]

    def test_qp_soft_break_joins(self):
        """QP 软换行：行尾 `=`，续行以 `=XX` 开头 -> 接上"""
        lines = vcardImport.unfoldLines(u"FN:=E5=AD=A3=\n=E7=BA=A2\nUID:u1")
        assert lines[0] == u"FN:=E5=AD=A3=E7=BA=A2", lines

    def test_space_folding_joins(self):
        """RFC 6350 折行：续行以空格开头，去掉那个空格后接上"""
        lines = vcardImport.unfoldLines(u"FN:Michael\n \nUID:u1")
        assert lines[0] == u"FN:Michael", lines

    def test_qp_declared_line_without_equals_start(self):
        """显式声明 QUOTED-PRINTABLE 时，续行不以 = 开头也要接上"""
        lines = vcardImport.unfoldLines(
            u"FN;ENCODING=QUOTED-PRINTABLE:=E5=AD=A3=\nxxx")
        assert lines[0] == u"FN;ENCODING=QUOTED-PRINTABLE:=E5=AD=A3xxx", lines


# ============================================================
# 二、vCard 解析
# ============================================================

class TestVCardParse:
    def test_basic_fields(self, tmp_path):
        got = vcardImport.readPath(writeVcf(str(tmp_path), "a.vcf", CARD_PLAIN))
        card = got["cards"][0]
        assert card["fn"] == u"王蓉"
        assert card["family"] == u"王"
        assert card["tel"] == "13900000001"
        assert card["uid"] == "u-wangrong"
        assert card["hasPhoto"] is False and card["photo"] == b""

    def test_photo_bytes_extracted(self, tmp_path):
        card = vcardImport.readPath(
            writeVcf(str(tmp_path), "p.vcf", CARD_PHOTO))["cards"][0]
        assert card["hasPhoto"] is True
        assert card["photo"][:3] == b"\xff\xd8\xff", "不是 JPEG：%r" % card["photo"][:6]
        assert len(card["photo"]) > 20, "头像字节太少了：%d" % len(card["photo"])

    def test_card_without_optional_fields_survives(self, tmp_path):
        """缺 N / BDAY 的卡**不许**让整条解析路径崩掉。

        one() 的缺省值曾经是 "" 而下面处处 `x is not None and x.value`，
        于是缺一个可选字段就 AttributeError -> 整份文件静默退回兜底解析器。
        """
        got = vcardImport.readFileCards(
            writeVcf(str(tmp_path), "m.vcf", CARD_MINIMAL))
        assert got["parser"] == "vobject", \
            "vobject 路径被打退了：%s" % (got.get("warnings") or [""])[0]
        assert got["cards"][0]["fn"] == "Michael"

    def test_multi_card_file(self, tmp_path):
        text = CARD_PLAIN + CARD_MINIMAL + CARD_PHOTO
        got = vcardImport.readPath(writeVcf(str(tmp_path), "multi.vcf", text))
        assert len(got["cards"]) == 3
        uids = sorted(c["uid"] for c in got["cards"])
        assert uids == ["u-michael", "u-wangrong", "u-wujun"]

    def test_group_card_is_recognised(self, tmp_path):
        """验收 ④：KIND:group 要被认成家庭组，MEMBER 要被收下"""
        text = CARD_PLAIN + CARD_PHOTO + CARD_GROUP
        got = vcardImport.readPath(writeVcf(str(tmp_path), "g.vcf", text))
        groups = [c for c in got["cards"] if c.get("isGroup")]
        assert len(groups) == 1, "家庭组卡片没被认出来"
        assert groups[0]["fn"] == u"王家人"
        # 成员引用在 **UID 参数**里（vCard 3.0 / Outlook 的形态）。
        # 只读值的实现会让成员数等于 0，而不会报错。
        assert groups[0]["members"] == ["u-wangrong", "u-wujun"], groups[0]["members"]

    def test_uri_photo_is_not_downloaded(self, tmp_path):
        """PHOTO;VALUE=URI 是外链 —— 导入时**绝不下载**。"""
        card = vcardImport.readPath(writeVcf(str(tmp_path), "u.vcf", u"""BEGIN:VCARD
VERSION:3.0
FN:Net Person
PHOTO;VALUE=URI:http://example.com/a.jpg
UID:u-net
END:VCARD
"""))["cards"][0]
        assert card["photo"] == b"", "外链头像不该被下载成字节"


# ============================================================
# 三、CSV 解析（**本轮第二个真 bug：通道从未跑通**）
# ============================================================

class TestCsvParse:
    def test_chinese_header_alias(self, tmp_path):
        got = csvImport.readContacts(writeCsv(str(tmp_path), "a.csv"))
        contacts, info = got
        assert info["totalRows"] == 3
        assert sorted(info["colIndex"]) == ["birthday", "categories", "company",
                                          "displayName", "email", "phone",
                                          "title"]
        one = contacts[0]
        assert one["displayName"] == u"王蓉"
        assert one["phone"] == "13900000001"
        assert one["email"] == "wangrong@example.com"
        assert one["birthday"] == "1985-03-07"
        assert one["hasBirthYear"] is True

    def test_categories_split(self, tmp_path):
        contacts, _info = csvImport.readContacts(writeCsv(str(tmp_path), "a.csv"))
        assert contacts[0]["categories"] == [u"家人;family;vip"]
        got, _labels = contact.splitCategories(contacts[0]["categories"])
        assert "family" in got and "vip" in got, got
        # 关系词会被归一（同事 -> colleague），否则同一个人会被拆成
        # “他归属于哪个家庭”的不同类别，而分类表里会出现两行。
        assert contact.splitCategories([u"同事"])[0] == ["colleague"]
        # 未知词原样保留（不猜）——猜一个跟关系无关的标签
        # 比猜错更差：分类表里会出现一个用户从未打签的值
        assert contact.splitCategories([u"父亲"])[0] == [u"父亲"]

    def test_unknown_columns_are_reported(self, tmp_path):
        """认不出的列要**报出来**，不能静默丢数据。"""
        path = writeCsv(str(tmp_path), "a.csv",
                        header=u"姓名,电话,公司,未知列A,未知列B")
        _contacts, info = csvImport.readContacts(path)
        assert info["unknownColumns"] == [u"未知列A", u"未知列B"], \
            info["unknownColumns"]

    def test_row_without_name_is_skipped_with_rowno(self, tmp_path):
        """没有姓名的行要跳过**并报出行号**（静默丢人是最难查的一类）"""
        path = writeCsv(str(tmp_path), "a.csv", rows=CSV_ROWS + [["", "139", "x", "", "y"]])
        contacts, info = csvImport.readContacts(path)
        assert len(contacts) == 3
        assert info["skipped"] and "第 5 行" in info["skipped"][0], info["skipped"]

    def test_report_does_not_raise(self, lib, tmp_path, capsys):
        """report()/printSummary() 曾经直接崩，命令于是一次都跑不完。

        ⚠️ 必须带 `lib`：不带就会通过 dbHandle() **懒加载正式库**
        去读生产数据。
        """
        planned = csvImport.plan(writeCsv(str(tmp_path), "a.csv"))
        csvImport.report(planned)                  # 不抛异常即通过
        out = capsys.readouterr().out
        assert "将要新建" in out
        res = csvImport.apply(planned)
        csvImport.printSummary(res)                 # 吃的是 apply 的 summary
        assert "新增人员" in capsys.readouterr().out

    def test_recognized_but_unstorable_columns_are_reported(self, lib, tmp_path):
        """公司/职务能识别出来，但 pb_person 没有这两列。

        → 它们必须被**报出来**，否则就是静默丢数据：
        用户会怀疑自己的公司职务怎么丢的。
        """
        planned = csvImport.plan(writeCsv(str(tmp_path), "a.csv"))
        assert set(planned["info"]["unstoredColumns"]) >= {"company", "title"}
        hit = [w for w in planned["warnings"] if "不会入库" in w]
        assert hit, planned["warnings"]
        # 报告里用的是字段名（company/title）而不是原始列名，
        # 因为同一个中文列名可能映射到不同字段（见 _buildAlias）
        assert "company" in hit[0] and "title" in hit[0], hit[0]

    def test_bom_and_delimiter_sniffing(self, tmp_path):
        path = os.path.join(str(tmp_path), "semi.csv")
        with io.open(path, "w", encoding="utf-8-sig", newline="") as hFile:
            hFile.write(u"姓名;电话\n王蓉;13900000001\n")
        contacts, info = csvImport.readContacts(path)
        assert info["delimiter"] == ";"
        assert contacts[0]["phone"] == "13900000001"


# ============================================================
# 四、幂等与「只补空」
# ============================================================

class TestIdempotency:
    def test_first_import_creates(self, lib, tmp_path):
        res = csvImport.apply(csvImport.plan(writeCsv(str(tmp_path), "a.csv")))
        assert len(res["created"]) == 3 and not res["updated"]
        assert sqliteCommon.countTableGeneral("pb_person", delFlag="*") == 3

    def test_second_import_creates_nothing(self, lib, tmp_path):
        path = writeCsv(str(tmp_path), "a.csv")
        csvImport.apply(csvImport.plan(path))
        res = csvImport.apply(csvImport.plan(path))
        assert not res["created"], "重复导入又建了人：%s" % (res["created"],)
        assert len(res["updated"]) == 3
        assert sqliteCommon.countTableGeneral("pb_person", delFlag="*") == 3

    def test_reimport_does_not_duplicate_categories(self, lib, tmp_path):
        path = writeCsv(str(tmp_path), "a.csv")
        first = csvImport.apply(csvImport.plan(path))
        second = csvImport.apply(csvImport.plan(path))
        assert second["categoryAdded"] == 0, "分类行被重复插入"
        assert second["categoryRows"] == first["categoryRows"]
        assert catCount() == 4

    def test_shrunken_categories_leave_no_stale_row(self, lib, tmp_path):
        """**验收 ② 的核心**：分类变少时旧行必须被清掉（这才是「无残留」）"""
        csvImport.apply(csvImport.plan(writeCsv(str(tmp_path), "a.csv")))
        assert catCount(lambda r: r["category"] == "family") == 2
        shrunk = [list(r) for r in CSV_ROWS]
        shrunk[0][5] = u"同事"          # 3 个分类 -> 1 个
        shrunk[2][5] = u""             # 清空
        res = csvImport.apply(csvImport.plan(
            writeCsv(str(tmp_path), "b.csv", rows=shrunk)))
        assert res["categoryRemoved"] == 2, res
        assert catCount(lambda r: r["category"] == "family") == 1
        assert catCount(lambda r: r["category"] == "vip") == 0

    def test_family_category_is_queryable(self, lib, tmp_path):
        """**验收 ③**：按 category='family' 查得出正确的人"""
        csvImport.apply(csvImport.plan(writeCsv(str(tmp_path), "a.csv")))
        fams = [r for r in cats() if r["category"] == "family"]
        assert len(fams) == 2
        names = set()
        for one in fams:
            row = sqliteCommon.query_pb_person("pb_person",
                                               personCode=one["personCode"])[0]
            names.add(row["displayName"])
        assert names == {u"王蓉", u"李四"}, names

    def test_update_does_not_wipe_existing_fields(self, lib, tmp_path):
        """只补空：vCard/CSV 缺字段**不许**把库里已有的抹成 NULL"""
        path = writeCsv(str(tmp_path), "a.csv")
        csvImport.apply(csvImport.plan(path))
        code = [r["personCode"] for r in
                sqliteCommon.query_pb_person("pb_person", mode="light")
                if r["displayName"] == u"王蓉"][0]
        # 第二次导入的文件里**没有**电话与邮箱两列
        csvImport.apply(csvImport.plan(writeCsv(
            str(tmp_path), "b.csv", header=u"姓名,公司",
            rows=[[u"王蓉", u"新公司"]])))
        row = sqliteCommon.query_pb_person("pb_person", personCode=code)[0]
        assert row["phone"] == "13900000001", "电话被空值覆盖了"
        assert row["email"] == "wangrong@example.com", "邮箱被空值覆盖了"
        # 姓氏：两个 CSV 都没有姓列，所以两边都是 None
        #（「本次给的值覆盖空值」的正向验证见 vCard 通道的用例）
        assert row["familyName"] is None
        # 公司不可能被写进去：pb_person 没有这个列。
        # 它必须在计划里被报出来（见 unstoredColumns 用例），
        # 而不是静默丢掉。

    def test_update_does_not_rename(self, lib, tmp_path):
        """导入**无权改名**：用户改过的 displayName 必须保住"""
        path = writeCsv(str(tmp_path), "a.csv")
        csvImport.apply(csvImport.plan(path))
        code = [r["personCode"] for r in
                sqliteCommon.query_pb_person("pb_person", mode="light")
                if r["displayName"] == u"王蓉"][0]
        sqliteCommon.insertManyTableGeneral(
            "pb_person", [{"personCode": code, "displayName": u"王蓉(已改)"}],
            conflictColumns=("personCode",), updateColumns=("displayName",),
            fillStandard=True)
        csvImport.apply(csvImport.plan(path))
        row = sqliteCommon.query_pb_person("pb_person", personCode=code)[0]
        assert row["displayName"] == u"王蓉(已改)"

    def test_update_path_survives_not_null_upsert(self, lib, tmp_path):
        """⚠️ upsert 的 update 行必须带 displayName（NOT NULL 且无 DEFAULT）。

        SQLite 是**先按插入**满足 NOT NULL 再决定走不走冲突分支，
        一个只带 personCode+phone 的行压根进不到 DO UPDATE ——
        而且它报的是 NOT NULL 约束错，**看不出**是「更新路径没带显示名」。
        """
        path = writeCsv(str(tmp_path), "a.csv")
        csvImport.apply(csvImport.plan(path))
        res = csvImport.apply(csvImport.plan(path))
        assert not res["failed"], \
            "二次导入写失败（多半是 displayName 没带）：%s" % (res["failed"][:1],)
        assert sqliteCommon.dbHandle().lastErrMsg == ""

    def test_cross_channel_idempotency(self, lib, tmp_path):
        """CSV 建的人，vCard 通道**不许**再建一遍（跨通道幂等）"""
        csvImport.apply(csvImport.plan(writeCsv(str(tmp_path), "a.csv")))
        before = sqliteCommon.countTableGeneral("pb_person", delFlag="*")
        vcard = u"BEGIN:VCARD\nVERSION:2.1\nFN:%s\nUID:%s\nEND:VCARD\n"
        text = u"".join([
            vcard % (u"王蓉", "u-wangrong"),
            vcard % (u"吴俊", "u-wujun"),
            vcard % (u"李四", "u-lisi"),
        ])
        res = vcardImport.apply(vcardImport.plan(
            writeVcf(str(tmp_path), "same.vcf", text)))
        assert not res["created"], \
            "vCard 通道把 CSV 导进来的人又建了一遍：%s" % (res["created"][:3],)
        assert sqliteCommon.countTableGeneral("pb_person", delFlag="*") == before

    def test_birthday_from_vcard_fills_empty_csv_field(self, lib, tmp_path):
        """CSV 没有生日列、vCard 有 -> 应当**补上**（只补空的正向用例）"""
        path = writeCsv(str(tmp_path), "a.csv", header=u"姓名,电话",
                        rows=[[u"王蓉", "13900000001"]])
        csvImport.apply(csvImport.plan(path))
        code = sqliteCommon.query_pb_person("pb_person", mode="light")[0]["personCode"]
        assert sqliteCommon.query_pb_person("pb_person",
                                            personCode=code)[0]["birthday"] is None
        vcardImport.apply(vcardImport.plan(writeVcf(
            str(tmp_path), "one.vcf",
            u"BEGIN:VCARD\nVERSION:2.1\nFN:王蓉\nUID:u-wangrong\n"
            u"BDAY:1985-03-07\nEND:VCARD\n")))
        row = sqliteCommon.query_pb_person("pb_person", personCode=code)[0]
        assert row["birthday"] == "1985-03-07"


# ============================================================
# 五、家庭组（验收 ④）
# ============================================================

class TestFamilyGroup:
    def test_kind_group_creates_pb_family_row(self, lib, tmp_path):
        """验收 ④：显式 KIND:group -> pb_family 有行，成员挂上去"""
        text = CARD_PLAIN + CARD_PHOTO + CARD_GROUP
        res = vcardImport.apply(vcardImport.plan(
            writeVcf(str(tmp_path), "g.vcf", text)))
        assert res["familyCreated"], "KIND:group 没有建出 pb_family 行"
        fams = sqliteCommon.query_pb_family("pb_family", mode="light")
        assert len(fams) == 1, fams
        assert res["familyMembers"] == 2, res["familyMembers"]
        assert res["families"], "summary.families 应记录本次涉及的家庭组"
        for one in res["families"]:
            if isinstance(one, dict):
                assert one.get("familyCode") == fams[0]["familyCode"], one

    def test_same_family_name_only_suggests(self, lib, tmp_path):
        """同姓**只提示、不自动建组**（一家人不同姓，同姓未必一家人）"""
        text = CARD_PLAIN + u"""BEGIN:VCARD
VERSION:2.1
FN:王大力
N:王;大力;;;
UID:u-wangdali
END:VCARD
"""
        res = vcardImport.apply(vcardImport.plan(
            writeVcf(str(tmp_path), "s.vcf", text)))
        assert not res["familyCreated"], "同姓被自动建组了"
        assert res["familySuggestions"], "同姓应当给出合并建议"
        assert not sqliteCommon.query_pb_family("pb_family", mode="light")

    def test_no_group_no_family_row(self, lib, tmp_path):
        res = vcardImport.apply(vcardImport.plan(
            writeVcf(str(tmp_path), "p.vcf", CARD_PLAIN + CARD_PHOTO)))
        assert not res["familyCreated"]
        assert not sqliteCommon.query_pb_family("pb_family", mode="light")


# ============================================================
# 六、头像落盘
# ============================================================

class TestAvatar:
    def test_avatar_written_to_vcards_dir(self, lib, tmp_path):
        """头像落 <thumb>/vcards/，**绝不进 pb_face**"""
        res = vcardImport.apply(vcardImport.plan(
            writeVcf(str(tmp_path), "p.vcf", CARD_PLAIN + CARD_PHOTO)))
        assert len(res["avatars"]) == 1, res["avatars"]
        assert not res["avatarFailed"], res["avatarFailed"]
        rows = [r for r in sqliteCommon.query_pb_person("pb_person", mode="light")
                if r.get("avatarFile")]
        assert len(rows) == 1
        one = rows[0]
        # 存的是**相对路径**，且与推导公式一致（DR-1：路径必须可推导）
        assert one["avatarFile"] == thumbStore.vcardAvatar_relpath(one["personCode"])
        assert one["avatarFile"].startswith(basicSettings.VCARD_SUBDIR + "/")
        assert thumbStore.vcardAvatar_exists(one["personCode"])
        assert sqliteCommon.countTableGeneral("pb_face", delFlag="*") == 0, \
            "头像绝不能进 pb_face（photoCode 是 NOT NULL 外键）"

    def test_avatar_stored_verbatim(self, lib, tmp_path):
        """存的是**原始字节**：再编码一次只会更糊"""
        vcardImport.apply(vcardImport.plan(
            writeVcf(str(tmp_path), "p.vcf", CARD_PHOTO)))
        one = [r for r in sqliteCommon.query_pb_person("pb_person", mode="light")
               if r.get("avatarFile")][0]
        size = os.path.getsize(thumbStore.vcardAvatar_abspath(one["personCode"]))
        assert size == len(vcardImport.photoBytesOf({"photo": _B64}))

    def test_person_without_photo_has_no_avatar(self, lib, tmp_path):
        res = vcardImport.apply(vcardImport.plan(
            writeVcf(str(tmp_path), "p.vcf", CARD_PLAIN + CARD_PHOTO)))
        rows = {r["displayName"]: r for r in
                sqliteCommon.query_pb_person("pb_person", mode="light")}
        assert not rows[u"王蓉"].get("avatarFile"), "没头像的人不该有 avatarFile"
        assert not thumbStore.vcardAvatar_exists(rows[u"王蓉"]["personCode"])
        assert rows[u"吴俊"].get("avatarFile")

    def test_reimport_overwrites_same_path(self, lib, tmp_path):
        """换个头像重导：同一个文件被覆盖，avatarFile 不变"""
        path = writeVcf(str(tmp_path), "p.vcf", CARD_PLAIN + CARD_PHOTO)
        vcardImport.apply(vcardImport.plan(path))
        one = [r for r in sqliteCommon.query_pb_person("pb_person", mode="light")
               if r.get("avatarFile")][0]
        before = one["avatarFile"]
        again = vcardImport.apply(vcardImport.plan(path))
        after = [r for r in sqliteCommon.query_pb_person("pb_person", mode="light")
                 if r.get("avatarFile")][0]
        assert after["avatarFile"] == before, "头像路径不该随导入次数变化"
        assert not again["created"], "第二次导入又建了新档案"

    def test_relpath_is_derivable_and_case_sensitive(self, lib, tmp_path):
        """路径必须与 personCode **一对一**。

        ⚠️ 本轮真缺陷：曾经对 personCode 做 `.lower()`，于是 `CS_Nanyang` 与
        `CS_nanyang`（通讯录里真实存在的两个人）**共用一张头像** ——
        落盘 1010 个文件而库里有 1012 行 avatarFile。
        """
        vcardImport.apply(vcardImport.plan(
            writeVcf(str(tmp_path), "p.vcf", CARD_PLAIN + CARD_PHOTO)))
        codes = [r["personCode"] for r in
                 sqliteCommon.query_pb_person("pb_person", mode="light")
                 if r.get("avatarFile")]
        assert len(set(thumbStore.vcardAvatarName(c) for c in codes)) == len(codes)
        a = thumbStore.vcardAvatarName("CS_Nanyang")
        b = thumbStore.vcardAvatarName("CS_nanyang")
        assert a != b, "只差大小写的两个 personCode 共用了同一个头像文件名"

    def test_traversal_attempt_is_neutralised(self, lib, tmp_path):
        """personCode 里带 ../.. 也逃不出 <thumb>（路径由 sha1 推导）"""
        rel = thumbStore.vcardAvatar_relpath("../../etc/passwd")
        assert rel.startswith(basicSettings.VCARD_SUBDIR + "/")
        assert ".." not in rel.split("/")

    def test_empty_personcode_rejected(self):
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.vcardAvatar_relpath("")

    def test_never_writes_into_photo_dir(self, lib, tmp_path):
        """原图目录绝对只读这条硬约束，在头像路径上也不许裂开"""
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.write_vcard_avatar("", b"\xff\xd8\xff")
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.write_vcard_avatar("x", b"")      # 空数据也拒绝
        rel = thumbStore.write_vcard_avatar("x", b"\xff\xd8\xff")
        assert rel.startswith(basicSettings.VCARD_SUBDIR + "/")
        # 头像必须落在 thumb 下，而不是 photo 下
        photoDir = os.path.normcase(os.path.abspath(paths.photo_dir()))
        thumbDir = os.path.normcase(os.path.abspath(paths.thumb_dir()))
        assert photoDir != thumbDir
        assert thumbStore.vcardAvatar_abspath("x").lower().startswith(
            thumbDir + os.sep.lower()), thumbStore.vcardAvatar_abspath("x")
        if os.path.isdir(photoDir):
            for root, _dirs, files in os.walk(photoDir):
                assert basicSettings.VCARD_SUBDIR not in root, root

    def test_photo_warning_is_aggregated(self, lib, tmp_path):
        """头像警告只出**一条**汇总，不逐卡刷屏（本机真实文件有 1012 个头像）"""
        text = u"".join(
            u"BEGIN:VCARD\nVERSION:2.1\nFN:P%d\nPHOTO;ENCODING=BASE64;JPEG:%s\n"
            u"UID:pu-%d\nEND:VCARD\n" % (i, _B64, i) for i in range(5))
        planned = vcardImport.plan(writeVcf(str(tmp_path), "many.vcf", text))
        res = vcardImport.apply(planned)
        photoWarnings = [w for w in res["warnings"] if basicSettings.VCARD_SUBDIR in w]
        assert len(photoWarnings) == 1, \
            "头像警告应当只有 1 条汇总，实际 %d 条" % len(photoWarnings)
        assert "5" in photoWarnings[0]

    def test_avatar_failure_does_not_abort_import(self, lib, tmp_path, monkeypatch):
        """头像写不下去**不能**让整个导入失败（头像是附赠品，人名册是主线）"""
        def boom(*a, **kw):
            raise thumbStore.ThumbStoreError("磁盘满了")
        monkeypatch.setattr(thumbStore, "write_vcard_avatar", boom)
        res = vcardImport.apply(vcardImport.plan(
            writeVcf(str(tmp_path), "p.vcf", CARD_PLAIN + CARD_PHOTO)))
        assert len(res["created"]) == 2, "头像失败把整个导入带走了"
        assert res["avatarFailed"], "头像失败没有记进 avatarFailed"
        assert any("头像写入失败" in w for w in res["warnings"])


# ============================================================
# 七、归档（验收 ⑤）
# ============================================================

class TestArchive:
    def test_original_is_copied_into_db_imports(self, lib, tmp_path):
        """验收 ⑤：原件**复制**到 <dbDir>\\imports\\，用户的原文件不动"""
        src = writeCsv(str(tmp_path), "a.csv")
        res = csvImport.apply(csvImport.plan(src))
        got = res.get("archive") or {}
        assert got, "没有归档记录：%s" % sorted(res)
        dst = got[0] if isinstance(got, (list, tuple)) else got
        path = dst if isinstance(dst, str) else (dst.get("path") or dst.get("target"))
        assert path and os.path.isfile(path), path
        assert os.path.isfile(src), "原文件被移动了（应当是复制）"
        assert "imports" in path.replace("/", os.sep)

    def test_archive_dir_is_under_db_dir(self, lib, tmp_path):
        """归档落点必须跟随**实际在写的那个库**。

        ⚠️ 临时库并不在 PHOTO_ROOT 里（测试库在 tmp/state），
        而 paths.db_file() 是从 PHOTO_ROOT 推导的 —— 若归档跟后者，
        `--db 临时库` 会把归档写进**正式树**，而实验库里一份都没有。
        """
        src = writeCsv(str(tmp_path), "a.csv")
        csvImport.apply(csvImport.plan(src))
        root = os.path.dirname(os.path.abspath(sqliteCommon.dbFilePath()))
        imports = os.path.join(root, "imports")
        assert os.path.isdir(imports), \
            "归档没落在实际库旁边（%s）" % imports

    def test_two_runs_do_not_overwrite_each_other(self, lib, tmp_path):
        """两次导入的归档必须**各自一份**（带时间戳），否则先导的那份被覆盖"""
        src = writeCsv(str(tmp_path), "a.csv")
        first = csvImport.apply(csvImport.plan(src))
        second = csvImport.apply(csvImport.plan(src))
        p1 = first["archive"][0] if isinstance(first["archive"], (list, tuple)) \
            else first["archive"]
        p2 = second["archive"][0] if isinstance(second["archive"], (list, tuple)) \
            else second["archive"]
        assert p1 != p2, "两次归档落到同一个文件：%s" % p1


if __name__ == "__main__":
    raise SystemExit("请用 pytest 运行："
                     "python -m pytest code/src/test/test_contact_import.py -v")
