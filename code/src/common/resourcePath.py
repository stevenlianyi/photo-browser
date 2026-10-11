#! /usr/bin/env python3
#encoding: utf-8

#Filename: resourcePath.py
#Description: 冻结（PyInstaller）感知的**资源路径唯一出口** —— 打包后不再有 __file__ 相对推导
#
# 为什么必须存在这个文件（本步 R10 的核心改造之一）
# ------------------------------------------------
#   冻结之后 `__file__` 指向的是**解包出来的临时目录 / _internal 目录**，
#   而不是仓库布局。原先散落各处的
#       os.path.abspath(os.path.join(os.path.dirname(__file__), ...))
#   在源码模式下完全正确、在冻结后**全部指错**，而且错得**安静**：
#     · 前端 `code/webserver/dist` 找不到 -> `os.path.isdir()` 为假 ->
#       app.py **安静地**退回「只返回 JSON 的根路由」：
#       任何页面都是 404，而 `/api/health` 依然 200 —— 最阴的一种静默失败；
#     · `china_district.json.gz` 找不到 -> nameZh 全 NULL，界面回退英文（降级，不报错）；
#     · `meanshape_68.npz` 找不到 -> pose 不可用 -> 侧脸过滤**静默失效**。
#   ⇒ 统一到一个出口，其它地方一律不许再写 `__file__` 式推导。
#     改路径规则时只改这一个文件。
#
# 三态（**这个文件存在的全部理由**）
# ---------------------------------
#   ① 源码模式   `sys.frozen` 不存在：
#        src  = <repo>/code/src        （本文件所在目录）
#        code = <repo>/code
#        repo = <repo>
#        资源在 repo/code/... 下**原地**，不需要任何拷贝
#   ② 冻结 · onedir（**本项目唯一允许的形态**，见 §①）
#        exeDir  = <app>\app           （exe 所在目录）
#        bundle  = <app>\app\_internal （PyInstaller 的 sys._MEIPASS）
#        先找 exeDir、再找 bundle —— 这样「把 datas 搬到 _internal 外面」
#        与「留在 _internal 里」两种打包方式**都成立**，不许只支持一种。
#   ③ 冻结 · onefile（**禁止**，但这里也写得对，免得将来有人试）
#        exeDir 与 bundle 都指向临时解包目录（_MEIPASS）—— 搜索顺序仍然成立。
#
# ⚠️ **为什么 --onefile 是禁止的**（不是性能偏好，是能不能跑的问题）
# ------------------------------------------------------------------
#   `processor/media/thumbMaker.py:499` 用 ProcessPoolExecutor、
#   `engine/face/pool.py:564` 用 `multiprocessing.get_context("spawn")`。
#   onefile 下**每一次 spawn 都要把几百 MB 重新解包**到临时目录，
#   表现为「启动风暴 / 卡死 / 临时目录爆盘」。onedir 只解包一次。
#
# 三条纪律
# --------
#   1. 本文件**只做路径拼接与存在性判断**，绝不创建目录、绝不读文件内容；
#   2. 本文件**不 import 任何本项目模块**（它要在最底层，任何模块都能安全 import 它），
#      也不 import config —— 否则 paths/basicSettings 与它形成环；
#   3. 找不到时**不抛异常**，返回「首选候选路径」或空串，由调用方决定
#      「降级」还是「报错」（前端缺失是 404 降级、模型缺失是明确报错，
#      这两种语义不能在这个文件里被统一掉）。

import os
import sys

_VERSION = "20261011"


# ============================================================
# 一、三态判定
# ============================================================

def isFrozen() -> bool:
    """是否运行在 PyInstaller 冻结产物里。

    ⚠️ 判 `sys.frozen` 而不是 `hasattr(sys, "_MEIPASS")`：
       onefile 与 onedir **都有** _MEIPASS，而本项目只允许 onedir，
       真正要看的是「是不是冻结产物」。
    """
    return bool(getattr(sys, "frozen", False))


def exeDir() -> str:
    """**exe 所在目录**（冻结时）；源码模式下返回仓库根（仅供日志展示，别拿它拼资源）。

    这是与 `_internal` **并列**的那一层，也就是安装包里的 `{app}\\app\\`。
    """
    if isFrozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return repoDir()


