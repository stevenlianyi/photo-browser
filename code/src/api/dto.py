#! /usr/bin/env python3
#encoding: utf-8

#Filename: dto.py
#Description: photo-browser API **统一响应结构**（步骤 9）—— 分页 / 错误 / 公共入参
#
# 本文件存在的唯一理由：让前端**只学一次**
# ------------------------------------------------
#   分页：所有列表接口一律返回 { page, size, total, items }
#   错误：所有非 2xx 一律返回 { code, message }
#   步骤 10 的前端只写一个 `request()` 拦截器就能处理全部接口；
#   如果每个路由自己发明字段名（有的叫 items、有的叫 data、有的叫 list），
#   前端要么写一堆 if，要么在某处静默拿到 undefined —— 而后者不会报错，
#   只会表现为「列表页空白」，是排障最贵的一类 bug。
#
# 为什么分页是 page/size 而**不是** 游标（硬约束要求的取舍）
# -------------------------------------------------------
#   硬约束两条同时成立：「分页统一 page/size/total」+「不要 OFFSET 深分页」。
#   二者的调和方式是：
#     · **列表类接口**（/api/photos、/api/contacts…）用 page/size —— 它们是
#       「筛选结果第几页」语义，用户能直接跳到第 5 页，OFFSET 是对的；
#       但加一道 `MAX_PAGE_OFFSET` 闸：翻到太深就**明确报错**并提示改用筛选，
#       而不是让 SQLite 扫 10 万行再丢弃（深分页慢还不报错，是最难查的一类）。
#     · **时间线** 走「按年月分段」（见 api/browse.py）—— 10 万张的时间线本质
#       是一条按年月降序的**有序流**，游标/分段才是对的模型。
#   ⇒ 分页键在**不同的轴**上，深分页的问题就不存在了。
#
# 错误码（字符串常量，不用数字）
# ----------------------------
#   用数字（globalDefinition 那一套）适合内部函数返回码；
#   HTTP 层的 code 是**前端要 switch 的分支**，字符串在日志/截图里自带含义，
#   打错一个数字只能回去查表。所以 api 层用字符串，与内部数字码并存不冲突。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from typing import Any, Generic, List, Optional, TypeVar            # noqa: E402

from pydantic import BaseModel, ConfigDict, Field                   # noqa: E402

_VERSION = "20261006"

ItemT = TypeVar("ItemT")


# ============================================================
# 一、错误码与错误体
# ============================================================

#: 参数非法（缺参、类型错、越界、动作值不在枚举里）
CODE_PARAM_INVALID: str = "PARAM_INVALID"
#: 资源不存在（photoCode / faceCode / personCode / jobCode / logCode）
CODE_NOT_FOUND: str = "NOT_FOUND"
#: **方法不匹配**（对只收 POST 的路径发了 GET 等）
#:
#: ⚠️ 为什么要单独一个码（步骤 9 实测发现）：动作不匹配原来是落到
#:   `PARAM_INVALID`(400) + 英文原文 "Method Not Allowed" 的 —— 前端会把它
#:   当成「我的参数写错了」去走表单校验分支，而真相是**调用姿势错了**
#:   （比如把 `POST /api/places/rebuild` 写成了 GET）。
#:   405 + 明确写出「允许哪些方法」才能让人一眼改对。
CODE_METHOD_NOT_ALLOWED: str = "METHOD_NOT_ALLOWED"
#: 状态非法（任务状态机不允许的流转、已撤销过、已停用）
CODE_TASK_STATE_ILLEGAL: str = "TASK_STATE_ILLEGAL"
#: 重名（改 displayName 撞 UNIQUE）—— **带 existing 详情**，见 DR-18
CODE_DUPLICATE_DISPLAY_NAME: str = "DUPLICATE_DISPLAY_NAME"
#: 深分页被闸门拦下
CODE_PAGE_TOO_DEEP: str = "PAGE_TOO_DEEP"
#: 库/表缺失或读写出错
CODE_DB_ERROR: str = "DB_ERROR"
#: 未预期异常（服务端 bug）
CODE_INTERNAL: str = "INTERNAL"

