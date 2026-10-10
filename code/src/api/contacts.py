#! /usr/bin/env python3
#encoding: utf-8

#Filename: contacts.py
#Description: photo-browser 联系人维护接口（步骤 9·P1，DR-17/18/19）
#
# 端点
# ----
#   GET    /api/contacts分页 + 筛选（category/familyGroupCode/keyword/delFlag）
#   POST   /api/contacts                界面新建手工档案（source=0、vcardUid 空）
#   PATCH  /api/contacts/{personCode}   **部分字段**更新（birthday 变更 -> 重算质心）
#   GET    /api/contacts/{personCode}/impact  停用影响面预览（**只读，不写一行**）
#   POST   /api/contacts/{personCode}/disable?confirm=true  两段式停用
#   POST   /api/contacts/{personCode}/enable   恢复（响应带「需重新确认人脸」提示）
#   GET    /api/contacts/duplicates     同名 / 疑似同人合并候选
#   POST   /api/contacts/import/csv     上传 .csv（?dryRun=true 只回计划）
#   GET/POST/PATCH /api/families[/{familyCode}]  家庭组维护（P2）
#
# 三条硬约束（本模块的存在就是为了守住它们）
# -------------------------------------------
#   ⚠️ **不提供任何导出接口**（DR-17）
#      没有 /api/contacts/export、没有 vCard 导出、没有聚类导出。
#      理由记录在案：家庭场景 contacts 只有几十~几百条，逐条编辑够用；
#      而导出侧一行代码都没有，两头不对称的往返要额外做列名对齐与回导锚点。
#      **明确放弃的收益**（免得日后当成漏做）：① 批量补全生日/分类只能逐条点；
#      ② 步骤 7 聚出的「未命名人物」不能「导出->Excel 填名->批量导回」；
#      ③ 备份改由步骤 12 的整库拷贝 db+thumb 承担，不依赖导出。
#      ⇒ 本模块**没有任何 GET .../export**，验收第 27 条会去确认 404。
#
#   ⚠️ **不提供 DELETE**（DR-19）
#      只允许停用（delFlag='1'）。硬删 = 制造孤儿 + 这个人的**全部确认工作作废**。
#      一个人下面挂着 pb_face（向量）+ pb_person_centroid（质心）+ pb_photo_person，
#      一个误点清零几小时的确认，代价太大。
#      ⇒ 本模块**没有任何 DELETE 路由**，验收第 28 条会去确认 404/405。
#
#   ⚠️ **写入一律复用 contactCommon / assigner / centroid**
#      `makePersonCode` / `makeDisplayName` / `splitCategories` / `_syncCategories`
#      全部转发，不在这里另写一套编码/命名/分类逻辑 ——
#      两套规则迟早分叉，而分叉之后「为什么导进来的叫张三(2)、界面建的叫张三(3)」
#      就成了一个只能靠读代码回答的问题。
#
# 联系人字段分两类（DR-18）—— 本模块最重要的���条
# ----------------------------------------------
#   **匹配相关**（改了必须 centroid.recomputePerson）：
#       birthday
#     它决定 `bucket.bucketKeyAdaptive()` 的年代档键（0–18 岁 3 年 / 18+ 10 年），
#     一改全套年代档键都变、旧质心全部作废。
#     ⚠️⚠️ **只调 recomputePerson 是不够的** —— 质心是**按 pb_face.shotBucket
#       划分年代档**算出来的，而 shotBucket 是**存���在脸表上**的。所以顺序必须是：
#           ① 改 pb_person.birthday
#              ② rebucket.rebucketPerson()   <- 重刷 pb_face.shotBucket（新年代档键）
#              ③ centroid.recomputePerson()  <- 按新年代档键重建全部质心
#       少做 ② 就是「改生日等于没改」：年代档键根本没变，重算出来的质心
#       和改之前**逐位相同**，而用户的直觉是「他忽然认不准了」。
#       （好在 ③ 自己会先跑 `_assertBucketOrder` 前置检查并抛错 ——
#        顺序反了它宁可拒绝执行，也不让你静默失配。）
#   **纯资料**（改了什么都不用做）：
#       displayName / familyName / familyGroupCode / relation / email / phone
#       pb_person_category
#     它们不参与任何划分年代档与匹配，所以 PATCH 它们**绝不**碰质心
#     （验收第 18 条：改 email 后质心的 modifyYMDHMS 必须一模一样）。

import os
import sys
import uuid

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import browse                                            # noqa: E402
from api import dto                                               # noqa: E402
from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from common import pinyin as pinyin                               # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database import queryCommon as query                         # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from engine.match import centroid as centroid                     # noqa: E402
from engine.match import rebucket as rebucket                     # noqa: E402
from processor.contact import contactCommon as contact            # noqa: E402
from processor.contact import csv_import as csvImport             # noqa: E402
from processor.review import assigner as assigner                 # noqa: E402

from fastapi import APIRouter, File, Query, UploadFile             # noqa: E402
from fastapi.responses import JSONResponse                        # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("apiContacts", "apicontacts.log")

router = APIRouter(tags=["contacts"])

#: 界面新建手工档案用的 personCode 前缀。与 IMPORT_CODE_PREFIX_CSV("CS_") /
#: IMPORT_CODE_PREFIX_VCARD("VC_") 刻意**不同**：
#   编码是**来源标识**。「CS_张三」和「VC_张三」是同一个人的两条记录时，
#   两者需要能被合并识别；而「UI_张三」就是纯手工建的，
#   它的存在意味着「这个人是用户在界面里敲进去的，不是通讯录里读来的」——
#   将来若做通讯录同步，**绝不能**把它当成同步目标覆盖掉。
PERSON_CODE_PREFIX_UI: str = "UI_"

#: 家庭组新建时的编码前缀（与 contactCommon.IMPORT_CODE_PREFIX_FAMILY 一致）
FAMILY_CODE_PREFIX: str = getattr(basicSettings, "IMPORT_CODE_PREFIX_FAMILY", "FM_")


# ============================================================
# 一、内部工具
# ============================================================

def _requirePerson(personCode: str, withDeleted: bool = False) -> dict:
    row = browse.personRow(personCode, withDeleted=withDeleted)
    if not row:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "personCode=%s 在 pb_person 里不存在" % personCode)
    return row


def _assertAvatarFace(person: dict, faceCode, given: bool) -> None:
    """校验「默认头像」的人脸编码（DR-41）。**只读，一行都不写**。

    三种情况
    --------
      · **没给该键** -> 直接返回（PATCH 语义：不给 = 一个都不动）
      · **给了空串 / None** -> 合法（= 清空，卡片与详情回退到代表脸，DR-40）
      · **给了非空** -> 必须存在、未软删，且 `pb_face.personCode == 这个人`

    ⚠️ 为什么「别人的脸」要拦在这里：库里一旦出现「头像指向别人的脸」这种
       矛盾行，**界面上完全看不出来** —— 它照样渲染成一张人脸照片，只是
       那张脸不是你选的那个人。这类错误只能靠写入口挡住。
    ⚠️ 用 404（`CODE_NOT_FOUND`）而不是 400 报「不存在」：与本项目其余
       「编码查不到」同构（dto.CODE_NOT_FOUND 的说明里就列了 faceCode）。
    ⚠️ **不校验 `isConfirmed`**：头像是**展示**不是归属，自动归属（未确认）
       的样本同样可以用 —— 没必要逼用户先去确认那张脸。
    ⚠️ 报错文案里**不写 `**加粗**`**：它会被原样 Toast 出来（本项目的
       message 是**给人看的一句话**，不是 Markdown）。
    """
    if not given:
        return
    code = str(faceCode or "").strip()
    if not code:
        return                                     # 清空：合法
    row = browse.faceRow(code)
    if not row:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "faceCode=%s 在 pb_face 里不存在（或已软删）" % code)
    personCode = str((person or {}).get("personCode") or "")
    owner = str(row.get("personCode") or "")
    if owner != personCode:
        mine = str((person or {}).get("displayName") or personCode)
        theirs = str((browse.personRow(owner, withDeleted=True) or {})
                     .get("displayName") or owner or "（未归属）")
        raise dto.ApiError(
            dto.CODE_PARAM_INVALID,
            "这张脸不属于「%s」（它归属于「%s」）—— 默认头像只能用他自己的人脸样本"
            % (mine, theirs))


