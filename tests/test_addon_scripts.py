#!/usr/bin/env python3
"""Tests for addon_scripts.py.

    python -m unittest tests.test_addon_scripts -v
"""
import gzip
import os
import struct
import sys
import unittest
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import addon_checker as ac  # noqa: E402
import addon_scripts as sc  # noqa: E402
from test_addon_checker import Base, quiet, valve_lzma, vpk_bytes, write  # noqa: E402

PISTOL = """// pistol
WeaponData
{
	"printname"	"#HL2_Pistol"
	"clip_size"	"18"
	"damage"	"8"
	SoundData
	{
		"single_shot"	"Weapon_Pistol.Single"
		"empty"		"Weapon_Pistol.Empty"
	}
}
"""


def split_vpk(folder, name, files, preload=0):
    """foo_dir.vpk with its data in foo_000.vpk; the first `preload` bytes of each file in the tree."""
    tree, blob = bytearray(), bytearray()
    for rel, data in files.items():
        d, _, base = rel.rpartition("/")
        stem, _, ext = base.rpartition(".")
        tree += ext.encode() + b"\0" + d.encode() + b"\0" + stem.encode() + b"\0"
        head, rest = data[:preload], data[preload:]
        tree += struct.pack("<IHHIIH", zlib.crc32(data) & 0xFFFFFFFF, len(head), 0, len(blob), len(rest), 0xFFFF)
        tree += head + b"\0\0"
        blob += rest
    tree += b"\0"
    write(os.path.join(folder, name + "_dir.vpk"), struct.pack("<III", 0x55AA1234, 1, len(tree)) + bytes(tree))
    write(os.path.join(folder, name + "_000.vpk"), bytes(blob))


ENTITIES = """{
"classname" "worldspawn"
"hammerid" "1"
}
{
"classname" "logic_relay"
"hammerid" "42"
"targetname" "start_relay"
"OnTrigger" "alyx,SpeakResponseConcept,TLK_A,0,-1"
}
"""


def bsp_bytes(entities):
    """A compiled map holding only its entity lump."""
    body = entities.encode() + b"\0"
    header = b"VBSP" + struct.pack("<i", 20)
    offset = len(header) + 64 * 16 + 4
    lumps = struct.pack("<iiii", offset, len(body), 0, 0) + b"\0" * (63 * 16)
    return header + lumps + struct.pack("<i", 1) + body


def lmp_bytes(entities):
    return sc.entities_lmp(entities, 0, 7)


def parse(text):
    return sc.flatten_kv(sc.parse_kv(text))


def value(flat, *keys):
    path = tuple((k, 1) for k in keys)
    return flat[path][2]


