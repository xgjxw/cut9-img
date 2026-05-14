# Prompt templates

## 3x3 grid template

```text
Create a clean 3x3 grid illustration for a Xiaohongshu-style Chinese life philosophy comic.

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
{subject_style}
minimal cute cartoon sticker style,
soft black outlines,
flat pastel colors,
warm clean lighting,
small props,
minimal background elements,
high whitespace,
clear readable composition.

Theme: {theme}

Nine no-text scenes, ordered left-to-right and top-to-bottom:
1. {scene_1}
2. {scene_2}
3. {scene_3}
4. {scene_4}
5. {scene_5}
6. {scene_6}
7. {scene_7}
8. {scene_8}
9. {scene_9}

Important composition rules:
each cell should look like an individual sticker placed in a square white tile;
leave at least 15% empty margin around every sticker;
all characters must be complete from head to toe;
no element should be cut off;
keep the grid perfectly aligned and evenly spaced.
```

## Negative prompt

```text
text, Chinese text, captions, cropped character, cut off body, overlapping cells, merged panels, uneven grid, diagonal grid, random borders, thick frame, UI screenshot, app interface, watermark, logo, page number, messy background, too much detail, tiny character, extra limbs, duplicated faces, low resolution
```
