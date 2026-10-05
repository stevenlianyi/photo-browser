#! /usr/bin/env python3
#encoding: utf-8

#Filename: contactCommon.py
#Description: photo-browser 联系人导入的**公共运行层**（CSV 与 vCard 两个通道共用）
#
# 为什么要单独一层
# --------------
#   csv_import.py 与 vcard_import.py 只是「两种文件格式 -> 联系人 dict」的解析器，
#   而「怎么认人（幂等）、怎么生成 personCode、分类行怎么重写、家庭组怎么挂、
#   原件怎么归档」这几件事**两个通道完全一样**。复制一遍的后果是：
#   改了 CSV 那边的幂等规则，vCard 那边的重复导入就开始造重复档案 ——
#   而且这种不一致不会报错，只会让库里多出几个人。所以只有一份实现。
#
# 本层的硬约束（与 plan/MVP_plan.md S4 一致）
# -------------------------------------------
#   1. **只建档案，绝不关联照片**：本模块只写 pb_person / pb_person_category /
#      pb_family，不碰 pb_face / pb_photo / pb_photo_person，也不读 photo 目录。
#      照片归属由人脸识别负责（步骤 5-7）。
#   2. **幂等**：优先 vCardUid 认人；无 UID 时用 displayName 认人；命中即 UPDATE，
#      不新增。邮箱/电话撞上只**提示**不自动合并（号码会被复用，自动合并会毁数据）。
#   3. **半自动**：同姓多人**只给「建议合并家庭组」提示**，绝不自动建 pb_family；
#      只有通讯录里**显式**写了 KIND:group 的组才落 pb_family。
#   4. **不做长事务**：分批提交（basicSettings.IMPORT_BATCH_ROWS），
#      中途中断时已落库的部分仍然有效。
#
# 与 tools/import_contacts.py 的关系
# ---------------------------------
#   那是步骤 5-7 期间为「10 个 vcf」写的工具，只导 pb_person。本模块是它的
#   正式继任者：多了 Categories、pb_family、归档、分批提交；personCode 前缀沿用
#   同一个 "VC_"，**保证步骤 8 之前导进去的人不会被当成另一个人重建**。
#   那个工具保留（它有自己的单测），本模块不 import 它，避免两套 vCard 解析互相污染。

import os
import re
import shutil
import sys
import unicodedata

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                # noqa: E402
from common import miscCommon as misc                        # noqa: E402
from common import paths as paths                            # noqa: E402
from common import sqliteHandle as sqliteHandle              # noqa: E402
from config import basicSettings as basicSettings            # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon   # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("contactCommon", "contactcommon.log")


class ContactImportError(Exception):
    """导入前置条件不满足（文件读不了 / 编码认不出 / 库表不存在）。

    刻意用**异常**而不是返回码：这些错误一旦发生，继续跑只会写出一半的库，
    而调用方（cli.py）必须明确报错并退出，不能「打印个 warning 继续」。
    """


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ============================================================
# 一、编码嗅探与文件读
# ============================================================

def sniffTextFile(path: str, encodings=None) -> tuple:
    """读文本文件并**嗅探编码**。

    参数
    ----
    path     : 文件路径
    encodings: 候选编码顺序（缺省 basicSettings.IMPORT_ENCODINGS）

    返回
    ----
    (text, encoding)

    Raises
    ------
    ContactImportError —— 文件不存在/ 读不了 / 所有候选编码都解不出。

    ⚠️ 顺序里 utf-8 必须在 gbk **前面**（basicSettings 里有注释）：
       纯 ASCII 两者都能解出，中文 GBK 字节几乎不可能恰好是合法 UTF-8；
       反过来（先 gbk）会把 UTF-8 中文解成乱码而且**不报错** ——
       那是本项目最不能接受的一类静默错误。
    ⚠️ 候选里刻意**没有 latin-1**：它对任意字节都不会失败，
       有了它就永远不会「明确报错」，只会静默解出一堆乱码。
    """
    order = tuple(encodings or basicSettings.IMPORT_ENCODINGS)
    if not os.path.isfile(path):
        raise ContactImportError("文件不存在: %s" % path)
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError as e:
        raise ContactImportError("读取失败: %s（%s）" % (path, e))
    if not blob:
        raise ContactImportError("文件为空: %s" % path)
    for enc in order:
        try:
            return blob.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    raise ContactImportError(
        "无法识别文件编码: %s\n已试: %s\n"
        "请把文件另存为 UTF-8（带 BOM 最稳）后重试。"
        % (path, "/".join(order)))


# ============================================================
# 二、字段归一（生日 / 电话 / 姓名键）
# ============================================================

