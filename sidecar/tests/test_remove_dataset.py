from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidecar.wecom_context_core import protocol
from sidecar.wecom_context_core.config import ignored_dataset_ids, load_config, save_config_atomic
from sidecar.wecom_context_core.vault_client import VaultError


class RemoveDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wecom-remove-ds-")
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        (self.vault / "snapshots").mkdir(parents=True)
        (self.vault / "private").mkdir(parents=True)
        self.config_path = self.root / "config.json"
        save_config_atomic(self.config_path, {
            "configVersion": 2,
            "vault_root": str(self.vault),
            "selected_dataset_id": "target000001",
            "data_dir": str(self.root / "data" / "target"),
            "activeSnapshotPath": str(self.vault / "snapshots" / "20260101-000000-000000-target000001"),
            "session_bindings": {
                "target000001:aaaaaaaaaaaaaaaa": {"session_key": "aaaaaaaaaaaaaaaa", "dataset_id": "target000001"},
                "other0000002:bbbbbbbbbbbbbbbb": {"session_key": "bbbbbbbbbbbbbbbb", "dataset_id": "other0000002"},
            },
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _snapshot(self, name: str, dataset_id: str) -> Path:
        path = self.vault / "snapshots" / name
        path.mkdir(parents=True)
        (path / "manifest.json").write_text(json.dumps({"dataset_id": dataset_id, "contains_plaintext_wecom_data": True}), encoding="utf-8")
        return path

    def _key(self, name: str, dataset_id: str) -> Path:
        path = self.vault / "private" / name
        path.write_text(json.dumps({"dataset_id": dataset_id, "global_key": "00" * 16, "validated_databases": ["db1"]}), encoding="utf-8")
        return path

    def test_remove_dataset_deletes_own_artifacts_without_hiding_source(self) -> None:
        own = self._snapshot("20260101-000000-000000-target000001", "target000001")
        other = self._snapshot("20260101-000000-000000-other0000002", "other0000002")
        own_key = self._key("keys-own.json", "target000001")
        other_key = self._key("keys-other.json", "other0000002")
        with patch.object(protocol, "discover_dataset_paths", return_value=[]), \
             patch.object(protocol, "_key_unlocks", return_value=False):
            result = protocol.remove_dataset_action(self.config_path, load_config(self.config_path), "target000001")
        config = load_config(self.config_path)
        self.assertTrue(result["removed"])
        self.assertEqual(result["deleted_snapshots"], 1)
        self.assertEqual(result["deleted_keys"], 1)
        self.assertFalse(own.exists())
        self.assertTrue(other.exists())
        self.assertFalse(own_key.exists())
        self.assertTrue(other_key.exists())
        self.assertEqual(ignored_dataset_ids(config), [])
        self.assertNotIn("selected_dataset_id", config)
        self.assertEqual(list(config.get("session_bindings", {})), ["other0000002:bbbbbbbbbbbbbbbb"])

    def test_key_shared_with_other_dataset_is_kept(self) -> None:
        self._snapshot("20260101-000000-000000-target000001", "target000001")
        shared_key = self._key("keys-shared.json", "target000001")
        sibling = self.root / "data" / "sibling"
        sibling.mkdir(parents=True)
        with patch.object(protocol, "discover_dataset_paths", return_value=[sibling]), \
             patch.object(protocol, "runtime_dataset_id", return_value="sibling00001"), \
             patch.object(protocol, "_key_unlocks", return_value=True):
            result = protocol.remove_dataset_action(self.config_path, load_config(self.config_path), "target000001")
        self.assertEqual(result["deleted_keys"], 0)
        self.assertTrue(shared_key.exists())

    def test_restore_dataset_keeps_source_visible_without_hidden_state(self) -> None:
        with patch.object(protocol, "discover_dataset_paths", return_value=[]):
            protocol.remove_dataset_action(self.config_path, load_config(self.config_path), "target000001")
            self.assertEqual(ignored_dataset_ids(load_config(self.config_path)), [])
            restored = protocol.restore_dataset_action(self.config_path, load_config(self.config_path), "target000001")
        self.assertTrue(restored["restored"])
        self.assertEqual(ignored_dataset_ids(load_config(self.config_path)), [])

    def test_remove_requires_dataset_id(self) -> None:
        with self.assertRaises(VaultError):
            protocol.remove_dataset_action(self.config_path, load_config(self.config_path), "")


if __name__ == "__main__":
    unittest.main()
