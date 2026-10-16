#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_rebucket_cli.py
#Description: rebucket_cli.py「默认只读」的回归测试（R17 实际踩到的生产数据事故）
#
# 为什么这组必须有，且必须单独放一个文件
# --------------------------------------
#   `rebucket_cli.py` 的承诺是「**默认只报不改**，改生产数据必须显式 --yes」。
#   而它的实现里，判定是在 `main()` 算出来的：
#       dryRun = bool(args.dry_run) or not bool(args.yes)
#   三个 cmd（cmdAll / cmdPerson / cmdPhoto）却读的是 `args.dry_run` ——
#   **"用户没给 --dry-run"与"用户没给 --yes"是两个不同的条件**。
#   于是出现最坏组合：main 打印「[dry-run] 未写任何一行」，
#   而 rebucketAll(dryRun=False) 已经把 125 行 shotBucket 写进了正式库。
#
#   test_rebucket.py 的 `test_dry_run_changes_nothing` **拦不住它**：
#   那条测的是模块层 `rebucketFace(dryRun=True)`，而模块层一直是对的。
#   参数传递这一环只有 CLI 层能覆盖 —— 所以本文件只做这一件事。
#
# 全部跑在 pytest 的 tmp 临时库上（temp_db 夹具 + dbHandle 单例），
# **绝不动 d:\PhotoLib 正式库**（conftest 的 guard_production_db 也会拦）。

import numpy as np
import pytest

from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from engine.face import engine as faceEngine
from engine.match import bucket as bucket
from engine.match import rebucket as rebucket
from tools import rebucket_cli as rebucket_cli

BIRTH_ADULT: str = "1975-04-02"        # 2000 年拍 -> 25 岁 -> 成年段（宽 10）
SHOT_ADULT: int = 2000


def unit(seed: int):
    rng = np.random.RandomState(seed)
    return faceEngine.l2normalize(rng.randn(1, basicSettings.EMBEDDING_DIM).ravel())


def addStaleFace(personCode="P01", faceCode="fc_1"):
    """造一张**已归属 + 有生日 + 桶键还是等宽占位桶**的脸。

    这正是 R17 事故里的那种行：`assigner` 没刷到、桶停在 faceStore 落的占位桶，
    与按生日算出的自适应桶不一致 -> `centroid._assertBucketOrder` 会抛 BucketStaleError。
    """
    sqliteCommon.insertManyTableGeneral(
        "pb_person", [{"personCode": personCode, "displayName": "小明",
                       "birthday": BIRTH_ADULT, "source": 0, "isConfirmed": 0}],
        conflictColumns=("personCode",), fillStandard=True)
    sqliteCommon.insertManyTableGeneral(
        "pb_photo", [{"photoCode": "PH_1", "relPath": "PH_1.jpg",
                      "relPathHash": "PH_1".ljust(64, "0")[:64],
                      "fileHash": "PH_1".ljust(64, "1")[:64],
                      "fileSize": 1024, "shotYear": SHOT_ADULT}],
        fillStandard=True)
    placeholder = bucket.bucketKeyOf(SHOT_ADULT, None)          # 等宽 5 年占位桶
    sqliteCommon.insertManyTableGeneral(
        "pb_face", [{"faceCode": faceCode, "photoCode": "PH_1",
                     "personCode": personCode, "isConfirmed": 1, "isStranger": 0,
                     "shotBucket": placeholder, "detScore": 0.9, "quality": 0.8,
                     "embedding": faceEngine.encodeEmbedding(unit(1))}],
        fillStandard=True)
    return placeholder, rebucket.shotBucketFor(SHOT_ADULT, BIRTH_ADULT)


def bucketOf(faceCode):
    return str(sqliteCommon.query_pb_face("pb_face", faceCode=faceCode)[0]
               .get("shotBucket") or "")


def widthOf(faceCode):
    return bucket.bucketWidth(bucketOf(faceCode))


@pytest.fixture
def lib(temp_db):
    sqliteCommon.dbHandle(temp_db)
    return temp_db


class TestCliDefaultIsReadOnly:

    def test_all_without_yes_writes_nothing(self, lib):
        """`--all`（不带 --yes）**必须一行都不写** —— 这是 R17 真实踩到的那条。

        同时带上 --recompute：重算也必须被 dry-run 拦住
        （否则"只读巡检"会顺带把质心写一遍，比刷桶更难发现）。
        """
        placeholder, adaptive = addStaleFace()
        assert widthOf("fc_1") == 5, "前提：夹具造的是占位桶（stale）"

        rc = rebucket_cli.main(["--all", "--recompute"])
        assert rc == 0
        assert bucketOf("fc_1") == placeholder, \
            "没给 --yes 就把 shotBucket 写库了 —— CLI 的「默认只读」被破坏"
        assert widthOf("fc_1") == 5
        assert bucketOf("fc_1") != adaptive

    def test_person_without_yes_writes_nothing(self, lib):
        """`--person` 的默认只读同样不能破（错误消息里推荐的就是这条命令）"""
        placeholder, _adaptive = addStaleFace()
        rc = rebucket_cli.main(["--person", "P01"])
        assert rc == 0
        assert bucketOf("fc_1") == placeholder
        assert widthOf("fc_1") == 5

    def test_all_with_yes_actually_writes(self, lib):
        """反向保险：给了 --yes 就必须真的写（别修成"永远不写"）"""
        placeholder, adaptive = addStaleFace()
        rc = rebucket_cli.main(["--all", "--yes"])
        assert rc == 0
        assert bucketOf("fc_1") == adaptive, "--yes 却什么都没改"
        assert widthOf("fc_1") == 10, "有生日 + 成年 -> 必须是 10 年自适应桶"
        assert adaptive != placeholder
