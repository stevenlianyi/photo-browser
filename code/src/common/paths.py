#! /usr/bin/env python3
#encoding: utf-8

#Filename: paths.py
#Description: photo-browser 照片库三条主路径的统一解析、校验与规范化
#
# 职责（业务层唯一允许问「路径在哪」的地方）：
#   photo_root()  / photo_dir()   —— 原图目录，【绝对只读】
#   thumb_dir()                   —— 缩略图 + 人脸裁剪图（生成物，可重建）
#   db_file()    / db_dir()       —— SQLite 主库 + 应用状态
#   validate_layout()             —— 三者互不嵌套、不得等于 photoRoot（DR-8）
#   ensure_dirs()                 —— 只建thumb / db / imports / exports，**绝不建 photo**
#   normalize_relpath()           —— 相对路径规范化，**仅用于算 hash**
#   dump_paths()                  —— 启动时打印全部解析结果
#
# 三条硬约束（违反即抛 PathLayoutError，绝不静默继续）：
#   1. photo\ 只读：ensure_dirs() 永不创建/写入 photo 下的任何路径；
#   2. 三目录互不嵌套：thumb 不能在 photo 里，db 不能在 photo/thumb 里，反之亦然；
#   3. 三者都不得等于 photoRoot 本身。
#
# ⚠️ normalize_relpath() 只用来算 relPathHash，
#    **绝不可用它改写库中 pb_photo.relPath 的原值**（原值必须保留，供人工核对）。

import os
import re
import sys
import threading
import types
import unicodedata

# 让本文件在任意 cwd / 直接 `python common\paths.py` 执行时都能 import 到兄弟包
# （沿用 contentHub 的做法：.../src/common/paths.py -> .../src）
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from config import basicSettings as basicSettings  # noqa: E402

_VERSION = "20261004"


# 归一化时用于识别「这其实不是相对路径」的盘符前缀，如 C: / d:
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# 派生目录名（照片库布局固定，见 README §四）
PHOTO_DIR_NAME: str = "photo"
THUMB_DIR_NAME: str = "thumb"
DB_DIR_NAME: str = "db"
DB_FILE_NAME: str = "photolib.db"
IMPORTS_DIR_NAME: str = "imports"
EXPORTS_DIR_NAME: str = "exports"


class PathLayoutError(Exception):
    """路径布局非法：三目录互相嵌套 / 等于 photoRoot 等。

    与 globalDefinition.ERR_PATH_LAYOUT_INVALID 对应。
    """


# ============================================================
# 一、配置读取
# ============================================================

def settingsModule():
    """取local_settings 模块；文件缺失（.gitignore 忽略、新机器未复制 .example）时
    返回一个由 basicSettings.DEFAULT_* 填充的临时模块，保证程序仍能启动。

    每次调用都重新读属性而非缓存，因此测试里 monkeypatch 配置立即生效。
    """
    try:
        from config import local_settings as ls
        return ls
    except ImportError:
        return types.SimpleNamespace(
            PHOTO_ROOT=basicSettings.DEFAULT_PHOTO_ROOT,
            THUMB_ROOT=basicSettings.DEFAULT_THUMB_ROOT,
            DB_FILE=basicSettings.DEFAULT_DB_FILE,
            _MISSING=True,
        )


def readSetting(name: str) -> str:
    """读一项本机配置（PHOTO_ROOT / THUMB_ROOT / DB_FILE）。

    取值优先级：local_settings.<name> → basicSettings.DEFAULT_<name> → ""
    返回值已去首尾空白；None 视作"未配置"。
    """
    ls = settingsModule()
    val = getattr(ls, name, None)
    if val is None or (isinstance(val, str) and val.strip() == ""):
        # 显式留空 THUMB_ROOT / DB_FILE 是合法的「走派生」语义，
        # 只有当 local_settings 根本没声明该属性时才回退 DEFAULT_（两者都是空串，结果一致）
        val = getattr(basicSettings, "DEFAULT_" + name, "")
    return ("" if val is None else str(val)).strip()


# ============================================================
# 二、基础工具
# ============================================================

def _abs(p) -> str:
    """规范化绝对路径（消解 . / ..、统一分隔符；不改变大小写、不要求存在）"""
    return os.path.abspath(os.path.normpath(os.fspath(p)))


def _key(p) -> str:
    """比较用键：Windows 下路径大小写不敏感，统一小写并去掉尾分隔符"""
    k = os.path.normcase(_abs(p))
    while len(k) > 3 and k[-1] in ("\\", "/"):
        k = k[:-1]
    return k


