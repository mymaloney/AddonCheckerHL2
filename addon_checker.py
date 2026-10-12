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

Public domain (The Unlicense). Largely written by an AI model ("vibe-coded"):
see the warnings in README.md.
"""

import argparse
import bz2
import fnmatch
import gzip
import itertools
import json
import lzma
import os
import re
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections import defaultdict
from dataclasses import dataclass, field

__version__ = "1.0.0"

HL2_APPID = "220"
VPK_MAGIC = struct.pack("<I", 0x55AA1234)
VPK_ARCHIVE_PART = re.compile(r"_\d{3}\.vpk$", re.IGNORECASE)  # foo_000.vpk holds data for foo_dir.vpk

# Conflicts here replace weapon/sound scripts, UI or maps: most likely to break the game.
SEVERE_DIRS = ("scripts/", "resource/", "cfg/", "maps/")


MAX_VPK_TREE = 512 * 1024 * 1024  # real directories are a few MB
MAX_LZMA_DICT = 256 * 1024 * 1024  # LZMA allocates its dictionary up front; real ones are far smaller


class BadAddon(Exception):
    pass


# MemoryError: a corrupt header can still ask for more memory than there is (32-bit Python).
READ_ERRORS = (BadAddon, OSError, EOFError, MemoryError, lzma.LZMAError, zlib.error, zipfile.BadZipFile)
# zipfile also raises these for encrypted members and compression methods it can't read.
ZIP_ERRORS = READ_ERRORS + (RuntimeError, NotImplementedError)


@dataclass(eq=False)  # compare addons by identity
class Addon:
    name: str
    location: str
    workshop_id: str = ""
    files: dict = field(default_factory=dict)  # path -> (size, crc32)
    errors: list = field(default_factory=list)
    leftover: bool = False  # Workshop folder that only holds cache files: the addon was removed
    stray: bool = False  # in the Workshop folder but not an item folder: the game never loads it
    in_workshop: bool = False  # found in a Workshop folder
    custom_off: bool = False  # custom/ addon moved to custom_disabled/ (the organizer's "off")
    rank: int = None  # load order position, lower = wins conflicts; None = unknown
    position: int = None  # 1-based line in workshop.txt, for display


def is_cache(name):
    """Caches the game generates (workshop.vpk.sound.cache, sound/sound.cache) aren't addon content."""
    return name.lower().endswith(".cache")


def norm(path):
    """Source paths are case-insensitive and use either slash."""
    return path.replace("\\", "/").strip("/").lower()


def name_order(name):
    """Sort key for file names: case-insensitive, with a tie-break so the order never depends on the disk."""
    return name.lower(), name


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


def _raw_lzma(path, skip, props, dict_size, size):
    """Opens LZMA data that starts `skip` bytes into the file. The decoder allocates dict_size
    up front, so it's checked first: a text file or a corrupt header can claim gigabytes.
    No match reaches further back than the data decoded so far, so a dictionary larger than
    the uncompressed size is never needed."""
    if props >= 225:
        raise BadAddon("not LZMA data")
    if size is not None:
        dict_size = min(dict_size, max(size, 4096))
    if dict_size > MAX_LZMA_DICT:
        raise BadAddon("LZMA header asks for a %d MB dictionary, which can't be right" % (dict_size >> 20))
    lc, props = props % 9, props // 9
    lp, pb = props % 5, props // 5
    filt = {"id": lzma.FILTER_LZMA1, "dict_size": max(dict_size, 4096), "lc": lc, "lp": lp, "pb": pb}
    f = open(path, "rb")
    try:
        f.seek(skip)
        return ClosingReader(lzma.open(f, format=lzma.FORMAT_RAW, filters=[filt]), f)
    except Exception:
        f.close()
        raise


class ClosingReader:
    """lzma.open() leaves a file object it was given open. On Windows an open file is
    locked, so close it along with the stream."""

    def __init__(self, stream, f):
        self.stream, self.f = stream, f

    def read(self, n):
        return self.stream.read(n)

    def close(self):
        try:
            self.stream.close()
        finally:
            self.f.close()


def _valve_lzma(path):
    # Valve's own header: "LZMA", uncompressed size, compressed size, 5 LZMA property bytes
    with open(path, "rb") as f:
        head = f.read(17)
    size, props, dict_size = struct.unpack_from("<I", head, 4)[0], head[12], struct.unpack_from("<I", head, 13)[0]
    return _raw_lzma(path, 17, props, dict_size, size)


def _plausible_lzma_dict(d):
    """The .lzma encoders only write 2^n or 2^n + 2^(n-1). Checking this keeps ordinary text
    files (readme.txt, addoninfo.txt) from being taken for headerless LZMA."""
    if d < 4096:
        return False
    top = 1 << (d.bit_length() - 1)
    return d in (top, top + (top >> 1))


def _lzma_alone(path):
    # .lzma ("LZMA alone"): 5 property bytes, then the uncompressed size or -1 if unknown
    with open(path, "rb") as f:
        head = f.read(13)
    props, dict_size, size = head[0], struct.unpack_from("<I", head, 1)[0], struct.unpack_from("<Q", head, 5)[0]
    if not _plausible_lzma_dict(dict_size) or (size != 2 ** 64 - 1 and size >= 2 ** 40):
        raise BadAddon("not LZMA data")
    return _raw_lzma(path, 13, props, dict_size, None if size == 2 ** 64 - 1 else size)