class KeyValues(unittest.TestCase):
    def test_parse_and_flatten(self):
        flat = parse(PISTOL)
        self.assertEqual(value(flat, "weapondata", "clip_size"), "18")
        self.assertEqual(value(flat, "weapondata", "sounddata", "empty"), "Weapon_Pistol.Empty")
        self.assertIs(value(flat, "weapondata", "sounddata"), sc.BLOCK)

    def test_repeated_keys_and_conditions_stay_apart(self):
        flat = parse('"S" { "wave" "a.wav" "wave" "b.wav" "x" "1" [$WIN32] "x" "2" [$X360] }')
        self.assertEqual(flat[(("s", 1), ("wave", 2))][2], "b.wav")
        self.assertEqual(flat[(("s", 1), ("x [$win32]", 1))][2], "1")
        self.assertEqual(flat[(("s", 1), ("x [$x360]", 1))][2], "2")

    def test_free_text_and_broken_files_arent_keyvalues(self):
        self.assertIsNone(sc.parse_kv("Thanks to everyone who helped make this mod"))
        self.assertIsNone(sc.parse_kv('"A" { "b" "c"'))
        self.assertIsNone(sc.parse_kv('"A" { "b" } }'))

    def test_round_trip(self):
        pairs = sc.parse_kv(PISTOL)
        self.assertEqual(sc.parse_kv("\n".join(sc.format_kv(pairs))), pairs)

    def test_random_sound_lists_are_sets(self):
        base = parse('"S" { "rndwave" { "wave" "a.wav" "wave" "b.wav" } }')
        one = parse('"S" { "rndwave" { "wave" "a.wav" "wave" "b.wav" "wave" "one.wav" } }')
        two = parse('"S" { "rndwave" { "wave" "A.WAV" "wave" "b.wav" "wave" "two.wav" } }')
        merged, conflicts = sc.merge_settings(base, [("one", one), ("two", two)])
        waves = sorted(v.lower() for _, _, v in merged.values() if v is not sc.BLOCK)
        self.assertEqual(waves, ["a.wav", "b.wav", "one.wav", "two.wav"])
        self.assertEqual(conflicts, [])
        self.assertEqual(sc.describe(sc.diff_settings(base, one), "kv"), ['+ s/rndwave/wave = "one.wav"'])

    def test_the_same_change_in_many_places_is_one_line(self):
        names = ["Snd.%d" % i for i in range(7)]
        old = parse("".join('"%s" { "channel" "CHAN_VOICE" }' % n for n in names))
        new = parse("".join('"%s" { "channel" "CHAN_STATIC" }' % n for n in names[:6]) + '"Snd.6" { "channel" "CHAN_ITEM" }')
        lines = sc.describe(sc.diff_settings(old, new), "kv")
        self.assertEqual(lines, ['channel: "CHAN_VOICE" -> "CHAN_STATIC"  in 6 places: snd.0, snd.1, snd.2, ...',
                                 'snd.6/channel: "CHAN_VOICE" -> "CHAN_ITEM"'])

    def test_repeats_in_one_place_are_counted_once(self):
        old = parse('"A" { "x" "1" } "B" { "x" "1" }')
        new = parse('"A" { "p" { "w" "1" } "p" { "w" "2" } "p" { "w" "3" } } "B" { "p" { "w" "4" } "p" { "w" "5" } }')
        self.assertEqual(sc.describe(sc.diff_settings(old, new, False), "kv"),
                         ["+ p = {...}  5 times in 2 places: a, b"])

    def test_diff(self):
        new = parse(PISTOL.replace('"18"', '"25"').replace('\t"damage"\t"8"\n', "")
                    .replace("SoundData", 'Extra { "a" "1" }\n\tSoundData'))
        changes = sc.describe(sc.diff_settings(parse(PISTOL), new), "kv")
        self.assertEqual(sorted(changes), sorted([
            'weapondata/clip_size: "18" -> "25"',
            "+ weapondata/extra = {...}",
            '- weapondata/damage (was "8")']))


class Cfg(unittest.TestCase):
    def test_settings(self):
        flat = sc.parse_cfg('sk_plr_dmg_pistol "8" // comment\nbind "F" "impulse 100"; bind g "+use"\n'
                            "echo hi\necho hi\nsk_plr_dmg_pistol 12\n")
        shown = sc.format_cfg(flat)
        self.assertIn("sk_plr_dmg_pistol 12", shown)
        self.assertNotIn('sk_plr_dmg_pistol "8"', shown)
        self.assertIn('bind "F" "impulse 100"', shown)
        self.assertIn('bind g "+use"', shown)
        self.assertEqual(shown.count("echo hi"), 2)

    def test_timed_scripts_keep_every_step(self):
        text = ("wait 5\nai_force_serverside_ragdoll 1\nent_fire a fireuser1\nwait 9\n"
                "ent_fire a fireuser1\nai_force_serverside_ragdoll 0\n")
        self.assertEqual(sc.format_cfg(sc.parse_cfg(text)), text.strip().splitlines())

    def test_quoted_and_unquoted_exec_are_the_same(self):
        old, new = sc.parse_cfg("exec skill.cfg\n"), sc.parse_cfg('exec "skill.cfg"\nexec "mine.cfg"\n')
        self.assertEqual(sc.describe(sc.diff_settings(old, new, False), "cfg"), ["+ exec mine.cfg"])


