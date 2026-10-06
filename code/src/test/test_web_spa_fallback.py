#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_web_spa_fallback.py
#Description: 前端构建产物的挂载与 SPA history 回退（步骤 10）
#
# 覆盖
#   * 深链接（/photos/P-000231）刷新回 index.html，**不是 404**
#   * 真实文件优先：/assets/app-xxx.js 返回 js 本身，不会被 index.html 吃掉
#   * 未匹配到的 /api/* 仍返回统一错误体 {code, message}
#   * /docs、/openapi.json 不被兜底路由抢走
#   * 目录穿越（.. 、反斜杠、同前缀兄弟目录）一律读不到 dist 之外的文件
#
# ⚠️ 为什么要专门测这个（步骤 10 修掉的坑）
# ------------------------------------------------
#   原来挂的是 StaticFiles(directory=dist, html=True)。html=True 只在
#   「路径是目录」时回 index.html，而**深链接不是目录**：
#     GET /photos/P-000231 → 找 dist\photos\P-000231 → 404
#   表现是「点得进去、一刷新就 404」。8 个路由里 3 个带路径参数，
#   刷新和收藏夹是日常操作，必须当正经用例测，不能靠手动点两下确认。
#
# ⚠️ 为什么不复用 api_env 那个 TestClient
# ------------------------------------------------
#   api_env 用 `with TestClient(...)`，退出时会跑 lifespan 的收尾：
#   resetScheduler + shutdownPool + **closeDb()**。这里再开一个带 lifespan 的
#   客户端会在 api_env 之前把库关掉，后面所有用例拿到的都是已释放的句柄
#   （表现为进程级 access violation，不是 Python 异常）。
#   本文件不需要启动自检，所以**不进with**（不进就不跑 lifespan），
#   直接用 TestClient 发请求。

import os

import pytest
from fastapi.testclient import TestClient

from common import paths as paths
from main import app as appMod

#: 假的 index.html 内容：正文里放一个可辨识标记，断言时看它有没有被吐出来
_INDEX_HTML = '<!doctype html><html><head><title>photo-browser</title></head><body><div id="app"></div><!--SPAMARK--></body></html>'
#: 假的打包产物
_ASSET_JS = 'console.log("fake bundle");//ASSETMARK'
#: 放在 dist **外面**的机密文件：任何一条路径穿越把它读出来都算失败
_SECRET = "TOPSECRET-PHOTO-LIB"


@pytest.fixture
def webClient(tmp_path, monkeypatch):
    """造一个指向临时 dist 的客户端（不跑 lifespan，不碰库）。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text(_ASSET_JS, encoding="utf-8")
    # dist 之外的机密文件（放在 dist 的**兄弟目录**里，专门防同前缀绕过）
    (tmp_path / "secret.txt").write_text(_SECRET, encoding="utf-8")
    (tmp_path / "dist_evil").mkdir()
    (tmp_path / "dist_evil" / "secret.txt").write_text(_SECRET, encoding="utf-8")

    monkeypatch.setattr(appMod, "_WEB_DIST", str(dist))
    application = appMod.createApp(dbFile=str(tmp_path / "unused.db"),
                                  photoRoot=str(tmp_path / "photo"),
                                  thumbRoot=str(tmp_path / "thumb"),
                                  mountWeb=True, cors=False)
    client = TestClient(application)
    try:
        yield {"client": client, "dist": dist}
    finally:
        paths.clearRootOverride()


# ============================================================
# 一、history 回退：深链接刷新必须是 index.html
# ============================================================

def test_rootServesIndex(webClient):
    response = webClient["client"].get("/")
    assert response.status_code == 200
    assert "SPAMARK" in response.text


@pytest.mark.parametrize("deepPath", [
    "/photos/P-000231",
    "/people/P-zhongwen",
    "/review",
    "/scan-jobs",
    "/settings",
    "/photos/P-000231/some/deep/segment",
])
def test_deepLinkFallsBackToIndex(webClient, deepPath):
    """8 个路由（深链接那 3 个带上参数）刷新都必须回 index.html。"""
    response = webClient["client"].get(deepPath)
    assert response.status_code == 200, "深链接 %s 返回了 %d（期望 200）" % (deepPath, response.status_code)
    assert "SPAMARK" in response.text


# ============================================================
# 二、真实文件优先：带指纹的资源不能被 index.html 顶掉
# ============================================================

def test_assetReturnsRealFile(webClient):
    """回退只对「路由」生效；真实存在的文件必须原样返回。

    否则 index.html 会把所有 js/css 请求都答成 HTML，
    页面白屏而且报错信息是「Unexpected token '<'」，极难定位。
    """
    response = webClient["client"].get("/assets/app-abc123.js")
    assert response.status_code == 200
    assert "ASSETMARK" in response.text
    assert "SPAMARK" not in response.text


def test_assetMissingReturnsReal404(webClient):
    """资源不存在时必须是**真404**，不能回 index.html。

    指纹文件 404 说明 dist 与 index.html 不匹配（换了 dist 没重新打包 / CDN 缓存过期）。
    这时如果回 HTML，浏览器会把 HTML 当 JS 解析，报出
    「Unexpected token '<'」这种与真实原因毫无关系的语法错 —— 极难定位。
    所以 /assets 交给 StaticFiles：命中前缀就由它应答，404 就是 404。
    """
    response = webClient["client"].get("/assets/not-exist-999.js")
    assert response.status_code == 404
    assert "SPAMARK" not in response.text, "缺失的资源不能回 index.html"


# ============================================================
# 三、/api 仍然保持统一错误体，且不被前端兜底吞掉
# ============================================================

def test_unknownApiReturnsUnifiedError(webClient):
    response = webClient["client"].get("/api/no-such-endpoint")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    # ⚠️ **全等**，不用 in：拼错成「/api/api/no-such-endpoint」时子串断言照样通过，
    # 而这个文案是排障时唯一能照着敲的命令，错了比没有更糟
    assert body["message"] == "接口不存在：/api/no-such-endpoint"
    assert "SPAMARK" not in response.text, "接口 404 不该回落到 index.html"


def test_docsNotHijacked(webClient):
    """FastAPI 自带路由必须优先于兜底路由（否则 /docs 会被 SPA 吞掉）。"""
    response = webClient["client"].get("/docs")
    assert response.status_code == 200
    assert "SPAMARK" not in response.text

    schema = webClient["client"].get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "photo-browser"


# ============================================================
# 四、目录穿越：读不到 dist 之外的任何文件
# ============================================================

@pytest.mark.parametrize("attackPath", [
    "/../secret.txt",
    "/../../secret.txt",
    "/assets/../../secret.txt",
    "/..%2fsecret.txt",
    "/%2e%2e/secret.txt",
    "/assets/..%5c..%5csecret.txt",
    "/..\\secret.txt",
    "/../dist_evil/secret.txt",
])
def test_pathTraversalBlocked(webClient, attackPath):
    """URL 里的 .. 一律不允许穿出 dist。

    少了这道闸，`GET /../photoBrowser.ini` 这类请求就能把库配置、
    日志甚至同盘其它文件读出来 —— 这个服务零鉴权，读到就是全泄露。
    """
    response = webClient["client"].get(attackPath)
    assert _SECRET not in response.text, "路径穿越 %s 读到了 dist 之外的文件" % attackPath


def test_resolveWebFileUnit(webClient):
    """"_resolveWebFile 单独测：路径归一 + 前缀判定的边界都在这里。"""
    dist = webClient["dist"]
    resolve = appMod._resolveWebFile

    # 正常命中
    assert resolve("index.html") == os.path.abspath(str(dist / "index.html"))
    assert resolve("assets/app-abc123.js") == os.path.abspath(str(dist / "assets" / "app-abc123.js"))
    # 前导斜杠、多余斜杠、当前目录标记都不影响结果
    assert resolve("/index.html") == resolve("index.html")
    assert resolve("//assets//app-abc123.js") == resolve("assets/app-abc123.js")
    assert resolve("./index.html") == resolve("index.html")

    # 不存在 / 越界 / 非法字符 -> None（调用方回 index.html）
    assert resolve("") is None
    assert resolve("/") is None
    assert resolve("assets/nope.js") is None
    assert resolve("../secret.txt") is None
    assert resolve("..\\secret.txt") is None
    assert resolve("assets/../../secret.txt") is None
    assert resolve("index.html\x00.png") is None
    # 同前缀兄弟目录：dist_evil 必须被当成越界
    assert resolve("../dist_evil/secret.txt") is None
    # 目录本身不算文件
    assert resolve("assets") is None


