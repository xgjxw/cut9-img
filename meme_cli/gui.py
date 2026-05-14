from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import threading
import time
import re
import shutil
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageDraw, ImageGrab, ImageTk

from . import __version__
from .cli import SUPPORTED_EXTS, parse_max_bytes, parse_size, run_convert, run_split_sheet, run_stitch_vertical

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BaseTk = TkinterDnD.Tk
    DND_READY = True
except Exception:
    DND_FILES = None
    BaseTk = tk.Tk
    DND_READY = False


BG = "#FCFAF8"
SIDEBAR_BG = "#F7F2F5"
CARD = "#FFFFFF"
LINE = "#E7DEE7"
TEXT = "#4B3A45"
TEXT_SOFT = "#8B7A86"
PINK = "#F5A8C1"
PINK_SOFT = "#FDE7EF"
GREEN = "#BDF0C9"
GREEN_TEXT = "#277A4A"
YELLOW = "#FFE08A"
YELLOW_SOFT = "#FFF4CA"
BLUE_SOFT = "#ECF7FF"
LAVENDER = "#F4F0FF"
MINT = "#EEF9F0"
PILL_BG = "#EAF8ED"


def resource_path(relative: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).resolve().parents[1] / relative


def make_card(master: tk.Misc, bg: str = CARD, border: str = LINE, *, padx: int = 18, pady: int = 18) -> tuple[tk.Frame, tk.Frame]:
    outer = tk.Frame(master, bg=bg, highlightthickness=1, highlightbackground=border, bd=0)
    inner = tk.Frame(outer, bg=bg, padx=padx, pady=pady)
    inner.pack(fill="both", expand=True)
    return outer, inner


class TogglePanel(tk.Frame):
    def __init__(self, master: tk.Misc, title: str, *, bg: str = BG) -> None:
        super().__init__(master, bg=bg)
        self._title = title
        self._open = False
        self._bg = bg
        self.button = tk.Button(
            self,
            text=f"{title}  +",
            command=self.toggle,
            bg=bg,
            fg=TEXT_SOFT,
            activebackground=bg,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            anchor="w",
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.button.pack(anchor="w")
        self.card, self.body = make_card(self, bg=LAVENDER, border=LAVENDER, padx=16, pady=14)

    def toggle(self) -> None:
        self._open = not self._open
        self.button.configure(text=f"{self._title}  {'-' if self._open else '+'}")
        if self._open:
            self.card.pack(fill="x", pady=(8, 0))
        else:
            self.card.pack_forget()


class BubbleButton(tk.Canvas):
    def __init__(self, master: tk.Misc, text: str, command, *, active: bool = False) -> None:
        super().__init__(master, width=172, height=46, bg=SIDEBAR_BG, highlightthickness=0, bd=0, cursor="hand2")
        self.text = text
        self.command = command
        self.active = active
        self.hover = False
        self.bind("<Button-1>", lambda _event: self.command())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.draw()

    def set_active(self, active: bool) -> None:
        self.active = active
        self.draw()

    def _enter(self, _event) -> None:
        self.hover = True
        self.draw()

    def _leave(self, _event) -> None:
        self.hover = False
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        fill = PINK if self.active else (PINK_SOFT if self.hover else CARD)
        outline = PINK if self.active or self.hover else LINE
        self.create_round_rect(2, 3, 170, 43, 14, fill=fill, outline=outline, width=2)
        self.create_text(86, 23, text=self.text, fill=TEXT, font=("Microsoft YaHei UI", 11, "bold"))

    def create_round_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs) -> None:
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        self.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


