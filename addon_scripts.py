#!/usr/bin/env python3
"""
HL2 Addon Scripts

Pulls the readable files (scripts, configs, HUD and menu files) out of every
Half-Life 2 addon you have installed, shows what each one changes compared with
the original game, setting by setting, and combines them all into one folder
you can start your own mod from. Models, materials, sounds and maps are only
counted, not copied.

    python addon_scripts.py                  find Steam + Half-Life 2 automatically
    python addon_scripts.py --out FOLDER     where to put the results (default: addon_scripts)
    python addon_scripts.py --help           all options

No third-party packages needed; Python 3.7 or newer. Uses addon_checker.py,
which must be in the same folder.

Public domain (The Unlicense). Largely written by an AI model ("vibe-coded"):
see the warnings in README.md.
"""

import argparse
import difflib
import os
import re
import shutil
import struct
import sys
import traceback
from collections import Counter, OrderedDict

import addon_checker as ac

# Text files that change how the game behaves. .vmt (material settings) only with --materials:
# they belong with the textures.
TEXT_EXTS = {"txt", "cfg", "res", "lst", "vdf", "nut", "rad", "ini", "kv"}
MATERIAL_EXTS = {"vmt"}
MAX_TEXT = 8 * 1024 * 1024  # bigger "text" files are data dumps, not settings
MAX_LISTED = 200  # changes listed per file in the report
MAX_DIFF_LINES = 80
GROUP_MIN = 5  # the same change in this many places is listed once

# Commands that can appear many times in a .cfg, each a separate setting: the first argument
# is part of what they set (bind KEY, alias NAME).
KEYED_COMMANDS = {"bind", "unbind", "alias", "bind_osx", "setinfo"}
# Commands that do something rather than set something: every line counts on its own.
ACTION_COMMANDS = {"exec", "echo", "play", "playgamesound", "say", "wait", "toggle", "incrementvar"}


def wanted(path, materials=False):
    if "/" not in path:
        return False  # addoninfo.txt and other files at the addon root aren't game content
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    if ext in MATERIAL_EXTS:
        return materials
    if ext == "lmp":
        return path.startswith("maps/")  # map entity patches
    return ext in TEXT_EXTS


def content_kind(path):
    """What a file the tool doesn't extract is, for the "also changes" line."""
    top = path.split("/", 1)[0]
    if top in ("materials", "models", "sound", "maps", "particles", "media", "resource", "scenes"):
        return {"sound": "sounds", "media": "videos", "resource": "resource files", "scenes": "scenes"}.get(top, top)
    return "other files"


# ---------------------------------------------------------------------------
# Reading files out of addons and the game
# ---------------------------------------------------------------------------

def read_addon_texts(addon, want):
    """Contents of an addon's files that want(path) accepts: ({path: bytes}, [errors])."""
    out, errors = {}, []
    location = addon.location
    if os.path.isdir(location):
        index = None  # lower-case path -> real path, for file systems that care about case
        for path in sorted(p for p in addon.files if want(p)):
            full = os.path.join(location, *path.split("/"))
            if not os.path.isfile(full):
                if index is None:
                    index = {}
                    for dirpath, _dirs, names in os.walk(location):
                        for name in names:
                            real = os.path.join(dirpath, name)
                            index.setdefault(ac.norm(os.path.relpath(real, location)), real)
                full = index.get(path)
                if not full:
                    continue  # it's in one of the addon's VPKs
            try:
                with open(full, "rb") as f:
                    out[path] = f.read(MAX_TEXT + 1)
            except OSError as e:
                errors.append("can't read %s: %s" % (path, ac.error_text(e)))
        try:
            names = sorted(os.listdir(location), key=ac.name_order)
        except OSError as e:
            return out, errors + ["can't open %s: %s" % (location, ac.error_text(e))]
        packed = [os.path.join(location, n) for n in names
                  if n.lower().endswith((".vpk", ".zip")) and not ac.VPK_ARCHIVE_PART.search(n)
                  and os.path.isfile(os.path.join(location, n))]
    else:
        packed = [location]
    for path in packed:
        try:
            for name, data in (ac.read_vpk_contents(path, lambda p: want(p) and p not in out) or {}).items():
                out.setdefault(name, data)
        except ac.ZIP_ERRORS as e:
            errors.append("%s: %s" % (os.path.basename(path), ac.error_text(e)))
    return out, errors


