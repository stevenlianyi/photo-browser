#! /usr/bin/env python3
#encoding: utf-8

#Filename: csv_import.py
#Description: photo-browser 联系人导入 · **主力通道**（Outlook 导出 CSV -> pb_person）
#
# 为什么是主力通道
# ----------------
#   Outlook / Exchange / 手机通讯录导出，**CSV 是唯一人人都会的格式**
#   （vCard 要在 Outlook 里点「导出联系人」，很多人根本不知道在哪）。
#   一个人 2000 个联系人，CSV 一秒导完；vCard 在 Outlook 里是**一个联系人一个文件**
#   （本项目的 20 个真实 .vcf 就是这么来的），2000 人就是 2000 个文件。
#
# 本文件只做「文件 -> 联系人 dict」，落库/幂等/分类/归档全在 contactCommon。
#
# 列名容错（为什么必须容错）
# ------------------------
#   同一列在 Outlook 不同版本/不同语言下叫法完全不同：
#     显示名= DisplayName / 显示名 / 姓名 / Name / Full Name
#     类别  = Categories / 类别 / 分类 / Category
#   而**导出模板是用户自己挑的** —— 少一列、多一列、中英文混排都是常态。
#   所以这里做「归一化表头 -> 别名表」匹配：认不出的列就当作没有这一列，
#   **绝不因为一个不认识的列名整份文件失败**（那等于 2000 人一个都导不进）。
#
# 硬约束
# ------
#   * **只建档案，绝不关联照片**：不写 pb_face / pb_photo / pb_photo_person，
#     照片归属由人脸识别负责。
#   * 编码嗅探 utf-8-sig / utf-8 / gbk / gb18030，认不出**明确报错**
#     （候选里刻意不放 latin-1：它永不失败，只会静默解出乱码）。
#   * 幂等：优先 vCardUid 认人，无 UID 用 displayName 认人；命中即 UPDATE。
#   * 同姓多人**只提示**「建议合并家庭组」，**不自动建**（半自动原则）。
#   * 分批提交，不做长事务。
#
# 用法
# ----
#   python code\src\main\cli.py import-csv --file D:\temp\contacts\outlook.csv --dry-run
#   python code\src\main\cli.py import-csv --file D:\temp\contacts\outlook.csv

import csv
import io
import os
import re
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                              # noqa: E402
from config import basicSettings as basicSettings                  # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.contact import contactCommon as contact            # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("csvImport", "csvimport.log")


class CsvImportError(contact.ContactImportError):
    """CSV 特有的前置错误（列名全认不出 / 编码认不出 / 文件读不了）"""


# ============================================================
# 一、列名归一与别名表
# ============================================================

def normHeader(text: str) -> str:
    """表头归一：去 BOM/引号/空白/下划线/连字符 -> 小写

    为什么要这么激进：Outlook 的表头在不同语言/版本里是
    「E-Mail Address」/「E-mail Address」/「E-Mail」/「邮箱」四种写法，
    只做 lower() 会漏掉带空格与下划线的变体。
    """
    t = str(text or "").strip().strip('"').strip("\ufeff")
    t = t.replace("\u3000", " ").replace("　", " ")
    t = re.sub(r"[\s_\-\.]+", "", t)
    return t.lower()


