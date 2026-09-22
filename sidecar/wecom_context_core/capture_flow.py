"""应用内一键取钥：把 CLI 取钥流程封装为 sidecar 的 capture_key action。

流程：校验目标数据集 → 校验企业微信登录态 → 退出企业微信 → 签名副本启动 →
只读 attach 扫描（复用 capture_key_macos.capture）→ 密钥验证 → 恢复原版企业微信。

安全边界与 CLI 相同：只读附加进程扫描数据库密钥，不读取、不上传任何聊天内容；
密钥只写入本机 vault private/（0600）。
"""

from __future__ import annotations

import contextlib
import io
import plistlib
import subprocess
import threading
import sys
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

from .vault_client import VaultError
from .vault_runtime import (
    account_id_for,
    discover_dataset_paths,
    dataset_id as runtime_dataset_id,
    key_validates_for,
)
CAPTURE_DURATION_SECONDS = 90
COPY_LAUNCH_TIMEOUT_SECONDS = 45
COPY_WARMUP_SECONDS = 12
ORIGINAL_APP = Path("/Applications/企业微信.app")


def _capture_module():
    """按打包布局加载 capture_key_macos（frozen 时位于 _MEIPASS/vault_runtime）。"""
    from .vault_runtime import _runtime_directory

    runtime_text = str(_runtime_directory())
    if runtime_text not in sys.path:
        sys.path.insert(0, runtime_text)
    import capture_key_macos  # type: ignore[import-not-found]

    return capture_key_macos


def freshest_current_dataset() -> Path | None:
    """当前登录企业 = 最近有写入的 current 数据集（与 discover 的 active 判定同语义）。"""
    now = time.time()
    candidates: list[tuple[float, Path]] = []
    for path in discover_dataset_paths():
        parts = {part.lower() for part in path.parts}
        if path.name.lower() != "data" or "backup" in parts:
            continue
        newest = 0.0
        try:
            for item in path.rglob("*"):
                try:
                    if item.is_file():
                        newest = max(newest, item.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            continue
        candidates.append((newest, path))
    if not candidates:
        return None
    best = max(candidates, key=lambda item: item[0])
    if best[0] <= 0:
        return None
    age_minutes = max(0.0, (now - best[0]) / 60)
    # 超过 30 分钟没有任何写入的"最新"数据集不可作为当前登录证据（企业微信可能已退出）。
    if age_minutes > 30:
        return None
    return best[1]

def apps_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "wecom-local-vault" / "apps"

def resolve_dataset(dataset_id_value: str, config: dict[str, Any] | None = None) -> Path:
    """按 dataset_id 找回数据集目录。config.data_dir 命中时走快路径；
    全盘扫描包 30s 线程超时（新容器目录首读会被 sandboxd 拖慢）。"""
    data_dir = (config or {}).get("data_dir")
    if isinstance(data_dir, str) and data_dir:
        try:
            if runtime_dataset_id(Path(data_dir)) == dataset_id_value:
                return Path(data_dir)
        except Exception:
            pass
    outcome: dict[str, Any] = {}

    def worker() -> None:
        outcome["paths"] = list(discover_dataset_paths())

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(30)
    paths = outcome.get("paths") if isinstance(outcome.get("paths"), list) else []
    for path in paths:
        try:
            if runtime_dataset_id(path) == dataset_id_value:
                return path
        except Exception:
            continue
    raise VaultError("DATASET_NOT_FOUND", f"找不到数据集 {dataset_id_value}", False)


def quit_wecom() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "企业微信" to quit'],
        capture_output=True,
        timeout=10,
        check=False,
    )
    time.sleep(2)
    subprocess.run(["pkill", "-x", "企业微信"], capture_output=True, check=False)
    time.sleep(1)


def launch_copy(copy_path: Path) -> int:
    """open 签名副本，等主进程出现并预热后返回 pid。

    副本刚启动就 attach 会因初始化未完成而提前退出（实测稳定复现），
    因此找到 pid 后再等固定预热时间，并复查进程仍存活。
    """
    subprocess.run(["open", "-n", str(copy_path)], capture_output=True, timeout=20, check=False)
    executable = str(signed_copy_executable(copy_path))
    deadline = time.monotonic() + COPY_LAUNCH_TIMEOUT_SECONDS
    pid = None
    while time.monotonic() < deadline:
        result = subprocess.run(["pgrep", "-f", executable], capture_output=True, text=True, check=False)
        for line in result.stdout.splitlines():
            if line.strip().isdigit():
                pid = int(line.strip())
                break
        if pid is not None:
            break
        time.sleep(1)
    if pid is None:
        raise VaultError("CAPTURE_FAILED", "签名副本启动超时；请重试", True)
    time.sleep(COPY_WARMUP_SECONDS)
    result = subprocess.run(["pgrep", "-f", executable], capture_output=True, text=True, check=False)
    alive = {int(line) for line in result.stdout.splitlines() if line.strip().isdigit()}
    if pid not in alive:
        raise VaultError("CAPTURE_FAILED", "企业微信签名副本启动后立即退出；请重试，或改在终端运行取钥脚本", True)
    return pid


def relaunch_original() -> None:
    subprocess.run(["pkill", "-x", "企业微信"], capture_output=True, check=False)
    time.sleep(2)
    if ORIGINAL_APP.is_dir():
        subprocess.run(["open", "-n", str(ORIGINAL_APP)], capture_output=True, timeout=20, check=False)
        time.sleep(3)


