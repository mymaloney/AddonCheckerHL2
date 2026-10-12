#!/usr/bin/env python3
"""Tests for addon_checker.py.

Builds fake addons (loose folders, plain and compressed VPKs) and fake Steam
libraries in a temp folder and checks what the checker makes of them.

    python -m unittest tests.test_addon_checker -v
"""
import bz2
import contextlib
import gzip
import io
import lzma
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import addon_checker as ac  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def vpk_bytes(files, version=1, tree_size=None):
    """A single-file VPK holding files ({"dir/name.ext": bytes})."""
    tree = {}
    for rel, data in files.items():
        d, _, name = rel.rpartition("/")
        base, dot, ext = name.rpartition(".")
        if not dot:
            base, ext = name, ""
        tree.setdefault(ext or " ", {}).setdefault(d or " ", []).append((base, data))
    out, blob = bytearray(), bytearray()
    for ext, dirs in tree.items():
        out += ext.encode() + b"\0"
        for d, entries in dirs.items():
            out += d.encode() + b"\0"
            for base, data in entries:
                out += base.encode() + b"\0"
                out += struct.pack("<IHHIIH", zlib.crc32(data) & 0xFFFFFFFF, 0, 0x7FFF, len(blob), len(data), 0xFFFF)
                blob += data
            out += b"\0"
        out += b"\0"
    out += b"\0"
    header = struct.pack("<III", 0x55AA1234, version, len(out) if tree_size is None else tree_size)
    if version == 2:
        header += struct.pack("<IIII", len(blob), 0, 0, 0)
    return header + bytes(out) + bytes(blob)


def valve_lzma(data, dict_field=None):
    """Valve's LZMA wrapper: "LZMA", sizes, 5 property bytes, raw LZMA1 data."""
    filt = {"id": lzma.FILTER_LZMA1, "dict_size": 1 << 20, "lc": 3, "lp": 0, "pb": 2}
    packed = lzma.compress(data, format=lzma.FORMAT_RAW, filters=[filt])
    props = (2 * 5 + 0) * 9 + 3
    dict_size = (1 << 20) if dict_field is None else dict_field
    return b"LZMA" + struct.pack("<II", len(data), len(packed)) + bytes([props]) + struct.pack("<I", dict_size) + packed


def scan_one(folder):
    """Scans one addon folder (as a custom/ addon) and returns it."""
    container = os.path.dirname(folder)
    addons = [a for a in ac.scan([container], progress=lambda *a: None, slow=None)
              if a.location == folder]
    return addons[0]


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
        yield out


def run_main(*argv):
    """Runs addon_checker's main() in this process. Returns (exit code, stdout)."""
    ac.LOAD_ORDER_NOTE = "Assumes the addon higher in the list wins."
    old = sys.argv
    sys.argv = ["addon_checker.py"] + list(argv)
    try:
        with quiet() as out:
            code = ac.main()
    finally:
        sys.argv = old
    return code, out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def addon(self, container, name, files):
        root = self.path(container, name)
        os.makedirs(root, exist_ok=True)
        for rel, data in files.items():
            write(os.path.join(root, *rel.split("/")), data)
        return root

    def library(self, name="lib", items=None, order=None, gameinfo=None, base_files=None, custom=None):
        """A Steam library with Half-Life 2 and Workshop items.
        items: {id: {path: data}}; order: [(id, enabled)]; custom: {folder: {addon: {path: data}}}."""
        lib = self.path(name)
        game = os.path.join(lib, "steamapps", "common", "Half-Life 2")
        os.makedirs(os.path.join(game, "hl2_complete", "cfg"), exist_ok=True)
        if gameinfo is not None:
            write(os.path.join(game, "hl2_complete", "gameinfo.txt"), gameinfo)
        if base_files is not None:
            write(os.path.join(game, "hl2", "hl2_pak_dir.vpk"), vpk_bytes(base_files))
        workshop = os.path.join(lib, "steamapps", "workshop", "content", "220")
        os.makedirs(workshop, exist_ok=True)
        for wsid, files in (items or {}).items():
            for rel, data in files.items():
                write(os.path.join(workshop, wsid, *rel.split("/")), data)
        if order is not None:
            lines = ['"AddonInfo"', "{"] + ['\t"%s"\t\t"%d"' % (w, e) for w, e in order] + ["}"]
            write(os.path.join(game, "hl2_complete", "cfg", "workshop.txt"), "\n".join(lines))
        for folder, addons in (custom or {}).items():
            for addon_name, files in addons.items():
                for rel, data in files.items():
                    write(os.path.join(game, folder, "custom", addon_name, *rel.split("/")), data)
        return lib, game, workshop

    def steam(self, *libs):
        """Points Steam discovery at libs (the first one is the Steam root)."""
        root = libs[0]
        vdf = ['"libraryfolders"', "{"]
        for n, lib in enumerate(libs):
            vdf += ['\t"%d"' % n, "\t{", '\t\t"path"\t\t"%s"' % lib.replace("\\", "\\\\"), "\t}"]
        write(os.path.join(root, "steamapps", "libraryfolders.vdf"), "\n".join(vdf + ["}"]))
        old = ac.steam_roots
        ac.steam_roots = lambda: [root]
        self.addCleanup(setattr, ac, "steam_roots", old)


