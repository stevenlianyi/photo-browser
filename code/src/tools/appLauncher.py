#! /usr/bin/env python3
#encoding: utf-8

#Filename: appLauncher.py
#Description: photo-browser **桌面版唯一入口**（PyInstaller 冻结产物的 exe 就是它）
#
# 它做的事（顺序**不可颠倒**）
# ------------------------
#   ① `multiprocessing.freeze_support()` —— 必须是 `__main__` 里的**第一行**
#   ② 定位/生成外置配置 `app.ini`（用户唯一能改的配置出口，见 config/appIni.py）
#   ③ 落命令行路径覆盖 → 写启动横幅（把"这次到底用的哪套路径"打印出来）
#   ④ **可写性检查**（用户可能把程序装到 Program Files）
#   ⑤ **自动建库 / 补表**（幂等；见 main.cli.ensureDb）
#   ⑥ 端口 / host 校验（都给**可读**错误，不抛堆栈）
#   ⑦ 起服务 → 等 /api/health 通 → 按需开浏览器
#
# ⚠️ 为什么需要这个文件（`tools/serve.py` 不够用）
# ---------------------------------------------
#   serve.py 是**源码开发者**的启动器：它靠"缺 local_settings.py 就阻塞"
#   来防「服务起来了但扫不出照片」，靠"跑 python xxx.py"来指路。
#   这两条对**打包用户**全是错的：
#     · 他没有 Python（任何 `python xxx.py` 都是废纸）；
#     · 他也没有 local_settings.py（那是包内部件）。
#   所以 exe 必须有一个自己的入口，而不是把 serve.py 硬塞进 exe。
#
# ⚠️ 为什么 `freeze_support()` 必须第一行、且必须在 `__main__` 保护内
# ----------------------------------------------------------------
#   本项目的多进程是 spawn（`engine/face/pool.py:564`、
#   `processor/media/thumbMaker.py:499`）。冻结后子进程是**再跑一遍这个 exe**，
#   命令行里带着 `--multiprocessing-fork`。若不先 `freeze_support()`，
#   那个参数会一路走到 `argparse`，得到
#     ｀error: unrecognized arguments: --multiprocessing-fork｀
#   然后每个子进程立刻退出 —— 表现为「人脸提取一张都没跑成」，
#   而主进程那边只看到"子进程全部退出"。这是 Windows 冻结的头号崩溃源。
#
# ⚠️ 这里**不写业务逻辑**：本文件只做「进程模型 / 路径解析 / 配置读取」三件事，
#   一行 DDL、一条业务查询都不许出现在这里（建库走 main.cli.ensureDb）。
#
# 用法
# ----
#   photo-browser.exe                    起服务 + 开浏览器（桌面快捷方式用的就是这个）
#   photo-browser.exe --init-db          只建库/补表，然后退出（**兜底通道**）
#   photo-browser.exe --check-only       只做启动自检，不起服务
#   photo-browser.exe --config D:\x\app.ini
#   photo-browser.exe --port 8799 --no-browser
#   photo-browser.exe --init-db --config D:\x\app.ini   指到另一个库做维护

import argparse
import multiprocessing
import os
import socket
import sys
import threading
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))              # .../code/src/tools
_SRC_DIR = os.path.dirname(_HERE_DIR)                              # .../code/src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

_VERSION = "20261011"

_BANNER_WIDTH = 78


class LaunchError(Exception):
    """启动前置检查失败（**都是用户能看懂并照做的**，不带堆栈）"""


# ============================================================
# 一、输出（编码 + 视觉分层）
# ============================================================

