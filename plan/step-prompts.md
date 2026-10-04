# photo-browser 分步开发提示语（12 步）

> 配套：`plan/开发计划.md`（总纲）｜`plan/数据库设计.md`（表定义）｜`plan/UI/photo-browser UI 设计.md`（信息架构）
>
> **使用方法**：每一步**单独开一个新对话**，把下面对应的提示语整段复制粘贴进去。
> 前一步验收未通过，不要开始下一步。
> 每步结束统一输出：**改动文件清单 + 验收结果 + 遗留问题**。

---

## 全局约定（每步都适用，已写进各提示语，此处仅备查）

| 项 | 约定 |
| --- | --- |
| 代码根 | `d:/home/lianyi/git/photo-browser/code/` |
| 文档根 | `d:/home/lianyi/git/photo-browser/plan/` |
| Python | `C:\Users\NINGMEI\.workbuddy\binaries\python\versions\3.13.12\python.exe`（venv 建在 `code/.venv`） |
| pip 源 | **必须官方源** `https://pypi.org/simple`（清华镜像在本机失效） |
| photoRoot | `d:\PhotoLib`（可改，`code/src/config/local_settings.py`） |
| 原图 | **绝对只读**：不写、不删、不改名 |
| 业务层 | **禁止裸 SQL**，一律走 `sqliteCommon` |
| 表定义 | `code/src/database/pb_*.txt` 是唯一数据源，**禁止手工改 `auto_generated/`** |
| 网络 | 服务只绑 `127.0.0.1` |

---
---

# 步骤 1 · 工程基线与配置骨架

```text
【photo-browser · 步骤 1/12 · 工程基线与配置骨架】

## 目标
建立可运行的 Python 后端工程骨架、配置体系与路径解析模块。本步不碰数据库、不碰扫描、不碰前端。

## 前置
无。这是第 1 步。

## 必须先读的项目文档
- d:/home/lianyi/git/photo-browser/plan/开发计划.md          ← 总纲，务必读完
- d:/home/lianyi/git/photo-browser/plan/数据库设计.md        ← 表定义与三层结构
- d:/home/lianyi/git/photo-browser/README.md

## 本步产出文件
1. d:/home/lianyi/git/photo-browser/requirements.txt
   - 后端依赖清单，注释里写明：必须用官方源 -i https://pypi.org/simple
   - 基础：fastapi, uvicorn[standard], pydantic-settings, python-multipart
   - 图像：pillow, opencv-python-headless, numpy
   - 元数据：reverse-geocoder（标注为可选依赖）
   - 人脸：insightface, onnxruntime（CPU 版，禁止 onnxruntime-gpu）
   - 联系人：vobject
   - 末尾注释：insightface 若在 Python 3.13 装不上，降级 Python 3.12 重建 venv
2. code/src/config/local_settings.py
   - PHOTO_ROOT: str = r"d:\PhotoLib"      # 默认值，可改
   - THUMB_ROOT: str = ""                   # 空 = 派生 <PHOTO_ROOT>\thumb
   - DB_FILE: str = ""                      # 空 = 派生 <PHOTO_ROOT>\db\photolib.db
3. code/src/config/local_settings.py.example   （同结构，仅作模板，入库）
4. code/src/config/basicSettings.py
   - 批大小 BATCH_SIZE=100；阈值 T_HIGH=0.55、T_LOW=0.35（以 S0 实测为准）
   - 质量过滤：MIN_DET_SCORE=0.6、MIN_FACE_EDGE=64、MAX_YAW=45
   - 支持的照片扩展名白名单（jpg/jpeg/png/webp/heic/bmp/tif/tiff/dng/cr2/nef/arf/wmv…）
   - 文件 hash 分块大小 8MB
5. code/src/config/sqliteSettings.py
   - 承载 PRAGMA 常量列表与 sqlite 文件装配入口（建库逻辑留到步骤 2，本步只放常量与装配函数签名）
6. code/src/common/miscCommon.py
   - setLogNew(...) 日志函数（复用 contentHub 风格：控制台 + 文件）
   - 当前时间 YYYYMMDDHHMMSS 字符串、ISO8601 UTC 字符串
   - 字符串/字节常用工具
7. code/src/common/globalDefinition.py
   - 错误码常量、任务状态机常量（IDLE/RUNNING/PAUSED/DONE/FAILED）、scanState 常量（0/1/2/3）
8. code/src/common/paths.py
   - photo_dir() / thumb_dir() / db_file()：空值时从 PHOTO_ROOT 派生，返回规范化绝对路径
   - validate_layout(photo, thumb, db)：三者互不嵌套、不得等于 PHOTO_ROOT，违规抛异常
   - ensure_dirs()：创建 thumb/db/imports/exports 目录（**绝不创建或写入 photo/**）
   - normalize_relpath(p)：把相对路径统一为正斜杠、去 ./、去首尾空白、Unicode NFC —— **仅用于算 hash，不得改写库中 relPath 原值**
   - dump_paths()：打印解析后的全部绝对路径，启动时调用
9. code/src/test/conftest.py 与 code/src/test/test_paths.py
   - 路径派生、嵌套校验（合法/非法各一组）、normalize_relpath 的单测

## 硬约束
- 禁止连接数据库、禁止建表、禁止写 pb_*.txt
- 禁止在任何情况下写入 photoRoot 下的 photo 目录
- local_settings.py 不入库（.gitignore 已忽略），必须提供 .example
- 不引入 SQLAlchemy（本项目用原生 sqlite3 + 代码生成器）
- 不引入 pymysql / cryptography（那是 MySQL 才需要）
- 配置项集中，禁止散落在业务模块里硬编码

## 验收清单（请逐条实际运行验证，不要只写代码就宣称通过）
1. python -m venv code/.venv 并按 requirements.txt 装依赖成功
2. python -c "import" 全部新增模块无报错
3. 用默认配置运行，能打印出：
   photo     = d:\PhotoLib\photo
   thumb     = d:\PhotoLib\thumb
   database  = d:\PhotoLib\db\photolib.db
4. 改 local_settings.py 的 PHOTO_ROOT 后再跑，路径随之变化，无需改任何业务代码
5. 把 THUMB_ROOT 故意设成 photo 的子目录，validate_layout 能抛异常
6. pytest code/src/test 通过

## 输出格式（本步结束后必须给出）
1. 改动文件清单（新增/修改，逐个列路径）
2. 验收结果（上面 6 条逐条：命令 + 实际输出或结论）
3. 遗留问题与需要我决策的点

有问题随时提，不要自行假设需求。
```

---
---

# 步骤 2 · SQLite 运行层 + 代码生成器 + 建库

