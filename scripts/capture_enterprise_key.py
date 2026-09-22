#!/usr/bin/env python3
"""capture-enterprise-key — 为指定企业微信数据集提取数据库密钥（一键封装）。

用法:
    python3 scripts/capture_enterprise_key.py          # 列出数据集并按序号选择
    python3 scripts/capture-enterprise_key.py <编号>    # 直接按编号提取

前提:
    1. 企业微信桌面端当前登录的就是要提取的企业账号
    2. frida（缺失时自动经本地代理安装到 sidecar/.venv）

安全说明: 只读附加企业微信进程扫描数据库密钥，不读取、不上传任何聊天内容；
密钥只写入本机 ~/Library/Application Support/wecom-local-vault/private/。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 需要 Crypto/wecom_context_core 依赖；缺失时切换到 sidecar 虚拟环境重新执行
try:
    from Crypto.Cipher import AES  # noqa: F401,E402
    sys.path.insert(0, str(ROOT / "sidecar"))
    sys.path.insert(0, str(ROOT / "third_party" / "yichen-skills" / "yichen-wecom-local-vault" / "scripts"))
except ImportError:
    venv_python = ROOT / "sidecar" / ".venv" / "bin" / "python"
    os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])
sys.path.insert(0, str(ROOT / "sidecar"))
sys.path.insert(0, str(ROOT / "third_party" / "yichen-skills" / "yichen-wecom-local-vault" / "scripts"))

from wecom_context_core.vault_runtime import (  # noqa: E402
    discover_dataset_paths,
    enterprise_name_for,
    key_validates_for,
)

CAPTURE = ROOT / "third_party" / "yichen-skills" / "yichen-wecom-local-vault" / "scripts" / "capture_key_macos.py"
PROXY = "http://127.0.0.1:7897"


def ensure_frida() -> None:
    try:
        import frida  # noqa: F401
        return
    except ImportError:
        pass
    print("frida 未安装，正在经本地代理下载安装…")
    proxy = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    opener = urllib.request.build_opener(proxy)
    urllib.request.install_opener(opener)
    request = urllib.request.Request("https://pypi.org/pypi/frida/json", headers={"User-Agent": "pip"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    candidates = [item for item in data["urls"] if "macosx" in item["filename"] and "abi3" in item["filename"]]
    arm64 = next((item for item in candidates if "arm64" in item["filename"]), None)
    wheel_url = (arm64 or candidates[0])["url"]
    filename = urllib.parse.urlparse(wheel_url).path.rsplit("/", 1)[-1]
    wheel = Path("/tmp") / filename
    subprocess.run(["curl", "-sS", "-x", PROXY, "-L", "--retry", "3", "-o", str(wheel), wheel_url], check=True)
    python = ROOT / "sidecar" / ".venv" / "bin" / "python"
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", str(wheel)], check=True)
    wheel.unlink()


def load_datasets() -> list[dict[str, str]]:
    include_backup = os.environ.get("WECOM_INCLUDE_BACKUP") == "1"
    candidates: list[dict[str, str]] = []
    for path in discover_dataset_paths():
        label = enterprise_name_for(path) or f"企业…{next(s for s in path.parts if s.isdigit() and len(s) >= 15)[-4:]}"
        kind = "当前" if path.name.lower() == "data" and "backup" not in {p.lower() for p in path.parts} else "备份"
        if kind == "备份" and not include_backup:
            continue
        candidates.append({
            "label": label,
            "kind": kind,
            "status": "已有密钥" if key_validates_for(path) else "无密钥",
            "path": str(path),
        })
    return [
        {"index": str(index), **item}
        for index, item in enumerate(candidates, 1)
    ]


def main() -> int:
    ensure_frida()
    entries = load_datasets()
    print("本机企业微信数据集：")
    for entry in entries:
        print(f"  [{entry['index']}] {entry['label']}·{entry['kind']} — {entry['status']}")
        print(f"      {entry['path']}")

    choice = sys.argv[1] if len(sys.argv) > 1 else input("输入要提取密钥的数据集编号: ").strip()
    target = next((entry for entry in entries if entry["index"] == choice), None)
    if target is None:
        print(f"无效编号: {choice}")
        return 1
    if target["status"] == "已有密钥":
        print(f"「{target['label']}」已经有可用密钥，无需提取。")
        return 0

    print()
    print(f"即将为「{target['label']}」提取数据库密钥。")
    print("要求：企业微信当前登录的必须是这个企业的账号。")
    print("方式：只读附加企业微信进程，扫描约 90 秒；不会读取任何聊天内容。")
    confirm = input("确认继续？[y/N] ").strip().lower()
    if confirm != "y":
        return 0

    result = subprocess.run(
        [
            str(ROOT / "sidecar" / ".venv" / "bin" / "python"),
            str(CAPTURE),
            "capture",
            "--data-dir",
            target["path"],
            "--mode",
            "attach",
            "--confirm-attach",
            "--duration",
            "90",
        ],
    )
    print()
    print("=== 提取后状态 ===")
    ok = key_validates_for(Path(target["path"]))
    print("密钥验证:", "成功 ✓" if ok else "失败 ✗（确认企业微信登录的是该企业后重试）")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
