# Wraeclast Companion (PoE 1)

A small native Wayland desktop companion with two windows: a regular build desk and a separate campaign HUD. The HUD is a GTK 4 layer-shell surface; focus comes from niri IPC or the included KWin 6 script. It uses compositor window metadata and normal pointer input only. It does not read game memory, inject code, or send game input.

## Install on Nobara (Fedora)

No Nix installation is required on Nobara/Fedora. Run the bundled installer from the repository root:

```sh
./install-nobara.sh
```

It installs the native RPM dependencies (`gtk4`, `gtk4-layer-shell`, `python3-gobject`, `python3-cairo`, `python3-pillow`, `gobject-introspection`), installs the app to `~/.local/share/wraeclast-companion`, symlinks `~/.local/bin/poe-companion`, adds the desktop launcher, and installs/enables the KDE Plasma 6 KWin focus bridge. To uninstall, run `./install-nobara.sh --uninstall` (add `--purge` to also delete saved builds and HUD settings).

## Build and run (Nix)

Requirements: Nix with flakes enabled, a GTK 4 Wayland session, and niri or KDE Plasma 6 Wayland. The project flake provides Python, GTK 4, GTK 4 Layer Shell, and their introspection bindings.

```sh
nix develop
python3 -m poe_companion.app
```

Or run `./bin/poe-companion` (the launcher enters the project shell). The launcher resolves symlinks, so it can also be symlinked into a personal `bin` directory and run from any working directory. It invokes `nix develop`; Nix must be installed and discoverable on `PATH` for both shell and desktop launches. To install a desktop launcher, run `./bin/install-desktop-entry`; it writes an absolute checkout path into `~/.local/share/applications/poe-companion.desktop` (or `$XDG_DATA_HOME/applications`). It opens the planner even when the game is not running. The planner can be moved to another monitor and keeps imported builds and campaign position in `~/.local/share/poe-companion/state.json` after the game closes.