```text
【photo-browser · 步骤 2/12 · SQLite 运行层 + 代码生成器 + 建库】

## 目标
打通「表定义 txt → 代码生成器 → 运行层 sqliteHandle → 真实建库」全链路。

## 前置
步骤 1 已完成并验收通过（配置与路径解析可用）。

## 必须先读的项目文档
- plan/开发计划.md 第二节（三层结构）、第 6.1 节（类型映射）、第 6.2 节（PRAGMA）
- plan/数据库设计.md（8 张表字段、§1.5 类型映射、§五 索引清单、§八 生成器工作流）
- 参考工程（**只读参考，不要直接复制粘贴）：
  - d:/home/lianyi/git/stock_rotation_strategy/src/common/sqliteHandle.py   ← SQLite 运行层参照
  - d:/home/lianyi/git/stock_rotation_strategy/src/database/mysqlCodeGenerator.py  ← 生成器结构参照
  - d:/home/lianyi/git/contentHub/code/src/database/*.txt  ← 表定义书写规范参照

## 第一件事：把 recID 改成 INT
把 code/src/database/ 下 8 个表定义文件的首行统一改为：
    recID INT AUTO_INCREMENT PRIMARY KEY COMMENT '记录ID'
涉及：pb_family / pb_person / pb_person_category / pb_photo / pb_face / pb_person_centroid / pb_photo_person / pb_scan_job
这是唯一数据源，改完才能跑生成器。禁止只改生成产物。

## 本步产出文件
1. code/src/common/sqliteHandle.py
   - open_db(db_path, read_only=False)：row_factory=sqlite3.Row、check_same_thread=False
   - 读连接与写连接**各自**执行同一套 PRAGMA：
     journal_mode=WAL / foreign_keys=ON / synchronous=NORMAL / busy_timeout=5000 / temp_store=MEMORY / cache_size=-64000
   - executeRead / executeWrite / fetchAll / fetchMany(2000) / fetchOne
   - 占位符转换：SQL 里 %s → ?（防注入）；禁止字符串拼接值
   - 事务：executemany 批量写 + 显式 commit/rollback
2. code/src/database/sqliteCodeGenerator.py
   - 读 pb_*.txt（单字段一行：名称 类型 [NOT NULL|UNIQUE|NULL|DEFAULT x] COMMENT '...'）
   - 类型映射（**顺序敏感，INT 规则必须排在 BIGINT 之前**）：
     INT AUTO_INCREMENT PRIMARY KEY -> INTEGER PRIMARY KEY AUTOINCREMENT
     BIGINT/INT/SMALLINT/TINYINT     -> INTEGER
     VARCHAR/CHAR/MEDIUMTEXT/TEXT    -> TEXT
     DECIMAL/FLOAT/DOUBLE            -> NUMERIC
     MEDIUMBLOB                      -> BLOB
   - 保留 #common begin/end 区段（通用 insertTableGeneral / updateTableGeneral / chkTableExist）
   - 各表生成：create_pb_xxx / query_pb_xxx / insert_pb_xxx / update_pb_xxx / delete_pb_xxx
   - 建表时一并输出索引（清单照 plan/数据库设计.md §五，命名 idx_<表>_<字段>，含 pb_face(personCode IS NULL) 部分索引）
   - 产物落 code/src/database/auto_generated/sqliteCommon.py
   - 注意：SQLite 无 VARCHAR 类型，VARCHAR(n) 的长度 (n) 不被强制，业务层自行校验
3. code/src/database/auto_generated/sqliteCommon.py   ← 生成产物，禁止手工改
4. code/src/tools/build_db.py
   - 按 pb_*.txt 建库建表建索引；缺目录自动创建（db 目录）
   - 支持重复执行（幂等）

## 硬约束
- 业务层禁止裸 SQL（本步只生成数据访问层，不写业务）
- 不建物理外键；关联一律用业务编码 photoCode / personCode
- 不引入 SQLAlchemy；本项目以 数据库设计.md 为准（MVP_plan.md 里的 SQLAlchemy 片段已废弃）
- 生成物只能由生成器产生，禁止手工编辑 auto_generated/
- 不写真实照片数据，本步只建空表结构

## 验收清单（逐条实际运行验证）
1. 运行生成器，auto_generated/sqliteCommon.py 成功产出，且文件头标注「自动生成，请勿手改」
2. python code/src/tools/build_db.py 建库成功，d:\PhotoLib\db\photolib.db 存在
3. sqlite3 / PRAGMA 查询确认 8 张表齐全，索引齐全
4. PRAGMA table_info(pb_photo) 显示 recID 类型为 INTEGER 且 pk=1
5. PRAGMA journal_mode 返回 wal；PRAGMA foreign_keys 返回 1
6. 重复执行 build_db.py 不报错、不重复建表（chkTableExist 幂等）
7. 写一条测试记录走通用 insert → query → update → 删除，验证 %s→? 转换与 blob 读写正常
8. PRAGMA integrity_check 返回 ok

## 输出格式
1. 改动文件清单
2. 验收结果（8 条逐条给命令与实际输出）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 3 · 扫描器（遍历 / hash / EXIF / 去重 / 批次限流）

```text
【photo-browser · 步骤 3/12 · 扫描器】

## 目标
把 photo 目录变成库里的结构化事实数据：遍历、双 hash、EXIF/年份、去重判定、增量扫描、批次限流与断点续扫。

## 前置
步骤 2 已完成（8 张表已建、sqliteCommon 可用）。

## 必须先读的项目文档
- plan/开发计划.md 第 3.1 节（扫描链路）与第 3.3 节（单写入者）
- plan/数据库设计.md §4.4 pb_photo 字段含义
- plan/MVP_plan.md 的 S2 章节

## 本步产出文件
1. code/src/processor/scanner/walker.py
   - 递归遍历 photoDir，扩展名白名单过滤
   - relPath（相对 photo 根，正斜杠，**保留磁盘原值不改写**）
   - relPathHash = sha256(规范化后的相对路径)；规范化 = 统一 /、去 ./、Unicode NFC，仅用于算 hash
   - fileHash = 流式 SHA-256，**分块 8MB**，禁止整文件读入
2. code/src/processor/scanner/meta.py
   - Pillow 读 EXIF：takenAt(DateTimeOriginal, UTC)、width/height/orientation、cameraModel、lat/lon
   - shotYear 识别优先级：EXIF → 文件名（识别 mmexport* 13 位毫秒时间戳、常见日期格式）→ mtime（最不可靠）
   - 截图类文件名（Screenshot* / 截图*）→ shotYear = NULL（不参与跨桶比对）
   - GPS 逆地理：reverse_geocoder 若导入失败或无数据，placeName 留空并记录一次 warning，**不得报错中断**
