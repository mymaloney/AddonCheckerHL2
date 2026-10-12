# HL2 Addon Checker

Finds Half-Life 2 addons that break each other. It scans your Steam Workshop
addons (`steamapps/workshop/content/220`) and your `custom/` folders, and reports:

| Section | Meaning |
|---|---|
| **Addons that do nothing** | Every file is overwritten by an addon that loads ahead of it, or is an exact copy of a file another addon provides. Safe to remove; they're checked together, so you can remove all of them at once. |
| **Addons with copies of base game files** | Files identical to the game's own. They change nothing, unless they win over another addon's version and so put the original back. Conflicts mark such copies `[same as base game]`. |
| **Addons that mostly do nothing** | At least half the files are overwritten or duplicated. They still change something. |
| **Addon pairs with the most conflicts** | Which addons fight the most, and which one wins. |
| **Conflicts** | Two addons ship the same file with **different** contents. The game only uses one, so the other addon is partly broken. |
| `[!]` conflicts | The file is in `scripts/`, `resource/`, `cfg/` or `maps/`: weapon scripts, sound scripts, UI and maps. These are the most likely to break gameplay. |
| `[MODEL]` conflicts | A model whose `.mdl`/`.vvd`/`.vtx`/`.phy` parts may come from different addons. Mixed parts give invisible or distorted models, or crashes. |
| **Same-path duplicates** | Same file, identical contents, in several addons. Harmless. |
| **Content duplicates** | Identical file contents under different names in different addons. Harmless, just wasted space. |

## Running it