#: 错误码 -> 默认 HTTP 状态码。**刻意让 409 只归 DUPLICATE_DISPLAY_NAME**：
#:   「撞名」在语义上不是「服务器炸了」也不是「你请求写错了」，
#:   409 + existing 是唯一能让前端弹出「已存在 X，你可能是想合并到 TA？」的表达。
_CODE_STATUS: dict = {
    CODE_PARAM_INVALID: 400,
    CODE_NOT_FOUND: 404,
    CODE_METHOD_NOT_ALLOWED: 405,
    CODE_TASK_STATE_ILLEGAL: 409,
    CODE_DUPLICATE_DISPLAY_NAME: 409,
    CODE_PAGE_TOO_DEEP: 400,
    CODE_DB_ERROR: 500,
    CODE_INTERNAL: 500,
}


class ErrorBody(BaseModel):
    """**全项目唯一的错误体**：{ code, message }。

    extra 字段只在个别错误上出现（DUPLICATE_DISPLAY_NAME 带 existing），
    它是可选的，前端必须容忍「没有 extra」。
    """

    code: str = Field(..., description="机器可判定的错误码（见 api.dto.CODE_*）")
    message: str = Field(..., description="给人看的一句话，可直接 Toast")
    extra: Optional[dict] = Field(default=None, description="按错误码可选的详情")


class ApiError(Exception):
    """api 层统一异常。路由里 `raise ApiError(CODE_XXX, "...")` 即可，
    由 registerErrorHandlers 装成 FastAPI 异常处理器，转成 { code, message }。

    为什么不直接用 HTTPException
    --------------------------
      HTTPException 的 body 是 `{"detail": ...}`，detail 可以是任意结构 ——
      于是「有的接口 detail 是字符串、有的接口 detail 是 dict」，
      前端每次都得先判类型。本项目要的是**一个结构走到底**。
    """

    def __init__(self, code: str, message: str, status: int = None,
                 extra: dict = None):
        Exception.__init__(self, message)
        self.code = str(code or CODE_INTERNAL)
        self.message = str(message or "")
        self.status = int(status if status is not None
                          else _CODE_STATUS.get(self.code, 400))
        self.extra = extra

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, "extra": self.extra}


def errorBody(code: str, message: str, extra: dict = None) -> dict:
    """直接造一个错误体（用于 exception_handler 内部，不抛异常）。"""
    return {"code": str(code), "message": str(message), "extra": extra}


# ============================================================
# 二、分页
# ============================================================

#: 前端不传 size 时的默认页大小。取 60：照片网格一屏放得下（UI 设计 P-02 的分页文案
#: 就是「显示 1-60 条」），联系人列表一屏也够。
DEFAULT_PAGE_SIZE: int = 60
#: 单页上限。**不静默截断**：超过就 400，让前端知道自己传错了。
MAX_PAGE_SIZE: int = 200
#: 深分页闸门。offset 超过它就报错并提示改用筛选/分段 ——
#: 10 万行 OFFSET 90000 会让 SQLite 扫完再丢弃，几十到几百毫秒，
#: 而且**不报错**：用户只会觉得「这一页好慢」，不会知道原因。
MAX_PAGE_OFFSET: int = 20000


class Page(BaseModel, Generic[ItemT]):
    """全项目唯一的分页响应体：{ page, size, total, items }。

    hasMore 是**冗余但刻意保留**的：前端判断「还有下一页」本来要写
    `page * size < total`，而一旦 total 与 items 数因故不一致，
    这个算式会给出一个看似合理的错误答案。hasMore 让服务端说了算。
    """

    page: int = Field(..., description="页码，从 1 开始")
    size: int = Field(..., description="本页请求的条数")
    total: int = Field(..., description="符合条件的总条数（不是本页条数）")
    items: List[ItemT] = Field(default_factory=list, description="本页数据")
    hasMore: bool = Field(default=False, description="是否还有下一页")


