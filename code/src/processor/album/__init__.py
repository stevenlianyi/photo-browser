#! /usr/bin/env python3
#encoding: utf-8

#Filename: __init__.py
#Description: processor 层：album 文件夹浏览（R16 · DR-59~63）
#
# 这一层是「业务层」，只允许调 database.auto_generated.sqliteCommon、
# database.queryCommon 与 common，**禁止裸 SQL、禁止直接 import tools/**。
#
# 「文件夹」浏览是**纯派生视图**：目录与张数全部现推自 `pb_photo.relPath`，
# **不落任何表**（DR-60）。所以这个包里只有一个模块 `dirPath.py`，它提供
# 「目录键」的唯一口径 —— SQL 侧与 Python 侧两套等价实现。
#
# ⚠️ 为什么口径必须只写一份
# --------------------------
# 目录键的消费点有三处：① 目录树端点（GROUP BY 聚合）② 照片列表筛选
# （WHERE 等值）③ 前端展示（面包屑 / 树节点的名称）。
# 三处任何一处自己再写一遍 `rtrim(...)` 或 `split('/')`，症状都是
# 「树说 45 张、网格说 44 张」—— 接口 200、两处各自看都对、**没有任何报错**。
# 这与 `placeStore.placeKeySql` / `placeKeyOf` 是同一条纪律。