def normalizeBday(raw: str) -> tuple:
    """生日原文 -> (birthday 'YYYY-MM-DD' or'', 原值 or'', 是否有出生年)

    支持 1971-02-10 / 1971/2/10 / 1971.2.10 / 1971年2月10日 /
    1971-02-10T00:00:00Z / 19710210 / 1971 / 2/10/1971（Outlook 英文版 M/D/Y）

    为什么这么在意「有没有出生年」：pb_person.birthday 决定分桶方式 ——
    有出生年走自适应分桶，无出生年静默降级为等宽 5 年（跨年代认人明显更差）。
    只有月日（`--0210`）去掉非数字后正好剩 4 位，不做 plausibility 判断就会
    变成「公元 210 年」，用户以为走的是自适应分桶。
    所以认不出来时返回空串（**不塞进 birthday**），并把原值带出去提示补录。
    """
    text = str(raw or "").strip()
    if not text:
        return "", "", False
    body = text.split("T")[0].strip()
    # 中文日期「1971年2月10日」：先把年月日换成 -再走统一逻辑，
    # 否则去掉非数字后剩 7 位（1971210），既不够 8 位也不是 4 位 -> 静默丢生日
    body = re.sub(r"[年\.]", "-", body)
    body = re.sub(r"月", "-", body)
    body = body.replace("日", "").replace("号", "")

    # Outlook 英文版：M/D/YYYY（3 段、末段 4 位）。中文版是 YYYY-M-D，已被下面覆盖
    md = re.match(r"^(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})$", body)
    if md:
        month, day, year = int(md.group(1)), int(md.group(2)), int(md.group(3))
        if basicSettings.IMPORT_MIN_YEAR <= year <= basicSettings.IMPORT_MAX_YEAR \
                and 1 <= month <= 12 and 1 <= day <= 31:
            return "%04d-%02d-%02d" % (year, month, day), text, True
        return "", text, False

    digits = re.sub(r"[^0-9]", "", body)
    if len(digits) >= 8:
        year, month, day = int(digits[0:4]), int(digits[4:6]), int(digits[6:8])
        if basicSettings.IMPORT_MIN_YEAR <= year <= basicSettings.IMPORT_MAX_YEAR \
                and 1 <= month <= 12 and 1 <= day <= 31:
            return "%04d-%02d-%02d" % (year, month, day), text, True
        return "", text, False
    if len(digits) == 4:
        year = int(digits)
        if basicSettings.IMPORT_MIN_YEAR <= year <= basicSettings.IMPORT_MAX_YEAR:
            return "%04d-01-01" % year, text, True
    # 4 位但不像年份（--0210 / 0210）-> 只有月日，年份未知
    return "", text, False


def phoneKey(raw: str) -> str:
    """电话归一化，**只用于比对**，绝不写进库。

    同一个号在不同导出里长得不一样：`+86285187018` 与 `02885187018` 是同一个。
    规则：只留数字 -> 去国际区号 86 -> 去长途前缀 0。
    ⚠️ 只归一化前缀，不合并号段：真实数据里 +86285187018 与 02885187018
       其实不是同一个号（前者少一位），差点被当成「同一人」丢掉一个真人。
    """
    digits = re.sub(r"[^0-9]", "", str(raw or ""))
    if not digits:
        return ""
    if len(digits) > 11 and digits.startswith("86"):
        digits = digits[2:]
    if len(digits) > 7 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def nameKey(text: str) -> str:
    """姓名比对键：Unicode NFKC + 去所有空白 + 小写。

    为什么去空白：Outlook 有时导出「Qing  Bai」，手机导出「QingBai」，
    肉眼是同一个人，但等值比较认不出来 -> 重复建档。
    为什么 NFKC：全角/半角、兼容字符统一，避免「Ⅶ」与「VII」这类差异。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or ""))).lower()


def sanitizeCodeKey(text: str, maxLen: int = 48) -> str:
    """把任意文本压成能进 personCode 的 ASCII 片段（可读优先，便于人工核对）。"""
    stem = re.sub(r"[^0-9A-Za-z]+", "_", unicodedata.normalize("NFKC", str(text or "")))
    stem = stem.strip("_")
    if not stem:
        # 全中文名/emoji 之类压不出 ASCII：用稳定摘要兜底，
        # 绝不能落到空串 —— personCode 为空会直接撞 UNIQUE 约束整批失败
        import hashlib
        stem = "c%s" % hashlib.sha1(
            unicodedata.normalize("NFKC", str(text or "")).encode("utf-8")).hexdigest()[:12]
    return stem[:maxLen]


# ============================================================
# 三、库内人员索引（幂等认人的依据）
# ============================================================

def loadPersonIndex(dbFile: str = None) -> dict:
    """一次性把 pb_person 读进内存建索引（人名册是**小表**，全读一次完全可接受）。

    返回
    ----
    dict:
        byUid   : {vcardUid -> row}
        byName  : {displayName 原样 -> row}
        byNameKey: {nameKey(displayName) -> row}
        byPhone : {phoneKey(phone) -> row}
        byEmail : {email 小写 -> row}
        byCode  : {personCode -> row}
        codes   : set(personCode)

    ⚠️ 为什么不按 vcardUid / phone 走生成层的 query 参数：
       生成层没有这几个查询参数，为一次导入去改生成器 + 全库迁移，
       代价与收益不成比例。分页读，避免一次性 dict 列表吃内存。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)          # DR-10：必须显式才切库
    index = {"byUid": {}, "byName": {}, "byNameKey": {}, "byPhone": {},
             "byEmail": {}, "byCode": {}, "codes": set()}
    offset = 0
    pageRows = basicSettings.IMPORT_PAGE_ROWS
    while True:
        got = sqliteCommon.query_pb_person("pb_person", mode="light", delFlag="*",
                                           orderBy="recID", limitNum=pageRows,
                                           offsetNum=offset)
        if not got:
            break
        for row in got:
            code = str(row.get("personCode") or "")
            if not code:
                continue
            index["codes"].add(code)
            index["byCode"][code] = row
            uid = str(row.get("vcardUid") or "")
            if uid:
                index["byUid"].setdefault(uid, row)
            name = str(row.get("displayName") or "")
            if name:
                index["byName"].setdefault(name, row)
                index["byNameKey"].setdefault(nameKey(name), row)
            key = phoneKey(row.get("phone"))
            if key:
                index["byPhone"].setdefault(key, row)
            mail = str(row.get("email") or "").lower()
            if mail:
                index["byEmail"].setdefault(mail, row)
        if len(got) < pageRows:
            break
        offset += pageRows
    return index