class Merging(unittest.TestCase):
    def test_changes_of_every_addon_are_kept_and_the_later_one_wins(self):
        base = parse(PISTOL)
        a = parse(PISTOL.replace('"18"', '"25"').replace('"8"', '"20"'))
        b = parse(PISTOL.replace('"18"', '"30"').replace("Weapon_Pistol.Empty", "Custom.Empty"))
        merged, conflicts = sc.merge_settings(base, [("A", a), ("B", b)])
        self.assertEqual(value(merged, "weapondata", "clip_size"), "30")
        self.assertEqual(value(merged, "weapondata", "damage"), "20")
        self.assertEqual(value(merged, "weapondata", "sounddata", "empty"), "Custom.Empty")
        self.assertEqual([(sc.show_path(p), a1, v1, a2, v2) for p, a1, v1, a2, v2 in conflicts],
                         [("weapondata/clip_size", "A", "25", "B", "30")])

    def test_new_blocks_from_two_addons_are_combined(self):
        a = parse('"Sounds" { "A.One" { "wave" "a.wav" } }')
        b = parse('"Sounds" { "B.One" { "wave" "b.wav" } }')
        merged, conflicts = sc.merge_settings(None, [("A", a), ("B", b)])
        text = "\n".join(sc.format_kv(sc.unflatten_kv(merged)))
        self.assertIn("a.wav", text)
        self.assertIn("b.wav", text)
        self.assertEqual(conflicts, [])

    def test_removed_setting(self):
        base = parse(PISTOL)
        a = parse(PISTOL.replace('\t"damage"\t"8"\n', ""))
        merged, _ = sc.merge_settings(base, [("A", a)])
        self.assertNotIn((("weapondata", 1), ("damage", 1)), merged)


class References(unittest.TestCase):
    def test_paths_the_game_would_load(self):
        refs = sc.asset_refs('"wave" "*^vo/npc/$gender01/hi.wav" // "wave" "old.wav"\n'
                             '"viewmodel" "models/weapons/v_gun.mdl" "x" "weapons\\Shot.WAV" "$basetexture" "a.vtf"')
        self.assertEqual(refs, {"sound/vo/npc/male01/hi.wav", "models/weapons/v_gun.mdl",
                                "sound/weapons/shot.wav", "materials/a.vtf"})


class Entities(unittest.TestCase):
    def test_entity_patch(self):
        parsed = sc.Parsed("maps/m_l_0.lmp", lmp_bytes(ENTITIES))
        self.assertEqual(parsed.kind, "ents")
        self.assertEqual(parsed.flat[(("logic_relay #42", 1), ("targetname", 1))][2], "start_relay")
        self.assertEqual(sc.parse_entities(sc.bsp_entities(bsp_bytes(ENTITIES)).decode()),
                         sc.parse_entities(ENTITIES))

    def test_added_and_removed_entities_are_named(self):
        old = sc.flatten_entities(sc.parse_entities(ENTITIES))
        new = sc.flatten_entities(sc.parse_entities(ENTITIES.replace('"logic_relay"', '"logic_branch"')))
        self.assertEqual(sorted(sc.describe(sc.diff_settings(old, new), "ents", sc.entity_names(
            sc.Parsed("maps/a_l_0.lmp", lmp_bytes(ENTITIES)), sc.Parsed("maps/a_l_0.lmp", lmp_bytes(
                ENTITIES.replace('"logic_relay"', '"logic_branch"')))))),
            ['+ entity logic_branch #42 "start_relay"', '- entity logic_relay #42 "start_relay"'])

    def test_round_trip(self):
        flat = sc.flatten_entities(sc.parse_entities(ENTITIES))
        again = sc.Parsed("maps/m_l_0.lmp", sc.entities_lmp("\n".join(sc.format_entities(flat))))
        self.assertEqual(again.flat, flat)


