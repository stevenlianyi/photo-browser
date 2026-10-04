#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_thumb_maker.py
#Description: 步骤 4 单测 —— 缩略图生成 / 人脸裁剪 / 图片文件服务
#
# 覆盖的验收点
# ------------
#   验收 2：二次请求同一张缩略图**不重复解码原图**（COUNTERS['decoded'] 不涨）
#   验收 3：Range -> 206 + Content-Range / Content-Length
#   验收 4：无 Range -> 200 完整文件
#   验收 6：手动删掉缩略图后再请求能自动重建
#   验收 7：400px WebP 体积在25KB 量级
#   以及 EXIF 方向纠正、原图只读、bbox 越界夹紧、原子写
#
# 为什么不用 TestClient
# --------------------
#   starlette.testclient 依赖 httpx，本项目没装（也不打算为此装）。
#   三个端点都是**普通函数**（只有 request 依赖），直接构造一个
#   迷你 request 桩来调用，既不引入依赖，也更能说明"路由层没有隐藏状态"。

import os
import zlib

import pytest

from common import miscCommon as misc
from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from processor.media import faceCropper as faceCropper
from processor.media import thumbMaker as thumbMaker
from processor.media import thumbStore as thumbStore
from api import static as staticApi


def _readFieldNames(table: str) -> set:
    """读 pb_<table>.txt 里的列名集合（去掉注释与行尾逗号）"""
    here = os.path.dirname(os.path.abspath(__file__))
    name = table if table.startswith("pb_") else "pb_%s" % table
    path = os.path.join(here, os.pardir, "database", "%s.txt" % name)
    names = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip().rstrip(",")
            if not line:
                continue
            names.add(line.split()[0])
    return names



# ============================================================
# 装置
# ============================================================

class Req(object):
    """极简 request 桩：端点只用到 .headers 与 .method"""

    def __init__(self, headers=None, method="GET"):
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.method = method


@pytest.fixture
def photo_fixture(scan_root, make_photo):
    """造 3 张真实图片并返回 (photoRoot, [row, ...])。

    刻意造不同尺寸与不同内容：内容不同 -> fileHash 不同 -> 缩略图路径不同；
    尺寸不同 -> 才能验"按宽度缩放 + 保持宽高比"。
    """
    specs = [("2024/2024-05-01/IMG_0001.jpg", 1600, 1200),
             ("2024/2024-05-01/IMG_0002.jpg", 1200, 1600),
             ("2023/family/phone/IMG_0003.png", 900, 900)]
    rows = []
    for relPath, width, height in specs:
        absPath = os.path.join(str(scan_root), *relPath.split("/"))
        make_photo(absPath, width=width, height=height)
        fileHash = misc.fileHashHex(absPath)
        rows.append({"photoCode": "PH_" + fileHash[:12], "relPath": relPath,
                     "relPathHash": fileHash, "fileHash": fileHash,
                     "fileSize": os.path.getsize(absPath),
                     "mimeType": None, "delFlag": "0"})
    return str(scan_root), rows


def _counters():
    return thumbMaker.countersSnapshot()


# ============================================================
# 一、单张生成
# ============================================================

class TestMakeThumbBytes:
    def test_width_and_ratio(self, photo_fixture):
        """按**宽度**缩放到目标宽，保持宽高比"""
        from PIL import Image
        from io import BytesIO
        photoRoot, rows = photo_fixture
        data = thumbMaker.make_thumb_bytes(
            os.path.join(photoRoot, *rows[0]["relPath"].split("/")), 400)
        image = Image.open(BytesIO(data))
        assert image.format == "WEBP"
        assert image.size[0] == 400
        assert abs(image.size[1] - 300) <= 1        # 1600x1200 -> 400x300

    def test_portrait_source(self, photo_fixture):
        from PIL import Image
        from io import BytesIO
        photoRoot, rows = photo_fixture
        data = thumbMaker.make_thumb_bytes(
            os.path.join(photoRoot, *rows[1]["relPath"].split("/")), 400)
        image = Image.open(BytesIO(data))
        assert image.size[0] == 400
        assert image.size[1] > image.size[0]        # 竖图仍然是竖的

    @pytest.mark.parametrize("size", (200, 400, 800))
    def test_all_sizes(self, photo_fixture, size):
        from PIL import Image
        from io import BytesIO
        photoRoot, rows = photo_fixture
        data = thumbMaker.make_thumb_bytes(
            os.path.join(photoRoot, *rows[0]["relPath"].split("/")), size)
        image = Image.open(BytesIO(data))
        assert image.size[0] == size

    def test_reject_unknown_size(self, photo_fixture):
        photoRoot, rows = photo_fixture
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbMaker.make_thumb_bytes(
                os.path.join(photoRoot, *rows[0]["relPath"].split("/")), 300)

    def test_missing_file_raises(self, scan_root):
        with pytest.raises(OSError):
            thumbMaker.make_thumb_bytes(os.path.join(str(scan_root), "nope.jpg"), 400)

    def test_never_upscales(self, scan_root, make_photo):
        """原图比缩略图小就**不放大** —— 硬拉只会得到一张糊图还白占磁盘"""
        from PIL import Image
        from io import BytesIO
        small = os.path.join(str(scan_root), "small.jpg")
        make_photo(small, width=120, height=90)
        data = thumbMaker.make_thumb_bytes(small, 400)
        assert Image.open(BytesIO(data)).size == (120, 90)

    def test_exif_orientation_applied(self, scan_root, make_photo):
        """EXIF orientation=6（横拍存成竖）必须在**内存副本**上纠正

        不纠正的话缩略图会躺倒；更重要的是纠正绝不能回写原图。
        """
        from PIL import Image
        from io import BytesIO
        absPath = os.path.join(str(scan_root), "rot.jpg")
        make_photo(absPath, width=1000, height=500, orientation=6)
        before = misc.fileHashHex(absPath)
        data = thumbMaker.make_thumb_bytes(absPath, 400)
        image = Image.open(BytesIO(data))
        # 1000x500 + orientation6 -> 显示为 500x1000 -> 缩略图 400x800（高>宽）
        assert image.size[1] > image.size[0], "EXIF 方向未被纠正: %s" % (image.size,)
        assert misc.fileHashHex(absPath) == before, "**原图被改动了**"

    def test_png_with_alpha(self, scan_root, make_photo):
        """带 alpha 的 PNG 也必须能出 WebP（P 模式/带 alpha 直接存 WEBP 会报错）"""
        from io import BytesIO
        from PIL import Image
        absPath = os.path.join(str(scan_root), "alpha.png")
        Image.new("RGBA", (600, 400), (10, 20, 30, 128)).save(absPath, "PNG")
        data = thumbMaker.make_thumb_bytes(absPath, 400)
        assert Image.open(BytesIO(data)).size == (400, 267)

    def test_corrupt_file_raises(self, scan_root):
        broken = os.path.join(str(scan_root), "broken.jpg")
        with open(broken, "wb") as handle:
            handle.write(b"this is definitely not a jpeg")
        with pytest.raises(Exception):
            thumbMaker.make_thumb_bytes(broken, 400)


# ============================================================
# 二、落盘 / 缓存命中
# ============================================================