def loadCategoryIndex() -> dict:
    """pb_person_category -> {personCode: {category: delFlag}}

    连软删行一起读（delFlag="*"）：软删的分类行如果不管，
    重导时会插出第二行同(personCode, category) 的记录 —— 越导越多。
    """
    index = {}
    offset = 0
    pageRows = basicSettings.IMPORT_PAGE_ROWS
    while True:
        got = sqliteCommon.query_pb_person_category("pb_person_category", delFlag="*",
                                                     mode="light", orderBy="recID",
                                                     limitNum=pageRows,
                                                     offsetNum=offset)
        if not got:
            break
        for row in got:
            code = str(row.get("personCode") or "")
            cat = str(row.get("category") or "")
            if not code or not cat:
                continue
            index.setdefault(code, {})[cat] = str(row.get("delFlag") or comGD.DEL_FLAG_NO)
        if len(got) < pageRows:
            break
        offset += pageRows
    return index


def loadFamilyCodes() -> set:
    """库里已有的 familyCode 集合（用于区分「新建家庭组」与「更新已有家庭组」）"""
    codes = set()
    offset = 0
    pageRows = basicSettings.IMPORT_PAGE_ROWS
    while True:
        got = sqliteCommon.query_pb_family("pb_family", delFlag="*", mode="light",
                                           orderBy="recID", limitNum=pageRows,
                                           offsetNum=offset)
        if not got:
            break
        for row in got:
            code = str(row.get("familyCode") or "")
            if code:
                codes.add(code)
        if len(got) < pageRows:
            break
        offset += pageRows
    return codes


# ============================================================
# 四、编码与显示名分配
# ============================================================

def makePersonCode(prefix: str, key: str, index: dict, taken: set) -> str:
    """personCode = 前缀 + 净化后的业务键（超长截断 + 冲突加序号）。

    人名册编码要**可读**：出问题时在日志/截图里一眼能看出是谁，
    所以用姓名/UID 而不是随机串。
    """
    stem = sanitizeCodeKey(key)
    code = "%s%s" % (prefix, stem)[:64]
    n = 2
    while code in index["codes"] or code in taken:
        code = ("%s%s%d" % (prefix, stem, n))[:64]
        n += 1
    return code


def makeDisplayName(name: str, index: dict, taken: set) -> tuple:
    """displayName 是 UNIQUE 索引（不是普通列），撞了加序号，**绝不覆盖别人**。

    返回 (最终显示名, 是否被加了后缀)
    """
    text = str(name or "")[:128]
    if text not in index["byName"] and text not in taken:
        return text, False
    n = 2
    while ("%s(%d)" % (text, n))[:128] in index["byName"] \
            or ("%s(%d)" % (text, n))[:128] in taken:
        n += 1
    return ("%s(%d)" % (text, n))[:128], True


# ============================================================
# 五、plan：算动作（不写库）
# ============================================================

#: person 表里「导入只补空、不覆盖」的字段说明：
#:   导出常常缺列（很多 Outlook 导出根本没有 TEL），拿空值覆盖用户后来
#:   手工填的内容是**数据倒退**，而且不会有任何报错。
_CREATE_FIELDS = ("familyName", "relation", "email", "phone", "birthday",
                  "vcardUid", "source", "ownerID")
_UPDATE_FIELDS = ("familyName", "relation", "email", "phone", "birthday",
                  "vcardUid", "source")