3. code/src/processor/scanner/runner.py
   - 增量三路判定：
     relPathHash 未命中                       → 新增
     relPathHash 命中且 fileHash 相同          → 跳过（幂等）
     relPathHash 命中但 fileHash 不同          → 更新元数据，人脸需重提取
     fileHash 命中但 relPathHash 未命中        → 标记「移动/重命名」，**不自动改路径**
     fileHash 命中且另一条 relPathHash 不同    → isDuplicate=1 + dupOfPhotoCode
   - 库中存在但磁盘找不到 → isMissing=1，**不删记录**（可能只是移动硬盘没插）
   - 批量 upsert：executemany，每 500 条提交一次
   - 进度写 pb_scan_job：processedCount/addedCount/skippedCount/duplicateCount/pendingCount/lastCursor
   - **批次限流**：累计处理到 batchSize（默认 100）即 jobStatus=PAUSED 并停下，等待「继续下一批」，从 lastCursor 续扫
4. code/src/schedule/scanScheduler.py
   - 后台任务调度与状态流转：IDLE → RUNNING → (PAUSED → 继续 → RUNNING) / DONE / FAILED
   - jobCode 幂等；异常写 errMsg 并置 FAILED
5. code/src/tools/scan_cli.py（或并入 main/cli.py）
   - 命令行入口：--root 指定扫描根（缺省用 photo_dir()）、--batch-size、--resume <jobCode>
6. code/src/test/：normalize_relpath、relPathHash 幂等、fileHash 分块一致性、增量三路判定的单测

## 硬约束
- **单写入者**：本步可以单进程；涉及 CPU 密集的读图时，子进程绝对不能连数据库，结果经 Queue 回主进程（步骤 5 才真正用到进程池）
- photo 目录**绝对只读**：不写、不删、不改名
- 事务粒度 500 条；中断后可续扫，不重复入库
- 不要在本步做人脸识别或缩略图生成（步骤 4、5 负责）

## 验收清单（准备一个小测试集：约 300 张，含子目录 4–5 级、含重命名、含重复内容、含缺 EXIF 的图）
1. 首次全量扫描：无遗漏、无重复入库
2. 立即第二次扫描：addedCount = 0（幂等性）
3. 新增 100 张：只处理这 100 张，其余跳过
4. 把某个文件改名：识别为「移动」而非「新增+删除」，且不自动改 relPath
5. 同一张图复制到另一个路径：被标记 isDuplicate=1 且 dupOfPhotoCode 正确
6. 临时移走一个子目录再扫描：对应记录被标 isMissing=1 而**没有被删除**
7. batchSize=100：处理到 100 张自动 PAUSED，点「继续」从 lastCursor 接着扫，最终 DONE，计数正确
8. EXIF 缺失的图走文件名/mtime 兜底；截图类 shotYear 为 NULL
9. 库中的 relPath 与磁盘实际路径逐字一致（未被规范化改写）
10. 单测全部通过

## 输出格式
1. 改动文件清单
2. 验收结果（10 条逐条给命令与实际输出/数据）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 4 · 缩略图与原图文件服务

```text
【photo-browser · 步骤 4/12 · 缩略图与原图文件服务】

## 目标
让前端能快速看图：缩略图按需生成 + 落盘缓存到 d:\PhotoLib\thumb\，原图接口支持 Range，并搭起最小 FastAPI 实例。

## 前置
步骤 3 已完成（库里已有照片记录与 fileHash）。

## 必须先读的项目文档
- plan/开发计划.md 第 3 节（数据流）、第 6.3 节（性能）、第 6.4 节（原子写）
- plan/数据库设计.md Q-5（缩略图存文件系统不入库）
- plan/照片管理方案_开源调研与自研设计.md 3.9（API 清单）

## 目录约定（已定稿，不得改动）
photoDir\   只读原图
thumbDir\   生成物，可随时重建
  ├─ thumbs\<fileHash[:2]>\<fileHash>_<size>.webp     size ∈ {200, 400, 800}
  └─ faces\<faceCode[:2]>\<faceCode>.jpg              160px（步骤 5 用）
路径必须**可推导**（pb_photo 没有 thumbPath 字段），由 fileHash + size 计算。

## 本步产出文件
1. code/src/processor/media/thumbStore.py
   - THUMB_SIZES = (200, 400, 800)
   - thumb_relpath(file_hash, size=400) -> "thumbs/<xx>/<hash>_<size>.webp"
   - face_relpath(face_code) -> "faces/<xx>/<faceCode>.jpg"
   - write_atomic(abs_path, data)：先写 <name>.tmp，再 os.replace；失败清理 tmp，**绝不留下半文件**
   - ensure_bucket_dir(relpath)：按需创建分桶目录
2. code/src/processor/media/thumbMaker.py
   - Pillow 打开原图 → 应用 EXIF orientation → 缩放到目标宽 → 存 WebP（质量 80 左右）
   - 保持宽高比；不改动原图任何字节
   - make_thumb(photoRow, size)：命中磁盘直接返回，未命中则生成后原子写
   - make_thumbs_bulk(photoRows, workers)：批量生成，**用进程池**（CPU 解码密集）
3. code/src/processor/media/faceCropper.py
   - crop_from_bbox(原图路径, bbox, outSize=160) -> bytes：按归一化 bbox 裁剪并保存 JPEG
   - bbox 越界要夹紧，不能抛异常导致整张照片失败
4. code/src/main/app.py（最小实例）
   - FastAPI()，挂载 /api 路由与静态资源
   - **只绑 127.0.0.1**（uvicorn host="127.0.0.1"）
5. code/src/api/static.py
   - GET /api/thumb/{photoCode}?size=400
     - 命中磁盘直接返回 FileResponse；未命中按需生成
     - 带 ETag（用 fileHash+size）与 Cache-Control: max-age
     - 支持 If-None-Match 返回 304
   - GET /api/original/{photoCode}
     - **必须支持 Range 请求**（解析 Range: bytes=start-end，返回 206 + Content-Range / Accept-Ranges: bytes）
     - MIME 按扩展名；支持 HEAD
   - GET /api/face/{faceCode}：人脸裁剪图（步骤 5 才有数据，本步先留好接口）
6. code/src/tools/gen_thumbs.py
   - 批量生成缩略图：--size 400 --workers 8 --limit N --resume
7. code/src/test/test_thumb_store.py：路径可推导性、原子写、分桶目录

## 硬约束
- photoDir 绝对只读
- 缩略图**不入库**（pb_photo 无 thumbPath 字段，路径必须能算出来）
- 缩略图必须原子写，禁止半文件被前端读到
- 批量生成用进程池；单张按需生成用线程池
- 服务只绑 127.0.0.1，不开 0.0.0.0
- 本步不做人脸识别

## 验收清单
1. 对测试集批量生成 400px 缩略图成功，文件落在 thumbDir\thumbs\<xx>\ 下，目录分桶生效
2. 二次请求同一张缩略图，响应头含 ETag，带 If-None-Match 返回 304，且**不重复解码原图**
3. GET /api/original 带 Range: bytes=0-1023 → 返回 206 与正确 Content-Range / Content-Length
4. GET /api/original 无 Range → 200 完整文件
5. 生成过程中强制中断，检查磁盘上没有残留 .tmp 半文件
6. 手动删除某个缩略图后再请求 → 能自动重新生成
7. 单张 400px WebP 体积约 25KB 量级（抽样 20 张统计）
8. 全程只读 photoDir：校验 photoDir 内文件数与总字节数扫描前后完全一致

## 输出格式
1. 改动文件清单
2. 验收结果（8 条逐条给命令与实际输出）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 5 · 人脸引擎（检测 + 特征 + 质量过滤）

```text
【photo-browser · 步骤 5/12 · 人脸引擎】