class TestMakeThumb:
    def test_creates_at_derived_path(self, photo_fixture):
        photoRoot, rows = photo_fixture
        row = rows[0]
        result = thumbMaker.make_thumb(row, 400, photoRoot=photoRoot)
        assert result["ok"] is True and result["created"] is True
        assert result["relpath"] == thumbStore.thumb_relpath(row["fileHash"], 400)
        assert os.path.isfile(result["absPath"])
        assert result["bytes"] == os.path.getsize(result["absPath"])

    def test_second_call_is_cache_hit_without_decode(self, photo_fixture):
        """**验收 2**：二次请求不能重复解码原图"""
        photoRoot, rows = photo_fixture
        thumbMaker.make_thumb(rows[0], 400, photoRoot=photoRoot)
        before = _counters()["decoded"]
        stamp = os.path.getmtime(thumbStore.thumb_abspath(rows[0]["fileHash"], 400))

        result = thumbMaker.make_thumb(rows[0], 400, photoRoot=photoRoot)
        assert result["ok"] is True
        assert result["created"] is False and result["cached"] is True
        assert _counters()["decoded"] == before, "重复解码了原图"
        assert os.path.getmtime(thumbStore.thumb_abspath(rows[0]["fileHash"], 400)) == stamp

    def test_force_rebuild(self, photo_fixture):
        photoRoot, rows = photo_fixture
        thumbMaker.make_thumb(rows[0], 400, photoRoot=photoRoot)
        before = _counters()["decoded"]
        result = thumbMaker.make_thumb(rows[0], 400, photoRoot=photoRoot, force=True)
        assert result["created"] is True
        assert _counters()["decoded"] == before + 1

    def test_rebuild_after_manual_delete(self, photo_fixture):
        """**验收 6**：手动删掉缩略图后再请求 -> 自动重新生成"""
        photoRoot, rows = photo_fixture
        first = thumbMaker.make_thumb(rows[0], 400, photoRoot=photoRoot)
        os.remove(first["absPath"])
        assert not os.path.exists(first["absPath"])

        again = thumbMaker.make_thumb(rows[0], 400, photoRoot=photoRoot)
        assert again["ok"] is True and again["created"] is True
        assert os.path.isfile(again["absPath"])

    def test_missing_original_does_not_raise(self, scan_root):
        """单张失败不抛异常（外接盘随时可能把文件带走）"""
        row = {"photoCode": "PH_X", "relPath": "gone/x.jpg",
               "fileHash": "ab" + "0" * 62}
        result = thumbMaker.make_thumb(row, 400, photoRoot=str(scan_root))
        assert result["ok"] is False and result["errMsg"]
        assert result["bytes"] == 0

    def test_empty_row(self):
        assert thumbMaker.make_thumb(None, 400)["ok"] is False
        assert thumbMaker.make_thumb({}, 400)["ok"] is False

    def test_bad_size_does_not_raise(self, photo_fixture):
        photoRoot, rows = photo_fixture
        result = thumbMaker.make_thumb(rows[0], 333, photoRoot=photoRoot)
        assert result["ok"] is False and "size" in result["errMsg"]

    def test_dirty_relpath_escape_refused(self, scan_root):
        """relPath 逃出照片根 -> 拒绝（不写任何文件）"""
        row = {"photoCode": "PH_EVIL", "relPath": "../../evil.jpg",
               "fileHash": "cd" + "0" * 62}
        result = thumbMaker.make_thumb(row, 400, photoRoot=str(scan_root))
        assert result["ok"] is False
        assert not os.path.exists(thumbStore.thumb_abspath(row["fileHash"], 400))

    def test_original_untouched(self, photo_fixture):
        """**原图零风险**：生成前后 fileHash / 大小 / mtime 全都不变"""
        photoRoot, rows = photo_fixture
        absOrig = os.path.join(photoRoot, *rows[0]["relPath"].split("/"))
        before = (misc.fileHashHex(absOrig), os.path.getsize(absOrig),
                  os.path.getmtime(absOrig))
        thumbMaker.make_thumb(rows[0], 400, photoRoot=photoRoot)
        after = (misc.fileHashHex(absOrig), os.path.getsize(absOrig),
                 os.path.getmtime(absOrig))
        assert before == after

    def test_size_about_25kb(self, scan_root, make_photo):
        """**验收 7**：400px WebP 体积在 25KB 量级

        这里用 1600x1200 的高细节图（随机噪点，最接近真实照片的压缩率）；
        开发计划 §6.3 记的「400px WebP ≈ 25KB」就是这个量级。
        """
        from PIL import Image
        import random
        rnd = random.Random(20261004)
        image = Image.new("RGB", (1600, 1200))
        image.putdata([(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
                       for _ in range(1600 * 1200)])
        absPath = os.path.join(str(scan_root), "noisy.jpg")
        image.save(absPath, quality=95)
        image.close()
        data = thumbMaker.make_thumb_bytes(absPath, 400)
        assert 5 * 1024 <= len(data) <= 60 * 1024, \
            "400px WebP 体积 %d 字节，超出 25KB 量级太多" % len(data)


# ============================================================
# 三、线程池按需 / 进程池批量
# ============================================================

class TestBulkAndPool:
    def test_bulk_serial(self, photo_fixture):
        photoRoot, rows = photo_fixture
        summary = thumbMaker.make_thumbs_bulk(rows, workers=1, size=400,
                                            photoRoot=photoRoot)
        assert summary["total"] == 3
        assert summary["created"] == 3 and summary["failed"] == 0
        for row in rows:
            assert thumbStore.exists(thumbStore.thumb_abspath(row["fileHash"], 400))

    def test_bulk_is_idempotent(self, photo_fixture):
        """第二次跑全部命中缓存，一张原图都不再解码"""
        photoRoot, rows = photo_fixture
        thumbMaker.make_thumbs_bulk(rows, workers=1, size=400, photoRoot=photoRoot)
        before = _counters()["decoded"]
        summary = thumbMaker.make_thumbs_bulk(rows, workers=1, size=400, photoRoot=photoRoot)
        assert summary["cached"] == 3 and summary["created"] == 0
        assert _counters()["decoded"] == before

    def test_bulk_process_pool(self, photo_fixture):
        """**进程池**（CPU 解码密集）—— Windows spawn 下也要能跑

        刻意传一个**与 paths.thumb_dir() 不同**的 thumbRoot 来钉死一条规则：
        **子进程不得自己解析配置**。spawn 起的全新解释器读不到主进程的配置打桩，
        若让它各自去读 local_settings，两边 cwd/环境不同就会写到两个不同的
        thumb 目录 —— 主进程统计报"生成成功"，磁盘上却一张都没有
        （这个坑真踩过一次：9 张测试缩略图落到了真实的 d:/PhotoLib/thumb/）。
        """
        photoRoot, rows = photo_fixture
        altThumb = os.path.join(photoRoot, os.pardir, "alt_thumb")
        summary = thumbMaker.make_thumbs_bulk(rows, workers=2, size=200,
                                            photoRoot=photoRoot, thumbRoot=altThumb)
        assert summary["failed"] == 0
        assert summary["created"] == 3
        assert summary["workers"] == 2
        for row in rows:
            assert thumbStore.exists(thumbStore.thumb_abspath(row["fileHash"], 200,
                                                              thumbRoot=altThumb))
        # 绝不能同时写到默认 thumb 目录去
        assert not thumbStore.exists(thumbStore.thumb_abspath(rows[0]["fileHash"], 200))

    def test_bulk_empty(self):
        summary = thumbMaker.make_thumbs_bulk([], workers=2, size=400)
        assert summary["total"] == 0 and summary["workers"] == 0

    def test_bulk_counts_failures(self, scan_root, photo_fixture):
        """单张坏图不拖垮整批"""
        photoRoot, rows = photo_fixture
        rows = rows + [{"photoCode": "PH_BAD", "relPath": "nope/x.jpg",
                       "fileHash": "ef" + "0" * 62}]
        summary = thumbMaker.make_thumbs_bulk(rows, workers=1, size=400,
                                            photoRoot=photoRoot)
        assert summary["created"] == 3
        assert summary["failed"] == 1
        assert len(summary["failures"]) == 1

    def test_bulk_progress_callback(self, photo_fixture):
        photoRoot, rows = photo_fixture
        seen = []
        thumbMaker.make_thumbs_bulk(rows, workers=1, size=400, photoRoot=photoRoot,
                                   onProgress=lambda d, t, one: seen.append((d, t)))
        assert [d for d, _ in seen] == [1, 2, 3]
        assert all(t == 3 for _, t in seen)

    def test_ensure_thumb_concurrent_same_key(self, photo_fixture):
        """同键并发（条带锁）：N 条请求只解码 1 次，全部拿到结果"""
        from concurrent.futures import ThreadPoolExecutor
        photoRoot, rows = photo_fixture
        before = _counters()["decoded"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(
                lambda _i: thumbMaker.ensure_thumb(rows[0], 400, photoRoot=photoRoot),
                range(6)))
        assert all(r["ok"] for r in results)
        assert _counters()["decoded"] == before + 1, "同键并发重复解码了"

    def test_ensure_thumb_timeout_is_not_fatal(self, photo_fixture):
        """拿不到锁时返回 ok=False，**绝不抛异常**（不能让请求队列卡死）"""
        photoRoot, rows = photo_fixture
        lock = thumbMaker._stripeFor("%s|400" % rows[0]["fileHash"])
        lock.acquire()
        try:
            result = thumbMaker.ensure_thumb(rows[0], 400, photoRoot=photoRoot,
                                            timeout=0.05)
            assert result["ok"] is False
            assert "超时" in result["errMsg"]
        finally:
            lock.release()

    def test_shutdown_pool(self):
        thumbMaker.getPool()
        thumbMaker.shutdownPool()
        thumbMaker.getPool()
        thumbMaker.shutdownPool(wait=False)


# ============================================================
# 四、人脸裁剪
# ============================================================

class TestClampBbox:
    def test_normal(self):
        assert faceCropper.clampBbox((0.1, 0.2, 0.3, 0.4), 1000, 800) == (100, 160, 400, 480)

    @pytest.mark.parametrize("bbox", ((-0.2, -0.2, 1.4, 1.4), (-1.0, 0.2, 1.5, 0.5),
                                      (0.9, 0.9, 0.5, 0.5), (-0.5, 0.5, 2.0, 0.2),
                                      (0.0, 0.0, 1.0, 1.0), (0.5, -0.9, 0.2, 2.0)))
    def test_out_of_range_is_clamped_not_raised(self, bbox):
        """**越界必须夹紧**：检测模型给的框本来就常越界，
        一张脸失败导致整张照片、整批识别停摆，代价与收益完全不成比例"""
        left, top, right, bottom = faceCropper.clampBbox(bbox, 1000, 800)
        assert 0 <= left < right <= 1000
        assert 0 <= top < bottom <= 800

    def test_fully_outside_raises_not_clamped(self):
        """与画面**无交集**（不是越界）必须明确失败

        若在这里"夹紧"，会退化成画面最边缘 1 像素的竖条 ——
        看起来成功了，裁出来却是一张纯色废图。
        """
        for bbox in ((1.5, 1.5, 0.2, 0.2), (-1.0, -1.0, 0.5, 0.5),
                     (-0.9, 0.2, 0.2, 0.2), (0.2, -0.9, 0.2, 0.2)):
            with pytest.raises(ValueError, match="画面之外"):
                faceCropper.clampBbox(bbox, 1000, 800)

    @pytest.mark.parametrize("bbox", (None, (0.1, 0.2, 0.3), (0.1, 0.2, 0.3, "x", 1),
                                      (0.1, 0.2, 0.0, 0.5), (0.1, 0.2, 0.5, -1)))
    def test_unusable_bbox_raises(self, bbox):
        with pytest.raises(ValueError):
            faceCropper.clampBbox(bbox, 1000, 800)

    def test_fully_outside_raises(self):
        """整框在画面外：夹紧后为空，裁出来是废图 -> 明确失败让调用方丢弃"""
        with pytest.raises(ValueError):
            faceCropper.clampBbox((1.5, 1.5, 0.2, 0.2), 1000, 800)
    def test_too_small_after_clamp_raises(self):
        """minEdge 由调用方（步骤 5 的质量过滤）决定，这里只验参数生效"""
        with pytest.raises(ValueError):
            faceCropper.clampBbox((0.5, 0.5, 0.001, 0.001), 1000, 800,
                                  minEdge=basicSettings.MIN_FACE_EDGE)
        # 默认 minEdge=1：40x30 的小脸框是**合法**的（质量过滤交给步骤 5）
        left, top, right, bottom = faceCropper.clampBbox((0.5, 0.5, 0.02, 0.02),
                                                          2000, 1500)
        assert (right - left, bottom - top) == (40, 30)

    def test_is_plausible_bbox(self):
        assert faceCropper.isPlausibleBbox((0.1, 0.1, 0.2, 0.2)) is True
        assert faceCropper.isPlausibleBbox((0.1, 0.1, 0.0, 0.2)) is False
        assert faceCropper.isPlausibleBbox(None) is False
        assert faceCropper.isPlausibleBbox(("a", "b", "c", "d")) is False


class TestFaceCrop:
    def test_returns_jpeg_bytes(self, scan_root, make_photo):
        from io import BytesIO
        from PIL import Image
        absPath = os.path.join(str(scan_root), "face_src.jpg")
        make_photo(absPath, width=1200, height=900)
        data = faceCropper.crop_from_bbox(absPath, (0.3, 0.3, 0.2, 0.2))
        image = Image.open(BytesIO(data))
        assert image.format == "JPEG"
        assert max(image.size) == basicSettings.FACE_CROP_SIZE

    def test_out_of_range_bbox_still_crops(self, scan_root, make_photo):
        """越界 bbox 不抛异常，照样出图（夹紧后等于整幅）"""
        from io import BytesIO
        from PIL import Image
        absPath = os.path.join(str(scan_root), "face_src2.jpg")
        make_photo(absPath, width=1000, height=800)
        size = basicSettings.FACE_CROP_SIZE
        # 显式 square=False 才能看出"整幅都被裁进来了"（等比后应是 1000:800）
        ratio = faceCropper.crop_from_bbox(absPath, (-0.3, -0.3, 1.6, 1.6), square=False)
        image = Image.open(BytesIO(ratio))
        assert max(image.size) == size
        assert image.size[0] / float(image.size[1]) == pytest.approx(1000 / 800.0, abs=0.05)
        # 缺省口径是方形，整幅被塞进正方形
        assert Image.open(BytesIO(
            faceCropper.crop_from_bbox(absPath, (-0.3, -0.3, 1.6, 1.6)))).size == (size, size)

    def test_square_output(self, scan_root, make_photo):
        from io import BytesIO
        from PIL import Image
        absPath = os.path.join(str(scan_root), "face_src3.jpg")
        make_photo(absPath, width=1000, height=800)
        data = faceCropper.crop_from_bbox(absPath, (0.1, 0.1, 0.5, 0.5), square=True)
        size = basicSettings.FACE_CROP_SIZE
        assert Image.open(BytesIO(data)).size == (size, size)

    def test_square_is_the_default(self, scan_root, make_photo):
        """**不传square 也要出正方形**（缺省跟 FACE_CROP_SQUARE，当前 True）

        人员头像位按圆形遮罩渲染，方形图不必再由前端裁。
        """
        from io import BytesIO
        from PIL import Image
        absPath = os.path.join(str(scan_root), "face_default_shape.jpg")
        make_photo(absPath, width=1000, height=800)
        size = basicSettings.FACE_CROP_SIZE
        assert basicSettings.FACE_CROP_SQUARE is True, "配置被改成False 了？"
        data = faceCropper.crop_from_bbox(absPath, (0.1, 0.1, 0.5, 0.3))
        assert Image.open(BytesIO(data)).size == (size, size)
        # safeCrop / crop_to_file / make_face_thumb 三层口径必须一致
        assert faceCropper.safeCrop(absPath, (0.1, 0.1, 0.5, 0.3))[0] == data
        faceCode = "de" + "7" * 62
        rec = faceCropper.crop_to_file(absPath, (0.1, 0.1, 0.5, 0.3), faceCode)
        assert rec["ok"] is True
        with open(rec["absPath"], "rb") as f:
            assert Image.open(BytesIO(f.read())).size == (size, size)

    def test_explicit_false_still_gives_ratio(self, scan_root, make_photo):
        """显式传 False 仍然可以做等比输出（配置项不是唯一入口）"""
        from io import BytesIO
        from PIL import Image
        absPath = os.path.join(str(scan_root), "face_ratio.jpg")
        make_photo(absPath, width=1000, height=800)
        data = faceCropper.crop_from_bbox(absPath, (0.1, 0.1, 0.5, 0.3), square=False)
        size = basicSettings.FACE_CROP_SIZE
        assert max(Image.open(BytesIO(data)).size) == size
        assert Image.open(BytesIO(data)).size != (size, size)

    def test_square_default_follows_config(self, scan_root, make_photo, monkeypatch):
        """配置改成等比、又不显式传 square -> 应该出等比图"""
        from io import BytesIO
        from PIL import Image
        absPath = os.path.join(str(scan_root), "face_cfg_shape.jpg")
        make_photo(absPath, width=1000, height=800)
        monkeypatch.setattr(basicSettings, "FACE_CROP_SQUARE", False)
        data = faceCropper.crop_from_bbox(absPath, (0.1, 0.1, 0.5, 0.3))
        size = basicSettings.FACE_CROP_SIZE
        assert max(Image.open(BytesIO(data)).size) == size
        assert Image.open(BytesIO(data)).size != (size, size)

    def test_upscales_small_face(self, scan_root, make_photo):
        """远处小脸只有几十像素：**允许放大**，糊但认得出人

        质量过滤（短边 < MIN_FACE_EDGE）是**步骤 5** 的职责，裁剪层不重复过滤，
        免得同一张脸在两处各判一次、早晚出现不一致。
        """
        from io import BytesIO
        from PIL import Image
        absPath = os.path.join(str(scan_root), "face_small.jpg")
        make_photo(absPath, width=2000, height=1500)
        data = faceCropper.crop_from_bbox(absPath, (0.5, 0.5, 0.02, 0.02))
        assert max(Image.open(BytesIO(data)).size) == basicSettings.FACE_CROP_SIZE

    def test_exif_orientation_applied(self, scan_root, make_photo):
        from io import BytesIO
        from PIL import Image
        absPath = os.path.join(str(scan_root), "face_rot.jpg")
        make_photo(absPath, width=1000, height=500, orientation=6)
        size = basicSettings.FACE_CROP_SIZE
        # 1000x500 + orientation6 -> 纠正后 500x1000（竖）
        # 显式 square=False 才看得出"纠正后是竖的"（方形会把这个信息抹平）
        ratio = faceCropper.crop_from_bbox(absPath, (0.0, 0.0, 1.0, 1.0), square=False)
        image = Image.open(BytesIO(ratio))
        assert image.size[1] > image.size[0], "EXIF 方向未纠正: %s" % (image.size,)
        assert max(image.size) == size
        # 缺省方形：不管横竖一律 160x160，前端头像位不用分支
        assert Image.open(BytesIO(
            faceCropper.crop_from_bbox(absPath, (0.0, 0.0, 1.0, 1.0)))).size == (size, size)

    def test_missing_file_raises(self, scan_root):
        with pytest.raises(OSError):
            faceCropper.crop_from_bbox(os.path.join(str(scan_root), "no.jpg"),
                                       (0.1, 0.1, 0.2, 0.2))

    def test_safe_crop_never_raises(self, scan_root):
        data, err = faceCropper.safeCrop(os.path.join(str(scan_root), "no.jpg"),
                                         (0.1, 0.1, 0.2, 0.2))
        assert data is None and err

    def test_safe_crop_bad_bbox(self, scan_root, make_photo):
        absPath = os.path.join(str(scan_root), "face_src4.jpg")
        make_photo(absPath, width=600, height=600)
        data, err = faceCropper.safeCrop(absPath, (0.1, 0.1, 0.0, 0.5))
        assert data is None and "宽高" in err

    def test_parse_face_box(self):
        """标准格式 "x,y,w,h"（formatFaceBox 的产物）"""
        assert faceCropper.parseFaceBox("0.1,0.2,0.3,0.4") == (0.1, 0.2, 0.3, 0.4)
        assert faceCropper.parseFaceBox("0.1000,0.2000,0.3000,0.4000") == (0.1, 0.2, 0.3, 0.4)

    @pytest.mark.parametrize("text,expect", (
        ("0.1, 0.2, 0.3, 0.4", (0.1, 0.2, 0.3, 0.4)),      # 多余空格
        ("[0.1,0.2,0.3,0.4]", (0.1, 0.2, 0.3, 0.4)),          # JSON 数组（只读兼容）
        ("0.1;0.2;0.3;0.4", (0.1, 0.2, 0.3, 0.4)),            # 分号（手抄）
        ("0.1|0.2|0.3|0.4", (0.1, 0.2, 0.3, 0.4)),            # 竖线（手抄）
        ((0.1, 0.2, 0.3, 0.4), (0.1, 0.2, 0.3, 0.4)),         # 已经是元组
        ([0.1, 0.2, 0.3, 0.4], (0.1, 0.2, 0.3, 0.4)),
    ))
    def test_parse_face_box_tolerant(self, text, expect):
        """只读侧容忍历史/手抄写法（写入侧只认 formatFaceBox）"""
        assert faceCropper.parseFaceBox(text) == expect

    @pytest.mark.parametrize("bad", (None, "", "abc", "1,2,3", "a,b,c,d", "[1,2,3]"))
    def test_parse_face_box_bad(self, bad):
        assert faceCropper.parseFaceBox(bad) == ()


class TestFormatFaceBox:
    """写 bbox 列的**唯一**入口：格式只有一处定义，才不会写读不一致"""

    def test_basic(self):
        assert faceCropper.formatFaceBox((0.1, 0.2, 0.3, 0.4)) == "0.1000,0.2000,0.3000,0.4000"

    def test_round_trip(self):
        """format -> parse 必须回到原值（这是"写读一致"的硬保证）"""
        for raw in ((0.0, 0.0, 1.0, 1.0), (0.123456, 0.0, 0.5, 0.25),
                    (1 / 3.0, 2 / 3.0, 1 / 7.0, 1 / 9.0)):
            text = faceCropper.formatFaceBox(raw)
            got = faceCropper.parseFaceBox(text)
            assert len(got) == 4
            for a, b in zip(raw, got):
                assert abs(a - b) < 10 ** (-faceCropper.BBOX_DECIMALS)

    def test_fits_varchar64(self):
        """4 个 8 位小数 + 3 逗号= 35 字符，VARCHAR(64) 必须装得下"""
        worst = faceCropper.formatFaceBox((-1.23456789e-9, 1.0, 1.0, 1.0))
        assert len(worst) <= 64

    def test_does_not_clamp(self):
        """**越界照原样写出**：库里要留检测模型的原始框，便于事后复查识别质量"""
        assert faceCropper.formatFaceBox((-0.2, -0.1, 1.4, 1.3)) == "-0.2000,-0.1000,1.4000,1.3000"

    def test_rejects_non_4tuple(self):
        for bad in ((0.1, 0.2, 0.3), (0.1, 0.2, 0.3, 0.4, 0.5), ()):
            with pytest.raises(ValueError):
                faceCropper.formatFaceBox(bad)

    def test_column_name_matches_schema(self, scan_root, make_photo):
        """代码读的列名必须就是 pb_face.txt 里的列名（用**行为**验，不用 grep 源码）

        `make_face_thumb` 曾经读 row["faceBox"]，而列名其实是 `bbox` ——
        row.get 拿不到值却**不报错**，一路走到 isPlausibleBbox(None) 才失败，
        报错信息还指向"数据有问题"，方向完全带偏。
        这里拿 schema 里真实的列名造一行，必须能裁出图。
        """
        schema = _readFieldNames("pb_face")
        assert "bbox" in schema
        relPath = "schema_check.jpg"
        make_photo(os.path.join(str(scan_root), relPath), width=600, height=600)
        row = {name: (faceCropper.formatFaceBox((0.2, 0.2, 0.4, 0.4))
                      if name == "bbox" else "") for name in schema}
        row.update({"faceCode": "b0" + "8" * 62, "relPath": relPath})
        assert faceCropper.make_face_thumb(row)["ok"] is True


class TestFaceCropToFile:
    def test_writes_at_derived_path(self, scan_root, make_photo):
        faceCode = "f0" + "1" * 62
        absPath = os.path.join(str(scan_root), "src.jpg")
        make_photo(absPath, width=900, height=700)
        result = faceCropper.crop_to_file(absPath, (0.2, 0.2, 0.3, 0.3), faceCode)
        assert result["ok"] is True and result["created"] is True
        assert result["relpath"] == "faces/f0/%s.jpg" % faceCode
        assert result["absPath"].endswith(os.path.join("faces", "f0", "%s.jpg" % faceCode))
        assert os.path.isfile(result["absPath"])
        assert thumbStore.findTmpFiles() == []

    def test_cached_second_time(self, scan_root, make_photo):
        faceCode = "f1" + "2" * 62
        absPath = os.path.join(str(scan_root), "src2.jpg")
        make_photo(absPath, width=900, height=700)
        faceCropper.crop_to_file(absPath, (0.2, 0.2, 0.3, 0.3), faceCode)
        again = faceCropper.crop_to_file(absPath, (0.2, 0.2, 0.3, 0.3), faceCode)
        assert again["cached"] is True and again["created"] is False

    def test_bad_face_code_refused(self, scan_root, make_photo):
        absPath = os.path.join(str(scan_root), "src3.jpg")
        make_photo(absPath, width=400, height=400)
        result = faceCropper.crop_to_file(absPath, (0.1, 0.1, 0.2, 0.2), "../evil")
        assert result["ok"] is False and result["absPath"] == ""

    def test_make_face_thumb_from_row(self, scan_root, make_photo):
        """行内键用**真实列名** bbox"""
        relPath = "faces_src/row.jpg"
        absPath = os.path.join(str(scan_root), *relPath.split("/"))
        make_photo(absPath, width=800, height=600)
        faceCode = "ab" + "3" * 62
        row = {"faceCode": faceCode, "relPath": relPath,
               "bbox": faceCropper.formatFaceBox((0.2, 0.2, 0.3, 0.3))}
        result = faceCropper.make_face_thumb(row)
        assert result["ok"] is True
        assert result["absPath"] == thumbStore.face_abspath(faceCode)

    def test_make_face_thumb_missing_bbox_key(self, scan_root, make_photo):
        """行里根本没有 bbox 值 -> 明确失败并点名列名（别让人去查数据质量）"""
        absPath = os.path.join(str(scan_root), "nobbox.jpg")
        make_photo(absPath, width=400, height=400)
        row = {"faceCode": "ef" + "5" * 62, "relPath": "nobbox.jpg"}
        result = faceCropper.make_face_thumb(row)
        assert result["ok"] is False
        assert "bbox" in result["errMsg"]

    def test_make_face_thumb_bad_box(self, scan_root, make_photo):
        absPath = os.path.join(str(scan_root), "x.jpg")
        make_photo(absPath, width=400, height=400)
        row = {"faceCode": "cd" + "4" * 62, "relPath": "x.jpg", "faceBox": "坏数据"}
        assert faceCropper.make_face_thumb(row)["ok"] is False


# ============================================================
# 五、Range 解析
# ============================================================

class TestParseRange:
    def test_simple(self):
        assert staticApi.parseRangeHeader("bytes=0-1023", 20000) == (0, 1023)

    def test_open_ended(self):
        assert staticApi.parseRangeHeader("bytes=500-", 20000) == (500, 19999)

    def test_suffix(self):
        assert staticApi.parseRangeHeader("bytes=-500", 20000) == (19500, 19999)

    def test_end_beyond_eof_is_clamped(self):
        assert staticApi.parseRangeHeader("bytes=0-99999", 20000) == (0, 19999)

    def test_case_insensitive(self):
        assert staticApi.parseRangeHeader("BYTES=0-9", 100) == (0, 9)

    def test_whole_file(self):
        assert staticApi.parseRangeHeader("bytes=0-", 100) == (0, 99)

    @pytest.mark.parametrize("value", ("", None, "   ", "items=0-9", "bytes=abc",
                                       "bytes=", "bytes=0", "0-9", "bytes=a-b",
                                       "bytes=0-99,200-299"))
    def test_ignored_returns_none(self, value):
        """不合规 / 多段 Range -> None（当作没有 Range，走 200 全量）

        多段 Range 按 RFC 7233 允许忽略；浏览器（尤其 <img>）实际从不发多段，
        支持 multipart/byteranges 只会引入边界 bug。
        """
        assert staticApi.parseRangeHeader(value, 20000) is None

    @pytest.mark.parametrize("value,size", (("bytes=99999-100000", 20000),
                                            ("bytes=-0", 20000),
                                            ("bytes=500-100", 20000),
                                            ("bytes=0-9", 0)))
    def test_unsatisfiable(self, value, size):
        with pytest.raises(staticApi.RangeNotSatisfiable):
            staticApi.parseRangeHeader(value, size)


class TestEtagHelpers:
    def test_quote(self):
        assert staticApi._quoteEtag("abc_400") == '"abc_400"'
        assert staticApi._quoteEtag('"abc_400"') == '"abc_400"'

    def test_match(self):
        assert staticApi._matchesEtag('"abc_400"', '"abc_400"') is True
        assert staticApi._matchesEtag('W/"abc_400"', '"abc_400"') is True
        assert staticApi._matchesEtag('"x", "abc_400"', '"abc_400"') is True
        assert staticApi._matchesEtag("*", '"abc_400"') is True
        assert staticApi._matchesEtag('"other"', '"abc_400"') is False
        assert staticApi._matchesEtag("", '"abc_400"') is False
        assert staticApi._matchesEtag(None, '"abc_400"') is False


class TestMime:
    @pytest.mark.parametrize("name,expect", (
        ("a.jpg", "image/jpeg"), ("a.JPEG", "image/jpeg"),
        ("a.png", "image/png"), ("a.heic", "image/heic"),
        ("a.avif", "image/avif"), ("a.tif", "image/tiff"),
        ("a.nef", "application/octet-stream"),
        ("a.unknown", "application/octet-stream"),
        ("a.webp", "image/webp"), ("a.jxl", "image/jxl")))
    def test_mime(self, name, expect):
        assert staticApi.mimeOf(name) == expect

    def test_fallback_to_db_mime(self):
        """mimetypes 查不到就用库里记录的 mimeType"""
        assert staticApi.mimeOf("a.zzz", "image/x-weird") == "image/x-weird"
        assert staticApi.mimeOf("a.zzz", "image/x-weird; charset=binary") == "image/x-weird"


# ============================================================
# 六、端点行为（直接调函数，不起服务）
# ============================================================

@pytest.fixture
def api_env(scan_root, make_photo, temp_db):
    """临时库 + 一条真实的 pb_photo 记录 + 磁盘上的原图"""
    relPath = "2024/2024-05-01/IMG_0001.jpg"
    absPath = os.path.join(str(scan_root), *relPath.split("/"))
    make_photo(absPath, width=1600, height=1200)
    fileHash = misc.fileHashHex(absPath)
    photoCode = "PH_API_0001"
    recID = sqliteCommon.insert_pb_photo("pb_photo", {
        "photoCode": photoCode, "relPath": relPath,
        "relPathHash": "r" + fileHash[1:], "fileHash": fileHash,
        "fileSize": os.path.getsize(absPath), "mimeType": None,
        "width": 1600, "height": 1200, "delFlag": "0"})
    assert recID > 0
    return {"photoCode": photoCode, "relPath": relPath, "fileHash": fileHash,
            "absOrig": absPath, "size": os.path.getsize(absPath),
            "photoRoot": str(scan_root)}


class TestThumbEndpoint:
    def test_first_request_generates(self, api_env):
        before = _counters()["decoded"]
        resp = staticApi.getThumb(api_env["photoCode"], Req(), 400)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/webp")
        assert resp.headers["x-thumb-cache"] == "generated"
        assert resp.headers["cache-control"] == basicSettings.THUMB_CACHE_CONTROL
        assert resp.headers["etag"] == '"%s"' % thumbStore.thumb_etag(
            api_env["fileHash"], 400)
        # ETag 里必须带编码参数：改了质量重新生成后，浏览器要能拿到新图而不是一直吃 304
        assert thumbStore.thumb_variant() in resp.headers["etag"]
        assert _counters()["decoded"] == before + 1

    def test_second_request_is_hit_without_decode(self, api_env):
        """**验收 2**（HTTP 层）：二次请求 ETag + 不重复解码"""
        staticApi.getThumb(api_env["photoCode"], Req(), 400)
        before = _counters()["decoded"]
        resp = staticApi.getThumb(api_env["photoCode"], Req(), 400)
        assert resp.status_code == 200
        assert resp.headers["x-thumb-cache"] == "hit"
        assert _counters()["decoded"] == before, "重复解码了原图"

    def test_if_none_match_304(self, api_env):
        first = staticApi.getThumb(api_env["photoCode"], Req(), 400)
        etag = first.headers["etag"]
        before = _counters()["decoded"]
        resp = staticApi.getThumb(api_env["photoCode"],
                                  Req({"if-none-match": etag}), 400)
        assert resp.status_code == 304
        assert resp.headers["etag"] == etag


class TestFailureStatusCodes:
    """出图失败的 HTTP 状态码：**刻意不都是 500**

    真实库里就有截断的/全零的 JPG（拷贝中断，d:\PhotoLib 实测 2137 张里9 张）。
    这类请求重试一万次也一样，回 5xx 会让前端和代理无限重试并弹错误提示。
    """

    def _addRow(self, relPath, fileHash, photoCode, fileSize=None):
        recID = sqliteCommon.insert_pb_photo("pb_photo", {
            "photoCode": photoCode, "relPath": relPath,
            "relPathHash": "r" + fileHash[1:], "fileHash": fileHash,
            "fileSize": fileSize if fileSize is not None else 0,
            "mimeType": None, "width": None, "height": None, "delFlag": "0"})
        assert recID > 0
        return photoCode

    def _writeBad(self, photoRoot, relPath, size=2048, zero=False):
        """在 photo 根下造一个"扩展名像图、内容解不出像素"的文件

        真实库里两种都踩到过：只有 JPEG 头没有熵编码数据（拷贝中途断），
        以及整份全零（拷贝预分配了大小却没写入内容）。
        """
        absPath = os.path.join(str(photoRoot), *relPath.split("/"))
        os.makedirs(os.path.dirname(absPath), exist_ok=True)
        head = b"\x00" * size if zero else b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * size
        with open(absPath, "wb") as f:
            f.write(head)
        return absPath

    def test_truncated_file_is_422_not_5xx(self, api_env):
        """原图在但解不出像素 -> 422"""
        from fastapi import HTTPException
        relPath = "corrupt/截断.jpg"
        self._writeBad(api_env["photoRoot"], relPath, 4096)
        code = self._addRow(relPath, "ab" + "1" * 62, "PH_CORRUPT", 4111)
        with pytest.raises(HTTPException) as info:
            staticApi.getThumb(code, Req(), 400)
        assert info.value.status_code == 422
        assert 400 <= info.value.status_code < 500, "5xx 会诱发无限重试"

    def test_zero_filled_file_is_422_and_writes_nothing(self, api_env):
        """全零的 .JPG（拷贝时预分配了大小却没写入内容）-> 422 且**不落任何文件**"""
        from fastapi import HTTPException
        relPath = "zero/全零.jpg"
        self._writeBad(api_env["photoRoot"], relPath, 8192, zero=True)
        fileHash = "cd" + "2" * 62
        code = self._addRow(relPath, fileHash, "PH_ZERO", 8192)
        with pytest.raises(HTTPException) as info:
            staticApi.getThumb(code, Req(), 400)
        assert info.value.status_code == 422
        assert not os.path.exists(thumbStore.thumb_abspath(fileHash, 400)), "失败竟落了文件"
        assert thumbStore.findTmpFiles() == [], "失败竟留了 .tmp"

    def test_missing_original_is_404(self, api_env):
        """原图不在磁盘（拔盘/挪走）-> 404，重试是有意义的"""
        from fastapi import HTTPException
        code = self._addRow("gone/nowhere.jpg", "ef" + "3" * 62, "PH_GONE", 1234)
        with pytest.raises(HTTPException) as info:
            staticApi.getThumb(code, Req(), 400)
        assert info.value.status_code == 404

    def test_err_codes_are_distinct(self):
        """三类故障必须能一眼分开，否则前端没法区别对待"""
        from processor.media import thumbMaker as tm
        assert tm.ERR_DECODE != tm.ERR_NO_ORIGINAL != tm.ERR_STORE
        assert tm.ERR_HTTP_STATUS[tm.ERR_DECODE] == 422
        assert tm.ERR_HTTP_STATUS[tm.ERR_NO_ORIGINAL] == 404
        assert tm.ERR_HTTP_STATUS[tm.ERR_STORE] == 500
        # 未知 errCode 兜底成 500（我们的问题就报我们的问题）
        assert tm.ERR_HTTP_STATUS.get("???") is None

    def test_pillow_oserror_is_decode_not_missing(self, api_env):
        """⚠️ 回归钉死：Pillow 对坏图抛的就是 **OSError**

        `UnidentifiedImageError` 是 OSError 的子类，截断图抛的是
        `OSError("image file is truncated")`。若按 OSError 分流成"原图不在"，
        正式库里9 张坏图会被全部误报成 404（"拔盘了？"），排障方向直接带偏。
        这里明确断言：文件在、但解不出 -> errCode 必须是 decode。
        """
        from processor.media import thumbMaker as tm
        relPath = "oserr/截断.jpg"
        self._writeBad(api_env["photoRoot"], relPath, 4096)
        assert os.path.isfile(os.path.join(str(api_env["photoRoot"]),
                                           *relPath.split("/")))
        one = tm.make_thumb({"photoCode": "PH_OSERR", "relPath": relPath,
                             "fileHash": "ab" + "6" * 62}, 200)
        assert one["ok"] is False
        assert one["errCode"] == tm.ERR_DECODE, \
            "坏图被误判成 %r（会让 9 张坏图都报成 404）" % one["errCode"]

    def test_decode_failure_counted_separately(self, api_env):
        """批量汇总里要能把"原图坏了"单独拎出来（否则"失败 9 张"没信息量）"""
        from processor.media import thumbMaker as tm
        tm.resetCounters()
        relPath = "broken/坏图.jpg"
        self._writeBad(api_env["photoRoot"], relPath, 2048)
        base = {"relPath": relPath, "fileSize": 2057, "width": None, "height": None}
        rows = [dict(base, fileHash="ef" + "4" * 62, photoCode="PH_BAD1"),
                dict(base, fileHash="ef" + "5" * 62, photoCode="PH_BAD2")]
        summary = tm.make_thumbs_bulk(rows, workers=1, size=200)
        assert summary["failed"] == 2
        assert summary["failedDecode"] == 2
        assert all(f["errCode"] == tm.ERR_DECODE for f in summary["failures"])
        assert summary["created"] == 0 and summary["bytesTotal"] == 0
        assert thumbStore.findTmpFiles() == []

    def test_if_none_match_marks_not_modified(self, api_env):
        """304 也要带X-Thumb-Cache: not-modified（省得前端以为是命中磁盘）"""
        first = staticApi.getThumb(api_env["photoCode"], Req(), 400)
        before = _counters()["decoded"]
        resp = staticApi.getThumb(api_env["photoCode"],
                                  Req({"if-none-match": first.headers["etag"]}), 400)
        assert resp.status_code == 304
        assert resp.headers["x-thumb-cache"] == "not-modified"
        assert _counters()["decoded"] == before

    def test_etag_differs_by_size(self, api_env):
        a = staticApi.getThumb(api_env["photoCode"], Req(), 200)
        b = staticApi.getThumb(api_env["photoCode"], Req(), 800)
        assert a.headers["etag"] != b.headers["etag"]

    def test_regenerate_after_delete(self, api_env):
        """**验收 6**（HTTP 层）：手动删除后自动重建"""
        staticApi.getThumb(api_env["photoCode"], Req(), 400)
        absThumb = thumbStore.thumb_abspath(api_env["fileHash"], 400)
        os.remove(absThumb)
        before = _counters()["decoded"]
        resp = staticApi.getThumb(api_env["photoCode"], Req(), 400)
        assert resp.status_code == 200
        assert resp.headers["x-thumb-cache"] == "generated"
        assert os.path.isfile(absThumb)
        assert _counters()["decoded"] == before + 1

    def test_unknown_size_400(self, api_env):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as info:
            staticApi.getThumb(api_env["photoCode"], Req(), 333)
        assert info.value.status_code == 400

    def test_unknown_photo_404(self, api_env):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as info:
            staticApi.getThumb("PH_NOT_EXIST", Req(), 400)
        assert info.value.status_code == 404

    def test_del_flag_hidden(self, api_env):
        """软删的照片不该还能拿到缩略图"""
        from fastapi import HTTPException
        rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=api_env["photoCode"])
        sqliteCommon.updateTableGeneral("pb_photo", "recID = %s", (rows[0]["recID"],),
                                       {"delFlag": "1"})
        with pytest.raises(HTTPException) as info:
            staticApi.getThumb(api_env["photoCode"], Req(), 400)
        assert info.value.status_code == 404


