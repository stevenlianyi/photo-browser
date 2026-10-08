#! /usr/bin/env python3
#encoding: utf-8

#Filename: app.py
#Description: photo-browser FastAPI 实例（步骤 4 最小版 / 步骤 9 全量 API）
#
# 本步做的事
# -------------
#   1. FastAPI() + 挂全部 /api 路由（static / browse / scan / review / contacts）
#   2. 挂静态资源（code\webserver\dist）**并实现 SPA history 回退**：
#      深链接（/photos/P-000231）刷新时回 index.html，而不是 404
#   3. **只绑 127.0.0.1**（开发计划 §一 硬约束表；绝不开 0.0.0.0）
#   4. **CORS 只允许本机**（步骤 9 加）—— 见下面的 _installCors
#   5. 统一错误体 `{code, message}`（dto.registerErrorHandlers）
#
# 为什么把"只绑 127.0.0.1"写死在代码里而不是只写在启动命令里
# ----------------------------------------------------------
#   写在启动命令里的话，任何一次 `uvicorn main.app:app --host 0.0.0.0`
#   就能把整个私人照片库暴露到局域网 —— 而这个库没有登录态、没有任何鉴权。
#   所以启动前显式校验 host：不是回环地址就**拒绝启动**（不是打个 warning）。
#   这是"防手滑"和"防默认配置"的分界线。
#
# ⚠️ CORS 为什么必须限定本机（步骤 9 加）
# ------------------------------------
#   这个库**零鉴权**。如果开 `allow_origins=["*"]`，那么**任何**网页上的
#   一段脚本都能 fetch http://127.0.0.1:8765/api/contacts，
#   把你的家庭成员名单、生日、邮箱全部读走 —— 这叫 DNS rebinding / CSRF 跨站读取。
#   Chrome 对 file:// 与 http://127.0.0.1 的跨源请求本来就带 Origin，
#   所以**默认不开 CORS 就已经够前端用**（同源访问不需要 CORS）。
#   这里显式开、且只允许回环来源，是为了将来「前端跑在 5173 开发端口」时不必改后端，
#   同时把口子收到最小：只信 127.0.0.1 / localhost / ::1 的任意端口。
#   ⚠️ 仍然**不带凭证**（allow_credentials=False）：本项目没有 cookie 会话，
#      带凭证只会让「将来有人加了登录」时的攻击面凭空变大。
#
# 不在本步做的事
# --------------
#   * 不做鉴权（只绑回环 + CORS 只信本机；真要跨机访问是步骤 12 的显式决策）
#   * 不挂 /docs 生产关闭？—— 本地工具，保留 /docs 方便自查接口

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../code/src/main
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../code/src
_CODE_DIR = os.path.dirname(_SRC_DIR)                           # .../code
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from api import browse as browseApi                               # noqa: E402
from api import contacts as contactsApi                           # noqa: E402
from api import dto as dto                                        # noqa: E402
from api import face as faceApi                                   # noqa: E402
from api import match as matchApi                                 # noqa: E402
from api import photoAction as photoActionApi                     # noqa: E402
from api import place as placeApi                                 # noqa: E402
from api import review as reviewApi                               # noqa: E402
from api import scan as scanApi                                   # noqa: E402
from api import settings as settingsApi                           # noqa: E402
from api import static as staticApi                               # noqa: E402

from fastapi import FastAPI                                      # noqa: E402
from fastapi.responses import JSONResponse                        # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("appMain", "appmain.log")

#: 允许绑定的回环地址白名单（其余一律拒绝启动）
LOOPBACK_HOSTS: frozenset = frozenset({"127.0.0.1", "localhost", "::1"})

#: 前端构建产物目录（没跑过 npm run build 时不存在，属正常）
#:
#: ⚠️ 这里必须是 `<仓库>/code/webserver/dist`，也就是**从 _SRC_DIR 再往上退一级**。
#:   原写法`os.path.dirname(_HERE_DIR)` 得到的是 `.../code/src`（不是 `.../code`，
#:   变量名的注释写着 `.../code`，值却不是 —— 注释与值不符的典型），
#:   于是拼出来的是 `.../code/src/webserver/dist`，永远不存在。
#:   为什么一直没被发现：拼错路径时 `os.path.isdir()` 为假，代码**安静地**
#:   退回「只返回 JSON 的根路由」，前端访问任何页面都是 404，
#:   而单测把 _WEB_DIST monkeypatch 成临时目录，恰好绕开了这个真实路径。
#:   （test_web_spa_fallback.py 里那条 realDist 部署冒烟用例就是为它补的。）
_WEB_DIST = os.path.abspath(os.path.join(_CODE_DIR, "webserver", "dist"))