## 目标
从照片中稳定抽出人脸与 512 维特征，写入 pb_face，并做人脸裁剪图。

## 前置
步骤 4 已完成（thumb 目录与文件服务可用）。

## 必须先读的项目文档
- plan/开发计划.md 第 3.2 节（识别链路）、第 3.3 节（单写入者）
- plan/数据库设计.md §4.5 pb_face 字段
- plan/MVP_plan.md 的 S3 章节
- 参考（可选，只读）：C:/Users/NINGMEI/WorkBuddy/selfDevelop/photoapp/tools/verify_accuracy.py ← S0 实测脚本，质量过滤阈值与统计口径以它为准

## 本步产出文件
1. code/src/engine/face/engine.py
   - 封装检测 + 提取，统一输出 (faceCode, bbox归一化x,y,w,h, detScore, poseYaw, posePitch, embedding)
   - insightface FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])，prepare(ctx_id=-1, det_size=(640,640))
   - **中文路径安全读图**：cv2.imread 在 Windows 读不了非 ASCII 路径 → 必须 np.fromfile + cv2.imdecode（这是 S0 脚本已踩过的坑）
   - embedding 存 float32[512] 小端 2048 字节，已 L2 归一化
   - 若 insightface 装不上：降级为裸 onnxruntime 加载 SCRFD + ArcFace，接口保持一致
2. code/src/engine/face/pool.py
   - ProcessPoolExecutor 封装
   - **子进程绝对不能连数据库**：只接收 (图片路径, ...) 返回特征数组，结果经 Queue 回主进程
   - 模型在子进程内懒加载一次（不要每张都重新加载）
3. code/src/engine/face/faceStore.py
   - 写 pb_face：faceCode 幂等键、photoCode、bbox、detScore、poseYaw、posePitch、quality、embedding(BLOB)、shotBucket、isConfirmed=0
   - 同步更新 pb_photo.faceCount 与 scanState（0→1）
   - 裁剪图落 thumbDir\faces\<xx>\<faceCode>.jpg（复用步骤 4 的 faceCropper + write_atomic）
4. code/src/tools/backtest_s0.py
   - 用 S0 的 100 张验证集（10 人 × 10 张，目录名 = 人名）跑本引擎，输出准确率/耗时统计
   - --root 参数指定验证集目录（不写死路径）

## 质量过滤（提取阶段做掉，丢弃不入库）
- detScore < 0.6
- 人脸框短边 < 64 px
- |poseYaw| > 45°
- 一张图内取面积最大的人脸为主脸，其余按 detScore 降序保留（全部存库）

## 硬约束
- **单写入者**：特征提取在子进程，写库只在主进程单线程批量提交（否则 database is locked）
- onnxruntime 必须 CPU 版，禁止 onnxruntime-gpu
- photoDir 绝对只读
- embedding 必须 float32[512] 小端，落 MEDIUMBLOB
- 单张处理耗时需统计（验收 <1s CPU）
- 本步不做分桶匹配（步骤 6）

## 验收清单
1. backtest_s0.py 在 100 张验证集上跑通，识别准确率与 S0 结果一致（±2%）
2. 单张平均耗时 <1s（CPU），输出 p50/p95
3. 质量过滤生效：构造小脸(<64px)、大角度侧脸、低分检测样本，确认被丢弃且不入库
4. pb_face 落库正确，embedding 长度 = 2048 字节
5. pb_photo.faceCount 与 pb_face 实际条数一致，scanState 由 0 变 1
6. 人脸裁剪图落在 thumbDir\faces\<xx>\ 下且肉眼位置正确
7. 代码走查确认：子进程内没有任何数据库连接/导入
8. 中文路径照片能正常处理（用中文文件名 + 中文目录各测一张）

## 输出格式
1. 改动文件清单
2. 验收结果（8 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 6 · 分桶 + 质心 + 匹配决策

