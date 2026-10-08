#! /usr/bin/env python3
#encoding: utf-8

#Filename: backup.py
#Description: photo-browser 备份 / 恢复 / 列清单（步骤 12 · P-08 数据维护）
r"""
（本文档字符串是 raw 的：正文里满是 `db\` / `thumb\` 这种路径片段，
 非 raw 字符串会把 `\ ` 当成非法转义序列，编译期就报 SyntaxWarning）

备份的定义（硬约束，不可改）
# ----------------------------
#   **停服务 → 拷贝 `db\` + `thumb\`**，**绝不拷贝 `photo\`**。
#
# 为什么不能提供「在服务运行时点一下按钮就备份」
# ------------------------------------------------
#   本项目是 SQLite **WAL** 模式。还在跑着的时候把 `photolib.db` 单文件拷走，
#   拷到的是「主库文件 + 尚未 checkpoint 进主文件的 -wal」的**中间态**：
#   最近几分钟的写入（人脸确认、质心重算、扫描增量）全都躺在 `-wal` 里。
#   而拷 `-wal` 也不可靠 —— 它在拷的过程中还在被写。
#   结果是：**备份成功、零报错，恢复后少几百条记录**。
#   这类「看起来成功其实悄悄丢数据」的故障是备份这件事里最贵的失败模式，
#   所以这里坚持：备份是**停服务之后**在命令行做的事。
#
# 三个子命令
# ----------
#   backup    拷贝 db\ 与 thumb\ 到 <PHOTO_ROOT>\backup\pb_<yyyymmddHHMMSS>\
#   restore   从某个备份目录还原 db\ 与 thumb\（**先自动另存现状**）
#   list      列出已有备份
#
# 为什么「恢复前自动另存现状」
# --------------------------
#   restore 是**覆盖式**的。直接覆盖的话，一次 restore 打错编号
#   就把当前库换掉了，而当前库可能正是用户三天前刚做完的确认工作。
#   所以脚本先给现状拍一份 `pre-restore_<时间戳>` —— 磁盘代价只是几百 MB，
#   而「不敢恢复」和「不敢备份」一样会让这个功能没人用。
#
# ⚠️ 关于 photoDir
#   恢复**不碰** `photo\`：原图是只读源，它不在备份里（备份它动辄几十 GB，
#   而它又不会坏）。所以「恢复」= 让索引回到某个时刻，
#   而不是把照片找回来 —— 界面上必须说清这句话，否则用户会以为
#   「恢复」能把删掉的照片也找回来（不能，那需要原图当时就在）。
#
# 用法
# ----
#   python code/src/tools/backup.py backup
#   python code/src/tools/backup.py backup --dest D:\PhotoLibBackup
#   python code/src/tools/backup.py list
#   python code/src/tools/backup.py restore --code pb_20261007153012
#   python code/src/tools/backup.py restore --code pb_20261007153012 --yes
"""

import argparse
import json
import os
import shutil
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../tools
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import paths as paths                                 # noqa: E402

_VERSION = "20261007"

#: 备份里 db\ 的子目录名（与源目录同名，便于人工核对）
DB_SUBDIR: str = paths.DB_DIR_NAME
THUMB_SUBDIR: str = paths.THUMB_DIR_NAME

#: 单文件超过这个大小就单独打进度（缩略图目录几万个文件，靠文件数更准）
_PROGRESS_EVERY: int = 2000


# ============================================================
# 一、小工具
# ============================================================

def stamp() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.localtime())


def dirSize(root: str) -> tuple:
    """(文件数, 总字节)。用于清单与打印。"""
    count, total = 0, 0
    for dirPath, _dirNames, files in os.walk(root):
        for name in files:
            path = os.path.join(dirPath, name)
            try:
                total += os.path.getsize(path)
                count += 1
            except OSError:
                continue                    # 拷贝过程中被占用/已消失，跳过并计数
    return count, total


def humanSize(num: int) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024
    return "%.1f TB" % value