def _isWithin(child, parent) -> bool:
    """child 是否等于 parent、或位于 parent 之下"""
    ck, pk = _key(child), _key(parent)
    if ck == pk:
        return True
    return ck.startswith(pk + os.sep)


# ============================================================
# 二之二、进程级根目录覆盖（步骤 4 起提供）
# ============================================================
# 用途：让"指到临时库做实验"这条路子对**所有层**都成立，而不只是某个 CLI。
#   背景：scan_cli.py --db / gen_thumbs.py --db --root --thumb 早就支持指到临时库，
#   但那是工具自己解析好再往下传；HTTP 服务里的 api/static.py 是直接调
#   paths.photo_dir() / paths.thumb_dir() 的，没有传参的口子。
#   结果就是：想用临时库起服务，只能去改 local_settings.py ——
#   那是在动**正式配置**，风险和收益完全不成比例。
#
# 为什么放在 paths 而不是各层自己解析
# ----------------------------------
#   "库在哪"必须只有一个真相。再加一份"服务自己的根"，就会出现
#   「A 层以为在 tmp、B 层写进正式库」这种最难查的错。
#   覆盖写在 paths 里，photo_dir() / thumb_dir() / db_file() /
#   validate_layout() / ensure_dirs() 全部自动跟随，不存在第二个真相。
#
# 纪律
# ----
#   * 缺省（不调 setRootOverride）时行为与之前**完全一致**，正式库照旧走 local_settings；
#   * 只能由命令行显式参数触发，不读环境变量、不猜、不自动探测；
#   * 覆盖值一律走 _abs() 归一，且仍要过 validate_layout() ——
#     指到 photo 目录里面去照样抛 PathLayoutError。

_ROOT_OVERRIDE: dict = {"photo": None, "thumb": None, "db": None}
_OVERRIDE_LOCK = threading.Lock()


def setRootOverride(photo: str = None, thumb: str = None, db: str = None) -> dict:
    """设置本进程的三条主路径覆盖（只影响当前进程，退出即失效）。

    参数
    ----
    photo / thumb / db : 传 None 或""表示"这一条不覆盖，仍走配置"

    返回
    ----
    dict —— 当前生效的覆盖（key -> 绝对路径或 None）

    Raises
    ------
    PathLayoutError —— 覆盖后的布局非法（嵌套 / 等于 photoRoot）
    """
    with _OVERRIDE_LOCK:
        for key, value in (("photo", photo), ("thumb", thumb), ("db", db)):
            text = "" if value is None else str(value).strip()
            _ROOT_OVERRIDE[key] = _abs(text) if text else None
    # 立即验一次布局：非法覆盖要在这里就炸，而不是等到第一次请求图片时
    validate_layout()
    return dict(_ROOT_OVERRIDE)


def clearRootOverride() -> None:
    """清掉全部覆盖，回到 local_settings（单测收尾 / 工具复用时用）"""
    with _OVERRIDE_LOCK:
        for key in _ROOT_OVERRIDE:
            _ROOT_OVERRIDE[key] = None


def rootOverrides() -> dict:
    """当前生效的覆盖（只读拷贝）。全为 None = 没覆盖，走配置。"""
    with _OVERRIDE_LOCK:
        return dict(_ROOT_OVERRIDE)


def _override(key: str) -> str:
    with _OVERRIDE_LOCK:
        return _ROOT_OVERRIDE.get(key)


# ============================================================
# 三、路径解析
# ============================================================

def photo_root() -> str:
    """照片库根目录（来自 local_settings.PHOTO_ROOT）"""
    root = readSetting("PHOTO_ROOT")
    if not root:
        raise PathLayoutError("PHOTO_ROOT 未配置：请复制 local_settings.py.example 为 local_settings.py")
    return _abs(root)


def photo_dir() -> str:
    """原图目录 <PHOTO_ROOT>\\photo —— **绝对只读，绝不创建、绝不写入**。

    被 setRootOverride(photo=...) 覆盖时直接返回覆盖值。
    """
    override = _override("photo")
    if override:
        return override
    return _abs(os.path.join(photo_root(), PHOTO_DIR_NAME))


def thumb_dir() -> str:
    """缩略图/人脸裁剪图根目录。THUMB_ROOT 为空时派生 <PHOTO_ROOT>\\thumb（DR-1）

    被 setRootOverride(thumb=...) 覆盖时直接返回覆盖值。
    """
    override = _override("thumb")
    if override:
        return override
    v = readSetting("THUMB_ROOT")
    return _abs(v if v else os.path.join(photo_root(), THUMB_DIR_NAME))


