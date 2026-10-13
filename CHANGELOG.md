# Changelog

> ⚠️ This project was **vibe-coded**: written largely by an AI model, directed and tested by a person.
> Read the warnings at the top of the [README](README.md) before using it.

## Unreleased

### Scripts and settings (`addon_scripts.py`, new)
- Copies the scripts and settings (text files) out of every loaded addon, shows what each one
  changes compared with the original game, setting by setting, and combines them all into one folder
  (`combined/`), the later-loading addon winning where two set the same thing. For mod makers.
- Organizer: Tools > Extract scripts and settings....
- Map entity patches (`.lmp`) are compared with the original map's entities and combined entity by
  entity.
- New files are listed with what's in them; the same change in many places is listed once.
- Random sound lists (`rndwave`) and entity outputs keep every addon's entries when combined.
- `exec "a.cfg"` and `exec a.cfg` count as the same command.
- Lists scripts that use sounds, models or materials the original game doesn't have;
  `--original-files-only` leaves those addons out of `combined/`.
- The report lists the original game's VPKs and loose folders it read, and VPKs `gameinfo.txt`
  names that aren't there; loose game files count as the original game's.

## 1.0.0 (2026-10-12)

First public release. Public domain ([The Unlicense](LICENSE)).

### Checker (`addon_checker.py`)
- Scans Half-Life 2's Workshop addons and the `custom/` folders the game loads (from `gameinfo.txt`),
  in the Steam library Half-Life 2 is installed in. Reads VPKs (plain, LZMA, xz, gzip, bzip2, zlib,
  zips of VPKs) and loose files without extracting anything.
- Load order: `custom/` addons first (A to Z), then `workshop.txt` from top to bottom; the addon that
  loads later wins.
- Reports conflicts, models built from different addons' parts, addons that do nothing (and which only
  do nothing together with others), duplicates, and copies of the original game's files.
- `--find` shows which addon's copy of a file the game uses.
- One unreadable addon never stops a scan; problems are listed instead.
- Workshop titles are remembered in `addon_titles.json`, so they also show offline.
- `--version`.

### Organizer (`addon_organizer.py`)
- Edits and saves the Workshop load order (`workshop.txt`), with rules, notes, profiles, undo/redo,
  and a check for changes the game made to `workshop.txt` meanwhile. The game's version is backed up
  before it's replaced; the newest 10 backups are kept.
- `custom/` addons: shown first in load order (C1, C2…); install by dropping files or folders on the
  window (Windows) or with Install addon… (folders, `.vpk`, `.zip`, and `.7z`/`.rar` with 7-Zip
  installed); turn off/on (moved to and from `custom_disabled/`); rename to change their place; remove
  to the Recycle Bin; Edit > Undo addon file change.
- Leftover folders of removed Workshop addons can be deleted.

### Builds
- Windows `.exe` builds of both tools (PyInstaller), attached to the GitHub release.

### Source SDK version (`src/addonchecker/`)
- Optional C++ version for a Source SDK 2013 mod's client DLL; scans `custom/` folders only.