Use **Import build** to paste a public `pobb.in` share link, a Path of Building Community share code, or the XML export directly into the multiline field. Link imports download the public raw build from `pobb.in` over HTTPS and need an internet connection; pasted codes and XML continue to work offline. Link requests use the documented [`pobb.in` raw API](https://github.com/Dav1dde/pasteofexile#public-api) `/ID/raw` and `/u/username/ID/raw` endpoints, stay on the `pobb.in` host through redirects, use a 12-second timeout, and are limited to 2 MiB. Pasted input is limited to 2 MiB and decoded XML to 8 MiB; unusually large notes or build fields are rejected with a clear message. Import data remains local.

PoB `^0`–`^9` and `^xRRGGBB` note color escapes render as native text spans in Overview; the HUD note reminder removes only recognized color markers. The original note string, including codes and links, is retained in saved data, and note text is never treated as markup or fetched.

## Build desk

Use the left navigation to switch between **Overview**, **Passive tree**, **Loadouts**, and **Campaign**. The build selector stays in the header. Overview shows character details and notes; Loadouts has separate selectors for gear and skill sets, with saved item slots and descriptions. PoB's explicit `{ID}` links and exact-name/singleton matching are used only when they establish a compatible linked loadout; otherwise the independent gear, skill, and tree selectors remain available without guessing combinations. Changing the selected skill set also updates the short build reminder in the HUD. These views are read-only and do not calculate stats or modify imported builds.

The passive graph uses actual allocated node IDs from each PoB tree spec and selects the exact declared version; it never upgrades an older spec. Versioned GGG metadata is bundled and pinned by the SHA-256 of each original `data.json`: PoB `3_29` uses export tag [`3.29.1`](https://github.com/grindinggear/skilltree-export/tree/3.29.1) (`7e9f755e33152129ebf36c2ebdad639c527e4ad70d274b1fefb860f30ca01122`), and PoB `3_30` uses tag [`3.30.0`](https://github.com/grindinggear/skilltree-export/tree/3.30.0) (`9686b969bbd744a77f13eeadc57bb71be5032b0ed22be1ee4dbf2025649ccab0`). Exact-version node, frame, group, connector and class artwork atlases are downloaded asynchronously from the pinned metadata and cached under `~/.cache/wraeclast-companion/passive-tree/` (or `$XDG_CACHE_HOME/wraeclast-companion/passive-tree/`). The loader validates dimensions, byte limits, and HTTPS redirects; if an older GGG CDN URL now serves a newer atlas, it falls back only to the matching version directory in PoB source commit `5606cc64fa9f15694e69001a52b90aa968d01a86`, then validates the original pinned dimensions before caching. After first use, cached artwork can be reused offline. Unsupported or mismatched versions show an unavailable message instead of borrowing another graph or atlas; the distinct `3_30_ruthless` tree variant is not currently supported. Older saved imports that only retained tree titles need to be imported again to restore allocation. The renderer follows PoB's display-node and connector rules, uses version-matched node/group/frame artwork and exact orbit geometry, and draws straight and circular connectors using normal/active colors sampled from each version's published line atlas. It provides cursor-anchored scroll zoom, pan, node/stat search, hover tooltips, fit-to-build/fit-all framing, and hides unselected sibling ascendancies by default (with an **All asc.** toggle) to avoid GGG's overlapping portrait circles. Cluster-jewel, tattoo, and override transformations are not applied, and are called out in the tree view.

## Campaign guide and in-game HUD

The offline guide covers Acts 1–10. It has an act selector and Previous/Next controls in both windows; selection is saved. You can use it without importing a build. When one is selected, the HUD includes a short build/gem/notes reminder.

The Campaign tab includes live HUD settings (enable/disable, compact bar vs. expanded details, screen position, opacity, **Preview HUD**, and `Client.txt` zone auto-tracking) persisted in `~/.config/poe-companion/hud.json`. When `Client.txt` auto-tracking is enabled, the companion tails newly appended `You have entered ...` lines from Steam/Proton PoE logs (including secondary Steam library folders) and advances matching campaign steps automatically.

Direct CLI control flags work while the HUD remains click-through (and use a fast local socket path without restarting GTK): `--next-step`, `--prev-step`, `--toggle-compact`, `--toggle-hud`, and `--toggle-clickable`.

The descriptions and beginner tips are written for this app, not copied from guide prose. Route and quest landmarks for all ten acts were checked against the community-maintained [PoE Wiki act pages](https://www.poewiki.net/wiki/Act_1), ([2](https://www.poewiki.net/wiki/Act_2), [3](https://www.poewiki.net/wiki/Act_3), [4](https://www.poewiki.net/wiki/Act_4), [5](https://www.poewiki.net/wiki/Act_5), [6](https://www.poewiki.net/wiki/Act_6), [7](https://www.poewiki.net/wiki/Act_7), [8](https://www.poewiki.net/wiki/Act_8), [9](https://www.poewiki.net/wiki/Act_9), [10](https://www.poewiki.net/wiki/Act_10)), its [quest index](https://www.poewiki.net/wiki/Quest), [passive skill quest list](https://www.poewiki.net/wiki/Passive_skill), and the [Acts quick guide](https://www.poewiki.net/wiki/Guide:Acts_quick_guide). Exact optional reward details and zone layouts can change with game patches; the guide favors major route landmarks and permanent quest rewards.

## Focus and matching

The HUD stays hidden until a focused window matches Path of Exile, moves to the output for that game window, hides when another app/planner is focused, and hides when focus tracking reports no window. A Steam client/library window and other Steam app IDs do not match. Defaults include `steam_app_238960` and PoE 1 executable IDs such as `PathOfExileSteam.exe` and `PathOfExile_x64Steam.exe`; inspect what your compositor reports and adjust `~/.config/poe-companion/config.json` if needed. Start from [the example](config/poe-companion.json), then set either `game_app_id_regex`/`game_title_regex` or one combined `game_window_regex`.

For troubleshooting, run `niri msg --json windows` and inspect the focused row's `app_id` and `title`; `niri msg --json workspaces` shows the output connected to its `workspace_id`. In KDE, KWin passes `resourceClass` (or resource name/desktop file) and caption; inspect the active window with KWin's scripting console (`Alt+F2`, enter `wm console`). Put the observed identity in `game_window_regex`. If the actual game window itself reports only the generic `steam` identity, set `allow_steam_window_identity` to `true` after confirming its title while the game is focused. The default excludes Steam launcher windows. The active game, launcher and Proton build may expose different XWayland versus Wayland identities.

## niri 26.04

Replace `/absolute/path/to/starter` in [config/niri.kdl](config/niri.kdl) with this checkout's absolute path (for example, the output of `pwd -P` from its directory), merge the block into your niri `binds` section, reload niri, and start the companion. The example binds **Mod+Shift+P** to toggle between the default click-through HUD and clickable HUD. The command does nothing unless the compositor currently reports a matching focused PoE window. No niri config is edited by the app.

niri focus is read through `niri msg --json event-stream`; its window and workspace queries provide app identity and output. If tracking is unavailable, the app hides the HUD and retries. Manual campaign controls remain available in the planner.

## KDE Plasma 6 Wayland

The companion needs the supplied KWin script to receive focus/output changes.

1. Install `kwin/poe-companion` with `kpackagetool6 --type=KWin/Script -i kwin/poe-companion` (or copy it to `~/.local/share/kwin/scripts/poe-companion`).
2. In **System Settings → Window Management → KWin Scripts**, enable **PoE Companion focus bridge** (log out/in if it is not listed).
3. Start the companion. It exposes a session D-Bus method consumed by that script. The script sends a focus heartbeat every two seconds; if updates stop, the app hides the HUD.
4. In **System Settings → Keyboard → Shortcuts → Add New → Command or Script**, create a command shortcut with the absolute path `<checkout>/bin/poe-companion --toggle-clickable`; choose **Meta+Shift+P** or another unused key. The launcher enters the project Nix shell, but the command still needs `nix` discoverable in the environment inherited by Plasma. If the shortcut environment omits it from `PATH`, add Nix's bin directory to the command environment or use a small wrapper that sets `PATH` before calling the launcher.

The KWin script reports `windowActivated`, including a null window when focus is lost, and the window's output. Clicking the HUD uses a non-keyboard-interactive layer surface so game keyboard focus can remain with PoE while HUD buttons receive pointer clicks. KWin integration is supplied as source/config only; it is not installed or enabled automatically. No compositor config is changed. See the official [KWin scripting tutorial](https://develop.kde.org/docs/plasma/kwin/) and [KWin 6 API](https://develop.kde.org/docs/plasma/kwin/api/) for script packaging and window identity properties.

## Checks and current limits

Run the lightweight checks with:

```sh
nix develop --command python3 -m unittest discover -s tests -v
nix develop --command python3 -m compileall -q poe_companion tests
```

The checks cover PoB decoding/field extraction, all-act campaign data and progress edges, and shared focus matching including the Steam-launcher guard. Nested compositor smoke tests can verify layer-shell mapping and focus behavior without touching the user's desktop. Steam/Proton/PoE identity and physical Plasma shortcuts still need checking in the target session. On niri, verify the actual game row with `niri msg --json windows`; if its identity is not matched, set `game_window_regex` in `~/.config/poe-companion/config.json` to a regex matching that row's app ID/title. On KDE, inspect the active window with KWin's scripting console (`Alt+F2`, enter `wm console`) and use the same override process.
