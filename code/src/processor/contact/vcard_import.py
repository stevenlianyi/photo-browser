#! /usr/bin/env python3
#encoding: utf-8

#Filename: vcard_import.py
#Description: photo-browser 联系人导入 · vCard 通道（3.0 / 4.0 -> pb_person + pb_family）
#
# 为什么要有两个解析器（vobject 不是万能的）
# ------------------------------------------
#   规范上 vobject 是首选（RFC 6350/2426都它扛）。但实测本机 20 个真实 .vcf：
#     16 个 vobject 正常；**4 个直接抛异常** ——
#       3 个 UnicodeDecodeError：这些卡是 2.1 + QUOTED-PRINTABLE，且折行用的是
#          标准 QP 软换行「=E8=8B=\n=B1」（行尾一个 `=`，续行**没有**前导空格）。
#          vobject 是**逐行**解码的，于是每行都留一个残缺的 `=xx` -> UnicodeDecodeError；
#       1 个 ParseError：续行以 `=38=E5=8F=...` 开头，同样是逐行解码切断了转义序列。
#   这类文件在 Outlook/安卓手机导出的通讯录里非常常见。**丢掉 20% 的人不能接受**，
#   所以：vobject 优先，**抛异常就退回自带的行解析器**（先拼折行再整体 QP 解码，
#   顺序反了就会把 B1 的 '=' 吃掉）。用哪个解析器在报告里如实写出来。
#
# 家庭组（KIND:group）
# ------------------
#   vCard 4.0：KIND:group + MEMBER（urn:uuid:... / mailto: / tel:）
#   vCard 3.0：X-ADDRESSBOOKSERVER-KIND;X-ADDRESSBOOKSERVER-ISGROUP:TRUE + X-ADDRESSBOOKSERVER-MEMBER
#   **显式**声明的组 -> pb_family，并把成员的 familyGroupCode 指过去。
#   同姓多人**不在这里建组**（见 contactCommon.suggestFamilies，只提示）。
#
# 硬约束
# ------
#   * 只建档案/家庭组，**绝不关联照片**（不写 pb_face / pb_photo / pb_photo_person）。
#   * 不导 PHOTO 头像：pb_face.photoCode 是 NOT NULL 外键，头像不属于任何 pb_photo 行；
#     且通讯录头像多是旧照/证件照，拿它当质心样本会引入偏斜分布。只统计并报告。
#   * Outlook 只认**单联系人 .vcf**，所以一个人一个文件是常态 —— 本模块按目录批量读。
#
# 用法
# ----
#   python code\src\main\cli.py import-vcard --path D:\temp\contacts --dry-run
#   python code\src\main\cli.py import-vcard --path D:\temp\contacts\0154_Qing\ Bai.vcf

import os
import quopri
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

_LOG = misc.setLogNew("vcardImport", "vcardimport.log")

#: TEL 参数类型优先级：手机号最可能是「本人常用号码」
TEL_PREF: tuple = ("CELL", "MOBILE", "MAIN", "WORK", "HOME", "VOICE")
#: 邮箱参数类型优先级（WORK 在前：工作邮箱往往更稳定）
MAIL_PREF: tuple = ("WORK", "HOME", "INTERNET", "OTHER")

#: vCard 2.1 里中文卡常见的组标记（拼写错的那个变体在真实数据里也出现过）
_GROUP_KIND_PROPS: tuple = ("x-addressbookserver-kind", "x-adressbookserver-kind",
                            "group")
_MEMBER_PROPS: tuple = ("member", "x-addressbookserver-member",
                       "x-adressbookserver-member")
_RELATED_TYPES: tuple = comGD.RELATION_ALL + ("associate", "friend", "agent")


class VCardImportError(contact.ContactImportError):
    """vCard 特有的前置错误（文件读不了 / 一个卡都解析不出来）"""


# ============================================================
# 一、折行与值解码（自带行解析器）
# ============================================================

