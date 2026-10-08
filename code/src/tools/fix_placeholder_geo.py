#! /usr/bin/env python3
#encoding: utf-8

#Filename: fix_placeholder_geo.py
#Description: 存量 (0,0) 占位坐标清理（修正步骤 R3 一次性脚本，DR-25）——
#             两个阶段：① pb_photo.placeName 清回 NULL
#             ② pb_place 里那条「加纳」幽灵地点软删退场
#
# 为什么需要这个脚本（不修会怎样）
# --------------------------------
#   相机未定位时会往 EXIF 里写 (0,0)。它在**合法范围内**，所以修正前
#   meta.reverseGeocode() 那条只判范围的校验放它过关了：
#     if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0): return None
#   (0,0) 完美通过 -> reverse_geocoder 老老实实查了**几内亚 Takoradi** ->
#   placeName 落成 "GH, Western, Takoradi"。后果不报错、也不难看：
#     ① 地点视图里凭空多出一个「加纳」国家，点进去是 26 张聚会合影；
#     ② 真正有GPS 的 65 张被这26 张稀释，地点层的可信度被静默拉低；
#     ③ placeName 一旦落库就**再也不会自己变**—— 光改代码不清库，错的地点一直在。
#   代码改对之后（isRealCoordinate），只有**重新扫描**过的照片才会被纠正；
#   而重扫要等下一次全量扫描。这一步先把存量错数据当场清掉。
#
# 判据：复用 meta.isRealCoordinate（**同一个函数，不另写一份**）
# ----------------------------------------------------------
#   「入库时怎么判」与「清库时怎么判」必须一模一样，否则会出现
#   「清库脚本清掉了、扫描器又写回来」的拉锯。所以这里 import 生产代码里的
#   isRealCoordinate，而不是复制一份 `(0,0)` 判断 —— 阈值改了这里自动跟着改。
#
# 阶段① 只清 placeName，**不清 lat/lon**（结论与理由写在这里，别再改）
# ----------------------------------------------------------
# 只清placeName，**不清 lat/lon**（结论与理由写在这里，别再改）
# --------------------------------------------------------
#   * lat / lon = EXIF 里的**原始事实**。相机确实写了 (0,0)，这是真实发生的
#     一次「没定位」。把它抹成 NULL 等于**销毁原始信息**：将来导出给别的工具 /
#     做数据迁移时，"这台设备当时没定位"本身就是有用的信号，而NULL 只说明
#     "我们不知道"，两者的信息量不一样。
#   * placeName = **推断出来的结果**（离线逆地理库查的），而且已经证明查错了。
#     推断错的东西必须清掉 ——留着它就会以假乱真。
#   * 判定「有没有定位」用meta.isRealCoordinate 就够了，不需要靠清空 lat/lon
#     来表达「无定位」。清空 lat/lon 反而会让「有GPS 总数」这个指标失真
#     （26 张从 91 掉到 65，看不出"曾经有 91 张设备写了坐标"）。
#   * meta.parseExifObject() 也**故意保留** (0,0) 不抹掉，与本脚本口径一致。
#
# 阶段② 为什么 pb_place 也要清（阶段① 清完它还在）
# ----------------------------------------------
#   pb_place 是**从 pb_photo 派生的缓存字典**（placeStore.rebuildPlaces 全量复算）。
#   阶段① 把 26 行的 placeName 清成 NULL 之后，那条「加纳」行就变成了一条
#   **没有任何照片支撑的幽灵行**：photoCount 还停在 26、centerLat/Lon 还是
#   (0,0)（几内亚湾），名字还挂在地点列表里。而 `listPlaces()` 的 where
#   **没有 photoCount > 0 过滤**，所以它会带着 0 张照片继续显示 —— 用户看到
#   「加纳，0 张」，库里却是「加纳，26 张」，两边对不上且都不报错。
#   ⚠️ 顺序反了就白删：只要 pb_photo 里还有那 26 行，任何一次 rebuild 都会把它
#     原样 upsert 回来。所以阶段① 必须在阶段② 之前（本脚本固定这个顺序）。
#
# 阶段② 判据：「幽灵地点」= 三条同时成立（**不是按名字硬编码 GH%**）
#   ① 该 placeName 在 pb_photo 里**已经一张照片都不剩**（用 liveAggregatePlaces 求）；
#   ② `source != 1`（**不是手工命名**）—— 手工地点是用户的劳动成果，
#      「照片搬走了」不等于「这个地点该消失」，与 DR-19「只停用不删除」同理；
#   ③ 还没被软删过。
#   为什么不用「名字 == GH, Western, Takoradi」这种硬编码：那只能治这一次，
#   下次换个数据集、再冒出一个新的错误地点名就得改脚本。而上面三条是**判据**，
#   实测在正式库上**恰好只命中「加纳」这一行**（其余 16 个地点都有照片支撑）。
#
# 阶段② 软删而不是硬删（理由）
#   * placeStore 自己的纪律就是「**不删行**，手工地点/历史地点要留着」——
#     硬删会让这条纪律出现例外，而下一个要删什么行就没人敢定了；
#   * 软删可逆（delFlag 改回 '0'），而这条行留着本身就是「这里出过什么 bug」的记录；
#   * 软删**不会被 rebuild 复活**：rebuildPlaces 的 upsert 只碰本次 `seen` 到的
#     placeCode，且 `delFlag` 既不进 updateColumns 也不进 forceColumns；
#     它的归零循环只扫 `delFlag = '0'` 的行，同样跳过软删行。
#
# 幂等与安全
# ----------
#   * --dry-run 是默认行为（只报不改），必须显式 --apply 才动数据；
#   * placeName 本来就是 NULL 的行**跳过不写**（重复跑不会反复 UPDATE）；
#   * 阶段① 只改 placeName 一列；阶段② 只改 pb_place.delFlag 一列。
#     **lat/lon/relPath/faceCount/photoCount 等一律不动**；
#   * 两阶段都**不 INSERT、不 DELETE** —— 逐表行数不变（验收第 7 条）。
#   * 阶段① 走 upsert（insertManyTableGeneral + forceColumns），**不能用
#     update_pb_photo**：normalizeDataSet 会把 None 整条丢掉，而 update 的
#     语义是「只补空」，两者都写不进 NULL（与assigner._patchFace 同一个坑）。
#   * 阶段② 用 updateTableGeneral 按 placeCode 更新（与 placeStore 归零 photoCount
#     同一个写法）：它是纯 UPDATE，没有 INSERT 语义、不撞 NOT NULL，
#     也就**不需要带齐 pb_place 的身份列**。
#
# 用法
# ----
#   python code\src\tools\fix_placeholder_geo.py --dry-run
#   python code\src\tools\fix_placeholder_geo.py --apply
#   python code\src\tools\fix_placeholder_geo.py --dry-run --db d:\tmp\x.db
#   python code\src\tools\fix_placeholder_geo.py --dry-run --sample 50
#   python code\src\tools\fix_placeholder_geo.py --apply --skip-place
#
# 硬约束：只改库里的列；photo 目录只读（本脚本根本不碰文件）。

