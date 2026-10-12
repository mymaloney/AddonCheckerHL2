#!/usr/bin/env python3
"""
HL2 Addon Organizer

A window for Half-Life 2's Workshop addons: see which addons conflict, set
rules for which one should win, sort the load order by those rules, and save
it back to the game's workshop.txt.

    python addon_organizer.py

Needs addon_checker.py in the same folder. Python 3.7+ with tkinter (included
with the python.org installer on Windows).
"""

import json
import os
import shutil
import sys
import threading
import time
import tkinter as tk
from collections import defaultdict
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addon_checker as ac  # noqa: E402

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "addon_rules.json")

STATUS_COLORS = {
    "nothing": "#9a9a9a",  # text colour: does nothing
    "disabled": "#b0b0b0",
    "fixed": "#6a6a9a",    # custom/ and unlisted: can't be ordered
}
ROW_BACKGROUNDS = {
    "broken": "#ffd9d9",   # breaks a rule
    "mostly": "#fff3cc",
}


def stable_toposort(ids, edges, from_end=False):
    """Orders ids so every (a, b) edge has a before b, otherwise keeping the given order.
    Returns a shorter list if the edges form a cycle."""
    import heapq
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


class Organizer:
    def __init__(self, root):
        self.root = root
        root.title("HL2 Addon Organizer")
        root.geometry("1300x760")
        root.minsize(900, 500)

        self.addons = []          # every installed addon with content
        self.by_id = {}           # workshop id -> Addon
        self.order = []           # [[id, enabled]] as in workshop.txt, including uninstalled ids
        self.saved_order = []
        self.load_order_path = None
        self.game_dir = None      # chosen in the Folders window; None = find automatically
        self.workshop_dir = None
        self.found_game = self.found_workshop = None  # what the last scan used
        self.base = {}            # the original game's files: path -> (size, crc)
        self.base_info = ""
        # Stands in for the original game in the Conflicts tab.
        self.base_addon = ac.Addon(name="Half-Life 2 (original game files)", location="(base game)")
        self.leftovers = []
        self.rules = []           # [{"winner": id, "loser": id}]
        self.last_wins = tk.BooleanVar(value=False)
        self.filter_text = tk.StringVar()
        self.find_text = tk.StringVar()
        self.find_rows = {}       # result row id -> Addon, for jumping to it
        self.selected = None      # Addon shown on the right
        self.backup_done = False
        self.removable = set()
        self.idle = {}
        self.by_path = {}
        self.stats = {}

        self.load_settings()
        self.build_ui()
        self.filter_text.trace_add("write", lambda *_: self.refresh_list())
        self.last_wins.trace_add("write", lambda *_: self.on_direction_changed())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_scan()

    # ------------------------------------------------------------------ settings

    def load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.rules = [r for r in data.get("rules", []) if r.get("winner") and r.get("loser")]
            self.last_wins.set(bool(data.get("last_wins", False)))
            self.game_dir = data.get("game_dir") or None
            self.workshop_dir = data.get("workshop_dir") or None
        except (OSError, ValueError):
            pass

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({"game_dir": self.game_dir, "workshop_dir": self.workshop_dir,
                           "last_wins": self.last_wins.get(), "rules": self.rules}, f, indent=2)
        except OSError as e:
            messagebox.showerror("Couldn't save rules", "%s\n%s" % (SETTINGS_FILE, e))

    # ------------------------------------------------------------------ layout

    def build_ui(self):
        bar = ttk.Frame(self.root, padding=(6, 6, 6, 0))
        bar.pack(fill="x")
        self.save_btn = ttk.Button(bar, text="Save load order", command=self.save_order)
        self.save_btn.pack(side="left")
        ttk.Button(bar, text="Undo changes", command=self.revert_order).pack(side="left", padx=(4, 12))
        ttk.Button(bar, text="Sort by rules", command=self.sort_by_rules).pack(side="left")
        ttk.Button(bar, text="Move removable addons to the losing end",
                   command=self.move_removable).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="Lower in the list wins", variable=self.last_wins).pack(side="left", padx=12)
        ttk.Button(bar, text="Rescan", command=self.start_scan).pack(side="right")
        ttk.Button(bar, text="Folders...", command=self.choose_folders).pack(side="right", padx=4)

        panes = ttk.PanedWindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=6, pady=6)

        # Left: load order
        left = ttk.Frame(panes)
        panes.add(left, weight=3)
        top = ttk.Frame(left)
        top.pack(fill="x")
        ttk.Label(top, text="Load order").pack(side="left")
        ttk.Label(top, text="   Filter:").pack(side="left")
        ttk.Entry(top, textvariable=self.filter_text, width=30).pack(side="left", fill="x", expand=True)

        cols = ("pos", "on", "name", "files", "wins", "loses", "status")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for col, text, width, anchor in [("pos", "#", 44, "e"), ("on", "On", 34, "center"),
                                         ("name", "Addon", 250, "w"), ("files", "Files", 60, "e"),
                                         ("wins", "Wins", 56, "e"), ("loses", "Loses", 56, "e"),
                                         ("status", "Status", 170, "w")]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "name"))
        for tag, color in STATUS_COLORS.items():
            self.tree.tag_configure(tag, foreground=color)
        for tag, color in ROW_BACKGROUNDS.items():
            self.tree.tag_configure(tag, background=color)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        self.tree.bind("<ButtonPress-1>", self.on_drag_start)
        self.tree.bind("<B1-Motion>", self.on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self.on_drag_end)
        self.tree.bind("<Double-1>", lambda e: self.toggle_enabled())
        self.tree.bind("<space>", lambda e: self.toggle_enabled())
        self.drag = None

        buttons = ttk.Frame(left)
        buttons.pack(side="right", fill="y", padx=(4, 0))
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        for text, cmd in [("Top", lambda: self.move_selected("top")), ("Up", lambda: self.move_selected(-1)),
                          ("Down", lambda: self.move_selected(1)), ("Bottom", lambda: self.move_selected("bottom")),
                          (None, None), ("On / Off", self.toggle_enabled)]:
            if text is None:
                ttk.Separator(buttons).pack(fill="x", pady=6)
            else:
                ttk.Button(buttons, text=text, command=cmd).pack(pady=2, fill="x")

        # Right: details for the selected addon
        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        self.detail_title = ttk.Label(right, text="Select an addon", font=("TkDefaultFont", 11, "bold"),
                                      wraplength=480, justify="left")
        self.detail_title.pack(fill="x")
        self.detail_info = ttk.Label(right, text="", wraplength=480, justify="left")
        self.detail_info.pack(fill="x", pady=(2, 6))

        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)

        # Conflicts tab
        conf = ttk.Frame(self.tabs)
        self.tabs.add(conf, text="Conflicts")
        ccols = ("pos", "result", "rule")
        self.conf_tree = ttk.Treeview(conf, columns=ccols, show="tree headings", selectmode="browse")
        self.conf_tree.heading("#0", text="Other addon / file")
        self.conf_tree.heading("pos", text="#")
        self.conf_tree.heading("result", text="Result")
        self.conf_tree.heading("rule", text="Rule")
        self.conf_tree.column("#0", width=200, minwidth=120)
        self.conf_tree.column("pos", width=36, anchor="e", stretch=False)
        self.conf_tree.column("result", width=125, minwidth=100, stretch=False)
        self.conf_tree.column("rule", width=96, stretch=False)
        self.conf_tree.tag_configure("broken", background=ROW_BACKGROUNDS["broken"])
        self.conf_tree.tag_configure("severe", foreground="#b00000")
        cs = ttk.Scrollbar(conf, orient="vertical", command=self.conf_tree.yview)
        self.conf_tree.configure(yscrollcommand=cs.set)
        rule_bar = ttk.Frame(conf)
        rule_bar.pack(side="bottom", fill="x", pady=4)
        cs.pack(side="right", fill="y")
        self.conf_tree.pack(side="left", fill="both", expand=True)
        self.conf_tree.bind("<Double-1>", self.jump_to_other)
        ttk.Button(rule_bar, text="This addon should win", command=lambda: self.add_rule(True)).pack(side="left")
        ttk.Button(rule_bar, text="The other should win", command=lambda: self.add_rule(False)).pack(side="left", padx=4)
        ttk.Button(rule_bar, text="Remove rule", command=self.remove_rule_for_selected).pack(side="left")

        # Files tab
        files = ttk.Frame(self.tabs)
        self.tabs.add(files, text="Files")
        self.files_tree = ttk.Treeview(files, columns=("state",), show="tree headings", selectmode="browse")
        self.files_tree.heading("#0", text="File")
        self.files_tree.heading("state", text="In game")
        self.files_tree.column("#0", width=240, minwidth=120)
        self.files_tree.column("state", width=220, minwidth=120)
        fs = ttk.Scrollbar(files, orient="vertical", command=self.files_tree.yview)
        self.files_tree.configure(yscrollcommand=fs.set)
        fs.pack(side="right", fill="y")
        self.files_tree.pack(side="left", fill="both", expand=True)

        # Find file tab: which addons change a file, e.g. every file of one map
        find = ttk.Frame(self.tabs)
        self.tabs.add(find, text="Find file")
        self.find_tab = find
        top = ttk.Frame(find)
        top.pack(fill="x", pady=(4, 2))
        ttk.Label(top, text="File:").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.find_text)
        entry.pack(side="left", fill="x", expand=True, padx=4)
        entry.bind("<Return>", lambda e: self.run_find())
        ttk.Button(top, text="Search", command=self.run_find).pack(side="left")
        ttk.Label(find, text="Part of a path, e.g. a map name like d1_town_02, or a pattern like maps/d1_town_*",
                  foreground="#777777").pack(anchor="w")
        self.find_tree = ttk.Treeview(find, columns=("pos", "state"), show="tree headings", selectmode="browse")
        self.find_tree.heading("#0", text="File / addon")
        self.find_tree.heading("pos", text="#")
        self.find_tree.heading("state", text="In game")
        self.find_tree.column("#0", width=250, minwidth=120)
        self.find_tree.column("pos", width=36, anchor="e", stretch=False)
        self.find_tree.column("state", width=200, minwidth=120)
        self.find_tree.tag_configure("used", foreground="#006400")
        self.find_tree.tag_configure("inactive", foreground=STATUS_COLORS["disabled"])
        self.find_tree.tag_configure("severe", foreground="#b00000")
        self.find_tree.tag_configure("note", foreground="#777777")
        ns = ttk.Scrollbar(find, orient="vertical", command=self.find_tree.yview)
        self.find_tree.configure(yscrollcommand=ns.set)
        ns.pack(side="right", fill="y")
        self.find_tree.pack(side="left", fill="both", expand=True)
        self.find_tree.bind("<Double-1>", self.jump_from_find)

        # Rules tab
        rules = ttk.Frame(self.tabs)
        self.tabs.add(rules, text="All rules")
        self.rules_tree = ttk.Treeview(rules, columns=("winner", "loser", "ok"), show="headings", selectmode="extended")
        self.rules_tree.heading("winner", text="Should win")
        self.rules_tree.heading("loser", text="Over")
        self.rules_tree.heading("ok", text="Now")
        self.rules_tree.column("ok", width=70, stretch=False)
        self.rules_tree.tag_configure("broken", background=ROW_BACKGROUNDS["broken"])
        self.rules_tree.pack(fill="both", expand=True)
        ttk.Button(rules, text="Delete selected rules", command=self.delete_rules).pack(anchor="w", pady=4)

        self.status = ttk.Label(self.root, text="", anchor="w", padding=(6, 0, 6, 4))
        self.status.pack(fill="x")

    # ------------------------------------------------------------------ scanning

    def start_scan(self, confirm=True):
        if confirm and self.is_dirty() and not messagebox.askyesno(
                "Rescan", "Rescanning discards your unsaved load order changes. Continue?"):
            return
        self.status.config(text="Finding Half-Life 2...")
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self.scan_thread, daemon=True).start()

    def scan_thread(self):
        def progress(i, total):
            self.root.after(0, lambda: self.status.config(text="Reading addon %d of %d..." % (i, total)))
        try:
            game = ac.resolve_game_dir(self.game_dir)
            workshop = ac.resolve_workshop_dir(self.workshop_dir)
            if not game and not workshop:
                game, workshop = ac.find_hl2_dirs()
            # One folder is enough to find the other in the same Steam library.
            workshop = workshop or (game and ac.workshop_dir_for_game(game))
            game = game or (workshop and ac.game_dir_for_workshop(workshop))
            containers, path = ac.game_folders(game, workshop)
            addons = ac.scan(containers, progress, workshop_dirs=[workshop] if workshop else []) if containers else []
            ids = {a.workshop_id for a in addons if a.workshop_id}
            if ids:
                self.root.after(0, lambda: self.status.config(text="Looking up Workshop titles..."))
                titles = ac.fetch_workshop_titles(ids)
                for a in addons:
                    if a.workshop_id in titles:
                        a.name = titles[a.workshop_id]
                    elif a.workshop_id:
                        a.name = "Workshop item %s" % a.workshop_id
            entries = ac.read_load_order(path) if path else []
            base, base_info = {}, "original game files not checked (Half-Life 2 folder not found)"
            if game:
                self.root.after(0, lambda: self.status.config(text="Reading the original game files..."))
                base, vpk_count, _, _ = ac.read_base_game(game)
                base_info = ("compared with %d original game files" % len(base) if vpk_count
                             else "no original game VPKs found, so files weren't compared with them")
        except Exception as e:  # report anything unexpected instead of dying silently
            message = str(e)
            self.root.after(0, lambda: messagebox.showerror("Scan failed", message))
            return
        self.root.after(0, lambda: self.scan_done(game, workshop, addons, path, entries, base, base_info))

    def scan_done(self, game, workshop, addons, path, entries, base=None, base_info=""):
        self.base, self.base_info = base or {}, base_info
        self.found_game, self.found_workshop = game, workshop
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
        self.load_order_path = path
        self.order = [[wsid, enabled] for wsid, enabled in entries]
        self.saved_order = [list(e) for e in self.order]
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

    def refresh_list(self):
        keep = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        needle = self.filter_text.get().strip().lower()
        broken = self.broken_ids()

        rows = [self.by_id[w] for w in self.listed_ids()]
        rows += sorted((a for a in self.addons if a.workshop_id and a.position is None), key=lambda a: a.name.lower())
        rows += [a for a in self.addons if not a.workshop_id]
        for a in rows:
            if needle and needle not in a.name.lower() and needle not in (a.workshop_id or ""):
                continue
            status = self.status_text(a)
            wins, loses = self.stats.get(a, (0, 0))
            tags = []
            if a.workshop_id in broken:
                tags.append("broken")
                status = ("rule broken; " + status).rstrip("; ")
            elif status.startswith("mostly"):
                tags.append("mostly")
            if status.endswith("does nothing"):
                tags.append("nothing")
            elif status == "disabled":
                tags.append("disabled")
            elif a.position is None:
                tags.append("fixed")
            on = "" if a.position is None else ("✔" if a.rank is not None else "–")
            self.tree.insert("", "end", iid=self.addon_key(a), tags=tags, values=(
                a.position or "", on, a.name, len(a.files), wins or "", loses or "", status))
        for key in keep:
            if self.tree.exists(key):
                self.tree.selection_add(key)

    # ------------------------------------------------------------------ order editing

    def movable_selection(self):
        return [k for k in self.tree.selection() if k in self.by_id and self.by_id[k].position is not None]

    def is_dirty(self):
        return self.order != self.saved_order

    def apply_order(self, ids_in_order):
        """ids_in_order: the installed, listed ids in their new order. Uninstalled ids keep their slots."""
        it = iter(ids_in_order)
        enabled = {w: e for w, e in self.order}
        self.order = [[next(it), None] if w in self.by_id else [w, e] for w, e in self.order]
        for entry in self.order:
            if entry[1] is None:
                entry[1] = enabled[entry[0]]
        self.recompute()

    def move_selected(self, where):
        if self.filter_text.get().strip():
            self.filter_text.set("")  # moving within a filtered view would be confusing
        sel = self.movable_selection()
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

    def toggle_enabled(self):
        sel = self.movable_selection()
        if not sel:
            return "break"
        turn_on = not all(e for w, e in self.order if w in sel)
        for entry in self.order:
            if entry[0] in sel:
                entry[1] = turn_on
        self.recompute()
        return "break"

    def on_drag_start(self, event):
        row = self.tree.identify_row(event.y)
        self.drag = row if row in self.by_id and self.by_id[row].position is not None else None
        self.drag_moved = False

    def on_drag_motion(self, event):
        if not self.drag or self.filter_text.get().strip():
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
                                 + "\n".join(stuck[:15]) + "\n\nDelete one of the rules in the \"All rules\" tab.")
            return
        if new == ids:
            messagebox.showinfo("Sort by rules", "The load order already follows every rule.")
            return
        self.apply_order(new)
        self.status.config(text="Sorted by rules. Not saved yet.")

    def move_removable(self):
        ids = self.listed_ids()
        idle = [w for w in ids if self.by_id[w] in self.removable]
        if not idle:
            messagebox.showinfo("Nothing to move", "No addons are currently doing nothing.")
            return
        rest = [w for w in ids if w not in idle]
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

    # ------------------------------------------------------------------ folders

    def choose_folders(self, message=None):
        win = tk.Toplevel(self.root)
        win.title("Half-Life 2 folders")
        win.transient(self.root)
        win.resizable(True, False)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        row = 0
        if message:
            ttk.Label(frame, text=message, foreground="#b00000").grid(row=0, column=0, columnspan=3, sticky="w",
                                                                      pady=(0, 8))
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
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
            ttk.Entry(frame, textvariable=var, width=70).grid(row=row, column=1, sticky="ew")
            ttk.Button(frame, text="Browse...", command=lambda v=var, t=label: browse(v, "Choose the " + t)
                       ).grid(row=row, column=2, padx=(6, 0))
            ttk.Label(frame, text=hint, foreground="#777777").grid(row=row + 1, column=1, sticky="w", pady=(0, 8))
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
        buttons.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Button(buttons, text="Detect automatically", command=detect).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", command=ok).pack(side="right", padx=6)
        win.bind("<Return>", lambda e: ok())
        win.bind("<Escape>", lambda e: win.destroy())
        win.grab_set()
        return win

    # ------------------------------------------------------------------ saving

    def save_order(self):
        if not self.load_order_path:
            messagebox.showerror("Can't save", "workshop.txt wasn't found.")
            return
        if not self.is_dirty():
            messagebox.showinfo("Save", "No changes to save.")
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

    def revert_order(self):
        if self.is_dirty() and messagebox.askyesno("Undo changes", "Go back to the last saved load order?"):
            self.order = [list(e) for e in self.saved_order]
            self.recompute()

    def on_close(self):
        if self.is_dirty() and not messagebox.askyesno("Quit", "Your load order changes aren't saved. Quit anyway?"):
            return
        self.root.destroy()

    def refresh_status(self):
        listed = [self.by_id[w] for w in self.listed_ids()]
        parts = ["%d addons in load order" % len(listed),
                 "%d do nothing" % len(self.removable)]
        broken = sum(1 for r in self.rules if self.rule_broken(r))
        if broken:
            parts.append("%d rules broken (Sort by rules fixes them)" % broken)
        unlisted = sum(1 for a in self.addons if a.workshop_id and a.position is None)
        if unlisted:
            parts.append("%d not in workshop.txt yet" % unlisted)
        if self.leftovers:
            parts.append("%d leftover folders from removed addons" % len(self.leftovers))
        if self.base_info:
            parts.append(self.base_info)
        parts.append("UNSAVED CHANGES" if self.is_dirty() else "saved")
        self.status.config(text="   |   ".join(parts))
        self.save_btn.state(["!disabled"] if self.is_dirty() else ["disabled"])

    # ------------------------------------------------------------------ details

    def on_select(self):
        sel = self.tree.selection()
        a = self.addon_for_key(sel[0]) if len(sel) == 1 else None
        if a is not self.selected:
            self.selected = a
            self.refresh_details()

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

    def refresh_details(self):
        self.conf_tree.delete(*self.conf_tree.get_children())
        self.files_tree.delete(*self.files_tree.get_children())
        a = self.selected
        if a is None:
            self.detail_title.config(text="Select an addon")
            self.detail_info.config(text="Drag addons to reorder them, or use the buttons. "
                                         "Double-click or press Space to turn an addon on or off.")
            return
        self.detail_title.config(text=a.name)
        info = [self.status_text(a) or "loaded", ac.plural(len(a.files), "file")]
        if a.workshop_id:
            info.append("Workshop id %s" % a.workshop_id)
        if a.errors:
            info.append("problems: " + "; ".join(a.errors[:2]))
        self.detail_info.config(text="   ·   ".join(i for i in info if i))

        rel = self.relations(a)
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
            node = self.conf_tree.insert("", "end", iid="o:" + self.addon_key(other), text=other.name, tags=tags,
                                         values=(other.position or "", ", ".join(parts), rule_text))
            for kind, label in (("loses", "overwritten by it"), ("reverted", "undone to the original by it"),
                                ("wins", "overwrites it"), ("unknown", "order unknown"), ("same", "identical")):
                for path in sorted(r[kind]):
                    self.conf_tree.insert(node, "end", text=path, values=("", label, ""),
                                          tags=("severe",) if ac.severity(path) == 2 and kind != "same" else ())
        if not rel:
            self.conf_tree.insert("", "end", text="No conflicts" if (a.rank is not None or not a.workshop_id)
                                  else "Not loaded, so nothing to compare", values=("", "", ""))

        for path in sorted(a.files):
            self.files_tree.insert("", "end", text=path, values=(self.file_state(a, path, a.files[path]),))

    def refresh_rules(self):
        self.rules_tree.delete(*self.rules_tree.get_children())
        for n, r in enumerate(self.rules):
            w, l = self.by_id.get(r["winner"]), self.by_id.get(r["loser"])
            broken = self.rule_broken(r)
            self.rules_tree.insert("", "end", iid=str(n), tags=("broken",) if broken else (), values=(
                w.name if w else "(not installed) " + r["winner"],
                l.name if l else "(not installed) " + r["loser"],
                "broken" if broken else ("ok" if w and l else "")))

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

    def jump_from_find(self, event):
        a = self.find_rows.get(self.find_tree.focus())
        if a and self.tree.exists(self.addon_key(a)):
            self.filter_text.set("")
            key = self.addon_key(a)
            self.tree.selection_set(key)
            self.tree.see(key)
            self.tabs.select(self.find_tab)  # stay on the search results
        return "break"

    def jump_to_other(self, event):
        other = self.selected_other()
        if other and self.tree.exists(self.addon_key(other)):
            self.filter_text.set("")
            key = self.addon_key(other)
            self.tree.selection_set(key)
            self.tree.see(key)


def main():
    root = tk.Tk()
    try:
        if sys.platform == "win32":
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
    except Exception:
        pass
    Organizer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
