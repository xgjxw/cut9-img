from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import sys
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.request import urlopen

from Crypto.Cipher import AES
from PIL import Image, ImageDraw, ImageSequence

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
MIN_RESIZE_EDGE = 32
PAGE_SZ = 4096
SALT_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b"SQLite format 3\x00"
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24


@dataclass
class SourceInfo:
    source: Path
    source_format: str
    animated: bool
    width: int
    height: int
    frames: int
    source_sha256: str


@dataclass
class ManifestEntry:
    source: str
    output: str
    source_format: str
    requested_format: str
    output_format: str
    width: int
    height: int
    frames: int
    animated: bool
    mode: int
    size: str
    source_sha256: str
    output_sha256: str
    bytes: int
    copied_directly: bool
    status: str
    note: str


@dataclass
class FailureEntry:
    source: str
    error: str


@dataclass
class SyncItem:
    fav_rowid: int
    nonstore_rowid: int
    type: int
    md5: str
    aes_key: str
    cdn_url: str
    extern_url: str
    extern_md5: str
    encrypt_url: str
    exported_file: str = ""


@dataclass
class SheetTile:
    index: int
    row: int
    col: int
    source_box: tuple[int, int, int, int]
    trimmed_box: tuple[int, int, int, int]
    output: str
    width: int
    height: int
    output_format: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_size(value: str) -> int:
    if value == "raw":
        return 0
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("size must be raw or a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("size must be > 0")
    return parsed


def parse_max_bytes(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max-bytes must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("max-bytes must be > 0")
    return parsed


def gather_inputs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() not in SUPPORTED_EXTS:
            raise ValueError(f"unsupported input format: {input_path.suffix}")
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(f"input not found: {input_path}")
    files = [
        path
        for path in input_path.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS
    ]
    return sorted(files)


def fit_size(width: int, height: int, force_size: int) -> tuple[int, int]:
    if force_size <= 0:
        return width, height
    longest = max(width, height)
    if longest == force_size:
        return width, height
    scale = force_size / longest
    new_w = max(1, round(width * scale))
    new_h = max(1, round(height * scale))
    return new_w, new_h


def resize_frame(frame: Image.Image, force_size: int) -> Image.Image:
    frame = frame.convert("RGBA")
    new_size = fit_size(frame.width, frame.height, force_size)
    if new_size != (frame.width, frame.height):
        frame = frame.resize(new_size, Image.Resampling.LANCZOS)
    return frame


def quantize_rgba(frame: Image.Image, mode: int) -> Image.Image:
    if mode == 2:
        pal = frame.quantize(method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    else:
        alpha = frame.getchannel("A")
        rgb = Image.new("RGB", frame.size, (0, 0, 0))
        rgb.paste(frame, mask=alpha)
        pal = rgb.convert(
            "P",
            palette=Image.Palette.ADAPTIVE,
            colors=255,
            dither=Image.Dither.FLOYDSTEINBERG,
        )

    alpha = frame.getchannel("A")
    if alpha.getbbox() is not None and alpha.getextrema()[0] < 255:
        transparent_mask = alpha.point(lambda value: 255 if value <= 127 else 0)
        pal.paste(255, mask=transparent_mask)
        pal.info["transparency"] = 255
    return pal


def inspect_source(path: Path) -> SourceInfo:
    with Image.open(path) as image:
        source_format = (image.format or path.suffix.lstrip(".")).lower()
        animated = bool(getattr(image, "is_animated", False))
        frames = getattr(image, "n_frames", 1)
        width, height = image.size
    return SourceInfo(
        source=path,
        source_format=source_format,
        animated=animated,
        width=width,
        height=height,
        frames=frames,
        source_sha256=sha256_file(path),
    )


def render_png(source: Path, target: Path, force_size: int, transparent_bg: bool = False) -> tuple[int, int, int]:
    with Image.open(source) as image:
        image = resize_frame(image, force_size)
        if transparent_bg:
            image = make_background_transparent(
                image,
                bg_rgb=sample_background_rgb(image),
                bg_tolerance=18,
                white_threshold=245,
            )
        width, height = image.size
        image.save(target, format="PNG", optimize=True)
    return width, height, 1


def render_gif(source: Path, target: Path, force_size: int, mode: int, transparent_bg: bool = False) -> tuple[int, int, int]:
    with Image.open(source) as image:
        durations: list[int] = []
        frames: list[Image.Image] = []

        for frame in ImageSequence.Iterator(image):
            rgba = resize_frame(frame, force_size)
            if transparent_bg:
                rgba = make_background_transparent(
                    rgba,
                    bg_rgb=sample_background_rgb(rgba),
                    bg_tolerance=18,
                    white_threshold=245,
                )
            frames.append(quantize_rgba(rgba, mode))
            durations.append(frame.info.get("duration", image.info.get("duration", 100)))

        if not frames:
            raise ValueError(f"no frames decoded from: {source}")

        width, height = frames[0].size
        save_kwargs = {
            "format": "GIF",
            "save_all": True,
            "append_images": frames[1:],
            "optimize": True,
            "duration": durations,
            "loop": image.info.get("loop", 0),
            "disposal": 2,
        }
        if "transparency" in frames[0].info:
            save_kwargs["transparency"] = frames[0].info["transparency"]
        frames[0].save(target, **save_kwargs)
    return width, height, len(frames)


def maybe_copy_direct(
    source: Path,
    target: Path,
    requested_size: int,
    resolved_format: str,
    keep_gif: bool,
    max_bytes: int | None,
) -> bool:
    if not keep_gif:
        return False
    if requested_size != 0:
        return False
    if resolved_format != "gif":
        return False
    if max_bytes is not None:
        return False
    if source.suffix.lower() != ".gif":
        return False
    shutil.copy2(source, target)
    return True


def relative_target(source_root: Path, source_file: Path, output_root: Path, output_format: str) -> Path:
    if source_root.is_file():
        rel = Path(source_file.stem)
    else:
        rel = source_file.relative_to(source_root).with_suffix("")
    ext = ".png" if output_format == "png" else ".gif"
    return output_root / rel.with_suffix(ext)


def resolve_output_format(requested_format: str, source_info: SourceInfo, wechat_safe: bool) -> str:
    if requested_format != "auto":
        return requested_format
    if source_info.animated or source_info.source.suffix.lower() == ".gif":
        return "gif"
    if wechat_safe:
        return "gif"
    return "png"


def initial_resize_edge(source_info: SourceInfo, requested_size: int) -> int:
    if requested_size > 0:
        return requested_size
    return max(source_info.width, source_info.height)


def shrink_edge(current_edge: int) -> int:
    next_edge = max(MIN_RESIZE_EDGE, int(current_edge * 0.9))
    if next_edge >= current_edge:
        next_edge = current_edge - 1
    return max(MIN_RESIZE_EDGE, next_edge)


def render_with_limit(
    source_info: SourceInfo,
    target: Path,
    requested_size: int,
    resolved_format: str,
    mode: int,
    max_bytes: int | None,
    transparent_bg: bool = False,
) -> tuple[int, int, int, int, str]:
    current_edge = initial_resize_edge(source_info, requested_size)
    note = ""

    while True:
        force_size = 0 if requested_size == 0 and current_edge >= max(source_info.width, source_info.height) else current_edge
        if resolved_format == "png":
            width, height, frames = render_png(source_info.source, target, force_size, transparent_bg)
        else:
            width, height, frames = render_gif(source_info.source, target, force_size, mode, transparent_bg)

        file_size = target.stat().st_size
        if max_bytes is None or file_size <= max_bytes:
            if current_edge != initial_resize_edge(source_info, requested_size):
                note = f"resized down to fit max-bytes={max_bytes}"
            return width, height, frames, file_size, note

        next_edge = shrink_edge(current_edge)
        if next_edge >= current_edge or next_edge == MIN_RESIZE_EDGE == current_edge:
            note = f"still exceeds max-bytes={max_bytes}"
            return width, height, frames, file_size, note
        current_edge = next_edge


def build_manifest_entry(
    source_info: SourceInfo,
    output: Path,
    requested_format: str,
    resolved_format: str,
    mode: int,
    size_label: str,
    copied_directly: bool,
    status: str,
    note: str,
    width: int,
    height: int,
    frames: int,
    bytes_out: int,
    output_sha256: str,
) -> ManifestEntry:
    return ManifestEntry(
        source=str(source_info.source),
        output=str(output),
        source_format=source_info.source_format,
        requested_format=requested_format,
        output_format=resolved_format,
        width=width,
        height=height,
        frames=frames,
        animated=source_info.animated,
        mode=mode,
        size=size_label,
        source_sha256=source_info.source_sha256,
        output_sha256=output_sha256,
        bytes=bytes_out,
        copied_directly=copied_directly,
        status=status,
        note=note,
    )


def convert_one(
    source_root: Path,
    source_info: SourceInfo,
    output_root: Path,
    requested_size: int,
    requested_format: str,
    mode: int,
    keep_gif: bool,
    dry_run: bool,
    max_bytes: int | None,
    wechat_safe: bool,
    transparent_bg: bool = False,
) -> ManifestEntry:
    resolved_format = resolve_output_format(requested_format, source_info, wechat_safe)
    target = relative_target(source_root, source_info.source, output_root, resolved_format)
    target.parent.mkdir(parents=True, exist_ok=True)

    width, height = fit_size(source_info.width, source_info.height, requested_size)
    frames = source_info.frames
    bytes_out = 0
    output_sha256 = ""
    copied_directly = False
    note = ""

    if not dry_run:
        copied_directly = maybe_copy_direct(
            source=source_info.source,
            target=target,
            requested_size=requested_size,
            resolved_format=resolved_format,
            keep_gif=keep_gif and not transparent_bg,
            max_bytes=max_bytes,
        )
        if copied_directly:
            bytes_out = target.stat().st_size
            output_sha256 = sha256_file(target)
            with Image.open(target) as copied:
                width, height = copied.size
                frames = getattr(copied, "n_frames", 1)
        else:
            width, height, frames, bytes_out, note = render_with_limit(
                source_info=source_info,
                target=target,
                requested_size=requested_size,
                resolved_format=resolved_format,
                mode=mode,
                max_bytes=max_bytes,
                transparent_bg=transparent_bg,
            )
            output_sha256 = sha256_file(target)

    return build_manifest_entry(
        source_info=source_info,
        output=target,
        requested_format=requested_format,
        resolved_format=resolved_format,
        mode=mode,
        size_label="raw" if requested_size == 0 else str(requested_size),
        copied_directly=copied_directly,
        status="ok",
        note=note,
        width=width,
        height=height,
        frames=frames,
        bytes_out=bytes_out,
        output_sha256=output_sha256,
    )


def write_manifest(output_root: Path, entries: Iterable[ManifestEntry], dry_run: bool) -> Path:
    entries = list(entries)
    manifest_path = output_root / "manifest.json"
    payload = {
        "tool": "meme-cli",
        "dry_run": dry_run,
        "count": len(entries),
        "items": [asdict(item) for item in entries],
    }
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def write_manifest_csv(output_root: Path, entries: Iterable[ManifestEntry]) -> Path:
    entries = list(entries)
    csv_path = output_root / "manifest.csv"
    output_root.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(entries[0]).keys()) if entries else [
            "source",
            "output",
            "source_format",
            "requested_format",
            "output_format",
            "width",
            "height",
            "frames",
            "animated",
            "mode",
            "size",
            "source_sha256",
            "output_sha256",
            "bytes",
            "copied_directly",
            "status",
            "note",
        ])
        writer.writeheader()
        for item in entries:
            writer.writerow(asdict(item))
    return csv_path


def write_failures(output_root: Path, failures: Iterable[FailureEntry]) -> Path | None:
    failures = list(failures)
    if not failures:
        return None
    failure_path = output_root / "failures.json"
    payload = {
        "count": len(failures),
        "items": [asdict(item) for item in failures],
    }
    output_root.mkdir(parents=True, exist_ok=True)
    failure_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return failure_path


def summarize(entries: Iterable[ManifestEntry], failures: Iterable[FailureEntry], dry_run: bool) -> dict:
    entry_list = list(entries)
    failure_list = list(failures)
    ok_items = [item for item in entry_list if item.status == "ok"]
    skipped_items = [item for item in entry_list if item.status == "skipped_duplicate"]
    output_formats: dict[str, int] = {}
    source_formats: dict[str, int] = {}
    status_counts: dict[str, int] = {}

    for item in entry_list:
        output_formats[item.output_format] = output_formats.get(item.output_format, 0) + 1
        source_formats[item.source_format] = source_formats.get(item.source_format, 0) + 1
        status_counts[item.status] = status_counts.get(item.status, 0) + 1

    total_bytes = sum(item.bytes for item in ok_items)
    avg_bytes = round(total_bytes / len(ok_items), 2) if ok_items else 0

    return {
        "tool": "meme-cli",
        "dry_run": dry_run,
        "totals": {
            "entries": len(entry_list),
            "ok": len(ok_items),
            "skipped_duplicate": len(skipped_items),
            "failed": len(failure_list),
            "total_output_bytes": total_bytes,
            "average_output_bytes": avg_bytes,
        },
        "breakdown": {
            "output_formats": output_formats,
            "source_formats": source_formats,
            "status": status_counts,
        },
        "largest_outputs": [
            {
                "source": item.source,
                "output": item.output,
                "bytes": item.bytes,
                "width": item.width,
                "height": item.height,
            }
            for item in sorted(ok_items, key=lambda entry: entry.bytes, reverse=True)[:10]
        ],
    }


def write_summary(output_root: Path, summary: dict) -> Path:
    summary_path = output_root / "summary.json"
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary_path


def default_state_home() -> Path:
    return Path.home() / ".wechat-cli"


def load_wechat_config(config_path: Path | None) -> dict:
    path = config_path or (default_state_home() / "config.json")
    if not path.exists():
        raise FileNotFoundError(f"wechat-cli config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_keys(keys_path: Path | None) -> dict:
    path = keys_path or (default_state_home() / "all_keys.json")
    if not path.exists():
        raise FileNotFoundError(f"wechat-cli keys not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytes(bytearray(SQLITE_HDR + decrypted + b"\x00" * RESERVE_SZ))

    encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
    cipher = AES.new(enc_key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(encrypted)
    return decrypted + b"\x00" * RESERVE_SZ


def full_decrypt(db_path: Path, out_path: Path, enc_key: bytes) -> None:
    file_size = db_path.stat().st_size
    total_pages = file_size // PAGE_SZ
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with db_path.open("rb") as fin, out_path.open("wb") as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if not page:
                    break
                page = page + b"\x00" * (PAGE_SZ - len(page))
            fout.write(decrypt_page(enc_key, page, pgno))


def decrypt_wal(wal_path: Path, out_path: Path, enc_key: bytes) -> int:
    if not wal_path.exists():
        return 0
    wal_size = wal_path.stat().st_size
    if wal_size <= WAL_HEADER_SZ:
        return 0

    patched = 0
    with wal_path.open("rb") as wf, out_path.open("r+b") as df:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        wal_salt1 = struct.unpack(">I", wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack(">I", wal_hdr[20:24])[0]
        frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ

        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack(">I", fh[0:4])[0]
            frame_salt1 = struct.unpack(">I", fh[8:12])[0]
            frame_salt2 = struct.unpack(">I", fh[12:16])[0]
            encrypted_page = wf.read(PAGE_SZ)
            if len(encrypted_page) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1000000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue
            dec = decrypt_page(enc_key, encrypted_page, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)
            patched += 1
    return patched


def decrypt_emoticon_db(report_dir: Path, db_dir: Path, keys: dict) -> Path:
    rel_key = r"emoticon\emoticon.db"
    if rel_key not in keys:
        raise KeyError("emoticon\\emoticon.db not found in all_keys.json")
    enc_key = bytes.fromhex(keys[rel_key]["enc_key"])
    src_db = db_dir / "emoticon" / "emoticon.db"
    src_wal = db_dir / "emoticon" / "emoticon.db-wal"
    out_db = report_dir / ".work" / "emoticon.db"
    full_decrypt(src_db, out_db, enc_key)
    decrypt_wal(src_wal, out_db, enc_key)
    return out_db


def load_sync_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"last_fav_rowid": 0}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_sync_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def query_sync_items(db_path: Path, since_rowid: int) -> list[SyncItem]:
    sql = """
    select
      fav.rowid as fav_rowid,
      n.rowid as nonstore_rowid,
      n.type,
      n.md5,
      coalesce(n.aes_key, ''),
      coalesce(n.cdn_url, ''),
      coalesce(n.extern_url, ''),
      coalesce(n.extern_md5, ''),
      coalesce(n.encrypt_url, '')
    from kFavEmoticonOrderTable fav
    join kNonStoreEmoticonTable n on fav.md5 = n.md5
    where fav.rowid > ?
    order by fav.rowid asc
    """
    items: list[SyncItem] = []
    with sqlite3.connect(db_path) as conn:
        for row in conn.execute(sql, (since_rowid,)):
            items.append(SyncItem(*row))
    return items


def current_max_fav_rowid(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("select coalesce(max(rowid), 0) from kFavEmoticonOrderTable").fetchone()
    return int(row[0]) if row else 0


def fetch_cdn_exports(report_dir: Path, items: list[SyncItem]) -> None:
    export_dir = report_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    for item in items:
        if not item.cdn_url:
            continue
        target = export_dir / f"{item.md5}.png"
        try:
            with urlopen(item.cdn_url, timeout=20) as resp:
                data = resp.read()
            target.write_bytes(data)
            item.exported_file = str(target)
        except Exception:
            item.exported_file = ""


def write_sync_report(report_dir: Path, payload: dict) -> Path:
    report_path = report_dir / "sync_report.json"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def sample_background_rgb(image: Image.Image) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    px = rgb.load()
    w, h = rgb.size
    points = [
        (0, 0),
        (w - 1, 0),
        (0, h - 1),
        (w - 1, h - 1),
        (w // 2, 0),
        (w // 2, h - 1),
        (0, h // 2),
        (w - 1, h // 2),
    ]
    rs = sorted(px[x, y][0] for x, y in points)
    gs = sorted(px[x, y][1] for x, y in points)
    bs = sorted(px[x, y][2] for x, y in points)
    mid = len(points) // 2
    return rs[mid], gs[mid], bs[mid]


def is_bg_like(
    rgba: tuple[int, int, int, int],
    bg_rgb: tuple[int, int, int],
    bg_tolerance: int,
    white_threshold: int,
    include_near_white: bool = True,
) -> bool:
    r, g, b, a = rgba
    if a <= 16:
        return True
    if include_near_white and r >= white_threshold and g >= white_threshold and b >= white_threshold and a >= 250:
        return True
    return (
        abs(r - bg_rgb[0]) <= bg_tolerance
        and abs(g - bg_rgb[1]) <= bg_tolerance
        and abs(b - bg_rgb[2]) <= bg_tolerance
        and a >= 250
    )


def build_bg_like_mask(
    image: Image.Image,
    bg_rgb: tuple[int, int, int],
    bg_tolerance: int,
    white_threshold: int,
    *,
    include_near_white: bool,
) -> list[list[bool]]:
    rgba = image.convert("RGBA")
    px = rgba.load()
    w, h = rgba.size
    mask: list[list[bool]] = [[False] * w for _ in range(h)]
    for y in range(h):
        row = mask[y]
        for x in range(w):
            row[x] = is_bg_like(
                px[x, y],
                bg_rgb=bg_rgb,
                bg_tolerance=bg_tolerance,
                white_threshold=white_threshold,
                include_near_white=include_near_white,
            )
    return mask


def edge_connected_background(bg_like_mask: list[list[bool]], *, min_neighbor_bg: int = 5) -> list[list[bool]]:
    h = len(bg_like_mask)
    w = len(bg_like_mask[0]) if h else 0
    connected: list[list[bool]] = [[False] * w for _ in range(h)]
    queue: deque[tuple[int, int]] = deque()

    def enqueue(x: int, y: int) -> None:
        if x < 0 or y < 0 or x >= w or y >= h:
            return
        if connected[y][x] or not bg_like_mask[y][x]:
            return
        connected[y][x] = True
        queue.append((x, y))

    for x in range(w):
        enqueue(x, 0)
        enqueue(x, h - 1)
    for y in range(h):
        enqueue(0, y)
        enqueue(w - 1, y)

    while queue:
        x, y = queue.popleft()
        for ny in range(max(0, y - 1), min(h, y + 2)):
            for nx in range(max(0, x - 1), min(w, x + 2)):
                if nx == x and ny == y:
                    continue
                if connected[ny][nx] or not bg_like_mask[ny][nx]:
                    continue
                if 0 < nx < w - 1 and 0 < ny < h - 1:
                    neighbors = 0
                    for yy in range(ny - 1, ny + 2):
                        for xx in range(nx - 1, nx + 2):
                            if xx == nx and yy == ny:
                                continue
                            if bg_like_mask[yy][xx]:
                                neighbors += 1
                    if neighbors < min_neighbor_bg:
                        continue
                connected[ny][nx] = True
                queue.append((nx, ny))

    return connected


def build_content_mask(
    image: Image.Image,
    bg_rgb: tuple[int, int, int],
    bg_tolerance: int,
    white_threshold: int,
) -> tuple[list[list[bool]], list[int], list[int]]:
    w, h = image.size
    bg_like = build_bg_like_mask(
        image,
        bg_rgb=bg_rgb,
        bg_tolerance=bg_tolerance,
        white_threshold=white_threshold,
        include_near_white=False,
    )
    edge_bg = edge_connected_background(bg_like)
    mask: list[list[bool]] = [[False] * w for _ in range(h)]
    col_counts = [0] * w
    row_counts = [0] * h

    for y in range(h):
        row = mask[y]
        for x in range(w):
            content = not edge_bg[y][x]
            row[x] = content
            if content:
                row_counts[y] += 1
                col_counts[x] += 1
    return mask, col_counts, row_counts


def pick_separator_centers(counts: list[int], segments: int, search_radius: int) -> list[int]:
    length = len(counts)
    cuts: list[int] = []
    for step in range(1, segments):
        target = round(length * step / segments)
        start = max(1, target - search_radius)
        end = min(length - 2, target + search_radius)
        window = counts[start : end + 1]
        local_min = min(window)
        local_max = max(window)
        threshold = local_min + max(1, int((local_max - local_min) * 0.12))
        min_offset = window.index(local_min)
        min_idx = start + min_offset

        left = min_idx
        while left > start and counts[left - 1] <= threshold:
            left -= 1
        right = min_idx
        while right < end and counts[right + 1] <= threshold:
            right += 1
        cuts.append((left + right) // 2)
    return cuts


def even_grid_edges(length: int, segments: int) -> list[int]:
    return [round(length * step / segments) for step in range(segments + 1)]


def trim_box_to_content(
    mask: list[list[bool]],
    box: tuple[int, int, int, int],
    pad: int,
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    min_x, min_y = x1, y1
    max_x, max_y = x0 - 1, y0 - 1

    for y in range(y0, y1):
        row = mask[y]
        for x in range(x0, x1):
            if row[x]:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y

    if max_x < min_x or max_y < min_y:
        return box

    width = x1 - x0
    height = y1 - y0
    max_trim_x = max(pad, round(width * 0.08))
    max_trim_y = max(pad, round(height * 0.08))

    min_x = max(x0, min_x - pad)
    min_y = max(y0, min_y - pad)
    max_x = min(x1 - 1, max_x + pad)
    max_y = min(y1 - 1, max_y + pad)
    min_x = min(min_x, x0 + max_trim_x)
    min_y = min(min_y, y0 + max_trim_y)
    max_x = max(max_x, x1 - 1 - max_trim_x)
    max_y = max(max_y, y1 - 1 - max_trim_y)
    return min_x, min_y, max_x + 1, max_y + 1


def make_background_transparent(
    image: Image.Image,
    bg_rgb: tuple[int, int, int],
    bg_tolerance: int,
    white_threshold: int,
) -> Image.Image:
    rgba = image.convert("RGBA")
    px = rgba.load()
    w, h = rgba.size
    bg_like = build_bg_like_mask(
        rgba,
        bg_rgb=bg_rgb,
        bg_tolerance=max(6, min(bg_tolerance, 12)),
        white_threshold=white_threshold,
        include_near_white=False,
    )
    edge_bg = edge_connected_background(bg_like, min_neighbor_bg=6)
    for y in range(h):
        for x in range(w):
            if edge_bg[y][x]:
                px[x, y] = (255, 255, 255, 0)
    return rgba


def fit_tile_to_square(image: Image.Image, size: int, transparent_bg: bool) -> Image.Image:
    if size <= 0:
        return image
    base = (0, 0, 0, 0) if transparent_bg else (255, 255, 255, 255)
    canvas = Image.new("RGBA", (size, size), base)
    scale = min(size / image.width, size / image.height)
    new_w = max(1, round(image.width * scale))
    new_h = max(1, round(image.height * scale))
    resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    offset = ((size - new_w) // 2, (size - new_h) // 2)
    canvas.paste(resized, offset, resized)
    return canvas


def save_sheet_tile(image: Image.Image, target: Path, output_format: str, mode: int) -> tuple[int, int]:
    if output_format == "png":
        image.save(target, format="PNG", optimize=True)
        return image.width, image.height

    pal = quantize_rgba(image.convert("RGBA"), mode)
    save_kwargs = {"format": "GIF", "save_all": False, "optimize": True, "disposal": 2}
    if "transparency" in pal.info:
        save_kwargs["transparency"] = pal.info["transparency"]
    pal.save(target, **save_kwargs)
    return image.width, image.height


def write_split_report(output_dir: Path, tiles: list[SheetTile], meta: dict) -> Path:
    report_path = output_dir / "sheet_report.json"
    payload = {
        "meta": meta,
        "count": len(tiles),
        "tiles": [
            {
                **asdict(tile),
                "source_box": list(tile.source_box),
                "trimmed_box": list(tile.trimmed_box),
            }
            for tile in tiles
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def write_split_preview(
    source: Image.Image,
    output_dir: Path,
    x_edges: list[int],
    y_edges: list[int],
    tiles: list[SheetTile],
) -> Path:
    preview = source.convert("RGBA").copy()
    draw = ImageDraw.Draw(preview)
    for x in x_edges[1:-1]:
        draw.line([(x, 0), (x, preview.height)], fill=(255, 0, 0, 255), width=2)
    for y in y_edges[1:-1]:
        draw.line([(0, y), (preview.width, y)], fill=(255, 0, 0, 255), width=2)
    for tile in tiles:
        x0, y0, x1, y1 = tile.trimmed_box
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(0, 180, 255, 255), width=2)
        draw.text((x0 + 4, y0 + 4), f"{tile.index:02d}", fill=(255, 80, 0, 255))
    preview_path = output_dir / "preview_boxes.png"
    output_dir.mkdir(parents=True, exist_ok=True)
    preview.save(preview_path, format="PNG", optimize=True)
    return preview_path


def run_split_sheet(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    source = Image.open(input_path).convert("RGBA")
    bg_rgb = sample_background_rgb(source)
    mask, col_counts, row_counts = build_content_mask(
        source,
        bg_rgb=bg_rgb,
        bg_tolerance=args.bg_tolerance,
        white_threshold=args.white_threshold,
    )

    cell_w = source.width / args.cols
    cell_h = source.height / args.rows
    x_edges = even_grid_edges(source.width, args.cols)
    y_edges = even_grid_edges(source.height, args.rows)

    tiles: list[SheetTile] = []
    index_width = len(str(args.rows * args.cols))

    for row_idx in range(args.rows):
        for col_idx in range(args.cols):
            x0, x1 = x_edges[col_idx], x_edges[col_idx + 1]
            y0, y1 = y_edges[row_idx], y_edges[row_idx + 1]
            source_box = (x0, y0, x1, y1)
            trimmed_box = trim_box_to_content(mask, source_box, args.trim_pad)
            tile = source.crop(trimmed_box)
            if args.transparent_bg:
                tile = make_background_transparent(
                    tile,
                    bg_rgb=bg_rgb,
                    bg_tolerance=args.bg_tolerance,
                    white_threshold=args.white_threshold,
                )
            else:
                tile = tile.convert("RGBA")
            tile = fit_tile_to_square(tile, args.size, args.transparent_bg)

            index = row_idx * args.cols + col_idx + 1
            ext = ".png" if args.format == "png" else ".gif"
            target = output_dir / f"{index:0{index_width}d}_r{row_idx + 1}c{col_idx + 1}{ext}"
            width, height = save_sheet_tile(tile, target, args.format, args.mode)
            tiles.append(
                SheetTile(
                    index=index,
                    row=row_idx + 1,
                    col=col_idx + 1,
                    source_box=source_box,
                    trimmed_box=trimmed_box,
                    output=str(target),
                    width=width,
                    height=height,
                    output_format=args.format,
                )
            )

    preview_path = write_split_preview(source, output_dir, x_edges, y_edges, tiles)
    report_path = write_split_report(
        output_dir,
        tiles,
        meta={
            "input": str(input_path),
            "rows": args.rows,
            "cols": args.cols,
            "format": args.format,
            "size": args.size,
            "transparent_bg": args.transparent_bg,
            "bg_rgb": list(bg_rgb),
            "x_edges": x_edges,
            "y_edges": y_edges,
        },
    )

    print(f"tiles: {len(tiles)}")
    print(f"preview: {preview_path}")
    print(f"report: {report_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meme-cli", description="Batch convert images into WeChat-friendly meme assets")
    sub = parser.add_subparsers(dest="command", required=True)

    convert = sub.add_parser("convert", help="Convert one image or a whole directory")
    convert.add_argument("input", help="Input file or directory")
    convert.add_argument("output", help="Output directory")
    convert.add_argument("--mode", choices=(1, 2), type=int, default=1, help="GIF quantization mode")
    convert.add_argument("--size", default="200", type=parse_size, help="Longest edge, raw or positive integer")
    convert.add_argument("--format", choices=("gif", "png", "auto"), default="gif", help="Output format")
    convert.add_argument("--keep-gif", action="store_true", help="Copy source GIF directly when possible")
    convert.add_argument("--dedupe", action="store_true", help="Skip duplicate source files by SHA256")
    convert.add_argument("--max-bytes", type=parse_max_bytes, default=None, help="Try shrinking output until file size fits")
    convert.add_argument("--wechat-safe", action="store_true", help="Prefer safer WeChat defaults")
    convert.add_argument("--transparent-bg", action="store_true", help="Turn detected background transparent")
    convert.add_argument("--manifest-csv", action="store_true", help="Also write manifest.csv")
    convert.add_argument("--dry-run", action="store_true", help="Scan only, do not write output files")

    sync_scan = sub.add_parser("sync-scan", help="Scan newly synced WeChat custom stickers")
    sync_scan.add_argument("report_dir", help="Directory to store reports/state/exports")
    sync_scan.add_argument("--config", default=None, help="Path to ~/.wechat-cli/config.json")
    sync_scan.add_argument("--keys-file", default=None, help="Path to ~/.wechat-cli/all_keys.json")
    sync_scan.add_argument("--state-file", default=None, help="Override state file path")
    sync_scan.add_argument("--since-rowid", type=int, default=None, help="Override last_fav_rowid for one-off scan")
    sync_scan.add_argument("--baseline", action="store_true", help="Only record current max rowid, do not emit items")
    sync_scan.add_argument("--download-cdn", action="store_true", help="Download cdn_url assets into report_dir/exports")

    split_sheet = sub.add_parser("split-sheet", help="Split a regular sticker sheet into single tiles")
    split_sheet.add_argument("input", help="Input sheet image")
    split_sheet.add_argument("output", help="Output directory")
    split_sheet.add_argument("--rows", type=int, required=True, help="Grid rows")
    split_sheet.add_argument("--cols", type=int, required=True, help="Grid columns")
    split_sheet.add_argument("--size", type=parse_size, default=200, help="Output square size, or raw")
    split_sheet.add_argument("--format", choices=("gif", "png"), default="gif", help="Per-tile output format")
    split_sheet.add_argument("--mode", choices=(1, 2), type=int, default=2, help="GIF quantization mode")
    split_sheet.add_argument("--search-ratio", type=float, default=0.18, help="Search window ratio around expected split lines")
    split_sheet.add_argument("--trim-pad", type=int, default=8, help="Extra padding after trimming content")
    split_sheet.add_argument("--bg-tolerance", type=int, default=18, help="Background color tolerance")
    split_sheet.add_argument("--white-threshold", type=int, default=245, help="Treat near-white pixels as background")
    split_sheet.add_argument("--transparent-bg", action="store_true", help="Turn detected background transparent in each tile")
    return parser


def normalize_args(args: argparse.Namespace) -> None:
    if args.wechat_safe:
        if args.max_bytes is None:
            args.max_bytes = 512 * 1024
        if args.format == "auto":
            return


def make_duplicate_entry(
    source_root: Path,
    source_info: SourceInfo,
    output_root: Path,
    requested_format: str,
    requested_size: int,
    mode: int,
    wechat_safe: bool,
    original_source: str,
) -> ManifestEntry:
    resolved_format = resolve_output_format(requested_format, source_info, wechat_safe)
    target = relative_target(source_root, source_info.source, output_root, resolved_format)
    width, height = fit_size(source_info.width, source_info.height, requested_size)
    return build_manifest_entry(
        source_info=source_info,
        output=target,
        requested_format=requested_format,
        resolved_format=resolved_format,
        mode=mode,
        size_label="raw" if requested_size == 0 else str(requested_size),
        copied_directly=False,
        status="skipped_duplicate",
        note=f"same source_sha256 as {original_source}",
        width=width,
        height=height,
        frames=source_info.frames,
        bytes_out=0,
        output_sha256="",
    )


def run_convert(args: argparse.Namespace) -> int:
    normalize_args(args)
    input_path = Path(args.input).expanduser().resolve()
    output_root = Path(args.output).expanduser().resolve()
    files = gather_inputs(input_path)
    if not files:
        print("no supported image found", file=sys.stderr)
        return 2

    entries: list[ManifestEntry] = []
    failures: list[FailureEntry] = []
    seen_hashes: dict[str, str] = {}

    for source_file in files:
        try:
            source_info = inspect_source(source_file)
            if args.dedupe and source_info.source_sha256 in seen_hashes:
                entry = make_duplicate_entry(
                    source_root=input_path,
                    source_info=source_info,
                    output_root=output_root,
                    requested_format=args.format,
                    requested_size=args.size,
                    mode=args.mode,
                    wechat_safe=args.wechat_safe,
                    original_source=seen_hashes[source_info.source_sha256],
                )
                entries.append(entry)
                print(f"[skip] {source_file.name} duplicate")
                continue

            entry = convert_one(
                source_root=input_path,
                source_info=source_info,
                output_root=output_root,
                requested_size=args.size,
                requested_format=args.format,
                mode=args.mode,
                keep_gif=args.keep_gif,
                dry_run=args.dry_run,
                max_bytes=args.max_bytes,
                wechat_safe=args.wechat_safe,
                transparent_bg=getattr(args, "transparent_bg", False),
            )
            entries.append(entry)
            seen_hashes[source_info.source_sha256] = str(source_info.source)
            print(f"[ok] {source_file.name} -> {Path(entry.output).name}")
        except Exception as exc:
            failures.append(FailureEntry(source=str(source_file), error=str(exc)))
            print(f"[fail] {source_file}: {exc}", file=sys.stderr)

    manifest_path = write_manifest(output_root, entries, args.dry_run)
    csv_path = write_manifest_csv(output_root, entries) if args.manifest_csv else None
    failure_path = write_failures(output_root, failures)
    summary = summarize(entries, failures, args.dry_run)
    summary_path = write_summary(output_root, summary)
    ok_count = sum(1 for item in entries if item.status == "ok")
    skip_count = sum(1 for item in entries if item.status == "skipped_duplicate")
    print(f"done: ok={ok_count}, skipped={skip_count}, failed={len(failures)}, total={len(entries) + len(failures)}")
    print(f"manifest: {manifest_path}")
    print(f"summary: {summary_path}")
    if csv_path is not None:
        print(f"manifest_csv: {csv_path}")
    if failure_path is not None:
        print(f"failures: {failure_path}")
    return 0 if ok_count > 0 else 1


def run_sync_scan(args: argparse.Namespace) -> int:
    report_dir = Path(args.report_dir).expanduser().resolve()
    config = load_wechat_config(Path(args.config).expanduser().resolve() if args.config else None)
    keys = load_keys(Path(args.keys_file).expanduser().resolve() if args.keys_file else None)
    db_dir = Path(config["db_dir"]).expanduser().resolve()
    state_path = Path(args.state_file).expanduser().resolve() if args.state_file else report_dir / "sync_state.json"

    dec_db = decrypt_emoticon_db(report_dir, db_dir, keys)
    max_rowid = current_max_fav_rowid(dec_db)

    if args.baseline:
        save_sync_state(state_path, {"last_fav_rowid": max_rowid})
        report_path = write_sync_report(report_dir, {
            "mode": "baseline",
            "last_fav_rowid": max_rowid,
            "new_count": 0,
            "items": [],
        })
        print(f"baseline set: last_fav_rowid={max_rowid}")
        print(f"report: {report_path}")
        print(f"state: {state_path}")
        return 0

    state = load_sync_state(state_path)
    since_rowid = args.since_rowid if args.since_rowid is not None else int(state.get("last_fav_rowid", 0))
    items = query_sync_items(dec_db, since_rowid)

    if args.download_cdn and items:
        fetch_cdn_exports(report_dir, items)

    payload = {
        "mode": "scan",
        "db_dir": str(db_dir),
        "since_rowid": since_rowid,
        "last_fav_rowid": max_rowid,
        "new_count": len(items),
        "items": [asdict(item) for item in items],
    }
    report_path = write_sync_report(report_dir, payload)
    save_sync_state(state_path, {"last_fav_rowid": max_rowid})

    print(f"new_items: {len(items)}")
    print(f"last_fav_rowid: {max_rowid}")
    print(f"report: {report_path}")
    print(f"state: {state_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "convert":
        return run_convert(args)
    if args.command == "sync-scan":
        return run_sync_scan(args)
    if args.command == "split-sheet":
        return run_split_sheet(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
