#!/usr/bin/env python3
"""Tests for the organizer window, addon_organizer.py.

Builds a fake Steam library in a temp folder, opens the real window on it (with
Steam's title lookup and the message boxes replaced) and checks load order
editing, saving, settings and the Problems actions.

    xvfb-run -a python3 -m unittest tests.test_addon_organizer -v

Needs tkinter and a display; without them every test is skipped.
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog
    import addon_checker as ac
    import addon_organizer as ao
except ImportError as e:
    tk = None
    IMPORT_ERROR = str(e)


def setUpModule():
    if tk is None:
        raise unittest.SkipTest("tkinter isn't available: %s" % IMPORT_ERROR)
    try:
        tk.Tk().destroy()
    except tk.TclError as e:
        raise unittest.SkipTest("no display: %s" % e)


# Workshop items (id: {path: content}), all loose files. 666 is installed but not in workshop.txt.
ADDONS = {
    "111": {"materials/shared/wall.vtf": b"111", "maps/m111.bsp": b"map 111"},
    "222": {"materials/shared/wall.vtf": b"222", "sound/s222.wav": b"s222"},
    "333": {"materials/shared/wall.vtf": b"333", "models/m333.mdl": b"m333"},
    "444": {"materials/shared/floor.vtf": b"444", "maps/m444.bsp": b"map 444"},
    "555": {"materials/shared/floor.vtf": b"555", "sound/s555.wav": b"s555"},
    "666": {"maps/m666.bsp": b"map 666"},
}
# Load order: by name it would be 111, 222, 333, 444, 555.
ORDER = [("333", True), ("111", True), ("555", True), ("222", True), ("444", False)]


def write(path, data=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def order_text(entries):
    return '"AddonInfo"\n{\n' + "".join('\t"%s"\t\t"%d"\n' % (w, e) for w, e in entries) + "}\n"


def make_library(steam_root, addons, order):
    """steam_root/steamapps/{common/Half-Life 2, workshop/content/220}. Returns (game, workshop, workshop.txt)."""
    game = os.path.join(steam_root, "steamapps", "common", "Half-Life 2")
    workshop = os.path.join(steam_root, "steamapps", "workshop", "content", "220")
    os.makedirs(os.path.join(game, "hl2", "custom"))
    os.makedirs(workshop)
    for wsid, files in addons.items():
        for path, data in files.items():
            write(os.path.join(workshop, wsid, *path.split("/")), data)
    txt = os.path.join(game, "hl2_complete", "cfg", "workshop.txt")
    write(txt, order_text(order))
    return game, workshop, txt


class OrganizerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="organizer_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        self.steam = os.path.join(home, ".steam", "steam")
        self.game, self.workshop, self.txt = make_library(self.steam, ADDONS, ORDER)
        write(os.path.join(self.game, "hl2", "custom", "mymod", "materials", "custom", "c.vtf"), b"custom")
        self.settings_file = os.path.join(self.tmp, "addon_rules.json")

        # Nothing outside the temp folder: settings, Steam, Steam's web API and message boxes.
        self.patch(ao, "SETTINGS_FILE", self.settings_file)
        self.patch(ac, "steam_roots", lambda: [self.steam])
        self.patch(ac, "fetch_workshop_titles", lambda ids, deadline=20: {i: "Addon %s" % i for i in ids})
        for name in ("HOME", "USERPROFILE"):
            self.addCleanup(self.restore_env, name, os.environ.get(name))
            os.environ[name] = home
        self.dialogs = []  # (kind, title, message)
        self.answers = []  # for the ask* boxes, in order; a callable is called. Default: yes / ok
        for kind in ("showinfo", "showwarning", "showerror"):
            self.patch(messagebox, kind, self.box(kind, "ok"))
        for kind in ("askyesno", "askokcancel"):
            self.patch(messagebox, kind, self.box(kind, True))
        self.patch(messagebox, "askyesnocancel", self.box("askyesnocancel", True))
        self.errors = []
        self.root = self.app = None

    def tearDown(self):
        self.close()

    def close(self, on_close=False):
        """Closes the window, first cancelling its timers so they don't fire into the next test's."""
        root, self.root = self.root, None
        if not root:
            return
        try:
            for job in root.tk.splitlist(root.tk.call("after", "info")):
                root.after_cancel(job)
            self.app.on_close() if on_close else root.destroy()
        except tk.TclError:
            pass

    def patch(self, obj, name, value):
        self.addCleanup(setattr, obj, name, getattr(obj, name))
        setattr(obj, name, value)

    @staticmethod
    def restore_env(name, value):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def box(self, kind, default):
        def show(title=None, message=None, **kw):
            self.dialogs.append((kind, title, message or ""))
            answer = self.answers.pop(0) if kind.startswith("ask") and self.answers else default
            return answer() if callable(answer) else answer
        return show

    def dialog_titles(self):
        return [d[1] for d in self.dialogs]

    # ---------------------------------------------------------------- driving the window

    def open(self, settings=None, raw_settings=None):
        """Starts the organizer and waits for its scan. settings are added to the folders to use;
        raw_settings is written to addon_rules.json as is instead."""
        if raw_settings is not None:
            with open(self.settings_file, "wb") as f:
                f.write(raw_settings if isinstance(raw_settings, bytes) else raw_settings.encode("utf-8"))
        else:
            data = {"game_dir": self.game, "workshop_dir": self.workshop}
            data.update(settings or {})
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda exc, val, tb: self.errors.append("%s: %s" % (exc.__name__, val))
        self.app = ao.Organizer(self.root)
        if not (settings or {}).get("geometry"):
            self.root.geometry("1200x800+0+0")
        self.wait()
        return self.app

    def wait(self, timeout=30):
        """Until the scan is done and its results are shown."""
        end = time.time() + timeout
        while time.time() < end:
            self.root.update()
            if not self.app.scanning and self.app.ui_queue.empty():
                break
            time.sleep(0.01)
        else:
            self.fail("the scan didn't finish")
        self.pump()

    def pump(self, n=5):
        for _ in range(n):
            self.root.update()

    def shown_listed(self):
        """Listed addons in the order the list shows them."""
        return [k for k in self.app.tree.get_children() if k in self.app.by_id and self.app.by_id[k].position]

    def enabled(self):
        return dict((w, e) for w, e in self.app.order)

    def center(self, key, col="name"):
        tree = self.app.tree
        tree.see(key)
        self.pump()
        cols = list(tree["displaycolumns"])
        x, y, w, h = tree.bbox(key, "#%d" % (cols.index(col) + 1))
        return x + w // 2, y + h // 2

    def press_time(self):
        """Event times a second apart, so two presses on one row don't count as a double-click."""
        self.clock = getattr(self, "clock", 100000) + 1000
        return self.clock

    def click(self, key, col="name"):
        x, y = self.center(key, col)
        t = self.press_time()
        self.app.tree.event_generate("<ButtonPress-1>", x=x, y=y, time=t)
        self.app.tree.event_generate("<ButtonRelease-1>", x=x, y=y, time=t + 10)

    def drag(self, src, dst, via=None):
        """Drags row src onto row dst, passing over row via on the way."""
        (x1, y1), (x0, y0) = self.center(src), self.center(dst)
        points = [self.center(via), (x0, y0)] if via else [(x0, y0)]
        tree = self.app.tree
        t = self.press_time()
        tree.event_generate("<ButtonPress-1>", x=x1, y=y1, time=t)
        self.pump()
        for x, y in points:
            tree.event_generate("<B1-Motion>", x=x, y=y, state=0x100, time=t + 10)
            self.pump()
        tree.event_generate("<ButtonRelease-1>", x=x0, y=y0, state=0x100, time=t + 20)
        self.pump()

    # ---------------------------------------------------------------- the list and dragging

    def test_drag_after_a_sorted_view_was_reset_keeps_the_load_order(self):  # ui-01
        app = self.open()
        app.sort_by_column("name")
        self.pump()
        self.assertNotEqual(self.shown_listed(), app.listed_ids())
        # Up arrow with nothing selected changes nothing, but goes back to the plain view.
        app.tree.selection_set(())
        app.move_selected(-1)
        self.pump()
        self.assertTrue(app.is_plain_view())
        self.assertEqual(self.shown_listed(), app.listed_ids())
        before = app.listed_ids()
        self.drag(before[1], before[0])
        self.assertEqual(app.listed_ids(), [before[1], before[0]] + before[2:])
        self.assertEqual(self.errors, [])

    def test_drag_uses_the_load_order_not_what_the_list_shows(self):  # ui-01
        app = self.open()
        before = app.listed_ids()
        for n, key in enumerate(reversed(before)):  # the list shows them backwards
            app.tree.move(key, "", n)
        shown = self.shown_listed()
        self.drag(shown[0], shown[1])  # the last addon, dropped on the one before it
        last, target = before[-1], before[-2]
        expected = [w for w in before if w != last]
        expected.insert(expected.index(target), last)
        self.assertEqual(app.listed_ids(), expected)
        self.assertEqual(self.shown_listed(), expected)

    def test_dragging_back_to_the_start(self):  # ui-01
        app = self.open()
        before = app.listed_ids()
        self.drag(before[1], before[1], via=before[2])  # down one row, then back up
        self.assertEqual(app.listed_ids(), before)
        self.drag(before[1], before[4], via=before[2])
        self.assertEqual(app.listed_ids(), [before[0], before[2], before[3], before[4], before[1]])

    def test_dragging_a_selected_row_moves_the_whole_selection(self):  # ui-10
        app = self.open()
        ids = app.listed_ids()
        app.tree.selection_set(ids[0:2])
        self.pump()
        self.drag(ids[1], ids[3], via=ids[2])
        self.assertEqual(app.listed_ids(), [ids[2], ids[3], ids[0], ids[1], ids[4]])
        self.assertEqual(set(app.tree.selection()), set(ids[0:2]))
        # A plain click on one of them, without dragging, still selects just that one.
        self.click(ids[1])
        self.pump()
        self.assertEqual(app.tree.selection(), (ids[1],))

    def test_held_keys_rebuild_the_list_once(self):  # ui-04
        app = self.open()
        calls = []
        real = app.recompute
        app.recompute = lambda: calls.append(1) or real()
        key = app.listed_ids()[0]
        app.tree.selection_set(key)
        app.tree.focus(key)
        app.tree.focus_force()
        self.pump()
        for _ in range(3):  # a held key: auto-repeats arrive before the first is handled
            app.tree.event_generate("<Control-Down>", when="tail")
        self.pump()
        self.assertEqual(app.listed_ids().index(key), 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.shown_listed(), app.listed_ids())
        self.assertEqual(app.tree.selection(), (key,))
        # Each step can still be undone on its own.
        app.undo()
        self.assertEqual(app.listed_ids().index(key), 2)

    def test_quick_clicks_land_on_the_rows_they_were_aimed_at(self):  # ui-04, ui-08
        app = self.open()
        app.set_order([["333", False], ["111", False], ["555", False], ["222", True], ["444", False]])
        app.view.set("Disabled")
        self.pump()
        rows = list(app.tree.get_children())
        self.assertEqual(rows[:3], ["333", "111", "555"])
        # Both clicks wait in the queue, as they do while the window is busy.
        points = [self.center(rows[0], "on"), self.center(rows[1], "on")]
        for x, y in points:
            t = self.press_time()
            app.tree.event_generate("<ButtonPress-1>", x=x, y=y, time=t, when="tail")
            app.tree.event_generate("<ButtonRelease-1>", x=x, y=y, time=t + 10, when="tail")
        self.pump()
        self.assertEqual(self.enabled(), {"333": True, "111": True, "555": False, "222": True, "444": False})
        self.assertEqual(self.errors, [])

    def test_a_click_made_while_the_list_was_rebuilt_is_skipped(self):  # ui-04
        app = self.open()
        x, y = self.center("111", "on")
        app.refresh_list()  # the click below waited while this ran, so it was aimed at the old rows
        app.tree.event_generate("<ButtonPress-1>", x=x, y=y, time=self.press_time())
        self.assertTrue(self.enabled()["111"])
        self.pump()
        self.click("111", "on")  # once the list is shown, clicks work again
        self.pump()
        self.assertFalse(self.enabled()["111"])

    def test_checkbox_in_a_filtered_view(self):  # ui-08
        app = self.open()
        app.set_order([["333", False], ["111", False], ["555", True], ["222", True], ["444", False]])
        app.view.set("Disabled")
        self.pump()
        rows = list(app.tree.get_children())
        self.click(rows[0], "on")
        self.pump()
        self.assertTrue(self.enabled()[rows[0]])
        self.assertNotIn(rows[1], app.tree.selection())  # the click didn't select the row that moved up
        self.assertEqual(self.errors, [])

    def test_moving_doesnt_rebuild_the_file_index(self):  # ui-04
        app = self.open()
        calls = []
        real = ac.index_files
        self.patch(ac, "index_files", lambda addons: calls.append(1) or real(addons))
        app.tree.selection_set(app.listed_ids()[1])
        app.move_selected(1)
        self.assertEqual(calls, [])
        app.toggle_enabled()  # which files are loaded changes
        self.assertEqual(calls, [1])
        self.assertEqual(self.shown_listed(), app.listed_ids())

    # ---------------------------------------------------------------- scanning

    def test_an_id_listed_twice_in_workshop_txt(self):  # org-01, ui-02
        write(self.txt, order_text([("111", 1), ("333", 1), ("222", 1), ("111", 0), ("555", 1), ("444", 0)]))
        app = self.open()
        self.assertEqual(self.errors, [])
        # The last line for an id counts, as in the checker.
        self.assertEqual(app.order, [["333", True], ["222", True], ["111", False], ["555", True], ["444", False]])
        self.assertEqual(self.shown_listed(), ["333", "222", "111", "555", "444"])
        warning = [d for d in self.dialogs if d[1] == "Addons listed twice"]
        self.assertEqual(len(warning), 1)
        self.assertIn("Addon 111", warning[0][2])
        # Rescanning still works.
        write(self.txt, order_text(ORDER))
        app.start_scan(confirm=False)
        self.wait(timeout=10)
        self.assertEqual(app.listed_ids(), [w for w, _ in ORDER])

    def test_a_failing_window_update_doesnt_stop_the_others(self):  # ui-02, org-01
        app = self.open()
        ran = []

        def boom():
            raise RuntimeError("boom")
        app.scanning = True
        app.ui(boom)
        app.ui(lambda: ran.append(1))
        end = time.time() + 3
        while not ran and time.time() < end:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(ran, [1])
        self.assertFalse(app.scanning)
        self.assertEqual(self.errors, ["RuntimeError: boom"])
        self.assertIn("Something went wrong", self.dialog_titles())
        app.ui(lambda: ran.append(2))
        end = time.time() + 3
        while len(ran) < 2 and time.time() < end:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(ran, [1, 2])

    def test_new_folders_during_a_scan_are_scanned(self):  # ui-06
        app = self.open()
        other = os.path.join(self.tmp, "library2")
        game2, workshop2, _ = make_library(other, {"801": {"maps/m801.bsp": b"801"}}, [("801", True)])
        titles, looking_up = ac.fetch_workshop_titles, threading.Event()

        def slow_titles(ids, deadline=20):  # the scan has read the folders by now
            looking_up.set()
            time.sleep(0.5)
            return titles(ids)
        self.patch(ac, "fetch_workshop_titles", slow_titles)
        app.start_scan(confirm=False)
        self.assertTrue(looking_up.wait(10))
        self.assertTrue(app.scanning)
        app.game_dir, app.workshop_dir = game2, workshop2  # as Folders... OK does
        app.start_scan(confirm=False)
        self.wait()
        self.assertEqual(app.found_workshop, workshop2)
        self.assertEqual(sorted(app.by_id), ["801"])

    def test_rescan_with_an_addon_selected(self):  # ui-08
        app = self.open()
        app.tree.selection_set(app.listed_ids()[0])
        self.pump()
        app.start_scan(confirm=False)
        self.wait()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.shown_listed(), [w for w, _ in ORDER])

    # ---------------------------------------------------------------- saving

    def backups(self):
        folder = os.path.dirname(self.txt)
        return sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".bak"))

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_save_notices_changes_made_by_the_game(self):  # org-04
        app = self.open()
        game_version = self.read(self.txt)
        app.tree.selection_set("555")
        app.move_selected("top")
        app.save_order()
        self.assertEqual(ac.read_load_order(self.txt), [(w, bool(e)) for w, e in app.order])
        self.assertEqual([self.read(b) for b in self.backups()], [game_version])

        # Saving again over our own file needs no new backup.
        app.move_selected(1)
        app.save_order()
        self.assertEqual(len(self.backups()), 1)

        # Now the game turns 333 off and adds a new subscription.
        changed = order_text([("555", 1), ("333", 0), ("111", 1), ("222", 1), ("444", 0), ("777", 1)])
        write(self.txt, changed)
        app.move_selected(1)
        self.dialogs = []
        self.answers = [None]  # Cancel
        app.save_order()
        self.assertEqual(self.dialog_titles(), ["workshop.txt has changed"])
        self.assertEqual(self.read(self.txt), changed)

        self.answers = [False]  # No: save anyway
        app.save_order()
        self.assertEqual(ac.read_load_order(self.txt), [(w, bool(e)) for w, e in app.order])
        # The game's new version is backed up too, under a new name even within the same second.
        self.assertEqual(sorted(self.read(b) for b in self.backups()), sorted([game_version, changed]))

        # Yes: rescan and load the game's version.
        write(self.txt, changed)
        app.move_selected(1)
        self.answers = [True]
        app.save_order()
        self.wait()
        self.assertEqual(self.read(self.txt), changed)
        self.assertEqual(app.order[:2], [["555", True], ["333", False]])
        self.assertFalse(app.is_dirty())

    # ---------------------------------------------------------------- settings

    def test_settings_with_a_byte_order_mark(self):  # ui-03
        text = json.dumps({"game_dir": self.game, "workshop_dir": self.workshop,
                           "rules": [{"winner": "111", "loser": "333"}]})
        app = self.open(raw_settings=b"\xef\xbb\xbf" + text.encode("utf-8"))
        self.assertEqual(len(app.rules), 1)
        self.assertNotIn("Settings", self.dialog_titles())

    def test_damaged_settings_are_kept(self):  # org-06, ui-03
        text = ('{"rules": [{"winner": "111", "loser": "333"}], "notes": {"111": "keep me"}, '
                '"profiles": {"p": [["111", true]]},}')  # a trailing comma
        app = self.open(raw_settings=text)
        self.assertEqual(app.rules, [])
        warnings = [d for d in self.dialogs if d[1] == "Settings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("addon_rules.json.broken", warnings[0][2])
        self.close(on_close=True)
        with open(self.settings_file + ".broken", encoding="utf-8") as f:
            self.assertEqual(f.read(), text)
        with open(self.settings_file, encoding="utf-8") as f:
            self.assertIsInstance(json.load(f), dict)

    def test_settings_of_the_wrong_shape(self):  # ui-03
        app = self.open(raw_settings="[1, 2]")
        self.assertEqual(app.rules, [])
        self.assertIn("Settings", self.dialog_titles())
        self.close()

        self.dialogs = []
        app = self.open(raw_settings=json.dumps({
            "game_dir": self.game, "workshop_dir": self.workshop,
            "rules": [5, {"winner": 111, "loser": "333"}, {"winner": "111"}], "notes": ["x"],
            "profiles": {"p": [["111", True], "junk", ["111", False]], "q": 3}, "theme": ["x"],
            "hidden_columns": "size", "sash": "wide", "geometry": 12}))
        self.assertEqual([(r["winner"], r["loser"]) for r in app.rules], [("111", "333")])
        self.assertEqual(app.notes, {})
        self.assertEqual(app.profiles, {"p": [["111", False]]})
        self.assertEqual(app.theme_name.get(), "dark")
        app.load_profile("p")
        self.assertEqual(self.errors, [])

    def test_settings_are_written_safely(self):  # org-06
        app = self.open()
        app.notes["111"] = "first"
        app.save_settings()
        with open(self.settings_file, encoding="utf-8") as f:
            good = f.read()

        def disk_full(obj, f, **kw):
            f.write(json.dumps(obj)[:20])
            raise OSError(28, "No space left on device")
        self.patch(json, "dump", disk_full)
        app.notes["111"] = "second"
        app.save_settings()
        self.assertIn("Couldn't save settings", self.dialog_titles())
        with open(self.settings_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), good)

    # ---------------------------------------------------------------- notes and keys

    def test_ctrl_z_in_a_text_box_is_for_the_text(self):  # org-08, ui-07
        app = self.open()
        before = [list(e) for e in app.order]
        app.tree.selection_set("555")
        app.move_selected("top")
        moved = [list(e) for e in app.order]
        self.assertNotEqual(moved, before)
        app.tree.selection_set("111")
        self.pump()
        app.note_text.set("my note")  # typed, not saved yet
        for entry in (app.note_entry, app.filter_entry, app.find_entry):
            entry.focus_force()
            self.pump()
            entry.event_generate("<Control-z>")
            self.pump()
            self.assertEqual(app.order, moved)
        self.assertEqual(app.note_entry.get(), "my note")
        # In the list it still undoes.
        app.tree.focus_force()
        self.pump()
        app.tree.event_generate("<Control-z>")
        self.pump()
        self.assertEqual(app.order, before)
        self.assertEqual(app.note_entry.get(), "my note")
        self.assertEqual(app.notes.get("111"), "my note")

    def test_a_typed_note_survives_a_checkbox_click(self):  # ui-07
        app = self.open()
        app.tree.selection_set("111")
        self.pump()
        app.note_entry.focus_force()
        app.note_text.set("breaks the pistol")
        self.pump()
        self.click("222", "on")
        self.pump()
        self.assertEqual(app.notes.get("111"), "breaks the pistol")
        self.assertFalse(self.enabled()["222"])

    # ---------------------------------------------------------------- details side

    def test_files_tab_of_addons_the_game_doesnt_load(self):  # org-05
        app = self.open()
        for key, state in (("444", "not loaded (turned off)"), ("666", "not loaded (not in workshop.txt)")):
            app.tree.selection_set(key)
            self.pump()
            rows = app.files_tree.get_children()
            self.assertEqual(len(rows), len(ADDONS[key]))
            for row in rows:
                self.assertEqual(app.files_tree.item(row, "values")[0], state)
                self.assertEqual(tuple(app.files_tree.item(row, "tags")), ("inactive",))

    def test_an_order_change_keeps_the_tab_shown(self):  # ui-09
        app = self.open()
        app.find_text.set("wall")
        app.run_find()
        self.assertEqual(app.tabs.select(), str(app.find_tab))
        app.tree.selection_set("111")
        self.pump()
        app.tabs.select(0)
        conflicts = app.tabs.select()
        self.click("222", "on")
        self.pump()
        self.assertEqual(app.tabs.select(), conflicts)
        app.move_selected(1)
        self.pump()
        self.assertEqual(app.tabs.select(), conflicts)

    def test_selection_count_and_find_results(self):  # ui-12
        app = self.open()
        ids = app.listed_ids()
        app.tree.selection_set(ids[0:3])
        self.pump()
        self.assertEqual(app.detail_title.cget("text"), "3 addons selected")
        app.tree.selection_add(ids[3])
        self.pump()
        self.assertEqual(app.detail_title.cget("text"), "4 addons selected")

        app.find_text.set("wall")
        app.run_find()
        self.pump()
        node = app.find_tree.get_children()[0]
        self.assertTrue(app.find_tree.item(node, "open"))
        x, y, w, h = app.find_tree.bbox(node)
        for t in (100000, 100100):  # a double-click on a file row opens or closes it
            app.find_tree.event_generate("<ButtonPress-1>", x=x + w // 2, y=y + h // 2, time=t)
            app.find_tree.event_generate("<ButtonRelease-1>", x=x + w // 2, y=y + h // 2, time=t + 10)
        self.pump()
        self.assertFalse(app.find_tree.item(node, "open"))

    def test_window_saved_on_a_missing_monitor(self):  # ui-11
        # The size the window gets for this geometry on screen; the screen (Windows shrinks a window
        # wider than the screen) and the window's minimum size can change it, so compare with it.
        self.open(settings={"geometry": "1100x650+40+30"})
        self.pump(20)
        self.assertLessEqual(abs(self.root.winfo_x() - 40), 10)
        width = self.root.winfo_width()
        self.close()
        self.open(settings={"geometry": "1100x650+3000+2500"})
        self.pump(20)
        self.assertLess(self.root.winfo_x(), self.root.winfo_screenwidth() - 100)
        self.assertLess(self.root.winfo_y(), self.root.winfo_screenheight() - 50)
        self.assertEqual(self.root.winfo_width(), width)

    # ---------------------------------------------------------------- tools

    def test_rules_in_a_circle(self):  # org-07
        app = self.open()
        app.rules = [{"winner": "111", "loser": "222"}, {"winner": "222", "loser": "333"},
                     {"winner": "333", "loser": "111"}, {"winner": "555", "loser": "111"}]
        app.sort_by_rules()
        errors = [d for d in self.dialogs if d[1] == "Rules contradict each other"]
        self.assertEqual(len(errors), 1)
        for line in ("Addon 111 should win over Addon 222", "Addon 222 should win over Addon 333",
                     "Addon 333 should win over Addon 111"):
            self.assertIn(line, errors[0][2])
        self.assertNotIn("Addon 555", errors[0][2])
        # The rule outside the circle is applied: the winner loads later.
        ids = app.listed_ids()
        self.assertGreater(ids.index("555"), ids.index("111"))

    def test_delete_leftovers(self):  # org-09
        cache = os.path.join(self.workshop, "999", "workshop.vpk.sound.cache")
        write(cache, b"cache")
        write(os.path.join(self.workshop, "997", "sound", "sound.cache"), b"cache")
        os.makedirs(os.path.join(self.game, "hl2", "custom", "emptywip"))
        precious = os.path.join(self.tmp, "precious")
        write(os.path.join(precious, "brick.vtf"), b"real content")
        # 998's content is behind a link inside it, which the scan doesn't follow.
        link = os.path.join(self.workshop, "998", "materials", "brick")
        write(os.path.join(self.workshop, "998", "sound", "sound.cache"), b"cache")
        os.makedirs(os.path.dirname(link))
        try:
            os.symlink(precious, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            link = None  # no symlinks here (Windows without the right): skip that part
        app = self.open()
        found = {os.path.basename(a.location) for a in app.leftovers}
        self.assertEqual(found, {"999", "997"})
        self.assertEqual([os.path.basename(a.location) for a in app.empty_folders], ["emptywip"])

        problems = {p[1]: p for p in app.problems()}
        workshop_item = [p for t, p in problems.items() if "leftover Workshop folder" in t]
        self.assertEqual(len(workshop_item), 1)
        self.assertEqual(workshop_item[0][3], "Delete them")
        custom_item = [p for t, p in problems.items() if "no addon files" in t]
        self.assertEqual(len(custom_item), 1)
        self.assertIn("emptywip", custom_item[0][2])
        self.assertIsNone(custom_item[0][3])

        def game_writes_meanwhile():  # into 997, while the confirmation is up
            write(os.path.join(self.workshop, "997", "maps", "new.bsp"), b"map")
            return True
        self.answers = [game_writes_meanwhile]
        app.delete_leftovers()
        self.assertFalse(os.path.exists(os.path.join(self.workshop, "999")))
        self.assertTrue(os.path.exists(os.path.join(self.workshop, "997", "maps", "new.bsp")))
        self.assertTrue(os.path.isdir(os.path.join(self.game, "hl2", "custom", "emptywip")))
        self.assertTrue(os.path.isfile(os.path.join(precious, "brick.vtf")))
        if link:
            self.assertTrue(os.path.islink(link))
        self.assertIn("Leftover folders", self.dialog_titles())  # 997 is reported, not deleted

    def test_a_folder_that_cant_be_read_is_not_deleted(self):  # org-09
        folder = os.path.join(self.tmp, "leftover")
        write(os.path.join(folder, "workshop.vpk.sound.cache"), b"cache")
        self.assertTrue(ao.only_cache_files(folder))
        real_walk = os.walk

        def walk(top, onerror=None, **kw):
            for step in real_walk(top, onerror=onerror, **kw):
                yield step
            if onerror:
                onerror(PermissionError(13, "Permission denied", os.path.join(top, "locked")))
        self.patch(os, "walk", walk)
        self.assertFalse(ao.only_cache_files(folder))


    # ---------------------------------------------------------------- what the checker decides

    def test_copied_item_folder_doesnt_make_the_item_removable(self):  # org-02
        for path, data in ADDONS["666"].items():
            write(os.path.join(self.workshop, "666 - Copy", *path.split("/")), data)
        write(os.path.join(self.txt), order_text(ORDER + [("666", True)]))
        app = self.open()
        self.assertNotIn(app.by_id["666"], app.removable)
        self.assertEqual([os.path.basename(a.location) for a in app.strays], ["666 - Copy"])
        self.assertNotIn("custom:" + os.path.join(self.workshop, "666 - Copy"), app.tree.get_children())
        self.assertTrue(any("doesn't load" in p[1] for p in app.problems()))

    def test_partly_read_addon_is_never_removable(self):  # org-03
        # 222 loses its wall to 111; its other content is in a VPK that can't be read.
        write(os.path.join(self.workshop, "222", "pak01_dir.vpk"), b"\x34\x12\xaa\x55\x03\x00\x00\x00")
        os.remove(os.path.join(self.workshop, "222", "sound", "s222.wav"))
        app = self.open()
        a = app.by_id["222"]
        self.assertTrue(a.errors)
        self.assertNotIn(a, app.removable)
        self.assertEqual(app.status_text(a), "problem reading")

    def test_removable_only_together(self):
        # 111 = X (#3, loads last), 222 = Y (#2), 333 = X plus its own file (#1): 111 and 222 do nothing
        # together, but without 222, removing 111 alone would make 222's Y the one used.
        addons = {"111": {"materials/w.vtf": b"X"}, "222": {"materials/w.vtf": b"Y"},
                  "333": {"materials/w.vtf": b"X", "maps/own.bsp": b"own"}}
        shutil.rmtree(self.workshop)
        for wsid, files in addons.items():
            for path, data in files.items():
                write(os.path.join(self.workshop, wsid, *path.split("/")), data)
        write(self.txt, order_text([("333", True), ("222", True), ("111", True)]))
        app = self.open()
        self.assertEqual({a.workshop_id for a in app.removable}, {"111", "222"})
        self.assertEqual(app.status_text(app.by_id["222"]), "does nothing")
        self.assertEqual(app.status_text(app.by_id["111"]), "does nothing (only with 1 other)")
        self.assertEqual(app.flags(app.by_id["111"]), "✕")


    # ---------------------------------------------------------------- custom/ addons and installing

    def custom_dir(self):
        return os.path.join(self.game, "hl2_complete", "custom")  # the first one the game loads

    def row(self, key, col):
        cols = [c[0] for c in ao.COLUMNS]
        return self.app.tree.item(key, "values")[cols.index(col)]

    def test_custom_addons_load_first(self):
        write(os.path.join(self.game, "hl2", "custom", "aaa", "materials", "shared", "wall.vtf"), b"custom")
        app = self.open()
        rows = app.tree.get_children()
        customs = ["custom:" + os.path.join(self.game, "hl2", "custom", n) for n in ("aaa", "mymod")]
        self.assertEqual(list(rows[:2]), customs)
        self.assertEqual([self.row(k, "pos") for k in customs], ["C1", "C2"])
        # They load first, so every Workshop addon's wall beats aaa's.
        self.assertEqual(app.stats[app.addon_for_key(customs[0])], [0, 1])

    def test_turning_a_custom_addon_off_and_on(self):
        mymod = os.path.join(self.game, "hl2", "custom", "mymod")
        off = os.path.join(self.game, "hl2", "custom_disabled", "mymod")
        app = self.open()
        key = "custom:" + mymod
        app.notes[key] = "my texture fix"
        self.click(key, col="on")
        self.pump()
        self.assertFalse(os.path.exists(mymod))
        self.assertTrue(os.path.isfile(os.path.join(off, "materials", "custom", "c.vtf")))
        a = app.addon_for_key("custom:" + off)
        self.assertTrue(a.custom_off)
        self.assertIsNone(a.rank)
        self.assertEqual(self.row("custom:" + off, "on"), "☐")
        self.assertEqual(app.status_text(a), "disabled")
        self.assertEqual(app.notes.get("custom:" + off), "my texture fix")  # the note moves along
        # A rescan finds it in custom_disabled/, still off.
        app.start_scan(confirm=False)
        self.wait()
        self.assertTrue(app.addon_for_key("custom:" + off).custom_off)
        # Space turns it back on.
        app.tree.selection_set("custom:" + off)
        app.toggle_enabled()
        self.pump()
        self.assertTrue(os.path.isdir(mymod) and not os.path.exists(off))
        self.assertEqual(self.row(key, "pos"), "C1")

    def test_turning_on_doesnt_overwrite(self):
        write(os.path.join(self.game, "hl2", "custom_disabled", "mymod", "materials", "old.vtf"), b"old")
        app = self.open()
        self.click("custom:" + os.path.join(self.game, "hl2", "custom_disabled", "mymod"), col="on")
        self.pump()
        self.assertTrue(any("already there" in d[2] for d in self.dialogs))
        self.assertTrue(os.path.isfile(os.path.join(self.game, "hl2", "custom", "mymod", "materials", "custom",
                                                    "c.vtf")))
        self.assertTrue(app.addon_for_key("custom:" + os.path.join(self.game, "hl2", "custom_disabled",
                                                                   "mymod")).custom_off)

    def install(self, *paths):
        self.app.install_paths(list(paths))
        self.wait()

    def test_install_a_wrapped_folder(self):
        app = self.open()
        src = os.path.join(self.tmp, "download", "HD Crowbar")
        write(os.path.join(src, "readme.txt"), b"thanks")
        write(os.path.join(src, "HD Crowbar", "materials", "models", "crowbar.vtf"), b"vtf")
        self.install(src)
        dest = os.path.join(self.custom_dir(), "HD Crowbar")
        self.assertTrue(os.path.isfile(os.path.join(dest, "materials", "models", "crowbar.vtf")))
        self.assertTrue(os.path.isfile(os.path.join(src, "readme.txt")))  # copied, not moved
        key = "custom:" + dest
        self.assertIn(key, app.tree.get_children())
        self.assertEqual(self.row(key, "pos"), "C1")

    def test_install_a_split_vpk(self):
        self.open()
        src = os.path.join(self.tmp, "download")
        for name in ("big_dir.vpk", "big_000.vpk", "big_001.vpk", "other.vpk"):
            write(os.path.join(src, name), b"x")
        self.install(os.path.join(src, "big_000.vpk"))  # dropping a part brings the whole set
        self.assertEqual(sorted(os.listdir(self.custom_dir())), ["big_000.vpk", "big_001.vpk", "big_dir.vpk"])

    def zip_file(self, name, members):
        path = os.path.join(self.tmp, "download", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            for member, data in members.items():
                z.writestr(member, data)
        return path

    def test_install_zips(self):
        self.open()
        folder_zip = self.zip_file("pack-v2.zip", {"Cool Pack/materials/a.vtf": b"a", "Cool Pack/readme.txt": b"r"})
        loose_zip = self.zip_file("maps.zip", {"map_a.vpk": b"a", "map_b.vpk": b"b"})
        flat_zip = self.zip_file("flat.zip", {"models/m.mdl": b"m", "credits.txt": b"c"})
        self.install(folder_zip, loose_zip, flat_zip)
        self.assertEqual(sorted(os.listdir(self.custom_dir())), ["Cool Pack", "flat", "map_a.vpk", "map_b.vpk"])
        self.assertTrue(os.path.isfile(os.path.join(self.custom_dir(), "flat", "models", "m.mdl")))
        self.assertEqual(self.dialogs, [])

    def test_unsafe_and_unknown_files_arent_installed(self):
        self.open()
        evil = self.zip_file("evil.zip", {"../../outside.txt": b"x", "materials/a.vtf": b"a"})
        rar = os.path.join(self.tmp, "download", "pack.rar")
        write(rar, b"Rar!")
        self.install(evil, rar)
        self.assertFalse(os.path.exists(self.custom_dir()) and os.listdir(self.custom_dir()))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "outside.txt")))
        message = [d[2] for d in self.dialogs if d[1] == "Install addon"][0]
        self.assertIn("outside its own folder", message)
        self.assertIn("7-Zip", message)

    def test_replacing_asks_first(self):
        self.open()
        old = os.path.join(self.custom_dir(), "pack", "materials", "old.vtf")
        write(old, b"old")
        src = os.path.join(self.tmp, "download", "pack")
        write(os.path.join(src, "materials", "new.vtf"), b"new")
        self.answers = [False]
        self.install(src)
        self.assertTrue(os.path.isfile(old))
        self.answers = [True]
        self.install(src)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isfile(os.path.join(self.custom_dir(), "pack", "materials", "new.vtf")))

    @unittest.skipUnless(sys.platform == "win32", "drag and drop from Explorer is Windows only")
    def test_drop_files_on_the_window(self):
        import ctypes
        from ctypes import wintypes
        app = self.open()
        self.assertTrue(app.file_drop)
        src = os.path.join(self.tmp, "download", "dropped")
        write(os.path.join(src, "materials", "d.vtf"), b"d")
        # What Explorer sends: a DROPFILES block (offset of the names, point, flags, wide) and the names.
        data = struct.pack("<IiiII", 20, 0, 0, 0, 1) + (src + "\0\0").encode("utf-16-le")
        kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        handle = kernel32.GlobalAlloc(0x0040, len(data))  # GPTR: fixed and zeroed; DragFinish frees it
        ctypes.memmove(handle, data, len(data))
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW(int(self.root.wm_frame(), 16), 0x0233, handle, 0)
        self.pump()
        self.wait()
        self.assertTrue(os.path.isfile(os.path.join(self.custom_dir(), "dropped", "materials", "d.vtf")))


    # ---------------------------------------------------------------- custom/ files: rename, remove, undo

    def custom_path(self, *parts):
        return os.path.join(self.game, "hl2", "custom", *parts)

    def test_rename_changes_place_and_undo_puts_it_back(self):
        write(self.custom_path("aaa", "materials", "a.vtf"), b"a")
        app = self.open()
        key = "custom:" + self.custom_path("mymod")
        app.notes[key] = "keep me"
        self.assertEqual(self.row(key, "pos"), "C2")
        app.tree.selection_set(key)
        self.patch(simpledialog, "askstring", lambda *a, **k: "000mod")
        app.rename_custom()
        self.pump()
        new_key = "custom:" + self.custom_path("000mod")
        self.assertTrue(os.path.isdir(self.custom_path("000mod")) and not os.path.exists(self.custom_path("mymod")))
        self.assertEqual(self.row(new_key, "pos"), "C1")  # now first, A to Z
        self.assertEqual(app.notes.get(new_key), "keep me")
        app.undo_file_change()
        self.pump()
        self.assertTrue(os.path.isdir(self.custom_path("mymod")) and not os.path.exists(self.custom_path("000mod")))
        self.assertEqual(self.row(key, "pos"), "C2")
        self.assertEqual(app.notes.get(key), "keep me")

    def test_bad_names_are_refused(self):
        app = self.open()
        app.tree.selection_set("custom:" + self.custom_path("mymod"))
        self.patch(simpledialog, "askstring", lambda *a, **k: "my:mod")
        app.rename_custom()
        self.assertTrue(os.path.isdir(self.custom_path("mymod")))
        self.assertIn("Rename", self.dialog_titles())

    def test_a_split_vpk_moves_with_its_parts(self):
        # One file, scripts/a.txt, with its data in big_000.vpk.
        tree = b"txt\0scripts\0a\0" + struct.pack("<IHHIIH", 0, 0, 0, 0, 4, 0xFFFF) + b"\0\0\0"
        write(self.custom_path("big_dir.vpk"), struct.pack("<III", 0x55AA1234, 1, len(tree)) + tree)
        write(self.custom_path("big_000.vpk"), b"data")
        app = self.open()
        app.toggle_custom(["custom:" + self.custom_path("big_dir.vpk")])
        off = os.path.join(self.game, "hl2", "custom_disabled")
        self.assertEqual(sorted(os.listdir(off)), ["big_000.vpk", "big_dir.vpk"])
        self.assertEqual(sorted(os.listdir(self.custom_path())), ["mymod"])

    def test_remove_goes_to_the_trash(self):
        trash = os.path.join(self.tmp, "trash")
        os.makedirs(trash)
        self.patch(ao, "send_to_trash", lambda p: shutil.move(p, os.path.join(trash, os.path.basename(p))))
        app = self.open()
        app.tree.selection_set("custom:" + self.custom_path("mymod"))
        self.answers = [False]
        app.remove_custom()
        self.assertTrue(os.path.isdir(self.custom_path("mymod")))  # said no
        app.remove_custom()  # yes
        self.assertFalse(os.path.exists(self.custom_path("mymod")))
        self.assertEqual(os.listdir(trash), ["mymod"])
        self.assertNotIn("custom:" + self.custom_path("mymod"), app.tree.get_children())

    @unittest.skipUnless(sys.platform == "win32", "the Recycle Bin is Windows only")
    def test_send_to_the_recycle_bin(self):
        path = os.path.join(self.tmp, "recycle me")
        write(os.path.join(path, "a.txt"), b"a")
        ao.send_to_trash(path)
        self.assertFalse(os.path.exists(path))

    def test_old_backups_are_removed(self):
        folder = os.path.dirname(self.txt)
        for n in range(12):
            old = os.path.join(folder, "workshop.txt.2025010%d-%06d.bak" % (n % 9 + 1, n))
            write(old, b"old")
            os.utime(old, (1000000 + n, 1000000 + n))
        write(os.path.join(folder, "workshop.txt.mine.bak"), b"not ours")
        app = self.open()
        app.tree.selection_set("555")
        app.move_selected("top")
        app.save_order()
        backups = [n for n in os.listdir(folder) if n.endswith(".bak")]
        self.assertEqual(len(backups), ao.KEEP_BACKUPS + 1)  # the newest 10 of ours, and the user's own
        self.assertIn("workshop.txt.mine.bak", backups)

    def fake_7zip(self):
        """A stand-in for 7-Zip that unpacks zips: enough to test what happens around it."""
        script = os.path.join(self.tmp, "fake7z.py")
        write(script, "import sys, zipfile\n"
                      "args = sys.argv[1:]\n"
                      "out = [a[2:] for a in args if a.startswith('-o')][0]\n"
                      "zipfile.ZipFile(args[-1]).extractall(out)\n")
        self.patch(ao, "find_7zip", lambda: [sys.executable, script])

    def test_install_a_7z(self):
        self.fake_7zip()
        self.open()
        archive = self.zip_file("pack.7z", {"Pack/materials/a.vtf": b"a"})
        self.install(archive)
        self.assertTrue(os.path.isfile(os.path.join(self.custom_dir(), "Pack", "materials", "a.vtf")))

    def test_7z_without_7zip(self):
        self.patch(ao, "find_7zip", lambda: None)
        self.open()
        archive = self.zip_file("pack.rar", {"Pack/materials/a.vtf": b"a"})
        self.install(archive)
        self.assertIn("install 7-Zip", [d[2] for d in self.dialogs if d[1] == "Install addon"][0])


if __name__ == "__main__":
    unittest.main()