def base_game_texts(game_dir, paths):
    """The original game's copies of paths, as the game finds them without addons:
    ({path: bytes}, [errors])."""
    search, gameinfo = ac.gameinfo_search_paths(game_dir)
    sources = []
    if gameinfo:
        for _keys, path in search:
            name = os.path.basename(path).lower()
            if name == "*" or name == "download":
                continue  # custom/* holds addons; download/ holds files servers sent
            if path.lower().endswith(".vpk"):
                for cand in (path[:-4] + "_dir.vpk", path):
                    if os.path.isfile(cand):
                        sources.append(cand)
                        break
            elif os.path.isdir(path):
                sources.append(path)
    else:
        sources = ac.fallback_vpks(game_dir)
    out, errors, left = {}, [], set(paths)
    for source in sources:
        if not left:
            break
        if os.path.isdir(source):
            for path in sorted(left):
                full = os.path.join(source, *path.split("/"))
                if os.path.isfile(full):
                    try:
                        with open(full, "rb") as f:
                            out[path] = f.read() if path.endswith(".bsp") else f.read(MAX_TEXT + 1)
                    except OSError as e:
                        errors.append("%s: %s" % (full, ac.error_text(e)))
        else:
            try:
                out.update(ac.read_vpk_contents(source, lambda p: p in left) or {})
            except ac.ZIP_ERRORS as e:
                errors.append("%s: %s" % (source, ac.error_text(e)))
        left -= set(out)
    return out, errors


def decode(data):
    """(text, encoding), or (None, None) for files that aren't text."""
    if len(data) > MAX_TEXT:
        return None, None
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            return None, None
    if b"\0" in data:
        return None, None
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace"), "cp1252"


# ---------------------------------------------------------------------------
# KeyValues (scripts/*.txt, resource/*.res...) and .cfg files
# ---------------------------------------------------------------------------

KV_TOKEN = re.compile(r'//[^\n]*|"([^"\n]*)"?|(\[[^\]\n]*\])|([{}])|([^\s{}"]+)')
BLOCK = object()  # value of a key that holds more keys


def parse_kv(text):
    """Valve KeyValues as a list of [key, value, condition]; value is a string or a list of the
    same. condition is a platform tag like [$WIN32], or "". None if the text isn't KeyValues."""
    root, stack, key = [], [], None
    current = root
    for m in KV_TOKEN.finditer(text):
        quoted, cond, brace, bare = m.groups()
        if quoted is None and cond is None and brace is None and bare is None:
            continue  # comment
        if cond is not None:
            if current and key is None:
                current[-1][2] = cond
            continue
        if brace == "{":
            if key is None:
                return None
            block = []
            current.append([key, block, ""])
            stack.append(current)
            current, key = block, None
        elif brace == "}":
            if key is not None or not stack:
                return None
            current = stack.pop()
        else:
            token = quoted if quoted is not None else bare
            if key is None:
                key = token
            else:
                current.append([key, token, ""])
                key = None
    if stack or key is not None:
        return None
    if not any(isinstance(v, list) for _, v, _ in root):
        return None  # free text that happens to split into pairs
    return root