class StartupConfigError(Exception):
    """启动配置非法（最典型：host 不是回环地址）"""


def _resolveWebFile(spaPath: str):
    """把 URL 路径安全地映射到 dist 里的真实文件；不存在或越界返回 None。

    ⚠️ 目录穿越必须在这里挡住：URL 里的 ``..`` 与反斜杠会让
    ``os.path.join`` 拼出 dist 之外的路径（``GET /../photoBrowser.ini``）。
    用 ``abspath`` 归一后再判断是否仍在 dist 内 —— 只判 ``startswith`` 是不够的，
    需要带上分隔符，否则 ``dist_evil`` 这种同前缀目录会被误判为在 dist 内。
    """
    if not spaPath:
        return None
    # URL 用的是 "/"，但 Windows 上 "\" 也能被某些客户端塞进来，统一拒掉
    if "\\" in spaPath or "\x00" in spaPath:
        return None
    parts = [seg for seg in spaPath.split("/") if seg not in ("", ".", "..")]
    if not parts:
        return None
    candidate = os.path.abspath(os.path.join(_WEB_DIST, *parts))
    distPrefix = _WEB_DIST.rstrip("\\/") + os.sep
    if not candidate.startswith(distPrefix):
        return None
    return candidate if os.path.isfile(candidate) else None


def assertLoopbackHost(host: str) -> str:
    """校验监听地址是回环，**非回环直接抛异常**（不是 warning）。

    这个库零鉴权，开0.0.0.0 等于把全家照片发到局域网。
    """
    text = "" if host is None else str(host).strip()
    if not text:
        return basicSettings.SERVER_HOST
    if text.lower() in LOOPBACK_HOSTS:
        return text
    raise StartupConfigError(
        "拒绝启动：host=%r 不是回环地址。\n"
        "  本项目服务**零鉴权**，只允许 127.0.0.1 / localhost / ::1（开发计划 §一 硬约束）。\n"
        "  确实需要跨机访问请显式改main/app.py 里的 LOOPBACK_HOSTS，那是你的决定，不是默认值。"
        % host)


def _startup(dbFile: str = None, photoRoot: str = None, thumbRoot: str = None) -> dict:
    """启动准备：落进程级覆盖、校验布局、建可写目录、显式指定库句柄、验表。

    返回实际使用的配置（/api/health 与启动横幅都读它）。
    """
    # 覆盖必须**先于** validate_layout / ensure_dirs 落下去：
    # 这两个函数都走 photo_dir() / thumb_dir() / db_file()，落了覆盖它们才跟着对。
    # 若在 createApp 里就落，这里只校验，等于同一件事说两遍；放在这里是为了
    # 「谁改了 paths 就必须重新走一遍启动自检」这条纪律只有一个落点。
    paths.setRootOverride(photo=photoRoot, thumb=thumbRoot, db=dbFile)
    layout = paths.validate_layout()
    paths.ensure_dirs()

    # DR-10：**必须显式传路径**。sqliteCommon.dbHandle() 无参调用绝不切库，
    # 但也正因如此，进程里第一次连库就得把目标说清楚 ——
    # 传了别的库再调query_*，不会被悄悄切回正式库。
    sqliteCommon.dbHandle(layout["db"])

    hasTable = sqliteCommon.chkTableExist("pb_photo")
    if not hasTable:
        _LOG.warning("启动自检: pb_photo 不存在，图片接口会一律 404。"
                     "先跑 tools\\build_db.py 与 tools\\scan_cli.py")
    config = dict(paths.all_paths())
    config["overrides"] = {k: v for k, v in paths.rootOverrides().items() if v}
    config["photoTable"] = hasTable
    return config