class ReadingContents(Base):
    FILES = {"scripts/a.txt": b"alpha", "cfg/b.cfg": b"bravo" * 100, "models/m.mdl": b"\0model"}

    def check(self, path):
        got = ac.read_vpk_contents(path, lambda p: not p.startswith("models/"))
        self.assertEqual(got, {"scripts/a.txt": b"alpha", "cfg/b.cfg": b"bravo" * 100})

    def test_plain_and_compressed(self):
        data = vpk_bytes(self.FILES)
        for name, packed in (("plain.vpk", data), ("v2.vpk", vpk_bytes(self.FILES, version=2)),
                             ("lzma.vpk", valve_lzma(data)), ("gz.vpk", gzip.compress(data))):
            write(self.path(name), packed)
            self.check(self.path(name))

    def test_zip_of_vpks(self):
        with zipfile.ZipFile(self.path("addon.zip"), "w") as z:
            z.writestr("addon.vpk", vpk_bytes(self.FILES))
        self.check(self.path("addon.zip"))

    def test_split_vpk_with_preload(self):
        split_vpk(self.tmp, "pak", self.FILES, preload=2)
        self.check(self.path("pak_dir.vpk"))

    def test_not_a_vpk(self):
        write(self.path("readme.txt"), "hello")
        self.assertIsNone(ac.read_vpk_contents(self.path("readme.txt"), lambda p: True))