def flatten_kv(pairs, prefix=(), out=None, as_set=False):
    """OrderedDict of path -> (key as written, condition, value or BLOCK). A path is a tuple of
    (lower-case key + condition, occurrence) steps, so repeated keys stay apart. In a list the
    game picks from at random (rndwave), entries are told apart by their value instead
    ("wave=a.wav"), so two addons that each add a sound both keep theirs."""
    out = OrderedDict() if out is None else out
    seen = Counter()
    for key, value, cond in pairs:
        name = key.lower() + (" " + cond.lower() if cond else "")
        if as_set and not isinstance(value, list):
            name += "=" + value.replace("\\", "/").lower()
        seen[name] += 1
        path = prefix + ((name, seen[name]),)
        if isinstance(value, list):
            out[path] = (key, cond, BLOCK)
            flatten_kv(value, path, out, as_set=key.lower() == "rndwave")
        else:
            out[path] = (key, cond, value)
    return out


def unflatten_kv(flat):
    root, blocks = [], {(): None}
    blocks[()] = root
    for path, (key, cond, value) in flat.items():
        parent = blocks.get(path[:-1])
        if parent is None:
            continue  # its block was removed
        if value is BLOCK:
            block = []
            parent.append([key, block, cond])
            blocks[path] = block
        else:
            parent.append([key, value, cond])
    return root


def quote(s):
    return '"%s"' % s


def format_kv(pairs, depth=0):
    lines, pad = [], "\t" * depth
    for key, value, cond in pairs:
        tail = " " + cond if cond else ""
        if isinstance(value, list):
            lines.append(pad + quote(key) + tail)
            lines.append(pad + "{")
            lines += format_kv(value, depth + 1)
            lines.append(pad + "}")
        else:
            lines.append(pad + quote(key) + "\t\t" + quote(value) + tail)
    return lines


def parse_cfg(text):
    """A .cfg file as an OrderedDict of setting -> (command as written, "", arguments)."""
    out = OrderedDict()
    seen = Counter()
    for line in text.splitlines():
        for part in split_commands(line):
            words = part.split(None, 2)
            if not words:
                continue
            cmd = words[0].lower()
            if cmd in KEYED_COMMANDS and len(words) > 1:
                name = cmd + " " + words[1].strip('"').lower()
                shown, args = words[0] + " " + words[1], words[2] if len(words) > 2 else ""
            elif cmd in ACTION_COMMANDS:
                name, shown, args = " ".join(part.replace('"', "").lower().split()), part, ""
            else:
                name, shown, args = cmd, words[0], part.split(None, 1)[1] if len(words) > 1 else ""
            seen[name] += 1
            if cmd in ACTION_COMMANDS:
                out[((name, seen[name]),)] = (shown, "", args)
            else:
                out[((name, 1),)] = (shown, "", args)  # set twice: the last one counts
                out.move_to_end(((name, 1),))
    return out


def split_commands(line):
    """A cfg line without its // comment, split at ; outside quotes."""
    parts, cur, quoted, i = [], "", False, 0
    while i < len(line):
        c = line[i]
        if c == '"':
            quoted = not quoted
        elif not quoted and line.startswith("//", i):
            break
        elif not quoted and c == ";":
            parts.append(cur.strip())
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    parts.append(cur.strip())
    return [p for p in parts if p]


def format_cfg(flat):
    return [(shown + " " + args).rstrip() for shown, _, args in flat.values()]


LUMP_HEADER = struct.Struct("<5i")  # data offset, lump id, lump version, length, map revision


def lmp_entities(data):
    """The entity text of a map's entity patch (maps/<map>_l_0.lmp), or None."""
    if len(data) < LUMP_HEADER.size:
        return None
    offset, lump, _version, length, _revision = LUMP_HEADER.unpack_from(data)
    if lump != 0 or offset < LUMP_HEADER.size:
        return None  # only the entity lump (0) is text
    return data[offset:offset + length].split(b"\0", 1)[0]


def bsp_entities(data):
    """The entity text of a compiled map (.bsp), or None."""
    if data[:4] != b"VBSP" or len(data) < 16:
        return None
    offset, length = struct.unpack_from("<ii", data, 8)
    return data[offset:offset + length].split(b"\0", 1)[0] or None


def entities_lmp(text, version=0, revision=0):
    body = text.encode("utf-8", "replace") + b"\0"
    return LUMP_HEADER.pack(LUMP_HEADER.size, 0, version, len(body), revision) + body


