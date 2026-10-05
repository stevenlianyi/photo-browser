#! /usr/bin/env python3
#encoding: utf-8

#Filename: __init__.py
#Description: engine/cluster 未归类人脸聚类（步骤 7）
#
# 三块职责，边界很硬
# ------------------
#   dbscan.py       **纯计算**：numpy DBSCAN + clusterCode 幂等编码。
#                   不import database.*，不读库不写库，可反复跑、可复现。
#   clustering.py   **唯一读 pb_face.embedding 的地方**（只读未归类集合）
#                   + **唯一写 pb_face.clusterCode 的地方**。
#
# ⚠️ clusterCode 的写权限为什么放在这一层（而不是 processor/review）
#    processor/review/__init__.py 里写着「review 层是唯一允许写
#    pb_face.personCode / isConfirmed / pb_photo_person 的地方（**除步骤 7
#    聚类写 clusterCode**）」—— 那条例外指的就是本模块。
#    保持聚类在 engine 层的理由：它与 matcher 一样是**机器判定**，
#    纯计算部分必须能脱离数据库单测（test_dbscan.py 就是这么写的）。
#
# 硬约束：只对**未归类集合**聚类 —— personCode IS NULL AND isStranger=0
#        AND delFlag='0'。已归类的脸绝不重新聚类，isStranger=1 永久排除。
#        不写 photoDir、不写 pb_review_log（聚类是机器行为，不是人工操作）。