def bundleDir() -> str:
    """PyInstaller 的 `sys._MEIPASS`（冻结）；源码模式下 = `code/src`。

    onedir 布局下它等于 `<exeDir>\\_internal`。
    """
    if isFrozen():
        meipass = getattr(sys, "_MEIPASS", None)
        return os.path.abspath(meipass or exeDir())
    return srcDir()


#: 源码根 `<repo>/code/src`（本文件在 `<src>/common/resourcePath.py`）
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: `<repo>/code`
_CODE_DIR = os.path.dirname(_SRC_DIR)
#: 仓库根
_REPO_DIR = os.path.dirname(_CODE_DIR)


def srcDir() -> str:
    """`<repo>/code/src`（源码模式下的「包根」）"""
    return _SRC_DIR


def codeDir() -> str:
    """`<repo>/code`"""
    return _CODE_DIR


def repoDir() -> str:
    """仓库根"""
    return _REPO_DIR


def searchRoots() -> list:
    """资源搜索根（**有序**，先命中先用）。

    ⚠️ 冻结时 **exeDir 在前、bundleDir 在后**（与文件头写的顺序一致）：
       为了让「模型/前端放在 exe 同级」这种可读布局优先命中。
       反过来写会让 `_internal\\web` 压过用户手动替换的 `web\\`，
       排查「我明明换了前端却没变」时非常费解。
    """
    if isFrozen():
        roots = [exeDir(), bundleDir()]
    else:
        roots = [_SRC_DIR, _CODE_DIR, _REPO_DIR]
    out = []
    for one in roots:
        if one and one not in out:
            out.append(one)
    return out


def describe() -> dict:
    """当前运行形态的快照（启动横幅 / 日志 / 排障用）"""
    return {
        "frozen": isFrozen(),
        "exeDir": exeDir(),
        "bundleDir": bundleDir(),
        "srcDir": srcDir(),
        "searchRoots": searchRoots(),
    }


# ============================================================
# 二、通用查找
# ============================================================

def candidates(relative: str) -> list:
    """把相对路径展开成**全部候选绝对路径**（顺序 = searchRoots()）

    `relative` 用正斜杠写（`"web/index.html"`），本函数负责转成当前平台分隔符。
    """
    parts = [seg for seg in str(relative or "").replace("\\", "/").split("/") if seg]
    return [os.path.abspath(os.path.join(root, *parts)) for root in searchRoots()]


def firstExisting(paths) -> str:
    """返回第一个存在的候选（文件或目录均可）；都不存在返回 ""。"""
    for one in paths:
        if one and os.path.exists(one):
            return os.path.abspath(one)
    return ""


def findResource(relative: str) -> str:
    """找一个资源（文件或目录），**找到返回绝对路径，找不到返回 ""**。"""
    return firstExisting(candidates(relative))


def preferred(relative: str) -> str:
    """首选候选路径（**不判断存在性**）。

    用途：调用方要把「想读的路径」打进错误信息或日志时，
    即便文件不存在也要有一个说得清楚的完整路径。
    """
    allCandidates = candidates(relative)
    return allCandidates[0] if allCandidates else ""


def resolve(relative: str, fallback: str = "") -> str:
    """找到就用找到的，找不到就用 `fallback`（一般是「源码模式下的老路径」）。

    这是把老代码从 `__file__` 推导平滑迁过来的入口：
        resolve("web", os.path.join(codeDir(), "webserver", "dist"))
    源码模式下 searchRoots() 里没有 `web`，于是回落到老路径 —— 行为逐字不变；
    冻结后命中的是 `<exeDir>\\web` 或 `<_MEIPASS>\\web`。
    """
    got = findResource(relative)
    return got if got else fallback


# ============================================================
# 三、各类资源的具体出口（**业务代码只准调这几个**）
# ============================================================

