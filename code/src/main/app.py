#! /usr/bin/env python3
#encoding: utf-8

#Filename: app.py
#Description: photo-browser FastAPI 实例（步骤 4 最小版）
#
# 本步只做三件事
# -------------
#   1. FastAPI() + 挂 /api 路由（目前只有 api.static）
#   2. 挂静态资源（code\webserver\dist，步骤 10 才有内容，现在挂不上是正常的）
#   3. **只绑 127.0.0.1**（开发计划 §一 硬约束表；绝不开 0.0.0.0）
#
# 为什么把"只绑 127.0.0.1"写死在代码里而不是只写在启动命令里
# ----------------------------------------------------------
#   写在启动命令里的话，任何一次 `uvicorn main.app:app --host 0.0.0.0`
#   就能把整个私人照片库暴露到局域网 —— 而这个库没有登录态、没有任何鉴权。
#   所以启动前显式校验 host：不是回环地址就**拒绝启动**（不是打个 warning）。
#   这是"防手滑"和"防默认配置"的分界线。
#
# 不在本步做的事
# --------------
#   * 不做鉴权（只绑回环 + 无 CORS，浏览器同源访问已经够用；
#     真要跨机访问是步骤 12 的显式决策，不是默认行为）
#   * 不开 CORS 中间件（同源，不需要；真开了等于给任意网页开一个读你照片的口子）
#   * 不挂 /docs 生产关闭？—— 本地工具，保留 /docs 方便自查接口

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../main
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
_REPO_SRC = os.path.dirname(_HERE_DIR)                          # .../code
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from api import static as staticApi                               # noqa: E402

from fastapi import FastAPI                                      # noqa: E402
from fastapi.responses import JSONResponse                        # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("appMain", "appmain.log")

#: 允许绑定的回环地址白名单（其余一律拒绝启动）
LOOPBACK_HOSTS: frozenset = frozenset({"127.0.0.1", "localhost", "::1"})

#: 前端构建产物目录（步骤 10 之前不存在，属正常）
_WEB_DIST = os.path.abspath(os.path.join(_REPO_SRC, "webserver", "dist"))


class StartupConfigError(Exception):
    """启动配置非法（最典型：host 不是回环地址）"""


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
              mountWeb: bool = True) -> FastAPI:
    """造一个 FastAPI 实例。

    参数
    ----
    dbFile   : 库文件（缺省 paths.db_file()）。显式传是为了测试能指到临时库，
               也为了落实 DR-10。
    photoRoot / thumbRoot : 路径覆盖（缺省走 local_settings）。走
               paths.setRootOverride，所以 api层直接调 paths.photo_dir() /
               paths.thumb_dir() 也会跟着对 —— **不制造第二个"服务自己的根"**。
    mountWeb : 是否挂前端构建产物目录。测试里传False（不碰 webserver）。
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
        print("==== photo-browser 服务启动 ====")
        print("  photo    : %s（**只读**）" % cfg["photo"])
        print("  thumb    : %s（生成物，可随时重建）" % cfg["thumb"])
        print("  database : %s" % cfg["database"])
        print("  pb_photo : %s" % ("就绪" if cfg["photoTable"] else "缺失，先跑 build_db/scan"))
        print("  监听     : http://%s:%d（仅回环）"
              % (basicSettings.SERVER_HOST, basicSettings.SERVER_PORT))
        print("")
        yield
        # 收尾：关掉按需缩略图的线程池（不关也不影响正确性，但进程退出时干净些）
        from processor.media import thumbMaker as thumbMaker
        thumbMaker.shutdownPool(wait=False)
        from database.auto_generated import sqliteCommon as sqliteCommon
        sqliteCommon.closeDb()

    application = FastAPI(
        title="photo-browser",
        version=_VERSION,
        description="本地优先的照片浏览 + 人脸归类工具（只绑 127.0.0.1）",
        lifespan=lifespan,
    )

    # ---- 业务路由（/api 前缀）----
    application.include_router(staticApi.router, prefix="/api")

    @application.get("/api/health", tags=["meta"], summary="健康检查")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "version": _VERSION,
                             "paths": appState["config"] or paths.all_paths()})

    # ---- 404：统一 JSON，别让前端拿到 HTML 错误页 ----
    @application.exception_handler(404)
    def _notFound(request, exc):
        return JSONResponse({"ok": False, "errMsg": "接口或资源不存在",
                             "path": str(getattr(request, "url", ""))}, status_code=404)

    # ---- 静态资源（前端构建产物；步骤 10 之前目录不存在，属正常）----
    #挂载必须放在所有 /api 路由**之后**：StaticFiles(directory=..., html=True) 挂在 "/"
    # 会吞掉根路径下的其它匹配，顺序反了会让 /api/* 打到前端服务器逻辑上。
    if mountWeb and os.path.isdir(_WEB_DIST):
        from fastapi.staticfiles import StaticFiles
        application.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web")
        _LOG.info("已挂前端构建产物: %s" % _WEB_DIST)
    else:
        @application.get("/", include_in_schema=False)
        def _root() -> JSONResponse:
            return JSONResponse({
                "ok": True, "name": "photo-browser", "version": _VERSION,
                "docs": "/docs",
                "endpoints": ["/api/health", "/api/thumb/{photoCode}",
                              "/api/original/{photoCode}", "/api/face/{faceCode}",
                              "/api/media/stats"],
            })

    return application


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
