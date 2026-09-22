from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidecar.wecom_context_core.snapshots import preflight
from sidecar.wecom_context_core.vault_client import VaultError


def make_dataset(root: Path) -> Path:
    """构造一个带 message.db 的数据集目录；页面内容用 4KB 零填充（可读、必然校验失败）。"""
    dataset = root / "dataset"
    message_dir = dataset / "db"
    message_dir.mkdir(parents=True)
    (message_dir / "message.db").write_bytes(b"\x00" * 4096)
    return dataset


def write_key(private: Path, name: str, dataset_id: str) -> None:
    private.mkdir(parents=True, exist_ok=True)
    (private / name).write_text(json.dumps({
        "dataset_id": dataset_id,
        "global_key": "00" * 16,
        "validated_databases": ["db1"],
    }), encoding="utf-8")


class FakeClient:
    """preflight 只用 status/data_dir/latest_snapshot，用最小桩替代 VaultClient。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir

    def status(self):
        return {"dataset_id": "ds", "database_count": 3}

    def latest_snapshot(self):
        return None


class KeySemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wecom-key-semantics-")
        self.root = Path(self._tmp.name)
        self.vault_root = self.root / "vault"
        self.private = self.vault_root / "private"
        self.dataset = make_dataset(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def client(self):
        return FakeClient(self.dataset)

    def test_no_key_files_reports_key_unavailable(self) -> None:
        with self.assertRaises(VaultError) as ctx:
            preflight(self.vault_root, self.client())
        self.assertEqual(ctx.exception.code, "KEY_UNAVAILABLE")
        self.assertIn("取钥", ctx.exception.args[0])

    def test_wrong_key_reports_mismatch_with_relogin_hint(self) -> None:
        write_key(self.private, "keys-other.json", "other-dataset")
        with self.assertRaises(VaultError) as ctx:
            preflight(self.vault_root, self.client())
        self.assertEqual(ctx.exception.code, "KEY_DATASET_MISMATCH")
        self.assertIn("重新取钥", ctx.exception.args[0])

    def test_matching_key_passes_and_reports_matched_metadata(self) -> None:
        write_key(self.private, "keys-a.json", "matched-ds")
        with patch("wecom_common.verify_key", return_value=True), patch("wecom_crypto.PAGE_SIZE", 4096):
            result = preflight(self.vault_root, self.client())
        self.assertEqual(result["key"], {"dataset_id": "matched-ds", "validated_database_count": 1})
        self.assertIsNone(result["previous_snapshot"])

    def test_unreadable_message_db_reports_read_blocked(self) -> None:
        write_key(self.private, "keys-a.json", "matched-ds")
        (self.dataset / "db" / "message.db").chmod(0o000)
        try:
            with self.assertRaises(VaultError) as ctx:
                preflight(self.vault_root, self.client())
            self.assertEqual(ctx.exception.code, "KEY_DATASET_READ_BLOCKED")
            self.assertTrue(ctx.exception.retryable)
        finally:
            (self.dataset / "db" / "message.db").chmod(0o644)

    def test_missing_message_db_reports_dataset_unavailable(self) -> None:
        write_key(self.private, "keys-a.json", "matched-ds")
        (self.dataset / "db" / "message.db").unlink()
        with self.assertRaises(VaultError) as ctx:
            preflight(self.vault_root, self.client())
        self.assertEqual(ctx.exception.code, "DATASET_UNAVAILABLE")

    def test_load_matching_key_stays_compatible(self) -> None:
        from sidecar.wecom_context_core.vault_runtime import (
            KEY_NO_KEYS,
            KEY_NO_MATCH,
            load_matching_key,
            load_matching_key_detailed,
        )
        write_key(self.private, "keys-a.json", "matched-ds")
        outcome, matched = load_matching_key_detailed(self.dataset, self.private)
        self.assertEqual(outcome, KEY_NO_MATCH)
        self.assertIsNone(matched)
        self.assertIsNone(load_matching_key(self.dataset))
        self.assertEqual(load_matching_key_detailed(self.dataset, self.private.parent / "nope")[0], KEY_NO_KEYS)


if __name__ == "__main__":
    unittest.main()
