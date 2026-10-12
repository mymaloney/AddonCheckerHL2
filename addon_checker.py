#!/usr/bin/env python3
"""
HL2 Addon Checker

Finds Half-Life 2 addons that overwrite each other's files, and addons that
ship duplicate copies of the same file. Scans Steam Workshop items
(steamapps/workshop/content/220) and custom/ folders.

    python addon_checker.py                  find Steam + Half-Life 2 automatically
    python addon_checker.py FOLDER [...]     scan folders that contain addons
    python addon_checker.py --help           all options

No third-party packages needed; Python 3.7 or newer.
"""

import argparse
import bz2
import fnmatch
import gzip
import json
import lzma
import os
import re
import struct
import sys
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections import defaultdict
from dataclasses import dataclass, field

HL2_APPID = "220"
VPK_MAGIC = struct.pack("<I", 0x55AA1234)
VPK_ARCHIVE_PART = re.compile(r"_\d{3}\.vpk$", re.IGNORECASE)  # foo_000.vpk holds data for foo_dir.vpk

# Conflicts here replace weapon/sound scripts, UI or maps: most likely to break the game.
SEVERE_DIRS = ("scripts/", "resource/", "cfg/", "maps/")


class BadAddon(Exception):
    pass


READ_ERRORS = (BadAddon, OSError, EOFError, lzma.LZMAError, zlib.error, zipfile.BadZipFile)


@dataclass(eq=False)  # compare addons by identity
class Addon:
    name: str
    location: str
    workshop_id: str = ""
    files: dict = field(default_factory=dict)  # path -> (size, crc32)
    errors: list = field(default_factory=list)
    leftover: bool = False  # folder only holds cache files: the addon was removed
    rank: int = None  # load order position, lower = wins conflicts; None = unknown
    position: int = None  # 1-based line in workshop.txt, for display


def is_cache(name):
    """Caches the game generates (workshop.vpk.sound.cache, sound/sound.cache) aren't addon content."""
    return name.lower().endswith(".cache")


def norm(path):
    """Source paths are case-insensitive and use either slash."""
    return path.replace("\\", "/").strip("/").lower()


# ---------------------------------------------------------------------------
# Opening (possibly compressed) VPKs
# ---------------------------------------------------------------------------

class ZlibReader:
    """Minimal streaming reader for raw zlib data, which has no file wrapper in the stdlib."""

    def __init__(self, f):
        self.f = f
        self.dec = zlib.decompressobj()
        self.buf = b""

    def read(self, n):
        while len(self.buf) < n and not self.dec.eof:
            chunk = self.f.read(64 * 1024)
            if not chunk:
                break
            self.buf += self.dec.decompress(chunk)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def close(self):
        self.f.close()