def _buildAlias() -> dict:
    alias = {}
    for field, names in (
        ("displayName", ("DisplayName", "显示名", "姓名", "名字", "Name",
                         "FullName", "Full Name", "联系人", "Contact", "联系人姓名")),
        ("familyName", ("Surname", "姓氏", "姓", "LastName", "Last Name",
                        "FamilyName", "Family Name")),
        ("givenName", ("GivenName", "名", "FirstName", "First Name")),
        ("relation", ("Relation", "关系", "家庭关系", "Relationship", "Relative")),
        ("email", ("E-Mail", "E-mail", "Email", "EMail", "E-Mail Address",
                   "电子邮件", "邮箱", "邮件", "EmailAddress")),
        ("phone", ("Mobile", "Mobile Phone", "MobilePhone", "手机", "手机号",
                   "电话", "电话号码", "Phone", "Phone Number", "Tel", "Telephone")),
        ("businessPhone", ("Business Phone", "Business Telephone", "办公电话",
                           "公司电话", "WorkPhone", "工作电话")),
        ("homePhone", ("Home Phone", "Home Telephone", "住宅电话", "家里电话")),
        ("birthday", ("Birthday", "Birth Date", "Birthday(Year/Month/Day)",
                      "生日", "出生日期", "出生年月日", "BirthDate")),
        ("categories", ("Categories", "Category", "类别", "分类", "Tags")),
        ("vcardUid", ("VCardUid", "VCard UID", "UID", "唯一标识", "唯一 ID", "Id")),
        ("title", ("Title", "职务", "职位")),
        ("company", ("Company", "公司", "Company Name", "单位")),
        ("nickname", ("Nickname", "NickName", "昵称", "别名")),
        ("notes", ("Notes", "Note", "备注", "说明")),
    ):
        for name in names:
            alias[normHeader(name)] = field
    return alias


#: 表头归一值 -> 内部字段名
COLUMN_ALIAS: dict = _buildAlias()

#: 电话取值优先级：手机号最可能是「本人常用号码」
PHONE_PREF: tuple = ("phone", "businessPhone", "homePhone")


# ============================================================
# 二、分隔符嗅探
# ============================================================

def sniffDelimiter(sample: str, candidates: tuple = (",", ";", "\t", "|")) -> str:
    """数引号外各候选分隔符的出现次数，取最多的。

    不用 csv.Sniffer：它靠统计猜，短文件/单列文件会猜错（比如把分号当行尾），
    而猜错的后果是「整份文件只导进 1 个人」且**不报错**。
    这里逐字符扫并跟踪引号状态，结果确定。
    """
    counts = dict((ch, 0) for ch in candidates)
    inQuote = False
    for ch in sample:
        if ch == '"':
            inQuote = not inQuote
        elif not inQuote and ch in counts:
            counts[ch] += 1
    best = max(candidates, key=lambda ch: (counts[ch], -candidates.index(ch)))
    return best if counts[best] > 0 else ","


#: 能从 CSV 表头识别出来、但 **pb_person 没有对应列**的字段。
#: 它们会被解析出来、但落不下去 —— 必须在计划里报出来，
#: 否则就是**静默丢数据**（用户会怀疑自己的公司职务怎么丢的）。
#: 要想真存，得给 pb_person 加列（当前 UI 也没有这个位）。
_UNSTORED_FIELDS: tuple = ("company", "title", "nickname", "notes",
                            "address", "department", "jobTitle")


def readTable(path: str) -> dict:
    """读 CSV -> {"header": [...], "rows": [(行号, dict字段->原文)], "encoding", "delimiter"}

    Raises CsvImportError —— 文件读不了 / 编码认不出 / 只有一个表头（没有数据）。
    """
    text, encoding = contact.sniffTextFile(path)
    sample = text[:8192]
    delimiter = sniffDelimiter(sample)
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        table = list(reader)
    except csv.Error as e:
        raise CsvImportError("CSV 解析失败: %s（%s）" % (path, e))
    table = [row for row in table if any(str(cell).strip() for cell in row)]
    if not table:
        raise CsvImportError("CSV 里没有数据行: %s" % path)
    header = [str(cell) for cell in table[0]]
    colIndex = {}
    unknown = []
    for pos, name in enumerate(header):
        field = COLUMN_ALIAS.get(normHeader(name))
        if field is None:
            unknown.append(name)
            continue
        # 同名列取**第一次**出现的位置：Outlook 有时把「姓名」导出两遍，
        # 取后面那个会拿到空的重复列
        colIndex.setdefault(field, pos)
    if "displayName" not in colIndex:
        raise CsvImportError(
            "%s 的表头里找不到「显示名/姓名/DisplayName」列，无法建档案。\n"
            "实际表头: %s" % (path, ", ".join(header)))
    rows = []
    for rowNo, row in enumerate(table[1:], start=2):
        data = {}
        for field, pos in colIndex.items():
            data[field] = (row[pos] if pos < len(row) else "").strip()
        rows.append((rowNo, data))
    # 认别出来但**无列可存**的字段（见 plan() 里的说明）
    unstored = [f for f in sorted(colIndex) if f in _UNSTORED_FIELDS]
    return {"header": header, "colIndex": colIndex, "rows": rows,
            "encoding": encoding, "delimiter": delimiter,
            "unknownColumns": unknown, "unstoredColumns": unstored,
            "file": os.path.basename(path)}


