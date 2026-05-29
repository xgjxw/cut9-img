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
from PIL import Image, ImageDraw, ImageFont, ImageSequence

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


def gather_ordered_images(input_value: str) -> list[Path]:
    input_value = input_value.strip()
    if not input_value:
        raise ValueError("input is empty")
    input_path = Path(input_value).expanduser()
    if input_path.exists():
        return gather_inputs(input_path.resolve())

    files: list[Path] = []
    for line in input_value.splitlines():
        path = Path(line.strip().strip('"')).expanduser()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
            files.append(path.resolve())
    if not files:
        raise FileNotFoundError(f"input not found: {input_value}")
    return files


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


def fit_into_square(image: Image.Image, cell_size: int, bg_rgb: tuple[int, int, int]) -> Image.Image:
    frame = image.convert("RGBA")
    frame.thumbnail((cell_size, cell_size), Image.Resampling.LANCZOS)
    cell = Image.new("RGBA", (cell_size, cell_size), (*bg_rgb, 255))
    x = (cell_size - frame.width) // 2
    y = (cell_size - frame.height) // 2
    cell.alpha_composite(frame, (x, y))
    return cell


def trim_near_white_border(image: Image.Image, *, threshold: int = 248, padding: int = 24) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    left, top, right, bottom = rgba.width, rgba.height, -1, -1
    for y in range(rgba.height):
        for x in range(rgba.width):
            r, g, b, a = pixels[x, y]
            if a > 8 and not (r >= threshold and g >= threshold and b >= threshold):
                left = min(left, x)
                top = min(top, y)
                right = max(right, x)
                bottom = max(bottom, y)
    if right < left or bottom < top:
        return rgba
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(rgba.width - 1, right + padding)
    bottom = min(rgba.height - 1, bottom + padding)
    return rgba.crop((left, top, right + 1, bottom + 1))


