#!/usr/bin/env python3
"""
HL2 Addon Organizer

A window for Half-Life 2's Workshop addons, in the spirit of Mod Organizer:
see which addons conflict, set rules for which one should win, sort and edit
the load order, keep profiles, and save it back to the game's workshop.txt.

    python addon_organizer.py

Needs addon_checker.py in the same folder. Python 3.7+ with tkinter (included
with the python.org installer on Windows).
"""

import heapq
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections import Counter, defaultdict
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addon_checker as ac  # noqa: E402

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "addon_rules.json")
WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id=%s"

THEMES = {
    "dark": {
        "bg": "#1e1f22", "panel": "#2b2d31", "field": "#232428", "fg": "#e3e5e8", "muted": "#9a9ca3",
        "border": "#3b3d44", "accent": "#4c82f7", "accent_fg": "#ffffff", "accent_hover": "#6596ff",
        "select": "#35508f", "select_fg": "#ffffff", "heading": "#313338", "stripe": "#26272b",
        "win": "#1d4430", "lose": "#55262b", "both": "#4f421b",
        "good": "#5fd38d", "bad": "#ff7b72", "warn": "#e3b341", "dim": "#80838b", "fixed": "#a5b0ff",
        "broken_bg": "#55262b", "mostly_bg": "#3b3418", "button": "#383a40", "button_hover": "#43454c",
    },
    "light": {
        "bg": "#f2f3f5", "panel": "#ffffff", "field": "#ffffff", "fg": "#1f2328", "muted": "#656d76",
        "border": "#d0d7de", "accent": "#2f6feb", "accent_fg": "#ffffff", "accent_hover": "#4b83f0",
        "select": "#c8dbff", "select_fg": "#1f2328", "heading": "#eaeef2", "stripe": "#f6f8fa",
        "win": "#d3f5dc", "lose": "#ffe0dd", "both": "#fff1b8",
        "good": "#1a7f37", "bad": "#cf222e", "warn": "#9a6700", "dim": "#8c959f", "fixed": "#5a5fc6",
        "broken_bg": "#ffe0dd", "mostly_bg": "#fff6cf", "button": "#f6f8fa", "button_hover": "#eaeef2",
    },
}

# Columns of the load order list: (id, heading, width at 96 dpi, anchor, stretches, optional)
COLUMNS = [
    ("on", "", 30, "center", False, False),
    ("pos", "#", 44, "e", False, False),
    ("name", "Addon", 250, "w", True, False),
    ("flags", "Flags", 52, "center", False, False),
    ("type", "Type", 112, "w", False, True),
    ("files", "Files", 58, "e", False, True),
    ("size", "Size", 74, "e", False, True),
    ("wins", "Wins", 58, "e", False, True),
    ("loses", "Loses", 62, "e", False, True),
    ("status", "Status", 190, "w", False, False),
    ("notes", "Notes", 160, "w", False, True),
]
DEFAULT_HIDDEN = ["size", "notes"]

VIEWS = ["All addons", "Has conflicts", "Overwrites others", "Is overwritten", "Does nothing",
         "Mostly does nothing", "Disabled", "Rule broken", "Not in load order", "Has notes"]

# Rough content type from where an addon's files go.
CATEGORIES = [("maps/", "Map"), ("sound/", "Sound"), ("models/", "Model"), ("materials/", "Texture"),
              ("scripts/", "Script"), ("resource/", "UI"), ("cfg/", "Config"), ("particles/", "Effect"),
              ("scenes/", "Scene"), ("media/", "Video")]


def stable_toposort(ids, edges, from_end=False):
    """Orders ids so every (a, b) edge has a before b, otherwise keeping the given order.
    Returns a shorter list if the edges form a cycle."""
    if from_end:
        ids = ids[::-1]
        edges = [(b, a) for a, b in edges]
    index = {w: n for n, w in enumerate(ids)}
    after, incoming = defaultdict(set), defaultdict(int)
    for a, b in set(edges):
        after[a].add(b)
        incoming[b] += 1
    ready = [index[w] for w in ids if not incoming[w]]
    heapq.heapify(ready)
    out = []
    while ready:
        w = ids[heapq.heappop(ready)]
        out.append(w)
        for b in after[w]:
            incoming[b] -= 1
            if not incoming[b]:
                heapq.heappush(ready, index[b])
    return out[::-1] if from_end else out


def addon_type(addon):
    counts = Counter()
    for path in addon.files:
        for prefix, label in CATEGORIES:
            if path.startswith(prefix):
                counts[label] += 1
                break
        else:
            counts["Other"] += 1
    if not counts:
        return ""
    total = sum(counts.values())
    picks = [label for label, n in counts.most_common() if n * 5 >= total][:2]
    if "Map" in counts and "Map" not in picks:
        picks = ["Map"] + picks[:1]  # a map is the point of the addon even if it ships many textures
    return " + ".join(picks)