def run_capture(module, dataset: Path, pid: int, duration: int) -> tuple[int, str]:
    buffer = io.StringIO()
    args = Namespace(
        mode="attach",
        pid=pid,
        data_dir=str(dataset),
        duration=duration,
        confirm_attach=True,
        generic_hooks_only=False,
        debug_candidates=False,
        save_candidates=False,
        candidate_output=None,
        output=None,
        confirm_signed_copy=False,
        wecom_copy=None,
        source_app=str(ORIGINAL_APP),
        reuse_signed_copy=True,
    )
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = module.capture(args)
    text = buffer.getvalue()
    return code, text.strip()


def latest_key_file() -> Path | None:
    private = Path.home() / "Library" / "Application Support" / "wecom-local-vault" / "private"
    candidates = sorted(private.glob("keys-*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if private.is_dir() else []
    return candidates[0] if candidates else None


def signed_copy_executable(bundle: Path) -> Path:
    return bundle / "Contents" / "MacOS" / "企业微信"


def app_version(bundle: Path) -> tuple[str, str] | None:
    """读 .app 的 (CFBundleShortVersionString, CFBundleVersion)；读不到返回 None。"""
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    if not isinstance(info, dict):
        return None
    return str(info.get("CFBundleShortVersionString") or ""), str(info.get("CFBundleVersion") or "")


def choose_signed_copy(module) -> Path:
    """挑一份可用于附加扫描的企业微信签名副本。

    优先复用 vault/apps 下版本与本机企业微信一致、可执行文件完整的副本（副本约 1.6 GB，
    重新复制 + ad-hoc 重签代价高）；版本不一致或没有可用副本时才新建一份。
    """
    root = apps_root()
    expected = app_version(ORIGINAL_APP)
    reusable: list[Path] = []
    if root.is_dir():
        for bundle in root.glob("WeComSigned-*.app"):
            if not signed_copy_executable(bundle).is_file():
                continue
            if expected is not None and app_version(bundle) != expected:
                continue
            reusable.append(bundle)
    if reusable:
        # 副本目录名自带零填充时间戳，字典序即时间序；copytree 保留源 App 的 mtime，
        # 按目录 mtime 排序会拿到任意一份。
        return max(reusable, key=lambda bundle: bundle.name)
    copy_path = Path(module.default_signed_copy_path())
    try:
        module.prepare_signed_copy(ORIGINAL_APP, copy_path, True)
    except (SystemExit, OSError, subprocess.SubprocessError) as exc:
        # prepare_signed_copy 用 SystemExit 报错、codesign 失败抛 CalledProcessError；
        # 这些都不带结构化 code，转成可重试的 CAPTURE_FAILED，避免整个 sidecar 请求变成 INTERNAL_ERROR。
        raise VaultError("CAPTURE_FAILED", f"无法准备企业微信签名副本：{exc}", True) from exc
    if not signed_copy_executable(copy_path).is_file():
        raise VaultError("CAPTURE_FAILED", f"企业微信签名副本不完整：{copy_path}", True)
    return copy_path


def capture_key_action(config_path: Path, config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    dataset_id_value = str(request.get("dataset_id") or config.get("selected_dataset_id") or "")
    if not dataset_id_value:
        raise VaultError("INVALID_REQUEST", "缺少 dataset_id", False)
    dataset = resolve_dataset(dataset_id_value, config)
    if key_validates_for(dataset):
        return {
            "captured": False,
            "verified": True,
            "already": True,
            "dataset_id": dataset_id_value,
            "message": "该企业已有可用密钥，无需提取",
        }
    active = freshest_current_dataset()
    if active is None:
        login_note = "无法从最近写入记录确认当前登录企业；请先在企业微信打开并登录目标企业后重试"
    else:
        active_identity = account_id_for(active)
        target_identity = account_id_for(dataset)
        if active_identity and target_identity and active_identity != target_identity:
            raise VaultError(
                "CAPTURE_FAILED",
                f"当前企业微信账户（{active_identity}）与目标数据账户（{target_identity}）不一致。"
                "请先在企业微信登录目标企业，保持其运行后再重试；本次未执行取钥扫描。",
                True,
            )
        login_note = None if active_identity and target_identity else "无法确认当前登录企业与目标数据账户是否一致；捕获失败时请先切换到目标企业后重试"
    module = _capture_module()
    try:
        import frida  # noqa: F401
    except ImportError as exc:
        raise VaultError("CAPTURE_FAILED", f"密钥捕获组件(frida)不可用: {exc}", False)

    copy_path = choose_signed_copy(module)
    quit_wecom()
    captured_log = ""
    capture_code = -1
    try:
        for attempt in range(2):
            copy_pid = launch_copy(copy_path)
            capture_code, captured_log = run_capture(module, dataset, copy_pid, CAPTURE_DURATION_SECONDS)
            if key_validates_for(dataset):
                break
            if capture_code == 4 and attempt == 0:
                # 副本在 agent 就绪前退出：重启副本重试一次
                subprocess.run(["pkill", "-x", "企业微信"], capture_output=True, check=False)
                time.sleep(3)
                continue
            break
    finally:
        relaunch_original()

    if not key_validates_for(dataset):
        tail = "\n".join(captured_log.splitlines()[-4:])
        raise VaultError(
            "CAPTURE_FAILED",
            f"取钥未成功（capture 退出码 {capture_code}）。确认企业微信登录的是该企业后重试。{(login_note or '')} {tail}".strip(),
            True,
        )
    key_file = latest_key_file()
    return {
        "captured": True,
        "verified": True,
        "already": False,
        "dataset_id": dataset_id_value,
        "key_file": str(key_file) if key_file else None,
        "log": "\n".join(captured_log.splitlines()[-4:]),
        "login_note": login_note,
    }
