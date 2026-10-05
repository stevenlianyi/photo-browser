#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ======================================================================================
# 自动生成，请勿手改
#   本文件由 database/sqliteCodeGenerator.py 从 database/pb_*.txt 生成。
#   改表 = 改 pb_*.txt + 重跑生成器；直接改这里会在下次生成时被覆盖，
#   且 .txt 与本文件会静默不一致（字段/索引/长度全部对不上）。
#
#   生成时间 : 2026-10-05 17:39:25
#   生成器   : database/sqliteCodeGenerator.py v20261005
#   数据源   : pb_family.txt, pb_person.txt, pb_person_category.txt, pb_photo.txt, pb_face.txt, pb_person_centroid.txt, pb_photo_person.txt, pb_scan_job.txt, pb_review_log.txt
#   表数量   : 9 张，字段 164 个，索引 23 个
#
#   上层：processor / engine / api —— **业务层禁止裸 SQL，一律调本文件**（数据库设计.md §1.2）
#   下层：common/sqliteHandle.py（读写双连接 + PRAGMA + %s->? 占位符转换）
#
#   ⚠️ SQLite 没有 VARCHAR(n)：VARCHAR(64) 与 TEXT 完全等价，**长度 (n) 不被强制**。
#      长度约束只存在于本文件的 TABLE_COLUMNS 里，业务层需自行校验。
#   ⚠️ 本模块 **import 时绝不连库**；第一次调用 dbHandle() 时才建连。
# ======================================================================================

import os
import sys
import threading

# 让本文件在任意 cwd 下都能 import 到兄弟包
#（.../src/database/auto_generated/sqliteCommon.py -> .../src）
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD
from common import miscCommon as misc
from common import paths as paths
from common import sqliteHandle as sqliteHandle

_VERSION = "20261005"

_LOG = misc.setLogNew("sqliteCommon", "sqlitecommon.log")

# 全局读写句柄（懒初始化单例）
_HANDLE = None
_HANDLE_LOCK = threading.RLock()

#common begin

# ==========================================================================
# 通用段（表无关）—— 由生成器维护，跨表复用，禁止手改
#   包含：表元数据字典 / 句柄 / 存在性判断 / 通用增删改 / 批量 upsert / 自检工具
# ==========================================================================


# ------------------------------------------------------------
# 1. 表元数据（**从 .txt 抄过来的唯一副本**）
#    SQLite 不保存 VARCHAR(n) 的长度，也不保存 COMMENT，
#    所以「长度校验」「字段中文名」只能从这里取。
# ------------------------------------------------------------

# 表清单（建库顺序 = 数据库设计.md §二）
TABLE_ORDER = (
    'pb_family',   # 家庭组
    'pb_person',   # 人员
    'pb_person_category',   # 人员分类
    'pb_photo',   # 照片
    'pb_face',   # 人脸
    'pb_person_centroid',   # 人员年代桶质心
    'pb_photo_person',   # 照片-人员关联
    'pb_scan_job',   # 扫描任务
    'pb_review_log',   # 纠错操作日志
)

# 表名 -> 中文名
TABLE_CN = {
    'pb_family': '家庭组',
    'pb_person': '人员',
    'pb_person_category': '人员分类',
    'pb_photo': '照片',
    'pb_face': '人脸',
    'pb_person_centroid': '人员年代桶质心',
    'pb_photo_person': '照片-人员关联',
    'pb_scan_job': '扫描任务',
    'pb_review_log': '纠错操作日志',
}

