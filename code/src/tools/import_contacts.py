#! /usr/bin/env python3
#encoding: utf-8
#Filename: import_contacts.py
#Description: photo-browser 通讯录导入（vCard -> pb_person）——
#             把手机导出的 .vcf 导入成人员档案
#
# 为什么需要这个工具
# ------------------
#   pb_person 是**质心与分桶的锚**：birthday 决定走自适应分桶还是等宽降级，
#   没有档案就永远匹配不出任何东西。而 vCard 是唯一**零人工录入成本**的
#   人员来源 —— 手机里本来就有姓名与生日。
#
# 幂等（三道防线，缺一道就可能造出重复档案）
# ------------------------------------------
#   ① 主键 vcardUid：vCard 规范里的 UID，重复导入靠它认人。
#   ② 兜底键：⚠️ 真实导出里**大量 vcf 根本没有 UID**，缺 UID 时退到
#      「文件名（不含扩展名）」当 vcardUid —— 同一批文件重复导入不会重复建档，
#      但**改名后再导会变成两个人**（报告会点名提示）。
#   ③ 联系方式：手机号 / 邮箱任一撞上库里已有的人、且 vcardUid 不同 ->
#      判为**疑似同一人**，默认**跳过并报告**，不自动合并。
#      为什么不用「有电话就算同一人」直接覆盖：号码会被复用（换号、回收号），
#      自动合并会把两个人并成一个，且事后**没有任何提示**。
#
# 生日口径（对匹配质量影响最大的一列）
# ------------------------------------
#   pb_person.birthday 直接决定分桶方式（bucket.birthYearOf）：
#     有出生年 -> 自适应分桶（0~18 岁每 3 年 / 18+ 每 10 年）
#     无出生年 -> 等宽 5 年降级（桶宽与年龄无关，跨年代认人明显更差）
#   所以 BDAY 解析不出来时必须**明确报告**，不能默默留空。
#   BDAY 写法有 1971-02-10 / 1971-02-10T00:00:00Z / 19710210 / 1971 /
#   --0210（只有月日）几种。只有月日时年份未知（会走等宽降级），
#   原值记进 memo 而**不是**塞进 birthday —— 塞进去会让 birthYearOf 拿到
#   一个非法年份、静默走降级，而用户以为走的是自适应。
#
# 覆盖规则
# --------
#   同一 vcardUid 已存在时**只补空、不覆盖**：vCard 里缺字段很常见
#   （有的导出根本没有 TEL），拿空值覆盖用户后来手工填的内容是数据倒退。
#   displayName 例外：它是 UNIQUE 键，冲突时加序号后缀而不是覆盖别人。
#
# 不做的事
# --------
#   * **不导 PHOTO（头像）**：pb_face.photoCode 是 NOT NULL 外键，头像不属于
#     任何 pb_photo 照片行；要落库得先造一条「虚拟照片」，那是另一个决定
#     （而且通讯录头像多是旧照/证件照，拿它当质心样本会引入偏斜分布）。
#     本工具只统计数量并报告。
#   * **不猜relation / familyGroupCode**：通讯录里没有家庭关系，
#     按姓氏归组是**猜测**（同姓未必是家人）。
#
# 用法
# ----
#   python code\src\tools\import_contacts.py --root D:\temp\contacts --dry-run
#   python code\src\tools\import_contacts.py --root D:\temp\contacts --yes
#   python code\src\tools\import_contacts.py --root D:\temp\contacts --yes --db d:\tmp\x.db
#   python code\src\tools\import_contacts.py --root D:\temp\contacts --yes --merge-same-phone
#
# 硬约束：只 INSERT/UPDATE pb_person；**不动 pb_face / pb_photo / pb_photo_person**，
#         不碰 photo 目录（本工具根本不读图片文件）。

import argparse
import base64
import binascii
import os
import re
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                            # noqa: E402
from common import miscCommon as misc                                  # noqa: E402
from common import pinyin as pinyin                                    # noqa: E402
from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from processor.media import thumbStore as thumbStore      # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("importContacts", "importcontacts.log")

#: TEL 参数类型优先级：手机号最可能是「本人常用号码」
_TEL_PREF: tuple = ("CELL", "MOBILE", "MAIN", "WORK", "HOME", "VOICE")
#: 邮箱参数类型优先级（WORK 在前：工作邮箱往往更稳定）
_MAIL_PREF: tuple = ("WORK", "HOME", "INTERNET", "OTHER")

_VCARD_EXT: tuple = (".vcf", ".VCF", ".vcard")
#: personCode 前缀。与 merger 拆人建档用的手工编码区分开，便于一眼看出是导入的
_CODE_PREFIX: str = "VC_"
#: pb_person.vcardUid 里存「文件名兜底」时加的前缀（便于与真 UID 区分）
_UID_FILE_PREFIX: str = "file:"
#: BDAY 里 4 位数字被当成「年份」的合理窗口。低于 _MIN_YEAR 的（如 --0210
#: 去掉非数字后剩 0210）一律当「只有月日」，否则会静默走等宽降级。
_MIN_YEAR: int = 1900
_MAX_YEAR: int = 2099


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ============================================================
# 一、vCard 解析
# ============================================================