def _contactStatsOf(codes: list) -> dict:
    """一批人的 photoCount / faceCount / confirmedFaceCount + 分类集合。

    复用 browse.personStatsOf（同一口径，同一处实现），
    再补上分类集合 —— 分类在 pb_person_category，要单独一次 IN 查询。
    """
    out = browse.personStatsOf(codes)
    if not codes:
        return out
    # ⚠️ 拼的是「IN 列表有几个 %s」，**不是** `% marks` 格式化：
    #   SQL 里还有 delFlag 那个真正的 %s，`%` 运算符会把它一起吃掉
    #   （报错是 "not enough arguments for format string"，离病根很远）。
    marks = ", ".join(["%s"] * len(codes))
    rows = query.selectList(
        "SELECT c.personCode AS personCode, c.category AS category"
        " FROM pb_person_category c WHERE c.personCode IN (" + marks + ")"
        " AND c.delFlag = %s ORDER BY c.personCode, c.category",
        tuple(codes) + (comGD.DEL_FLAG_NO,))
    for row in rows:
        code = str(row.get("personCode") or "")
        if code in out:
            out[code].setdefault("categories", []).append(str(row.get("category") or ""))
    return out


def _conflict409(wantName: str) -> None:
    """displayName 撞 UNIQUE 时抛 409 + 对方信息（DR-18，验收第 20 条）。

    ⚠️ **不写库**：这一段必须在 `updateTableGeneral` 之前做完 ——
      撞 UNIQUE 索引的写会整条失败回滚，所以「先写再说」不会留下脏数据，
      但会让用户看到一条看不懂的 sqlite 错误（`UNIQUE constraint failed`）。
      而 DR-18 明确要求给「已存在「X」，你可能是想合并到 TA？」+ 合并入口：
      **重名极可能就是同一人**（导入时 makeDisplayName 已在加后缀避让，
      所以能在库里撞上重名，几乎必然是用户自己在界面建了第二个）。
    """
    rows = sqliteCommon.query_pb_person("pb_person", displayName=str(wantName),
                                        delFlag="*", limitNum=1)
    if not rows:
        return
    other = rows[0]
    code = str(other.get("personCode") or "")
    stats = browse.personStatsOf([code]).get(code, {})
    raise dto.ApiError(
        dto.CODE_DUPLICATE_DISPLAY_NAME,
        "已存在显示名「%s」（personCode=%s，%d 张照片 / %d 张脸）—— "
        "你可能是想合并到 TA：用 POST /api/review/merge "
        "{fromPersonCode, toPersonCode}"
        % (wantName, code, int(stats.get("photoCount") or 0),
           int(stats.get("faceCount") or 0)),
        extra={"code": dto.CODE_DUPLICATE_DISPLAY_NAME,
               "existing": {"personCode": code,
                            "displayName": str(other.get("displayName") or ""),
                            "photoCount": int(stats.get("photoCount") or 0),
                            "faceCount": int(stats.get("faceCount") or 0),
                            "birthday": other.get("birthday") or None,
                            "mergeEndpoint": "/api/review/merge"}})


def _syncCategoriesOf(personCode: str, categories: list) -> dict:
    """分类增删 —— **转发 contactCommon._syncCategories**，不自己实现。

    为什么转发而不是直接 upsert pb_person_category
    --------------------------------------------
      `_syncCategories` 有一条关键规则：**本次没给 Categories（categoriesGiven=False）
      就一行都不动**，而「给了空列表」才表示清空。
      这个「没填」与「清空」的区分是导入链路的生命线（导出常常缺列，
      拿空值覆盖用户后来手工填的内容是**数据倒退**而且不报错）。
      自己写一遍就等于把这个区分弄丢。

    ⚠️ 它要求的 item 结构与 summary 结构都是**导入链路的形状**
      （op / categoriesGiven / labels / contact，以及 summary 里的
      categoryAdded / categoryRemoved / categoryRevived / categoryRows / warnings）。
      本函数照着搭一份最小骨架 —— 这是「复用」要付的一点装配成本，
      比另写一套 upsert 便宜得多（另写就意味着上面那条规则分叉）。
    """
    summary = {"categoryAdded": 0, "categoryRemoved": 0, "categoryRevived": 0,
               "categoryRows": 0, "warnings": []}
    categories, labels = contact.splitCategories(list(categories or []))
    contact._syncCategories([{"op": "update",                  # 非 skip -> 会处理
                              "categoriesGiven": True,          # 明确给了（空列表也算）
                              "personCode": str(personCode),
                              "categories": categories,
                              "labels": labels,
                              "contact": {"file": "界面编辑"}}], summary)
    # ⚠️ 返���**排好序**的：`splitCategories` 保留用户输入顺序，而
    #   `GET /api/contacts` 那一侧是从库里按 category 排序读出来的。
    #   两边顺序不一致的话，前端做完编辑再刷新，分类标签会「跳一下」——
    #   而用户完全没做什么，久了就会怀疑是数据乱了。
    return dict(summary, categories=sorted(categories))


# ============================================================
# 二、GET /api/contacts
# ============================================================

