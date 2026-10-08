#! /usr/bin/env python3
#encoding: utf-8

#Filename: serve.py
#Description: 启动器 —— 跑服务之前先把「会让启动失败或静默不可用」的东西查一遍
#
# 为什么要有这个文件（不是「顺手加个脚本」）
# ------------------------------------------
#   照着 README 手敲启动命令，有**五个已知的坑**，其中两个是**静默**的：
#
#   ① **工作目录必须是 `code/src`**。`-m main.app` 靠 `sys.path[0]`（= 当前目录）
#      找 `main` 包，所以从 `code\` 或仓库根跑会直接
#      `ModuleNotFoundError: No module named 'main'`。
#      ⚠️ README 里曾经写的就是错的（`cd code; ... -m main.app`），已改。
#   ② **必须用 `code\.venv` 的解释器**。直接敲 `python` 会命中 WindowsApps
#      的基础解释器（3.13.x，site-packages 在用户目录），依赖与 .venv 不同 ——
#      本机实测因此让 vobject 相关用例假红过一次。
#   ③ **`local_settings.py` 被 .gitignore 忽略**，新 clone 的仓库只有
#      `.example`。缺它时 `paths.readSetting()` 走默认值（`d:\PhotoLib`），
#      **不报错**，于是服务起来了、页面全空��— 看起来像「扫不出照片」。
#   ④ **`dist/index.html` 不存在**时 `main/app.py` 安静地退回「只返回 JSON 的根
#      路由」，任何页面都是 404，而后端 `/api/health` 依然 ok:true。
#   ⑤ 端口被占时 uvicorn 报 `OSError: [Errno 10048]`，但 8765 上很可能正跑着
#      **另一个库的**服务（换过 `--db` 忘了换回来），于是你在浏览器里看着
#      另一个库的数据发愣。
#
#   这五条里③ ④ ⑤ 都不会「报错给你看」，所以逐条查、查完再启。
#
# 用法
# ----
#   仓库根的 start.cmd 会转调本文件（推荐；.cmd 不受 PowerShell 执行策略约束）：
#     start.cmd
#     start.cmd --dev
#     start.cmd --check-only
#     start.cmd --port 8799 --db d:\PhotoLib\db\step11-verify.db
#
#   也能直接调（此时工作目录任意，本文件自己把 src 加进 sys.path）：
#     code\.venv\Scripts\python.exe code\src\tools\serve.py --check-only
#
# ⚠️ **为什么入口是 .cmd 而不是 .ps1**（实测撞过）
# ---------------------------------------------
#   PowerShell 5.1 在执行策略为 Restricted / AllSigned 时**拒绝运行 .ps1**：
#     PS D:\...> .\start.ps1
#     无法加载文件 ...，因为在此系统上禁止运行脚本。（UnauthorizedAccess）
#   绕开的正确方式是**换一种文件类型**，而不是教用户 `Set-ExecutionPolicy` ——
#   那是机器/用户的安全设置，项目没理由为了自己的方便让人把它调低。
#   `.cmd` 由 cmd.exe 执行，不受该策略约束，双击也能用。
#   （顺带砍掉了「.ps1 用 -Dev、Python 用 --dev」的双套参数语法 —— 两套必然分叉。）
#
# ⚠️ 本文件**不新增任何配置口径**：端口/根目录一律读 basicSettings 与 local_settings，
#    覆盖只走 `--db/--root/--thumb`（落到 paths.setRootOverride，仅本进程有效）。
#    「配置在哪改」的唯一答案仍然是 code/src/config/local_settings.py。

import argparse
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))              # .../code/src/tools
_SRC = os.path.dirname(_HERE)                                    # .../code/src
_CODE = os.path.dirname(_SRC)                                    # .../code
_REPO = os.path.dirname(_CODE)                                   # 仓库根
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from common import paths as paths                                # noqa: E402
from config import basicSettings as basicSettings                # noqa: E402

