from __future__ import annotations

import argparse
import io
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
from .cli import SUPPORTED_EXTS, parse_max_bytes, parse_size, run_convert, run_split_sheet

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


class MemeGui(BaseTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("九宫格切图器")
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
        self._nav_buttons["convert"] = self._sidebar_button(nav, "单图制作", lambda: self._switch_mode("convert"), active=False)
        self._nav_buttons["outputs"] = self._sidebar_button(nav, "输出目录", lambda: self._switch_mode("outputs"), active=False)
        self._nav_buttons["clear"] = self._sidebar_button(nav, "清空当前", lambda: self._clear_current(self._mode), active=False)
        self._nav_buttons["split"].pack(fill="x", pady=(0, 10))
        self._nav_buttons["convert"].pack(fill="x", pady=(0, 10))
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
            "outputs": self._build_outputs_page(),
        }

    def _build_convert_page(self) -> tk.Frame:
        page = tk.Frame(self.page_host, bg=BG)
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=1)
        self._build_drop_zone(
            page,
            mode="convert",
            title="放入单图",
            subtitle="拖入图片或粘贴图片",
            allow_dir=False,
            browse_text="选择图片",
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
        preview = tk.Label(preview_shell, bg="#FAFAFC", fg=TEXT_SOFT, text="点击选择图片\n或拖拽 / Ctrl+V 粘贴", justify="center", font=("Microsoft YaHei UI", 12))
        preview.pack(fill="both", expand=True)

        hint = "支持拖拽和 Ctrl+V 粘贴" if DND_READY else "可用 Ctrl+V 粘贴，或点击选择图片"
        info = tk.Label(wrap, bg=CARD, fg=TEXT_SOFT, text=hint, justify="center", font=("Microsoft YaHei UI", 9), wraplength=520)
        info.grid(row=2, column=0, pady=(8, 0))

        action = tk.Frame(wrap, bg=CARD)
        action.grid(row=3, column=0, pady=(10, 0))
        self._small_button(action, browse_text, lambda: self._choose_input_file(mode), bg=PINK_SOFT).pack()

        self._input_views[mode] = {"preview": preview, "info": info}
        preview_shell.bind("<Button-1>", lambda _event: self._choose_input_file(mode))
        preview.bind("<Button-1>", lambda _event: self._choose_input_file(mode))
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

        tk.Label(body, text="调色模式", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self._chip_group(body, self.convert_vars["mode"], [("模式 1", "1"), ("模式 2", "2")]).pack(anchor="w", pady=(8, 12))

        for text, variable in [
            ("背景转透明", self.convert_vars["transparent_bg"]),
            ("微信稳妥模式", self.convert_vars["wechat_safe"]),
        ]:
            tk.Checkbutton(
                body,
                text=text,
                variable=variable,
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
        self._small_button(grid_row, "3 x 3", lambda: self._set_grid_preset(3, 3), bg=PINK_SOFT).pack(side="left", padx=(0, 8))
        self._small_button(grid_row, "4 x 4", lambda: self._set_grid_preset(4, 4), bg=SIDEBAR_BG).pack(side="left", padx=(0, 8))
        self._small_button(grid_row, "5 x 5", lambda: self._set_grid_preset(5, 5), bg=SIDEBAR_BG).pack(side="left")

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
        self._small_button(head, "刷新", self._refresh_output_dirs, bg=PINK_SOFT).pack(side="right")
        tk.Label(body, text="这里会列出最近自动生成的结果目录，点击即可打开。", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(8, 16))
        list_wrap = tk.Frame(body, bg=CARD)
        list_wrap.pack(fill="both", expand=True)
        self._output_canvas = tk.Canvas(list_wrap, bg=CARD, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(list_wrap, orient="vertical", command=self._output_canvas.yview)
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
        return Path(tempfile.gettempdir()) / "meme-cli-output"

    def _on_output_mousewheel(self, event) -> None:
        if self._output_canvas is not None and getattr(event, "delta", 0):
            self._output_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _refresh_output_dirs(self) -> None:
        if self._output_list_body is None:
            return
        for child in self._output_list_body.winfo_children():
            child.destroy()
        root = self._output_root()
        dirs = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not dirs:
            tk.Label(self._output_list_body, text="还没有输出结果。完成一次切图或制作后会出现在这里。", bg=CARD, fg=TEXT_SOFT, font=("Microsoft YaHei UI", 10)).pack(anchor="w")
            return
        for path in dirs[:30]:
            row = tk.Frame(self._output_list_body, bg="#FBFBFC", padx=14, pady=10, highlightthickness=1, highlightbackground=LINE)
            row.pack(fill="x", pady=(0, 8))
            meta = tk.Frame(row, bg="#FBFBFC")
            meta.pack(side="left", fill="x", expand=True)
            info = self._describe_output_dir(path)
            tk.Label(meta, text=info["display"], bg="#FBFBFC", fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
            tk.Label(meta, text=info["detail"], bg="#FBFBFC", fg=TEXT_SOFT, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(3, 0))
            actions = tk.Frame(row, bg="#FBFBFC")
            actions.pack(side="right")
            self._small_button(actions, "打开", lambda p=path: self._open_dir(str(p)), bg=MINT).pack(side="left", padx=(0, 8))
            self._small_button(actions, "删除", lambda p=path: self._delete_output_dir(p), bg=PINK_SOFT).pack(side="left")

    def _describe_output_dir(self, path: Path) -> dict[str, str]:
        name = path.name
        count = len([p for p in path.iterdir() if p.is_file() and p.suffix.lower() in {".gif", ".png"} and p.name != "preview_boxes.png"])
        kind = "切图" if "_split_" in name else "制作" if "_gif_" in name or "_convert_" in name else "输出"
        stamp_match = re.search(r"(20\d{6}-\d{6})", name)
        if stamp_match:
            raw = stamp_match.group(1)
            nice_time = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[9:11]}:{raw[11:13]}"
        else:
            nice_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        token = self._extract_output_token(name, kind)
        display = f"{kind}-{nice_time}-{count}张-{token}"
        return {
            "display": display,
            "detail": path.name,
        }

    def _extract_output_token(self, name: str, kind: str) -> str:
        marker = "_split_" if kind == "切图" else "_gif_" if "_gif_" in name else "_convert_"
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

    def _sidebar_button(self, parent: tk.Misc, text: str, command, *, active: bool = True) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=PINK if active else SIDEBAR_BG,
            fg=TEXT,
            activebackground=PINK if active else PINK_SOFT,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            padx=16,
            pady=9,
            anchor="center",
            font=("Microsoft YaHei UI", 11, "bold"),
            cursor="hand2",
        )
        self._style_sidebar_bubble(button, active)
        button.bind("<Enter>", lambda _event, b=button: self._hover_sidebar_bubble(b, True))
        button.bind("<Leave>", lambda _event, b=button: self._hover_sidebar_bubble(b, False))
        return button

    def _style_sidebar_bubble(self, button: tk.Button, active: bool) -> None:
        button.configure(
            bg=PINK if active else CARD,
            activebackground=PINK if active else PINK_SOFT,
            highlightthickness=2,
            highlightbackground=PINK if active else LINE,
            highlightcolor=PINK,
        )

    def _hover_sidebar_bubble(self, button: tk.Button, hovering: bool) -> None:
        active = button.cget("bg") == PINK
        if active:
            return
        button.configure(bg=PINK_SOFT if hovering else CARD, highlightbackground=PINK if hovering else LINE)

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
        return self.convert_vars["output"] if self._mode == "convert" else self.split_vars["output"]

    def _vars(self, mode: str) -> dict[str, tk.Variable]:
        return self.convert_vars if mode == "convert" else self.split_vars

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
            self._style_sidebar_bubble(button, key == mode)
        if mode == "split":
            self._hero_title.configure(text="九宫格切图器")
            self._hero_sub.configure(text="拖入拼图或粘贴图片，选宫格，点开始。结果会自动打开。")
            self._set_toast("准备好了，选择或粘贴一张拼图即可开始。")
        elif mode == "convert":
            self._hero_title.configure(text="单图制作器")
            self._hero_sub.configure(text="把单张图片统一转成 GIF / PNG，可自动背景转透明。")
            self._set_toast("选择或粘贴一张图片即可开始制作。")
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
        else:
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
            view["preview"].configure(image="", text="点击选择图片\n或拖拽 / Ctrl+V 粘贴")
            hint = "支持拖拽和 Ctrl+V 粘贴" if DND_READY else "可用 Ctrl+V 粘贴，或点击选择图片"
            view["info"].configure(text=hint)
        self._preview_refs.pop(mode, None)
        idle_text = "请先选择拼图" if mode == "split" else "请先选择图片"
        if mode in self._action_buttons:
            self._action_buttons[mode].configure(text=idle_text, state="disabled", bg="#E8E3E8", fg=TEXT_SOFT, cursor="arrow")
        if mode in self._status_labels:
            self._status_labels[mode].configure(text="完成后自动打开结果目录", fg=TEXT_SOFT)
        if mode in self._open_result_buttons:
            self._open_result_buttons[mode].pack_forget()
        self._flash_toast("已清空，可以重新导入拼图。")

    def _set_input(self, mode: str, path: Path) -> None:
        vars_map = self._vars(mode)
        vars_map["input"].set(str(path))
        vars_map["output"].set(str(self._suggest_output(mode, path)))
        self._update_preview(mode, path)
        if mode in self._action_buttons:
            text = "开始切图" if mode == "split" else "开始制作"
            self._action_buttons[mode].configure(text=text, state="normal", bg=GREEN, fg=GREEN_TEXT, cursor="hand2")
        if mode in self._status_labels:
            if mode == "split":
                rows, cols = self.split_vars["rows"].get(), self.split_vars["cols"].get()
                status = f"将按 {rows} x {cols} 切图，完成后自动打开结果目录"
            else:
                status = "将转换为表情图片，完成后自动打开结果目录"
            self._status_labels[mode].configure(text=status, fg=TEXT_SOFT)
        if mode in self._open_result_buttons:
            self._open_result_buttons[mode].pack_forget()

    def _suggest_output(self, mode: str, path: Path) -> Path:
        stem = path.name if path.is_dir() else path.stem
        suffix = "gif" if mode == "convert" else "split"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return Path(tempfile.gettempdir()) / "meme-cli-output" / f"{stem}_{suffix}_{stamp}"

    def _update_preview(self, mode: str, path: Path) -> None:
        view = self._input_views[mode]
        preview: tk.Label = view["preview"]  # type: ignore[assignment]
        info: tk.Label = view["info"]  # type: ignore[assignment]

        if path.is_dir():
            count = len([p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS])
            preview.configure(image="", text="文件夹")
            info.configure(text=f"{path.name}，共 {count} 张支持图片")
            self._preview_refs.pop(mode, None)
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
        info.configure(text=f"{width} x {height} · {frames} 帧")
        self._preview_refs[mode] = photo

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
        temp_dir = Path(tempfile.gettempdir()) / "meme-cli-paste"
        temp_dir.mkdir(parents=True, exist_ok=True)
        target = temp_dir / f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        image.convert("RGBA").save(target, format="PNG")
        return target

    def _set_grid_preset(self, rows: int, cols: int) -> None:
        self.split_vars["rows"].set(str(rows))
        self.split_vars["cols"].set(str(cols))
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
            if p.is_file() and p.suffix.lower() in {".gif", ".png"} and p.name not in {"preview_boxes.png"}
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
        ui_mode = "split" if job_mode == "split-sheet" else "convert"
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
        text = text or ("开始切图" if mode == "split" else "开始制作")
        if mode in self._action_buttons:
            self._action_buttons[mode].configure(text=text, state="normal", bg=GREEN, fg=GREEN_TEXT, cursor="hand2")
        if mode in self._status_labels:
            self._status_labels[mode].configure(text="完成后自动打开结果目录", fg=TEXT_SOFT)

    def _show_success(self, output_dir: Path, mode: str) -> None:
        count = len([p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() in {".gif", ".png"} and p.name != "preview_boxes.png"])
        if mode in self._action_buttons:
            self._action_buttons[mode].configure(text="继续切图" if mode == "split" else "继续制作", state="normal", bg=GREEN, fg=GREEN_TEXT, cursor="hand2")
        if mode in self._status_labels:
            verb = "切出" if mode == "split" else "生成"
            self._status_labels[mode].configure(text=f"已{verb} {count} 张图片，结果目录已打开。", fg=GREEN_TEXT)
        if mode in self._open_result_buttons:
            self._open_result_buttons[mode].pack(pady=(10, 0))
        self._refresh_output_dirs()

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


def main() -> None:
    app = MemeGui()
    app.mainloop()


if __name__ == "__main__":
    main()
