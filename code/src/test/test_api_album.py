#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_album.py
#Description: 文件夹浏览（R16 · DR-59~63）的测试
#
# 覆盖（对应 plan/文件夹浏览规划.md 第八节的验收清单）
#   * **口径同源**：`dirKeySql`（SQL）与 `dirKeyOf`（Python）对**现网数据**与
#     边界样本给同一个答案 —— 这是本功能唯一的真风险点
#   * **树张数 == 照片列表 total**（逐目录对照，走两条独立代码路径）
#   * **仅直属**：不含子目录的照片
#   * **通配符反例**：目录名含 `_` / `%` 时不多报（用 LIKE 实现时本用例会红）
#   * **根目录**：保留 token / 空串 / 不传参三种写法等价，且"不传参"**不等于**全库
#   * 软删照片不进任何一层计数
#   * 空状态两种（库里没有照片那层 / 目录在库里已不存在）
#   * 非法目录键 -> 400（而不是返回一个"空目录"）
#   * 树组装：补中间层、递归张数、数字感知排序（纯函数，不查库）
#   * OpenAPI：端点存在、且**没有** export / DELETE
#
# ⚠️ 为什么「仅直属」与「通配符反例」要单独写用例
#   它们都是**静默多报**：用 `LIKE 'dir/%'` 实现时接口照样 200、结构完好、
#   只是多几张 —— 用户不会发现，除非他数过。这类缺陷只有用例能守住。

import pytest


# ============================================================
# 夹具：在 api_env 的构造数据上再补几类目录
# ============================================================