def planContact(contact: dict, index: dict, prefix: str,
                takenNames: set, takenCodes: set, warnings: list) -> dict:
    """给**一个**联系人定动作，并把结果登记回 index（供同批后续联系人比对）。

    参数
    ----
    contact: 归一后的联系人 dict（见 csv_import/vcard_import 的说明）
    index  : loadPersonIndex() 的结果（本函数会就地补充本批已分配的条目）
    prefix : personCode 前缀（CSV 用 CS_，vCard 用 VC_）

    返回
    ----
    dict:
        op          : "create" / "update" / "skip"
        hit         : 靠什么认出来的人（vcardUid / displayName / ""）
        personCode  : 落库编码
        displayName : 最终显示名
        row         : pb_person 行（update 时只含「本次真的给了值」的列）
        categories  : 归一后的 category 列表（可能为空）
        labels      : {category: 原始标签}
        categoriesGiven : 来源文件是否**明确给了**非空 Categories
        groupCode   : vCard 显式家庭组编码（无则空串；**只用于建议去重**，
                      真正的挂接在 _applyGroups 里做）
        contact: 原联系人 dict（日志/报告用）
    """
    # ⚠️ 头像**不在这里逐卡验证**，只记当前状态；
    #    真正的落盘在 applyPlan（那里才拿得住 personCode），
    #    统计警告也在那里只出**一条**。
    #    真实通讯录里有 1012 张带头像的卡，逐卡一条就是
    #    1012 行一模一样的刷屏，把真正值得看的警告全部淹掉。
    if contact.get("hasPhoto") and not contact.get("photo"):
        warnings.append("%s 的 PHOTO 解不出来（非 base64，未下载外链）"
                        % contact.get("file", ""))

    rawName = str(contact.get("displayName") or "").strip()
    uid = str(contact.get("uid") or "").strip()
    hit = ""
    existing = None
    if uid:
        existing = index["byUid"].get(uid)
        if existing is not None:
            hit = "vcardUid"
    if existing is None and rawName:
        # 无 UID（或 UID 没命中）时按displayName 认人 —— MVP_plan S4 的第二条幂等规则
        existing = index["byNameKey"].get(nameKey(rawName))
        if existing is not None:
            hit = "displayName"

    #邮箱 / 电话撞车：**只提示，不自动合并**（号码会被复用，自动合并会毁数据）
    for field, hitKind in (("email", "邮箱"), ("phone", "电话")):
        value = str(contact.get(field) or "").strip()
        if not value:
            continue
        key = value.lower() if field == "email" else phoneKey(value)
        other = (index["byEmail"].get(key) if field == "email"
                 else index["byPhone"].get(key))
        if other is not None and (existing is None
                                  or other.get("personCode") != existing.get("personCode")):
            warnings.append(
                "%s（%s）的%s与库里 %s（%s）相同但没匹配上同一个人 ->"
                " **只提示，未自动合并**（邮箱/电话会被复用，自动合并会把两个人并成一个）"
                % (contact.get("file", ""), rawName, hitKind,
                   other.get("personCode"), other.get("displayName")))

    if existing is None:
        personCode = makePersonCode(prefix, uid or rawName, index, takenCodes)
        displayName, renamed = makeDisplayName(rawName, index, takenNames)
        if renamed:
            warnings.append(
                "%s：显示名「%s」与库中/本批已有的人重名，已记为「%s」"
                "（displayName 是 UNIQUE 键，**不能覆盖别人**）"
                % (contact.get("file", ""), rawName, displayName))
        row = {"personCode": personCode, "displayName": displayName,
               "source": comGD.PERSON_SOURCE_IMPORT, "isConfirmed": 0,
               "modifyYMDHMS": misc.getTime()}
        for field in _CREATE_FIELDS:
            if contact.get(field):
                row[field] = contact[field]
        if contact.get("ownerID"):
            row["ownerID"] = contact["ownerID"]
        memo = _memoOf(contact, isCreate=True)
        if memo:
            row["memo"] = memo
        op = "create"
    else:
        personCode = str(existing.get("personCode") or "")
        # ⚠️ 命中后**沿用库里那个显示名**，绝不能走 makeDisplayName ——
        #    那会给老档案改名叫「XXX(2)」，而用户从没要求过改名。
        displayName = str(existing.get("displayName") or rawName)
        row = {"personCode": personCode, "source": comGD.PERSON_SOURCE_IMPORT,
               "modifyYMDHMS": misc.getTime()}
        # ⚠️ displayName 必须在 **INSERT 列清单**里，但不进 updateColumns
        #   pb_person.displayName 是 NOT NULL 且**无 DEFAULT**；而写入形式是
        #   `INSERT ... ON CONFLICT(personCode) DO UPDATE SET ...`——SQLite 先按插入满足
        #   NOT NULL，再决定走不走冲突分支。所以一个不带 displayName
        #   的 update 行**根本进不去 DO UPDATE**，报的是
        #   `NOT NULL constraint failed: pb_person.displayName`。
        #   → 这里放进行里（作占位），而 applyPlan 会把 displayName
        #     从 update 批的 updateColumns 里剔掉 —— 库里的旧名字不会被覆盖。
        row["displayName"] = displayName
        for field in _UPDATE_FIELDS:
            value = contact.get(field)
            # vcardUid 例外：空值不覆盖（否则同名的另一个人会把别人的 UID 抹掉，
            # 下次导入就认不出本人了 -> 又建一个新档案）
            if value:
                row[field] = value
        if not str(existing.get("memo") or "").strip():
            memo = _memoOf(contact, isCreate=False)
            if memo:
                row["memo"] = memo
        op = "update"
        if hit == "displayName" and uid and not str(existing.get("vcardUid") or ""):
            warnings.append(
                "%s（%s）没有 UID，靠显示名认到已有档案 %s -> 已把 UID 补上"
                % (contact.get("file", ""), rawName, personCode))

    takenNames.add(displayName)
    takenCodes.add(personCode)
    categories, labels = splitCategories(contact.get("categories"))
    item = {"op": op, "hit": hit, "personCode": personCode,
            "displayName": displayName, "row": row,
            "categories": categories, "labels": labels,
            "categoriesGiven": bool(contact.get("categoriesGiven")),
            "groupCode": str(contact.get("groupCode") or ""),
            "contact": contact}
    _register(index, contact, item)
    return item


