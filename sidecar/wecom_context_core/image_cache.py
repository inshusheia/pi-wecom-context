#!/usr/bin/env python3
"""Resolve locally cached WeCom image files for image messages in a snapshot.

链路（本机实测，2026-09-16）：
    message_table.content_type = 4/15（图片）
      → file.db:file_table4（message_id + 扩展类型）取 md5 键与原始大小
      → Documents/Profiles/<账号>/Caches/Images/<YYYY-MM>/<key>[_HD|_THUMB]/<企微实际文件名>

缓存文件是明文 JPEG/PNG（容器内已解密），因此可直接交给多模态模型；本模块只读，
不做解密、不改动企微任何文件。键必须是 32 位十六进制，禁止用它做路径拼接之外的用途。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from functools import lru_cache
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

WECOM_PROFILES_ROOT = Path(
    "~/Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles"
).expanduser()

CACHE_KIND = "Images"
KEY_RE = re.compile(r"^[0-9a-f]{32}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# file_table4.message_type 的实测语义（2026-09-16，真实快照）：
#   1 = 图片资源，2 = 文件（如 .docx/.pdf），3 = 视频（video.MP4），0 = 其它
# extension_type = 4 也只在图片行上出现，作为兼容条件一并接受；不能用消息侧的
# content_type 过滤，图片消息（content_type=4）的文件行 message_type 仍是 1。
IMAGE_FILE_MESSAGE_TYPES = (1,)
IMAGE_FILE_EXTENSION_TYPES = (4,)
# 只有这几种格式能直接交给多模态模型；其余一律标记为不支持，不做静默转换。
MODEL_READABLE_MIME = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_HEAD_BYTES = 64


@dataclass(frozen=True)
class ImageCandidate:
    """一个本地缓存候选文件。"""

    path: Path
    variant: str  # "hd" | "standard"
    size_bytes: int
    sha256: str
    mime_type: str | None
    width: int | None
    height: int | None
    content_matches_key: bool


@dataclass(frozen=True)
class ResolvedImage:
    """解析结果：status 直接对应 prepare-context 契约里的 image.status。"""

    key: str
    status: str  # original | thumbnail | unsupported | missing
    reason: str | None
    path: Path | None
    mime_type: str | None
    sha256: str | None
    size_bytes: int | None
    width: int | None
    height: int | None
    expected_size: int | None = None
    candidates_seen: int = field(default=0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_image(path: Path) -> tuple[str | None, int | None, int | None]:
    """按文件头识别真实格式与尺寸；识别不了就返回 None，绝不靠扩展名猜。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(MAX_HEAD_BYTES)
            if head[:3] == b"\xff\xd8\xff":
                return ("image/jpeg", *_jpeg_size(handle))
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                width = int.from_bytes(head[16:20], "big")
                height = int.from_bytes(head[20:24], "big")
                return ("image/png", width or None, height or None)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                width = int.from_bytes(head[6:8], "little")
                height = int.from_bytes(head[8:10], "little")
                return ("image/gif", width or None, height or None)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                return ("image/webp", *_webp_size(head))
    except OSError:
        return (None, None, None)
    return (None, None, None)


def _jpeg_size(handle) -> tuple[int | None, int | None]:
    """扫描 SOF 段取宽高；损坏文件返回 None 而不是抛异常。"""
    handle.seek(2)
    while True:
        marker = handle.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return (None, None)
        code = marker[1]
        if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
            continue
        length_bytes = handle.read(2)
        if len(length_bytes) < 2:
            return (None, None)
        length = int.from_bytes(length_bytes, "big")
        if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            body = handle.read(5)
            if len(body) < 5:
                return (None, None)
            return (int.from_bytes(body[3:5], "big"), int.from_bytes(body[1:3], "big"))
        if code == 0xDA:
            return (None, None)
        handle.seek(max(length - 2, 0), 1)


def _webp_size(head: bytes) -> tuple[int | None, int | None]:
    chunk = head[12:16]
    if chunk == b"VP8X" and len(head) >= 30:
        width = int.from_bytes(head[24:27], "little") + 1
        height = int.from_bytes(head[27:30], "little") + 1
        return (width, height)
    return (None, None)


def _safe_iterdir(path: Path) -> list[Path]:
    """容器目录扫描会被 macOS sandboxd/TCC 打断（InterruptedError 属 OSError），
    这里对单次中断重试一次，仍失败就返回空列表而不是让整条解析链失败。"""
    for attempt in (0, 1):
        try:
            return sorted(path.iterdir())
        except OSError:
            if attempt == 1:
                return []