def unfold(text: str) -> list:
    """还原折行（RFC 6350：续行以空格或 TAB 开头）"""
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def splitValue(line: str) -> tuple:
    """`TEL;CELL;CHARSET=UTF-8:+8613` -> (("TEL","CELL"),("CHARSET","UTF-8"), 值)

    ⚠️ 传进来的是**整行**（含属性名），不是冒号前那段。
       分号只在**参数区**里是分隔符，值里也可能有分号（ADR 的街道/城市/邮编
    就用分号隔），所以要逐字符扫并跟踪引号状态，不能直接 split(';')。
    """
    params, buf, inQuote, i = [], [], False, 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            inQuote = not inQuote
            buf.append(ch)
        elif ch == ":" and not inQuote:
            break
        elif ch == ";" and not inQuote:
            params.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        # ⚠️ 冒号前的**最后一段**参数也要收进来。
        #    漏了它的话 `ADR;WORK;CHARSET=UTF-8:xxx` 会被解析成只有
        #    ADR/WORK 两个参数 —— CHARSET/ENCODING 静默消失，
        #    而值看起来完全正常，解析不报错。
        params.append("".join(buf))
    keys = []
    for item in params:
        name, _sep, value = item.partition("=")
        keys.append((name.strip().upper(), value.strip().strip('"')))
    return tuple(keys), line[i + 1:]


def parseCards(text: str) -> list:
    """一个文件里可能有多个 vCard（本批是一家一个，但别写死）"""
    cards, cur, inCard = [], [], False
    for line in unfold(text):
        upper = line.strip().upper()
        if upper.startswith("BEGIN:VCARD"):
            cur, inCard = [], True
            continue
        if upper.startswith("END:VCARD"):
            if inCard:
                cards.append(cur)
            cur, inCard = [], False
            continue
        if inCard and line.strip():
            cur.append(line)
    return cards


def collect(card: list) -> dict:
    """一张 vCard -> dict[属性名] = list[(params, value)]"""
    out = {}
    for line in card:
        if ":" not in line:
            continue
        keys, value = splitValue(line)
        prop = (keys[0][0] if keys and keys[0][0]
                else line.split(":", 1)[0].strip().upper())
        out.setdefault(prop, []).append((dict(keys[1:]), value))
    return out



# ============================================================
# 一之二、ENCODING 解码（QUOTED-PRINTABLE / BASE64）
# ============================================================
# 为什么不能忽略（真实数据给的诚实话）
# ------------------------------------------
#   本机真实通讯录（lianyi-unique.vcf，2030 张卡）里：
#     · 821/2027 个联系人的 FN 是 QUOTED-PRINTABLE 中文——
#       不解码就是 821 个 `=E5=AD=A3=E7=BA=A2=E6=B1=9F` 不能看的人名，
#       而且**不报错**（它就是一个合法的字符串）。
#     · 1012 张卡的 PHOTO 是 BASE64 JPEG，不解码就完全抹掉头像。
#   两者是同一个失调：**参数里的 ENCODING 被当成没看见**。
#
# 为什么写在这里而不是分散到各处
# --------------------------------------------
#   解码只能在“拆完键对之后、取值之前”做：
#   它需要知道原行上的参数（ENCODING=TYPE=TYPE）。
#   放在 collect() 里做 -> 每个属性都是解码后的值，
#   下游一律拿到的都是人话（不拿到一个屏幕上的 =E5=AD=A3）。

_QP_SOFT_BREAK = re.compile(r"=(?:\r\n|\n|\r)")
# 说明：软换行在 vCard 3.0 里就是「=回车」这两个字符，
# 必须匹配**整个回车**（=\r\n / =\n / =\r）。
# 只匹配单个字符的话，= 后面会留下一个回车字符，
# 结果是「字与字之间多了个换行」—— 中文名被拆成两行，
# 而下面的字节解码会直接报错或解出一半个字。
# 说明：软换行在 vCard 3.0 里就是“=回车”这两个字符，
# 排除它之后下面的 =E5=AD=A3= 才能正确拼回一个字。


def decodeValue(value: str, params: dict) -> str:
    """按 ENCODING 参数解码一个属性的值。**返回可读文本**。

    支持
    ----
      QUOTED-PRINTABLE（大实例里的中文都是它）
          =E5=AD=A3 -> 字；软换行的 =\r\n 去掉；
          末尾的 = 表示“这个空格被转义了”，解码时变回空格。
      BASE64（PHOTO 头像）
          去掉空白与换行内容后解码；失败时返回空串（不抛）。
      BASE64 本身不适用于文本属性（解出的是乱码），
      所以只有 PHOTO/PHOTO-DATA 路径会用到 (photoOf())。

    未知的 ENCODING 原样返回（不猜）—— 猜错的结果比不解码更糟。
    """
    enc = str(params.get("ENCODING") or "").strip().upper()
    text = str(value or "")
    if not enc:
        return text
    if enc == "QUOTED-PRINTABLE":
        # 软换行必须先接上（否则中间会多出一个 = 导致解码错位）
        text = _QP_SOFT_BREAK.sub("", text)
        out = bytearray()
        i = 0
        while i < len(text):
            ch = text[i]
            if ch != "=":
                out.extend(ch.encode("utf-8"))
                i += 1
                continue
            hexPart = text[i + 1:i + 3]
            if len(hexPart) == 2 and all(c in "0123456789abcdefABCDEF"
                                         for c in hexPart):
                out.append(int(hexPart, 16))
                i += 3
                continue
            # = 后面不是两个十六进制字符 -> RFC 规定它转义的是**空格**，
            # 包括行末单独一个 =（它后面是空串）。
            out.append(0x20)
            i += 1
        return out.decode("utf-8", "replace")
    if enc in ("BASE64", "B"):
        return decodeBase64(text)
    return text


