# Addon Checker for Half-Life 2

A Source SDK 2013 mod feature that scans your `custom/` addons and reports:

| Section | Meaning |
|---|---|
| **Conflicts** | Two addons ship the same file with **different** contents. The game only uses one, so the other addon is partly broken. When the engine's load order is known, the report marks which copy is `used` and which is `ignored`. |
| `[!]` conflicts | The file is in `scripts/`, `resource/`, `cfg/` or `maps/`: weapon scripts, sound scripts, UI and maps. These are the most likely to break gameplay. |
| `[MODEL]` conflicts | A model whose `.mdl`/`.vvd`/`.vtx`/`.phy` parts may come from different addons. Mixed parts give invisible or distorted models, or crashes. |
| **Same-path duplicates** | Same file, identical contents, in several addons. Harmless; you can delete the extras. |
| **Content duplicates** | Identical file contents under different names in different addons. Harmless, just wasted space. |

It reads both kinds of addon in `custom/`:

- **Folders**, including a `pak01_dir.vpk` inside a folder
- **VPKs** (v1 and v2): single-file `foo.vpk`, or `foo_dir.vpk` with its `foo_000.vpk`… parts

VPK contents aren't extracted. The checker compares the CRC32 values stored in each VPK's directory, so a scan takes seconds even with gigabytes of addons.

## Using it in game

| Command | What it does |
|---|---|
| `addon_check` | Scan every mounted `custom/` folder: the mod's own, plus `hl2/custom` etc. if the mod mounts them |
| `addon_check "C:\some\custom"` | Scan specific folders instead |
| `addon_check_console_limit 25` | Entries per section printed to console (0 = all) |
| `addon_check_dupe_minsize 1024` | Smallest file (bytes) checked for content duplicates |
| launch option `-addoncheck` | Run the scan once when the game starts |

The console shows a summary. The full report is written to `<your mod>/addon_report.txt`.

## Adding it to your mod

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

## Testing without the game

The scanner also builds as a command-line tool, which is useful for checking a real `custom/` folder quickly:

```sh
g++ -O2 -I src/addonchecker tools/addon_checker_cli.cpp src/addonchecker/addon_scanner.cpp -o addon_checker
./addon_checker "<Steam>/steamapps/common/Half-Life 2/hl2/custom"
```

On Windows, MSVC works too: `cl /EHsc /O2 /I src\addonchecker tools\addon_checker_cli.cpp src\addonchecker\addon_scanner.cpp`.

The tool doesn't know the engine's load order, so it lists conflicts without `used`/`ignored`. It exits with code 2 when conflicts are found.

## Files

```
src/addonchecker/
  addon_scanner.h / .cpp        scanner + report text (no SDK dependency)
  addon_checker_command.cpp     console commands, convars, -addoncheck (client DLL)
tools/
  addon_checker_cli.cpp         standalone command-line build of the scanner
```
