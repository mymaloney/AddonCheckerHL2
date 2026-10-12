# HL2 Addon Checker

> ### ⚠️ This project was vibe-coded
>
> **This tool was written almost entirely by an AI model** ("vibe-coded"), directed and tested by a
> person on a real Half-Life 2 install. Nobody has reviewed every line of it the way a careful human
> author would. Treat it accordingly:
>
> - **Back up first.** Copy your `custom/` folders and `hl2_complete/cfg/workshop.txt` somewhere safe
>   before using the organizer. It moves, renames, installs and removes addon files, and rewrites
>   `workshop.txt`.
> - **Don't trust its advice blindly.** "Does nothing, safe to remove" and "which addon wins" come from
>   the tool's own reading of your files and of how the game loads them. Check in game before you
>   unsubscribe from or delete anything you care about.
> - **There is no warranty** of any kind (see [LICENSE](LICENSE)). If it breaks your setup, you get to
>   keep both pieces.
> - Bug reports are welcome, but fixes will probably be vibe-coded too.

Finds Half-Life 2 addons that break each other. It scans your Steam Workshop
addons (`steamapps/workshop/content/220`) and your `custom/` folders, and reports:

| Section | Meaning |
|---|---|
| **Addons that do nothing** | Every file is overwritten by an addon that loads after it, or is an exact copy of a file another addon provides. Safe to remove all at once. Some are only safe together with others (marked *only together with*): removing one of those alone changes the game. Addons that couldn't be fully read are never listed here. |
| **Addons with copies of base game files** | Files identical to the game's own. They change nothing, unless they win over another addon's version and so put the original back. Conflicts mark such copies `[same as base game]`. |
| **Addons that mostly do nothing** | At least half the files are overwritten or duplicated. They still change something. |
| **Addon pairs with the most conflicts** | Which addons fight the most, and which one wins. |
| **Conflicts** | Two addons ship the same file with **different** contents. The game only uses one, so the other addon is partly broken. |
| `[!]` conflicts | The file is in `scripts/`, `resource/`, `cfg/` or `maps/`: weapon scripts, sound scripts, UI and maps. These are the most likely to break gameplay. |
| `[MODEL]` conflicts | The file is part of a model (`models/`). |
| **Models built from different addons** | The game takes each part of a model (`.mdl`, `.vvd`, `.dx90.vtx`, `.phy`, `.ani`) from the last addon to load that has it, so a model can end up made of different addons' parts, even when no single file conflicts (e.g. one addon adds a `.phy` to another addon's model). Mismatched parts give invisible or distorted models, or crashes. Parts from the original game aren't counted, since a reskin can legitimately ship only a hex-edited `.mdl`. |
| **Same-path duplicates** | Same file, identical contents, in several addons. Harmless. |
| **Content duplicates** | Identical file contents under different names in different addons. Harmless, just wasted space. |

## Download

Get the zip from the [Releases page](../../releases): it has `addon_organizer.exe` (the window) and
`addon_checker.exe` (the report) for Windows, plus the `.py` files. Unpack it anywhere and run one.
Windows SmartScreen may warn that the program is unrecognized: it isn't signed. If you'd rather not
run an unsigned `.exe` (reasonable, given the warning above), run the `.py` files with Python instead.

> ⚠️ **Vibe-coded software, no warranty.** Back up your addons and `workshop.txt` first.

Public domain: see [LICENSE](LICENSE) (The Unlicense). What changed in each version is in
[CHANGELOG.md](CHANGELOG.md).

## Running it