# ============================================================
# 三、读表 -> 联系人 dict
# ============================================================

def readContacts(path: str) -> tuple:
    """读一个 CSV -> (联系人 dict 列表, 表信息 dict)

    联系人 dict 字段（contactCommon.planContact 的入参）：
        displayName / familyName / relation / email / phone / birthday /
        bdayRaw / hasBirthYear / uid / uidFromFile / categories /
        categoriesGiven / company / title / nickname / file / rowNo / sourceKind
    """
    table = readTable(path)
    contacts = []
    skipped = []
    for rowNo, data in table["rows"]:
        contact = makeContact(data, table["file"], rowNo)
        if not contact["displayName"]:
            skipped.append("第 %d 行没有可用姓名，已跳过" % rowNo)
            continue
        contacts.append(contact)
    info = {"file": table["file"], "path": os.path.abspath(path),
            "encoding": table["encoding"], "delimiter": table["delimiter"],
            "header": table["header"], "colIndex": table["colIndex"],
            "unknownColumns": table["unknownColumns"],
            "unstoredColumns": table["unstoredColumns"],
            "totalRows": len(table["rows"]), "skipped": skipped}
    return contacts, info


def makeContact(data: dict, fileName: str, rowNo: int) -> dict:
    """一行 -> 联系人 dict

    ⚠️ 本函数的局部变量不能叫 contact（本步修的真 bug）
    --------------------------------------------------------
      模块别名本身就叫 `contact`（`from processor.contact import contactCommon as contact`），
      而下面又有一个 `contact = {...}`：Python 在编译期就把全函数的 `contact`
      归为**局部变量**，所以第一行的 `contact.normalizeBday(...)` 拿到的是
      一个尚未赋值的局部名 -> `UnboundLocalError`。
      后果：**CSV 通道从来没能跑通过一次**（正因是它没有单测）。
      → 改名为 out，不再影响模块别名。
    """
    birthday, bdayRaw, hasYear = contact.normalizeBday(data.get("birthday"))
    phone = ""
    for field in PHONE_PREF:
        if data.get(field):
            phone = data[field]
            break
    categories = [data.get("categories")] if data.get("categories") else []
    out = {
        "displayName": (data.get("displayName") or "").strip(),
        "familyName": (data.get("familyName") or "").strip(),
        "relation": comGD.normalizeRelation(data.get("relation")),
        "email": (data.get("email") or "").strip(),
        "phone": phone,
        "birthday": birthday,
        "bdayRaw": bdayRaw,
        "hasBirthYear": hasYear,
        "uid": (data.get("vcardUid") or "").strip(),
        "uidFromFile": False,
        "categories": categories,
        # 只有这一列**确实有值**才算「本次给了分类」——
        # 空单元格是「没填」，不是「清空」，两者在幂等处理上完全相反
        "categoriesGiven": bool(categories and categories[0]),
        "company": (data.get("company") or "").strip(),
        "title": (data.get("title") or "").strip(),
        "nickname": (data.get("nickname") or "").strip(),
        "notes": (data.get("notes") or "").strip(),
        "file": fileName,
        "rowNo": rowNo,
        "sourceKind": "CSV",
        "groupCode": "",
        "hasPhoto": False,
    }
    return out


# ============================================================
# 四、plan（只算不写） / apply（落库）
# ============================================================

