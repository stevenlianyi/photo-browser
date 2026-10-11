#! /usr/bin/env python3
#encoding: utf-8

#Filename: appIni.py
#Description: 外置配置 `app.ini` 的读取 + 首次自动生成（R10 打包分发步骤）
#
# 为什么必须有它（**冻结之后 local_settings.py 是一条死路**）
# ----------------------------------------------------------
#   `config/local_settings.py` 是**包内的 Python 模块**，而且被 `.gitignore` 忽略：
#     · 冻结后用户在界面上**没有任何办法**改掉它 —— 它不是文本文件，
#       是打进 exe 内部的字节码；改默认的 `d:\PhotoLib` 只能靠改源码重打包；
#     · 而 `paths.readSetting()` 原先的优先级是
#       `local_settings.X → basicSettings.DEFAULT_X`，**不读环境变量、不读 INI**。
#   ⇒ 装到 E 盘的用户，照片库仍然指向 `d:\PhotoLib`，且**不报错**。
#   所以外置一份 INI，是"打包分发"这件事能成立的前提，不是"顺手加个配置"。
#
# 读取顺序（**只有这四条，别再加第五条**）
# ------------------------------------
#   ① `--config <path>` 命令行显式指定（最高优先级，排障/多库并存用）
#   ② 环境变量 `PHOTO_BROWSER_INI`
#   ③ `<exe 目录>\app.ini`        ← 安装器写的通常是 `<exe>\..\app.ini`，见 ④
#   ④ `<exe 目录>\..\app.ini`
#   首次运行若都**不存在** → 在 ①（或 ③）的位置**自动生成**一份带注释的 app.ini。
#
# ⚠️ 源码模式（未冻结）下**只有 ①② 生效**，且**不自动生成**文件
# -----------------------------------------------------------------
#   理由：源码模式下 `<exe 目录>` 是 `code\.venv\Scripts`，往那儿写配置是错的；
#         而往仓库根写 `app.ini` 会让「开发时跑的配置」与「测试跑的配置」分叉 ——
#         1400+ 条单测里大量 monkeypatch `config.local_settings`，
#         多出来的一层 INI 会把它们**静默**劫持掉。
#   ⇒ 源码模式一律走 `local_settings.py`（行为与本步之前**逐字相同**）；
#     想用 INI 就显式 `--config` 或设环境变量，那是个明确动作。
#
# ⚠️ 本文件**只读配置**，绝不写业务数据、绝不建目录之外的东西
#      （唯一的写动作是 `ensureIni()` 生成模板，且**只在文件不存在时**）

import configparser
import os
import sys

_VERSION = "20261011"

#: 环境变量名（与 `--config` 同语义，优先级低一档）
INI_ENV: str = "PHOTO_BROWSER_INI"

#: 文件名（**不许改名**：安装器 `installer.iss` 里写死了这个名）
INI_NAME: str = "app.ini"

#: `[paths]` 段的三项与本项目三个配置名的对应（`paths._readIniSetting` 用它，
#: 写成映射而不是散在字符串里，改名时只有一处会漏）
SECTION_PATHS: str = "paths"
SECTION_SERVER: str = "server"
SECTION_MODELS: str = "models"
SECTION_SCAN: str = "scan"


