#! /usr/bin/env python3
#encoding: utf-8

#Filename: photo-browser.spec
#Description: PyInstaller 打包脚本（**--onedir，禁止 --onefile**）
#
# 为什么是 onedir、为什么绝不能改成 onefile
# ----------------------------------------
#   本项目的多进程是 **spawn**：
#     · `processor/media/thumbMaker.py:499` —— 缩略图批量用 ProcessPoolExecutor
#     · `engine/face/pool.py:564`           —— 人脸提取用 get_context("spawn")
#   onefile 下**每一次 spawn 都要把几百 MB 重新解包**到临时目录 ——
#   表现为「启动风暴 / 卡死 / 临时目录爆盘」。onedir 只解包一次。
#   这不是性能偏好，是**能不能跑**的问题。
#
# 用法
# ----
#   code\.venv\Scripts\python.exe -m PyInstaller build\photo-browser.spec ^
#       --distpath dist --workpath build\work --noconfirm
#   （或者直接跑 build\package.ps1，它会连前端构建与 ISCC 一起做完）
#
# 产物
# ----
#   dist\photo-browser\
#     photo-browser.exe
#     _internal\            <- PyInstaller 的全部依赖（含 datas 的默认落点）
#     web\                  <- 前端 dist（由 package.ps1 从 _internal 搬出来）
#     models\buffalo_l\     <- 人脸模型（同上；**不含** 275MB 的 buffalo_l.zip）
#
# ⚠️ 关于「datas 到底落在哪」
#     PyInstaller 6.x 的 onedir 会把 datas 放进 `_internal\`（= `sys._MEIPASS`）。
#     package.ps1 在打包后把 `_internal\web` / `_internal\models`
#     **搬到 exe 同级**，让目录布局与安装说明一致、也方便用户单独替换前端。
#     两种布局 `common/resourcePath.py` 都能找到（exeDir 优先、_MEIPASS 兜底），
#     所以「只跑 spec 不跑 package.ps1」也是能跑的。

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