def unfoldLines(text: str) -> list:
    """还原两种折行，返回不带折行的行列表。

    ① QP 软换行（RFC 2045）：行尾一个 `=`，续行**以 `=` 开头**（后面跟 `XX` 转义）
       或该属性显式声明了 `ENCODING=QUOTED-PRINTABLE`。
    ② 行折行（RFC 6350）：续行以空格或 TAB 开头，续行的第一个字符要**去掉**。

    ⚠️⚠️ 绝不能只看「上一行以 = 结尾」就当成 QP 软换行（本步修的真bug）
    -------------------------------------------------------
      **base64 的行尾padding 恰好也长成 `=`**。真实通讯录里 1012 张带头像的卡
      几乎每张的 PHOTO 行都以此结尾，于是原来的写法会把**下一张卡的
      `BEGIN:VCARD` 吞进 PHOTO 行里**：

          PHOTO;ENCODING=BASE64;JPEG:/9j/4AAQ...=
          BEGIN:VCARD            <- 被当成 QP 续行接上去了
          ...
          END:VCARD              <- 这一张永远等不到 END

      实测 lianyi-unique.vcf：**2030 张卡只切出 1713 张（丢 317）**，
      317 张的头像接成不可解的垃圾，而**全程不报错**——只是联系人少了 15%。
      判据必须看**续行长什么样**（以 `=` 开头 = QP 的 `=XX` 转义），
      而不是只看上一行结尾。
    ⚠️ 两种折行可以叠加（一行被折了三次），所以要循环到没有折行为止。
    ⚠️ 顺序必须是「先拼折行、再整体 QP 解码」：反过来会把 `=E8=8B=\n=B1`
       解成 `=E8=8BB1`（续行的 '=' 被当成软换行吃掉），字直接坏掉。
    """
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        prev = out[-1] if out else ""
        if prev.endswith("=") and raw[:1] != " ":
            # QP 续行的两种形态：续行以 '=' 开头（=XX 转义），或本行显式声明了 QP
            head = prev[:prev.find(":")] if ":" in prev else ""
            isQp = raw[:1] == "=" or "QUOTED-PRINTABLE" in head.upper()
            if isQp:
                out[-1] = prev[:-1] + raw      # 吃掉上一行行尾的 '='，直接接上
                continue
            # ⚠️ 否则这是一个**新的属性行**（base64 padding / 巧合）——
            #    落到下面正常 append，绝不能把两张卡粘成一张。
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
            continue
        out.append(raw)
    return out