```text
【photo-browser · 步骤 6/12 · 分桶 + 质心 + 匹配决策】

## 目标
把「这张脸是谁」变成可解释的三段式判定：自动归属 / 待人工确认 / 进聚类。

## 前置
步骤 5 已完成（pb_face 有数据且 embedding 可读）。

## 必须先读的项目文档
- plan/开发计划.md 第 3.2 节（识别链路）
- plan/数据库设计.md §4.6 pb_person_centroid、§六 D-3/D-5
- plan/MVP_plan.md S3 的分桶规则与匹配决策

## 本步产出文件
1. code/src/engine/match/bucket.py
   - bucket_key_adaptive(shot_year, birth_year=None) -> str
     age = shot_year - birth_year
     age <= 18  → 从 birth_year 起每 3 年一桶   （0–18 岁 3 年一桶）
     age >  18  → 从 birth_year+18 起每 10 年一桶（18+ 10 年一桶）
   - birth_year 未知 → 降级 bucket_key_equal(shot_year)：等宽 5 年
   - shot_year 未知 → 返回空/NULL，不参与跨桶比对（截图类）
2. code/src/engine/match/centroid.py
   - 按 (personCode, bucketKey) 归一化均值向量；sampleCount < 3 的桶不启用
   - recompute(personCode, bucketKey)：人工确认后**立即**重算，不做全量重跑
   - load_all_centroids() -> (matrix, index)：启动/请求时全量加载，3 万 × 512 float32 ≈ 60MB
3. code/src/engine/match/matcher.py
   - match(face)：
     候选桶 = [B0-1, B0, B0+1] 中所有有样本的桶
     score = **max**(cosine(face, centroid[bucket]))   ← 取 max，不取 mean
     score >= T_HIGH → 自动归属
     T_LOW <= score < T_HIGH → 待人工确认，记 Top-5 候选
     score < T_LOW → 未知人脸，进聚类（步骤 7）
   - 全量人脸一次性比对用 numpy 矩阵乘（暴力搜索，不引 ANN 索引）
   - Top-5 候选排序稳定（同分按 displayName）
4. code/src/processor/review/assigner.py
   - assign(faceCode, personCode, source) → 写 pb_face.personCode / isConfirmed，写 pb_photo_person（linkKey = photoCode:personCode 幂等）
   - 确认后立即调用 centroid.recompute
   - confirm_person(personCode, faceCodes[]) 批量确认
5. code/src/processor/review/merger.py
   - merge(fromPerson, toPerson)：迁移 pb_face.personCode、pb_person_centroid、pb_photo_person、avatarFaceCode
   - split(faceCode, newPersonCode 或置为未归属)
6. code/src/test/test_bucket.py / test_matcher.py：桶边界单测必须覆盖 0/3/17/18/19/70 岁、跨年、shotYear=NULL

## 硬约束
- 相邻三桶取 **max**，不是 mean
- 每桶 sampleCount >= 3 才启用
- 向量 BLOB 为 float32[512] 小端
- 不引入 FAISS / sqlite-vec 等向量库（3 万规模暴力比对 <10ms）
- 阈值写进 config/basicSettings.py，可切换「保守/激进」两套
- 匹配只写库不做 IO，不写 photoDir

## 验收清单
1. 桶边界单测全部通过（列出实际测试用例与结果）
2. 相邻三桶 max 生效：构造一个在相邻桶分数更高的人脸，确认取到较大值
3. sampleCount < 3 的桶不参与匹配
4. 三段式决策可复现：同一批人脸跑两次结果完全一致
5. 切换「保守/激进」阈值，结果按预期变化且可回退
6. 全量 3 万人脸 load_all_centroids 后内存占用 <100MB
7. assign 后该 person 的 pb_person_centroid 立即更新（sampleCount 变化可见）
8. merge/split 后 pb_photo_person 与 pb_face 一致，无孤儿记录
9. 用 S0 验证集做回归：分桶后的 FR 明显低于不分桶

## 输出格式
1. 改动文件清单
2. 验收结果（9 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 7 · 聚类与待确认数据

```text
【photo-browser · 步骤 7/12 · 聚类与待确认数据】

## 目标
把识别不出来的脸自动聚成「未命名人物」候选，并让待确认队列的数据口径与扫描进度一致。

## 前置
步骤 6 已完成（matcher 能输出「未知人脸」集合）。

## 必须先读的项目文档
- plan/开发计划.md 第 3.2 节
- plan/数据库设计.md §4.5 pb_face.clusterCode、§四 D-4（不建独立待确认表）

## 本步产出文件
1. code/src/engine/cluster/dbscan.py
   - 纯 numpy 实现 DBSCAN（余弦距离 = 1 - cos），可选 sklearn 作为加速后端（若装则用 sklearn，不装则用自实现）
   - 参数默认 eps=0.45、min_samples=3（集中放 basicSettings.py）
   - **只对未归类人脸集合聚类**（personCode IS NULL）
   - clusterCode 幂等：同一簇重跑应得到相同编码（编码规则：cluster_<hash>，hash 基于簇内 faceCode 排序后计算）
   - 代表样本：每簇取 detScore 最高的人脸作为簇代表，便于前端展示
2. 待确认队列查询（写入生成层或 processor）
   - 待确认定义：personCode IS NULL OR isConfirmed=0
   - 每个待确认人脸返回 Top-N 候选人物（复用步骤 6 的 matcher，候选含头像 faceCode、displayName、similarity）
   - 统计条数，供与 pb_scan_job.pendingCount 交叉校验
3. code/src/tools/cluster_cli.py：--eps 0.45 --min-samples 3 [--dry-run]
4. code/src/test/test_dbscan.py：合成数据验证聚类正确性、clusterCode 幂等性

## 硬约束
- 不新建待确认表（用 personCode 可空表达，数据库设计 D-4）
- 不新建 pb_face_cluster 表（用 clusterCode，Q-3 建议暂不建）
- 已归类人脸绝不能被重新聚类
- 聚类只读 embedding，不写 photoDir
- eps / min_samples 必须可配置

## 验收清单
1. 对未归类集合聚类，簇数量合理；输出每簇代表样本的 contact sheet（临时拼图即可）供抽查
2. 肉眼抽查 3 个簇，确认簇内确实是同一个人
3. 重跑聚类，clusterCode 完全稳定（不产生新编码）
4. 已归类人脸（personCode 非空）数量在聚类前后不变
5. 待确认队列条数 == pb_scan_job.pendingCount
6. 单测通过；3 万规模聚类耗时可接受（给出实际秒数）

## 输出格式
1. 改动文件清单
2. 验收结果（6 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 8 · 联系人导入（CSV / vCard）

```text
【photo-browser · 步骤 8/12 · 联系人导入（CSV / vCard）】

## 目标
把通讯录变成「可被识别归属」的已知人员档案。本步只建档案，不关联照片。

## 前置
步骤 2 已完成（sqliteCommon 可用）。本步与 3–7 无强耦合，可独立完成。

## 必须先读的项目文档
- plan/数据库设计.md §4.1 pb_family、§4.2 pb_person、§4.3 pb_person_category
- plan/MVP_plan.md S4 章节
- plan/UI/photo-browser UI 设计.md（联系人导入的幂等与「半自动」原则）

## 本步产出文件
1. code/src/processor/contact/csv_import.py（**主力通道**）
   - 支持 Outlook 导出的 CSV（列名中英文容错：DisplayName/显示名、Categories/类别、E-Mail/Email、Mobile/电话、Birthday/生日、Surname/姓氏、Title…）
   - 编码自动嗅探（utf-8-sig / gbk / utf-8），失败明确报错
   - 幂等：优先 vCardUid；无 UID 用 displayName 匹配。命中则 UPDATE，不新增
   - Categories 列按分号/逗号分隔 → 拆成多行写 pb_person_category
   - **重复导入不得残留旧分类行**（先清该 person 的旧 category 再写新的）
   - source=1（vCard/CSV 导入）
2. code/src/processor/contact/vcard_import.py
   - vobject 解析 vCard 3.0 / 4.0
   - KIND:group 识别为家庭组 → 写 pb_family，并把成员的 familyGroupCode 指向它
   - 注意 Outlook 只认单联系人 .vcf（支持批量文件目录）
3. code/src/main/cli.py
   - 命令：import-csv --file <path>；import-vcard --path <dir|file>；list-families
   - 导入原件归档复制到 dbDir\imports\（带时间戳，保留原文件不改）
   - 导入结果摘要：新增 N / 更新 M / 分类行 K / 家庭组 J
4. code/src/test/test_csv_import.py：幂等（重复导入 0 新增）、分类拆分、家庭组提示

## 硬约束
- **只建档案，绝不关联照片**（照片归属由人脸识别负责）
- FamilyName 相同 → **提示**用户是否合并家庭组，**不自动建**（半自动原则）
- 不做 Microsoft Graph API（P2，本步不做）
- 不写 photoDir
- 导入不阻塞、不做长事务（分批提交）

## 验收清单
1. 导入 200 人 CSV：姓名/姓氏/关系/生日/邮箱/电话/分类全部正确落库
2. 重复导入**同一文件**：新增 0 条，已存在记录被更新，pb_person_category 无残留旧行
3. 按 category='family' 查询结果正确
4. vCard 4.0 的 KIND:group 能识别为 pb_family，并把成员挂上 familyGroupCode
5. 相同 FamilyName 的记录只给「建议合并」提示，不自动建家庭组
6. GBK 编码的 CSV 能正常导入（不乱码、不报错）
7. 导入原件已归档到 dbDir\imports\
8. 单测通过

## 输出格式
1. 改动文件清单
2. 验收结果（8 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 9 · 后端 API 全量

```text
【photo-browser · 步骤 9/12 · 后端 API 全量】