def clampPage(page: int, size: int, defaultSize: int = DEFAULT_PAGE_SIZE) -> tuple:
    """把 page/size 归一到合法值。**越界一律抛错，不静默回落**。

    为什么 page/size 越界要报错而不是回落
    ------------------------------------
      静默回落是**最贵的**一种"贴心"：前端 off-by-one 传了 page=0，
      服务端悄悄按 1 处理并返回 200，页面看着正常，于是这个 bug 可以活很久，
      直到某个列表的第 2 页开始重复第一页的数据才被人发现。
    """
    try:
        p = int(page if page is not None else 1)
    except (TypeError, ValueError):
        raise ApiError(CODE_PARAM_INVALID, "page 必须是整数: %r" % page)
    try:
        s = int(size if size is not None else defaultSize)
    except (TypeError, ValueError):
        raise ApiError(CODE_PARAM_INVALID, "size 必须是整数: %r" % size)
    if p < 1:
        raise ApiError(CODE_PARAM_INVALID, "page 从 1 开始，收到 %d" % p)
    if s < 1:
        raise ApiError(CODE_PARAM_INVALID, "size 至少为 1，收到 %d" % s)
    if s > MAX_PAGE_SIZE:
        raise ApiError(CODE_PARAM_INVALID,
                       "size 超过单页上限 %d，收到 %d" % (MAX_PAGE_SIZE, s))
    return p, s


def offsetOf(page: int, size: int, maxOffset: int = MAX_PAGE_OFFSET) -> int:
    """page/size -> OFFSET，并过一道深分页闸。"""
    at = (int(page) - 1) * int(size)
    if at > int(maxOffset):
        raise ApiError(
            CODE_PAGE_TOO_DEEP,
            "offset=%d 超过深分页上限 %d：深翻页会让数据库扫描并丢弃大量行，"
            "请收窄筛选条件（或时间线接口按年月分段拉取）" % (at, maxOffset),
            extra={"page": int(page), "size": int(size),
                   "maxOffset": int(maxOffset)})
    return at


def pageBody(items: list, page: int, size: int, total: int = None) -> dict:
    """把「一页 items + 总数」组装成统一分页体。

    total 传 None 时**不猜**：直接用 len(items)，
    调用方（列表路由）必须显式给出真实的 COUNT，否则前端分页器会是错的。
    """
    rows = list(items or [])
    totalValue = int(total) if total is not None else len(rows)
    return {"page": int(page), "size": int(size), "total": totalValue,
            "items": rows, "hasMore": int(page) * int(size) < totalValue}


def pageSlice(rows: list, page: int, size: int, total: int = None) -> dict:
    """从一个**已完整取回**的 list 里切出一页（内存分页）。

    只适用于「结果集本身就有界」的接口（家庭组成员、簇成员这种几十条的）。
    10 万条的东西**绝不能**先全取再切 —— 那是深分页问题的另一种写法。
    """
    allRows = list(rows or [])
    start = (int(page) - 1) * int(size)
    offsetOf(page, size)
    return pageBody(allRows[start:start + int(size)], page, size,
                    total if total is not None else len(allRows))


# ============================================================
# 三、通用成功体
# ============================================================

class OkBody(BaseModel):
    """非列表类接口的统一成功体：{ ok, ... }。

    为什么不是 `{}`：写操作（「已确认 1 张」「质心已重算」）必须让调用方
    **能分辨成功与什么都没发生**。assigner 大量返回 changed=False 的情况
    （幂等重复点击），前端需要据此决定要不要刷新界面。
    """

    ok: bool = True
    detail: Optional[dict] = Field(default=None, description="按接口自定义的附加字段")


def okBody(**fields) -> dict:
    """`return okBody(personCode=..., centroidRebuilt=True)` -> {"ok":true, ...}"""
    out = {"ok": True}
    out.update(fields)
    return out


# ============================================================
# 四、请求体（pydantic 模型）
# ============================================================

class ScanStartBody(BaseModel):
    """POST /api/scan/start"""
    rootPath: Optional[str] = Field(default=None, description="扫描根；缺省 photo_dir()（**只读**）")
    batchSize: Optional[int] = Field(default=None, description="每批处理张数；缺省 basicSettings.BATCH_SIZE")
    maxBatches: Optional[int] = Field(default=None,
                                      description="后台最多跑几批；缺省跑到 DONE")
    countTotal: bool = Field(default=False,
                             description="是否在 start 里先全树数一遍总数。"
                                         "**缺省 false**：那是一次 scandir"
                                         "（10 万张 0.3~2s），第一批本来就会重数")
    jobCode: Optional[str] = Field(default=None, description="指定则幂等复用该任务")
    autoFace: bool = Field(default=True,
                           description="扫描跑完后**自动接着识别人脸**（缺省 true）。"
                                       "关掉它就只清点不入脸库 —— 待确认数会一直是 0，"
                                       "因为它数的是 pb_face 而不是 pb_photo")