def plan(path: str, dbFile: str = None) -> dict:
    """扫一遍 CSV，算出每行「新建/更新/跳过」。**不写任何一行。**"""
    contacts, info = readContacts(path)
    index = contact.loadPersonIndex(dbFile)
    takenNames, takenCodes = set(), set()
    warnings = list(info["skipped"])
    items = []
    prefix = basicSettings.IMPORT_CODE_PREFIX_CSV
    for item in contacts:
        if item.get("bdayRaw") and not item.get("birthday"):
            warnings.append(
                "%s 第 %d 行生日「%s」解析不出年月日 -> **不写入 birthday**"
                "（无出生年会静默降级为等宽 5 年分桶，跨年代认人明显变差，"
                "宁可留空让人看见）" % (info["file"], item["rowNo"], item["bdayRaw"]))
        if re.match(r"^\s*\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{4}\s*$",
                    str(item.get("bdayRaw") or "")):
            warnings.append(
                "%s 第 %d 行生日「%s」按 M/D/YYYY（Outlook 英文版格式）解析，请核对"
                % (info["file"], item["rowNo"], item["bdayRaw"]))
        plannedOne = contact.planContact(item, index, prefix,
                                         takenNames, takenCodes, warnings)
        items.append(plannedOne)
    if info["unknownColumns"]:
        warnings.append(
            "表头里这些列没有对应字段，已忽略: %s"
            % ", ".join(info["unknownColumns"][:12]))
    # ⚠️ 区分两类「没存进去」，不能混成一个警告
    #   unknownColumns（格子不识别）与下面这个（格子**识别了**
    #   但 pb_person 没有这个列，解析出来只能丢弃）。
    #   真实数据里公司/职务就是后者：列识别了、值也读出来了，
    #   却没有一个列可以存。不报上来就是**静默丢数据**。
    if info.get("unstoredColumns"):
        warnings.append(
            "这些列识别出来了，但 pb_person 没有对应列，**不会入库**"
            "（如果保存它们需要加列）: %s"
            % ", ".join(info["unstoredColumns"][:12]))
    if not items:
        warnings.append("%s 里没有一行能建出档案（全部缺姓名）" % info["file"])
    return {"kind": "csv", "source": os.path.abspath(path), "info": info,
            "items": items, "groups": [], "warnings": warnings,
            "dbFile": sqliteCommon.dbFilePath() if items else ""}


def apply(planned: dict, ownerID: str = "", archive: bool = True,
          batchRows: int = None) -> dict:
    """落库 + 归档原件。

    归档在**写库之前**做，且失败只当warning：库写成功是主目标，
    但归档失败必须让用户在摘要里看见（否则原件就悄悄丢了）。
    """
    result = {"archive": {"ok": False, "path": "", "errMsg": "未归档"}}
    if archive and planned.get("source"):
        kind = "csv"
        result["archive"] = contact.archiveOriginal(planned["source"], kind)
        if not result["archive"]["ok"]:
            planned.setdefault("warnings", []).append(
                "原件归档失败：%s" % result["archive"]["errMsg"])
    summary = contact.applyPlan(planned, ownerID=ownerID, batchRows=batchRows)
    summary["archive"] = result["archive"]
    return summary


# ============================================================
# 五、报告
# ============================================================

