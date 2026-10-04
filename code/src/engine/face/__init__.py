#! /usr/bin/env python3
#encoding: utf-8
#Filename: __init__.py
#Description: engine/face 人脸引擎（步骤 5）
#
# 三层的分工（**单写入者的边界就在这里**）
# -------------------------------------------
#   engine.py     纯计算。检测 + 提取 + 质量过滤。**不碰数据库**，
#                 因此可以在子进程里放心跑（pool.py）。
#   pool.py       ProcessPoolExecutor 封装。只调度，不连库。
#   faceStore.py  唯一写 pb_face 的地方。**只在主进程调用**。
#
# ⚠️ 反向依赖禁令：engine.py 与 pool.py **绝不允许** import faceStore
#    或任何 database.* / common.sqlite* 模块。子进程 import 到就是 database is locked。
#    pool.assertNoDatabaseImport() 会对三者做静态自查。