class AssignBody(BaseModel):
    """PUT /api/review/{faceCode}/assign"""
    personCode: str = Field(..., description="归属到谁（pb_person.personCode）")


class BatchAssignBody(BaseModel):
    """POST /api/review/batch-assign"""
    faceCodes: List[str] = Field(default_factory=list, description="要确认的人脸编码列表")
    personCode: str = Field(..., description="统一归给谁")


class FixBody(BaseModel):
    """POST /api/review/fix —— **改判**（DR-16②）"""
    faceCode: Optional[str] = Field(default=None, description="单张改判")
    faceCodes: List[str] = Field(default_factory=list, description="批量改判（与 faceCode 二选一）")
    action: str = Field(..., description="assign 改判到某人 / unknown 置为未知 / stranger 标陌生人")
    personCode: Optional[str] = Field(default=None, description="action=assign 时必填")
    reason: Optional[str] = Field(default=None, description="改判理由，写进 pb_review_log.detail")


class BatchFixBody(BaseModel):
    """POST /api/review/batch-fix —— 同一 clusterCode 批量改判"""
    faceCodes: List[str] = Field(default_factory=list,
                                 description="人脸编码列表（通常来自簇视图）")
    clusterCode: Optional[str] = Field(
        default=None,
        description="**与 faceCodes 二选一**。给簇编码时服务端现查活成员"
                    "（⚠️ clusterCode 是内容指纹、不许跨会话缓存："
                    "确认掉一个成员剩下的脸就分裂成新编码了）")
    action: str = Field(..., description="assign / unknown / stranger")
    personCode: Optional[str] = Field(default=None, description="action=assign 时必填")
    reason: Optional[str] = Field(default=None, description="写进日志 detail")


class MergeBody(BaseModel):
    """POST /api/review/merge"""
    fromPersonCode: str = Field(..., description="被合并方（会被软删）")
    toPersonCode: str = Field(..., description="保留方")


class SplitBody(BaseModel):
    """POST /api/review/split"""
    faceCode: str = Field(..., description="要拆的那张脸")
    personCode: Optional[str] = Field(default=None,
                                      description="拆给谁；不给 = 置为未归属（回待确认队列）")
    displayName: Optional[str] = Field(default=None, description="personCode 不存在时自动建档用的姓名")
    birthday: Optional[str] = Field(default=None, description="同上，生日（直接影响年代档划分）")


class UndoBody(BaseModel):
    """POST /api/review/undo"""
    logCode: str = Field(..., description="要撤销的 SPLIT / MERGE 日志编码")


class MarkDuplicateBody(BaseModel):
    """POST /api/photos/{photoCode}/mark-duplicate（步骤 11）

    ⚠️ 为什么必填而不是「一个 flag」：重复标记**必须指向一张具体的主照片**
       （pb_photo.dupOfPhotoCode），否则「这条记录重复了」是一句无法核对、
       也无法撤销的话。UI 上对应「选一张作为主照片」的对话框，不允许留空提交。
    """
    dupOfPhotoCode: str = Field(...,
                                description="主照片的 photoCode（不能是它自己）")


class ShotYearFixBody(BaseModel):
    """POST /api/photos/{photoCode}/shot-year-fix（DR-42）

    `shotYear` 给值 = 人工修正；**给 null = 恢复自动**（回到 EXIF/文件名/mtime）。

    ⚠️ 为什么「恢复自动」与「修正」共用一个字段而不是两个端点：
       它们改的是**同一列**（pb_photo.shotYearOverride），只是取值不同；
       拆成两个端点会让前端需要自己判断"现在该调哪个"，而判断依据
       （当前有没有 override）本来就在照片详情的响应里（`shotYearOverride`）。

    ⚠️ 路由里用 `model_dump(exclude_unset=True)` 判"到底传没传 shotYear"：
       不传 = 参数漏了（400），传 null = 恢复自动 —— 两者都是 None，
       只能靠字段是否在 `model_fields_set` 里区分。混在一起的话，
       前端漏传一个字段就会**静默把用户的修正清掉**。
    """
    shotYear: Optional[int] = Field(
        default=None,
        description="修正后的拍摄年份；传 null = 恢复自动（回到 EXIF/文件名/mtime）")
    # ⚠️ 这里**刻意不加** ge/le 这类区间约束：pydantic 的越界会回 422，
    #    而本项目所有错误都是 `{code, message}` + 400/404/409 的形状（见本文件
    #    顶部说明）。年份区间由 `bucket.validShotYear` 校验（它同时管着
    #    SHOT_YEAR_MIN/MAX 这一处真相），越界时由路由映射成 400。


