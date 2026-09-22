from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sidecar.wecom_context_core.image_cache import _md5, downloaded_image_index  # noqa: PLC2701 - 测试要复用同一哈希口径
from sidecar.wecom_context_core.package_builder import (
    build_source,
    compose_text,
    normalise_request,
    package_files,
    package_id,
    package_stats,
    prune_packages,
    write_manifest,
)


def png_bytes(width: int, height: int, tail: bytes = b"") -> bytes:
    chunk = b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk + tail


class PackageBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wecom-package-")
        self.root = Path(self._tmp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.package_dir = self.root / "packages" / "pkg-test-0001"
        self.package_dir.mkdir(parents=True)
        self.images = self.root / "Profiles" / "P" / "Caches" / "Images"
        self.images.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------ 夹具
    def write_message_db(self, rows: list[tuple]) -> None:
        connection = sqlite3.connect(self.snapshot / "message.db")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS message_table (message_id INTEGER, conversation_id TEXT, sender_id INTEGER,"
            " content_type INTEGER, send_time INTEGER, content TEXT, extra_content TEXT, local_extra_content TEXT)"
        )
        connection.executemany("INSERT INTO message_table VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.commit()
        connection.close()

    def write_file_db(self, rows: list[tuple]) -> None:
        connection = sqlite3.connect(self.snapshot / "file.db")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS file_table4 (message_id INTEGER, file_index INTEGER, message_type INTEGER,"
            " extension_type INTEGER, name TEXT, size INTEGER, md5 TEXT, info_extension BLOB)"
        )
        connection.executemany("INSERT INTO file_table4 VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.commit()
        connection.close()

    def write_manifest(self, dataset_id: str = "07e6cb301334") -> None:
        (self.snapshot / "manifest.json").write_text(
            json.dumps({"dataset_id": dataset_id, "created_at": "2026-09-16T11:04:01+08:00"}),
            encoding="utf-8",
        )

    def place_image(self, payload: bytes) -> str:
        key = hashlib.md5(payload).hexdigest()
        folder = self.images / "2026-09" / key
        folder.mkdir(parents=True, exist_ok=True)
        (folder / key).write_bytes(payload)
        return key

    def binding(self, **overrides) -> dict:
        value = {
            "session_key": "a1b2c3d4e5f60718",
            "conversation_id": "S:1_2",
            "dataset_id": "07e6cb301334",
            "snapshot_id": "snap-1",
            "snapshot_path": str(self.snapshot),
            "mode": "active",
            "session_name": "工资条",
            "kind": "单聊",
        }
        value.update(overrides)
        return value

    def build(self, *, messages=None, include_images=True, fallback_index=None, **image_overrides):
        state = {
            "count": 0,
            "omitted": 0,
            "bytes": 0,
            "max_images": 8,
            "max_total_bytes": 8 * 1024 * 1024,
            "max_edge_pixels": 2048,
            "package_dir": self.package_dir,
        }
        state.update(image_overrides)
        return build_source(
            source_index=1,
            source_request={"include_images": include_images},
            binding=self.binding(),
            snapshot=self.snapshot,
            manifest={"created_at": "2026-09-16T11:04:01+08:00", "dataset_id": "07e6cb301334"},
            messages=messages or [],
            token_budget=2500,
            max_message_characters=2000,
            stale_after_minutes=120,
            image_state=state,
            roots=[self.images],
            fallback_index=fallback_index,
        )

    @staticmethod
    def message(message_id: int, content: str, *, content_type: int = 2) -> dict:
        return {
            "message_id": message_id,
            "conversation_id": "S:1_2",
            "sender": "张三",
            "content_type": content_type,
            "type_name": "文本",
            "send_time": 1_700_000_000,
            "time": "2026-09-13 15:28",
            "content": content,
        }

    # ------------------------------------------------------------------ 图片
    def test_usable_image_is_copied_and_referenced_by_number(self) -> None:
        payload = png_bytes(1280, 720, tail=b"\x00" * 100)
        key = self.place_image(payload)
        self.write_file_db([(10, 0, 1, 4, "pic.png", len(payload), key, None)])
        built = self.build(messages=[self.message(10, "[图片]", content_type=4)])

        images = [item for item in built["images"] if item["image_id"].startswith("img_")]
        self.assertEqual(len(images), 1)
        stored = images[0]
        self.assertEqual(stored["image_id"], "img_0001")
        self.assertEqual(stored["status"], "original")
        self.assertEqual(stored["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(stored["width"], 1280)
        copied = Path(stored["path"])
        self.assertTrue(copied.is_file())
        self.assertTrue(copied.is_relative_to(self.package_dir))
        self.assertEqual(copied.read_bytes(), payload)
        self.assertEqual(built["messages"][0]["image_ids"], ["img_0001"])
        self.assertIn("【图片 1】", built["messages"][0]["content"])
        self.assertIn("【图片 1】", built["text"])
        self.assertEqual(built["source"]["image_count"], 1)
        self.assertEqual(built["source"]["omitted_image_count"], 0)
    def test_downloaded_image_is_materialized_when_cache_is_missing(self) -> None:
        payload = png_bytes(128, 128, tail=b"\x03" * 100)
        key = hashlib.md5(payload).hexdigest()
        downloaded = self.root / "downloads" / "WeCom Context" / "07e6cb301334" / "工资条" / "image_0001.png"
        downloaded.parent.mkdir(parents=True)
        downloaded.write_bytes(payload)
        self.write_file_db([(12, 0, 1, 4, "pic.png", len(payload), key, None)])
        built = self.build(
            messages=[self.message(12, "[图片]", content_type=4)],
            fallback_index=downloaded_image_index(self.root / "downloads"),
        )
        image = next(item for item in built["images"] if item["image_id"] == "img_0001")
        self.assertEqual(image["status"], "original")
        self.assertEqual(Path(image["path"]).read_bytes(), payload)
        self.assertEqual(built["source"]["image_count"], 1)


    def test_missing_image_is_reported_without_path(self) -> None:
        self.write_file_db([(11, 0, 1, 4, "gone.jpg", 4096, "a" * 32, None)])
        built = self.build(messages=[self.message(11, "[图片]", content_type=4)])
        omitted = [item for item in built["images"] if not item["image_id"].startswith("img_")]
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]["status"], "missing")
        self.assertIsNone(omitted[0]["path"])
        self.assertIsNone(omitted[0]["sha256"])
        self.assertTrue(omitted[0]["reason"])
        self.assertEqual(built["messages"][0]["image_ids"], [])
        self.assertIn("【图片不可用】", built["messages"][0]["content"])
        self.assertEqual(built["source"]["omitted_image_count"], 1)
        self.assertTrue(any(item["code"] == "IMAGE_UNAVAILABLE" for item in built["warnings"]))

    def test_image_count_limit_marks_extra_images_too_large(self) -> None:
        for index in range(3):
            payload = png_bytes(32, 32, tail=bytes([index]) * 20)
            key = self.place_image(payload)
            self.write_file_db([(20 + index, 0, 1, 4, f"p{index}.png", len(payload), key, None)])
        built = self.build(
            messages=[self.message(20 + index, "[图片]", content_type=4) for index in range(3)],
            max_images=2,
        )
        usable = [item for item in built["images"] if item["image_id"].startswith("img_")]
        limited = [item for item in built["images"] if item["status"] == "too_large"]
        self.assertEqual(len(usable), 2)
        self.assertEqual(len(limited), 1)
        self.assertIn("上限", limited[0]["reason"])
        self.assertTrue(any(item["code"] == "IMAGE_LIMIT" for item in built["warnings"]))

    def test_total_bytes_limit_degrades_to_too_large(self) -> None:
        payload = png_bytes(64, 64, tail=b"\x00" * 400)
        key = self.place_image(payload)
        self.write_file_db([(30, 0, 1, 4, "big.png", len(payload), key, None)])
        built = self.build(messages=[self.message(30, "[图片]", content_type=4)], max_total_bytes=10)
        self.assertEqual(built["source"]["image_count"], 0)
        self.assertEqual(built["images"][0]["status"], "too_large")
        self.assertIsNone(built["images"][0]["path"])

    def test_images_are_skipped_entirely_when_not_requested(self) -> None:
        payload = png_bytes(64, 64, tail=b"\x00" * 40)
        key = self.place_image(payload)
        self.write_file_db([(40, 0, 1, 4, "pic.png", len(payload), key, None)])
        built = self.build(messages=[self.message(40, "[图片]", content_type=4)], include_images=False)
        self.assertEqual(built["images"], [])
        self.assertNotIn("【图片", built["messages"][0]["content"])

    def test_image_numbering_is_global_in_encounter_order(self) -> None:
        keys = []
        for index in range(2):
            payload = png_bytes(16, 16, tail=bytes([index + 7]) * 30)
            keys.append(self.place_image(payload))
            self.write_file_db([(50 + index, 0, 1, 4, f"n{index}.png", len(payload), keys[-1], None)])
        built = self.build(messages=[self.message(50 + index, "[图片]", content_type=4) for index in range(2)])
        self.assertEqual(built["messages"][0]["image_ids"], ["img_0001"])
        self.assertEqual(built["messages"][1]["image_ids"], ["img_0002"])
        self.assertIn("【图片 1】", built["messages"][0]["content"])
        self.assertIn("【图片 2】", built["messages"][1]["content"])

    def test_image_message_body_is_replaced_by_marker(self) -> None:
        """图片消息的正文是解码出来的 CDN 长链接，不能原样进上下文。"""
        payload = png_bytes(32, 32, tail=b"\x00" * 40)
        key = self.place_image(payload)
        self.write_file_db([(60, 0, 1, 4, "pic.png", len(payload), key, None)])
        url = "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=" + "a" * 900
        built = self.build(messages=[self.message(60, url, content_type=4)])
        content = built["messages"][0]["content"]
        self.assertNotIn("imunion", content)
        self.assertIn("【图片 1】", content)
        self.assertLess(len(content), 60)

    def test_long_link_in_normal_message_is_trimmed(self) -> None:
        long_link = "https://example.com/path?q=" + "b" * 400
        built = self.build(messages=[self.message(61, f"看看这个 {long_link}")])
        content = built["messages"][0]["content"]
        self.assertNotIn("example.com", content)
        self.assertIn("链接已省略", content)
        self.assertIn("看看这个", content)

    def test_normal_message_text_is_untouched(self) -> None:
        built = self.build(messages=[self.message(62, "周三 15:00 开会")])
        self.assertEqual(built["messages"][0]["content"], "周三 15:00 开会")

    # ------------------------------------------------------------------ 汇总
    def test_compose_text_states_the_untrusted_boundary_once(self) -> None:
        text = compose_text(["【来源 1】A", "【来源 2】B"])
        self.assertEqual(text.count("不可信引用数据"), 1)
        self.assertIn("2 个来源", text)
        self.assertIn("【来源 2】B", text)

    def test_package_stats_totals_text_and_images(self) -> None:
        stats = package_stats(
            [{"estimated_tokens": 100, "truncated": False}, {"estimated_tokens": 50, "truncated": True}],
            [{"message_id": "1"}],
            [
                {"image_id": "img_0001", "bytes": 1000},
                {"image_id": "unusable_0001", "bytes": None},
            ],
        )
        self.assertEqual(stats["source_count"], 2)
        self.assertEqual(stats["message_count"], 1)
        self.assertEqual(stats["image_count"], 1)
        self.assertEqual(stats["omitted_image_count"], 1)
        self.assertEqual(stats["image_bytes"], 1000)
        self.assertEqual(stats["estimated_tokens"], 150)
        self.assertTrue(stats["truncated"])

    def test_prune_packages_keeps_newest(self) -> None:
        root = self.root / "prune-root"
        for index in range(4):
            (root / f"pkg-2026010{index}-000000-000000").mkdir(parents=True, exist_ok=True)
        (root / "not-a-package").mkdir()
        removed = prune_packages(root, keep=2)
        self.assertEqual(removed, 2)
        remaining = sorted(path.name for path in root.iterdir())
        self.assertIn("not-a-package", remaining, "非资料包目录不得被删除")
        self.assertEqual(len([name for name in remaining if name.startswith("pkg-")]), 2)

    def test_package_files_rejects_path_traversal(self) -> None:
        for name in ("../etc", "pkg-../../x", "other-1", ""):
            with self.assertRaises(ValueError):
                package_files(self.root, name)

    def test_write_manifest_roundtrips_payload(self) -> None:
        payload = {"package_id": "pkg-test-0001", "sources": [], "images": []}
        write_manifest(self.package_dir, payload)
        stored = json.loads((self.package_dir / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, payload)

    def test_package_id_is_unique_and_prefixed(self) -> None:
        first = package_id()
        self.assertTrue(first.startswith("pkg-"))
        self.assertNotEqual(first, package_id())

    # ------------------------------------------------------------------ 请求校验
    def test_normalise_request_validates_sources(self) -> None:
        with self.assertRaises(ValueError):
            normalise_request({})
        with self.assertRaises(ValueError):
            normalise_request({"sources": []})
        with self.assertRaises(ValueError):
            normalise_request({"sources": [{"dataset_id": "d", "snapshot_id": "s", "session_key": "zz"}]})
        with self.assertRaises(ValueError):
            normalise_request({"sources": [{"dataset_id": "d", "snapshot_id": "s", "session_key": "a" * 16}] * 21})

    def test_source_may_omit_dataset_and_snapshot(self) -> None:
        """外部调用方（Pi Connector）只给 session_key 时，企业与快照应以绑定为准。"""
        plan = normalise_request({"sources": [{"session_key": "b" * 16}]})
        self.assertEqual(plan["sources"][0]["dataset_id"], "")
        self.assertEqual(plan["sources"][0]["snapshot_id"], "")
        self.assertEqual(plan["sources"][0]["session_key"], "b" * 16)

    def test_normalise_request_applies_defaults_and_clamps(self) -> None:
        plan = normalise_request(
            {
                "sources": [{"dataset_id": "d", "snapshot_id": "s", "session_key": "a" * 16, "limit": 9999}],
                "image_options": {"max_images": 999, "max_total_bytes": 1},
                "max_context_tokens": 5,
            }
        )
        self.assertEqual(plan["sources"][0]["limit"], 500)
        self.assertEqual(plan["max_images"], 32)
        self.assertEqual(plan["max_total_bytes"], 1)
        self.assertEqual(plan["max_context_tokens"], 100)
        self.assertFalse(plan["sources"][0]["include_images"])


if __name__ == "__main__":
    unittest.main()