def report(planned: dict) -> dict:
    """打印计划（--dry-run 与实跑共用，保证两次看到的是同一份东西）"""
    items = planned["items"]
    creates = [it for it in items if it["op"] == "create"]
    updates = [it for it in items if it["op"] == "update"]
    withYear = [it for it in items if it["contact"].get("hasBirthYear")]
    noYear = [it for it in items if not it["contact"].get("hasBirthYear")]
    withCat = [it for it in items if it["categories"]]
    info = planned["info"]
    print("==== CSV 导入计划 ====")
    print("文件      : %s" % planned["source"])
    print("编码      : %s（嗅探）  分隔符: %r" % (info["encoding"], info["delimiter"]))
    print("数据行: %d   识别出列: %d（%s）"
          % (info["totalRows"], len(info["colIndex"]),
             # ⚠️ colIndex 的 **value 是列号(int)**，key 才是字段名。
             #    原写法对 values() 做 join -> TypeError，而不是报错、是整个
             #    命令在打印计划的最后一步直接崩掉。
             ", ".join(sorted(str(k) for k in info["colIndex"]))))
    print("库        : %s" % planned.get("dbFile", ""))
    print("将要新建  : %d    将要更新: %d" % (len(creates), len(updates)))
    print("带分类    : %d 人    有出生年: %d    无出生年: %d（将走等宽 5 年降级）"
          % (len(withCat), len(withYear), len(noYear)))
    for item in items[:12]:
        print("  %-6s %-26s %-22s 姓=%-8s 生日=%-10s 分类=%s"
              % ("新建" if item["op"] == "create" else "更新",
                 item["personCode"], item["displayName"],
                 item["contact"].get("familyName") or "-",
                 item["contact"].get("birthday") or
                 (("%s(无年份)" % item["contact"]["bdayRaw"])
                  if item["contact"].get("bdayRaw") else "无"),
                 ",".join(item["categories"]) or "无"))
    if len(items) > 12:
        print("  ...（其余 %d 条省略）" % (len(items) - 12))
    return {"create": len(creates), "update": len(updates), "noYear": len(noYear)}


def printSummary(summary: dict) -> None:
    """实跑摘要：新增 N / 更新 M / 分类行 K / 家庭组 J"""
    print("")
    print("---- 导入结果 ----")
    print("新增人员  : %d" % len(summary["created"]))
    print("更新人员  : %d" % len(summary["updated"]))
    print("分类行    : %d 行（本次新增 %d、清理旧行 %d、复活软删 %d）"
          % (summary["categoryRows"], summary["categoryAdded"],
             summary["categoryRemoved"], summary["categoryRevived"]))
    print("家庭组    : %d 个（新建 %d，成员挂接 %d 人）"
          % (len(summary["families"]), len(summary["familyCreated"]),
             summary["familyMembers"]))
    archive = summary.get("archive") or {}
    if archive.get("ok"):
        print("原件归档  : %s" % archive["path"])
    elif archive.get("errMsg") and archive["errMsg"] != "未归档":
        print("原件归档  : **失败** %s" % archive["errMsg"])
    for code in summary["failed"]:
        print("  ! %s -> %s" % (code[0], code[1]))
    if summary["familySuggestions"]:
        print("")
        print("建议合并的家庭组（**未自动建**，同姓不等于一家人，请人工确认）：")
        for item in summary["familySuggestions"][:12]:
            print("  · %s（%s）：%d 人 —— %s"
                  % (item["familyName"], item["suggestCode"], len(item["persons"]),
                     "、".join(name for _code, name in item["persons"][:8])))
        if len(summary["familySuggestions"]) > 12:
            print("  ...（其余 %d 组省略）" % (len(summary["familySuggestions"]) - 12))
    if summary["warnings"]:
        print("")
        print("提示 %d 条：" % len(summary["warnings"]))
        for text in summary["warnings"][:40]:
            print("  · %s" % text)
        if len(summary["warnings"]) > 40:
            print("  ...（其余 %d 条省略）" % (len(summary["warnings"]) - 40))
    total = sqliteCommon.countTableGeneral("pb_person", delFlag="*")
    print("")
    print("pb_person 现共 %d 人（含软删）；"
          "pb_person_category 现共 %d 行；pb_family 现共 %d 个。"
          % (total,
             sqliteCommon.countTableGeneral("pb_person_category", delFlag="*"),
             sqliteCommon.countTableGeneral("pb_family", delFlag="*")))


if __name__ == "__main__":
    contact._fixConsole()
    print("csv_import _VERSION:", _VERSION)
    print("列名别名表: %d 条 -> %d 个字段"
          % (len(COLUMN_ALIAS), len(set(COLUMN_ALIAS.values()))))
    print("编码嗅探  :", basicSettings.IMPORT_ENCODINGS)