def parse_entities(text):
    """A map's entities as a list of [[key, value, ""]...], or None."""
    entities, current, key = [], None, None
    for m in KV_TOKEN.finditer(text):
        quoted, _cond, brace, bare = m.groups()
        if brace == "{":
            if current is not None:
                return None
            current = []
        elif brace == "}":
            if current is None or key is not None:
                return None
            entities.append(current)
            current = None
        elif quoted is not None or bare is not None:
            if current is None:
                return None
            token = quoted if quoted is not None else bare
            if key is None:
                key = token
            else:
                current.append([key, token, ""])
                key = None
    return entities if current is None else None


def entity_name(pairs):
    keys = {}
    for k, v, _ in pairs:
        keys.setdefault(k.lower(), v)
    name = keys.get("classname", "entity")
    if keys.get("hammerid"):
        return "%s #%s" % (name, keys["hammerid"])
    if keys.get("targetname"):
        return '%s "%s"' % (name, keys["targetname"])
    if keys.get("origin"):
        return "%s at (%s)" % (name, keys["origin"])
    return name


def flatten_entities(entities):
    """Like flatten_kv: one block per entity, named by its class and Hammer id (or name or
    place). Outputs (OnTrigger...) can repeat, so they're told apart by their value."""
    out, seen = OrderedDict(), Counter()
    for pairs in entities:
        name = entity_name(pairs)
        seen[name] += 1
        path = ((name, seen[name]),)
        out[path] = (name, "", BLOCK)
        keys = Counter()
        for key, value, _ in pairs:
            step = key.lower() + ("=" + value.lower() if key.lower().startswith("on") else "")
            keys[step] += 1
            out[path + ((step, keys[step]),)] = (key, "", value)
    return out


def format_entities(flat):
    lines = []
    for _name, pairs, _ in unflatten_kv(flat):
        lines.append("{")
        lines += ["%s %s" % (quote(k), quote(v)) for k, v, _ in pairs if not isinstance(v, list)]
        lines.append("}")
    return lines


class Parsed:
    """A text file read as settings: kind is "kv", "cfg" or "ents" (map entities), flat the
    settings; or kind "text" or "binary"."""

    def __init__(self, path, data):
        self.data = data
        self.kind, self.flat = "binary", None
        if path.endswith(".lmp"):
            raw = lmp_entities(data)
            self.text, self.encoding = decode(raw) if raw is not None else (None, None)
            entities = parse_entities(self.text) if self.text is not None else None
            if entities is not None:
                self.kind, self.flat = "ents", flatten_entities(entities)
                self.lump = LUMP_HEADER.unpack_from(data)
            return
        self.text, self.encoding = decode(data)
        if self.text is None:
            return
        self.kind = "text"
        if path.endswith(".cfg"):
            self.kind, self.flat = "cfg", parse_cfg(self.text)
            return
        pairs = parse_kv(self.text)
        if pairs is not None:
            self.kind, self.flat = "kv", flatten_kv(pairs)


def show_path(path):
    """weapondata/clip_size; rndwave entries without their "=value"."""
    return "/".join(name.partition("=")[0] + ("" if n == 1 else "[%d]" % n) for name, n in path)


def show_value(value):
    return "{...}" if value is BLOCK else quote(value)


def diff_settings(old, new, removals=True):
    """Changes from old to new flat settings: [(kind, path, old value, new value)] with kind
    "changed", "added" or "removed". Keys under an added or removed block aren't listed.
    removals=False for .cfg files: one that leaves a command out isn't taking it away."""
    changes = []
    for path, (_, _, value) in new.items():
        if path in old:
            if old[path][2] is not value and old[path][2] != value:
                changes.append(("changed", path, old[path][2], value))
        elif path[:-1] == () or path[:-1] in old:
            changes.append(("added", path, None, value))
    for path, (_, _, value) in old.items() if removals else ():
        if path not in new and (path[:-1] == () or path[:-1] in new):
            changes.append(("removed", path, value, None))
    return changes


