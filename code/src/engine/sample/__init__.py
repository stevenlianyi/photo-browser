#! /usr/bin/env python3
#encoding: utf-8

#Filename: __init__.py
#Description: engine/sample 年代抽样（R11 · DR-45 ~ DR-50）
#
# 职责边界（与 engine/match 同一套硬规则）
# ----------------------------------------
#   eraSampler.py  **抽样 + 批次落库的唯一入口**（读 pb_face/pb_photo，写 pb_era_sample*）。
#                  不做文件 IO：不读原图、不写 zip、不碰 photo 目录。
#   （zip 打包在 processor/media/eraPack.py —— 它是「读原图产出文件」的媒体活，
#     与缩略图/人脸裁剪同一层；本层只负责「抽哪几张」这件事。）
#
# ⚠️ 为什么抽样**不进 pb_review_log**（DR-49）
#   那张表的定义被 api/photoAction.py 的头注释钉死为「**归属纠错**排障链」
#   （回答「这张脸当初怎么被认成这个人的」）。抽样不改归属、不改质心、不改桶，
#   写进去只会让真正需要的纠错历史混入无语义条目。
#   批次自己带 randomSeed + 参数快照，**复现能力不依赖 review 日志**。