class EndToEnd(Base):
    def setup_install(self):
        skill = "sk_plr_dmg_pistol 8\nsk_plr_dmg_smg1 4\n"
        lib, game, workshop = self.library(
            base_files={"scripts/weapon_pistol.txt": PISTOL.encode(), "cfg/skill.cfg": skill.encode(),
                        "maps/town.bsp": bsp_bytes(ENTITIES), "sound/weapons/pistol/fire.wav": b"RIFF",
                        "scripts/game_sounds_new.txt": b'"Valve.Cut" { "wave" "weapons/never_shipped.wav" }'},
            items={"111": {"scripts/weapon_pistol.txt": PISTOL.replace('"18"', '"25"').replace('"8"', '"12"'),
                           "models/gun.mdl": "\0mdl", "materials/gun.vmt": '"VertexLitGeneric" {}'},
                   "222": {"pak.vpk": vpk_bytes({
                       "scripts/weapon_pistol.txt": PISTOL.replace('"18"', '"30"').encode(),
                       "cfg/skill.cfg": b"sk_plr_dmg_pistol 8\nsk_plr_dmg_smg1 6\n",
                       "scripts/credits.txt": b"Made by someone"})},
                   "333": {"scripts/weapon_pistol.txt": PISTOL.replace('"18"', '"99"')},
                   "666": {"scripts/game_sounds_new.txt": '"Valve.Cut" { "wave" "weapons/never_shipped.wav" }\n'
                                                           '"New.Shot" { "wave" "weapons/new.wav" }\n'
                                                           '"Old.Shot" { "wave" "^weapons/pistol/fire.wav" }\n'
                                                           '"Gone.Shot" { "wave" "weapons/gone.wav" }',
                           "sound/weapons/new.wav": "RIFF"},
                   "444": {"maps/town_l_0.lmp": lmp_bytes(ENTITIES.replace("TLK_A", "TLK_B")),
                           "cfg/custom_scripts/19.cfg": "r_drawdecals 1\n"},
                   "555": {"maps/town_l_0.lmp": lmp_bytes(ENTITIES.replace('"start_relay"', '"go_relay"')),
                           "cfg/custom_scripts/19.cfg": "cl_ragdoll_fade_time 99999\n"}},
            order=[("111", 1), ("222", 1), ("333", 0), ("444", 1), ("555", 1), ("666", 1)],
            custom={"hl2": {"my_mod": {"cfg/skill.cfg": "sk_plr_dmg_smg1 5\n"}}})
        return game

    def run_tool(self, *args):
        old = sys.argv
        sys.argv = ["addon_scripts.py", "--offline", "--out", self.path("out")] + list(args)
        try:
            with quiet() as out:
                code = sc.main()
        finally:
            sys.argv = old
        return code, out.getvalue()

    def read(self, *parts):
        with open(self.path("out", *parts), encoding="utf-8") as f:
            return f.read()

    def test_report_and_combined_folder(self):
        game = self.setup_install()
        code, out = self.run_tool("--game-dir", game)
        self.assertEqual(code, 0, out)
        report = self.read("report.txt")
        self.assertIn('weapondata/clip_size: "18" -> "25"', report)
        self.assertIn('weapondata/damage: "8" -> "12"', report)
        self.assertIn('sk_plr_dmg_smg1: "4" -> "6"', report)
        self.assertIn("scripts/credits.txt  (new file, 1 line)\n       + Made by someone", report)
        self.assertIn("cfg/custom_scripts/19.cfg  (new file, 1 setting)\n       + r_drawdecals 1", report)
        self.assertIn("(in the game, only workshop/555's copy is used)", report)
        # Map entity patches, compared with the original map's entities
        self.assertIn('maps/town_l_0.lmp  (2 changes)\n'
                      '       + logic_relay #42/ontrigger = "alyx,SpeakResponseConcept,TLK_B,0,-1"\n'
                      '       - logic_relay #42/ontrigger (was "alyx,SpeakResponseConcept,TLK_A,0,-1")', report)
        self.assertIn('logic_relay #42/targetname: "start_relay" -> "go_relay"', report)
        self.assertIn("Also changes (not copied): 1 materials, 1 models", report)
        self.assertNotIn('"99"', report)  # 333 is turned off
        # Load order: custom/ first, then workshop.txt top to bottom; the later one wins.
        self.assertLess(report.index("hl2/custom/my_mod"), report.index("workshop/111"))
        self.assertLess(report.index("workshop/111"), report.index("workshop/222"))
        self.assertIn('weapondata/clip_size: workshop/111 sets "25", workshop/222 sets "30" -> workshop/222 wins',
                      report)
        self.assertIn('sk_plr_dmg_smg1: hl2/custom/my_mod sets "5", workshop/222 sets "6" -> workshop/222 wins',
                      report)

        pistol = sc.flatten_kv(sc.parse_kv(self.read("combined", "scripts", "weapon_pistol.txt")))
        self.assertEqual(value(pistol, "weapondata", "clip_size"), "30")
        self.assertEqual(value(pistol, "weapondata", "damage"), "12")  # from 111, which 222 didn't change
        skill = self.read("combined", "cfg", "skill.cfg")
        self.assertIn("sk_plr_dmg_smg1 6", skill)
        self.assertIn("sk_plr_dmg_pistol 8", skill)  # my_mod leaving it out doesn't take it away
        self.assertNotIn("- sk_plr_dmg_pistol", report)
        self.assertEqual(self.read("combined", "scripts", "credits.txt"), "Made by someone")
        with open(self.path("out", "combined", "maps", "town_l_0.lmp"), "rb") as f:
            town = sc.Parsed("maps/town_l_0.lmp", f.read())
        relay = {k[1][0]: v[2] for k, v in town.flat.items() if k[0][0] == "logic_relay #42" and len(k) == 2}
        self.assertEqual(relay["targetname"], "go_relay")  # 555's change
        self.assertIn("alyx,SpeakResponseConcept,TLK_B,0,-1", relay.values())  # and 444's
        self.assertNotIn("alyx,SpeakResponseConcept,TLK_A,0,-1", relay.values())
        self.assertEqual(town.lump[4], 7)  # map revision kept
        self.assertFalse(os.path.exists(self.path("out", "combined", "materials")))
        self.assertTrue(any(n.endswith("workshop_111") or "111" in n for n in os.listdir(self.path("out", "addons"))))

    def test_scripts_that_need_files_the_game_doesnt_have(self):
        game = self.setup_install()
        code, out = self.run_tool("--game-dir", game)
        report = self.read("report.txt")
        self.assertIn("Scripts that add sounds, models or materials the original game doesn't have:\n"
                      "  workshop/666: 2 files (1 its own, 1 missing)\n\n", report)
        self.assertIn("       sound/weapons/gone.wav  (missing)\n       sound/weapons/new.wav  (its own)", report)
        self.assertNotIn("pistol/fire.wav  (", report)  # the original game has it
        self.assertNotIn("never_shipped", report)  # the original script already names it
        self.assertIn("  1 VPK read: " + os.path.join("hl2", "hl2_pak_dir.vpk"), report)
        self.assertIn("1 addon's scripts use sounds", out)
        self.assertTrue(os.path.isfile(self.path("out", "combined", "scripts", "game_sounds_new.txt")))

        code, out = self.run_tool("--game-dir", game, "--original-files-only")
        self.assertEqual(code, 0, out)
        self.assertIn("Left out of combined/ (--original-files-only).", self.read("report.txt"))
        self.assertFalse(os.path.exists(self.path("out", "combined", "scripts", "game_sounds_new.txt")))
        self.assertTrue(os.path.isfile(self.path("out", "addons", "006 workshop_666", "scripts", "game_sounds_new.txt")))

    def test_loose_game_files_count_as_the_original_game(self):
        game = self.setup_install()
        write(os.path.join(game, "hl2", "sound", "weapons", "gone.wav"), "RIFF")
        self.run_tool("--game-dir", game)
        self.assertIn("  workshop/666: 1 file (1 its own)\n", self.read("report.txt"))

    def test_gameinfo_sources_are_listed(self):
        gameinfo = ('"GameInfo" { FileSystem { SearchPaths {\n game+mod hl2/custom/*\n'
                    ' game |all_source_engine_paths|hl2/hl2_pak.vpk\n game hl2/gone_pak.vpk\n'
                    ' gamebin hl2/bin\n game |all_source_engine_paths|hl2\n } } }')
        lib, game, workshop = self.library(gameinfo=gameinfo, base_files={"scripts/a.txt": b"x"},
                                           items={"1": {"scripts/b.txt": '"S" { "wave" "weapons/loose.wav" }'}},
                                           order=[("1", 1)])
        write(os.path.join(game, "hl2", "sound", "weapons", "loose.wav"), "RIFF")
        os.makedirs(os.path.join(game, "hl2", "bin"), exist_ok=True)
        self.run_tool("--game-dir", game)
        report = self.read("report.txt")
        self.assertIn("  1 VPK read: " + os.path.join("hl2", "hl2_pak_dir.vpk"), report)
        self.assertIn("  loose folders: hl2\n", report)
        self.assertIn("  named in gameinfo.txt but not there: " + os.path.join("hl2", "gone_pak.vpk"), report)
        self.assertIn("doesn't have:\n  none", report)  # loose.wav is a loose game file

    def test_materials_option(self):
        game = self.setup_install()
        self.run_tool("--game-dir", game, "--materials")
        self.assertTrue(os.path.isfile(self.path("out", "combined", "materials", "gun.vmt")))

    def test_without_the_game_everything_is_new(self):
        game = self.setup_install()
        workshop = os.path.join(os.path.dirname(os.path.dirname(game)), "workshop", "content", "220")
        code, out = self.run_tool(workshop, "--game-dir", self.path("nowhere"))
        self.assertEqual(code, 0, out)
        self.assertIn("scripts/weapon_pistol.txt  (new file", self.read("report.txt"))

    def test_folder_that_cant_be_opened(self):
        code, out = self.run_tool(self.path("missing"))
        self.assertEqual(code, 1)
        self.assertIn("Couldn't find", out)

    def test_other_folders_are_never_overwritten(self):
        game = self.setup_install()
        write(self.path("out", "combined", "keep.txt"), "mine")
        code, out = self.run_tool("--game-dir", game)
        self.assertEqual(code, 1)
        self.assertIn("already holds other files", out)
        self.assertEqual(self.read("combined", "keep.txt"), "mine")

    def test_earlier_results_are_replaced(self):
        game = self.setup_install()
        self.run_tool("--game-dir", game)
        write(self.path("out", "combined", "stale.txt"), "old")
        code, _ = self.run_tool("--game-dir", game)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.path("out", "combined", "stale.txt")))


if __name__ == "__main__":
    unittest.main()