_WEB_DIST = os.path.join(_CODE, "webserver", "dist")
_LOCAL_SETTINGS = os.path.join(_SRC, "config", "local_settings.py")
_LOCAL_SETTINGS_EXAMPLE = _LOCAL_SETTINGS + ".example"


def _out(text=""):
    """打印一行。

    ⚠️ 编码为什么要分两种情况处理（本机实测踩过）
    ---------------------------------------------
      · **重定向到文件/管道**（`> log.txt`、`Start-Process -RedirectStandardOutput`）：
        此时 Python 用 `locale.getpreferredencoding()`，本机是 **cp936**。
        不处理的话日志文件是 GBK，`Get-Content` / `git diff` 全是乱码。
        ⇒ 强制 UTF-8（无 BOM），这样任何工具读它都对。
      · **真实控制台**（双击 start.ps1、或在终端里跑）：
        Python 走 `io._WindowsConsoleIO`，它**认控制台的代码页**并正确渲染 ——
        强行改成 UTF-8 反而会让所有中文变乱码（窗口不会自动切代码页）。
        ⇒ 只补 `errors="replace"`。

      为什么要errors="replace"：控制台是 cp936，而 `⚠`(U+26A0)、`◐`、`✓`
      都不在 cp936 里。不给 replace 的话 print 会抛 **UnicodeEncodeError**,
      整个启动器在最后一步崩掉 —— 而崩掉的原因只是界面上有个符号。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    print(text, flush=True)


def _title(text):
    _out("")
    _out("== %s ==" % text)


def _warn(text):
    _out("  [注意] %s" % text)


def _err(text):
    _out("  [阻塞] %s" % text)


def _portBusy(port, host="127.0.0.1"):
    """端口是否已被占用（bind 试探；**只探 127.0.0.1**，不碰外部网卡）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        return False
    except OSError:
        return True
    finally:
        sock.close()


# ============================================================
# 一、启动前体检
# ============================================================