## 目标
提供前端可直接消费的完整 HTTP 接口。

## 前置
步骤 3–8 已完成（扫描/缩略图/人脸/匹配/联系人都可用）。

## 必须先读的项目文档
- plan/照片管理方案_开源调研与自研设计.md 3.9（API 清单）
- plan/MVP_plan.md S5
- plan/UI/photo-browser UI 设计.md（页面需要哪些数据）

## 本步产出文件
1. code/src/api/dto.py：统一响应结构
   - 分页统一 { page, size, total, items }；错误统一 { code, message }
2. code/src/api/browse.py（P0）
   - GET /api/timeline：按年月分组，首屏 <500ms（3 万张）
   - GET /api/photos：分页 + 筛选（personCode / shotYear 区间 / placeName / hasFace / isDuplicate）+ 排序
   - GET /api/persons：人物列表（含 photoCount、年代跨度）
   - GET /api/persons/{personCode}：详情（含各年代桶分组统计）
   - GET /api/photos/{photoCode}：详情（含 faceCount、出现的人、EXIF 信息）
3. code/src/api/scan.py（P0）
   - POST /api/scan/start { rootPath?, batchSize? } → jobCode
   - GET /api/scan/status/{jobCode} → 真实计数（processed/added/skipped/duplicate/pending/jobStatus/lastCursor）
   - POST /api/scan/resume/{jobCode} → 继续下一批
   - POST /api/scan/stop/{jobCode}
   - GET /api/scan/jobs → 任务列表
4. code/src/api/review.py（P1）
   - GET /api/review/pending?page&size&pageToken
   - PUT /api/review/{faceCode}/assign { personCode } → 确认归属
   - POST /api/review/batch-assign { faceCodes[], personCode }
   - POST /api/review/merge { fromPersonCode, toPersonCode }
   - POST /api/review/split { faceCode, personCode? }
   - POST /api/review/ignore { faceCode }（标记陌生人）
   - GET /api/review/pending/count → 侧栏角标
5. code/src/api/contacts.py（P1）
   - POST /api/contacts/import/csv（multipart）
   - GET /api/contacts（分页 + 按 category 筛选）
   - GET /api/families、GET /api/places（供筛选与后续地图）
6. code/src/main/app.py：路由统一注册、静态资源、CORS 仅本机
7. code/src/test/：关键接口冒烟测试

## 硬约束
- 业务层**禁止裸 SQL**，全部经 sqliteCommon
- 分页统一 page/size/total；**不要**用 OFFSET 深分页（时间线用游标或按年月分段）
- 扫描是后台任务 + 内存状态 + jobId 轮询，**不得阻塞请求**
- 服务只绑 127.0.0.1
- 合并/拆分/软删等不可逆操作：服务端做参数复述所需的查询，前端负责二次确认
- 原图零风险：任何接口都不得写/删 photoDir

## 验收清单
1. uvicorn 启动后访问 /docs 能看到全部接口且可试调
2. 触发扫描 → 轮询 /api/scan/status/{jobCode} 能看到**真实计数**推进，到 batchSize 转 PAUSED
3. 3 万张照片下 GET /api/timeline 首屏响应 <500ms（给出实测毫秒数）
4. GET /api/photos 筛选 personCode / 年份区间 / hasFace 均正确
5. 待确认队列分页正确，Top-5 候选按相似度降序
6. assign 后该 person 的 photoCount 立即变化；确认后质心即时重算
7. merge 双方数据正确合并，无孤儿记录
8. 确认无接口修改 photoDir（代码走查 + 文件数/字节数前后比对）

## 输出格式
1. 改动文件清单
2. 验收结果（8 条逐条给命令与实际输出/数值）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 10 · 前端骨架 + 双主题

