#! /usr/bin/env python3
#encoding: utf-8

#Filename: __init__.py
#Description: review 层：人工确认/改判的落库 + **两个队列的读取**（步骤 6/7）
#
# assigner.py  单张/批量归属 + 幂等照片-人员关联 + **确认后立即重算质心**
# merger.py    合并人员 / 拆出人员
# queue.py     **只读**：待确认队列 / 「我不同意」列表 / 四态计数
#
# 这一层是**唯一**允许写 pb_face.personCode / isConfirmed / pb_photo_person 的地方
#   （除步骤 7 聚类写 clusterCode —— 那个写在 engine/cluster/clustering.py）。
#   engine/match/* 与 engine/cluster/dbscan.py 全部是纯计算，不碰库。
#
# queue.py 一个字都不写：改判走 assigner.fix()，确认走 assigner.confirm()。
# 两个队列的口径（personCode / isConfirmed / isStranger 推导的四态）
# 只有queue.py 一处定义，见该文件头部。
#
# 单写入者：与 faceStore 同一条纪律，只在主进程主线程调用。