def _register(index: dict, contact: dict, item: dict) -> None:
    """把本条登记进内存索引：**同一批文件里**的两个联系人撞了邮箱/电话/显示名时
    也必须当成撞车处理。只在开头读一次库是不够的 —— 那等于
    「一个导出目录里塞了两份同一个人的名片」会建出两个人。"""
    code = item["personCode"]
    fake = {"personCode": code, "displayName": item["displayName"],
            "vcardUid": item["row"].get("vcardUid") or contact.get("uid") or "",
            "phone": item["row"].get("phone") or "",
            "email": item["row"].get("email") or ""}
    index["codes"].add(code)
    index["byCode"][code] = fake
    if fake["vcardUid"]:
        index["byUid"].setdefault(fake["vcardUid"], fake)
    index["byName"].setdefault(fake["displayName"], fake)
    index["byNameKey"].setdefault(nameKey(fake["displayName"]), fake)
    key = phoneKey(fake["phone"])
    if key:
        index["byPhone"].setdefault(key, fake)
    if fake["email"]:
        index["byEmail"].setdefault(fake["email"].lower(), fake)


def splitCategories(rawList) -> tuple:
    """原始类别标签列表 -> (归一后的 category 列表, {category: 原标签})

    分隔符：Outlook 用分号，中文导出常见逗号/顿号/全角分号 —— 一起认。
    去重按**归一后的值**：同一个人写了「家人;家庭」只落一行 family，
    否则 (personCode, category) 会重复（该表没有唯一索引，重复行不会报错）。
    """
    out, labels = [], {}
    for raw in (rawList or []):
        for piece in re.split(r"[;；,，、\n\r]+", str(raw or "")):
            label = piece.strip()
            if not label:
                continue
            cat = comGD.normalizeCategory(label)
            if not cat:
                continue
            if cat not in out:
                out.append(cat)
                labels[cat] = label[:32]
    return out, labels


def _memoOf(contact: dict, isCreate: bool) -> str:
    """memo 里留来源痕迹：哪个文件导的、生日原值、公司/职务（本表没有对应列）。

    只在**新建**时写满；更新时只在 memo 为空时补 —— 用户自己写的备注不能被
    一次导入冲掉。
    """
    bits = ["%s:%s" % (contact.get("sourceKind", "import"), contact.get("file", ""))]
    if contact.get("rowNo"):
        bits.append("行%s" % contact["rowNo"])
    if contact.get("uidFromFile"):
        bits.append("无UID(用文件名当幂等键)")
    if contact.get("bdayRaw") and not contact.get("birthday"):
        bits.append("生日=%s(无年份)" % contact["bdayRaw"])
    for field, label in (("company", "公司"), ("title", "职务"),
                         ("nickname", "昵称")):
        if contact.get(field):
            bits.append("%s=%s" % (label, contact[field]))
    if not isCreate:
        bits.append("导入补档")
    return "; ".join(bits)[:200]


# ============================================================
# 六、家庭组：显式组落库/ 同姓只提示（半自动原则）
# ============================================================

def suggestFamilies(items: list) -> list:
    """同姓多人 -> 「建议合并为一个家庭组」。**只产出建议，绝不自动建。**

    为什么不能自动建：同姓不等于一家人（同事里同姓的极多）。自动建组等于
    替用户做了一个人际关系的判断，事后还很难发现 —— 这与 UI 设计 P0-2
    「不确定就要问，不要猜」是同一条纪律。

    已经明确挂在同一个 familyGroupCode 下的人不再提示（组已经存在了）。
    """
    buckets = {}
    for item in items:
        if item.get("op") == "skip":
            continue
        familyName = str(item["contact"].get("familyName") or "").strip()
        if not familyName:
            continue
        buckets.setdefault(nameKey(familyName), []).append(item)
    out = []
    for _key, group in buckets.items():
        if len(group) < 2:
            continue
        # 已挂同一个家庭组的人不再提示（组已经存在了，提示是噪音）
        groupCodes = set()
        for it in group:
            groupCodes.add(str(it.get("groupCode")
                              or it["contact"].get("familyGroupCode") or ""))
        groupCodes.discard("")
        if len(groupCodes) == 1:
            continue
        out.append({
            "familyName": group[0]["contact"]["familyName"],
            "suggestCode": "%s%s" % (basicSettings.IMPORT_CODE_PREFIX_FAMILY,
                                     sanitizeCodeKey(group[0]["contact"]["familyName"])),
            "persons": [(it["personCode"], it["displayName"]) for it in group],
        })
    out.sort(key=lambda x: (-len(x["persons"]), x["familyName"]))
    return out