@router.get("/contacts", summary="联系人列表（分页 + 筛选）")
def listContacts(page: int = Query(default=1, ge=1),
                 size: int = Query(default=dto.DEFAULT_PAGE_SIZE),
                 keyword: str = Query(default=None,
                                      description="姓名 / 拼音 / 邮箱 / 电话 / 备注 模糊匹配"),
                 category: str = Query(default=None, description="分类筛选"),
                 familyGroupCode: str = Query(default=None, description="家庭组筛选"),
                 delFlag: str = Query(default=None,
                                      description="0 未停用（默认）/ 1 已停用 / all 不限"),
                 source: int = Query(default=None,
                                     description="来源筛选：0 手工 / 1 导入 / 2 Graph"),
                 withPhoto: int = Query(default=None,
                                        description="1 只看有照片的人 / 0 只看没照片的")):
    """联系人列表。

    ⚠️ 每条都带 **photoCount / faceCount / confirmedFaceCount**（验收第 16 条）。
      这三个数不是 pb_person 上的列，是从 pb_photo_person / pb_face 聚合出来的，
      而且它们是 UI 判断「这个人值不值得管」的唯一依据：
      photoCount=0 意味着「这个人一张照片都没出现过」——
      要么是通讯录里 imported 但没露过面的人，要么是误导入的陌生人。
    ⚠️ `delFlag` 缺省**只看未停用**（`0`）：停用的人在 UI 上是历史，
      不该和活人混在一个列表里；要看得显式传 `delFlag=1` 或 `all`。
    """
    p, s = dto.clampPage(page, size)
    at = dto.offsetOf(p, s)

    where = []
    values = []
    flag = (delFlag or "").strip()
    if flag.lower() == "all":
        where.append("1 = 1")               # ⚠️ 不要写成 "1 = %s" + 一个值：
        #   那会让 WHERE 串多出一个没有对应值的占位符，toSqliteSQL 的
        #   「占位符个数 vs 参数个数」校验直接抛错（而且报错信息指向
        #   toSqliteSQL，看不出是哪一层拼错的）。
    else:
        if flag and flag not in comGD.DEL_FLAG_ALL:
            raise dto.ApiError(dto.CODE_PARAM_INVALID,
                               "delFlag 只支持 0 / 1 / all，收到 %r" % delFlag)
        where.append("p.delFlag = %s")
        values.append(flag or comGD.DEL_FLAG_NO)
    if familyGroupCode:
        where.append("p.familyGroupCode = %s")
        values.append(str(familyGroupCode))
    if source is not None:
        where.append("p.source = %s")
        values.append(int(source))
    if keyword:
        # displayNamePinyin = 派生拼音检索串（见 common/pinyin.py）：
        # 联系人页的搜索框与人物网格必须一致，否则同一批人在两个页面
        # 一个搜得到、一个搜不到。
        where.append("(p.displayName LIKE %s OR p.displayNamePinyin LIKE %s"
                     " OR p.familyName LIKE %s OR p.email LIKE %s OR p.phone LIKE %s"
                     " OR p.memo LIKE %s)")
        like = "%%%s%%" % str(keyword)
        values.extend([like, like, like, like, like, like])
    if category:
        where.append("EXISTS (SELECT 1 FROM pb_person_category c"
                     " WHERE c.personCode = p.personCode AND c.category = %s"
                     " AND c.delFlag = %s)")
        values.extend([str(category), comGD.DEL_FLAG_NO])
    if withPhoto is not None:
        where.append(("EXISTS (SELECT 1 FROM pb_photo_person pp"
                      " WHERE pp.personCode = p.personCode)" if int(withPhoto)
                      else "NOT EXISTS (SELECT 1 FROM pb_photo_person pp"
                           " WHERE pp.personCode = p.personCode)"))
    cond = " AND ".join(where) if where else "1 = 1"

    total = int(query.selectValue("SELECT COUNT(*) AS rowNum FROM pb_person p WHERE " + cond,
                                  tuple(values)) or 0)
    rows = query.selectList(
        "SELECT p.personCode AS personCode, p.displayName AS displayName,"
        " p.familyName AS familyName, p.familyGroupCode AS familyGroupCode,"
        " p.relation AS relation, p.email AS email, p.phone AS phone,"
        " p.birthday AS birthday, p.vcardUid AS vcardUid,"
        " p.avatarFaceCode AS avatarFaceCode,"
        # 通讯录头像（`personSummary` 用它拼 contactAvatarUrl）：联系人页正是
        # 「导入的人」最集中的地方，漏了它这里就又是一片首字母。
        " p.avatarFile AS avatarFile, p.source AS source,"
        " p.isConfirmed AS isConfirmed, p.delFlag AS delFlag, p.memo AS memo"
        " FROM pb_person p WHERE " + cond +
        " ORDER BY p.displayName ASC, p.personCode ASC LIMIT %s OFFSET %s",
        tuple(values) + (s, at))

    codes = [str(r.get("personCode") or "") for r in rows]
    stats = _contactStatsOf(codes)
    covers = browse.personCoversOf(rows)      # 封面脸，批量解析（DR-40）
    items = []
    for row in rows:
        code = str(row.get("personCode") or "")
        one = stats.get(code) or {}
        summary = browse.personSummary(row, one.get("photoCount", 0),
                                       one.get("faceCount", 0),
                                       one.get("confirmedFaceCount", 0),
                                       one.get("yearLow"), one.get("yearHigh"),
                                       coverFaceCode=covers.get(code))
        summary["vcardUid"] = row.get("vcardUid") or None
        summary["categories"] = one.get("categories") or []
        items.append(summary)
    return dto.pageBody(items, p, s, total)


# ============================================================
# 三、POST /api/contacts（界面新建手工档案）
# ============================================================

@router.post("/contacts", summary="界面新建手工联系人（source=0、vcardUid 空）")
def createContact(body: dto.ContactCreateBody) -> JSONResponse:
    """新建一个**手工档案**。

    ⚠️ `source=0` 且 **`vcardUid` 为空**（验收第 17 条）：
      vcardUid 是「重复导入幂等键」。手工建的人没有 UID，
      如果这里随手编一个（比如用 personCode），将来一次通讯录导入就会
      「认」出这个人并**覆盖**界面上填的字段 —— 而用户根本没意识到
      自己建的人被通讯录里的同名同姓者顶掉了。
      ⇒ 所以宁可让它空着：导入时它只会走 displayName 匹配，
      撞上了就在导入报告里提示「疑似同人，建议合并」，那才是可见的处理方式。

    重名不报错：走 `contactCommon.makeDisplayName()` **加序号避让**
    （`张三` -> `张三(2)`），并在响应的 `warnings` 里说清楚。
      为什么不报 409：那是 PATCH 改名的场景（用户以为自己改的是这一行）；
      新建时用户很可能确实想建第二个人（比如同名兄弟），直接拒绝会逼他
      去改联系人。⚠️ 但**别学 PATCH** 那样报错—— DR-18 说的「重名极可能就是同一人」
      在这里是弱信号（新库建两个同名的人很正常）。
    """
    name = str(body.displayName or "").strip()
    if not name:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "displayName 不能为空")

    index = contact.loadPersonIndex()
    takenNames, takenCodes = set(), set()
    displayName, renamed = contact.makeDisplayName(name, index, takenNames)
    warnings = []
    if renamed:
        warnings.append("显示名「%s」已有同名人员，已记为「%s」"
                        "（displayName 是 UNIQUE 键，**不能覆盖别人**）"
                        % (name, displayName))
    personCode = contact.makePersonCode(PERSON_CODE_PREFIX_UI, name, index, takenCodes)

    familyName = str(body.familyName or "").strip() or None
    groupCode = str(body.familyGroupCode or "").strip()
    if groupCode and not sqliteCommon.query_pb_family("pb_family", familyCode=groupCode,
                                                      limitNum=1):
        # vCard 的 KIND:group 语义：给一个不存在的 familyCode 就建出来
        _insertFamily(groupCode, groupCode, "由联系人「%s」自动创建" % displayName)
        warnings.append("家庭组 %s 不存在，已自动创建" % groupCode)

    dataSet = {
        "personCode": personCode,
        "displayName": displayName,
        "familyName": familyName,
        "familyGroupCode": groupCode or None,
        "relation": str(body.relation or "") or None,
        "email": str(body.email or "") or None,
        "phone": str(body.phone or "") or None,
        "birthday": str(body.birthday or "") or None,
        # 拼音检索串：**服务端派生**，不接受前端传入。
        # ⚠️ 用的是**避让重名后的最终 displayName**，不是用户原填的名字 ——
        #    否则库里是「张三(2)」而拼音是「zhangsan」，
        #    用户搜 zhangsan2 零结果，看着就像搜索坏了。
        "displayNamePinyin": pinyin.personPinyin(displayName, familyName) or None,
        "vcardUid": None,                 # ⚠️ 刻意为空（见函数头）
        "source": comGD.PERSON_SOURCE_MANUAL,
        "isConfirmed": 0,
        "memo": str(body.memo or "") or None,
        "ownerID": basicSettings.DEFAULT_OWNER_ID,
        "regID": basicSettings.DEFAULT_OWNER_ID,
    }
    recID = sqliteCommon.insertManyTableGeneral(
        "pb_person", [dataSet], conflictColumns=("personCode",),
        updateColumns=("displayName", "familyName", "familyGroupCode", "relation",
                       "email", "phone", "birthday", "displayNamePinyin",
                       "vcardUid", "source", "isConfirmed", "memo", "modifyYMDHMS"),
        fillStandard=True,
        forceColumns=("familyName", "familyGroupCode", "relation", "email",
                      "phone", "birthday", "vcardUid", "avatarFaceCode", "memo"))
    if recID == -2:                        # sqliteHandle.RET_ERROR
        raise dto.ApiError(dto.CODE_DB_ERROR,
                           "pb_person 写入失败: %s" % sqliteCommon.dbHandle().lastErrMsg)

    categories, _labels = contact.splitCategories(body.categories or [])
    if categories:
        _syncCategoriesOf(personCode, body.categories or [])
    if str(body.birthday or ""):
        # 新建时年代档键只影响**将来**的归属（此刻他一张脸都没有），
        # 所以这里不需要 rebucket —— 没有脸可刷。
        warnings.append("已填生日：这个人**将来**被认领人脸时会按自适应划分年代档"
                        "（0–18 岁 3 年 / 18+ 10 年）建质心")

    _LOG.info("新建联系人 %s（%s，source=%d）", personCode, displayName,
              comGD.PERSON_SOURCE_MANUAL)
    return JSONResponse(status_code=201,
                        content=dto.okBody(personCode=personCode,
                                           displayName=displayName,
                                           source=comGD.PERSON_SOURCE_MANUAL,
                                           vcardUid=None, categories=categories,
                                           warnings=warnings,
                                           detailUrl="/api/persons/%s" % personCode))