You need [Python 3.7+](https://www.python.org/downloads/). No extra packages.

```sh
python addon_checker.py
```

That finds Steam and every Steam library folder by itself, then scans:

- `steamapps/workshop/content/220/`
- every `custom/` folder in Half-Life 2 (`hl2/custom`, `episodic/custom`, `ep2/custom`, …)

On Windows you can also double-click `addon_checker.py`; the window stays open until you press Enter.

A summary is printed, and the full report is saved to `addon_report.txt`.
Workshop items are shown by their title, looked up from Steam's public API.
Use `--offline` to skip that and show item IDs instead.

| Option | |
|---|---|
| `python addon_checker.py FOLDER ...` | Scan these folders instead. Each one should *contain* addons, e.g. `...\workshop\content\220` or `...\hl2\custom`. |
| `--out FILE` | Report file (default `addon_report.txt`) |
| `--limit N` | Entries per section on screen, 0 = all (default 25). The file always has everything. |
| `--min-dupe-size N` | Smallest file in bytes checked for content duplicates (default 1024) |
| `--offline` | Don't look up Workshop titles |
| `--load-order FILE` | The game's `workshop.txt`, if it isn't found automatically |
| `--last-wins` | The addon lower in `workshop.txt` wins conflicts |
| `--find TEXT` | Only list files matching `TEXT` (e.g. `d1_town_02`, `maps/d1_town_*`) and what each addon does with them |
| `--all` | Also check disabled addons and ones missing from `workshop.txt` |

The exit code is 2 when conflicts were found and 0 when none were.

## Organizer window

```sh
python addon_organizer.py
```

A Mod Organizer-style window for your Workshop load order. Needs `addon_checker.py` in the same
folder; tkinter comes with the python.org installer on Windows. Dark and light themes (View menu).

**The load order list**

- Click the checkbox (or press Space, or double-click) to turn addons on or off.
- Drag rows to reorder, or use the arrow buttons / Ctrl+Up, Ctrl+Down, Ctrl+Home, Ctrl+End.
  Several rows can be selected and moved together.
- **Flags:** ▲ overwrites other addons, ▼ is overwritten, ✕ does nothing (every file is
  overwritten, identical elsewhere, or identical to the original game).
- **Select an addon** and the others are tinted: green for addons it overwrites, red for addons
  that overwrite it.
- Columns for type (Map, Sound, Model, Texture…), files, size, wins, loses, status and notes;
  turn optional ones on or off in View. Click a heading to sort, click **#** to go back to load order.
- **Filter** by name, id or note, and pick a view: has conflicts, overwrites others, is overwritten,
  does nothing, mostly does nothing, disabled, rule broken, not in load order, has notes.
- **Right-click** for on/off, moving, the Workshop page, the addon's folder, and copying its id.

**The details side**

- With nothing selected: an overview of your setup.
- **Conflicts tab:** every addon the selected one clashes with, how many files it wins, loses or
  shares identically, and the original game's files it leaves unchanged. Expand a row for the files;
  double-click to jump to that addon. Set rules here: *This addon should win* / *The other should win*.
- **Files tab:** every file in the addon and whether the game uses it, or which addon overwrites it.
- **Find file tab:** type part of a path (a map name like `d1_town_02`, or `maps/d1_town_*`) to see
  every addon with a matching file and which copy the game uses. A map name also finds its
  companion files (`maps/graphs/*.ain` node graphs, `_l_0.lmp` lighting patches, map materials),
  a common cause of map bugs when they come from a different addon than the `.bsp`.
- **Rules tab:** all rules, and which are broken. **Sort by rules** fixes broken ones by moving each
  winner just ahead of its loser, leaving everything else where it was.
- **Notes:** a note per addon, shown in the Notes column and searchable with the filter.

**Toolbar and menus**

- **Save load order** (Ctrl+S) writes `workshop.txt` after backing up the old one as
  `workshop.txt.<date>-<time>.bak`. **Close the game first**, since it may rewrite the file.
- **Undo / Redo** (Ctrl+Z / Ctrl+Y) every load order change; File > Revert to saved.
- **Group idle addons** moves every addon that does nothing to the losing end of the list. The game
  plays the same, and they end up together in the game's Add-ons screen for unsubscribing.
- **Profiles:** save the current order and on/off states under a name, and switch between them.
- **Problems** lists anything that needs attention (broken rules, unreadable addons, addons not in
  `workshop.txt` yet, leftover folders) with a button to fix each; leftover folders can be deleted
  there after a confirmation.
- **Folders...** shows which Half-Life 2 and Workshop folders are used, with *Browse...* buttons.
  A Steam library folder (e.g. `D:\SteamLibrary`) is fine too. It opens by itself if Steam can't be found.
- File > **Export report...** writes the same report as `addon_checker.py` for the current order.
- View > **Lower in the list wins** flips the assumed load order direction.

Rules, notes, profiles, theme and window layout are kept in `addon_rules.json` next to the tool.

## What it reads

- **Workshop items:** each item folder is one addon. The VPKs inside are read whether they're plain or compressed. Supported compression: LZMA (standard and Valve's `LZMA` header), xz, gzip, bzip2, zlib, and zip files containing VPKs. Loose files in an item folder are read too.
- **`custom/` addons:** sub-folders (loose files, plus a `pak01_dir.vpk` inside), single `foo.vpk` files, and multi-part `foo_dir.vpk` + `foo_000.vpk`…
- VPK contents are never extracted. The tool compares the CRC32 values each VPK stores for its files, so even gigabytes of addons scan quickly.
- Files at an addon's root (`addoninfo.txt`, preview images…) are ignored, since they aren't game content.

If a Workshop item uses a format the tool doesn't recognize, the report lists it under
**Problems reading addons**, with the file's first bytes, so support can be added.

## Base game files

Addons are also compared with Half-Life 2's own files. The tool reads the VPKs listed in
`hl2_complete/gameinfo.txt` (or `hl2/gameinfo.txt`), in the game's order, so where Episode Two
replaces a Half-Life 2 file, the Episode Two version counts as the original.

- A file identical to the original counts as doing nothing, unless it beats another addon's
  version of that file. In that case it puts the original back, and the losing addon is reported
  as "undone back to the original by" it.
- Only VPKs are read. Loose files in the game folders aren't compared.
- `--game-dir FOLDER` points at the Half-Life 2 folder if it isn't found automatically;
  `--no-base-game` skips the comparison.

