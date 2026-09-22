from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sidecar.wecom_context_core.config import CONFIG_VERSION, binding_key, load_config, migrate_config
from sidecar.wecom_context_core.protocol import (
    allow_session_action,
    bind_session_action,
    bootstrap_action,
    clear_session_action,
    dispatch,
    download_images_action,
    prepare_context_action,
    read_context_action,
    refresh_action,
    select_dataset_action,
    select_session_action,
    session_key,
    sessions_action,
    status_without_dataset,
)
from sidecar.wecom_context_core.vault_client import VaultClient, VaultError

CONVERSATION_SINGLE = "S:100_200"
CONVERSATION_GROUP = "R:300"
CONVERSATION_GONE = "S:900_901"
HISTORY_ONLY = "S:800_801"


def now_iso(offset_minutes: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=offset_minutes)
    return moment.isoformat(timespec="seconds")


def conversation(conversation_id: str, name: str, kind: str = "单聊", last_message_time: int = 1) -> dict:
    return {
        "conversation_id": conversation_id,
        "display_name": name,
        "kind": kind,
        "last_message_time": last_message_time,
    }


class FakeVault:
    """VaultClient 的测试替身：真实目录快照 + 内存会话/历史，接口与 sidecar 依赖一致。"""

    def __init__(self, root: Path, dataset_id: str = "ds-1") -> None:
        self.root = root
        self.snapshots_dir = root
        self.dataset_id = dataset_id
        self.snapshots: dict[str, dict] = {}
        self.active: str | None = None
        self.history_calls: list[tuple[str, str]] = []
        self.messages: dict[str, list[dict]] = {}
        self.on_decrypt = None
        self.decrypt_calls = 0

    def add_snapshot(self, name: str, created_at: str, sessions: list[dict] | None = None, dataset_id: str | None = None, activate: bool = False) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "manifest.json").write_text(
            json.dumps(
                {
                    "dataset_id": dataset_id or self.dataset_id,
                    "created_at": created_at,
                    "contains_plaintext_wecom_data": True,
                }
            ),
            encoding="utf-8",
        )
        self.snapshots[name] = {
            "path": path,
            "created_at": created_at,
            "sessions": [dict(item) for item in (sessions or [])],
        }
        if activate or self.active is None:
            self.active = name
        return path

    # --- VaultClient 接口 ---
    def latest_snapshot(self) -> Path | None:
        return self.snapshots[self.active]["path"] if self.active else None

    def sessions(self, limit: int = 50) -> dict:
        return self.sessions_at_snapshot(self.latest_snapshot(), limit)

    def sessions_at_snapshot(self, snapshot: Path, limit: int = 50) -> dict:
        entry = self.snapshots.get(Path(snapshot).name)
        if entry is None:
            raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照")
        items = entry["sessions"][:limit]
        return {"count": len(items), "sessions": items}

    def history_at_snapshot(self, snapshot: Path, conversation_id: str, limit: int = 30, start=None, end=None) -> dict:
        self.history_calls.append((Path(snapshot).name, conversation_id))
        return {
            "session": {"display_name": f"会话-{conversation_id}"},
            "count": len(self.messages.get(conversation_id, [])),
            "messages": self.messages.get(conversation_id, []),
        }

    def all_messages_at_snapshot(self, snapshot: Path, conversation_id: str) -> dict:
        return self.history_at_snapshot(snapshot, conversation_id)

    def history(self, conversation_id: str, limit: int = 30, start=None, end=None) -> dict:
        return self.history_at_snapshot(self.latest_snapshot(), conversation_id, limit, start, end)

    def status(self) -> dict:
        return {"dataset_id": self.dataset_id, "database_count": 19, "formats": {}}

    def decrypt(self) -> dict:
        self.decrypt_calls += 1
        if self.on_decrypt is not None:
            self.on_decrypt()
        return {"snapshot": str(self.latest_snapshot()), "decrypted": 19, "not_decrypted": 0}


class ProtocolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.config_path = self.root / "config.json"

    def write_config(self, config: dict) -> dict:
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return config

    def read_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def v2_config(self, **extra) -> dict:
        config = {"configVersion": CONFIG_VERSION, "data_dir": str(self.root / "Data"), "selected_dataset_id": "ds-1"}
        config.update(extra)
        return config


