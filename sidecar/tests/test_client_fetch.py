from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sidecar.wecom_context_core.client_fetch import (
    ClientFetchError,
    ClientFetchTarget,
    _parse_probe,
    _run_osascript,
    fetch_images_via_client,
)

class ClientFetchTests(unittest.TestCase):
    def test_parse_probe_accepts_geometry_and_match_count(self) -> None:
        self.assertEqual(_parse_probe("found|10|20|300|64"), ("found", (10.0, 20.0, 300.0, 64.0)))
        self.assertEqual(_parse_probe("matches|0"), ("matches", 0))
        self.assertEqual(_parse_probe("invalid"), ("invalid", None))

    def test_osascript_permission_error_is_actionable(self) -> None:
        with patch(
            "sidecar.wecom_context_core.client_fetch.subprocess.run",
            return_value=SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="不允许进行辅助功能访问 (-1743)",
            ),
        ):
            with self.assertRaises(ClientFetchError) as context:
                _run_osascript("return 1")
        self.assertEqual(context.exception.code, "CLIENT_ACCESSIBILITY_REQUIRED")

    def test_osascript_automation_error_is_distinguished(self) -> None:
        with patch(
            "sidecar.wecom_context_core.client_fetch.subprocess.run",
            return_value=SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Not authorized to send Apple events to System Events (-1743)",
            ),
        ):
            with self.assertRaises(ClientFetchError) as context:
                _run_osascript("return 1")
        self.assertEqual(context.exception.code, "CLIENT_AUTOMATION_REQUIRED")

    def test_fetch_waits_for_original_cache_entry(self) -> None:
        target = ClientFetchTarget("a" * 32, 12, "57")
        missing = SimpleNamespace(status="missing")
        original = SimpleNamespace(status="original")
        with patch("sidecar.wecom_context_core.client_fetch._open_session"), patch(
            "sidecar.wecom_context_core.client_fetch.cache_roots", return_value=[]
        ), patch(
            "sidecar.wecom_context_core.client_fetch.resolve_image", side_effect=[missing, original]
        ), patch(
            "sidecar.wecom_context_core.client_fetch.time.sleep"
        ):
            result = fetch_images_via_client("示例企业", [target], timeout_seconds=2)
        self.assertTrue(result["attempted"])
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["fetched_keys"], [target.key])
        self.assertEqual(result["missing"], [])
        self.assertIsNone(result["error"])

    def test_fetch_returns_bounded_timeout_error(self) -> None:
        target = ClientFetchTarget("b" * 32, 13, "58")
        missing = SimpleNamespace(status="missing")
        with patch("sidecar.wecom_context_core.client_fetch._open_session"), patch(
            "sidecar.wecom_context_core.client_fetch.cache_roots", return_value=[]
        ), patch(
            "sidecar.wecom_context_core.client_fetch.resolve_image", return_value=missing
        ), patch(
            "sidecar.wecom_context_core.client_fetch.time.sleep"
        ), patch(
            "sidecar.wecom_context_core.client_fetch.time.monotonic", side_effect=[0.0, 0.0, 2.0]
        ):
            result = fetch_images_via_client("示例企业", [target], timeout_seconds=1)
        self.assertEqual(result["error_code"], "CLIENT_IMAGE_FETCH_TIMEOUT")
        self.assertEqual(result["missing"][0]["key"], target.key)
        self.assertEqual(result["missing"][0]["status"], "missing")
        self.assertEqual(result["attempts"], 1)

    def test_fetch_reopens_session_when_cache_makes_no_progress(self) -> None:
        target = ClientFetchTarget("d" * 32, 15, "60")
        missing = SimpleNamespace(status="missing")
        original = SimpleNamespace(status="original")
        with patch(
            "sidecar.wecom_context_core.client_fetch._open_session"
        ) as open_session, patch(
            "sidecar.wecom_context_core.client_fetch.cache_roots", return_value=[]
        ), patch(
            "sidecar.wecom_context_core.client_fetch.resolve_image", side_effect=[missing, original]
        ), patch(
            "sidecar.wecom_context_core.client_fetch.time.sleep"
        ), patch(
            "sidecar.wecom_context_core.client_fetch.time.monotonic", side_effect=[0.0, 0.0, 0.0, 1.0, 1.0]
        ), patch(
            "sidecar.wecom_context_core.client_fetch.REOPEN_INTERVAL_SECONDS", 0.0
        ):
            result = fetch_images_via_client("示例企业", [target], timeout_seconds=2)
        self.assertEqual(result["fetched_keys"], [target.key])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(open_session.call_count, 2)

    def test_fetch_reports_ui_failure_without_throwing(self) -> None:
        target = ClientFetchTarget("c" * 32, 14, "59")
        with patch(
            "sidecar.wecom_context_core.client_fetch._open_session",
            side_effect=ClientFetchError("CLIENT_ACCESSIBILITY_REQUIRED", "需要允许辅助功能"),
        ):
            result = fetch_images_via_client("示例企业", [target], timeout_seconds=1)
        self.assertEqual(result["error_code"], "CLIENT_ACCESSIBILITY_REQUIRED")
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["missing"][0]["message_id"], "59")