# ============================================================
# 四、PATCH /api/contacts/{personCode}（部分字段更新 · DR-18）
# ============================================================

#: PATCH 允许改的字段 -> pb_person 列。**白名单**：
#:   多写一个键就多一条「前端能改但没想过后果」的路。
#: ⚠️ `avatarFaceCode`（DR-41）是名单里**唯一「值不是自由文本」**的字段：
#:   它必须指向一张**属于这个人**的 `pb_face` 行 —— 所以进白名单的同时，
#:   patchContact 里配了一道 `_assertAvatarFace` 前置校验（写库前查库）。
CONTACT_PATCH_COLUMNS: tuple = ("displayName", "familyName", "familyGroupCode",
                                "relation", "email", "phone", "birthday",
                                "avatarFaceCode", "memo")


@router.patch("/contacts/{personCode}", summary="部分字段更新（birthday 变更自动重算质心）")
def patchContact(personCode: str, body: dto.ContactPatchBody) -> dict:
    """改联系人。**只写传了的字段**（PATCH 语义）。

    分两类处理（DR-18）
    -----------------
      **匹配相关：只有 `birthday`**
        变更时按三步走，**顺序不可颠倒**：
          ① 写 pb_person.birthday
          ② `rebucket.rebucketPerson()` —— 重刷 pb_face.shotBucket
          ③ `centroid.recomputePerson()` —— 按**新年代档键**重建全部质心
        ⚠️ 少做 ② 就是「改生日等于没改」：`centroid.recomputePerson`
           是**按脸表里已存的 shotBucket 划分年代档**的（见 centroid.loadFaceVectors
           的注释与 rebucket.expectedBucketOf），年代档键不刷，重算出来的质心
           与改之前逐位相同 —— 而用户的直觉是「他忽然认不准了」。
           （③ 自己会先跑 `_assertBucketOrder` 前置检查并抛错，
            所以真把顺序做反了，这里会**报错**而不是静默失配。）
        响应带 `centroidRebuilt: true` + 新旧年代档清单 + 受影响的脸数。
      **纯资料：displayName / familyName / familyGroupCode / relation /
        email / phone / 分类 / `avatarFaceCode`**
        改了**什么都不用做** —— 响应里 `centroidRebuilt` 缺席或 false，
        且该人 `pb_person_centroid` 的 `modifyYMDHMS` 一个字都不变
        （验收第 18 条）。

    默认头像（DR-41）
    -----------------
      `avatarFaceCode` 给一个**属于这个人**的 faceCode = 设为默认头像；
      给空串 = 清空（卡片回退到代表脸）。⚠️ 它是**展示**字段：
      不重算质心、不 rebucket、**不写 `pb_review_log`**
      （操作历史回答的是「这张脸当初怎么被认成这个人的」，与头像无关）。
      校验见 `_assertAvatarFace`：**别人的脸一律 400**。

    重名（displayName）
    ------------------
      撞 UNIQUE 时返回 **409 + `{code:"DUPLICATE_DISPLAY_NAME",
      existing:{personCode, displayName, photoCount, faceCount}}`**，**不写库**
      （验收第 20 条）。见 `_conflict409` 的说明：DR-18 认为
      「重名极可能就是同一人」，所以要给合并入口而不是只报错。

    分类
    ----
      `categories` **给键** = 以本次为准（空列表会清空）；
      **不给该键** = 一行都不动。这个区分是刻意的（见 `_syncCategoriesOf`）。
    """
    row = _requirePerson(personCode)
    code = str(row.get("personCode") or "")
    given = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    # ⚠️ 未知字段必须**先于**「没有任何字段要改」报出来：
    #   ContactPatchBody 用 extra="allow"，多出来的键都在 model_extra 里。
    #   如果顺序反了，用户传 {"personCode": "..."} 会得到
    #   「没有任何字段要改」—— 而他明明写了东西，
    #   这个错误信息会把他引到完全错误的方向。
    unknown = sorted(set(body.model_extra or {}) -
                     set(CONTACT_PATCH_COLUMNS) - {"categories"})
    if unknown:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "不支持改这些字段: %s（可改: %s + categories）"
                           % (unknown, list(CONTACT_PATCH_COLUMNS)))
    if not given:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "没有任何字段要改（PATCH 至少要给一个字段）")

    patch = {}
    for column in CONTACT_PATCH_COLUMNS:
        if column in given:
            value = given[column]
            patch[column] = (None if value in ("", None) else str(value))

    # ---- 默认头像预检（**必须在写之前**：见 _assertAvatarFace 的说明）----
    _assertAvatarFace(row, given.get("avatarFaceCode"),
                      "avatarFaceCode" in given)

    # ---- 重名预检（**必须在写之前**：撞 UNIQUE 的写会整条失败）----
    if patch.get("displayName"):
        wantName = str(patch["displayName"])[:128]
        if wantName != str(row.get("displayName") or ""):
            rows = sqliteCommon.query_pb_person(
                "pb_person", displayName=wantName, delFlag="*", limitNum=1)
            if rows and str(rows[0].get("personCode") or "") != code:
                _conflict409(wantName)

    # ---- birthday 是否真的变了（比字符串：'' 与 None 等价，'1985-01-01' != '1985-1-1'）----
    oldBirthday = str(row.get("birthday") or "").strip()
    newBirthday = "" if "birthday" in given else None
    if newBirthday is not None:
        newBirthday = str(patch.get("birthday") or "").strip()
    birthdayChanged = (newBirthday is not None and newBirthday != oldBirthday)

    # ---- 姓名/姓氏变了 -> 拼音必须跟着重算（否则拼音检索会静默失真）----
    # ⚠️ 只能在**写成功之后**再谈「以哪个为准」，而这里算的是补丁后的值：
    #    displayName 走 patch（可能被截到 128 字），familyName 走
    #    patch → 库里旧值 的顺序取第一个非空。
    newName = patch.get("displayName") or row.get("displayName")
    #⚠️ 不能写 `patch.get("familyName") or row.get("familyName")`：
    #    「把姓氏清空」时 patch["familyName"] 是 None，会又捡回旧姓氏，
    #    拼音里就永远留着那个姓的 token —— 搜「wang」还能命中一个已经没有
    #    姓王的人。这里按 key 是否存在来取，而不是按真值。
    newFamily = (patch["familyName"] if "familyName" in patch
                 else row.get("familyName")) or ""
    if patch.get("displayName") or "familyName" in patch:
        patch["displayNamePinyin"] = pinyin.personPinyin(newName, newFamily) or None

    if patch:
        patch["modifyYMDHMS"] = misc.getTime()
        rtn = sqliteCommon.updateTableGeneral(
            "pb_person", "personCode = %s", (code,), patch)
        if rtn == -2:                        # sqliteHandle.RET_ERROR
            raise dto.ApiError(dto.CODE_DB_ERROR,
                               "pb_person 更新失败: %s" % sqliteCommon.dbHandle().lastErrMsg)

    warnings = []
    if "familyGroupCode" in patch:
        groupCode = str(patch.get("familyGroupCode") or "")
        if groupCode and not sqliteCommon.query_pb_family("pb_family",
                                                          familyCode=groupCode,
                                                          limitNum=1):
            _insertFamily(groupCode, groupCode,
                          "由联系人「%s」自动创建" % (patch.get("displayName")
                                                  or row.get("displayName")))
            warnings.append("家庭组 %s 不存在，已自动创建" % groupCode)

    categories = None
    if "categories" in given:
        categories = _syncCategoriesOf(code, given.get("categories") or [])["categories"]

    # ---- 匹配相关副作用：改生日 ->刷新年代档 -> 重算质心 ----
    rebuilt = None
    if birthdayChanged:
        person = _requirePerson(code)          # 重读：拿到新 birthday
        reb = rebucket.rebucketPerson(code)
        stat = centroid.recomputePerson(code)
        rebuilt = {
            "birthdayFrom": oldBirthday or None,
            "birthdayTo": str(person.get("birthday") or "") or None,
            "facesChecked": int(reb.get("faces") or 0),
            "facesRebucketed": int(reb.get("changed") or 0),
            "oldBuckets": reb.get("oldBuckets") or [],
            "newBuckets": reb.get("newBuckets") or [],
            "centroidBuckets": [{"bucketKey": one.get("bucketKey"),
                                 "sampleCount": int(one.get("sampleCount") or 0),
                                 "enabled": bool(one.get("enabled"))}
                                for one in (stat.get("buckets") or [])],
            "centroidEnabled": int(stat.get("enabled") or 0),
            "samples": reb.get("samples") or [],
        }
        _LOG.info("PATCH %s: birthday %s -> %s，刷新年代档 %d/%d 张，质心 %d 年代档（启用 %d）",
                  code, oldBirthday or "(空)", rebuilt["birthdayTo"] or "(空)",
                  rebuilt["facesRebucketed"], rebuilt["facesChecked"],
                  len(rebuilt["centroidBuckets"]), rebuilt["centroidEnabled"])
        if not rebuilt["facesChecked"]:
            warnings.append("这个人还没有任何脸，改生日**当前**不产生质心变化；"
                            "但将来归属人脸时会按新生日划分年代档")

    fresh = _requirePerson(code)
    stats = _contactStatsOf([code]).get(code, {})
    out = browse.personSummary(fresh, stats.get("photoCount", 0),
                               stats.get("faceCount", 0),
                               stats.get("confirmedFaceCount", 0),
                               stats.get("yearLow"), stats.get("yearHigh"),
                               coverFaceCode=browse.personCoversOf([fresh]).get(code))
    out["vcardUid"] = fresh.get("vcardUid") or None
    out["categories"] = stats.get("categories") or browse.categoriesOf(code)
    return dto.okBody(personCode=code, changedFields=sorted(given.keys()),
                      birthdayChanged=bool(birthdayChanged),
                      # ⚠️ 只有真的重算了才是 true；纯资料改动这里是 false
                      centroidRebuilt=bool(rebuilt is not None),
                      centroid=rebuilt, categories=categories,
                      warnings=warnings, contact=out)