def _valve_lzma(path):
    # Valve's own header: "LZMA", uncompressed size, compressed size, 5 LZMA property bytes
    f = open(path, "rb")
    head = f.read(17)
    props, dict_size = head[12], struct.unpack_from("<I", head, 13)[0]
    lc, props = props % 9, props // 9
    lp, pb = props % 5, props // 5
    filt = {"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}
    return lzma.open(f, format=lzma.FORMAT_RAW, filters=[filt])


# (name, test on the first bytes, opener). Every candidate must also decompress to a VPK header.
OPENERS = [
    ("VPK", lambda h: h[:4] == VPK_MAGIC, lambda p: open(p, "rb")),
    ("Valve LZMA", lambda h: h[:4] == b"LZMA" and len(h) >= 17, _valve_lzma),
    ("xz", lambda h: h[:6] == b"\xfd7zXZ\x00", lambda p: lzma.open(p, format=lzma.FORMAT_XZ)),
    ("gzip", lambda h: h[:2] == b"\x1f\x8b", lambda p: gzip.open(p, "rb")),
    ("bzip2", lambda h: h[:3] == b"BZh", lambda p: bz2.open(p, "rb")),
    ("zlib", lambda h: len(h) >= 2 and h[0] & 0x0F == 8 and (h[0] << 8 | h[1]) % 31 == 0,
     lambda p: ZlibReader(open(p, "rb"))),
    ("LZMA", lambda h: len(h) >= 13 and h[0] < 225, lambda p: lzma.open(p, format=lzma.FORMAT_ALONE)),
]


def read_exact(stream, n):
    data = stream.read(n)
    if len(data) != n:
        raise BadAddon("file is truncated")
    return data


def detect_vpk(path):
    """Returns the opener for this file if it is a (compressed) VPK, else None."""
    with open(path, "rb") as f:
        head = f.read(32)
    for _, test, opener in OPENERS:
        if not test(head):
            continue
        try:
            s = opener(path)
            try:
                if s.read(4) == VPK_MAGIC:
                    return opener
            finally:
                s.close()
        except (OSError, EOFError, lzma.LZMAError, zlib.error, ValueError):
            pass
    return None


def parse_vpk(stream, files, magic_read=False):
    """Reads a VPK's directory tree (v1 or v2) into files. Only the tree is read, not the data."""
    if not magic_read and read_exact(stream, 4) != VPK_MAGIC:
        raise BadAddon("not a VPK file")
    version, tree_size = struct.unpack("<II", read_exact(stream, 8))
    if version == 2:
        read_exact(stream, 16)  # section sizes we don't need
    elif version != 1:
        raise BadAddon("unsupported VPK version %d" % version)
    tree = read_exact(stream, tree_size)

    pos = 0

    def string():
        nonlocal pos
        end = tree.find(b"\0", pos)
        if end < 0:
            raise BadAddon("corrupt VPK directory")
        s = tree[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    # extension -> directory -> name -> entry; each list ends with "". " " means none/root.
    while True:
        ext = string()
        if not ext:
            break
        while True:
            directory = string()
            if not directory:
                break
            while True:
                name = string()
                if not name:
                    break
                if pos + 18 > len(tree):
                    raise BadAddon("corrupt VPK directory")
                crc, preload, _archive, _offset, length, _end = struct.unpack_from("<IHHIIH", tree, pos)
                pos += 18 + preload
                path = name if directory == " " else directory + "/" + name
                if ext != " ":
                    path += "." + ext
                files.setdefault(norm(path), (preload + length, crc))


def read_packed_file(addon, path):
    """Adds a (compressed) VPK or a zip of VPKs to the addon. Returns False if it's neither."""
    if zipfile.is_zipfile(path):
        found = False
        with zipfile.ZipFile(path) as z:
            for member in z.namelist():
                if VPK_ARCHIVE_PART.search(member):
                    continue
                with z.open(member) as s:
                    if s.read(4) == VPK_MAGIC:
                        parse_vpk(s, addon.files, magic_read=True)
                        found = True
        return found

    opener = detect_vpk(path)
    if not opener:
        return False
    s = opener(path)
    try:
        parse_vpk(s, addon.files)
    finally:
        s.close()
    return True


# ---------------------------------------------------------------------------
# Scanning addons
# ---------------------------------------------------------------------------

def crc_file(path):
    crc, size = 0, 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)  # same CRC32 VPK directories store
            size += len(chunk)
    return size, crc & 0xFFFFFFFF


def scan_folder_addon(addon, root):
    unknown = []
    for entry in sorted(os.scandir(root), key=lambda e: e.name.lower()):
        if entry.is_dir():
            # Loose content: materials/, models/, sound/...
            for dirpath, _, names in os.walk(entry.path):
                for name in names:
                    if is_cache(name):
                        continue
                    full = os.path.join(dirpath, name)
                    rel = norm(os.path.relpath(full, root))
                    try:
                        addon.files.setdefault(rel, crc_file(full))
                    except OSError as e:
                        addon.errors.append("can't read %s: %s" % (rel, e.strerror))
        elif entry.is_file():
            if VPK_ARCHIVE_PART.search(entry.name) or is_cache(entry.name):
                continue
            try:
                if not read_packed_file(addon, entry.path):
                    unknown.append(entry)
            except READ_ERRORS as e:
                addon.errors.append("%s: %s" % (entry.name, e))

    # Removing a Workshop addon deletes its VPK but leaves the game's cache file behind.
    if not addon.files and not addon.errors and not unknown:
        addon.leftover = True

    # Root files like addoninfo.txt are normal. Only worth mentioning if nothing else was readable.
    if not addon.files and not addon.errors and unknown:
        details = []
        for e in unknown[:3]:
            with open(e.path, "rb") as f:
                details.append("%s (starts with %s)" % (e.name, f.read(8).hex(" ")))
        addon.errors.append("no readable content, unrecognized file format: " + ", ".join(details))


def collect_addons(containers, workshop_dirs=()):
    """Each container is a folder of addons (workshop/content/220 or a custom/ folder), or a single .vpk.
    workshop_dirs: containers to treat as Workshop folders even if they aren't named .../content/220."""
    workshop_keys = {os.path.normcase(os.path.realpath(d)) for d in workshop_dirs}
    addons, seen = [], set()
    for container in containers:
        container = os.path.abspath(container)
        key = os.path.normcase(os.path.realpath(container))
        if key in seen:
            continue
        seen.add(key)

        if os.path.isfile(container):
            entries = [container]
            label = os.path.basename(os.path.dirname(container))
        else:
            try:
                entries = sorted((os.path.join(container, n) for n in os.listdir(container)), key=str.lower)
            except OSError as e:
                print("Can't open %s: %s" % (container, e.strerror), file=sys.stderr)
                continue
            label = parent_name(container) + "/" + os.path.basename(container)

        is_workshop = key in workshop_keys or (
            os.path.basename(container) == HL2_APPID and parent_name(container) == "content")
        if is_workshop:
            label = "workshop"
        for path in entries:
            name = os.path.basename(path)
            if os.path.isdir(path):
                addon = Addon(name=label + "/" + name, location=path)
                if is_workshop and name.isdigit():
                    addon.workshop_id = name
                addons.append((addon, "folder"))
            elif name.lower().endswith(".vpk") and not VPK_ARCHIVE_PART.search(name):
                addons.append((Addon(name=label + "/" + name, location=path), "vpk"))
    return addons


def parent_name(path):
    return os.path.basename(os.path.dirname(path))


def print_progress(i, total):
    print("\rReading addon %d/%d..." % (i, total), end="" if i < total else "\n", file=sys.stderr, flush=True)


def scan(containers, progress=print_progress, workshop_dirs=()):
    addons = collect_addons(containers, workshop_dirs)
    total = len(addons)
    for i, (addon, kind) in enumerate(addons, 1):
        progress(i, total)
        if kind == "folder":
            scan_folder_addon(addon, addon.location)
        else:
            try:
                if not read_packed_file(addon, addon.location):
                    addon.errors.append("not a VPK file")
            except READ_ERRORS as e:
                addon.errors.append(str(e))
        # Files at the addon root (addoninfo.txt...) and caches aren't game content.
        addon.files = {p: v for p, v in addon.files.items() if "/" in p and not is_cache(p)}
    return [a for a, _ in addons]


# ---------------------------------------------------------------------------
# Workshop titles
# ---------------------------------------------------------------------------

def fetch_workshop_titles(ids):
    """Looks up item titles with Steam's public API. Returns {} if offline."""
    titles = {}
    ids = sorted(ids)
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        data = {"itemcount": len(batch)}
        for i, wsid in enumerate(batch):
            data["publishedfileids[%d]" % i] = wsid
        req = urllib.request.Request(
            "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
            data=urllib.parse.urlencode(data).encode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                details = json.load(r)["response"]["publishedfiledetails"]
        except Exception:
            return titles
        for d in details:
            if d.get("title"):
                titles[str(d["publishedfileid"])] = d["title"]
    return titles


# ---------------------------------------------------------------------------
# Base game files
# ---------------------------------------------------------------------------

# Used when gameinfo.txt can't be read: earlier folders win, like the game's own search path.
FALLBACK_GAME_FOLDERS = ["hl2_complete", "ep2", "episodic", "hl2", "lostcoast", "platform"]


def gameinfo_vpks(game_dir):
    """The VPKs the game mounts, highest priority first, read from gameinfo.txt's SearchPaths.
    Returns (list of _dir.vpk / .vpk paths, gameinfo.txt path or None)."""
    for folder in ("hl2_complete", "hl2"):
        gameinfo = os.path.join(game_dir, folder, "gameinfo.txt")
        try:
            with open(gameinfo, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        m = re.search(r'"?SearchPaths"?\s*\{(.*?)\}', text, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        vpks = []
        for line in m.group(1).splitlines():
            line = line.split("//")[0]
            parts = re.findall(r'"([^"]*)"|(\S+)', line)
            parts = [a or b for a, b in parts]
            if len(parts) < 2 or not parts[1].lower().endswith(".vpk") or "*" in parts[1]:
                continue
            value = parts[1].replace("|gameinfo_path|", os.path.dirname(gameinfo) + "/")
            value = value.replace("|all_source_engine_paths|", game_dir + "/")
            path = os.path.normpath(value if os.path.isabs(value) else os.path.join(game_dir, value))
            # "hl2/hl2_pak.vpk" in gameinfo means hl2/hl2_pak_dir.vpk on disk.
            for cand in (path[:-4] + "_dir.vpk", path):
                if os.path.isfile(cand) and cand not in vpks:
                    vpks.append(cand)
                    break
        if vpks:
            return vpks, gameinfo
    return [], None


def fallback_vpks(game_dir):
    names = sorted(os.listdir(game_dir), key=lambda n: (
        FALLBACK_GAME_FOLDERS.index(n.lower()) if n.lower() in FALLBACK_GAME_FOLDERS else 99, n.lower()))
    vpks = []
    for sub in names:
        folder = os.path.join(game_dir, sub)
        if not os.path.isdir(folder) or sub.lower() in ("bin", "custom", "download", "addons"):
            continue
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(".vpk") and not VPK_ARCHIVE_PART.search(name):
                vpks.append(os.path.join(folder, name))
    return vpks


def read_base_game(game_dir):
    """The original game's files as the game sees them: path -> (size, crc32).
    Returns (files, number of VPKs read, where the list of VPKs came from, errors)."""
    vpks, source = gameinfo_vpks(game_dir)
    if not vpks:
        vpks, source = fallback_vpks(game_dir), "the game folders (gameinfo.txt not readable)"
    base = Addon(name="Half-Life 2 (base game)", location=game_dir)
    errors = []
    for vpk in vpks:
        part = Addon(name=vpk, location=vpk)
        try:
            read_packed_file(part, vpk)
        except READ_ERRORS as e:
            errors.append("%s: %s" % (vpk, e))
        for path, content in part.files.items():
            base.files.setdefault(path, content)  # first in the search path wins
    return base.files, len(vpks), source, errors


# ---------------------------------------------------------------------------
# Finding Steam
# ---------------------------------------------------------------------------

def steam_roots():
    roots = []
    if sys.platform == "win32":
        import winreg
        keys = [(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")]
        for hive, key, value in keys:
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(winreg.QueryValueEx(k, value)[0])
            except OSError:
                pass
        roots += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    else:
        home = os.path.expanduser("~")
        roots += [os.path.join(home, ".steam", "steam"),
                  os.path.join(home, ".local", "share", "Steam"),
                  os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"),
                  os.path.join(home, "Library", "Application Support", "Steam")]
    return [r for r in roots if os.path.isdir(os.path.join(r, "steamapps"))]


def library_folders(steam_root):
    libs = [steam_root]
    try:
        with open(os.path.join(steam_root, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return libs
    # New format: "path" "D:\\SteamLibrary". Old format: "1" "D:\\SteamLibrary".
    for m in re.finditer(r'"(?:path|\d+)"\s+"((?:[^"\\]|\\.)*)"', text):
        path = m.group(1).replace("\\\\", "\\")
        if os.path.isdir(os.path.join(path, "steamapps")):
            libs.append(path)
    return libs


def find_load_order_file(containers):
    """hl2_complete/cfg/workshop.txt in the game folder of the library that holds the Workshop items."""
    for c in containers:
        c = os.path.abspath(c)
        if os.path.basename(c) == HL2_APPID and parent_name(c) == "content":
            steamapps = os.path.dirname(os.path.dirname(os.path.dirname(c)))
            path = os.path.join(steamapps, "common", "Half-Life 2", "hl2_complete", "cfg", "workshop.txt")
            if os.path.isfile(path):
                return path
    return None


def read_load_order(path):
    """workshop.txt: "AddonInfo" { "<id>" "1|0" ... }, in load order. Returns [(id, enabled)]."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return [(m.group(1), m.group(2) != "0") for m in re.finditer(r'"(\d+)"\s+"(\d+)"', text)]


def write_load_order(path, entries):
    """Writes workshop.txt in the game's own format. entries: [(id, enabled)] in load order."""
    lines = ['"AddonInfo"', "{"]
    lines += ['\t"%s"\t\t"%d"' % (wsid, 1 if enabled else 0) for wsid, enabled in entries]
    lines += ["}", ""]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\r\n" if sys.platform == "win32" else "\n") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)


def resolve_game_dir(path):
    """The Half-Life 2 folder, given it or a folder above it (a Steam library, steamapps, common)."""
    if not path:
        return None
    for cand in (path, os.path.join(path, "Half-Life 2"), os.path.join(path, "common", "Half-Life 2"),
                 os.path.join(path, "steamapps", "common", "Half-Life 2")):
        if os.path.isdir(os.path.join(cand, "hl2_complete")) or os.path.isdir(os.path.join(cand, "hl2")):
            return os.path.normpath(cand)
    return None


def resolve_workshop_dir(path):
    """The workshop/content/220 folder, given it or a folder above it."""
    if not path:
        return None
    for cand in (path, os.path.join(path, HL2_APPID), os.path.join(path, "content", HL2_APPID),
                 os.path.join(path, "workshop", "content", HL2_APPID),
                 os.path.join(path, "steamapps", "workshop", "content", HL2_APPID)):
        if os.path.basename(os.path.normpath(cand)) == HL2_APPID and os.path.isdir(cand):
            return os.path.normpath(cand)
    return None


def workshop_dir_for_game(game_dir):
    """Steam keeps Workshop items in the same library as the game."""
    steamapps = os.path.dirname(os.path.dirname(os.path.normpath(game_dir)))
    path = os.path.join(steamapps, "workshop", "content", HL2_APPID)
    return path if os.path.isdir(path) else None


def game_dir_for_workshop(workshop_dir):
    steamapps = os.path.dirname(os.path.dirname(os.path.dirname(os.path.normpath(workshop_dir))))
    return resolve_game_dir(os.path.join(steamapps, "common", "Half-Life 2"))


def has_game_files(game_dir):
    """True for a real install: the game's own VPKs are there, not just leftover config folders."""
    for sub in ("hl2", "hl2_complete", "ep2", "episodic"):
        try:
            if any(n.lower().endswith("_dir.vpk") for n in os.listdir(os.path.join(game_dir, sub))):
                return True
        except OSError:
            pass
    return False


def count_workshop_items(workshop_dir):
    try:
        return sum(1 for n in os.listdir(workshop_dir) if n.isdigit())
    except OSError:
        return 0


def find_hl2_dirs():
    """
    (Half-Life 2 folder, Workshop folder) from Steam's libraries; either can be None.
    Several libraries can have a Half-Life 2 folder (an old install leaves cfg folders
    behind), so prefer the one with the game's files, next to the Workshop folder.
    """
    libs, seen = [], set()
    for root in steam_roots():
        for lib in library_folders(root):
            key = os.path.normcase(os.path.realpath(lib))
            if key not in seen:
                seen.add(key)
                libs.append(lib)

    candidates = []
    for n, lib in enumerate(libs):
        game = resolve_game_dir(os.path.join(lib, "steamapps", "common", "Half-Life 2"))
        workshop = resolve_workshop_dir(os.path.join(lib, "steamapps", "workshop", "content", HL2_APPID))
        if game or workshop:
            items = count_workshop_items(workshop) if workshop else 0
            score = (bool(game) and has_game_files(game), items > 0, bool(game), items, -n)
            candidates.append((score, game, workshop))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: c[0], reverse=True)
    _, game, workshop = candidates[0]
    if not workshop:  # Workshop items in another library than the game
        workshop = next((w for _, _, w in candidates if w), None)
    if not game:
        game = next((g for _, g, _ in candidates if g), None)
    return game, workshop


def game_folders(game_dir, workshop_dir):
    """What to scan for a given game + Workshop folder, and where workshop.txt is.
    Returns (containers, load order file or None)."""
    containers = []
    if workshop_dir:
        containers.append(workshop_dir)
    load_order = None
    if game_dir:
        for sub in sorted(os.listdir(game_dir)):
            custom = os.path.join(game_dir, sub, "custom")
            if os.path.isdir(custom):
                containers.append(custom)
        path = os.path.join(game_dir, "hl2_complete", "cfg", "workshop.txt")
        load_order = path if os.path.isfile(path) else None
    return containers, load_order


def find_hl2_addon_folders():
    found, seen = [], set()
    for root in steam_roots():
        for lib in library_folders(root):
            key = os.path.normcase(os.path.realpath(lib))
            if key in seen:
                continue
            seen.add(key)

            workshop = os.path.join(lib, "steamapps", "workshop", "content", HL2_APPID)
            if os.path.isdir(workshop):
                found.append(workshop)

            # hl2/custom, episodic/custom, ep2/custom, lostcoast/custom...
            game = os.path.join(lib, "steamapps", "common", "Half-Life 2")
            if os.path.isdir(game):
                for sub in sorted(os.listdir(game)):
                    custom = os.path.join(game, sub, "custom")
                    if os.path.isdir(custom):
                        found.append(custom)
    return found


# ---------------------------------------------------------------------------
# Analysis + report
# ---------------------------------------------------------------------------

def known(*addons):
    return all(a.rank is not None for a in addons)


def by_rank(copies):
    """Winner first when the load order is known; unknown ranks go last."""
    return sorted(copies, key=lambda c: (c[0].rank is None, c[0].rank or 0))


def severity(path):
    return 2 if path.startswith(SEVERE_DIRS) else 1 if path.startswith("models/") else 0


def find_idle_addons(addons, by_path, base=None):
    """
    Works out what each addon actually contributes. A file does nothing when
    an addon that loads ahead of it overwrites it, when another addon has an
    identical copy that would take over, or when it's identical to the base
    game's own file and no other addon has it. Addons where every file does
    nothing can be removed without changing the game.

    Addons are checked least important first, and each removable one is taken
    out before checking the next, so all of them can be removed together
    (of two identical addons, only the one that loses is removable).

    Results: (addon, overwritten_by, copies_in, unknown, same_as_base, reverted_by).
    reverted_by counts files overwritten by an addon whose copy is the base game's own.
    """
    base = base or {}

    def contribution(addon, removed):
        overwritten_by, copies_in, reverted_by = defaultdict(int), defaultdict(int), defaultdict(int)
        unknown = same_as_base = 0
        for path, content in addon.files.items():
            others = [c for c in by_path[path] if c[0] is not addon and c[0] not in removed]
            if not others:
                # Only this addon has it: without it, the game's own file is used.
                if base.get(path) == content:
                    same_as_base += 1
                continue
            if known(addon, *(c[0] for c in others)):
                best = by_rank(others)[0]
                if (best[1], best[2]) == content:
                    copies_in[best[0].name] += 1  # same file takes over (or already wins)
                elif best[0].rank < addon.rank:
                    winner_is_original = base.get(path) == (best[1], best[2])
                    (reverted_by if winner_is_original else overwritten_by)[best[0].name] += 1
                # else: this addon's copy is the one the game uses: needed
            elif all((c[1], c[2]) == content for c in others):
                copies_in[others[0][0].name] += 1  # identical everywhere, order doesn't matter
            else:
                unknown += 1
        return overwritten_by, copies_in, unknown, same_as_base, reverted_by

    removed, results = set(), []
    for addon in sorted(addons, key=lambda a: (a.rank is None, -(a.rank or 0))):
        if not addon.files:
            continue
        stats = contribution(addon, removed)
        if idle_count(stats) == len(addon.files):
            removed.add(addon)
            results.append((addon,) + stats)

    # The rest, against the final set of removable addons.
    for addon in addons:
        if addon.files and addon not in removed:
            stats = contribution(addon, removed)
            if idle_count(stats) * 2 >= len(addon.files):
                results.append((addon,) + stats)
    return removed, results


def idle_count(stats):
    """Files that do nothing, from find_idle_addons stats (with or without the addon in front)."""
    if not isinstance(stats[0], dict):
        stats = stats[1:]
    return sum(stats[0].values()) + sum(stats[1].values()) + stats[3] + sum(stats[4].values())


def index_files(addons):
    by_path = defaultdict(list)  # path -> [(addon, size, crc)]
    for addon in addons:
        for path, (size, crc) in addon.files.items():
            by_path[path].append((addon, size, crc))
    return by_path


def analyze(addons, min_dupe_size, base=None):
    base = base or {}
    by_path = index_files(addons)

    conflicts, same_path = [], []
    for path, copies in sorted(by_path.items()):
        if len(copies) < 2:
            continue
        identical = len({(s, c) for _, s, c in copies}) == 1
        (same_path if identical else conflicts).append((path, by_rank(copies)))
    conflicts.sort(key=lambda item: (-severity(item[0]), item[0]))

    # Identical contents under different paths, in different addons.
    by_content = defaultdict(list)
    for path, copies in by_path.items():
        addon, size, crc = copies[0]  # same-path copies are reported above
        if size >= max(min_dupe_size, 1):
            by_content[(size, crc)].append((path, addon))
    content_dupes = [(size, group) for (size, _), group in by_content.items()
                     if len(group) > 1 and len({id(a) for _, a in group}) > 1]
    content_dupes.sort(key=lambda item: (-item[0], item[1][0][0]))

    # Which addons fight the most. With a known load order the pair is (winner, loser).
    pair_counts = defaultdict(lambda: [0, 0])
    for path, copies in conflicts:
        owners = []
        for a, _, _ in copies:
            if a not in owners:
                owners.append(a)
        for i in range(len(owners)):
            for j in range(i + 1, len(owners)):
                a, b = owners[i], owners[j]
                if not known(a, b) and a.name.lower() > b.name.lower():
                    a, b = b, a
                counts = pair_counts[(a, b)]
                counts[0] += 1
                counts[1] += severity(path) == 2
    pairs = sorted(pair_counts.items(), key=lambda item: (-item[1][0], item[0][0].name, item[0][1].name))

    removed, idle = find_idle_addons(addons, by_path, base)

    # Files identical to the base game's own, per addon, most first.
    base_copies = []
    for addon in addons:
        same = sorted(p for p, content in addon.files.items() if base.get(p) == content)
        if same:
            base_copies.append((addon, same))
    base_copies.sort(key=lambda item: (-len(item[1]), item[0].name.lower()))

    return conflicts, same_path, content_dupes, pairs, removed, idle, base_copies, base


def path_matcher(query):
    """Case-insensitive part of a path, or a wildcard pattern like maps/d1_town_*."""
    q = norm(query)
    if any(ch in q for ch in "*?["):
        return lambda p: fnmatch.fnmatchcase(p, q) or fnmatch.fnmatchcase(p, "*" + q + "*")
    return lambda p: q in p


def find_files(query, addons, by_path, base=None, limit=500):
    """
    Every file matching query that some addon has, and what each addon does with it.
    addons: every addon to search, loaded or not; by_path: index of the loaded ones.
    Returns ([(path, summary, [(addon or None for the base game, status)])], number of matching paths).
    """
    base = base or {}
    match = path_matcher(query)
    found = defaultdict(list)
    for a in addons:
        for p in a.files:
            if match(p):
                found[p].append(a)
    paths = sorted(found)
    results = []
    for p in paths[:limit]:
        loaded = by_path.get(p, [])
        loaded_set = {c[0] for c in loaded}
        ranked = by_rank(loaded)
        winner = ranked[0] if loaded and known(*(c[0] for c in loaded)) else None
        not_loaded = sorted((a for a in found[p] if a not in loaded_set), key=lambda a: a.name.lower())
        rows = []
        for a in [c[0] for c in ranked] + not_loaded:
            content = a.files[p]
            if a not in loaded_set:
                status = "disabled" if a.position else "not loaded"
            elif winner is None:
                status = "load order unknown"
            elif a is winner[0]:
                status = "USED"
            elif content == (winner[1], winner[2]):
                status = "identical to the used copy"
            else:
                status = "overwritten"
            if base.get(p) == content:
                status += ", same as the original"
            rows.append((a, status))
        if p in base:
            rows.append((None, "original game file" + (", used (no loaded addon has it)" if not loaded else "")))
        if winner:
            summary = "used: " + winner[0].name
            if base.get(p) == (winner[1], winner[2]):
                summary += " (same as the original)"
        elif loaded:
            summary = "load order unknown"
        else:
            summary = "no loaded addon has it"
        results.append((p, summary, rows))
    return results, len(paths)


def nice_size(n):
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1048576)
    if n >= 1024:
        return "%.1f KB" % (n / 1024)
    return "%d bytes" % n


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def label(addon):
    return addon.name + ("  [#%d]" % addon.position if addon.position else "")


def format_report(addons, skipped, containers, load_order, analysis, limit, base_info=""):
    conflicts, same_path, content_dupes, pairs, removed, idle, base_copies, base = analysis
    out = []
    w = out.append

    total_files = sum(len(a.files) for a in addons)
    w("HL2 Addon Checker: %d addons, %d files" % (len(addons), total_files))
    w("%d conflicts, %d same-path duplicates, %d content duplicates"
      % (len(conflicts), len(same_path), len(content_dupes)))
    w("%d addons do nothing and can be removed" % len(removed))
    for title, items, _ in skipped:
        if items:
            w("%d %s (listed at the end)" % (len(items), title.lower()))
    w("")
    w("Scanned:")
    for c in containers:
        w("  " + c)
    if load_order:
        w("Load order: " + load_order)
        w("  [#N] = position in that list. " + LOAD_ORDER_NOTE)
    else:
        w("Load order: not found, so the report can't say which addon wins a conflict.")
    w("Base game: " + (base_info or "not checked"))
    w("")

    def more(i, items):
        if limit and i >= limit:
            w("  ... and %d more (see the report file)" % (len(items) - i))
            return True
        return False

    def top(counter, n=3):
        items = sorted(counter.items(), key=lambda kv: -kv[1])
        text = ", ".join("%s (%d)" % kv for kv in items[:n])
        if len(items) > n:
            text += ", and %d more" % (len(items) - n)
        return text

    broken = [a for a in addons if a.errors]
    if broken:
        w("== PROBLEMS READING ADDONS (%d) ==" % len(broken))
        for a in broken:
            w("  " + a.name)
            for e in a.errors[:5]:
                w("      " + e)
        w("")

    def idle_entries(entries, header, explain):
        w("== %s (%d) ==" % (header, len(entries)))
        for line in explain:
            w(line)
        w("")
        for i, (addon, over, copies, unknown, same_as_base, reverted) in enumerate(entries):
            if more(i, entries):
                break
            n = len(addon.files)
            idle_n = idle_count((over, copies, unknown, same_as_base, reverted))
            if idle_n == n:
                w("  %s  (%s)" % (label(addon), plural(n, "file")))
            else:
                w("  %s  (%d%% of %s do nothing)" % (label(addon), 100 * idle_n // n, plural(n, "file")))
            if over:
                w("      overwritten by: " + top(over))
            if reverted:
                w("      undone back to the original by: " + top(reverted))
            if copies:
                w("      identical copies in: " + top(copies))
            if same_as_base:
                w("      identical to the base game: %s" % plural(same_as_base, "file"))
            if unknown:
                w("      %s: winner unknown (load order not known)" % plural(unknown, "file"))
        w("")

    nothing = [e for e in idle if e[0] in removed]
    mostly = sorted((e for e in idle if e[0] not in removed),
                    key=lambda e: -idle_count(e) / len(e[0].files))
    idle_entries(nothing, "ADDONS THAT DO NOTHING", [
        "Every file in these is overwritten by an addon that loads ahead of it, is an",
        "exact copy of a file another addon provides, or is identical to the base game's",
        "own file. Removing them changes nothing in game. They were checked together,",
        "so you can remove all of them."])
    idle_entries(mostly, "ADDONS THAT MOSTLY DO NOTHING", [
        "At least half of these addons' files are overwritten or duplicated elsewhere.",
        "They still change something, so check what's left before removing them."])

    if base:
        total = sum(len(paths) for _, paths in base_copies)
        w("== ADDONS WITH COPIES OF BASE GAME FILES (%d addons, %s) ==" % (len(base_copies), plural(total, "file")))
        w("These files are identical to the game's own. On their own they change nothing,")
        w("but one can undo another addon's change to that file if it wins over it.")
        w("")
        for i, (addon, paths) in enumerate(base_copies):
            if more(i, base_copies):
                break
            w("  %s  (%d of %s)" % (label(addon), len(paths), plural(len(addon.files), "file")))
            shown = paths if not limit else paths[:5]
            for p in shown:
                w("      " + p)
            if len(paths) > len(shown):
                w("      ... and %d more" % (len(paths) - len(shown)))
        w("")

    w("== ADDON PAIRS WITH THE MOST CONFLICTS (%d) ==" % len(pairs))
    w("Each pair overwrites this many of each other's files.")
    w("")
    for i, ((a, b), (count, severe)) in enumerate(pairs):
        if more(i, pairs):
            break
        w("  %5s%s" % (plural(count, "file"), "  (%d [!])" % severe if severe else ""))
        if known(a, b):
            w("      used:        " + label(a))
            w("      overwritten: " + label(b))
        else:
            w("      " + label(a))
            w("      " + label(b))
    w("")

    w("== CONFLICTS (%d) ==" % len(conflicts))
    w("Same file, different contents. The game only uses one copy, so the other")
    w("addons lose that file. [!] = scripts, UI, cfg or maps: likely to break things.")
    w("[MODEL] = if a model's .mdl/.vvd/.vtx/.phy parts come from different addons")
    w("it can turn invisible, distort or crash the game.")
    w("")
    for i, (path, copies) in enumerate(conflicts):
        if more(i, conflicts):
            break
        tag = {2: "[!] ", 1: "[MODEL] "}.get(severity(path), "")
        w("  %s%s" % (tag, path))
        order_known = known(*(a for a, _, _ in copies))
        for n, (addon, size, crc) in enumerate(copies):
            mark = ("  <- used" if n == 0 else "") if order_known else ""
            if base.get(path) == (size, crc):
                mark = "  [same as base game]" + mark
            w("      %s  (%s)%s" % (label(addon), nice_size(size), mark))
    w("")

    w("== SAME-PATH DUPLICATES (%d) ==" % len(same_path))
    w("Same file, identical contents, in several addons. Harmless.")
    w("")
    for i, (path, copies) in enumerate(same_path):
        if more(i, same_path):
            break
        w("  " + path)
        w("      in: " + ", ".join(a.name for a, _, _ in copies))
    w("")

    w("== CONTENT DUPLICATES (%d) ==" % len(content_dupes))
    w("Identical contents under different names in different addons. Harmless,")
    w("just wasted space. Biggest first.")
    w("")
    for i, (size, group) in enumerate(content_dupes):
        if more(i, content_dupes):
            break
        w("  %s each:" % nice_size(size))
        for path, addon in group:
            w("      %s  [%s]" % (path, addon.name))

    for title, items, explain in skipped:
        if not items:
            continue
        w("")
        w("== %s (%d) ==" % (title.upper(), len(items)))
        w(explain)
        w("")
        for i, a in enumerate(items):
            if more(i, items):
                break
            w("  %s\n      %s" % (a.name, a.location))

    return "\n".join(out) + "\n"


LOAD_ORDER_NOTE = "Assumes the addon higher in the list wins."


def guess_game_dir(chosen, containers):
    """--game-dir, else the Half-Life 2 folder next to the scanned Workshop or custom folders."""
    if chosen:
        return resolve_game_dir(chosen)
    for c in containers:
        c = os.path.abspath(c)
        if os.path.basename(c) == HL2_APPID:
            game = game_dir_for_workshop(c)
        elif os.path.basename(c).lower() == "custom":
            game = resolve_game_dir(os.path.dirname(os.path.dirname(c)))
        else:
            game = None
        if game:
            return game
    return find_hl2_dirs()[0]


def main():
    global LOAD_ORDER_NOTE
    ap = argparse.ArgumentParser(description="Find Half-Life 2 addons that conflict or duplicate each other.")
    ap.add_argument("folders", nargs="*",
                    help="folders containing addons (workshop/content/220, hl2/custom...). Default: find automatically")
    ap.add_argument("--out", default="addon_report.txt", help="report file (default: addon_report.txt)")
    ap.add_argument("--limit", type=int, default=25, help="entries per section shown on screen, 0 = all (default 25)")
    ap.add_argument("--min-dupe-size", type=int, default=1024,
                    help="smallest file in bytes checked for content duplicates (default 1024)")
    ap.add_argument("--offline", action="store_true", help="don't look up Workshop item titles on Steam")
    ap.add_argument("--load-order", metavar="FILE",
                    help="the game's workshop.txt (default: Half-Life 2/hl2_complete/cfg/workshop.txt)")
    ap.add_argument("--last-wins", action="store_true",
                    help="the addon lower in workshop.txt wins conflicts, instead of the higher one")
    ap.add_argument("--game-dir", metavar="FOLDER",
                    help="the Half-Life 2 folder, for comparing addons with the base game (default: found automatically)")
    ap.add_argument("--no-base-game", action="store_true", help="don't compare addons with the base game's files")
    ap.add_argument("--find", metavar="TEXT",
                    help="list the files matching TEXT (e.g. d1_town_02 or maps/d1_town_*) and which addons change them")
    ap.add_argument("--all", action="store_true",
                    help="also check disabled addons and ones missing from workshop.txt")
    args = ap.parse_args()

    # Keep the window open when double-clicked on Windows.
    pause = sys.platform == "win32" and not args.folders and sys.stdin and sys.stdin.isatty()

    containers = args.folders or find_hl2_addon_folders()
    if not containers:
        print("Couldn't find Half-Life 2's Workshop or custom folders.")
        print(r'Pass them yourself, e.g.:  python addon_checker.py "D:\SteamLibrary\steamapps\workshop\content\220"')
        if pause:
            input("Press Enter to exit...")
        return 1

    addons = scan(containers)

    ids = {a.workshop_id for a in addons if a.workshop_id}
    if ids and not args.offline:
        titles = fetch_workshop_titles(ids)
        for a in addons:
            if a.workshop_id in titles:
                a.name = "%s [workshop %s]" % (titles[a.workshop_id], a.workshop_id)

    leftovers = [a for a in addons if a.leftover]
    addons = [a for a in addons if not a.leftover]

    disabled, unlisted = [], []
    load_order = args.load_order or find_load_order_file(containers)
    if load_order:
        try:
            entries = read_load_order(load_order)
        except OSError as e:
            print("Can't read %s: %s" % (load_order, e.strerror), file=sys.stderr)
            entries, load_order = [], None
        info = {wsid: (n, enabled) for n, (wsid, enabled) in enumerate(entries)}
        if args.last_wins:
            LOAD_ORDER_NOTE = "Assumes the addon lower in the list wins (--last-wins)."
        active = []
        for a in addons:
            if not a.workshop_id:
                active.append(a)  # custom/ addons: always loaded, order unknown
                continue
            if a.workshop_id not in info:
                unlisted.append(a)
                if args.all:
                    active.append(a)
                continue
            n, enabled = info[a.workshop_id]
            a.position = n + 1
            if not enabled:
                disabled.append(a)
                if not args.all:
                    continue
            a.rank = -n if args.last_wins else n
            active.append(a)
        if not args.all:
            addons = active

    skipped = [
        ("Disabled addons " + ("included (--all)" if args.all else "skipped"), disabled,
         "Turned off in the game's Add-ons menu."),
        ("Addons missing from workshop.txt " + ("included (--all)" if args.all else "skipped"), unlisted,
         "Installed, but not in the game's list yet. Start the game once to refresh it."),
        ("Leftover folders skipped", leftovers,
         "These only hold cache files, left behind by addons you removed. Safe to delete."),
    ]

    base, base_info = {}, "not checked (--no-base-game)" if args.no_base_game else ""
    if not args.no_base_game:
        game = guess_game_dir(args.game_dir, containers)
        if not game:
            base_info = "Half-Life 2 folder not found, so files weren't compared with it (use --game-dir)"
        else:
            print("Reading the base game's files...", file=sys.stderr)
            base, vpk_count, source, errors = read_base_game(game)
            if vpk_count:
                base_info = "%d files from %s, as listed in %s" % (len(base), plural(vpk_count, "VPK"), source)
            else:
                base_info = "no VPKs found in %s, so files weren't compared with it" % game
            for e in errors:
                print("Base game: " + e, file=sys.stderr)

    if args.find:
        everything = addons + [a for _, items, _ in skipped[:2] if not args.all for a in items]
        results, total = find_files(args.find, everything, index_files(addons), base, limit=args.limit or 10 ** 9)
        if not results:
            print("No addon has a file matching %r." % args.find)
        for path, summary, rows in results:
            print("%s%s" % ("[!] " if severity(path) == 2 else "", path))
            print("    " + summary)
            for a, status in rows:
                pos = ("#%d" % a.position) if a and a.position else "-"
                print("    %5s  %-50s  %s" % (pos, a.name if a else "Half-Life 2 (original game files)", status))
        if total > len(results):
            print("... and %d more files (use --limit 0 to see all)" % (total - len(results)))
        return 0

    analysis = analyze(addons, args.min_dupe_size, base)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(format_report(addons, skipped, containers, load_order, analysis, 0, base_info))

    print(format_report(addons, skipped, containers, load_order, analysis, args.limit, base_info))
    print("Full report: " + os.path.abspath(args.out))

    if pause:
        input("Press Enter to exit...")
    return 2 if analysis[0] else 0


if __name__ == "__main__":
    sys.exit(main())
