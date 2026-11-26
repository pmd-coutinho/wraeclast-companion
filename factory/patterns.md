# Codebase Patterns & Conventions

<!--
  Read by spec-writer, builder, review-fixer and reviewers on every job.
  Refresh with `/factory refresh-patterns`. Keep it under ~150 lines and pointer-based.
-->

## Architecture & Boundaries
- Pure-Python 3 GTK 4 desktop app (PyGObject + PyCairo + Pillow + `gtk4-layer-shell`). No web UI,
  no server, no package manager: deps come from the system (`install-nobara.sh`) or `flake.nix`.
  Don't add third-party Python packages; stdlib only beyond the four above.
- `poe_companion/app.py` — the only GTK *application* module: `Companion(Gtk.Application)`, the planner
  window (pages built in `_build_*_page`), the layer-shell HUD (`build_hud`, `apply_hud_layout`),
  `Store` (state.json), HUD prefs, control socket, focus tracking (niri / KWin D-Bus), `main()`.
- Logic modules are **GTK-free** and unit-testable; keep them that way:
  - `pob.py` — Path of Building share-code / XML / pobb.in import → plain `dict` build.
  - `passive_tree.py` — loads pinned `data/skilltree-*.json.gz`, `prepare_tree_graph()`.
  - `tree_art.py` — downloads/caches/validates tree atlas images from `web.poecdn.com`.
  - `client_log.py` — `Client.txt` discovery (Steam libraries), tailing, zone/level parsing.
  - `policy.py` — focus matching (`is_game_focus`), HUD click toggle, campaign `advance`.
  - `tools.py` — item cards, gem colours, tree diff, regex presets, `RunTimer`, level milestones.
  - `notes.py` — PoB `^N` / `^xRRGGBB` colour-escape parsing.
- `tree_canvas.py` — the only other GTK module: `PassiveTreeCanvas(Gtk.DrawingArea)` + pure geometry
  helpers at module top (`fit_scale`, `zoom_pan_offset`, `search_matching_nodes`, …).
- Data: `poe_companion/campaign.json` (Acts 1–10: `act`, `title`, `target`, `steps[]` with `title`,
  `detail`, `tip`, `layout`, `tags`). Tree JSON is version-pinned and gzipped in `data/`.
- Direction: `app.py` → logic modules. Logic modules never import `app.py`, `gi` or `tree_canvas`.
- Outside Python: `bin/poe-companion` (POSIX sh launcher + CLI hotkeys over the control socket),
  `install-nobara.sh` (bash installer/uninstaller), `kwin/poe-companion/` (KWin JS focus bridge calling
  D-Bus `org.poecompanion.Companion.FocusChanged`), `config/` (sample user config, niri keybinds).

## Canonical Exemplars
- New pure logic / parser: mirror `poe_companion/tools.py` (small typed functions, bounded inputs,
  `load_regex_presets` / `save_custom_regexes` for a validated user JSON file).