class ContactCreateBody(BaseModel):
    """POST /api/contacts —— 界面新建手工档案（source=0，vcardUid 空）"""
    displayName: str = Field(..., description="显示名（**UNIQUE**；重名会自动加 (2) 后缀并记警告）")
    familyName: Optional[str] = None
    familyGroupCode: Optional[str] = Field(default=None, description="挂到已有家庭组；不存在则自动建")
    relation: Optional[str] = Field(default=None, description="parent/spouse/child/sibling")
    email: Optional[str] = None
    phone: Optional[str] = None
    birthday: Optional[str] = Field(default=None, description="YYYY-MM-DD（**改它会触发质心重算**）")
    categories: List[str] = Field(default_factory=list,
                                   description="分类（family/friend/colleague；未登记的走别名归一）")
    memo: Optional[str] = None


class ContactPatchBody(BaseModel):
    """PATCH /api/contacts/{personCode} —— **部分字段**更新（只写传了的字段）

    ⚠️ birthday 是唯一需要重算质心的字段（DR-18）。其余字段（displayName /
       familyName / familyGroupCode / relation / email / phone / 分类 /
       avatarFaceCode）改了**什么都不用做** —— 它们不参与任何划分年代档与匹配。

    ⚠️ `avatarFaceCode`（DR-41）是**展示**字段，不进质心、不写 `pb_review_log`：
       · 给一个属于**这个人**的 faceCode = 设为默认头像
       · 给空串 / null = 清空（卡片与详情回退到「代表脸」，见 DR-40）
       · 给了**别人的** faceCode → 400（校验在路由里，因为它要查库）

    ⚠️ `extra="allow"`（而不是默认的 ignore，也不是 forbid）
       -----------------------------------------------
       · 默认（ignore）：传 `personCode` 会被**静静丢掉**，
         于是「我没改任何字段」与「我改了个不存在的字段」返回**同一个错误** ——
         前端会显示「没有字段要改」，而用户明明写了个东西。
       · forbid：pydantic 直接回 422，形状与本项目其余错误不同构
         （本项目的形状是 `{code, message}` + 400/404/409 按语义分）。
       · allow：多出来的键进 `model_extra`，由路由用 400 明确拒掉。
    """
    model_config = ConfigDict(extra="allow")

    displayName: Optional[str] = None
    familyName: Optional[str] = None
    familyGroupCode: Optional[str] = None
    relation: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    birthday: Optional[str] = None
    avatarFaceCode: Optional[str] = Field(
        default=None,
        description="默认头像的人脸编码（必须是**这个人**的脸）；空串/null = 清空回退代表脸")
    categories: Optional[List[str]] = Field(
        default=None,
        description="给值 = 以本次为准（空列表会清空分类）；**不给该键 = 一个都不动**")
    memo: Optional[str] = None


class FamilyBody(BaseModel):
    """POST/PATCH /api/families"""
    familyCode: Optional[str] = Field(default=None, description="新建时必填（UNIQUE）")
    familyName: str = Field(..., description="家庭组名称")
    notes: Optional[str] = None


# ============================================================
# 五、异常处理器注册（由 main/app.py 调用）
# ============================================================