@pytest.fixture
def album_env(api_env, api_rows):
    """补照片，覆盖：根目录文件 / 子目录 / 下划线与百分号目录 / 软删。

    ⚠️ **诱饵目录是刻意造的**（`2016-05-01-三亚` / `50/off`）：
       `LIKE '2016_05_01_三亚/%'` 里 `_` 是「任意一个字符」，会命中
       `2016-05-01-三亚`；`LIKE '50%off/%'` 里 `%` 是「任意串」，会命中
       `50/off`。⇒ 一旦有人把等值写成 LIKE，下面两条断言会**当场变红**。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon

    rows = [
        # 直接放在 photo 根下 -> 目录键是空串
        ("PH_ROOT", "root_only.jpg", "rh_root", "fh_root", 0),
        # 子目录（验证"仅直属"：它的父目录 2013/01 不能被算进去）
        ("PH_DEEP", "2013/01/sub/deep.jpg", "rh_deep", "fh_deep", 0),
        # 下划线目录 + 诱饵（连字符）
        ("PH_UNDER", "2016_05_01_三亚/IMG_1.jpg", "rh_under", "fh_under", 0),
        ("PH_UNDER_DECOY", "2016-05-01-三亚/IMG_1.jpg",
         "rh_underD", "fh_underD", 0),
        # 百分号目录 + 诱饵（斜杠）
        ("PH_PCT", "50%off/1.jpg", "rh_pct", "fh_pct", 0),
        ("PH_PCT_DECOY", "50/off/1.jpg", "rh_pctD", "fh_pctD", 0),
        # 软删：落在 2013/01 下，**不能**被算进任何一层
        ("PH_DEL", "2013/01/deleted.jpg", "rh_del", "fh_del", 1),
    ]
    sqliteCommon.insertManyTableGeneral(
        "pb_photo",
        [{"photoCode": code, "relPath": rel, "relPathHash": rh, "fileHash": fh,
          "fileSize": 1000, "mimeType": "image/jpeg",
          "takenAt": "2021-01-01T00:00:00Z", "shotYear": 2021,
          "faceCount": 0, "isDuplicate": 0, "isMissing": 0, "scanState": 1,
          "delFlag": delFlag, "regID": "test"}
         for code, rel, rh, fh, delFlag in rows],
        conflictColumns=("photoCode",),
        updateColumns=("relPath", "delFlag", "faceCount"),
        fillStandard=True, forceColumns=("delFlag",))
    return api_env


def albumPath(env, **params):
    """调 `GET /api/albums/path`（不传 path = 根目录）。"""
    got = env["client"].get("/api/albums/path", params=params)
    assert got.status_code == 200, got.text
    return got.json()


def photoTotalOf(env, dirPath):
    """`GET /api/photos?dirPath=` 的 total（**与树端点完全独立的取数路径**）。"""
    got = env["client"].get("/api/photos", params={"dirPath": dirPath, "size": 1})
    assert got.status_code == 200, got.text
    return int(got.json()["total"])


def walkTree(node, out=None):
    """把树拍平成 `{目录键: 节点}`（断言用）。"""
    out = {} if out is None else out
    out[node["path"]] = node
    for child in node["children"]:
        walkTree(child, out)
    return out


# ============================================================
# 一、口径同源（本功能唯一的真风险点）
# ============================================================

def test_dirKeySqlEqualsPythonOnRealRows(album_env, api_rows):
    """**SQL 表达式与 Python 实现必须给同一个答案** —— 对现网每一行 `relPath`。

    ⚠️ 这条是本功能的"地基"：两套口径不一致的症状是
       「树说 45 张、网格说 44 张」，接口 200、两处各自看都对。
       所以不放边界样本了事，而是拿**库里真实存在的每一行**跑一遍。
    """
    from database import queryCommon as query
    from processor.album import dirPath

    sql = ("SELECT p.relPath AS relPath, " + dirPath.dirKeySql("p") +
           " AS dirKey FROM pb_photo p")
    rows = query.selectList(sql)
    assert rows, "构造数据里应当有照片"
    for one in rows:
        assert (one.get("dirKey") or "") == dirPath.dirKeyOf(one.get("relPath")), \
            "两套口径不一致：relPath=%r" % one.get("relPath")


def test_dirKeySqlEqualsPythonOnEdgeSamples():
    """边界样本也必须一致（含无分隔符 / 尾斜杠 / 重复分隔符 / 前导斜杠）。"""
    import sqlite3

    from processor.album import dirPath

    samples = ["abc.jpg", "a/b/c.jpg", "a/b/c/d/e.jpg", "2013.07.26 华盛顿/x.jpg",
               "2016_05_01_三亚/IMG_1.JPG", "50%off/1.jpg", "a/b/c", "a/b/",
               "a//b/c.jpg", "/a.jpg", "", "a/b/c.jpg/"]
    handle = sqlite3.connect(":memory:")
    handle.execute("CREATE TABLE t (relPath TEXT)")
    handle.executemany("INSERT INTO t VALUES (?)", [(one,) for one in samples])
    for relPath, sqlKey in handle.execute(
            "SELECT relPath, " + dirPath.dirKeySql("t") + " FROM t").fetchall():
        assert (sqlKey or "") == dirPath.dirKeyOf(relPath), \
            "两套口径不一致：relPath=%r" % relPath
    handle.close()


def test_treeCountMatchesPhotoListTotal(album_env):
    """**树上每个节点的直属张数 == `/api/photos?dirPath=` 的 total**。

    走两条完全独立的代码路径（一次 GROUP BY 聚合 vs 等值筛选 + COUNT），
    它们必须逐目录合得上 —— 这正是"口径只写一份"要保证的性质。
    """
    tree = albumPath(album_env)["root"]
    nodes = walkTree(tree)
    assert len(nodes) >= 5, "构造数据应当有多个目录"
    for path, node in sorted(nodes.items()):
        assert node["count"] == photoTotalOf(album_env, path), \
            "目录 %r 的树张数与照片列表 total 不一致" % path


def test_directCountEqualsSubtreeMinusChildren(album_env):
    """`totalCount`（递归）必须等于 `count`（直属）加上全部子节点的递归张数。"""
    tree = albumPath(album_env)["root"]

    def check(node):
        kids = sum(child["totalCount"] for child in node["children"])
        assert node["totalCount"] == node["count"] + kids, node["path"]
        for child in node["children"]:
            check(child)

    check(tree)
    # 根节点的递归张数 == 全库未软删照片数（软删那张不能算进来）
    resp = albumPath(album_env)
    assert tree["totalCount"] == resp["photoTotal"]


# ============================================================
# 二、仅直属 / 通配符反例
# ============================================================

def test_directOnlyExcludesSubdirectories(album_env):
    """`2013/01` 只有 1 张（`a.jpg`）；它的子目录 `2013/01/sub` 那张**不算进来**。"""
    assert photoTotalOf(album_env, "2013/01") == 1
    # 而子目录自己是另一个目录键，单独可达
    assert photoTotalOf(album_env, "2013/01/sub") == 1
    # 树里 `2013/01` 的直属张数与它一致（不是 2）
    nodes = walkTree(albumPath(album_env)["root"])
    assert nodes["2013/01"]["count"] == 1
    assert nodes["2013/01"]["totalCount"] == 2      # 递归才含子目录
    assert nodes["2013/01/sub"]["count"] == 1       # 子目录有自己的直属张数
    assert nodes["2013"]["count"] == 0              # 中间层没有直属照片
    # 递归 = 2013/01 的 2 张 + 2013/07 的 1 张
    assert nodes["2013"]["totalCount"] == 3
    assert nodes["2013/07"]["count"] == 1


def test_underscoreInDirNameDoesNotOverMatch(album_env):
    """目录名里的 `_` 不是通配符：`2016_05_01_三亚` 只能命中它自己。

    ⚠️ 用 `LIKE` 实现时，诱饵目录 `2016-05-01-三亚`（连字符）会被 `_` 匹配上
       ⇒ total 变成 2，本用例变红。
    """
    assert photoTotalOf(album_env, "2016_05_01_三亚") == 1
    assert photoTotalOf(album_env, "2016-05-01-三亚") == 1


def test_percentInDirNameDoesNotOverMatch(album_env):
    """目录名里的 `%` 不是通配符：`50%off` 只能命中它自己。

    ⚠️ 用 `LIKE` 实现时，诱饵目录 `50/off` 会被 `50%off/` 里的 `%` 匹配上
       （`%` 连斜杠都能吃）⇒ total 变成 2，本用例变红。
    """
    assert photoTotalOf(album_env, "50%off") == 1
    assert photoTotalOf(album_env, "50/off") == 1


# ============================================================
# 三、根目录三种写法
# ============================================================

def test_rootThreeWaysAreEquivalent(album_env):
    """根目录的三种写法**必须等价**，且**都不等于"全库"**。

    ⚠️ 为什么单列一条：前端 `api/request.js` 的 `paramsSerializer` 会丢弃空串，
       所以「不传参数」这条路**必然**被走到 —— 它一旦被实现成"不筛选"，
       用户进了根目录却看到**全库照片**，且不报任何错。
       这里把「根目录 == 只取无斜杠 relPath」钉死。
    """
    omitted = albumPath(album_env)
    empty = albumPath(album_env, path="")
    token = albumPath(album_env, path="__root__")
    for one in (empty, token):
        assert one["path"] == "" == omitted["path"]
        assert one["directCount"] == omitted["directCount"]
    # 根目录只有 1 张（PH_ROOT），**远小于**全库
    assert omitted["directCount"] == 1
    assert omitted["photoTotal"] > omitted["directCount"], \
        "根目录的直属张数不能被算成全库张数"
    # 照片列表侧同理：返回的每一行都必须是**无斜杠**的 relPath
    got = album_env["client"].get("/api/photos",
                                  params={"dirPath": "/root", "size": 60})
    assert got.status_code == 400, "非法键要 400，不能静默给一个空目录"
    got = album_env["client"].get("/api/photos",
                                  params={"dirPath": "__root__", "size": 60})
    items = got.json()["items"]
    assert len(items) == 1
    assert "/" not in items[0]["relPath"]


# ============================================================
# 四、软删 / 空状态 / 非法键
# ============================================================

def test_softDeletedPhotoIsNotCountedAnywhere(album_env):
    """软删那张（`2013/01/deleted.jpg`）不进任何一层计数。

    ⚠️ 它**必须**落在有真实照片的目录里才有意义：若它独占一个目录，
       "该目录消失"与"该目录张数为 0"看起来一样，测不出漏算 `delFlag`。
    """
    resp = albumPath(album_env, path="2013/01")
    assert resp["directCount"] == 1, "软删那张被算进来了"
    # 全库合计也不含它
    allDirect = walkTree(albumPath(album_env)["root"])
    assert allDirect["2013/01"]["count"] == 1
    # 它自己不该在照片列表里出现
    got = album_env["client"].get("/api/photos",
                                  params={"dirPath": "2013/01", "size": 60})
    assert [one["photoCode"] for one in got.json()["items"]] != ["PH_DEL"]
    assert "PH_DEL" not in [one["photoCode"] for one in got.json()["items"]]


def test_missingDirGivesReadableEmptyState(album_env):
    """目录在库里已不存在：`pathExists=false` + 一句人话，**面包屑仍给全**。

    ⚠️ 这是「旧链接 / 目录改名」的常见现场。返回一个只剩标题的空页面
       会被读成"页面坏了"；给出完整面包屑 + 说明才能让人明白发生了什么。
    """
    resp = albumPath(album_env, path="2013/01/已经没有这个目录")
    assert resp["path"] == "2013/01/已经没有这个目录"
    assert resp["pathExists"] is False
    assert resp["directCount"] == 0
    assert resp["note"], "路径不存在时必须给一句说明，不能静默"
    # 面包屑由外向内、含根，且中间层仍带真实张数
    assert [one["path"] for one in resp["breadcrumb"]] == \
        ["", "2013", "2013/01", "2013/01/已经没有这个目录"]
    assert resp["breadcrumb"][2]["count"] == 1
    # 路径不存在**不影响**整棵树的可用性（树照旧给全）
    assert walkTree(resp["root"])["misc"]["count"] == 2


def test_emptyLibraryIsDistinguishableFromEmptyDir(api_env):
    """空状态两种要能分开：`photoTotal == 0`（库里没照片）vs `directCount == 0`。

    ⚠️ 只给一个 0 是分不出来的 —— 而两者的用户动作完全不同
       （"去扫描" vs "换个目录"）。
    """
    resp = albumPath(api_env, path="")
    assert resp["pathExists"] is True
    assert resp["photoTotal"] > 0
    assert resp["directCount"] == 0, "构造数据里根目录没有直属照片"
    assert resp["subDirCount"] > 0, "根目录有子目录 —— 界面据此说'左侧有 N 个子目录'"
    assert resp["note"] is None, "目录存在时不该给 note"


@pytest.mark.parametrize("bad", ["\\windows\\style", "/abs/path", "a/../b", "a/./b"])
def test_invalidDirKeyIs400(album_env, bad):
    """非法目录键 -> **400**，而不是返回一个"空目录"。

    ⚠️ 这些键 100% 匹配不到数据（本功能不碰文件系统）。返回空目录会让用户
       去查"是不是照片被删了"；400 才能让人立刻改对。
    """
    got = album_env["client"].get("/api/albums/path", params={"path": bad})
    assert got.status_code == 400, got.text
    assert got.json()["code"] == "PARAM_INVALID"
    got = album_env["client"].get("/api/photos", params={"dirPath": bad})
    assert got.status_code == 400, got.text


def test_breadcrumbLeadsToRoot(album_env):
    """面包屑由外向内、首段是根、末段是当前目录，各段都带张数。"""
    resp = albumPath(album_env, path="2013/01")
    crumbs = resp["breadcrumb"]
    assert crumbs[0]["path"] == "" and crumbs[-1]["path"] == "2013/01"
    assert crumbs[-1]["count"] == 1
    assert crumbs[-1]["totalCount"] == 2
    assert resp["pathName"] == "01"
    assert resp["directCount"] == 1


# ============================================================
# 五、锚点定位（「从目录里点开一张老照片后左右翻页」的必需项）
# ============================================================

def test_anchorLocatesPhotoInsideDirFilter(album_env):
    """`dirPath` + `anchorPhotoCode` 必须能定位到**该目录内**的那一页。

    ⚠️ 这是本功能与 R6（DR-31）的交界处，也是最容易漏改的地方：
       `anchorOffsetOf(cond, values, ...)` 用的是**同一个 cond**，所以
       `dirPath` 只要进了 `where`，锚点就自动跟着生效 —— 但"自动生效"这件事
       必须有用例守着（漏了 `values.append(...)` 时它会静默退回第 1 页，
       表现为「从这个目录点开的老照片，两个箭头都禁用」）。
       这里用 `size=1` 把 2 张照片撑成 2 页，`2013/01` 里那张之外再挑 `2024/06`。
    """
    got = album_env["client"].get(
        "/api/photos", params={"dirPath": "2024/06", "size": 1,
                               "anchorPhotoCode": "PH_2024_06b"})
    assert got.status_code == 200, got.text
    body = got.json()
    # 该目录按 takenAt DESC：PH_2024_06(08:08:08) 在前、PH_2024_06b(08:09:08) 在后？
    # 实际 DESC 是「时间大的在前」⇒ PH_2024_06b 在前、PH_2024_06 在后。
    # 无论哪种，锚点算出来的页必须**正好包含它自己**。
    assert body["total"] == 2
    assert [one["photoCode"] for one in body["items"]] == ["PH_2024_06b"], \
        "锚点没有定位到它所在的那一页（返回了 %r，page=%s）" \
        % ([one["photoCode"] for one in body["items"]], body["page"])
    assert body["page"] == 1
    # 换一张：另一张应当落在第 2 页
    got = album_env["client"].get(
        "/api/photos", params={"dirPath": "2024/06", "size": 1,
                               "anchorPhotoCode": "PH_2024_06"})
    body = got.json()
    assert [one["photoCode"] for one in body["items"]] == ["PH_2024_06"]
    assert body["page"] == 2, "第 2 张照片必须被定位到第 2 页（size=1）"


def test_anchorOutsideDirFilterFallsBackToFirstPage(album_env):
    """锚点**不在**该目录里时不定位（按原 page 返回），不会给出一个错的页。"""
    got = album_env["client"].get(
        "/api/photos", params={"dirPath": "50%off", "size": 1, "page": 1,
                               "anchorPhotoCode": "PH_2024_06b"})
    body = got.json()
    assert body["page"] == 1
    assert [one["photoCode"] for one in body["items"]] == ["PH_PCT"]


# ============================================================
# 六、纯函数：树组装 / 排序 / 归一（不查库）
# ============================================================

def test_buildTreeCompletesAncestorsAndSumsTotals():
    """**中间层目录没有直属照片时也必须出现在树里**（否则那个分支点不到）。"""
    from api import album

    counts = {"a/b/c": 5, "a/b/d": 3, "x": 2}
    root, nodes = album.buildTree(counts)
    assert sorted(nodes.keys()) == ["", "a", "a/b", "a/b/c", "a/b/d", "x"]
    assert nodes["a/b"]["count"] == 0, "中间层没有直属照片"
    assert nodes["a/b"]["totalCount"] == 8
    assert nodes["a"]["totalCount"] == 8
    assert nodes["a"]["hasChildren"] is True
    assert root["totalCount"] == 10 == sum(counts.values())
    assert root["count"] == 0
    # 父节点里挂的子节点引用必须**就是** nodes 里那些节点（同一个对象）
    assert nodes["a"]["children"][0] is nodes["a/b"]


def test_nameSortIsNumberAware():
    """数字段按数值比：`2013.7.1` 必须排在 `2013.10.01` **前面**。

    ⚠️ 纯字符串序会把 `2013.10.01` 排在前面（'1' < '7'），而本库目录名
       大量以日期开头 —— 那会让树里的顺序看起来"莫名其妙"。
    """
    from processor.album import dirPath

    names = ["2013.10.01 a", "2013.7.1 b", "2016_05_01_三亚", "BaiRuiQin",
             "bairuiqin", "MOT Friends", "50%off", "2013"]
    got = dirPath.sortDirNames(names)
    assert got.index("2013.7.1 b") < got.index("2013.10.01 a")
    assert got.index("50%off") < got.index("2013"), "数值比：50 < 2013"
    assert got.index("2013") < got.index("2016_05_01_三亚")
    # 同键不同名（大小写）也必须有确定顺序（否则刷新两次顺序会跳）
    assert got.index("BaiRuiQin") < got.index("bairuiqin")
    assert dirPath.sortDirNames(names) == got, "排序必须是确定的"
    # 无数字时退化为普通（小写）字符串比较
    assert dirPath.nameSortKey("Family") < dirPath.nameSortKey("Friends")


def test_normalizeAndInvalidReason():
    """归一与校验：保留 token -> 空串；反斜杠 / 绝对路径 / `.` `..` 一律非法。"""
    from processor.album import dirPath

    assert dirPath.normalizeDirKey(None) == ""
    assert dirPath.normalizeDirKey("") == ""
    assert dirPath.normalizeDirKey("  ") == ""
    assert dirPath.normalizeDirKey(dirPath.ALBUM_ROOT_TOKEN) == ""
    assert dirPath.normalizeDirKey(" 2013/01 ") == "2013/01"
    assert dirPath.normalizeDirKey("/2013/01/") == "2013/01", "去掉首尾分隔符"
    assert dirPath.normalizeDirKey("2013//01") == "2013/01", "折叠重复分隔符"
    # 合法
    for good in (None, "", "__root__", "2013/01", "2016_05_01_三亚", "50%off"):
        assert dirPath.invalidReason(good) == "", good
    # 非法（每条都要说得出现由）
    for bad in ("a\\b", "/abs", "a/../b", "./a", ".."):
        assert dirPath.invalidReason(bad), bad


def test_dirNameParentAndAncestors():
    """三个派生函数的口径（树节点名 / 父键 / 祖先前缀）。"""
    from processor.album import dirPath

    assert dirPath.dirNameOf("") == ""
    assert dirPath.dirNameOf("a") == "a"
    assert dirPath.dirNameOf("a/b/c") == "c"
    assert dirPath.parentKeyOf("") == "", "根的父是根本身（不是 None，免得漏判死循环）"
    assert dirPath.parentKeyOf("a") == ""
    assert dirPath.parentKeyOf("a/b/c") == "a/b"
    assert dirPath.ancestorsOf("") == [""]
    assert dirPath.ancestorsOf("a/b/c") == ["", "a", "a/b", "a/b/c"]


# ============================================================
# 七、OpenAPI / 只读纪律
# ============================================================

def test_albumPathIsInOpenApiAndReadOnly(api_env):
    """端点出现在 OpenAPI 里，且本模块**只**有 GET（无 export / 无 DELETE）。"""
    paths = api_env["client"].get("/openapi.json").json()["paths"]
    assert "/api/albums/path" in paths
    assert sorted(paths["/api/albums/path"].keys()) == ["get"]
    for path, methods in paths.items():
        assert "delete" not in methods, "%s 暴露了 DELETE" % path
    # 本功能**只读**：写方法一律不该被接受
    for method in ("post", "patch", "put", "delete"):
        got = getattr(api_env["client"], method)("/api/albums/path")
        assert got.status_code in (404, 405), \
            "%s /api/albums/path 竟然返回 %d" % (method, got.status_code)


def test_assertSelectStillBlocksWriteForms():
    """R16 为 `replace()` 放行之后，只读闸**对写语句必须照旧全拦**。

    ⚠️ 这条用例是那次放行的「对价」。`assertSelect` 原来写的是
       「出现 `REPLACE` 就拒」，而目录键偏偏要用 `replace(X,Y,Z)` 这个**标量函数**
       —— 放行它时唯一正确的做法是**按形态**区分（语句的 `REPLACE` 后面是 `INTO`，
       函数的后面是 `(`），而不是把 `REPLACE` 从黑名单里删掉。
       所以这里把「各种写形态仍然被拦」逐条钉住：漏掉任何一条，
       本模块就不再是"结构性只读"了。
    """
    from database import queryCommon as query

    # 该放行的：函数形态（含带空格的写法）
    for good in ("SELECT replace(a, 'x', 'y') FROM pb_photo",
                 "SELECT REPLACE (a, 'x', 'y') FROM pb_photo",
                 "SELECT rtrim(rtrim(p.relPath, replace(p.relPath,'/','')), '/')"
                 " FROM pb_photo p"):
        assert query.assertSelect(good), good
    # 必须拦下的：语句形态 + 其余写关键词
    for bad in ("REPLACE INTO pb_photo (a) VALUES (1)",
                "INSERT OR REPLACE INTO pb_photo (a) VALUES (1)",
                "INSERT INTO pb_photo (a) VALUES (1)",
                "UPDATE pb_photo SET a = 1",
                "DELETE FROM pb_photo",
                "DROP TABLE pb_photo",
                "ALTER TABLE pb_photo ADD COLUMN x TEXT",
                "CREATE TABLE x (a TEXT)",
                "ATTACH DATABASE 'x.db' AS y",
                "PRAGMA table_info(pb_photo)",
                "VACUUM",
                "SELECT 1; DELETE FROM pb_photo"):
        with pytest.raises(query.QuerySqlError):
            query.assertSelect(bad)


def test_albumBrowseDoesNotTouchAnyTable(album_env, api_rows):
    """跑一轮目录浏览后，五张业务表的行数**逐表不变**（DR-60 的只读承诺）。

    ⚠️ 只断言 `/api/albums/path` 与 `/api/photos?dirPath=` 这两条**新的读取路径**
       不写库；`pb_review_log` 也在断言里 —— 它是最容易被"顺手记一笔"的表。
    """
    tables = ("pb_photo", "pb_face", "pb_photo_person", "pb_person_centroid",
              "pb_review_log")
    before = {one: api_rows.count(one) for one in tables}
    for path in ("", "2013", "2013/01", "2013/01/sub", "misc", "50%off"):
        albumPath(album_env, path=path)
        photoTotalOf(album_env, path)
    after = {one: api_rows.count(one) for one in tables}
    assert before == after, "目录浏览写库了：%r -> %r" % (before, after)