def decodeBase64(text: str) -> str:
    """BASE64 -> 字节字符串；不合法返回 ""（不抛）。

    为什么不抛异常：头像是**附赠品**，一个人头像解不出来
    不应该让**整个通讯录导入**报错（那是更惨的失败）。
    返回空串后由调用方当作“这个人没头像”处理。
    """
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return ""
    # 邮件客户端常在前面带 data:前缀或换行的 MIME 头
    if":" in compact[:40] and not compact.startswith("/"):
        head, _sep, rest = compact.partition(":")
        if "," in rest:
            compact = rest.split(",", 1)[1]
    compact = compact.rstrip("=")
    pad = (-len(compact)) % 4
    try:
        return base64.b64decode(compact + "=" * pad).decode("latin-1")
    except (binascii.Error, ValueError):
        return ""


def photoBytesOf(value: str, params: dict) -> bytes:
    """PHOTO 属性的值 -> 图像字节（解不出返回 b""）。

    两种形式：
      · PHOTO;ENCODING=BASE64;JPEG:/9j/4AA...   -> 直接 base64（本机真实数据就是这种）
      · PHOTO;VALUE=URI:http://...            -> 是 URL，**不下载**（不该在导入时去拉网络）
    本函数一律返回**字节**，而不是 str：写盘时不能再经过一次编码。
    """
    raw = str(value or "")
    if str(params.get("VALUE") or "").strip().upper() == "URI":
        return b""                                # 外链头像：不下载
    text = decodeBase64(raw if str(params.get("ENCODING") or "").strip().upper()
                        in ("BASE64", "B") else raw)
    if not text:
        return b""
    try:
        return text.encode("latin-1")
    except (UnicodeEncodeError, ValueError):
        return b""

def _first(cards: dict, prop: str, prefs: tuple = ()) -> str:
    """按参数类型优先级取值；都不匹配就取第一个非空。

    ⚠️ 顺序不能反：**先挑值、后解码**
    --------------------------------------------
      解码需要知道行上的参数（ENCODING=TYPE），而值本身一旦先被解码就丢了参数。
      且不能对所有值无条件解码：BASE64 的值里全是 '='，而本模块的
      photoBytesOf() 必须拿**原始**字符串去解 base64。
    """
    items = cards.get(prop) or []
    for want in prefs:
        for param, value in items:
            types = [t.upper() for t in
                     str(param.get("TYPE", "")).replace(",", " ").split()]
            if want in types and value.strip():
                return decodeValue(value.strip(), param)
    for param, value in items:
        if value.strip():
            return decodeValue(value.strip(), param)
    return ""


def photoOf(cards: dict) -> bytes:
    """PHOTO 属性 -> 图像字节（解不出返回 b""）。

    为什么不走 _first：头像要的是**字节**，不是解码后的字符串；
    且必须拿未解码的原值。
    """
    for params, value in (cards.get("PHOTO") or []):
        data = photoBytesOf(value, params)
        if data:
            return data
    return b""


def normalizeBday(raw: str) -> tuple:
    """vCard BDAY -> (birthday or'', 原值 or'', 是否有出生年)

    支持：1971-02-10 / 1971-02-10T00:00:00Z / 19710210 / 1971 / --0210
    返回的 birthday 一律是 'YYYY-MM-DD'（只有年份时补 01-01），
    这样 bucket.birthYearOf(text[:4]) 与按月的展示都对得上。

    ⚠️ `--0210`（vCard 允许「只有月日」）去掉非数字后正好剩 4 位 ——
       不加 plausibility 判断就会把它当成**公元 210 年**，于是
       birthYearOf 拿到非法年份 -> 静默走等宽 5 年降级，
       而用户以为走的是自适应分桶。所以 4 位数字必须落在合理年份窗口内。
    """
    text = str(raw or "").strip()
    if not text:
        return "", "", False
    body = text.split("T")[0].strip()
    digits = re.sub(r"[^0-9]", "", body)
    if len(digits) >= 8:
        return ("%s-%s-%s" % (digits[0:4], digits[4:6], digits[6:8]),
                text, True)
    if len(digits) == 4 and _MIN_YEAR <= int(digits) <= _MAX_YEAR:
        return "%s-01-01" % digits, text, True
    # 4 位但不像年份（--0210 / 0210）-> 只有月日，年份未知
    return "", text, False


def _decodeName(cards: dict) -> tuple:
    """(显示名, 姓)。FN 优先；没有 FN 就用 N 拼「姓+名」。"""
    full = _first(cards, "FN")
    if not full:
        parts = (_first(cards, "N") or "").split(";")
        family = parts[0].strip() if parts else ""
        given = parts[1].strip() if len(parts) > 1 else ""
        full = ("%s %s" % (given, family)).strip() if family else given
    family = ""
    parts = (_first(cards, "N") or "").split(";")
    if parts:
        family = parts[0].strip()
    return full.strip(), family


def readContact(path: str) -> dict:
    """读一个 vcf 文件 -> 联系人 dict（保留旧签名：取第一张有姓名的卡）"""
    got = readContacts(path)
    return got[0] if got else {}