def preflight(port, dev, needWeb):
    """逐条检查，**返回致命问题列表**（非空就别启动）。

    刻意分成「阻塞」与「注意」两级：③ 缺 local_settings 会让服务**看起来正常**
    但扫不出照片，④ 缺 dist 会让页面全404 —— 这两条如果当成 warning，
    用户会以为服务起来了，然后用十分钟去找「为什么没有照片」。
    """
    fatal = []
    warn = []

    _title("体检")

    # ---- ① 前端产物 ----
    indexFile = os.path.join(_WEB_DIST, "index.html")
    if needWeb and not dev:
        if not os.path.isfile(indexFile):
            fatal.append("前端未构建：%s 不存在\n"
                         "       先跑：cd code\\webserver; npm install; npm run build"
                         % indexFile)
        else:
            _out("  [ok] 前端产物：%s" % indexFile)

    # ---- ② 本机配置 ----
    if not os.path.isfile(_LOCAL_SETTINGS):
        if os.path.isfile(_LOCAL_SETTINGS_EXAMPLE):
            fatal.append("缺本机配置：%s（该文件被 .gitignore 忽略，不入库）\n"
                         "       先复制一份并改PHOTO_ROOT：\n"
                         "         copy %s %s\n"
                         "         notepad %s"
                         % (_LOCAL_SETTINGS, _LOCAL_SETTINGS_EXAMPLE,
                            _LOCAL_SETTINGS, _LOCAL_SETTINGS))
        else:
            fatal.append("缺本机配置，且找不到模板 %s"
                         % _LOCAL_SETTINGS_EXAMPLE)
    else:
        _out("  [ok] 本机配置：%s" % _LOCAL_SETTINGS)

    # ---- ③ 路径布局 + 原图目录是否真的存在 ----
    try:
        layout = paths.validate_layout()
    except paths.PathLayoutError as e:
        fatal.append("路径布局非法：%s\n"
                     "       改 %s 里的 PHOTO_ROOT（照片/thumbnail/db 三个目录不能互相嵌套，"
                     "也不能等于 photo\\）" % (e, _LOCAL_SETTINGS))
        layout = None

    if layout:
        #⚠️ 键名是 `db`（validate_layout 的返回），不是 `database`（all_paths 的键）。
        #   两个函数对同一条路径用了不同键名，写错的话这里直接 KeyError。
        for key, text, mustExist in (("photo", "原图", True),
                                     ("thumb", "缩略图", False),
                                     ("db", "库", False)):
            target = layout[key]
            exists = os.path.isdir(target) if key != "db" else os.path.isfile(target)
            if mustExist and not exists:
                fatal.append("%s 目录不存在：%s\n"
                             "       这是**你自己的照片目录**，应用只读不建 —— "
                             "请把照片放进去，或改 %s 里的 PHOTO_ROOT"
                             % (text, target, _LOCAL_SETTINGS))
            elif key == "database" and not exists:
                warn.append("库文件还不存在：%s\n"
                            "         首次使用请先跑一次建库+扫描：\n"
                            "           python code\\src\\tools\\build_db.py\n"
                            "           python code\\src\\tools\\scan_cli.py --root <原图目录>"
                            % target)
            else:
                _out("  [ok] %-4s：%s" % (text, target))

    # ---- ④ 数据库里有没有照片 ----
    # ⚠️ 为什么还要查这一下：③ 与布局检查全过、服务也起来了，
    #   但库里 0 张照片时页面同样是空的，而这时**唯一说得清原因的地方**就是这里。
    if layout and os.path.isfile(layout["db"]):
        try:
            from database.auto_generated import sqliteCommon as sqliteCommon
            from database import queryCommon as queryCommon
            sqliteCommon.dbHandle(layout["db"])
            try:
                if sqliteCommon.chkTableExist("pb_photo"):
                    total = queryCommon.selectValue(
                        "SELECT COUNT(*) AS rowNum FROM pb_photo")
                    if int(total or 0) > 0:
                        _out("  [ok] 库中照片 %s 张" % total)
                    else:
                        warn.append("库里 0 张照片 —— 界面会有内容但列表空\n"
                                    "         跑一次扫描：python code\\src\\tools\\scan_cli.py")
            finally:
                sqliteCommon.closeDb()
        except Exception as e:                                  # noqa: BLE001
            warn.append("读库失败（不影响启动，但要留意）：%s" % e)

    # ---- ⑤ 端口 ----
    if _portBusy(port):
        fatal.append("端口 %d 已被占用\n"
                     "       先停掉旧进程；或换端口：--port 8799\n"
                     "       ⚠️ 8765 上很可能跑着**另一个库**的服务"
                     "（换过 --db 忘了换回来），"
                     "此时在浏览器里看到的是那个库的数据" % int(port))
    else:
        _out("  [ok] 端口 %d 空闲" % int(port))

    for one in warn:
        _warn(one)
    return fatal


# ============================================================
# 二、启动
# ============================================================

def _waitHealthy(port, host, timeout=30.0):
    """等服务真的能回/health —— 打印「起来了」之前先确认它真的起来了。

    不做这件事的话，「端口空闲 -> 打印地址 -> 实际因路径异常退出」这套顺序
    会让用户点开一个连不上的链接，还要回来读日志才知道刚才已经挂了。
    """
    import urllib.request
    url = "http://%s:%d/api/health" % (host, int(port))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.getcode() == 200:
                    return True
        except Exception:                                        # noqa: BLE001
            time.sleep(0.4)
    return False


def _startDevServer():
    """另起一个 Vite dev server（5173），并把输出丢到日志文件。

    ⚠️ 开发模式的正确姿势是**开 5173 而不是 8765**：
    前端改代码热更走的是 dev server；8765 上那份是**上一次 build 的快照**，
    在它上面改前端永远是「改了没反应」，很容易被误判成 Vite 缓存问题。
    """
    import subprocess
    webDir = os.path.join(_CODE, "webserver")
    if not os.path.isdir(os.path.join(webDir, "node_modules")):
        _err("前端依赖没装：%s 下没有 node_modules\n"
             "         先跑：cd code\\webserver; npm install" % webDir)
        return None
    logDir = os.path.join(_CODE, "log")
    os.makedirs(logDir, exist_ok=True)
    logFile = open(os.path.join(logDir, "vite-dev.log"), "wb")    # noqa: SIM115
    proc = subprocess.Popen(
        ["npm.cmd", "run", "dev"], cwd=webDir, stdout=logFile,
        stderr=subprocess.STDOUT, creationflags=getattr(subprocess,
                                                        "CREATE_NO_WINDOW", 0))
    return proc