#: 首次运行自动生成用的模板。
#:
#: ⚠️ **逐条注释为什么这样写**：
#:   · `photo_root` = 安装时由安装器写成「实际安装目录」；这里留的默认值
#:     只是**安装器没写成功**时的兜底（安装器写不进去才轮到它）；
#:   · `thumb_root` / `db_file` 留空 = 走 DR-8 派生（`<photo_root>\thumb`、
#:     `<photo_root>\db\photolib.db`）—— **别在这里把派生结果写死**，
#:     否则用户改了 photo_root 之后这两个不会跟着变（又是"看起来生效了"）；
#:   · `model_root` 留空 = 三段回退（随包 → 用户主目录 → 下载），见
#:     `engine.face.engine.modelBaseRoot()`。
_TEMPLATE = """\
; ============================================================
; photo-browser 外置配置（app.ini）
;
;   本文件是**用户可改**的配置出口。改完**重启 photo-browser 生效**。
;   文件位置：与本文件同级即程序安装目录；exe 在 <安装目录>\\app\\ 之下。
;
;   ⚠️ 升级**不会覆盖**本文件（安装器只在文件不存在时生成），
;      卸载**不会删除**本文件，也不会删除 photo / thumb / db / backup。
;   ⚠️ 路径里可以用正斜杠或反斜杠，两种都认；不要用引号把路径包起来。
; ============================================================

[paths]
; 照片库根目录。**照片必须放在 <photo_root>\\photo\\ 之下**（原图只读）。
; ⚠️ photo_dir 是派生的，不能单独配置 —— 想指向已有照片目录，
;    请让照片位于 <那个目录>\\photo\\ 之内，再把 photo_root 指到它的上一级。
photo_root = %(photo_root)s
; 缩略图 / 人脸裁剪图根目录。留空 = <photo_root>\\thumb（DR-8 派生）
thumb_root =
; SQLite 主库文件。留空 = <photo_root>\\db\\photolib.db（DR-8 派生）
db_file =

[server]
host = 127.0.0.1
; ⚠️ 只允许回环地址（127.0.0.1 / localhost / ::1）。填别的会被拒绝启动 ——
;    本服务**零鉴权**，开 0.0.0.0 等于把全家照片发到局域网。
port = %(port)d
; 1 = 启动后自动打开浏览器；0 = 不打开（只打印地址）
open_browser = 1

[models]
; 人脸模型基目录。留空 = 依次找
;   ① <安装目录>\\app\\models\\buffalo_l（随包，优先，离线可用）
;   ② %%USERPROFILE%%\\.insightface\\models\\buffalo_l（你以前下过）
;   ③ 都没有才联网下载（约 325MB，离线机请把模型拷进 ①）
model_root =

[scan]
; 一次扫描任务处理到该张数即暂停，等用户「继续下一批」
batch_size = %(batch_size)d
"""


#: 进程内缓存。`{"parser": ConfigParser|None, "path": str, "loaded": bool}`
#:
#: ⚠️ 缓存是**必要的**（`paths.readSetting()` 每次解析都读盘的话，
#:    一次扫描 10 万张就是 10 万次 open），
#:    但必须提供 `reset()`：`--config` 要在**任何**配置读取之前落下，
#:    单测里也要能在用例之间换配置。
_STATE = {"parser": None, "path": "", "loaded": False, "generated": "",
          "encoding": ""}

#: 命令行显式指定的路径（`--config`）
_EXPLICIT = {"path": ""}


class AppIniError(Exception):
    """app.ini 存在但**读不动**（权限 / 编码 / 语法）。"""


# ============================================================
# 一、定位
# ============================================================

def setExplicitPath(path: str) -> str:
    """`--config <path>` 落点。**必须在任何配置读取之前调用**（appLauncher 第一步）。"""
    _EXPLICIT["path"] = os.path.abspath(os.path.expanduser(str(path))) if path else ""
    reset()
    return _EXPLICIT["path"]


def explicitPath() -> str:
    return _EXPLICIT["path"]


def envPath() -> str:
    return str(os.environ.get(INI_ENV) or "").strip()


def candidatePaths(includeImplicit: bool = True) -> list:
    """按优先级列出候选（可能不存在）。

    参数
    ----
    includeImplicit : False 时**只**返回 ①②（显式通道）。
        源码模式用它 —— 源码模式不认"exe 同级 app.ini"这条隐式通道。
    """
    out = []
    for one in (explicitPath(), envPath()):
        if one:
            text = os.path.abspath(os.path.expanduser(one))
            if text not in out:
                out.append(text)
    if includeImplicit:
        # 局部 import：appIni 是**配置层最底层**之一，resourcePath 只是拼路径，
        # 而且它在最底层不 import 本项目任何模块，环是不会有的
        from common import resourcePath as resourcePath
        if resourcePath.isFrozen():
            for one in resourcePath.appIniCandidates():
                if one not in out:
                    out.append(one)
    return out


def existingPaths() -> list:
    """**全部存在**的候选（按优先级）。用于「同时有好几份 app.ini」时的提示。

    ⚠️ 为什么值得单独报一下：安装布局里 `{app}\\app.ini`（安装器写的）与
       `{app}\\app\\app.ini`（如果有人在那儿放过一份）会同时存在，
       而按优先级**前者会被后者压住**。不提示的话，用户改了半天没生效，
       界面与日志里一点线索都没有 —— 这正是 R10 要消灭的那类静默失败。
    """
    return [one for one in candidatePaths() if os.path.isfile(one)]


def activePath() -> str:
    """当前生效的 app.ini 绝对路径；**都不存在返回 ""**（= 走 local_settings/默认值）。"""
    for one in candidatePaths():
        if os.path.isfile(one):
            return one
    return ""