def webDistCandidates() -> list:
    """前端构建产物的候选目录（**有序**）。

    冻结：`<exeDir>\\web` → `<_MEIPASS>\\web`
    源码：`<repo>/code/webserver/dist`（老路径，**必须保留** ——
          `test_web_spa_fallback.py` 有一条用例断言 `_WEB_DIST` 等于它）
    """
    if isFrozen():
        return candidates("web")
    return [os.path.abspath(os.path.join(_CODE_DIR, "webserver", "dist"))]


def webDistDir() -> str:
    """前端 dist 目录：命中就返回命中的；都没命中返回**首选候选**。

    ⚠️ **不返回空串**：`main/app.py` 拿它做 `os.path.isdir()` 判断，
       返回一个真实但不存在的路径，排障时能在日志里看到「想找的是哪里」。
    """
    allCandidates = webDistCandidates()
    got = firstExisting(allCandidates)
    return got if got else (allCandidates[0] if allCandidates else "")


def databaseSourceDir() -> str:
    """`pb_*.txt` 表定义目录（DATABASE **源码**，不是运行期库文件）。

    ⚠️ 运行期建库/`--migrate` 用的是**生成物** `database/auto_generated/sqliteCommon.py`
       （已把表结构内联），并不 open 这些 .txt；
       只有 `database/sqliteCodeGenerator.py`（代码生成器）与个别测试会读它们。
       仍然打进包里的理由：① 让「重跑生成器」在离线机上成为可能；
       ② 万一将来某条链路改成运行期读 .txt，不会**静默**缺文件。
    """
    if isFrozen():
        return findResource("database") or preferred("database")
    return os.path.join(_SRC_DIR, "database")


def dataFileCandidates(fileName: str) -> list:
    """`data/` 下数据文件（`china_district.json.gz` / `meanshape_68.npz`）的候选。

    源码模式要同时覆盖两种历史布局：
      · `<repo>/code/src/data/`（`china_district.json.gz` 在这）
      · `<repo>/code/data/`    （`meanshape_68.npz` 在这）
    冻结后两者都被 datas 收进 `data/`（见 spec 的 datas 段），所以只有一处。
    """
    if isFrozen():
        return candidates("data/" + os.path.basename(fileName))
    return [
        os.path.abspath(os.path.join(_SRC_DIR, "data", os.path.basename(fileName))),
        os.path.abspath(os.path.join(_CODE_DIR, "data", os.path.basename(fileName))),
    ]


def dataFile(fileName: str) -> str:
    """找 `data/` 下的数据文件：命中返回纯绝对路径，没命中返回**首选候选**。"""
    allCandidates = dataFileCandidates(fileName)
    got = firstExisting(allCandidates)
    return got if got else (allCandidates[0] if allCandidates else "")


def modelsBaseCandidates() -> list:
    """**模型基目录**的候选（基目录 = 其下还有一层 `models\\<模型包>`，与 insightface 口径一致）。

    冻结：`<exeDir>`（`<exeDir>\\models\\buffalo_l\\*.onnx`）、`<_MEIPASS>`
    源码：空列表（源码模式模型走用户主目录，见 engine.defaultModelRoot 的三段回退）
    """
    if isFrozen():
        return [exeDir(), bundleDir()]
    return []


def modelPackCandidates(pack: str) -> list:
    """某个模型包目录的候选（**含源码模式**，用于「随包优先」的判定）。

    冻结：`<exeDir>\\models\\<pack>` → `<_MEIPASS>\\models\\<pack>`
    源码：`<repo>/models/<pack>` → `<repo>/code/models/<pack>`
          （允许开发机上放一份用来演「随包」这条路径；
            没有就自然落到用户主目录，行为与改动前一致）
    """
    if isFrozen():
        return candidates("models/" + pack)
    return [
        os.path.abspath(os.path.join(_REPO_DIR, "models", pack)),
        os.path.abspath(os.path.join(_CODE_DIR, "models", pack)),
    ]