- Untrusted text → structured data: `poe_companion/pob.py` (`MAX_*` limits at top, `_bounded_text`,
  raise the module's `ImportError(ValueError)` with a user-facing message).
- Network fetch: `pob.py::download_pobbin_build` (host allowlist, custom `HTTPRedirectHandler`,
  timeout, `read(MAX+1)` size cap) and `tree_art.py::load_tree_atlases` (digest/size validation, cache).
- New planner page/tab: `app.py::_build_regex_page` (small) or `_build_overview_page`; register it in
  the nav pill bar in `build_ui` and the `Ctrl+N` handling in `_on_window_key_pressed`.
- Background work from the UI: `app.py::_begin_pobbin_import` / `_finish_pobbin_import` — worker
  `threading.Thread(daemon=True)`, results back via `GLib.idle_add`, stale results dropped by request id.
- Periodic UI work: `GLib.timeout_add*` callbacks returning `GLib.SOURCE_CONTINUE` / `SOURCE_REMOVE`
  (see `poll_client_log`, `_tick_run_timer`; registered in `app.py` with `GLib.timeout_add`).
- New HUD preference: add to `DEFAULT_HUD_PREFS` + validation in `load_hud_prefs`, wire in
  `_on_hud_pref_changed`, apply in `apply_hud_layout`.
- New CLI hotkey: `bin/poe-companion` case → socket command → `app.py::handle_command`; document in README.
- Campaign content: edit `campaign.json` steps; keep `title`, `detail`, `tip` non-empty.
- Tests: `tests/test_core.py` classes `ToolsAndQoLTests` (logic), `LocalStateTests` (Store),
  `ClientLogAndHudTests` (tailer + prefs).

## Shared Primitives
- State: `app.py::Store` (validated `state.json`, atomic `.tmp` + `replace`, `read_only_reason` keeps
  unreadable data untouched; `StateError` messages are shown to the user). Saved builds must pass
  `_valid_saved_build` — any new build field needs a validator entry there or saving fails.
- Small JSON reads: `app.py::load_json(path, default)` (128 KiB cap, type-checked default).
- Paths: `DATA` (`~/.local/share/poe-companion`), `CONFIG` / `HUD_PREFS_PATH`
  (`~/.config/poe-companion/`), `tools.REGEX_PREFS_PATH`, tree art cache
  `~/.cache/wraeclast-companion/passive-tree/`. Module-level constants so tests can monkeypatch them.
- Styling: one app `Gtk.CssProvider` string in `app.py::build_ui` (≈ lines 474–872) with the palette
  (bg `#090d14`, text `#e6edf5`, gold accent `#e5b864`/`#f4cc7b`, muted `#93a6bc`/`#7e92a8`) and classes
  (`planner-card`, `hero-card`, `section-kicker`, `planner-subtle`, `planner-accent`, `compact-btn`,
  `nav-pill-btn`, `diff-pill-add`/`-rem`, …). HUD CSS is generated separately (`self.hud_css`,
  opacity-aware) in `apply_hud_layout`. Reuse classes; add new ones to the same CSS block.
- Widgets: `_scrolled(child)`, `_clear_box(box)`, `_set_dropdown(...)` (guards re-entrant
  `notify::selected`), `_populate_step_tags(...)`, `_copy_regex(...)` (clipboard + status).
- User feedback: `self.status.set_text(...)` (footer), `_set_import_feedback` in the import dialog.
- Focus: `policy.is_game_focus(info, config)` with regexes from `config.json`; never hard-code app ids.

## Testing & Fixtures
- Runner: stdlib `unittest`, single file `tests/test_core.py`; run `bash factory/checks.sh`
  (or `nix develop --command python3 -m unittest discover -s tests -v`).
- Tests import `poe_companion.app` (needs GTK typelibs) but never open windows or a display;
  test logic and data, not widgets.
- Isolate filesystem state with `tempfile.TemporaryDirectory()` and swap module constants
  (`app_module.DATA = Path(directory)` in try/finally) or pass `path=` arguments.
- Network: never hit the network; `unittest.mock.patch` `urllib.request.build_opener` (see the tree-art
  download/cache tests: `mock.patch("poe_companion.tree_art.urllib.request.build_opener", ...)`). Clocks: inject (`RunTimer(clock=lambda: now[0])`).
- Build fixtures inline (PoB XML bytes, share codes via `base64` + `zlib`); load `campaign.json`
  from `ROOT / "poe_companion/campaign.json"`.
- Campaign tests assert specific phrases and ordering in `detail` text — rewording a step can
  legitimately require updating those assertions; don't delete them.

## Gotchas & Anti-patterns
- `pob.py` defines its own `ImportError` (shadows the builtin); `app.py` imports it as
  `PobImportError`. Don't `except ImportError` in `app.py` expecting the PoB one.
- `app.py` loads `libgtk4-layer-shell` via `ctypes` **before** `import gi` — keep that order; don't add
  GTK imports above it. `Layer` may be `None` (no layer-shell): guard HUD code paths.
- Never touch GTK widgets from worker threads; always marshal back with `GLib.idle_add`.
- Every external input is bounded (`MAX_*` constants, file-size checks before `read_text`, schema
  validation). Keep new inputs bounded the same way; never `eval`/`pickle` user data.
- Network access is allowlisted: pobb.in (HTTPS raw paths only) and `web.poecdn.com` / pinned PoB
  source commit for art. Don't add hosts casually; redirects must re-validate.
- The control socket (`control.sock`, mode 0600) takes plain-text commands; new commands go through
  `handle_command`'s explicit matches only.
- No game memory reading, injection, input automation or packet inspection — the project promises
  "zero game injection" (GGG ToS). Only compositor focus metadata and `Client.txt` tailing.
- Unknown keys in `state.json` make `Store` read-only by design (data protection); adding a top-level
  state key requires updating the allowed set in `Store.__init__` and `Store.save`.
- HUD must stay click-through by default and keyboard-focus-free; don't steal PoE's focus.
- `app.py` is large (~3.3k lines): put new non-GTK logic in a logic module (usually `tools.py`) and
  keep `app.py` to wiring and widgets.
- README documents every user-facing feature and shortcut — update it with user-visible changes.
