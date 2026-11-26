# AGENTS.md

Wraeclast Companion: a native Linux/Wayland Path of Exile 1 build desk, passive
tree viewer and campaign leveling HUD, in pure Python 3 with GTK 4, Cairo and
`gtk4-layer-shell`. See `README.md` for features and `factory/patterns.md` for
code layout, exemplars and conventions.

## Setup (fresh checkout)

There is no Python package manager and nothing to install inside the repo.
Dependencies (PyGObject, PyCairo, Pillow, GTK 4, gtk4-layer-shell,
gobject-introspection) come from either:

- **Nix**: `nix develop` (see `flake.nix`) — run commands as
  `nix develop --command <cmd>`.
- **Nobara/Fedora**: system RPMs, installed by `./install-nobara.sh`.

No env files. User data lives outside the repo (`~/.local/share/poe-companion/`,
`~/.config/poe-companion/`, `~/.cache/wraeclast-companion/`); tests never touch it.

## Run

`./bin/poe-companion` (or `nix develop --command python3 -m poe_companion.app`).
It is a desktop GTK app that needs a Wayland display; there is no dev server or
web UI. Agents can't open a display, so verify behaviour through tests.

## Validation

`bash factory/checks.sh` runs everything (it uses native `python3` when GTK is
importable, otherwise `nix develop`):

- syntax check of `poe_companion/` and `tests/`
- `python3 -m json.tool` on `poe_companion/campaign.json`,
  `config/poe-companion.json`, `kwin/poe-companion/metadata.json`
- `bash -n install-nobara.sh`, `sh -n bin/poe-companion bin/install-desktop-entry`
- `python3 -m unittest discover -s tests`

## Software factory

`/factory run <EPIC-KEY | KEY-1 KEY-2 ...>` (Jira) or `/factory run [<n> ...]`
(GitHub issues labeled `factory`) builds tickets through intake → spec (Opus
high, chunked) → build (sequential Haiku chunks) → gate → Opus (high) reviews
(code + security, plus ux + ui on UI changes; Haiku point-by-point fixes) →
PR human-review summary, one PR per ticket (never auto-merged). State is in
`factory/jobs/` (gitignored); `factory/bin/job` is its only writer. Checks:
`factory/checks.sh`. Dashboard:
`cd factory && python3 -m http.server --bind 127.0.0.1`, then open
http://localhost:8000/dashboard.html.

Each job works in its own git worktree, a full checkout of the repo at
`.worktrees/<job-id>` (gitignored). A tool run from the repo root that doesn't
read `.gitignore` sees those copies too; exclude `.worktrees/` in its config.