```text
【photo-browser · 步骤 10/12 · 前端骨架 + 双主题】

## 目标
从零搭起可运行的 Vue3 前端工程，并建立「浅色淡雅 + 跟随系统」的主题体系。本步页面只需骨架与真实文案，不接业务数据。

## 前置
步骤 9 已完成（接口可用）。本步需先核实本机 Node.js / npm 版本。

## 必须先读的项目文档
- plan/开发计划.md「设计方向 / 主题策略 / 页面与布局 / 交互与动效 / 响应式与无障碍」五节
- plan/UI/photo-browser UI 设计.md（信息架构、页面清单、组件规范、状态机、ASCII 布局）

## 技术栈（固定，不要替换）
Vue 3 + Vite + Pinia + Vue Router + Tailwind CSS + Element Plus + axios
- Tailwind CSS 3.4.17（不要用 v4，配置方式不同）
- 图标用 lucide-vue-next
- 构建产物 dist/ 不入库

## 本步产出文件（code/webserver/ 下）
1. 工程配置：package.json、vite.config.js（含 server.allowedHosts）、tailwind.config.js、postcss.config.js、index.html、.env.development（VITE_API_BASE=http://127.0.0.1:8000）
2. src/main.js、src/App.vue
3. src/styles/tokens.css —— **双套 CSS 变量**
   浅色（默认）：底 #F7F8FA / 卡片 #FFFFFF / 边框 #E8EBF0 / 主色 #5B8DEF / 次主色 #4A76D8 / 浅主色 #EAF1FE
                文本 #1F2430 / #5A6274 / #8A93A6；功能色 成功 #2E9E6B、警告 #E8A33D、危险 #E05A5A、信息 #5B8DEF、点缀 #F4A7B9
   深色（@media prefers-color-scheme: dark）：底 #14161A / 卡片 #1E2128 / 边框 #2B2F38，语义色用同色系微调版
   字体 Inter + PingFang SC；标题 24px/600，副标题 16px/600，正文 14px/400
   圆角：卡片 12 / 按钮 8 / 缩略图 8；间距 4px 基准；阴影极轻
4. src/styles/element-theme.css —— Element Plus 在 html.dark 下的 CSS 变量覆盖
5. src/styles/main.css —— Tailwind 三层 + 基础样式
6. src/utils/theme.js —— 三态切换（浅色 / 深色 / 跟随系统），写 localStorage，首屏读取避免闪白
7. src/router/index.js —— 8 个路由：/ /photos /photos/:photoCode /people /people/:personCode /review /scan-jobs /settings
8. src/api/request.js（axios 封装：baseURL、统一错误提示、拦截器）+ src/api/{scan,browse,review,contacts,static}.js（薄封装）
9. src/store/{photos,persons,review,scan,settings}.js（Pinia，先建结构与状态，不接全部接口）
10. src/components/layout/{AppSidebar,AppTopbar}.vue
    - Sidebar 220px，6 项：概览 / 照片流 / 人物库 / 待确认（带角标①）/ 扫描任务 / 设置；<1024 收窄为图标
    - Topbar 56px：面包屑 + 页面标题 + 搜索 + 待确认角标 + **主题三态切换开关**
11. src/views/{Overview,Photos,PhotoDetail,People,PersonDetail,Review,ScanJobs,Settings}View.vue —— 8 个页面骨架
    - **写真实中文文案，禁止 Lorem/占位文案**
    - 每页含该页的关键区块占位（统计卡、筛选条、网格、候选列表、任务表格、参数表单等），并接上真实布局结构

## 硬约束
- **不要暗色单一主题**（原 UI 稿已推翻）：默认浅色，跟随系统，另提供手动开关
- Element Plus 必须在两套主题下都可读，不能出现隐形文字
- 照片呈现区不叠加任何滤镜（此步无真实照片，但布局要为「照片优先」留位：大留白、低饱和界面）
- 所有交互元素用语义标签（button / input），不用 div 模拟
- 单文件不超过 300 行；样式统一用 Tailwind 类
- 本步不接业务接口数据（骨架 + 文案即可），数据接入在步骤 11、12

## 验收清单
1. 先报 Node.js 与 npm 版本（本机此前未核实）
2. npm install 成功；npm run dev 起得来；npm run build 成功
3. 系统切换深色 → 页面自动变深色，无需刷新
4. 手动三态切换（浅/深/跟随）都生效，刷新后仍保持
5. Element Plus 的 Button/Table/Input/Dialog/Tabs/Drawer/Toast/Pagination 在浅色与深色下均可读（逐个目视检查）
6. 8 个路由都能跳转，刷新不 404
7. <1024 时 Sidebar 收窄为图标；<768 时布局可用
8. 全流程键盘可达，焦点可见
9. 语义色（成功/警告/危险/信息）在两套主题下对比度均达 WCAG AA

## 输出格式
1. 改动文件清单
2. 验收结果（9 条逐条给命令/截图说明/实际输出）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 11 · 照片流 + 照片详情 + 待确认队列

```text
【photo-browser · 步骤 11/12 · 照片流 + 照片详情 + 待确认队列】

## 目标
打通主链路：看照片 → 看人脸 → 确认归属。这是全项目最关键的一步。

## 前置
步骤 10 已完成（前端骨架与主题就绪）。

## 必须先读的项目文档
- plan/UI/photo-browser UI 设计.md 第 4.3/4.4/4.6 节（三个页面的 ASCII 布局）、第 1.2 节设计原则、第 6.2 节交互流程
- plan/开发计划.md 步骤 11 行的全部验收项

## 本步产出文件
1. src/views/PhotosView.vue
   - 筛选条：人物 / 年份区间 / 地点 / 仅含人脸 / 重复 / 排序
   - 网格视图（默认）+ 时间轴视图（一键切换）
   - 网格只加载缩略图（/api/thumb），**点击才取原图**（/api/original）
   - 角标：👤n（含 n 张脸）/ ⚠重复 / 📷有 GPS
   - 滚动懒加载 + 骨架占位；分页信息（显示 1-60 条，共 N 条）
2. src/components/photo/PhotoThumb.vue
   - 1:1 或 4:3，圆角 8；hover 轻微上浮（2px / 120ms ease-out），角标淡入
3. src/views/PhotoDetailView.vue
   - 主图（支持 Range 预加载）+ 右侧信息栏：拍摄信息（时间/相机/尺寸/地点/文件 hash）
   - 「出现的人」列表可跳转人物详情
   - 操作：确认归属 / 标记重复 / 软删除（软删需 danger 二次确认）
4. src/components/photo/FaceBox.vue
   - 绝对定位框 + 2px 描边；**已归属绿 / 待确认橙**
   - 悬停显示人名 + 相似度
   - **颜色 + 图标 + 文字三重编码**（灰度/色盲下仍可辨）
5. src/components/photo/BucketTimeline.vue
   - 按年代桶分组的横向缩略图带；桶数自适应，空桶不显示
6. src/views/ReviewView.vue（**待确认队列，核心页**）
   - 顶部：剩余 N / 总数 M、跳过、忽略此人脸
   - 左侧未知人脸大图；右侧候选人物按相似度降序（头像 + 姓名 + 相似度 + 确认按钮）
   - 灰区条目橙色提示；候选旁可显示关系提示（如「兄妹，长相接近」，取自 relation 字段）
   - 底部：无匹配 → [新建人物] [标记为陌生人]
   - **键盘快捷键**：1/2/3 选候选、N 新建、S 跳过、I 忽略
   - **批量确认**：同一聚类簇下所有人脸一次全确认
   - 「合并」/「拆分」入口在手边，不藏三级菜单
7. src/components/review/CandidateRow.vue、src/components/review/ConfirmMerge.vue
   - ConfirmMerge：**复述双方姓名 + 照片数**，动词按钮「确认合并」，二次确认
8. 对应 store 与 api 接入（photos / review）

## 硬约束
- **网格里绝不加载原图**
- 不确定就要问：灰区（`T_LOW`–`T_HIGH`）一律进队列，**不静默归属**
- 人脸框必须永远可见
- 不可逆操作（合并/软删）必须参数复述 + 二次确认
- 原图零风险：UI 不提供任何编辑/覆盖/删除原图的入口
- 状态识别不得单靠颜色

