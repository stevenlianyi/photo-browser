#! /usr/bin/env python3
#encoding: utf-8

#Filename: __init__.py
#Description: processor 层：place 地点字典（pb_place，步骤 9 新增）
#
# 这一层是「业务层」，只允许调 database.auto_generated.sqliteCommon、
# database.queryCommon 与 common，**禁止裸 SQL、禁止直接 import tools/**。
# placeStore.py 是 `pb_place` 的唯一实现：聚合复算（rebuildPlaces）与查询
# （listPlaces / liveAggregatePlaces）。api/browse.py 只负责整形与路由，
# 不自己拼地点相关的 SQL —— 否则「同一地段的口径」会分叉成两份。