# (name, test on the first bytes, opener). Every candidate must also decompress to a VPK header.
OPENERS = [
    ("VPK", lambda h: h[:4] == VPK_MAGIC, lambda p: open(p, "rb")),
    ("Valve LZMA", lambda h: h[:4] == b"LZMA" and len(h) >= 17, _valve_lzma),
    ("xz", lambda h: h[:6] == b"\xfd7zXZ\x00", lambda p: lzma.open(p, format=lzma.FORMAT_XZ)),
    ("gzip", lambda h: h[:2] == b"\x1f\x8b", lambda p: gzip.open(p, "rb")),
    ("bzip2", lambda h: h[:3] == b"BZh", lambda p: bz2.open(p, "rb")),
    ("zlib", lambda h: len(h) >= 2 and h[0] & 0x0F == 8 and (h[0] << 8 | h[1]) % 31 == 0,
     lambda p: ZlibReader(open(p, "rb"))),
    ("LZMA", lambda h: len(h) >= 13 and h[0] < 225, _lzma_alone),
]


def read_exact(stream, n):
    """Reads in pieces, so a size from a corrupt header can't allocate it all before the
    file turns out to be shorter."""
    parts, left = [], n
    while left:
        data = stream.read(min(left, 1024 * 1024))
        if not data:
            raise BadAddon("file is truncated")
        parts.append(data)
        left -= len(data)
    return b"".join(parts)


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
        except (BadAddon, OSError, EOFError, MemoryError, lzma.LZMAError, zlib.error, ValueError):
            pass
    return None


def parse_vpk(stream, files, magic_read=False):
    """Reads a VPK's directory tree (v1 or v2) into files. Only the tree is read, not the data.
    Returns how many entries the tree lists."""
    if not magic_read and read_exact(stream, 4) != VPK_MAGIC:
        raise BadAddon("not a VPK file")
    version, tree_size = struct.unpack("<II", read_exact(stream, 8))
    if tree_size > MAX_VPK_TREE:
        raise BadAddon("VPK directory claims to be %d MB, which can't be right" % (tree_size >> 20))
    if version == 2:
        read_exact(stream, 16)  # section sizes we don't need
    elif version != 1:
        raise BadAddon("unsupported VPK version %d" % version)
    tree = read_exact(stream, tree_size)

    pos = count = 0

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
            return count
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
                count += 1


def read_packed_file(addon, path):
    """Adds a (compressed) VPK or a zip of VPKs to the addon. Returns how many files it lists,
    or None if it's neither."""
    now_reading(path)
    # Check for a VPK first: a VPK whose last file is a map ends with the map's packed-files
    # zip, so it can also look like a zip file.
    opener = detect_vpk(path)
    if opener:
        s = opener(path)
        try:
            return parse_vpk(s, addon.files)
        finally:
            s.close()

    if zipfile.is_zipfile(path):
        found = None
        with zipfile.ZipFile(path) as z:
            for member in z.namelist():
                # Only VPKs matter. Other members (an encrypted readme, a format zipfile can't
                # decompress) would only raise errors that have nothing to do with the addon.
                if not member.lower().endswith(".vpk") or VPK_ARCHIVE_PART.search(member):
                    continue
                try:
                    with z.open(member) as s:
                        if s.read(4) == VPK_MAGIC:
                            found = (found or 0) + parse_vpk(s, addon.files, magic_read=True)
                except ZIP_ERRORS as e:
                    addon.errors.append("%s, %s: %s" % (os.path.basename(path), member, error_text(e)))
                    found = found or 0  # it is the addon's content, just unreadable
        return found
    return None


def error_text(e):
    """str() of some exceptions (MemoryError) is empty."""
    return getattr(e, "strerror", None) or str(e) or type(e).__name__


# ---------------------------------------------------------------------------
# Scanning addons
# ---------------------------------------------------------------------------

def crc_file(path):
    now_reading(path)
    crc, size = 0, 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)  # same CRC32 VPK directories store
            size += len(chunk)
    return size, crc & 0xFFFFFFFF


def dir_key(path):
    """Identifies a folder on disk, following links and junctions, to stop at loops.
    st_ino can be 0 on some file systems (FAT, network shares); fall back to the real path there."""
    st = os.stat(path)
    if st.st_ino:
        return st.st_dev, st.st_ino
    return os.path.normcase(os.path.realpath(path))


def is_link(path):
    """Symlinks, and on Windows also junctions (os.path.isjunction only exists from 3.12)."""
    if os.path.islink(path):
        return True
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def scan_folder_addon(addon, root):
    def walk_error(e):
        addon.errors.append("can't open %s: %s" % (os.path.relpath(e.filename or root, root), error_text(e)))

    try:
        with os.scandir(root) as it:
            entries = sorted(it, key=lambda e: name_order(e.name))
    except OSError as e:
        walk_error(e)
        return

    unknown = []
    root_names = {e.name.lower() for e in entries}
    content = False  # anything besides cache files, readable or not
    seen = {dir_key(root)}  # don't loop through junctions / links that point back up the tree
    for entry in entries:
        if is_link(entry.path):
            content = True
        if entry.is_dir():
            # Loose content: materials/, models/, sound/...
            for dirpath, dirs, names in os.walk(entry.path, onerror=walk_error, followlinks=True):
                try:
                    key = dir_key(dirpath)
                except OSError as e:
                    walk_error(e)
                    dirs[:] = []
                    continue
                if key in seen:
                    dirs[:] = []
                    continue
                seen.add(key)
                dirs.sort(key=name_order)
                for d in dirs:
                    if is_link(os.path.join(dirpath, d)):
                        content = True
                for name in sorted(names, key=name_order):
                    if is_cache(name):
                        continue
                    content = True
                    full = os.path.join(dirpath, name)
                    rel = norm(os.path.relpath(full, root))
                    try:
                        addon.files.setdefault(rel, crc_file(full))
                    except OSError as e:
                        addon.errors.append("can't read %s: %s" % (rel, error_text(e)))
        elif entry.is_file():
            if is_cache(entry.name):
                continue
            content = True
            if VPK_ARCHIVE_PART.search(entry.name):
                # foo_000.vpk is data for foo_dir.vpk. Without that, nothing of it can be read.
                if VPK_ARCHIVE_PART.sub("_dir.vpk", entry.name.lower()) not in root_names:
                    addon.errors.append("%s: its directory file (..._dir.vpk) is missing" % entry.name)
                continue
            try:
                count = read_packed_file(addon, entry.path)
                if count is None:
                    unknown.append(entry)
                elif count == 0 and not entry.name.lower().endswith(".zip"):
                    addon.errors.append("%s: the VPK has no files in it" % entry.name)
            except READ_ERRORS as e:
                addon.errors.append("%s: %s" % (entry.name, error_text(e)))

    # Removing a Workshop addon deletes its VPK but leaves the game's cache file behind.
    if addon.in_workshop and not content and not addon.errors:
        addon.leftover = True

    # Root files like addoninfo.txt are normal. Only worth mentioning if nothing else was readable.
    if not addon.files and not addon.errors and unknown:
        details = []
        for e in unknown[:3]:
            try:
                with open(e.path, "rb") as f:
                    head = f.read(8)
                details.append("%s (starts with %s)" % (e.name, " ".join("%02x" % b for b in bytearray(head))))
            except OSError as err:
                details.append("%s (%s)" % (e.name, error_text(err)))
        addon.errors.append("no readable content, unrecognized file format: " + ", ".join(details))