def _find_entries(root: Path, minimum_depth: int, maximum_depth: int, entry_type: str) -> list[Path]:
    """用系统 find 枚举容器目录，绕过自定义二进制触发的 TCC 卡住。"""
    try:
        result = subprocess.run(
            [
                "/usr/bin/find",
                str(root),
                "-mindepth",
                str(minimum_depth),
                "-maxdepth",
                str(maximum_depth),
                "-type",
                entry_type,
                "-print",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [Path(line) for line in result.stdout.splitlines() if line]


def _find_directories(root: Path, minimum_depth: int, maximum_depth: int) -> list[Path]:
    return _find_entries(root, minimum_depth, maximum_depth, "d")


def _find_files(root: Path, minimum_depth: int, maximum_depth: int) -> list[Path]:
    return _find_entries(root, minimum_depth, maximum_depth, "f")

def cache_roots(profiles_root: Path | None = None) -> list[Path]:
    """返回所有账号的图片缓存根目录；系统 find 枚举避免 TCC 卡顿。"""
    root = (profiles_root or WECOM_PROFILES_ROOT).expanduser()
    candidates = _find_directories(root, 3, 3)
    return [candidate for candidate in candidates if candidate.name == CACHE_KIND and candidate.parent.name == "Caches"]
@lru_cache(maxsize=64)
def _month_directories(root: Path) -> tuple[Path, ...]:
    """缓存一次根目录月份枚举，避免逐图片重复触发 macOS 容器访问。"""
    months = [entry for entry in _safe_iterdir(root) if entry.is_dir() and MONTH_RE.fullmatch(entry.name)]
    months.sort(key=lambda entry: entry.name, reverse=True)
    return tuple(months)



def _cache_directory_index(roots: list[Path]) -> dict[str, list[tuple[Path, str]]]:
    """一次枚举所有缓存文件，避免逐键重复扫描容器目录。"""
    index: dict[str, list[tuple[Path, str]]] = {}
    for root in roots:
        for path in _find_files(root, 3, 3):
            directory = path.parent
            month = directory.parent.name
            if not MONTH_RE.fullmatch(month):
                continue
            name = directory.name
            variant = "standard"
            key = name
            if name.endswith("_HD"):
                key = name[:-3]
                variant = "hd"
            elif name.endswith("_THUMB"):
                key = name[:-6]
                variant = "thumb"
            if KEY_RE.fullmatch(key):
                index.setdefault(key, []).append((path, variant))
    return index
def downloaded_image_index(downloads_root: Path | None = None) -> dict[str, list[tuple[Path, str]]]:
    """索引 Downloads 中已导出的图片，优先使用清单保存的原始图片键。"""
    root = (downloads_root or (Path.home() / "Downloads")) / "WeCom Context"
    if not root.is_dir():
        return {}
    index: dict[str, list[tuple[Path, str]]] = {}
    indexed: set[Path] = set()

    def add(path: Path, key: str) -> None:
        if not KEY_RE.fullmatch(key):
            return
        try:
            if not path.is_file() or path.is_symlink():
                return
        except OSError:
            return
        index.setdefault(key, []).append((path, "downloaded"))
        indexed.add(path)

    for manifest_path in root.rglob("download-manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in manifest.get("files", []) if isinstance(manifest, dict) else []:
            if not isinstance(item, dict):
                continue
            path = manifest_path.parent / str(item.get("file") or "")
            key = str(item.get("key") or "").lower()
            if not KEY_RE.fullmatch(key):
                try:
                    key = _md5(path)
                except OSError:
                    continue
            add(path, key)

    for path in root.rglob("image_*"):
        if path in indexed:
            continue
        try:
            key = _md5(path)
        except OSError:
            continue
        add(path, key)
    return index



def _image_entries(
    key: str,
    *,
    roots: list[Path],
    directory_index: dict[str, list[tuple[Path, str]]] | None,
    fallback_index: dict[str, list[tuple[Path, str]]] | None,
) -> list[tuple[Path, str]]:
    if directory_index is not None:
        entries = list(directory_index.get(key, []))
    else:
        entries = [
            (path, variant)
            for root in roots
            for path, variant in candidate_paths(root, key)
        ]
    if fallback_index is not None:
        entries.extend(fallback_index.get(key, []))
    return entries



def _probe_candidates(entries: list[tuple[Path, str]], key: str) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    for path, variant in entries:
        try:
            mime, width, height = probe_image(path)
            size = path.stat().st_size
            candidates.append(
                ImageCandidate(path, variant, size, _sha256(path), mime, width, height, _md5(path) == key)
            )
        except OSError:
            continue
    return candidates





def candidate_paths(root: Path, key: str) -> list[tuple[Path, str]]:
    """列出某键在标准、高清和缩略图目录中的实际缓存文件。

    企业微信的缓存文件名不一定等于 md5 键，例如截图会保留
    ``企业微信截图_*.png`` 文件名；目录名才是稳定的图片键。
    """
    if not KEY_RE.fullmatch(key):
        return []
    found: list[tuple[Path, str]] = []
    for month in _month_directories(root):
        for suffix, variant in (("_HD", "hd"), ("", "standard"), ("_THUMB", "thumb")):
            directory = month / f"{key}{suffix}"
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in _safe_iterdir(directory):
                if path.is_file() and not path.is_symlink():
                    found.append((path, variant))
    return found

def resolve_image(
    key: str,
    *,
    expected_size: int | None = None,
    roots: list[Path] | None = None,
    directory_index: dict[str, list[tuple[Path, str]]] | None = None,
    fallback_index: dict[str, list[tuple[Path, str]]] | None = None,
    max_bytes: int | None = None,
    max_edge_pixels: int | None = None,
) -> ResolvedImage:
    """解析图片键，优先企微缓存，缓存缺失时复用已导出的图片文件。"""
    if not KEY_RE.fullmatch(key):
        return ResolvedImage(key, "missing", "图片键格式非法", None, None, None, None, None, None, expected_size)
    resolved_roots = cache_roots() if roots is None else roots
    entries = _image_entries(
        key,
        roots=resolved_roots,
        directory_index=directory_index,
        fallback_index=fallback_index,
    )
    candidates = _probe_candidates(entries, key)
    if not candidates:
        return ResolvedImage(
            key, "missing", "本地图片缓存中不存在该图片（未下载或已被清理）",
            None, None, None, None, None, None, expected_size, 0,
        )

    def rank(candidate: ImageCandidate) -> tuple[int, int, int, int]:
        return (
            0 if candidate.content_matches_key else 1,
            0 if expected_size is not None and candidate.size_bytes == expected_size else 1,
            0 if candidate.variant == "hd" else 1,
            -candidate.size_bytes,
        )

    best = min(candidates, key=rank)
    if best.mime_type is None:
        return ResolvedImage(
            key, "failed", "本地文件不是可识别的图片格式", best.path, None, best.sha256,
            best.size_bytes, None, None, expected_size, len(candidates),
        )
    if best.mime_type not in MODEL_READABLE_MIME:
        return ResolvedImage(
            key, "unsupported", f"暂不支持把 {best.mime_type} 交给模型",
            best.path, best.mime_type, best.sha256, best.size_bytes, best.width, best.height,
            expected_size, len(candidates),
        )
    if max_bytes is not None and best.size_bytes > max_bytes:
        return ResolvedImage(
            key, "too_large", f"图片 {best.size_bytes} 字节超过单张上限 {max_bytes} 字节，未发送",
            best.path, best.mime_type, best.sha256, best.size_bytes, best.width, best.height,
            expected_size, len(candidates),
        )
    longest_edge = max(value for value in (best.width, best.height) if value is not None) if (best.width and best.height) else None
    if max_edge_pixels is not None and longest_edge is not None and longest_edge > max_edge_pixels:
        return ResolvedImage(
            key, "too_large", f"图片长边 {longest_edge} 像素超过上限 {max_edge_pixels} 像素，未发送",
            best.path, best.mime_type, best.sha256, best.size_bytes, best.width, best.height,
            expected_size, len(candidates),
        )
    if best.content_matches_key or (expected_size is not None and best.size_bytes == expected_size):
        return ResolvedImage(
            key, "original", None, best.path, best.mime_type, best.sha256, best.size_bytes,
            best.width, best.height, expected_size, len(candidates),
        )
    reason = "本地仅有压缩版本"
    if expected_size is not None:
        reason = f"本地仅有压缩版本（{best.size_bytes} 字节，原始 {expected_size} 字节），细节可能不足"
    return ResolvedImage(
        key, "thumbnail", reason, best.path, best.mime_type, best.sha256, best.size_bytes,
        best.width, best.height, expected_size, len(candidates),
    )


@dataclass(frozen=True)
class Tile:
    """长截图的一个分片：无损裁剪，可直接作为独立图片交给模型。"""

    path: Path
    mime_type: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    index: int
    total: int


# macOS 自带 sips（Apple 签名，随系统提供）用于无损裁剪：本模块不引入图像处理依赖，
# 也不做会糊掉文字的整体缩放。
SIPS_BINARY = Path("/usr/bin/sips")
TILE_MIME = "image/jpeg"


def tiling_available() -> bool:
    return SIPS_BINARY.is_file()


def split_long_image(
    source: Path,
    *,
    width: int,
    height: int,
    max_edge_pixels: int,
    output_dir: Path,
    max_tiles: int = 12,
) -> tuple[list[Tile], int]:
    """把超长截图按 max_edge_pixels 高度无损分片。

    实测（macOS 26 / sips）：`-c H W --cropOffset top 0` 在 top+H 超出图像高度时会被
    sips 静默处理成不合预期的结果（boundary 情况直接返回整图）。因此这里：
    1) 每片都按完整片高裁剪，末片向前重叠（不产生过短的片）；
    2) 逐片校验输出尺寸必须与请求完全一致，不一致就左移 1 像素重试一次；
    3) 仍不一致的片直接丢弃并计入 omitted，**绝不把未裁剪的整图当成分片交给模型**。

    返回 (分片列表, 未生成的分片数)。
    """
    if not tiling_available() or max_edge_pixels <= 0 or width <= 0 or height <= max_edge_pixels:
        return ([], 0)
    output_dir.mkdir(parents=True, exist_ok=True)
    tile_height = min(max_edge_pixels, height)
    total_expected = -(-height // max_edge_pixels)
    send_count = min(total_expected, max(max_tiles, 1))
    tiles: list[Tile] = []
    for index in range(send_count):
        top = index * max_edge_pixels
        if top + tile_height > height:
            # 末片向前重叠：保证每片都是完整片高，重叠部分无副作用。
            top = max(height - tile_height, 0)
        target = output_dir / f"tile_{index + 1:02d}.jpg"
        cropped = False
        for shift in (0, 1):
            candidate = max(top - shift, 0)
            if _crop_with_sips(source, top=candidate, height=tile_height, width=width, target=target):
                cropped = True
                break
        if not cropped:
            target.unlink(missing_ok=True)
            continue
        tiles.append(
            Tile(
                path=target,
                mime_type=TILE_MIME,
                width=width,
                height=tile_height,
                size_bytes=target.stat().st_size,
                sha256=_sha256(target),
                index=index + 1,
                total=total_expected,
            )
        )
    omitted = max(total_expected - len(tiles), 0)
    return (tiles, omitted)


def _crop_with_sips(source: Path, *, top: int, height: int, width: int, target: Path) -> bool:
    """裁剪一片并严格校验输出尺寸；尺寸不符一律视为失败。"""
    target.unlink(missing_ok=True)
    result = subprocess.run(
        [
            str(SIPS_BINARY),
            "-c",
            str(height),
            str(width),
            "--cropOffset",
            str(top),
            "0",
            str(source),
            "--out",
            str(target),
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not target.is_file():
        return False
    mime_type, out_width, out_height = probe_image(target)
    return mime_type is not None and out_width == width and out_height == height


def _field_key(info_extension: object) -> str | None:
    """file_table4.info_extension 里内嵌的 32 位十六进制键（md5 为空时使用）。"""
    if not info_extension:
        return None
    if isinstance(info_extension, str):
        info_extension = info_extension.encode("utf-8", "ignore")
    match = re.search(rb"([0-9a-f]{32})", bytes(info_extension))
    return match.group(1).decode() if match else None


def image_keys_for_messages(snapshot: Path, message_ids: list[str]) -> dict[str, list[dict]]:
    """从快照的 file.db 取消息 → 图片键映射，保持 file_index 顺序。"""
    wanted = {str(value) for value in message_ids if str(value)}
    result: dict[str, list[dict]] = {value: [] for value in wanted}
    if not wanted:
        return result
    path = snapshot / "file.db"
    if not path.is_file() or path.is_symlink():
        return result
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(file_table4)")}
        if not columns:
            return result
        selected = [
            name
            for name in ("message_id", "file_index", "message_type", "extension_type", "name", "size", "md5", "info_extension")
            if name in columns
        ]
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM file_table4"
        ).fetchall()
    except sqlite3.DatabaseError:
        return result
    finally:
        connection.close()
    for row in rows:
        record = dict(zip(selected, row))
        message_id = str(record.get("message_id") or "")
        if message_id not in wanted:
            continue
        extension_type = record.get("extension_type")
        message_type = record.get("message_type")
        is_image = any(
            isinstance(value, int) and value in IMAGE_FILE_MESSAGE_TYPES
            for value in (message_type,)
        ) or any(
            isinstance(value, int) and value in IMAGE_FILE_EXTENSION_TYPES
            for value in (extension_type,)
        )
        if not is_image:
            continue
        key = str(record.get("md5") or "").lower()
        if not KEY_RE.fullmatch(key):
            key = _field_key(record.get("info_extension")) or ""
        if not KEY_RE.fullmatch(key):
            continue
        size = record.get("size")
        result[message_id].append(
            {
                "key": key,
                "file_index": int(record.get("file_index") or 0),
                "name": str(record.get("name") or ""),
                "size": int(size) if isinstance(size, int) and size > 0 else None,
            }
        )
    for values in result.values():
        values.sort(key=lambda item: item["file_index"])
    return result