def registerErrorHandlers(application) -> None:
    """把「所有异常」收敛成 { code, message }。

    装这几条的理由
    --------------
      ① ApiError          —— 本项目自己的错误类型
      ② HTTPException     —— FastAPI 自带（static.py 的 thumb/original 在用）
      ③ RequestValidation —— FastAPI 的 422，body 是 `{"detail":[{loc,msg,type}]}`，
                            前端要单独写一段解析才能拿到「哪个字段错了」，
                            这里拍平成 { code, message, extra:{errors:[...]}}，
                            **和其余错误同一个壳**
      ④ Exception         —— 兜底。**生产环境不回传堆栈**（里面可能带本机路径），
                            只回一个 code + message，具体原因进服务端日志。

    ⚠️ 404 也在这里统一：FastAPI 的默认 404 body 是 `{"detail":"Not Found"}`，
       与其它错误不同构 —— 前端的错误拦截器会漏掉它。
    """
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @application.exception_handler(ApiError)
    def _apiError(request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=exc.body())

    @application.exception_handler(RequestValidationError)
    def _validationError(request, exc: RequestValidationError):
        errors = [{"loc": [str(x) for x in one.get("loc", ())],
                   "msg": str(one.get("msg", "")), "type": str(one.get("type", ""))}
                  for one in (exc.errors() or ())]
        first = errors[0]["msg"] if errors else "请求体/查询参数校验失败"
        return JSONResponse(
            status_code=422,
            content=errorBody(CODE_PARAM_INVALID, "参数不合法: %s" % first,
                              extra={"errors": errors}))

    @application.exception_handler(StarletteHTTPException)
    def _httpError(request, exc):
        status = int(getattr(exc, "status_code", 500))
        #⚠️ 两个状态要**分别**接住，不能一起落到兜底那行：
        #  · 404：它是「前端调错地址」，与业务错误同构才能被同一个拦截器处理
        #    （FastAPI 默认 body 是 `{"detail":"Not Found"}`，形状不同构）
        #  · 405：它是「**方法**错了」而不是「参数错了」。原来它落到
        #    PARAM_INVALID(400) + 英文 "Method Not Allowed" —— 前端会去走
        #    表单校验分支，而真相是把 POST 写成了 GET 之类。
        #    这里把 `Allow` 头里的可用方法一起告诉调用方。
        if status == 404:
            return JSONResponse(
                status_code=404,
                content=errorBody(CODE_NOT_FOUND,
                                  "接口或资源不存在: %s" % request.url.path,
                                  extra={"path": request.url.path}))
        if status == 405:
            allow = ""
            headers = getattr(exc, "headers", None) or {}
            if isinstance(headers, dict):
                allow = str(headers.get("Allow") or headers.get("allow") or "")
            return JSONResponse(
                status_code=405,
                content=errorBody(
                    CODE_METHOD_NOT_ALLOWED,
                    "%s 不支持 %s 方法%s"
                    % (request.url.path, request.method,
                       ("，允许: %s" % allow) if allow else ""),
                    extra={"path": request.url.path, "method": request.method,
                           "allow": allow or None}),
                headers={"Allow": allow} if allow else None)
        detail = getattr(exc, "detail", "")
        message = detail if isinstance(detail, str) else str(detail)
        # 5xx 归 INTERNAL（而不是 PARAM_INVALID）—— 「服务端炸了」不该被
        # 前端当成「你参数写错了」。其余 4xx 保留 PARAM_INVALID。
        code = CODE_INTERNAL if status >= 500 else CODE_PARAM_INVALID
        return JSONResponse(status_code=status,
                            content=errorBody(code, message))

    @application.exception_handler(Exception)
    def _unhandled(request, exc: Exception):
        #兜底：**不把异常原文回传给前端**（可能含本机绝对路径 / SQL 文本）。
        # 具体原因进日志，前端拿 code=INTERNAL 自己去查服务端日志。
        from common import miscCommon as misc
        misc.setLogNew("apiMain", "apimain.log").error(
            "未处理异常 %s %s: %s: %s", request.method, request.url.path,
            type(exc).__name__, exc)
        return JSONResponse(status_code=500,
                            content=errorBody(CODE_INTERNAL,
                                              "服务端内部错误，请查看服务端日志"))


if __name__ == "__main__":
    print("dto.py _VERSION:", _VERSION)
    print("分页: 默认 size=%d 上限=%d 深分页闸=%d"
          % (DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MAX_PAGE_OFFSET))
    print("错误码:", sorted(_CODE_STATUS.keys()))
    print("pageBody 示例:", pageBody([1, 2, 3], page=2, size=3, total=10))
    print("ErrorBody    :", ErrorBody(code=CODE_NOT_FOUND, message="没有这个人").model_dump())
    for _call in (lambda: clampPage(0, 10), lambda: clampPage(1, 999),
                  lambda: offsetOf(500, 100)):
        try:
            _call()
            print("未拦截（不该发生）")
        except ApiError as _e:
            print("已拦截:", _e.status, _e.body())