class TestOriginalEndpoint:
    def test_range_206(self, api_env):
        """**验收 3**：Range: bytes=0-1023 -> 206 + 正确 Content-Range/Length"""
        size = api_env["size"]
        resp = staticApi.getOriginal(api_env["photoCode"],
                                     Req({"range": "bytes=0-1023"}))
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 0-1023/%d" % size
        assert int(resp.headers["content-length"]) == 1024
        assert resp.headers["accept-ranges"] == "bytes"
        assert resp.headers["content-type"].startswith("image/jpeg")

        data = b"".join(staticApi._iterFileRange(api_env["absOrig"], 0, 1023))
        assert len(data) == 1024
        with open(api_env["absOrig"], "rb") as handle:
            assert data == handle.read(1024)

    def test_range_suffix(self, api_env):
        size = api_env["size"]
        resp = staticApi.getOriginal(api_env["photoCode"],
                                     Req({"range": "bytes=-512"}))
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes %d-%d/%d" % (size - 512, size - 1, size)
        assert int(resp.headers["content-length"]) == 512

    def test_range_open_ended(self, api_env):
        size = api_env["size"]
        resp = staticApi.getOriginal(api_env["photoCode"],
                                     Req({"range": "bytes=1000-"}))
        assert resp.status_code == 206
        assert int(resp.headers["content-length"]) == size - 1000

    def test_range_unsatisfiable_416(self, api_env):
        size = api_env["size"]
        resp = staticApi.getOriginal(api_env["photoCode"],
                                     Req({"range": "bytes=%d-" % (size + 10)}))
        assert resp.status_code == 416
        assert resp.headers["content-range"] == "bytes */%d" % size

    def test_multi_range_falls_back_to_200(self, api_env):
        resp = staticApi.getOriginal(api_env["photoCode"],
                                     Req({"range": "bytes=0-99,200-299"}))
        assert resp.status_code == 200
        assert int(resp.headers["content-length"]) == api_env["size"]

    def test_no_range_200(self, api_env):
        """**验收 4**：无 Range -> 200 完整文件"""
        resp = staticApi.getOriginal(api_env["photoCode"], Req())
        assert resp.status_code == 200
        assert int(resp.headers["content-length"]) == api_env["size"]
        assert resp.headers["accept-ranges"] == "bytes"
        data = b"".join(staticApi._iterFileRange(api_env["absOrig"], 0, api_env["size"] - 1))
        assert len(data) == api_env["size"]
        with open(api_env["absOrig"], "rb") as handle:
            assert data == handle.read()

    def test_head_no_body(self, api_env):
        resp = staticApi.getOriginal(api_env["photoCode"], Req(method="HEAD"))
        assert resp.status_code == 200
        assert int(resp.headers["content-length"]) == api_env["size"]
        assert not resp.body

    def test_head_with_range(self, api_env):
        size = api_env["size"]
        resp = staticApi.getOriginal(api_env["photoCode"],
                                     Req({"range": "bytes=0-1023"}, method="HEAD"))
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 0-1023/%d" % size
        assert not resp.body

    def test_etag_304(self, api_env):
        first = staticApi.getOriginal(api_env["photoCode"], Req())
        resp = staticApi.getOriginal(api_env["photoCode"],
                                     Req({"if-none-match": first.headers["etag"]}))
        assert resp.status_code == 304

    def test_etag_ignored_when_range_present(self, api_env):
        """有 Range 时 If-None-Match 的语义归 If-Range 管，这里不返回 304"""
        first = staticApi.getOriginal(api_env["photoCode"], Req())
        resp = staticApi.getOriginal(api_env["photoCode"],
                                     Req({"if-none-match": first.headers["etag"],
                                          "range": "bytes=0-10"}))
        assert resp.status_code == 206

    def test_missing_file_404(self, api_env):
        """库里有、磁盘没有（拔盘/移走）-> 404 而不是 500"""
        from fastapi import HTTPException
        os.remove(api_env["absOrig"])
        with pytest.raises(HTTPException) as info:
            staticApi.getOriginal(api_env["photoCode"], Req())
        assert info.value.status_code == 404

    def test_unknown_photo_404(self, api_env):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as info:
            staticApi.getOriginal("PH_NOPE", Req())
        assert info.value.status_code == 404

    def test_never_reads_whole_file_for_range(self, api_env):
        """Range 只读那一段：seek 之后的读取量 == 请求字节数"""
        import io
        absOrig = api_env["absOrig"]
        with open(absOrig, "rb") as handle:
            handle.seek(4096)
            chunk = handle.read(1024)
        assert len(chunk) == 1024
        with open(absOrig, "rb") as handle:
            assert chunk == handle.read()[4096:5120]

    def test_iter_range_multiple_chunks(self, api_env, monkeypatch):
        """块大小被调小时多块拼接结果不变（STREAM_CHUNK_SIZE 调小也正确）"""
        absOrig = api_env["absOrig"]
        with open(absOrig, "rb") as handle:
            expect = handle.read()
        got = b"".join(staticApi._iterFileRange(absOrig, 3, 1000, chunkSize=7))
        assert got == expect[3:1001]


