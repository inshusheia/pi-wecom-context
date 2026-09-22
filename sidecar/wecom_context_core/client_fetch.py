from __future__ import annotations

"""Use the running WeCom desktop client to refill one image cache entry.

This module deliberately does not speak to WeCom's CDN.  It opens the native
client, lets the client perform its own authenticated media request, and then
waits for the plaintext image to appear under the normal Images cache.
"""

import ctypes
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .image_cache import cache_roots, resolve_image

WECOM_APP = Path("/Applications/企业微信.app")
WECOM_PROCESS = "企业微信"
DEFAULT_TIMEOUT_SECONDS = 90.0
MAX_TIMEOUT_SECONDS = 90.0
POLL_INTERVAL_SECONDS = 0.5
REOPEN_INTERVAL_SECONDS = 8.0
MAX_SESSION_OPEN_ATTEMPTS = 4
HISTORY_SCROLL_PAGES = 6
HISTORY_SCROLL_DELAY_SECONDS = 0.25
KEY_RE = re.compile(r"^[0-9a-f]{32}$")
RETRYABLE_SESSION_ERRORS = frozenset(
    {
        "CLIENT_UI_NOT_READY",
        "CLIENT_UI_PROBE_FAILED",
        "CLIENT_CONVERSATION_NOT_VISIBLE",
        "CLIENT_CONVERSATION_NOT_FOUND",
    }
)


@dataclass(frozen=True)
class ClientFetchTarget:
    key: str
    expected_size: int | None
    message_id: str


class ClientFetchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _apple_string(value: str) -> str:
    """Quote a trusted display name for a single AppleScript string literal."""
    escaped = (
        str(value or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return f'"{escaped}"'


def _run_osascript(script: str, timeout_seconds: float = 10.0) -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ClientFetchError("CLIENT_UI_NOT_READY", "读取企业微信界面超时") from error
    except OSError as error:
        raise ClientFetchError("CLIENT_UI_UNAVAILABLE", "无法启动 macOS 界面自动化") from error
    if result.returncode != 0:
        detail = f"{result.stdout}\n{result.stderr}".lower()
        if any(token in detail for token in ("not authorized to send apple events", "apple events", "automation", "自动化")):
            raise ClientFetchError(
                "CLIENT_AUTOMATION_REQUIRED",
                "需要在“系统设置 → 隐私与安全性 → 自动化”中允许 WeCom Context 控制系统事件和企业微信",
            )
        if any(token in detail for token in ("assistive", "accessibility", "辅助功能", "设备控制和数据访问", "权限", "不允许", "-25208")):
            raise ClientFetchError(
                "CLIENT_ACCESSIBILITY_REQUIRED",
                "需要在“系统设置 → 隐私与安全性 → 设备控制和数据访问（旧称辅助功能）”中允许 WeCom Context 控制企业微信",
            )
        if "-1743" in detail:
            raise ClientFetchError(
                "CLIENT_AUTOMATION_REQUIRED",
                "需要在“系统设置 → 隐私与安全性 → 自动化”中允许 WeCom Context 控制系统事件和企业微信",
            )
        raise ClientFetchError("CLIENT_UI_PROBE_FAILED", "无法读取企业微信界面")
    return result.stdout.strip()


def _session_row_script(display_name: str) -> str:
    target = _apple_string(display_name)
    return f'''set targetName to {target}
tell application "System Events"
 tell process "{WECOM_PROCESS}"
  if not (exists window 1) then return "not_running"
  tell window 1
   try
    set targetRows to rows of table 1 of scroll area 1 of splitter group 1 of splitter group 1
   on error
    return "no_conversation_table"
   end try
   set matches to {{}}
   repeat with rowItem in targetRows
    set matched to false
    repeat with cellItem in UI elements of rowItem
     repeat with childItem in UI elements of cellItem
      try
       if (value of childItem as text) is targetName then set matched to true
      end try
     end repeat
    end repeat
    if matched then set end of matches to rowItem
   end repeat
   if (count of matches) is 1 then
    set rowPosition to position of item 1 of matches
    set rowExtent to size of item 1 of matches
    return "found|" & (item 1 of rowPosition) & "|" & (item 2 of rowPosition) & "|" & (item 1 of rowExtent) & "|" & (item 2 of rowExtent)
   end if
   return "matches|" & (count of matches)
  end tell
 end tell
end tell'''

def _other_session_row_script(display_name: str) -> str:
    target = _apple_string(display_name)
    return f'''set targetName to {target}
tell application "System Events"
 tell process "{WECOM_PROCESS}"
  if not (exists window 1) then return "not_running"
  tell window 1
   try
    set targetRows to rows of table 1 of scroll area 1 of splitter group 1 of splitter group 1
   on error
    return "no_conversation_table"
   end try
   repeat with rowItem in targetRows
    set matched to false
    repeat with cellItem in UI elements of rowItem
     repeat with childItem in UI elements of cellItem
      try
       if (value of childItem as text) is targetName then set matched to true
      end try
     end repeat
    end repeat
    if not matched then
     set otherPosition to position of rowItem
     set otherExtent to size of rowItem
     if (item 2 of otherExtent) > 5 then
      return "found|" & (item 1 of otherPosition) & "|" & (item 2 of otherPosition) & "|" & (item 1 of otherExtent) & "|" & (item 2 of otherExtent)
     end if
    end if
   end repeat
   return "not_found"
  end tell
 end tell
end tell'''


def _find_other_row(display_name: str) -> tuple[float, float, float, float] | None:
    status, value = _parse_probe(_run_osascript(_other_session_row_script(display_name)))
    if status == "found":
        return value  # type: ignore[return-value]
    return None


def _search_field_script() -> str:
    return f'''tell application "System Events"
 tell process "{WECOM_PROCESS}"
  if not (exists window 1) then return "not_running"
  tell window 1
   try
    set fieldItem to text field 1 of splitter group 1 of splitter group 1
    set fieldPosition to position of fieldItem
    set fieldExtent to size of fieldItem
    return "found|" & (item 1 of fieldPosition) & "|" & (item 2 of fieldPosition) & "|" & (item 1 of fieldExtent) & "|" & (item 2 of fieldExtent)
   on error
    return "not_found"
   end try
  end tell
 end tell
end tell'''


def _parse_probe(output: str) -> tuple[str, tuple[float, float, float, float] | int | None]:
    fields = output.strip().split("|")
    if fields and fields[0] == "found" and len(fields) == 5:
        try:
            return "found", tuple(float(value) for value in fields[1:5])  # type: ignore[return-value]
        except ValueError:
            return "invalid", None
    if fields and fields[0] == "matches" and len(fields) == 2:
        try:
            return "matches", int(fields[1])
        except ValueError:
            return "invalid", None
    if fields and fields[0] in {"not_running", "no_conversation_table", "not_found"}:
        return fields[0], None
    return "invalid", None


def _center(bounds: tuple[float, float, float, float]) -> tuple[float, float]:
    x, y, width, height = bounds
    return x + width / 2.0, y + height / 2.0


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_CG_EVENT_MOUSE_DOWN = 1
_CG_EVENT_MOUSE_UP = 2
_CG_MOUSE_BUTTON_LEFT = 0
_CG_HID_EVENT_TAP = 0


def _click_at(point: tuple[float, float]) -> None:
    """Post a real mouse click; AXPress is not reliable for the CEF view."""
    try:
        core_graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        core_foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    except OSError as error:
        raise ClientFetchError("CLIENT_UI_UNAVAILABLE", "系统不支持企业微信界面自动化") from error

    core_graphics.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        _CGPoint,
        ctypes.c_uint32,
    ]
    core_graphics.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    core_graphics.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    core_graphics.CGEventPost.restype = None
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.restype = None

    point_value = _CGPoint(float(point[0]), float(point[1]))
    for event_type in (_CG_EVENT_MOUSE_DOWN, _CG_EVENT_MOUSE_UP):
        event = core_graphics.CGEventCreateMouseEvent(
            None,
            event_type,
            point_value,
            _CG_MOUSE_BUTTON_LEFT,
        )
        if not event:
            raise ClientFetchError("CLIENT_ACCESSIBILITY_REQUIRED", "无法向企业微信发送鼠标事件")
        try:
            core_graphics.CGEventPost(_CG_HID_EVENT_TAP, event)
        finally:
            core_foundation.CFRelease(event)
        time.sleep(0.05)


def _open_wecom() -> None:
    if not WECOM_APP.is_dir():
        raise ClientFetchError("CLIENT_NOT_INSTALLED", "未找到 /Applications/企业微信.app")
    try:
        subprocess.run(
            ["/usr/bin/open", "-a", str(WECOM_APP)],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ClientFetchError("CLIENT_LAUNCH_FAILED", "无法启动企业微信") from error

    deadline = time.monotonic() + 12.0
    last_error: ClientFetchError | None = None
    while time.monotonic() < deadline:
        try:
            status = _run_osascript(
                f'''tell application "System Events"
 tell process "{WECOM_PROCESS}"
  if exists window 1 then return "ready"
  return "waiting"
 end tell
end tell''',
                timeout_seconds=3,
            )
            if status != "ready":
                time.sleep(0.25)
                continue
            _run_osascript(
                f'''tell application "System Events"
 tell process "{WECOM_PROCESS}"
  set frontmost to true
 end tell
end tell''',
                timeout_seconds=3,
            )
            return
        except ClientFetchError as error:
            last_error = error
        time.sleep(0.25)
    if last_error is not None:
        raise last_error
    raise ClientFetchError("CLIENT_UI_NOT_READY", "企业微信窗口在限定时间内未准备好")

def _paste_search_text(display_name: str) -> None:
    try:
        subprocess.run(
            ["/usr/bin/pbcopy"],
            input=display_name,
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ClientFetchError("CLIENT_SEARCH_FAILED", "无法填写企业微信会话搜索框") from error
    _run_osascript(
        f'''tell application "System Events"
 tell process "{WECOM_PROCESS}"
  keystroke "a" using {{command down}}
  keystroke "v" using {{command down}}
 end tell
end tell''',
        timeout_seconds=5,
    )


def _find_row(display_name: str, attempts: int = 16) -> tuple[float, float, float, float]:
    last_status = "not_found"
    for _ in range(attempts):
        status, value = _parse_probe(_run_osascript(_session_row_script(display_name)))
        last_status = status
        if status == "found":
            return value  # type: ignore[return-value]
        if status == "matches" and value != 0:
            raise ClientFetchError("CLIENT_CONVERSATION_AMBIGUOUS", "企业微信中存在多个同名会话，未自动选择")
        time.sleep(0.25)

    search_status, search_value = _parse_probe(_run_osascript(_search_field_script()))
    if search_status != "found":
        if last_status == "not_running":
            raise ClientFetchError("CLIENT_UI_NOT_READY", "企业微信主窗口尚未准备好")
        raise ClientFetchError("CLIENT_CONVERSATION_NOT_VISIBLE", "企业微信当前列表中找不到目标会话")
    _click_at(_center(search_value))  # type: ignore[arg-type]
    _paste_search_text(display_name)
    for _ in range(attempts):
        status, value = _parse_probe(_run_osascript(_session_row_script(display_name)))
        if status == "found":
            return value  # type: ignore[return-value]
        if status == "matches" and value != 0:
            raise ClientFetchError("CLIENT_CONVERSATION_AMBIGUOUS", "企业微信搜索结果存在多个同名会话")
        time.sleep(0.25)
    raise ClientFetchError("CLIENT_CONVERSATION_NOT_FOUND", "企业微信搜索不到目标会话")



def _scroll_session_history(pages: int = HISTORY_SCROLL_PAGES) -> bool:
    """向上翻页，触发企业微信加载不可见的历史图片。"""
    safe_pages = max(1, min(int(pages), 12))
    # 企业微信当前 CEF 窗口不暴露稳定的 AX scroll-area 层级；直接向
    # 消息区域发送 Page Up 比依赖某一版 UI 的 splitter 路径可靠。
    script = f'''tell application "System Events"
 tell process "{WECOM_PROCESS}"
  if not (exists window 1) then return "not_running"
  tell window 1
   set windowPosition to position
   set windowExtent to size
  end tell
  set focusPoint to {{(item 1 of windowPosition) + ((item 1 of windowExtent) * 0.55), (item 2 of windowPosition) + ((item 2 of windowExtent) * 0.45)}}
  click at focusPoint
  repeat with scrollIndex from 1 to {safe_pages}
   key code 116
   delay {HISTORY_SCROLL_DELAY_SECONDS}
  end repeat
  return "scrolled"
 end tell
end tell'''
    try:
        return _run_osascript(script, timeout_seconds=8) == "scrolled"
    except ClientFetchError:
        return False

def _open_session(display_name: str) -> None:
    _open_wecom()
    other_bounds = _find_other_row(display_name)
    if other_bounds is not None:
        _click_at(_center(other_bounds))
        time.sleep(0.3)
    bounds = _find_row(display_name)
    _click_at(_center(bounds))
    time.sleep(0.5)
    _scroll_session_history()


def _target_payload(target: ClientFetchTarget) -> dict[str, Any]:
    return {
        "key": target.key,
        "message_id": target.message_id,
        "expected_size": target.expected_size,
    }


def fetch_images_via_client(
    display_name: str,
    targets: list[ClientFetchTarget],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """反复打开目标会话并等待缺失图片进入企业微信缓存。"""
    unique: dict[str, ClientFetchTarget] = {}
    for target in targets:
        if KEY_RE.fullmatch(target.key):
            unique.setdefault(target.key, target)
    if not unique:
        return {
            "attempted": False,
            "requested": 0,
            "fetched_keys": [],
            "missing": [],
            "error_code": None,
            "error": None,
            "attempts": 0,
        }

    timeout = min(max(float(timeout_seconds), 1.0), MAX_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    fetched: set[str] = set()
    latest: dict[str, str] = {}
    attempts = 0
    last_open_at = float("-inf")
    last_error: ClientFetchError | None = None

    while True:
        now = time.monotonic()
        should_reopen = attempts == 0 or (
            now - last_open_at >= REOPEN_INTERVAL_SECONDS
            and attempts < MAX_SESSION_OPEN_ATTEMPTS
        )
        if should_reopen:
            attempts += 1
            last_open_at = now
            try:
                _open_session(display_name)
                last_error = None
            except ClientFetchError as error:
                last_error = error
                if error.code not in RETRYABLE_SESSION_ERRORS:
                    break

        roots = cache_roots()
        for key, target in unique.items():
            if key in fetched:
                continue
            resolved = resolve_image(key, expected_size=target.expected_size, roots=roots)
            latest[key] = resolved.status
            if resolved.status == "original":
                fetched.add(key)

        now = time.monotonic()
        if len(fetched) == len(unique) or now >= deadline:
            break
        time.sleep(min(POLL_INTERVAL_SECONDS, max(deadline - now, 0.0)))

    missing = [
        {**_target_payload(target), "status": latest.get(key, "missing")}
        for key, target in unique.items()
        if key not in fetched
    ]
    if not missing:
        error_code = None
        error = None
    elif last_error is not None:
        error_code = last_error.code
        error = f"{last_error.message}；已尝试打开会话 {attempts} 次"
    else:
        error_code = "CLIENT_IMAGE_FETCH_TIMEOUT"
        error = (
            "企业微信已打开会话，但图片缓存未在限定时间内恢复；"
            f"已尝试打开会话 {attempts} 次。请保持企业微信登录目标企业，"
            "手动进入并滚动目标会话后再重试"
        )
    return {
        "attempted": True,
        "requested": len(unique),
        "fetched_keys": sorted(fetched),
        "missing": missing,
        "error_code": error_code,
        "error": error,
        "attempts": attempts,
    }