def createApp(dbFile: str = None, photoRoot: str = None, thumbRoot: str = None,
              mountWeb: bool = True, cors: bool = True) -> FastAPI:
    """造一个 FastAPI 实例。

    参数
    ----
    dbFile   : 库文件（缺省 paths.db_file()）。显式传是为了测试能指到临时库，
               也为了落实 DR-10。
    photoRoot / thumbRoot : 路径覆盖（缺省走 local_settings）。走
               paths.setRootOverride，所以 api层直接调 paths.photo_dir() /
               paths.thumb_dir() 也会跟着对 —— **不制造第二个"服务自己的根"**。
    mountWeb : 是否挂前端构建产物目录。测试里传False（不碰 webserver）。
    cors     : 是否挂 CORS 中间件。**只允许回环来源**，见文件头说明；
               测试里可传 False 少一层中间件。
    """
    # 覆盖在**建 app 时**就落下去（而不是等 lifespan）：这样任何在 createApp
    # 之后、启动之前读 paths 的代码（自定义中间件、路由依赖）拿到的也是对的库。
    if dbFile or photoRoot or thumbRoot:
        paths.setRootOverride(photo=photoRoot, thumb=thumbRoot, db=dbFile)

    appState = {"config": None}

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_application):
        # 用 lifespan 而不是已废弃的 on_event("startup")：
        # 启动准备（建可写目录 + 显式指定库句柄 + 验表）必须**在**开始收请求之前跑完，
        # lifespan 是官方推荐写法，也少一条 DeprecationWarning。
        appState["config"] = _startup(dbFile, photoRoot, thumbRoot)
        cfg = appState["config"]
        # 地点中文名的地理数据**在这里预热**（DR-29）。
        # 为什么不在第一次 /api/places 时才加载：那次要解压 5.7MB gzip
        # 并建 2834 个多边形 + 两棵 STRtree，实测约 1.4 秒。落在请求路径上，
        # 用户看到的是「点开地点页先卡一下」，而这个服务绑回环、单人本机用，
        # 启动时多花这点时间完全可接受。详见 placeNameZh.warmUp 的说明。
        # ⚠️ 预热失败**绝不影响启动**：它只会让 nameZh 为 NULL、界面回退英文。
        from processor.place import placeNameZh as placeNameZh
        zhState = placeNameZh.warmUp()
        appState["placeZh"] = zhState
        print("==== photo-browser 服务启动 ====")
        print("  photo    : %s（**只读**）" % cfg["photo"])
        print("  thumb    : %s（生成物，可随时重建）" % cfg["thumb"])
        print("  database : %s" % cfg["database"])
        print("  pb_photo : %s" % ("就绪" if cfg["photoTable"] else "缺失，先跑 build_db/scan"))
        print("  地点中文名: %s" % zhState.get("text", "?"))
        print("  CORS     : %s（仅回环来源）" % ("开" if cors else "关"))
        print("  监听     : http://%s:%d（仅回环）"
              % (basicSettings.SERVER_HOST, basicSettings.SERVER_PORT))
        print("")
        yield
        # ---- 收尾：顺序**不可颠倒**，每一步都在为下一步「清场」----
        #
        # ① 扫描调度器：**先停后台线程**。`resetScheduler()` 内部会等
        #    runUntilDone 退出（有超时）。必须排在关库之前 ——
        #    后台线程还握着库句柄时关库，会在扫描线程里抛一片
        #    「Cannot operate on a closed database」，而且那些异常
        #    出现在**另一个线程**，主线程只会看到「日志里莫名其妙多了一堆错」。
        from api import scan as scanApi
        scanApi.resetScheduler()
        #
        # ①之二 人脸识别调度器：**同样必须排在关库之前**，理由逐条同上。
        #      它与人脸提取的**进程池**是两层东西：resetScheduler 等的是
        #      后台线程，而那个线程里还套着 engine.face.pool 的
        #      ProcessPoolExecutor（子进程各自持一份模型权重，
        #      收尾要等它们把手上那批算完，否则 faceStore 的事务会落在半路）。
        from api import face as faceApi
        faceApi.resetScheduler()
        #
        # ② 缩略图线程池：`wait=True` —— 等**在途**的生成任务做完。
        #    ⚠️ 原来是 `wait=False`（不等待），这里改掉的原因：
        #    池里的 worker 会读原图 + 走 thumbStore 写盘 + 查 pb_photo，
        #    不等它们就往下关库，等于让一个正在用连接的工作线程
        #    撞上 close()。单线程测试看不出来，但服务退出时
        #    进程可能带着未释放的句柄/半个写盘文件退出。
        #    等它的代价是**退出慢一点**（最长一张缩略图的生成时间），
        #    换来的是「退出后盘上不会留半个文件、库里不会留半条记录」。
        from processor.media import thumbMaker as thumbMaker
        thumbMaker.shutdownPool(wait=True)
        #
        # ③ 最后关库。此时①②都已确认线程退出，
        #    `sqliteHandle.close()` 里那句「必须确保后台线程已停下」
        #    才有了实际保证（见它的 docstring）。
        from database.auto_generated import sqliteCommon as sqliteCommon
        sqliteCommon.closeDb()

    application = FastAPI(
        title="photo-browser",
        version=_VERSION,
        description="本地优先的照片浏览 + 人脸归类工具（只绑 127.0.0.1）",
        lifespan=lifespan,
    )

    # ---- 业务路由（/api 前缀）----
    # ⚠️ 顺序 = 「具体 -> 泛化」。FastAPI 按注册顺序匹配；虽然我们的路径
    #   互不歧义（/review/pending 与 /review/{faceCode}/assign 段数不同），
    #   但保持这个顺序能让人读代码时不必再想这件事。
    # ⚠️ `placeApi` **必须排在 `browseApi` 之前**（R5）：`/api/places*` 整个
    #   命名空间只在 place.py 一处注册，而它内部有 `{placeCode}` 通配段与
    #   静态段 `/places/rebuild` 的先后要求 —— 同模块内靠声明顺序保证。
    #   两个模块各注册一条 `/api/places` 时，后注册的那条会被**静默遮蔽**。
    application.include_router(staticApi.router, prefix="/api")
    application.include_router(scanApi.router, prefix="/api")
    application.include_router(faceApi.router, prefix="/api")
    # 人脸匹配（步骤 6 的 Web 入口）：把「待确认队列」里的脸自动归属给某人。
    # ⚠️ 它**不是** pb_scan_job 里的任务（无 jobCode/无批次），前缀必须自成一个
    #   命名空间 /api/match/**：/api/face/** 已经被 static.py 的 {faceCode}
    #   裁剪图路由占着，任何静态段都可能被它接走并 404（见 api/face.py 文件头）。
    application.include_router(matchApi.router, prefix="/api")
    application.include_router(placeApi.router, prefix="/api")
    application.include_router(browseApi.router, prefix="/api")
    application.include_router(photoActionApi.router, prefix="/api")
    application.include_router(reviewApi.router, prefix="/api")
    application.include_router(contactsApi.router, prefix="/api")
    application.include_router(settingsApi.router, prefix="/api")

    # ---- 统一错误体 { code, message }（含 404 / 422 / 未处理异常）----
    # ⚠️ 必须**在路由注册之后**装：FastAPI 的 exception_handler 与
    #   Starlette 的 ServerErrorMiddleware 对「未处理异常」的处理顺序有关，
    #   而业务异常处理器是逐个注册的，顺序不影响语义但影响可读性。
    dto.registerErrorHandlers(application)

    # ---- CORS：只信本机 ----
    if cors:
        _installCors(application)

    @application.get("/api/health", tags=["meta"], summary="健康检查")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "version": _VERSION,
                             "paths": appState["config"] or paths.all_paths(),
                             # ⚠️ 地点中文名数据源状态放在 /api/health 里而不是新建端点：
                             #   「界面全是英文」的第一诊断动作就是看健康检查，
                             #   多一个端点就多一处「排障时忘了看」的可能。
                             "placeZh": appState.get("placeZh") or {}})

    # ---- 静态资源（前端构建产物；步骤 10 之前目录不存在，属正常）----
    #挂载必须放在所有 /api 路由**之后**：挂在 "/" 上的兜底路由会吞掉根路径下的
    # 其它匹配，顺序反了会让 /api/* 打到前端服务器逻辑上。
    #
    # ⚠️ 为什么不能直接用 StaticFiles(directory=dist, html=True)（步骤 10 修）
    # ------------------------------------------------------------------
    #   html=True 只在「路径是目录」时回 index.html，**深链接不是目录**：
    #     GET /photos/P-000231 → 去找 dist\photos\P-000231（不存在）→ 404
    #   结果是：SPA 里点得进去，**刷新就 404**，F5 或收藏夹直接废掉。
    #   而 photo-browser 的 8 个路由里有 3 个带路径参数（照片/人物详情），
    #   深链接刷新是日常操作，不是边缘场景。
    #   所以这里自己实现 SPA 回退：命中真实文件就返回文件，否则一律回 index.html，
    #   把「路由判定」交给前端 router（vue-router 用的是 history 模式）。
    if mountWeb and os.path.isdir(_WEB_DIST):
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
        _indexFile = os.path.abspath(os.path.join(_WEB_DIST, "index.html"))
        _assetsDir = os.path.join(_WEB_DIST, "assets")
        # 带指纹的静态资源交给 StaticFiles（它有 ETag / Range / 缓存头，比手写强）
        if os.path.isdir(_assetsDir):
            application.mount("/assets", StaticFiles(directory=_assetsDir), name="assets")

        if not os.path.isfile(_indexFile):
            _LOG.warning("前端产物目录存在但没有 index.html: %s（先跑 npm run build）" % _WEB_DIST)

        @application.get("/{spaPath:path}", include_in_schema=False)
        def spaFallback(spaPath: str):
            """SPA history 回退：真实文件优先，其余交给前端路由。"""
            if spaPath.startswith("api/"):
                # 未匹配到的接口仍要保持统一错误体 {code, message}
                # ⚠️ spaPath 已经是**去掉前导斜杠**的相对路径（这里是 "api/nope"），
                #    所以拼的时候只能加一个斜杠 —— 写成 "/api/%s" 会输出
                #    「接口不存在：/api/api/nope」，排障时容易被当成路径真出错了
                return JSONResponse({"code": "NOT_FOUND",
                                     "message": "接口不存在：/%s" % spaPath},
                                    status_code=404)
            target = _resolveWebFile(spaPath)
            if target is not None:
                return FileResponse(target)
            if os.path.isfile(_indexFile):
                return FileResponse(_indexFile)
            return JSONResponse({"code": "WEB_DIST_EMPTY",
                                 "message": "前端未构建：缺少 dist/index.html"}, status_code=503)

        _LOG.info("已挂前端构建产物: %s（SPA history 回退已启用）" % _WEB_DIST)
    else:
        @application.get("/", include_in_schema=False)
        def _root() -> JSONResponse:
            return JSONResponse({
                "ok": True, "name": "photo-browser", "version": _VERSION,
                "docs": "/docs",
                "routers": ["static", "scan", "browse", "review",
                                          "contacts", "settings"],
            })

    return application