def splitValue(line: str) -> tuple:
    """`TEL;CELL;CHARSET=UTF-8:+8613` -> (("TEL",""),("CELL",""),("CHARSET","UTF-8")), 值

    ⚠️ 传进来的是**整行**（含属性名）。分号只在**参数区**里是分隔符，
       值里也可能有分号（ADR 的街道/城市/邮编就用分号隔），所以逐字符扫并跟踪引号。
    ⚠️ 冒号前的**最后一段**参数也要收进来：漏了它的话
       `ADR;WORK;CHARSET=UTF-8:xxx` 会被解析成只有 ADR/WORK，
       CHARSET/ENCODING 静默消失 -> QP 解码没 charset可用 -> 乱码，且不报错。
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
        params.append("".join(buf))
    keys = []
    for item in params:
        name, _sep, value = item.partition("=")
        keys.append((name.strip().upper(), value.strip().strip('"')))
    return tuple(keys), line[i + 1:]


def decodeValue(params: dict, value: str) -> str:
    """按 CHARSET/ENCODING 解值（QUOTED-PRINTABLE / BASE64 / 其它原样）。"""
    encoding = str(params.get("ENCODING") or "").upper()
    charset = str(params.get("CHARSET") or "").strip() or "utf-8"
    if "QUOTED-PRINTABLE" in encoding:
        try:
            raw = quopri.decodestring(value.encode("utf-8", "surrogateescape"))
        except (ValueError, TypeError):
            return value
        try:
            return raw.decode(charset, "replace")
        except LookupError:
            return raw.decode("utf-8", "replace")
    if "BASE64" in encoding or "B" == encoding:
        import base64
        try:
            return base64.b64decode(value + "===").decode(charset, "replace")
        except (ValueError, TypeError):
            return value
    if encoding:
        try:
            return value.encode("utf-8").decode(charset, "replace")
        except (LookupError, UnicodeDecodeError, UnicodeEncodeError):
            return value
    return value


def collectLines(lines: list) -> dict:
    """一组行 -> {属性名: [(params dict, 已解码的值), ...]}"""
    out = {}
    for line in lines:
        if ":" not in line:
            continue
        keys, rawValue = splitValue(line)
        if not keys or not keys[0][0]:
            continue
        prop = keys[0][0]
        params = dict((name, value) for name, value in keys[1:] if name)
        out.setdefault(prop, []).append((params, decodeValue(params, rawValue)))
    return out


def _pick(cards: dict, prop: str, prefs: tuple = ()) -> str:
    """按参数类型优先级取值；都不匹配就取第一个非空"""
    items = cards.get(prop) or []
    for want in prefs:
        for params, value in items:
            types = [t.upper() for t in
                     str(params.get("TYPE", "")).replace(",", " ").split()]
            if want in types and str(value).strip():
                return str(value).strip()
    for _params, value in items:
        if str(value).strip():
            return str(value).strip()
    return ""


def _nameParts(cards: dict) -> tuple:
    """(显示名, 姓, 名)。FN 优先；没有 FN 就用 N 拼「名 姓」。"""
    full = _pick(cards, "FN")
    family = given = ""
    parts = [p.strip() for p in _pick(cards, "N").split(";")]
    if parts:
        family = parts[0] if parts else ""
        given = parts[1] if len(parts) > 1 else ""
    if not full:
        full = ("%s %s" % (given, family)).strip() if family else given
    return full.strip(), family, given


def _isGroup(cards: dict) -> bool:
    kind = _pick(cards, "KIND").lower()
    if kind in ("group", "org"):
        return True
    for prop in _GROUP_KIND_PROPS:
        if prop in cards:
            value = _pick(cards, prop).upper()
            if value in ("TRUE", "GROUP", "1"):
                return True
    return False


#: MEMBER 行里「引用放在参数里」的参数名（按优先级）
_MEMBER_REF_PARAMS: tuple = ("UID", "X-ADDRESSBOOKSERVER-UID",
                            "EMAIL", "X-ADDRESSBOOKSERVER-EMAIL")


def _paramText(params: dict, name: str) -> str:
    """取一个 vCard 参数的文本值。

    ⚠️ vobject 的 params 值是**列表**：`params["UID"] == ["u-a"]`。
       直接 str() 拿到的是 `"['u-a']"` —— 它既不是个 UID，也不会等于任何
       库里的 vcardUid，所以成员挂接会静默失败（家庭组建了、一个人都没有）。
    """
    val = (params or {}).get(name)
    if isinstance(val, (list, tuple)):
        val = val[0] if val else ""
    return str(val or "").strip()


def _membersFromItems(items) -> list:
    """vobject 的 member 行对象 -> 引用列表（值为空时取参数）。"""
    out = []
    for m in items or ():
        text = str(getattr(m, "value", "") or "").strip()
        if not text:
            params = getattr(m, "params", None) or {}
            for name in _MEMBER_REF_PARAMS:
                text = _paramText(params, name)
                if text:
                    break
        if text and text not in out:
            out.append(text)
    return out


def _members(cards: dict) -> list:
    """组卡片的成员引用列表。

    ⚠️ 引用可能在**值**里，也可能在**参数**里
    --------------------------------------------------
      vCard 3.0（也正是 Outlook / Exchange 导出家庭组时用的）把引用放在参数：

          MEMBER;UID=3a1f...:
          MEMBER;X-ADDRESSBOOKSERVER-EMAIL=zhang@example.com:

      值是**空的**。只读值的实现会让成员数等于 0
      —— 家庭组建了、一个人都不能挂上，而**不会报错**。
      → 值为空时回退到参数。
    """
    # ⚠️ 属性名要大小写不敏感地查
    #   _MEMBER_PROPS 是小写，而 collectLines()/splitValue() 把属性名**统一转大写**
    #   存进 dict（键是 "MEMBER"）。属性名大小写不一致时
    #   cards.get("member") 永远是 None —— 成员列表空的原因在这里。
    out = []
    for prop in _MEMBER_PROPS:
        for params, value in cards.get(prop.upper()) or []:
            text = str(value).strip()
            if not text:
                for name in _MEMBER_REF_PARAMS:
                    text = _paramText(params, name)
                    if text:
                        break
            if text and text not in out:
                out.append(text)
    return out


def _related(cards: dict) -> list:
    out = []
    for params, value in cards.get("RELATED") or []:
        relType = comGD.normalizeRelation(
            str(params.get("TYPE", "")).replace(",", " ").strip().lower())
        out.append({"type": relType, "value": str(value).strip()})
    return out


# ============================================================
# 二、vobject 通道
# ============================================================

def _cardsFromVobject(text: str) -> list:
    """用 vobject 解析 -> 归一化的卡片 dict 列表。

    Raises 任何异常由调用方兜（vobject 遇到非规范文件会抛 UnicodeDecodeError /
    ParseError / ValidateError，**不能**让一份文件里 4 个人被整份丢掉）。
    """
    import vobject

    out = []
    comps = list(vobject.readComponents(text))
    for comp in comps:
        contents = comp.contents

        def one(name, default=None):
            """取第一个该属性的 vobject 行；没有返回 **None**。

            ⚠️ 缺省值必须是 None，不能是 ""。
            原先写的是 default=""，而下面每一处都是 `xItem is not None 且 xItem.value`
            —— 少一个 N 或 BDAY 就走到 `"".value` 上抛 AttributeError，
            而这个异常会被 readCardsText 的 except 全盖掉，
            **整个文件静默回退到行解析器**。
            后果：vobject 路径一直是死的，而现代码里没人发现——
            因为行解析器能跑通，且 parser 字段值很安静地落到它身上。
            """
            items = contents.get(name)
            if not items:
                return default
            return items[0]

        _verItem, _uidItem = one("version"), one("uid")
        version = str(getattr(_verItem, "value", "") or "").strip()
        uid = str(getattr(_uidItem, "value", "") or "").strip()
        fnItem = one("fn")
        fn = str(fnItem.value or "").strip() if fnItem is not None else ""
        nItem = one("n")
        family = given = ""
        if nItem is not None and nItem.value:
            parts = [str(x).strip() for x in list(nItem.value)]
            family = parts[0] if parts else ""
            given = parts[1] if len(parts) > 1 else ""
        if not fn:
            fn = ("%s %s" % (given, family)).strip() if family else given
        telItem = one("tel")
        mailItem = one("email")
        bdayItem = one("bday")
        catItem = one("categories")
        kindItem = one("kind")
        orgItem = one("org")
        titleItem = one("title")
        noteItem = one("note")
        # ⚠️ 不能用 one("photo")：它的缺省值是 **""**（一个 str）而不是 None，
        #    所以 `photoItem is not None` 对**每一张卡** 都成立（hasPhoto 一直是 True），
        #    而对空字符串取 .value 会直接崩掉整个 vobject 路径。
        photoItems = [x for x in (contents.get("photo") or ())
                      if isinstance(getattr(x, "value", None), (bytes, bytearray))]
        photoValue = bytes(photoItems[0].value) if photoItems else b""
        cards = {
            "tel": [({}, str(telItem.value or ""))] if telItem is not None else [],
            "email": [({}, str(mailItem.value or ""))] if mailItem is not None else [],
        }
        related = []
        for item in contents.get("related") or []:
            types = item.params.get("TYPE") or []
            if isinstance(types, str):
                types = [types]
            related.append({"type": comGD.normalizeRelation(
                " ".join(str(t) for t in types).lower()),
                "value": str(item.value or "").strip()})
        categories = []
        if catItem is not None and catItem.value:
            categories = [str(c) for c in (catItem.value
                                           if isinstance(catItem.value, (list, tuple))
                                           else [catItem.value])]
        out.append({
            "version": version,
            "kind": str(kindItem.value or "").lower().strip() if kindItem is not None else "",
            "uid": uid,
            "fn": fn,
            "family": family,
            "given": given,
            "tel": _pick(cards, "tel", TEL_PREF),
            "email": _pick(cards, "email", MAIL_PREF),
            "bday": str(bdayItem.value or "").strip() if bdayItem is not None else "",
            "categories": [c.strip() for c in categories if str(c).strip()],
            "title": str(titleItem.value or "").strip() if titleItem is not None else "",
            "org": str(orgItem.value or "").strip() if orgItem is not None else "",
            "note": str(noteItem.value or "").strip() if noteItem is not None else "",
            # ⚠️ 成员引用可能在**参数**里（vCard 3.0 / Outlook 的形态）：
            #    `MEMBER;UID=xxx:` 的值是空的。只读 .value 会让成员数等于 0。
            "members": _membersFromItems(contents.get("member") or []),
            "related": related,
            "hasPhoto": bool(photoValue),
            # vobject 已把 ENCODING=BASE64 解成 bytes，原样带出（不要再解一遍）
            "photo": photoValue,
        })
    # KIND 判定：vobject 已给出 kind，但**vCard 3.0 的组标记不在 kind 属性里**
    #（是 X-ADDRESSBOOKSERVER-KIND;X-ADDRESSBOOKSERVER-ISGROUP:TRUE，
    #   vobject 会把它放成 x-addressbookserver-kind=TRUE），所以再判一次。
    # 漏判的后果很严重：组卡片会被当成一个人建档（组名就是 FN）。
    for card, comp in zip(out, comps):
        if str(card["kind"]) in ("group", "org"):
            card["isGroup"] = True
            continue
        for prop in _GROUP_KIND_PROPS:
            items = comp.contents.get(prop)
            if items and str(items[0].value or "").strip().upper() in ("TRUE", "GROUP", "1"):
                card["isGroup"] = True
                break
    return out


# ============================================================
# 三、读文件 / 读目录
# ============================================================

def readCardsText(text: str) -> tuple:
    """一段文本 -> (归一化卡片列表, 用了哪个解析器, 错误说明)

    vobject 优先；**抛异常或一张卡都没读出来就退回自带行解析器**。
    """
    try:
        cards = _cardsFromVobject(text)
        if cards:
            for card in cards:
                card.setdefault("isGroup", False)
            return cards, "vobject", ""
    except Exception as e:                # vobject 的异常类型随版本变化，全兜
        errMsg = "%s: %s" % (type(e).__name__, str(e)[:120])
    else:
        errMsg = "vobject 未解析出任何卡片"
    cards = []
    current = None
    for line in unfoldLines(text):
        upper = line.strip().upper()
        if upper.startswith("BEGIN:VCARD"):
            current = []
            continue
        if upper.startswith("END:VCARD"):
            if current is not None:
                cards.append(current)
            current = None
            continue
        if current is not None and line.strip():
            current.append(line)
    out = []
    for lines in cards:
        picked = collectLines(lines)
        rawPhoto = collectRawProp(lines, "PHOTO")
        displayName, family, given = _nameParts(picked)
        version = _pick(picked, "VERSION")
        out.append({
            "version": version,
            "kind": _pick(picked, "KIND").lower(),
            "isGroup": _isGroup(picked),
            "uid": _pick(picked, "UID"),
            "fn": displayName,
            "family": family,
            "given": given,
            "tel": _pick(picked, "TEL", TEL_PREF),
            "email": _pick(picked, "EMAIL", MAIL_PREF),
            "bday": _pick(picked, "BDAY"),
            "categories": [c.strip() for c in _pick(picked, "CATEGORIES").split(",")
                           if c.strip()],
            "title": _pick(picked, "TITLE"),
            "org": _pick(picked, "ORG"),
            "note": _pick(picked, "NOTE"),
            "members": _members(picked),
            "related": _related(picked),
            # ⚠️ hasPhoto 必须看**解出来没有字节**，不能只看
            #    「有没有 PHOTO 这个属性」—— 外链头像（VALUE=URI）
            #    与解不出的 base64 都符合属性存在，但它们**没有字节**。
            "hasPhoto": bool(rawPhoto),
            "photo": rawPhoto,
        })
    return out, "行解析器", errMsg



def photoBytesOf(card: dict) -> bytes:
    """一张卡 -> 头像**字节**（没有/解不开返回 b""）。

    ⚠️ 两条解析路径的取法完全不同，这是本函数存在的唯一理由
    ------------------------------------------------------
      · vobject 路径：vobject 已经把 ENCODING=BASE64 解成了 **bytes**
        （实测 photo.value 是 b'\xff\xd8\xff\xe0\x00\x10JFIF'），
        我们只要原样接着；
      · 行解析器（vobject 不可用时的兜底）：拿到的是**未解码的 base64 串**，
        必须自己解。
    混用这两种取法的后果是「vobject 装了就正常、没装就静默丢头像」——
    而本机装没装 vobject 只是一行 requirements 的差别。
    """
    if not isinstance(card, dict):
        return b""
    got = card.get("photo")
    if isinstance(got, (bytes, bytearray)):
        return bytes(got)
    if isinstance(got, str) and got.strip():
        import base64
        import binascii
        compact = "".join(got.split())
        if compact.lower().startswith("data:"):
            _head, _sep, rest = compact.partition(":")
            compact = rest.split(",", 1)[1] if "," in rest else ""
        compact = compact.rstrip("=")
        compact += "=" * ((-len(compact)) % 4)
        try:
            return base64.b64decode(compact)
        except (binascii.Error, ValueError):
            return b""
    return b""


def collectRawProp(lines: list, prop: str) -> bytes:
    """一组**原始行** -> 指定属性的**未解码**字节值（没有返回 b""）。

    ⚠️ 为什么不能用 collectLines 的结果
    --------------------------
      collectLines 把每个值都过了 decodeValue，而它对 BASE64 的做法是
      `base64.b64decode(...)` 后**再按 charset(=utf-8) 解成 str**。二进制数据按 utf-8 解
      必然乱码，所以它给出来的是一段**不可逆的乱码**。
      → 头像必须回到未解码的原始行自己解。
    """
    wanted = str(prop or "").strip().upper()
    for line in lines or ():
        if ":" not in line:
            continue
        keys, rawValue = splitValue(line)
        if not keys or keys[0][0] != wanted:
            continue
        params = dict((name, value) for name, value in keys[1:] if name)
        if str(params.get("VALUE") or "").strip().upper() == "URI":
            continue                       # 外链头像**不下载**：一次导入
                                             # 1078 个外链就是 1078 次网络请求，还会把
                                             # 通讯录里的 URL 变成一份对外抓取清单
        enc = str(params.get("ENCODING") or "").strip().upper()
        if enc and "BASE64" not in enc and enc != "B":
            continue                       # 不是 base64（外链/明文路径）不解
        got = photoBytesOf({"photo": rawValue})
        if got:
            return got
    return b""


def readFileCards(path: str) -> dict:
    """读一个 .vcf -> {"cards": [...], "parser": str, "warnings": [...]}

    每张卡片都带上来源文件名：Outlook 一个人一个文件，出问题时
    「哪个文件里的哪张卡」是唯一有用的定位信息。
    """
    text, encoding = contact.sniffTextFile(path)
    cards, parser, errMsg = readCardsText(text)
    fileName = os.path.basename(path)
    for card in cards:
        card["file"] = fileName
    warnings = []
    if errMsg:
        warnings.append("%s：vobject 解析失败（%s），已改用行解析器"
                        % (fileName, errMsg))
    if not cards:
        warnings.append("%s 里没有 BEGIN:VCARD/END:VCARD 之间的内容" % path)
    return {"file": fileName, "path": os.path.abspath(path),
            "cards": cards, "parser": parser, "encoding": encoding,
            "warnings": warnings}


def readPath(path: str) -> dict:
    """读一个 .vcf 或一个目录下的全部 .vcf（Outlook 一个联系人一个文件）"""
    target = os.path.abspath(path)
    if os.path.isdir(target):
        files = sorted(os.path.join(target, name)
                       for name in os.listdir(target)
                       if os.path.splitext(name)[1].lower()
                       in basicSettings.IMPORT_VCARD_EXTS)
    else:
        files = [target]
    cards, warnings, filesInfo = [], [], []
    for filePath in files:
        try:
            one = readFileCards(filePath)
        except contact.ContactImportError as e:
            warnings.append(str(e))
            continue
        cards.extend(one["cards"])
        warnings.extend(one["warnings"])
        filesInfo.append({"file": one["file"], "path": one["path"],
                          "parser": one["parser"], "cards": len(one["cards"]),
                          "encoding": one["encoding"]})
    if not files:
        raise VCardImportError(
            "目录里没有 .vcf 文件（支持 %s）: %s"
            % ("/".join(basicSettings.IMPORT_VCARD_EXTS), target))
    if not cards:
        raise VCardImportError("%s 下没解析出任何 vCard（%d 个文件）"
                               % (target, len(files)))
    return {"source": target, "isDir": os.path.isdir(target),
            "files": filesInfo, "cards": cards, "warnings": warnings}


# ============================================================
# 四、plan（只算不写）
# ============================================================

def cardToContact(card: dict, fileName: str, rowNo: int) -> dict:
    """归一化卡片 -> 联系人 dict（与 csv_import 输出同构）"""
    birthday, bdayRaw, hasYear = contact.normalizeBday(card.get("bday"))
    relation = ""
    for item in card.get("related") or []:
        if item.get("type") in comGD.RELATION_ALL:
            relation = item["type"]
            break
    return {
        "displayName": str(card.get("fn") or "").strip(),
        "familyName": str(card.get("family") or "").strip(),
        "relation": relation,
        "email": str(card.get("email") or "").strip(),
        "phone": str(card.get("tel") or "").strip(),
        "birthday": birthday,
        "bdayRaw": bdayRaw,
        "hasBirthYear": hasYear,
        "uid": str(card.get("uid") or "").strip(),
        "uidFromFile": not str(card.get("uid") or "").strip(),
        "categories": list(card.get("categories") or []),
        "categoriesGiven": bool(card.get("categories")),
        "company": str(card.get("org") or "").strip(),
        "title": str(card.get("title") or "").strip(),
        "notes": str(card.get("note") or "").strip(),
        "file": fileName,
        "rowNo": rowNo,
        "sourceKind": "vCard",
        "groupCode": "",
        "hasPhoto": bool(card.get("hasPhoto")),
        # 头像字节（两条解析路径的取法在 photoBytesOf）
        "photo": photoBytesOf(card),
    }


def plan(path: str, dbFile: str = None) -> dict:
    """扫一遍 vCard，算出每个联系人「新建/更新/跳过」与每个组「落哪个家庭组」。

    **不写任何一行。**
    """
    info = readPath(path)
    index = contact.loadPersonIndex(dbFile)
    takenNames, takenCodes = set(), set()
    warnings = list(info["warnings"])
    items, groups, skipped = [], [], []
    prefix = basicSettings.IMPORT_CODE_PREFIX_VCARD

    personCards = [card for card in info["cards"] if not card.get("isGroup")]
    groupCards = [card for card in info["cards"] if card.get("isGroup")]

    # ---- 个人：先全部规划（组成员靠 UID 关联，必须等人先建出来） ----
    for card in personCards:
        one = cardToContact(card, str(card.get("file") or ""), 0)
        if not one["displayName"]:
            skipped.append("%s 有一张卡没有可用姓名（缺 FN/N），已跳过"
                           % one["file"])
            continue
        if one["bdayRaw"] and not one["birthday"]:
            warnings.append(
                "%s 生日「%s」解析不出年月日 -> **不写入 birthday**"
                "（无出生年会静默降级为等宽 5 年分桶）" % (one["file"], one["bdayRaw"]))
        items.append(contact.planContact(one, index, prefix,
                                         takenNames, takenCodes, warnings))

    # ---- 组：显式 KIND:group -> pb_family ----
    byUid, byName = {}, {}
    for item in items:
        uid = str(item["row"].get("vcardUid") or item["contact"].get("uid") or "")
        if uid:
            byUid.setdefault(uid, item)
        byName.setdefault(contact.nameKey(item["displayName"]), item)
    for card in groupCards:
        familyName = str(card.get("fn") or card.get("org") or "").strip()
        if not familyName:
            warnings.append("有一张 KIND:group 卡没有 FN -> 无法作为家庭组，已跳过")
            continue
        familyCode = contact.makePersonCode(
            basicSettings.IMPORT_CODE_PREFIX_FAMILY,
            card.get("uid") or familyName, {"codes": set()}, set())
        members, unresolved = [], []
        for ref in card.get("members") or []:
            item = _resolveMember(ref, byUid, byName, index)
            if item is None or not item.get("personCode"):
                unresolved.append(ref)
                continue
            members.append(item["personCode"])
            item["groupCode"] = familyCode
        if unresolved:
            warnings.append(
                "家庭组「%s」有 %d 个成员在通讯录里找不到对应的人（UID/姓名都对不上）：%s"
                " -> **未挂接**，请人工核对"
                % (familyName, len(unresolved),
                   ", ".join(unresolved[:6])))
        groups.append({"familyCode": familyCode, "familyName": familyName,
                       "notes": str(card.get("note") or "").strip()[:400],
                       "file": str(card.get("file") or ""),
                       "members": members, "unresolved": unresolved})
    warnings.extend(skipped)
    return {"kind": "vcard", "source": info["source"], "info": info,
            "items": items, "groups": groups, "warnings": warnings,
            "dbFile": sqliteCommon.dbFilePath() if items else ""}


def _resolveMember(ref: str, byUid: dict, byName: dict, index: dict):
    """组成员引用 -> 已规划的 item（认不出返回 None，**不猜**）。

    认人的顺序：整串 UID -> urn:uuid: 去掉前缀后的 UID -> 显示名。
    MEMBER 也可能写成 mailto:/tel:（少数导出），这两种直接认不出，
    交给上层报 unresolved —— 宁可报出来，也不要挂错人。
    """
    text = str(ref or "").strip()
    if not text:
        return None
    if text in byUid:
        return byUid[text]
    lowered = text.lower()
    for uid, item in byUid.items():
        if uid.lower() == lowered:
            return item
    bare = re.sub(r"^(urn:uuid:|mailto:|tel:)", "", text, flags=re.IGNORECASE)
    if bare in byUid:
        return byUid[bare]
    for uid, item in byUid.items():
        if uid.lower() == bare.lower():
            return item
    row = index["byUid"].get(text) or index["byUid"].get(bare)
    if row is not None:
        return {"personCode": str(row.get("personCode") or ""),
                "displayName": str(row.get("displayName") or ""), "groupCode": ""}
    key = contact.nameKey(bare)
    if key and key in byName:
        return byName[key]
    if key and key in index["byNameKey"]:
        row = index["byNameKey"][key]
        return {"personCode": str(row.get("personCode") or ""),
                "displayName": str(row.get("displayName") or ""), "groupCode": ""}
    return None


def apply(planned: dict, ownerID: str = "", archive: bool = True,
          batchRows: int = None) -> dict:
    """落库 + 归档原件（目录导入时把整个目录下的 .vcf 都归档一份）"""
    archives = []
    if archive and planned.get("source"):
        targets = [item["path"] for item in planned["info"]["files"]]
        for src in targets:
            got = contact.archiveOriginal(src, "vcard")
            archives.append(got)
            if not got["ok"]:
                planned.setdefault("warnings", []).append(
                    "原件归档失败：%s" % got["errMsg"])
    summary = contact.applyPlan(planned, ownerID=ownerID, batchRows=batchRows)
    okList = [item["path"] for item in archives if item["ok"]]
    failedList = [item["errMsg"] for item in archives if not item["ok"]]
    summary["archive"] = {"ok": bool(okList), "path": okList[0] if okList else "",
                          "paths": okList, "errMsg": "; ".join(failedList)}
    return summary


# ============================================================
# 五、报告
# ============================================================

def report(planned: dict) -> dict:
    """打印计划（--dry-run 与实跑共用）"""
    items = planned["items"]
    creates = [it for it in items if it["op"] == "create"]
    updates = [it for it in items if it["op"] == "update"]
    withYear = [it for it in items if it["contact"].get("hasBirthYear")]
    info = planned["info"]
    print("==== vCard 导入计划 ====")
    print("来源      : %s%s" % (planned["source"], "（目录）" if info["isDir"] else ""))
    print("文件      : %d 个 .vcf，卡片 %d 张（个人 %d / 家庭组 %d）"
          % (len(info["files"]), len(info["cards"]), len(items),
             len(planned["groups"])))
    for item in info["files"][:12]:
        print("    %-40s %s  %d 张" % (item["file"][:40], item["parser"], item["cards"]))
    if len(info["files"]) > 12:
        print("    ...（其余 %d 个文件省略）" % (len(info["files"]) - 12))
    print("解析器    : vobject优先，失败逐个退回行解析器（见下方提示）")
    print("库        : %s" % planned.get("dbFile", ""))
    print("将要新建  : %d    将要更新: %d    有出生年: %d / %d"
          % (len(creates), len(updates), len(withYear), len(items)))
    for item in items[:12]:
        print("  %-6s %-26s %-22s 姓=%-10s 生日=%-10s 分类=%s"
              % ("新建" if item["op"] == "create" else "更新",
                 item["personCode"], item["displayName"],
                 item["contact"].get("familyName") or "-",
                 item["contact"].get("birthday") or
                 (("%s(无年份)" % item["contact"]["bdayRaw"])
                  if item["contact"].get("bdayRaw") else "无"),
                 ",".join(item["categories"]) or "无"))
    if len(items) > 12:
        print("  ...（其余 %d 条省略）" % (len(items) - 12))
    for grp in planned["groups"]:
        print("  家庭组  %-26s %-20s 成员 %d 人%s"
              % (grp["familyCode"], grp["familyName"], len(grp["members"]),
                 ("，**%d 人认不出**" % len(grp["unresolved"]))
                 if grp["unresolved"] else ""))
    return {"create": len(creates), "update": len(updates)}


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
        print("原件归档  : %d 个文件 -> %s"
              % (len(archive.get("paths") or []),
                 os.path.dirname(archive["paths"][0])))
    elif archive.get("errMsg") and archive["errMsg"] != "未归档":
        print("原件归档  : **失败** %s" % archive["errMsg"])
    for code in summary["failed"]:
        print("  ! %s -> %s" % (code[0], code[1]))
    if summary.get("familyUnresolved"):
        print("")
        print("未挂接的组成员 %d 个（通讯录里找不到对应的人）："
              % len(summary["familyUnresolved"]))
        for familyName, code in summary["familyUnresolved"][:12]:
            print("  · %s -> %s" % (familyName, code))
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
    print("")
    print("pb_person 现共 %d 人（含软删）；"
          "pb_person_category 现共 %d 行；pb_family 现共 %d 个。"
          % (sqliteCommon.countTableGeneral("pb_person", delFlag="*"),
             sqliteCommon.countTableGeneral("pb_person_category", delFlag="*"),
             sqliteCommon.countTableGeneral("pb_family", delFlag="*")))


if __name__ == "__main__":
    contact._fixConsole()
    print("vcard_import _VERSION:", _VERSION)
    probe = ("BEGIN:VCARD\nVERSION:2.1\n"
             "N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:%E7=8E=8B;;;\n"
             "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=E7=8E=8B=E8=93=89"
             "\nTEL;CELL:13800000000\nEND:VCARD\n")
    cards, parser, err = readCardsText(probe)
    print("QP 折行解码自检 -> fn=%r parser=%s err=%r"
          % (cards[0]["fn"] if cards else None, parser, err))