def copyTree(src: str, dst: str, skipDbSideFiles: bool = True) -> tuple:
    r"""拷贝整棵目录树。返回 (文件数, 字节)。

    ⚠️ **为什么不直接 shutil.copytree**
      ① db\ 里的 `photolib.db-wal` / `-shm` 在服务停干净后**应该不出现**；
        出现了就说明还有进程开着这个库，备份会拿到中间态。
        这里默认**跳过** `-wal/-shm` 并在返回值里说明 ——
        比「安静地拷进备份里，恢复后库多出一份陈旧 WAL」安全得多。
        （真需要连 WAL 一起拷的场景只有热备，而那本来就不该做。）
      ② 需要进度：thumb\ 10 万张的目录拷几分钟没输出，用户会以为卡死了。
    """
    if not os.path.isdir(src):
        return 0, 0
    os.makedirs(dst, exist_ok=True)
    copied, total, skipped = 0, 0, 0
    for dirPath, dirNames, files in os.walk(src):
        rel = os.path.relpath(dirPath, src)
        outDir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(outDir, exist_ok=True)
        for name in files:
            if skipDbSideFiles and (name.endswith("-wal") or name.endswith("-shm")):
                skipped += 1
                continue
            fromPath = os.path.join(dirPath, name)
            toPath = os.path.join(outDir, name)
            try:
                shutil.copy2(fromPath, toPath)
                copied += 1
                total += os.path.getsize(fromPath)
            except OSError as e:
                print("  ! 跳过 %s（%s）" % (fromPath, e))
            if copied % _PROGRESS_EVERY == 0:
                print("    ... 已复制 %d 个文件（%.1f）" % (copied, humanSize(total)))
    if skipped:
        print("  · 跳过 %d 个 -wal/-shm 文件（说明可能有进程仍开着这个库）" % skipped)
    return copied, total


def uniqueBackupCode(root: str) -> str:
    """生成一个**不与已有备份重名**的备份编号。

    为什么不能只用 `pb_<yyyymmddHHMMSS>`
    ----------------------------------
      时间戳只到秒。真实场景里恰好会撞上两次：
        ① 一次 restore 会先给现状拍一份安全备份，紧接着用户又按了一次备份；
        ② 脚本/自动化连续跑两次。
      撞名时如果直接报错，用户看到的是「备份失败」而**已经有一份好备份躺在那里**；
      如果静默复用那个目录，就会把两次备份叠进同一个目录 —— 清单被后写的覆盖，
      而目录里混着两个时刻的文件，恢复出来的库是**哪两个时刻的混合**，
      且不报错。所以这里宁可加后缀，也要保证「一个目录 = 一个时刻」。
    """
    base = "%s%s" % (paths.BACKUP_PREFIX, stamp())
    code = base
    suffix = 2
    while os.path.exists(os.path.join(root, code)):
        code = "%s-%d" % (base, suffix)
        suffix += 1
    return code


def assertNoWal(dbDir: str) -> None:
    """库里还有 -wal 就提醒一句（不阻断，但要让人看见）。"""
    try:
        names = os.listdir(dbDir)
    except OSError:
        return
    left = [n for n in names if n.endswith("-wal")]
    if left:
        print("  ⚠ %s 仍存在：%s" % (dbDir, ", ".join(left)))
        print("    若刚停服务，这是正常的（WAL 关连接时会自动 checkpoint 并删掉）。")


# ============================================================
# 二、backup
# ============================================================