class MemeGui(BaseTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("表情包工坊")
        self.geometry("980x760")
        self.minsize(900, 700)
        self.configure(bg=BG)

        self._running = False
        self._mode = "split"
        self._toast_reset = None
        self._preview_refs: dict[str, ImageTk.PhotoImage] = {}
        self._input_views: dict[str, dict[str, tk.Widget]] = {}
        self._log_widgets: list[ScrolledText] = []
        self._last_output_file: Path | None = None
        self._nav_buttons: dict[str, tk.Button] = {}
        self._start_button: tk.Button | None = None
        self._open_result_button: tk.Button | None = None
        self._status_label: tk.Label | None = None
        self._action_buttons: dict[str, tk.Button] = {}
        self._status_labels: dict[str, tk.Label] = {}
        self._open_result_buttons: dict[str, tk.Button] = {}
        self._output_list_body: tk.Frame | None = None
        self._output_canvas: tk.Canvas | None = None
        self._output_thumb_refs: list[ImageTk.PhotoImage] = []
        self._grid_buttons: list[tuple[tk.Button, int, int]] = []

        self.convert_vars = {
            "input": tk.StringVar(),
            "output": tk.StringVar(),
            "format": tk.StringVar(value="gif"),
            "size": tk.StringVar(value="200"),
            "mode": tk.StringVar(value="2"),
            "max_bytes": tk.StringVar(value="524288"),
            "keep_gif": tk.BooleanVar(value=False),
            "dedupe": tk.BooleanVar(value=True),
            "wechat_safe": tk.BooleanVar(value=True),
            "transparent_bg": tk.BooleanVar(value=True),
            "manifest_csv": tk.BooleanVar(value=True),
        }
        self.split_vars = {
            "input": tk.StringVar(),
            "output": tk.StringVar(),
            "rows": tk.StringVar(value="3"),
            "cols": tk.StringVar(value="3"),
            "size": tk.StringVar(value="200"),
            "format": tk.StringVar(value="gif"),
            "mode": tk.StringVar(value="2"),
            "search_ratio": tk.StringVar(value="0.18"),
            "trim_pad": tk.StringVar(value="8"),
            "bg_tolerance": tk.StringVar(value="18"),
            "white_threshold": tk.StringVar(value="245"),
            "transparent_bg": tk.BooleanVar(value=True),
        }
        self.stitch_vars = {
            "input": tk.StringVar(),
            "output": tk.StringVar(),
            "cell_size": tk.StringVar(value="1080"),
            "gutter": tk.StringVar(value="8"),
            "format": tk.StringVar(value="png"),
            "xhs_plan": tk.StringVar(),
            "captions": tk.StringVar(),
            "caption_height": tk.StringVar(value="0"),
            "caption_font_size": tk.StringVar(value="64"),
            "preview_enabled": tk.BooleanVar(value=True),
            "post_author": tk.StringVar(value="小猫松弛所"),
            "post_avatar": tk.StringVar(),
            "post_title": tk.StringVar(value="松弛感自救的 9 个小动作"),
            "post_content": tk.StringVar(value="越忙越要把自己放回来。先稳住，再往前走。"),
            "post_tags": tk.StringVar(value="#松弛感 #自我修复 #情绪稳定 #治愈 #小红书插图"),
        }

        self._setup_icon()
        self._setup_style()
        self._build_ui()
        self._switch_mode("split")
        self.bind_all("<Control-v>", self._handle_global_paste, add=True)

    def _setup_icon(self) -> None:
        icon_path = resource_path("assets/app_icon.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(default=str(icon_path))
            except Exception:
                pass

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Cute.TCombobox", padding=6)

    def _build_ui(self) -> None:
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True)
        self._build_sidebar(root)
        self._build_main(root)

    def _build_sidebar(self, root: tk.Frame) -> None:
        bar = tk.Frame(root, bg=SIDEBAR_BG, width=208)
        bar.pack(side="left", fill="y")
        bar.pack_propagate(False)

        head = tk.Frame(bar, bg=SIDEBAR_BG, padx=22, pady=26)
        head.pack(fill="x")
        tk.Label(head, text="表情包工坊", bg=SIDEBAR_BG, fg=TEXT, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        tk.Label(head, text="LOCAL MEME TOOL", bg=SIDEBAR_BG, fg=TEXT_SOFT, font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(2, 0))

        nav = tk.Frame(bar, bg=SIDEBAR_BG, padx=18, pady=10)
        nav.pack(fill="x")
        self._nav_buttons["split"] = self._sidebar_button(nav, "九宫格切图", lambda: self._switch_mode("split"))
        self._nav_buttons["convert"] = self._sidebar_button(nav, "转换器", lambda: self._switch_mode("convert"), active=False)
        self._nav_buttons["stitch"] = self._sidebar_button(nav, "组图器", lambda: self._switch_mode("stitch"), active=False)
        self._nav_buttons["outputs"] = self._sidebar_button(nav, "输出目录", lambda: self._switch_mode("outputs"), active=False)
        self._nav_buttons["clear"] = self._sidebar_button(nav, "清空当前", lambda: self._clear_current(self._mode), active=False)
        self._nav_buttons["split"].pack(fill="x", pady=(0, 10))
        self._nav_buttons["convert"].pack(fill="x", pady=(0, 10))
        self._nav_buttons["stitch"].pack(fill="x", pady=(0, 10))
        self._nav_buttons["outputs"].pack(fill="x", pady=(0, 10))
        self._nav_buttons["clear"].pack(fill="x", pady=(18, 0))

        footer = tk.Frame(bar, bg=SIDEBAR_BG, padx=18, pady=18)
        footer.pack(side="bottom", fill="x")
        badge = tk.Frame(footer, bg=CARD, padx=14, pady=12, highlightthickness=1, highlightbackground=LINE)
        badge.pack(fill="x")
        avatar = tk.Canvas(badge, width=40, height=40, bg=CARD, highlightthickness=0)
        avatar.pack(side="left")
        avatar.create_oval(2, 2, 38, 38, fill=PINK, outline="")
        avatar.create_text(20, 20, text="我", fill="white", font=("Microsoft YaHei UI", 10, "bold"))
        meta = tk.Frame(badge, bg=CARD)
        meta.pack(side="left", padx=10)
        tk.Label(meta, text="当前用户", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(meta, text="本地模式", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 8)).pack(anchor="w")

    def _build_main(self, root: tk.Frame) -> None:
        main = tk.Frame(root, bg=BG, padx=28, pady=20)
        main.pack(side="left", fill="both", expand=True)
        self.main = main

        self._hero_title = tk.Label(main, text="", bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 24, "bold"))
        self._hero_title.pack()
        self._hero_sub = tk.Label(main, text="", bg=BG, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 10))
        self._hero_sub.pack(pady=(6, 12))

        self._toast_outer, toast_body = make_card(main, bg=PILL_BG, border=PILL_BG, padx=18, pady=9)
        self._toast_outer.pack(pady=(0, 12))
        self._toast_label = tk.Label(toast_body, text="", bg=PILL_BG, fg=GREEN_TEXT, font=("Microsoft YaHei UI", 10, "bold"))
        self._toast_label.pack()

        self.page_host = tk.Frame(main, bg=BG)
        self.page_host.pack(fill="both", expand=True)
        self.pages = {
            "split": self._build_split_page(),
            "convert": self._build_convert_page(),
            "stitch": self._build_stitch_page(),
            "outputs": self._build_outputs_page(),
        }

    def _build_convert_page(self) -> tk.Frame:
        page = tk.Frame(self.page_host, bg=BG)
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=1)
        self._build_drop_zone(
            page,
            mode="convert",
            title="放入图片",
            subtitle="选择图片或文件夹，批量转成表情图片",
            allow_dir=True,
            browse_text="选择图片 / 文件夹",
        ).grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self._build_convert_options(page).grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        return page

    def _build_split_page(self) -> tk.Frame:
        page = tk.Frame(self.page_host, bg=BG)
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=1)
        self._build_drop_zone(
            page,
            mode="split",
            title="放入拼图",
            subtitle="拖入拼图或粘贴图片",
            allow_dir=False,
            browse_text="选择拼图",
        ).grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self._build_split_options(page).grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        return page

    def _build_stitch_page(self) -> tk.Frame:
        page = tk.Frame(self.page_host, bg=BG)
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=1)
        self._build_drop_zone(
            page,
            mode="stitch",
            title="放入多张图片",
            subtitle="按文件名顺序，两张一组上下拼接",
            allow_dir=True,
            browse_text="选择多张图片 / 文件夹",
        ).grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self._build_stitch_options(page).grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        return page

    def _build_drop_zone(
        self,
        parent: tk.Frame,
        *,
        mode: str,
        title: str,
        subtitle: str,
        allow_dir: bool,
        browse_text: str,
    ) -> tk.Frame:
        wrap = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE, padx=24, pady=16)
        wrap.grid_columnconfigure(0, weight=1)

        tk.Label(wrap, text=title, bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 16, "bold")).grid(row=0, column=0, pady=(0, 10))

        preview_shell = tk.Frame(wrap, bg="#FAFAFC", highlightthickness=1, highlightbackground=LINE, width=250, height=250)
        preview_shell.grid(row=1, column=0)
        preview_shell.pack_propagate(False)
        empty_text = "点击选择图片/文件夹\n或拖拽 / Ctrl+V 粘贴" if allow_dir else "点击选择图片\n或拖拽 / Ctrl+V 粘贴"
        preview = tk.Label(preview_shell, bg="#FAFAFC", fg=TEXT_SOFT, text=empty_text, justify="center", font=("Microsoft YaHei UI", 12))
        preview.pack(fill="both", expand=True)

        hint = "支持拖拽和 Ctrl+V 粘贴" if DND_READY else "可用 Ctrl+V 粘贴，或点击选择图片"
        info = tk.Label(wrap, bg=CARD, fg=TEXT_SOFT, text=hint, justify="center", font=("Microsoft YaHei UI", 9), wraplength=520)
        info.grid(row=2, column=0, pady=(8, 0))

        action = tk.Frame(wrap, bg=CARD)
        action.grid(row=3, column=0, pady=(10, 0))
        choose_cmd = (lambda: self._choose_stitch_input()) if mode == "stitch" else (lambda: self._choose_input_file_or_dir(mode)) if allow_dir else (lambda: self._choose_input_file(mode))
        self._small_button(action, browse_text, choose_cmd, bg=PINK_SOFT).pack()
        if mode == "stitch":
            self._small_button(action, "选择切图结果目录", self._choose_split_output_for_stitch, bg=MINT).pack(pady=(8, 0))

        self._input_views[mode] = {"preview": preview, "info": info}
        preview_shell.bind("<Button-1>", lambda _event: choose_cmd())
        preview.bind("<Button-1>", lambda _event: choose_cmd())
        for widget in (wrap, preview_shell, preview):
            self._bind_drop(widget, mode, allow_dir)
        return wrap

    def _draw_drop_bg(self, canvas: tk.Canvas) -> None:
        canvas.create_rectangle(0, 0, 4000, 4000, fill=BG, outline="")
        canvas.create_oval(56, 46, 132, 122, fill="#FFFFFF", outline="")
        canvas.create_oval(856, 46, 932, 122, fill="#FFFFFF", outline="")
        canvas.create_oval(772, 214, 876, 318, fill="#FFFFFF", outline="")

    def _build_primary_action(self, parent: tk.Frame, text: str, command) -> tk.Frame:
        wrap = tk.Frame(parent, bg=BG)
        button = tk.Button(
            wrap,
            text=text,
            command=command,
            bg=GREEN,
            fg=GREEN_TEXT,
            activebackground=GREEN,
            activeforeground=GREEN_TEXT,
            relief="flat",
            bd=0,
            padx=46,
            pady=12,
            font=("Microsoft YaHei UI", 14, "bold"),
            cursor="hand2",
        )
        button.pack()
        return wrap

    def _build_output_card(self, parent: tk.Frame, variable: tk.StringVar) -> tk.Frame:
        outer, body = make_card(parent, bg=CARD, border=LINE, padx=18, pady=13)
        tk.Label(body, text="输出目录", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(body, text="默认会跟随输入路径，也可以手动指定。", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, sticky="w", pady=(4, 10))
        body.grid_columnconfigure(0, weight=1)
        row = tk.Frame(body, bg=CARD)
        row.grid(row=2, column=0, sticky="ew")
        entry = tk.Entry(row, textvariable=variable, relief="flat", bd=0, bg=MINT, fg=TEXT, font=("Microsoft YaHei UI", 10))
        entry.pack(side="left", fill="x", expand=True, ipady=9)
        self._small_button(row, "选目录", lambda: self._choose_output_dir(variable), bg=YELLOW_SOFT).pack(side="left", padx=8)
        self._small_button(row, "打开", lambda: self._open_dir(variable.get()), bg=SIDEBAR_BG).pack(side="left")
        return outer

    def _build_convert_options(self, parent: tk.Frame) -> tk.Frame:
        outer, body = make_card(parent, bg=CARD, border=LINE, padx=24, pady=14)
        tk.Label(body, text="格式", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self._chip_group(body, self.convert_vars["format"], [("GIF", "gif"), ("PNG", "png")]).pack(anchor="w", pady=(8, 16))

        tk.Label(body, text="尺寸", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self._chip_group(body, self.convert_vars["size"], [("原图", "raw"), ("200px", "200"), ("240px", "240")]).pack(anchor="w", pady=(8, 16))

        tk.Checkbutton(
            body,
            text="背景转透明",
            variable=self.convert_vars["transparent_bg"],
            bg=CARD,
            fg=TEXT,
            activebackground=CARD,
            selectcolor=CARD,
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(0, 8))

        self._build_mode_action(body, mode="convert", idle_text="请先选择图片", action_text="开始制作", command=self.start_convert)
        return outer

    def _build_split_options(self, parent: tk.Frame) -> tk.Frame:
        outer, body = make_card(parent, bg=CARD, border=LINE, padx=24, pady=14)

        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x")

        tk.Label(grid, text="宫格", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        grid_row = tk.Frame(grid, bg=CARD)
        grid_row.pack(anchor="w", pady=(8, 16))
        self._build_grid_button(grid_row, "3 x 3", 3, 3).pack(side="left", padx=(0, 8))
        self._build_grid_button(grid_row, "4 x 4", 4, 4).pack(side="left", padx=(0, 8))
        self._build_grid_button(grid_row, "5 x 5", 5, 5).pack(side="left")
        self._refresh_grid_buttons()

        tk.Label(grid, text="格式", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self._chip_group(grid, self.split_vars["format"], [("GIF", "gif"), ("PNG", "png")]).pack(anchor="w", pady=(8, 16))

        tk.Label(grid, text="尺寸", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self._chip_group(grid, self.split_vars["size"], [("原图", "raw"), ("200px", "200"), ("240px", "240")]).pack(anchor="w", pady=(8, 10))

        transparent = tk.Checkbutton(
            body,
            text="背景转透明",
            variable=self.split_vars["transparent_bg"],
            bg=CARD,
            fg=TEXT,
            activebackground=CARD,
            selectcolor=CARD,
            font=("Microsoft YaHei UI", 10),
        )
        transparent.pack(anchor="w", pady=(0, 8))

        self._build_mode_action(body, mode="split", idle_text="请先选择拼图", action_text="开始切图", command=self.start_split)
        return outer

    def _build_stitch_options(self, parent: tk.Frame) -> tk.Frame:
        outer, body = make_card(parent, bg=CARD, border=LINE, padx=24, pady=14)
        tk.Label(body, text="小红书模板", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        tk.Label(body, text="自动裁掉外圈白边，再按宽度铺满；8 张图会输出 4 组。", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9), wraplength=360).pack(anchor="w", pady=(5, 14))
        self._small_button(body, "复制小红书爆款 Skill", self._copy_xhs_skill_prompt, bg=YELLOW_SOFT).pack(anchor="w", pady=(0, 14))

        tk.Label(body, text="格式", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self._chip_group(body, self.stitch_vars["format"], [("PNG", "png"), ("JPG", "jpg")]).pack(anchor="w", pady=(8, 16))

        fields = tk.Frame(body, bg=CARD)
        fields.pack(fill="x", pady=(0, 8))
        tk.Label(fields, text="成图宽度", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 10)).grid(row=0, column=0, sticky="w")
        tk.Entry(fields, textvariable=self.stitch_vars["cell_size"], relief="flat", bd=0, bg=MINT, fg=TEXT, width=10).grid(row=0, column=1, sticky="w", padx=(8, 20), ipady=6)
        tk.Label(fields, text="图片间隔", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 10)).grid(row=1, column=0, sticky="w", pady=(10, 0))
        tk.Entry(fields, textvariable=self.stitch_vars["gutter"], relief="flat", bd=0, bg=MINT, fg=TEXT, width=10).grid(row=1, column=1, sticky="w", padx=(8, 20), pady=(10, 0), ipady=6)

        tk.Label(fields, text="文案文件", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 10)).grid(row=2, column=0, sticky="w", pady=(10, 0))
        plan_row = tk.Frame(fields, bg=CARD)
        plan_row.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))
        plan_row.grid_columnconfigure(0, weight=1)
        tk.Entry(plan_row, textvariable=self.stitch_vars["xhs_plan"], relief="flat", bd=0, bg=MINT, fg=TEXT).grid(row=0, column=0, sticky="ew", ipady=6)
        self._small_button(plan_row, "选择", self._choose_stitch_text_plan, bg=PINK).grid(row=0, column=1, padx=(8, 0))
        tk.Label(
            fields,
            text="可选：选择 xhs_plan.json 或 captions.txt；不选就只拼图不加字幕。",
            bg=CARD,
            fg=TEXT_SOFT,
            font=("Microsoft YaHei UI", 9),
            wraplength=320,
        ).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
        fields.grid_columnconfigure(1, weight=1)

        self._build_mode_action(body, mode="stitch", idle_text="请先选择多张图片", action_text="开始组图", command=self.start_stitch)

        preview = TogglePanel(body, "发布预览设置", bg=CARD)
        preview.pack(fill="x", pady=(12, 0))
        tk.Checkbutton(
            preview.body,
            text="完成后打开小红书发布预览",
            variable=self.stitch_vars["preview_enabled"],
            bg=LAVENDER,
            fg=TEXT,
            activebackground=LAVENDER,
            selectcolor=LAVENDER,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 8))
        post_fields = tk.Frame(preview.body, bg=LAVENDER)
        post_fields.pack(fill="x")
        for idx in range(2):
            post_fields.grid_columnconfigure(idx, weight=1 if idx == 1 else 0)
        self._labeled_entry(post_fields, "作者名", self.stitch_vars["post_author"], row=0)
        self._labeled_entry(post_fields, "头像", self.stitch_vars["post_avatar"], row=1, button=("选择", self._choose_post_avatar))
        self._labeled_entry(post_fields, "标题", self.stitch_vars["post_title"], row=2)
        self._labeled_entry(post_fields, "内容", self.stitch_vars["post_content"], row=3)
        self._labeled_entry(post_fields, "标签", self.stitch_vars["post_tags"], row=4)

        return outer

    def _build_mode_action(self, parent: tk.Frame, *, mode: str, idle_text: str, action_text: str, command) -> None:
        action = tk.Frame(parent, bg=CARD)
        action.pack(fill="x", pady=(14, 0))
        button = tk.Button(
            action,
            text=idle_text,
            command=command,
            bg="#E8E3E8",
            fg=TEXT_SOFT,
            disabledforeground=TEXT_SOFT,
            activebackground=GREEN,
            activeforeground=GREEN_TEXT,
            relief="flat",
            bd=0,
            padx=68,
            pady=13,
            font=("Microsoft YaHei UI", 15, "bold"),
            cursor="arrow",
            state="disabled",
        )
        button.pack()
        status = tk.Label(action, text="完成后自动打开结果目录", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9))
        status.pack(pady=(10, 0))
        open_button = self._small_button(action, "再次打开结果目录", lambda m=mode: self._open_dir(self._vars(m)["output"].get()), bg=SIDEBAR_BG)
        self._action_buttons[mode] = button
        self._status_labels[mode] = status
        self._open_result_buttons[mode] = open_button
        if mode == "split":
            self._start_button = button
            self._status_label = status
            self._open_result_button = open_button

    def _build_grid_button(self, parent: tk.Misc, text: str, rows: int, cols: int) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            command=lambda: self._set_grid_preset(rows, cols),
            bg=CARD,
            fg=TEXT,
            activebackground=PINK,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            padx=16,
            pady=8,
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
        )
        self._grid_buttons.append((button, rows, cols))
        return button

    def _refresh_grid_buttons(self) -> None:
        rows = int(self.split_vars["rows"].get() or "3")
        cols = int(self.split_vars["cols"].get() or "3")
        for button, b_rows, b_cols in self._grid_buttons:
            selected = rows == b_rows and cols == b_cols
            button.configure(bg=PINK if selected else CARD, activebackground=PINK if selected else PINK_SOFT)

    def _build_action_footer(self, parent: tk.Frame) -> tk.Frame:
        wrap = tk.Frame(parent, bg=BG)
        self._start_button = tk.Button(
            wrap,
            text="请先选择拼图",
            command=self.start_split,
            bg="#E8E3E8",
            fg=TEXT_SOFT,
            disabledforeground=TEXT_SOFT,
            activebackground=GREEN,
            activeforeground=GREEN_TEXT,
            relief="flat",
            bd=0,
            padx=68,
            pady=14,
            font=("Microsoft YaHei UI", 15, "bold"),
            cursor="hand2",
            state="disabled",
        )
        self._start_button.pack()
        self._status_label = tk.Label(wrap, text="完成后自动打开结果目录", bg=BG, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9))
        self._status_label.pack(pady=(12, 0))
        self._open_result_button = self._small_button(wrap, "再次打开结果目录", lambda: self._open_dir(self.split_vars["output"].get()), bg=SIDEBAR_BG)
        return wrap

    def _build_outputs_page(self) -> tk.Frame:
        page = tk.Frame(self.page_host, bg=BG)
        outer, body = make_card(page, bg=CARD, border=LINE, padx=24, pady=18)
        outer.pack(fill="both", expand=True)
        head = tk.Frame(body, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="输出目录", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        self._small_button(head, "批量删除", self._delete_all_output_dirs, bg=PINK_SOFT).pack(side="right", padx=(8, 0))
        self._small_button(head, "刷新", self._refresh_output_dirs, bg=PINK_SOFT).pack(side="right")
        tk.Label(body, text="这里会列出最近自动生成的结果目录，点击即可打开。", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(8, 16))
        list_wrap = tk.Frame(body, bg=CARD)
        list_wrap.pack(fill="both", expand=True)
        self._output_canvas = tk.Canvas(list_wrap, bg=CARD, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(
            list_wrap,
            orient="vertical",
            command=self._output_canvas.yview,
            bg="#F8D8E4",
            troughcolor="#FFF7FA",
            activebackground="#F2A1BD",
            relief="flat",
            bd=0,
            width=8,
        )
        self._output_canvas.configure(yscrollcommand=scrollbar.set)
        self._output_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._output_list_body = tk.Frame(self._output_canvas, bg=CARD)
        window = self._output_canvas.create_window((0, 0), window=self._output_list_body, anchor="nw")
        self._output_list_body.bind("<Configure>", lambda _event: self._output_canvas.configure(scrollregion=self._output_canvas.bbox("all")) if self._output_canvas else None)
        self._output_canvas.bind("<Configure>", lambda event: self._output_canvas.itemconfigure(window, width=event.width))
        self._output_canvas.bind("<Enter>", lambda _event: self.bind_all("<MouseWheel>", self._on_output_mousewheel, add=True))
        self._output_canvas.bind("<Leave>", lambda _event: self.unbind_all("<MouseWheel>"))
        self._refresh_output_dirs()
        return page

    def _output_root(self) -> Path:
        return Path(tempfile.gettempdir()) / "meme-workshop-output"

    def _on_output_mousewheel(self, event) -> None:
        if self._output_canvas is not None and getattr(event, "delta", 0):
            self._output_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _refresh_output_dirs(self) -> None:
        if self._output_list_body is None:
            return
        for child in self._output_list_body.winfo_children():
            child.destroy()
        self._output_thumb_refs.clear()
        root = self._output_root()
        dirs = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not dirs:
            tk.Label(self._output_list_body, text="还没有输出结果。完成一次切图或制作后会出现在这里。", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 10)).pack(anchor="w")
            return
        for path in dirs[:30]:
            row_bg = "#FBFBFC"
            row = tk.Frame(self._output_list_body, bg=row_bg, padx=14, pady=10, highlightthickness=1, highlightbackground=LINE, cursor="hand2")
            row.pack(fill="x", pady=(0, 8))
            thumb = self._make_output_dir_thumbnail(path)
            thumb_label = tk.Label(row, image=thumb, bg=row_bg, cursor="hand2")
            thumb_label.pack(side="left", padx=(0, 12))
            self._output_thumb_refs.append(thumb)
            actions = tk.Frame(row, bg=row_bg)
            actions.pack(side="right", padx=(8, 0))
            meta = tk.Frame(row, bg=row_bg, cursor="hand2")
            meta.pack(side="left", fill="x", expand=True)
            info = self._describe_output_dir(path)
            title = tk.Label(meta, text=info["display"], bg=row_bg, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2")
            title.pack(anchor="w")
            detail = tk.Label(meta, text=self._middle_ellipsis(info["detail"], 54), bg=row_bg, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9), cursor="hand2")
            detail.pack(anchor="w", pady=(3, 0))
            summary = tk.Label(meta, text=self._middle_ellipsis(info["summary"], 58), bg=row_bg, fg=GREEN_TEXT, font=("Microsoft YaHei UI", 9), cursor="hand2")
            summary.pack(anchor="w", pady=(3, 0))
            if info["kind"] == "切图":
                self._small_button(actions, "组图", lambda p=path: self._use_output_dir_for_stitch(p), bg=GREEN).pack(side="left", padx=(0, 6))
            delete_btn = tk.Button(
                actions,
                text="❌",
                command=lambda p=path: self._delete_output_dir(p),
                bg=row_bg,
                fg="#C25A78",
                activebackground=PINK_SOFT,
                activeforeground="#C25A78",
                relief="flat",
                bd=0,
                padx=8,
                pady=6,
                font=("Segoe UI Emoji", 11),
                cursor="hand2",
            )
            delete_btn.pack(side="left")
            for widget in (row, thumb_label, meta, title, detail, summary):
                widget.bind("<Button-1>", lambda _event, p=path: self._open_dir(str(p)))
                widget.bind("<Enter>", lambda _event, r=row, m=meta, t=thumb_label, a=actions, labels=(title, detail, summary): self._paint_output_row(r, m, t, a, labels, "#FFF7FA"))
                widget.bind("<Leave>", lambda _event, r=row, m=meta, t=thumb_label, a=actions, labels=(title, detail, summary): self._paint_output_row(r, m, t, a, labels, "#FBFBFC"))

    def _middle_ellipsis(self, text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        keep = max(8, (max_chars - 1) // 2)
        return f"{text[:keep]}…{text[-keep:]}"

    def _paint_output_row(self, row: tk.Frame, meta: tk.Frame, thumb: tk.Label, actions: tk.Frame, labels: tuple[tk.Label, ...], color: str) -> None:
        row.configure(bg=color)
        meta.configure(bg=color)
        thumb.configure(bg=color)
        actions.configure(bg=color)
        for label in labels:
            label.configure(bg=color)

    def _describe_output_dir(self, path: Path) -> dict[str, str]:
        name = path.name
        image_files = self._list_preview_images(path)
        count = len(image_files)
        kind = "切图" if "_split_" in name else "组图" if "_stitch_" in name else "制作" if "_gif_" in name or "_convert_" in name else "输出"
        stamp_match = re.search(r"(20\d{6}-\d{6})", name)
        if stamp_match:
            raw = stamp_match.group(1)
            nice_time = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[9:11]}:{raw[11:13]}"
        else:
            nice_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        token = self._extract_output_token(name, kind)
        display = f"{kind}-{nice_time}-{count}张-{token}"
        sample = "、".join(p.name for p in image_files[:3])
        summary = f"包含：{sample}" if sample else "没有可预览图片"
        if len(image_files) > 3:
            summary += f" 等 {len(image_files)} 张"
        return {
            "display": display,
            "detail": path.name,
            "summary": summary,
            "kind": kind,
        }

    def _list_preview_images(self, path: Path) -> list[Path]:
        if not path.exists() or not path.is_dir():
            return []
        skip_names = {"preview_boxes.png"}
        return sorted(
            [
                p for p in path.iterdir()
                if p.is_file()
                and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                and p.name not in skip_names
            ],
            key=lambda p: p.name.lower(),
        )

    def _make_output_dir_thumbnail(self, path: Path) -> ImageTk.PhotoImage:
        canvas = Image.new("RGBA", (132, 88), (255, 255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((0, 0, 131, 87), radius=10, fill=(255, 255, 255, 255), outline=(231, 222, 231, 255), width=1)
        images = self._list_preview_images(path)[:4]
        if not images:
            draw.text((66, 44), "空目录", fill=(139, 122, 134, 255), anchor="mm", font=("Microsoft YaHei UI", 10))
            return ImageTk.PhotoImage(canvas)

        boxes = [(8, 8, 62, 40), (70, 8, 124, 40), (8, 48, 62, 80), (70, 48, 124, 80)]
        if len(images) == 1:
            boxes = [(10, 8, 122, 80)]
        elif len(images) == 2:
            boxes = [(8, 12, 62, 76), (70, 12, 124, 76)]
        for image_path, box in zip(images, boxes):
            x0, y0, x1, y1 = box
            try:
                with Image.open(image_path) as image:
                    thumb = image.convert("RGBA")
                    thumb.thumbnail((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
            except Exception:
                continue
            px = x0 + ((x1 - x0) - thumb.width) // 2
            py = y0 + ((y1 - y0) - thumb.height) // 2
            draw.rounded_rectangle((x0, y0, x1, y1), radius=6, fill=(250, 250, 252, 255), outline=(231, 222, 231, 255))
            canvas.alpha_composite(thumb, (px, py))
        return ImageTk.PhotoImage(canvas)

    def _use_output_dir_for_stitch(self, path: Path) -> None:
        if not path.exists() or not path.is_dir():
            messagebox.showerror("目录不存在", str(path))
            return
        self._switch_mode("stitch")
        self.after(30, lambda p=path: self._set_input("stitch", p))
        self.after(60, lambda: self._flash_toast("已把这个切图目录放入组图器，可以直接开始组图。"))

    def _extract_output_token(self, name: str, kind: str) -> str:
        marker = "_split_" if kind == "切图" else "_stitch_" if kind == "组图" else "_gif_" if "_gif_" in name else "_convert_"
        token = name.split(marker, 1)[0] if marker in name else name
        token = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "", token).strip("_-")
        if not token:
            return "gogogo"
        if len(token) > 18:
            token = f"{token[:8]}…{token[-6:]}"
        return token

    def _delete_output_dir(self, path: Path) -> None:
        root = self._output_root().resolve()
        target = path.resolve()
        if root not in target.parents:
            messagebox.showerror("删除失败", "目标目录不在输出根目录内。")
            return
        if not target.exists() or not target.is_dir():
            self._refresh_output_dirs()
            return
        if not messagebox.askyesno("确认删除", f"确定删除这个输出目录吗？\n\n{target.name}"):
            return
        shutil.rmtree(target)
        self._refresh_output_dirs()

    def _delete_all_output_dirs(self) -> None:
        root = self._output_root()
        dirs = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
        if not dirs:
            self._refresh_output_dirs()
            return
        if not messagebox.askyesno("批量删除", f"确定删除全部 {len(dirs)} 个输出目录吗？"):
            return
        for path in dirs:
            shutil.rmtree(path)
        self._refresh_output_dirs()

    def _option_panel(self, parent: tk.Frame, title: str, subtitle: str, *, bg: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=bg, bd=0)
        outer.inner = tk.Frame(outer, bg=bg, padx=18, pady=14)  # type: ignore[attr-defined]
        outer.inner.pack(fill="both", expand=True)  # type: ignore[attr-defined]
        head = tk.Frame(outer.inner, bg=bg)  # type: ignore[attr-defined]
        head.pack(fill="x")
        tk.Label(head, text=title, bg=bg, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        tk.Label(head, text=subtitle, bg=bg, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9)).pack(side="right")
        return outer

    def _build_convert_advanced(self, parent: tk.Frame) -> None:
        tk.Label(parent, text="体积上限(bytes)", bg=LAVENDER, fg=TEXT, font=("Microsoft YaHei UI", 10)).grid(row=0, column=0, sticky="w")
        tk.Entry(parent, textvariable=self.convert_vars["max_bytes"], relief="flat", bd=0, bg=CARD, fg=TEXT, width=16).grid(row=0, column=1, sticky="w", padx=(8, 20), ipady=6)
        tk.Checkbutton(parent, text="源 GIF 直拷", variable=self.convert_vars["keep_gif"], bg=LAVENDER, fg=TEXT, activebackground=LAVENDER, selectcolor=LAVENDER, font=("Microsoft YaHei UI", 10)).grid(row=1, column=0, sticky="w", pady=(12, 0))
        tk.Checkbutton(parent, text="去重", variable=self.convert_vars["dedupe"], bg=LAVENDER, fg=TEXT, activebackground=LAVENDER, selectcolor=LAVENDER, font=("Microsoft YaHei UI", 10)).grid(row=1, column=1, sticky="w", pady=(12, 0))
        tk.Checkbutton(parent, text="导出 manifest.csv", variable=self.convert_vars["manifest_csv"], bg=LAVENDER, fg=TEXT, activebackground=LAVENDER, selectcolor=LAVENDER, font=("Microsoft YaHei UI", 10)).grid(row=1, column=2, sticky="w", pady=(12, 0))

    def _build_split_advanced(self, parent: tk.Frame) -> None:
        fields = [
            ("search ratio", self.split_vars["search_ratio"]),
            ("trim pad", self.split_vars["trim_pad"]),
            ("bg tolerance", self.split_vars["bg_tolerance"]),
            ("white threshold", self.split_vars["white_threshold"]),
        ]
        for idx, (label, variable) in enumerate(fields):
            tk.Label(parent, text=label, bg=LAVENDER, fg=TEXT, font=("Microsoft YaHei UI", 10)).grid(row=idx // 2, column=(idx % 2) * 2, sticky="w", pady=(0, 10))
            tk.Entry(parent, textvariable=variable, relief="flat", bd=0, bg=CARD, fg=TEXT, width=12).grid(row=idx // 2, column=(idx % 2) * 2 + 1, sticky="w", padx=(8, 20), pady=(0, 10), ipady=6)

    def _build_toolbox(self, parent: tk.Frame) -> tk.Frame:
        outer, body = make_card(parent, bg=CARD, border=LINE, padx=18, pady=14)
        tk.Label(body, text="工具箱", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x", pady=(12, 0))
        for idx in range(3):
            grid.grid_columnconfigure(idx, weight=1)
        self._tool_button(grid, "粘贴 / 导入", lambda: self._paste_clipboard(self._mode, True)).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._tool_button(grid, "打开输出目录", lambda: self._open_dir(self._current_output_var().get())).grid(row=0, column=1, sticky="ew", padx=8)
        self._tool_button(grid, "打开当前文件", self._open_last_output).grid(row=0, column=2, sticky="ew", padx=(8, 0))
        self._tool_button(grid, "清空状态", lambda: self._clear_current(self._mode)).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(10, 0))
        self._tool_button(grid, "切换高级设置", self._toggle_advanced_current).grid(row=1, column=1, sticky="ew", padx=8, pady=(10, 0))
        self._tool_button(grid, "查看日志", self._toggle_logs).grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=(10, 0))
        self._tool_button(grid, "发布预览", lambda: self._show_xhs_publish_preview()).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self._recent_label = tk.Label(body, text="最近结果：还没有输出文件", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9))
        self._recent_label.pack(anchor="w", pady=(12, 0))
        return outer

    def _build_log_panel(self, parent: tk.Frame) -> tk.Frame:
        panel = TogglePanel(parent, "运行日志")
        log = ScrolledText(panel.body, height=7, wrap="word", font=("Consolas", 10), relief="flat", bd=0, bg=CARD, fg=TEXT)
        log.pack(fill="both", expand=True)
        log.configure(state="disabled")
        self._log_widgets.append(log)
        row = tk.Frame(panel.body, bg=LAVENDER)
        row.pack(fill="x", pady=(8, 0))
        self._small_button(row, "清空日志", self._clear_log, bg=CARD).pack(side="left")
        return panel

    def _sidebar_button(self, parent: tk.Misc, text: str, command, *, active: bool = True) -> BubbleButton:
        return BubbleButton(parent, text, command, active=active)

    def _small_button(self, parent: tk.Misc, text: str, command, *, bg: str) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=TEXT,
            activebackground=bg,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
        )

    def _labeled_entry(
        self,
        parent: tk.Frame,
        label: str,
        variable: tk.StringVar,
        *,
        row: int,
        button: tuple[str, object] | None = None,
    ) -> None:
        tk.Label(parent, text=label, bg=parent.cget("bg"), fg=TEXT, font=("Microsoft YaHei UI", 10)).grid(row=row, column=0, sticky="w", pady=(0, 8))
        wrap = tk.Frame(parent, bg=parent.cget("bg"))
        wrap.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=(0, 8))
        wrap.grid_columnconfigure(0, weight=1)
        tk.Entry(wrap, textvariable=variable, relief="flat", bd=0, bg=CARD, fg=TEXT).grid(row=0, column=0, sticky="ew", ipady=6)
        if button:
            self._small_button(wrap, button[0], button[1], bg=CARD).grid(row=0, column=1, padx=(8, 0))

    def _tool_button(self, parent: tk.Misc, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#FBFBFC",
            fg=TEXT,
            activebackground="#FBFBFC",
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            padx=12,
            pady=10,
            font=("Microsoft YaHei UI", 10),
            cursor="hand2",
        )

    def _chip_group(self, parent: tk.Misc, variable: tk.StringVar, options: list[tuple[str, str]]) -> tk.Frame:
        row = tk.Frame(parent, bg=parent.cget("bg"))
        buttons: list[tuple[tk.Button, str]] = []

        def refresh(*_args) -> None:
            current = variable.get()
            for button, value in buttons:
                if value == current:
                    button.configure(bg=PINK, fg=TEXT, activebackground=PINK)
                else:
                    button.configure(bg=CARD, fg=TEXT, activebackground=CARD)

        for label, value in options:
            button = tk.Button(
                row,
                text=label,
                bg=CARD,
                fg=TEXT,
                activebackground=CARD,
                activeforeground=TEXT,
                relief="flat",
                bd=0,
                padx=16,
                pady=8,
                font=("Microsoft YaHei UI", 10, "bold"),
                command=lambda selected=value: variable.set(selected),
                cursor="hand2",
            )
            button.pack(side="left", padx=(0, 10))
            buttons.append((button, value))
        variable.trace_add("write", refresh)
        refresh()
        return row

    def _current_output_var(self) -> tk.StringVar:
        if self._mode == "convert":
            return self.convert_vars["output"]
        if self._mode == "stitch":
            return self.stitch_vars["output"]
        return self.split_vars["output"]

    def _vars(self, mode: str) -> dict[str, tk.Variable]:
        if mode == "convert":
            return self.convert_vars
        if mode == "stitch":
            return self.stitch_vars
        return self.split_vars

    def _on_mousewheel(self, event) -> None:
        if hasattr(self, "page_canvas") and getattr(event, "delta", 0):
            self.page_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _switch_mode(self, mode: str) -> None:
        if mode not in self.pages:
            mode = "split"
        self._mode = mode
        for key, frame in self.pages.items():
            if key == mode:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for key, button in self._nav_buttons.items():
            if isinstance(button, BubbleButton):
                button.set_active(key == mode)
        if mode == "split":
            self._hero_title.configure(text="九宫格切图")
            self._hero_sub.configure(text="拖入拼图或粘贴图片，选宫格，点开始。结果会自动打开。")
            self._set_toast("准备好了，选择或粘贴一张拼图即可开始。")
        elif mode == "convert":
            self._hero_title.configure(text="转换器")
            self._hero_sub.configure(text="把单张或整个文件夹的图片批量转成 GIF / PNG，可自动背景转透明。")
            self._set_toast("选择图片或文件夹即可开始转换。")
        elif mode == "stitch":
            self._hero_title.configure(text="组图器")
            self._hero_sub.configure(text="内置小红书上下拼接模板，多张图片按顺序两两成组。")
            self._set_toast("选择多张图片或文件夹，8 张会输出 4 组上下拼接图。")
        else:
            self._hero_title.configure(text="输出目录")
            self._hero_sub.configure(text="快速打开最近自动生成的结果目录。")
            self._set_toast("这里会自动收集切图和制作输出。")
            self._refresh_output_dirs()

    def _set_toast(self, text: str) -> None:
        self._toast_label.configure(text=text)
        if self._toast_reset is not None:
            self.after_cancel(self._toast_reset)
            self._toast_reset = None

    def _flash_toast(self, text: str, *, reset: bool = True) -> None:
        self._set_toast(text)
        if reset:
            self._toast_reset = self.after(3000, lambda: self._switch_mode(self._mode))

    def _toggle_advanced_current(self) -> None:
        if self._mode == "convert":
            self.convert_advanced.toggle()
        elif self._mode == "split":
            self.split_advanced.toggle()

    def _toggle_logs(self) -> None:
        panel = self.convert_log_panel if self._mode == "convert" else self.split_log_panel
        panel.toggle()

    def _bind_drop(self, widget: tk.Widget, mode: str, allow_dir: bool) -> None:
        if not DND_READY:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", lambda event: self._handle_drop(event.data, mode, allow_dir))
        except Exception:
            pass

    def _handle_drop(self, data: str, mode: str, allow_dir: bool) -> None:
        try:
            items = self.tk.splitlist(data)
        except Exception:
            items = [data]
        if mode == "stitch":
            paths = [Path(raw.strip("{}")).expanduser() for raw in items]
            files = [path for path in paths if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS]
            dirs = [path for path in paths if path.is_dir()]
            if len(files) > 1:
                self._set_input_value(mode, "\n".join(str(path) for path in files), files[0])
                self._append_log(f"[drop] {mode} <- {len(files)} files")
                self._flash_toast("已接收多张图片，将按拖入顺序组图。")
                return
            if dirs and allow_dir:
                self._set_input(mode, dirs[0])
                self._append_log(f"[drop] {mode} <- {dirs[0]}")
                self._flash_toast("已接收图片文件夹。")
                return
        for raw in items:
            path = Path(raw.strip("{}")).expanduser()
            if not path.exists():
                continue
            if path.is_dir() and allow_dir:
                self._set_input(mode, path)
                self._append_log(f"[drop] {mode} <- {path}")
                self._flash_toast("已接收拖拽内容。")
                return
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
                self._set_input(mode, path)
                self._append_log(f"[drop] {mode} <- {path}")
                self._flash_toast("已接收拖拽内容。")
                return

    def _choose_input_file_or_dir(self, mode: str) -> None:
        path = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[("Image files", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp"), ("All files", "*.*")],
        )
        if not path:
            path = filedialog.askdirectory(title="选择图片文件夹")
        if path:
            self._set_input(mode, Path(path))

    def _choose_stitch_input(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择多张图片",
            filetypes=[("Image files", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp"), ("All files", "*.*")],
        )
        if paths:
            self._set_input_value("stitch", "\n".join(paths), Path(paths[0]))
            return
        path = filedialog.askdirectory(title="选择图片文件夹")
        if path:
            self._set_input("stitch", Path(path))

    def _choose_split_output_for_stitch(self) -> None:
        self._show_split_output_picker()

    def _show_split_output_picker(self) -> None:
        root = self._output_root()
        dirs = [p for p in root.iterdir() if p.is_dir() and "_split_" in p.name] if root.exists() else []
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        win = tk.Toplevel(self)
        win.title("选择切图结果目录")
        win.geometry("760x620")
        win.minsize(620, 480)
        win.configure(bg=BG)
        win.transient(self)
        win.grab_set()
        win._thumb_refs = []  # type: ignore[attr-defined]

        outer, body = make_card(win, bg=CARD, border=LINE, padx=24, pady=18)
        outer.pack(fill="both", expand=True, padx=18, pady=18)
        head = tk.Frame(body, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="选择切图结果目录", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        self._small_button(head, "关闭", win.destroy, bg=PINK_SOFT).pack(side="right")
        tk.Label(body, text="点击一条切图结果即可放入组图器。", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(8, 14))

        list_wrap = tk.Frame(body, bg=CARD)
        list_wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(list_wrap, bg=CARD, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(
            list_wrap,
            orient="vertical",
            command=canvas.yview,
            bg="#F8D8E4",
            troughcolor="#FFF7FA",
            activebackground="#F2A1BD",
            relief="flat",
            bd=0,
            width=8,
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content = tk.Frame(canvas, bg=CARD)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind("<Enter>", lambda _event: win.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"), add=True))
        canvas.bind("<Leave>", lambda _event: win.unbind_all("<MouseWheel>"))

        if not dirs:
            tk.Label(content, text="还没有九宫格切图结果。请先完成一次切图。", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 11)).pack(anchor="w", pady=20)
            return

        for path in dirs[:40]:
            self._build_split_picker_row(content, path, win)

    def _build_split_picker_row(self, parent: tk.Frame, path: Path, win: tk.Toplevel) -> None:
        row_bg = "#FBFBFC"
        row = tk.Frame(parent, bg=row_bg, padx=14, pady=10, highlightthickness=1, highlightbackground=LINE, cursor="hand2")
        row.pack(fill="x", pady=(0, 8))
        thumb = self._make_output_dir_thumbnail(path)
        win._thumb_refs.append(thumb)  # type: ignore[attr-defined]
        thumb_label = tk.Label(row, image=thumb, bg=row_bg, cursor="hand2")
        thumb_label.pack(side="left", padx=(0, 12))
        meta = tk.Frame(row, bg=row_bg, cursor="hand2")
        meta.pack(side="left", fill="x", expand=True)
        info = self._describe_output_dir(path)
        title = tk.Label(meta, text=info["display"], bg=row_bg, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2")
        title.pack(anchor="w")
        detail = tk.Label(meta, text=self._middle_ellipsis(info["detail"], 56), bg=row_bg, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9), cursor="hand2")
        detail.pack(anchor="w", pady=(3, 0))
        summary = tk.Label(meta, text=self._middle_ellipsis(info["summary"], 60), bg=row_bg, fg=GREEN_TEXT, font=("Microsoft YaHei UI", 9), cursor="hand2")
        summary.pack(anchor="w", pady=(3, 0))

        def choose(_event=None, p=path) -> None:
            win.destroy()
            self._set_input("stitch", p)
            self._flash_toast("已选择切图结果目录，可以直接开始组图。")

        for widget in (row, thumb_label, meta, title, detail, summary):
            widget.bind("<Button-1>", choose)
            widget.bind("<Enter>", lambda _event, r=row, m=meta, t=thumb_label, labels=(title, detail, summary): self._paint_picker_row(r, m, t, labels, "#FFF7FA"))
            widget.bind("<Leave>", lambda _event, r=row, m=meta, t=thumb_label, labels=(title, detail, summary): self._paint_picker_row(r, m, t, labels, "#FBFBFC"))

    def _paint_picker_row(self, row: tk.Frame, meta: tk.Frame, thumb: tk.Label, labels: tuple[tk.Label, ...], color: str) -> None:
        row.configure(bg=color)
        meta.configure(bg=color)
        thumb.configure(bg=color)
        for label in labels:
            label.configure(bg=color)

    def _choose_stitch_text_plan(self) -> None:
        path = filedialog.askopenfilename(
            title="选择文案文件",
            filetypes=[
                ("文案配置", "xhs_plan.json;captions.txt;*.json;*.txt"),
                ("JSON 文件", "*.json"),
                ("文本文件", "*.txt"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.stitch_vars["xhs_plan"].set(path)
            self.stitch_vars["captions"].set("")
            if self.stitch_vars["caption_height"].get().strip() in {"", "0"}:
                self.stitch_vars["caption_height"].set("180")

    def _choose_post_avatar(self) -> None:
        path = filedialog.askopenfilename(
            title="选择头像",
            filetypes=[("图片文件", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"), ("所有文件", "*.*")],
        )
        if path:
            self.stitch_vars["post_avatar"].set(path)

    def _copy_xhs_skill_prompt(self) -> None:
        skill_path = Path("skills") / "xhs-life-philosophy-illustration"
        prompt = f"""使用项目内小红书爆款插图 Skill：
{skill_path}

请帮我生成一套小红书人生哲学/职场哲学插图素材包。

要求：
1. 先问我确认主题和主体样式。
2. 主体样式从这些里面选：
   - workplace-monk：职场和尚
   - office-cat：打工猫
   - round-office-worker：圆脸打工人
   - zen-rabbit：禅意兔子
   - tiny-robot：小机器人
3. 生成 9 条短文案切片，每条对应一格图。
4. 生成一张无字九宫格生图提示词，不要让图片模型生成中文文字。
5. 同时生成九张单独方图提示词。
6. 每次额外输出一句简短标题钩子。
7. 输出 hook.txt、tags.txt、post_copy.txt、captions.txt、xhs_plan.json、image_prompt_grid.txt、image_prompt_individual.txt。
8. xhs_plan.json 要包含 hook 和 tags，并能直接给表情包工坊组图器当“文案文件”使用。
9. post_copy.txt 要能直接复制去小红书发布。

默认建议：
主题：松弛感自救
主体：office-cat
标题钩子：瞬间被这段话点醒了~
风格：小红书治愈贴纸漫画，白底，高留白，九宫格，无字。
"""
        self.clipboard_clear()
        self.clipboard_append(prompt)
        self._flash_toast("已复制小红书爆款 Skill 提示，可直接粘贴给 Codex。")

    def _choose_input_file(self, mode: str) -> None:
        path = filedialog.askopenfilename(
            title="选择图片" if mode == "convert" else "选择拼图",
            filetypes=[("Image files", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp"), ("All files", "*.*")],
        )
        if path:
            self._set_input(mode, Path(path))

    def _choose_output_dir(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            variable.set(path)

    def _clear_current(self, mode: str) -> None:
        if mode == "outputs":
            self._refresh_output_dirs()
            return
        self._vars(mode)["input"].set("")
        self._vars(mode)["output"].set("")
        view = self._input_views.get(mode)
        if view:
            empty_text = "点击选择图片/文件夹\n或拖拽 / Ctrl+V 粘贴" if mode in {"convert", "stitch"} else "点击选择图片\n或拖拽 / Ctrl+V 粘贴"
            view["preview"].configure(image="", text=empty_text)
            hint = "支持拖拽和 Ctrl+V 粘贴" if DND_READY else "可用 Ctrl+V 粘贴，或点击选择图片"
            view["info"].configure(text=hint)
        self._preview_refs.pop(mode, None)
        idle_text = "请先选择拼图" if mode == "split" else "请先选择多张图片" if mode == "stitch" else "请先选择图片或文件夹"
        if mode in self._action_buttons:
            self._action_buttons[mode].configure(text=idle_text, state="disabled", bg="#E8E3E8", fg=TEXT_SOFT, cursor="arrow")
        if mode in self._status_labels:
            self._status_labels[mode].configure(text="完成后自动打开结果目录", fg=TEXT_SOFT)
        if mode in self._open_result_buttons:
            self._open_result_buttons[mode].pack_forget()
        self._flash_toast("已清空，可以重新导入拼图。")

    def _set_input(self, mode: str, path: Path) -> None:
        self._set_input_value(mode, str(path), path)

    def _set_input_value(self, mode: str, value: str, preview_path: Path) -> None:
        vars_map = self._vars(mode)
        vars_map["input"].set(value)
        vars_map["output"].set(str(self._suggest_output(mode, preview_path)))
        self._update_preview(mode, preview_path)
        if mode in self._action_buttons:
            text = "开始切图" if mode == "split" else "开始组图" if mode == "stitch" else "开始转换"
            self._action_buttons[mode].configure(text=text, state="normal", bg=GREEN, fg=GREEN_TEXT, cursor="hand2")
        if mode in self._status_labels:
            if mode == "split":
                rows, cols = self.split_vars["rows"].get(), self.split_vars["cols"].get()
                status = f"将按 {rows} x {cols} 切图，完成后自动打开结果目录"
            elif mode == "stitch":
                count = self._count_stitch_inputs(value)
                groups = (count + 1) // 2
                status = f"将按顺序上下拼接，预计输出 {groups} 组图片"
            else:
                status = "将批量转换为表情图片，完成后自动打开结果目录"
            self._status_labels[mode].configure(text=status, fg=TEXT_SOFT)
        if mode in self._open_result_buttons:
            self._open_result_buttons[mode].pack_forget()

    def _suggest_output(self, mode: str, path: Path) -> Path:
        stem = path.name if path.is_dir() else path.stem
        suffix = "gif" if mode == "convert" else "stitch" if mode == "stitch" else "split"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return Path(tempfile.gettempdir()) / "meme-workshop-output" / f"{stem}_{suffix}_{stamp}"

    def _count_stitch_inputs(self, value: str) -> int:
        path = Path(value) if "\n" not in value else None
        if path is not None and path.exists():
            if path.is_dir():
                return len([p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS])
            return 1
        return len([line for line in value.splitlines() if line.strip()])

    def _update_preview(self, mode: str, path: Path) -> None:
        view = self._input_views[mode]
        preview: tk.Label = view["preview"]  # type: ignore[assignment]
        info: tk.Label = view["info"]  # type: ignore[assignment]

        if path.is_dir():
            files = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]
            count = len(files)
            action = "上下组图" if mode == "stitch" else "批量转换"
            if files:
                folder_thumb = self._make_folder_input_thumbnail(files[:4], 236)
                photo = ImageTk.PhotoImage(folder_thumb)
                preview.configure(image=photo, text="")
                self._preview_refs[mode] = photo
            else:
                preview.configure(image="", text=f"文件夹\n{action}")
                self._preview_refs.pop(mode, None)
            info.configure(text=f"{path.name}，共 {count} 张支持图片")
            return

        try:
            with Image.open(path) as image:
                thumb = self._make_preview_image(image, mode)
                frames = getattr(image, "n_frames", 1)
                width, height = image.size
        except Exception as exc:
            preview.configure(image="", text="预览失败")
            info.configure(text=f"{path.name}\n{exc}")
            self._preview_refs.pop(mode, None)
            return

        photo = ImageTk.PhotoImage(thumb)
        preview.configure(image=photo, text="")
        extra = ""
        if mode == "stitch":
            extra = f"\n已选 {self._count_stitch_inputs(self.stitch_vars['input'].get())} 张，按顺序两两拼接"
        info.configure(text=f"{width} x {height} · {frames} 帧{extra}")
        self._preview_refs[mode] = photo

    def _make_folder_input_thumbnail(self, files: list[Path], size: int) -> Image.Image:
        canvas = Image.new("RGBA", (size, size), (250, 250, 252, 255))
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((1, 1, size - 2, size - 2), radius=10, fill=(250, 250, 252, 255), outline=(231, 222, 231, 255))
        gap = 8
        cell = (size - gap * 3) // 2
        boxes = [
            (gap, gap),
            (gap * 2 + cell, gap),
            (gap, gap * 2 + cell),
            (gap * 2 + cell, gap * 2 + cell),
        ]
        for file_path, (x, y) in zip(files, boxes):
            try:
                with Image.open(file_path) as image:
                    thumb = image.convert("RGBA")
                    thumb.thumbnail((cell - 8, cell - 8), Image.Resampling.LANCZOS)
            except Exception:
                continue
            draw.rounded_rectangle((x, y, x + cell, y + cell), radius=8, fill=(255, 255, 255, 255), outline=(231, 222, 231, 255))
            canvas.alpha_composite(thumb, (x + (cell - thumb.width) // 2, y + (cell - thumb.height) // 2))
        return canvas

    def _make_preview_image(self, image: Image.Image, mode: str) -> Image.Image:
        thumb = image.convert("RGBA")
        thumb.thumbnail((236, 236), Image.Resampling.LANCZOS)
        if mode != "split":
            return thumb
        rows = max(1, int(self.split_vars["rows"].get() or "3"))
        cols = max(1, int(self.split_vars["cols"].get() or "3"))
        draw = ImageDraw.Draw(thumb)
        for step in range(1, cols):
            x = round(thumb.width * step / cols)
            draw.line([(x, 0), (x, thumb.height)], fill=(245, 168, 193, 230), width=2)
        for step in range(1, rows):
            y = round(thumb.height * step / rows)
            draw.line([(0, y), (thumb.width, y)], fill=(245, 168, 193, 230), width=2)
        return thumb

    def _handle_global_paste(self, _event) -> str | None:
        if self._paste_clipboard(self._mode, False):
            return "break"
        return None

    def _paste_clipboard(self, mode: str, show_message: bool) -> bool:
        try:
            payload = ImageGrab.grabclipboard()
        except Exception as exc:
            if show_message:
                messagebox.showerror("读取剪贴板失败", str(exc))
            return False

        candidate = self._resolve_clipboard_payload(mode, payload)
        if candidate is None:
            if show_message:
                messagebox.showinfo("没有可用图片", "剪贴板里没有图片或图片路径。")
            return False

        self._set_input(mode, candidate)
        self._append_log(f"[clipboard] {mode} <- {candidate}")
        self._flash_toast("已接收剪贴板图片。")
        return True

    def _resolve_clipboard_payload(self, mode: str, payload) -> Path | None:
        if isinstance(payload, Image.Image):
            return self._save_clipboard_image(payload, mode)
        if isinstance(payload, list):
            for item in payload:
                path = Path(item)
                if not path.exists():
                    continue
                if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
                    return path
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if text:
            path = Path(text.strip('"'))
            if path.exists():
                if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
                    return path
        return None

    def _save_clipboard_image(self, image: Image.Image, mode: str) -> Path:
        temp_dir = Path(tempfile.gettempdir()) / "meme-workshop-paste"
        temp_dir.mkdir(parents=True, exist_ok=True)
        target = temp_dir / f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        image.convert("RGBA").save(target, format="PNG")
        return target

    def _set_grid_preset(self, rows: int, cols: int) -> None:
        self.split_vars["rows"].set(str(rows))
        self.split_vars["cols"].set(str(cols))
        self._refresh_grid_buttons()
        current = self.split_vars["input"].get().strip()
        if current:
            self._update_preview("split", Path(current))
        if "split" in self._status_labels:
            self._status_labels["split"].configure(text=f"将按 {rows} x {cols} 切图，完成后自动打开结果目录")
        self._flash_toast(f"已切换为 {rows} x {cols} 网格。")

    def _open_dir(self, value: str) -> None:
        if not value:
            return
        path = Path(value)
        if path.is_file():
            path = path.parent
        if not path.exists():
            messagebox.showerror("路径不存在", str(path))
            return
        os.startfile(str(path))

    def _open_last_output(self) -> None:
        if self._last_output_file and self._last_output_file.exists():
            os.startfile(str(self._last_output_file))
            return
        self._open_dir(self._current_output_var().get())

    def _clear_log(self) -> None:
        for widget in self._log_widgets:
            widget.configure(state="normal")
            widget.delete("1.0", tk.END)
            widget.configure(state="disabled")

    def _append_log(self, message: str) -> None:
        for widget in self._log_widgets:
            widget.configure(state="normal")
            widget.insert(tk.END, message.rstrip() + "\n")
            widget.see(tk.END)
            widget.configure(state="disabled")

    def _update_recent_output(self, output_dir: Path) -> None:
        candidates = [
            p for p in output_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in {".gif", ".png", ".jpg", ".jpeg"} and p.name not in {"preview_boxes.png"}
        ]
        if candidates:
            self._last_output_file = max(candidates, key=lambda p: p.stat().st_mtime)
            if hasattr(self, "_recent_label"):
                self._recent_label.configure(text=f"最近结果：{self._last_output_file.name}")
        else:
            self._last_output_file = None
            if hasattr(self, "_recent_label"):
                self._recent_label.configure(text=f"最近结果：{output_dir}")

    def _ensure_not_running(self) -> bool:
        if self._running:
            messagebox.showinfo("任务执行中", "当前已有任务在运行，请等它结束。")
            return False
        return True

    def _run_job(self, title: str, func, args: argparse.Namespace) -> None:
        if not self._ensure_not_running():
            return
        self._running = True
        self._append_log(f"== {title} ==")
        self._flash_toast(f"{title} 已开始执行……", reset=False)
        job_mode = getattr(args, "command", "")
        ui_mode = "split" if job_mode == "split-sheet" else "stitch" if job_mode == "stitch-vertical" else "convert"
        if ui_mode in self._action_buttons:
            self._action_buttons[ui_mode].configure(text="正在处理…", state="disabled", bg="#E8E3E8", fg=TEXT_SOFT, cursor="arrow")
        if ui_mode in self._status_labels:
            self._status_labels[ui_mode].configure(text="正在处理，请稍等。", fg=TEXT_SOFT)

        def worker() -> None:
            buf = io.StringIO()
            try:
                with redirect_stdout(buf), redirect_stderr(buf):
                    code = func(args)
                text = buf.getvalue()
                if text:
                    self.after(0, lambda: self._append_log(text))
                self.after(0, lambda: self._append_log(f"[exit] code={code}"))
                if code == 0:
                    out_dir = Path(args.output).expanduser().resolve()
                    self.after(0, lambda: self._update_recent_output(out_dir))
                    self.after(0, lambda: self._open_dir(str(out_dir)))
                    self.after(0, lambda m=ui_mode: self._show_success(out_dir, m))
                    if ui_mode == "stitch" and bool(self.stitch_vars["preview_enabled"].get()):
                        self.after(0, lambda: self._show_xhs_publish_preview(out_dir))
                    self.after(0, lambda: self._flash_toast(f"{title} 已完成。"))
                else:
                    self.after(0, lambda: self._flash_toast(f"{title} 已结束，返回 code={code}。"))
                    self.after(0, lambda m=ui_mode: self._restore_start_button(m, "处理失败，再试一次"))
            except Exception:
                self.after(0, lambda: self._append_log(traceback.format_exc()))
                self.after(0, lambda: self._flash_toast("任务失败，请重新选择图片。", reset=False))
                self.after(0, lambda m=ui_mode: self._restore_start_button(m, "处理失败，再试一次"))
            finally:
                self.after(0, self._clear_running)

        threading.Thread(target=worker, daemon=True).start()

    def _clear_running(self) -> None:
        self._running = False

    def _restore_start_button(self, mode: str, text: str | None = None) -> None:
        text = text or ("开始切图" if mode == "split" else "开始组图" if mode == "stitch" else "开始制作")
        if mode in self._action_buttons:
            self._action_buttons[mode].configure(text=text, state="normal", bg=GREEN, fg=GREEN_TEXT, cursor="hand2")
        if mode in self._status_labels:
            self._status_labels[mode].configure(text="完成后自动打开结果目录", fg=TEXT_SOFT)

    def _show_success(self, output_dir: Path, mode: str) -> None:
        count = len([p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() in {".gif", ".png", ".jpg", ".jpeg"} and p.name != "preview_boxes.png"])
        if mode in self._action_buttons:
            self._action_buttons[mode].configure(text="继续切图" if mode == "split" else "继续组图" if mode == "stitch" else "继续转换", state="normal", bg=GREEN, fg=GREEN_TEXT, cursor="hand2")
        if mode in self._status_labels:
            verb = "切出" if mode == "split" else "生成" if mode == "stitch" else "转换"
            self._status_labels[mode].configure(text=f"已{verb} {count} 张图片，结果目录已打开。", fg=GREEN_TEXT)
        if mode in self._open_result_buttons:
            self._open_result_buttons[mode].pack(pady=(10, 0))
        self._refresh_output_dirs()

    def _show_xhs_publish_preview(self, output_dir: Path | None = None) -> None:
        if output_dir is None:
            if not self._last_output_file:
                messagebox.showinfo("没有预览内容", "请先完成一次组图。")
                return
            output_dir = self._last_output_file.parent
        image_paths = [
            path for path in sorted(output_dir.iterdir())
            if path.is_file()
            and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            and path.name not in {"preview_boxes.png"}
            and not path.name.startswith("sheet_report")
        ]
        if not image_paths:
            messagebox.showinfo("没有预览内容", "结果目录里没有可预览的图片。")
            return

        win = tk.Toplevel(self)
        win.title("小红书发布预览")
        win.geometry("1180x760")
        win.minsize(980, 640)
        win.configure(bg="#EFEFEF")
        win._image_refs = []  # type: ignore[attr-defined]

        shell = tk.Frame(win, bg=CARD)
        shell.pack(fill="both", expand=True, padx=18, pady=16)
        shell.grid_columnconfigure(0, weight=3)
        shell.grid_columnconfigure(1, weight=2)
        shell.grid_rowconfigure(0, weight=1)

        image_host = tk.Frame(shell, bg="#F8F8F8", highlightthickness=1, highlightbackground=LINE)
        image_host.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        image_host.grid_rowconfigure(0, weight=1)
        image_host.grid_columnconfigure(0, weight=1)
        image_area = tk.Frame(image_host, bg="#F8F8F8")
        image_area.grid(row=0, column=0, sticky="nsew")
        image_area.grid_rowconfigure(0, weight=1)
        image_area.grid_columnconfigure(1, weight=1)

        image_label = tk.Label(image_area, bg="#F8F8F8")
        image_label.grid(row=0, column=1, sticky="nsew", padx=8, pady=12)
        prev_btn = tk.Button(image_area, text="‹", bg="#EFEFEF", fg=TEXT, activebackground=PINK_SOFT, relief="flat", bd=0, width=3, font=("Microsoft YaHei UI", 22, "bold"), cursor="hand2")
        next_btn = tk.Button(image_area, text="›", bg="#EFEFEF", fg=TEXT, activebackground=PINK_SOFT, relief="flat", bd=0, width=3, font=("Microsoft YaHei UI", 22, "bold"), cursor="hand2")
        prev_btn.grid(row=0, column=0, sticky="ns", padx=(10, 0), pady=260)
        next_btn.grid(row=0, column=2, sticky="ns", padx=(0, 10), pady=260)
        pager = tk.Label(image_host, bg="#F8F8F8", fg=TEXT_SOFT, font=("Microsoft YaHei UI", 10, "bold"))
        pager.grid(row=1, column=0, pady=(0, 10))

        prepared_images: list[ImageTk.PhotoImage] = []
        max_w = 640
        max_h = 660
        for path in image_paths:
            try:
                with Image.open(path) as image:
                    frame = image.convert("RGBA")
                    frame.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(frame)
            except Exception:
                continue
            prepared_images.append(photo)
        win._image_refs = prepared_images  # type: ignore[attr-defined]
        current_index = tk.IntVar(value=0)

        def render_page() -> None:
            if not prepared_images:
                image_label.configure(image="", text="图片加载失败", fg=TEXT_SOFT, font=("Microsoft YaHei UI", 14))
                pager.configure(text="")
                return
            index = current_index.get()
            image_label.configure(image=prepared_images[index], text="")
            dots = " ".join("●" if i == index else "○" for i in range(len(prepared_images)))
            pager.configure(text=f"{index + 1}/{len(prepared_images)}   {dots}")
            prev_btn.configure(state="normal" if index > 0 else "disabled")
            next_btn.configure(state="normal" if index < len(prepared_images) - 1 else "disabled")

        def jump_page(delta: int) -> None:
            if not prepared_images:
                return
            target = min(max(0, current_index.get() + delta), len(prepared_images) - 1)
            current_index.set(target)
            render_page()

        prev_btn.configure(command=lambda: jump_page(-1))
        next_btn.configure(command=lambda: jump_page(1))
        win.bind("<Left>", lambda _event: jump_page(-1))
        win.bind("<Right>", lambda _event: jump_page(1))
        win.bind("<MouseWheel>", lambda event: jump_page(1 if event.delta < 0 else -1))
        render_page()

        side = tk.Frame(shell, bg=CARD)
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_rowconfigure(1, weight=1)
        self._build_xhs_post_info(side).grid(row=0, column=0, sticky="ew")
        self._build_xhs_comments(side).grid(row=1, column=0, sticky="nsew", pady=(14, 0))

    def _build_xhs_post_info(self, parent: tk.Frame) -> tk.Frame:
        card = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE, padx=16, pady=14)
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x")

        avatar = self._make_avatar_image(self.stitch_vars["post_avatar"].get().strip(), 44)
        card._avatar_ref = ImageTk.PhotoImage(avatar)  # type: ignore[attr-defined]
        tk.Label(head, image=card._avatar_ref, bg=CARD).pack(side="left")  # type: ignore[attr-defined]
        tk.Label(head, text=self.stitch_vars["post_author"].get().strip() or "小红书作者", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(side="left", padx=(10, 0))
        tk.Button(head, text="关注", bg="#FF2E55", fg="white", activebackground="#FF2E55", activeforeground="white", relief="flat", bd=0, padx=24, pady=7, font=("Microsoft YaHei UI", 11, "bold")).pack(side="right")

        title = self.stitch_vars["post_title"].get().strip() or "今天也要好好照顾自己"
        content = self.stitch_vars["post_content"].get().strip()
        tags = self.stitch_vars["post_tags"].get().strip()
        tk.Label(card, text=title, bg=CARD, fg="#222222", anchor="w", justify="left", wraplength=360, font=("Microsoft YaHei UI", 13, "bold")).pack(fill="x", pady=(14, 4))
        if content:
            tk.Label(card, text=content, bg=CARD, fg="#333333", anchor="w", justify="left", wraplength=360, font=("Microsoft YaHei UI", 10)).pack(fill="x", pady=(0, 8))
        if tags:
            tk.Label(card, text=tags, bg=CARD, fg="#1F4E8C", anchor="w", justify="left", wraplength=360, font=("Microsoft YaHei UI", 10)).pack(fill="x")
        tk.Label(card, text=time.strftime("%m-%d") + " 广东", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(12, 0))
        return card

    def _build_xhs_comments(self, parent: tk.Frame) -> tk.Frame:
        card = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        title = tk.Label(card, text="共 203 条评论", bg=CARD, fg=TEXT_SOFT, anchor="w", font=("Microsoft YaHei UI", 10))
        title.pack(fill="x", padx=16, pady=(12, 8))

        comments = [
            ("Tbmhbe~", "其实不结婚生子能免除极大部分痛苦和艰辛", "7天前 福建", "♡ 21   💬 5"),
            ("派小星", "真相了！", "7天前 福建", "♡ 1   回复"),
            ("橘子去皮", "独立人格之后，就知道有多爽了。", "5小时前 广东", "♡ 赞   回复"),
            ("咬咬咬", "你看到的成功同学，大多都是踩在父母肩膀上的。如果让他们来体验你的剧本，那未必能坚持到如今的地步。你当下的课题，是你自己的修行。", "刚刚", "♡ 3187   ☆ 1607   💬 203"),
        ]
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="both", expand=True, padx=16)
        for name, text, meta, action in comments:
            item = tk.Frame(body, bg=CARD)
            item.pack(fill="x", pady=(0, 14))
            self._comment_avatar(item).pack(side="left", anchor="n", padx=(0, 10))
            content = tk.Frame(item, bg=CARD)
            content.pack(side="left", fill="x", expand=True)
            tk.Label(content, text=name, bg=CARD, fg=TEXT_SOFT, anchor="w", font=("Microsoft YaHei UI", 9)).pack(fill="x")
            tk.Label(content, text=text, bg=CARD, fg="#333333", anchor="w", justify="left", wraplength=340, font=("Microsoft YaHei UI", 10)).pack(fill="x", pady=(2, 4))
            tk.Label(content, text=f"{meta}    {action}", bg=CARD, fg=TEXT_SOFT, anchor="w", font=("Microsoft YaHei UI", 9)).pack(fill="x")

        bottom = tk.Frame(card, bg=CARD, padx=12, pady=10)
        bottom.pack(fill="x", side="bottom")
        tk.Label(bottom, text="说点什么...", bg="#F5F5F5", fg=TEXT_SOFT, anchor="w", padx=16, pady=8, font=("Microsoft YaHei UI", 10)).pack(side="left", fill="x", expand=True)
        tk.Label(bottom, text="♡ 3187   ☆ 1607   💬 203   ↗", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 10)).pack(side="right", padx=(10, 0))
        return card

    def _make_avatar_image(self, path_value: str, size: int) -> Image.Image:
        if path_value and Path(path_value).is_file():
            try:
                with Image.open(path_value) as image:
                    avatar = image.convert("RGBA")
                    avatar.thumbnail((size, size), Image.Resampling.LANCZOS)
                    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
                    canvas.alpha_composite(avatar, ((size - avatar.width) // 2, (size - avatar.height) // 2))
                    return canvas
            except Exception:
                pass
        image = Image.new("RGBA", (size, size), (238, 249, 240, 255))
        draw = ImageDraw.Draw(image)
        draw.ellipse((2, 2, size - 3, size - 3), fill=(238, 249, 240, 255), outline=(190, 230, 200, 255), width=2)
        draw.ellipse((size * 0.32, size * 0.22, size * 0.68, size * 0.58), fill=(255, 255, 255, 255), outline=(80, 80, 80, 255), width=2)
        draw.rounded_rectangle((size * 0.28, size * 0.54, size * 0.72, size * 0.86), radius=8, fill=(255, 168, 80, 255), outline=(80, 80, 80, 255), width=2)
        return image

    def _comment_avatar(self, parent: tk.Frame) -> tk.Label:
        size = 34
        image = Image.new("RGBA", (size, size), (245, 245, 245, 255))
        draw = ImageDraw.Draw(image)
        draw.ellipse((1, 1, size - 2, size - 2), fill=(238, 242, 255, 255), outline=(220, 220, 230, 255))
        draw.ellipse((11, 8, 23, 20), fill=(150, 160, 180, 255))
        draw.rounded_rectangle((8, 20, 26, 30), radius=5, fill=(120, 130, 160, 255))
        photo = ImageTk.PhotoImage(image)
        label = tk.Label(parent, image=photo, bg=CARD)
        label.image = photo  # type: ignore[attr-defined]
        return label

    def start_convert(self) -> None:
        try:
            max_bytes_raw = self.convert_vars["max_bytes"].get().strip()
            args = argparse.Namespace(
                input=self.convert_vars["input"].get().strip(),
                output=self.convert_vars["output"].get().strip(),
                size=parse_size(self.convert_vars["size"].get().strip()),
                format=self.convert_vars["format"].get().strip(),
                mode=int(self.convert_vars["mode"].get().strip()),
                max_bytes=parse_max_bytes(max_bytes_raw) if max_bytes_raw else None,
                keep_gif=bool(self.convert_vars["keep_gif"].get()),
                dedupe=bool(self.convert_vars["dedupe"].get()),
                wechat_safe=bool(self.convert_vars["wechat_safe"].get()),
                transparent_bg=bool(self.convert_vars["transparent_bg"].get()),
                manifest_csv=bool(self.convert_vars["manifest_csv"].get()),
                dry_run=False,
                command="convert",
            )
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if not args.input:
            messagebox.showerror("参数错误", "请先选择图片。")
            return
        if not args.output:
            args.output = str(self._suggest_output("convert", Path(args.input)))
        self._run_job("批量转换", run_convert, args)

    def start_split(self) -> None:
        try:
            args = argparse.Namespace(
                input=self.split_vars["input"].get().strip(),
                output=self.split_vars["output"].get().strip(),
                rows=int(self.split_vars["rows"].get().strip()),
                cols=int(self.split_vars["cols"].get().strip()),
                size=parse_size(self.split_vars["size"].get().strip()),
                format=self.split_vars["format"].get().strip(),
                mode=int(self.split_vars["mode"].get().strip()),
                search_ratio=float(self.split_vars["search_ratio"].get().strip()),
                trim_pad=int(self.split_vars["trim_pad"].get().strip()),
                bg_tolerance=int(self.split_vars["bg_tolerance"].get().strip()),
                white_threshold=int(self.split_vars["white_threshold"].get().strip()),
                transparent_bg=bool(self.split_vars["transparent_bg"].get()),
                command="split-sheet",
            )
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if not args.input:
            messagebox.showerror("参数错误", "请先选择拼图。")
            return
        if not args.output:
            args.output = str(self._suggest_output("split", Path(args.input)))
        if args.rows <= 0 or args.cols <= 0:
            messagebox.showerror("参数错误", "行数和列数必须大于 0。")
            return
        self._run_job("拼图切图", run_split_sheet, args)

    def start_stitch(self) -> None:
        try:
            text_plan = self.stitch_vars["xhs_plan"].get().strip()
            plan_suffix = Path(text_plan).suffix.lower() if text_plan else ""
            if text_plan and plan_suffix not in {".json", ".txt"}:
                raise ValueError("文案文件只支持 xhs_plan.json 或 captions.txt；不需要字幕时请留空。")
            xhs_plan = text_plan if plan_suffix == ".json" else None
            captions = text_plan if text_plan and plan_suffix != ".json" else None
            caption_height = int(self.stitch_vars["caption_height"].get().strip() or "0")
            if text_plan and caption_height <= 0:
                caption_height = 180
            args = argparse.Namespace(
                input=self.stitch_vars["input"].get().strip(),
                output=self.stitch_vars["output"].get().strip(),
                template="xhs",
                cell_size=int(self.stitch_vars["cell_size"].get().strip()),
                gutter=int(self.stitch_vars["gutter"].get().strip()),
                bg="#ffffff",
                format=self.stitch_vars["format"].get().strip(),
                trim=True,
                captions=captions,
                xhs_plan=xhs_plan,
                caption_height=caption_height,
                caption_font="auto",
                caption_font_size=int(self.stitch_vars["caption_font_size"].get().strip() or "64"),
                caption_min_font_size=28,
                caption_margin_x=80,
                caption_color="#000000",
                command="stitch-vertical",
            )
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if not args.input:
            messagebox.showerror("参数错误", "请先选择多张图片或图片文件夹。")
            return
        if args.cell_size <= 0 or args.gutter < 0:
            messagebox.showerror("参数错误", "输出宽度必须大于 0，上下间隔不能小于 0。")
            return
        if not args.output:
            first = Path(args.input.splitlines()[0]) if "\n" in args.input else Path(args.input)
            args.output = str(self._suggest_output("stitch", first))
        self._run_job("小红书组图", run_stitch_vertical, args)


def main() -> None:
    app = MemeGui()
    app.mainloop()


if __name__ == "__main__":
    main()
