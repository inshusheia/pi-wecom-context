from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sidecar.wecom_context_core.image_cache import (
    cache_roots,
    candidate_paths,
    downloaded_image_index,
    image_keys_for_messages,
    probe_image,
    resolve_image,
    split_long_image,
    tiling_available,
)


def png_bytes(width: int, height: int, tail: bytes = b"") -> bytes:
    chunk = b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk + tail


def jpeg_bytes(width: int, height: int, tail: bytes = b"") -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + tail


class ImageCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wecom-image-cache-")
        self.root = Path(self._tmp.name)
        self.images = self.root / "Profiles" / "PROFILEHASH" / "Caches" / "Images"
        self.images.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def place(
        self,
        payload: bytes,
        *,
        month: str = "2026-09",
        variant: str = "standard",
        key: str | None = None,
        name: str | None = None,
        declared_key: str | None = None,
    ) -> tuple[str, Path]:
        """把一个文件放进缓存树；key 默认取内容 md5，declared_key 可强制目录名与内容不一致。"""
        directory_key = declared_key or key or hashlib.md5(payload).hexdigest()
        suffix = {"hd": "_HD", "thumb": "_THUMB"}.get(variant, "")
        folder = self.images / month / f"{directory_key}{suffix}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name or directory_key)
        path.write_bytes(payload)
        return directory_key, path

    def resolve(self, key: str, expected: int | None = None):
        return resolve_image(key, expected_size=expected, roots=[self.images])

    # ---------------------------------------------------------------- 解析成功
    def test_png_matching_key_resolves_as_original(self) -> None:
        payload = png_bytes(1280, 720)
        key, path = self.place(payload)
        result = self.resolve(key)
        self.assertEqual(result.status, "original")
        self.assertIsNone(result.reason)
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual((result.width, result.height), (1280, 720))
        self.assertEqual(result.size_bytes, len(payload))
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(Path(result.path), path)

    def test_jpeg_dimensions_read_from_sof_marker(self) -> None:
        payload = jpeg_bytes(640, 480)
        key, _ = self.place(payload)
        mime, width, height = probe_image(self.images / "2026-09" / key / key)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual((width, height), (640, 480))

    # ---------------------------------------------------------------- 变体选择
    def test_compressed_variant_is_reported_as_thumbnail_with_sizes(self) -> None:
        original_size = 4000
        key, path = self.place(b"\x00" * original_size, declared_key="a" * 32)
        small = png_bytes(64, 64, tail=b"\x00" * 100)
        self.place(small, declared_key="a" * 32)
        result = self.resolve("a" * 32, expected=original_size)
        self.assertEqual(result.status, "thumbnail")
        self.assertEqual(result.size_bytes, len(small))
        self.assertIn(str(len(small)), result.reason)
        self.assertIn(str(original_size), result.reason)

    def test_size_match_beats_hd_variant(self) -> None:
        payload = png_bytes(200, 200, tail=b"\x00" * 500)
        self.place(payload, declared_key="b" * 32, variant="hd")
        standard = png_bytes(200, 200, tail=b"\x00" * 200)
        self.place(standard, declared_key="b" * 32)
        result = self.resolve("b" * 32, expected=len(standard))
        self.assertEqual(result.status, "original")
        self.assertEqual(result.size_bytes, len(standard))

    def test_hd_variant_preferred_when_nothing_matches(self) -> None:
        standard = png_bytes(100, 100, tail=b"\x00" * 64)
        hd = png_bytes(100, 100, tail=b"\x00" * 4096)
        self.place(standard, declared_key="c" * 32)
        self.place(hd, declared_key="c" * 32, variant="hd")
        result = self.resolve("c" * 32)
        self.assertEqual(result.status, "thumbnail")
        self.assertEqual(result.size_bytes, len(hd))

    def test_named_files_in_hd_and_thumbnail_directories_are_resolved(self) -> None:
        """企微截图缓存的文件名可与 md5 键不同，目录键仍应足够定位。"""
        key = "f" * 32
        standard = png_bytes(100, 100, tail=b"\x00" * 64)
        thumbnail = png_bytes(100, 100, tail=b"\x00" * 128)
        hd = png_bytes(100, 100, tail=b"\x00" * 256)
        self.place(standard, declared_key=key, name="企业微信截图_thumb.png")
        self.place(thumbnail, declared_key=key, variant="thumb", name="企业微信截图_thumb.png")
        hd_key, hd_path = self.place(hd, declared_key=key, variant="hd", name="企业微信截图_original.png")
        result = self.resolve(hd_key, expected=len(hd))
        self.assertEqual(result.status, "original")
        self.assertEqual(Path(result.path), hd_path)

    def test_newest_month_wins_for_same_rank(self) -> None:
        older = png_bytes(300, 300, tail=b"\x00" * 900)
        newer = png_bytes(300, 300, tail=b"\x00" * 901)
        self.place(older, declared_key="d" * 32, month="2026-08")
        self.place(newer, declared_key="d" * 32, month="2026-09", variant="hd")
        result = self.resolve("d" * 32)
        self.assertEqual(result.size_bytes, len(newer))

    # ---------------------------------------------------------------- 边界与异常
    def test_unknown_key_reports_missing(self) -> None:
        result = self.resolve("e" * 32)
        self.assertEqual(result.status, "missing")
        self.assertIsNone(result.path)
        self.assertIn("不存在", result.reason)

    def test_illegal_key_is_rejected_before_touching_filesystem(self) -> None:
        for key in ("../../../etc/passwd", "F" * 32, "abc", "", "g" * 32):
            result = resolve_image(key, roots=[self.images / "does-not-exist"])
            self.assertEqual(result.status, "missing", key)
            self.assertIn("非法", result.reason)

    def test_gif_is_reported_unsupported_with_path_for_manual_review(self) -> None:
        payload = b"GIF89a" + (16).to_bytes(2, "little") + (16).to_bytes(2, "little") + b"\x00" * 32
        key, path = self.place(payload)
        result = self.resolve(key)
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.mime_type, "image/gif")
        self.assertEqual(Path(result.path), path)

    def test_non_image_content_with_matching_key_reports_failed(self) -> None:
        key, _ = self.place(b"this is not an image at all")
        result = self.resolve(key)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.mime_type)
        self.assertIn("格式", result.reason)

    def test_oversized_bytes_are_reported_too_large(self) -> None:
        payload = png_bytes(64, 64, tail=b"\x00" * 5000)
        key, path = self.place(payload)
        result = resolve_image(key, roots=[self.images], max_bytes=1024)
        self.assertEqual(result.status, "too_large")
        self.assertEqual(Path(result.path), path)
        self.assertIn("1024", result.reason)

    def test_oversized_edge_pixels_are_reported_too_large(self) -> None:
        payload = png_bytes(4000, 20, tail=b"\x00" * 40)
        key, path = self.place(payload)
        result = resolve_image(key, roots=[self.images], max_edge_pixels=2048)
        self.assertEqual(result.status, "too_large")
        self.assertEqual(Path(result.path), path)
        self.assertIn("4000", result.reason)
        self.assertEqual(resolve_image(key, roots=[self.images], max_edge_pixels=4096).status, "original")

    def test_within_limits_still_resolves_as_original(self) -> None:
        payload = png_bytes(64, 64, tail=b"\x00" * 100)
        key, _ = self.place(payload)
        result = resolve_image(key, roots=[self.images], max_bytes=10_000, max_edge_pixels=1024)
        self.assertEqual(result.status, "original")

    def test_candidate_paths_ignores_non_month_directories(self) -> None:
        key, path = self.place(png_bytes(8, 8, tail=b"\x00" * 40))
        stray = self.images / "tmp" / key
        stray.mkdir(parents=True, exist_ok=True)
        (stray / key).write_bytes(b"\x00" * 8)
        found = candidate_paths(self.images, key)
        self.assertEqual([item[0] for item in found], [path])

    def test_cache_roots_skips_missing_profiles_root(self) -> None:
        self.assertEqual(cache_roots(self.root / "nowhere"), [])
    def test_downloaded_image_is_used_when_wecom_cache_is_missing(self) -> None:
        payload = png_bytes(64, 64, tail=b"\x01" * 40)
        key = hashlib.md5(payload).hexdigest()
        downloaded = self.root / "downloads" / "WeCom Context" / "ds-1" / "会话" / "image_0001.png"
        downloaded.parent.mkdir(parents=True)
        downloaded.write_bytes(payload)
        fallback = downloaded_image_index(self.root / "downloads")
        result = resolve_image(key, roots=[], fallback_index=fallback)
        self.assertEqual(result.status, "original")
        self.assertEqual(Path(result.path), downloaded)


    def test_month_entry_that_is_a_plain_file_is_skipped(self) -> None:
        (self.images / "2026-09").write_bytes(b"not a directory")
        key, path = self.place(png_bytes(8, 8, tail=b"\x00" * 40), month="2026-08")
        found = candidate_paths(self.images, key)
        self.assertEqual([item[0] for item in found], [path])

    def test_unreadable_root_does_not_break_resolution_from_other_root(self) -> None:
        """容器目录扫描被系统打断（或用无权限）时，其它账号的缓存仍要能解析。"""
        blocked = self.root / "blocked" / "Caches" / "Images"
        blocked.mkdir(parents=True)
        blocked.chmod(0o000)
        try:
            try:
                next(blocked.iterdir())
            except PermissionError:
                pass
            else:
                self.skipTest("当前用户可无视权限位，无法构造不可读目录")
            payload = png_bytes(64, 64, tail=b"\x00" * 100)
            key, path = self.place(payload)
            result = resolve_image(key, roots=[blocked, self.images])
            self.assertEqual(result.status, "original")
            self.assertEqual(Path(result.path), path)
        finally:
            blocked.chmod(0o700)

    # ---------------------------------------------------------------- file.db 映射
    def make_snapshot(self, rows: list[tuple]) -> Path:
        snapshot = self.root / "snapshot"
        snapshot.mkdir(exist_ok=True)
        connection = sqlite3.connect(snapshot / "file.db")
        connection.execute(
            "CREATE TABLE file_table4 (message_id INTEGER, file_index INTEGER, message_type INTEGER,"
            " extension_type INTEGER, name TEXT, size INTEGER, md5 TEXT, info_extension BLOB)"
        )
        connection.executemany("INSERT INTO file_table4 VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.commit()
        connection.close()
        return snapshot

    def test_index_keeps_image_rows_sorted_by_file_index(self) -> None:
        snapshot = self.make_snapshot([
            (2420, 1, 1, 4, "second.jpg", 2048, "1" * 32, None),
            (2420, 0, 1, 4, "first.jpg", 1024, "2" * 32, None),
            (2420, 2, 2, 3, "document.pdf", 4096, "3" * 32, None),
            (2420, 3, 3, 0, "video.MP4", 11642096, "5" * 32, None),
            (999, 0, 1, 4, "other.jpg", 512, "4" * 32, None),
        ])
        mapping = image_keys_for_messages(snapshot, ["2420", "8888"])
        self.assertEqual([item["key"] for item in mapping["2420"]], ["2" * 32, "1" * 32])
        self.assertEqual(mapping["2420"][0]["size"], 1024)
        self.assertEqual(mapping["8888"], [])
        self.assertIsNone(mapping.get("999"))

    def test_index_falls_back_to_info_extension_key(self) -> None:
        snapshot = self.make_snapshot([
            (3001, 0, 1, 4, "", 700, "", b"\x08\x02\x12 " + b"a1b2c3d4e5f60718293a4b5c6d7e8f90" + b"\x18\x00"),
            (3002, 0, 1, 4, "", 700, "not-a-key", b"no key here"),
            (3003, 0, 1, 4, "", 700, None, None),
        ])
        mapping = image_keys_for_messages(snapshot, ["3001", "3002", "3003"])
        self.assertEqual([item["key"] for item in mapping["3001"]], ["a1b2c3d4e5f60718293a4b5c6d7e8f90"])
        self.assertEqual(mapping["3002"], [])
        self.assertEqual(mapping["3003"], [])

    def test_index_without_file_db_returns_empty_mapping(self) -> None:
        snapshot = self.root / "empty-snapshot"
        snapshot.mkdir()
        mapping = image_keys_for_messages(snapshot, ["1"])
        self.assertEqual(mapping, {"1": []})


class LongImageTilingTests(unittest.TestCase):
    """长截图分片：企微图片大量是 1 万像素高的聊天截图，整体缩放会让文字不可读。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wecom-tiling-")
        self.root = Path(self._tmp.name)
        self.out = self.root / "tiles"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def tall_png(width: int, height: int, band: int = 64) -> bytes:
        """写一个真实可解码的 PNG（每 64 行换一次颜色，便于肉眼核对分片位置）。"""
        import struct
        import zlib

        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        raw = b"".join(
            b"\x00" + bytes([(row // band) % 256, 16, 32]) * width for row in range(height)
        )
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b"")
        )

    @unittest.skipUnless(tiling_available(), "需要 macOS 自带的 sips")
    def test_long_image_is_split_into_equal_height_tiles(self) -> None:
        source = self.root / "long.png"
        source.write_bytes(self.tall_png(48, 5000))
        tiles, omitted = split_long_image(
            source, width=48, height=5000, max_edge_pixels=1024, output_dir=self.out, max_tiles=12
        )
        self.assertEqual(omitted, 0)
        self.assertEqual(len(tiles), 5)
        self.assertEqual([tile.index for tile in tiles], [1, 2, 3, 4, 5])
        self.assertTrue(all(tile.total == 5 for tile in tiles))
        for tile in tiles:
            self.assertEqual((tile.width, tile.height), (48, 1024))
            self.assertEqual(tile.mime_type, "image/jpeg")
            self.assertTrue(tile.path.is_file())
            self.assertGreater(tile.size_bytes, 0)
        self.assertEqual(len({tile.sha256 for tile in tiles}), 5, "每片内容必须不同")

    @unittest.skipUnless(tiling_available(), "需要 macOS 自带的 sips")
    def test_tiles_are_never_uncropped_originals(self) -> None:
        """实测踩过的坑：sips 在某些偏移下会静默返回整图，绝不能当成分片交给模型。"""
        source = self.root / "long.png"
        source.write_bytes(self.tall_png(32, 3000))
        tiles, _ = split_long_image(
            source, width=32, height=3000, max_edge_pixels=1024, output_dir=self.out, max_tiles=12
        )
        self.assertTrue(tiles)
        for tile in tiles:
            self.assertEqual(tile.height, 1024, f"分片 {tile.index} 高度不对：{tile.height}")

    @unittest.skipUnless(tiling_available(), "需要 macOS 自带的 sips")
    def test_capped_tiles_report_omitted_count(self) -> None:
        source = self.root / "long.png"
        source.write_bytes(self.tall_png(32, 10000))
        tiles, omitted = split_long_image(
            source, width=32, height=10000, max_edge_pixels=1024, output_dir=self.out, max_tiles=3
        )
        self.assertEqual(len(tiles), 3)
        self.assertEqual(omitted, 10 - 3)
        self.assertTrue(all(tile.total == 10 for tile in tiles), "total 必须是全部分片数，不是已发送数")

    def test_short_image_is_not_tiled(self) -> None:
        source = self.root / "short.png"
        source.write_bytes(self.tall_png(16, 64))
        tiles, omitted = split_long_image(
            source, width=16, height=64, max_edge_pixels=1024, output_dir=self.out, max_tiles=12
        )
        self.assertEqual((tiles, omitted), ([], 0))


if __name__ == "__main__":
    unittest.main()