def _installCors(application) -> None:
    """挂 CORS 中间件：**只允许回环来源**（文件头解释了为什么必须限定）。

    来源用「正则匹配主机」而不是枚举具体端口：`http://127.0.0.1:5173` 与
    `http://localhost:5174` 都该放行（前端开发服务器端口不固定），
    但 `http://evil.example.com` 一个都不能进。
    端口**必须**显式带上 —— 否则 `http://127.0.0.1.evil.com` 也会命中
    「以 127.0.0.1 开头」这种朴素写法（这是 CORS 正则最常见的一个坑）。
    """
    from fastapi.middleware.cors import CORSMiddleware

    application.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$",
        allow_credentials=False,          # 本项目无 cookie 会话，带凭证只会放大攻击面
        allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS", "HEAD"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Accept-Ranges", "ETag", "X-Thumb-Cache"],
        max_age=600,
    )


#: uvicorn 入口用的模块级实例：`uvicorn main.app:app --host 127.0.0.1`
app = createApp()


def main(argv=None) -> int:
    """命令行启动入口：只绑 127.0.0.1"""
    import argparse

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="photo-browser",
        description="启动 photo-browser 服务（**只绑 127.0.0.1**）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--host", default=basicSettings.SERVER_HOST,
                        help="监听地址（只允许回环，缺省 %s）" % basicSettings.SERVER_HOST)
    parser.add_argument("--port", type=int, default=basicSettings.SERVER_PORT,
                        help="监听端口（缺省 %d）" % basicSettings.SERVER_PORT)
    parser.add_argument("--db", default=None, help="库文件（缺省 paths.db_file()）")
    parser.add_argument("--root", default=None,
                        help="原图根（缺省 paths.photo_dir()，**只读**）")
    parser.add_argument("--thumb", default=None, help="缩略图根（缺省 paths.thumb_dir()）")
    parser.add_argument("--no-web", action="store_true", help="不挂前端构建产物")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        host = assertLoopbackHost(args.host)
    except StartupConfigError as e:
        print("[Error] %s" % e, file=sys.stderr)
        return 2

    import uvicorn
    print("app.py _VERSION: %s" % _VERSION)
    print("监听           : http://%s:%d" % (host, args.port))
    try:
        application = createApp(dbFile=args.db, photoRoot=args.root,
                                thumbRoot=args.thumb, mountWeb=not args.no_web)
    except paths.PathLayoutError as e:
        print("[PathLayoutError] %s" % e, file=sys.stderr)
        return 1
    uvicorn.run(application, host=host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