def iniPath() -> str:
    """**要给用户看的** app.ini 完整路径。

    与 `activePath()` 的差别：文件还不存在时返回「将要生成/应该放的位置」，
    这样「路径不可写」的提示里能给出一个**可操作**的完整路径，
    而不是一句「请改 app.ini」（用户会先问"哪个 app.ini"）。
    """
    got = activePath()
    if got:
        return got
    allCandidates = candidatePaths()
    return allCandidates[0] if allCandidates else ""


# ============================================================
# 二、读取
# ============================================================

def reset() -> None:
    """清缓存（`--config` 落点变化后 / 单测用例之间必须调）"""
    _STATE["parser"] = None
    _STATE["path"] = ""
    _STATE["loaded"] = False
    _STATE["generated"] = ""
    _STATE["encoding"] = ""


#: 解码 app.ini 的**尝试顺序**（**多编码回退是必需的，不是"以防万一"**）
#:
#: 实测证据（R10 打包验收）：安装器的 `[Code]` 用 Inno 的 `SaveStringToFile()` 写
#: app.ini，而它写出来的是**系统 ANSI 代码页**（本机 cp936）—— 不是 UTF-8。
#: 只按 utf-8 读的后果是：解析抛 UnicodeDecodeError -> 按"无配置"继续 ->
#: `photo_root` 回落 `d:\PhotoLib`。于是装到 `D:\apps\photobrowser` 的用户，
#: 数据仍然跑去 D:\PhotoLib —— **正是本步要修掉的那个缺陷，而且完全不报错**。
#: 反过来，用户用新版记事本（默认 UTF-8）存盘又会是 UTF-8。
#: 两种都得认，所以按「UTF-8 优先」的顺序逐个试。
#:
#: 为什么 UTF-8 一定排在最前：GBK 编码的中文**几乎不可能**同时是合法 UTF-8，
#: 而纯 ASCII 内容两种都能解 —— 先 UTF-8 只会解对，不会解错。
_INI_ENCODINGS: tuple = ("utf-8-sig", "utf-8", "cp936", "gbk", "big5")