def fit_panel_to_width(image: Image.Image, width: int, bg_rgb: tuple[int, int, int], *, trim: bool) -> Image.Image:
    panel = trim_near_white_border(image) if trim else image.convert("RGBA")
    if panel.width != width:
        height = max(1, round(panel.height * width / panel.width))
        panel = panel.resize((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", panel.size, (*bg_rgb, 255))
    canvas.alpha_composite(panel, (0, 0))
    return canvas


def parse_hex_color(value: str) -> tuple[int, int, int]:
    raw = value.strip().lstrip("#")
    if len(raw) != 6:
        raise ValueError("bg color must be like #ffffff")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def find_default_caption_font() -> Path | None:
    candidates = [
        Path(r"C:\Windows\Fonts\msyhbd.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def resolve_caption_font(value: str | None) -> Path | None:
    if not value or value.strip().lower() == "auto":
        return find_default_caption_font()
    path = Path(value).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"caption font not found: {path}")
    return path.resolve()


def load_xhs_plan(path: str | None) -> dict[str, object]:
    if not path:
        return {}
    plan_path = Path(path).expanduser().resolve()
    if not plan_path.is_file():
        raise FileNotFoundError(f"xhs plan not found: {plan_path}")
    return json.loads(plan_path.read_text(encoding="utf-8"))


def _caption_text_from_item(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("caption", "text", "subtitle", "title"):
            value = item.get(key)
            if isinstance(value, str):
                return value.strip()
    return ""


def load_caption_lines(path: str | None, plan: dict[str, object] | None = None) -> list[str]:
    if plan:
        captions = plan.get("captions")
        if isinstance(captions, list):
            return [_caption_text_from_item(item) for item in captions]
        slices = plan.get("slices")
        if isinstance(slices, list):
            return [_caption_text_from_item(item) for item in slices]
    if not path:
        return []

    caption_path = Path(path).expanduser().resolve()
    if not caption_path.is_file():
        raise FileNotFoundError(f"caption file not found: {caption_path}")
    if caption_path.suffix.lower() == ".json":
        payload = json.loads(caption_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [_caption_text_from_item(item) for item in payload]
        if isinstance(payload, dict):
            return load_caption_lines(None, payload)
        raise ValueError("caption json must be a list or object")
    return [line.strip() for line in caption_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def wrap_caption_text(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            width = draw.textbbox((0, 0), candidate, font=font)[2]
            if width <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = char
        if current:
            lines.append(current)
    return lines or [""]


def scale_box(box: list[float] | tuple[float, float, float, float], width: int, height: int, *, base_width: int, base_height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    return (
        int(round(float(x1) * width / base_width)),
        int(round(float(y1) * height / base_height)),
        int(round(float(x2) * width / base_width)),
        int(round(float(y2) * height / base_height)),
    )


def scale_point(point: list[float] | tuple[float, float], width: int, height: int, *, base_width: int, base_height: int) -> tuple[int, int]:
    x, y = point
    return (
        int(round(float(x) * width / base_width)),
        int(round(float(y) * height / base_height)),
    )


def fit_caption_font(
    text: str,
    *,
    font_path: Path | None,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int,
) -> tuple[ImageFont.ImageFont, list[str], int]:
    if start_size <= 0:
        raise ValueError("caption font size must be > 0")
    if min_size <= 0 or min_size > start_size:
        raise ValueError("caption min font size must be > 0 and <= caption font size")

    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for size in range(start_size, min_size - 1, -2):
        font = ImageFont.truetype(str(font_path), size) if font_path else ImageFont.load_default(size=size)
        lines = wrap_caption_text(text, font, max_width)
        line_height = max(1, round(size * 1.22))
        widest = max(draw.textbbox((0, 0), line, font=font)[2] for line in lines)
        if widest <= max_width and line_height * len(lines) <= max_height:
            return font, lines, line_height

    font = ImageFont.truetype(str(font_path), min_size) if font_path else ImageFont.load_default(size=min_size)
    return font, wrap_caption_text(text, font, max_width), max(1, round(min_size * 1.22))


def fit_text_in_box(
    text: str,
    *,
    font_path: Path | None,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int,
) -> tuple[ImageFont.ImageFont, list[str], int]:
    return fit_caption_font(
        text,
        font_path=font_path,
        max_width=max_width,
        max_height=max_height,
        start_size=start_size,
        min_size=min_size,
    )


def draw_centered_text_box(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    *,
    font_path: Path | None,
    font_size: int,
    min_font_size: int,
    fill: tuple[int, int, int],
    padding: int = 18,
    stroke_fill: tuple[int, int, int] | None = None,
    stroke_width: int = 0,
) -> None:
    x1, y1, x2, y2 = box
    max_width = max(1, x2 - x1 - padding * 2)
    max_height = max(1, y2 - y1 - padding * 2)
    font, lines, line_height = fit_text_in_box(
        text,
        font_path=font_path,
        max_width=max_width,
        max_height=max_height,
        start_size=font_size,
        min_size=min_font_size,
    )
    total_height = line_height * len(lines)
    y = y1 + max(0, (y2 - y1 - total_height) // 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = x1 + (x2 - x1 - text_width) // 2
        kwargs = {}
        if stroke_fill is not None and stroke_width > 0:
            kwargs["stroke_width"] = stroke_width
            kwargs["stroke_fill"] = (*stroke_fill, 255)
        draw.text((x, y), line, font=font, fill=(*fill, 255), **kwargs)
        y += line_height


def draw_stroke_text_box(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    *,
    font_path: Path | None,
    font_size: int,
    min_font_size: int,
    fill: tuple[int, int, int],
    stroke_fill: tuple[int, int, int],
    stroke_width: int,
    padding: int = 0,
) -> None:
    draw_centered_text_box(
        draw,
        box,
        text,
        font_path=font_path,
        font_size=font_size,
        min_font_size=min_font_size,
        fill=fill,
        padding=padding,
        stroke_fill=stroke_fill,
        stroke_width=stroke_width,
    )


def build_contact_sheet(images: list[Path], output_path: Path, *, thumb_width: int = 260, thumb_height: int = 550, gutter: int = 20) -> None:
    if not images:
        return
    thumbs: list[Image.Image] = []
    for image_path in images:
        with Image.open(image_path) as image:
            thumb = image.convert("RGB")
            thumb.thumbnail((thumb_width, thumb_height), Image.Resampling.LANCZOS)
            thumbs.append(thumb)

    columns = len(thumbs)
    canvas = Image.new("RGB", (columns * (thumb_width + gutter), thumb_height + 48), "white")
    draw = ImageDraw.Draw(canvas)
    label_font = ImageFont.truetype(str(find_default_caption_font()), 18) if find_default_caption_font() else ImageFont.load_default()
    for idx, thumb in enumerate(thumbs, start=1):
        x = (idx - 1) * (thumb_width + gutter) + 10
        canvas.paste(thumb, (x, 35))
        draw.text((x, 8), f"page {idx}", font=label_font, fill=(0, 0, 0))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)


def draw_bubble_shape(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    fill: tuple[int, int, int],
    outline: tuple[int, int, int],
    outline_width: int,
    tail: list[tuple[int, int]] | None = None,
) -> None:
    draw.ellipse(box, fill=(*fill, 255), outline=(*outline, 255), width=outline_width)
    if tail:
        draw.polygon(tail, fill=(*fill, 255), outline=(*outline, 255))


def draw_rounded_box(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    radius: int,
    fill: tuple[int, int, int],
    outline: tuple[int, int, int],
    outline_width: int,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=(*fill, 255), outline=(*outline, 255), width=outline_width)


def append_caption_area(
    panel: Image.Image,
    caption: str,
    *,
    caption_height: int,
    font_path: Path | None,
    font_size: int,
    min_font_size: int,
    margin_x: int,
    color_rgb: tuple[int, int, int],
    bg_rgb: tuple[int, int, int],
) -> Image.Image:
    if caption_height <= 0 or not caption:
        return panel.convert("RGBA")
    if margin_x < 0:
        raise ValueError("caption margin must be >= 0")

    base = panel.convert("RGBA")
    canvas = Image.new("RGBA", (base.width, base.height + caption_height), (*bg_rgb, 255))
    canvas.alpha_composite(base, (0, 0))

    max_width = max(1, base.width - margin_x * 2)
    max_height = max(1, caption_height - 24)
    font, lines, line_height = fit_caption_font(
        caption,
        font_path=font_path,
        max_width=max_width,
        max_height=max_height,
        start_size=font_size,
        min_size=min_font_size,
    )

    draw = ImageDraw.Draw(canvas)
    total_height = line_height * len(lines)
    y = base.height + max(0, (caption_height - total_height) // 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = (base.width - text_width) // 2
        draw.text((x, y), line, font=font, fill=(*color_rgb, 255))
        y += line_height
    return canvas


def compose_xhs_vertical_pair(
    top_path: Path,
    bottom_path: Path | None,
    target: Path,
    *,
    cell_size: int,
    gutter: int,
    bg_rgb: tuple[int, int, int],
    output_format: str,
) -> tuple[int, int]:
    if cell_size <= 0:
        raise ValueError("cell-size must be > 0")
    if gutter < 0:
        raise ValueError("gutter must be >= 0")

    width = cell_size
    height = cell_size * 2 + gutter
    canvas = Image.new("RGBA", (width, height), (*bg_rgb, 255))

    with Image.open(top_path) as top_image:
        canvas.alpha_composite(fit_into_square(top_image, cell_size, bg_rgb), (0, 0))
    if bottom_path is not None:
        with Image.open(bottom_path) as bottom_image:
            canvas.alpha_composite(fit_into_square(bottom_image, cell_size, bg_rgb), (0, cell_size + gutter))

    target.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "jpg":
        canvas.convert("RGB").save(target, format="JPEG", quality=95, optimize=True)
    else:
        canvas.save(target, format="PNG", optimize=True)
    return width, height


def compose_xhs_vertical_flow(
    top_path: Path,
    bottom_path: Path | None,
    target: Path,
    *,
    width: int,
    gutter: int,
    bg_rgb: tuple[int, int, int],
    output_format: str,
    trim: bool,
    top_caption: str = "",
    bottom_caption: str = "",
    caption_height: int = 0,
    caption_font: Path | None = None,
    caption_font_size: int = 64,
    caption_min_font_size: int = 28,
    caption_margin_x: int = 80,
    caption_color_rgb: tuple[int, int, int] = (0, 0, 0),
) -> tuple[int, int]:
    if width <= 0:
        raise ValueError("width must be > 0")
    if gutter < 0:
        raise ValueError("gutter must be >= 0")

    panels: list[Image.Image] = []
    with Image.open(top_path) as top_image:
        top_panel = fit_panel_to_width(top_image, width, bg_rgb, trim=trim)
        panels.append(
            append_caption_area(
                top_panel,
                top_caption,
                caption_height=caption_height,
                font_path=caption_font,
                font_size=caption_font_size,
                min_font_size=caption_min_font_size,
                margin_x=caption_margin_x,
                color_rgb=caption_color_rgb,
                bg_rgb=bg_rgb,
            )
        )
    if bottom_path is not None:
        with Image.open(bottom_path) as bottom_image:
            bottom_panel = fit_panel_to_width(bottom_image, width, bg_rgb, trim=trim)
            panels.append(
                append_caption_area(
                    bottom_panel,
                    bottom_caption,
                    caption_height=caption_height,
                    font_path=caption_font,
                    font_size=caption_font_size,
                    min_font_size=caption_min_font_size,
                    margin_x=caption_margin_x,
                    color_rgb=caption_color_rgb,
                    bg_rgb=bg_rgb,
                )
            )

    height = sum(panel.height for panel in panels) + gutter * (len(panels) - 1)
    canvas = Image.new("RGBA", (width, height), (*bg_rgb, 255))
    y = 0
    for panel in panels:
        canvas.alpha_composite(panel, (0, y))
        y += panel.height + gutter

    target.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "jpg":
        canvas.convert("RGB").save(target, format="JPEG", quality=95, optimize=True)
    else:
        canvas.save(target, format="PNG", optimize=True)
    return width, height


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
        "tool": "meme-workshop",
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
        "tool": "meme-workshop",
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


def run_stitch_vertical(args: argparse.Namespace) -> int:
    plan = load_xhs_plan(getattr(args, "xhs_plan", None))
    input_files = gather_ordered_images(args.input)
    if not input_files:
        print("no supported image found", file=sys.stderr)
        return 2

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    bg_rgb = parse_hex_color(str(plan.get("bg", args.bg)))
    caption_color_rgb = parse_hex_color(str(plan.get("caption_color", getattr(args, "caption_color", "#000000"))))
    captions = load_caption_lines(getattr(args, "captions", None), plan)
    caption_height = int(plan.get("caption_height", getattr(args, "caption_height", 0)))
    caption_font_size = int(plan.get("caption_font_size", getattr(args, "caption_font_size", 64)))
    caption_min_font_size = int(plan.get("caption_min_font_size", getattr(args, "caption_min_font_size", 28)))
    caption_margin_x = int(plan.get("caption_margin_x", getattr(args, "caption_margin_x", 80)))
    caption_font = resolve_caption_font(str(plan.get("caption_font", getattr(args, "caption_font", "auto"))))
    ext = ".jpg" if args.format == "jpg" else ".png"
    pairs = [input_files[index : index + 2] for index in range(0, len(input_files), 2)]
    report: list[dict[str, object]] = []

    for idx, pair in enumerate(pairs, start=1):
        top_index = (idx - 1) * 2
        bottom_index = top_index + 1
        top = pair[0]
        bottom = pair[1] if len(pair) > 1 else None
        target = output_dir / f"group_{idx:03d}{ext}"
        width, height = compose_xhs_vertical_flow(
            top,
            bottom,
            target,
            width=args.cell_size,
            gutter=args.gutter,
            bg_rgb=bg_rgb,
            output_format=args.format,
            trim=args.trim,
            top_caption=captions[top_index] if top_index < len(captions) else "",
            bottom_caption=captions[bottom_index] if bottom_index < len(captions) else "",
            caption_height=caption_height,
            caption_font=caption_font,
            caption_font_size=caption_font_size,
            caption_min_font_size=caption_min_font_size,
            caption_margin_x=caption_margin_x,
            caption_color_rgb=caption_color_rgb,
        )
        report.append(
            {
                "output": str(target),
                "top": str(top),
                "bottom": str(bottom) if bottom is not None else "",
                "top_caption": captions[top_index] if top_index < len(captions) else "",
                "bottom_caption": captions[bottom_index] if bottom_index < len(captions) else "",
                "width": width,
                "height": height,
            }
        )
        bottom_name = bottom.name if bottom is not None else "<blank>"
        print(f"[ok] {top.name} + {bottom_name} -> {target.name}")

    report_path = output_dir / "stitch_report.json"
    report_path.write_text(json.dumps({"count": len(report), "items": report}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: groups={len(report)}, source_images={len(input_files)}")
    print(f"report: {report_path}")
    return 0


def run_overlay_bubbles(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).expanduser().resolve()
    if not plan_path.is_file():
        raise FileNotFoundError(f"plan not found: {plan_path}")
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("plan json must be an object")

    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("plan must include non-empty pages array")

    defaults = payload.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}

    base_width = int(payload.get("base_width", defaults.get("base_width", 864)))
    base_height = int(payload.get("base_height", defaults.get("base_height", 1821)))
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    default_font = resolve_caption_font(str(defaults.get("font", "auto")))
    default_fill = parse_hex_color(str(defaults.get("fill", "#faf9f6")))
    default_outline = parse_hex_color(str(defaults.get("outline", "#121212")))
    default_text = parse_hex_color(str(defaults.get("text_color", "#161616")))
    default_text_stroke = defaults.get("text_stroke_color")
    default_text_stroke_rgb = parse_hex_color(str(default_text_stroke)) if default_text_stroke else None
    default_text_stroke_width = int(defaults.get("text_stroke_width", 0))
    default_outline_width = int(defaults.get("outline_width", 3))
    default_min_font_size = int(defaults.get("min_font_size", 18))
    default_font_size = int(defaults.get("font_size", 30))
    report: list[dict[str, object]] = []
    generated_outputs: list[Path] = []

    for index, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            raise ValueError("each page must be an object")
        image_value = page.get("image")
        if not isinstance(image_value, str) or not image_value.strip():
            raise ValueError("page.image is required")
        image_path = Path(image_value).expanduser()
        if not image_path.is_absolute():
            image_path = (plan_path.parent / image_path).resolve()
        if not image_path.is_file():
            raise FileNotFoundError(f"image not found: {image_path}")

        output_name = page.get("output")
        if not isinstance(output_name, str) or not output_name.strip():
            output_name = f"page_{index:02d}.png"
        target = output_dir / output_name
        items = page.get("items", [])
        if not isinstance(items, list):
            raise ValueError("page.items must be a list")

        with Image.open(image_path) as source:
            canvas = source.convert("RGBA")
        draw = ImageDraw.Draw(canvas)
        width, height = canvas.size

        for item in items:
            if not isinstance(item, dict):
                raise ValueError("overlay item must be an object")
            kind = str(item.get("type", "bubble"))
            text = str(item.get("text", ""))
            box_value = item.get("box")
            if not isinstance(box_value, list) or len(box_value) != 4:
                raise ValueError("overlay item box must be [x1,y1,x2,y2]")
            box = scale_box(box_value, width, height, base_width=base_width, base_height=base_height)
            font_path = resolve_caption_font(str(item.get("font", defaults.get("font", "auto"))))
            font_size = int(item.get("font_size", default_font_size))
            min_font_size = int(item.get("min_font_size", default_min_font_size))
            fill_rgb = parse_hex_color(str(item.get("fill", defaults.get("fill", "#faf9f6"))))
            outline_rgb = parse_hex_color(str(item.get("outline", defaults.get("outline", "#121212"))))
            text_rgb = parse_hex_color(str(item.get("text_color", defaults.get("text_color", "#161616"))))
            text_stroke_color = item.get("text_stroke_color", default_text_stroke)
            text_stroke_rgb = parse_hex_color(str(text_stroke_color)) if text_stroke_color else default_text_stroke_rgb
            text_stroke_width = int(item.get("text_stroke_width", default_text_stroke_width))
            outline_width = int(item.get("outline_width", default_outline_width))
            padding = int(item.get("padding", 18))

            if kind == "bubble":
                tail_points = None
                raw_tail = item.get("tail")
                if isinstance(raw_tail, list) and raw_tail:
                    tail_points = [
                        scale_point(point, width, height, base_width=base_width, base_height=base_height)
                        for point in raw_tail
                    ]
                draw_bubble_shape(
                    draw,
                    box,
                    fill=fill_rgb,
                    outline=outline_rgb,
                    outline_width=outline_width,
                    tail=tail_points,
                )
                draw_centered_text_box(
                    draw,
                    box,
                    text,
                    font_path=font_path or default_font,
                    font_size=font_size,
                    min_font_size=min_font_size,
                    fill=text_rgb,
                    padding=padding,
                    stroke_fill=text_stroke_rgb,
                    stroke_width=text_stroke_width,
                )
            elif kind == "caption_box":
                radius = int(item.get("radius", 10))
                draw_rounded_box(
                    draw,
                    box,
                    radius=radius,
                    fill=fill_rgb,
                    outline=outline_rgb,
                    outline_width=outline_width,
                )
                draw_centered_text_box(
                    draw,
                    box,
                    text,
                    font_path=font_path or default_font,
                    font_size=font_size,
                    min_font_size=min_font_size,
                    fill=text_rgb,
                    padding=padding,
                    stroke_fill=text_stroke_rgb,
                    stroke_width=text_stroke_width,
                )
            elif kind == "text":
                draw_centered_text_box(
                    draw,
                    box,
                    text,
                    font_path=font_path or default_font,
                    font_size=font_size,
                    min_font_size=min_font_size,
                    fill=text_rgb,
                    padding=padding,
                    stroke_fill=text_stroke_rgb,
                    stroke_width=text_stroke_width,
                )
            elif kind == "paper_text":
                paper_text_rgb = parse_hex_color(str(item.get("text_color", "#641414")))
                draw_centered_text_box(
                    draw,
                    box,
                    text,
                    font_path=font_path or default_font,
                    font_size=font_size,
                    min_font_size=min_font_size,
                    fill=paper_text_rgb,
                    padding=padding,
                    stroke_fill=text_stroke_rgb,
                    stroke_width=text_stroke_width,
                )
            elif kind == "stroke_text":
                resolved_stroke_rgb = text_stroke_rgb or (0, 0, 0)
                resolved_stroke_width = text_stroke_width if text_stroke_width > 0 else 3
                draw_stroke_text_box(
                    draw,
                    box,
                    text,
                    font_path=font_path or default_font,
                    font_size=font_size,
                    min_font_size=min_font_size,
                    fill=text_rgb,
                    stroke_fill=resolved_stroke_rgb,
                    stroke_width=resolved_stroke_width,
                    padding=padding,
                )
            else:
                raise ValueError(f"unsupported overlay item type: {kind}")

        target.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(target, format="PNG", optimize=True)
        generated_outputs.append(target)
        report.append({"input": str(image_path), "output": str(target), "items": len(items), "width": width, "height": height})
        print(f"[ok] {image_path.name} -> {target.name}")

    report_path = output_dir / "overlay_report.json"
    report_path.write_text(json.dumps({"count": len(report), "items": report}, ensure_ascii=False, indent=2), encoding="utf-8")
    build_contact_sheet(generated_outputs, output_dir / "review_contact_sheet.png")
    print(f"done: pages={len(report)}")
    print(f"report: {report_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meme-workshop", description="Local meme and Xiaohongshu collage workshop")
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

    stitch = sub.add_parser("stitch-vertical", help="Stitch ordered images into Xiaohongshu-style vertical pairs")
    stitch.add_argument("input", help="Input image directory, image file, or newline-separated image file list")
    stitch.add_argument("output", help="Output directory")
    stitch.add_argument("--template", choices=("xhs",), default="xhs", help="Built-in template")
    stitch.add_argument("--cell-size", type=int, default=1080, help="Output width for each stitched group")
    stitch.add_argument("--gutter", type=int, default=8, help="White gutter between top and bottom images")
    stitch.add_argument("--bg", default="#ffffff", help="Canvas background color")
    stitch.add_argument("--format", choices=("png", "jpg"), default="png", help="Output format")
    stitch.add_argument("--no-trim", dest="trim", action="store_false", help="Keep original white border instead of trimming")
    stitch.add_argument("--captions", default=None, help="UTF-8 txt/json captions, one caption per source image")
    stitch.add_argument("--xhs-plan", default=None, help="JSON plan generated by the xhs-life-philosophy-illustration skill")
    stitch.add_argument("--caption-height", type=int, default=0, help="Caption area height appended under each panel; 0 disables captions")
    stitch.add_argument("--caption-font", default="auto", help="Caption font path, or auto for a Chinese system font")
    stitch.add_argument("--caption-font-size", type=int, default=64, help="Preferred caption font size")
    stitch.add_argument("--caption-min-font-size", type=int, default=28, help="Minimum caption font size for auto-fit")
    stitch.add_argument("--caption-margin-x", type=int, default=80, help="Horizontal caption margin in pixels")
    stitch.add_argument("--caption-color", default="#000000", help="Caption text color")
    stitch.set_defaults(trim=True)

    overlay = sub.add_parser("overlay-bubbles", help="Overlay in-panel comic bubbles/text from a JSON plan")
    overlay.add_argument("plan", help="Plan JSON file describing pages and bubble items")
    overlay.add_argument("output", help="Output directory")
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
    if args.command == "stitch-vertical":
        return run_stitch_vertical(args)
    if args.command == "overlay-bubbles":
        return run_overlay_bubbles(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