def addon_size(addon):
    return sum(size for size, _ in addon.files.values())


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opens Explorer
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Tooltip:
    def __init__(self, widget, text, theme):
        self.widget, self.text, self.theme = widget, text, theme
        self.tip = self.job = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _event=None):
        self.hide()
        self.job = self.widget.after(500, self.show)

    def show(self):
        colors = THEMES[self.theme()]
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self.tip, text=self.text, justify="left", background=colors["heading"], foreground=colors["fg"],
                 relief="solid", borderwidth=1, padx=8, pady=4, wraplength=360).pack()

    def hide(self, _event=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Organizer:
    def __init__(self, root):
        self.root = root
        root.title("HL2 Addon Organizer")
        root.minsize(1000, 560)

        self.addons = []          # every installed addon with content
        self.by_id = {}           # workshop id -> Addon
        self.order = []           # [[id, enabled]] as in workshop.txt, including uninstalled ids
        self.saved_order = []
        self.history, self.future = [], []  # undo / redo snapshots of self.order
        self.load_order_path = None
        self.containers = []
        self.game_dir = None      # chosen in the Folders window; None = find automatically
        self.workshop_dir = None
        self.found_game = self.found_workshop = None  # what the last scan used
        self.base = {}            # the original game's files: path -> (size, crc)
        self.base_info = ""
        # Stands in for the original game in the Conflicts tab.
        self.base_addon = ac.Addon(name="Half-Life 2 (original game files)", location="(base game)")
        self.leftovers = []
        self.rules = []           # [{"winner": id, "loser": id}]
        self.notes = {}           # addon key -> text
        self.profiles = {}        # name -> [[id, enabled]]
        self.settings = {}
        self.types, self.sizes = {}, {}
        self.last_wins = tk.BooleanVar(value=False)
        self.filter_text = tk.StringVar()
        self.view = tk.StringVar(value=VIEWS[0])
        self.find_text = tk.StringVar()
        self.note_text = tk.StringVar()
        self.theme_name = tk.StringVar(value="dark")
        self.sort = ("pos", False)
        self.find_rows = {}       # result row id -> Addon, for jumping to it
        self.selected = None      # Addon shown on the right
        self.selected_rel = {}
        self.row_tags = {}        # tree row -> (foreground tag, background tag)
        self.backup_done = False
        self.scanning = False
        self.removable = set()
        self.idle = {}
        self.by_path = {}
        self.stats = {}
        self.drag = None
        self.drag_moved = False

        # The scan runs in a background thread; it hands window updates to the main thread
        # through this queue instead of touching tkinter itself.
        self.ui_queue = queue.Queue()
        self.load_settings()
        self.setup_fonts()
        self.style = ttk.Style(root)
        self.build_menu()
        self.build_ui()
        self.rebuild_profiles_menu()
        self.apply_theme()
        self.restore_layout()
        self.bind_keys()
        self.poll_ui()
        self.filter_text.trace_add("write", lambda *_: self.refresh_list())
        self.view.trace_add("write", lambda *_: self.refresh_list())
        self.last_wins.trace_add("write", lambda *_: self.on_direction_changed())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_scan()

    # ------------------------------------------------------------------ settings

    def load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.settings = data
        self.rules = [r for r in data.get("rules", []) if r.get("winner") and r.get("loser")]
        self.last_wins.set(bool(data.get("last_wins", False)))
        self.game_dir = data.get("game_dir") or None
        self.workshop_dir = data.get("workshop_dir") or None
        self.notes = dict(data.get("notes", {}))
        self.profiles = dict(data.get("profiles", {}))
        self.theme_name.set(data.get("theme", "dark") if data.get("theme") in THEMES else "dark")
        self.hidden_columns = list(data.get("hidden_columns", DEFAULT_HIDDEN))

    def save_settings(self):
        self.settings.update({
            "game_dir": self.game_dir, "workshop_dir": self.workshop_dir, "last_wins": self.last_wins.get(),
            "theme": self.theme_name.get(), "hidden_columns": self.hidden_columns,
            "rules": self.rules, "notes": self.notes, "profiles": self.profiles,
        })
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except OSError as e:
            messagebox.showerror("Couldn't save settings", "%s\n%s" % (SETTINGS_FILE, e))

    # ------------------------------------------------------------------ look

    def px(self, n):
        """Pixels at 96 dpi -> pixels on this screen."""
        return int(n * self.scale)

    def setup_fonts(self):
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        family = "Segoe UI" if sys.platform == "win32" else None
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            f = tkfont.nametofont(name)
            if family:
                f.configure(family=family)
            f.configure(size=10)
        base = tkfont.nametofont("TkDefaultFont")
        self.font_bold = base.copy()
        self.font_bold.configure(weight="bold")
        self.font_title = base.copy()
        self.font_title.configure(size=15, weight="bold")
        self.font_big = base.copy()
        self.font_big.configure(size=20, weight="bold")
        self.font_small = base.copy()
        self.font_small.configure(size=9)
        self.row_height = base.metrics("linespace") + self.px(9)

    def colors(self):
        return THEMES[self.theme_name.get()]

    def apply_theme(self):
        c = self.colors()
        s = self.style
        s.theme_use("clam")
        s.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
                    troughcolor=c["bg"], focuscolor=c["accent"], selectbackground=c["select"],
                    selectforeground=c["select_fg"], insertcolor=c["fg"], arrowcolor=c["muted"])
        s.configure("TFrame", background=c["bg"])
        s.configure("Card.TFrame", background=c["panel"])
        s.configure("TLabel", background=c["bg"], foreground=c["fg"])
        s.configure("Card.TLabel", background=c["panel"])
        s.configure("Muted.TLabel", foreground=c["muted"])
        s.configure("CardMuted.TLabel", background=c["panel"], foreground=c["muted"])
        s.configure("Title.TLabel", background=c["panel"], font=self.font_title)
        s.configure("Big.TLabel", background=c["panel"], font=self.font_big)
        s.configure("Section.TLabel", font=self.font_bold)
        for kind in ("good", "bad", "warn"):
            s.configure("%s.TLabel" % kind.title(), background=c["panel"], foreground=c[kind], font=self.font_bold)
        s.configure("TButton", background=c["button"], foreground=c["fg"], bordercolor=c["border"],
                    padding=(self.px(10), self.px(4)), relief="flat", width=0)
        s.map("TButton", background=[("disabled", c["bg"]), ("pressed", c["border"]), ("active", c["button_hover"])],
              foreground=[("disabled", c["dim"])])
        s.configure("Accent.TButton", background=c["accent"], foreground=c["accent_fg"], bordercolor=c["accent"])
        s.map("Accent.TButton", background=[("disabled", c["button"]), ("active", c["accent_hover"])],
              foreground=[("disabled", c["dim"])])
        s.configure("Tool.TButton", padding=(self.px(8), self.px(3)))
        s.configure("Link.TButton", background=c["panel"], foreground=c["accent"], bordercolor=c["panel"],
                    padding=(self.px(4), self.px(2)))
        s.map("Link.TButton", background=[("active", c["button_hover"])])
        s.configure("Problems.TButton", foreground=c["warn"])
        s.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        s.map("TCheckbutton", background=[("active", c["bg"])], indicatorcolor=[("selected", c["accent"])])
        s.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"], padding=self.px(4))
        s.configure("TCombobox", fieldbackground=c["field"], foreground=c["fg"], background=c["button"],
                    padding=self.px(3))
        s.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["fg"])],
              selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["fg"])])
        self.root.option_add("*TCombobox*Listbox.background", c["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["select"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["select_fg"])
        s.configure("Treeview", background=c["panel"], fieldbackground=c["panel"], foreground=c["fg"],
                    rowheight=self.row_height, borderwidth=0)
        s.map("Treeview", background=[("selected", c["select"])], foreground=[("selected", c["select_fg"])])
        s.configure("Treeview.Heading", background=c["heading"], foreground=c["fg"], font=self.font_bold,
                    relief="flat", padding=(self.px(6), self.px(4)))
        s.map("Treeview.Heading", background=[("active", c["button_hover"])])
        s.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, self.px(4), 0, 0))
        s.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"], padding=(self.px(14), self.px(6)),
                    borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", c["panel"])], foreground=[("selected", c["fg"])])
        s.configure("TPanedwindow", background=c["bg"])
        s.configure("Sash", sashthickness=self.px(6), background=c["bg"])
        s.configure("Vertical.TScrollbar", background=c["button"], troughcolor=c["bg"], bordercolor=c["bg"],
                    arrowcolor=c["muted"])
        s.configure("TSeparator", background=c["border"])
        self.root.configure(background=c["bg"])
        for menu in self.menus:
            menu.configure(background=c["panel"], foreground=c["fg"], activebackground=c["select"],
                           activeforeground=c["select_fg"], disabledforeground=c["dim"], relief="flat",
                           selectcolor=c["accent"])
        self.configure_tags()
        if self.addons:
            self.refresh_list()
            self.refresh_details()

    def configure_tags(self):
        c = self.colors()
        t = self.tree
        t.tag_configure("odd", background=c["stripe"])
        t.tag_configure("broken", background=c["broken_bg"])
        t.tag_configure("mostly", background=c["mostly_bg"])
        t.tag_configure("rel_win", background=c["win"])
        t.tag_configure("rel_lose", background=c["lose"])
        t.tag_configure("rel_both", background=c["both"])
        t.tag_configure("nothing", foreground=c["dim"])
        t.tag_configure("disabled", foreground=c["dim"])
        t.tag_configure("fixed", foreground=c["fixed"])
        for tree in (self.conf_tree, self.find_tree, self.rules_tree, self.files_tree):
            tree.tag_configure("broken", background=c["broken_bg"])
            tree.tag_configure("severe", foreground=c["bad"])
            tree.tag_configure("used", foreground=c["good"])
            tree.tag_configure("inactive", foreground=c["dim"])
            tree.tag_configure("note", foreground=c["muted"])
            tree.tag_configure("lost", foreground=c["warn"])
        self.legend_canvas_colors()

    # ------------------------------------------------------------------ layout

    def build_menu(self):
        bar = tk.Menu(self.root, tearoff=False)
        self.menus = [bar]

        def menu(label):
            m = tk.Menu(bar, tearoff=False)
            bar.add_cascade(label=label, menu=m)
            self.menus.append(m)
            return m

        f = menu("File")
        f.add_command(label="Save load order", accelerator="Ctrl+S", command=self.save_order)
        f.add_command(label="Revert to saved", command=self.revert_order)
        f.add_separator()
        f.add_command(label="Rescan", accelerator="F5", command=self.start_scan)
        f.add_command(label="Folders...", command=self.choose_folders)
        f.add_command(label="Export report...", command=self.export_report)
        f.add_command(label="Open workshop.txt folder", command=self.open_load_order_folder)
        f.add_separator()
        f.add_command(label="Quit", command=self.on_close)

        e = menu("Edit")
        e.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        e.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        e.add_separator()
        e.add_command(label="Turn selected on / off", accelerator="Space", command=self.toggle_enabled)
        e.add_command(label="Move to top", accelerator="Ctrl+Home", command=lambda: self.move_selected("top"))
        e.add_command(label="Move up", accelerator="Ctrl+Up", command=lambda: self.move_selected(-1))
        e.add_command(label="Move down", accelerator="Ctrl+Down", command=lambda: self.move_selected(1))
        e.add_command(label="Move to bottom", accelerator="Ctrl+End", command=lambda: self.move_selected("bottom"))
        e.add_separator()
        e.add_command(label="Find in list", accelerator="Ctrl+F", command=self.focus_filter)

        t = menu("Tools")
        t.add_command(label="Sort by rules", command=self.sort_by_rules)
        t.add_command(label="Move addons that do nothing to the losing end", command=self.move_removable)
        t.add_separator()
        t.add_command(label="Problems...", command=self.show_problems)

        self.profiles_menu = menu("Profiles")

        v = menu("View")
        for name in THEMES:
            v.add_radiobutton(label="%s theme" % name.title(), variable=self.theme_name, value=name,
                              command=self.on_theme_changed)
        v.add_separator()
        self.column_vars = {}
        for col, heading, *_rest in COLUMNS:
            if _rest[-1]:  # optional
                var = tk.BooleanVar(value=col not in self.hidden_columns)
                self.column_vars[col] = var
                v.add_checkbutton(label="Show %s column" % heading, variable=var, command=self.on_columns_changed)
        v.add_separator()
        v.add_checkbutton(label="Lower in the list wins", variable=self.last_wins)

        h = menu("Help")
        h.add_command(label="How it works", command=self.show_help)
        self.root.configure(menu=bar)
        self.rebuild_profiles_menu()

    def build_ui(self):
        # Toolbar
        bar = ttk.Frame(self.root, padding=(self.px(10), self.px(8), self.px(10), self.px(4)))
        bar.pack(fill="x")
        self.save_btn = ttk.Button(bar, text="Save load order", style="Accent.TButton", command=self.save_order)
        self.save_btn.pack(side="left")
        self.undo_btn = ttk.Button(bar, text="↶ Undo", style="Tool.TButton", command=self.undo)
        self.undo_btn.pack(side="left", padx=(self.px(8), 0))
        self.redo_btn = ttk.Button(bar, text="↷ Redo", style="Tool.TButton", command=self.redo)
        self.redo_btn.pack(side="left", padx=(self.px(4), 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=self.px(10))
        sort_btn = ttk.Button(bar, text="⇅ Sort by rules", style="Tool.TButton", command=self.sort_by_rules)
        sort_btn.pack(side="left")
        clean_btn = ttk.Button(bar, text="⤓ Group idle addons", style="Tool.TButton", command=self.move_removable)
        clean_btn.pack(side="left", padx=(self.px(4), 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=self.px(10))
        ttk.Label(bar, text="Profile", style="Muted.TLabel").pack(side="left")
        self.profile_box = ttk.Combobox(bar, state="readonly", width=18)
        self.profile_box.pack(side="left", padx=(self.px(6), 0))
        self.profile_box.bind("<<ComboboxSelected>>", lambda e: self.load_profile(self.profile_box.get()))
        save_profile = ttk.Button(bar, text="Save as...", style="Tool.TButton", command=self.save_profile)
        save_profile.pack(side="left", padx=(self.px(4), 0))

        rescan = ttk.Button(bar, text="⟳ Rescan", style="Tool.TButton", command=self.start_scan)
        rescan.pack(side="right")
        folders = ttk.Button(bar, text="Folders...", style="Tool.TButton", command=self.choose_folders)
        folders.pack(side="right", padx=(0, self.px(4)))
        self.problems_btn = ttk.Button(bar, text="✓ No problems", style="Tool.TButton", command=self.show_problems)
        self.problems_btn.pack(side="right", padx=(0, self.px(10)))

        theme = self.theme_name.get
        Tooltip(self.save_btn, "Write the load order to the game's workshop.txt (a backup is made first). "
                               "Close the game before saving.", theme)
        Tooltip(sort_btn, "Reorder so every rule is met, moving each rule's winner just ahead of its loser.", theme)
        Tooltip(clean_btn, "Move every addon that does nothing to the end of the list where it loses. "
                           "The game plays the same, and they end up together for unsubscribing.", theme)
        Tooltip(save_profile, "Save the current load order and on/off states as a named profile.", theme)
        Tooltip(self.problems_btn, "Things that need attention: broken rules, unreadable addons, leftover "
                                   "folders and more.", theme)

        self.panes = ttk.PanedWindow(self.root, orient="horizontal")
        self.panes.pack(fill="both", expand=True, padx=self.px(10), pady=(self.px(4), self.px(4)))

        # ---- Left: load order
        left = ttk.Frame(self.panes)
        self.panes.add(left, weight=3)
        head = ttk.Frame(left)
        head.pack(fill="x", pady=(0, self.px(6)))
        self.list_title = ttk.Label(head, text="Load order", style="Section.TLabel")
        self.list_title.pack(side="left")
        self.view_box = ttk.Combobox(head, state="readonly", values=VIEWS, textvariable=self.view, width=19)
        self.view_box.pack(side="right")
        self.filter_entry = ttk.Entry(head, textvariable=self.filter_text, width=24)
        self.filter_entry.pack(side="right", padx=(0, self.px(6)))
        ttk.Label(head, text="Filter", style="Muted.TLabel").pack(side="right", padx=(0, self.px(6)))

        listframe = ttk.Frame(left)
        listframe.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listframe, columns=[c[0] for c in COLUMNS], show="headings", selectmode="extended")
        for col, heading, width, anchor, stretch, _ in COLUMNS:
            self.tree.heading(col, text=heading, command=lambda c=col: self.sort_by_column(c))
            self.tree.column(col, width=self.px(width), minwidth=self.px(min(width, 40)), anchor=anchor,
                             stretch=stretch)
        self.apply_columns()
        scroll = ttk.Scrollbar(listframe, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        self.tree.bind("<ButtonPress-1>", self.on_drag_start)
        self.tree.bind("<B1-Motion>", self.on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self.on_drag_end)
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<space>", lambda e: self.toggle_enabled())
        self.tree.bind("<Button-3>", self.show_context_menu)
        if sys.platform == "darwin":
            self.tree.bind("<Button-2>", self.show_context_menu)

        foot = ttk.Frame(left)
        foot.pack(fill="x", pady=(self.px(6), 0))
        for text, cmd, tip in [("⤒", lambda: self.move_selected("top"), "Move to top (Ctrl+Home)"),
                               ("↑", lambda: self.move_selected(-1), "Move up (Ctrl+Up)"),
                               ("↓", lambda: self.move_selected(1), "Move down (Ctrl+Down)"),
                               ("⤓", lambda: self.move_selected("bottom"), "Move to bottom (Ctrl+End)")]:
            b = ttk.Button(foot, text=text, width=3, style="Tool.TButton", command=cmd)
            b.pack(side="left", padx=(0, self.px(3)))
            Tooltip(b, tip, theme)
        b = ttk.Button(foot, text="On / Off", style="Tool.TButton", command=self.toggle_enabled)
        b.pack(side="left", padx=(self.px(6), 0))
        Tooltip(b, "Turn the selected addons on or off (Space, or click the checkbox)", theme)
        self.legend = tk.Canvas(foot, height=self.px(22), highlightthickness=0)
        self.legend.pack(side="right", fill="x", expand=True)

        # ---- Right: details
        right = ttk.Frame(self.panes)
        self.panes.add(right, weight=2)
        card = ttk.Frame(right, style="Card.TFrame", padding=self.px(14))
        card.pack(fill="x")
        self.detail_title = ttk.Label(card, text="", style="Title.TLabel", wraplength=self.px(520), justify="left")
        self.detail_title.pack(fill="x")
        self.detail_info = ttk.Label(card, text="", style="CardMuted.TLabel", wraplength=self.px(520), justify="left")
        self.detail_info.pack(fill="x", pady=(self.px(2), 0))
        self.overview = ttk.Frame(card, style="Card.TFrame")
        self.actions = ttk.Frame(card, style="Card.TFrame")
        for text, cmd in [("Workshop page", self.open_workshop_page), ("Open folder", self.open_selected_folder),
                          ("Copy ID", self.copy_workshop_id)]:
            ttk.Button(self.actions, text=text, style="Link.TButton", command=cmd).pack(side="left",
                                                                                         padx=(0, self.px(6)))
        self.notes_row = ttk.Frame(card, style="Card.TFrame")
        ttk.Label(self.notes_row, text="Notes", style="CardMuted.TLabel").pack(side="left", padx=(0, self.px(8)))
        self.note_entry = ttk.Entry(self.notes_row, textvariable=self.note_text)
        self.note_entry.pack(side="left", fill="x", expand=True)
        self.note_entry.bind("<Return>", lambda e: self.save_note())
        self.note_entry.bind("<FocusOut>", lambda e: self.save_note())

        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True, pady=(self.px(8), 0))

        def tab(title):
            frame = ttk.Frame(self.tabs, style="Card.TFrame", padding=self.px(8))
            self.tabs.add(frame, text=title)
            return frame

        def scrolled_tree(parent, columns, show="tree headings"):
            holder = ttk.Frame(parent, style="Card.TFrame")
            holder.pack(fill="both", expand=True)
            tree = ttk.Treeview(holder, columns=columns, show=show)
            sb = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            tree.pack(side="left", fill="both", expand=True)
            return tree

        # Conflicts tab
        conf = tab("Conflicts")
        rule_bar = ttk.Frame(conf, style="Card.TFrame")
        rule_bar.pack(side="bottom", fill="x", pady=(self.px(8), 0))
        self.conf_tree = scrolled_tree(conf, ("pos", "result", "rule"))
        self.conf_tree.configure(selectmode="browse")
        for col, text, width, anchor, stretch in [("#0", "Other addon / file", 220, "w", True),
                                                  ("pos", "#", 40, "e", False),
                                                  ("result", "Result", 150, "w", False),
                                                  ("rule", "Rule", 100, "w", False)]:
            self.conf_tree.heading(col, text=text)
            self.conf_tree.column(col, width=self.px(width), minwidth=self.px(36), anchor=anchor, stretch=stretch)
        self.conf_tree.bind("<Double-1>", self.jump_to_other)
        ttk.Button(rule_bar, text="This addon should win", command=lambda: self.add_rule(True)).pack(side="left")
        ttk.Button(rule_bar, text="The other should win", command=lambda: self.add_rule(False)).pack(
            side="left", padx=self.px(6))
        ttk.Button(rule_bar, text="Remove rule", command=self.remove_rule_for_selected).pack(side="left")

        # Files tab
        files = tab("Files")
        self.files_tree = scrolled_tree(files, ("state",))
        self.files_tree.heading("#0", text="File")
        self.files_tree.heading("state", text="In game")
        self.files_tree.column("#0", width=self.px(260), minwidth=self.px(120))
        self.files_tree.column("state", width=self.px(240), minwidth=self.px(120))

        # Find file tab: which addons change a file, e.g. every file of one map
        find = tab("Find file")
        self.find_tab = find
        top = ttk.Frame(find, style="Card.TFrame")
        top.pack(fill="x", pady=(0, self.px(4)))
        entry = ttk.Entry(top, textvariable=self.find_text)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.run_find())
        self.find_entry = entry
        ttk.Button(top, text="Search", style="Accent.TButton", command=self.run_find).pack(side="left",
                                                                                           padx=(self.px(6), 0))
        ttk.Label(find, text="Part of a path, like a map name (d1_town_02) or a pattern (maps/d1_town_*)",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(0, self.px(4)))
        self.find_tree = scrolled_tree(find, ("pos", "state"))
        self.find_tree.configure(selectmode="browse")
        self.find_tree.heading("#0", text="File / addon")
        self.find_tree.heading("pos", text="#")
        self.find_tree.heading("state", text="In game")
        self.find_tree.column("#0", width=self.px(260), minwidth=self.px(120))
        self.find_tree.column("pos", width=self.px(40), anchor="e", stretch=False)
        self.find_tree.column("state", width=self.px(210), minwidth=self.px(120))
        self.find_tree.bind("<Double-1>", self.jump_from_find)

        # Rules tab
        rules = tab("Rules")
        self.rules_tree = scrolled_tree(rules, ("winner", "loser", "ok"), show="headings")
        self.rules_tree.configure(selectmode="extended")
        self.rules_tree.heading("winner", text="Should win")
        self.rules_tree.heading("loser", text="Over")
        self.rules_tree.heading("ok", text="Now")
        self.rules_tree.column("ok", width=self.px(70), stretch=False)
        ttk.Button(rules, text="Delete selected rules", command=self.delete_rules).pack(anchor="w",
                                                                                       pady=(self.px(8), 0))

        # Status bar
        statusbar = ttk.Frame(self.root, padding=(self.px(10), self.px(2), self.px(10), self.px(6)))
        statusbar.pack(fill="x")
        self.status = ttk.Label(statusbar, text="", style="Muted.TLabel", anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.dirty_label = ttk.Label(statusbar, text="", style="Muted.TLabel")
        self.dirty_label.pack(side="right")

    def legend_canvas_colors(self):
        """Colour key for the list, drawn as small swatches."""
        c = self.colors()
        cv = self.legend
        cv.configure(background=c["bg"])
        cv.delete("all")
        x = self.px(6)
        y = self.px(11)
        for color, text in [(None, "▲ overwrites  ▼ overwritten  ✕ does nothing"),
                            (c["win"], "selected wins"), (c["lose"], "selected loses")]:
            if color:
                cv.create_rectangle(x, y - self.px(6), x + self.px(12), y + self.px(6), fill=color,
                                    outline=c["border"])
                x += self.px(16)
            item = cv.create_text(x, y, text=text, anchor="w", fill=c["muted"], font=self.font_small)
            x = cv.bbox(item)[2] + self.px(14)

    def restore_layout(self):
        geometry = self.settings.get("geometry")
        if geometry:
            try:
                self.root.geometry(geometry)
            except tk.TclError:
                geometry = None
        if not geometry:
            w = min(self.px(1500), self.root.winfo_screenwidth() - 80)
            h = min(self.px(860), self.root.winfo_screenheight() - 120)
            self.root.geometry("%dx%d" % (w, h))
        # Give the details side its share once the window has a size.
        self.root.after(80, self.restore_sash)

    def restore_sash(self):
        self.root.update_idletasks()
        width = self.panes.winfo_width()
        if width < 200:
            self.root.after(100, self.restore_sash)
            return
        frac = self.settings.get("sash", 0.58)
        pos = int(width * min(max(frac, 0.3), 0.8))
        try:
            self.panes.sashpos(0, pos)
        except tk.TclError:
            pass

    def bind_keys(self):
        r = self.root
        r.bind_all("<Control-s>", lambda e: self.save_order())
        r.bind_all("<Control-z>", lambda e: self.undo())
        r.bind_all("<Control-y>", lambda e: self.redo())
        r.bind_all("<Control-Shift-Z>", lambda e: self.redo())
        r.bind_all("<Control-f>", lambda e: self.focus_filter())
        r.bind_all("<F5>", lambda e: self.start_scan())
        self.tree.bind("<Control-Up>", lambda e: self.move_selected(-1) or "break")
        self.tree.bind("<Control-Down>", lambda e: self.move_selected(1) or "break")
        self.tree.bind("<Control-Home>", lambda e: self.move_selected("top") or "break")
        self.tree.bind("<Control-End>", lambda e: self.move_selected("bottom") or "break")

    def center(self, win):
        """Put a dialog in the middle of the main window."""
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))

    def focus_filter(self):
        self.filter_entry.focus_set()
        self.filter_entry.select_range(0, "end")

    def on_theme_changed(self):
        self.apply_theme()
        self.save_settings()

    def apply_columns(self):
        shown = [c[0] for c in COLUMNS if c[0] not in self.hidden_columns]
        self.tree.configure(displaycolumns=shown)

    def on_columns_changed(self):
        self.hidden_columns = [col for col, var in self.column_vars.items() if not var.get()]
        self.apply_columns()
        self.save_settings()

    # ------------------------------------------------------------------ scanning

    def start_scan(self, confirm=True):
        if self.scanning:
            return
        if confirm and self.is_dirty() and not messagebox.askyesno(
                "Rescan", "Rescanning discards your unsaved load order changes. Continue?"):
            return
        self.scanning = True
        self.status.config(text="Finding Half-Life 2...")
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self.scan_thread, daemon=True).start()

    def poll_ui(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self.poll_ui)

    def ui(self, fn):
        """Run fn on the main thread (for use from the scan thread)."""
        self.ui_queue.put(fn)

    def set_status(self, text):
        self.ui(lambda: self.status.config(text=text))

    def scan_thread(self):
        def progress(i, total, name=""):
            self.set_status("Reading addon %d of %d: %s" % (i, total, name))

        def slow(path, seconds):
            self.set_status("Still reading %s (%d s). If this never finishes, that file is the problem."
                            % (path, seconds))
        try:
            game = ac.resolve_game_dir(self.game_dir)
            workshop = ac.resolve_workshop_dir(self.workshop_dir)
            if not game and not workshop:
                game, workshop = ac.find_hl2_dirs()
            # One folder is enough to find the other in the same Steam library.
            workshop = workshop or (game and ac.workshop_dir_for_game(game))
            game = game or (workshop and ac.game_dir_for_workshop(workshop))
            containers, path = ac.game_folders(game, workshop)
            addons = (ac.scan(containers, progress, workshop_dirs=[workshop] if workshop else [], slow=slow)
                      if containers else [])
            ids = {a.workshop_id for a in addons if a.workshop_id}
            if ids:
                self.set_status("Looking up Workshop titles on Steam...")
                titles = ac.fetch_workshop_titles(ids)
                for a in addons:
                    if a.workshop_id in titles:
                        a.name = titles[a.workshop_id]
                    elif a.workshop_id:
                        a.name = "Workshop item %s" % a.workshop_id
            entries = ac.read_load_order(path) if path else []
            base, base_info = {}, "original game files not checked (Half-Life 2 folder not found)"
            if game:
                self.set_status("Reading the original game files...")
                base, vpk_count, _, _ = ac.read_base_game(game, slow)
                base_info = ("compared with %d original game files" % len(base) if vpk_count
                             else "no original game VPKs found, so files weren't compared with them")
        except Exception as e:  # report anything unexpected instead of dying silently
            message = str(e)
            self.set_status("Scan failed: " + message)
            self.ui(lambda: setattr(self, "scanning", False))
            self.ui(lambda: messagebox.showerror("Scan failed", message))
            return
        self.ui(lambda: self.scan_done(game, workshop, containers, addons, path, entries, base, base_info))

    def scan_done(self, game, workshop, containers, addons, path, entries, base=None, base_info=""):
        self.scanning = False
        self.base, self.base_info = base or {}, base_info
        self.found_game, self.found_workshop = game, workshop
        self.containers = containers
        if not game or not workshop:
            missing = [n for n, d in (("Half-Life 2", game), ("Workshop", workshop)) if not d]
            text = ("Couldn't find the %s folder. Please choose it." % missing[0] if len(missing) == 1
                    else "Couldn't find the Half-Life 2 and Workshop folders. Please choose them.")
            self.root.after(100, lambda: self.choose_folders(text))
        elif not path:
            messagebox.showwarning("No load order",
                                   "Couldn't find hl2_complete/cfg/workshop.txt in\n%s\n\n"
                                   "Start the game once so it creates it." % game)
        self.leftovers = [a for a in addons if a.leftover]
        self.addons = [a for a in addons if not a.leftover]
        self.by_id = {a.workshop_id: a for a in self.addons if a.workshop_id}
        self.types = {a: addon_type(a) for a in self.addons}
        self.sizes = {a: addon_size(a) for a in self.addons}
        self.load_order_path = path
        self.order = [[wsid, enabled] for wsid, enabled in entries]
        self.saved_order = [list(e) for e in self.order]
        self.history, self.future = [], []
        self.backup_done = False
        self.selected = None
        self.root.title("HL2 Addon Organizer - %s" % (game or workshop or "no folders"))
        self.recompute()
        self.check_load_order_matches()

    def check_load_order_matches(self):
        """A workshop.txt that lists few of the installed addons is usually from the wrong folder."""
        installed = [a for a in self.addons if a.workshop_id]
        listed = [a for a in installed if a.position is not None]
        if not self.load_order_path or len(installed) < 5 or len(listed) * 2 >= len(installed):
            return
        stale = len([w for w, _ in self.order if w not in self.by_id])
        messagebox.showwarning(
            "Load order doesn't match your addons",
            "Only %d of your %d installed Workshop addons are in\n%s\n\n"
            "and it lists %d addons that aren't installed. It may be from an old or different "
            "Half-Life 2 folder: check Folders... (the folders in use are in the window title).\n\n"
            "If the folder is right, start the game once so it updates the list."
            % (len(listed), len(installed), self.load_order_path, stale))

    # ------------------------------------------------------------------ analysis

    def listed_ids(self):
        return [wsid for wsid, _ in self.order if wsid in self.by_id]

    def recompute(self):
        enabled = {wsid: en for wsid, en in self.order}
        positions = {wsid: n for n, (wsid, _) in enumerate(self.order)}
        last_wins = self.last_wins.get()
        active = []
        for a in self.addons:
            a.position = a.rank = None
            if a.workshop_id:
                if a.workshop_id not in positions:
                    continue  # not in workshop.txt: the game doesn't load it yet
                n = positions[a.workshop_id]
                a.position = n + 1
                if not enabled[a.workshop_id]:
                    continue
                a.rank = -n if last_wins else n
            active.append(a)

        self.by_path = ac.index_files(active)
        self.removable, idle = ac.find_idle_addons(active, self.by_path, self.base)
        self.idle = {e[0]: e for e in idle}

        # Per addon: files where it beats / loses to a different copy.
        self.stats = defaultdict(lambda: [0, 0])
        for path, copies in self.by_path.items():
            if len({(s, c) for _, s, c in copies}) < 2 or not ac.known(*(c[0] for c in copies)):
                continue
            ranked = ac.by_rank(copies)
            winner = ranked[0]
            for c in ranked[1:]:
                if (c[1], c[2]) != (winner[1], winner[2]):
                    self.stats[c[0]][1] += 1
            if any((c[1], c[2]) != (winner[1], winner[2]) for c in ranked[1:]):
                self.stats[winner[0]][0] += 1

        self.refresh_list()
        self.refresh_details()
        self.highlight_relations()
        self.refresh_rules()
        self.refresh_status()
        if self.find_text.get().strip():
            self.run_find()  # the order changed, so which copy is used may have too

    def rule_broken(self, rule):
        a, b = self.by_id.get(rule["winner"]), self.by_id.get(rule["loser"])
        if not a or not b or a.position is None or b.position is None:
            return False
        return (a.position > b.position) != self.last_wins.get()

    def broken_ids(self):
        out = set()
        for r in self.rules:
            if self.rule_broken(r):
                out.update((r["winner"], r["loser"]))
        return out

    # ------------------------------------------------------------------ list

    def addon_key(self, a):
        return a.workshop_id or "custom:" + a.location

    def addon_for_key(self, key):
        if key == self.addon_key(self.base_addon):
            return self.base_addon
        if key.startswith("custom:"):
            return next((a for a in self.addons if "custom:" + a.location == key), None)
        return self.by_id.get(key)

    def status_text(self, a):
        if a.workshop_id and a.position is None:
            return "not in workshop.txt yet"
        if a.workshop_id and a.rank is None:
            return "disabled"
        if a.errors:
            return "problem reading"
        if a in self.removable:
            return "does nothing" if a.workshop_id else "custom/, does nothing"
        if not a.workshop_id:
            return "custom/ (always loaded)"
        if a in self.idle:
            e = self.idle[a]
            pct = 100 * ac.idle_count(e) // max(len(a.files), 1)
            return "mostly nothing (%d%%)" % pct
        return ""

    def flags(self, a):
        if a in self.removable:
            return "✕"
        wins, loses = self.stats.get(a, (0, 0))
        return ("▲" if wins else "") + ("▼" if loses else "")

    def in_view(self, a, view, broken):
        wins, loses = self.stats.get(a, (0, 0))
        status = self.status_text(a)
        return {
            "All addons": True,
            "Has conflicts": bool(wins or loses),
            "Overwrites others": bool(wins),
            "Is overwritten": bool(loses),
            "Does nothing": a in self.removable,
            "Mostly does nothing": status.startswith("mostly"),
            "Disabled": status == "disabled",
            "Rule broken": a.workshop_id in broken,
            "Not in load order": a.position is None,
            "Has notes": bool(self.notes.get(self.addon_key(a))),
        }.get(view, True)

    def is_plain_view(self):
        """Load order shown as is: no filter, no other view, sorted by position."""
        return not self.filter_text.get().strip() and self.view.get() == VIEWS[0] and self.sort[0] == "pos"

    def reset_view(self):
        if not self.is_plain_view():
            self.filter_text.set("")
            self.view.set(VIEWS[0])
            self.sort = ("pos", False)
            self.update_headings()

    def sort_by_column(self, col):
        if col in ("on", "flags"):
            return
        key, reverse = self.sort
        self.sort = (col, not reverse) if key == col else (col, col in ("files", "size", "wins", "loses"))
        if col == "pos":
            self.sort = ("pos", False)
        self.update_headings()
        self.refresh_list()
        if self.sort[0] != "pos":
            self.status.config(text="Sorted by %s. Click # to go back to load order (needed to drag and move)."
                               % dict((c[0], c[1]) for c in COLUMNS)[col])

    def update_headings(self):
        key, reverse = self.sort
        for col, heading, *_ in COLUMNS:
            arrow = (" ▼" if reverse else " ▲") if col == key and key != "pos" else ""
            self.tree.heading(col, text=heading + arrow)

    def refresh_list(self):
        keep = set(self.tree.selection())
        top = self.tree.yview()[0] if self.tree.get_children() else 0
        self.tree.delete(*self.tree.get_children())
        needle = self.filter_text.get().strip().lower()
        view = self.view.get()
        broken = self.broken_ids()

        rows = [self.by_id[w] for w in self.listed_ids()]
        rows += sorted((a for a in self.addons if a.workshop_id and a.position is None), key=lambda a: a.name.lower())
        rows += [a for a in self.addons if not a.workshop_id]
        rows = [a for a in rows if (not needle or needle in a.name.lower() or needle in (a.workshop_id or "")
                                    or needle in self.notes.get(self.addon_key(a), "").lower())
                and self.in_view(a, view, broken)]
        key, reverse = self.sort
        if key != "pos":
            sorters = {
                "name": lambda a: a.name.lower(), "type": lambda a: self.types.get(a, ""),
                "files": lambda a: len(a.files), "size": lambda a: self.sizes.get(a, 0),
                "wins": lambda a: self.stats.get(a, (0, 0))[0], "loses": lambda a: self.stats.get(a, (0, 0))[1],
                "status": lambda a: self.status_text(a), "notes": lambda a: self.notes.get(self.addon_key(a), ""),
            }
            rows.sort(key=sorters[key], reverse=reverse)

        self.row_tags = {}
        for n, a in enumerate(rows):
            status = self.status_text(a)
            wins, loses = self.stats.get(a, (0, 0))
            bg = "odd" if n % 2 else ""
            if a.workshop_id in broken:
                bg = "broken"
                status = ("rule broken; " + status).rstrip("; ")
            elif status.startswith("mostly"):
                bg = "mostly"
            fg = ""
            if status.endswith("does nothing") or status == "disabled":
                fg = "nothing"
            elif a.position is None:
                fg = "fixed"
            on = "" if a.position is None else ("☑" if a.rank is not None else "☐")
            key = self.addon_key(a)
            self.row_tags[key] = (fg, bg)
            self.tree.insert("", "end", iid=key, tags=[t for t in (fg, bg) if t], values=(
                on, a.position or "", a.name, self.flags(a), self.types.get(a, ""), len(a.files),
                ac.nice_size(self.sizes.get(a, 0)), wins or "", loses or "", status,
                self.notes.get(key, "")))
        for k in keep:
            if self.tree.exists(k):
                self.tree.selection_add(k)
        if rows:
            self.tree.yview_moveto(top)
        total = len([a for a in self.addons if a.position is not None])
        self.list_title.config(text="Load order   %d of %d shown" % (len(rows), len(self.addons))
                               if len(rows) != len(self.addons) else "Load order   %d addons, %d listed"
                               % (len(self.addons), total))
        self.highlight_relations()

    def highlight_relations(self):
        """Mod Organizer style: colour the addons the selected one overwrites, or is overwritten by."""
        rel = self.selected_rel
        for key, (fg, bg) in self.row_tags.items():
            a = self.addon_for_key(key)
            r = rel.get(a) if rel and a is not self.selected else None
            win = r and r["wins"]
            lose = r and (r["loses"] or r["reverted"])
            hl = "rel_both" if win and lose else "rel_win" if win else "rel_lose" if lose else bg
            self.tree.item(key, tags=[t for t in (fg, hl) if t])

    # ------------------------------------------------------------------ order editing

    def movable_selection(self):
        return [k for k in self.tree.selection() if k in self.by_id and self.by_id[k].position is not None]

    def is_dirty(self):
        return self.order != self.saved_order

    def set_order(self, new_order, remember=True):
        """Every load order change goes through here, so it can be undone."""
        if new_order == self.order:
            return
        if remember:
            self.history.append([list(e) for e in self.order])
            del self.history[:-200]
            self.future = []
        self.order = [list(e) for e in new_order]
        self.recompute()

    def apply_order(self, ids_in_order):
        """ids_in_order: the installed, listed ids in their new order. Uninstalled ids keep their slots."""
        it = iter(ids_in_order)
        enabled = {w: e for w, e in self.order}
        new = []
        for w, e in self.order:
            if w in self.by_id:
                w = next(it)
                e = enabled[w]
            new.append([w, e])
        self.set_order(new)

    def undo(self):
        if not self.history:
            self.status.config(text="Nothing to undo.")
            return
        self.future.append([list(e) for e in self.order])
        self.set_order(self.history.pop(), remember=False)

    def redo(self):
        if not self.future:
            self.status.config(text="Nothing to redo.")
            return
        self.history.append([list(e) for e in self.order])
        self.set_order(self.future.pop(), remember=False)

    def move_selected(self, where):
        sel = self.movable_selection()
        self.reset_view()  # moving within a filtered or sorted view would be confusing
        if not sel:
            return
        ids = self.listed_ids()
        chosen = [w for w in ids if w in sel]
        rest = [w for w in ids if w not in sel]
        if where == "top":
            new = chosen + rest
        elif where == "bottom":
            new = rest + chosen
        else:
            new = list(ids)
            idx = sorted(new.index(w) for w in chosen)
            if where < 0:
                for i in idx:
                    if i > 0 and new[i - 1] not in chosen:
                        new[i - 1], new[i] = new[i], new[i - 1]
            else:
                for i in reversed(idx):
                    if i < len(new) - 1 and new[i + 1] not in chosen:
                        new[i + 1], new[i] = new[i], new[i + 1]
        self.apply_order(new)
        self.tree.selection_set(chosen)
        self.tree.see(chosen[0])

    def toggle_enabled(self, keys=None):
        sel = keys or self.movable_selection()
        if not sel:
            return "break"
        turn_on = not all(e for w, e in self.order if w in sel)
        self.set_order([[w, turn_on if w in sel else e] for w, e in self.order])
        return "break"

    def column_at(self, x):
        col = self.tree.identify_column(x)
        if not col.startswith("#") or col == "#0":
            return None
        shown = self.tree["displaycolumns"]
        if shown in ("#all", ("#all",)):
            shown = [c[0] for c in COLUMNS]
        index = int(col[1:]) - 1
        return shown[index] if 0 <= index < len(shown) else None

    def on_drag_start(self, event):
        row = self.tree.identify_row(event.y)
        self.drag_moved = False
        self.drag = None
        if not row or row not in self.by_id or self.by_id[row].position is None:
            return
        if self.column_at(event.x) == "on":
            self.toggle_enabled([row])
            self.tree.selection_set(row)
            return "break"
        self.drag = row

    def on_drag_motion(self, event):
        if not self.drag or not self.is_plain_view():
            return
        target = self.tree.identify_row(event.y)
        if target and target != self.drag and target in self.by_id and self.by_id[target].position is not None:
            self.tree.move(self.drag, "", self.tree.index(target))
            self.drag_moved = True

    def on_drag_end(self, event):
        if self.drag and self.drag_moved:
            new = [k for k in self.tree.get_children() if k in self.by_id and self.by_id[k].position is not None]
            dragged = self.drag
            self.drag = None
            self.apply_order(new)
            self.tree.selection_set(dragged)
        self.drag = None

    def on_double_click(self, event):
        if self.column_at(event.x) == "on":
            return "break"  # the single click already toggled it
        if self.tree.identify_row(event.y):
            return self.toggle_enabled()

    def sort_by_rules(self):
        """
        Satisfies every rule with as little movement as possible: each rule's
        winner moves to just ahead of its loser, everything else keeps its order.
        """
        ids = self.listed_ids()
        last_wins = self.last_wins.get()
        edges = []  # (earlier, later) in list order
        for r in self.rules:
            w, l = r["winner"], r["loser"]
            if w in self.by_id and l in self.by_id and w in ids and l in ids:
                edges.append((l, w) if last_wins else (w, l))
        # Building the list from the end pulls "earlier" items up (the winner when
        # higher wins); building from the start pushes "later" items down (the
        # winner when lower wins).
        new = stable_toposort(ids, edges, from_end=not last_wins)
        if len(new) < len(ids):
            stuck = [self.by_id[w].name for w in ids if w not in new]
            messagebox.showerror("Rules contradict each other",
                                 "These addons have rules that go in a circle, so no order satisfies them all:\n\n"
                                 + "\n".join(stuck[:15]) + "\n\nDelete one of the rules in the Rules tab.")
            return
        if new == ids:
            messagebox.showinfo("Sort by rules", "The load order already follows every rule.")
            return
        self.reset_view()
        self.apply_order(new)
        self.status.config(text="Sorted by rules. Not saved yet.")

    def move_removable(self):
        ids = self.listed_ids()
        idle = [w for w in ids if self.by_id[w] in self.removable]
        if not idle:
            messagebox.showinfo("Nothing to move", "No addons are currently doing nothing.")
            return
        rest = [w for w in ids if w not in idle]
        self.reset_view()
        # The losing end: the bottom if higher wins, the top if lower wins.
        self.apply_order(idle + rest if self.last_wins.get() else rest + idle)
        self.tree.selection_set(idle)
        self.tree.see(idle[0])
        messagebox.showinfo(
            "Moved",
            "Moved %d addons that do nothing to the %s of the list. The game plays the same, since they "
            "were already losing or are identical copies.\n\nThey're selected now. Save the load order, then "
            "you'll find them together in the game's Add-ons screen."
            % (len(idle), "top" if self.last_wins.get() else "bottom"))

    def on_direction_changed(self):
        self.save_settings()
        if self.addons:
            self.recompute()

    # ------------------------------------------------------------------ context menu & actions

    def show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
            self.on_select()
        a = self.addon_for_key(row)
        sel = self.movable_selection()
        c = self.colors()
        m = tk.Menu(self.root, tearoff=False, background=c["panel"], foreground=c["fg"],
                    activebackground=c["select"], activeforeground=c["select_fg"], disabledforeground=c["dim"])
        title = a.name if len(self.tree.selection()) == 1 else "%d addons" % len(self.tree.selection())
        m.add_command(label=title[:60], state="disabled")
        m.add_separator()
        state = "normal" if sel else "disabled"
        enabled = sel and all(e for w, e in self.order if w in sel)
        m.add_command(label="Turn off" if enabled else "Turn on", state=state, command=self.toggle_enabled)
        m.add_command(label="Move to top", state=state, command=lambda: self.move_selected("top"))
        m.add_command(label="Move up", state=state, command=lambda: self.move_selected(-1))
        m.add_command(label="Move down", state=state, command=lambda: self.move_selected(1))
        m.add_command(label="Move to bottom", state=state, command=lambda: self.move_selected("bottom"))
        m.add_separator()
        m.add_command(label="Show conflicts", command=lambda: self.tabs.select(0))
        m.add_command(label="Show files", command=lambda: self.tabs.select(1))
        m.add_command(label="Edit notes", command=self.focus_note)
        m.add_separator()
        single = len(self.tree.selection()) == 1
        m.add_command(label="Open Workshop page", state="normal" if single and a.workshop_id else "disabled",
                      command=self.open_workshop_page)
        m.add_command(label="Open folder", state="normal" if single else "disabled",
                      command=self.open_selected_folder)
        m.add_command(label="Copy Workshop ID", state="normal" if single and a.workshop_id else "disabled",
                      command=self.copy_workshop_id)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def open_workshop_page(self):
        a = self.selected
        if a and a.workshop_id:
            webbrowser.open(WORKSHOP_URL % a.workshop_id)

    def open_selected_folder(self):
        a = self.selected
        if a and a.location and os.path.exists(a.location):
            open_path(a.location if os.path.isdir(a.location) else os.path.dirname(a.location))

    def open_load_order_folder(self):
        if self.load_order_path:
            open_path(os.path.dirname(self.load_order_path))

    def copy_workshop_id(self):
        a = self.selected
        if a and a.workshop_id:
            self.root.clipboard_clear()
            self.root.clipboard_append(a.workshop_id)
            self.status.config(text="Copied %s" % a.workshop_id)

    def focus_note(self):
        if self.selected and self.selected is not self.base_addon:
            self.note_entry.focus_set()

    def save_note(self):
        a = self.selected
        if not a or a is self.base_addon:
            return
        key = self.addon_key(a)
        text = self.note_text.get().strip()
        if text == self.notes.get(key, ""):
            return
        if text:
            self.notes[key] = text
        else:
            self.notes.pop(key, None)
        self.save_settings()
        if self.tree.exists(key):
            self.tree.set(key, "notes", text)

    # ------------------------------------------------------------------ profiles

    def rebuild_profiles_menu(self):
        m = self.profiles_menu
        m.delete(0, "end")
        m.add_command(label="Save current load order as...", command=self.save_profile)
        names = sorted(self.profiles, key=str.lower)
        if names:
            m.add_separator()
            for name in names:
                m.add_command(label="Load \"%s\"" % name, command=lambda n=name: self.load_profile(n))
            m.add_separator()
            delete = tk.Menu(m, tearoff=False)
            self.menus.append(delete)
            for name in names:
                delete.add_command(label=name, command=lambda n=name: self.delete_profile(n))
            m.add_cascade(label="Delete profile", menu=delete)
        if hasattr(self, "profile_box"):
            self.profile_box.configure(values=names)

    def save_profile(self):
        if not self.order:
            messagebox.showinfo("Profiles", "There's no load order to save yet.")
            return
        name = simpledialog.askstring("Save profile", "Name for this load order:", parent=self.root,
                                      initialvalue=self.profile_box.get() or "")
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.profiles and not messagebox.askyesno("Save profile", "Replace the profile \"%s\"?" % name):
            return
        self.profiles[name] = [list(e) for e in self.order]
        self.save_settings()
        self.rebuild_profiles_menu()
        self.profile_box.set(name)
        self.status.config(text="Saved profile \"%s\"." % name)

    def load_profile(self, name):
        entries = self.profiles.get(name)
        if not entries:
            return
        # Profile order first; anything not in the profile keeps its state at the end.
        in_profile = {w for w, _ in entries}
        new = [list(e) for e in entries if e[0] in {w for w, _ in self.order}]
        new += [list(e) for e in self.order if e[0] not in in_profile]
        self.reset_view()
        self.set_order(new)
        self.profile_box.set(name)
        self.status.config(text="Loaded profile \"%s\". Save the load order to use it in game." % name)

    def delete_profile(self, name):
        if messagebox.askyesno("Delete profile", "Delete the profile \"%s\"?" % name):
            self.profiles.pop(name, None)
            self.save_settings()
            self.rebuild_profiles_menu()
            if self.profile_box.get() == name:
                self.profile_box.set("")

    # ------------------------------------------------------------------ folders

    def choose_folders(self, message=None):
        c = self.colors()
        win = tk.Toplevel(self.root)
        win.title("Half-Life 2 folders")
        win.transient(self.root)
        win.resizable(True, False)
        win.configure(background=c["bg"])
        frame = ttk.Frame(win, padding=self.px(16))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        row = 0
        if message:
            ttk.Label(frame, text=message, foreground=c["bad"]).grid(row=0, column=0, columnspan=3, sticky="w",
                                                                     pady=(0, self.px(10)))
            row = 1
        game = tk.StringVar(value=self.found_game or self.game_dir or "")
        workshop = tk.StringVar(value=self.found_workshop or self.workshop_dir or "")

        def browse(var, title):
            start = var.get() if os.path.isdir(var.get()) else None
            chosen = filedialog.askdirectory(parent=win, title=title, initialdir=start, mustexist=True)
            if chosen:
                var.set(os.path.normpath(chosen))
                # Picking the game also finds its library's Workshop folder, if that's still empty.
                if var is game and not workshop.get():
                    g = ac.resolve_game_dir(var.get())
                    workshop.set((g and ac.workshop_dir_for_game(g)) or "")
                elif var is workshop and not game.get():
                    w = ac.resolve_workshop_dir(var.get())
                    game.set((w and ac.game_dir_for_workshop(w)) or "")

        for label, var, hint in [
                ("Half-Life 2 folder", game, r"e.g. D:\SteamLibrary\steamapps\common\Half-Life 2"),
                ("Workshop folder", workshop, r"e.g. D:\SteamLibrary\steamapps\workshop\content\220")]:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, self.px(10)))
            ttk.Entry(frame, textvariable=var, width=70).grid(row=row, column=1, sticky="ew")
            ttk.Button(frame, text="Browse...", command=lambda v=var, t=label: browse(v, "Choose the " + t)
                       ).grid(row=row, column=2, padx=(self.px(6), 0))
            ttk.Label(frame, text=hint, style="Muted.TLabel").grid(row=row + 1, column=1, sticky="w",
                                                                   pady=(self.px(2), self.px(10)))
            row += 2

        def detect():
            g, w = ac.find_hl2_dirs()
            game.set(g or "")
            workshop.set(w or "")
            if not g or not w:
                messagebox.showinfo("Detect", "Steam didn't say where %s is. Please browse to it." %
                                    " and ".join(n for n, d in (("Half-Life 2", g), ("the Workshop folder", w))
                                                 if not d), parent=win)

        def ok():
            g = ac.resolve_game_dir(game.get().strip())
            w = ac.resolve_workshop_dir(workshop.get().strip())
            problems = []
            if game.get().strip() and not g:
                problems.append("The Half-Life 2 folder should contain hl2 and hl2_complete.")
            if workshop.get().strip() and not w:
                problems.append("The Workshop folder should be steamapps\\workshop\\content\\220.")
            if not g and not w:
                problems.append("Choose at least one folder.")
            if problems:
                messagebox.showerror("Folders", "\n".join(problems), parent=win)
                return
            if self.is_dirty() and not messagebox.askyesno(
                    "Folders", "Rescanning discards your unsaved load order changes. Continue?", parent=win):
                return
            self.game_dir, self.workshop_dir = g, w
            self.save_settings()
            win.destroy()
            self.start_scan(confirm=False)

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(self.px(6), 0))
        ttk.Button(buttons, text="Detect automatically", command=detect).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", style="Accent.TButton", command=ok).pack(side="right", padx=self.px(6))
        win.bind("<Return>", lambda e: ok())
        win.bind("<Escape>", lambda e: win.destroy())
        self.center(win)
        win.grab_set()
        return win

    # ------------------------------------------------------------------ problems

    def problems(self):
        """[(severity, title, detail, action label or None, action)]"""
        out = []
        broken = [r for r in self.rules if self.rule_broken(r)]
        if broken:
            out.append(("warn", "%s not met" % ac.plural(len(broken), "rule"),
                        "The load order doesn't follow some of your rules.", "Sort by rules", self.sort_by_rules))
        unreadable = [a for a in self.addons if a.errors]
        for a in unreadable:
            out.append(("bad", "Couldn't fully read %s" % a.name, "; ".join(a.errors[:2]),
                        "Open folder", lambda a=a: open_path(a.location if os.path.isdir(a.location)
                                                             else os.path.dirname(a.location))))
        unlisted = [a for a in self.addons if a.workshop_id and a.position is None]
        if unlisted:
            out.append(("warn", "%s not in workshop.txt" % ac.plural(len(unlisted), "addon"),
                        "The game adds new subscriptions to its list when it starts. Start it once, then rescan.",
                        None, None))
        if self.removable:
            out.append(("info", "%s do nothing" % ac.plural(len(self.removable), "addon"),
                        "Every file is overwritten or identical elsewhere. You can unsubscribe from them.",
                        "Move them down", self.move_removable))
        if self.leftovers:
            out.append(("info", "%s from removed addons" % ac.plural(len(self.leftovers), "leftover folder"),
                        "These Workshop folders only hold cache files the game left behind.",
                        "Delete them", self.delete_leftovers))
        if self.base_info.startswith(("no original", "original game files not")):
            out.append(("warn", "Original game files weren't compared", self.base_info, "Folders...",
                        self.choose_folders))
        if self.addons and not self.load_order_path:
            out.append(("bad", "No workshop.txt", "The load order can't be read or saved.", "Folders...",
                        self.choose_folders))
        return out

    def show_problems(self):
        c = self.colors()
        items = self.problems()
        win = tk.Toplevel(self.root)
        win.title("Problems")
        win.transient(self.root)
        win.configure(background=c["bg"])
        frame = ttk.Frame(win, padding=self.px(16))
        frame.pack(fill="both", expand=True)
        if not items:
            ttk.Label(frame, text="✓ Nothing needs attention.", foreground=c["good"], font=self.font_bold).pack(
                anchor="w")
        for severity, title, detail, label, action in items:
            row = ttk.Frame(frame, style="Card.TFrame", padding=self.px(10))
            row.pack(fill="x", pady=(0, self.px(8)))
            icon = {"bad": "✖", "warn": "⚠", "info": "ℹ"}[severity]
            color = {"bad": c["bad"], "warn": c["warn"], "info": c["accent"]}[severity]
            ttk.Label(row, text=icon, foreground=color, background=c["panel"], font=self.font_title).pack(
                side="left", padx=(0, self.px(10)))
            text = ttk.Frame(row, style="Card.TFrame")
            text.pack(side="left", fill="x", expand=True)
            ttk.Label(text, text=title, style="Card.TLabel", font=self.font_bold).pack(anchor="w")
            ttk.Label(text, text=detail, style="CardMuted.TLabel", wraplength=self.px(440), justify="left").pack(
                anchor="w")
            if label:
                ttk.Button(row, text=label, command=lambda act=action: (win.destroy(), act())).pack(
                    side="right", padx=(self.px(10), 0))
        ttk.Button(frame, text="Close", command=win.destroy).pack(anchor="e")
        win.bind("<Escape>", lambda e: win.destroy())
        self.center(win)

    def delete_leftovers(self):
        # Only folders that still hold nothing but cache files.
        doomed = []
        for a in self.leftovers:
            try:
                names = [n for _, _, files in os.walk(a.location) for n in files]
            except OSError:
                continue
            if all(ac.is_cache(n) for n in names):
                doomed.append(a)
        if not doomed:
            messagebox.showinfo("Leftover folders", "Nothing to delete.")
            return
        if not messagebox.askyesno(
                "Delete leftover folders",
                "Delete %s? They only hold cache files left behind by addons you removed.\n\n%s"
                % (ac.plural(len(doomed), "folder"), "\n".join(a.location for a in doomed[:8])
                   + ("\n..." if len(doomed) > 8 else ""))):
            return
        failed = []
        for a in doomed:
            try:
                shutil.rmtree(a.location)
            except OSError as e:
                failed.append("%s: %s" % (a.location, e.strerror))
        self.leftovers = [a for a in self.leftovers if a not in doomed or os.path.exists(a.location)]
        self.refresh_status()
        if failed:
            messagebox.showwarning("Leftover folders", "Some couldn't be deleted:\n\n" + "\n".join(failed[:8]))
        else:
            self.status.config(text="Deleted %s." % ac.plural(len(doomed), "leftover folder"))

    # ------------------------------------------------------------------ saving

    def save_order(self):
        if not self.load_order_path:
            messagebox.showerror("Can't save", "workshop.txt wasn't found.")
            return
        if not self.is_dirty():
            self.status.config(text="No changes to save.")
            return
        if not messagebox.askokcancel(
                "Save load order",
                "Close Half-Life 2 before saving, or it may overwrite the file.\n\n"
                "A backup of the current workshop.txt is made first.\n\n" + self.load_order_path):
            return
        try:
            if not self.backup_done:
                backup = self.load_order_path + time.strftime(".%Y%m%d-%H%M%S.bak")
                shutil.copy2(self.load_order_path, backup)
                self.backup_done = True
            ac.write_load_order(self.load_order_path, [(w, e) for w, e in self.order])
        except OSError as e:
            messagebox.showerror("Couldn't save", str(e))
            return
        self.saved_order = [list(e) for e in self.order]
        self.refresh_status()
        self.status.config(text="Saved.")

    def revert_order(self):
        if self.is_dirty() and messagebox.askyesno("Revert", "Go back to the last saved load order?"):
            self.set_order(self.saved_order)

    def export_report(self):
        if not self.addons:
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Export report", defaultextension=".txt",
                                            initialfile="addon_report.txt", filetypes=[("Text", "*.txt")])
        if not path:
            return
        active = [a for a in self.addons if a.rank is not None or not a.workshop_id]
        disabled = [a for a in self.addons if a.workshop_id and a.position is not None and a.rank is None]
        unlisted = [a for a in self.addons if a.workshop_id and a.position is None]
        skipped = [("Disabled addons skipped", disabled, "Turned off in the game's Add-ons menu."),
                   ("Addons missing from workshop.txt skipped", unlisted,
                    "Installed, but not in the game's list yet. Start the game once to refresh it."),
                   ("Leftover folders skipped", self.leftovers,
                    "These only hold cache files, left behind by addons you removed. Safe to delete.")]
        ac.LOAD_ORDER_NOTE = ("Assumes the addon lower in the list wins." if self.last_wins.get()
                              else "Assumes the addon higher in the list wins.")
        text = ac.format_report(active, skipped, self.containers, self.load_order_path,
                                ac.analyze(active, 1024, self.base), 0, self.base_info)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            messagebox.showerror("Export report", str(e))
            return
        self.status.config(text="Report saved to %s" % path)

    def on_close(self):
        self.save_note()
        if self.is_dirty() and not messagebox.askyesno("Quit", "Your load order changes aren't saved. Quit anyway?"):
            return
        try:
            width = self.panes.winfo_width()
            self.settings["sash"] = round(self.panes.sashpos(0) / width, 3) if width else 0.58
            self.settings["geometry"] = self.root.geometry()
        except tk.TclError:
            pass
        self.save_settings()
        self.root.destroy()

    def refresh_status(self):
        listed = [self.by_id[w] for w in self.listed_ids()]
        on = sum(1 for a in listed if a.rank is not None)
        parts = ["%d listed, %d on" % (len(listed), on), "%d do nothing" % len(self.removable)]
        broken = sum(1 for r in self.rules if self.rule_broken(r))
        if broken:
            parts.append("%s broken" % ac.plural(broken, "rule"))
        if self.base_info:
            parts.append(self.base_info)
        self.status.config(text="   ·   ".join(parts))
        dirty = self.is_dirty()
        self.dirty_label.config(text="● Unsaved changes" if dirty else "✓ Saved",
                                foreground=self.colors()["warn" if dirty else "muted"])
        self.save_btn.state(["!disabled"] if dirty else ["disabled"])
        self.undo_btn.state(["!disabled"] if self.history else ["disabled"])
        self.redo_btn.state(["!disabled"] if self.future else ["disabled"])
        problems = self.problems()
        serious = [p for p in problems if p[0] != "info"]
        self.problems_btn.config(text=("⚠ %s" % ac.plural(len(serious), "problem")) if serious else
                                 ("ℹ %s" % ac.plural(len(problems), "suggestion")) if problems else "✓ No problems")
        self.problems_btn.configure(style="Problems.TButton" if serious else "Tool.TButton")

    # ------------------------------------------------------------------ details

    def on_select(self):
        sel = self.tree.selection()
        a = self.addon_for_key(sel[0]) if len(sel) == 1 else None
        if a is not self.selected:
            self.save_note()
            self.selected = a
            self.refresh_details()
            self.highlight_relations()

    def relations(self, addon):
        """other addon -> {"wins": [paths], "loses": [paths], "reverted": [paths], "same": [paths], ...}
        The original game appears as self.base_addon, with the files this addon leaves unchanged."""
        rel = defaultdict(lambda: {"wins": [], "loses": [], "reverted": [], "same": [], "unknown": []})
        if addon.rank is None and addon.workshop_id:
            return rel  # not loaded
        for path, content in addon.files.items():
            if self.base.get(path) == content:
                rel[self.base_addon]["same"].append(path)
            copies = self.by_path.get(path, [])
            for other, size, crc in copies:
                if other is addon:
                    continue
                if (size, crc) == content:
                    rel[other]["same"].append(path)
                elif not ac.known(addon, other):
                    rel[other]["unknown"].append(path)
                elif addon.rank < other.rank:
                    rel[other]["wins"].append(path)
                elif self.base.get(path) == (size, crc):
                    rel[other]["reverted"].append(path)  # loses to a copy of the original file
                else:
                    rel[other]["loses"].append(path)
        return rel

    def file_state(self, addon, path, content):
        copies = [c for c in self.by_path.get(path, []) if c[0] is not addon]
        original = self.base.get(path) == content
        if not copies:
            return "same as the original game file" if original else "used (only this addon)"
        if not ac.known(addon, *(c[0] for c in copies)):
            return "load order unknown"
        best = ac.by_rank(copies + [(addon,) + content])[0]
        if best[0] is addon:
            others = ac.plural(len(copies), "other")
            return ("used: puts back the original file over %s" % others) if original else "used, over " + others
        if (best[1], best[2]) == content:
            return "identical copy in " + best[0].name
        if self.base.get(path) == (best[1], best[2]):
            return "undone to the original by " + best[0].name
        return "overwritten by " + best[0].name

    def rule_between(self, a, b):
        for r in self.rules:
            if {r["winner"], r["loser"]} == {a.workshop_id, b.workshop_id}:
                return r
        return None

    def show_overview(self):
        """What the details side shows when no single addon is selected."""
        for w in self.overview.winfo_children():
            w.destroy()
        self.actions.pack_forget()
        self.notes_row.pack_forget()
        self.overview.pack(fill="x", pady=(self.px(10), 0))
        listed = [self.by_id[w] for w in self.listed_ids()]
        conflicts = sum(1 for p, copies in self.by_path.items() if len({(s, c) for _, s, c in copies}) > 1)
        tiles = [(len(self.addons), "addons"), (sum(1 for a in listed if a.rank is not None), "turned on"),
                 (conflicts, "conflicting files"), (len(self.removable), "do nothing")]
        for n, (value, label) in enumerate(tiles):
            tile = ttk.Frame(self.overview, style="Card.TFrame")
            tile.grid(row=0, column=n, sticky="w", padx=(0, self.px(26)))
            ttk.Label(tile, text=str(value), style="Big.TLabel").pack(anchor="w")
            ttk.Label(tile, text=label, style="CardMuted.TLabel").pack(anchor="w")

    def refresh_details(self):
        self.conf_tree.delete(*self.conf_tree.get_children())
        self.files_tree.delete(*self.files_tree.get_children())
        a = self.selected
        self.selected_rel = {}
        if a is None:
            n = len(self.tree.selection())
            self.detail_title.config(text="%d addons selected" % n if n > 1 else "Your setup")
            self.detail_info.config(text="Select an addon to see what it conflicts with. Drag to reorder, click "
                                         "the checkbox or press Space to turn addons on or off, right-click for "
                                         "more.")
            self.show_overview()
            return
        self.overview.pack_forget()
        self.actions.pack(fill="x", pady=(self.px(8), 0))
        self.notes_row.pack(fill="x", pady=(self.px(8), 0))
        self.note_text.set(self.notes.get(self.addon_key(a), ""))
        self.detail_title.config(text=a.name)
        info = [self.status_text(a) or ("loaded, #%d" % a.position if a.position else "loaded"),
                ac.plural(len(a.files), "file"), ac.nice_size(self.sizes.get(a, 0))]
        if self.types.get(a):
            info.append(self.types[a])
        if a.workshop_id:
            info.append("Workshop %s" % a.workshop_id)
        if a.errors:
            info.append("problems: " + "; ".join(a.errors[:2]))
        self.detail_info.config(text="   ·   ".join(i for i in info if i))

        rel = self.relations(a)
        self.selected_rel = rel

        def weight(item):
            other, r = item
            conflicts = len(r["wins"]) + len(r["loses"]) + len(r["reverted"]) + len(r["unknown"])
            return other is self.base_addon, -conflicts, -len(r["same"]), other.name.lower()
        for other, r in sorted(rel.items(), key=weight):
            parts = []
            if r["loses"] or r["reverted"]:
                parts.append("loses %d" % (len(r["loses"]) + len(r["reverted"])))
            if r["wins"]:
                parts.append("wins %d" % len(r["wins"]))
            if r["unknown"]:
                parts.append("%d unknown" % len(r["unknown"]))
            if r["same"]:
                parts.append("%d identical" % len(r["same"]))
            rule = self.rule_between(a, other) if a.workshop_id and other.workshop_id else None
            rule_text = ""
            tags = []
            if rule:
                rule_text = "this wins" if rule["winner"] == a.workshop_id else "other wins"
                if self.rule_broken(rule):
                    rule_text += " ✗"
                    tags.append("broken")
            severe = sum(ac.severity(p) == 2 for p in r["wins"] + r["loses"] + r["reverted"] + r["unknown"])
            if severe:
                tags.append("severe")
                parts.append("%d [!]" % severe)
            elif r["loses"] or r["reverted"]:
                tags.append("lost")
            elif not (r["wins"] or r["unknown"]):
                tags.append("note")
            node = self.conf_tree.insert("", "end", iid="o:" + self.addon_key(other), text=other.name, tags=tags,
                                         values=(other.position or "", ", ".join(parts), rule_text))
            for kind, label in (("loses", "overwritten by it"), ("reverted", "undone to the original by it"),
                                ("wins", "overwrites it"), ("unknown", "order unknown"), ("same", "identical")):
                for path in sorted(r[kind]):
                    self.conf_tree.insert(node, "end", text=path, values=("", label, ""),
                                          tags=("severe",) if ac.severity(path) == 2 and kind != "same" else ())
        if not rel:
            self.conf_tree.insert("", "end", text="No conflicts" if (a.rank is not None or not a.workshop_id)
                                  else "Not loaded, so nothing to compare", values=("", "", ""), tags=("note",))

        for path in sorted(a.files):
            state = self.file_state(a, path, a.files[path])
            tag = "used" if state.startswith("used") else "lost" if state.startswith(("overwritten", "undone")) \
                else "note"
            self.files_tree.insert("", "end", text=path, values=(state,), tags=(tag,))

    def refresh_rules(self):
        self.rules_tree.delete(*self.rules_tree.get_children())
        for n, r in enumerate(self.rules):
            w, l = self.by_id.get(r["winner"]), self.by_id.get(r["loser"])
            broken = self.rule_broken(r)
            self.rules_tree.insert("", "end", iid=str(n), tags=("broken",) if broken else (), values=(
                w.name if w else "(not installed) " + r["winner"],
                l.name if l else "(not installed) " + r["loser"],
                "broken" if broken else ("ok" if w and l else "")))
        self.tabs.tab(3, text="Rules (%d)" % len(self.rules) if self.rules else "Rules")

    def selected_other(self):
        sel = self.conf_tree.selection()
        if not sel:
            return None
        node = sel[0]
        while self.conf_tree.parent(node):
            node = self.conf_tree.parent(node)
        return self.addon_for_key(node[2:]) if node.startswith("o:") else None

    def add_rule(self, this_wins):
        a, other = self.selected, self.selected_other()
        if not a or not other:
            messagebox.showinfo("Add a rule", "Select an addon on the left, then one of its conflicts here.")
            return
        if other is self.base_addon:
            messagebox.showinfo("Add a rule", "Addons always win over the original game files.")
            return
        if not a.workshop_id or not other.workshop_id:
            messagebox.showinfo("Add a rule", "custom/ addons aren't in the load order, so rules can't move them.")
            return
        old = self.rule_between(a, other)
        if old:
            self.rules.remove(old)
        winner, loser = (a, other) if this_wins else (other, a)
        self.rules.append({"winner": winner.workshop_id, "loser": loser.workshop_id,
                           "note": "%s > %s" % (winner.name, loser.name)})
        self.save_settings()
        self.recompute()
        if self.rule_broken(self.rules[-1]):
            self.status.config(text="Rule added. It isn't met yet: press \"Sort by rules\" to apply it.")

    def remove_rule_for_selected(self):
        a, other = self.selected, self.selected_other()
        rule = self.rule_between(a, other) if a and other else None
        if rule:
            self.rules.remove(rule)
            self.save_settings()
            self.recompute()

    def delete_rules(self):
        drop = {int(i) for i in self.rules_tree.selection()}
        if drop:
            self.rules = [r for n, r in enumerate(self.rules) if n not in drop]
            self.save_settings()
            self.recompute()

    def run_find(self):
        query = self.find_text.get().strip()
        open_paths = {self.find_tree.item(i, "text") for i in self.find_tree.get_children()
                      if self.find_tree.item(i, "open")}
        self.find_tree.delete(*self.find_tree.get_children())
        self.find_rows = {}
        if not query:
            return
        self.tabs.select(self.find_tab)
        results, total = ac.find_files(query, self.addons, self.by_path, self.base)
        if not results:
            self.find_tree.insert("", "end", text="No addon has a file matching \"%s\"" % query, tags=("note",))
            return
        expand = len(results) <= 20
        for path, summary, rows in results:
            node = self.find_tree.insert("", "end", text=path, values=("", summary),
                                         open=expand or path in open_paths,
                                         tags=("severe",) if ac.severity(path) == 2 else ())
            for a, status in rows:
                tags = ("used",) if status.startswith("USED") else (
                    ("inactive",) if status.startswith(("disabled", "not loaded")) else ())
                row = self.find_tree.insert(node, "end", text=a.name if a else self.base_addon.name,
                                            values=(a.position or "" if a else "", status),
                                            tags=tags if a else ("note",))
                if a:
                    self.find_rows[row] = a
        if total > len(results):
            self.find_tree.insert("", "end", text="... and %d more files: type more of the name" % (total - len(results)),
                                  tags=("note",))

    def jump_to(self, a):
        key = self.addon_key(a)
        if not self.tree.exists(key):
            self.reset_view()
        if self.tree.exists(key):
            self.tree.selection_set(key)
            self.tree.see(key)

    def jump_from_find(self, event):
        a = self.find_rows.get(self.find_tree.focus())
        if a:
            self.jump_to(a)
            self.tabs.select(self.find_tab)  # stay on the search results
        return "break"

    def jump_to_other(self, event):
        other = self.selected_other()
        if other and other is not self.base_addon:
            self.jump_to(other)

    # ------------------------------------------------------------------ help

    def show_help(self):
        messagebox.showinfo("How it works", (
            "The list is your Workshop load order from workshop.txt. By default the addon higher in the "
            "list wins when two addons have the same file (View > Lower in the list wins flips that).\n\n"
            "Flags: ▲ overwrites other addons, ▼ is overwritten, ✕ does nothing (every file is overwritten "
            "or identical elsewhere).\n\n"
            "Select an addon to see its conflicts. Addons it overwrites are tinted green, addons that "
            "overwrite it red.\n\n"
            "Rules (Conflicts tab) say which addon should win; Sort by rules applies them with as little "
            "movement as possible.\n\n"
            "Changes only reach the game when you Save. Close the game first; a backup of workshop.txt is "
            "made the first time you save."))


def main():
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)  # before the window exists, so text is sharp and sized right
        except Exception:
            pass
    root = tk.Tk()
    Organizer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
