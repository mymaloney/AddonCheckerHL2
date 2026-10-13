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
import re
import shutil
import stat
import subprocess
import sys
import threading
import tempfile
import time
import tkinter as tk
import webbrowser
import zipfile
from collections import Counter, defaultdict
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addon_checker as ac  # noqa: E402
import addon_scripts  # noqa: E402

SETTINGS_FILE = os.path.join(ac.app_dir(), "addon_rules.json")  # next to the .py or the .exe
KEEP_BACKUPS = 10  # workshop.txt backups kept; older ones are removed when saving
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


def edges_in_cycles(edges):
    """The (a, b) edges that are part of a cycle, i.e. a can be reached again from b."""
    after = defaultdict(set)
    for a, b in edges:
        after[a].add(b)

    def reaches(start, goal):
        seen, todo = set(), [start]
        while todo:
            w = todo.pop()
            if w == goal:
                return True
            if w not in seen:
                seen.add(w)
                todo.extend(after[w])
        return False
    return {(a, b) for a, b in edges if reaches(b, a)}


def without_repeats(entries):
    """[[id, enabled]] with each id once. The last entry for an id is the one that counts,
    as in addon_checker.py, so that's the one kept."""
    seen, out = set(), []
    for e in reversed(entries):
        if e[0] not in seen:
            seen.add(e[0])
            out.append(e)
    return out[::-1]


def unique_path(stem, ext):
    """stem + ext, or stem-2 + ext, stem-3 + ext... whichever doesn't exist yet."""
    path, n = stem + ext, 1
    while os.path.exists(path):
        n += 1
        path = "%s-%d%s" % (stem, n, ext)
    return path


def is_link(path):
    """Symlinks, and Windows junctions, which os.path.islink only reports from Python 3.12 on."""
    try:
        st = os.lstat(path)
    except OSError:
        return True  # can't tell, so treat it as something to keep
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def only_cache_files(folder):
    """True if folder holds nothing but cache files and empty folders. A link counts as content, and a
    subfolder that can't be read makes it False, so deleting the folder can never remove real files."""
    if is_link(folder):
        return False
    errors = []
    for dirpath, dirs, names in os.walk(folder, onerror=errors.append):
        if any(is_link(os.path.join(dirpath, n)) for n in dirs + names):
            return False
        if not all(ac.is_cache(n) for n in names):
            return False
    return not errors


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


# ---------------------------------------------------------------------------
# Installing custom/ addons
# ---------------------------------------------------------------------------

# Folders that hold game content: an addon folder has some of these at its top.
CONTENT_DIRS = {"materials", "models", "sound", "maps", "scripts", "resource", "cfg", "particles", "scenes",
                "media", "expressions", "shaders", "sounds"}


class InstallError(Exception):
    pass


# The game loads everything inside custom/, so a turned-off custom/ addon is moved next to it, here.
DISABLED_FOLDER = "custom_disabled"


def disabled_dir(custom_dir):
    return os.path.join(os.path.dirname(custom_dir), DISABLED_FOLDER)


def addon_root(folder):
    """An addon is often wrapped in one more folder ("myaddon/myaddon/materials"): skip such
    wrappers, down to the folder that holds the content."""
    while True:
        try:
            names = os.listdir(folder)
        except OSError as e:
            raise InstallError("can't open %s: %s" % (folder, e.strerror or e))
        dirs = [n for n in names if os.path.isdir(os.path.join(folder, n))]
        files = [n for n in names if not os.path.isdir(os.path.join(folder, n))]
        if len(dirs) == 1 and dirs[0].lower() not in CONTENT_DIRS and \
                not any(f.lower().endswith(".vpk") for f in files):
            folder = os.path.join(folder, dirs[0])  # only readmes or nothing next to it
            continue
        return folder


def vpk_parts(path):
    """A dropped VPK and the files that belong with it: foo_dir.vpk comes with foo_000.vpk, foo_001.vpk...
    (dropping a part brings the whole set)."""
    folder, name = os.path.split(path)
    m = re.match(r"(.*)_(?:dir|\d{3})\.vpk$", name, re.IGNORECASE)
    if not m:
        return [path]
    stem = m.group(1).lower()
    parts = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
             if re.match(re.escape(stem) + r"_(?:dir|\d{3})\.vpk$", n, re.IGNORECASE)]
    if not any(p.lower().endswith("_dir.vpk") for p in parts):
        raise InstallError("%s is only part of an addon: its %s_dir.vpk isn't next to it" % (name, m.group(1)))
    return parts


def find_7zip():
    """The command to run 7-Zip, which unpacks .7z and .rar (Python can't), or None."""
    for name in ("7z", "7za", "7zz"):
        exe = shutil.which(name)
        if exe:
            return [exe]
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        exe = os.path.join(os.environ.get(var, ""), "7-Zip", "7z.exe")
        if os.environ.get(var) and os.path.isfile(exe):
            return [exe]
    return None