def _fixConsole() -> None:
    """让中文在两种场合都能正确显示。

    ⚠️ 为什么分两种情况（实测）：重定向到文件/管道时用 UTF-8（否则是 GBK，
       用文本工具看是乱码）；真实控制台只补 `errors="replace"` ——
       强行改成 UTF-8 会让**所有中文变乱码**（窗口不会跟着切代码页）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _out(text: str = "") -> None:
    try:
        print(text, flush=True)
    except (UnicodeEncodeError, OSError):
        pass


def _title(text: str) -> None:
    _out("")
    _out("== %s %s" % (text, "=" * max(0, _BANNER_WIDTH - len(text) - 4)))


def _bad(text: str) -> None:
    _out("  [阻塞] %s" % text)


def _note(text: str) -> None:
    _out("  [注意] %s" % text)


# ============================================================
# 二、可写性检查（§3.2）
# ============================================================

def _writableProbe(targetDir: str) -> str:
    """真的写一个探测文件再删掉（**不是** `os.access`）。

    ⚠️ 为什么不能用 `os.access(path, os.W_OK)`：
       在 Windows 上它对「ACL 允许但进程未被提权」的目录（Program Files）
       会因为 UAC 虚拟化/继承权限而**返回 True**，而真正写入时才失败 ——
       于是"检查通过、运行到一半才炸"，而且炸在写缩略图的地方，
       离原因（目录不可写）很远。写一个文件是唯一可信的判定。
    """
    probe = os.path.join(targetDir, ".pb_write_probe_%d_%d" % (os.getpid(), int(time.time() * 1000)))
    try:
        with open(probe, "w", encoding="ascii") as handle:
            handle.write("ok")
    except OSError as e:
        return "%s（%s: %s）" % (targetDir, type(e).__name__, e)
    finally:
        try:
            if os.path.isfile(probe):
                os.remove(probe)
        except OSError:
            pass
    return ""


def checkWritable(paths) -> tuple:
    """检查 photo_root / thumb / db 是否可写。返回 `(blockingMessages, notes)`。

    检查顺序（**每一层都有它的理由，别合并**）
    ----------------------------------------
      1. `photo_root` 不存在 -> **创建**（只创建到该层；`photo\\` **不建**）
         —— 用户把「照片根」指到一个还没建的目录是常见且合理的；
            而 `<photo_root>\\photo` 是**他自己的原图区**，程序往里建目录
            等于擅自改动别人的照片库结构（paths.py 的硬约束 1）。
      2. 布局校验（三目录互不嵌套 / 不等于 photoRoot）—— DR-8。
      3. 建可写目录（thumb / db / imports / exports）与 backup。
      4. 对 photo_root / thumb / db **实际写探测文件**。
      5. `<photo_root>\\photo` 不存在或为空 -> **不报错**，给首次使用引导。
    """
    blocking = []
    notes = []

    try:
        photoRoot = paths.photo_root()
    except paths.PathLayoutError as e:
        blocking.append("照片库根目录未配置：%s\n"
                        "       请改 `%s` 里的 `[paths] photo_root`"
                        % (e, _iniPathForMessage()))
        return blocking, notes

    if not os.path.isdir(photoRoot):
        try:
            os.makedirs(photoRoot, exist_ok=True)
            notes.append("照片库根目录不存在，已创建：%s" % photoRoot)
        except OSError as e:
            blocking.append("照片库根目录不存在且无法创建：%s\n"
                            "       (%s: %s)\n"
                            "       请改 `%s` 里的 `[paths] photo_root` 指向一个有写权限的目录"
                            "（例如 `D:\\PhotoLib`），或换一个安装位置重装"
                            % (photoRoot, type(e).__name__, e, _iniPathForMessage()))
            return blocking, notes

    try:
        paths.validate_layout()
    except paths.PathLayoutError as e:
        blocking.append("路径布局非法：%s\n"
                        "       改 `%s` 里的 `[paths]`：photo / thumb / db 三者"
                        "不能互相嵌套，也不能等于 photo_root 本身"
                        % (e, _iniPathForMessage()))
        return blocking, notes

    try:
        paths.ensure_dirs()
    except OSError as e:
        blocking.append("创建数据目录失败（%s: %s）\n"
                        "       photo_root = %s\n"
                        "       请改 `%s` 里的 `[paths] photo_root`"
                        % (type(e).__name__, e, photoRoot, _iniPathForMessage()))
        return blocking, notes

    # backup 不在 paths.writable_dirs()（那是"三条主路径"的清单），
    # 但安装后的布局里它是一等公民（见 README「备份与恢复」）。
    # 提前建出来还有一个好处：卸载时"photo/thumb/db/backup 全在"这条红线
    # **在用户一次都没备份过的情况下也能被验证**。
    try:
        os.makedirs(paths.backup_root(), exist_ok=True)
    except OSError as e:
        notes.append("备份目录无法创建（%s）：%s —— 备份功能会不可用" % (paths.backup_root(), e))

    checked = [("photo_root", photoRoot),
               ("thumb", paths.thumb_dir()),
               ("db", paths.db_dir())]
    for label, target in checked:
        if not os.path.isdir(target):
            blocking.append("%s 目录不存在：%s" % (label, target))
            continue
        problem = _writableProbe(target)
        if problem:
            blocking.append("`%s` 当前不可写：%s\n"
                            "       请改 `%s` 里的 `[paths] photo_root` 指向一个有写权限的目录"
                            "（例如 `D:\\PhotoLib`），或换一个安装位置重装"
                            % (label, problem, _iniPathForMessage()))
    return blocking, notes


def checkPhotoDir(paths) -> list:
    """`<photo_root>\\photo` 的首次使用引导（**返回 note，不是错误**）。

    ⚠️ 必须**明确写出完整路径**：`photo_dir` 是**派生**的（DR-8），
       用户不能把 `photo_root` 直接指向一个已有的照片目录 ——
       除非照片恰好在 `<那个目录>\\photo\\` 之下。
       只说「把照片放进照片目录」等于没说：他改的就是"照片目录"那一项。
    """
    try:
        photoDir = paths.photo_dir()
    except Exception as e:                                       # noqa: BLE001 - 兜底
        return ["无法解析原图目录：%s" % e]
    if not os.path.isdir(photoDir):
        return ["还没找到照片目录：`%s`\n"
                "       请把你的照片放进 **`%s`** 之后，回到界面「扫描任务」页开始扫描。\n"
                "       ⚠️ 照片必须放在 `<photo_root>\\photo\\` 之下（原图区只读，"
                "程序不会替你创建这个目录）。" % (photoDir, photoDir)]
    try:
        empty = not os.listdir(photoDir)
    except OSError:
        empty = False
    if empty:
        return ["照片目录是空的：`%s`\n"
                "       把你的照片放进去之后，回到界面「扫描任务」页开始扫描。" % photoDir]
    return []


def _iniPathForMessage() -> str:
    """错误信息里要给出的 app.ini **完整路径**（不可写提示的"下一步"）。"""
    try:
        from config import appIni as appIni
        return appIni.iniPath() or "(未定位到 app.ini)"
    except Exception:                                            # noqa: BLE001 - 兜底
        return "(未定位到 app.ini)"


# ============================================================
# 三、建库 / 补表
# ============================================================

def initDatabase(dbFile: str = None, quiet: bool = False, verbose: bool = True) -> str:
    """建库 / 补表（**幂等**）。走 `main.cli.ensureDb`，本文件不写一条 DDL。"""
    from main import cli as cli
    from common import paths as paths
    from database.auto_generated import sqliteCommon as sqliteCommon

    target = cli.ensureDb(dbFile=dbFile, quiet=quiet, migrate=True)
    sqliteCommon.dbHandle(target)
    missing = [name for name in sqliteCommon.TABLE_ORDER
               if not sqliteCommon.chkTableExist(name)]
    if missing:
        raise LaunchError("建库后仍缺表：%s（DDL 不完整？）" % ", ".join(missing))
    if verbose:
        _out("  库文件   : %s" % target)
        _out("  表       : %d/%d 张齐全"
             % (len(sqliteCommon.TABLE_ORDER), len(sqliteCommon.TABLE_ORDER)))
    return target


def tableList(dbFile: str) -> list:
    """`sqlite_master` 里 `pb_*` 的表名清单（验收第 6/7/8 条要贴这个）。"""
    from database.auto_generated import sqliteCommon as sqliteCommon
    handle = sqliteCommon.dbHandle(os.path.abspath(dbFile))
    handle.executeRead("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name LIKE 'pb\\_%' ESCAPE '\\' ORDER BY name;")
    return [str(row.get("name")) for row in handle.fetchAll()]


def countRows(dbFile: str, tableName: str) -> int:
    """某张表的行数（含软删行；只做维护/验收统计用）。"""
    from database.auto_generated import sqliteCommon as sqliteCommon
    handle = sqliteCommon.dbHandle(os.path.abspath(dbFile))
    handle.executeRead("SELECT COUNT(*) AS rowNum FROM %s;" % tableName)
    row = handle.fetchOne() or {}
    return int(row.get("rowNum") or 0)


# ============================================================
# 四、端口 / host
# ============================================================

def portBusy(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否被占（只 bind 回环，不碰外部网卡）"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def waitHealthy(port: int, host: str, timeout: float = 60.0) -> bool:
    """等 /api/health 真的返回 200 —— 打印"已就绪"之前先确认它真的起来了。"""
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


# ============================================================
# 五、参数与主流程
# ============================================================

def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photo-browser",
        description="photo-browser 本地照片浏览 / 人脸归类（只绑 127.0.0.1，零鉴权）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带参数启动 = 起服务 + 自动打开浏览器。")
    parser.add_argument("--config", default=None, metavar="<path>",
                        help="外置配置 app.ini 的路径（缺省：<exe目录>\\app.ini）")
    parser.add_argument("--init-db", action="store_true",
                        help="只建库 / 补表然后退出（**兜底通道**：自动建表失败时用）")
    parser.add_argument("--check-only", action="store_true",
                        help="只做启动自检（路径 / 可写性 / 建库 / 端口），不起服务")
    parser.add_argument("--host", default=None, help="监听地址（**只允许回环**）")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    parser.add_argument("--db", default=None, help="库文件（覆盖 app.ini）")
    parser.add_argument("--root", default=None, help="原图根（覆盖 app.ini，**只读**）")
    parser.add_argument("--thumb", default=None, help="缩略图根（覆盖 app.ini）")
    parser.add_argument("--no-browser", action="store_true",
                        help="启动后不自动打开浏览器")
    parser.add_argument("--no-auto-db", action="store_true",
                        help="跳过「启动前自动建库/补表」（排障用，正常别加）")
    # ---- 数据维护（R10：打包用户没有 Python，`tools\backup.py` 用不了）----
    parser.add_argument("--backup", action="store_true",
                        help="停服务后备份 db\\ + thumb\\（**不含 photo\\ 原图**），然后退出")
    parser.add_argument("--restore", default=None, metavar="<备份编号>",
                        help="从某个备份还原 db\\ + thumb\\（会先自动另存现状），然后退出")
    parser.add_argument("--list-backups", action="store_true",
                        help="列出已有备份，然后退出")
    parser.add_argument("--yes", action="store_true",
                        help="配合 --restore：跳过交互确认（脚本化用）")
    # ---- 缩略图批量生成 ----
    # 为什么也要暴露：`make_thumbs_bulk` 走的是 **ProcessPoolExecutor**，
    # 与服务按需生成缩略图（线程池）是**两条代码路径**。
    # 打包用户没 Python、跑不了 tools\gen_thumbs.py，于是那条进程池路径
    # 在打包产物里**永远不会被走到** —— 一旦它在冻结环境下有问题，
    # 只有等到某个用户"想预生成一批缩略图"时才会炸。
    parser.add_argument("--gen-thumbs", action="store_true",
                        help="批量预生成缩略图（进程池），然后退出")
    parser.add_argument("--gen-size", type=int, nargs="+", default=None, metavar="PX",
                        help="缩略图宽度，可多个：--gen-size 200 400 800")
    parser.add_argument("--gen-workers", type=int, default=None, metavar="N",
                        help="进程数（缺省 CPU-1）")
    parser.add_argument("--gen-limit", type=int, default=0, metavar="N",
                        help="只处理前 N 张（0=全部）")
    parser.add_argument("--gen-rebuild", action="store_true",
                        help="忽略已有缩略图，全部重生成")
    return parser


def runBackupCommand(args) -> int:
    """把 `--backup / --restore / --list-backups` 转发给 `tools/backup.py`。

    ⚠️ 为什么不另写一套备份逻辑：备份/恢复是**覆盖式**的，
       两套实现一定会分叉（比如一处拷了 `-wal` 另一处没拷），
       而分叉的代价是"恢复后少几百条记录且不报错"。
       ⇒ 只做参数转发，一行备份逻辑都不复制。
    """
    from tools import backup as backupTool
    if args.list_backups:
        argv = ["list"]
    elif args.restore:
        argv = ["restore", "--code", str(args.restore)]
        if args.yes:
            argv.append("--yes")
    else:
        argv = ["backup"]
    _title("备份 / 恢复（服务必须先停）")
    _out("  转发给 tools/backup.py：%s" % " ".join(argv))
    return int(backupTool.main(argv))


def runGenThumbsCommand(args) -> int:
    """把 `--gen-thumbs` 转发给 `tools/gen_thumbs.py`（**只转发，不复制逻辑**）。"""
    from tools import gen_thumbs as genThumbs
    argv = ["gen"]
    if args.gen_size:
        argv.append("--size")
        argv += [str(one) for one in args.gen_size]
    if args.gen_workers:
        argv += ["--workers", str(args.gen_workers)]
    if args.gen_limit:
        argv += ["--limit", str(args.gen_limit)]
    if args.gen_rebuild:
        argv.append("--rebuild")
    if args.db:
        argv += ["--db", str(args.db)]
    if args.root:
        argv += ["--root", str(args.root)]
    if args.thumb:
        argv += ["--thumb", str(args.thumb)]
    _title("批量预生成缩略图（进程池）")
    _out("  转发给 tools/gen_thumbs.py：%s" % " ".join(argv))
    return int(genThumbs.main(argv))


def _printBanner(paths, layout, extra: dict) -> None:
    _out("")
    _out("  photoRoot : %s" % layout.get("photoRoot", "?"))
    _out("  photo     : %s  （**只读**）" % layout.get("photo", "?"))
    _out("  thumb     : %s" % layout.get("thumb", "?"))
    _out("  database  : %s" % layout.get("database", "?"))
    _out("  backup    : %s" % paths.backup_root())
    for key in ("iniPath", "iniState", "logDir", "mode"):
        if extra.get(key):
            _out("  %-9s : %s" % (key, extra[key]))


def runServer(dbFile: str, photoRoot: str, thumbRoot: str, host: str, port: int,
              openWeb: bool) -> int:
    """起服务（uvicorn）→ 等健康检查 → 按需开浏览器 → 阻塞到 Ctrl+C。"""
    from main import app as appMain

    try:
        application = appMain.createApp(dbFile=dbFile, photoRoot=photoRoot,
                                        thumbRoot=thumbRoot, mountWeb=True)
    except Exception as e:                                       # noqa: BLE001
        raise LaunchError("创建服务实例失败：%s: %s" % (type(e).__name__, e))

    import uvicorn

    def _serve():
        # reload 必须关：单写入者的进程模型 + 多进程 spawn，
        # 开了 reload 会另起进程，库句柄会指向两个进程（serve.py 里同样的理由）。
        uvicorn.run(application, host=host, port=port, log_level="info")

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    if not waitHealthy(port, host):
        raise LaunchError("服务没能在 60s 内通过 /api/health 自检\n"
                          "       请查看日志目录里的 appmain.log；"
                          "常见原因：库文件损坏 / 端口被占用 / 模型文件缺失")

    _title("已就绪")
    _out("  界面：http://%s:%d" % (host, port))
    _out("  接口：http://%s:%d/docs" % (host, port))
    _out("  健康：http://%s:%d/api/health" % (host, port))
    _out("")
    _out("  Ctrl+C 停止。**关闭这个窗口 = 停止服务**。")
    _out("  ⚠️ 备份 / 恢复必须先停服务（否则会拷到一半的库）。")

    if openWeb:
        try:
            import webbrowser
            webbrowser.open("http://%s:%d" % (host, port))
            # 打印出来是为了**可验证**：验收「双击快捷方式 → 自动开浏览器」
            # 时，这条输出就是"确实调用了"的证据（而不是靠肉眼盯桌面）。
            _out("  已请求打开浏览器：http://%s:%d" % (host, port))
        except Exception as e:                                   # noqa: BLE001 - 兜底
            # 打不开浏览器**绝不能**影响服务：代价只是"少点一次链接"
            _note("打开浏览器失败（服务不受影响，请手动访问上面的地址）：%s" % e)

    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        _out("")
        _out("正在停止…（等后台任务收尾，最长一条缩略图的时间）")
    return 0


def main(argv=None) -> int:
    _fixConsole()
    args = buildParser().parse_args(argv if argv is not None else sys.argv[1:])

    from common import paths as paths
    from common import resourcePath as resourcePath
    from config import appIni as appIni
    from config import basicSettings as basicSettings

    # ---- ① 配置必须在任何读取之前定位好（appIni 的缓存是按这个顺序建立的）----
    if args.config:
        got = appIni.setExplicitPath(args.config)
        if not os.path.isfile(got):
            # 显式指定却不存在 = 明确的用户错误，**不静默回落**
            # （静默回落会让"我改了配置怎么没生效"变成一场猜谜）
            _bad("--config 指定的文件不存在：%s" % got)
            return 2
    generated = appIni.ensureIni(port=basicSettings.serverPort(),
                                 batchSize=basicSettings.batchSize())
    iniPath = appIni.loadedPath() or appIni.iniPath()
    iniState = ("本次新建" if appIni.generatedPath()
                else "已存在" if appIni.loadedPath() else "未使用（走默认值）")

    # ---- ② 命令行路径覆盖（优先于 app.ini）----
    # ⚠️ 必须**先于** validate_layout / ensure_dirs 落下去（它们都走 paths 的解析）
    paths.setRootOverride(photo=args.root, thumb=args.thumb, db=args.db)

    # ---- ②之二 数据维护子命令（与起服务互斥，先处理掉）----
    if args.gen_thumbs:
        return runGenThumbsCommand(args)

    if args.backup or args.restore or args.list_backups:
        # ⚠️ 备份/恢复**必须在服务停止时做**：WAL 模式下拷活着的库会拿到
        #    「主库 + 未 checkpoint 的 -wal」的中间态 —— 备份成功、零报错，
        #    恢复后少几百条记录。这里用端口探测给出**明确警告**（不阻断：
        #    端口被占也可能是别的程序；最终判断权留给用户）。
        # `--list-backups` 只读清单目录，不受 WAL 影响 —— 不对它报警告
        # （无谓的警告会让真正的警告失去分量）
        if (not args.list_backups
                and portBusy(int(args.port or basicSettings.serverPort()),
                             str(args.host or basicSettings.serverHost()))):
            _note("端口上还跑着服务 —— 请**先关掉 photo-browser 的窗口**再执行本命令，"
                  "否则备份可能拿到未 checkpoint 的中间态")
        return runBackupCommand(args)

    mode = "打包(onedir)/exe" if resourcePath.isFrozen() else "源码模式"
    _title("photo-browser 启动自检")
    _out("  版本      : %s" % _VERSION)
    _out("  运行形态  : %s" % mode)
    _out("  外置配置  : %s  [%s%s]"
         % (iniPath or "(无)", iniState,
            ("，编码 " + appIni.loadedEncoding()) if appIni.loadedEncoding() else ""))
    _out("  日志目录  : %s" % resourcePath.logDir())
    # 同时存在多份 app.ini 时**必须说一句**：按优先级只有第一份生效，
    # 而"我改的那份没生效"是排障时最费时间的一类问题（没有任何报错）。
    _allIni = appIni.existingPaths()
    if len(_allIni) > 1:
        _note("找到多份 app.ini，**只有第一份生效**（其余被忽略）：")
        for _index, _one in enumerate(_allIni):
            _out("        %d) %s%s" % (_index + 1, _one, "  <- 生效" if _index == 0 else ""))

    # ---- ③ 路径 + 可写性 ----
    blocking, notes = checkWritable(paths)
    if blocking:
        _title("无法启动")
        for one in blocking:
            _bad(one)
        _out("")
        _out("  修完之后重新双击快捷方式即可；只想看自检结果：--check-only")
        return 1
    for one in notes:
        _note(one)

    try:
        layout = paths.all_paths()
        layout["photoRoot"] = paths.photo_root()
    except paths.PathLayoutError as e:
        _bad("路径配置非法：%s" % e)
        return 1

    extra = {"iniPath": iniPath, "iniState": iniState,
             "logDir": resourcePath.logDir(), "mode": mode}
    _printBanner(paths, layout, extra)

    # ---- ④ 建库 / 补表（幂等）----
    if args.init_db or not args.no_auto_db:
        try:
            initDatabase(dbFile=args.db, quiet=not args.init_db, verbose=args.init_db)
        except Exception as e:                                   # noqa: BLE001
            _title("建库 / 补表失败")
            _bad("%s: %s" % (type(e).__name__, e))
            _out("")
            _out("  可先看日志目录里的 builddb.log；"
                 "若库文件已损坏，可把它改名后重跑 `--init-db` 重建空库（**照片原图不受影响**）")
            return 1

    if args.init_db:
        try:
            names = tableList(args.db or paths.db_file())
        except Exception as e:                                   # noqa: BLE001
            names = ["(列表读取失败：%s)" % e]
        _title("建库 / 补表完成")
        _out("  库文件 : %s" % (args.db or paths.db_file()))
        _out("  表(%d) : %s" % (len(names), ", ".join(names)))
        _out("")
        _out("  照片原图目录（**只读，本命令绝不改动**）：%s" % paths.photo_dir())
        return 0

    # ---- ⑤ 首次使用引导（**不是错误**）----
    for one in checkPhotoDir(paths):
        _note(one)

    # ---- ⑥ 端口 / host ----
    host = str(args.host or basicSettings.serverHost()).strip()
    port = int(args.port or basicSettings.serverPort())
    if host.lower() not in basicSettings.LOOPBACK_HOSTS:
        _title("无法启动")
        _bad("host=%r 不是回环地址。\n"
             "       本项目服务**零鉴权**，只允许 127.0.0.1 / localhost / ::1 —— "
             "开 0.0.0.0 等于把全家照片发到局域网。\n"
             "       请改 `%s` 里的 `[server] host`" % (host, iniPath or "app.ini"))
        return 1
    if portBusy(port, host):
        _title("无法启动")
        _bad("端口 %d 已被占用。\n"
             "       8765 上很可能**已经跑着一个** photo-browser（在浏览器里看看是不是它）。\n"
             "       先关掉那个窗口；或改 `%s` 里的 `[server] port`，"
             "或本次启动加 `--port 8799`" % (port, iniPath or "app.ini"))
        return 1
    _out("  端口      : %s:%d 空闲" % (host, port))

    if args.check_only:
        _title("自检通过（--check-only，未启动）")
        return 0

    if args.db or args.root or args.thumb:
        paths.setRootOverride(photo=args.root, thumb=args.thumb, db=args.db)

    # ---- ⑦ 起服务 ----
    try:
        return runServer(dbFile=args.db, photoRoot=args.root, thumbRoot=args.thumb,
                         host=host, port=port,
                         openWeb=(not args.no_browser) and basicSettings.openBrowser())
    except LaunchError as e:
        _title("启动失败")
        _bad(str(e))
        return 1
    except paths.PathLayoutError as e:
        _title("启动失败")
        _bad("路径配置非法：%s" % e)
        return 1
    except KeyboardInterrupt:
        _out("")
        _out("已中止。")
        return 130


if __name__ == "__main__":
    # ⚠️⚠️ **必须是本文件的第一条可执行语句**（Windows 冻结 + spawn 的硬要求）：
    #     PyInstaller 的子进程会带着 `--multiprocessing-fork` 重新跑这个 exe，
    #     这一行负责把它接管走。放到 argparse 之后等于没写。
    multiprocessing.freeze_support()
    sys.exit(main())