def _readIniText(path: str) -> tuple:
    """按候选编码逐个尝试解码，返回 `(text, encoding)`。

    ⚠️ 最后一个 `latin-1` 兜底**永不失败**（任何字节序列都是合法 latin-1）：
       它的作用是"总能读出一个字符串"，让 configparser 去报**语法**错误，
       而不是把「编码认不出来」伪装成「没有配置文件」——后者会静默回落默认值。
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    for encoding in _INI_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace"), "latin-1"


def _loadParser() -> configparser.ConfigParser:
    """解析当前生效的 app.ini；**没有文件时返回一个空 parser**（不抛异常）。

    ⚠️ 返回空 parser 而不是 None：调用方 `setting()` 就能写成一条判断，
       不用每处都先问「有没有配置文件」——那是"配置缺失"与"配置为空"
       这两种完全不同的情况，混在一起最容易写出静默回落。
    """
    if _STATE["loaded"]:
        return _STATE["parser"]
    _STATE["loaded"] = True
    path = activePath()
    _STATE["path"] = path
    parser = configparser.ConfigParser(interpolation=None)
    if path and os.path.isfile(path):
        try:
            text, encoding = _readIniText(path)
            parser.read_string(text, source=path)
            _STATE["encoding"] = encoding
        except (OSError, configparser.Error, ValueError) as e:
            print("[appIni] 解析 %s 失败（按「无配置」继续）：%s" % (path, e), file=sys.stderr)
            parser = configparser.ConfigParser(interpolation=None)
    _STATE["parser"] = parser
    return parser


def loadedEncoding() -> str:
    """实际用哪种编码读进来的（排障用：GBK 说明是安装器/旧记事本写的）"""
    _loadParser()
    return _STATE.get("encoding", "")


def loadedPath() -> str:
    """实际读到的 app.ini 路径（空 = 没读到，走的是默认值）。**先解析再问。**"""
    _loadParser()
    return _STATE["path"]


def setting(section: str, key: str, default: str = "") -> str:
    """读一项，`default` 与「显式留空」**分开处理**：

    · 键不存在      -> 返回 `default`
    · 键存在但为空串 -> 返回 `""`（= 显式留空。`thumb_root=` 就是这种语义：
                         "我要走派生"，不是"我要默认值"）
    """
    parser = _loadParser()
    try:
        if not parser.has_section(section):
            return default
        if not parser.has_option(section, key):
            return default
        return str(parser.get(section, key, raw=True) or "").strip()
    except (configparser.Error, ValueError) as e:            # pragma: no cover - 兜底
        print("[appIni] 读取 [%s] %s 失败：%s" % (section, key, e), file=sys.stderr)
        return default


def getBool(section: str, key: str, default: bool = False) -> bool:
    """读布尔。真值：1/true/yes/on/y/t（大小写不敏感）；其余非空值为假。"""
    text = setting(section, key, "").strip().lower()
    if text == "":
        return bool(default)
    return text in ("1", "true", "yes", "on", "y", "t")


def getInt(section: str, key: str, default: int = 0) -> int:
    text = setting(section, key, "").strip()
    if text == "":
        return int(default)
    try:
        return int(text)
    except ValueError:
        return int(default)


def allSettings() -> dict:
    """全部已读到的配置（给启动横幅 / 日志 / 排障）。**不打印密码类字段**（本项目没有）。"""
    parser = _loadParser()
    out = {}
    for section in parser.sections():
        out[section] = dict((key, parser.get(section, key, raw=True)) for key in parser.options(section))
    return out


# ============================================================
# 三、首次生成（**只在文件不存在时**）
# ============================================================

def defaultPhotoRoot() -> str:
    """自动生成 app.ini 时 `photo_root` 该填什么。

    规则（**对应"装到哪、数据就在哪"这条需求**）
    ------------------------------------------
      ① exe 在 `<root>\\app\\` 之下，且 `<root>` 下已经有 photo/thumb/db/backup
         任意一个 -> 那就是安装根，返回 `<root>`；
      ② 否则返回 **exe 所在目录**。
    ⚠️ **绝不返回写死的 `d:\\PhotoLib`**：那正是本步要修掉的缺陷
       （程序装到 E:、数据跑去 D:，用户毫不知情）。
    """
    from common import resourcePath as resourcePath
    exe = resourcePath.exeDir()
    parent = os.path.dirname(exe)
    if parent and parent != exe:
        for probe in ("photo", "db", "thumb", "backup"):
            if os.path.isdir(os.path.join(parent, probe)):
                return parent
    return exe


def ensureIni(port: int = 8765, batchSize: int = 100, quiet: bool = False) -> str:
    """**冻结环境下**：都不存在则生成一份带注释的 app.ini；返回生效路径。

    源码模式（未冻结）**不生成**，返回 ""（见文件头说明）。

    ⚠️ 生成动作必须"只在不存在时"发生 —— 这条纪律对安装器（`[Code]`+`FileExists`）
       与对本函数是**同一条**：一旦每次启动都重写，
       用户手改的 `photo_root` 会被覆盖回默认，而他的照片还在老路径上
       ⇒ 表现为「照片全没了」（实际是路径被改回去了）。
    """
    from common import resourcePath as resourcePath

    got = activePath()
    if got:
        _loadParser()
        return got
    if not resourcePath.isFrozen():
        _STATE["loaded"] = True
        _STATE["parser"] = configparser.ConfigParser(interpolation=None)
        _STATE["path"] = ""
        return ""

    target = iniPath()
    if not target:
        return ""
    # 再判一次存在性：`iniPath()` 在都不存在时给的是"将要生成的位置"，
    # 而极少数情况下（并发启动两个实例）另一个进程可能刚写完
    if not os.path.isfile(target):
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            payload = _TEMPLATE % {"photo_root": defaultPhotoRoot(),
                                   "port": int(port), "batch_size": int(batchSize)}
            # newline="\r\n"：Windows 记事本打开 INI 才不会变成一整行
            # （旧版记事本不认单独的 \n，这是实测踩过的）
            with open(target, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(payload)
            _STATE["generated"] = target
            if not quiet:
                print("[appIni] 首次运行，已生成外置配置：%s" % target)
        except OSError as e:
            print("[appIni] 生成 %s 失败：%s" % (target, e), file=sys.stderr)
            return ""
    reset()
    _loadParser()
    return target


def generatedPath() -> str:
    """本次进程内**刚刚生成**的 app.ini 路径（没生成过返回 ""）"""
    return _STATE["generated"]


if __name__ == "__main__":
    print("appIni _VERSION: %s" % _VERSION)
    print("candidates:")
    for one in candidatePaths():
        print("   %s %s" % ("[存在]" if os.path.isfile(one) else "[    ]", one))
    print("active : %s" % (activePath() or "(无 —— 走 local_settings / DEFAULT_*)"))
    print("iniPath: %s" % (iniPath() or "(无)"))
    print("settings: %s" % allSettings())