You need [Python 3.7+](https://www.python.org/downloads/) (or the `.exe` from the release). No extra packages.

```sh
python addon_checker.py
```

That finds your Half-Life 2 install through Steam by itself, then scans what the game loads:

- the Workshop folder in the same Steam library (`steamapps/workshop/content/220/`)
- the `custom/` folders listed in the game's `gameinfo.txt` (or `hl2_complete`, `ep2`, `episodic`
  and `hl2/custom` if it can't be read). Other games in the Half-Life 2 folder, like Lost Coast,
  have their own `custom/` folders, which Half-Life 2 doesn't load.

If an old install left folders in another Steam library, they're ignored: the game doesn't load them.
The folders used are printed at the start; use `--game-dir` or pass folders to pick others.

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
| `--find TEXT` | Only list files matching `TEXT` (e.g. `d1_town_02`, `maps/d1_town_*`) and what each addon does with them |
| `--all` | Also check disabled addons and ones missing from `workshop.txt` |
| `--game-dir FOLDER` | The Half-Life 2 folder to use, if the right one isn't found automatically |

The exit code is 2 when conflicts or mixed models were found, 0 when none were, and 1 when
something went wrong (a folder or `--load-order` file that can't be opened, a report that can't be saved).

## Organizer window

> ⚠️ **The organizer changes files on disk.** It was vibe-coded (written by an AI). Saving rewrites
> `workshop.txt`, and the `custom/` features below move, rename, install and remove addon folders
> right away. Keep your own backup.

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
  winner just after its loser (the later one wins), leaving everything else where it was.
- **Notes:** a note per addon, shown in the Notes column and searchable with the filter.

**`custom/` addons**

- They're shown first, as **C1, C2…**, in the order the game loads them: ahead of the Workshop addons,
  A to Z by folder name. Every Workshop addon loads later, so it wins over them; among themselves, Z
  beats A. **Rename...** (right-click) changes its name, and so its place.
- **The checkbox** turns one off by moving it to `custom_disabled/`, next to its `custom/` folder (the
  game loads everything inside `custom/`, so it can't stay there), and back on again. This happens right
  away, without Save. A name that's already taken on the other side is never overwritten.
- **Install addon...** (toolbar and File menu) copies an addon into the game's `custom/` folder (the first
  one `gameinfo.txt` lists, usually `hl2_complete/custom`). On Windows you can also **drop** files and
  folders from Explorer onto the window. It takes:
  - a folder (an extra wrapper folder, like `HD Crowbar/HD Crowbar/materials`, is skipped),
  - a `.vpk` (a `foo_dir.vpk` comes with its `foo_000.vpk`… parts),
  - a `.zip`, which is unpacked; one with only VPKs puts them straight into `custom/`.
  - a `.7z` or `.rar`, if [7-Zip](https://7-zip.org) is installed (it does the unpacking).

  An addon that's already there is only replaced after you say so, and the organizer rescans afterwards.
- **Remove (to the Recycle Bin)...** (right-click) moves a `custom/` addon to the Recycle Bin (or the
  Trash on Linux), where you can restore it from. It never deletes for good: if that isn't possible,
  nothing happens.
- **Edit > Undo addon file change** puts back the last turn on/off or rename (they happen on disk right
  away, so the load order's own Undo doesn't cover them).

> ⚠️ Vibe-coded: these move real folders around. Close the game first, and keep a backup of anything
> you can't download again.

**Toolbar and menus**

- **Save load order** (Ctrl+S) writes `workshop.txt` after backing up the old one as
  `workshop.txt.<date>-<time>.bak` (the newest 10 backups are kept). **Close the game first**, since it
  may rewrite the file. If the game changed `workshop.txt` since it was read, you're asked first.
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

Rules, notes, profiles, theme and window layout are kept in `addon_rules.json` next to the tool, and
Workshop titles in `addon_titles.json` (so they show offline too).

## What it reads

- **Workshop items:** each item folder (named by its id) is one addon. Anything else in the Workshop folder, like an Explorer copy `1234 - Copy` or a VPK put there by hand, is listed as *not Workshop items*: the game never loads it. The VPKs inside an item are read whether they're plain or compressed. Supported compression: LZMA (standard and Valve's `LZMA` header), xz, gzip, bzip2, zlib, and zip files containing VPKs. Loose files in an item folder are read too.
- **`custom/` addons:** sub-folders (loose files, plus a `pak01_dir.vpk` inside), single `foo.vpk` files, and multi-part `foo_dir.vpk` + `foo_000.vpk`…
- VPK contents are never extracted. The tool compares the CRC32 values each VPK stores for its files, so even gigabytes of addons scan quickly.
- Files at an addon's root (`addoninfo.txt`, preview images…) are ignored, since they aren't game content.

If a Workshop item uses a format the tool doesn't recognize, the report lists it under
**Problems reading addons**, with the file's first bytes, so support can be added. Addons with
problems are never reported as doing nothing, since the part that couldn't be read may matter.

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
- `--all` checks them anyway, as if they were enabled; only the addons the game really loads decide
  which addons do nothing, though.
- **The addon that loads later wins:** the one lower in the list (larger `#N`).
- `custom/` addons load ahead of every Workshop addon, A to Z by folder name (custom folder by custom
  folder, in `gameinfo.txt`'s order). So any Workshop addon beats a `custom/` addon, and among `custom/`
  addons Z beats A. Addons in `custom_disabled/` (the organizer's "off") aren't loaded or checked.

## Tests

> ⚠️ Passing tests don't make vibe-coded software safe: the tests were written by the same AI, from the
> same understanding of how the game works.

```sh
python -m unittest tests.test_addon_checker -v
python -m unittest tests.test_addon_organizer -v    # opens windows; on Linux without a desktop: xvfb-run -a ...
```

The tests build fake addons (loose folders, plain and compressed VPKs, broken ones) and fake Steam
libraries, and check what the checker makes of them; the organizer tests drive the real window on
them. CI runs both on Linux and Windows with Python 3.7 and the newest Python.

## Source SDK version (optional)

`src/addonchecker/` has the same checker as C++ for a Source SDK 2013 **mod** (a `client.dll`).
It runs inside that mod with an `addon_check` console command. It only scans `custom/` folders,
not the Workshop. Most people want the Python tool above.

Besides conflicts and duplicates, it reports **mixed models**: models whose `.mdl`, `.vvd`,
`.dx90.vtx`, `.phy` and `.ani` parts the game would take from different addons, even when no single
file conflicts (e.g. one addon adds a `.phy` to another addon's model). The scan runs in the
background, so the game doesn't freeze while it reads big loose-file addons.

### Console commands

| Command | What it does |
|---|---|
| `addon_check` | Scan every mounted `custom/` folder in the background: the mod's own, plus `hl2/custom` etc. if the mod mounts them |
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

The tool doesn't know the engine's load order, so it lists conflicts without `used`/`ignored`, and lists models that *could* mix. To give it the order, run `path_info` in the game console and pass the `GAME` search paths, highest priority first, separated by `;`:

```sh
./addon_checker --order "C:/.../hl2/custom/addon_b;C:/.../hl2/custom/addon_a.vpk" "C:/.../hl2/custom"
```

It exits with code 2 when it finds conflicts or mixed models, 0 when it finds none.

### Tests

```sh
g++ -O2 -I src/addonchecker tools/addon_checker_cli.cpp src/addonchecker/addon_scanner.cpp -o addon_checker
python3 tests/run_tests.py ./addon_checker
```

The tests build fake `custom/` folders (loose addons and VPKs, including broken ones) and check the report. CI runs them with GCC and Clang on Linux and MSVC (32 and 64-bit) on Windows. CI also compiles the game-side code against the Source SDK 2013 headers; to run that locally, use `tests/check_sdk_build.sh <path to source-sdk-2013>`.

### Files

```
src/addonchecker/
  addon_scanner.h / .cpp        scanner + report text (no SDK dependency)
  addon_checker_command.cpp     console commands, convars, -addoncheck (client DLL)
tools/
  addon_checker_cli.cpp         standalone command-line build of the scanner
tests/
  run_tests.py                  tests for the command-line build
  check_sdk_build.sh            compiles the game-side code against SDK headers
```

## Once more: this was vibe-coded

Almost all of this repository (the Python tools, the organizer, the C++ version, the tests and this
README) was written by an AI model, with a person directing it and testing each version on a real
Half-Life 2 install. It's public domain and comes with no warranty. Back up your addons and
`workshop.txt`, and double-check anything it tells you to remove.
