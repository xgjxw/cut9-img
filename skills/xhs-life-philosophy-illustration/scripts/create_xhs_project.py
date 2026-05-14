from __future__ import annotations

import argparse
import json
from pathlib import Path


STYLE_DESCRIPTIONS = {
    "workplace-monk": "cute minimalist workplace monk, round white face, orange robe, simple black line limbs, calm but expressive face, soft black outline, pastel colors",
    "office-cat": "cute white office cat wearing a tiny orange hoodie, round face, expressive eyes, simple sticker cartoon style, soft black outline",
    "round-office-worker": "simple cute office worker, round white face, orange sweater, black line limbs, minimal sticker cartoon style, soft thick outline",
    "zen-rabbit": "small white rabbit with orange vest, calm face, expressive long ears, soft pastel sticker style, clean black outline",
    "tiny-robot": "small friendly robot, white body with orange accents, simple screen face, office and life props, minimal futuristic sticker style",
}


DEFAULT_CAPTIONS = [
    "消息越多，成果越少",
    "忙到最后，只剩汇报",
    "会开完了，事还在原地",
    "优化细节，逃避重点",
    "越急的事，越要慢想",
    "真正成长，不靠加班",
    "稳定情绪，也是工作量",
    "边界清楚，关系更久",
    "下班不是逃跑，是续航",
]


DEFAULT_SCENES = [
    "the character is surrounded by many chat bubbles and phones, looking overwhelmed",
    "the character carries a huge stack of KPI reports and papers",
    "the character sits in a long meeting while the task remains untouched",
    "the character polishes a tiny chart while ignoring a huge dark problem cloud",
    "the character meditates calmly among urgent red task labels",
    "the character waters a small growth plant while clocks stay in the background",
    "the character puts down a heavy emotion mask and breathes calmly",
    "the character stands at a clear boundary line with a coworker, both peaceful",
    "the character drinks tea after work beside a small checklist and sunset window",
]


DEFAULT_HOOK = "瞬间被这段话点醒了~"


FIXED_TAGS = "#拒绝内耗[话题]#  #拒绝焦虑[话题]#  #治愈[话题]#  #人间清醒[话题]#  #心灵鸡汤[话题]#  #有道理的话[话题]#  #此话可否有道理[话题]#  #很治愈我的话[话题]#  #脑洞[话题]#  #成长[话题]#"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a Xiaohongshu philosophy illustration project package.")
    parser.add_argument("--theme", required=True, help="Chinese topic/theme")
    parser.add_argument("--style", choices=sorted(STYLE_DESCRIPTIONS), required=True, help="Subject style id")
    parser.add_argument("--output", required=True, help="Output project directory")
    parser.add_argument("--captions", default=None, help="Optional UTF-8 txt file, one caption per line")
    parser.add_argument("--hook", default=DEFAULT_HOOK, help="Short Xiaohongshu title hook")
    parser.add_argument("--caption-height", type=int, default=180)
    parser.add_argument("--caption-font-size", type=int, default=64)
    parser.add_argument("--caption-min-font-size", type=int, default=28)
    parser.add_argument("--caption-margin-x", type=int, default=80)
    return parser.parse_args()


def load_captions(path: str | None) -> list[str]:
    if not path:
        return DEFAULT_CAPTIONS
    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 9:
        raise ValueError("caption file must contain exactly 9 non-empty lines")
    return lines


def build_grid_prompt(theme: str, style: str, captions: list[str]) -> str:
    style_text = STYLE_DESCRIPTIONS[style]
    scene_lines = []
    for index, (caption, scene) in enumerate(zip(captions, DEFAULT_SCENES), start=1):
        scene_lines.append(f"{index}. Scene meaning: {caption}. Visual action: {scene}.")
    return f"""Create a clean 3x3 grid illustration for a Xiaohongshu-style Chinese life philosophy comic.

Canvas: square 1:1 composition.
Layout: exactly nine equal square cells arranged in a 3 by 3 grid.
Background: pure white.
Gutters: equal white gutters between all cells, clean and consistent.
Each cell must be visually independent.
Each cell contains one centered sticker-style cartoon character scene.
Every sticker must stay fully inside its own square cell.
No sticker may touch or cross the cell boundary.
No overlapping between cells.
No cropping.
No outer frame.
No UI elements.
No watermark.
No numbers.
No page indicators.
No text.
No captions.

Style:
{style_text};
minimal cute cartoon sticker style;
soft black outlines;
flat pastel colors;
warm clean lighting;
small props;
minimal background elements;
high whitespace;
clear readable composition.

Theme: {theme}

Nine no-text scenes, ordered left-to-right and top-to-bottom:
{chr(10).join(scene_lines)}

Important composition rules:
each cell should look like an individual sticker placed in a square white tile;
leave at least 15% empty margin around every sticker;
all characters must be complete from head to toe;
no element should be cut off;
keep the grid perfectly aligned and evenly spaced.

Negative prompt:
text, Chinese text, captions, cropped character, cut off body, overlapping cells, merged panels, uneven grid, diagonal grid, random borders, thick frame, UI screenshot, app interface, watermark, logo, page number, messy background, too much detail, tiny character, extra limbs, duplicated faces, low resolution
"""


def build_individual_prompts(theme: str, style: str, captions: list[str]) -> str:
    style_text = STYLE_DESCRIPTIONS[style]
    blocks = []
    for index, (caption, scene) in enumerate(zip(captions, DEFAULT_SCENES), start=1):
        blocks.append(
            f"""[{index:02d}] {caption}
square 1:1 image, pure white background, one centered complete sticker-style character scene, no text, no caption, no border, no watermark.
Theme: {theme}.
Subject style: {style_text}.
Visual action: {scene}.
Leave large empty margin; show the full character; soft black outline; pastel colors."""
        )
    return "\n\n".join(blocks) + "\n"


def main() -> int:
    args = parse_args()
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    captions = load_captions(args.captions)

    plan = {
        "schema": "xhs-life-philosophy-plan/v1",
        "theme": args.theme,
        "style": args.style,
        "hook": args.hook,
        "tags": FIXED_TAGS,
        "layout": "grid-3x3-no-text-source-then-captioned-vertical-groups",
        "caption_height": args.caption_height,
        "caption_font": "auto",
        "caption_font_size": args.caption_font_size,
        "caption_min_font_size": args.caption_min_font_size,
        "caption_margin_x": args.caption_margin_x,
        "caption_color": "#000000",
        "bg": "#ffffff",
        "captions": [{"index": index, "caption": caption} for index, caption in enumerate(captions, start=1)],
        "meme_cli": {
            "split": "python -m meme_cli.cli split-sheet source_grid.png tiles --rows 3 --cols 3 --size raw --format png",
            "stitch": "python -m meme_cli.cli stitch-vertical tiles groups --xhs-plan xhs_plan.json --cell-size 1080 --gutter 16",
        },
    }

    (output / "hook.txt").write_text(args.hook.strip() + "\n", encoding="utf-8")
    (output / "tags.txt").write_text(FIXED_TAGS + "\n", encoding="utf-8")
    (output / "post_copy.txt").write_text(f"{args.hook.strip()}\n\n{FIXED_TAGS}\n", encoding="utf-8")
    (output / "captions.txt").write_text("\n".join(captions) + "\n", encoding="utf-8")
    (output / "xhs_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "image_prompt_grid.txt").write_text(build_grid_prompt(args.theme, args.style, captions), encoding="utf-8")
    (output / "image_prompt_individual.txt").write_text(build_individual_prompts(args.theme, args.style, captions), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