def describe_one(what, name, old, new, kind):
    if what == "changed":
        return "%s: %s -> %s" % (name, show_value(old), show_value(new))
    if what == "added":
        if kind == "cfg":
            return ("+ %s %s" % (name, new)).rstrip()
        return "+ %s = %s" % (name, show_value(new))
    return "- %s (was %s)" % (name, show_value(old))


def describe(changes, kind):
    """Report lines for changes. The same change to the same setting in many places (a sound
    property changed in 500 sounds) is one line, with a few of the places."""
    def group(what, path, old, new):
        return (what, path[-1][0].partition("=")[0], old, new) if len(path) > 1 and kind != "cfg" else None
    places = OrderedDict()
    for c in changes:
        g = group(*c)
        if g:
            places.setdefault(g, []).append(show_path(c[1][:-1]))
    lines, done = [], set()
    for what, path, old, new in changes:
        g = group(what, path, old, new)
        if g and len(places[g]) >= GROUP_MIN:
            if g not in done:
                done.add(g)
                lines.append("%s  in %d places: %s, ..." % (
                    describe_one(what, g[1], old, new, kind), len(places[g]), ", ".join(places[g][:3])))
            continue
        lines.append(describe_one(what, show_path(path) if kind != "cfg" else path[0][0], old, new, kind))
    return lines


def listing(parsed):
    """Report lines for a file the original game doesn't have: what it contains."""
    if parsed.flat is None:
        lines = ["+ " + line for line in parsed.text.splitlines() if line.strip()]
        return lines[:MAX_DIFF_LINES] + (["... (see the file itself)"] if len(lines) > MAX_DIFF_LINES else [])
    return describe([("added", p, None, v) for p, (_, _, v) in parsed.flat.items()
                     if v is not BLOCK or parsed.kind == "cfg"], parsed.kind)


def text_diff(old, new, path):
    return list(difflib.unified_diff(old.splitlines(), new.splitlines(), "original/" + path,
                                     "addon/" + path, n=1, lineterm=""))


# ---------------------------------------------------------------------------
# Combining addons
# ---------------------------------------------------------------------------

def merge_settings(base, versions, removals=True):
    """Applies each addon's changes to base in load order (the later addon wins).
    base: flat settings or None; versions: [(addon, flat)]. Returns (merged flat, conflicts),
    conflicts as [(path, earlier addon, its value, later addon, its value)]."""
    merged = OrderedDict(base or ())
    owner = {}  # path -> (addon, value) of the last addon that set it
    conflicts = []

    def claim(path, addon, value):
        prev = owner.get(path)
        if prev and prev[0] is not addon and prev[1] is not value and prev[1] != value:
            conflicts.append((path, prev[0], prev[1], addon, value))
        owner[path] = (addon, value)

    for addon, flat in versions:
        for what, path, _old, new in diff_settings(base or {}, flat, removals):
            if what == "removed":
                claim(path, addon, None)
                for p in [p for p in merged if p[:len(path)] == path]:
                    del merged[p]
                continue
            # An added block brings everything under it.
            for p in [p for p in flat if p[:len(path)] == path] if new is BLOCK else [path]:
                for depth in range(1, len(p)):
                    if p[:depth] not in merged:
                        merged[p[:depth]] = flat[p[:depth]]  # its block was removed by another addon
                if p in merged and merged[p][2] is BLOCK and flat[p][2] is BLOCK:
                    continue
                merged[p] = flat[p]
                claim(p, addon, flat[p][2])
    return merged, conflicts


def safe_name(name, limit=80):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:limit].rstrip(" .") or "addon"


def load_order_sorted(addons):
    """Earliest-loading first: higher rank first, unknown ranks in scan order at the start."""
    return sorted(addons, key=lambda a: (a.rank is not None, -(a.rank or 0)))


class OutputFolderInUse(Exception):
    def __str__(self):
        return ("%s already holds other files. Choose an empty folder, or one this tool wrote before."
                % self.args[0])


