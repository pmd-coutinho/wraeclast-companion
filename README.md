# Wraeclast Companion (Path of Exile 1)

A native Linux/Wayland Path of Exile 1 build desk, passive skill tree viewer, and in-game campaign leveling HUD built with GTK 4, Cairo, and `gtk4-layer-shell`.

- **Zero game injection or memory reading** — uses only Wayland compositor focus metadata, normal pointer input, and optional `Client.txt` log tailing.
- **Shows only while PoE 1 is focused** — automatically follows PoE to its active monitor and hides when you switch windows.
- **Self-contained HUD controls** — keeps the overlay click-through while letting you tap compact `‹` / `›` / `▾` mini-buttons directly on the HUD without stealing keyboard focus from PoE.

---

## Features

### Build Desk & Path of Building Import
- Modern integrated GTK 4 headerbar with segmented pill navigation (`Ctrl+1–5`), global **+ Import** dialog with 1-click **Paste from Clipboard** (`Ctrl+I`), and a live **HUD** preview pill.
- Import public `pobb.in` share links (HTTPS `/ID/raw` and `/u/user/ID/raw`), PoB Community share codes, or raw XML exports (offline).
- Hero build dashboard with stat pills, full-height **Build notes** with inline search and native PoB `^0`–`^9` / `^xRRGGBB` color rendering.
- **Visual Loadouts:** PoE-styled rarity item cards (Unique/Rare/Magic/Normal), visual **Gem Link Cards** with color-coded socket dots and link bars, linked loadout switching, and instant gear/gem filtering.
- **Vendor & Stash Regex Helper (`Regex` tab):** one-click copyable PoE search regex presets (3-links, movement speed boots, caster wands, life + resists, safe maps) plus custom saved presets in `~/.config/poe-companion/regexes.json`.
- Keeps saved builds and campaign progress locally in `~/.local/share/poe-companion/state.json`.

### Interactive Passive Skill Tree
- Exact version-pinned GGG passive tree data (`3_29` / `3.29.1` and `3_30` / `3.30.0`) with official artwork atlases cached under `~/.cache/wraeclast-companion/passive-tree/`.
- **Passive Tree Diff Mode:** compare any two tree variants (e.g., *Act 3* → *Act 6* → *Maps*) to highlight newly added nodes in emerald green (`+N`) and respecced nodes in crimson (`−M`) on the canvas and tooltips.
- Smooth `< 1 ms` interactive pan & cursor-anchored scroll zoom with HiDPI rendering, node/stat search, hover tooltips, **Fit build** / **Fit all**, and keyboard shortcuts (`+` / `-` / `0` / `Esc`).
- Hides unselected sibling ascendancies by default (with an **All asc.** toggle) to eliminate GGG's overlapping ascendancy portraits.
- *Read-only:* cluster-jewel, tattoo, and override transformations are not calculated.

### In-Game Leveling HUD & Run Timer (Acts 1–10)
- **Interactive Act Checklist & Live Run Timer:** clickable step timeline on the Campaign tab with Start/Pause/Reset timer, automatic **Act 1–10 splits**, and an optional timer pill on the in-game HUD.
- **Compact by default:** slim bar with act/step badge, progress bar, step title, and one-line cue; expands on demand for full route details, beginner tips, and build/gem reminders.
- **Automatic `Client.txt` tracking:** discovers Steam/Proton PoE logs (including secondary Steam library drives via `libraryfolders.vdf`), tails `You have entered ...` lines incrementally, and advances matching campaign steps and act splits automatically.
- **Live HUD settings & Preview:** configure enable/disable, compact bar, clickable mini-buttons, HUD timer, screen position (`Top center`, `Top right`, `Top left`, `Bottom center`, `Bottom left`), opacity, `Client.txt` auto-tracking, and **Preview HUD** (persisted in `~/.config/poe-companion/hud.json`).
- **Optional CLI hotkeys** (instant local socket dispatch): `--next-step`, `--prev-step`, `--toggle-compact`, `--toggle-hud`, and `--toggle-clickable`.

---

## Quick Install

### Nobara / Fedora Linux (Native — No Nix Required)

```sh
git clone https://github.com/pmd-coutinho/wraeclast-companion.git
cd wraeclast-companion
./install-nobara.sh
```

What the installer does:
1. Installs native RPM dependencies (`gtk4`, `gtk4-layer-shell`, `python3-gobject`, `python3-cairo`, `python3-pillow`, `gobject-introspection`).
2. Installs the app to `~/.local/share/wraeclast-companion`, symlinks `~/.local/bin/poe-companion`, and adds the desktop menu launcher.
3. Installs and enables the KDE Plasma 6 KWin focus bridge (`kwin/poe-companion`).

> **First run on KDE Plasma 6:** Log out and back in once (or click Apply in **System Settings → Window Management → KWin Scripts**) so KWin activates the focus bridge.

To uninstall:
```sh
./install-nobara.sh --uninstall        # keeps saved builds & config
./install-nobara.sh --uninstall --purge  # removes everything
```

### Nix / NixOS (Flakes)

```sh
nix develop
python3 -m poe_companion.app
```

Or run `./bin/poe-companion` directly (auto-detects native GTK 4 + Pillow and falls back to `nix develop`). To install a desktop entry from a checkout, run `./bin/install-desktop-entry`.

---

## Compositor Setup & Window Matching

The HUD stays hidden until the focused window matches Path of Exile 1 (`steam_app_238960`, `PathOfExileSteam.exe`, `PathOfExile_x64Steam.exe`, etc.) and excludes Steam client/library windows and PoE 2.

If your Proton/launcher build reports a different window identity, copy `config/poe-companion.json` to `~/.config/poe-companion/config.json` and set `game_window_regex` (or `game_app_id_regex` / `game_title_regex`).

### KDE Plasma 6 Wayland
- Installed automatically by `./install-nobara.sh`.
- Manual install: `kpackagetool6 --type=KWin/Script -i kwin/poe-companion`, then enable **PoE Companion focus bridge** in **System Settings → Window Management → KWin Scripts**.
- Optional keyboard shortcuts can be added in **System Settings → Keyboard → Shortcuts → Add New → Command** pointing to `~/.local/bin/poe-companion --next-step` (etc.).

### niri
- Focus tracking works automatically via `niri msg --json event-stream`.
- Optional keybinds are provided in [`config/niri.kdl`](config/niri.kdl).

---

## Development & Tests

```sh
nix develop --command python3 -m unittest discover -s tests -v
nix develop --command python3 -m compileall -q poe_companion tests
```

The test suite covers PoB decoding and `pobb.in` bounds, pinned passive tree geometry and canvas helpers, all 10 acts of campaign data, `Client.txt` zone matching and Steam library discovery, HUD preferences, state persistence, and focus matching policy.