def collect_addons(containers, workshop_dirs=()):
    """Each container is a folder of addons (workshop/content/220 or a custom/ folder), or a single .vpk.
    workshop_dirs: containers to treat as Workshop folders even if they aren't named .../content/220.
    Returns ([(addon, "folder" or "vpk")], [(container, error) for containers that couldn't be opened])."""
    workshop_keys = {os.path.normcase(os.path.realpath(d)) for d in workshop_dirs}
    addons, failed, seen = [], [], set()
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
                entries = [os.path.join(container, n) for n in sorted(os.listdir(container), key=name_order)]
            except OSError as e:
                failed.append((container, error_text(e)))
                continue
            label = parent_name(container) + "/" + os.path.basename(container)

        is_workshop = key in workshop_keys or is_workshop_dir(container)
        if is_workshop:
            label = "workshop"
        for path in entries:
            name = os.path.basename(path)
            if os.path.isdir(path):
                addon, kind = Addon(name=label + "/" + name, location=path), "folder"
            elif name.lower().endswith(".vpk") and not VPK_ARCHIVE_PART.search(name):
                addon, kind = Addon(name=label + "/" + name, location=path), "vpk"
            else:
                continue
            if is_workshop:
                addon.in_workshop = True
                # The game loads Workshop items by id. A copied folder ("1234 - Copy") or a
                # VPK dropped in here is never loaded, so it can't stand in for a real item.
                if kind == "folder" and name.isdigit():
                    addon.workshop_id = name
                else:
                    addon.stray = True
            addons.append((addon, kind))
    return addons, failed


def is_workshop_dir(path):
    """.../workshop/content/220, in any letter case."""
    path = os.path.normpath(path)
    return os.path.basename(path) == HL2_APPID and parent_name(path).lower() == "content"


def parent_name(path):
    return os.path.basename(os.path.dirname(path))


# The file being read right now, so a slow or stuck read can be reported.
_reading = {"path": None, "since": 0.0}


def now_reading(path):
    _reading["path"], _reading["since"] = path, time.time()


def now_reading_path():
    return _reading["path"]


