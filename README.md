# HL2 Addon Checker

Finds Half-Life 2 addons that break each other. It scans your Steam Workshop
addons (`steamapps/workshop/content/220`) and your `custom/` folders, and reports:

| Section | Meaning |
|---|---|
| **Addons that do nothing** | Every file is overwritten by an addon that loads ahead of it, or is an exact copy of a file another addon provides. Safe to remove; they're checked together, so you can remove all of them at once. |
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
| `--all` | Also check disabled addons and ones missing from `workshop.txt` |

The exit code is 2 when conflicts were found and 0 when none were.

## What it reads

- **Workshop items:** each item folder is one addon. The VPKs inside are read whether they're plain or compressed. Supported compression: LZMA (standard and Valve's `LZMA` header), xz, gzip, bzip2, zlib, and zip files containing VPKs. Loose files in an item folder are read too.
- **`custom/` addons:** sub-folders (loose files, plus a `pak01_dir.vpk` inside), single `foo.vpk` files, and multi-part `foo_dir.vpk` + `foo_000.vpk`…
- VPK contents are never extracted. The tool compares the CRC32 values each VPK stores for its files, so even gigabytes of addons scan quickly.
- Files at an addon's root (`addoninfo.txt`, preview images…) are ignored, since they aren't game content.

If a Workshop item uses a format the tool doesn't recognize, the report lists it under
**Problems reading addons**, with the file's first bytes, so support can be added.

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