# 表名 -> 字段元数据列表（顺序 = .txt 里的顺序 = 建表列顺序）
#   sqliteType: INTEGER / TEXT / NUMERIC / BLOB（INTEGER PRIMARY KEY AUTOINCREMENT 只在 recID）
#   length/ scale: VARCHAR(64)->64；DECIMAL(10,7)->10/7；其余 None
TABLE_COLUMNS = {
    'pb_family': [
        {"name": 'recID', "type": 'INT',
            "sqliteType": 'INTEGER PRIMARY KEY AUTOINCREMENT', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": True,
            "autoIncrement": True, "default": None, "comment": '记录ID'},
        {"name": 'familyCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": True, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '家庭组编码 幂等键'},
        {"name": 'familyName', "type": 'VARCHAR(128)',
            "sqliteType": 'TEXT', "length": 128, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '家庭名称 如 陈家或Steven家'},
        {"name": 'notes', "type": 'VARCHAR(400)',
            "sqliteType": 'TEXT', "length": 400, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '备注'},
        {"name": 'label', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'label'},
        {"name": 'memo', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'memo'},
        {"name": 'regID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册ID'},
        {"name": 'regYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册年月日'},
        {"name": 'modifyID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改用户ID'},
        {"name": 'modifyYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改年月日'},
        {"name": 'delFlag', "type": 'CHAR(1)',
            "sqliteType": 'TEXT', "length": 1, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '删除标记'},
    ],
    'pb_person': [
        {"name": 'recID', "type": 'INT',
            "sqliteType": 'INTEGER PRIMARY KEY AUTOINCREMENT', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": True,
            "autoIncrement": True, "default": None, "comment": '记录ID'},
        {"name": 'personCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": True, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '人员编码 幂等键'},
        {"name": 'displayName', "type": 'VARCHAR(128)',
            "sqliteType": 'TEXT', "length": 128, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '显示名 唯一'},
        {"name": 'familyName', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '姓氏'},
        {"name": 'familyGroupCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '关联pb_family.familyCode'},
        {"name": 'relation', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '家庭关系 parent或spouse或child或sibling'},
        {"name": 'email', "type": 'VARCHAR(128)',
            "sqliteType": 'TEXT', "length": 128, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '邮箱'},
        {"name": 'phone', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '电话'},
        {"name": 'birthday', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '生日YYYY-MM-DD 自适应分桶依赖'},
        {"name": 'vcardUid', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'vCardUID 重复导入幂等键'},
        {"name": 'avatarFaceCode', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '头像人脸编码 关联pb_face.faceCode'},
        {"name": 'source', "type": 'TINYINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '来源 0手工1vCard/CSV2Graph'},
        {"name": 'isConfirmed', "type": 'TINYINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '是否已确认'},
        {"name": 'ownerID', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '归属用户loginID'},
        {"name": 'label', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'label'},
        {"name": 'memo', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'memo'},
        {"name": 'regID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册ID'},
        {"name": 'regYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册年月日'},
        {"name": 'modifyID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改用户ID'},
        {"name": 'modifyYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改年月日'},
        {"name": 'delFlag', "type": 'CHAR(1)',
            "sqliteType": 'TEXT', "length": 1, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '删除标记'},
    ],
    'pb_person_category': [
        {"name": 'recID', "type": 'INT',
            "sqliteType": 'INTEGER PRIMARY KEY AUTOINCREMENT', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": True,
            "autoIncrement": True, "default": None, "comment": '记录ID'},
        {"name": 'personCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '人员编码 关联pb_person.personCode'},
        {"name": 'category', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '分类 family或friend或colleague'},
        {"name": 'label', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'label'},
        {"name": 'memo', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'memo'},
        {"name": 'regID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册ID'},
        {"name": 'regYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册年月日'},
        {"name": 'modifyID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改用户ID'},
        {"name": 'modifyYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改年月日'},
        {"name": 'delFlag', "type": 'CHAR(1)',
            "sqliteType": 'TEXT', "length": 1, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '删除标记'},
    ],
    'pb_photo': [
        {"name": 'recID', "type": 'INT',
            "sqliteType": 'INTEGER PRIMARY KEY AUTOINCREMENT', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": True,
            "autoIncrement": True, "default": None, "comment": '记录ID'},
        {"name": 'photoCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": True, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '照片编码 幂等键'},
        {"name": 'relPath', "type": 'VARCHAR(1024)',
            "sqliteType": 'TEXT', "length": 1024, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '相对photo目录路径 保留原值不改写'},
        {"name": 'relPathHash', "type": 'CHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": True, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'sha256规范化相对路径'},
        {"name": 'fileHash', "type": 'CHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'sha256文件内容 内容级去重'},
        {"name": 'fileSize', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '文件字节数'},
        {"name": 'mimeType', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'MIME类型'},
        {"name": 'width', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '宽'},
        {"name": 'height', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '高'},
        {"name": 'orientation', "type": 'SMALLINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'EXIF方向'},
        {"name": 'takenAt', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'EXIF DateTimeOriginal UTC ISO8601'},
        {"name": 'shotYear', "type": 'SMALLINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '拍摄年份 分桶键'},
        {"name": 'lat', "type": 'DECIMAL(10,7)',
            "sqliteType": 'NUMERIC', "length": 10, "scale": 7,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'GPS纬度'},
        {"name": 'lon', "type": 'DECIMAL(10,7)',
            "sqliteType": 'NUMERIC', "length": 10, "scale": 7,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'GPS经度'},
        {"name": 'placeName', "type": 'VARCHAR(256)',
            "sqliteType": 'TEXT', "length": 256, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '逆地理地点'},
        {"name": 'cameraModel', "type": 'VARCHAR(128)',
            "sqliteType": 'TEXT', "length": 128, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '相机型号'},
        {"name": 'faceCount', "type": 'SMALLINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '人脸数量'},
        {"name": 'isDuplicate', "type": 'TINYINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '是否重复'},
        {"name": 'dupOfPhotoCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '指向主照片'},
        {"name": 'movedToPhotoCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '疑似已移动到的新记录 待用户确认'},
        {"name": 'isMissing', "type": 'TINYINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '库中存在但磁盘找不到'},
        {"name": 'scanState', "type": 'TINYINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '0待扫描1已入人脸库2待人工确认3完成'},
        {"name": 'scannedYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '最近扫描时间'},
        {"name": 'ownerID', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '归属用户loginID'},
        {"name": 'label', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'label'},
        {"name": 'memo', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'memo'},
        {"name": 'regID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册ID'},
        {"name": 'regYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册年月日'},
        {"name": 'modifyID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改用户ID'},
        {"name": 'modifyYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改年月日'},
        {"name": 'delFlag', "type": 'CHAR(1)',
            "sqliteType": 'TEXT', "length": 1, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '删除标记'},
    ],
    'pb_face': [
        {"name": 'recID', "type": 'INT',
            "sqliteType": 'INTEGER PRIMARY KEY AUTOINCREMENT', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": True,
            "autoIncrement": True, "default": None, "comment": '记录ID'},
        {"name": 'faceCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": True, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '人脸编码 幂等键'},
        {"name": 'photoCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '关联pb_photo.photoCode'},
        {"name": 'personCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '关联pb_person.personCode 空=未归属进待确认队列'},
        {"name": 'clusterCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '聚类簇编码 可能是同一个人'},
        {"name": 'bbox', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '归一化框 x,y,w,h(逗号分隔,0~1,原点左上) 格式见faceCropper.formatFaceBox'},
        {"name": 'detScore', "type": 'DECIMAL(6,4)',
            "sqliteType": 'NUMERIC', "length": 6, "scale": 4,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'SCRFD检测置信度'},
        {"name": 'poseYaw', "type": 'DECIMAL(6,2)',
            "sqliteType": 'NUMERIC', "length": 6, "scale": 2,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '偏航角 侧脸过滤'},
        {"name": 'posePitch', "type": 'DECIMAL(6,2)',
            "sqliteType": 'NUMERIC', "length": 6, "scale": 2,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '俯仰角'},
        {"name": 'quality', "type": 'DECIMAL(6,4)',
            "sqliteType": 'NUMERIC', "length": 6, "scale": 4,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '综合质量分'},
        {"name": 'embedding', "type": 'MEDIUMBLOB',
            "sqliteType": 'BLOB', "length": None, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'float32[512]小端2048字节'},
        {"name": 'shotBucket', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '拍摄年代桶 如1995-1999'},
        {"name": 'isConfirmed', "type": 'TINYINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '归属是否经人工确认 0否(含自动归属) 1是'},
        {"name": 'isStranger', "type": 'TINYINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '是否标记为陌生人 0否 1是 陌生人不再进待确认队列'},
        {"name": 'label', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'label'},
        {"name": 'memo', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'memo'},
        {"name": 'regID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册ID'},
        {"name": 'regYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册年月日'},
        {"name": 'modifyID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改用户ID'},
        {"name": 'modifyYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改年月日'},
        {"name": 'delFlag', "type": 'CHAR(1)',
            "sqliteType": 'TEXT', "length": 1, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '删除标记'},
    ],
    'pb_person_centroid': [
        {"name": 'recID', "type": 'INT',
            "sqliteType": 'INTEGER PRIMARY KEY AUTOINCREMENT', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": True,
            "autoIncrement": True, "default": None, "comment": '记录ID'},
        {"name": 'personCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '关联pb_person.personCode'},
        {"name": 'bucketKey', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '年代桶键 如1995-1999 兜底桶固定为ALL不分桶'},
        {"name": 'centroid', "type": 'MEDIUMBLOB',
            "sqliteType": 'BLOB', "length": None, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '桶内样本归一化均值 float32[512] 只用isConfirmed=1样本'},
        {"name": 'sampleCount', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '参与计算的人工确认样本数 小于3不启用该桶'},
        {"name": 'label', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'label'},
        {"name": 'memo', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'memo'},
        {"name": 'regID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册ID'},
        {"name": 'regYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册年月日'},
        {"name": 'modifyID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改用户ID'},
        {"name": 'modifyYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改年月日'},
        {"name": 'delFlag', "type": 'CHAR(1)',
            "sqliteType": 'TEXT', "length": 1, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '删除标记'},
    ],
    'pb_photo_person': [
        {"name": 'recID', "type": 'INT',
            "sqliteType": 'INTEGER PRIMARY KEY AUTOINCREMENT', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": True,
            "autoIncrement": True, "default": None, "comment": '记录ID'},
        {"name": 'linkKey', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": True, "unique": True, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '幂等键 photoCode加冒号加personCode'},
        {"name": 'photoCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '关联pb_photo.photoCode'},
        {"name": 'personCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '关联pb_person.personCode'},
        {"name": 'faceCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '由哪张脸判定'},
        {"name": 'confidence', "type": 'DECIMAL(6,4)',
            "sqliteType": 'NUMERIC', "length": 6, "scale": 4,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '余弦相似度'},
        {"name": 'source', "type": 'TINYINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '0自动归属未经人工确认 1人工确认或改判'},
        {"name": 'label', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'label'},
        {"name": 'memo', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'memo'},
        {"name": 'regID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册ID'},
        {"name": 'regYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册年月日'},
        {"name": 'modifyID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改用户ID'},
        {"name": 'modifyYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改年月日'},
        {"name": 'delFlag', "type": 'CHAR(1)',
            "sqliteType": 'TEXT', "length": 1, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '删除标记'},
    ],
    'pb_scan_job': [
        {"name": 'recID', "type": 'INT',
            "sqliteType": 'INTEGER PRIMARY KEY AUTOINCREMENT', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": True,
            "autoIncrement": True, "default": None, "comment": '记录ID'},
        {"name": 'jobCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": True, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '扫描任务编码 幂等键'},
        {"name": 'rootPath', "type": 'VARCHAR(512)',
            "sqliteType": 'TEXT', "length": 512, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '扫描根目录'},
        {"name": 'batchSize', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '100', "comment": '每批处理张数 默认100'},
        {"name": 'batchIndex', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '已完成批次数'},
        {"name": 'totalCount', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '发现文件总数'},
        {"name": 'processedCount', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '已处理数'},
        {"name": 'addedCount', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '新增数'},
        {"name": 'skippedCount', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '跳过数'},
        {"name": 'duplicateCount', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '重复数'},
        {"name": 'pendingCount', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '待人工确认数'},
        {"name": 'lastCursor', "type": 'VARCHAR(1024)',
            "sqliteType": 'TEXT', "length": 1024, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '断点游标 最后处理文件相对路径'},
        {"name": 'jobStatus', "type": 'VARCHAR(24)',
            "sqliteType": 'TEXT', "length": 24, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": "'IDLE'", "comment": 'IDLE或RUNNING或PAUSED或DONE或FAILED'},
        {"name": 'startedYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '开始时间'},
        {"name": 'finishedYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '结束时间'},
        {"name": 'errMsg', "type": 'VARCHAR(512)',
            "sqliteType": 'TEXT', "length": 512, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '错误信息'},
        {"name": 'label', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'label'},
        {"name": 'memo', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'memo'},
        {"name": 'regID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册ID'},
        {"name": 'regYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册年月日'},
        {"name": 'modifyID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改用户ID'},
        {"name": 'modifyYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改年月日'},
        {"name": 'delFlag', "type": 'CHAR(1)',
            "sqliteType": 'TEXT', "length": 1, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '删除标记'},
    ],
    'pb_review_log': [
        {"name": 'recID', "type": 'INT',
            "sqliteType": 'INTEGER PRIMARY KEY AUTOINCREMENT', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": True,
            "autoIncrement": True, "default": None, "comment": '记录ID'},
        {"name": 'logCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": True, "unique": True, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '操作编码 幂等键'},
        {"name": 'opType', "type": 'VARCHAR(24)',
            "sqliteType": 'TEXT', "length": 24, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'ASSIGN首次确认 FIX改判 UNKNOWN置为未知 STRANGER标记陌生人 BATCH_ASSIGN批量确认 SPLIT拆分 MERGE合并 UNDO撤销 共8种 见数据库设计4.9'},
        {"name": 'faceCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '关联pb_face.faceCode 单张脸操作时填'},
        {"name": 'photoCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '关联pb_photo.photoCode 便于按照片回溯'},
        {"name": 'fromPersonCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '操作前归属人 可空'},
        {"name": 'toPersonCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '操作后归属人 可空 表示置为未知'},
        {"name": 'similarity', "type": 'DECIMAL(6,4)',
            "sqliteType": 'NUMERIC', "length": 6, "scale": 4,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '当时的余弦相似度'},
        {"name": 'faceCount', "type": 'INT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '本次影响的人脸张数 合并拆分时大于1'},
        {"name": 'detail', "type": 'VARCHAR(400)',
            "sqliteType": 'TEXT', "length": 400, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '操作摘要 合并拆分时记双方姓名与照片数'},
        {"name": 'isRevertible', "type": 'TINYINT',
            "sqliteType": 'INTEGER', "length": None, "scale": None,
            "notNull": True, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": '0', "comment": '是否可撤销 0否 1是 仅拆分与合并可撤销'},
        {"name": 'revertedByLogCode', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '被哪条撤销操作回滚 为空表示未撤销'},
        {"name": 'opUser', "type": 'VARCHAR(64)',
            "sqliteType": 'TEXT', "length": 64, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '操作人 本机单用户固定值'},
        {"name": 'opYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '操作时间 YYYYMMDDHHMMSS'},
        {"name": 'label', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'label'},
        {"name": 'memo', "type": 'VARCHAR(200)',
            "sqliteType": 'TEXT', "length": 200, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": 'memo'},
        {"name": 'regID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册ID'},
        {"name": 'regYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '注册年月日'},
        {"name": 'modifyID', "type": 'VARCHAR(32)',
            "sqliteType": 'TEXT', "length": 32, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改用户ID'},
        {"name": 'modifyYMDHMS', "type": 'VARCHAR(16)',
            "sqliteType": 'TEXT', "length": 16, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '修改年月日'},
        {"name": 'delFlag', "type": 'CHAR(1)',
            "sqliteType": 'TEXT', "length": 1, "scale": None,
            "notNull": False, "unique": False, "primaryKey": False,
            "autoIncrement": False, "default": None, "comment": '删除标记'},
    ],
}

# 表名 -> 索引清单（plan/数据库设计.md §五）
#   UNIQUE 列的索引名统一 idx_<表>_<字段>；复合索引用 idx_<表>_<字段1>_<字段2>
TABLE_INDEXES = {
    'pb_family': (
        {"name": 'idx_pb_family_familyCode', "columns": ('familyCode',), "unique": True, "where": None},
    ),
    'pb_person': (
        {"name": 'idx_pb_person_personCode', "columns": ('personCode',), "unique": True, "where": None},
        {"name": 'idx_pb_person_displayName', "columns": ('displayName',), "unique": True, "where": None},
    ),
    'pb_person_category': (
    ),
    'pb_photo': (
        {"name": 'idx_pb_photo_photoCode', "columns": ('photoCode',), "unique": True, "where": None},
        {"name": 'idx_pb_photo_relPathHash', "columns": ('relPathHash',), "unique": True, "where": None},
        {"name": 'idx_pb_photo_fileHash', "columns": ('fileHash',), "unique": False, "where": None},
        {"name": 'idx_pb_photo_shotYear', "columns": ('shotYear',), "unique": False, "where": None},
        {"name": 'idx_pb_photo_scanState', "columns": ('scanState',), "unique": False, "where": None},
        {"name": 'idx_pb_photo_movedToPhotoCode', "columns": ('movedToPhotoCode',), "unique": False, "where": None},
    ),
    'pb_face': (
        {"name": 'idx_pb_face_faceCode', "columns": ('faceCode',), "unique": True, "where": None},
        {"name": 'idx_pb_face_photoCode', "columns": ('photoCode',), "unique": False, "where": None},
        {"name": 'idx_pb_face_personCode', "columns": ('personCode',), "unique": False, "where": None},
        {"name": 'idx_pb_face_personCode_isnull', "columns": ('personCode',), "unique": False, "where": 'personCode IS NULL'},
        {"name": 'idx_pb_face_personCode_isConfirmed', "columns": ('personCode', 'isConfirmed'), "unique": False, "where": 'isConfirmed=0 AND isStranger=0'},
    ),
    'pb_person_centroid': (
        {"name": 'idx_pb_person_centroid_personCode_bucketKey', "columns": ('personCode', 'bucketKey'), "unique": True, "where": None},
    ),
    'pb_photo_person': (
        {"name": 'idx_pb_photo_person_linkKey', "columns": ('linkKey',), "unique": True, "where": None},
        {"name": 'idx_pb_photo_person_personCode', "columns": ('personCode',), "unique": False, "where": None},
        {"name": 'idx_pb_photo_person_photoCode', "columns": ('photoCode',), "unique": False, "where": None},
    ),
    'pb_scan_job': (
        {"name": 'idx_pb_scan_job_jobCode', "columns": ('jobCode',), "unique": True, "where": None},
    ),
    'pb_review_log': (
        {"name": 'idx_pb_review_log_logCode', "columns": ('logCode',), "unique": True, "where": None},
        {"name": 'idx_pb_review_log_faceCode', "columns": ('faceCode',), "unique": False, "where": None},
        {"name": 'idx_pb_review_log_opType', "columns": ('opType',), "unique": False, "where": None},
        {"name": 'idx_pb_review_log_isRevertible', "columns": ('isRevertible',), "unique": False, "where": 'isRevertible=1 AND revertedByLogCode IS NULL'},
    ),
}

# 表名 -> upsert 默认冲突键（业务幂等键，见 数据库设计.md §1.6）
CONFLICT_COLUMNS = {
    'pb_family': ('familyCode',),
    'pb_person': ('personCode',),
    'pb_person_category': (),
    'pb_photo': ('photoCode',),
    'pb_face': ('faceCode',),
    'pb_person_centroid': ('personCode', 'bucketKey'),
    'pb_photo_person': ('linkKey',),
    'pb_scan_job': ('jobCode',),
    'pb_review_log': ('logCode',),
}

# 表名 -> query 支持的等值过滤字段（全部带索引）
QUERY_FILTER_FIELDS = {
    'pb_family': ('familyCode',),
    'pb_person': ('personCode', 'displayName', 'vcardUid', 'familyGroupCode'),
    'pb_person_category': ('personCode', 'category'),
    'pb_photo': ('photoCode', 'relPathHash', 'fileHash', 'dupOfPhotoCode'),
    'pb_face': ('faceCode', 'photoCode', 'personCode'),
    'pb_person_centroid': ('personCode', 'bucketKey'),
    'pb_photo_person': ('linkKey', 'photoCode', 'personCode'),
    'pb_scan_job': ('jobCode', 'jobStatus'),
    'pb_review_log': ('logCode', 'opType', 'faceCode', 'photoCode', 'fromPersonCode', 'toPersonCode'),
}

# 表名 -> 允许为 NULL 的字段（query 的 nullFields 参数白名单：待确认队列等）
NULLABLE_FIELDS = {
    'pb_family': ('notes', 'label', 'memo', 'regID', 'regYMDHMS', 'modifyID', 'modifyYMDHMS', 'delFlag'),
    'pb_person': ('familyName', 'familyGroupCode', 'relation', 'email', 'phone', 'birthday', 'vcardUid', 'avatarFaceCode', 'ownerID', 'label', 'memo', 'regID', 'regYMDHMS', 'modifyID', 'modifyYMDHMS', 'delFlag'),
    'pb_person_category': ('label', 'memo', 'regID', 'regYMDHMS', 'modifyID', 'modifyYMDHMS', 'delFlag'),
    'pb_photo': ('mimeType', 'width', 'height', 'orientation', 'takenAt', 'shotYear', 'lat', 'lon', 'placeName', 'cameraModel', 'dupOfPhotoCode', 'movedToPhotoCode', 'scannedYMDHMS', 'ownerID', 'label', 'memo', 'regID', 'regYMDHMS', 'modifyID', 'modifyYMDHMS', 'delFlag'),
    'pb_face': ('personCode', 'clusterCode', 'bbox', 'detScore', 'poseYaw', 'posePitch', 'quality', 'embedding', 'shotBucket', 'label', 'memo', 'regID', 'regYMDHMS', 'modifyID', 'modifyYMDHMS', 'delFlag'),
    'pb_person_centroid': ('centroid', 'label', 'memo', 'regID', 'regYMDHMS', 'modifyID', 'modifyYMDHMS', 'delFlag'),
    'pb_photo_person': ('faceCode', 'confidence', 'label', 'memo', 'regID', 'regYMDHMS', 'modifyID', 'modifyYMDHMS', 'delFlag'),
    'pb_scan_job': ('lastCursor', 'startedYMDHMS', 'finishedYMDHMS', 'errMsg', 'label', 'memo', 'regID', 'regYMDHMS', 'modifyID', 'modifyYMDHMS', 'delFlag'),
    'pb_review_log': ('faceCode', 'photoCode', 'fromPersonCode', 'toPersonCode', 'similarity', 'detail', 'revertedByLogCode', 'opUser', 'opYMDHMS', 'label', 'memo', 'regID', 'regYMDHMS', 'modifyID', 'modifyYMDHMS', 'delFlag'),
}

# 表名 -> 允许 ORDER BY 的字段（白名单，杜绝排序字段注入）
ORDER_FIELDS = {
    'pb_family': ('recID', 'familyCode', 'familyName'),
    'pb_person': ('recID', 'personCode', 'displayName'),
    'pb_person_category': ('recID', 'personCode', 'category'),
    'pb_photo': ('recID', 'shotYear', 'takenAt', 'fileSize', 'scannedYMDHMS'),
    'pb_face': ('recID', 'shotBucket', 'quality', 'faceCode'),
    'pb_person_centroid': ('recID', 'personCode', 'bucketKey', 'sampleCount'),
    'pb_photo_person': ('recID', 'photoCode', 'personCode', 'confidence'),
    'pb_scan_job': ('recID', 'jobStatus', 'jobCode'),
    'pb_review_log': ('recID', 'opType', 'opYMDHMS', 'faceCode'),
}

# 表名 -> 主键名（当前 9 张表统一是 recID，见 数据库设计.md §1.3）
PRIMARY_KEYS = {
    'pb_family': 'recID',
    'pb_person': 'recID',
    'pb_person_category': 'recID',
    'pb_photo': 'recID',
    'pb_face': 'recID',
    'pb_person_centroid': 'recID',
    'pb_photo_person': 'recID',
    'pb_scan_job': 'recID',
    'pb_review_log': 'recID',
}


# ------------------------------------------------------------
# 2. 句柄（懒初始化：import 本模块绝不连库）
# ------------------------------------------------------------

def dbHandle(dbFile = None):
    """取（并懒初始化）全局读写句柄。

    - 第一次调用才建连，dbFile 缺省取 paths.db_file()（= <PHOTO_ROOT>/db/photolib.db）
    - **无参调用绝不切换目标库**：已有句柄就直接返回它。
      原因：本模块所有 query_/insert_/update_ 内部都是 dbHandle() 无参调用，
      若无参也按 paths.db_file() 重新解析，那么「先用 dbHandle(临时库) 指定库、
      再调一个生成函数」的组合会被悄悄切回正式库 —— 表现为
      `build_db.py --db 临时库 --verify` 校验的是正式库、
      `scan_cli.py --db 临时库` 把任务写进正式库而把照片写进临时库。
      这种「同一进程同时存在两个库句柄」是最难查的一类错，所以从根上堵死：
      要换库必须**显式**调 dbHandle(路径)。
    - 显式传入与当前不同的 dbFile 时，关掉旧句柄重开（tools/build_db.py --db 用）
    - 线程安全：并发首调只会成功建一次
    """
    global _HANDLE
    with _HANDLE_LOCK:
        if _HANDLE is not None and not dbFile:
            return _HANDLE
        target = paths.db_file() if not dbFile else os.path.abspath(os.path.normpath(str(dbFile)))
        if _HANDLE is not None and _HANDLE.dbFile == target:
            return _HANDLE
        if _HANDLE is not None:
            _HANDLE.close()
        _HANDLE = sqliteHandle.sqliteHandle(target)
        return _HANDLE


def closeDb():
    """关掉全局句柄（进程退出/切库时用）"""
    global _HANDLE
    with _HANDLE_LOCK:
        if _HANDLE is not None:
            _HANDLE.close()
            _HANDLE = None
    return True


# ------------------------------------------------------------
# 3. 存在性判断
# ------------------------------------------------------------

def chkTableExist(tableName):
    """表是否存在。

    SQLite **没有 information_schema**（stock 的 MySQL 版查的那张表这里不存在），
    一律查 sqlite_master。
    """
    result = False
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    sqlStr = "SELECT name FROM sqlite_master WHERE type = \'table\' AND name = %s;"
    if db.executeRead(sqlStr, (tableName,)) == sqliteHandle.RET_ERROR:
        return result
    result = db.fetchOne() is not None
    return result


def chkIndexExist(indexName):
    """索引是否存在（含 SQLite 自动建的 sqlite_autoindex_* 隐式唯一索引）"""
    result = False
    db = dbHandle()
    sqlStr = "SELECT name FROM sqlite_master WHERE type = \'index\' AND name = %s;"
    if db.executeRead(sqlStr, (indexName,)) == sqliteHandle.RET_ERROR:
        return result
    result = db.fetchOne() is not None
    return result


def dropTableGeneral(tableName):
    """删表（连同其索引）。返回 True = 已删掉。"""
    if tableName not in TABLE_COLUMNS:
        return False
    db = dbHandle()
    sqlStr = "DROP TABLE IF EXISTS " + tableName + ";"
    if db.executeWrite(sqlStr) == sqliteHandle.RET_ERROR:
        return False
    return not chkTableExist(tableName)


# ------------------------------------------------------------
# 4. 数据归一（业务 dict -> 可直接绑定的 dict）
# ------------------------------------------------------------

def normalizeDataSet(tableName, dataSet):
    """按 TABLE_COLUMNS 把业务传入的 dict 归一成「可直接绑定参数」的 dict。

    返回
    ----
    (saveSet, skipList)
      saveSet  —— 只含本表已有字段、且值类型与 SQLite 声明类型一致的项
      skipList —— 被跳过的字段名（表外字段 / 类型对不上 / 空数值）

    归一规则
    --------
      INTEGER : int(v)；None / "" / 非数字 -> 跳过（让库走 DEFAULT 或 NULL）
      NUMERIC : float(v)；同上
      TEXT    : str(v)；None -> 跳过；**空串原样保留**（更新时要能清空字段）
      BLOB    : bytes/bytearray/memoryview 原样；其余一律跳过
                （绝不 str->bytes 猜编码，人脸向量错了是静默脏数据）
      不在 dataSet 里的字段 -> 不出现在 saveSet（INSERT 时即 DEFAULT/NULL）
    """
    saveSet = {}
    skipList = []
    if tableName not in TABLE_COLUMNS or not dataSet:
        return saveSet, skipList
    for column in TABLE_COLUMNS[tableName]:
        name = column["name"]
        if name not in dataSet:
            continue
        value = dataSet.get(name)
        sqliteType = column["sqliteType"]
        if sqliteType.startswith("INTEGER"):
            if value is None or value == "":
                skipList.append(name)
                continue
            try:
                saveSet[name] = int(value)
            except (TypeError, ValueError):
                skipList.append(name)
            continue
        if sqliteType.startswith("NUMERIC"):
            if value is None or value == "":
                skipList.append(name)
                continue
            try:
                saveSet[name] = float(value)
            except (TypeError, ValueError):
                skipList.append(name)
            continue
        if sqliteType.startswith("BLOB"):
            if isinstance(value, (bytes, bytearray, memoryview)):
                saveSet[name] = bytes(value)
            else:
                skipList.append(name)
            continue
        if value is None:
            skipList.append(name)
            continue
        saveSet[name] = value if isinstance(value, str) else str(value)
    return saveSet, skipList


# ------------------------------------------------------------
# 5. 通用增删改
# ------------------------------------------------------------

def insertTableGeneral(tableName, dataSet):
    """通用插入：INSERT INTO t (cols) VALUES (%s,...)；成功返回新行 recID。

    列名全部来自 normalizeDataSet 归一后的 key（= 本表白名单），
    值一律走占位符，**任何情况下都不拼字符串**。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSet:
        return result
    columnList = list(dataSet.keys())
    sqlStr = ("INSERT INTO " + tableName + " (" + ", ".join(columnList) + ") VALUES ("
              + ", ".join(["%s"] * len(columnList)) + ");")
    db = dbHandle()
    rtn = db.executeWrite(sqlStr, tuple(dataSet[name] for name in columnList))
    if rtn == sqliteHandle.RET_ERROR:
        return 0
    return db.insertID()


def insertManyTableGeneral(tableName, dataSetList, conflictColumns = (), updateColumns = (),
                           fillStandard = False, forceColumns = ()):
    """批量写（executemany，整批一次事务）。

    参数
    ----
    conflictColumns : 业务幂等键列名序列
        非空 -> INSERT ... ON CONFLICT(<列>) DO UPDATE SET ... / DO NOTHING（幂等重扫）
        为空 -> 普通 INSERT
    updateColumns : 要在 DO UPDATE 里刷新的列
        为空且 conflictColumns 非空时，缺省为「除冲突键外、且本行出现的所有列」
    fillStandard : bool
        True 时逐行补 delFlag（未删）与 regYMDHMS（注册时间）
    forceColumns : 列名序列 —— 强制出现在 INSERT 列清单里（只认本表白名单）
        为什么需要它：normalizeDataSet 把 None /"" 整条丢掉，于是当
        **整批所有行在该列上都为空**时，该列不会进入 INSERT，
        ON CONFLICT DO UPDATE 自然也不会覆盖它 -> 旧值永久残留。
        典型场景（步骤 3 扫描器）：某张图的内容被换成一张无 EXIF 的图，
        shotYear 应从 2023 变成 NULL；不强制该列的话2023 会一直留着，
        而且**库里看不出任何异常**，是典型的静默脏数据。
        强制后该列恒在列清单里，缺该键的行按 NULL 写入（=「显式清空」）。
        纯 INSERT 场景不需要它（缺列即DEFAULT/NULL）。

    返回
    ----
    (影响行数, 实际列名元组)
    """
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return 0, ()
    conflict = tuple(conflictColumns or ())
    # 一次归一、一次拼行：不要为了拿列名把 normalizeDataSet 跑两遍
    #（500 行一批的扫描入库，这段会被调用 5 万次级别）
    now = misc.getTime()
    saveSetList = []
    for dataSet in dataSetList:
        saveSet, _skip = normalizeDataSet(tableName, dataSet)
        saveSet.pop("recID", None)
        if fillStandard:
            saveSet.setdefault("delFlag", comGD.DEL_FLAG_NO)
            saveSet.setdefault("regYMDHMS", now)
        saveSetList.append(saveSet)
    columnNames = []
    for saveSet in saveSetList:
        for name in saveSet:
            if name not in columnNames:
                columnNames.append(name)
    # 强制列：让「整批皆空」的列也进入列清单（否则 upsert 无法把旧值清成 NULL）
    if forceColumns:
        allowed = set(column["name"] for column in TABLE_COLUMNS.get(tableName, ()))
        for name in forceColumns:
            if name in allowed and name != "recID" and name not in columnNames:
                columnNames.append(name)
    if not columnNames:
        return 0, ()
    rowsList = [tuple(saveSet.get(name) for name in columnNames) for saveSet in saveSetList]

    sqlStr = ("INSERT INTO " + tableName + " (" + ", ".join(columnNames) + ") VALUES ("
              + ", ".join(["%s"] * len(columnNames)) + ")")
    if conflict:
        sqlStr += " ON CONFLICT (" + ", ".join(conflict) + ") "
        if not updateColumns:
            updateColumns = [name for name in columnNames if name not in conflict]
        # 注册时间与软删标记不参与覆盖：否则重扫会改掉注册时间、把软删行悄悄复活
        updateColumns = [name for name in updateColumns
                         if name in columnNames and name not in conflict
                         and name not in ("recID", "regYMDHMS", "delFlag")]
        if updateColumns:
            sqlStr += "DO UPDATE SET " + ", ".join(
                [name + " = excluded." + name for name in updateColumns])
        else:
            sqlStr += "DO NOTHING"
    sqlStr += ";"
    db = dbHandle()
    rtn = 0
    with db.transaction():
        rtn = db.executeWriteMany(sqlStr, rowsList)
    return rtn, tuple(columnNames)


def updateTableGeneral(tableName, keySqlstr, keyValues, dataSet):
    """通用更新：UPDATE t SET c = %s, ... WHERE <keySqlstr>；返回影响行数。

    keySqlstr 只由本文件的生成代码拼（形如 "recID = %s"），不接受外部字符串。
    """
    if not dataSet:
        return 0
    setList = []
    valuesList = []
    for name, value in dataSet.items():
        setList.append(name + " = %s")
        valuesList.append(value)
    sqlStr = ("UPDATE " + tableName + " SET " + ", ".join(setList)
              + " WHERE " + keySqlstr + ";")
    db = dbHandle()
    rtn = db.executeWrite(sqlStr, tuple(valuesList) + tuple(keyValues or ()))
    if rtn == sqliteHandle.RET_ERROR:
        return 0
    return rtn


def deleteTableGeneral(tableName, keySqlstr, keyValues):
    """物理删除：DELETE FROM t WHERE <keySqlstr>；返回影响行数。"""
    sqlStr = "DELETE FROM " + tableName + " WHERE " + keySqlstr + ";"
    db = dbHandle()
    rtn = db.executeWrite(sqlStr, tuple(keyValues or ()))
    if rtn == sqliteHandle.RET_ERROR:
        return 0
    return rtn


def columnDefOf(tableName, columnName):
    """拼出单列的 DDL 片段（列名 + SQLite 类型 + NOT NULL + DEFAULT），供 ALTER TABLE ADD COLUMN 用。

    返回 None 表示列名不在本表白名单里（**不接受外部随便传的字符串**）。
    ⚠️ NOT NULL 且**没有 DEFAULT** 的列拼出来的片段，加到**非空表**上必然被
       SQLite 拒绝（ADD COLUMN 要求新列有默认值或可空）；这类列只可能出现在
       新建表里，老表上永远不会是「缺列」，所以真遇到时调用方应当跳过并报告。
    """
    for column in TABLE_COLUMNS.get(tableName, ()):
        if column["name"] != columnName:
            continue
        parts = [column["name"] + " " + column["sqliteType"]]
        if column["autoIncrement"]:
            parts.append("PRIMARY KEY AUTOINCREMENT")
        if column["notNull"]:
            parts.append("NOT NULL")
        if column["default"] is not None:
            parts.append("DEFAULT " + str(column["default"]))
        return " ".join(parts)
    return None


def addColumnGeneral(tableName, columnName):
    """ALTER TABLE <表> ADD COLUMN <列> —— schema 演进用（**加列，不改类型、不删列**）。

    为什么需要它
    ------------
    建表 DDL 是 `CREATE TABLE IF NOT EXISTS`：老库里已存在的表**不会**因为
    pb_*.txt 加了字段而自动变出那一列，于是「代码已按新字段写库、老库没这列」
    -> 运行时 `no such column` 直接炸。本函数让 tools/build_db.py --migrate
    能把老库补齐，且**不动任何一行数据**（SQLite 的 ADD COLUMN 是纯元数据操作）。

    返回
    ----
    (ok: bool, errMsg: str)
        ok=False 时 errMsg 说明原因（列名不在白名单 / 约束冲突 / SQL 被拒）。
        幂等：列已存在时直接返回 (True, "已存在")，不报错。
    """
    if tableName not in TABLE_COLUMNS:
        return False, "非法表名 %r" % tableName
    columnDef = columnDefOf(tableName, columnName)
    if not columnDef:
        return False, "列 %s.%s 不在表定义里" % (tableName, columnName)
    db = dbHandle()
    db.executeRead("SELECT name FROM sqlite_master WHERE type = 'table' AND name = %s;",
                   (tableName,))
    if db.fetchOne() is None:
        return False, "表 %s 不存在（先建表）" % tableName
    db.executeRead("PRAGMA table_info(%s);" % tableName)
    for row in db.fetchAll():
        if row.get("name") == columnName:
            return True, "已存在"
    sqlStr = "ALTER TABLE " + tableName + " ADD COLUMN " + columnDef + ";"
    if db.executeWrite(sqlStr) == sqliteHandle.RET_ERROR:
        return False, db.lastErrMsg
    return True, "已新增 %s" % columnName


def countTableGeneral(tableName, delFlag = comGD.DEL_FLAG_NO):
    """表内记录数（默认只数未软删的；delFlag 传 ""/"*" 数全部）。出错返回 -1。"""
    if tableName not in TABLE_COLUMNS:
        return -1
    sqlStr = "SELECT COUNT(*) AS rowNum FROM " + tableName
    valuesList = []
    if delFlag not in (None, "", "*"):
        sqlStr += " WHERE delFlag = %s"
        valuesList.append(delFlag)
    db = dbHandle()
    if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
        return -1
    return int(db.fetchValue(0) or 0)


def countWhereGeneral(tableName, whereSqlstr, keyValues = ()):
    """带条件计数：SELECT COUNT(*) FROM t WHERE <whereSqlstr>。出错返回 -1。

    为什么需要它（步骤 7 加的，四态口径的硬需求）
    ------------------------------------------------
      生成层的 query_* 只能按「主键 + 业务码 + nullFields(IS NULL)」过滤，
      **表达不了** `isStranger = 0` / `isConfirmed = 0` 这类「等于 0」的语义。
      而四态（未归属 / 自动归属 / 人工确认 / 陌生人）恰恰全靠这两个 0 值推导
      —— 于是「待确认队列条数」这种验收口径只剩两条路：
        ① 把整表捞出来在Python 里数（10 万行白跑一趟，违背DR-12 的分页纪律）；
        ② 在业务层写裸 SQL（违背「业务层禁止裸 SQL」）。
      本函数是这两者之间的正解：**SQL 在这一层拼一次，业务层只传条件串**。

    ⚠️⚠️ whereSqlstr 的每一个值都必须写成 %s 占位符，值走 keyValues ——
       绝不要把值直接拼进字符串。带用户输入的查询（如按名字筛人）
       一旦用 f-string拼 where，注入与「引号没转义」两类错会同时出现，
       而且它们都不会报错，只会让「我不同意」列表悄悄少几条。
    ⚠️ 条件里若要限定列，只能用本表白名单里的列名（TABLE_COLUMNS），
       否则会拼出 `no such column`。
    """
    if tableName not in TABLE_COLUMNS:
        return -1
    sqlStr = "SELECT COUNT(*) AS rowNum FROM " + tableName
    if whereSqlstr:
        sqlStr += " WHERE " + str(whereSqlstr)
    db = dbHandle()
    if db.executeRead(sqlStr, tuple(keyValues or ())) == sqliteHandle.RET_ERROR:
        return -1
    return int(db.fetchValue(0) or 0)


# ------------------------------------------------------------
# 6. 建库 / 自检
# ------------------------------------------------------------

def createAllTables():
    """按 TABLE_ORDER 逐表建表（幂等）。返回 {表名: 是否就绪}"""
    result = {}
    for tableName in TABLE_ORDER:
        func = globals().get("create_" + tableName)
        if func is None:
            _LOG.error("createAllTables: 缺少 create_%s" % tableName)
            result[tableName] = False
            continue
        result[tableName] = bool(func(tableName))
    return result


def dropAllTables():
    """删掉全部 pb_* 表（不可逆；给「改表要重建」用）。"""
    result = {}
    for tableName in reversed(TABLE_ORDER):
        result[tableName] = dropTableGeneral(tableName)
    return result


def tableInfo(tableName):
    """PRAGMA table_info -> list[dict]（字段名/类型/notnull/dflt/pk，建库自检用）"""
    if tableName not in TABLE_COLUMNS:
        return []
    db = dbHandle()
    if db.executeRead("PRAGMA table_info(%s);" % tableName) == sqliteHandle.RET_ERROR:
        return []
    return db.fetchAll()


def indexList(tableName):
    """PRAGMA index_list -> list[dict]（name/unique/origin/partial）"""
    if tableName not in TABLE_COLUMNS:
        return []
    db = dbHandle()
    if db.executeRead("PRAGMA index_list(%s);" % tableName) == sqliteHandle.RET_ERROR:
        return []
    return db.fetchAll()


def indexSqlList(tableName):
    """取某表**生成器登记的**索引 DDL 列表（用于核对建表结果）"""
    creator = globals().get("indexSqlList_" + tableName)
    if creator is None:
        return []
    return creator()


def dbFilePath():
    """当前句柄指向的库文件绝对路径"""
    return dbHandle().dbFile


def pragmaSnapshot():
    """读写两个连接上 PRAGMA 的实际生效值（验收用）"""
    return dbHandle().pragmaDict()


def integrityCheck():
    """PRAGMA integrity_check -> 'ok' 或错误描述"""
    db = dbHandle()
    if db.executeRead("PRAGMA integrity_check;") == sqliteHandle.RET_ERROR:
        return "ERR"
    return db.fetchValue("ERR")


#common end

# ==========================================================================
# pb_family 家庭组
# ==========================================================================

# pb_family 建表（幂等：已存在直接返回 True，不重复建）
def create_pb_family(tableName):
    """建 pb_family 表 + 索引。返回 True = 表已就绪。"""
    if tableName not in TABLE_COLUMNS:
        _LOG.error("create_pb_family: 非法表名 %r" % tableName)
        return False
    if chkTableExist(tableName):
        return True
    db = dbHandle()
    if db.executeWrite(createTableSQL_pb_family(tableName)) == sqliteHandle.RET_ERROR:
        return False
    for indexSql in indexSqlList_pb_family():
        if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:
            db.rollbackWrite()
            return False
    return chkTableExist(tableName)



def createTableSQL_pb_family(tableName):
    """pb_family 的建表 DDL（表名来自调用方，必须是 pb_family）。"""
    sqlStr = (
        "CREATE TABLE IF NOT EXISTS " + tableName + " ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "familyCode TEXT NOT NULL,"
        "familyName TEXT NOT NULL,"
        "notes TEXT,"
        "label TEXT,"
        "memo TEXT,"
        "regID TEXT,"
        "regYMDHMS TEXT,"
        "modifyID TEXT,"
        "modifyYMDHMS TEXT,"
        "delFlag TEXT"
        ");"
    )
    return sqlStr



def indexSqlList_pb_family():
    """pb_family 的索引 DDL（清单见 数据库设计.md §五，全部 IF NOT EXISTS 故幂等）。"""
    sqlList = [
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_family_familyCode ON pb_family(familyCode);',   # txt-UNIQUE
    ]
    return sqlList



# pb_family 查询记录
def query_pb_family(tableName, recID = 0, familyCode = "", nullFields = (), delFlag = "0",
        mode = "full", orderBy = "recID", descFlag = False, limitNum = 0,
        offsetNum = 0):
    """查 pb_family。

    参数
    ----
    recID        : int  —— 主键精确查，>0 时生效
    familyCode   : str  —— 非空时等值过滤
    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内
    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限
    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存
    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID
    descFlag     : bool —— 是否倒序
    limitNum     : int  —— >0 时生效
    offsetNum    : int  —— 分页偏移

    返回
    ----
    list[dict]（空列表 = 没查到，不是出错）
    """
    result = []
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    valuesList = []
    whereList = []
    try:
        try:
            recID = int(recID)
        except (TypeError, ValueError):
            recID = 0
        if recID > 0:
            whereList.append("recID = %s")
            valuesList.append(recID)

        for fieldName, fieldValue in (('familyCode', familyCode),):
            if fieldValue is not None and fieldValue != "":
                whereList.append(fieldName + " = %s")
                valuesList.append(fieldValue)

        for fieldName in (nullFields or ()):
            if fieldName in NULLABLE_FIELDS.get(tableName, ()):
                whereList.append(fieldName + " IS NULL")

        if delFlag not in (None, "", "*"):
            whereList.append("delFlag = %s")
            valuesList.append(delFlag)

        columnList = ["*"]
        if mode == "light":
            columnList = [c["name"] for c in TABLE_COLUMNS[tableName]
                if c["sqliteType"] != "BLOB"]
            if not columnList:
                columnList = ["*"]
        sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName
        if whereList:
            sqlStr += " WHERE " + " AND ".join(whereList)
        orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"
        sqlStr += " ORDER BY " + orderField
        if descFlag:
            sqlStr += " DESC"
        if int(limitNum or 0) > 0:
            sqlStr += " LIMIT %s OFFSET %s"
            valuesList.append(int(limitNum))
            valuesList.append(int(offsetNum or 0))

        if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
            return result
        result = db.fetchAll()
    except Exception as e:
        _LOG.error("query_pb_family: %s" % e)
    return result



# pb_family 增加记录
def insert_pb_family(tableName, dataSet):
    """新增一条 pb_family，返回新行 recID（<=0 表示失败）。

    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略
    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值
    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间
    """
    result = 0
    if tableName not in TABLE_COLUMNS:
        return result
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("insert_pb_family: 跳过无法归一的字段 %s" % skipList)
    if "delFlag" not in saveSet:
        saveSet["delFlag"] = comGD.DEL_FLAG_NO
    if "regYMDHMS" not in saveSet:
        saveSet["regYMDHMS"] = misc.getTime()
    result = insertTableGeneral(tableName, saveSet)
    return result



# pb_family 批量增加记录（executemany + 显式事务）
def insertMany_pb_family(tableName, dataSetList):
    """批量新增 pb_family（整批一次提交，失败整批回滚），返回写入行数。

    步骤 3 扫描入库走这里：一次几百行，别一行一提交。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        fillStandard=True)
    return rtn



# pb_family 批量 upsert（幂等重扫走这里）
def upsertMany_pb_family(tableName, dataSetList, conflictColumns = None, updateColumns = None):
    """按业务幂等键批量写 pb_family：

    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING

    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值
    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING
    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    conflict = list(conflictColumns) if conflictColumns else list(CONFLICT_COLUMNS.get(tableName, ()))
    update = list(updateColumns) if updateColumns else ()
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        conflictColumns=conflict, updateColumns=update,
        fillStandard=True)
    return rtn



# pb_family 修改记录
def update_pb_family(tableName, recID, dataSet):
    """按 recID 改一条 pb_family，返回影响行数（0 = 无字段可改或没命中）。

    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略
    - TEXT 字段传空串是真的「清空」（不会被当成未提供）
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("update_pb_family: 跳过无法归一的字段 %s" % skipList)
    if not saveSet:
        return 0
    saveSet["modifyYMDHMS"] = misc.getTime()
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_family 删除记录
def delete_pb_family(tableName, recID, hardDelete = False):
    """删一条 pb_family，返回影响行数。

    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS
    hardDelete=True           -> 物理 DELETE

    ⚠️ 软删除后记录仍在表里，业务幂等键（familyCode）依然被唯一索引占着；
       想复用同一条记录请走 update，别指望再 insert 一遍。
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    if hardDelete:
        return deleteTableGeneral(tableName, "recID = %s", [recID])
    saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_family 删表
def drop_pb_family(tableName):
    """删除 pb_family 表及其索引（不可逆；只给「改表要重建」用）。"""
    return dropTableGeneral(tableName)


# ==========================================================================
# pb_person 人员
# ==========================================================================

# pb_person 建表（幂等：已存在直接返回 True，不重复建）
def create_pb_person(tableName):
    """建 pb_person 表 + 索引。返回 True = 表已就绪。"""
    if tableName not in TABLE_COLUMNS:
        _LOG.error("create_pb_person: 非法表名 %r" % tableName)
        return False
    if chkTableExist(tableName):
        return True
    db = dbHandle()
    if db.executeWrite(createTableSQL_pb_person(tableName)) == sqliteHandle.RET_ERROR:
        return False
    for indexSql in indexSqlList_pb_person():
        if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:
            db.rollbackWrite()
            return False
    return chkTableExist(tableName)



def createTableSQL_pb_person(tableName):
    """pb_person 的建表 DDL（表名来自调用方，必须是 pb_person）。"""
    sqlStr = (
        "CREATE TABLE IF NOT EXISTS " + tableName + " ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "personCode TEXT NOT NULL,"
        "displayName TEXT NOT NULL,"
        "familyName TEXT,"
        "familyGroupCode TEXT,"
        "relation TEXT,"
        "email TEXT,"
        "phone TEXT,"
        "birthday TEXT,"
        "vcardUid TEXT,"
        "avatarFaceCode TEXT,"
        "source INTEGER NOT NULL DEFAULT 0,"
        "isConfirmed INTEGER NOT NULL DEFAULT 0,"
        "ownerID TEXT,"
        "label TEXT,"
        "memo TEXT,"
        "regID TEXT,"
        "regYMDHMS TEXT,"
        "modifyID TEXT,"
        "modifyYMDHMS TEXT,"
        "delFlag TEXT"
        ");"
    )
    return sqlStr



def indexSqlList_pb_person():
    """pb_person 的索引 DDL（清单见 数据库设计.md §五，全部 IF NOT EXISTS 故幂等）。"""
    sqlList = [
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_person_personCode ON pb_person(personCode);',   # txt-UNIQUE
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_person_displayName ON pb_person(displayName);',   # INDEX_SPEC
    ]
    return sqlList



# pb_person 查询记录
def query_pb_person(tableName, recID = 0, personCode = "", displayName = "", vcardUid = "",
        familyGroupCode = "", nullFields = (), delFlag = "0", mode = "full",
        orderBy = "recID", descFlag = False, limitNum = 0, offsetNum = 0):
    """查 pb_person。

    参数
    ----
    recID        : int  —— 主键精确查，>0 时生效
    personCode   : str  —— 非空时等值过滤
    displayName  : str  —— 非空时等值过滤
    vcardUid     : str  —— 非空时等值过滤
    familyGroupCode: str  —— 非空时等值过滤
    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内
    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限
    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存
    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID
    descFlag     : bool —— 是否倒序
    limitNum     : int  —— >0 时生效
    offsetNum    : int  —— 分页偏移

    返回
    ----
    list[dict]（空列表 = 没查到，不是出错）
    """
    result = []
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    valuesList = []
    whereList = []
    try:
        try:
            recID = int(recID)
        except (TypeError, ValueError):
            recID = 0
        if recID > 0:
            whereList.append("recID = %s")
            valuesList.append(recID)

        for fieldName, fieldValue in (('personCode', personCode), ('displayName', displayName), ('vcardUid', vcardUid), ('familyGroupCode', familyGroupCode)):
            if fieldValue is not None and fieldValue != "":
                whereList.append(fieldName + " = %s")
                valuesList.append(fieldValue)

        for fieldName in (nullFields or ()):
            if fieldName in NULLABLE_FIELDS.get(tableName, ()):
                whereList.append(fieldName + " IS NULL")

        if delFlag not in (None, "", "*"):
            whereList.append("delFlag = %s")
            valuesList.append(delFlag)

        columnList = ["*"]
        if mode == "light":
            columnList = [c["name"] for c in TABLE_COLUMNS[tableName]
                if c["sqliteType"] != "BLOB"]
            if not columnList:
                columnList = ["*"]
        sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName
        if whereList:
            sqlStr += " WHERE " + " AND ".join(whereList)
        orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"
        sqlStr += " ORDER BY " + orderField
        if descFlag:
            sqlStr += " DESC"
        if int(limitNum or 0) > 0:
            sqlStr += " LIMIT %s OFFSET %s"
            valuesList.append(int(limitNum))
            valuesList.append(int(offsetNum or 0))

        if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
            return result
        result = db.fetchAll()
    except Exception as e:
        _LOG.error("query_pb_person: %s" % e)
    return result



# pb_person 增加记录
def insert_pb_person(tableName, dataSet):
    """新增一条 pb_person，返回新行 recID（<=0 表示失败）。

    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略
    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值
    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间
    """
    result = 0
    if tableName not in TABLE_COLUMNS:
        return result
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("insert_pb_person: 跳过无法归一的字段 %s" % skipList)
    if "delFlag" not in saveSet:
        saveSet["delFlag"] = comGD.DEL_FLAG_NO
    if "regYMDHMS" not in saveSet:
        saveSet["regYMDHMS"] = misc.getTime()
    result = insertTableGeneral(tableName, saveSet)
    return result



# pb_person 批量增加记录（executemany + 显式事务）
def insertMany_pb_person(tableName, dataSetList):
    """批量新增 pb_person（整批一次提交，失败整批回滚），返回写入行数。

    步骤 3 扫描入库走这里：一次几百行，别一行一提交。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        fillStandard=True)
    return rtn



# pb_person 批量 upsert（幂等重扫走这里）
def upsertMany_pb_person(tableName, dataSetList, conflictColumns = None, updateColumns = None):
    """按业务幂等键批量写 pb_person：

    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING

    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值
    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING
    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    conflict = list(conflictColumns) if conflictColumns else list(CONFLICT_COLUMNS.get(tableName, ()))
    update = list(updateColumns) if updateColumns else ()
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        conflictColumns=conflict, updateColumns=update,
        fillStandard=True)
    return rtn



# pb_person 修改记录
def update_pb_person(tableName, recID, dataSet):
    """按 recID 改一条 pb_person，返回影响行数（0 = 无字段可改或没命中）。

    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略
    - TEXT 字段传空串是真的「清空」（不会被当成未提供）
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("update_pb_person: 跳过无法归一的字段 %s" % skipList)
    if not saveSet:
        return 0
    saveSet["modifyYMDHMS"] = misc.getTime()
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_person 删除记录
def delete_pb_person(tableName, recID, hardDelete = False):
    """删一条 pb_person，返回影响行数。

    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS
    hardDelete=True           -> 物理 DELETE

    ⚠️ 软删除后记录仍在表里，业务幂等键（personCode）依然被唯一索引占着；
       想复用同一条记录请走 update，别指望再 insert 一遍。
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    if hardDelete:
        return deleteTableGeneral(tableName, "recID = %s", [recID])
    saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_person 删表
def drop_pb_person(tableName):
    """删除 pb_person 表及其索引（不可逆；只给「改表要重建」用）。"""
    return dropTableGeneral(tableName)


# ==========================================================================
# pb_person_category 人员分类
# ==========================================================================

# pb_person_category 建表（幂等：已存在直接返回 True，不重复建）
def create_pb_person_category(tableName):
    """建 pb_person_category 表 + 索引。返回 True = 表已就绪。"""
    if tableName not in TABLE_COLUMNS:
        _LOG.error("create_pb_person_category: 非法表名 %r" % tableName)
        return False
    if chkTableExist(tableName):
        return True
    db = dbHandle()
    if db.executeWrite(createTableSQL_pb_person_category(tableName)) == sqliteHandle.RET_ERROR:
        return False
    for indexSql in indexSqlList_pb_person_category():
        if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:
            db.rollbackWrite()
            return False
    return chkTableExist(tableName)



def createTableSQL_pb_person_category(tableName):
    """pb_person_category 的建表 DDL（表名来自调用方，必须是 pb_person_category）。"""
    sqlStr = (
        "CREATE TABLE IF NOT EXISTS " + tableName + " ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "personCode TEXT NOT NULL,"
        "category TEXT NOT NULL,"
        "label TEXT,"
        "memo TEXT,"
        "regID TEXT,"
        "regYMDHMS TEXT,"
        "modifyID TEXT,"
        "modifyYMDHMS TEXT,"
        "delFlag TEXT"
        ");"
    )
    return sqlStr



def indexSqlList_pb_person_category():
    """pb_person_category 的索引 DDL（清单见 数据库设计.md §五，全部 IF NOT EXISTS 故幂等）。"""
    sqlList = [
    ]
    return sqlList



# pb_person_category 查询记录
def query_pb_person_category(tableName, recID = 0, personCode = "", category = "",
        nullFields = (), delFlag = "0", mode = "full",
        orderBy = "recID", descFlag = False, limitNum = 0,
        offsetNum = 0):
    """查 pb_person_category。

    参数
    ----
    recID        : int  —— 主键精确查，>0 时生效
    personCode   : str  —— 非空时等值过滤
    category     : str  —— 非空时等值过滤
    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内
    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限
    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存
    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID
    descFlag     : bool —— 是否倒序
    limitNum     : int  —— >0 时生效
    offsetNum    : int  —— 分页偏移

    返回
    ----
    list[dict]（空列表 = 没查到，不是出错）
    """
    result = []
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    valuesList = []
    whereList = []
    try:
        try:
            recID = int(recID)
        except (TypeError, ValueError):
            recID = 0
        if recID > 0:
            whereList.append("recID = %s")
            valuesList.append(recID)

        for fieldName, fieldValue in (('personCode', personCode), ('category', category)):
            if fieldValue is not None and fieldValue != "":
                whereList.append(fieldName + " = %s")
                valuesList.append(fieldValue)

        for fieldName in (nullFields or ()):
            if fieldName in NULLABLE_FIELDS.get(tableName, ()):
                whereList.append(fieldName + " IS NULL")

        if delFlag not in (None, "", "*"):
            whereList.append("delFlag = %s")
            valuesList.append(delFlag)

        columnList = ["*"]
        if mode == "light":
            columnList = [c["name"] for c in TABLE_COLUMNS[tableName]
                if c["sqliteType"] != "BLOB"]
            if not columnList:
                columnList = ["*"]
        sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName
        if whereList:
            sqlStr += " WHERE " + " AND ".join(whereList)
        orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"
        sqlStr += " ORDER BY " + orderField
        if descFlag:
            sqlStr += " DESC"
        if int(limitNum or 0) > 0:
            sqlStr += " LIMIT %s OFFSET %s"
            valuesList.append(int(limitNum))
            valuesList.append(int(offsetNum or 0))

        if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
            return result
        result = db.fetchAll()
    except Exception as e:
        _LOG.error("query_pb_person_category: %s" % e)
    return result



# pb_person_category 增加记录
def insert_pb_person_category(tableName, dataSet):
    """新增一条 pb_person_category，返回新行 recID（<=0 表示失败）。

    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略
    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值
    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间
    """
    result = 0
    if tableName not in TABLE_COLUMNS:
        return result
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("insert_pb_person_category: 跳过无法归一的字段 %s" % skipList)
    if "delFlag" not in saveSet:
        saveSet["delFlag"] = comGD.DEL_FLAG_NO
    if "regYMDHMS" not in saveSet:
        saveSet["regYMDHMS"] = misc.getTime()
    result = insertTableGeneral(tableName, saveSet)
    return result



# pb_person_category 批量增加记录（executemany + 显式事务）
def insertMany_pb_person_category(tableName, dataSetList):
    """批量新增 pb_person_category（整批一次提交，失败整批回滚），返回写入行数。

    步骤 3 扫描入库走这里：一次几百行，别一行一提交。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        fillStandard=True)
    return rtn



# pb_person_category 批量 upsert（幂等重扫走这里）
def upsertMany_pb_person_category(tableName, dataSetList, conflictColumns = None, updateColumns = None):
    """按业务幂等键批量写 pb_person_category：

    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING

    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值
    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING
    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    conflict = list(conflictColumns) if conflictColumns else list(CONFLICT_COLUMNS.get(tableName, ()))
    update = list(updateColumns) if updateColumns else ()
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        conflictColumns=conflict, updateColumns=update,
        fillStandard=True)
    return rtn



# pb_person_category 修改记录
def update_pb_person_category(tableName, recID, dataSet):
    """按 recID 改一条 pb_person_category，返回影响行数（0 = 无字段可改或没命中）。

    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略
    - TEXT 字段传空串是真的「清空」（不会被当成未提供）
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("update_pb_person_category: 跳过无法归一的字段 %s" % skipList)
    if not saveSet:
        return 0
    saveSet["modifyYMDHMS"] = misc.getTime()
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_person_category 删除记录
def delete_pb_person_category(tableName, recID, hardDelete = False):
    """删一条 pb_person_category，返回影响行数。

    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS
    hardDelete=True           -> 物理 DELETE

    ⚠️ 软删除后记录仍在表里，业务幂等键（xxxCode）依然被唯一索引占着；
       想复用同一条记录请走 update，别指望再 insert 一遍。
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    if hardDelete:
        return deleteTableGeneral(tableName, "recID = %s", [recID])
    saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_person_category 删表
def drop_pb_person_category(tableName):
    """删除 pb_person_category 表及其索引（不可逆；只给「改表要重建」用）。"""
    return dropTableGeneral(tableName)


# ==========================================================================
# pb_photo 照片
# ==========================================================================

# pb_photo 建表（幂等：已存在直接返回 True，不重复建）
def create_pb_photo(tableName):
    """建 pb_photo 表 + 索引。返回 True = 表已就绪。"""
    if tableName not in TABLE_COLUMNS:
        _LOG.error("create_pb_photo: 非法表名 %r" % tableName)
        return False
    if chkTableExist(tableName):
        return True
    db = dbHandle()
    if db.executeWrite(createTableSQL_pb_photo(tableName)) == sqliteHandle.RET_ERROR:
        return False
    for indexSql in indexSqlList_pb_photo():
        if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:
            db.rollbackWrite()
            return False
    return chkTableExist(tableName)



def createTableSQL_pb_photo(tableName):
    """pb_photo 的建表 DDL（表名来自调用方，必须是 pb_photo）。"""
    sqlStr = (
        "CREATE TABLE IF NOT EXISTS " + tableName + " ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "photoCode TEXT NOT NULL,"
        "relPath TEXT NOT NULL,"
        "relPathHash TEXT NOT NULL,"
        "fileHash TEXT NOT NULL,"
        "fileSize INTEGER NOT NULL DEFAULT 0,"
        "mimeType TEXT,"
        "width INTEGER,"
        "height INTEGER,"
        "orientation INTEGER,"
        "takenAt TEXT,"
        "shotYear INTEGER,"
        "lat NUMERIC,"
        "lon NUMERIC,"
        "placeName TEXT,"
        "cameraModel TEXT,"
        "faceCount INTEGER NOT NULL DEFAULT 0,"
        "isDuplicate INTEGER NOT NULL DEFAULT 0,"
        "dupOfPhotoCode TEXT,"
        "movedToPhotoCode TEXT,"
        "isMissing INTEGER NOT NULL DEFAULT 0,"
        "scanState INTEGER NOT NULL DEFAULT 0,"
        "scannedYMDHMS TEXT,"
        "ownerID TEXT,"
        "label TEXT,"
        "memo TEXT,"
        "regID TEXT,"
        "regYMDHMS TEXT,"
        "modifyID TEXT,"
        "modifyYMDHMS TEXT,"
        "delFlag TEXT"
        ");"
    )
    return sqlStr



def indexSqlList_pb_photo():
    """pb_photo 的索引 DDL（清单见 数据库设计.md §五，全部 IF NOT EXISTS 故幂等）。"""
    sqlList = [
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_photo_photoCode ON pb_photo(photoCode);',   # txt-UNIQUE
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_photo_relPathHash ON pb_photo(relPathHash);',   # txt-UNIQUE
        'CREATE INDEX IF NOT EXISTS idx_pb_photo_fileHash ON pb_photo(fileHash);',   # INDEX_SPEC
        'CREATE INDEX IF NOT EXISTS idx_pb_photo_shotYear ON pb_photo(shotYear);',   # INDEX_SPEC
        'CREATE INDEX IF NOT EXISTS idx_pb_photo_scanState ON pb_photo(scanState);',   # INDEX_SPEC
        'CREATE INDEX IF NOT EXISTS idx_pb_photo_movedToPhotoCode ON pb_photo(movedToPhotoCode);',   # INDEX_SPEC
    ]
    return sqlList



# pb_photo 查询记录
def query_pb_photo(tableName, recID = 0, photoCode = "", relPathHash = "", fileHash = "",
        dupOfPhotoCode = "", nullFields = (), delFlag = "0", mode = "full",
        orderBy = "recID", descFlag = False, limitNum = 0, offsetNum = 0):
    """查 pb_photo。

    参数
    ----
    recID        : int  —— 主键精确查，>0 时生效
    photoCode    : str  —— 非空时等值过滤
    relPathHash  : str  —— 非空时等值过滤
    fileHash     : str  —— 非空时等值过滤
    dupOfPhotoCode: str  —— 非空时等值过滤
    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内
    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限
    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存
    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID
    descFlag     : bool —— 是否倒序
    limitNum     : int  —— >0 时生效
    offsetNum    : int  —— 分页偏移

    返回
    ----
    list[dict]（空列表 = 没查到，不是出错）
    """
    result = []
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    valuesList = []
    whereList = []
    try:
        try:
            recID = int(recID)
        except (TypeError, ValueError):
            recID = 0
        if recID > 0:
            whereList.append("recID = %s")
            valuesList.append(recID)

        for fieldName, fieldValue in (('photoCode', photoCode), ('relPathHash', relPathHash), ('fileHash', fileHash), ('dupOfPhotoCode', dupOfPhotoCode)):
            if fieldValue is not None and fieldValue != "":
                whereList.append(fieldName + " = %s")
                valuesList.append(fieldValue)

        for fieldName in (nullFields or ()):
            if fieldName in NULLABLE_FIELDS.get(tableName, ()):
                whereList.append(fieldName + " IS NULL")

        if delFlag not in (None, "", "*"):
            whereList.append("delFlag = %s")
            valuesList.append(delFlag)

        columnList = ["*"]
        if mode == "light":
            columnList = [c["name"] for c in TABLE_COLUMNS[tableName]
                if c["sqliteType"] != "BLOB"]
            if not columnList:
                columnList = ["*"]
        sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName
        if whereList:
            sqlStr += " WHERE " + " AND ".join(whereList)
        orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"
        sqlStr += " ORDER BY " + orderField
        if descFlag:
            sqlStr += " DESC"
        if int(limitNum or 0) > 0:
            sqlStr += " LIMIT %s OFFSET %s"
            valuesList.append(int(limitNum))
            valuesList.append(int(offsetNum or 0))

        if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
            return result
        result = db.fetchAll()
    except Exception as e:
        _LOG.error("query_pb_photo: %s" % e)
    return result



# pb_photo 增加记录
def insert_pb_photo(tableName, dataSet):
    """新增一条 pb_photo，返回新行 recID（<=0 表示失败）。

    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略
    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值
    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间
    """
    result = 0
    if tableName not in TABLE_COLUMNS:
        return result
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("insert_pb_photo: 跳过无法归一的字段 %s" % skipList)
    if "delFlag" not in saveSet:
        saveSet["delFlag"] = comGD.DEL_FLAG_NO
    if "regYMDHMS" not in saveSet:
        saveSet["regYMDHMS"] = misc.getTime()
    result = insertTableGeneral(tableName, saveSet)
    return result



# pb_photo 批量增加记录（executemany + 显式事务）
def insertMany_pb_photo(tableName, dataSetList):
    """批量新增 pb_photo（整批一次提交，失败整批回滚），返回写入行数。

    步骤 3 扫描入库走这里：一次几百行，别一行一提交。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        fillStandard=True)
    return rtn



# pb_photo 批量 upsert（幂等重扫走这里）
def upsertMany_pb_photo(tableName, dataSetList, conflictColumns = None, updateColumns = None):
    """按业务幂等键批量写 pb_photo：

    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING

    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值
    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING
    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    conflict = list(conflictColumns) if conflictColumns else list(CONFLICT_COLUMNS.get(tableName, ()))
    update = list(updateColumns) if updateColumns else ()
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        conflictColumns=conflict, updateColumns=update,
        fillStandard=True)
    return rtn



# pb_photo 修改记录
def update_pb_photo(tableName, recID, dataSet):
    """按 recID 改一条 pb_photo，返回影响行数（0 = 无字段可改或没命中）。

    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略
    - TEXT 字段传空串是真的「清空」（不会被当成未提供）
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("update_pb_photo: 跳过无法归一的字段 %s" % skipList)
    if not saveSet:
        return 0
    saveSet["modifyYMDHMS"] = misc.getTime()
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_photo 删除记录
def delete_pb_photo(tableName, recID, hardDelete = False):
    """删一条 pb_photo，返回影响行数。

    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS
    hardDelete=True           -> 物理 DELETE

    ⚠️ 软删除后记录仍在表里，业务幂等键（photoCode）依然被唯一索引占着；
       想复用同一条记录请走 update，别指望再 insert 一遍。
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    if hardDelete:
        return deleteTableGeneral(tableName, "recID = %s", [recID])
    saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_photo 删表
def drop_pb_photo(tableName):
    """删除 pb_photo 表及其索引（不可逆；只给「改表要重建」用）。"""
    return dropTableGeneral(tableName)


# ==========================================================================
# pb_face 人脸
# ==========================================================================

# pb_face 建表（幂等：已存在直接返回 True，不重复建）
def create_pb_face(tableName):
    """建 pb_face 表 + 索引。返回 True = 表已就绪。"""
    if tableName not in TABLE_COLUMNS:
        _LOG.error("create_pb_face: 非法表名 %r" % tableName)
        return False
    if chkTableExist(tableName):
        return True
    db = dbHandle()
    if db.executeWrite(createTableSQL_pb_face(tableName)) == sqliteHandle.RET_ERROR:
        return False
    for indexSql in indexSqlList_pb_face():
        if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:
            db.rollbackWrite()
            return False
    return chkTableExist(tableName)



def createTableSQL_pb_face(tableName):
    """pb_face 的建表 DDL（表名来自调用方，必须是 pb_face）。"""
    sqlStr = (
        "CREATE TABLE IF NOT EXISTS " + tableName + " ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "faceCode TEXT NOT NULL,"
        "photoCode TEXT NOT NULL,"
        "personCode TEXT,"
        "clusterCode TEXT,"
        "bbox TEXT,"
        "detScore NUMERIC,"
        "poseYaw NUMERIC,"
        "posePitch NUMERIC,"
        "quality NUMERIC,"
        "embedding BLOB,"
        "shotBucket TEXT,"
        "isConfirmed INTEGER NOT NULL DEFAULT 0,"
        "isStranger INTEGER NOT NULL DEFAULT 0,"
        "label TEXT,"
        "memo TEXT,"
        "regID TEXT,"
        "regYMDHMS TEXT,"
        "modifyID TEXT,"
        "modifyYMDHMS TEXT,"
        "delFlag TEXT"
        ");"
    )
    return sqlStr



def indexSqlList_pb_face():
    """pb_face 的索引 DDL（清单见 数据库设计.md §五，全部 IF NOT EXISTS 故幂等）。"""
    sqlList = [
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_face_faceCode ON pb_face(faceCode);',   # txt-UNIQUE
        'CREATE INDEX IF NOT EXISTS idx_pb_face_photoCode ON pb_face(photoCode);',   # INDEX_SPEC
        'CREATE INDEX IF NOT EXISTS idx_pb_face_personCode ON pb_face(personCode);',   # INDEX_SPEC
        'CREATE INDEX IF NOT EXISTS idx_pb_face_personCode_isnull ON pb_face(personCode) WHERE personCode IS NULL;',   # INDEX_SPEC
        'CREATE INDEX IF NOT EXISTS idx_pb_face_personCode_isConfirmed ON pb_face(personCode, isConfirmed) WHERE isConfirmed=0 AND isStranger=0;',   # INDEX_SPEC
    ]
    return sqlList



# pb_face 查询记录
def query_pb_face(tableName, recID = 0, faceCode = "", photoCode = "", personCode = "",
        nullFields = (), delFlag = "0", mode = "full", orderBy = "recID",
        descFlag = False, limitNum = 0, offsetNum = 0):
    """查 pb_face。

    参数
    ----
    recID        : int  —— 主键精确查，>0 时生效
    faceCode     : str  —— 非空时等值过滤
    photoCode    : str  —— 非空时等值过滤
    personCode   : str  —— 非空时等值过滤
    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内
    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限
    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存
    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID
    descFlag     : bool —— 是否倒序
    limitNum     : int  —— >0 时生效
    offsetNum    : int  —— 分页偏移

    返回
    ----
    list[dict]（空列表 = 没查到，不是出错）
    """
    result = []
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    valuesList = []
    whereList = []
    try:
        try:
            recID = int(recID)
        except (TypeError, ValueError):
            recID = 0
        if recID > 0:
            whereList.append("recID = %s")
            valuesList.append(recID)

        for fieldName, fieldValue in (('faceCode', faceCode), ('photoCode', photoCode), ('personCode', personCode)):
            if fieldValue is not None and fieldValue != "":
                whereList.append(fieldName + " = %s")
                valuesList.append(fieldValue)

        for fieldName in (nullFields or ()):
            if fieldName in NULLABLE_FIELDS.get(tableName, ()):
                whereList.append(fieldName + " IS NULL")

        if delFlag not in (None, "", "*"):
            whereList.append("delFlag = %s")
            valuesList.append(delFlag)

        columnList = ["*"]
        if mode == "light":
            columnList = [c["name"] for c in TABLE_COLUMNS[tableName]
                if c["sqliteType"] != "BLOB"]
            if not columnList:
                columnList = ["*"]
        sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName
        if whereList:
            sqlStr += " WHERE " + " AND ".join(whereList)
        orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"
        sqlStr += " ORDER BY " + orderField
        if descFlag:
            sqlStr += " DESC"
        if int(limitNum or 0) > 0:
            sqlStr += " LIMIT %s OFFSET %s"
            valuesList.append(int(limitNum))
            valuesList.append(int(offsetNum or 0))

        if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
            return result
        result = db.fetchAll()
    except Exception as e:
        _LOG.error("query_pb_face: %s" % e)
    return result



# pb_face 增加记录
def insert_pb_face(tableName, dataSet):
    """新增一条 pb_face，返回新行 recID（<=0 表示失败）。

    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略
    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值
    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间
    """
    result = 0
    if tableName not in TABLE_COLUMNS:
        return result
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("insert_pb_face: 跳过无法归一的字段 %s" % skipList)
    if "delFlag" not in saveSet:
        saveSet["delFlag"] = comGD.DEL_FLAG_NO
    if "regYMDHMS" not in saveSet:
        saveSet["regYMDHMS"] = misc.getTime()
    result = insertTableGeneral(tableName, saveSet)
    return result



# pb_face 批量增加记录（executemany + 显式事务）
def insertMany_pb_face(tableName, dataSetList):
    """批量新增 pb_face（整批一次提交，失败整批回滚），返回写入行数。

    步骤 3 扫描入库走这里：一次几百行，别一行一提交。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        fillStandard=True)
    return rtn



# pb_face 批量 upsert（幂等重扫走这里）
def upsertMany_pb_face(tableName, dataSetList, conflictColumns = None, updateColumns = None):
    """按业务幂等键批量写 pb_face：

    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING

    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值
    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING
    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    conflict = list(conflictColumns) if conflictColumns else list(CONFLICT_COLUMNS.get(tableName, ()))
    update = list(updateColumns) if updateColumns else ()
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        conflictColumns=conflict, updateColumns=update,
        fillStandard=True)
    return rtn



# pb_face 修改记录
def update_pb_face(tableName, recID, dataSet):
    """按 recID 改一条 pb_face，返回影响行数（0 = 无字段可改或没命中）。

    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略
    - TEXT 字段传空串是真的「清空」（不会被当成未提供）
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("update_pb_face: 跳过无法归一的字段 %s" % skipList)
    if not saveSet:
        return 0
    saveSet["modifyYMDHMS"] = misc.getTime()
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_face 删除记录
def delete_pb_face(tableName, recID, hardDelete = False):
    """删一条 pb_face，返回影响行数。

    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS
    hardDelete=True           -> 物理 DELETE

    ⚠️ 软删除后记录仍在表里，业务幂等键（faceCode）依然被唯一索引占着；
       想复用同一条记录请走 update，别指望再 insert 一遍。
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    if hardDelete:
        return deleteTableGeneral(tableName, "recID = %s", [recID])
    saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_face 删表
def drop_pb_face(tableName):
    """删除 pb_face 表及其索引（不可逆；只给「改表要重建」用）。"""
    return dropTableGeneral(tableName)


# ==========================================================================
# pb_person_centroid 人员年代桶质心
# ==========================================================================

# pb_person_centroid 建表（幂等：已存在直接返回 True，不重复建）
def create_pb_person_centroid(tableName):
    """建 pb_person_centroid 表 + 索引。返回 True = 表已就绪。"""
    if tableName not in TABLE_COLUMNS:
        _LOG.error("create_pb_person_centroid: 非法表名 %r" % tableName)
        return False
    if chkTableExist(tableName):
        return True
    db = dbHandle()
    if db.executeWrite(createTableSQL_pb_person_centroid(tableName)) == sqliteHandle.RET_ERROR:
        return False
    for indexSql in indexSqlList_pb_person_centroid():
        if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:
            db.rollbackWrite()
            return False
    return chkTableExist(tableName)



def createTableSQL_pb_person_centroid(tableName):
    """pb_person_centroid 的建表 DDL（表名来自调用方，必须是 pb_person_centroid）。"""
    sqlStr = (
        "CREATE TABLE IF NOT EXISTS " + tableName + " ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "personCode TEXT NOT NULL,"
        "bucketKey TEXT NOT NULL,"
        "centroid BLOB,"
        "sampleCount INTEGER NOT NULL DEFAULT 0,"
        "label TEXT,"
        "memo TEXT,"
        "regID TEXT,"
        "regYMDHMS TEXT,"
        "modifyID TEXT,"
        "modifyYMDHMS TEXT,"
        "delFlag TEXT"
        ");"
    )
    return sqlStr



def indexSqlList_pb_person_centroid():
    """pb_person_centroid 的索引 DDL（清单见 数据库设计.md §五，全部 IF NOT EXISTS 故幂等）。"""
    sqlList = [
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_person_centroid_personCode_bucketKey ON pb_person_centroid(personCode, bucketKey);',   # INDEX_SPEC
    ]
    return sqlList



# pb_person_centroid 查询记录
def query_pb_person_centroid(tableName, recID = 0, personCode = "", bucketKey = "",
        nullFields = (), delFlag = "0", mode = "full",
        orderBy = "recID", descFlag = False, limitNum = 0,
        offsetNum = 0):
    """查 pb_person_centroid。

    参数
    ----
    recID        : int  —— 主键精确查，>0 时生效
    personCode   : str  —— 非空时等值过滤
    bucketKey    : str  —— 非空时等值过滤
    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内
    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限
    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存
    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID
    descFlag     : bool —— 是否倒序
    limitNum     : int  —— >0 时生效
    offsetNum    : int  —— 分页偏移

    返回
    ----
    list[dict]（空列表 = 没查到，不是出错）
    """
    result = []
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    valuesList = []
    whereList = []
    try:
        try:
            recID = int(recID)
        except (TypeError, ValueError):
            recID = 0
        if recID > 0:
            whereList.append("recID = %s")
            valuesList.append(recID)

        for fieldName, fieldValue in (('personCode', personCode), ('bucketKey', bucketKey)):
            if fieldValue is not None and fieldValue != "":
                whereList.append(fieldName + " = %s")
                valuesList.append(fieldValue)

        for fieldName in (nullFields or ()):
            if fieldName in NULLABLE_FIELDS.get(tableName, ()):
                whereList.append(fieldName + " IS NULL")

        if delFlag not in (None, "", "*"):
            whereList.append("delFlag = %s")
            valuesList.append(delFlag)

        columnList = ["*"]
        if mode == "light":
            columnList = [c["name"] for c in TABLE_COLUMNS[tableName]
                if c["sqliteType"] != "BLOB"]
            if not columnList:
                columnList = ["*"]
        sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName
        if whereList:
            sqlStr += " WHERE " + " AND ".join(whereList)
        orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"
        sqlStr += " ORDER BY " + orderField
        if descFlag:
            sqlStr += " DESC"
        if int(limitNum or 0) > 0:
            sqlStr += " LIMIT %s OFFSET %s"
            valuesList.append(int(limitNum))
            valuesList.append(int(offsetNum or 0))

        if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
            return result
        result = db.fetchAll()
    except Exception as e:
        _LOG.error("query_pb_person_centroid: %s" % e)
    return result



# pb_person_centroid 增加记录
def insert_pb_person_centroid(tableName, dataSet):
    """新增一条 pb_person_centroid，返回新行 recID（<=0 表示失败）。

    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略
    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值
    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间
    """
    result = 0
    if tableName not in TABLE_COLUMNS:
        return result
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("insert_pb_person_centroid: 跳过无法归一的字段 %s" % skipList)
    if "delFlag" not in saveSet:
        saveSet["delFlag"] = comGD.DEL_FLAG_NO
    if "regYMDHMS" not in saveSet:
        saveSet["regYMDHMS"] = misc.getTime()
    result = insertTableGeneral(tableName, saveSet)
    return result



# pb_person_centroid 批量增加记录（executemany + 显式事务）
def insertMany_pb_person_centroid(tableName, dataSetList):
    """批量新增 pb_person_centroid（整批一次提交，失败整批回滚），返回写入行数。

    步骤 3 扫描入库走这里：一次几百行，别一行一提交。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        fillStandard=True)
    return rtn



# pb_person_centroid 批量 upsert（幂等重扫走这里）
def upsertMany_pb_person_centroid(tableName, dataSetList, conflictColumns = None, updateColumns = None):
    """按业务幂等键批量写 pb_person_centroid：

    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING

    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值
    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING
    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    conflict = list(conflictColumns) if conflictColumns else list(CONFLICT_COLUMNS.get(tableName, ()))
    update = list(updateColumns) if updateColumns else ()
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        conflictColumns=conflict, updateColumns=update,
        fillStandard=True)
    return rtn



# pb_person_centroid 修改记录
def update_pb_person_centroid(tableName, recID, dataSet):
    """按 recID 改一条 pb_person_centroid，返回影响行数（0 = 无字段可改或没命中）。

    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略
    - TEXT 字段传空串是真的「清空」（不会被当成未提供）
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("update_pb_person_centroid: 跳过无法归一的字段 %s" % skipList)
    if not saveSet:
        return 0
    saveSet["modifyYMDHMS"] = misc.getTime()
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_person_centroid 删除记录
def delete_pb_person_centroid(tableName, recID, hardDelete = False):
    """删一条 pb_person_centroid，返回影响行数。

    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS
    hardDelete=True           -> 物理 DELETE

    ⚠️ 软删除后记录仍在表里，业务幂等键（personCode）依然被唯一索引占着；
       想复用同一条记录请走 update，别指望再 insert 一遍。
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    if hardDelete:
        return deleteTableGeneral(tableName, "recID = %s", [recID])
    saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_person_centroid 删表
def drop_pb_person_centroid(tableName):
    """删除 pb_person_centroid 表及其索引（不可逆；只给「改表要重建」用）。"""
    return dropTableGeneral(tableName)


# ==========================================================================
# pb_photo_person 照片-人员关联
# ==========================================================================

# pb_photo_person 建表（幂等：已存在直接返回 True，不重复建）
def create_pb_photo_person(tableName):
    """建 pb_photo_person 表 + 索引。返回 True = 表已就绪。"""
    if tableName not in TABLE_COLUMNS:
        _LOG.error("create_pb_photo_person: 非法表名 %r" % tableName)
        return False
    if chkTableExist(tableName):
        return True
    db = dbHandle()
    if db.executeWrite(createTableSQL_pb_photo_person(tableName)) == sqliteHandle.RET_ERROR:
        return False
    for indexSql in indexSqlList_pb_photo_person():
        if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:
            db.rollbackWrite()
            return False
    return chkTableExist(tableName)



def createTableSQL_pb_photo_person(tableName):
    """pb_photo_person 的建表 DDL（表名来自调用方，必须是 pb_photo_person）。"""
    sqlStr = (
        "CREATE TABLE IF NOT EXISTS " + tableName + " ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "linkKey TEXT NOT NULL,"
        "photoCode TEXT NOT NULL,"
        "personCode TEXT NOT NULL,"
        "faceCode TEXT,"
        "confidence NUMERIC,"
        "source INTEGER NOT NULL DEFAULT 0,"
        "label TEXT,"
        "memo TEXT,"
        "regID TEXT,"
        "regYMDHMS TEXT,"
        "modifyID TEXT,"
        "modifyYMDHMS TEXT,"
        "delFlag TEXT"
        ");"
    )
    return sqlStr



def indexSqlList_pb_photo_person():
    """pb_photo_person 的索引 DDL（清单见 数据库设计.md §五，全部 IF NOT EXISTS 故幂等）。"""
    sqlList = [
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_photo_person_linkKey ON pb_photo_person(linkKey);',   # txt-UNIQUE
        'CREATE INDEX IF NOT EXISTS idx_pb_photo_person_personCode ON pb_photo_person(personCode);',   # INDEX_SPEC
        'CREATE INDEX IF NOT EXISTS idx_pb_photo_person_photoCode ON pb_photo_person(photoCode);',   # INDEX_SPEC
    ]
    return sqlList



# pb_photo_person 查询记录
def query_pb_photo_person(tableName, recID = 0, linkKey = "", photoCode = "", personCode = "",
        nullFields = (), delFlag = "0", mode = "full", orderBy = "recID",
        descFlag = False, limitNum = 0, offsetNum = 0):
    """查 pb_photo_person。

    参数
    ----
    recID        : int  —— 主键精确查，>0 时生效
    linkKey      : str  —— 非空时等值过滤
    photoCode    : str  —— 非空时等值过滤
    personCode   : str  —— 非空时等值过滤
    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内
    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限
    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存
    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID
    descFlag     : bool —— 是否倒序
    limitNum     : int  —— >0 时生效
    offsetNum    : int  —— 分页偏移

    返回
    ----
    list[dict]（空列表 = 没查到，不是出错）
    """
    result = []
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    valuesList = []
    whereList = []
    try:
        try:
            recID = int(recID)
        except (TypeError, ValueError):
            recID = 0
        if recID > 0:
            whereList.append("recID = %s")
            valuesList.append(recID)

        for fieldName, fieldValue in (('linkKey', linkKey), ('photoCode', photoCode), ('personCode', personCode)):
            if fieldValue is not None and fieldValue != "":
                whereList.append(fieldName + " = %s")
                valuesList.append(fieldValue)

        for fieldName in (nullFields or ()):
            if fieldName in NULLABLE_FIELDS.get(tableName, ()):
                whereList.append(fieldName + " IS NULL")

        if delFlag not in (None, "", "*"):
            whereList.append("delFlag = %s")
            valuesList.append(delFlag)

        columnList = ["*"]
        if mode == "light":
            columnList = [c["name"] for c in TABLE_COLUMNS[tableName]
                if c["sqliteType"] != "BLOB"]
            if not columnList:
                columnList = ["*"]
        sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName
        if whereList:
            sqlStr += " WHERE " + " AND ".join(whereList)
        orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"
        sqlStr += " ORDER BY " + orderField
        if descFlag:
            sqlStr += " DESC"
        if int(limitNum or 0) > 0:
            sqlStr += " LIMIT %s OFFSET %s"
            valuesList.append(int(limitNum))
            valuesList.append(int(offsetNum or 0))

        if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
            return result
        result = db.fetchAll()
    except Exception as e:
        _LOG.error("query_pb_photo_person: %s" % e)
    return result



# pb_photo_person 增加记录
def insert_pb_photo_person(tableName, dataSet):
    """新增一条 pb_photo_person，返回新行 recID（<=0 表示失败）。

    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略
    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值
    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间
    """
    result = 0
    if tableName not in TABLE_COLUMNS:
        return result
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("insert_pb_photo_person: 跳过无法归一的字段 %s" % skipList)
    if "delFlag" not in saveSet:
        saveSet["delFlag"] = comGD.DEL_FLAG_NO
    if "regYMDHMS" not in saveSet:
        saveSet["regYMDHMS"] = misc.getTime()
    result = insertTableGeneral(tableName, saveSet)
    return result



# pb_photo_person 批量增加记录（executemany + 显式事务）
def insertMany_pb_photo_person(tableName, dataSetList):
    """批量新增 pb_photo_person（整批一次提交，失败整批回滚），返回写入行数。

    步骤 3 扫描入库走这里：一次几百行，别一行一提交。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        fillStandard=True)
    return rtn



# pb_photo_person 批量 upsert（幂等重扫走这里）
def upsertMany_pb_photo_person(tableName, dataSetList, conflictColumns = None, updateColumns = None):
    """按业务幂等键批量写 pb_photo_person：

    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING

    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值
    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING
    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    conflict = list(conflictColumns) if conflictColumns else list(CONFLICT_COLUMNS.get(tableName, ()))
    update = list(updateColumns) if updateColumns else ()
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        conflictColumns=conflict, updateColumns=update,
        fillStandard=True)
    return rtn



# pb_photo_person 修改记录
def update_pb_photo_person(tableName, recID, dataSet):
    """按 recID 改一条 pb_photo_person，返回影响行数（0 = 无字段可改或没命中）。

    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略
    - TEXT 字段传空串是真的「清空」（不会被当成未提供）
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("update_pb_photo_person: 跳过无法归一的字段 %s" % skipList)
    if not saveSet:
        return 0
    saveSet["modifyYMDHMS"] = misc.getTime()
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_photo_person 删除记录
def delete_pb_photo_person(tableName, recID, hardDelete = False):
    """删一条 pb_photo_person，返回影响行数。

    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS
    hardDelete=True           -> 物理 DELETE

    ⚠️ 软删除后记录仍在表里，业务幂等键（linkKey）依然被唯一索引占着；
       想复用同一条记录请走 update，别指望再 insert 一遍。
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    if hardDelete:
        return deleteTableGeneral(tableName, "recID = %s", [recID])
    saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_photo_person 删表
def drop_pb_photo_person(tableName):
    """删除 pb_photo_person 表及其索引（不可逆；只给「改表要重建」用）。"""
    return dropTableGeneral(tableName)


# ==========================================================================
# pb_scan_job 扫描任务
# ==========================================================================

# pb_scan_job 建表（幂等：已存在直接返回 True，不重复建）
def create_pb_scan_job(tableName):
    """建 pb_scan_job 表 + 索引。返回 True = 表已就绪。"""
    if tableName not in TABLE_COLUMNS:
        _LOG.error("create_pb_scan_job: 非法表名 %r" % tableName)
        return False
    if chkTableExist(tableName):
        return True
    db = dbHandle()
    if db.executeWrite(createTableSQL_pb_scan_job(tableName)) == sqliteHandle.RET_ERROR:
        return False
    for indexSql in indexSqlList_pb_scan_job():
        if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:
            db.rollbackWrite()
            return False
    return chkTableExist(tableName)



def createTableSQL_pb_scan_job(tableName):
    """pb_scan_job 的建表 DDL（表名来自调用方，必须是 pb_scan_job）。"""
    sqlStr = (
        "CREATE TABLE IF NOT EXISTS " + tableName + " ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "jobCode TEXT NOT NULL,"
        "rootPath TEXT NOT NULL,"
        "batchSize INTEGER NOT NULL DEFAULT 100,"
        "batchIndex INTEGER NOT NULL DEFAULT 0,"
        "totalCount INTEGER NOT NULL DEFAULT 0,"
        "processedCount INTEGER NOT NULL DEFAULT 0,"
        "addedCount INTEGER NOT NULL DEFAULT 0,"
        "skippedCount INTEGER NOT NULL DEFAULT 0,"
        "duplicateCount INTEGER NOT NULL DEFAULT 0,"
        "pendingCount INTEGER NOT NULL DEFAULT 0,"
        "lastCursor TEXT,"
        "jobStatus TEXT NOT NULL DEFAULT 'IDLE',"
        "startedYMDHMS TEXT,"
        "finishedYMDHMS TEXT,"
        "errMsg TEXT,"
        "label TEXT,"
        "memo TEXT,"
        "regID TEXT,"
        "regYMDHMS TEXT,"
        "modifyID TEXT,"
        "modifyYMDHMS TEXT,"
        "delFlag TEXT"
        ");"
    )
    return sqlStr



def indexSqlList_pb_scan_job():
    """pb_scan_job 的索引 DDL（清单见 数据库设计.md §五，全部 IF NOT EXISTS 故幂等）。"""
    sqlList = [
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_scan_job_jobCode ON pb_scan_job(jobCode);',   # txt-UNIQUE
    ]
    return sqlList



# pb_scan_job 查询记录
def query_pb_scan_job(tableName, recID = 0, jobCode = "", jobStatus = "", nullFields = (),
        delFlag = "0", mode = "full", orderBy = "recID", descFlag = False,
        limitNum = 0, offsetNum = 0):
    """查 pb_scan_job。

    参数
    ----
    recID        : int  —— 主键精确查，>0 时生效
    jobCode      : str  —— 非空时等值过滤
    jobStatus    : str  —— 非空时等值过滤
    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内
    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限
    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存
    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID
    descFlag     : bool —— 是否倒序
    limitNum     : int  —— >0 时生效
    offsetNum    : int  —— 分页偏移

    返回
    ----
    list[dict]（空列表 = 没查到，不是出错）
    """
    result = []
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    valuesList = []
    whereList = []
    try:
        try:
            recID = int(recID)
        except (TypeError, ValueError):
            recID = 0
        if recID > 0:
            whereList.append("recID = %s")
            valuesList.append(recID)

        for fieldName, fieldValue in (('jobCode', jobCode), ('jobStatus', jobStatus)):
            if fieldValue is not None and fieldValue != "":
                whereList.append(fieldName + " = %s")
                valuesList.append(fieldValue)

        for fieldName in (nullFields or ()):
            if fieldName in NULLABLE_FIELDS.get(tableName, ()):
                whereList.append(fieldName + " IS NULL")

        if delFlag not in (None, "", "*"):
            whereList.append("delFlag = %s")
            valuesList.append(delFlag)

        columnList = ["*"]
        if mode == "light":
            columnList = [c["name"] for c in TABLE_COLUMNS[tableName]
                if c["sqliteType"] != "BLOB"]
            if not columnList:
                columnList = ["*"]
        sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName
        if whereList:
            sqlStr += " WHERE " + " AND ".join(whereList)
        orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"
        sqlStr += " ORDER BY " + orderField
        if descFlag:
            sqlStr += " DESC"
        if int(limitNum or 0) > 0:
            sqlStr += " LIMIT %s OFFSET %s"
            valuesList.append(int(limitNum))
            valuesList.append(int(offsetNum or 0))

        if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
            return result
        result = db.fetchAll()
    except Exception as e:
        _LOG.error("query_pb_scan_job: %s" % e)
    return result



# pb_scan_job 增加记录
def insert_pb_scan_job(tableName, dataSet):
    """新增一条 pb_scan_job，返回新行 recID（<=0 表示失败）。

    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略
    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值
    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间
    """
    result = 0
    if tableName not in TABLE_COLUMNS:
        return result
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("insert_pb_scan_job: 跳过无法归一的字段 %s" % skipList)
    if "delFlag" not in saveSet:
        saveSet["delFlag"] = comGD.DEL_FLAG_NO
    if "regYMDHMS" not in saveSet:
        saveSet["regYMDHMS"] = misc.getTime()
    result = insertTableGeneral(tableName, saveSet)
    return result



# pb_scan_job 批量增加记录（executemany + 显式事务）
def insertMany_pb_scan_job(tableName, dataSetList):
    """批量新增 pb_scan_job（整批一次提交，失败整批回滚），返回写入行数。

    步骤 3 扫描入库走这里：一次几百行，别一行一提交。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        fillStandard=True)
    return rtn



# pb_scan_job 批量 upsert（幂等重扫走这里）
def upsertMany_pb_scan_job(tableName, dataSetList, conflictColumns = None, updateColumns = None):
    """按业务幂等键批量写 pb_scan_job：

    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING

    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值
    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING
    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    conflict = list(conflictColumns) if conflictColumns else list(CONFLICT_COLUMNS.get(tableName, ()))
    update = list(updateColumns) if updateColumns else ()
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        conflictColumns=conflict, updateColumns=update,
        fillStandard=True)
    return rtn



# pb_scan_job 修改记录
def update_pb_scan_job(tableName, recID, dataSet):
    """按 recID 改一条 pb_scan_job，返回影响行数（0 = 无字段可改或没命中）。

    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略
    - TEXT 字段传空串是真的「清空」（不会被当成未提供）
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("update_pb_scan_job: 跳过无法归一的字段 %s" % skipList)
    if not saveSet:
        return 0
    saveSet["modifyYMDHMS"] = misc.getTime()
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_scan_job 删除记录
def delete_pb_scan_job(tableName, recID, hardDelete = False):
    """删一条 pb_scan_job，返回影响行数。

    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS
    hardDelete=True           -> 物理 DELETE

    ⚠️ 软删除后记录仍在表里，业务幂等键（jobCode）依然被唯一索引占着；
       想复用同一条记录请走 update，别指望再 insert 一遍。
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    if hardDelete:
        return deleteTableGeneral(tableName, "recID = %s", [recID])
    saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_scan_job 删表
def drop_pb_scan_job(tableName):
    """删除 pb_scan_job 表及其索引（不可逆；只给「改表要重建」用）。"""
    return dropTableGeneral(tableName)


# ==========================================================================
# pb_review_log 纠错操作日志
# ==========================================================================

# pb_review_log 建表（幂等：已存在直接返回 True，不重复建）
def create_pb_review_log(tableName):
    """建 pb_review_log 表 + 索引。返回 True = 表已就绪。"""
    if tableName not in TABLE_COLUMNS:
        _LOG.error("create_pb_review_log: 非法表名 %r" % tableName)
        return False
    if chkTableExist(tableName):
        return True
    db = dbHandle()
    if db.executeWrite(createTableSQL_pb_review_log(tableName)) == sqliteHandle.RET_ERROR:
        return False
    for indexSql in indexSqlList_pb_review_log():
        if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:
            db.rollbackWrite()
            return False
    return chkTableExist(tableName)



def createTableSQL_pb_review_log(tableName):
    """pb_review_log 的建表 DDL（表名来自调用方，必须是 pb_review_log）。"""
    sqlStr = (
        "CREATE TABLE IF NOT EXISTS " + tableName + " ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT,"
        "logCode TEXT NOT NULL,"
        "opType TEXT NOT NULL,"
        "faceCode TEXT,"
        "photoCode TEXT,"
        "fromPersonCode TEXT,"
        "toPersonCode TEXT,"
        "similarity NUMERIC,"
        "faceCount INTEGER NOT NULL DEFAULT 0,"
        "detail TEXT,"
        "isRevertible INTEGER NOT NULL DEFAULT 0,"
        "revertedByLogCode TEXT,"
        "opUser TEXT,"
        "opYMDHMS TEXT,"
        "label TEXT,"
        "memo TEXT,"
        "regID TEXT,"
        "regYMDHMS TEXT,"
        "modifyID TEXT,"
        "modifyYMDHMS TEXT,"
        "delFlag TEXT"
        ");"
    )
    return sqlStr



def indexSqlList_pb_review_log():
    """pb_review_log 的索引 DDL（清单见 数据库设计.md §五，全部 IF NOT EXISTS 故幂等）。"""
    sqlList = [
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_review_log_logCode ON pb_review_log(logCode);',   # txt-UNIQUE
        'CREATE INDEX IF NOT EXISTS idx_pb_review_log_faceCode ON pb_review_log(faceCode);',   # INDEX_SPEC
        'CREATE INDEX IF NOT EXISTS idx_pb_review_log_opType ON pb_review_log(opType);',   # INDEX_SPEC
        'CREATE INDEX IF NOT EXISTS idx_pb_review_log_isRevertible ON pb_review_log(isRevertible) WHERE isRevertible=1 AND revertedByLogCode IS NULL;',   # INDEX_SPEC
    ]
    return sqlList



# pb_review_log 查询记录
def query_pb_review_log(tableName, recID = 0, logCode = "", opType = "", faceCode = "",
        photoCode = "", fromPersonCode = "", toPersonCode = "",
        nullFields = (), delFlag = "0", mode = "full", orderBy = "recID",
        descFlag = False, limitNum = 0, offsetNum = 0):
    """查 pb_review_log。

    参数
    ----
    recID        : int  —— 主键精确查，>0 时生效
    logCode      : str  —— 非空时等值过滤
    opType       : str  —— 非空时等值过滤
    faceCode     : str  —— 非空时等值过滤
    photoCode    : str  —— 非空时等值过滤
    fromPersonCode: str  —— 非空时等值过滤
    toPersonCode : str  —— 非空时等值过滤
    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内
    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限
    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存
    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID
    descFlag     : bool —— 是否倒序
    limitNum     : int  —— >0 时生效
    offsetNum    : int  —— 分页偏移

    返回
    ----
    list[dict]（空列表 = 没查到，不是出错）
    """
    result = []
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    valuesList = []
    whereList = []
    try:
        try:
            recID = int(recID)
        except (TypeError, ValueError):
            recID = 0
        if recID > 0:
            whereList.append("recID = %s")
            valuesList.append(recID)

        for fieldName, fieldValue in (('logCode', logCode), ('opType', opType), ('faceCode', faceCode), ('photoCode', photoCode), ('fromPersonCode', fromPersonCode), ('toPersonCode', toPersonCode)):
            if fieldValue is not None and fieldValue != "":
                whereList.append(fieldName + " = %s")
                valuesList.append(fieldValue)

        for fieldName in (nullFields or ()):
            if fieldName in NULLABLE_FIELDS.get(tableName, ()):
                whereList.append(fieldName + " IS NULL")

        if delFlag not in (None, "", "*"):
            whereList.append("delFlag = %s")
            valuesList.append(delFlag)

        columnList = ["*"]
        if mode == "light":
            columnList = [c["name"] for c in TABLE_COLUMNS[tableName]
                if c["sqliteType"] != "BLOB"]
            if not columnList:
                columnList = ["*"]
        sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName
        if whereList:
            sqlStr += " WHERE " + " AND ".join(whereList)
        orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"
        sqlStr += " ORDER BY " + orderField
        if descFlag:
            sqlStr += " DESC"
        if int(limitNum or 0) > 0:
            sqlStr += " LIMIT %s OFFSET %s"
            valuesList.append(int(limitNum))
            valuesList.append(int(offsetNum or 0))

        if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
            return result
        result = db.fetchAll()
    except Exception as e:
        _LOG.error("query_pb_review_log: %s" % e)
    return result



# pb_review_log 增加记录
def insert_pb_review_log(tableName, dataSet):
    """新增一条 pb_review_log，返回新行 recID（<=0 表示失败）。

    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略
    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值
    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间
    """
    result = 0
    if tableName not in TABLE_COLUMNS:
        return result
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("insert_pb_review_log: 跳过无法归一的字段 %s" % skipList)
    if "delFlag" not in saveSet:
        saveSet["delFlag"] = comGD.DEL_FLAG_NO
    if "regYMDHMS" not in saveSet:
        saveSet["regYMDHMS"] = misc.getTime()
    result = insertTableGeneral(tableName, saveSet)
    return result



# pb_review_log 批量增加记录（executemany + 显式事务）
def insertMany_pb_review_log(tableName, dataSetList):
    """批量新增 pb_review_log（整批一次提交，失败整批回滚），返回写入行数。

    步骤 3 扫描入库走这里：一次几百行，别一行一提交。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        fillStandard=True)
    return rtn



# pb_review_log 批量 upsert（幂等重扫走这里）
def upsertMany_pb_review_log(tableName, dataSetList, conflictColumns = None, updateColumns = None):
    """按业务幂等键批量写 pb_review_log：

    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING

    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值
    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING
    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return result
    conflict = list(conflictColumns) if conflictColumns else list(CONFLICT_COLUMNS.get(tableName, ()))
    update = list(updateColumns) if updateColumns else ()
    rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,
        conflictColumns=conflict, updateColumns=update,
        fillStandard=True)
    return rtn



# pb_review_log 修改记录
def update_pb_review_log(tableName, recID, dataSet):
    """按 recID 改一条 pb_review_log，返回影响行数（0 = 无字段可改或没命中）。

    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略
    - TEXT 字段传空串是真的「清空」（不会被当成未提供）
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    saveSet, skipList = normalizeDataSet(tableName, dataSet)
    saveSet.pop("recID", None)
    if skipList:
        _LOG.warning("update_pb_review_log: 跳过无法归一的字段 %s" % skipList)
    if not saveSet:
        return 0
    saveSet["modifyYMDHMS"] = misc.getTime()
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_review_log 删除记录
def delete_pb_review_log(tableName, recID, hardDelete = False):
    """删一条 pb_review_log，返回影响行数。

    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS
    hardDelete=True           -> 物理 DELETE

    ⚠️ 软删除后记录仍在表里，业务幂等键（logCode）依然被唯一索引占着；
       想复用同一条记录请走 update，别指望再 insert 一遍。
    """
    if tableName not in TABLE_COLUMNS:
        return 0
    if hardDelete:
        return deleteTableGeneral(tableName, "recID = %s", [recID])
    saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}
    return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)



# pb_review_log 删表
def drop_pb_review_log(tableName):
    """删除 pb_review_log 表及其索引（不可逆；只给「改表要重建」用）。"""
    return dropTableGeneral(tableName)



# ==========================================================================
# 全库操作
# ==========================================================================

def checkSqliteDataBase():
    """建库入口：按 TABLE_ORDER 建全表（幂等），返回 {表名: 是否就绪}。

    ⚠️ **本模块 import 时不会自动建库**，必须显式调用（tools/build_db.py 就调它）——
       「import 一个模块就在磁盘上落库文件」这种事不能有。
    """
    return createAllTables()


if __name__ == "__main__":
    print("sqliteCommon _VERSION:", _VERSION)
    print("表清单            :", len(TABLE_ORDER), "张", TABLE_ORDER)
    for tableName in TABLE_ORDER:
        print("   %-22s %-10s 字段 %2d  索引 %d"
              % (tableName, TABLE_CN.get(tableName, ""),
                 len(TABLE_COLUMNS[tableName]), len(TABLE_INDEXES[tableName])))
    print("库文件(未连接)   :", paths.db_file())