# ============================================================
# 五、停用影响面 + 两段式停用 / 恢复（DR-19）
# ============================================================

@router.get("/contacts/{personCode}/impact", summary="停用影响面预览（只读，不写一行）")
def getImpact(personCode: str) -> dict:
    """**只读**。这个端点存在的理由：停用是不可逆的语义操作（脸会退回队列），
    必须在动手之前把后果说清楚（开发计划 DR-19：「他必须先知道」）。

    ⚠️ 它**一个字节都不写** —— 验收第 22 条要核对
       pb_person / pb_face / pb_person_centroid / pb_review_log 四张表行数不变。
       所以这里只调 `assigner.personImpact()`（同样是纯读）。
    """
    _requirePerson(personCode)
    impact = assigner.personImpact(str(personCode))
    row = browse.personRow(personCode)
    impact["displayName"] = str(row.get("displayName") or "")
    impact["birthday"] = row.get("birthday") or None
    impact["destructive"] = False
    impact["note"] = ("停用 = delFlag 改 '1'，**不硬删**（DR-19）；"
                      "人脸退回待确认队列、质心全删、关联行清理；"
                      "确认=true 才会执行")
    return dto.okBody(**impact)


@router.post("/contacts/{personCode}/disable", summary="停用（不带 confirm=true 只返回影响面）")
def disableContact(personCode: str,
                   confirm: int = Query(default=0,
                                        description="**1 才执行**；不给/0 只返回影响面")):
    """两段式停用（验收第 22 / 23 条）。

    **不带 `confirm=true`** -> 只返回影响面，
    **pb_person / pb_face / pb_person_centroid / pb_review_log 四张表一行都不改**。
    **带 `confirm=true`** -> 按固定顺序执行（见 `assigner.disablePerson`）：
        ① `centroid.dropPerson()` ② 人脸全部退回未归属
        ③ 关联行按纪律 ③ 存废 ④ 落 `pb_review_log(opType=DISABLE)`
    ⚠️ 顺序里最要紧的是**先删质心再退人脸**：反过来做，那些脸会变成
       「人工确认但无质心」—— 既不在待确认队列（personCode 非空），
       也不在「我不同意」（isConfirmed=1），**从所有队列里消失**。
    """
    row = _requirePerson(personCode, withDeleted=True)
    code = str(row.get("personCode") or "")
    impact = assigner.personImpact(code)
    impact["displayName"] = str(row.get("displayName") or "")

    if not int(confirm):
        return dto.okBody(executed=False, confirmRequired=True,
                          note="这是影响面预览；确认无误后带 ?confirm=true 再次调用",
                          **impact)

    if str(row.get("delFlag") or comGD.DEL_FLAG_NO) == comGD.DEL_FLAG_YES:
        #⚠️ `impact` 里已经带 personCode，别再单独传一遍 ——
        #   `okBody(personCode=..., **impact)` 会抛
        #   「got multiple values for keyword argument 'personCode'」。
        impact["executed"] = False
        impact["alreadyDisabled"] = True
        impact["note"] = "这个人已经是停用状态（幂等）"
        return dto.okBody(**impact)
    try:
        result = assigner.disablePerson(code, reason="界面停用")
    except assigner.AssignerError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))

    from processor.review import queue as reviewQueue
    after = reviewQueue.countStates()
    from engine.match import centroid as centroidMod
    centroidRows = sqliteCommon.query_pb_person_centroid(
        "pb_person_centroid", personCode=code, mode="light")
    leftFaces = sqliteCommon.query_pb_face("pb_face", personCode=code, mode="light")
    return dto.okBody(
        executed=True, personCode=code,
        displayName=impact.get("displayName"),
        delFlag=result.get("delFlag"),
        facesReturned=int(result.get("faces") or 0),
        confirmedBefore=int(result.get("confirmedBefore") or 0),
        centroidsDropped=int(result.get("centroidsDropped") or 0),
        centroidRowsLeft=len(centroidRows),          # 验收：必须是 0
        facesStillAssigned=len(leftFaces),           # 验收：必须是 0
        linksDropped=int(result.get("linksDropped") or 0),
        logCode=result.get("logCode"),
        pendingCount=int(after.get("pending") or 0),
        disputedCount=int(after.get("disputed") or 0),
        note="人脸已全部退回待确认队列；要恢复自动匹配需重新确认")


