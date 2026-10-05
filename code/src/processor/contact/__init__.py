#! /usr/bin/env python3
#encoding: utf-8

#Filename: __init__.py
#Description: processor 层：contact 联系人导入（CSV/vCard -> pb_person/pb_family，步骤 8）
#
# 这一层是「业务层」，只允许调 database.auto_generated.sqliteCommon 与 common，
# **禁止裸SQL、禁止直接 import tools/**（工具在业务层之上）。
# contactCommon.py 是CSV / vCard 两个通道共用的运行层（幂等认人、分类行重写、
# 家庭组挂接、原件归档）；csv_import.py / vcard_import.py 只负责「文件 -> 联系人」。
