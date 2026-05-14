---
name: xhs-life-philosophy-illustration
description: Generate Xiaohongshu-style life/work philosophy illustration packages from a theme. Use when Codex needs to create Chinese caption slices, choose or confirm a dynamic recurring subject style, write no-text image prompts for a 3x3 grid or nine individual square stickers, and output files compatible with meme-workshop/xhs-plan caption stitching.
---

# Xiaohongshu Life Philosophy Illustration

## Workflow

1. Receive a theme, e.g. "职场低内耗", "人到中年的清醒", "长期主义", "松弛感自救".
2. If the user has not selected a subject style, present the built-in styles from `references/subject_styles.md` and ask for confirmation before image generation.
3. Create one short title hook before the nine captions. The hook should be curiosity-driven, healing-oriented, and non-anxious, normally 8-18 Chinese characters, e.g. `瞬间被这段话点醒了~`.
4. Create nine short Chinese caption slices. Each slice should express one concrete healing/self-growth insight, normally 8-16 Chinese characters.
5. Create an image plan:
   - Prefer one no-text 3x3 grid image when the user wants a single generated asset.
   - Use nine independent square sticker prompts when grid cutting quality matters more than generation convenience.
   - Never ask the image model to render Chinese captions. Captions are added later by `meme-workshop`.
6. Save a project package containing:
   - `hook.txt`: one short Xiaohongshu title hook.
   - `tags.txt`: fixed Xiaohongshu topic tags.
   - `post_copy.txt`: hook + fixed tags, ready to paste.
   - `captions.txt`: one caption per line, ordered left-to-right, top-to-bottom.
   - `xhs_plan.json`: caption and rendering metadata accepted by `meme-workshop stitch-vertical --xhs-plan`.
   - `image_prompt_grid.txt`: no-text prompt for a 3x3 source image.
   - `image_prompt_individual.txt`: nine no-text per-cell prompts.
7. After image generation and grid cutting, call `meme-workshop` like:

```powershell
python -m meme_cli.cli split-sheet source_grid.png tiles --rows 3 --cols 3 --size raw --format png
python -m meme_cli.cli stitch-vertical tiles groups --xhs-plan path\to\xhs_plan.json --cell-size 1080 --gutter 16
```

## Built-in Subject Styles

Load `references/subject_styles.md` when presenting style choices or writing prompts. Use these IDs in `xhs_plan.json`:

- `workplace-monk`
- `office-cat`
- `round-office-worker`
- `zen-rabbit`
- `tiny-robot`

## Copywriting Rules

- Only produce healing, anti-anxiety, anti-overthinking, human clarity, and self-growth themes.
- Reject anxiety-inducing, fear-mongering, hustle-pressure, comparison-pressure, and scarcity-pressure angles.
- The title hook must be short, emotionally safe, and curiosity-driven; it should quickly make users want to read the nine panels.
- Good hook examples:
  - `瞬间被这段话点醒了~`
  - `这九句话治好了我`
  - `慢慢来，反而更快`
  - `不焦虑后，路更清楚`
  - `原来成长可以不痛苦`
- Use one clear truth per cell.
- Make captions parallel when possible.
- Avoid abstract slogans with no visual action.
- Prefer verbs and observable workplace/life situations.
- Keep each caption short enough for one line at 1080px; allow two lines only when necessary.

## Fixed Xiaohongshu Tags

Always output these exact tags in `tags.txt`, `post_copy.txt`, and `xhs_plan.json`:

```text
#拒绝内耗[话题]#  #拒绝焦虑[话题]#  #治愈[话题]#  #人间清醒[话题]#  #心灵鸡汤[话题]#  #有道理的话[话题]#  #此话可否有道理[话题]#  #很治愈我的话[话题]#  #脑洞[话题]#  #成长[话题]#
```

Good caption pattern:

```text
消息越多，成果越少
忙到最后，只剩汇报
越急的事，越要慢想
```

## Prompt Rules

For grid prompts, include these hard layout constraints:

```text
exactly nine equal square cells arranged in a 3 by 3 grid,
pure white background,
equal white gutters,
each sticker centered inside its own square cell,
all characters fully visible,
large empty margin,
no text,
no caption,
no UI,
no border,
no watermark,
no cropping,
no overlap
```

For individual prompts, create one square no-text sticker scene per caption:

```text
square 1:1 image, pure white background, one centered complete sticker-style character scene, no text, no caption, no border, no watermark
```

## Deterministic Package Script

Use `scripts/create_xhs_project.py` to scaffold the files after deciding the theme/style/captions:

```powershell
python C:\Users\Administrator\.codex\skills\xhs-life-philosophy-illustration\scripts\create_xhs_project.py `
  --theme "职场低内耗" `
  --style workplace-monk `
  --output .\xhs_project
```

Then edit the generated captions/prompts if the user's theme requires sharper copy.