@router.post("/contacts/{personCode}/enable", summary="恢复（提示需重新确认人脸）")
def enableContact(personCode: str) -> dict:
    """恢复 `delFlag`。**响应必带 `note` 提示「需重新确认人脸才能自动匹配」**
    （验收第 24 条）—— 恢复后质心是 0 行，这个人**当前不参与自动匹配**。
    """
    row = _requirePerson(personCode, withDeleted=True)
    code = str(row.get("personCode") or "")
    try:
        result = assigner.enablePerson(code)
    except assigner.AssignerError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))

    from processor.review import queue as reviewQueue
    after = reviewQueue.countStates()
    return dto.okBody(executed=True, changed=bool(result.get("changed")),
                      personCode=code,
                      displayName=str(row.get("displayName") or ""),
                      delFlag=result.get("delFlag"),
                      faceCount=int(result.get("faceCount") or 0),
                      centroidCount=int(result.get("centroidCount") or 0),
                      logCode=result.get("logCode") or None,
                      pendingCount=int(after.get("pending") or 0),
                      note=result.get("note") or assigner._ENABLE_NOTE)


# ============================================================
# 六、GET /api/contacts/duplicates（同名 / 疑似同人合并候选）
# ============================================================

@router.get("/contacts/duplicates", summary="疑似同人合并候选（同名归一 / 同邮箱 / 同电话）")
def listDuplicates(page: int = Query(default=1, ge=1),
                   size: int = Query(default=20, le=dto.MAX_PAGE_SIZE),
                   includeSoftDeleted: int = Query(default=0,
                                                   description="1 把已停用的人也算进来")):
    """报出「极可能是同一个人」的候选对，供 UI 直接给合并入口。

    ⚠️⚠️ **「显示名完全相同」这种重名在本库里根本存不进去**
    ------------------------------------------------------
      `pb_person.displayName` 上有 **UNIQUE 索引**（数据库设计.md §4.2），
      而 `contactCommon.makeDisplayName()` 在导入时就已经在加 `(2)/(3)` 后缀避让。
      也就是说：**任何两个 personCode 的 displayName 不可能字面相同**。
      ⇒ 所以这个端点的第一类判据不是「字面重名」，而是
        **`contactCommon.nameKey()` 归一之后相同、但原文不同** ——
        那才是真正会发生且真正危险的情形：
            "Zhang San"  vs  "Zhang  San"   （多空格）
            "ｚｈａｎｇ"  vs  "zhang"      （全角/半角 + NFKC）
            "李明 "     vs  "李明"        （尾随空格）
        这些人**极可能是同一人**（一次手敲、一次导入），却因为原文差一个空格
        而在库里是两条独立档案，各自认领了一部分照片。
        `contactCommon.planContact` 认人时用的就是 nameKey，所以它们的
        `byNameKey` 本来就会撞 —— 这里只是把那个碰撞**显式报出来**。

    另外两类
    --------
      · **同邮箱**：强信号（`pb_person.email` 无唯一约束，可以重复）。
      · **同电话**：中等强度。**只提示、不自动合并** —— 号码会被复用
        （家庭共用座机、公司总机），自动合并会把两个不同的人并成一个
        （这是 contactCommon.planContact 早就定下的规矩）。

    ⚠️ 刻意**不按人脸相似度/年代档距离**推荐合并：那是 merger 的活；
       而且自动合并比自动拆分安全得多（合错了能再拆，拆错了用户
       看不出哪个才对）。
    """
    p, s = dto.clampPage(page, size, defaultSize=20)
    delFlag = "*" if int(includeSoftDeleted) else comGD.DEL_FLAG_NO
    allRows = sqliteCommon.query_pb_person("pb_person", delFlag=delFlag,
                                           mode="light", orderBy="displayName")
    codes = [str(r.get("personCode") or "") for r in allRows]
    stats = browse.personStatsOf(codes)
    _byCode = {str(r.get("personCode") or ""): r for r in allRows}

    def _pack(one, other, reasons):
        a, b = str(one.get("personCode")), str(other.get("personCode"))
        primary = min(reasons, key=lambda r: _REASON_RANK.get(r, 9))
        return {"reason": primary, "reasons": sorted(reasons,
                                                     key=lambda r: _REASON_RANK.get(r, 9)),
                "personA": a, "personB": b,
                "nameA": str(one.get("displayName") or ""),
                "nameB": str(other.get("displayName") or ""),
                "emailA": one.get("email") or None, "emailB": other.get("email") or None,
                "phoneA": one.get("phone") or None, "phoneB": other.get("phone") or None,
                "birthdayA": one.get("birthday") or None,
                "birthdayB": other.get("birthday") or None,
                "photoCountA": int((stats.get(a) or {}).get("photoCount") or 0),
                "photoCountB": int((stats.get(b) or {}).get("photoCount") or 0),
                "mergeEndpoint": "/api/review/merge",
                "hint": "；".join(_DUPLICATE_HINTS[r] for r in
                                 sorted(reasons, key=lambda r: _REASON_RANK.get(r, 9)))}

    # ⚠️ 同一对人可能**同时**命中多个判据（同邮箱 + 同手机号）。
    #   早先的写法是「第一个命中的判据独占这一对」，于是
    #   「同邮箱且同手机号」只会报出 email，phone 那个更强的信号被吞掉 ——
    #   而用户看到的是一个不完整的理由。改成**一对一行 + reasons 列表**。
    found = {}
    buckets = {"nameKey": {}, "email": {}, "phone": {}}
    for row in allRows:
        nameKey = contact.nameKey(str(row.get("displayName") or ""))
        if nameKey:
            buckets["nameKey"].setdefault(nameKey, []).append(row)
        mail = str(row.get("email") or "").strip().lower()
        if mail:
            buckets["email"].setdefault(mail, []).append(row)
        # ⚠️ 电话必须走 `contact.phoneKey()` 归一，不能直接比字符串：
        #   同一个中国手机号在通讯录里可能是
        #     "13800138000" / "138-0013-8000" / "138 0013 8000" /
        #     "+8613800138000" / "8613800138000" / "008613800138000"
        #   **六种写法**，字面比较一个都配不上对 —— 而它们确实是同一个号。
        #   phoneKey 的口径（**全部判据在 contactCommon**）：
        #     · 带 `+` 或 `00` 开头 = 国际写法 -> 先吃 `+`/`00` 再吃区号 `86`
        #     · 其余 = **默认中国国内号（手机号 11 位）**
        #     · 中间的 `-` / 空格 / 括号 / 点 一律去掉
        #     · 国内长途前缀 `0`（座机区号）也去掉
        #   ⚠️ 它**只归一前缀、不合并号段**：`+86285187018`（少一位的截断脏数据）
        #      与 `02885187018` 归一到不同的键，**故意不配成对** ——
        #      宁可漏一次合并提示，也不能把两个真人并成一个。
        number = contact.phoneKey(row.get("phone"))
        if number:
            buckets["phone"].setdefault(number, []).append(row)
    for reason, groups in sorted(buckets.items()):
        for _value, group in sorted(groups.items()):
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    key = tuple(sorted((str(group[i].get("personCode")),
                                        str(group[j].get("personCode")))))
                    found.setdefault(key, []).append(reason)
    pairs = [_pack(_byCode[key[0]], _byCode[key[1]], sorted(set(reasons)))
             for key, reasons in sorted(found.items())]
    pairs.sort(key=lambda it: (_REASON_RANK.get(it["reason"], 9),
                               -min(it["photoCountA"], it["photoCountB"]),
                               it["nameA"], it["nameB"]))
    start = (p - 1) * s
    return dto.pageBody(pairs[start:start + s], p, s, len(pairs))