def doBackup(destRoot: str = None, label: str = None) -> dict:
    dbDir = paths.db_dir()
    thumbDir = paths.thumb_dir()
    photoDir = paths.photo_dir()
    root = destRoot or paths.backup_root()

    for name, target in (("db 目录", dbDir), ("thumb 目录", thumbDir)):
        if not os.path.isdir(target):
            print("[Error] %s 不存在：%s（先跑 build_db / scan，或检查配置）" % (name, target),
                  file=sys.stderr)
            return {"ok": False, "errMsg": "%s 不存在: %s" % (name, target)}

    code = uniqueBackupCode(root)
    targetRoot = os.path.abspath(os.path.join(root, code))

    print("==== photo-browser 备份 ====")
    print("  备份编号 : %s" % code)
    print("  目标     : %s" % targetRoot)
    print("  数据库   : %s" % dbDir)
    print("  缩略图   : %s" % thumbDir)
    print("  原图     : %s（**不备份、不改动**）" % photoDir)
    print("")
    print("  ⚠ 请确认 photo-browser 服务已停止。")
    print("    仍在运行时拷走的是 WAL 中间态，恢复后会少数据且不报错。")
    print("")
    assertNoWal(dbDir)

    os.makedirs(targetRoot, exist_ok=False)
    dbFiles, dbBytes = copyTree(dbDir, os.path.join(targetRoot, DB_SUBDIR))
    thumbFiles, thumbBytes = copyTree(thumbDir, os.path.join(targetRoot, THUMB_SUBDIR))

    manifest = {
        "backupCode": code,
        "createdYMDHMS": stamp(),
        "label": label or None,
        "source": {"db": dbDir, "thumb": thumbDir, "photo": photoDir},
        "db": {"files": dbFiles, "bytes": dbBytes, "sizeText": humanSize(dbBytes)},
        "thumb": {"files": thumbFiles, "bytes": thumbBytes, "sizeText": humanSize(thumbBytes)},
        "note": "不含 photo\\（原图只读源，不在备份内）；恢复只还原索引与缩略图",
    }
    with open(os.path.join(targetRoot, paths.BACKUP_MANIFEST_NAME), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print("")
    print("  db    : %d 个文件 / %s" % (dbFiles, humanSize(dbBytes)))
    print("  thumb : %d 个文件 / %s" % (thumbFiles, humanSize(thumbBytes)))
    print("")
    print("备份完成。")
    print("备份路径: %s" % targetRoot)
    return {"ok": True, "backupCode": code, "path": targetRoot, "manifest": manifest}


# ============================================================
# 三、list
# ============================================================

def doList(destRoot: str = None) -> list:
    root = destRoot or paths.backup_root()
    if not os.path.isdir(root):
        print("还没有任何备份（目录不存在）：%s" % root)
        return []
    names = sorted(n for n in os.listdir(root)
                   if n.startswith(paths.BACKUP_PREFIX)
                   and os.path.isdir(os.path.join(root, n)))
    if not names:
        print("还没有任何备份：%s" % root)
        return []
    print("备份根目录: %s" % root)
    print("")
    print("%-22s %-10s %-10s %s" % ("备份编号", "db", "thumb", "创建时间"))
    out = []
    for name in reversed(names):
        full = os.path.join(root, name)
        manifest = {}
        mFile = os.path.join(full, paths.BACKUP_MANIFEST_NAME)
        if os.path.isfile(mFile):
            try:
                with open(mFile, "r", encoding="utf-8") as fh:
                    manifest = json.load(fh) or {}
            except (OSError, ValueError):
                manifest = {}
        db = manifest.get("db") or {}
        thumb = manifest.get("thumb") or {}
        dbSize = db.get("sizeText") or "?"
        thumbSize = thumb.get("sizeText") or "?"
        if dbSize == "?":
            dbSize = humanSize(dirSize(os.path.join(full, DB_SUBDIR))[1])
        if thumbSize == "?":
            thumbSize = humanSize(dirSize(os.path.join(full, THUMB_SUBDIR))[1])
        created = str(manifest.get("createdYMDHMS") or "?")
        print("%-22s %-10s %-10s %s" % (name, dbSize, thumbSize, created))
        out.append({"backupCode": name, "path": full, "manifest": manifest})
    print("")
    print("恢复某个备份：python code/src/tools/backup.py restore --code <备份编号>")
    return out


# ============================================================
# 四、restore
# ============================================================

def doRestore(code: str, destRoot: str = None, assumeYes: bool = False) -> dict:
    root = destRoot or paths.backup_root()
    source = os.path.abspath(os.path.join(root, str(code)))
    if not os.path.isdir(source):
        print("[Error] 找不到备份目录：%s" % source, file=sys.stderr)
        return {"ok": False, "errMsg": "备份不存在: %s" % source}
    for name in (DB_SUBDIR, THUMB_SUBDIR):
        if not os.path.isdir(os.path.join(source, name)):
            print("[Error] 备份不完整（缺 %s\\）：%s" % (name, source), file=sys.stderr)
            return {"ok": False, "errMsg": "备份不完整，缺 %s" % name}

    dbDir = paths.db_dir()
    thumbDir = paths.thumb_dir()

    print("==== photo-browser 恢复 ====")
    print("  备份     : %s" % source)
    print("  目标 db  : %s" % dbDir)
    print("  目标 thumb: %s" % thumbDir)
    print("")
    print("  ⚠ 这是**覆盖式**恢复：现有 db\\ 与 thumb\\ 会被替换。")
    print("  ⚠ 恢复**不会**找回原图 —— 原图不在备份里（它只读、不可能丢）。")
    if not assumeYes:
        print("")
        answer = input("  确认恢复？输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("已取消。")
            return {"ok": False, "errMsg": "用户取消"}

    # ---- 先给现状拍一份，**这一步失败就不往下走** ----
    print("")
    print("① 先把现状另存为 pre-restore_%s ……" % stamp())
    safety = doBackup(destRoot=root, label="pre-restore")
    if not safety.get("ok"):
        print("[Error] 现状另存失败，**已中止恢复**（否则就是在没有退路的情况下覆盖）",
              file=sys.stderr)
        return {"ok": False, "errMsg": "现状另存失败，已中止"}

    print("② 替换 db\\ …")
    if os.path.isdir(dbDir):
        shutil.rmtree(dbDir)
    os.makedirs(dbDir, exist_ok=True)
    copyTree(os.path.join(source, DB_SUBDIR), dbDir)

    print("③ 替换 thumb\\ …")
    if os.path.isdir(thumbDir):
        shutil.rmtree(thumbDir)
    os.makedirs(thumbDir, exist_ok=True)
    copyTree(os.path.join(source, THUMB_SUBDIR), thumbDir)

    print("")
    print("恢复完成。")
    print("  恢复自     : %s" % source)
    print("  恢复前快照 : %s" % safety.get("path"))
    print("  重启 photo-browser 服务后即可使用；")
    print("  若库与缩略图不一致，可跑 python code/src/tools/gen_thumbs.py 重建缩略图。")
    return {"ok": True, "restoredFrom": source, "safetyBackup": safety.get("path")}


# ============================================================
# 五、CLI
# ============================================================

def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="backup.py", description="photo-browser 备份 / 恢复（**不含 photo\\ 原图**）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command")

    pBackup = sub.add_parser("backup", help="停服务后拷贝 db\\ + thumb\\")
    pBackup.add_argument("--dest", default=None, help="备份根目录（默认 <PHOTO_ROOT>\\backup）")
    pBackup.add_argument("--label", default=None, help="备注，写进 manifest")

    pList = sub.add_parser("list", help="列出已有备份")
    pList.add_argument("--dest", default=None, help="备份根目录")

    pRestore = sub.add_parser("restore", help="从备份还原 db\\ + thumb\\")
    pRestore.add_argument("--code", required=True, help="备份编号（list 里那一列）")
    pRestore.add_argument("--dest", default=None, help="备份根目录")
    pRestore.add_argument("--yes", action="store_true", help="跳过交互确认")

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    print("backup.py _VERSION: %s" % _VERSION)
    if not args.command:
        parser.print_help()
        return 1
    if args.command == "backup":
        result = doBackup(args.dest, args.label)
    elif args.command == "list":
        doList(args.dest)
        result = {"ok": True}
    else:
        result = doRestore(args.code, args.dest, args.yes)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())