def run(argv=None):
    parser = argparse.ArgumentParser(
        prog="serve.py", description="启动 photo-browser（先体检，再启动）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=basicSettings.SERVER_HOST,
                        help="监听地址（**只允许回环**，缺省 %s）"
                             % basicSettings.SERVER_HOST)
    parser.add_argument("--port", type=int, default=basicSettings.SERVER_PORT,
                        help="监听端口（缺省 %d）" % basicSettings.SERVER_PORT)
    parser.add_argument("--db", default=None, help="库文件（缺省按配置）")
    parser.add_argument("--root", default=None,
                        help="原图根（缺省按配置，**只读**）")
    parser.add_argument("--thumb", default=None, help="缩略图根（缺省按配置）")
    parser.add_argument("--dev", action="store_true",
                        help="同时起 Vite dev server，并提示打开 5173")
    parser.add_argument("--check-only", action="store_true", help="只做体检，不启动")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # ⚠️ 体检前先落路径覆盖，否则查的是「配置里的路径」而不是「本次要用的路径」。
    #    这条顺序反了会给出**看着对但其实没查**的体检结果。
    paths.setRootOverride(photo=args.root, thumb=args.thumb, db=args.db)

    needWeb = not args.dev
    fatal = preflight(args.port, args.dev, needWeb)
    if fatal:
        _title("无法启动")
        for one in fatal:
            _err(one)
        _out("")
        _out("  修完上面的问题再跑一次；只想看配置体检结果：--check-only")
        return 1
    if args.check_only:
        _title("体检通过（--check-only，未启动）")
        return 0

    # ---- 端口二次确认：体检与启动之间隔了几秒，可能刚被别人占了 ----
    if _portBusy(args.port):
        _err("端口 %d 在体检之后被占用（一般是另一个 photo-browser 刚起）"
             % args.port)
        return 1

    from main import app as appMain
    try:
        application = appMain.createApp(dbFile=args.db, photoRoot=args.root,
                                        thumbRoot=args.thumb, mountWeb=needWeb)
    except paths.PathLayoutError as e:
        _err("路径布局非法：%s" % e)
        return 1

    devProc = _startDevServer() if args.dev else None

    import uvicorn
    import threading

    def _serve():
        #reload=False：开发时改后端就重启 serve.py 一次。
        #   开发模式的热更交给前端 5173，后端开 reload 会与「单写入者」的
        #   进程模型打架（重载时另起进程，库句柄会指向两个进程）。
        uvicorn.run(application, host=args.host, port=args.port, log_level="info")

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    if not _waitHealthy(args.port, args.host):
        _err("服务没能在 30s 内通过 /api/health 自检，已中止（Ctrl+C 退出）")
        if devProc:
            devProc.terminate()
        return 1

    _title("已就绪")
    _out("  界面：http://%s:%d" % (args.host, args.port))
    _out("  接口：http://%s:%d/docs" % (args.host, args.port))
    _out("  健康：http://%s:%d/api/health" % (args.host, args.port))
    if args.dev:
        _out("")
        _out("  ⚠️ 开发模式请打开 **http://localhost:5173**（Vite dev server）")
        _out("     8765 上那份是上一次 npm run build 的快照，在它上面改前端看不到效果")
        _out("     dev 日志：%s" % os.path.join(_CODE, "log", "vite-dev.log"))
    _out("")
    _out("  Ctrl+C 停止。**备份/恢复必须先停服务**（见 README「备份与恢复」）")

    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        _out("")
        _out("正在停止…")
    finally:
        if devProc:
            devProc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(run())