#: 三类候选的排序优先级（nameKey 归一重名最可能是同一人）
_REASON_RANK: dict = {"nameKey": 0, "email": 1, "phone": 2}

_DUPLICATE_HINTS: dict = {
    "nameKey": "两个显示名**归一之后相同、原文不同**（多空格/全角半角/NFKC）—— "
               "极可能就是同一人，建议合并",
    "email": "邮箱相同 —— 强信号，但邮箱也可能被复用，**请人工确认**",
    "phone": "电话号码归一后相同（已去掉 `-`/空格/`+`/`86`/区号前缀 0）—— "
             "中强度信号，但号码会被复用（家庭座机/公司总机），**请人工确认**",
}


# ============================================================
# 七、POST /api/contacts/import/csv
# ============================================================

@router.post("/contacts/import/csv", summary="上传 CSV 导入联系人（?dryRun=true 只回计划）")
async def importCsv(file: UploadFile = File(..., description="CSV 文件（.csv）"),
                    dryRun: int = Query(default=0,
                                        description="**1 = 只回计划，一个字都不写**")):
    """CSV 导入（步骤 8 的 `csv_import.plan/apply` 的 HTTP 包装）。

    ⚠️ **dryRun=true 时必须不写任何一行**（验收第 26 条）：
      `csv_import.plan()` 本身就是纯读（它只 `readContacts` + `loadPersonIndex`），
      所以 dryRun 就是**只调 plan 不调 apply** —— 没有「先 apply 再回滚」这种做法，
      那样会触发一堆 upsert 与分类增删，回滚不干净就等于没回滚。
      响应给回：新建/更新/跳过的条数 + 警告列表。

    ⚠️ 归档原件会写到 `db\\imports\\`（**不是 photo 目录**）。
      dryRun 不归档；实跑时 `csv_import.apply(archive=True)` 先归档再写库，
      归档失败只降级为 warning（库写成功是主目标）。
    ⚠️ 文件落点：`db\\imports\\csv\\`（`contactCommon.archiveOriginal` 的口径），
      传进来的是内存字节，**先落一个临时文件**给 plan() 读 ——
      plan 需要一个真实路径（它要做编码嗅探）。
    """
    raw = await file.read()
    if not raw:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "上传的文件是空的")
    stem = os.path.splitext(os.path.basename(file.filename or "contacts.csv"))[0]
    suffix = os.path.splitext(file.filename or ".csv")[1] or ".csv"
    # ⚠️ 落在 paths.imports_dir() 下（= <dbDir>\imports）—— **绝不是 photo 目录**。
    #    文件名带 uuid：只靠时间戳的话，同一秒里传两个同名文件会互相覆盖，
    #    而结果是「第二个文件的导入计划里是第一个文件的内容」——
    #    这类错不报错，只是计划静默地不对。
    dropDir = os.path.join(paths.imports_dir(), "csv")
    try:
        os.makedirs(dropDir, exist_ok=True)
    except OSError as e:
        raise dto.ApiError(dto.CODE_DB_ERROR, "建导入暂存目录失败: %s" % e)
    tmpPath = os.path.join(dropDir, "upload_%s_%s%s" % (stem or "contacts",
                                                        uuid.uuid4().hex[:8], suffix))
    try:
        with open(tmpPath, "wb") as handle:
            handle.write(raw)
    except OSError as e:
        raise dto.ApiError(dto.CODE_DB_ERROR, "写导入暂存文件失败: %s" % e)

    try:
        planned = csvImport.plan(tmpPath)
    except Exception as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "CSV 解析失败: %s: %s"
                           % (type(e).__name__, e))

    items = planned.get("items") or []
    creates = [it for it in items if it.get("op") == "create"]
    updates = [it for it in items if it.get("op") == "update"]
    skips = [it for it in items if it.get("op") == "skip"]

    if int(dryRun):
        # 暂存文件删掉：dryRun 的定义就是「什么都不留」
        _silentRemove(tmpPath)
        return dto.okBody(dryRun=True, written=False,
                          file=planned.get("source"),
                          encoding=(planned.get("info") or {}).get("encoding"),
                          totalRows=int((planned.get("info") or {}).get("totalRows") or 0),
                          createCount=len(creates), updateCount=len(updates),
                          skipCount=len(skips),
                          preview=[{"op": it.get("op"), "personCode": it.get("personCode"),
                                    "displayName": it.get("displayName"),
                                    "hit": it.get("hit"),
                                    "categories": it.get("categories") or []}
                                   for it in items[:50]],
                          warnings=planned.get("warnings") or [],
                          note="dryRun：**一个字节都没写**（含暂存文件已删除）")

    summary = csvImport.apply(planned, ownerID=basicSettings.DEFAULT_OWNER_ID,
                              archive=True)
    # ⚠️ `applyPlan` 返回的是 **created/updated/skipped/failed 四个 personCode 列表**
    #   （不是计数）。早先按 create/update/skip 取，四个数全是 None ——
    #   而响应仍然是 200，导入也真的成功了，于是「前端显示导入了 0 个人」
    #   配上一份完整的库。这种错只在**比对**时才会暴露。
    created = list(summary.get("created") or [])
    updated = list(summary.get("updated") or [])
    skipped = list(summary.get("skipped") or [])
    failed = list(summary.get("failed") or [])
    _LOG.info("CSV 导入 %s：新建 %d 更新 %d 跳过 %d 失败 %d",
              planned.get("source"), len(created), len(updated),
              len(skipped), len(failed))
    return dto.okBody(
        dryRun=False, written=True, file=planned.get("source"),
        createCount=len(created), updateCount=len(updated),
        skipCount=len(skipped), failedCount=len(failed),
        created=created, updated=updated, skipped=skipped, failed=failed,
        categoryRows=int(summary.get("categoryRows") or 0),
        categoryAdded=int(summary.get("categoryAdded") or 0),
        categoryRemoved=int(summary.get("categoryRemoved") or 0),
        rebucketed=int((summary.get("rebucketed") or {}).get("changed") or 0),
        archive=summary.get("archive") or {},
        warnings=list(planned.get("warnings") or []) + list(summary.get("warnings") or []),
        familySuggestions=summary.get("familySuggestions") or [])


