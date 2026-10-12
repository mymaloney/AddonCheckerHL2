#!/usr/bin/env python3
"""Tests for the addon_checker command-line tool.

Builds custom/ folders full of fake addons (loose folders and VPKs) in a temp
folder, runs the tool on them and checks the report.

    python3 tests/run_tests.py ./addon_checker
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

CHECKER = None  # path to the built tool, from the command line


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def make_vpk(path, files, version=1):
    """Writes a single-file VPK holding files ({"dir/name.ext": bytes})."""
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
                # crc, preload bytes, archive index (0x7fff = this file), offset, length, terminator
                out += struct.pack("<IHHIIH", zlib.crc32(data) & 0xFFFFFFFF, 0, 0x7FFF, len(blob), len(data), 0xFFFF)
                blob += data
            out += b"\0"
        out += b"\0"
    out += b"\0"

    header = struct.pack("<III", 0x55AA1234, version, len(out))
    if version == 2:
        header += struct.pack("<IIII", len(blob), 0, 0, 0)
    write(path, header + bytes(out) + bytes(blob))


def run(*args, **kwargs):
    p = subprocess.run([CHECKER] + [str(a) for a in args], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=60, **kwargs)
    return p.returncode, p.stdout.decode()


def section(report, title):
    """The lines of one report section, without its header and explanation."""
    lines = report.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("== " + title))
    body = []
    for line in lines[start + 1:]:
        if line.startswith("== "):
            break
        body.append(line)
    # The explanation ends at the first blank line.
    return body[body.index("") + 1:] if "" in body else []


def entries(report, title):
    return [l for l in section(report, title) if l.strip()]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.custom = os.path.join(self.tmp, "custom")
        os.makedirs(self.custom)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def addon(self, name, files):
        for rel, data in files.items():
            write(os.path.join(self.custom, name, *rel.split("/")), data)

    def order(self, *names):
        return ";".join(os.path.join(self.custom, n) for n in names)


class ConflictsAndDuplicates(Base):
    def test_clean(self):
        self.addon("a", {"scripts/a.txt": "a"})
        self.addon("b", {"scripts/b.txt": "b"})
        code, out = run(self.custom)
        self.assertEqual(code, 0)
        self.assertIn("2 addons, 2 files", out)
        self.assertIn("0 conflicts, 0 mixed models, 0 same-path duplicates, 0 content duplicates", out)

    def test_conflict_and_severity(self):
        self.addon("a", {"scripts/weapon.txt": "one", "sound/x.wav": "one", "models/m.mdl": "one"})
        self.addon("b", {"scripts/weapon.txt": "two", "sound/x.wav": "two", "models/m.mdl": "two"})
        code, out = run(self.custom)
        self.assertEqual(code, 2)
        heads = [l for l in entries(out, "CONFLICTS") if not l.startswith("      ")]
        # Most dangerous first.
        self.assertEqual(heads, ["  [!] scripts/weapon.txt", "  [MODEL] models/m.mdl", "  sound/x.wav"])

    def test_used_and_ignored_with_load_order(self):
        self.addon("a", {"scripts/w.txt": "one"})
        self.addon("b", {"scripts/w.txt": "two"})
        _, out = run("--order", self.order("b", "a"), self.custom)
        self.assertEqual(entries(out, "CONFLICTS"), [
            "  [!] scripts/w.txt",
            "      b  (3 bytes)  <- used",
            "      a  (3 bytes)  <- ignored",
        ])

    def test_same_path_duplicate_is_not_a_conflict(self):
        self.addon("a", {"materials/x.vmt": "same"})
        self.addon("b", {"materials/x.vmt": "same"})
        code, out = run(self.custom)
        self.assertEqual(code, 0)
        self.assertEqual(entries(out, "SAME-PATH DUPLICATES"), ["  materials/x.vmt", "      in: a, b"])

    def test_content_duplicate_found_next_to_same_path_copy(self):
        # b/other.mdl matches a/m.mdl. b also has its own m.mdl, which used to
        # hide the match, because only one copy per path was compared.
        blob = os.urandom(4096)
        self.addon("b", {"models/m.mdl": blob, "models/other.mdl": blob})
        self.addon("a", {"models/m.mdl": blob})
        _, out = run(self.custom)
        self.assertEqual(entries(out, "CONTENT DUPLICATES"), [
            "  4.0 KB each:",
            "      models/m.mdl  [a]",
            "      models/m.mdl  [b]",
            "      models/other.mdl  [b]",
        ])

    def test_content_duplicate_needs_two_addons_and_two_names(self):
        blob = os.urandom(4096)
        self.addon("a", {"models/x.mdl": blob, "models/y.mdl": blob})  # same addon only
        self.addon("b", {"models/z.mdl": os.urandom(4096)})
        _, out = run(self.custom)
        self.assertEqual(entries(out, "CONTENT DUPLICATES"), [])

    def test_small_files_skip_content_check(self):
        self.addon("a", {"models/x.txt": "tiny"})
        self.addon("b", {"models/y.txt": "tiny"})
        _, out = run(self.custom)
        self.assertEqual(entries(out, "CONTENT DUPLICATES"), [])

    def test_root_files_are_not_content(self):
        self.addon("a", {"addoninfo.txt": "a", "scripts/x.txt": "x"})
        self.addon("b", {"addoninfo.txt": "b"})
        _, out = run(self.custom)
        self.assertIn("2 addons, 1 files", out)
        self.assertIn("0 conflicts", out)

    def test_output_is_sorted(self):
        for name in ["zeta", "Alpha", "mid"]:
            self.addon(name, {"scripts/w.txt": name})
        _, out = run(self.custom)
        self.assertEqual(entries(out, "CONFLICTS")[1:], [
            "      Alpha  (5 bytes)", "      mid  (3 bytes)", "      zeta  (4 bytes)"])

    def test_several_custom_folders(self):
        other = os.path.join(self.tmp, "other_custom")
        write(os.path.join(other, "c", "scripts", "w.txt"), "c")
        self.addon("a", {"scripts/w.txt": "a"})
        code, out = run(self.custom, other, self.custom + os.sep)  # last one is a repeat
        self.assertEqual(code, 2)
        self.assertIn("2 addons, 2 files", out)

    def test_usage(self):
        self.assertEqual(run()[0], 1)
        self.assertEqual(run("--order")[0], 1)


class Vpks(Base):
    def test_vpk_v1_and_v2_against_loose(self):
        make_vpk(os.path.join(self.custom, "one.vpk"), {"scripts/w.txt": b"same", "sound/s.wav": b"vpk"}, 1)
        make_vpk(os.path.join(self.custom, "two_dir.vpk"), {"scripts/w.txt": b"same"}, 2)
        write(os.path.join(self.custom, "two_000.vpk"), b"archive data, not an addon")
        self.addon("loose", {"scripts/w.txt": "same", "sound/s.wav": "loose"})
        code, out = run(self.custom)
        self.assertEqual(code, 2)
        self.assertIn("3 addons, 5 files", out)
        self.assertEqual(entries(out, "SAME-PATH DUPLICATES"),
                         ["  scripts/w.txt", "      in: loose, one.vpk, two_dir.vpk"])
        self.assertEqual(entries(out, "CONFLICTS"),
                         ["  sound/s.wav", "      loose  (5 bytes)", "      one.vpk  (3 bytes)"])

    def test_vpk_root_and_extensionless_files(self):
        make_vpk(os.path.join(self.custom, "v.vpk"), {"addoninfo.txt": b"x", "scripts/noext": b"y"})
        _, out = run(self.custom)
        self.assertIn("1 addons, 1 files", out)

    def test_pak01_inside_folder(self):
        self.addon("folder", {"scripts/loose.txt": "l"})
        make_vpk(os.path.join(self.custom, "folder", "pak01_dir.vpk"), {"scripts/w.txt": b"packed"})
        self.addon("other", {"scripts/w.txt": "different"})
        _, out = run(self.custom)
        self.assertIn("2 addons, 3 files", out)
        self.assertEqual(entries(out, "CONFLICTS")[0], "  [!] scripts/w.txt")

    def test_vpk_order_matches_dir_name(self):
        # The engine lists foo_dir.vpk as foo.vpk in its search path.
        make_vpk(os.path.join(self.custom, "pack_dir.vpk"), {"scripts/w.txt": b"vpk"})
        self.addon("loose", {"scripts/w.txt": "loose"})
        _, out = run("--order", self.order("pack.vpk", "loose"), self.custom)
        self.assertEqual(entries(out, "CONFLICTS")[1], "      pack_dir.vpk  (3 bytes)  <- used")

    def test_bad_vpks_are_reported(self):
        write(os.path.join(self.custom, "notvpk.vpk"), b"hello world, not a vpk")
        write(os.path.join(self.custom, "short.vpk"), struct.pack("<III", 0x55AA1234, 1, 100) + b"\0" * 10)
        write(os.path.join(self.custom, "v3.vpk"), struct.pack("<III", 0x55AA1234, 3, 0))
        # A tree whose first string never ends.
        write(os.path.join(self.custom, "corrupt.vpk"), struct.pack("<III", 0x55AA1234, 1, 8) + b"txtscrip")
        code, out = run(self.custom)
        self.assertEqual(code, 0)
        # This section has no explanation paragraph, the entries start right away.
        lines = "\n".join(l for l in out.splitlines() if l.startswith("  ") and ".vpk: " in l)
        self.assertIn("notvpk.vpk: not a VPK directory file", lines)
        self.assertIn("short.vpk: truncated VPK", lines)
        self.assertIn("v3.vpk: unsupported VPK version", lines)
        self.assertIn("corrupt.vpk: corrupt VPK directory", lines)

    @unittest.skipIf(os.name == "nt", "needs POSIX resource limits")
    def test_huge_tree_size_does_not_allocate(self):
        # A header claiming a ~4 GB directory used to be allocated up front.
        import resource

        def limit_memory():
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))

        write(os.path.join(self.custom, "huge.vpk"), struct.pack("<III", 0x55AA1234, 1, 0xFFFFFFF0) + b"\0" * 64)
        code, out = run(self.custom, preexec_fn=limit_memory)
        self.assertEqual(code, 0, out)
        self.assertIn("huge.vpk: truncated VPK", out)


class Links(Base):
    def test_symlink_loop(self):
        self.addon("a", {"scripts/w.txt": "a"})
        try:
            os.symlink(os.path.join(self.custom, "a"), os.path.join(self.custom, "a", "scripts", "loop"),
                       target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest("can't create symlinks here: %s" % e)
        code, out = run(self.custom)
        self.assertEqual(code, 0)
        self.assertIn("1 addons, 1 files", out)


MDL, VVD, VTX, PHY = ".mdl", ".vvd", ".dx90.vtx", ".phy"


class MixedModels(Base):
    def model(self, addon, parts, tag):
        self.addon(addon, {"models/props/chair" + p: "%s from %s" % (p, tag) for p in parts})

    def test_partial_override_with_load_order(self):
        self.model("full", [MDL, VVD, VTX], "full")
        self.model("patch", [MDL], "patch")
        code, out = run("--order", self.order("patch", "full"), self.custom)
        self.assertEqual(code, 2)
        self.assertEqual(entries(out, "MIXED MODELS"), [
            "  [MODEL] models/props/chair",
            "      .mdl        patch  (ignored: full)",
            "      .vvd        full",
            "      .dx90.vtx   full",
        ])

    def test_partial_override_that_loses_is_fine(self):
        self.model("full", [MDL, VVD, VTX], "full")
        self.model("patch", [MDL], "patch")
        _, out = run("--order", self.order("full", "patch"), self.custom)
        self.assertEqual(entries(out, "MIXED MODELS"), [])
        self.assertIn("  [MODEL] models/props/chair.mdl", entries(out, "CONFLICTS"))

    def test_unknown_order_reports_possible_mix(self):
        self.model("full", [MDL, VVD, VTX], "full")
        self.model("patch", [MDL], "patch")
        _, out = run(self.custom)
        self.assertEqual(entries(out, "MIXED MODELS"), [
            "  [MODEL] models/props/chair  (in some load orders)",
            "      .mdl        full, patch",
            "      .vvd        full",
            "      .dx90.vtx   full",
        ])

    def test_two_complete_models_never_mix(self):
        self.model("a", [MDL, VVD, VTX, PHY], "a")
        self.model("b", [MDL, VVD, VTX, PHY], "b")
        _, out = run(self.custom)
        self.assertEqual(entries(out, "MIXED MODELS"), [])
        self.assertEqual(len([l for l in entries(out, "CONFLICTS") if "[MODEL]" in l]), 4)

    def test_extra_part_from_another_addon(self):
        # No path conflict at all, but b's .phy gets paired with a's model.
        self.model("a", [MDL, VVD, VTX], "a")
        self.model("b", [PHY], "b")
        code, out = run(self.custom)
        self.assertEqual(code, 2)
        self.assertIn("0 conflicts, 1 mixed models", out)

    def test_identical_parts_are_interchangeable(self):
        self.model("a", [MDL, VVD, VTX], "a")
        self.addon("b", {"models/props/chair.vvd": ".vvd from a"})  # same bytes as a's
        _, out = run(self.custom)
        self.assertEqual(entries(out, "MIXED MODELS"), [])

    def test_low_dx_vtx_ignored(self):
        self.model("a", [MDL, VVD, VTX], "a")
        self.addon("b", {"models/props/chair.dx80.vtx": "old", "models/props/chair.sw.vtx": "old"})
        _, out = run(self.custom)
        self.assertEqual(entries(out, "MIXED MODELS"), [])

    def test_model_in_vpk_mixes_with_loose(self):
        make_vpk(os.path.join(self.custom, "full.vpk"),
                 {"models/props/chair" + p: ("%s full" % p).encode() for p in [MDL, VVD, VTX]})
        self.model("patch", [VVD], "patch")
        _, out = run("--order", self.order("patch", "full.vpk"), self.custom)
        self.assertEqual(entries(out, "MIXED MODELS")[2], "      .vvd        patch  (ignored: full.vpk)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: run_tests.py <path to addon_checker> [unittest args]")
    CHECKER = os.path.abspath(sys.argv.pop(1))
    unittest.main()
