#!/usr/bin/env bash
set -euo pipefail

# Wraeclast Companion installer for Nobara Linux (Fedora-based).
# Installs native RPM dependencies (no Nix required), copies the app into
# ~/.local/share/wraeclast-companion, adds ~/.local/bin/poe-companion,
# creates the desktop launcher, and installs the KDE Plasma 6 KWin focus bridge.

APP_NAME="Wraeclast Companion"
BIN_NAME="poe-companion"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"

INSTALL_DIR="$XDG_DATA_HOME/wraeclast-companion"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$XDG_DATA_HOME/applications"
KWIN_SCRIPT_DIR="$XDG_DATA_HOME/kwin/scripts/poe-companion"
DESKTOP_FILE="$APPS_DIR/poe-companion.desktop"

RPM_PACKAGES=(
    python3
    python3-gobject
    python3-cairo
    python3-pillow
    gtk4
    gtk4-layer-shell
    gobject-introspection
)

ACTION="install"
SKIP_DEPS=0
PURGE_DATA=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Install or uninstall $APP_NAME on Nobara / Fedora Linux.

Options:
  --skip-deps     Skip installing RPM packages via dnf
  --uninstall     Remove the installed app, launcher, and KWin script
  --purge         With --uninstall, also remove saved builds and HUD config
  -h, --help      Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-deps) SKIP_DEPS=1 ;;
        --uninstall) ACTION="uninstall" ;;
        --purge) PURGE_DATA=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; }

do_uninstall() {
    info "Uninstalling $APP_NAME..."
    rm -f "$BIN_DIR/$BIN_NAME" "$DESKTOP_FILE"
    rm -rf "$INSTALL_DIR"

    if command -v kpackagetool6 >/dev/null 2>&1; then
        kpackagetool6 --type=KWin/Script -r poe-companion >/dev/null 2>&1 || true
    fi
    rm -rf "$KWIN_SCRIPT_DIR"

    if command -v kwriteconfig6 >/dev/null 2>&1; then
        kwriteconfig6 --file kwinrc --group Plugins --key poe-companionEnabled --delete >/dev/null 2>&1 || true
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    fi

    if [[ $PURGE_DATA -eq 1 ]]; then
        info "Removing saved builds and configuration..."
        rm -rf "$XDG_DATA_HOME/poe-companion" "$XDG_CONFIG_HOME/poe-companion" \
               "${XDG_CACHE_HOME:-$HOME/.cache}/wraeclast-companion"
    else
        info "Kept saved builds and settings in ~/.local/share/poe-companion and ~/.config/poe-companion."
    fi

    info "Uninstall complete."
}

if [[ "$ACTION" == "uninstall" ]]; then
    do_uninstall
    exit 0
fi

if [[ ! -d "$SCRIPT_DIR/poe_companion" || ! -f "$SCRIPT_DIR/bin/poe-companion" ]]; then
    err "Run this script from the Wraeclast Companion repository root."
    exit 1
fi

if [[ $SKIP_DEPS -eq 0 ]]; then
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        if [[ "${ID:-}" != "nobara" && "${ID:-}" != "fedora" && "${ID_LIKE:-}" != *fedora* ]]; then
            warn "Detected '${PRETTY_NAME:-unknown}' (this installer targets Nobara/Fedora)."
        fi
    fi

    if ! command -v dnf >/dev/null 2>&1; then
        err "dnf was not found. Install dependencies manually or re-run with --skip-deps."
        exit 1
    fi

    info "Installing native dependencies via dnf..."
    if [[ $EUID -eq 0 ]]; then
        dnf install -y "${RPM_PACKAGES[@]}"
    else
        sudo dnf install -y "${RPM_PACKAGES[@]}"
    fi
fi

info "Installing application files to $INSTALL_DIR..."
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/bin" "$BIN_DIR" "$APPS_DIR"

cp -a "$SCRIPT_DIR/poe_companion" "$INSTALL_DIR/"
rm -rf "$INSTALL_DIR/poe_companion/__pycache__"
cp -a "$SCRIPT_DIR/bin/poe-companion" "$INSTALL_DIR/bin/poe-companion"
chmod 755 "$INSTALL_DIR/bin/poe-companion"
if [[ -d "$SCRIPT_DIR/config" ]]; then
    cp -a "$SCRIPT_DIR/config" "$INSTALL_DIR/"
fi
if [[ -d "$SCRIPT_DIR/kwin" ]]; then
    cp -a "$SCRIPT_DIR/kwin" "$INSTALL_DIR/"
fi

ln -sf "$INSTALL_DIR/bin/poe-companion" "$BIN_DIR/$BIN_NAME"

info "Installing desktop launcher to $DESKTOP_FILE..."
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Wraeclast Companion
Comment=Path of Exile 1 campaign guide, build desk, and leveling HUD
Exec=$BIN_DIR/$BIN_NAME
Icon=applications-games
Terminal=false
Categories=Game;Utility;
StartupNotify=false
EOF

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

if [[ -d "$INSTALL_DIR/kwin/poe-companion" ]]; then
    if command -v kpackagetool6 >/dev/null 2>&1; then
        info "Installing KDE Plasma 6 KWin focus bridge..."
        if kpackagetool6 --type=KWin/Script -s poe-companion >/dev/null 2>&1; then
            kpackagetool6 --type=KWin/Script -u "$INSTALL_DIR/kwin/poe-companion" >/dev/null 2>&1 || true
        else
            kpackagetool6 --type=KWin/Script -i "$INSTALL_DIR/kwin/poe-companion" >/dev/null 2>&1 || true
        fi
    else
        mkdir -p "$(dirname "$KWIN_SCRIPT_DIR")"
        rm -rf "$KWIN_SCRIPT_DIR"
        cp -a "$INSTALL_DIR/kwin/poe-companion" "$KWIN_SCRIPT_DIR"
    fi

    if command -v kwriteconfig6 >/dev/null 2>&1; then
        kwriteconfig6 --file kwinrc --group Plugins --key poe-companionEnabled true >/dev/null 2>&1 || true
    fi
fi

if command -v python3 >/dev/null 2>&1 && [[ $SKIP_DEPS -eq 0 ]]; then
    if ! python3 -c 'import ctypes, gi, cairo, PIL; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
        warn "Python GTK 4/Pillow check failed; verify the RPM packages installed cleanly."
    fi
fi

cat <<EOF

✓ $APP_NAME installed!

  Launch:    $BIN_DIR/$BIN_NAME (or open "Wraeclast Companion" from your app menu)
  Uninstall: $SCRIPT_DIR/install-nobara.sh --uninstall

KDE Plasma 6 (Nobara default) notes:
  • The KWin focus bridge was installed and enabled. If the HUD does not appear
    when focusing PoE 1 on your first run, log out and back in once (or toggle
    "PoE Companion focus bridge" in System Settings → Window Management → KWin Scripts).
  • Optional hotkeys (System Settings → Keyboard → Shortcuts → Add New → Command):
      Next step:      $BIN_DIR/$BIN_NAME --next-step
      Previous step:  $BIN_DIR/$BIN_NAME --prev-step
      Toggle compact: $BIN_DIR/$BIN_NAME --toggle-compact
      Toggle HUD:     $BIN_DIR/$BIN_NAME --toggle-hud
      Click-through:  $BIN_DIR/$BIN_NAME --toggle-clickable
EOF