## Load order

The game keeps your Workshop addons' load order in
`steamapps/common/Half-Life 2/hl2_complete/cfg/workshop.txt`: one line per addon, in load order,
`1` = enabled and `0` = disabled. The tool reads it automatically, or use `--load-order FILE`.

- Conflicts show which copy is `<- used`, and each addon's position in the list as `[#N]`.
- Disabled addons, and installed addons that aren't in the list yet, are skipped (listed at the end).
  The game only updates the list when it starts, so launch it once after subscribing to something.
  `--all` checks them anyway, as if they were enabled.
- **Which end of the list wins is assumed: the addon higher in the list (smaller `#N`).**
  If the game turns out to work the other way, use `--last-wins`.
  To check: find a conflict in the report where you can tell the addons apart in game
  (a map or a weapon model), see which one you get, and compare their `#N`.
- `custom/` addons aren't in the list, so conflicts involving them show no winner.

## Source SDK version (optional)

`src/addonchecker/` has the same checker as C++ for a Source SDK 2013 **mod** (a `client.dll`).
It runs inside that mod with an `addon_check` console command. It only scans `custom/` folders,
not the Workshop. Most people want the Python tool above.

### Console commands

| Command | What it does |
|---|---|
| `addon_check` | Scan every mounted `custom/` folder: the mod's own, plus `hl2/custom` etc. if the mod mounts them |
| `addon_check "C:\some\custom"` | Scan specific folders instead |
| `addon_check_console_limit 25` | Entries per section printed to console (0 = all) |
| `addon_check_dupe_minsize 1024` | Smallest file (bytes) checked for content duplicates |
| launch option `-addoncheck` | Run the scan once when the game starts |

The console shows a summary. The full report is written to `<your mod>/addon_report.txt`.

### Adding it to your mod

You need a Source SDK 2013 mod. You can build one from [ValveSoftware/source-sdk-2013](https://github.com/ValveSoftware/source-sdk-2013), following its README.

1. Copy `src/addonchecker/` into the SDK's `src/game/client/` folder, so you have `src/game/client/addonchecker/`.

2. Open your mod's client project script, e.g. `src/game/client/client_hl2.vpc` (or `client_episodic.vpc`, or whichever your mod builds). Add this inside the `$Project` block, next to the other `$Folder` entries:

   ```
   $Folder "Source Files"
   {
       $Folder "Addon Checker"
       {
           $File "addonchecker/addon_checker_command.cpp"
           $File "addonchecker/addon_scanner.cpp"
           {
               $Configuration
               {
                   $Compiler
                   {
                       $Create/UsePrecompiledHeader "Not Using Precompiled Headers"
                   }
               }
           }
           $File "addonchecker/addon_scanner.h"
       }
   }
   ```

   `addon_scanner.cpp` is plain C++ and doesn't include `cbase.h`, so it must not use the precompiled header.

3. Regenerate the project files (`createallprojects` / `creategameprojects`) and build the client DLL.

4. Copy the built `client.dll` into your mod's `bin/`, start the mod, and run `addon_check` in the console.

### Testing without the game

The scanner also builds as a command-line tool, which is useful for checking a real `custom/` folder quickly:

```sh
g++ -O2 -I src/addonchecker tools/addon_checker_cli.cpp src/addonchecker/addon_scanner.cpp -o addon_checker
./addon_checker "<Steam>/steamapps/common/Half-Life 2/hl2/custom"
```

On Windows, MSVC works too: `cl /EHsc /O2 /I src\addonchecker tools\addon_checker_cli.cpp src\addonchecker\addon_scanner.cpp`.

The tool doesn't know the engine's load order, so it lists conflicts without `used`/`ignored`. It exits with code 2 when conflicts are found.

### Files

```
src/addonchecker/
  addon_scanner.h / .cpp        scanner + report text (no SDK dependency)
  addon_checker_command.cpp     console commands, convars, -addoncheck (client DLL)
tools/
  addon_checker_cli.cpp         standalone command-line build of the scanner
```