class SlowReadWatch:
    """While active, calls slow(path, seconds) every 10 seconds that one file is still being read."""

    def __init__(self, slow):
        self.slow = slow
        self.stop = threading.Event()

    def __enter__(self):
        def watch():
            reported = None
            while not self.stop.wait(1):
                path, since = _reading["path"], _reading["since"]
                waited = int(time.time() - since)
                if path and waited >= 10 and (path, waited // 10) != reported:
                    reported = (path, waited // 10)
                    self.slow(path, waited)
        if self.slow:
            threading.Thread(target=watch, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        now_reading(None)


def print_progress(i, total, name=""):
    line = "Reading addon %d/%d: %s" % (i, total, name)
    print("\r" + line[:110].ljust(110), end="" if i < total else "\n", file=sys.stderr, flush=True)


def print_slow(path, seconds):
    print("\n  still reading %s (%ds)..." % (path, seconds), file=sys.stderr, flush=True)


def read_addon(addon, kind):
    now_reading(addon.location)
    try:
        if kind == "folder":
            scan_folder_addon(addon, addon.location)
        else:
            try:
                if read_packed_file(addon, addon.location) is None:
                    addon.errors.append("not a VPK file")
            except READ_ERRORS as e:
                addon.errors.append(error_text(e))
    except Exception as e:  # one odd addon must never stop the whole scan
        addon.errors.append("couldn't read %s: %s" % (now_reading_path() or addon.location, error_text(e)))
    # Files at the addon root (addoninfo.txt...) and caches aren't game content.
    addon.files = {p: v for p, v in addon.files.items() if "/" in p and not is_cache(p)}


def scan(containers, progress=print_progress, workshop_dirs=(), slow=print_slow, failed=None):
    """Reads every addon in containers. Containers that can't be opened are added to `failed`
    as (container, error) if a list is given."""
    addons, missing = collect_addons(containers, workshop_dirs)
    if failed is not None:
        failed.extend(missing)
    else:
        for container, error in missing:
            print("Can't open %s: %s" % (container, error), file=sys.stderr)
    with SlowReadWatch(slow):
        for i, (addon, kind) in enumerate(addons, 1):
            progress(i, len(addons), addon.name)
            read_addon(addon, kind)
    return [a for a, _ in addons]


# ---------------------------------------------------------------------------
# Workshop titles
# ---------------------------------------------------------------------------

def app_dir():
    """The tool's own folder, for its settings and caches. In a PyInstaller .exe, __file__ is in a
    temporary folder that's deleted on exit, so it's the .exe's folder instead."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def title_cache_file():
    return os.path.join(app_dir(), "addon_titles.json")


def cached_titles(ids, path=None):
    """Titles from earlier lookups, so they show without a connection too."""
    try:
        with open(path or title_cache_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {i: data[i] for i in ids if isinstance(data.get(i), str)}


def save_titles(titles, path=None):
    path = path or title_cache_file()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data.update(titles)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass  # only a cache


def fetch_workshop_titles(ids, deadline=20, cache=True):
    """Looks up item titles with Steam's public API. Gives up after `deadline` seconds. Titles
    found are remembered (cache=True), and earlier ones fill in what Steam doesn't answer."""
    titles = {}
    worker = threading.Thread(target=_fetch_titles, args=(sorted(ids), titles), daemon=True)
    worker.start()
    worker.join(deadline)
    titles = dict(titles)
    if not cache:
        return titles
    if titles:
        save_titles(titles)
    known = cached_titles(ids)
    known.update(titles)
    return known


def _fetch_titles(ids, titles):
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
            return
        for d in details:
            if d.get("title"):
                titles[str(d["publishedfileid"])] = d["title"]


# ---------------------------------------------------------------------------
# Base game files
# ---------------------------------------------------------------------------

# Used when gameinfo.txt can't be read: earlier folders win, like the game's own search path.
FALLBACK_GAME_FOLDERS = ["hl2_complete", "ep2", "episodic", "hl2", "lostcoast", "platform"]


GAMEINFO_FOLDERS = ("hl2_complete", "hl2")


def gameinfo_search_paths(game_dir, folders=GAMEINFO_FOLDERS):
    """SearchPaths of the first readable gameinfo.txt in folders, highest priority first:
    ([(keys, path)], gameinfo.txt path or None). Paths are resolved to full paths; a trailing
    "*" (e.g. custom/*) is kept."""
    for folder in folders:
        gameinfo = os.path.join(game_dir, folder, "gameinfo.txt")
        try:
            with open(gameinfo, encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        m = re.search(r'"?SearchPaths"?\s*\{(.*?)\}', text, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        paths = []
        for line in m.group(1).splitlines():
            line = line.split("//")[0]
            parts = re.findall(r'"([^"]*)"|(\S+)', line)
            parts = [a or b for a, b in parts]
            if len(parts) < 2:
                continue
            value = parts[1].replace("|gameinfo_path|", os.path.dirname(gameinfo) + "/")
            value = value.replace("|all_source_engine_paths|", game_dir + "/")
            paths.append((parts[0].lower(), os.path.normpath(value if os.path.isabs(value) else os.path.join(game_dir, value))))
        return paths, gameinfo
    return [], None


def gameinfo_vpks(game_dir):
    """The VPKs the game mounts, highest priority first, read from gameinfo.txt's SearchPaths.
    Returns (list of _dir.vpk / .vpk paths, gameinfo.txt path or None)."""
    for folder in GAMEINFO_FOLDERS:
        paths, gameinfo = gameinfo_search_paths(game_dir, (folder,))
        vpks = []
        for _, path in paths:
            if not path.lower().endswith(".vpk") or "*" in path:
                continue
            # "hl2/hl2_pak.vpk" in gameinfo means hl2/hl2_pak_dir.vpk on disk.
            for cand in (path[:-4] + "_dir.vpk", path):
                if os.path.isfile(cand) and cand not in vpks:
                    vpks.append(cand)
                    break
        if vpks:
            return vpks, gameinfo
    return [], None


# Used when gameinfo.txt can't be read: the custom folders Half-Life 2 and its episodes mount.
FALLBACK_CUSTOM_FOLDERS = ["hl2_complete", "ep2", "episodic", "hl2"]


def mounted_custom_dirs(game_dir, existing=True):
    """The custom/ folders the game loads addons from: gameinfo.txt's "custom/*" entries, in
    the game's order. Other games that share the Half-Life 2 folder (Lost Coast, Half-Life:
    Source) have their own custom/ folders, which Half-Life 2 never loads.
    existing=False also returns folders that don't exist yet (to install into)."""
    paths, gameinfo = gameinfo_search_paths(game_dir)
    if gameinfo:
        dirs = []
        for keys, path in paths:
            if "game" in keys and os.path.basename(path) == "*":
                folder = os.path.dirname(path)
                if folder not in dirs:
                    dirs.append(folder)
    else:
        dirs = [os.path.join(game_dir, sub, "custom") for sub in FALLBACK_CUSTOM_FOLDERS]
    return [d for d in dirs if os.path.isdir(d)] if existing else dirs


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


def read_base_game(game_dir, slow=print_slow):
    """The original game's files as the game sees them: path -> (size, crc32).
    Returns (files, number of VPKs read, where the list of VPKs came from, errors)."""
    vpks, source = gameinfo_vpks(game_dir)
    if not vpks:
        vpks, source = fallback_vpks(game_dir), "the game folders (gameinfo.txt not readable)"
    base = Addon(name="Half-Life 2 (base game)", location=game_dir)
    errors = []
    with SlowReadWatch(slow):
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
    # Key and value on one line, so a number on its own (old "ContentStatsID" values) isn't a key.
    for m in re.finditer(r'^[ \t]*"(?:path|\d+)"[ \t]+"((?:[^"\\]|\\.)*)"', text, re.MULTILINE):
        path = m.group(1).replace("\\\\", "\\")
        if os.path.isdir(os.path.join(path, "steamapps")):
            libs.append(path)
    return libs


def find_load_order_file(containers):
    """hl2_complete/cfg/workshop.txt in the game folder of the library that holds the Workshop items."""
    for c in containers:
        c = os.path.abspath(c)
        if is_workshop_dir(c):
            steamapps = os.path.dirname(os.path.dirname(os.path.dirname(c)))
            path = os.path.join(steamapps, "common", "Half-Life 2", "hl2_complete", "cfg", "workshop.txt")
            if os.path.isfile(path):
                return path
    return None


def keyvalues_tokens(text):
    """Tokens of a Valve KeyValues file: quoted or bare strings and braces, without // comments."""
    return [m.group(1) if m.group(1) is not None else m.group(2)
            for m in re.finditer(r'//[^\n]*|"([^"]*)"|([{}]|[^\s{}"]+)', text)
            if m.group(1) is not None or m.group(2) is not None]


def read_load_order(path):
    """workshop.txt: "AddonInfo" { "<id>" "1|0" ... }, in load order. Returns [(id, enabled)]."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        tokens = keyvalues_tokens(f.read())
    if "{" in tokens:
        tokens = tokens[tokens.index("{") + 1:]
    entries = []
    for key, value in zip(tokens[::2], tokens[1::2]):
        if key in ("{", "}") or value in ("{", "}"):
            break
        if key.isdigit():
            entries.append((key, value != "0"))
    return entries


def write_load_order(path, entries):
    """Writes workshop.txt in the game's own format. entries: [(id, enabled)] in load order."""
    root = "AddonInfo"
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            tokens = keyvalues_tokens(f.read())
        if tokens and tokens[0] not in ("{", "}"):
            root = tokens[0]  # keep whatever the game named it
    except OSError:
        pass
    lines = ['"%s"' % root, "{"]
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
        containers += mounted_custom_dirs(game_dir)
        path = os.path.join(game_dir, "hl2_complete", "cfg", "workshop.txt")
        load_order = path if os.path.isfile(path) else None
    return containers, load_order


def find_install(game_dir=None):
    """(Half-Life 2 folder, Workshop folder) to use: the given game folder, or the install
    Steam knows about. One folder is enough to find the other in the same Steam library."""
    game = resolve_game_dir(game_dir) if game_dir else None
    workshop = workshop_dir_for_game(game) if game else None
    if not game:
        game, workshop = find_hl2_dirs()
    workshop = workshop or (game and workshop_dir_for_game(game))
    game = game or (workshop and game_dir_for_workshop(workshop))
    return game or None, workshop or None


# ---------------------------------------------------------------------------
# Analysis + report
# ---------------------------------------------------------------------------

def rank_custom_addons(addons, last_wins=True):
    """The game loads custom/ addons ahead of every Workshop addon, A to Z by folder name, and the
    addon that loads later wins. The scan already lists them in that order (custom/ folder by
    custom/ folder, in gameinfo.txt's order), so they rank behind every Workshop addon (whose
    ranks are 0, -1, -2... from workshop.txt), Z beating A. Returns them in load order.
    last_wins=False is the opposite model, kept for tests of the ranking itself."""
    custom = [a for a in addons if not a.workshop_id and not a.stray and not a.leftover and not a.custom_off]
    for n, a in enumerate(custom):
        a.rank = len(custom) - n if last_wins else n - len(custom)
    return custom


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
    an addon that loads after it overwrites it, when another addon has an
    identical copy that would take over, or when it's identical to the base
    game's own file and no other addon has it. Addons where every file does
    nothing can be removed without changing the game.

    Addons are checked least important first, and each removable one is taken
    out before checking the next, so all of them can be removed together
    (of two identical addons, only the one that loses is removable).

    Addons that couldn't be fully read are never removable: their unread part
    may be what matters. Their readable files still count for the others.

    Returns (removable, results, together).
    results: (addon, overwritten_by, copies_in, unknown, same_as_base, reverted_by) for the
    removable addons and the ones that mostly do nothing. reverted_by counts files overwritten
    by an addon whose copy is the base game's own.
    together: removable addon -> the other removable addons it may only be removed with
    (empty if it can go on its own). Removing one without those can change the game.
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
                best = min(others, key=lambda c: c[0].rank)
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

    removed, order, results = set(), [], []
    for addon in sorted(addons, key=lambda a: (a.rank is None, -(a.rank or 0))):
        if not addon.files or addon.errors:
            continue
        stats = contribution(addon, removed)
        if idle_count(stats) == len(addon.files):
            removed.add(addon)
            order.append(addon)
            results.append((addon,) + stats)

    # Each one was checked with the ones before it already gone. If it isn't idle on its own,
    # it depends on those of them that share its files (and on what those depend on).
    together = {}
    for n, addon in enumerate(order):
        if idle_count(contribution(addon, set())) == len(addon.files):
            together[addon] = []
            continue
        earlier = set(order[:n])
        need = []
        for path in addon.files:
            for c in by_path[path]:
                if c[0] in earlier and c[0] not in need:
                    need.append(c[0])
        for other in list(need):
            for dep in together.get(other, []):
                if dep not in need:
                    need.append(dep)
        together[addon] = need

    # The rest, against the final set of removable addons.
    for addon in addons:
        if addon.files and not addon.errors and addon not in removed:
            stats = contribution(addon, removed)
            if idle_count(stats) * 2 >= len(addon.files):
                results.append((addon,) + stats)
    return removed, results, together


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


# The parts the game loads for a model. .dx80.vtx / .sw.vtx are only used below DirectX 9.
MODEL_PARTS = (".mdl", ".vvd", ".dx90.vtx", ".phy", ".ani")


def model_part(path):
    """(model path without extension, part) for a model file, else None."""
    if path.startswith("models/"):
        for ext in MODEL_PARTS:
            if path.endswith(ext):
                return path[:-len(ext)], ext
    return None


def find_mixed_models(by_path):
    """
    Models the game builds from parts of different addons. It takes each part (.mdl, .vvd,
    .dx90.vtx, .phy, .ani) from the last addon to load that has it, and the parts have to match:
    a .vvd or .vtx from another build of the model makes it invisible, distorted or crash.

    A model is mixed when no single addon has every part the game ends up using (identical
    copies count as the same). With an unknown order (custom/ addons), every order is tried.
    Parts that come from the original game aren't counted: a reskin legitimately ships only
    a hex-edited .mdl that still matches the original .vvd and .vtx.

    Returns [(model, order_known, [(part, copies sorted by load order when known)])].
    """
    models = defaultdict(dict)
    for path, copies in by_path.items():
        mp = model_part(path)
        if mp:
            models[mp[0]][mp[1]] = copies

    def mixed(parts, order):
        used = {}
        for ext, copies in parts.items():
            owner = {c[0]: c for c in copies}
            used[ext] = next(owner[a] for a in order if a in owner)
        for a in order:
            if all(any(c[0] is a and (c[1], c[2]) == (u[1], u[2]) for c in parts[ext])
                   for ext, u in used.items()):
                return False
        return True

    out = []
    for model in sorted(models):
        parts = models[model]
        addons = []
        for ext in MODEL_PARTS:
            for c in parts.get(ext, []):
                if c[0] not in addons:
                    addons.append(c[0])
        if len(addons) < 2:
            continue
        order_known = known(*addons)
        if order_known:
            is_mixed = mixed(parts, sorted(addons, key=lambda a: a.rank))
        elif len(addons) <= 6:
            is_mixed = any(mixed(parts, list(order)) for order in itertools.permutations(addons))
        else:
            is_mixed = True  # too many to try every order; flag it to be safe
        if is_mixed:
            out.append((model, order_known,
                        [(ext, by_rank(parts[ext]) if order_known else parts[ext])
                         for ext in MODEL_PARTS if ext in parts]))
    return out


def analyze(addons, min_dupe_size, base=None, loaded=None):
    """loaded: the addons the game really loads, if not all of `addons` (--all adds disabled
    ones). Only those can make another addon removable."""
    base = base or {}
    by_path = index_files(addons)

    conflicts, same_path = [], []
    for path, copies in sorted(by_path.items()):
        if len(copies) < 2:
            continue
        identical = len({(s, c) for _, s, c in copies}) == 1
        (same_path if identical else conflicts).append((path, by_rank(copies)))
    conflicts.sort(key=lambda item: (-severity(item[0]), item[0]))

    # Identical contents under different paths, in different addons. Every copy counts:
    # per path, each distinct content once, with every addon that has it.
    by_content = defaultdict(list)  # (size, crc) -> [(path, [addons])]
    for path, copies in by_path.items():
        owners = defaultdict(list)
        for addon, size, crc in copies:
            owners[(size, crc)].append(addon)
        for (size, crc), who in owners.items():
            if size >= max(min_dupe_size, 1):
                by_content[(size, crc)].append((path, sorted(who, key=lambda a: a.name.lower())))
    content_dupes = []
    for (size, _), group in by_content.items():
        if len(group) > 1 and len({id(a) for _, who in group for a in who}) > 1:
            content_dupes.append((size, sorted(group, key=lambda g: g[0])))
    content_dupes.sort(key=lambda item: (-item[0], item[1][0][0]))

    # Which addons fight the most. With a known load order the pair is (winner, loser), and
    # only copies that differ from the one used count; otherwise every two different copies.
    pair_counts = defaultdict(lambda: [0, 0])
    for path, copies in conflicts:
        severe = severity(path) == 2
        if known(*(c[0] for c in copies)):
            winner = copies[0]
            losers = []
            for c in copies[1:]:
                if (c[1], c[2]) != (winner[1], winner[2]) and c[0] is not winner[0] and c[0] not in losers:
                    losers.append(c[0])
            pairs_here = [(winner[0], loser) for loser in losers]
        else:
            pairs_here = set()
            for i in range(len(copies)):
                for j in range(i + 1, len(copies)):
                    a, b = copies[i], copies[j]
                    if (a[1], a[2]) != (b[1], b[2]) and a[0] is not b[0]:
                        pairs_here.add((a[0], b[0]) if a[0].name.lower() <= b[0].name.lower() else (b[0], a[0]))
        for pair in pairs_here:
            counts = pair_counts[pair]
            counts[0] += 1
            counts[1] += severe
    pairs = sorted(pair_counts.items(), key=lambda item: (-item[1][0], item[0][0].name, item[0][1].name))

    if loaded is None:
        removed, idle, together = find_idle_addons(addons, by_path, base)
    else:
        removed, idle, together = find_idle_addons(loaded, index_files(loaded), base)

    # Files identical to the base game's own, per addon, most first.
    base_copies = []
    for addon in addons:
        same = sorted(p for p, content in addon.files.items() if base.get(p) == content)
        if same:
            base_copies.append((addon, same))
    base_copies.sort(key=lambda item: (-len(item[1]), item[0].name.lower()))

    return Analysis(conflicts, same_path, content_dupes, pairs, removed, idle, base_copies, base,
                    find_mixed_models(by_path), together)


class Analysis(tuple):
    """What analyze() found. Unpacks like the original 8-tuple; the newer results are attributes."""

    def __new__(cls, conflicts, same_path, content_dupes, pairs, removed, idle, base_copies, base,
                mixed_models, together):
        self = tuple.__new__(cls, (conflicts, same_path, content_dupes, pairs, removed, idle, base_copies, base))
        self.mixed_models, self.together = mixed_models, together
        return self


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


# The pairs section can have a line for every two addons; past this it's noise, even in the file.
MAX_PAIRS = 200


def format_report(addons, skipped, containers, load_order, analysis, limit, base_info="", failed=()):
    conflicts, same_path, content_dupes, pairs, removed, idle, base_copies, base = analysis
    mixed_models = getattr(analysis, "mixed_models", [])
    together = getattr(analysis, "together", {})
    out = []
    w = out.append

    total_files = sum(len(a.files) for a in addons)
    w("HL2 Addon Checker %s: %d addons, %d files" % (__version__, len(addons), total_files))
    w("%d conflicts, %d mixed models, %d same-path duplicates, %d content duplicates"
      % (len(conflicts), len(mixed_models), len(same_path), len(content_dupes)))
    w("%d addons do nothing and can be removed" % len(removed))
    for title, items, _ in skipped:
        if items:
            w("%d %s (listed at the end)" % (len(items), title.lower()))
    w("")
    w("Scanned:")
    failed = dict(failed)
    for c in containers:
        w("  " + c + ("  (couldn't open: %s)" % failed[os.path.abspath(c)] if os.path.abspath(c) in failed else ""))
    if load_order:
        w("Load order: " + load_order)
        w("  [#N] = position in that list. " + LOAD_ORDER_NOTE)
    else:
        w("Load order: workshop.txt not found, so the report can't say which Workshop addon wins a conflict.")
    w("custom/ addons load ahead of Workshop addons, A to Z by folder name.")
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
            if together.get(addon):
                w("      only together with: " + ", ".join(label(a) for a in together[addon][:5])
                  + (", and %d more" % (len(together[addon]) - 5) if len(together[addon]) > 5 else ""))
        w("")

    nothing = [e for e in idle if e[0] in removed]
    mostly = sorted((e for e in idle if e[0] not in removed),
                    key=lambda e: -idle_count(e) / len(e[0].files))
    idle_entries(nothing, "ADDONS THAT DO NOTHING", [
        "Every file in these is overwritten by an addon that loads after it, is an",
        "exact copy of a file another addon provides, or is identical to the base game's",
        "own file. Removing all of them changes nothing in game. An addon marked",
        "\"only together with\" must be removed along with those, or the game changes."])
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
        if i >= MAX_PAIRS:
            w("  ... and %d more pairs" % (len(pairs) - i))
            break
        w("  %5s%s" % (plural(count, "file"), "  (%d [!])" % severe if severe else ""))
        if known(a, b):
            w("      used:        " + label(a))
            w("      overwritten: " + label(b))
        else:
            w("      " + label(a))
            w("      " + label(b))
    w("")

    w("== MODELS BUILT FROM DIFFERENT ADDONS (%d) ==" % len(mixed_models))
    w("The game takes each part of a model (.mdl .vvd .dx90.vtx .phy .ani) from the")
    w("last addon to load that has it, so these models end up made of different addons'")
    w("parts. Parts that don't match make a model invisible or distorted, or crash")
    w("the game. Keep one addon's full set of parts, or have it load after the others.")
    w("")
    for i, (model, order_known, parts) in enumerate(mixed_models):
        if more(i, mixed_models):
            break
        w("  [MODEL] %s%s" % (model, "" if order_known else "  (in some load orders; order unknown)"))
        for ext, copies in parts:
            if order_known:
                text = label(copies[0][0])
                others = [label(c[0]) for c in copies[1:]]
                if others:
                    text += "  (ignored: %s)" % ", ".join(others)
            else:
                text = ", ".join(label(c[0]) for c in copies)
            w("      %-10s %s" % (ext, text))
    w("")

    w("== CONFLICTS (%d) ==" % len(conflicts))
    w("Same file, different contents. The game only uses one copy, so the other")
    w("addons lose that file. [!] = scripts, UI, cfg or maps: likely to break things.")
    w("[MODEL] = part of a model; see MODELS BUILT FROM DIFFERENT ADDONS above.")
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
        for path, owners in group:
            w("      %s  [%s]" % (path, ", ".join(a.name for a in owners)))

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


LOAD_ORDER_NOTE = "The addon lower in the list loads later and wins."


def guess_game_dir(chosen, containers):
    """--game-dir, else the Half-Life 2 folder next to the scanned Workshop or custom folders."""
    if chosen:
        return resolve_game_dir(chosen)
    for c in containers:
        c = os.path.abspath(c)
        if is_workshop_dir(c):
            game = game_dir_for_workshop(c)
        elif os.path.basename(c).lower() == "custom":
            game = resolve_game_dir(os.path.dirname(os.path.dirname(c)))
        else:
            game = None
        if game:
            return game
    return find_hl2_dirs()[0]


def double_clicked():
    """Started by double-clicking on Windows: a console window that closes as soon as we exit."""
    return sys.platform == "win32" and len(sys.argv) == 1 and bool(sys.stdin) and sys.stdin.isatty()


def use_safe_output():
    """Workshop titles can have any characters. Redirected to a file or a pipe, Windows would
    write in its ANSI code page and stop at the first character that has no code there."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # an unusual stream: leave it as it is
            pass


def main():
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
    # The game was found to always work this way; the option is still accepted.
    ap.add_argument("--last-wins", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--game-dir", metavar="FOLDER",
                    help="the Half-Life 2 folder (default: found automatically). Without folders to scan, "
                         "its Workshop and custom folders are scanned")
    ap.add_argument("--no-base-game", action="store_true", help="don't compare addons with the base game's files")
    ap.add_argument("--find", metavar="TEXT",
                    help="list the files matching TEXT (e.g. d1_town_02 or maps/d1_town_*) and which addons change them")
    ap.add_argument("--all", action="store_true",
                    help="also check disabled addons and ones missing from workshop.txt")
    ap.add_argument("--version", action="version", version="HL2 Addon Checker " + __version__)
    args = ap.parse_args()
    use_safe_output()

    # A --load-order that can't be read is a typo or the wrong file: say so before the slow part.
    if args.load_order:
        try:
            read_load_order(args.load_order)
        except OSError as e:
            print("Can't read %s: %s" % (args.load_order, error_text(e)), file=sys.stderr)
            return 1

    game = workshop = None
    if args.folders:
        containers = args.folders
        load_order = args.load_order or find_load_order_file(containers)
    else:
        # The game only loads the Workshop and custom folders of the library it's installed in.
        # Other libraries can still hold an old install's folders.
        game, workshop = find_install(args.game_dir)
        containers, load_order = game_folders(game, workshop)
        load_order = args.load_order or load_order
        if containers:
            print("Half-Life 2: %s" % (game or "not found"), file=sys.stderr)
            print("Workshop:    %s" % (workshop or "not found"), file=sys.stderr)
    if not containers:
        print("Couldn't find Half-Life 2's Workshop or custom folders.")
        print(r'Pass them yourself, e.g.:  python addon_checker.py "D:\SteamLibrary\steamapps\workshop\content\220"')
        return 1

    failed = []
    addons = scan(containers, workshop_dirs=[workshop] if workshop else (), failed=failed)
    for container, error in failed:
        print("Can't open %s: %s" % (container, error), file=sys.stderr)
    if len(failed) == len({os.path.normcase(os.path.realpath(c)) for c in containers}):
        print("None of the folders could be opened, so there's nothing to check.", file=sys.stderr)
        return 1

    ids = {a.workshop_id for a in addons if a.workshop_id}
    if ids:
        if args.offline:
            titles = cached_titles(ids)  # from earlier runs
        else:
            print("Looking up Workshop titles on Steam...", file=sys.stderr, flush=True)
            titles = fetch_workshop_titles(ids)
            if not titles:
                print("  no answer from Steam, showing Workshop ids instead", file=sys.stderr)
        for a in addons:
            if a.workshop_id in titles:
                a.name = "%s [workshop %s]" % (titles[a.workshop_id], a.workshop_id)

    leftovers = [a for a in addons if a.leftover]
    stray = [a for a in addons if a.stray and not a.leftover]
    addons = [a for a in addons if not a.leftover and not a.stray]

    rank_custom_addons(addons)
    disabled, unlisted = [], []
    loaded = None  # with --all: the addons the game really loads
    if load_order:
        try:
            entries = read_load_order(load_order)
        except OSError as e:
            print("Can't read %s: %s. Load order not used." % (load_order, error_text(e)), file=sys.stderr)
            load_order = None
    if args.load_order and not ids:
        print("No Workshop items found in the scanned folders, so --load-order isn't used. "
              "Pass the workshop/content/220 folder itself.", file=sys.stderr)
    if load_order:
        info = {wsid: (n, enabled) for n, (wsid, enabled) in enumerate(entries)}
        active = []
        for a in addons:
            if not a.workshop_id:
                active.append(a)  # custom/ addons: always loaded, ahead of the Workshop ones
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
            a.rank = -n  # later in workshop.txt loads later and wins
            active.append(a)
        if args.all:
            loaded = [a for a in active if a not in disabled and a not in unlisted]
        else:
            addons = active

    skipped = [
        ("Disabled addons " + ("included (--all)" if args.all else "skipped"), disabled,
         "Turned off in the game's Add-ons menu."),
        ("Addons missing from workshop.txt " + ("included (--all)" if args.all else "skipped"), unlisted,
         "Installed, but not in the game's list yet. Start the game once to refresh it."),
        ("Not Workshop items, skipped", stray,
         "In the Workshop folder, but not an item folder (a copy, or a file put there by hand).\n"
         "The game never loads these."),
        ("Leftover folders skipped", leftovers,
         "These only hold cache files, left behind by addons you removed. Safe to delete."),
    ]

    base, base_info = {}, "not checked (--no-base-game)" if args.no_base_game else ""
    if not args.no_base_game:
        game = game or guess_game_dir(args.game_dir, containers)
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
        not_loaded = [a for _, items, _ in skipped[:2] if not args.all for a in items]
        everything = addons + not_loaded + stray
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
        return 1 if failed else 0

    analysis = analyze(addons, args.min_dupe_size, base, loaded=loaded)
    written = True
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(format_report(addons, skipped, containers, load_order, analysis, 0, base_info, failed))
    except OSError as e:
        written = False
        print("Couldn't save the report to %s: %s. Use --out to save it somewhere else."
              % (os.path.abspath(args.out), error_text(e)), file=sys.stderr)

    print(format_report(addons, skipped, containers, load_order, analysis, args.limit, base_info, failed))
    if written:
        print("Full report: " + os.path.abspath(args.out))

    if failed or not written:
        return 1
    return 2 if analysis[0] or analysis.mixed_models else 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 1
    except Exception:
        traceback.print_exc()
        code = 1
    if double_clicked():
        input("Press Enter to exit...")  # keep the window open, also after an error
    sys.exit(code)