def db_file() -> str:
    """SQLite 主库文件。DB_FILE 为空时派生 <PHOTO_ROOT>\\db\\photolib.db

    被 setRootOverride(db=...) 覆盖时直接返回覆盖值。
    """
    override = _override("db")
    if override:
        return override
    v = readSetting("DB_FILE")
    return _abs(v if v else os.path.join(photo_root(), DB_DIR_NAME, DB_FILE_NAME))


def db_dir() -> str:
    """库目录（db_file 所在目录）"""
    return os.path.dirname(db_file())


def imports_dir() -> str:
    """导入原件归档目录 <dbDir>\\imports"""
    return _abs(os.path.join(db_dir(), IMPORTS_DIR_NAME))


def exports_dir() -> str:
    """导出产物目录 <dbDir>\\exports"""
    return _abs(os.path.join(db_dir(), EXPORTS_DIR_NAME))


def writable_dirs() -> list:
    """本项目**允许创建**的目录清单 —— 显式列举，确保 photo 不在其中。

    ensure_dirs() 只认这份清单，从结构上杜绝「误建 photo」。
    """
    return [thumb_dir(), db_dir(), imports_dir(), exports_dir()]


def all_paths() -> dict:
    """一次性取出全部解析结果（供 dump_paths / 日志 / 启动自检使用）"""
    return {
        "photoRoot": photo_root(),
        "photo": photo_dir(),
        "thumb": thumb_dir(),
        "database": db_file(),
        "dbDir": db_dir(),
        "imports": imports_dir(),
        "exports": exports_dir(),
    }


# ============================================================
# 四、布局校验
# ============================================================

def validate_layout(photo=None, thumb=None, db=None) -> dict:
    """校验三条主路径的布局合法性（DR-8）。

    参数
    ----
    photo / thumb / db : str | None
        为 None 时取各自默认解析值。允许传自定义路径做单测。

    规则
    ----
      1. 三者都不得等于 photoRoot；
      2. 三者互不嵌套（任一不得等于或位于另一之下）。

    返回
    ----
    dict —— 实际参与校验的三个绝对路径

     Raises
    ------
    PathLayoutError
    """
    root = photo_root()
    photo = photo_dir() if photo is None else _abs(photo)
    thumb = thumb_dir() if thumb is None else _abs(thumb)
    db = db_file() if db is None else _abs(db)

    # 规则 1：不得等于 photoRoot
    for name, p in (("photo", photo), ("thumb", thumb), ("db", db)):
        if _key(p) == _key(root):
            raise PathLayoutError(
                "%s 不得等于 photoRoot：photoRoot=%s, %s=%s" % (name, root, name, p))

    # 规则 2：三者互不嵌套
    items = (("photo", photo), ("thumb", thumb), ("db", db))
    for an, ap in items:
        for bn, bp in items:
            if an == bn:
                continue
            if _isWithin(ap, bp):
                raise PathLayoutError(
                    "%s 不得位于 %s 之内：%s=%s, %s=%s" % (an, bn, an, ap, bn, bp))

    # 规则 1 之二：photo 也不得等于 photoRoot（覆盖 photo= 时最容易踩）
    if _key(photo) == _key(root):
        raise PathLayoutError("photo 不得等于 photoRoot：photoRoot=%s" % root)

    return {"photo": photo, "thumb": thumb, "db": db}


# ============================================================
# 五、目录创建
# ============================================================

def ensure_dirs() -> dict:
    """创建 thumb / db / imports / exports 目录（幂等）。

    ⚠️ **绝不创建或写入 photo 目录** —— photo 下的内容由用户自己放，
       应用只读。函数入口会再校验一次「待建目录不在 photo 之内」，
       即便将来有人往 writable_dirs() 里误加条目也会立刻报错。

    返回
    ----
    dict —— 目录名 -> 绝对路径
    """
    validate_layout()

    photo = photo_dir()
    targets = writable_dirs()
    for d in targets:
        if _isWithin(d, photo):
            raise PathLayoutError(
                "拒绝创建 photo 目录内的路径（原图只读硬约束）：%s" % d)
    for d in targets:
        os.makedirs(d, exist_ok=True)
    return dict(zip(("thumbDir", "dbDir", "importsDir", "exportsDir"), targets))


# ============================================================
# 六、相对路径规范化（仅用于算 hash）
# ============================================================

