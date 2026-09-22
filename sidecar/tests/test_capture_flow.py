from __future__ import annotations

import plistlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sidecar.wecom_context_core import capture_flow
from sidecar.wecom_context_core.vault_client import VaultError


class CaptureFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wecom-capture-flow-")
        self.root = Path(self._tmp.name)
        self.dataset = self.root / "dataset"
        self.dataset.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_dataset_reports_not_found(self) -> None:
        with patch.object(capture_flow, "discover_dataset_paths", return_value=[]):
            with self.assertRaises(VaultError) as ctx:
                capture_flow.capture_key_action(Path("/tmp/config.json"), {}, {"dataset_id": "abc123def456"})
        self.assertEqual(ctx.exception.code, "DATASET_NOT_FOUND")

    def test_invalid_request_without_dataset_id(self) -> None:
        with self.assertRaises(VaultError) as ctx:
            capture_flow.capture_key_action(Path("/tmp/config.json"), {}, {})
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")

    def test_already_key_short_circuits_before_any_process_work(self) -> None:
        with patch.object(capture_flow, "resolve_dataset", return_value=self.dataset), \
             patch.object(capture_flow, "key_validates_for", return_value=True):
            result = capture_flow.capture_key_action(
                Path("/tmp/config.json"), {}, {"dataset_id": "abc123def456"}
            )
        self.assertEqual(result["already"], True)
        self.assertEqual(result["verified"], True)
        self.assertEqual(result["captured"], False)


    def test_recently_active_different_account_stops_before_process_work(self) -> None:
        target = self.root / "1680000000000001" / "Data"
        active = self.root / "1680000000000002" / "Data"
        target.mkdir(parents=True)
        active.mkdir(parents=True)
        with patch.object(capture_flow, "resolve_dataset", return_value=target), \
             patch.object(capture_flow, "key_validates_for", return_value=False), \
             patch.object(capture_flow, "freshest_current_dataset", return_value=active), \
             patch.object(
                 capture_flow,
                 "account_id_for",
                 side_effect=lambda path: "1680000000000001" if path == target else "1680000000000002",
             ), \
             patch.object(capture_flow, "quit_wecom") as quit_wecom:
            with self.assertRaises(VaultError) as ctx:
                capture_flow.capture_key_action(Path("/tmp/config.json"), {}, {"dataset_id": "target"})
        self.assertEqual(ctx.exception.code, "CAPTURE_FAILED")
        self.assertIn("不一致", str(ctx.exception))
        self.assertIn("本次未执行取钥扫描", str(ctx.exception))
        quit_wecom.assert_not_called()

    def test_freshest_current_dataset_ignores_backup_dirs(self) -> None:
        backup = self.root / "1680000000000001" / "Backup" / "123" / "Data"
        backup.mkdir(parents=True)
        (backup / "message.db").write_bytes(b"\x00" * 100)
        current = self.root / "1680000000000002" / "Data"
        current.mkdir(parents=True)
        (current / "message.db").write_bytes(b"\x00" * 100)
        with patch.object(capture_flow, "discover_dataset_paths", return_value=[backup, current]):
            result = capture_flow.freshest_current_dataset()
        self.assertEqual(result, current)

    def make_app(self, bundle: Path, version: tuple[str, str] | None) -> Path:
        executable = capture_flow.signed_copy_executable(bundle)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"binary")
        if version is not None:
            with (bundle / "Contents" / "Info.plist").open("wb") as handle:
                plistlib.dump(
                    {"CFBundleShortVersionString": version[0], "CFBundleVersion": version[1]}, handle
                )
        return bundle

    def run_capture_action(self, module) -> tuple[dict, dict]:
        """跑完整取钥动作：副本选择 → 启动副本 → 附加扫描 → 校验密钥。"""
        state: dict = {}
        with patch.dict(sys.modules, {"frida": types.ModuleType("frida")}), \
             patch.object(capture_flow, "resolve_dataset", return_value=self.dataset), \
             patch.object(capture_flow, "apps_root", return_value=self.root / "apps"), \
             patch.object(capture_flow, "ORIGINAL_APP", self.original), \
             patch.object(capture_flow, "freshest_current_dataset", return_value=self.dataset), \
             patch.object(capture_flow, "_capture_module", return_value=module), \
             patch.object(capture_flow, "latest_key_file", return_value=None), \
             patch.object(capture_flow, "quit_wecom"), \
             patch.object(capture_flow, "relaunch_original"), \
             patch.object(
                 capture_flow,
                 "key_validates_for",
                 side_effect=lambda _path: bool(state.get("captured")),
             ), \
             patch.object(capture_flow, "launch_copy", side_effect=lambda path: state.update(copy=path) or 4242), \
             patch.object(
                 capture_flow,
                 "run_capture",
                 side_effect=lambda *_args: (state.update(captured=True), (0, "attach ok"))[1],
             ):
            result = capture_flow.capture_key_action(Path("/tmp/config.json"), {}, {"dataset_id": "abc123def456"})
        return result, state

    def test_capture_attaches_to_reusable_signed_copy(self) -> None:
        # 回归：choose_signed_copy 缺失时这里会 NameError → sidecar 返回 INTERNAL_ERROR。
        self.original = self.make_app(self.root / "企业微信.app", ("5.0.11", "99983"))
        self.make_app(self.root / "apps" / "WeComSigned-20260912-001838.app", ("5.0.11", "99983"))
        copy_app = self.make_app(self.root / "apps" / "WeComSigned-20260913-140012.app", ("5.0.11", "99983"))
        module = types.SimpleNamespace(
            prepare_signed_copy=Mock(side_effect=AssertionError("版本一致时不应重建副本")),
            default_signed_copy_path=lambda: self.root / "apps" / "new.app",
            capture=lambda _args: 0,
        )
        result, state = self.run_capture_action(module)
        self.assertEqual(result["captured"], True)
        self.assertEqual(result["verified"], True)
        self.assertEqual(result["dataset_id"], "abc123def456")
        self.assertEqual(state["copy"], copy_app)

    def test_capture_rebuilds_copy_when_version_differs(self) -> None:
        self.original = self.make_app(self.root / "企业微信.app", ("5.0.12", "100001"))
        self.make_app(self.root / "apps" / "WeComSigned-20260912-001838.app", ("5.0.11", "99983"))
        rebuilt = self.root / "apps" / "WeComSigned-new.app"
        prepare = Mock(side_effect=lambda _source, copy_path, _reuse: self.make_app(copy_path, None))
        module = types.SimpleNamespace(
            prepare_signed_copy=prepare,
            default_signed_copy_path=lambda: rebuilt,
            capture=lambda _args: 0,
        )
        result, state = self.run_capture_action(module)
        self.assertEqual(result["captured"], True)
        prepare.assert_called_once_with(self.original, rebuilt, True)
        self.assertEqual(state["copy"], rebuilt)

    def test_prepare_failure_reports_retryable_capture_failure(self) -> None:
        self.original = self.make_app(self.root / "企业微信.app", None)
        module = types.SimpleNamespace(
            prepare_signed_copy=Mock(side_effect=SystemExit("copy failed")),
            default_signed_copy_path=lambda: self.root / "apps" / "new.app",
            capture=lambda _args: 0,
        )
        with self.assertRaises(VaultError) as ctx:
            self.run_capture_action(module)
        self.assertEqual(ctx.exception.code, "CAPTURE_FAILED")
        self.assertEqual(ctx.exception.retryable, True)


if __name__ == "__main__":
    unittest.main()