class DownloadImagesTests(ProtocolTestCase):
    def test_download_is_idempotent_and_keeps_unavailable_report(self) -> None:
        vault = FakeVault(self.root)
        vault.add_snapshot("snap-1", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        vault.messages[CONVERSATION_SINGLE] = [
            {"message_id": 1, "time": "2026-09-20 10:00:00", "sender": "甲"},
        ]
        key = "a" * 32
        source = self.root / "source.png"
        source.write_bytes(b"image-bytes")
        resolved = SimpleNamespace(
            status="original",
            reason=None,
            path=source,
            mime_type="image/png",
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            size_bytes=source.stat().st_size,
        )
        config = self.v2_config()
        request = {
            "dataset_id": "ds-1",
            "snapshot_id": "snap-1",
            "session_key": session_key(CONVERSATION_SINGLE),
        }
        with patch(
            "sidecar.wecom_context_core.protocol.image_keys_for_messages",
            return_value={"1": [{"key": key, "size": source.stat().st_size, "file_index": 0}]},
        ), patch(
            "sidecar.wecom_context_core.protocol.resolve_image",
            return_value=resolved,
        ), patch(
            "sidecar.wecom_context_core.protocol.cache_roots",
            return_value=[],
        ) as roots:
            first = download_images_action(
                self.config_path,
                config,
                vault,
                request,
                downloads_root=self.root / "downloads",
            )
            second = download_images_action(
                self.config_path,
                config,
                vault,
                request,
                downloads_root=self.root / "downloads",
            )
        self.assertEqual(roots.call_count, 2)
        self.assertEqual(first["downloaded"], 1)
        self.assertEqual(first["skipped_existing"], 0)
        self.assertEqual(second["downloaded"], 0)
        self.assertEqual(second["skipped_existing"], 1)
        directory = Path(first["directory"])
        self.assertTrue((directory / "image_0001.png").is_file())
        self.assertTrue((directory / "download-manifest.json").is_file())
    def test_download_can_refill_missing_cache_through_client(self) -> None:
        vault = FakeVault(self.root)
        vault.add_snapshot("snap-1", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        vault.messages[CONVERSATION_SINGLE] = [
            {"message_id": 1, "time": "2026-09-20 10:00:00", "sender": "甲"},
        ]
        key = "b" * 32
        source = self.root / "source.png"
        source.write_bytes(b"refilled-image")
        resolved = SimpleNamespace(
            status="original",
            reason=None,
            path=source,
            mime_type="image/png",
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            size_bytes=source.stat().st_size,
        )
        config = self.v2_config()
        request = {
            "dataset_id": "ds-1",
            "snapshot_id": "snap-1",
            "session_key": session_key(CONVERSATION_SINGLE),
            "fetch_via_client": True,
        }
        with patch(
            "sidecar.wecom_context_core.protocol.image_keys_for_messages",
            return_value={"1": [{"key": key, "size": source.stat().st_size, "file_index": 0}]},
        ), patch(
            "sidecar.wecom_context_core.protocol.resolve_image",
            side_effect=[SimpleNamespace(status="missing", reason="本地缓存不存在", path=None), resolved],
        ), patch(
            "sidecar.wecom_context_core.protocol.cache_roots",
            return_value=[],
        ), patch(
            "sidecar.wecom_context_core.protocol.fetch_images_via_client",
            return_value={
                "attempted": True,
                "requested": 1,
                "fetched_keys": [key],
                "missing": [],
                "error_code": None,
                "error": None,
            },
        ) as fetch:
            result = download_images_action(
                self.config_path,
                config,
                vault,
                request,
                downloads_root=self.root / "downloads",
            )
        fetch.assert_called_once()
        self.assertEqual(result["client_fetched"], 1)
        self.assertEqual(result["client_fetch_missing"], 0)
        self.assertIsNone(result["client_fetch_error"])
        self.assertEqual(result["downloaded"], 1)


class BootstrapTests(ProtocolTestCase):
    def dataset(self, dataset_id: str, minutes: float | None, key: bool = True, active: bool = False) -> dict:
        return {
            "dataset_id": dataset_id,
            "kind": "current",
            "display_name": f"企业-{dataset_id}",
            "database_count": 19,
            "encrypted_database_count": 19,
            "wal_count": 0,
            "key_available": key,
            "recent_write_minutes": minutes,
            "active": active,
        }

    def run_bootstrap(self, datasets, vault=None, config=None, scan_deferred=False, auto_refresh=True, refresh_error=None):
        refresh_calls: list = []

        def fake_refresh(*args, **kwargs):
            refresh_calls.append(args)
            if refresh_error is not None:
                raise refresh_error
            if vault is not None and vault.on_decrypt is not None:
                vault.on_decrypt()
            return {"created": True}

        stored = self.write_config(config) if config is not None else {}
        with patch(
            "sidecar.wecom_context_core.protocol.discover_datasets_action",
            return_value={"count": len(datasets), "selected_dataset_id": None, "datasets": datasets, "scan_deferred": scan_deferred},
        ), patch("sidecar.wecom_context_core.protocol.make_client", return_value=vault), patch(
            "sidecar.wecom_context_core.protocol.select_dataset_action"
        ) as select, patch("sidecar.wecom_context_core.protocol.refresh_action", side_effect=fake_refresh) as refresh:
            result = bootstrap_action(self.config_path, stored, {"auto_refresh": auto_refresh})
        return result, select, refresh, refresh_calls

    def run_bootstrap_real(self, datasets, paths: dict[str, Path], vault, config=None):
        """跑真实 select_dataset_action（只 mock 数据集路径解析与 client 构造）。"""

        def runtime_id(path: Path) -> str:
            for dataset_id, candidate in paths.items():
                if Path(path) == candidate:
                    return dataset_id
            raise AssertionError(f"unexpected dataset path: {path}")

        stored = self.write_config(config) if config is not None else {}
        with patch(
            "sidecar.wecom_context_core.protocol.discover_datasets_action",
            return_value={"count": len(datasets), "selected_dataset_id": None, "datasets": datasets, "scan_deferred": False},
        ), patch("sidecar.wecom_context_core.protocol.discover_dataset_paths", return_value=list(paths.values())), patch(
            "sidecar.wecom_context_core.protocol.runtime_dataset_id", side_effect=runtime_id
        ), patch(
            "sidecar.wecom_context_core.protocol.dataset_presentation", return_value=("current", "当前数据")
        ), patch(
            "sidecar.wecom_context_core.protocol.enterprise_name_for", return_value=None
        ), patch("sidecar.wecom_context_core.protocol.make_client", return_value=vault):
            return bootstrap_action(self.config_path, stored, {"auto_refresh": True})

    def test_bootstrap_without_config_multiple_enterprises_needs_dataset(self) -> None:
        datasets = [self.dataset("ds-a", 3.0, active=True), self.dataset("ds-b", 5.0)]
        result, select, refresh, _ = self.run_bootstrap(datasets)
        self.assertEqual(result["readiness"], "needs_dataset")
        self.assertIsNone(result["dataset"])
        self.assertIsNone(result["snapshot"])
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["selection_reason"], "ambiguous")
        self.assertIn("DATASET_AMBIGUOUS", [item["code"] for item in result["warnings"]])
        select.assert_not_called()
        refresh.assert_not_called()

    def test_bootstrap_without_datasets_needs_dataset(self) -> None:
        result, _, _, _ = self.run_bootstrap([])
        self.assertEqual(result["readiness"], "needs_dataset")
        self.assertEqual(result["selection_reason"], "none")
        self.assertIn("NO_DATASET", [item["code"] for item in result["warnings"]])

    def test_bootstrap_single_dataset_is_adopted_then_refreshed(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        vault.on_decrypt = lambda: vault.add_snapshot(
            "snap-new",
            now_iso(),
            sessions=[conversation(CONVERSATION_SINGLE, "甲"), conversation(CONVERSATION_GROUP, "群", "群聊")],
        )
        result, select, refresh, _ = self.run_bootstrap([self.dataset("ds-a", 1.0, active=True)], vault=vault)
        self.assertEqual(result["readiness"], "ready")
        self.assertEqual(result["selection_reason"], "single_dataset")
        self.assertEqual(result["dataset"]["dataset_id"], "ds-a")
        self.assertTrue(result["dataset"]["key_available"])
        self.assertEqual(result["snapshot"]["snapshot_id"], "snap-new")
        self.assertTrue(result["snapshot"]["refreshed"])
        self.assertFalse(result["snapshot"]["degraded"])
        self.assertEqual([item["conversation_id"] for item in result["sessions"]], [CONVERSATION_SINGLE, CONVERSATION_GROUP])
        self.assertEqual(result["sessions"][0]["dataset_id"], "ds-a")
        self.assertEqual(result["sessions"][0]["snapshot_id"], "snap-new")
        select.assert_called_once()
        refresh.assert_called_once()

    def test_bootstrap_fresh_snapshot_skips_refresh(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        vault.add_snapshot("snap-fresh", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        result, _, refresh, _ = self.run_bootstrap(
            [self.dataset("ds-a", 1.0, active=True)], vault=vault, config=self.v2_config(selected_dataset_id="ds-a")
        )
        self.assertEqual(result["readiness"], "ready")
        self.assertEqual(result["selection_reason"], "kept")
        self.assertFalse(result["snapshot"]["refreshed"])
        self.assertFalse(result["snapshot"]["degraded"])
        self.assertEqual(len(result["sessions"]), 1)
        refresh.assert_not_called()

    def test_bootstrap_stale_snapshot_triggers_refresh(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        vault.add_snapshot("snap-old", now_iso(45.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        vault.on_decrypt = lambda: vault.add_snapshot(
            "snap-new", now_iso(), sessions=[conversation(CONVERSATION_SINGLE, "甲")], activate=True
        )
        result, _, refresh, _ = self.run_bootstrap(
            [self.dataset("ds-a", 45.0, active=True)], vault=vault, config=self.v2_config(selected_dataset_id="ds-a")
        )
        self.assertEqual(result["readiness"], "ready")
        self.assertTrue(result["snapshot"]["refreshed"])
        self.assertEqual(result["snapshot"]["snapshot_id"], "snap-new")
        refresh.assert_called_once()

    def test_bootstrap_refreshs_when_source_database_newer_than_snapshot(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        vault.add_snapshot("snap-old", now_iso(9.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        vault.on_decrypt = lambda: vault.add_snapshot(
            "snap-new", now_iso(), sessions=[conversation(CONVERSATION_SINGLE, "甲")], activate=True
        )
        result, _, refresh, _ = self.run_bootstrap(
            [self.dataset("ds-a", 1.0, active=True)], vault=vault, config=self.v2_config(selected_dataset_id="ds-a")
        )
        self.assertTrue(result["snapshot"]["refreshed"])
        self.assertEqual(result["snapshot"]["snapshot_id"], "snap-new")
        refresh.assert_called_once()

    def test_bootstrap_refresh_failure_keeps_valid_snapshot_degraded(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        vault.add_snapshot("snap-old", now_iso(45.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        result, _, _, _ = self.run_bootstrap(
            [self.dataset("ds-a", 45.0, active=True)],
            vault=vault,
            config=self.v2_config(selected_dataset_id="ds-a"),
            refresh_error=VaultError("REFRESH_FAILED", "解密失败", True),
        )
        self.assertEqual(result["readiness"], "ready")
        self.assertTrue(result["snapshot"]["degraded"])
        self.assertFalse(result["snapshot"]["refreshed"])
        self.assertEqual(result["snapshot"]["snapshot_id"], "snap-old")
        self.assertIn("REFRESH_FAILED", [item["code"] for item in result["warnings"]])
        self.assertEqual(len(result["sessions"]), 1)

    def test_bootstrap_refresh_failure_without_snapshot_reports_refresh_failed(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        result, _, _, _ = self.run_bootstrap(
            [self.dataset("ds-a", 1.0, active=True)],
            vault=vault,
            config=self.v2_config(selected_dataset_id="ds-a"),
            refresh_error=VaultError("REFRESH_FAILED", "解密失败", True),
        )
        self.assertEqual(result["readiness"], "refresh_failed")
        self.assertIsNone(result["snapshot"])
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["dataset"]["dataset_id"], "ds-a")

    def test_bootstrap_scan_deferred_keeps_selected_dataset(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        vault.add_snapshot("snap-fresh", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        result, select, refresh, _ = self.run_bootstrap(
            [self.dataset("ds-a", 400.0), self.dataset("ds-b", 1.0, active=True)],
            vault=vault,
            config=self.v2_config(selected_dataset_id="ds-a"),
            scan_deferred=True,
        )
        self.assertEqual(result["dataset"]["dataset_id"], "ds-a")
        self.assertEqual(result["selection_reason"], "kept")
        self.assertIn("SCAN_DEFERRED", [item["code"] for item in result["warnings"]])
        select.assert_not_called()
        refresh.assert_not_called()

    def test_bootstrap_scan_deferred_without_selection_needs_dataset(self) -> None:
        result, _, _, _ = self.run_bootstrap([self.dataset("ds-a", 1.0), self.dataset("ds-b", 2.0)], scan_deferred=True)
        self.assertEqual(result["readiness"], "needs_dataset")
        self.assertEqual(result["selection_reason"], "ambiguous")
        self.assertIn("SCAN_DEFERRED", [item["code"] for item in result["warnings"]])

    def test_bootstrap_auto_adopts_live_enterprise(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-b")
        vault.add_snapshot("snap-b", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        result, select, _, _ = self.run_bootstrap(
            [self.dataset("ds-a", 400.0), self.dataset("ds-b", 1.0, active=True)],
            vault=vault,
            config=self.v2_config(selected_dataset_id="ds-a"),
        )
        self.assertEqual(result["dataset"]["dataset_id"], "ds-b")
        self.assertEqual(result["selection_reason"], "auto_adopted")
        self.assertIn("ENTERPRISE_ADOPTED", [item["code"] for item in result["warnings"]])
        select.assert_called_once()

    def test_bootstrap_selected_dataset_without_key_reports_needs_key(self) -> None:
        result, _, refresh, _ = self.run_bootstrap(
            [self.dataset("ds-a", 1.0, key=False, active=True)], config=self.v2_config(selected_dataset_id="ds-a")
        )
        self.assertEqual(result["readiness"], "needs_key")
        self.assertIsNotNone(result["dataset"])
        self.assertFalse(result["dataset"]["key_available"])
        self.assertIn("KEY_MISSING", [item["code"] for item in result["warnings"]])
        refresh.assert_not_called()

    def test_bootstrap_explicit_selection_is_not_stolen_by_live_enterprise(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        vault.add_snapshot("snap-a", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        result, select, _, _ = self.run_bootstrap(
            [self.dataset("ds-a", 400.0), self.dataset("ds-b", 1.0, active=True)],
            vault=vault,
            config=self.v2_config(selected_dataset_id="ds-a", selection_mode="explicit"),
        )
        codes = [item["code"] for item in result["warnings"]]
        self.assertEqual(result["dataset"]["dataset_id"], "ds-a")
        self.assertEqual(result["selection_reason"], "kept")
        self.assertNotIn("ENTERPRISE_ADOPTED", codes)
        self.assertNotIn("ENTERPRISE_ADOPT_SKIPPED", codes)
        select.assert_not_called()
        stored = self.read_config()
        self.assertEqual(stored["selected_dataset_id"], "ds-a")
        self.assertEqual(stored["selection_mode"], "explicit")

    def test_bootstrap_auto_mode_still_adopts_live_enterprise(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-b")
        vault.add_snapshot("snap-b", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        result, select, _, _ = self.run_bootstrap(
            [self.dataset("ds-a", 400.0), self.dataset("ds-b", 1.0, active=True)],
            vault=vault,
            config=self.v2_config(selected_dataset_id="ds-a", selection_mode="auto"),
        )
        self.assertEqual(result["dataset"]["dataset_id"], "ds-b")
        self.assertEqual(result["selection_reason"], "auto_adopted")
        self.assertIn("ENTERPRISE_ADOPTED", [item["code"] for item in result["warnings"]])
        select.assert_called_once()

    def test_bootstrap_auto_adoption_marks_auto_and_does_not_thrash(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-b")
        vault.add_snapshot("snap-b", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        paths = {"ds-a": self.root / "one" / "Data", "ds-b": self.root / "two" / "Data"}
        for path in paths.values():
            path.mkdir(parents=True)
        datasets = [self.dataset("ds-a", 400.0), self.dataset("ds-b", 1.0, active=True)]

        first = self.run_bootstrap_real(datasets, paths, vault)
        self.assertEqual(first["selection_reason"], "auto_adopted")
        self.assertEqual(first["dataset"]["dataset_id"], "ds-b")
        stored = self.read_config()
        self.assertEqual(stored["selected_dataset_id"], "ds-b")
        self.assertEqual(stored["data_dir"], str(paths["ds-b"]))
        self.assertEqual(stored["selection_mode"], "auto")

        second = self.run_bootstrap_real(datasets, paths, vault, config=stored)
        self.assertEqual(second["selection_reason"], "kept")
        self.assertEqual(second["dataset"]["dataset_id"], "ds-b")
        self.assertNotIn("ENTERPRISE_ADOPTED", [item["code"] for item in second["warnings"]])
        again = self.read_config()
        self.assertEqual(again["selection_mode"], "auto")
        self.assertEqual(again["data_dir"], str(paths["ds-b"]))

    def test_bootstrap_explicit_selection_degrades_when_dataset_disappears(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        vault.add_snapshot("snap-a", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        paths = {"ds-a": self.root / "one" / "Data"}
        paths["ds-a"].mkdir(parents=True)
        result = self.run_bootstrap_real(
            [self.dataset("ds-a", 1.0, active=True)],
            paths,
            vault,
            config=self.v2_config(selected_dataset_id="ds-gone", selection_mode="explicit"),
        )
        self.assertEqual(result["dataset"]["dataset_id"], "ds-a")
        self.assertEqual(result["selection_reason"], "single_dataset")
        stored = self.read_config()
        self.assertEqual(stored["selected_dataset_id"], "ds-a")
        self.assertEqual(stored["selection_mode"], "auto")

    def test_bootstrap_single_dataset_without_key_reports_needs_key(self) -> None:
        result, select, refresh, _ = self.run_bootstrap([self.dataset("ds-a", 1.0, key=False, active=True)])
        self.assertEqual(result["readiness"], "needs_key")
        self.assertEqual(result["selection_reason"], "single_dataset")
        self.assertEqual(result["dataset"]["dataset_id"], "ds-a")
        self.assertIn("KEY_MISSING", [item["code"] for item in result["warnings"]])
        refresh.assert_not_called()

    def test_bootstrap_without_auto_refresh_reports_refresh_failed_without_snapshot(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        result, _, refresh, _ = self.run_bootstrap(
            [self.dataset("ds-a", 1.0, active=True)],
            vault=vault,
            config=self.v2_config(selected_dataset_id="ds-a"),
            auto_refresh=False,
        )
        self.assertEqual(result["readiness"], "refresh_failed")
        self.assertIn("REFRESH_SKIPPED", [item["code"] for item in result["warnings"]])
        refresh.assert_not_called()

    def test_dispatch_bootstrap_returns_protocol_envelope(self) -> None:
        vault = FakeVault(self.root, dataset_id="ds-a")
        vault.add_snapshot("snap-fresh", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        self.write_config(self.v2_config(selected_dataset_id="ds-a"))
        with patch(
            "sidecar.wecom_context_core.protocol.discover_datasets_action",
            return_value={"count": 1, "selected_dataset_id": "ds-a", "datasets": [self.dataset("ds-a", 1.0, active=True)], "scan_deferred": False},
        ), patch("sidecar.wecom_context_core.protocol.make_client", return_value=vault):
            response = dispatch({"action": "bootstrap", "request_id": "r-1", "auto_refresh": True}, self.config_path)
        self.assertEqual(response["protocol_version"], "1")
        self.assertEqual(response["request_id"], "r-1")
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["readiness"], "ready")


class BindSessionTests(ProtocolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.vault = FakeVault(self.root)
        self.vault.add_snapshot("snap-1", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲"), conversation(CONVERSATION_GROUP, "群", "群聊")])

    def test_bind_session_writes_active_binding_without_touching_global_default(self) -> None:
        key = session_key(CONVERSATION_SINGLE)
        config = self.v2_config(selected_session_key="other-key")
        first = bind_session_action(self.config_path, config, self.vault, session_key_value=key)
        self.assertTrue(first["bound"])
        self.assertEqual(first["mode"], "active")
        self.assertEqual(first["recovered"], "rebuilt_mapping")
        self.assertEqual(first["snapshot_id"], "snap-1")
        self.assertIsNone(first["history_snapshot_id"])
        self.assertEqual(first["dataset_id"], "ds-1")
        self.assertEqual(first["conversation_id"], CONVERSATION_SINGLE)

        again = bind_session_action(self.config_path, self.read_config(), self.vault, session_key_value=key)
        self.assertEqual(again["recovered"], "none")

        updated = self.read_config()
        binding = updated["session_bindings"][binding_key("ds-1", key)]
        self.assertEqual(binding["conversation_id"], CONVERSATION_SINGLE)
        self.assertEqual(binding["mode"], "active")
        self.assertEqual(binding["snapshot_id"], "snap-1")
        self.assertEqual(updated["selected_session_key"], "other-key")

    def test_bind_session_rejects_dataset_change(self) -> None:
        with self.assertRaises(VaultError) as context:
            bind_session_action(self.config_path, self.v2_config(), self.vault, session_key_value=session_key(CONVERSATION_SINGLE), dataset_id_value="ds-old")
        self.assertEqual(context.exception.code, "DATASET_CHANGED")
        self.assertTrue(context.exception.retryable)

    def test_bind_session_rejects_snapshot_change(self) -> None:
        with self.assertRaises(VaultError) as context:
            bind_session_action(self.config_path, self.v2_config(), self.vault, session_key_value=session_key(CONVERSATION_SINGLE), snapshot_id_value="snap-old")
        self.assertEqual(context.exception.code, "SNAPSHOT_CHANGED")
        self.assertTrue(context.exception.retryable)

    def test_bind_session_recovers_from_conversation_id(self) -> None:
        key = session_key(CONVERSATION_GROUP)
        authoritative = bind_session_action(
            self.config_path,
            self.v2_config(),
            self.vault,
            session_key_value="deadbeefdeadbeef",
            conversation_id_value=CONVERSATION_GROUP,
        )
        self.assertEqual(authoritative["recovered"], "from_conversation_id")
        self.assertEqual(authoritative["session_key"], key)

    def test_bind_session_reports_gone_with_history_candidates(self) -> None:
        self.vault.add_snapshot("snap-0", now_iso(120.0), sessions=[conversation(CONVERSATION_GONE, "旧会话")])
        with self.assertRaises(VaultError) as context:
            bind_session_action(
                self.config_path,
                self.v2_config(),
                self.vault,
                session_key_value=session_key(CONVERSATION_GONE),
                conversation_id_value=CONVERSATION_GONE,
            )
        error = context.exception
        self.assertEqual(error.code, "SESSION_GONE")
        self.assertTrue(error.retryable)
        self.assertEqual([item["snapshot_id"] for item in error.details["history_candidates"]], ["snap-0"])

    def test_bind_session_gone_without_history_reports_empty_candidates(self) -> None:
        with self.assertRaises(VaultError) as context:
            bind_session_action(self.config_path, self.v2_config(), self.vault, session_key_value="deadbeefdeadbeef")
        self.assertEqual(context.exception.code, "SESSION_GONE")
        self.assertEqual(context.exception.details["history_candidates"], [])

    def test_bind_session_allow_history_pins_history_snapshot(self) -> None:
        self.vault.add_snapshot("snap-0", now_iso(120.0), sessions=[conversation(CONVERSATION_GONE, "旧会话")])
        key = session_key(CONVERSATION_GONE)
        result = bind_session_action(
            self.config_path,
            self.v2_config(),
            self.vault,
            session_key_value=key,
            conversation_id_value=CONVERSATION_GONE,
            allow_history=True,
        )
        self.assertEqual(result["mode"], "history")
        self.assertEqual(result["recovered"], "history_snapshot")
        self.assertEqual(result["snapshot_id"], "snap-0")
        self.assertEqual(result["history_snapshot_id"], "snap-0")
        manifest = json.loads((self.root / "snap-0" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result["history_created_at"], manifest["created_at"])
        binding = self.read_config()["session_bindings"][binding_key("ds-1", key)]
        self.assertEqual(binding["mode"], "history")
        self.assertEqual(binding["snapshot_id"], "snap-0")
        self.assertNotIn("selected_session_key", self.read_config())

    def test_select_session_sets_global_default_and_reports_selected(self) -> None:
        key = session_key(CONVERSATION_SINGLE)
        result = select_session_action(self.config_path, self.v2_config(), self.vault, key)
        self.assertTrue(result["selected"])
        self.assertTrue(result["bound"])
        self.assertEqual(result["recovered"], "rebuilt_mapping")
        self.assertEqual(self.read_config()["selected_session_key"], key)


class ReadContextBindingTests(ProtocolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.vault = FakeVault(self.root)
        self.vault.add_snapshot("snap-1", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲")])
        self.vault.add_snapshot("snap-0", now_iso(120.0), sessions=[conversation(CONVERSATION_GONE, "旧会话")])

    def test_read_context_requires_binding_and_never_falls_back_to_global_selection(self) -> None:
        key = session_key(CONVERSATION_SINGLE)
        config = self.v2_config(selected_session_key=key)
        with self.assertRaises(VaultError) as context:
            read_context_action(self.config_path, config, self.vault, {"session_key": key})
        self.assertEqual(context.exception.code, "SESSION_NOT_ALLOWED")
        with self.assertRaises(VaultError) as missing:
            read_context_action(self.config_path, config, self.vault, {})
        self.assertEqual(missing.exception.code, "INVALID_REQUEST")

    def test_prepare_context_accepts_explicit_source_without_persistent_binding(self) -> None:
        key = session_key(CONVERSATION_SINGLE)
        config = self.v2_config()
        self.write_config(config)
        before = self.config_path.read_text(encoding="utf-8")
        result = prepare_context_action(
            self.config_path,
            config,
            self.vault,
            {
                "sources": [
                    {
                        "dataset_id": "ds-1",
                        "snapshot_id": "snap-1",
                        "session_key": key,
                        "limit": 30,
                        "include_images": False,
                    }
                ],
                "max_context_tokens": 2500,
                "max_message_characters": 2000,
                "image_options": {"max_images": 0},
            },
            packages_root=self.root / "packages",
        )
        self.assertEqual(result["sources"][0]["session_key"], key)
        self.assertEqual(self.vault.history_calls[-1], ("snap-1", CONVERSATION_SINGLE))
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertNotIn("session_bindings", self.read_config())

    def test_read_context_uses_bound_snapshot_and_rejects_identity_mismatch(self) -> None:
        key = session_key(CONVERSATION_SINGLE)
        bind_session_action(self.config_path, self.v2_config(), self.vault, session_key_value=key)
        config = self.read_config()
        result = read_context_action(self.config_path, config, self.vault, {"session_key": key, "dataset_id": "ds-1", "snapshot_id": "snap-1"})
        self.assertEqual(result["session_key"], key)
        self.assertEqual(self.vault.history_calls[-1], ("snap-1", CONVERSATION_SINGLE))
        with self.assertRaises(VaultError) as dataset_changed:
            read_context_action(self.config_path, config, self.vault, {"session_key": key, "dataset_id": "ds-2"})
        self.assertEqual(dataset_changed.exception.code, "DATASET_CHANGED")
        with self.assertRaises(VaultError) as snapshot_changed:
            read_context_action(self.config_path, config, self.vault, {"session_key": key, "snapshot_id": "snap-0"})
        self.assertEqual(snapshot_changed.exception.code, "SNAPSHOT_CHANGED")

    def test_read_context_keeps_active_and_history_bindings_on_separate_snapshots(self) -> None:
        active_key = session_key(CONVERSATION_SINGLE)
        history_key = session_key(CONVERSATION_GONE)
        bind_session_action(self.config_path, self.v2_config(), self.vault, session_key_value=active_key)
        bind_session_action(
            self.config_path,
            self.read_config(),
            self.vault,
            session_key_value=history_key,
            conversation_id_value=CONVERSATION_GONE,
            allow_history=True,
        )
        config = self.read_config()
        self.assertEqual(config["session_bindings"][binding_key("ds-1", active_key)]["snapshot_id"], "snap-1")
        self.assertEqual(config["session_bindings"][binding_key("ds-1", history_key)]["snapshot_id"], "snap-0")

        active = read_context_action(self.config_path, config, self.vault, {"session_key": active_key})
        history = read_context_action(self.config_path, config, self.vault, {"session_key": history_key})
        self.assertEqual(self.vault.history_calls, [("snap-1", CONVERSATION_SINGLE), ("snap-0", CONVERSATION_GONE)])
        self.assertNotIn("read_only_history", active["details"])
        self.assertTrue(history["details"]["read_only_history"])
        self.assertEqual(history["details"]["history_snapshot_id"], "snap-0")
        self.assertNotEqual(active["details"]["snapshot_created_at"], history["details"]["snapshot_created_at"])

        again = read_context_action(self.config_path, config, self.vault, {"session_key": active_key})
        self.assertEqual(self.vault.history_calls[-1], ("snap-1", CONVERSATION_SINGLE))
        self.assertTrue(again["content"].startswith("以下内容是用户授权读取的企业微信历史记录"))

    def test_read_context_reports_gone_when_history_snapshot_lost_session(self) -> None:
        key = session_key(CONVERSATION_GONE)
        bind_session_action(
            self.config_path,
            self.v2_config(),
            self.vault,
            session_key_value=key,
            conversation_id_value=CONVERSATION_GONE,
            allow_history=True,
        )
        config = self.read_config()
        self.vault.snapshots["snap-0"]["sessions"] = []
        with self.assertRaises(VaultError) as context:
            read_context_action(self.config_path, config, self.vault, {"session_key": key})
        self.assertEqual(context.exception.code, "SESSION_GONE")

    def test_preview_context_uses_explicit_identity_without_writing_config(self) -> None:
        key = session_key(CONVERSATION_SINGLE)
        bind_session_action(self.config_path, self.v2_config(), self.vault, session_key_value=key)
        before = self.config_path.read_text(encoding="utf-8")
        with patch("sidecar.wecom_context_core.protocol.make_client", return_value=self.vault):
            response = dispatch(
                {
                    "action": "preview_context",
                    "request_id": "p-1",
                    "session_key": key,
                    "dataset_id": "ds-1",
                    "snapshot_id": "snap-1",
                    "limit": 30,
                },
                self.config_path,
            )
        self.assertTrue(response["ok"])
        details = response["data"]
        for field in ("session_name", "snapshot_created_at", "snapshot_age_minutes", "original_message_count", "retained_message_count", "estimated_tokens", "truncated", "redactions"):
            self.assertIn(field, details)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)


class RefreshBindingTests(ProtocolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.vault = FakeVault(self.root)
        self.vault.add_snapshot("snap-old", now_iso(60.0), sessions=[conversation(CONVERSATION_SINGLE, "甲"), conversation(CONVERSATION_GROUP, "群", "群聊")])
        self.vault.add_snapshot("snap-hist", now_iso(600.0), sessions=[conversation(HISTORY_ONLY, "旧会话")])

    def base_config(self) -> dict:
        config = self.v2_config(
            snapshotMode="fixed",
            activeSnapshotPath=str(self.root / "snap-old"),
            activeSnapshotCreatedAt=now_iso(60.0),
            activeSnapshotDatasetId="ds-1",
            selected_session_key=session_key(CONVERSATION_SINGLE),
        )
        bindings = config.setdefault("session_bindings", {})
        for conversation_id in (CONVERSATION_SINGLE, CONVERSATION_GROUP, CONVERSATION_GONE):
            key = session_key(conversation_id)
            bindings[binding_key("ds-1", key)] = {
                "session_key": key,
                "conversation_id": conversation_id,
                "dataset_id": "ds-1",
                "snapshot_id": "snap-old",
                "snapshot_path": str(self.root / "snap-old"),
                "mode": "active",
                "bound_at": now_iso(5.0),
            }
        history_key = session_key(HISTORY_ONLY)
        bindings[binding_key("ds-1", history_key)] = {
            "session_key": history_key,
            "conversation_id": HISTORY_ONLY,
            "dataset_id": "ds-1",
            "snapshot_id": "snap-hist",
            "snapshot_path": str(self.root / "snap-hist"),
            "mode": "history",
            "bound_at": now_iso(5.0),
        }
        return config

    def run_refresh(self, config: dict) -> dict:
        created_at = now_iso()

        def on_decrypt() -> None:
            self.vault.add_snapshot(
                "snap-new",
                created_at,
                sessions=[conversation(CONVERSATION_SINGLE, "甲"), conversation(CONVERSATION_GROUP, "群", "群聊")],
                activate=True,
            )

        self.vault.on_decrypt = on_decrypt
        self.write_config(config)
        with patch(
            "sidecar.wecom_context_core.protocol.preflight",
            return_value={"status": {"dataset_id": "ds-1", "database_count": 19}, "key": {"validated_database_count": 19}, "previous_snapshot": str(self.root / "snap-old")},
        ), patch(
            "sidecar.wecom_context_core.protocol.validate_snapshot",
            return_value={"snapshot": str(self.root / "snap-new"), "manifest": {"created_at": created_at, "dataset_id": "ds-1"}, "session_count": 2},
        ):
            return refresh_action(self.config_path, config, self.vault, True)

    def test_refresh_preserves_live_bindings_and_invalidates_gone_ones(self) -> None:
        result = self.run_refresh(self.base_config())
        self.assertTrue(result["created"])
        self.assertEqual(result["generation"], {"dataset_id": "ds-1", "snapshot_id": "snap-new"})
        self.assertEqual(
            sorted(result["preserved_session_keys"]),
            sorted([session_key(CONVERSATION_SINGLE), session_key(CONVERSATION_GROUP), session_key(HISTORY_ONLY)]),
        )
        self.assertEqual(result["invalidated_session_keys"], [session_key(CONVERSATION_GONE)])
        self.assertEqual(result["session_binding"], "preserved")

        updated = self.read_config()
        single = updated["session_bindings"][binding_key("ds-1", session_key(CONVERSATION_SINGLE))]
        self.assertEqual(single["snapshot_id"], "snap-new")
        self.assertEqual(single["snapshot_path"], str((self.root / "snap-new").resolve()))
        self.assertEqual(single["conversation_id"], CONVERSATION_SINGLE)
        self.assertNotIn(binding_key("ds-1", session_key(CONVERSATION_GONE)), updated["session_bindings"])
        history = updated["session_bindings"][binding_key("ds-1", session_key(HISTORY_ONLY))]
        self.assertEqual(history["snapshot_id"], "snap-hist")
        self.assertEqual(history["mode"], "history")
        self.assertEqual(updated["activeSnapshotPath"], str((self.root / "snap-new").resolve()))

    def test_refresh_clears_global_selection_when_binding_disappears(self) -> None:
        config = self.base_config()
        config["selected_session_key"] = session_key(CONVERSATION_GONE)
        result = self.run_refresh(config)
        self.assertEqual(result["session_binding"], "cleared")
        self.assertNotIn("selected_session_key", self.read_config())

    def test_refresh_without_bindings_reports_none(self) -> None:
        config = self.v2_config(snapshotMode="fixed", activeSnapshotPath=str(self.root / "snap-old"), activeSnapshotDatasetId="ds-1")
        result = self.run_refresh(config)
        self.assertEqual(result["session_binding"], "none")
        self.assertEqual(result["preserved_session_keys"], [])
        self.assertEqual(result["invalidated_session_keys"], [])


class SessionsAndConfigTests(ProtocolTestCase):
    def test_sessions_action_returns_meta_without_persisting_legacy_fields(self) -> None:
        vault = FakeVault(self.root)
        vault.add_snapshot(
            "snap-1",
            now_iso(1.0),
            sessions=[conversation(CONVERSATION_SINGLE, "甲"), conversation(CONVERSATION_GROUP, "群", "群聊"), conversation("X:1", "系统会话", "其他")],
        )
        config = self.v2_config(selected_session_key=session_key(CONVERSATION_SINGLE))
        self.write_config(config)
        result = sessions_action(self.config_path, config, vault, 50)
        self.assertEqual(result["dataset_id"], "ds-1")
        self.assertEqual(result["snapshot_id"], "snap-1")
        self.assertEqual(result["snapshot_created_at"], vault.snapshots["snap-1"]["created_at"])
        self.assertEqual([item["conversation_id"] for item in result["sessions"]], [CONVERSATION_SINGLE, CONVERSATION_GROUP])
        self.assertTrue(result["sessions"][0]["selected"])
        self.assertFalse(result["sessions"][1]["selected"])
        stored = self.read_config()
        for field in ("session_key_map", "allowlisted_session_keys", "historySnapshotPath"):
            self.assertNotIn(field, stored)
        self.assertEqual(stored["configVersion"], CONFIG_VERSION)

    def test_message_time_is_formatted_as_wire_string(self) -> None:
        vault = FakeVault(self.root)
        vault.add_snapshot(
            "snap-1",
            now_iso(1.0),
            sessions=[
                {"conversation_id": CONVERSATION_SINGLE, "display_name": "甲", "kind": "单聊", "last_message_time": 1789271774},
                {"conversation_id": CONVERSATION_GROUP, "display_name": "群", "kind": "群聊", "last_message_time": 0},
            ],
        )
        result = sessions_action(self.config_path, self.v2_config(), vault, 50)
        times = {item["conversation_id"]: item["last_message_time"] for item in result["sessions"]}
        self.assertRegex(times[CONVERSATION_SINGLE], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertIsNone(times[CONVERSATION_GROUP])

    def test_sessions_action_include_all_keeps_system_sessions(self) -> None:
        vault = FakeVault(self.root)
        vault.add_snapshot(
            "snap-1",
            now_iso(1.0),
            sessions=[conversation(CONVERSATION_SINGLE, "甲"), conversation("X:1", "系统会话", "其他")],
        )
        result = sessions_action(self.config_path, self.v2_config(), vault, 50, include_all=True)
        self.assertEqual([item["conversation_id"] for item in result["sessions"]], [CONVERSATION_SINGLE, "X:1"])

    def test_allow_session_creates_active_binding_without_changing_selection(self) -> None:
        vault = FakeVault(self.root)
        vault.add_snapshot("snap-1", now_iso(1.0), sessions=[conversation(CONVERSATION_SINGLE, "甲"), conversation(CONVERSATION_GROUP, "群", "群聊")])
        selected = session_key(CONVERSATION_SINGLE)
        config = self.v2_config(selected_session_key=selected)
        result = allow_session_action(self.config_path, config, vault, session_key(CONVERSATION_GROUP))
        self.assertTrue(result["allowed"])
        stored = self.read_config()
        self.assertEqual(stored["selected_session_key"], selected)
        binding = stored["session_bindings"][binding_key("ds-1", session_key(CONVERSATION_GROUP))]
        self.assertEqual(binding["mode"], "active")
        self.assertEqual(binding["conversation_id"], CONVERSATION_GROUP)
        with self.assertRaises(VaultError) as context:
            allow_session_action(self.config_path, stored, vault, "deadbeefdeadbeef")
        self.assertEqual(context.exception.code, "SESSION_NOT_FOUND")

    def select_dataset(self, paths: dict[str, Path], dataset_id: str, config: dict) -> dict:
        def runtime_id(path: Path) -> str:
            for key, candidate in paths.items():
                if Path(path) == candidate:
                    return key
            raise AssertionError(f"unexpected dataset path: {path}")

        with patch("sidecar.wecom_context_core.protocol.discover_dataset_paths", return_value=list(paths.values())), patch(
            "sidecar.wecom_context_core.protocol.runtime_dataset_id", side_effect=runtime_id
        ), patch(
            "sidecar.wecom_context_core.protocol.dataset_presentation", return_value=("current", "当前数据")
        ), patch("sidecar.wecom_context_core.protocol.enterprise_name_for", return_value=None):
            return select_dataset_action(self.config_path, config, dataset_id)

    def test_select_dataset_keeps_only_target_enterprise_bindings(self) -> None:
        paths = {"ds-a": self.root / "one" / "Data", "ds-b": self.root / "two" / "Data"}
        for path in paths.values():
            path.mkdir(parents=True)
        bindings = {}
        for dataset_id, session_key_value in (("ds-a", "aaaa"), ("ds-a", "bbbb"), ("ds-b", "cccc")):
            bindings[binding_key(dataset_id, session_key_value)] = {
                "session_key": session_key_value,
                "conversation_id": f"S:1_{session_key_value}",
                "dataset_id": dataset_id,
                "snapshot_id": "snap-old",
                "snapshot_path": f"/snapshots/{dataset_id}",
                "mode": "active",
                "bound_at": "now",
            }
        config = self.v2_config(
            snapshotMode="fixed",
            activeSnapshotPath=str(self.root / "snap-old"),
            activeSnapshotCreatedAt="old",
            activeSnapshotDatasetId="ds-a",
            selected_session_key="cccc",
            session_bindings=bindings,
        )
        result = self.select_dataset(paths, "ds-b", config)
        updated = self.read_config()
        self.assertEqual(result["dataset_id"], "ds-b")
        self.assertEqual(sorted(updated["session_bindings"]), [binding_key("ds-b", "cccc")])
        self.assertEqual(updated["selected_session_key"], "cccc")
        self.assertEqual(updated["selection_mode"], "explicit")
        self.assertNotIn("activeSnapshotPath", updated)

        switched_away_selection = {**config, "selected_session_key": "aaaa"}
        self.select_dataset(paths, "ds-b", switched_away_selection)
        updated_two = self.read_config()
        self.assertNotIn("selected_session_key", updated_two)
        self.assertEqual(sorted(updated_two["session_bindings"]), [binding_key("ds-b", "cccc")])

    def test_select_dataset_clears_snapshot_and_session_bindings(self) -> None:
        dataset = self.root / "current" / "Data"
        dataset.mkdir(parents=True)
        config = self.v2_config(
            snapshotMode="fixed",
            activeSnapshotPath=str(self.root / "snap-old"),
            activeSnapshotCreatedAt="old",
            activeSnapshotDatasetId="old",
            selected_session_key="old-session",
            session_bindings={
                binding_key("old", "old-session"): {
                    "session_key": "old-session",
                    "conversation_id": "conversation",
                    "dataset_id": "old",
                    "snapshot_id": "snap-old",
                    "snapshot_path": "/old/snapshot",
                    "mode": "active",
                    "bound_at": "old",
                }
            },
        )
        with patch("sidecar.wecom_context_core.protocol.discover_dataset_paths", return_value=[dataset]), patch(
            "sidecar.wecom_context_core.protocol.runtime_dataset_id", return_value="current-id"
        ), patch("sidecar.wecom_context_core.protocol.dataset_presentation", return_value=("current", "当前数据")):
            result = select_dataset_action(self.config_path, config, "current-id")

        updated = self.read_config()
        self.assertEqual(result["dataset_id"], "current-id")
        self.assertEqual(updated["selected_dataset_id"], "current-id")
        self.assertEqual(updated["data_dir"], str(dataset))
        self.assertEqual(updated["snapshotMode"], "fixed")
        for field in (
            "activeSnapshotPath",
            "activeSnapshotCreatedAt",
            "activeSnapshotDatasetId",
            "selected_session_key",
            "session_bindings",
            "session_key_map",
            "allowlisted_session_keys",
            "historySnapshotPath",
        ):
            self.assertNotIn(field, updated)

    def test_clear_session_removes_bindings_and_selection(self) -> None:
        config = self.v2_config(
            selected_session_key="session",
            session_bindings={
                binding_key("ds-1", "session"): {
                    "session_key": "session",
                    "conversation_id": "conversation",
                    "dataset_id": "ds-1",
                    "snapshot_id": "snap-1",
                    "snapshot_path": "/snapshots/snap-1",
                    "mode": "active",
                    "bound_at": "now",
                }
            },
        )
        clear_session_action(self.config_path, config)
        updated = self.read_config()
        self.assertNotIn("selected_session_key", updated)
        self.assertNotIn("session_bindings", updated)
        for field in ("session_key_map", "allowlisted_session_keys", "historySnapshotPath"):
            self.assertNotIn(field, updated)

    def test_status_without_dataset_requires_selection_when_datasets_exist(self) -> None:
        with patch(
            "sidecar.wecom_context_core.protocol.discover_datasets_action",
            return_value={"count": 2, "selected_dataset_id": None, "datasets": []},
        ):
            status = status_without_dataset(self.config_path, {})
        self.assertTrue(status["dataset_selection_required"])
        self.assertFalse(status["dataset"]["available"])
        self.assertFalse(status["snapshot"]["available"])

    def test_fixed_snapshot_wins_over_newer_directory(self) -> None:
        snapshots = self.root / "snapshots"
        active = snapshots / "active"
        newer = snapshots / "newer"
        active.mkdir(parents=True)
        newer.mkdir(parents=True)
        client = VaultClient(root=self.root, snapshot_path=active)
        self.assertEqual(client.latest_snapshot(), active.resolve())


class ConfigMigrationTests(ProtocolTestCase):
    def test_v1_config_migrates_to_v2_bindings_and_is_idempotent(self) -> None:
        self.write_config(
            {
                "protocol_version": "1",
                "data_dir": "/vault/data",
                "selected_dataset_id": "ds-1",
                "activeSnapshotPath": "/vault/snapshots/snap-1",
                "activeSnapshotCreatedAt": "2026-09-13T00:00:00+00:00",
                "activeSnapshotDatasetId": "ds-1",
                "selected_session_key": "key-selected",
                "session_key_map": {"key-selected": "S:1_2", "key-other": "S:3_4"},
                "allowlisted_session_keys": ["key-selected", "key-other"],
                "historySnapshotPath": "/vault/snapshots/snap-0",
                "allowSendToWecom": True,
            }
        )
        first = load_config(self.config_path)
        self.assertEqual(first["configVersion"], 2)
        self.assertEqual(first["selected_session_key"], "key-selected")
        self.assertEqual(first["allowSendToWecom"], True)
        self.assertEqual(first["data_dir"], "/vault/data")
        selected = first["session_bindings"][binding_key("ds-1", "key-selected")]
        self.assertEqual(selected["conversation_id"], "S:1_2")
        self.assertEqual(selected["mode"], "history")
        self.assertEqual(selected["snapshot_id"], "snap-0")
        self.assertEqual(selected["snapshot_path"], "/vault/snapshots/snap-0")
        other = first["session_bindings"][binding_key("ds-1", "key-other")]
        self.assertEqual(other["conversation_id"], "S:3_4")
        self.assertEqual(other["mode"], "active")
        self.assertEqual(other["snapshot_id"], "snap-1")
        self.assertEqual(other["snapshot_path"], "/vault/snapshots/snap-1")

        stored = self.read_config()
        for field in ("session_key_map", "allowlisted_session_keys", "historySnapshotPath"):
            self.assertNotIn(field, stored)
        self.assertEqual(load_config(self.config_path), first)
        self.assertEqual(load_config(self.config_path), first)

    def test_migration_is_idempotent_for_v2_config(self) -> None:
        binding = {
            "session_key": "key",
            "conversation_id": "S:1_2",
            "dataset_id": "ds-1",
            "snapshot_id": "snap-1",
            "snapshot_path": "/snapshots/snap-1",
            "mode": "active",
            "bound_at": "now",
        }
        config = {"configVersion": 2, "selected_dataset_id": "ds-1", "session_bindings": {binding_key("ds-1", "key"): binding}}
        self.write_config(config)
        self.assertEqual(load_config(self.config_path), config)
        self.assertEqual(migrate_config(config), config)

    def test_migration_drops_selected_session_without_conversation(self) -> None:
        config = {
            "selected_session_key": "key-orphan",
            "session_key_map": {},
            "allowlisted_session_keys": ["key-orphan"],
            "historySnapshotPath": "/vault/snapshots/snap-0",
        }
        migrated = migrate_config(config)
        self.assertNotIn("selected_session_key", migrated)
        self.assertNotIn("session_bindings", migrated)
        for field in ("session_key_map", "allowlisted_session_keys", "historySnapshotPath"):
            self.assertNotIn(field, migrated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
