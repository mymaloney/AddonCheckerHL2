# Addon Checker for Half-Life 2

A Source SDK 2013 mod feature that scans your `custom/` addons and reports:

| Section | Meaning |
|---|---|
| **Conflicts** | Two addons ship the same file with **different** contents. The game only uses one, so the other addon is partly broken. When the engine's load order is known, the report marks which copy is `used` and which is `ignored`. |
| `[!]` conflicts | The file is in `scripts/`, `resource/`, `cfg/` or `maps/`: weapon scripts, sound scripts, UI and maps. These are the most likely to break gameplay. |
| `[MODEL]` conflicts | The file is part of a model (`models/`). |
| **Mixed models** | The game takes each part of a model (`.mdl`, `.vvd`, `.dx90.vtx`, `.phy`, `.ani`) from the first addon that has it, so a model can end up built from different addons' parts. Mismatched parts give invisible or distorted models, or crashes. This catches it even when no single file conflicts, e.g. one addon adds a `.phy` to another addon's model. With a known load order it shows which addon each part comes from; without one, it lists the models that would mix in some load order. |
| **Same-path duplicates** | Same file, identical contents, in several addons. Harmless; you can delete the extras. |
| **Content duplicates** | Identical file contents under different names in different addons. Harmless, just wasted space. |

It reads both kinds of addon in `custom/`:

- **Folders**, including a `pak01_dir.vpk` inside a folder
- **VPKs** (v1 and v2): single-file `foo.vpk`, or `foo_dir.vpk` with its `foo_000.vpk`… parts

VPK contents aren't extracted. The checker compares the CRC32 values stored in each VPK's directory, so VPK addons scan in seconds even when they hold gigabytes. Loose folder addons have to be read in full to checksum them, so a big one takes longer. In game the scan runs in the background, and the report prints when it's done.

## Using it in game

| Command | What it does |
|---|---|
| `addon_check` | Scan every mounted `custom/` folder in the background: the mod's own, plus `hl2/custom` etc. if the mod mounts them |
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

The tool doesn't know the engine's load order, so it lists conflicts without `used`/`ignored`, and lists models that *could* mix. To give it the order, run `path_info` in the game console and pass the `GAME` search paths, highest priority first, separated by `;`:

```sh
./addon_checker --order "C:/.../hl2/custom/addon_b;C:/.../hl2/custom/addon_a.vpk" "C:/.../hl2/custom"
```

It exits with code 2 when it finds conflicts or mixed models, 0 when it finds none.

## Tests

```sh
g++ -O2 -I src/addonchecker tools/addon_checker_cli.cpp src/addonchecker/addon_scanner.cpp -o addon_checker
python3 tests/run_tests.py ./addon_checker
```

The tests build fake `custom/` folders (loose addons and VPKs, including broken ones) and check the report. CI runs them with GCC and Clang on Linux and MSVC (32 and 64-bit) on Windows. CI also compiles the game-side code against the Source SDK 2013 headers; to run that locally, use `tests/check_sdk_build.sh <path to source-sdk-2013>`.

## Files

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