class TestFaceEndpoint:
    def test_404_no_face_data(self, api_env):
        """步骤 4 还没有任何 pb_face 数据 -> 404（接口先留好）"""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as info:
            staticApi.getFace("FE" + "0" * 62, Req())
        assert info.value.status_code == 404

    def test_serves_face_image(self, scan_root, make_photo, temp_db):
        faceCode = "fa" + "9" * 62
        srcPath = os.path.join(str(scan_root), "face_api.jpg")
        make_photo(srcPath, width=1000, height=800)
        made = faceCropper.crop_to_file(srcPath, (0.2, 0.2, 0.3, 0.3), faceCode)
        assert made["ok"] is True
        recID = sqliteCommon.insert_pb_face("pb_face", {
            "faceCode": faceCode, "photoCode": "PH_FACE", "faceBox": "0.2,0.2,0.3,0.3",
            "delFlag": "0"})
        assert recID > 0

        resp = staticApi.getFace(faceCode, Req())
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["etag"] == '"%s"' % thumbStore.face_etag(faceCode)
        assert thumbStore.face_variant() in resp.headers["etag"]

        again = staticApi.getFace(faceCode, Req({"if-none-match": resp.headers["etag"]}))
        assert again.status_code == 304

    def test_404_when_crop_missing(self, scan_root, temp_db):
        """库里有 faceCode 但裁剪图不在（步骤 5 还没跑）-> 404，不做按需生成"""
        from fastapi import HTTPException
        faceCode = "fb" + "8" * 62
        sqliteCommon.insert_pb_face("pb_face", {
            "faceCode": faceCode, "photoCode": "PH_X", "faceBox": "0.1,0.1,0.2,0.2",
            "delFlag": "0"})
        with pytest.raises(HTTPException) as info:
            staticApi.getFace(faceCode, Req())
        assert info.value.status_code == 404


# ============================================================
# 七、启动守卫
# ============================================================

class TestLoopbackGuard:
    def test_accepts_loopback(self):
        from main import app as appMod
        for host in ("127.0.0.1", "localhost", "::1", "LOCALHOST"):
            assert appMod.assertLoopbackHost(host).lower() in appMod.LOOPBACK_HOSTS

    def test_rejects_public_host(self):
        """零鉴权 + 0.0.0.0 = 把全家照片发到局域网 —— 必须是**拒绝启动**"""
        from main import app as appMod
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with pytest.raises(appMod.StartupConfigError):
                appMod.assertLoopbackHost(host)

    def test_empty_falls_back(self):
        from main import app as appMod
        assert appMod.assertLoopbackHost("") == basicSettings.SERVER_HOST
        assert appMod.assertLoopbackHost(None) == basicSettings.SERVER_HOST

    def test_default_config_is_loopback(self):
        assert basicSettings.SERVER_HOST == "127.0.0.1"