import argparse
import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                        # noqa: E402
from common import miscCommon as misc                              # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.place import placeStore                             # noqa: E402
from processor.scanner import meta as meta                          # noqa: E402

_VERSION = "20261007"

_LOG = misc.setLogNew("fixPlaceholderGeo", "fixplaceholdergeo.log")

#: 样例打印条数（只看前几条就够判断口径对不对）
_SAMPLE_NUM: int = 5
#: 一次事务的行数（与扫描器 COMMIT_ROWS_PER_TXN 同量级）
_COMMIT_STEP: int = 500


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/路径会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def scan(dbFile: str = None) -> dict:
    """扫一遍库，算出「谁该被清」。**不写任何一行。**

    走 sqliteCommon.query_pb_photo 取行（业务层禁止裸 SQL），
    占位判定逐行交给 meta.isRealCoordinate —— 与扫描器同一个判据。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)             # DR-10：必须显式才切库

    toFix, alreadyClean = [], 0
    stat = {"total": 0, "latNull": 0, "gpsReal": 0, "gpsPlaceholder": 0,
            "placeNameNotNull": 0}
    # delFlag="*"：软删行也一并清干净（错的地点名没道理留着，只是不显示而已）
    for row in sqliteCommon.query_pb_photo("pb_photo", mode="light", delFlag="*"):
        stat["total"] += 1
        lat, lon = row.get("lat"), row.get("lon")
        if lat is None:
            stat["latNull"] += 1
        if row.get("placeName"):
            stat["placeNameNotNull"] += 1
        if lat is None or lon is None:
            continue
        if meta.isRealCoordinate(lat, lon):
            stat["gpsReal"] += 1
            continue
        stat["gpsPlaceholder"] += 1
        if row.get("placeName"):
            toFix.append(row)
        else:
            alreadyClean += 1
    return {"toFix": toFix, "alreadyClean": alreadyClean, "stat": stat,
            "dbFile": sqliteCommon.dbFilePath()}


def scanGhostPlaces() -> dict:
    """扫 pb_place，找出「幽灵地点」。**不写任何一行。**

    判据见文件头阶段②：三条同时成立才算
    ① 该 placeName 在 pb_photo 里已一张照片都不剩；
    ② source != SOURCE_MANUAL（不是手工命名）；
    ③ 还没被软删过。

    ⚠️ 已软删的 placeCode 必须另外查，**不能读 listPlaces() 返回的 delFlag**：
       它的 SELECT 投影里**根本没有 delFlag 这一列**（只有 placeCode / placeName /
       source / photoCount / firstShotYear / lastShotYear / centerLat / centerLon /
       modifyYMDHMS）。踩过一次：判据写成 `one.get("delFlag")` -> 永远读到 None ->
       「已退场」永远不成立 -> 重复跑会把同一条行反复报告，而脚本看起来一切正常。
       这就是「静默失效」：不报错、统计还挺好，只是判据根本没生效。
       所以这里用 query_pb_place(delFlag='1') 单独取已软删集合。
    """
    live = placeStore.liveAggregatePlaces(limitNum=0)
    liveItems = live["items"] if isinstance(live, dict) else live
    liveNames = set(str(one.get("placeName") or "") for one in liveItems)

    retired = set(str(one.get("placeCode") or "")
                  for one in sqliteCommon.query_pb_place(
                      "pb_place", delFlag=comGD.DEL_FLAG_YES, mode="light"))

    ghosts, keepManual = [], []
    # delFlag="*"：连已软删的也一并看，避免重复报告同一行
    listed = placeStore.listPlaces(delFlag="*", limitNum=0)
    for one in listed["items"]:
        code = str(one.get("placeCode") or "")
        if code in retired:                          # 已退场，幂等：跳过
            continue
        name = str(one.get("placeName") or "")
        if int(one.get("source") or 0) == placeStore.SOURCE_MANUAL:
            keepManual.append(name)                  # 手工地点：照片没了也留着
            continue
        if name not in liveNames:
            ghosts.append(one)
    return {"ghosts": ghosts, "keepManual": keepManual,
            "liveCount": len(liveNames), "retiredCount": len(retired),
            "listed": listed["total"], "missingTable": listed["missingTable"]}


def _retirePlace(placeCode: str) -> int:
    """把一条幽灵地点**软删**（delFlag='1'）。返回影响行数（0 = 没改成）。

    ⚠️ 用 updateTableGeneral 而不是 upsert：这是纯 UPDATE，没有 INSERT 语义，
    所以**不需要带齐 pb_place 的 NOT NULL 身份列**（pb_photo 那边栽过这个坑）。
    ⚠️ `updateTableGeneral` 出错时返回 **0 而不是负数**，所以调用方只能按
    「> 0 才算真改了」计数 —— 错把 0 记成"已退场"会让统计数字看着对、实际漏改
    （与 placeStore 归零 photoCount 时踩的同一个坑）。
    """
    rtn = sqliteCommon.updateTableGeneral(
        "pb_place", "placeCode = %s", (str(placeCode),),
        {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()})
    return rtn if rtn and rtn > 0 else 0


#: pb_photo 里NOT NULL 且**没有 DEFAULT** 的列 —— upsert 走的是 INSERT 语义，
#: 缺任何一列都会撞 `NOT NULL constraint failed: pb_photo.<列>`。
#: 实测踩过：只带 photoCode 会撞 relPath（relPathHash / fileHash 同理）。
#: 好在它们只进INSERT 列清单、不在 updateColumns 里，所以绝不会被改写。
_IDENTITY_COLUMNS: tuple = ("photoCode", "relPath", "relPathHash", "fileHash")


def _clearPlaceName(rows: list) -> int:
    """把占位行的 placeName 清成 NULL（upsert + forceColumns，只改这一列）。

    ⚠️ 必须带齐 pb_photo 的 NOT NULL 身份列（见 _IDENTITY_COLUMNS）：upsert 走的是
    INSERT 语义，缺列会撞 `NOT NULL constraint failed: pb_photo.relPath`
    （与 fix_confirmed_flag._fixBytes 同一个坑，那次是 photoCode）。
    ⚠️ 行里**故意不写 placeName 键**（写 None 会被 normalizeDataSet 整条丢掉），
    由 forceColumns 保证该列恒在列清单里、缺键按 NULL 写入 = 显式清空。
    ⚠️ updateColumns 只含 placeName + modifyYMDHMS：身份列只用于让 INSERT 不违规，
    不会被「顺手」改写（lat/lon 一个字节都不动）。
    """
    payload = []
    for one in rows:
        row = {name: one.get(name) for name in _IDENTITY_COLUMNS}
        row["placeName"] = None
        row["modifyYMDHMS"] = misc.getTime()
        payload.append(row)
    rtn, _columns = sqliteCommon.insertManyTableGeneral(
        "pb_photo", payload,
        conflictColumns=("photoCode",),
        updateColumns=("placeName", "modifyYMDHMS"),
        fillStandard=True,
        forceColumns=("placeName",))
    return rtn


def main(argv=None) -> int:
    _fixConsole()
    parser = argparse.ArgumentParser(
        prog="fix_placeholder_geo.py",
        description="清理 (0,0) 占位坐标被误逆地理成的 placeName（清成 NULL）")
    parser.add_argument("--db", default="", help="库文件（缺省正式库）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报不改（默认行为）")
    parser.add_argument("--apply", action="store_true",
                        help="确认实跑（真正写库）")
    parser.add_argument("--sample", type=int, default=_SAMPLE_NUM,
                        help="清单样例打印条数（默认 %d）" % _SAMPLE_NUM)
    parser.add_argument("--skip-place", action="store_true",
                        help="只清 pb_photo.placeName，不动 pb_place 幽灵地点")
    args = parser.parse_args(argv)
    # 默认只报不改：改生产数据必须显式 --apply
    dryRun = bool(args.dry_run) or not bool(args.apply)

    print("fix_placeholder_geo _VERSION: %s" % _VERSION)
    print("meta._VERSION              : %s" % meta._VERSION)
    print("占位阈值 PLACEHOLDER_EPS   : %g 度（约 %.0f m）"
          % (meta.PLACEHOLDER_EPS, meta.PLACEHOLDER_EPS * 111320.0))
    report = scan(args.db or None)
    toFix = report["toFix"]
    stat = report["stat"]

    print("库                : %s" % report["dbFile"])
    print("\n---- 阶段① 清理前盘点（pb_photo）----")
    print("  pb_photo 总行数        : %d" % stat["total"])
    print("  lat IS NULL（无 GPS）  : %d" % stat["latNull"])
    print("  有 GPS 且非占位        : %d" % stat["gpsReal"])
    print("  占位坐标（会被清）     : %d" % stat["gpsPlaceholder"])
    print("  placeName 非空         : %d" % stat["placeNameNotNull"])
    print("\n---- 阶段① 待清清单 ----")
    print("  placeName 非空、需清成 NULL : %d 行" % len(toFix))
    print("  占位且 placeName 本来就空   : %d 行（跳过不写，幂等）"
          % report["alreadyClean"])
    if toFix:
        print("  样例 relPath / placeName：")
        for one in toFix[:max(0, args.sample)]:
            print("    %-52s lat=%-12s lon=%-12s %r"
                  % (str(one.get("relPath") or "")[:52],
                     one.get("lat"), one.get("lon"), one.get("placeName")))

    # ---- 阶段② 盘点：pb_place 幽灵地点（必须在阶段① 之后判，否则顺序反了白干）----
    ghosts, keepManual = [], []
    if args.skip_place:
        print("\n---- 阶段② pb_place ----")
        print("  已按 --skip-place 跳过")
    else:
        placeRep = scanGhostPlaces()
        ghosts = placeRep["ghosts"]
        keepManual = placeRep["keepManual"]
        print("\n---- 阶段② pb_place 幽灵地点 ----")
        if placeRep["missingTable"]:
            print("  pb_place 表不存在，跳过（不影响阶段①）")
        else:
            print("  pb_place 行数（含软删）  : %d" % placeRep["listed"])
            print("  pb_photo 里的地点数      : %d" % placeRep["liveCount"])
            print("  已软删（历史退场）       : %d 条，不重复处理"
                  % placeRep["retiredCount"])
            print("  幽灵地点（无照片支撑）   : %d 条，将软删退场" % len(ghosts))
            print("  手工地点（照片没了也留） : %d 条，不动" % len(keepManual))
            for one in ghosts[:max(0, args.sample)]:
                print("    %-34s photoCount=%-5s center=(%s, %s) src=%s"
                      % (str(one.get("placeName") or "")[:34],
                         one.get("photoCount"), one.get("centerLat"),
                         one.get("centerLon"), one.get("source")))

    if dryRun:
        print("\n[dry-run] 未做任何修改。确认无误后加 --apply 实跑。")
        return 0

    if not toFix and not ghosts:
        print("\n无需清理（占位行的 placeName 与幽灵地点本来就都是干净的）。")
        return 0

    # ---- 阶段① 执行：必须先清源头 ----
    written = 0
    for begin in range(0, len(toFix), _COMMIT_STEP):
        chunk = toFix[begin:begin + _COMMIT_STEP]
        rtn = _clearPlaceName(chunk)
        if rtn == -2:                            # sqliteHandle.RET_ERROR
            print("[Error] 第 %d 批写入失败: %s"
                  % (begin // _COMMIT_STEP + 1, sqliteCommon.dbHandle().lastErrMsg))
            return 1
        written += len(chunk)
        print("  已清 %d/%d 行" % (written, len(toFix)))

    # ---- 阶段② 执行：源头清干净了，幽灵地点才判定得出来 ----
    retired = 0
    if ghosts:
        print("\n阶段② 软删幽灵地点：")
        for one in ghosts:
            code = str(one.get("placeCode") or "")
            if not code:
                print("[Error] 幽灵地点缺 placeCode，跳过: %r" % one.get("placeName"))
                continue
            if _retirePlace(code) > 0:
                retired += 1
                print("  已退场 %-34s photoCount=%s"
                      % (str(one.get("placeName") or "")[:34], one.get("photoCount")))
            else:
                print("[Error] 软删失败（影响 0 行）: %s" % code)
                return 1

    after = scan(args.db or None)
    astat = after["stat"]
    print("\n实跑完成：阶段① 清 %d 行 / 阶段② 软删 %d 条" % (written, retired))
    print("\n---- 清理后盘点（pb_photo）----")
    print("  pb_photo 总行数        : %d（清理前 %d -> %s）"
          % (astat["total"], stat["total"],
             "不变 ✅" if astat["total"] == stat["total"] else "**变了，有问题**"))
    print("  placeName 非空         : %d（清理前 %d）"
          % (astat["placeNameNotNull"], stat["placeNameNotNull"]))
    print("  占位坐标（已不该有placeName）: %d"
          % len(after["toFix"]))
    print("  有 GPS 且非占位        : %d（未动）" % astat["gpsReal"])
    if not args.skip_place:
        afterPlace = scanGhostPlaces()
        print("---- 清理后盘点（pb_place）----")
        print("  pb_place 行数（含软删）: %d（清理前 %d -> %s）"
              % (afterPlace["listed"], placeRep["listed"],
                 "不变 ✅" if afterPlace["listed"] == placeRep["listed"]
                 else "**变了，有问题**"))
        print("  幽灵地点（已不该有）   : %d" % len(afterPlace["ghosts"]))
        print("  已软删（本次 + 历史）  : %d 条" % afterPlace["retiredCount"])
        print("  手工地点（未动）       : %d" % len(afterPlace["keepManual"]))
    _LOG.info("占位坐标清理: pb_photo 清 %d 行（总 %d -> %d，placeName 非空 %d -> %d）；"
              "pb_place 软删 %d 条",
              written, stat["total"], astat["total"],
              stat["placeNameNotNull"], astat["placeNameNotNull"], retired)
    return 0 if astat["total"] == stat["total"] else 1


if __name__ == "__main__":
    sys.exit(main())