# ============================================================
# 七、归档原件
# ============================================================

def archiveOriginal(srcPath: str, kind: str = "import", stamp: str = None) -> dict:
    """把导入原件**复制**到 <dbDir>\\imports\\<时间戳>_<kind>\\ 下。

    返回 {"ok": bool, "path": str, "errMsg": str}

    为什么归档：通讯录是「会变的外部数据」，三个月后用户会问
    「库里这个生日是哪版通讯录里的」。留一份原件 + 时间戳，答案就在那。
    为什么用复制而不是移动：原件在 Outlook/手机里是**只读的事实来源**，
    步骤 8 无权改动用户的文件（与「原图只读」同一条纪律）。
    """
    src = os.path.abspath(srcPath)
    if not os.path.isfile(src):
        return {"ok": False, "path": "", "errMsg": "原件不存在: %s" % src}
    try:
        # ⚠️ 归档落点必须跟随**实际在写的那个库**
        #   paths.imports_dir() 是从 paths.db_file() 推导的，而 db_file() 又是从
        #   PHOTO_ROOT 推导的。而 `--db 临时库` / 单测的 dbHandle(临时库)
        #   **并不会**改 PHOTO_ROOT —— 归档会落到正式树里，
        #   而实验库里一份都没有。这与 DR-10 是同一类错：
        #   「当前连的库」和「路径推导的库」不是一个东西。
        dbFile = sqliteCommon.dbFilePath()
        base = (os.path.join(os.path.dirname(os.path.abspath(dbFile)), "imports")
                if dbFile else paths.imports_dir())
        photoDir = paths.photo_dir()
    except paths.PathLayoutError as e:
        return {"ok": False, "path": "", "errMsg": "路径布局非法: %s" % e}
    if os.path.commonpath([os.path.normcase(base), os.path.normcase(photoDir)]) \
            == os.path.normcase(photoDir):
        # 理论上 validate_layout 已经挡住了；这里再挡一次，
        # 因为「往 photo 里写东西」是本项目唯一不可谈判的红线
        return {"ok": False, "path": "",
                "errMsg": "拒绝归档到 photo 目录内: %s" % base}
    folder = os.path.join(base, "%s_%s" % (stamp or misc.getTime(), kind))
    try:
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, os.path.basename(src))
        n = 1
        while os.path.exists(target):
            target = os.path.join(folder, "%s(%d)%s"
                                  % (os.path.splitext(os.path.basename(src))[0],
                                     n, os.path.splitext(src)[1]))
            n += 1
        shutil.copy2(src, target)      # copy2 保留 mtime，方便对账
        return {"ok": True, "path": target, "errMsg": ""}
    except OSError as e:
        return {"ok": False, "path": "", "errMsg": "归档失败: %s（%s）" % (src, e)}


# ============================================================
# 八、apply：分批落库
# ============================================================