# ============================================================
# 五、真实部署路径冒烟（**不monkeypatch**，这条就是为路径拼错而加的）
# ============================================================

def test_webDistPathPointsAtRealFrontendProject():
    """_WEB_DIST 必须等于 <仓库>/code/webserver/dist。

    ⚠️ 这条防的是 steps 1-9 就存在、直到步骤 10 收尾才暴露的路径拼错：
      当时写的是 ``os.path.join(os.path.dirname(_HERE_DIR), "webserver", "dist")``，
      而 _HERE_DIR 是 ``.../code/src/main``，dirname 只退一级得到 ``.../code/src``，
      于是拼出 ``.../code/src/webserver/dist`` —— 永远不存在。
      错得如此安静：isdir() 为假 → 退回只回 JSON 的根路由 → 前端整站 404，
      而上面所有用例都 monkeypatch 了这个路径，恰好把真实路径绕开。
    """
    expected = os.path.join(os.path.dirname(appMod._SRC_DIR), "webserver", "dist")
    assert appMod._WEB_DIST == os.path.abspath(expected)
    assert appMod._WEB_DIST.endswith(os.path.join("webserver", "dist"))


@pytest.mark.skipif(not os.path.isfile(os.path.join(appMod._WEB_DIST, "index.html")),
                    reason="还没跑过 npm run build（无真实 dist，跳过）")
def test_realDistServesDeepLink(tmp_path):
    """真实构建产物存在时，**不patch 任何路径**直接起服务，深链接必须回 index.html。"""
    # 按 **bytes** 比：文本模式读会把 CRLF 一律转成 LF，跟磁盘上的原文件对不上
    # （Vite 产出的 index.html 是 CRLF），那是测试自己写错，不是服务端问题
    with open(os.path.join(appMod._WEB_DIST, "index.html"), "rb") as handle:
        indexBytes = handle.read()
    application = appMod.createApp(dbFile=str(tmp_path / "unused.db"),
                                  photoRoot=str(tmp_path / "photo"),
                                  thumbRoot=str(tmp_path / "thumb"),
                                  mountWeb=True, cors=False)
    client = TestClient(application)
    try:
        for deepPath in ("/", "/photos/P-000231", "/people/P-zhongwen", "/review"):
            response = client.get(deepPath)
            assert response.status_code == 200, "%s -> %d" % (deepPath, response.status_code)
            assert response.content == indexBytes, "%s 没有回真实 index.html" % deepPath
        # 接口仍然不被前端吞掉
        assert client.get("/api/nope").status_code == 404
    finally:
        paths.clearRootOverride()