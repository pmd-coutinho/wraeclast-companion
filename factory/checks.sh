#!/usr/bin/env bash
# The repo's checks. Exit 0 only when the branch is fit to review.
#
# The gate runs this script from the main checkout with the job's worktree as
# the working directory. Keep it fast, deterministic and non-interactive: no
# watch modes, no prompts, nothing that rewrites files.
#
# Wraeclast Companion is a pure-Python GTK 4 app with no Python package
# manager: its dependencies (PyGObject, PyCairo, Pillow, GTK 4,
# gtk4-layer-shell) come from the system (Nobara/Fedora RPMs) or from
# `nix develop` (flake.nix). Use native python3 when it can import them,
# otherwise run everything inside the flake's dev shell.
set -euo pipefail

# __pycache__/ is gitignored, but don't litter the worktree anyway.
export PYTHONDONTWRITEBYTECODE=1

run_checks() {
    set -euo pipefail
    echo "== syntax: python sources (no bytecode written)"
    python3 - <<'PY'
import pathlib, sys
failed = False
for path in sorted([*pathlib.Path("poe_companion").rglob("*.py"), *pathlib.Path("tests").rglob("*.py")]):
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        failed = True
sys.exit(1 if failed else 0)
PY
    echo "== json: campaign data, sample config, KWin metadata"
    for f in poe_companion/campaign.json config/poe-companion.json kwin/poe-companion/metadata.json; do
        python3 -m json.tool "$f" >/dev/null
    done
    echo "== shell: installer and launchers parse"
    bash -n install-nobara.sh
    sh -n bin/poe-companion
    sh -n bin/install-desktop-entry
    echo "== tests: unittest"
    python3 -m unittest discover -s tests
}

if python3 -c 'import gi, cairo, PIL; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
    run_checks
elif command -v nix >/dev/null 2>&1 && [ -f flake.nix ]; then
    export -f run_checks
    nix develop --command bash -c run_checks
else
    echo "factory/checks.sh: need python3 with GTK 4 + PyGObject + PyCairo + Pillow, or nix (flake.nix)." >&2
    exit 1
fi