def applyPlan(planned: dict, ownerID: str = "", batchRows: int = None) -> dict:
    """按 plan 落库。**分批提交**，每批自带事务（不做长事务）。

    返回统计 dict：
        created / updated / skipped / failed  : personCode 列表
        categoryRows      : 本次涉及的有效分类行总数（新增+保留）
        categoryAdded / categoryRemoved / categoryRevived : 分类行变化
        families / familyCreated / familyMembers / familyUnresolved
        avatars / avatarFailed : 头像落盘结果
        familySuggestions / warnings / archive
    """
    batch = int(batchRows or basicSettings.IMPORT_BATCH_ROWS)
    if batch < 1:
        batch = basicSettings.IMPORT_BATCH_ROWS
    summary = {"created": [], "updated": [], "skipped": [], "failed": [],
               "categoryRows": 0, "categoryAdded": 0, "categoryRemoved": 0,
               "categoryRevived": 0, "families": [], "familyCreated": [],
               "familyMembers": 0, "familyUnresolved": [],
               # 头像：落盘成功的 personCode / 失败的 (personCode, 原因)
               "avatars": [], "avatarFailed": [],
               "familySuggestions": [], "warnings": list(planned.get("warnings") or [])}
    items = list(planned.get("items") or [])
    if not items and not planned.get("groups"):
        return summary

    # ---- 0. 头像落盘（必须在写库**之前**）----
    # 头像落点：<thumb>\vcards\<sha1(personCode)[:2]>\<sha1(personCode)>.jpg（可推导，DR-1）
    # ⚠️ 为什么落盘不走 pb_face：pb_face.photoCode 是 NOT NULL 外键，
    #    一张人脸必须挂在某张 pb_photo 下；而通讯录头像是 vCard 内嵌的
    #    base64，**不属于任何一张照片**。硬塞就要虚构一行“无照片的
    #    pb_face”，而那会破坏上面那条不变式。
    # ⚠️ 头像写不下不能中断导入：头像是附赠品，人名册才是主线。
    from processor.media import thumbStore as _thumbStore
    for item in items:
        photo = (item.get("contact") or {}).get("photo")
        if not photo or item["op"] == "skip":
            continue
        try:
            item["row"]["avatarFile"] = _thumbStore.write_vcard_avatar(
                item["personCode"], photo)
            summary["avatars"].append(item["personCode"])
        except Exception as e:                # noqa: BLE001 - 头像失败不得进中断导入
            summary["avatarFailed"].append((item["personCode"], str(e)))
            summary["warnings"].append("头像写入失败 %s: %s"
                                       % (item["personCode"], e))
            _LOG.error("头像写入失败 %s: %s", item["personCode"], e)
    if summary["avatars"]:
        summary["warnings"].append(
            "共 %d 个联系人的头像已落盘到 <thumb>\\%s\\（不属于任何一张照片）"
            % (len(summary["avatars"]), basicSettings.VCARD_SUBDIR))

    # ---- 1. pb_person 分批 upsert ----
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        for op in ("create", "update"):
            rows = []
            for item in chunk:
                if item["op"] != op:
                    continue
                row = dict(item["row"])
                if ownerID:
                    row["ownerID"] = ownerID
                rows.append((item, row))
            if not rows:
                continue
            # updateColumns = 本行真正要刷的列（除幂等键与标准字段）。
            # 这样「本次没给的列」在 SQL 里根本不出现 —— 天然实现「空值不覆盖」
            # ⚠️ update 批里**剔掉 displayName**（不让导入去改名）
            #   它在 row 里只是为了让 INSERT 满足 NOT NULL（见 planContact 的注释）。
            #   放进 updateColumns 就变成「用户改过的名字被导入覆盖」——
            #   而名字是用户的，不是导入的事。
            skipOnUpdate = ("personCode", "regYMDHMS", "delFlag",
                            "displayName") if op == "update" else (
                            "personCode", "regYMDHMS", "delFlag")
            updateColumns = []
            for _item, row in rows:
                for name in row:
                    if name not in updateColumns and name not in skipOnUpdate:
                        updateColumns.append(name)
            dataSetList = [row for _item, row in rows]
            rtn, _cols = sqliteCommon.insertManyTableGeneral(
                "pb_person", dataSetList, conflictColumns=("personCode",),
                updateColumns=tuple(updateColumns), fillStandard=True)
            if rtn == sqliteHandle.RET_ERROR:
                errMsg = sqliteCommon.dbHandle().lastErrMsg
                for item, _row in rows:
                    summary["failed"].append((item["personCode"], errMsg))
                    summary["warnings"].append(
                        "写库失败 %s: %s" % (item["personCode"], errMsg))
                _LOG.error("applyPlan 写pb_person 失败: %s", errMsg)
                continue
            for item, _row in rows:
                summary["created" if op == "create" else "updated"].append(item["personCode"])
                _LOG.info("%s %s（%s）生日=%s 分类=%s", op, item["personCode"],
                          item["displayName"],
                          item["row"].get("birthday") or "无",
                          ",".join(item["categories"]) or "无")
        # 每批之间让一下：导入不该把机器占满，中途 Ctrl+C 也能立刻响应
        _progress("已处理 %d / %d 人" % (min(start + batch, len(items)), len(items)))

    summary["skipped"] = [it["personCode"] for it in items if it["op"] == "skip"]

    # ---- 2. 分类行：差异重写（不残留旧行，也不重复插） ----
    _syncCategories(items, summary)

    # ---- 3. 显式家庭组（vCard KIND:group） ----
    _applyGroups(planned, summary)

    # ---- 4. 同姓建议（只提示） ----
    summary["familySuggestions"] = suggestFamilies(items)
    return summary


def _progress(text: str) -> None:
    """导入进度：P0-4「进度诚实」，不打假进度条，只报真实计数"""
    if os.environ.get("PHOTO_BROWSER_QUIET"):
        return
    sys.stdout.write("  ... %s\n" % text)
    sys.stdout.flush()