def appIniCandidates() -> list:
    """外置配置 `app.ini` 的**内建**候选（不含 `--config` / 环境变量，那两条在 appIni.py）。

    冻结：`<exeDir>\\app.ini` → `<exeDir>\\..\\app.ini`
          （安装布局里 exe 在 `{app}\\app\\`，而 `app.ini` 在 `{app}\\` ——
            所以第二条是为了「有人把 exe 直接摆在 `{app}` 根」也能找到）
    源码：`<repo>/app.ini`（只在**显式存在**时生效，见 appIni.py 的说明）
    """
    if isFrozen():
        return [
            os.path.abspath(os.path.join(exeDir(), "app.ini")),
            os.path.abspath(os.path.join(os.path.dirname(exeDir()), "app.ini")),
        ]
    return [os.path.abspath(os.path.join(_REPO_DIR, "app.ini"))]


def logDir() -> str:
    """运行日志目录。

    冻结：`<exeDir>\\log`（与 `photo_root` 无关 —— 日志属于**程序**不属于数据）
    源码：`<repo>/code/log`（老路径）
    """
    if isFrozen():
        return os.path.abspath(os.path.join(exeDir(), "log"))
    return os.path.abspath(os.path.join(_CODE_DIR, "log"))


# ============================================================
# 四、给用户看的「可执行命令」提示（frozen 感知）
# ============================================================
# 为什么放在这个文件里（看着像越界，其实是最省事的一处）
# ------------------------------------------------------
#   「该给用户什么命令」与「是不是冻结」是**同一个判断**。
#   分散到 app.py / serve.py / 排障文档里各写一份，迟早会分叉成
#   「打包版里仍然印着 `python code\src\tools\build_db.py`」——
#   而那正是本步要修掉的东西（打包用户**没有 Python**，这条指引完全无效，
#   用户照做只会得到「'python' 不是内部或外部命令」）。
#   ⇒ 只此一处产出，其它地方一律调它。

def exeName() -> str:
    """当前可执行文件名（冻结 = exe 名；源码 = 解释器名，仅用于提示文案）"""
    return os.path.basename(sys.executable) if isFrozen() else "python"


def programCommand(*argv) -> str:
    """「本程序 + 参数」的可执行命令串。

    冻结：`"<exe 绝对路径>" --backup`（带引号 —— exe 可能在含空格的路径下）
    源码：`photo-browser.exe --backup`（**只是给界面看的文案**；源码模式下
          界面展示的应当是 `python code/src/tools/xxx.py`，调用方自己分支）
    """
    tail = " ".join(str(one) for one in argv)
    if isFrozen():
        return ('"%s" %s' % (os.path.abspath(sys.executable), tail)).strip()
    return ("photo-browser.exe %s" % tail).strip()


def initDbCommand() -> str:
    """**用户能真正执行**的「建库 / 补表」命令（不含任何 Shell 前缀）。"""
    if isFrozen():
        return programCommand("--init-db")
    return "python code\\src\\tools\\build_db.py --migrate"


def dbInitHint(prefix: str = "") -> str:
    """缺库/缺表时给用户看的**一行可执行**提示。

    ⚠️ 冻结分支里**绝不许出现 `python xxx.py`** —— 打包用户没有 Python。
    """
    head = (prefix + "：") if prefix else ""
    if isFrozen():
        return ("%s请运行 `%s` 完成建库 / 补表。\n"
                "       （正常启动时会自动建库，这条只在自动建表失败、"
                "或需要人工确认时才用）" % (head, initDbCommand()))
    return ("%s请先跑 `%s`（打包版请跑 `%s`）"
            % (head, initDbCommand(),
               os.path.join("{exe 所在目录}", "photo-browser.exe") + " --init-db"))


def scanHint() -> str:
    """扫描照片的可执行提示（冻结后 `scan_cli.py` 也不可用）。"""
    if isFrozen():
        return "在界面「扫描任务」页点「开始扫描」"
    return "python code\\src\\tools\\scan_cli.py"


if __name__ == "__main__":
    import json
    print(json.dumps(describe(), ensure_ascii=False, indent=2))
    for name in ("web", "database", "data/china_district.json.gz",
                 "data/meanshape_68.npz", "models/buffalo_l"):
        print("%-32s -> %s" % (name, findResource(name) or "(未找到)"))
    print("%-32s -> %s" % ("webDistDir", webDistDir()))