def _silentRemove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ============================================================
# 八、家庭组（P2，可延后；这里给最小可用面）
# ============================================================

def _insertFamily(familyCode: str, familyName: str, notes: str = "") -> int:
    """新建一个家庭组行（**唯一**的写入口，POST 与 PATCH 都走它）。"""
    rtn, _cols = sqliteCommon.insertManyTableGeneral(
        "pb_family",
        [{"familyCode": str(familyCode), "familyName": str(familyName),
          "notes": str(notes or "") or None,
          "regID": basicSettings.DEFAULT_OWNER_ID,
          "modifyID": basicSettings.DEFAULT_OWNER_ID}],
        conflictColumns=("familyCode",),
        updateColumns=("familyName", "notes", "modifyYMDHMS"),
        fillStandard=True, forceColumns=("notes",))
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise dto.ApiError(dto.CODE_DB_ERROR,
                           "pb_family 写入失败: %s" % sqliteCommon.dbHandle().lastErrMsg)
    return int(rtn)


@router.get("/families", summary="家庭组列表")
def listFamilies(page: int = Query(default=1, ge=1),
                 size: int = Query(default=dto.DEFAULT_PAGE_SIZE)):
    """家庭组 + 每组成员数。

    ⚠️ 组员数走 `COUNT(*) ... GROUP BY familyGroupCode`：
      人名册是**小表**（几十~几百人），一次全扫比逐组查便宜几个数量级；
      而且这跟 `/api/persons` 的做法一致（同一口径两处实现会漂移）。
    """
    p, s = dto.clampPage(page, size)
    at = dto.offsetOf(p, s)
    total = sqliteCommon.countTableGeneral("pb_family")
    rows = sqliteCommon.query_pb_family("pb_family", orderBy="familyName",
                                        limitNum=s, offsetNum=at)
    counts = {str(r.get("familyGroupCode") or ""): int(r.get("cnt") or 0)
              for r in query.selectList(
                  "SELECT p.familyGroupCode AS familyGroupCode, COUNT(*) AS cnt"
                  " FROM pb_person p WHERE p.familyGroupCode IS NOT NULL"
                  " AND p.delFlag = %s GROUP BY p.familyGroupCode",
                  (comGD.DEL_FLAG_NO,))}
    items = [{"familyCode": str(r.get("familyCode") or ""),
              "familyName": str(r.get("familyName") or ""),
              "notes": r.get("notes") or None,
              "memberCount": counts.get(str(r.get("familyCode") or ""), 0)}
             for r in rows]
    return dto.pageBody(items, p, s, total)


@router.get("/families/{familyCode}", summary="家庭组详情（含成员）")
def getFamily(familyCode: str):
    """一个家庭组 + 它的成员（含各自的 photoCount）。"""
    rows = sqliteCommon.query_pb_family("pb_family", familyCode=str(familyCode),
                                        limitNum=1)
    if not rows:
        raise dto.ApiError(dto.CODE_NOT_FOUND, "familyCode=%s 不存在" % familyCode)
    members = sqliteCommon.query_pb_person("pb_person",
                                           familyGroupCode=str(familyCode),
                                           mode="light", orderBy="displayName")
    codes = [str(m.get("personCode") or "") for m in members]
    stats = browse.personStatsOf(codes)
    covers = browse.personCoversOf(members)   # 封面脸，批量解析（DR-40）

    def _member(one):
        code = str(one.get("personCode") or "")
        one_stat = stats.get(code) or {}
        return browse.personSummary(one, one_stat.get("photoCount", 0),
                                    one_stat.get("faceCount", 0),
                                    one_stat.get("confirmedFaceCount", 0),
                                    one_stat.get("yearLow"), one_stat.get("yearHigh"),
                                    coverFaceCode=covers.get(code))

    return {"ok": True,
            "familyCode": str(rows[0].get("familyCode") or ""),
            "familyName": str(rows[0].get("familyName") or ""),
            "notes": rows[0].get("notes") or None,
            "members": [_member(m) for m in members]}


@router.post("/families", summary="新建家庭组")
def createFamily(body: dto.FamilyBody) -> dict:
    """新建家庭组。`familyCode` 缺省按 `FM_<名字>` 生成。"""
    name = str(body.familyName or "").strip()
    if not name:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "familyName 不能为空")
    code = str(body.familyCode or "").strip()
    if not code:
        index = contact.loadFamilyCodes()
        stem = contact.sanitizeCodeKey(name, 48) or "group"
        code, n = ("%s%s" % (FAMILY_CODE_PREFIX, stem))[:64], 2
        while code in index:
            code = ("%s%s%d" % (FAMILY_CODE_PREFIX, stem, n))[:64]
            n += 1
    if sqliteCommon.query_pb_family("pb_family", familyCode=code, delFlag="*"):
        raise dto.ApiError(dto.CODE_DUPLICATE_DISPLAY_NAME,
                           "家庭组编码 %s 已存在" % code,
                           extra={"code": dto.CODE_DUPLICATE_DISPLAY_NAME,
                                  "existing": {"familyCode": code}})
    _insertFamily(code, name, str(body.notes or ""))
    return dto.okBody(familyCode=code, familyName=name, notes=body.notes)


@router.patch("/families/{familyCode}", summary="改家庭组（名称 / 备注）")
def patchFamily(familyCode: str, body: dto.FamilyBody) -> dict:
    """改家庭组的名称/备注。

    ⚠️ **familyCode 不允许改**：它是 `pb_person.familyGroupCode` 的引用目标，
       改它等于要级联改所有成员 —— 而 `update_pb_person` 写 NULL 不可靠、
       `updateTableGeneral` 又没有批量列更新的白名单保障。
       真要改编码应该是「新建一个 + 改成员 + 停用旧的」，那是步骤 12 的决策。
    """
    rows = sqliteCommon.query_pb_family("pb_family", familyCode=str(familyCode),
                                        limitNum=1)
    if not rows:
        raise dto.ApiError(dto.CODE_NOT_FOUND, "familyCode=%s 不存在" % familyCode)
    if body.familyCode and str(body.familyCode) != str(familyCode):
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "familyCode 不可修改（它是 pb_person.familyGroupCode "
                           "的引用目标，改它要级联改所有成员）")
    patch = {"familyName": str(body.familyName or "").strip(),
             "notes": str(body.notes or "") or None,
             "modifyYMDHMS": misc.getTime()}
    rtn = sqliteCommon.updateTableGeneral(
        "pb_family", "familyCode = %s", (str(familyCode),), patch)
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise dto.ApiError(dto.CODE_DB_ERROR,
                           "pb_family 更新失败: %s" % sqliteCommon.dbHandle().lastErrMsg)
    return dto.okBody(familyCode=str(familyCode), familyName=patch["familyName"],
                      notes=patch["notes"])


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("contacts.py _VERSION:", _VERSION)
    print("库:", sqliteCommon.dbFilePath() or "(未连接)")
    print("路由:", [(r.path, sorted(r.methods)) for r in router.routes])
    print("**没有 DELETE 路由**（DR-19）:", [r.path for r in router.routes
                                            if "DELETE" in (r.methods or [])] or "确认：不存在")
    print("**没有 export 路由**（DR-17）:", [r.path for r in router.routes
                                            if "export" in r.path] or "确认：不存在")
    print("personCode 前缀:", PERSON_CODE_PREFIX_UI, "/ 家庭组:", FAMILY_CODE_PREFIX)
    print("可改字段:", CONTACT_PATCH_COLUMNS, "+ categories")