def is_results_folder(folder):
    """Earlier results are replaced; any other folder is left alone."""
    try:
        with open(os.path.join(folder, "report.txt"), encoding="utf-8", errors="replace") as f:
            return f.read(64).startswith("HL2 Addon Scripts")
    except OSError:
        return False


def write_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def encode(lines, encoding, header=None):
    text = "\n".join(([header] if header else []) + lines) + "\n"
    if encoding == "utf-16":
        return b"\xff\xfe" + text.replace("\n", "\r\n").encode("utf-16-le")
    return text.replace("\n", "\r\n").encode(encoding or "utf-8", "replace")


def extract(addons, game_dir, out_dir, materials=False, progress=None):
    """Writes the report, each addon's files and the combined folder to out_dir.
    addons: the addons the game loads, ranked (lower rank wins). Returns a summary dict."""
    progress = progress or (lambda *a: None)
    want = lambda p: wanted(p, materials)  # noqa: E731
    order = load_order_sorted(addons)
    label = {a: (a.name if a.name else os.path.basename(a.location)) for a in order}

    texts, problems = {}, []
    for i, a in enumerate(order, 1):
        progress(i, len(order), label[a])
        files, errors = read_addon_texts(a, want)
        texts[a] = files
        problems += ["%s: %s" % (label[a], e) for e in errors]

    paths = sorted({p for files in texts.values() for p in files})
    base, base_errors, base_info = {}, [], "not compared (Half-Life 2 folder not found)"
    if game_dir:
        progress(len(order), len(order), "the original game files")
        maps = {p: re.sub(r"_l_\d+\.lmp$", ".bsp", p) for p in paths if p.endswith(".lmp")}
        base, base_errors = base_game_texts(game_dir, [maps.get(p, p) for p in paths])
        for lmp, bsp in maps.items():  # an entity patch changes the original map's entities
            text = bsp_entities(base.pop(bsp)) if bsp in base else None
            if text is not None:
                base[lmp] = entities_lmp(text.decode("utf-8", "replace"))
        base_info = "%s (%d of %d files are also in the original game)" % (game_dir, len(base), len(paths))
    problems += ["original game: " + e for e in base_errors]

    if os.path.isdir(out_dir) and os.listdir(out_dir):
        if not is_results_folder(out_dir):
            raise OutputFolderInUse(out_dir)
        for sub in ("addons", "combined"):  # results of an earlier run
            shutil.rmtree(os.path.join(out_dir, sub), ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    parsed_base = {p: Parsed(p, d) for p, d in base.items()}
    report = ["HL2 Addon Scripts %s" % ac.__version__,
              "Compared with: " + base_info, "",
              "What every addon changes in the game's scripts and settings, and one folder with all of",
              "them combined (combined/). Models, materials, sounds and maps are counted, not copied.",
              "Addons are listed in load order: an addon further down loads later and wins.", ""]
    for n, a in enumerate(order, 1):
        report.append("%3d. %s" % (n, label[a]))
    report.append("")

    by_path = {}  # path -> [(addon, Parsed)] of addons that change it, in load order
    copied = 0
    for n, a in enumerate(order, 1):
        folder = os.path.join(out_dir, "addons", "%03d %s" % (n, safe_name(label[a])))
        report.append("=" * 78)
        report.append("%d. %s" % (n, label[a]))
        report.append("   " + a.location)
        others = Counter(content_kind(p) for p in a.files if "/" in p and not want(p))
        if others:
            report.append("   Also changes (not copied): " + ", ".join(
                "%d %s" % (c, k) for k, c in sorted(others.items(), key=lambda kc: (-kc[1], kc[0]))))
        files = texts[a]
        if not files:
            report.append("   No scripts or settings.")
        for path in sorted(files):
            data = files[path]
            write_file(os.path.join(folder, *path.split("/")), data[:MAX_TEXT])
            copied += 1
            mine, orig = Parsed(path, data), parsed_base.get(path)
            if mine.kind == "binary":
                report.append("   %s  (not a text file)" % path)
                continue
            if orig is not None and orig.data == data:
                report.append("   %s  (same as the original game)" % path)
                continue
            by_path.setdefault(path, []).append((a, mine))
            if orig is None or orig.kind == "binary":
                lines = listing(mine)
                count = (len([v for _, _, v in mine.flat.values() if v is not BLOCK]) if mine.flat is not None
                         else len(mine.text.splitlines()))
                report.append("   %s  (new file, %s)" % (
                    path, ac.plural(count, "setting" if mine.flat is not None else "line")))
            elif mine.flat is not None and orig.kind == mine.kind:
                changes = diff_settings(orig.flat, mine.flat, mine.kind != "cfg")
                if not changes:
                    report.append("   %s  (same settings as the original game, written differently)" % path)
                    continue
                report.append("   %s  (%s)" % (path, ac.plural(len(changes), "change")))
                lines = describe(changes, mine.kind)
            elif mine.text is None:
                report.append("   %s  (changed, can't be read as text)" % path)
                continue
            else:
                lines = text_diff(orig.text, mine.text, path)[2:]
                report.append("   %s  (changed, %d lines differ)" % (
                    path, len([x for x in lines if x[:1] in "+-"])))
                if len(lines) > MAX_DIFF_LINES:
                    lines = lines[:MAX_DIFF_LINES] + ["... (see the file itself)"]
            if len(lines) > MAX_LISTED:
                lines = lines[:MAX_LISTED] + ["... and %d more" % (len(lines) - MAX_LISTED)]
            report += ["       " + line for line in lines]
        report.append("")

    # The combined folder: every changed file, with the changes of all addons that touch it.
    combined = os.path.join(out_dir, "combined")
    report += ["=" * 78, "Combined (combined/)", "",
               "Files more than one addon changes. Settings files are combined setting by setting;",
               "where two addons set the same thing, the one that loads later wins.", ""]
    conflict_count = shared = 0
    for path in sorted(by_path):
        versions = by_path[path]
        target = os.path.join(combined, *path.split("/"))
        last = versions[-1][1]
        if len(versions) == 1:
            write_file(target, last.data)
            continue
        shared += 1
        names = ", ".join(label[a] for a, _ in versions)
        orig = parsed_base.get(path)
        same_kind = last.flat is not None and all(p.kind == last.kind for _, p in versions)
        if same_kind and (orig is None or orig.kind == last.kind):
            merged, conflicts = merge_settings(orig.flat if orig else None, [(a, p.flat) for a, p in versions],
                                               last.kind != "cfg")
            if last.kind == "ents":
                _, _, version, _, revision = last.lump
                write_file(target, entities_lmp("\n".join(format_entities(merged)) + "\n", version, revision))
            else:
                header = "// Combined by HL2 Addon Scripts from: " + names
                lines = format_kv(unflatten_kv(merged)) if last.kind == "kv" else format_cfg(merged)
                write_file(target, encode(lines, last.encoding, header))
            report.append("%s: combined from %s" % (path, names))
            report.append("    (in the game, only %s's copy is used)" % label[versions[-1][0]])
            for p, a1, v1, a2, v2 in conflicts:
                conflict_count += 1
                name = show_path(p) if last.kind != "cfg" else p[0][0]
                report.append("    %s: %s sets %s, %s sets %s -> %s wins" % (
                    name, label[a1], "(removed)" if v1 is None else show_value(v1),
                    label[a2], "(removed)" if v2 is None else show_value(v2), label[a2]))
        else:
            write_file(target, last.data)
            conflict_count += 1
            report.append("%s: changed by %s. Not a settings file, so %s's copy is used whole"
                          % (path, names, label[versions[-1][0]]))
    if not shared:
        report.append("No file is changed by more than one addon.")
    if problems:
        report += ["", "Couldn't read:"] + ["  " + p for p in problems]
    report.append("")
    report_path = os.path.join(out_dir, "report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    return {"report": report_path, "addons": len(order), "files": copied, "changed": len(by_path),
            "shared": shared, "conflicts": conflict_count, "problems": problems}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def loaded_addons(folders=(), game_dir=None, include_all=False, offline=False, log=print):
    """The addons the game loads, ranked, plus the Half-Life 2 folder: (addons, game_dir)."""
    workshop = None
    if folders:
        containers = list(folders)
        load_order = ac.find_load_order_file(containers)
        game = ac.guess_game_dir(game_dir, containers)
    else:
        game, workshop = ac.find_install(game_dir)
        containers, load_order = ac.game_folders(game, workshop)
    if not containers:
        return None, game
    failed = []
    addons = ac.scan(containers, progress=lambda *a: None, workshop_dirs=[workshop] if workshop else (),
                     failed=failed)
    for container, error in failed:
        log("Can't open %s: %s" % (container, error))
    if len(failed) == len(set(containers)):
        return None, game
    ids = {a.workshop_id for a in addons if a.workshop_id}
    if ids:
        titles = ac.cached_titles(ids) if offline else ac.fetch_workshop_titles(ids)
        for a in addons:
            if a.workshop_id in titles:
                a.name = "%s [workshop %s]" % (titles[a.workshop_id], a.workshop_id)
    addons = [a for a in addons if not a.leftover and not a.stray]
    ac.rank_custom_addons(addons)
    if load_order:
        try:
            addons = ac.apply_load_order(addons, ac.read_load_order(load_order), include_all)[0]
        except OSError as e:
            log("Can't read %s: %s. Load order not used." % (load_order, ac.error_text(e)))
    return addons, game


def main():
    ap = argparse.ArgumentParser(description="Show what every Half-Life 2 addon changes in the game's scripts and "
                                             "settings, and combine them into one folder.")
    ap.add_argument("folders", nargs="*", help="folders containing addons. Default: find automatically")
    ap.add_argument("--out", default="addon_scripts", help="folder for the results (default: addon_scripts)")
    ap.add_argument("--game-dir", metavar="FOLDER", help="the Half-Life 2 folder (default: found automatically)")
    ap.add_argument("--materials", action="store_true", help="also copy material settings (.vmt)")
    ap.add_argument("--all", action="store_true", help="also include disabled addons")
    ap.add_argument("--offline", action="store_true", help="don't look up Workshop item titles on Steam")
    ap.add_argument("--version", action="version", version="HL2 Addon Scripts " + ac.__version__)
    args = ap.parse_args()
    ac.use_safe_output()

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    log("Reading addons...")
    addons, game = loaded_addons(args.folders, args.game_dir, args.all, args.offline, log)
    if addons is None:
        print("Couldn't find Half-Life 2's Workshop or custom folders. Pass them yourself, or use --game-dir.")
        return 1
    if not game:
        log("Half-Life 2 folder not found, so files aren't compared with the original game (use --game-dir).")
    try:
        summary = extract(addons, game, args.out, args.materials,
                          progress=lambda i, n, name: log("  [%d/%d] %s" % (i, n, name)))
    except (OutputFolderInUse, OSError) as e:
        print("Can't write the results: %s" % (e if isinstance(e, OutputFolderInUse) else ac.error_text(e)))
        return 1
    print("%s with scripts or settings copied from %s." % (
        ac.plural(summary["files"], "file"), ac.plural(summary["addons"], "addon")))
    print("%s change the original game; %s changed by more than one addon (%s)." % (
        ac.plural(summary["changed"], "file"), ac.plural(summary["shared"], "file"),
        ac.plural(summary["conflicts"], "conflict")))
    for p in summary["problems"]:
        print("Couldn't read " + p)
    print("Report:   " + os.path.abspath(summary["report"]))
    print("Combined: " + os.path.abspath(os.path.join(args.out, "combined")))
    return 1 if summary["problems"] else 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 1
    except Exception:
        traceback.print_exc()
        code = 1
    if ac.double_clicked():
        input("Press Enter to exit...")
    sys.exit(code)