def _syncCategories(items: list, summary: dict) -> None:
    """pb_person_category 重写。

    规则（对应验收「重复导入不得残留旧分类行」）
    ------------------------------------------
      * 本次**给了**非空 Categories -> 该人的分类集合以本次为准：
        旧集合里本次没有的行删掉（否则库里留着「上版本的分类」），
        本次的行缺则插、软删则复活。重复导入同一文件 -> 0 删 0 插。
      * 本次 Categories 为**空** -> 一行都不动。理由：Outlook 导出的
        Categories 列对很多人是空的，那是「没填」而不是「清空」。
        把空当清空会把用户手工加的分类全部抹掉 —— 那是数据丢失，不是幂等。
    """
    index = loadCategoryIndex()
    now = misc.getTime()
    pending = []
    for item in items:
        if item["op"] == "skip" or not item.get("categoriesGiven"):
            continue
        code = item["personCode"]
        desired = list(item["categories"])
        exist = index.get(code, {})
        stale = [cat for cat in exist if cat not in desired]
        for cat in stale:
            sqliteCommon.deleteTableGeneral(
                "pb_person_category", "personCode = %s AND category = %s",
                (code, cat))
            summary["categoryRemoved"] += 1
            exist.pop(cat, None)
        for cat in desired:
            if cat in exist:
                summary["categoryRows"] += 1
                if exist[cat] != comGD.DEL_FLAG_NO:
                    sqliteCommon.updateTableGeneral(
                        "pb_person_category", "personCode = %s AND category = %s",
                        (code, cat),
                        {"delFlag": comGD.DEL_FLAG_NO, "modifyYMDHMS": now})
                    summary["categoryRevived"] += 1
                    exist[cat] = comGD.DEL_FLAG_NO
                continue
            pending.append({"personCode": code, "category": cat,
                            "label": item["labels"].get(cat),
                            "memo": "导入:%s" % item["contact"].get("file", ""),
                            "regYMDHMS": now, "modifyYMDHMS": now})
            exist[cat] = comGD.DEL_FLAG_NO
            summary["categoryRows"] += 1
    batch = basicSettings.IMPORT_BATCH_ROWS
    for start in range(0, len(pending), batch):
        rtn, _cols = sqliteCommon.insertManyTableGeneral(
            "pb_person_category", pending[start:start + batch], fillStandard=True)
        if rtn == sqliteHandle.RET_ERROR:
            errMsg = sqliteCommon.dbHandle().lastErrMsg
            summary["warnings"].append("分类行写入失败: %s" % errMsg)
            _LOG.error("分类行写入失败: %s", errMsg)
        else:
            summary["categoryAdded"] += rtn if rtn > 0 else 0


def _applyGroups(planned: dict, summary: dict) -> None:
    """vCard 里**显式**声明的 KIND:group -> pb_family，成员挂 familyGroupCode。

    只认通讯录里明写的组。同姓多人**不在这里建组**（见 suggestFamilies）。
    """
    groups = planned.get("groups") or []
    if not groups:
        return
    now = misc.getTime()
    existed = loadFamilyCodes()
    for grp in groups:
        row = {"familyCode": grp["familyCode"], "familyName": grp["familyName"],
               "notes": grp.get("notes"), "memo": "vCard KIND:group:%s" % grp.get("file", ""),
               "modifyYMDHMS": now}
        rtn, _cols = sqliteCommon.insertManyTableGeneral(
            "pb_family", [row], conflictColumns=("familyCode",),
            updateColumns=("familyName", "notes", "memo", "modifyYMDHMS"),
            fillStandard=True)
        if rtn == sqliteHandle.RET_ERROR:
            errMsg = sqliteCommon.dbHandle().lastErrMsg
            summary["warnings"].append("家庭组 %s 写入失败: %s"
                                       % (grp["familyCode"], errMsg))
            continue
        summary["families"].append(grp["familyCode"])
        if grp["familyCode"] not in existed:
            summary["familyCreated"].append(grp["familyCode"])
        for code in grp["members"]:
            got = sqliteCommon.query_pb_person("pb_person", personCode=code, mode="light")
            if not got:
                summary["familyUnresolved"].append((grp["familyName"], code))
                continue
            current = str(got[0].get("familyGroupCode") or "")
            if current and current != grp["familyCode"]:
                # 已经在别的组里 -> 不覆盖，只提示（半自动）
                summary["warnings"].append(
                    "%s 已属于家庭组 %s，**未改挂**到 %s"
                    % (got[0].get("displayName"), current, grp["familyName"]))
                continue
            sqliteCommon.updateTableGeneral(
                "pb_person", "personCode = %s", (code,),
                {"familyGroupCode": grp["familyCode"], "modifyYMDHMS": now})
            summary["familyMembers"] += 1


if __name__ == "__main__":
    _fixConsole()
    print("contactCommon _VERSION :", _VERSION)
    print("编码嗅探顺序           :", basicSettings.IMPORT_ENCODINGS)
    print("分批提交行数           :", basicSettings.IMPORT_BATCH_ROWS)
    print("personCode 前缀: csv=%s vcard=%s family=%s"
          % (basicSettings.IMPORT_CODE_PREFIX_CSV,
             basicSettings.IMPORT_CODE_PREFIX_VCARD,
             basicSettings.IMPORT_CODE_PREFIX_FAMILY))
    for probe in ("1971-02-10", "19710210", "1971", "--0210", "2/10/1971",
                  "1971年2月10日", ""):
        print("  normalizeBday(%-12r) -> %s" % (probe, normalizeBday(probe)))
    for probe in ("家人", "大学同学", " Family ", ""):
        print("  normalizeCategory(%-10r) -> %r" % (probe, comGD.normalizeCategory(probe)))
    print("splitCategories(['家人;朋友,大学同学']) ->",
          splitCategories(["家人;朋友,大学同学"]))