def readContacts(path: str) -> list:
    """读一个 vcf 文件 -> **联系人列表**（一个文件里可以有多张卡）。

    别偷懒只取第一张：本批 10 个文件恰好是一家一张卡，但「一个文件多张卡」
    是vCard 的正常用法（导出多联系人时很常见），只导第一张会**静默丢人**。
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            text = blob.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("%s 无法解码" % path)
    stem = os.path.splitext(os.path.basename(path))[0]
    out = []
    for index, card in enumerate(parseCards(text)):
        picked = collect(card)
        displayName, familyName = _decodeName(picked)
        if not displayName:
            continue
        birthday, bdayRaw, hasYear = normalizeBday(_first(picked, "BDAY"))
        uid = _first(picked, "UID")
        if not uid:
            # 同一文件里的第 2 张卡要用「文件名#2」兜底，否则两张卡会撞成一个人
            uid = _UID_FILE_PREFIX + (stem if index == 0
                                      else "%s#%d" % (stem, index + 1))
        out.append({
            "file": os.path.basename(path),
            "vcardUid": uid,
            "uidFromFile": (not _first(picked, "UID")),
            "displayName": displayName,
            "familyName": familyName,
            "phone": _first(picked, "TEL", _TEL_PREF),
            "email": _first(picked, "EMAIL", _MAIL_PREF),
            "birthday": birthday,
            "bdayRaw": bdayRaw,
            "hasBirthYear": hasYear,
            "hasPhoto": bool(picked.get("PHOTO")),
            # 头像字节（已解 base64）。为空表示没有头像 /
            # 解不出（两者对上面的警告口径一样，都是“导不出”）。
            "photo": photoOf(picked),
            "nickname": _first(picked, "X-ANDROID-CUSTOM"),
        })
    return out


# ============================================================
# 二、与库里已有人员对账（幂等三道防线）
# ============================================================

def phoneKey(raw: str) -> str:
    """电话归一化，**只用于比对**，绝不写进库。

    为什么要归一化：同一个号码在不同的导出里长得不一样 ——
    `+86285187018`（国际格式）与 `02885187018`（本地格式）是同一个号。
    不归一化的话第③ 道防线（联系方式撞车）对最常见的一类重复完全失效。

    规则：只留数字 -> 去国际区号 86 -> 去长途前缀 0。
    """
    digits = re.sub(r"[^0-9]", "", str(raw or ""))
    if not digits:
        return ""
    if len(digits) > 11 and digits.startswith("86"):
        digits = digits[2:]
    if len(digits) > 7 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def _sameContact(row: dict, contact: dict) -> tuple:
    """库里的这个人是不是这位联系人的另一个副本？-> (命中方式, 是否可信)

    可信度分级很重要：**同号不同名**在国内极常见（一家人共用一个座机），
    把这种情况当「同一人」跳掉会**丢掉一个真实的人**；反过来当成两个人
    导入也只是多了一个人，不会毁数据。所以：
      · 邮箱相同 / 号码相同且姓名相同 -> 可信，跳过
      · 仅号码相同（姓名不同）-> **只警告，照常导入**
    """
    if not row:
        return "", False
    if str(row.get("personCode") or "") == contact.get("targetCode"):
        return "personCode", True
    mail = str(contact.get("email") or "").lower()
    if mail and str(row.get("email") or "").lower() == mail:
        return "email", True
    newKey = phoneKey(contact.get("phone"))
    oldKey = phoneKey(row.get("phone"))
    if newKey and newKey == oldKey:
        sameName = str(row.get("displayName") or "").strip().lower() == \
            str(contact.get("displayName") or "").strip().lower()
        return ("phone+name" if sameName else "phone"), bool(sameName)
    return "", False


def loadIndex() -> dict:
    """一次性把 pb_person 读进内存建索引。

    ⚠️ 为什么不按 vcardUid / phone 走生成层的 query 参数：
       生成层没有这两个查询参数，而为一次导入去改生成器+ 全库迁移
       代价与收益不成比例（人名册是**小表**，全读一次完全可接受）。
    """
    byUid, byPhone, byMail, byName, codes = {}, {}, {}, {}, set()
    for row in sqliteCommon.query_pb_person("pb_person", mode="light",
                                           delFlag="*"):
        code = str(row.get("personCode") or "")
        if not code:
            continue
        codes.add(code)
        uid = str(row.get("vcardUid") or "")
        if uid:
            byUid[uid] = row
        name = str(row.get("displayName") or "")
        if name:
            byName[name] = row
        for key in (phoneKey(row.get("phone")),):
            if key:
                byPhone.setdefault(key, row)
        mail = str(row.get("email") or "")
        if mail:
            byMail.setdefault(mail.lower(), row)
    return {"byUid": byUid, "byPhone": byPhone, "byMail": byMail,
            "byName": byName, "codes": codes}


def makeCode(contact: dict, index: dict) -> str:
    """personCode = VC_ + 净化后的 vcardUid（超长截断 + 冲突加序号）。

    人名册编码要**可读**：出问题时在日志/截图里一眼能看出是谁，
    所以用姓名+号码前缀而不是随机串。
    """
    base = contact["vcardUid"]
    if base.startswith(_UID_FILE_PREFIX):
        base = base[len(_UID_FILE_PREFIX):]
    stem = re.sub(r"[^0-9A-Za-z]+", "_", base).strip("_")
    if not stem:
        stem = "c%s" % abs(hash(contact["displayName"])) % 100000
    stem = stem[:56]
    code = _CODE_PREFIX + stem
    n = 2
    while code in index["codes"] and \
            index["byUid"].get(contact["vcardUid"], {}).get("personCode") != code:
        code = "%s%s%d" % (_CODE_PREFIX, stem, n)
        n += 1
    return code


def makeName(contact: dict, index: dict, taken: set) -> str:
    """displayName 是 UNIQUE 键：撞了就加序号，**绝不覆盖别人**"""
    name = contact["displayName"][:128]
    if name not in index["byName"] and name not in taken:
        return name
    n = 2
    # ⚠️ 两个来源都要查：库里已占用的（byName）与本批已分配的（taken）。
    #    只查taken 会撞上「库里已有『Qing Bai』，本批第 2 个也叫『Qing Bai』」
    #    而给出同名的第 2 份 —— 于是第 3 个直接撞 UNIQUE 约束整批失败。
    while ("%s(%d)" % (name, n))[:128] in index["byName"] or \
            ("%s(%d)" % (name, n))[:128] in taken:
        n += 1
    return ("%s(%d)" % (name, n))[:128]


# ============================================================
# 三、plan（只算不写） / apply（落库）
# ============================================================

def _planOne(fileName: str, contact: dict, index: dict, actions: list,
             warnings: list, taken: set, mergeSamePhone: bool) -> None:
    """给**一个**联系人定动作，并把结果登记回index（供同批后续联系人比对）"""
    if contact.get("photo"):
        # 头像**能**导，但要真的落盘才算数；解不开/写失败时由apply() 记 failed。
        # ⚠️ 这里刻意**不逐卡发警告**：本机真实通讯录里有 1012 张带头像的卡，
        # 逐卡一条就是 1012 行一模一样的刷屏，把真正的警告全淹掉。
        # 汇总在 plan() 末尾出。
        pass
    existing = index["byUid"].get(contact["vcardUid"])
    if existing:
        contact["targetCode"] = str(existing.get("personCode") or "")
        # ⚠️ 命中 vcardUid 时**沿用库里那个名字**，绝不能走makeName ——
        #    那会给老档案改名叫「XXX(2)」，而用户从没要求过改名。
        contact["name"] = str(existing.get("displayName") or
                              contact["displayName"])
        hit, trusted = "vcardUid", True
    else:
        contact["targetCode"] = makeCode(contact, index)
        hit, trusted = "", False
        dupRow = {}
        if contact.get("email"):
            dupRow = index["byMail"].get(contact["email"].lower()) or {}
        if not dupRow and contact.get("phone"):
            dupRow = index["byPhone"].get(phoneKey(contact["phone"])) or {}
        if contact.get("email") or contact.get("phone"):
            hit, trusted = _sameContact(dupRow, contact)
        if hit and not trusted:
            # 同号不同名：一家人共用座机在国内极常见，**只警告、照常导入**
            warnings.append(
                "%s（%s，%s）与库里 %s（%s）号码相同但姓名不同 ->"
                " 按**不同的人**导入（共用座机很常见，别自动合并）"
                % (fileName, contact["displayName"], contact.get("phone"),
                   str(dupRow.get("personCode") or "?"),
                   str(dupRow.get("displayName") or "?")))
        elif hit and not mergeSamePhone:
            warnings.append(
                "%s（%s）的%s 与库里 %s（%s）相同但 vcardUid 不同 ->"
                " **疑似同一人，已跳过**（要合并请加 --merge-same-phone）"
                % (fileName, contact["displayName"], hit,
                   str(dupRow.get("personCode") or "?"),
                   str(dupRow.get("displayName") or "?")))
            return
        contact["name"] = makeName(contact, index, taken)
    taken.add(contact["name"])
    fill = []
    if existing:
        for field, label in (("birthday", "生日"), ("phone", "电话"),
                             ("email", "邮箱"), ("familyName", "姓")):
            if contact.get(field) and not str(existing.get(field) or ""):
                fill.append(label)
    actions.append({"contact": contact,
                    "op": ("update" if existing else "create"),
                    "hit": hit, "fill": fill})
    # 立刻登记进索引：**同一批文件里**的两个联系人撞了电话/邮箱/姓名时
    # 也必须当成撞车处理。只在开头读一次库是不够的 ——
    # 那等于「同一个 vcf 目录里塞了两份同一个人的名片」会建出两个人。
    index["codes"].add(contact["targetCode"])
    index["byUid"][contact["vcardUid"]] = {
        "personCode": contact["targetCode"], "displayName": contact["name"],
        "phone": contact.get("phone"), "email": contact.get("email"),
        "birthday": contact.get("birthday"), "vcardUid": contact["vcardUid"]}
    index["byName"][contact["name"]] = index["byUid"][contact["vcardUid"]]
    if contact.get("phone"):
        index["byPhone"].setdefault(phoneKey(contact["phone"]),
                                    index["byUid"][contact["vcardUid"]])
    if contact.get("email"):
        index["byMail"].setdefault(contact["email"].lower(),
                                   index["byUid"][contact["vcardUid"]])


def plan(root: str, dbFile: str = None, mergeSamePhone: bool = False) -> dict:
    """扫一遍通讯录，算出每个联系人「新建/更新/跳过」。**不写任何一行。**"""
    if dbFile:
        sqliteCommon.dbHandle(dbFile)             # DR-10：必须显式才切库
    index = loadIndex()
    taken = set()
    actions, warnings = [], []
    files = sorted(f for f in os.listdir(root)
                   if os.path.splitext(f)[1] in _VCARD_EXT)
    for fileName in files:
        try:
            contacts = readContacts(os.path.join(root, fileName))
        except (OSError, ValueError) as e:
            warnings.append("%s 读取失败：%s" % (fileName, e))
            continue
        if not contacts:
            warnings.append("%s 里没有可用的 vCard（缺 FN/N）" % fileName)
            continue
        for contact in contacts:
            _planOne(fileName, contact, index, actions, warnings, taken,
                     mergeSamePhone)
    # 头像数量汇总（不逐卡发警告）
    # ⚠️ 真实通讯录里有 1012 张带头像的卡；逐卡一条就是 1012 行
    # 一模一样的刹达，把真正值得看的警告（同号名人、只有 EMAIL
    # 的卡、解不出的卡）全部淹掉。
    withPhoto = sum(1 for item in actions if item["contact"].get("photo"))
    if withPhoto:
        warnings.append("共 %d 个联系人带 PHOTO 头像（已落盘到"
                        " <thumb>\\%s\\，它不属于任何一张照片）"
                        % (withPhoto, basicSettings.VCARD_SUBDIR))
    return {"files": len(files), "actions": actions, "warnings": warnings,
            "dbFile": sqliteCommon.dbFilePath()}


def _memoOf(contact: dict) -> str:
    """memo 里留来源痕迹：哪个文件导的、BDAY 原值、缺 UID 的事实"""
    bits = ["vCard:%s" % contact["file"]]
    if contact["uidFromFile"]:
        bits.append("无UID(用文件名当幂等键)")
    if contact["bdayRaw"] and not contact["birthday"]:
        bits.append("BDAY=%s(无年份)" % contact["bdayRaw"])
    return "; ".join(bits)[:200]


#: item["fill"] 里的中文标签 -> pb_person 列名。
#: 二者必须一一对应，_planOne 算fill 时用的就是左边这套标签。
_FILL_FIELD: dict = {"生日": "birthday", "电话": "phone",
                     "邮箱": "email", "姓": "familyName"}


def _rowOfCreate(contact: dict, avatarFile: str = "") -> tuple:
    """新建一行的 (row, updateColumns)。**所有列都写**，缺值写 NULL。"""
    row = {"personCode": contact["targetCode"], "displayName": contact["name"],
           "familyName": (contact["familyName"] or None),
           "phone": (contact["phone"] or None),
           "email": (contact["email"] or None),
           "birthday": (contact["birthday"] or None),
           "vcardUid": contact["vcardUid"],
           "source": comGD.PERSON_SOURCE_IMPORT,
           # 批量导入**不等于**人工确认过：这一列的语义是「用户核对过这个档案」
           "isConfirmed": 0,
           "memo": _memoOf(contact),
           "modifyYMDHMS": misc.getTime()}
    if avatarFile:
        row["avatarFile"] = avatarFile
    # 拼音检索串（服务端派生，pinyin.personPinyin 是唯一算法）
    row["displayNamePinyin"] = pinyin.personPinyin(
        contact["name"], contact.get("familyName")) or None
    return row, ("displayName", "familyName", "phone", "email", "birthday",
                 "displayNamePinyin", "vcardUid", "source", "memo",
                 "avatarFile", "modifyYMDHMS")


def _rowOfUpdate(contact: dict, item: dict, avatarFile: str = "") -> tuple:
    """更新一行 -> (row, updateColumns)；**没有可补的列就返回 (None, ())**。

    ⚠️⚠️ 这里绝不能用 forceColumns（修 BUG-2 的要点）
    ----------------------------------------------
      forceColumns 的语义是「把该列**显式写成 NULL**」（步骤 3 用它把
      shotYear 2023 清成 NULL 用）。而 update 的语义正好相反：**只补空**。
      两者凑在一起 = 「拿空值覆盖用户手工填的内容」，是数据倒退。
      实测（修好返回值判断之后）：一次 update 就能把
      phone / email / birthday / familyName 全部抹成 NULL。
    ⇒ 所以这里**只放真要补的列**，一个不多。normalizeDataSet 会把
      None/"" 整条丢掉，所以「没放的列」根本不会出现在 SQL 里。

      同理不能碰的列：
        · displayName —— 改名是用户的事，导入只负责「首次命名」
        · isConfirmed —— 用户核对过的事实，导入无权撤销（DR-16）
        · memo        —— 里面有用户自己写的内容
        · source      —— 已导入过的人不该被再标一次导入来源
      头像 avatarFile 是**例外**：它由 vCard 独家决定、用户改不了，
      所以新头像应当**直接覆盖**旧头像（用户换了头像就该跟着变）。
    """
    row = {"personCode": contact["targetCode"],
           "modifyYMDHMS": misc.getTime()}
    # ⚠️⚠️ displayName 必须出现在 INSERT 列清单里，但**不进 updateColumns**
    # ------------------------------------------------------------
    #   pb_person.displayName 是 `VARCHAR(128) NOT NULL` **且没有 DEFAULT**。
    #   而生成层的写入形式是 `INSERT ... ON CONFLICT(personCode) DO UPDATE SET ...`
    #   —— SQLite 是先把这一行当成**插入**去满足 NOT NULL，再决定走不走
    #   冲突处理。所以一个只带 personCode+phone 的行**无法通过**，
    #   即便这个 personCode 已经存在、冲突分支根本不会执行。
    #   错误表现：NOT NULL constraint failed: pb_person.displayName
    #   且它是**隐形**的（不报错、不影响其他人），只会让整批
    #   更新静静失败。
    #   → 解法：把 displayName 放进 **INSERT 列清单**作为占位（满足 NOT NULL），
    #     但**不放进 updateColumns**—— 因此 DO UPDATE 不会动它，
    #     库里的旧名字保持不变（归属于用户，不是导入的事）。
    #     而 contact["name"] 在按uid 命中时已经被 _planOne 改成了库里那个名字，
    #     所以即使本来确实误写了 displayName，它也只是写个同值。
    row["displayName"] = contact["name"]
    for label in (item.get("fill") or ()):
        field = _FILL_FIELD.get(label)
        if field and contact.get(field):
            row[field] = contact[field]
    # ⚠️ 只有**这次真的要补姓氏**才重算拼音：
    #    名字沿用库里的（见上面 displayName 的注释），拼音也就原样正确；
    #    而这里拿不到库里那一份 familyName（index 里的行未必带这列），
    #    硬算就会把库里已有的姓 token 抹掉 —— 那是**把对的改成错的**。
    if row.get("familyName"):
        row["displayNamePinyin"] = pinyin.personPinyin(
            contact["name"], row["familyName"]) or None
    if avatarFile:
        row["avatarFile"] = avatarFile
    columns = tuple(k for k in row if k != "personCode")
    if not columns:
        return None, ()
    return row, columns


def apply(planned: dict) -> dict:
    """按 plan 落库。返回统计 {created, updated, unchanged, failed, avatars}。"""
    created, updated, unchanged, failed, avatars = [], [], [], [], []
    for item in planned["actions"]:
        contact = item["contact"]
        code = contact["targetCode"]
        avatarFile = ""
        photo = contact.get("photo")
        if photo:
            try:
                avatarFile = thumbStore.write_vcard_avatar(code, photo)
                avatars.append((code, len(photo)))
            except (thumbStore.ThumbStoreError, OSError) as e:
                #头像写不下去**不能**让人整个导入失败：头像是附赠品，
                # 人名册才是主线。记一条失败、继续导。
                failed.append((code, "头像写入失败：%s" % e))
                _LOG.error("头像写入失败 %s：%s", code, e)

        if item["op"] == "create":
            row, columns = _rowOfCreate(contact, avatarFile)
            force = ("familyName", "phone", "email", "birthday", "memo")
        else:
            row, columns = _rowOfUpdate(contact, item, avatarFile)
            force = ()
        if row is None:
            # 库里的值已经是对的，一个字节都不用改。
            # 仍然记成 updated ——「匹配到已有的人」是这次导入的结论，
            # 只不过结论是「不需要写」。别让调用方以为漏处理了谁。
            updated.append(code)
            continue

        # ⚠️⚠️ BUG-1：insertManyTableGeneral 返回的是 **(rtn, columnNames)**。
        #    原来这里写成 `rtn = ...` 没解包，于是 `rtn == -2` 永远不成立 ——
        #    SQLite 报「NOT NULL constraint failed」也照样 updated.append()，
        #    对外显示「更新成功」而库里一条没变。
        #    （对照 processor/review/assigner._patchFace：它是正确解包的。）
        rtn, _cols = sqliteCommon.insertManyTableGeneral(
            "pb_person", [row], conflictColumns=("personCode",),
            updateColumns=columns, fillStandard=True, forceColumns=force)
        if not isinstance(rtn, int):
            # 形状变了要立刻炸：静默当成成功正是这个 bug 当初能活下来的原因。
            raise AssignerError(
                "insertManyTableGeneral 返回的不是整数而是 %r —— 生成层接口"
                "变了？（正常应返回 (rtn, columns)，rtn 是 int）" % (rtn,))
        if rtn == -2:                    # sqliteHandle.RET_ERROR
            failed.append((code, sqliteCommon.dbHandle().lastErrMsg))
            _LOG.error("导入 %s 失败：%s", code, sqliteCommon.dbHandle().lastErrMsg)
            continue
        (created if item["op"] == "create" else updated).append(code)
        _LOG.info("%s %s（%s）生日=%s 电话=%s%s", item["op"], code,
                  contact["displayName"], contact["birthday"] or "无",
                  contact["phone"] or "无",
                  (" 头像 %d 字节" % len(photo)) if photo else "")
    return {"created": created, "updated": updated, "unchanged": unchanged,
            "failed": failed, "avatars": avatars}


# ============================================================
# 四、main
# ============================================================

def _report(planned: dict) -> dict:
    """打印计划（--dry-run 与实跑共用，保证两次看到的是同一份东西）"""
    creates = [a for a in planned["actions"] if a["op"] == "create"]
    updates = [a for a in planned["actions"] if a["op"] == "update"]
    withYear = [a for a in planned["actions"] if a["contact"]["hasBirthYear"]]
    noYear = [a for a in planned["actions"] if not a["contact"]["hasBirthYear"]]
    print("通讯录        : %s（%d 个 .vcf）"
          % (planned.get("root", ""), planned["files"]))
    print("库: %s" % planned["dbFile"])
    print("将要新建: %d    将要更新: %d" % (len(creates), len(updates)))
    for item in planned["actions"]:
        c = item["contact"]
        print("  %-6s %-28s %-22s生日=%-10s 电话=%s"
              % ("新建" if item["op"] == "create" else "更新",
                 c["targetCode"], c["displayName"],
                 c["birthday"] or ("%s(无年份)" % c["bdayRaw"] or "无"),
                 c["phone"] or "无"))
        if item["op"] == "update" and item["fill"]:
            print("        只补空字段：%s（已有值不覆盖）" % "、".join(item["fill"]))
    print("有出生年: %d 位（走自适应分桶）  无出生年: %d 位（走等宽 5 年降级）"
          % (len(withYear), len(noYear)))
    if noYear:
        print("  ⚠ 无出生年的档案跨年代识别会明显变差，建议手工补生日：")
        for item in planned["actions"]:
            if not item["contact"]["hasBirthYear"]:
                print("     %s %s（BDAY=%s）"
                      % (item["contact"]["targetCode"],
                         item["contact"]["displayName"],
                         item["contact"]["bdayRaw"] or "无"))
    if planned["warnings"]:
        print("提示%d 条：" % len(planned["warnings"]))
        for text in planned["warnings"]:
            print("  · %s" % text)
    return {"create": len(creates), "update": len(updates),
            "noYear": len(noYear)}


def _rebucketImported(result: dict) -> dict:
    """落库之后：对**本次新建/更新的人**刷 shotBucket + 重算质心。

    为什么这是必须的（DR-22 / R2）
    --------------------------
      本工具是「生日进库」的主入口（文件头的「生日口径」一节就是在说这件事），
      而 pb_person.birthday 直接决定桶键：
        有出生年 -> 自适应分桶（0~18 岁每 3 年 / 18+ 每 10 年）
        无出生年 -> 等宽 5 年降级
      生日到位的那一刻，就是这些人的脸该刷成自适应桶的时刻。漏掉的后果与
      DR-20 同类（只是范围局限在本次导入的人）：脸表留等宽桶 -> 质心按等宽桶建
      -> 这批人跨年代认不出来，**且不报错**。

    只处理本次导入的人
      全库刷桶是 `tools/rebucket_cli.py --all` 的活（改存量口径的一次性动作），
      不该由一次导入顺带扫全库脸表。代价与导入规模成正比。

    顺序：刷桶 -> 重算质心，不可颠倒（DR-22）
    失败不炸导入：主目标是「把人建进来」，记 failed 让用户看见即可
      （与上面 apply() 里头像写入失败的处置口径一致）。
    """
    out = {"persons": 0, "faces": 0, "changed": 0, "recomputed": 0, "failed": []}
    codes = list(result.get("created") or ()) + list(result.get("updated") or ())
    if not codes:
        return out
    from engine.match import centroid as centroid
    from engine.match import rebucket as rebucket
    for code in codes:
        try:
            info = rebucket.rebucketPerson(code)
            if info.get("error"):
                continue
            out["persons"] += 1
            out["faces"] += int(info.get("faces") or 0)
            out["changed"] += int(info.get("changed") or 0)
            stat = centroid.recomputePerson(code)
            out["recomputed"] += len(stat.get("buckets") or ())
        except (rebucket.BucketStaleError, RuntimeError, ValueError) as e:
            out["failed"].append((code, str(e)))
            _LOG.error("导入后刷桶/重算失败 %s：%s", code, e)
    return out


def main(argv=None) -> int:
    _fixConsole()
    p = argparse.ArgumentParser(description="通讯录（vCard）导入 pb_person")
    p.add_argument("--root", default="", help="vcf 所在目录")
    p.add_argument("--db", default="", help="目标库（默认正式库；实验请指临时库）")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    p.add_argument("--yes", action="store_true", help="确认落库")
    p.add_argument("--merge-same-phone", action="store_true",
                   help="手机号/邮箱撞上已有的人时按同一人合并（默认跳过）")
    args = p.parse_args(argv)
    if not args.root:
        p.print_help()
        return 2
    if not args.dry_run and not args.yes:
        print("必须显式给 --dry-run 或 --yes（本工具会写正式库）。")
        return 2
    if not os.path.isdir(args.root):
        print("目录不存在：%s" % args.root)
        return 2

    print("import_contacts.py _VERSION:", _VERSION)
    planned = plan(os.path.abspath(args.root), args.db or None,
                   args.merge_same_phone)
    planned["root"] = os.path.abspath(args.root)
    counts = _report(planned)
    if args.dry_run:
        print("\n[dry-run] 未写任何一行。确认无误后加 --yes 实跑。")
        return 0
    result = apply(planned)
    print("\n实跑完成：新建 %d、更新 %d、失败 %d"
          % (len(result["created"]), len(result["updated"]),
             len(result["failed"])))
    # ---- 生日到位 -> 刷桶 + 重算质心（DR-22 / R2）----
    # ⚠️ 必须在落库**之后**：桶键取决于 pb_person.birthday，
    #    而生日是上面 apply() 刚写进去的。顺序反了就是僵尸质心。
    reb = _rebucketImported(result)
    if reb["persons"]:
        print("刷桶/重算    : %d 人、%d 张脸，其中 %d 张改桶，重算 %d 个桶质心"
              % (reb["persons"], reb["faces"], reb["changed"],
                 reb["recomputed"]))
        if reb["changed"]:
            print("              （桶键变了：这些人有生日，脸表已从等宽降级桶"
                  "刷成自适应桶）")
        else:
            print("              （这些人的脸桶键已是对的，无需改动）")
    for code, err in reb["failed"]:
        print("  !刷桶/重算 %s -> %s" % (code, err))
    for code in result["created"]:
        print("  + %s" % code)
    for code in result["updated"]:
        print("  ~ %s" % code)
    for code, err in result["failed"]:
        print("  ! %s -> %s" % (code, err))
    total = sqliteCommon.countTableGeneral("pb_person", delFlag="*")
    print("pb_person 现共 %d 人（含软删）。" % total)
    if counts["noYear"]:
        print("⚠ 有 %d 位没出生年 -> 分桶降级为等宽 5 年，跨年代识别会变差。"
              % counts["noYear"])
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())