def unpack_7zip(source, target):
    name = os.path.basename(source)
    command = find_7zip()
    if not command:
        raise InstallError("%s: install 7-Zip (7-zip.org) to install .7z and .rar files here, or unpack it "
                           "yourself and drop the folder" % name)
    os.makedirs(target)
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: no console flashing up
    try:
        done = subprocess.run(command + ["x", "-y", "-o" + target, source], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, creationflags=flags, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        raise InstallError("can't unpack %s: %s" % (name, e))
    if done.returncode:
        output = [l.strip() for l in done.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        errors = [l for l in output if "error" in l.lower()]
        raise InstallError("can't unpack %s: %s" % (name, errors[-1] if errors else "7-Zip couldn't read it"))
    # 7-Zip keeps paths inside the folder, but check anyway, and refuse links: they could point anywhere.
    real = os.path.realpath(target)
    for dirpath, dirs, names in os.walk(target):
        for n in dirs + names:
            p = os.path.join(dirpath, n)
            if os.path.islink(p) or not os.path.realpath(p).startswith(real + os.sep):
                raise InstallError("%s has a file outside its own folder (%s); not installed" % (name, n))


def unpacked_addon(target, name):
    """What to install from an unpacked archive: [(name in custom/, path)]."""
    root = addon_root(target)
    entries = os.listdir(root)
    vpks = [n for n in entries if n.lower().endswith(".vpk")]
    if vpks and len(vpks) == len(entries) and not any(n.lower().startswith("pak01_") for n in vpks):
        return [(n, os.path.join(root, n)) for n in sorted(vpks)]  # loose VPKs go in custom/ itself
    top = os.path.basename(root) if root != target else os.path.splitext(name)[0]
    return [(top, root)]


def addon_files(location):
    """The files on disk that make up a custom/ addon: its folder or VPK, and a foo_dir.vpk's parts."""
    if os.path.isfile(location) and re.search(r"_dir\.vpk$", location, re.IGNORECASE):
        return vpk_parts(location)
    return [location]


def moves_for(location, dest):
    """[(from, to)] to move or rename an addon from location to dest; a foo_dir.vpk's parts follow it."""
    files = addon_files(location)
    if len(files) == 1:
        return [(location, dest)]
    stem = re.match(r"(.*)_dir\.vpk$", os.path.basename(location), re.IGNORECASE).group(1)
    m = re.match(r"(.*)_dir\.vpk$", os.path.basename(dest), re.IGNORECASE)
    new_stem = m.group(1) if m else os.path.splitext(os.path.basename(dest))[0]
    return [(f, os.path.join(os.path.dirname(dest), new_stem + os.path.basename(f)[len(stem):])) for f in files]


def move_files(moves):
    """Moves every (from, to) or none: never overwrites, and puts back what moved if one fails."""
    for _, dest in moves:
        if os.path.lexists(dest):
            raise OSError("%s is already there" % os.path.basename(dest))
    done = []
    try:
        for src, dest in moves:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(src, dest)
            done.append((src, dest))
    except OSError:
        for src, dest in reversed(done):
            try:
                os.replace(dest, src)
            except OSError:
                pass
        raise


def send_to_trash(path):
    """To the Recycle Bin (Windows) or the desktop's Trash, so it can be restored. Raises OSError
    if that isn't possible; never deletes for good."""
    path = os.path.abspath(path)
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        # pFrom is a list of paths ending in two nulls, which a c_wchar_p field won't take: a raw buffer.
        fields = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                  ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                  ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
        attrs = {"_fields_": fields}
        if ctypes.sizeof(ctypes.c_void_p) == 4:
            attrs = {"_pack_": 1, "_fields_": fields}  # 32-bit shellapi.h packs this structure
        op_type = type("SHFILEOPSTRUCTW", (ctypes.Structure,), attrs)
        # FO_DELETE with ALLOWUNDO (Recycle Bin), NOCONFIRMATION, SILENT, NOERRORUI, WANTNUKEWARNING
        # (asks instead of deleting for good where there's no Recycle Bin, e.g. a network drive).
        names = ctypes.create_unicode_buffer(path + "\0")  # the buffer adds the second null
        op = op_type(None, 3, ctypes.cast(names, ctypes.c_void_p), None,
                     0x0040 | 0x0010 | 0x0004 | 0x0400 | 0x4000, False, None, None)
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if result or op.fAnyOperationsAborted or os.path.lexists(path):
            raise OSError("couldn't move %s to the Recycle Bin (error %d)" % (os.path.basename(path), result))
        return
    for command in (["gio", "trash"], ["trash-put"], ["kioclient5", "move"]):
        exe = shutil.which(command[0])
        if not exe:
            continue
        args = [exe] + command[1:] + [path] + (["trash:/"] if command[0] == "kioclient5" else [])
        if subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0 \
                and not os.path.lexists(path):
            return
    raise OSError("couldn't move %s to the Trash: no Trash found on this system" % os.path.basename(path))


def prune_backups(path, keep=KEEP_BACKUPS):
    """Removes all but the newest `keep` workshop.txt.<date>-<time>.bak backups made by the organizer."""
    folder, name = os.path.split(path)
    try:
        found = [os.path.join(folder, n) for n in os.listdir(folder)
                 if n.startswith(name + ".") and n.endswith(".bak") and re.match(r"\.\d{8}-\d{6}", n[len(name):])]
        found.sort(key=lambda p: (os.path.getmtime(p), p))
    except OSError:
        return
    for old in found[:-keep] if keep else found:
        try:
            os.remove(old)
        except OSError:
            pass


def plan_install(source, workdir):
    """What dropping source installs: [(name in custom/, path to copy)]. A .zip is unpacked into workdir.
    Raises InstallError for things that aren't addons."""
    name = os.path.basename(os.path.normpath(source))
    lower = name.lower()
    if os.path.isdir(source):
        root = addon_root(source)
        return [(name, root)]
    if lower.endswith(".vpk"):
        return [(os.path.basename(p), p) for p in vpk_parts(source)]
    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(source) as z:
                for member in z.namelist():
                    parts = member.replace("\\", "/").split("/")
                    if member.startswith(("/", "\\")) or ":" in parts[0] or ".." in parts:
                        raise InstallError("%s has a file outside its own folder (%s); not installed" % (name, member))
                target = os.path.join(workdir, "unpacked")
                z.extractall(target)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as e:
            raise InstallError("can't unpack %s: %s" % (name, e))
        return unpacked_addon(target, name)
    if lower.endswith((".7z", ".rar")):
        target = os.path.join(workdir, "unpacked")
        unpack_7zip(source, target)
        return unpacked_addon(target, name)
    raise InstallError("%s isn't an addon: drop a folder, a .vpk, a .zip, a .7z or a .rar" % name)


def copy_into(src, dest):
    """Copies a file or folder to dest through a temporary name, so a failed copy leaves nothing half done."""
    tmp = dest + ".installing"
    if os.path.lexists(tmp):
        shutil.rmtree(tmp) if os.path.isdir(tmp) else os.remove(tmp)
    try:
        if os.path.isdir(src):
            shutil.copytree(src, tmp)
        else:
            shutil.copy2(src, tmp)
        if os.path.lexists(dest):
            shutil.rmtree(dest) if os.path.isdir(dest) and not os.path.islink(dest) else os.remove(dest)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.lexists(tmp):
            shutil.rmtree(tmp, ignore_errors=True) if os.path.isdir(tmp) else os.remove(tmp)
        raise


def enable_file_drop(root, callback):
    """Windows: files and folders dropped on the window from Explorer call callback([paths]).
    Tk has no file drop of its own, so this listens for the WM_DROPFILES message Windows sends.
    Returns False where that isn't possible."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        set_proc = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW  # 32-bit Python
        set_proc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        set_proc.restype = ctypes.c_void_p
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM]
        user32.CallWindowProcW.restype = LRESULT
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragFinish.argtypes = [ctypes.c_void_p]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        WM_DROPFILES, GWLP_WNDPROC = 0x0233, -4

        root.update_idletasks()
        hwnd = int(root.wm_frame(), 16)  # the top-level window around Tk's own

        def proc(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                paths = []
                for i in range(shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)):
                    size = shell32.DragQueryFileW(wparam, i, None, 0) + 1
                    buf = ctypes.create_unicode_buffer(size)
                    shell32.DragQueryFileW(wparam, i, buf, size)
                    paths.append(buf.value)
                shell32.DragFinish(wparam)
                root.after(0, lambda: callback(paths))  # outside this window message
                return 0
            return user32.CallWindowProcW(old, h, msg, wparam, lparam)

        new = WNDPROC(proc)
        old = set_proc(hwnd, GWLP_WNDPROC, ctypes.cast(new, ctypes.c_void_p))
        if not old:
            return False
        root._file_drop = (new, old)  # the callback must live as long as the window
        shell32.DragAcceptFiles(hwnd, True)
        return True
    except Exception:  # an unusual Windows or Python: install through the button instead
        return False


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
        root.title("HL2 Addon Organizer %s" % ac.__version__)
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
        self.custom_dirs = []     # the custom/ folders the game loads, in its order
        self.file_undo = []       # [(addon, old location, old custom_off)]: moves and renames on disk
        self.strays = []          # in the Workshop folder but not item folders: never loaded
        self.empty_folders = []   # custom/ folders with no addon files
        self.together = {}        # removable addon -> the others it may only be removed with
        self.rules = []           # [{"winner": id, "loser": id}]
        self.notes = {}           # addon key -> text
        self.profiles = {}        # name -> [[id, enabled]]
        self.settings = {}
        self.types, self.sizes = {}, {}
        # The addon that loads later wins (confirmed in game): lower in workshop.txt beats higher,
        # and every Workshop addon beats the custom/ ones, which load first.
        self.last_wins = tk.BooleanVar(value=True)
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
        self.disk_entries = None  # workshop.txt as last read or written here: [(id, enabled)]
        self.disk_ours = False    # whether that version was written here (then saving needs no backup)
        self.scanning = False
        self.rescan_pending = False  # Rescan or new folders while a scan runs: scan again when it ends
        self.removable = set()
        self.idle = {}
        self.by_path = {}
        self.indexed = None       # the set of active addons by_path was built from
        self.conflict_paths = []  # [(path, copies)] with more than one version of the file
        self.stats = {}
        self.recompute_due = False
        self.list_rebuilt, self.settle_job = False, None  # see the end of refresh_list
        self.note_key = None      # addon whose note is in the Notes box
        self.drag = None          # ids being dragged, in load order
        self.drag_row = None      # the row the drag started on
        self.drag_plan = None     # the listed ids as they'd be if dropped now

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
        self.file_drop = enable_file_drop(self.root, self.install_paths)
        self.poll_ui()
        self.filter_text.trace_add("write", lambda *_: self.refresh_list())
        self.view.trace_add("write", lambda *_: self.refresh_list())
        self.last_wins.trace_add("write", lambda *_: self.on_direction_changed())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.settings_problem:
            messagebox.showwarning("Settings", self.settings_problem)
        self.start_scan()

    # ------------------------------------------------------------------ settings

    def load_settings(self):
        self.settings_problem = None     # told to the user once the window is up
        self.keep_settings_file = False  # the file couldn't be read: never overwrite it
        name = os.path.basename(SETTINGS_FILE)
        data = {}
        try:
            with open(SETTINGS_FILE, encoding="utf-8-sig") as f:  # -sig: Notepad may have added a BOM
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("it doesn't hold a { ... } block")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            data = {}
            problem = ("%s couldn't be read (%s), so the organizer starts without its rules, notes and profiles.\n\n"
                       % (name, e))
            kept = None
            if isinstance(e, ValueError):  # damaged (a hand edit, or a write cut short): set it aside
                kept = unique_path(SETTINGS_FILE, ".broken")
                try:
                    os.replace(SETTINGS_FILE, kept)
                except OSError:
                    kept = None
            if kept:
                problem += ("The file was kept as %s. Fix it in a text editor and rename it back to %s to get "
                            "them back." % (os.path.basename(kept), name))
            else:
                self.keep_settings_file = True
                problem += "Changes to them won't be saved this time, so the file is left as it is."
            self.settings_problem = problem

        def get(key, kind, default):
            value = data.get(key, default)
            return value if isinstance(value, kind) else default

        self.settings = data
        self.rules = [dict(r, winner=str(r["winner"]), loser=str(r["loser"])) for r in get("rules", list, [])
                      if isinstance(r, dict) and isinstance(r.get("winner"), (str, int))
                      and isinstance(r.get("loser"), (str, int)) and r["winner"] and r["loser"]]
        self.game_dir = get("game_dir", str, "") or None
        self.workshop_dir = get("workshop_dir", str, "") or None
        self.notes = {k: v for k, v in get("notes", dict, {}).items() if isinstance(v, str)}
        self.profiles = {}
        for profile, entries in get("profiles", dict, {}).items():
            if isinstance(entries, list):
                entries = [e for e in entries if isinstance(e, list) and len(e) == 2 and isinstance(e[0], (str, int))]
                self.profiles[profile] = without_repeats([[str(w), bool(on)] for w, on in entries])
        self.theme_name.set(get("theme", str, "dark") if get("theme", str, "") in THEMES else "dark")
        self.hidden_columns = [c for c in get("hidden_columns", list, DEFAULT_HIDDEN) if isinstance(c, str)]

    def save_settings(self):
        if self.keep_settings_file:
            return
        self.settings.update({
            "game_dir": self.game_dir, "workshop_dir": self.workshop_dir,
            "theme": self.theme_name.get(), "hidden_columns": self.hidden_columns,
            "rules": self.rules, "notes": self.notes, "profiles": self.profiles,
        })
        self.settings.pop("last_wins", None)  # an old setting: the later addon always wins
        # Through a temp file, so a write that's cut short (disk full, crash) can't damage the old one.
        tmp = SETTINGS_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
            os.replace(tmp, SETTINGS_FILE)
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
        f.add_command(label="Install addon files (.vpk, .zip)...", command=self.install_files)
        f.add_command(label="Install addon folder...", command=self.install_folder)
        f.add_command(label="Open custom folder", command=self.open_custom_folder)
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
        e.add_command(label="Undo addon file change (custom/ on, off, rename)", command=self.undo_file_change)
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
        t.add_command(label="Extract scripts and settings...", command=self.extract_scripts)
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
        install = ttk.Button(bar, text="＋ Install addon...", style="Tool.TButton", command=self.install_files)
        install.pack(side="right", padx=(0, self.px(4)))
        folders = ttk.Button(bar, text="Folders...", style="Tool.TButton", command=self.choose_folders)
        folders.pack(side="right", padx=(0, self.px(4)))
        self.problems_btn = ttk.Button(bar, text="✓ No problems", style="Tool.TButton", command=self.show_problems)
        self.problems_btn.pack(side="right", padx=(0, self.px(10)))

        theme = self.theme_name.get
        Tooltip(self.save_btn, "Write the load order to the game's workshop.txt (the game's version is backed up "
                               "first). Close the game before saving.", theme)
        Tooltip(sort_btn, "Reorder so every rule is met, moving each rule's winner just after its loser (the later one wins).", theme)
        Tooltip(clean_btn, "Move every addon that does nothing to the end of the list where it loses. "
                           "The game plays the same, and they end up together for unsubscribing.", theme)
        Tooltip(install, "Copy an addon (.vpk, .zip or a folder) into the game's custom folder. On Windows you can "
                         "also drop files and folders on the window. custom/ addons load ahead of every Workshop "
                         "addon, A to Z by name.", theme)
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
        self.tree.bind("<space>", lambda e: self.toggle_enabled(soon=True))
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

    def screen_area(self):
        """(x, y, width, height) of the desktop: every monitor on Windows, the (virtual) screen elsewhere."""
        if sys.platform == "win32":
            try:
                from ctypes import windll
                metric = windll.user32.GetSystemMetrics
                if metric(78) and metric(79):  # SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
                    return metric(76), metric(77), metric(78), metric(79)
            except Exception:
                pass
        r = self.root
        return (0, 0, max(r.winfo_screenwidth(), r.winfo_vrootwidth()),
                max(r.winfo_screenheight(), r.winfo_vrootheight()))

    def restore_layout(self):
        geometry = self.settings.get("geometry")
        m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", geometry) if isinstance(geometry, str) else None
        if m:
            # Last closed on a monitor that isn't there now: keep the size, let the system place it.
            w, h, x, y = (int(n) for n in m.groups())
            ax, ay, aw, ah = self.screen_area()
            if not (ax - w + 100 <= x <= ax + aw - 100 and ay - 50 <= y <= ay + ah - 100):
                geometry = "%dx%d" % (w, h)
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
        if not isinstance(frac, (int, float)):
            frac = 0.58
        pos = int(width * min(max(frac, 0.3), 0.8))
        try:
            self.panes.sashpos(0, pos)
        except tk.TclError:
            pass

    def bind_keys(self):
        r = self.root
        r.bind_all("<Control-s>", lambda e: self.save_order())
        # In a text box (Notes, Filter, Find...) these are for the text, not the load order.
        r.bind_all("<Control-z>", lambda e: None if self.in_text_box(e) else self.undo(soon=True))
        r.bind_all("<Control-y>", lambda e: None if self.in_text_box(e) else self.redo(soon=True))
        r.bind_all("<Control-Shift-Z>", lambda e: None if self.in_text_box(e) else self.redo(soon=True))
        r.bind_all("<Control-f>", lambda e: self.focus_filter())
        r.bind_all("<F5>", lambda e: self.start_scan())
        # Held down, these repeat faster than the list can be rebuilt (see recompute_soon).
        for key, where in (("Up", -1), ("Down", 1), ("Home", "top"), ("End", "bottom")):
            self.tree.bind("<Control-%s>" % key, lambda e, w=where: self.move_selected(w, soon=True) or "break")

    def in_text_box(self, event):
        try:
            kind = self.root.tk.call("winfo", "class", str(event.widget))
        except tk.TclError:
            return False
        return kind in ("Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox", "Text")

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
            # The folders may have changed (Folders... OK): scan again as soon as this scan ends.
            self.rescan_pending = True
            self.status.config(text="A scan is running. It will start over when it's done...")
            return
        if confirm and self.is_dirty() and not messagebox.askyesno(
                "Rescan", "Rescanning discards your unsaved load order changes. Continue?"):
            return
        self.save_note()
        self.scanning = True
        self.recompute_due = False
        self.status.config(text="Finding Half-Life 2...")
        # Forget the rows before deleting them, so the selection change this causes doesn't look them up.
        self.row_tags, self.selected, self.selected_rel = {}, None, {}
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self.scan_thread, daemon=True).start()

    def poll_ui(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception as e:
                    # Report it and carry on: if this loop stopped, no scan would ever show again.
                    self.scanning = self.rescan_pending = False
                    self.root.report_callback_exception(*sys.exc_info())
                    self.status.config(text="Something went wrong: %s" % e)
                    messagebox.showerror("Something went wrong", "%s\n\nThe list may be incomplete. Rescan (F5) to "
                                         "try again." % (str(e) or type(e).__name__))
        except queue.Empty:
            pass
        finally:
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
            custom_dirs = ac.mounted_custom_dirs(game, existing=False) if game else []
            off_dirs = [d for d in (disabled_dir(c) for c in custom_dirs) if os.path.isdir(d)]
            addons = (ac.scan(containers + off_dirs, progress, workshop_dirs=[workshop] if workshop else [],
                              slow=slow) if containers or off_dirs else [])
            off_keys = {os.path.normcase(os.path.abspath(d)) for d in off_dirs}
            for a in addons:
                a.custom_off = os.path.normcase(os.path.dirname(os.path.abspath(a.location))) in off_keys
            self.custom_dirs = custom_dirs
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
            self.ui(lambda: self.scan_failed(message))
            return
        self.ui(lambda: self.scan_done(game, workshop, containers, addons, path, entries, base, base_info))

    def rescan_if_pending(self):
        """After a scan: if Rescan was asked for (or new folders chosen) meanwhile, its results are stale."""
        self.scanning = False
        if not self.rescan_pending:
            return False
        self.rescan_pending = False
        self.start_scan(confirm=False)
        return True

    def scan_failed(self, message):
        if not self.rescan_if_pending():
            messagebox.showerror("Scan failed", message)

    def scan_done(self, game, workshop, containers, addons, path, entries, base=None, base_info=""):
        if self.rescan_if_pending():
            return
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
        # The game never loads these, so they mustn't count as copies that make another addon removable.
        self.strays = [a for a in addons if a.stray and not a.leftover]
        self.empty_folders = [a for a in addons if not a.in_workshop and not a.files and not a.errors]
        self.addons = [a for a in addons if not a.leftover and not a.stray and a not in self.empty_folders]
        self.by_id = {a.workshop_id: a for a in self.addons if a.workshop_id}
        self.types = {a: addon_type(a) for a in self.addons}
        self.sizes = {a: addon_size(a) for a in self.addons}
        self.load_order_path = path
        # An id listed twice (a hand edit) would show up twice; keep the entry that counts.
        self.order = without_repeats([[wsid, enabled] for wsid, enabled in entries])
        repeated = sorted({w for w, n in Counter(w for w, _ in entries).items() if n > 1})
        self.saved_order = [list(e) for e in self.order]
        self.disk_entries, self.disk_ours = [(w, bool(e)) for w, e in entries], False
        self.history, self.future = [], []
        self.file_undo = []  # these addons are new objects
        self.selected = None
        self.root.title("HL2 Addon Organizer %s - %s" % (ac.__version__, game or workshop or "no folders"))
        self.recompute()
        if repeated:
            names = [self.by_id[w].name if w in self.by_id else "Workshop item %s" % w for w in repeated]
            messagebox.showwarning(
                "Addons listed twice",
                "workshop.txt lists %s more than once:\n\n%s\n\nThe organizer uses the last line for each one, "
                "and saving the load order writes each addon once."
                % (ac.plural(len(repeated), "addon"), "\n".join(names[:10]) + ("\n..." if len(names) > 10 else "")))
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

    def recompute_soon(self):
        """
        Held keys and quick clicks change the order faster than the list can be rebuilt, so they
        only change self.order, and this rebuilds once when they've all been handled. Until then the
        rows stay as the user saw them, so a click that was waiting still lands on its row.
        """
        if not self.recompute_due:
            self.recompute_due = True
            self.root.after_idle(self.recompute_if_due)

    def recompute_if_due(self):
        if self.recompute_due:
            self.recompute()
            sel = self.tree.selection()
            if sel:
                self.tree.see(sel[0])

    def recompute(self):
        self.recompute_due = False
        self.save_note()  # before refresh_details refills the Notes box
        enabled = {wsid: en for wsid, en in self.order}
        positions = {wsid: n for n, (wsid, _) in enumerate(self.order)}
        last_wins = self.last_wins.get()
        active = []
        for a in self.addons:
            a.position = a.rank = None
            if a.custom_off:
                continue  # in custom_disabled/: the game doesn't load it
            if a.workshop_id:
                if a.workshop_id not in positions:
                    continue  # not in workshop.txt: the game doesn't load it yet
                n = positions[a.workshop_id]
                a.position = n + 1
                if not enabled[a.workshop_id]:
                    continue
                a.rank = -n if last_wins else n
            active.append(a)
        ac.rank_custom_addons(self.custom_order(), last_wins)  # ahead of the Workshop ones, A to Z

        # Moving an addon doesn't change which files are loaded, only who wins, so the index of every
        # loaded file is only rebuilt when an addon is turned on or off.
        if self.indexed != frozenset(active):
            self.by_path = ac.index_files(active)
            self.conflict_paths = [(path, copies) for path, copies in self.by_path.items()
                                   if len(copies) > 1 and len({(s, c) for _, s, c in copies}) > 1]
            self.indexed = frozenset(active)
        self.removable, idle, self.together = ac.find_idle_addons(active, self.by_path, self.base)
        self.idle = {e[0]: e for e in idle}

        # Per addon: files where it beats / loses to a different copy.
        self.stats = defaultdict(lambda: [0, 0])
        for path, copies in self.conflict_paths:
            if not ac.known(*(c[0] for c in copies)):
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
            self.run_find(select_tab=False)  # the order changed, so which copy is used may have too

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
        if a.custom_off:
            return "disabled"
        if a.workshop_id and a.position is None:
            return "not in workshop.txt yet"
        if a.workshop_id and a.rank is None:
            return "disabled"
        if a.errors:
            return "problem reading"
        if a in self.removable:
            need = self.together.get(a)
            return (("does nothing" if a.workshop_id else "custom/, does nothing")
                    + (" (only with %s)" % ac.plural(len(need), "other") if need else ""))
        if not a.workshop_id:
            return "custom/ (loads first, A to Z)"
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
            # Sort first: the two lines after it refresh the list, which must come out in load order.
            self.sort = ("pos", False)
            self.filter_text.set("")
            self.view.set(VIEWS[0])
            self.update_headings()
            self.refresh_list()  # even if the caller then changes nothing

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

        custom = self.custom_order()  # load first, A to Z (see recompute)
        rows = custom + sorted((a for a in self.addons if a.custom_off), key=lambda a: a.name.lower())
        rows += [self.by_id[w] for w in self.listed_ids()]
        rows += sorted((a for a in self.addons if a.workshop_id and a.position is None), key=lambda a: a.name.lower())
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
            if a in self.removable or status == "disabled":
                fg = "nothing"
            elif a.position is None:
                fg = "fixed"
            if not a.workshop_id:
                on = "☐" if a.custom_off else "☑"
            else:
                on = "" if a.position is None else ("☑" if a.rank is not None else "☐")
            key = self.addon_key(a)
            self.row_tags[key] = (fg, bg)
            place = a.position or ("C%d" % (custom.index(a) + 1) if a in custom else "")
            self.tree.insert("", "end", iid=key, tags=[t for t in (fg, bg) if t], values=(
                on, place, a.name, self.flags(a), self.types.get(a, ""), len(a.files),
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
        # Clicks that arrived while this ran were aimed at the old rows; on_drag_start skips them.
        # Idle callbacks only run once those are handled.
        if self.settle_job:
            self.root.after_cancel(self.settle_job)
        self.list_rebuilt = True
        self.settle_job = self.root.after_idle(self.list_settled)

    def list_settled(self):
        self.list_rebuilt, self.settle_job = False, None

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

    def set_order(self, new_order, remember=True, soon=False):
        """Every load order change goes through here, so it can be undone.
        soon: for keys and clicks, which can come quickly; see recompute_soon."""
        if new_order == self.order:
            return
        if remember:
            self.history.append([list(e) for e in self.order])
            del self.history[:-200]
            self.future = []
        self.order = [list(e) for e in new_order]
        if soon:
            self.recompute_soon()
        else:
            self.recompute()

    def apply_order(self, ids_in_order, soon=False):
        """ids_in_order: the installed, listed ids in their new order. Uninstalled ids keep their slots."""
        it = iter(ids_in_order)
        enabled = {w: e for w, e in self.order}
        new = []
        for w, e in self.order:
            if w in self.by_id:
                w = next(it)
                e = enabled[w]
            new.append([w, e])
        self.set_order(new, soon=soon)

    def undo(self, soon=False):
        if not self.history:
            self.status.config(text="Nothing to undo.")
            return
        self.future.append([list(e) for e in self.order])
        self.set_order(self.history.pop(), remember=False, soon=soon)

    def redo(self, soon=False):
        if not self.future:
            self.status.config(text="Nothing to redo.")
            return
        self.history.append([list(e) for e in self.order])
        self.set_order(self.future.pop(), remember=False, soon=soon)

    def move_selected(self, where, soon=False):
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
        self.apply_order(new, soon=soon)
        self.tree.selection_set(chosen)
        self.tree.see(chosen[0])

    def toggle_enabled(self, keys=None, soon=False):
        if keys is None:
            custom = [k for k in self.tree.selection() if k.startswith("custom:")]
            if custom:
                self.toggle_custom(custom)
        sel = keys or self.movable_selection()
        if not sel:
            return "break"
        turn_on = not all(e for w, e in self.order if w in sel)
        self.set_order([[w, turn_on if w in sel else e] for w, e in self.order], soon=soon)
        return "break"

    def custom_order(self):
        """The custom/ addons the game loads, in its order: custom/ folder by custom/ folder as
        gameinfo.txt lists them, A to Z by name within each."""
        dirs = [os.path.normcase(os.path.abspath(d)) for d in getattr(self, "custom_dirs", [])]

        def key(a):
            folder = os.path.normcase(os.path.dirname(os.path.abspath(a.location)))
            return dirs.index(folder) if folder in dirs else len(dirs), ac.name_order(os.path.basename(a.location))
        return sorted((a for a in self.addons if not a.workshop_id and not a.custom_off), key=key)

    def move_addon(self, a, dest, off, undo=True):
        """Moves or renames a custom/ addon on disk (a foo_dir.vpk with its parts) and updates it
        here, keeping its note. Raises OSError; nothing is overwritten or left half moved."""
        old_location, old_off, old_key = a.location, a.custom_off, self.addon_key(a)
        move_files(moves_for(a.location, dest))
        folder = os.path.dirname(dest)
        a.location, a.custom_off = dest, off
        a.name = "%s/%s/%s" % (os.path.basename(os.path.dirname(folder)), os.path.basename(folder),
                               os.path.basename(dest))
        if old_key in self.notes:
            self.notes[self.addon_key(a)] = self.notes.pop(old_key)
        if undo:
            self.file_undo.append((a, old_location, old_off))
            del self.file_undo[:-50]

    def toggle_custom(self, keys):
        """Turns custom/ addons on or off by moving them between custom/ and custom_disabled/
        right away (the game reads folders, not a list). Like the Workshop checkboxes: if any
        is off they're all turned on, else all off."""
        addons = [a for a in (self.addon_for_key(k) for k in keys) if a and not a.workshop_id]
        if not addons:
            return
        turn_on = any(a.custom_off for a in addons)
        moved, problems = [], []
        for a in addons:
            if a.custom_off != turn_on:
                continue  # already that way
            folder, name = os.path.split(os.path.abspath(a.location))
            dest_dir = os.path.join(os.path.dirname(folder), "custom") if turn_on else disabled_dir(folder)
            try:
                self.move_addon(a, os.path.join(dest_dir, name), not turn_on)
            except OSError as e:
                problems.append("%s: %s (if the game is running, close it)" % (name, e.strerror or e))
                continue
            moved.append(name)
        self.after_file_change(addons, moved, "Turned on" if turn_on else "Turned off",
                               "moved to " + ("custom" if turn_on else DISABLED_FOLDER), problems)

    def after_file_change(self, addons, done, verb, how, problems):
        if done:
            self.save_settings()  # notes follow the files
            self.recompute()
            keys = [self.addon_key(a) for a in addons if self.tree.exists(self.addon_key(a))]
            if keys:
                self.tree.selection_set(keys)
            self.status.config(text="%s %s (%s). Edit > Undo addon file change puts it back." % (
                verb, ", ".join(done[:3]) + (" and more" if len(done) > 3 else ""), how))
        if problems:
            messagebox.showwarning("custom/ addons", "\n".join(problems[:8]))

    def selected_custom(self):
        return [a for a in (self.addon_for_key(k) for k in self.tree.selection()) if a and not a.workshop_id]

    def rename_custom(self):
        """Renaming is how a custom/ addon changes place: the game loads them A to Z."""
        addons = self.selected_custom()
        if len(addons) != 1:
            return
        a = addons[0]
        folder, name = os.path.split(a.location)
        is_vpk = os.path.isfile(a.location)
        new = simpledialog.askstring(
            "Rename", "New name for %s.\ncustom/ addons load A to Z, and a later one wins over an earlier one."
            % name, initialvalue=name, parent=self.root)
        if new is None:
            return
        new = new.strip().rstrip(". ")
        if is_vpk and not new.lower().endswith(".vpk"):
            new += ".vpk"
        if is_vpk and len(addon_files(a.location)) > 1 and not new.lower().endswith("_dir.vpk"):
            new = new[:-4] + "_dir.vpk"  # foo_dir.vpk needs the _dir to find its parts
        if not new or new == name:
            return
        if re.search(r'[\\/:*?"<>|]', new) or new.lower() in ("custom", DISABLED_FOLDER):
            messagebox.showerror("Rename", "%s can't be used as a name." % new)
            return
        try:
            self.move_addon(a, os.path.join(folder, new), a.custom_off)
        except OSError as e:
            messagebox.showerror("Rename", "%s: %s (if the game is running, close it)" % (name, e.strerror or e))
            return
        self.after_file_change([a], [new], "Renamed %s to" % name, "custom/ addons load A to Z", [])

    def remove_custom(self):
        """custom/ addons to the Recycle Bin (or Trash), where they can be restored from."""
        addons = self.selected_custom()
        if not addons:
            return
        names = [os.path.basename(a.location) for a in addons]
        if not messagebox.askyesno(
                "Remove", "Move %s to the Recycle Bin?\n\n%s\n\nYou can restore it from there."
                % (ac.plural(len(addons), "custom/ addon"), "\n".join(names[:8]) + ("\n..." if len(names) > 8 else ""))):
            return
        removed, problems = [], []
        for a in addons:
            try:
                for path in addon_files(a.location):
                    send_to_trash(path)
            except (OSError, InstallError) as e:
                problems.append("%s: %s" % (os.path.basename(a.location), e.strerror if isinstance(e, OSError)
                                            and e.strerror else e))
                continue
            removed.append(os.path.basename(a.location))
            self.addons.remove(a)
            self.notes.pop(self.addon_key(a), None)
            self.file_undo = [u for u in self.file_undo if u[0] is not a]
        if removed:
            self.save_settings()
            self.recompute()
            self.status.config(text="Moved %s to the Recycle Bin." % ", ".join(removed[:3]) + (
                " and more" if len(removed) > 3 else ""))
        if problems:
            messagebox.showwarning("Remove", "\n".join(problems[:8]))

    def undo_file_change(self):
        """Undoes the last move or rename of a custom/ addon (turning on or off, Rename)."""
        while self.file_undo:
            a, location, off = self.file_undo.pop()
            if a not in self.addons:
                continue
            try:
                self.move_addon(a, location, off, undo=False)
            except OSError as e:
                messagebox.showerror("Undo", "%s: %s" % (os.path.basename(a.location), e.strerror or e))
                return
            self.after_file_change([a], [os.path.basename(location)], "Put back", "undone", [])
            self.status.config(text="Put back %s." % os.path.basename(location))
            return
        self.status.config(text="No addon file change to undo.")

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
        self.drag = self.drag_plan = None
        if self.list_rebuilt:
            return "break"  # pressed while the list was being rebuilt: the row under it has changed
        row = self.tree.identify_row(event.y)
        if row.startswith("custom:") and self.column_at(event.x) == "on":
            self.toggle_custom([row])
            if self.tree.exists(row):
                self.tree.selection_set(row)
            return "break"
        if not row or row not in self.by_id or self.by_id[row].position is None:
            return
        if self.column_at(event.x) == "on":
            self.toggle_enabled([row], soon=True)
            if self.tree.exists(row):
                self.tree.selection_set(row)
            return "break"
        self.drag_row = row
        sel = self.movable_selection()
        modifiers = 0x0001 | 0x0004 | (0x0008 if sys.platform == "darwin" else 0)  # Shift, Control, Command
        if row in sel and len(sel) > 1 and not event.state & modifiers:
            # Pressed on one of several selected rows: drag them all, so keep the selection.
            self.drag = [w for w in self.listed_ids() if w in sel]
            self.tree.focus_set()
            self.tree.focus(row)
            return "break"
        self.drag = [row]

    def dragged_order(self, order, target):
        """order with the dragged ids taken out and put back at target: below it if it's below them,
        above it otherwise."""
        moving = set(self.drag)
        rest = [w for w in order if w not in moving]
        at = rest.index(target) + (1 if order.index(target) > order.index(self.drag[0]) else 0)
        return rest[:at] + self.drag + rest[at:]

    def on_drag_motion(self, event):
        if not self.drag or not self.is_plain_view():
            return
        target = self.tree.identify_row(event.y)
        if not target or target in self.drag or target not in self.by_id or self.by_id[target].position is None:
            return
        # Worked out from the load order itself, never from what the list shows.
        plan = self.dragged_order(self.drag_plan or self.listed_ids(), target)
        if plan == self.drag_plan:
            return
        self.drag_plan = plan
        # Show where they'll land. The listed addons are shown in load order, after the custom/ ones.
        rows = self.tree.get_children()
        first = next((n for n, k in enumerate(rows) if k in self.by_id), 0)
        moving = set(self.drag)
        self.tree.detach(*self.drag)
        for n, w in enumerate(plan):
            if w in moving:
                self.tree.move(w, "", first + n)

    def on_drag_end(self, event):
        dragged, plan, self.drag, self.drag_plan = self.drag, self.drag_plan, None, None
        if not dragged:
            return
        if plan and sorted(plan) == sorted(self.listed_ids()):
            self.apply_order(plan)
            self.tree.selection_set(dragged)
        elif len(dragged) > 1:
            self.tree.selection_set(self.drag_row)  # a click without a drag selects just that row

    def on_double_click(self, event):
        if self.column_at(event.x) == "on":
            return "break"  # the single click already toggled it
        if self.tree.identify_row(event.y):
            return self.toggle_enabled()

    def sort_by_rules(self):
        """
        Satisfies every rule with as little movement as possible: each rule's
        winner moves to just after its loser (the later one wins), everything else keeps its order.
        """
        ids = self.listed_ids()
        last_wins = self.last_wins.get()
        edges = []  # [((earlier, later) in list order, rule)]
        for r in self.rules:
            w, l = r["winner"], r["loser"]
            if w in self.by_id and l in self.by_id and w in ids and l in ids:
                edges.append(((l, w) if last_wins else (w, l), r))
        # Rules that go in a circle can't all be met: leave those out and apply the rest.
        circle = edges_in_cycles([e for e, _ in edges])
        skipped = [r for e, r in edges if e in circle]
        # Building the list from the end pulls "earlier" items up (the winner when
        # higher wins); building from the start pushes "later" items down (the
        # winner when lower wins).
        new = stable_toposort(ids, [e for e, _ in edges if e not in circle], from_end=not last_wins)
        if new != ids:
            self.reset_view()
            self.apply_order(new)
            self.status.config(text="Sorted by rules. Not saved yet.")
        if skipped:
            lines = ["%s should win over %s" % (self.by_id[r["winner"]].name, self.by_id[r["loser"]].name)
                     for r in skipped]
            messagebox.showerror("Rules contradict each other",
                                 "These rules go in a circle, so no order meets them all:\n\n"
                                 + "\n".join(lines[:15]) + ("\n..." if len(lines) > 15 else "")
                                 + ("\n\nThe other rules were applied." if new != ids else "")
                                 + "\n\nDelete one of these rules in the Rules tab.")
        elif new == ids:
            messagebox.showinfo("Sort by rules", "The load order already follows every rule.")

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
        custom = self.selected_custom()
        if custom:
            m.add_separator()
            m.add_command(label="Turn on" if any(x.custom_off for x in custom) else "Turn off",
                          command=lambda: self.toggle_custom([self.addon_key(x) for x in custom]))
            m.add_command(label="Rename... (changes its place)", state="normal" if len(custom) == 1 else "disabled",
                          command=self.rename_custom)
            m.add_command(label="Remove (to the Recycle Bin)...", command=self.remove_custom)
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
                        "Every file is overwritten or identical elsewhere, so removing all of them together "
                        "changes nothing in game. Those marked \"only with\" do nothing only while the others "
                        "do too: remove them together.",
                        "Move them down", self.move_removable))
        if self.strays:
            out.append(("warn", "%s in the Workshop folder that the game doesn't load"
                        % ac.plural(len(self.strays), "item"),
                        "%s%s. The game only loads folders named by a Workshop item's id; these look like "
                        "copies or files put there by hand." % (", ".join(os.path.basename(a.location)
                                                                           for a in self.strays[:3]),
                                                                 " and more" if len(self.strays) > 3 else ""),
                        "Open folder", lambda: open_path(self.found_workshop)))
        workshop_leftovers = self.workshop_leftovers()
        if workshop_leftovers:
            out.append(("info", "%s from removed addons" % ac.plural(len(workshop_leftovers),
                                                                     "leftover Workshop folder"),
                        "These Workshop folders only hold cache files the game left behind.",
                        "Delete them", self.delete_leftovers))
        empty = self.empty_folders + [a for a in self.leftovers if a not in workshop_leftovers]
        if empty:
            out.append(("info", "%s with no addon files" % ac.plural(len(empty), "folder"),
                        "%s%s. They're empty or only hold cache files, so the game gets nothing from them. "
                        "They're left alone here: maybe you're still making that addon."
                        % (", ".join(a.name for a in empty[:3]), " and more" if len(empty) > 3 else ""),
                        None, None))
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

    def workshop_leftovers(self):
        """Leftover folders of removed Workshop items. Only these can be deleted here: an empty
        custom/ folder may be an addon someone is still making."""
        if not self.found_workshop:
            return []
        workshop = os.path.normcase(os.path.abspath(self.found_workshop))
        return [a for a in self.leftovers if a.workshop_id
                and os.path.normcase(os.path.dirname(os.path.abspath(a.location))) == workshop]

    def delete_leftovers(self):
        # Only folders that still hold nothing but cache files.
        doomed = [a for a in self.workshop_leftovers() if only_cache_files(a.location)]
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
            # Again right before deleting: Steam or the game may have put something there meanwhile.
            if not only_cache_files(a.location):
                failed.append("%s: it isn't only cache files now" % a.location)
                continue
            try:
                shutil.rmtree(a.location)
            except OSError as e:
                failed.append("%s: %s" % (a.location, e.strerror or e))
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
        path = self.load_order_path
        # The game rewrites workshop.txt when addons are turned on or off or subscribed to in game.
        try:
            on_disk = [(w, bool(e)) for w, e in ac.read_load_order(path)]
        except OSError:
            on_disk = None
        changed = on_disk != self.disk_entries
        if changed:
            answer = messagebox.askyesnocancel(
                "workshop.txt has changed",
                "workshop.txt was changed after the organizer read it, probably by the game. Saving now would "
                "undo those changes.\n\n"
                "Yes: rescan to load the new version (your unsaved changes here are lost).\n"
                "No: save anyway (the new version is backed up first).\n"
                "Cancel: don't save.\n\n"
                "Close Half-Life 2 before saving.\n\n" + path)
            if answer is None:
                return
            if answer:
                self.start_scan(confirm=False)
                return
        elif not messagebox.askokcancel(
                "Save load order",
                "Close Half-Life 2 before saving, or it may overwrite the file.\n\n"
                + ("" if self.disk_ours else "A backup of the current workshop.txt is made first.\n\n") + path):
            return
        entries = [(w, bool(e)) for w, e in self.order]
        backup = None
        try:
            # Keep every version the organizer didn't write itself.
            if os.path.exists(path) and (changed or not self.disk_ours):
                backup = unique_path(path + time.strftime(".%Y%m%d-%H%M%S"), ".bak")
                shutil.copy2(path, backup)
                prune_backups(path)
            ac.write_load_order(path, entries)
        except OSError as e:
            messagebox.showerror("Couldn't save", str(e))
            return
        self.disk_entries, self.disk_ours = entries, True
        self.saved_order = [list(e) for e in self.order]
        self.refresh_status()
        self.status.config(text="Saved. The old file was backed up as %s." % os.path.basename(backup) if backup
                           else "Saved.")

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
        active = [a for a in self.addons if a.rank is not None or (not a.workshop_id and not a.custom_off)]
        disabled = [a for a in self.addons if a.workshop_id and a.position is not None and a.rank is None]
        unlisted = [a for a in self.addons if a.workshop_id and a.position is None]
        skipped = [("Disabled addons skipped", disabled, "Turned off in the game's Add-ons menu."),
                   ("Addons missing from workshop.txt skipped", unlisted,
                    "Installed, but not in the game's list yet. Start the game once to refresh it."),
                   ("Not Workshop items, skipped", self.strays,
                    "In the Workshop folder, but not an item folder (a copy, or a file put there by hand).\n"
                    "The game never loads these."),
                   ("Leftover folders skipped", self.leftovers,
                    "These only hold cache files, left behind by addons you removed. Safe to delete.")]
        text = ac.format_report(active, skipped, self.containers, self.load_order_path,
                                ac.analyze(active, 1024, self.base), 0, self.base_info)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            messagebox.showerror("Export report", str(e))
            return
        self.status.config(text="Report saved to %s" % path)

    def extract_scripts(self):
        """Every loaded addon's scripts and settings, what they change, and all of them combined
        (addon_scripts.py), in a folder the user picks."""
        if not self.addons or self.scanning:
            return
        folder = filedialog.askdirectory(parent=self.root, title="Choose an empty folder for the scripts and settings")
        if not folder:
            return
        # The load order on screen, saved or not: the addons that would load, ranked.
        active = [a for a in self.addons if a.rank is not None or (not a.workshop_id and not a.custom_off)]
        game = self.found_game
        self.status.config(text="Extracting scripts and settings...")

        def work():
            def progress(i, total, name=""):
                self.set_status("Extracting scripts and settings %d of %d: %s" % (i, total, name))
            try:
                result = addon_scripts.extract(active, game, folder, progress=progress)
            except Exception as e:
                message = str(e) or type(e).__name__
                self.set_status("Extracting failed: " + message)
                self.ui(lambda: messagebox.showerror("Extract scripts and settings", message))
                return
            self.ui(lambda: self.extract_done(result, folder))
        threading.Thread(target=work, daemon=True).start()

    def extract_done(self, result, folder):
        self.status.config(text="Scripts and settings saved to %s" % folder)
        text = ("%s copied from %s.\n%s change the original game; %s changed by more than one addon "
                "(%s, the addon that loads later wins).\n\nreport.txt lists every change. combined/ holds "
                "all of them in one folder." % (
                    ac.plural(result["files"], "file"), ac.plural(result["addons"], "addon"),
                    ac.plural(result["changed"], "file"), ac.plural(result["shared"], "file"),
                    ac.plural(result["conflicts"], "conflict")))
        if result["problems"]:
            text += "\n\nSome files couldn't be read; report.txt lists them."
        if messagebox.askyesno("Extract scripts and settings", text + "\n\nOpen the folder?"):
            open_path(folder)

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
        elif a is None:
            self.detail_title.config(text=self.overview_title())  # how many are selected may have changed

    def overview_title(self):
        n = len(self.tree.selection())
        return "%d addons selected" % n if n > 1 else "Your setup"

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
        if addon.workshop_id and addon.rank is None:
            return "not loaded (turned off)" if addon.position else "not loaded (not in workshop.txt)"
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
        tiles = [(len(self.addons), "addons"), (sum(1 for a in listed if a.rank is not None), "turned on"),
                 (len(self.conflict_paths), "conflicting files"), (len(self.removable), "do nothing")]
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
            self.note_key = None
            self.detail_title.config(text=self.overview_title())
            self.detail_info.config(text="Select an addon to see what it conflicts with. Drag to reorder, click "
                                         "the checkbox or press Space to turn addons on or off, right-click for "
                                         "more.")
            self.show_overview()
            return
        self.overview.pack_forget()
        self.actions.pack(fill="x", pady=(self.px(8), 0))
        self.notes_row.pack(fill="x", pady=(self.px(8), 0))
        if self.addon_key(a) != self.note_key:  # the same addon again: keep what's being typed
            self.note_key = self.addon_key(a)
            self.note_text.set(self.notes.get(self.note_key, ""))
        self.detail_title.config(text=a.name)
        info = [self.status_text(a) or ("loaded, #%d" % a.position if a.position else "loaded"),
                ac.plural(len(a.files), "file"), ac.nice_size(self.sizes.get(a, 0))]
        if self.types.get(a):
            info.append(self.types[a])
        if a.workshop_id:
            info.append("Workshop %s" % a.workshop_id)
        if a.errors:
            info.append("problems: " + "; ".join(a.errors[:2]))
        if self.together.get(a):
            info.append("remove only together with " + ", ".join(o.name for o in self.together[a][:3])
                        + (" and %d more" % (len(self.together[a]) - 3) if len(self.together[a]) > 3 else ""))
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
                else "inactive" if state.startswith("not loaded") else "note"
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

    def run_find(self, select_tab=True):
        """select_tab: False when only refreshing the results after a change, so the tab shown stays."""
        query = self.find_text.get().strip()
        open_paths = {self.find_tree.item(i, "text") for i in self.find_tree.get_children()
                      if self.find_tree.item(i, "open")}
        self.find_tree.delete(*self.find_tree.get_children())
        self.find_rows = {}
        if not query:
            return
        if select_tab:
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
        # A file row: let the double-click open or close it as usual.

    def jump_to_other(self, event):
        other = self.selected_other()
        if other and other is not self.base_addon:
            self.jump_to(other)

    # ------------------------------------------------------------------ help

    # ------------------------------------------------------------------ installing custom/ addons

    def install_target(self):
        """The custom/ folder new addons go to: the first one the game loads (made if missing)."""
        if not self.found_game:
            messagebox.showerror("Install addon", "The Half-Life 2 folder wasn't found. Choose it in Folders... first.")
            return None
        return ac.mounted_custom_dirs(self.found_game, existing=False)[0]

    def open_custom_folder(self):
        target = self.install_target()
        if target:
            os.makedirs(target, exist_ok=True)
            open_path(target)

    def install_files(self):
        if not self.install_target():
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="Install addon",
                                            filetypes=[("Addons", "*.vpk *.zip"), ("All files", "*.*")])
        if paths:
            self.install_paths(list(paths))

    def install_folder(self):
        if not self.install_target():
            return
        path = filedialog.askdirectory(parent=self.root, title="Install addon folder", mustexist=True)
        if path:
            self.install_paths([path])

    def install_paths(self, paths):
        """Installs dropped or chosen folders, VPKs and zips into custom/, then rescans."""
        target = self.install_target()
        if not target or not paths:
            return
        if self.scanning:
            messagebox.showinfo("Install addon", "Wait for the scan to finish, then drop the addon again.")
            return
        installed, problems = [], []
        work = tempfile.mkdtemp(prefix="addon_install_")
        try:
            for path in paths:
                try:
                    plan = plan_install(path, os.path.join(work, str(len(installed) + len(problems))))
                except InstallError as e:
                    problems.append(str(e))
                    continue
                for name, src in plan:
                    dest = os.path.join(target, name)
                    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dest)):
                        problems.append("%s is already installed there" % name)
                        continue
                    if os.path.lexists(dest) and not messagebox.askyesno(
                            "Install addon", "custom/%s is already there. Replace it?" % name):
                        continue
                    try:
                        os.makedirs(target, exist_ok=True)
                        copy_into(src, dest)
                        installed.append(name)
                    except OSError as e:
                        problems.append("%s: %s" % (name, e.strerror or e))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if problems:
            messagebox.showwarning("Install addon", "\n".join(problems[:8]))
        if installed:
            self.status.config(text="Installed %s into %s." % (", ".join(installed[:3]) + (" and more" if len(
                installed) > 3 else ""), target))
            self.start_scan()

    def show_help(self):
        messagebox.showinfo("How it works - version %s" % ac.__version__, (
            "The list is the load order: your custom/ addons first (A to Z), then the Workshop addons in "
            "workshop.txt's order. The addon lower in the list loads later and wins when two addons have "
            "the same file.\n\n"
            "Flags: ▲ overwrites other addons, ▼ is overwritten, ✕ does nothing (every file is overwritten "
            "or identical elsewhere).\n\n"
            "Select an addon to see its conflicts. Addons it overwrites are tinted green, addons that "
            "overwrite it red.\n\n"
            "Rules (Conflicts tab) say which addon should win; Sort by rules applies them with as little "
            "movement as possible.\n\n"
            "custom/ addons (shown first, C1, C2...) load ahead of every Workshop addon, A to Z by folder name. "
            "Install one with Install addon..., or on Windows by dropping it on the window.\n\n"
            "Load order changes only reach the game when you Save. Close the game first. Before the organizer "
            "replaces a workshop.txt it didn't write itself, it keeps a backup of it (the newest %d are kept).\n\n"
            "Turning custom/ addons on or off, renaming and removing them happen right away, on disk. "
            "Edit > Undo addon file change puts a move or rename back; removed addons are in the Recycle Bin.\n\n"
            "This tool was largely written by an AI (\"vibe-coded\") and comes with no warranty. Keep your own "
            "backup of your addons and workshop.txt." % KEEP_BACKUPS))


def main():
    # The windowed .exe has no console: printing (the checker reports some things that way) would fail.
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(os.devnull, "w")
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