## 验收清单
1. 3 万张照片滚动不卡（说明用了分页还是虚拟滚动，给出实测帧率或耗时）
2. 打开照片流时，Network 面板**只看到 /api/thumb**，没有 /api/original
3. 点击缩略图 → 进入详情 → 才发起 /api/original 请求；拖动滚动原图流畅（Range 生效）
4. 人脸框在已归属/待确认两种状态下颜色、图标、文字都正确且可辨
5. 确认一张人脸归属后：该人照片数立即更新，队列自动前进到下一条
6. 批量确认 50 张同类人脸 ≤3 次点击
7. 键盘 1/2/3/N/S/I 全部生效（逐个测）
8. 合并弹窗正确复述双方姓名与照片数，取消无副作用
9. 网格 hover 微交互、懒加载骨架、角标淡入均正常
10. 浅色与深色两套主题下本页全部可读

## 输出格式
1. 改动文件清单
2. 验收结果（10 条逐条说明实测情况）
3. 遗留问题与需要我决策的点
```

---
---

# 步骤 12 · 人物库/详情 + 扫描台 + 设置 + 打磨

```text
【photo-browser · 步骤 12/12 · 人物库/详情 + 扫描台 + 设置 + 打磨】

## 目标
补齐剩余 4 个页面、备份脚本与收尾，完成里程碑 M4。

## 前置
步骤 11 已完成（主链路可用）。

## 必须先读的项目文档
- plan/UI/photo-browser UI 设计.md 第 4.2/4.5/4.7 节（概览、人物详情、扫描任务布局）、第 3.2 节页面树
- plan/开发计划.md 步骤 12 行验收项、第八节里程碑

## 本步产出文件
1. src/views/PeopleView.vue（人物库）
   - 人物卡片网格：头像 + 姓名 + 照片数 + 年代跨度
   - 筛选：分类（family/friend/colleague 芯片）/ 家庭组
   - 空状态引导（先导入联系人再扫描）
2. src/components/common/PersonCard.vue —— 圆形头像，hover 上浮，点击进详情
3. src/views/PersonDetailView.vue（人物详情）
   - 头部：头像 + 姓名 + 家庭关系 + 分类 + 照片数
   - Tab1 时间轴：**按年代桶分组**的该人照片（复用 BucketTimeline）
   - Tab2 人脸样本：该人所有 face，可移除错样本
   - 操作：合并到… / 改头像 / 编辑资料
4. src/views/ScanJobsView.vue（扫描任务）
   - 新建扫描（选根目录 + 批大小，默认 100）
   - 任务列表：状态 / 本批进度（真实计数）/ 累计 / 操作
   - 状态色 + 图标 + 文字三重编码：RUNNING 蓝◐、PAUSED 橙⏸、DONE 绿✓、FAILED 红✕
   - 操作：开始 / **继续下一批** / 暂停 / 查看错误（展开原始 errMsg）
   - **显式展示「已暂停等待指示」**，不要假进度条
5. src/views/SettingsView.vue（设置）
   - 照片根目录（只读展示）
   - 识别参数：T_high / T_low / 分桶策略（改动提示「需重新生成质心」）
   - 数据：重新生成质心 / 备份 / 恢复 / 关于
6. src/views/OverviewView.vue 补全（统计卡 + 最近入库缩略图行 + 扫描状态条 + 待确认入口）
7. 后端补齐（后端为主、前端为壳）
   - GET /api/places、GET /api/map（Leaflet + 离线瓦片，可选功能）
   - 重复照片对比接口
   - 备份脚本 code/src/tools/backup.py：停服务 → 拷贝 db\ + thumb\ → 输出备份路径；另提供 restore
8. 重复照片视图（可选，若时间允许）

## 硬约束
- 人物时间轴必须**按年代桶分组**（S0 结论：分桶是刚需）
- 扫描台进度必须是真实计数，禁止假进度条
- 改阈值必须提示需重算质心，并提供触发按钮
- 备份 = 停服务 → 拷贝 `db\` + `thumb\`；**不要备份 photoDir**（原图不动）
- 不提供任何编辑/覆盖/删除原图的入口
- 移动端仅浏览 + 轻操作；扫描/合并/删除引导到桌面端

## 验收清单
1. 人物库卡片显示头像/姓名/照片数/年代跨度，分类与家庭组筛选正确
2. 人物详情时间轴按年代桶分组且空桶不显示
3. Tab2 可移除错样本，移除后该脸回到待确认队列
4. 扫描任务：新建 → 运行 → 到 100 张自动 PAUSED → 点「继续下一批」→ 断点续扫 → DONE，全程计数正确
5. FAILED 任务能展开查看原始错误
6. 设置页改 T_high/T_low 有「需重算质心」提示，且能触发重算
7. 备份脚本产出完整备份（db + thumb），恢复后应用正常
8. 8 个页面在浅色/深色下全部可读，<1024 与 <768 断点正常
9. 全项目回归：主链路（扫描 → 浏览 → 确认 → 人物时间轴）端到端走通
10. 输出 M4 自测清单：接下来一周你要用哪些功能、怎么记录问题

## 输出格式
1. 改动文件清单
2. 验收结果（10 条逐条说明实测情况）
3. 遗留问题与需要我决策的点
4. M4 一周自测建议清单
```

---
---

## 附录 A · 步骤与里程碑对照

| 步 | 主题 | 里程碑 |
| --- | --- | --- |
| 1 | 工程基线与配置骨架 | — |
| 2 | SQLite 运行层 + 生成器 + 建库 | — |
| 3 | 扫描器 | **M1**（扫描幂等、去重准确、续扫可用） |
| 4 | 缩略图与原图文件服务 | — |
| 5 | 人脸引擎 | — |
| 6 | 分桶 + 质心 + 匹配 | — |
| 7 | 聚类与待确认数据 | **M2**（识别复现 S0、确认闭环生效） |
| 8 | 联系人导入 | — |
| 9 | 后端 API 全量 | **M3**（接口全可用、扫描可后台跑） |
| 10 | 前端骨架 + 双主题 | — |
| 11 | 照片流 + 详情 + 待确认队列 | — |
| 12 | 人物库/详情 + 扫描台 + 设置 + 打磨 | **M4**（自己真正用一周） |

## 附录 B · 每步固定的输出格式

```
1. 改动文件清单（新增 / 修改，逐个列路径，标注新增还是修改）
2. 验收结果（该步验收清单逐条：执行的命令 + 实际输出或结论 + 数值）
3. 遗留问题与需要我决策的点（如果有拿不准的需求，不要自行假设，列出来问我）
```

> 提醒：新对话里请**只粘贴当前步骤的提示语**，不要把 12 步一起粘过去，否则上下文会过长导致遗漏约束。