#: spec 所在目录（PyInstaller 会注入 SPECPATH；这里做一次兜底以便用 python 直接读）
_SPEC_DIR = os.path.abspath(globals().get("SPECPATH") or os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.abspath(os.path.join(_SPEC_DIR, os.pardir))
_CODE = os.path.join(_REPO, "code")
_SRC = os.path.join(_CODE, "src")


def _require(path, why):
    if not os.path.exists(path):
        raise SystemExit(
            "[spec] 缺少必需文件：%s\n       %s\n"
            "       请先修好再打包（**宁可打包失败，也不要出一个静默残缺的包**）"
            % (path, why))
    return path


# ============================================================
# 一、datas（运行期要读的文件；少一项都是一种**静默**失败）
# ============================================================
datas = []

# ---- 前端构建产物 ----
# 少了它：/api/health 照样 200，但所有页面 404（app.py 会安静退回只返回 JSON 的根路由）。
_WEB_DIST = _require(
    os.path.join(_CODE, "webserver", "dist"),
    "前端未构建：先跑 `cd code\\webserver; npm install; npm run build`")
datas.append((_WEB_DIST, "web"))

# ---- 建库用的表定义 ----
# ⚠️ 运行期建库/--migrate 用的是**生成物** `database/auto_generated/sqliteCommon.py`
#    （表结构已内联，不 open 这些 .txt）。仍然打进来的理由：
#      ① 让「重跑代码生成器」在离线机上成为可能；
#      ② 万一将来某条链路改成运行期读 .txt，不会**静默**缺文件
#         （`sqliteCodeGenerator.parseTableFile()` 读不到就抛 GenError，
#          而那是运行期才暴露的）。
#    只收 pb_*.txt（12 个），不带 .pyc / __pycache__。
_DB_DIR = os.path.join(_SRC, "database")
_txtCount = 0
for _name in sorted(os.listdir(_DB_DIR)):
    if _name.startswith("pb_") and _name.endswith(".txt"):
        datas.append((os.path.join(_DB_DIR, _name), "database"))
        _txtCount += 1
if _txtCount != 12:
    raise SystemExit("[spec] database/pb_*.txt 应有 12 个，实际 %d 个 —— "
                     "表定义变了就要同步改这里与文档" % _txtCount)

# ---- 离线区县数据（地点中文名）----
# 少了它：nameZh 全 NULL、界面回退英文 —— **降级不报错**，最难发现。
datas.append((_require(os.path.join(_SRC, "data", "china_district.json.gz"),
                       "区县边界数据（tools/fetch_zh_geo.py 产出）"), "data"))

# ---- 68 点均值形状（裸 ONNX 后端的姿态估计输入）----
# 少了它：pose 不可用 -> yaw 侧脸过滤**静默失效**（side_face 恒为 0）。
datas.append((_require(os.path.join(_CODE, "data", "meanshape_68.npz"),
                       "人脸 68 点均值形状"), "data"))

# ---- 人脸模型（随包 → 离线可用）----
# ⚠️ 只打 `buffalo_l\` 里那 5 个 onnx；**绝不打** `~/.insightface/models/buffalo_l.zip`
#    （275MB 的下载残留，打了等于白涨 275MB 安装包）。
_MODEL_PACK_DIR = None
for _candidate in (os.path.join(_REPO, "models", "buffalo_l"),
                   os.path.join(os.path.expanduser("~"), ".insightface", "models", "buffalo_l")):
    if os.path.isdir(_candidate):
        _MODEL_PACK_DIR = _candidate
        break
_MODEL_FILES = ("det_10g.onnx", "w600k_r50.onnx", "1k3d68.onnx")
if _MODEL_PACK_DIR:
    # 只收三个后端真正加载的文件（另外两个 2d106det / genderage 我们用不到）：
    # 每多收一个就是几十 MB 的安装包，而它们永远不会被读。
    for _name in _MODEL_FILES:
        _full = os.path.join(_MODEL_PACK_DIR, _name)
        if os.path.isfile(_full):
            datas.append((_full, "models/buffalo_l"))
    _missing = [n for n in _MODEL_FILES if not os.path.isfile(os.path.join(_MODEL_PACK_DIR, n))]
    if _missing:
        raise SystemExit("[spec] 模型包 %s 缺少 %s —— 打包会得到一个**静默残缺**的包"
                         % (_MODEL_PACK_DIR, _missing))
else:
    # 不 raise：允许打一个"不带模型"的包（用户首次运行会联网下载 325MB）。
    # 但必须**显式喊出来**，否则安装包突然瘦了 300MB 而没人知道为什么。
    print("[spec][warn] 未找到 buffalo_l 模型包 —— 本次打的包**不含模型**，"
          "首次人脸提取会联网下载约 325MB（离线机不可用）")

# ---- reverse_geocoder 的数据集（rg_cities1000.csv 等，约 7.5MB）----
# 它由包自身用 importlib 资源 API 读，**不在源码树里**，必须用 collect_data_files。
try:
    datas += collect_data_files("reverse_geocoder")
except Exception as _e:                                          # noqa: BLE001
    print("[spec][warn] collect_data_files('reverse_geocoder') 失败：%s" % _e)

# ---- insightface 自带数据（很小；避免它的 import 期资源查找失败）----
try:
    import insightface
    _IFACE_DIR = os.path.dirname(os.path.abspath(insightface.__file__))
    if os.path.isdir(os.path.join(_IFACE_DIR, "data", "objects")):
        # ⚠️⚠️ 这里有个**只能靠实测发现**的坑：
        #   `insightface/data/pickle_object.py` 的 get_object() 写了：
        #       if getattr(sys, 'frozen', False):
        #           base_dir = sys._MEIPASS          # <-- 冻结时**硬编码**到 _MEIPASS
        #       else:
        #           base_dir = Path(__file__).parent.absolute()
        #       objects_dir = osp.join(base_dir, 'objects')
        #   也就是说冻结后它**只在 `<_MEIPASS>\objects\` 里找**，不看自己包目录。
        #   不把它放到 `objects\` 就直接踩空 -> landmark_3d_68 的均值形状为 None
        #   -> 姿态坏掉（而且它只是 print 一行 [Error]，不抛异常）。
        #   ⇒ 必须**显式**落到 `objects`（不能靠 collect_data_files 的包内布局）。
        datas.append((os.path.join(_IFACE_DIR, "data", "objects"), "objects"))
    datas += collect_data_files("insightface")
except Exception as _e:                                          # noqa: BLE001
    print("[spec][warn] 收集 insightface 数据失败：%s" % _e)


# ============================================================
# 二、binaries（显式收，防 hook 漏）
# ============================================================
binaries = []
for _pkg in ("onnxruntime", "cv2", "shapely", "numpy"):
    try:
        binaries += collect_dynamic_libs(_pkg)
    except Exception as _e:                                      # noqa: BLE001
        print("[spec][warn] collect_dynamic_libs(%r) 失败：%s" % (_pkg, _e))


# ============================================================
# 三、hiddenimports
# ============================================================
#
# 通则（**本仓库踩过这个坑**）：PyInstaller 的静态分析**只看顶层 import**，
# 而本项目大量使用「函数内部 import 做可选依赖降级」。
# `schedule/faceScheduler.py` 的注释已经记过一次：子进程是在 multiprocessing 里
# spawn 出来的，缺依赖时它们在 `_queueWorkerLoop` 第一行就 ModuleNotFoundError ——
# 主进程只看到「子进程全部退出 / 一块都没回来」，没有任何指向性。
hiddenimports = [
    # ---- uvicorn 的**运行期**动态选择（它按字符串 import 这些子模块）----
    "uvicorn.logging", "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # ---- 被 spawn 的子进程按名字 import 的模块（缺了就白跑）----
    "engine.face.pool",             # 人脸提取子进程的 target 在本模块
    "processor.scanner.runner",     # 扫描批次
    "processor.media.thumbMaker",   # 缩略图 ProcessPoolExecutor 的 target
    "processor.media.faceCropper",
    # ---- R11 年代抽样（api.eraSample 是 app.py 的显式 import，静态可发现；
    #      下面两个是**保险**：懒 import 时 PyInstaller 会漏）----
    "engine.sample.eraSampler", "processor.media.eraPack",
    # ---- 重型可选依赖（顶层不一定 import，靠函数内 import 才用到）----
    "onnxruntime", "onnx", "cv2", "shapely", "insightface",
    "insightface.app", "insightface.utils.face_align",
    "PIL", "PIL.Image", "numpy",
    # `common/pinyin.py:79` 函数内 import（缺了拼音搜索整条失效、不报错）
    "pypinyin",
    # `processor/contact/vcard_import.py:323` 函数内 import
    "vobject",
    # `processor/scanner/meta.py:411` 函数内 import（缺了就没有地点名）
    "reverse_geocoder",
    # ---- 生成层与业务包（顶层已 import，列出来是为了让 spec 自解释）----
    "database.auto_generated.sqliteCommon",
    "common.sqliteHandle",
    "schedule.scanScheduler", "schedule.faceScheduler",
    "engine.match.matcher", "engine.match.centroid", "engine.match.rebucket",
    "engine.match.bucket", "engine.cluster.clustering", "engine.cluster.dbscan",
    "processor.place.placeNameZh", "processor.place.placeFinalize",
    "processor.place.placeStore", "processor.review.assigner",
    "processor.review.queue", "processor.review.merger",
    "processor.contact.csv_import", "processor.contact.vcard_import",
]
# ⚠️ 刻意**不列** `sklearn`：本项目没有装它（DBSCAN 走自实现 numpy 版），
#    列进去只会得到一条 "Hidden import not found" 警告，纯噪声。


# ============================================================
# 四、excludes（只剪**确定用不到**的）
# ============================================================
#
# ⚠️ 纪律：**先全量打包跑通，再逐项瘦身**。
#    `scipy` / `skimage` / `networkx` 是 insightface 的传递依赖，
#    裁剪有风险（`insightface.app` 可能 import scipy），
#    每剪一项都要重跑「人脸提取 10 张」那条验收。
excludes = [
    "pip", "_pytest", "pytest", "pytest_cov", "pygments",
    "tkinter", "matplotlib", "IPython", "notebook", "jupyter",
    "sphinx", "docutils", "setuptools", "_distutils_hack",
]
# ⚠️ **不要**把 `scipy` / `skimage` / `networkx` 放进 excludes ——
#    它们不在上面的清单里是有意的（见上）


# ============================================================
# 五、Analysis / COLLECT
# ============================================================

_ENTRY = _require(os.path.join(_SRC, "tools", "appLauncher.py"),
                  "exe 的唯一入口（freeze_support + app.ini + 自动建库 + 起服务）")

a = Analysis(
    [_ENTRY],
    pathex=[_SRC],                 # 让 `main` / `api` / `common` ... 等顶层包被找到
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir：二进制不塞进 exe
    name="photo-browser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # ⚠️ 不用 UPX：它会把部分 DLL 压坏/触发杀软误报
    console=True,                   # 保留控制台窗口 —— "关窗口 = 停服务" 是设计的一部分
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="photo-browser",
)