# ---------------------------------------------------------------------------
# Reading addons
# ---------------------------------------------------------------------------

class Reading(Base):
    def test_compressed_vpks_are_read(self):
        vpk = vpk_bytes({"scripts/a.txt": b"hello"})
        alone = lzma.compress(vpk, format=lzma.FORMAT_ALONE)
        cases = {
            "plain.vpk": vpk,
            "v2.vpk": vpk_bytes({"scripts/a.txt": b"hello"}, version=2),
            "valve.vpk": valve_lzma(vpk),
            "huge_dict_field.vpk": valve_lzma(vpk, dict_field=0xFFFFFFFF),  # clamped to the data size
            "alone.vpk": alone,
            "xz.vpk": lzma.compress(vpk),
            "gz.vpk": gzip.compress(vpk),
            "bz.vpk": bz2.compress(vpk),
            "zlib.vpk": zlib.compress(vpk),
        }
        for name, data in cases.items():
            with self.subTest(name):
                folder = self.path("c" + name, "addon")
                write(os.path.join(folder, name), data)
                a = scan_one(folder)
                self.assertEqual(a.errors, [])
                self.assertEqual(a.files, {"scripts/a.txt": (5, zlib.crc32(b"hello"))})

    def test_zip_of_vpks(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", "not a vpk")
            z.writestr("pak01_dir.vpk", vpk_bytes({"maps/x.bsp": b"map"}))
        folder = self.path("c", "addon")
        write(os.path.join(folder, "addon.zip"), buf.getvalue())
        a = scan_one(folder)
        self.assertEqual(a.errors, [])
        self.assertIn("maps/x.bsp", a.files)

    def unreadable_zip(self, member):
        """A zip whose member uses Deflate64 (method 9), which zipfile can't decompress."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(member, vpk_bytes({"maps/x.bsp": b"map"}))
        data = bytearray(buf.getvalue())
        data[8:10] = struct.pack("<H", 9)  # local header
        cd = data.rfind(b"PK\x01\x02")
        data[cd + 10:cd + 12] = struct.pack("<H", 9)  # central directory
        return bytes(data)

    def test_unreadable_zip_doesnt_stop_the_scan(self):
        self.addon("c", "a_good", {"scripts/a.txt": "a"})
        bad = self.addon("c", "b_bad", {"materials/x.vmt": "x"})
        write(os.path.join(bad, "pack.zip"), self.unreadable_zip("pak01_dir.vpk"))
        self.addon("c", "c_good", {"scripts/c.txt": "c"})
        addons = ac.scan([self.path("c")], progress=lambda *a: None, slow=None)
        self.assertEqual([os.path.basename(a.location) for a in addons], ["a_good", "b_bad", "c_good"])
        bad = addons[1]
        self.assertEqual(len(bad.errors), 1)
        self.assertIn("pack.zip, pak01_dir.vpk", bad.errors[0])
        self.assertEqual(addons[2].errors, [])

    def test_unreadable_non_vpk_zip_member_is_ignored(self):
        folder = self.addon("c", "a", {"scripts/a.txt": "a"})
        write(os.path.join(folder, "sources.zip"), self.unreadable_zip("readme.txt"))
        self.assertEqual(scan_one(folder).errors, [])

    def test_text_files_arent_probed_as_lzma(self):
        # Text starting like this has valid LZMA property bytes; probing it as headerless
        # LZMA used to allocate the "dictionary" its next bytes claim: up to 2 GB.
        folder = self.addon("c", "a", {"scripts/a.txt": "a"})
        for name, text in [("credits.txt", "credits: me"), ("readme.txt", "// HD Crowbar"),
                           ("addoninfo.txt", "AddonInfo\n{\n}"), ("install.txt", "Install: copy")]:
            write(os.path.join(folder, name), text)
        tracemalloc.start()
        try:
            a = scan_one(folder)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(a.errors, [])
        self.assertLess(peak, 64 * 1024 * 1024)

    def test_short_vpk_with_big_tree_size(self):
        folder = self.path("c", "a")
        write(os.path.join(folder, "pak01_dir.vpk"), struct.pack("<III", 0x55AA1234, 1, 500 << 20) + b"\0" * 50)
        tracemalloc.start()
        try:
            a = scan_one(folder)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(a.errors, ["pak01_dir.vpk: file is truncated"])
        self.assertLess(peak, 64 * 1024 * 1024)

    def test_unknown_format_is_reported_with_its_first_bytes(self):
        folder = self.path("c", "a")
        write(os.path.join(folder, "addon.7z"), b"7z\xbc\xaf\x27\x1c\x00\x04rest")
        a = scan_one(folder)
        self.assertEqual(a.errors, ["no readable content, unrecognized file format: "
                                    "addon.7z (starts with 37 7a bc af 27 1c 00 04)"])

    def test_vanished_folder_doesnt_stop_the_scan(self):
        self.addon("c", "a", {"scripts/a.txt": "a"})
        gone = self.addon("c", "b", {"scripts/b.txt": "b"})
        self.addon("c", "c", {"scripts/c.txt": "c"})

        def progress(i, total, name=""):
            if i == 1:
                shutil.rmtree(gone)  # e.g. unsubscribed while the scan runs

        addons = ac.scan([self.path("c")], progress=progress, slow=None)
        self.assertEqual(len(addons), 3)
        self.assertTrue(addons[1].errors)
        self.assertEqual(addons[2].errors, [])
        self.assertIn("scripts/c.txt", addons[2].files)

    def test_crash_in_one_addon_is_recorded(self):
        self.addon("c", "a", {"scripts/a.txt": "a"})
        self.addon("c", "b", {"scripts/b.txt": "b"})
        old = ac.scan_folder_addon

        def boom(addon, root):
            if root.endswith("a"):
                raise ValueError("something unexpected")
            old(addon, root)

        ac.scan_folder_addon = boom
        try:
            addons = ac.scan([self.path("c")], progress=lambda *a: None, slow=None)
        finally:
            ac.scan_folder_addon = old
        self.assertIn("something unexpected", addons[0].errors[0])
        self.assertIn("scripts/b.txt", addons[1].files)

    def test_walk_order_is_sorted(self):
        folder = self.path("c", "a")
        for name in ["zeta", "Beta", "alpha", "beta"]:
            write(os.path.join(folder, "materials", name, "x.vtf"), name)
        a = scan_one(folder)
        self.assertEqual(list(a.files), ["materials/alpha/x.vtf", "materials/beta/x.vtf", "materials/zeta/x.vtf"])

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks")
    def test_link_loop_is_read_once(self):
        folder = self.addon("c", "a", {"materials/x.vtf": "x"})
        try:
            os.symlink(folder, os.path.join(folder, "materials", "loop"), target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest("can't make symlinks here: %s" % e)
        a = scan_one(folder)
        self.assertEqual(list(a.files), ["materials/x.vtf"])

    def test_orphan_archive_part_and_empty_vpk_are_problems(self):
        folder = self.path("c", "a")
        write(os.path.join(folder, "pak01_000.vpk"), b"data only")
        write(os.path.join(folder, "empty.vpk"), vpk_bytes({}))
        a = scan_one(folder)
        self.assertEqual(sorted(a.errors), ["empty.vpk: the VPK has no files in it",
                                            "pak01_000.vpk: its directory file (..._dir.vpk) is missing"])
        self.assertFalse(a.leftover)

    def test_split_vpk_next_to_loose_folders(self):
        folder = self.path("c", "a")
        write(os.path.join(folder, "materials", "x.vtf"), "x")
        write(os.path.join(folder, "pak01_dir.vpk"), vpk_bytes({"maps/x.bsp": b"map"}))
        write(os.path.join(folder, "pak01_000.vpk"), b"data")
        write(os.path.join(folder, "zz", "y.vtf"), "y")
        self.assertEqual(scan_one(folder).errors, [])

    def test_duplicate_vpks_are_not_called_empty(self):
        folder = self.path("c", "a")
        vpk = vpk_bytes({"maps/x.bsp": b"map"})
        write(os.path.join(folder, "a.vpk"), vpk)
        write(os.path.join(folder, "b.vpk"), vpk)
        self.assertEqual(scan_one(folder).errors, [])


class Leftovers(Base):
    def scan_workshop(self):
        workshop = self.path("steamapps", "workshop", "content", "220")
        return {a.location: a for a in ac.scan([workshop], progress=lambda *a: None, slow=None)}, workshop

    def test_only_cache_files_is_leftover(self):
        write(self.path("steamapps", "workshop", "content", "220", "111", "sound", "sound.cache"), "c")
        addons, workshop = self.scan_workshop()
        self.assertTrue(addons[os.path.join(workshop, "111")].leftover)

    def test_folder_with_data_part_isnt_leftover(self):
        write(self.path("steamapps", "workshop", "content", "220", "111", "pak01_000.vpk"), "d")
        addons, workshop = self.scan_workshop()
        self.assertFalse(addons[os.path.join(workshop, "111")].leftover)

    def test_custom_folder_is_never_leftover(self):
        write(self.path("hl2", "custom", "wip", "sound.cache"), "c")
        addon = ac.scan([self.path("hl2", "custom")], progress=lambda *a: None, slow=None)[0]
        self.assertFalse(addon.leftover)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks")
    def test_folder_with_a_link_isnt_leftover(self):
        target = self.addon("elsewhere", "content", {"materials/x.vtf": "x"})
        item = self.path("steamapps", "workshop", "content", "220", "111")
        os.makedirs(item)
        try:
            os.symlink(target, os.path.join(item, "materials"), target_is_directory=True)
            os.symlink(os.path.join(self.tmp, "missing"), os.path.join(item, "broken"), target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest("can't make symlinks here: %s" % e)
        addons, workshop = self.scan_workshop()
        self.assertFalse(addons[item].leftover)


class WorkshopFolder(Base):
    def test_case_of_content_folder(self):
        workshop = self.path("steamapps", "workshop", "Content", "220")
        write(os.path.join(workshop, "111", "scripts", "a.txt"), "a")
        a = ac.scan([workshop], progress=lambda *a: None, slow=None)[0]
        self.assertEqual(a.workshop_id, "111")

    def test_copies_and_loose_files_are_stray(self):
        workshop = self.path("steamapps", "workshop", "content", "220")
        write(os.path.join(workshop, "111", "scripts", "a.txt"), "a")
        write(os.path.join(workshop, "111 - Copy", "scripts", "a.txt"), "a")
        write(os.path.join(workshop, "dropped.vpk"), vpk_bytes({"scripts/a.txt": b"a"}))
        addons = {os.path.basename(a.location): a for a in ac.scan([workshop], progress=lambda *a: None, slow=None)}
        self.assertFalse(addons["111"].stray)
        self.assertTrue(addons["111 - Copy"].stray)
        self.assertTrue(addons["dropped.vpk"].stray)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def addon(name, files, rank=None, errors=()):
    a = ac.Addon(name=name, location=name, files={p: (len(d), zlib.crc32(d.encode())) for p, d in files.items()})
    a.rank = rank
    a.errors = list(errors)
    return a


class Idle(unittest.TestCase):
    def idle(self, addons, base=None):
        return ac.find_idle_addons(addons, ac.index_files(addons), base or {})

    def test_overwritten_addon_is_removable(self):
        top = addon("top", {"materials/a.vtf": "new"}, rank=0)
        low = addon("low", {"materials/a.vtf": "old"}, rank=1)
        removed, _, together = self.idle([top, low])
        self.assertEqual(removed, {low})
        self.assertEqual(together[low], [])

    def test_partly_read_addon_is_never_removable(self):
        top = addon("top", {"materials/a.vtf": "new"}, rank=0)
        low = addon("low", {"materials/a.vtf": "old"}, rank=1, errors=["maps.vpk: file is truncated"])
        removed, results, _ = self.idle([top, low])
        self.assertEqual(removed, set())
        self.assertEqual(results, [])

    def test_removable_only_together(self):
        # J #1 = X, I #2 = Y, B #3 = X plus a file of its own. Removing I and J together is
        # safe (B's X takes over); removing only J would make I's Y the one the game uses.
        j = addon("J", {"scripts/w.txt": "X"}, rank=0)
        i = addon("I", {"scripts/w.txt": "Y"}, rank=1)
        b = addon("B", {"scripts/w.txt": "X", "scripts/own.txt": "B"}, rank=2)
        removed, _, together = self.idle([j, i, b])
        self.assertEqual(removed, {i, j})
        self.assertEqual(together[i], [])
        self.assertEqual(together[j], [i])

    def test_base_game_copy_only_together(self):
        original = (len("orig"), zlib.crc32(b"orig"))
        restore = addon("restore", {"sound/smg1.wav": "orig"}, rank=0)
        loud = addon("loud", {"sound/smg1.wav": "loud"}, rank=1)
        removed, _, together = self.idle([restore, loud], base={"sound/smg1.wav": original})
        self.assertEqual(removed, {restore, loud})
        self.assertEqual(together[loud], [])
        self.assertEqual(together[restore], [loud])

    def test_all_option_only_loaded_addons_decide(self):
        disabled = addon("disabled", {"materials/a.vtf": "old pack"}, rank=0)
        enabled = addon("enabled", {"materials/a.vtf": "new pack"}, rank=1)
        analysis = ac.analyze([disabled, enabled], 1024, loaded=[enabled])
        self.assertEqual(analysis[4], set())


class Duplicates(unittest.TestCase):
    def test_renamed_copy_of_a_conflicting_file(self):
        a = addon("a", {"materials/x.vtf": "X" * 2000})
        b = addon("b", {"materials/x.vtf": "Y" * 2000})
        c = addon("c", {"materials/renamed/y.vtf": "Y" * 2000})
        dupes = ac.analyze([a, b, c], 1024)[2]
        self.assertEqual([[(p, [o.name for o in who]) for p, who in group] for _, group in dupes],
                         [[("materials/renamed/y.vtf", ["c"]), ("materials/x.vtf", ["b"])]])

    def test_same_path_copies_arent_content_duplicates(self):
        a = addon("a", {"materials/x.vtf": "X" * 2000})
        b = addon("b", {"materials/x.vtf": "X" * 2000})
        analysis = ac.analyze([a, b], 1024)
        self.assertEqual(analysis[2], [])
        self.assertEqual(len(analysis[1]), 1)

    def test_one_addon_alone_isnt_a_duplicate(self):
        a = addon("a", {"materials/x.vtf": "X" * 2000, "materials/y.vtf": "X" * 2000})
        self.assertEqual(ac.analyze([a], 1024)[2], [])


class Pairs(unittest.TestCase):
    def test_identical_losers_dont_fight(self):
        one = addon("one", {"materials/a.vtf": "NEW"}, rank=0)
        two = addon("two", {"materials/a.vtf": "OLD"}, rank=1)
        three = addon("three", {"materials/a.vtf": "OLD"}, rank=2)
        pairs = ac.analyze([one, two, three], 1024)[3]
        self.assertEqual(sorted((a.name, b.name) for (a, b), _ in pairs), [("one", "three"), ("one", "two")])

    def test_unknown_order_pairs_only_differing_copies(self):
        a = addon("a", {"materials/x.vtf": "1"})
        b = addon("b", {"materials/x.vtf": "1"})
        c = addon("c", {"materials/x.vtf": "2"})
        pairs = ac.analyze([a, b, c], 1024)[3]
        self.assertEqual(sorted((p.name, q.name) for (p, q), _ in pairs), [("a", "c"), ("b", "c")])


class MixedModels(unittest.TestCase):
    MESH = {"models/z/classic.mdl": "mdl", "models/z/classic.vvd": "vvd", "models/z/classic.dx90.vtx": "vtx"}

    def mixed(self, *addons):
        return ac.find_mixed_models(ac.index_files(addons))

    def test_physics_from_another_addon(self):
        mesh = addon("mesh", self.MESH, rank=0)
        phys = addon("phys", {"models/z/classic.phy": "phy"}, rank=1)
        found = self.mixed(mesh, phys)
        self.assertEqual([(m, known) for m, known, _ in found], [("models/z/classic", True)])
        self.assertEqual([(ext, [c[0].name for c in copies]) for ext, copies in found[0][2]],
                         [(".mdl", ["mesh"]), (".vvd", ["mesh"]), (".dx90.vtx", ["mesh"]), (".phy", ["phys"])])

    def test_partial_override_that_wins(self):
        full = addon("full", self.MESH, rank=1)
        patch = addon("patch", {"models/z/classic.mdl": "other mdl"}, rank=0)
        self.assertEqual(len(self.mixed(full, patch)), 1)

    def test_partial_override_that_loses_is_fine(self):
        full = addon("full", self.MESH, rank=0)
        patch = addon("patch", {"models/z/classic.mdl": "other mdl"}, rank=1)
        self.assertEqual(self.mixed(full, patch), [])

    def test_two_complete_models_never_mix(self):
        a = addon("a", self.MESH)
        b = addon("b", {p: d + "2" for p, d in self.MESH.items()})
        self.assertEqual(self.mixed(a, b), [])

    def test_identical_parts_are_interchangeable(self):
        a = addon("a", self.MESH, rank=1)
        b = addon("b", {"models/z/classic.vvd": "vvd"}, rank=0)
        self.assertEqual(self.mixed(a, b), [])

    def test_unknown_order_may_mix(self):
        full = addon("full", self.MESH)
        patch = addon("patch", {"models/z/classic.mdl": "other mdl"})
        found = self.mixed(full, patch)
        self.assertEqual([(m, known) for m, known, _ in found], [("models/z/classic", False)])

    def test_low_dx_vtx_ignored(self):
        a = addon("a", self.MESH)
        b = addon("b", {"models/z/classic.dx80.vtx": "old", "models/z/classic.sw.vtx": "old"})
        self.assertEqual(self.mixed(a, b), [])


class CustomLoadOrder(Base):
    """custom/ addons load ahead of every Workshop addon, A to Z by folder name."""

    def setup_folders(self):
        lib, game, workshop = self.library(items={"111": {"scripts/w.txt": "workshop"}}, order=[("111", 1)],
                                           custom={"hl2": {"b_mod": {"scripts/w.txt": "b"},
                                                           "A_mod": {"scripts/w.txt": "a"}}})
        return workshop, os.path.join(game, "hl2", "custom")

    def winner(self, *args):
        workshop, custom = self.setup_folders()
        code, out = run_main("--offline", "--no-base-game", "--out", self.path("r.txt"), workshop, custom, *args)
        lines = out.splitlines()
        start = lines.index("  [!] scripts/w.txt")
        return [l.strip() for l in lines[start + 1:start + 4]]

    def test_custom_first_a_to_z(self):
        self.assertEqual(self.winner(), ["hl2/custom/A_mod  (1 bytes)  <- used",
                                         "hl2/custom/b_mod  (1 bytes)",
                                         "workshop/111  [#1]  (8 bytes)"])

    def test_last_wins_flips_it(self):
        self.assertEqual(self.winner("--last-wins"), ["workshop/111  [#1]  (8 bytes)  <- used",
                                                      "hl2/custom/b_mod  (1 bytes)",
                                                      "hl2/custom/A_mod  (1 bytes)"])

    def test_without_workshop_txt_custom_order_is_still_known(self):
        self.addon("c", "b", {"scripts/w.txt": "b"})
        self.addon("c", "a", {"scripts/w.txt": "a"})
        code, out = run_main("--offline", "--no-base-game", "--out", self.path("r.txt"), self.path("c"))
        self.assertIn("      tmp/c/a  (1 bytes)  <- used".replace("tmp", os.path.basename(self.tmp)), out)

    def test_overwritten_custom_addon_is_removable(self):
        top = addon("a", {"materials/x.vtf": "new"})
        low = addon("b", {"materials/x.vtf": "old"})
        ac.rank_custom_addons([top, low])
        self.assertEqual(ac.find_idle_addons([top, low], ac.index_files([top, low]))[0], {low})


# ---------------------------------------------------------------------------
# Finding Steam and the game
# ---------------------------------------------------------------------------

class Environment(Base):
    def test_old_libraryfolders_format(self):
        root = self.path("steam")
        other = self.path("other")
        os.makedirs(os.path.join(other, "steamapps"))
        write(os.path.join(root, "steamapps", "libraryfolders.vdf"),
              '"LibraryFolders"\n{\n\t"TimeNextStatsReport"\t\t"1234567890"\n'
              '\t"ContentStatsID"\t\t"4535426147254446186"\n\t"1"\t\t"%s"\n}\n' % other.replace("\\", "\\\\"))
        self.assertEqual(ac.library_folders(root), [root, other])

    def test_workshop_txt_comments_unquoted_and_bom(self):
        path = self.path("workshop.txt")
        write(path, b'\xef\xbb\xbf"AddonInfo"\n{\n\t"111"\t\t"1"\n//\t"222"\t\t"1"\n\t"333" "0" // off\n'
                    b'\t444\t\t1\n}\n')
        self.assertEqual(ac.read_load_order(path), [("111", True), ("333", False), ("444", True)])

    def test_write_keeps_root_key(self):
        path = self.path("workshop.txt")
        write(path, '"WorkshopAddons"\n{\n\t"111"\t"1"\n}\n')
        ac.write_load_order(path, [("111", False), ("222", True)])
        self.assertEqual(ac.read_load_order(path), [("111", False), ("222", True)])
        with open(path) as f:
            self.assertTrue(f.read().startswith('"WorkshopAddons"'))

    def test_only_mounted_custom_folders(self):
        gameinfo = ('"GameInfo"\n{\n\tFileSystem\n\t{\n\t\tSearchPaths\n\t\t{\n'
                    '\t\t\tgame+mod+custom_mod\t|gameinfo_path|custom/*\n'
                    '\t\t\tgame+mod\t\thl2/custom/*\n'
                    '\t\t\tgame\t\t\t|all_source_engine_paths|hl2/hl2_pak.vpk\n'
                    '\t\t}\n\t}\n}\n')
        lib, game, workshop = self.library(gameinfo=gameinfo, base_files={"scripts/base.txt": b"b"}, custom={
            "hl2_complete": {"a": {"scripts/a.txt": "a"}},
            "hl2": {"b": {"scripts/b.txt": "b"}},
            "lostcoast": {"c": {"scripts/c.txt": "c"}},
        })
        containers, _ = ac.game_folders(game, workshop)
        self.assertEqual(containers, [workshop, os.path.join(game, "hl2_complete", "custom"),
                                      os.path.join(game, "hl2", "custom")])

    def test_custom_fallback_without_gameinfo(self):
        lib, game, workshop = self.library(custom={"hl2": {"b": {"scripts/b.txt": "b"}},
                                                   "lostcoast": {"c": {"scripts/c.txt": "c"}}})
        containers, _ = ac.game_folders(game, workshop)
        self.assertEqual(containers, [workshop, os.path.join(game, "hl2", "custom")])


class CommandLine(Base):
    def test_default_run_uses_the_real_install_only(self):
        # An old library still has an install's leftovers: a stale workshop.txt and an older
        # copy of item 111 that also ships hud.vtf. Only the real install counts.
        old, _, _ = self.library("old", items={"111": {"materials/hud.vtf": "old 111", "scripts/x.txt": "x"}},
                                 order=[("999", 1)])
        real, game, _ = self.library("real", base_files={"scripts/base.txt": b"b"},
                                     items={"111": {"scripts/x.txt": "x"}, "222": {"materials/hud.vtf": "222"}},
                                     order=[("111", 1), ("222", 1)])
        self.steam(old, real)
        code, out = run_main("--offline", "--no-base-game", "--out", self.path("r.txt"))
        self.assertIn(os.path.join(real, "steamapps", "workshop", "content", "220"), out)
        self.assertNotIn(os.path.join(old, "steamapps"), out)
        self.assertIn("0 addons do nothing", out)
        self.assertEqual(code, 0)

    def test_stray_copy_doesnt_make_the_item_removable(self):
        lib, game, workshop = self.library(items={"111": {"maps/a.bsp": "map"}, "111 - Copy": {"maps/a.bsp": "map"}},
                                           order=[("111", 1)])
        code, out = run_main("--offline", "--no-base-game", "--out", self.path("r.txt"), workshop)
        self.assertIn("0 addons do nothing", out)
        self.assertIn("== NOT WORKSHOP ITEMS, SKIPPED (1) ==", out)

    def test_unreadable_load_order_is_an_error(self):
        lib, game, workshop = self.library(items={"111": {"maps/a.bsp": "map"}})
        code, out = run_main("--offline", "--no-base-game", "--load-order", self.path("typo.txt"), workshop)
        self.assertEqual(code, 1)

    def test_folder_that_cant_be_opened(self):
        code, out = run_main("--offline", "--no-base-game", "--out", self.path("r.txt"), self.path("missing"))
        self.assertEqual(code, 1)

    def test_report_that_cant_be_written(self):
        self.addon("c", "a", {"scripts/a.txt": "a"})
        code, out = run_main("--offline", "--no-base-game", "--out", self.tmp, self.path("c"))
        self.assertEqual(code, 1)
        self.assertIn("HL2 Addon Checker: 1 addons", out)

    def test_conflicts_and_mixed_models_exit_2(self):
        self.addon("c", "mesh", {"models/z/a.mdl": "m", "models/z/a.vvd": "v", "models/z/a.dx90.vtx": "x"})
        self.addon("c", "phys", {"models/z/a.phy": "p"})
        code, out = run_main("--offline", "--no-base-game", "--out", self.path("r.txt"), self.path("c"))
        self.assertIn("0 conflicts, 1 mixed models", out)
        self.assertEqual(code, 2)

    def test_find(self):
        lib, game, workshop = self.library(items={"111": {"maps/d1_town_02.bsp": "a"}, "222": {"maps/d1_town_02.bsp": "b"}},
                                           order=[("222", 1), ("111", 1)])
        code, out = run_main("--offline", "--no-base-game", "--find", "d1_town_02", workshop)
        self.assertIn("used: workshop/222", out)
        self.assertEqual(code, 0)

    def test_non_utf8_output(self):
        # Redirected output on Windows uses the ANSI code page; titles can have any characters.
        self.addon("c", "Ружьё pack 🔫", {"scripts/a.txt": "a"})
        self.addon("c", "other", {"scripts/a.txt": "b"})
        env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "addon_checker.py"), "--offline", "--no-base-game",
                            "--out", self.path("r.txt"), self.path("c")],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)
        self.assertEqual(p.returncode, 2, p.stderr.decode("utf-8", "replace"))
        self.assertIn("Ружьё pack 🔫", p.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
