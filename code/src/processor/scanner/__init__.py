#! /usr/bin/env python3
#encoding: utf-8

#Filename: __init__.py
#Description: processor.scanner：扫描器（步骤 3）
#   walker.py  遍历 + 双 hash（8MB 分块流式）
#   meta.py    EXIF / 年份识别 / GPS 逆地理
#   runner.py  增量判定 + 批量 upsert + 进度 + 批次限流 + 断点续扫