def normalize_relpath(p) -> str:
    """把相对路径统一成「正斜杠 + 无 ./ + 无首尾空白 + Unicode NFC」。

    **仅用于计算 relPathHash**，绝不可用它改写库中 pb_photo.relPath 的原值。

    规范化步骤
    ----------
      1. Unicode NFC（Windows 上NTFS 存 NFD，同一文件名跨系统字节不同）
      2. 去首尾空白
      3. 反斜杠 -> 正斜杠
      4. 纯分隔符（"/" "///" ...）直接返回空串
      5. 拒绝 UNC（//server/share）
      6. 拒绝盘符绝对路径（C:/...）
      7. 连续分隔符折叠为一个
      8. 去掉任意位置的空段与 "." 段（./a/b、a/./b、a//b、/a/b 归一为同一串）
      9. 拒绝含 ".." 段的路径（可逃出 photoDir，破坏原图只读边界）

    Returns
    -------
    str —— 规范化结果；输入为空/纯分隔符时返回 ""

    Raises
    ------
    ValueError —— 输入是绝对路径（盘符/UNC），或含 ".." 段时
    """
    if p is None:
        raise ValueError("normalize_relpath: 路径为 None")

    s = unicodedata.normalize("NFC", str(p)).strip()
    s = s.replace("\\", "/")

    # 纯分隔符视为空路径 —— 必须先于UNC 判定，否则 "///" 会被误判成 UNC
    if not s.strip("/"):
        return ""

    # 绝对路径一律拒绝：relPath 必须是 photo 目录内的相对路径
    if s.startswith("//"):
        raise ValueError("normalize_relpath: 不接受 UNC 路径: %r" % p)
    if _DRIVE_RE.match(s):
        raise ValueError("normalize_relpath: 不接受盘符绝对路径: %r" % p)

    # 折叠连续分隔符
    while "//" in s:
        s = s.replace("//", "/")

    # 按段过滤：空段与 "." 段一律丢掉（不只是在开头，a/./b.jpg 与 a/b.jpg
    # 必须是同一个 hash，否则同一个文件会算出两个 relPathHash）
    parts = [seg for seg in s.split("/") if seg not in ("", ".")]

    # ".." 会逃出 photoDir（如 "../其他目录/x.jpg"），必须拒绝：
    # 否则「relPathHash 相同即同一文件」的判定会建立在 photo 目录之外的路径上
    if any(seg == ".." for seg in parts):
        raise ValueError("normalize_relpath: 不接受含 '..' 的相对路径: %r" % p)

    return "/".join(parts)


def relPathHash(p) -> str:
    """规范化后算 sha256 —— 扫描器增量判定的「路径级去重」键（步骤 3 使用）。"""
    from common import miscCommon as miscCommon
    return miscCommon.sha256Hex(normalize_relpath(p))


# ============================================================
# 七、启动自检输出
# ============================================================

# dump_paths() 的固定列宽（验收要求的输出格式）
_LABEL_W: int = 10


def dump_paths(validate: bool = True) -> dict:
    """打印解析后的全部绝对路径（启动时调用）。

    输出示例
    --------
        ==== photo-browser 路径配置 ====
        photoRoot = d:\\PhotoLib
        photo     = d:\\PhotoLib\\photo
        thumb     = d:\\PhotoLib\\thumb
        database  = d:\\PhotoLib\\db\\photolib.db
        dbDir     = d:\\PhotoLib\\db
        imports   = d:\\PhotoLib\\db\\imports
        exports   = d:\\PhotoLib\\db\\exports

    参数
    ----
    validate : bool
        True 时打印后再跑一遍 validate_layout()，非法布局直接抛异常。

    返回
    ----
    dict —— all_paths() 结果
    """
    p = all_paths()
    print("==== photo-browser 路径配置 ====")
    for key in ("photoRoot", "photo", "thumb", "database", "dbDir", "imports", "exports"):
        print("%-*s= %s" % (_LABEL_W, key, p[key]))
    overrides = {k: v for k, v in rootOverrides().items() if v}
    if overrides:
        # 局部 import：paths 是全项目最底层的模块，miscCommon 只在这里用得上，
        # 不值得为它在 import 段排一个位置（relPathHash 也是这么写的）
        from common import miscCommon as miscCommon
        print("[override] 进程级根目录覆盖（命令行显式指定，优先于 local_settings）: %s"
              % miscCommon.jsonDumps(overrides, ensure_ascii=False))
    ls = settingsModule()
    if getattr(ls, "_MISSING", False):
        print("[warn] 未找到 config/local_settings.py，已回退 basicSettings.DEFAULT_* 兜底值；"
              "请复制 local_settings.py.example 为 local_settings.py")
    if validate:
        validate_layout()
    return p


if __name__ == "__main__":
    try:
        dump_paths()
    except PathLayoutError as e:
        print("[PathLayoutError]", e, file=sys.stderr)
        raise
