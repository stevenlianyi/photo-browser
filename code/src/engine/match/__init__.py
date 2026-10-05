#! /usr/bin/env python3
#encoding: utf-8

#Filename: __init__.py
#Description: engine/match 人脸分桶 + 质心 + 匹配决策（步骤 6）
#
# 三块职责，边界很硬
# ------------------
#   bucket.py    **纯函数**。拍摄年 + 出生年 -> 年代桶键。不碰数据库、不 import numpy。
#   centroid.py  pb_person_centroid 的唯一写入口 + 全量加载成矩阵。
#   matcher.py   三段式决策。**只写库不做 IO**（不读 photoDir、不裁图、不碰缩略图）。
#
# ⚠️ 反向依赖禁令：bucket.py **绝不允许** import centroid / matcher / 任何
#    database.* 模块。它是"给定两个整数算出一个字符串"的东西，
#    一旦它开始查库，就再也没法在单测里穷举边界了 —— 而边界正是本步最容易错的地方
#    （0/3/17/18/19/70 岁各自落在哪个桶，全靠单测钉住）。
