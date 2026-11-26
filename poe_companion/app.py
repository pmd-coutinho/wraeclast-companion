from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# gtk4-layer-shell documents that its shared library must be globally visible
# before GI imports GTK. Loading it here keeps both supported launch paths safe.
_layer_library = None
for _lib_name in ("libgtk4-layer-shell.so.0", "libgtk4-layer-shell.so"):
    try:
        _layer_library = ctypes.CDLL(_lib_name, mode=ctypes.RTLD_GLOBAL)
        break
    except OSError:
        continue

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

Layer = None
if _layer_library is not None:
    try:
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell as Layer
    except (ValueError, ImportError):
        Layer = None

from .pob import (MAX_LINKED_LOADOUTS, MAX_NOTES_CHARS, ImportError as PobImportError, download_pobbin_build,
                  looks_like_share_url, parse_build)
from .notes import MAX_COLOR_TAGS, format_note_spans, plain_note_summary
from .passive_tree import TreeDataError, prepare_tree_graph
from .tree_art import load_tree_atlases
from .policy import advance, is_game_focus, toggle_clickable
from .tree_canvas import PassiveTreeCanvas
from .client_log import ClientLogTailer, discover_client_log, match_campaign_step
from .tools import (RunTimer, compute_tree_diff, format_duration, gem_socket_color,
                    generate_build_vendor_regex, is_support_gem, level_milestone_cue,
                    load_regex_presets, next_level_milestone, parse_item_card, save_custom_regexes)
from . import tray

APP_ID = "org.poecompanion.Companion"
DATA = Path.home() / ".local/share/poe-companion"
CONFIG = Path.home() / ".config/poe-companion/config.json"
HUD_PREFS_PATH = Path.home() / ".config/poe-companion/hud.json"
SOCKET = DATA / "control.sock"
CAMPAIGN = Path(__file__).with_name("campaign.json")
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_SAVED_BUILDS = 50
HUD_POSITIONS = (
    ("top-center", "Top center"),
    ("top-right", "Top right"),
    ("top-left", "Top left"),
    ("bottom-center", "Bottom center"),
    ("bottom-left", "Bottom left"),
)
DEFAULT_HUD_PREFS = {
    "enabled": True,
    "position": "top-center",
    "compact": True,
    "opacity": 0.88,
    "interactive_controls": True,
    "show_timer": False,
    "auto_track_client_log": True,
    "client_log_path": "",
}


def load_hud_prefs(path: Path = HUD_PREFS_PATH) -> dict:
    raw = load_json(path, {})
    prefs = dict(DEFAULT_HUD_PREFS)
    if isinstance(raw.get("enabled"), bool):
        prefs["enabled"] = raw["enabled"]
    if raw.get("position") in {k for k, _ in HUD_POSITIONS}:
        prefs["position"] = raw["position"]
    if isinstance(raw.get("compact"), bool):
        prefs["compact"] = raw["compact"]
    if isinstance(raw.get("opacity"), (int, float)) and not isinstance(raw.get("opacity"), bool):
        prefs["opacity"] = min(0.96, max(0.45, float(raw["opacity"])))
    if isinstance(raw.get("interactive_controls"), bool):
        prefs["interactive_controls"] = raw["interactive_controls"]
    if isinstance(raw.get("show_timer"), bool):
        prefs["show_timer"] = raw["show_timer"]
    if isinstance(raw.get("auto_track_client_log"), bool):
        prefs["auto_track_client_log"] = raw["auto_track_client_log"]
    if isinstance(raw.get("client_log_path"), str) and len(raw["client_log_path"]) <= 1024:
        prefs["client_log_path"] = raw["client_log_path"]
    return prefs


def save_hud_prefs(prefs: dict, path: Path = HUD_PREFS_PATH):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"Could not save HUD preferences: {exc}", file=sys.stderr)


def compact_step_summary(detail: str, limit: int = 128) -> str:
    """Return a concise single-sentence cue from a campaign step detail for the compact HUD."""
    text = re.sub(r"\s+", " ", (detail or "").strip())
    if not text:
        return ""
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    if len(first_sentence) <= limit:
        return first_sentence
    return first_sentence[:limit - 1].rstrip(" ,;:") + "…"


class StateError(ValueError):
    """A planner state update could not be saved without losing user data."""


def load_json(path: Path, default):
    try:
        if path.stat().st_size > 128 * 1024:
            return default
        value = json.loads(path.read_text())
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError, TypeError):
        return default


def _valid_saved_build(build) -> bool:
    if not isinstance(build, dict):
        return False
    if any(not isinstance(build.get(key, ""), str) for key in ("name", "class", "ascendancy", "level", "notes")):
        return False
    if len(build.get("notes", "")) > MAX_NOTES_CHARS:
        return False
    def valid_groups(groups):
        return isinstance(groups, list) and len(groups) <= 128 and all(
            isinstance(group, dict) and isinstance(group.get("label"), str)
            and len(group["label"]) <= 1024 and isinstance(group.get("gems"), list)
            and len(group["gems"]) <= 64
            and all(isinstance(gem, str) and len(gem) <= 2048 for gem in group["gems"])
            for group in groups
        )

    if not valid_groups(build.get("groups", [])):
        return False
    variants = build.get("variants", [])
    if not isinstance(variants, list) or len(variants) > 32 or not all(isinstance(x, str) for x in variants):
        return False
    skill_sets = build.get("skill_sets", [])
    if not isinstance(skill_sets, list) or len(skill_sets) > 32:
        return False
    if not all(isinstance(item, dict) and isinstance(item.get("id"), str)
                and isinstance(item.get("title"), str) and valid_groups(item.get("groups", []))
                for item in skill_sets):
        return False
    tree_specs = build.get("tree_specs", [])
    if not isinstance(tree_specs, list) or len(tree_specs) > 32:
        return False
    if not all(isinstance(item, dict) and all(isinstance(item.get(key, ""), str)
                                               for key in ("id", "title", "tree_version", "mastery_effects"))
               and all(item.get(key) is None or (isinstance(item.get(key), int) and not isinstance(item.get(key), bool) and 0 <= item[key] <= 255)
                       for key in ("class_id", "ascend_class_id", "secondary_ascend_class_id"))
               and (item.get("tree_display_version") is None or (isinstance(item.get("tree_display_version"), str)
                                                                    and len(item["tree_display_version"]) <= 1024))
               and isinstance(item.get("nodes", []), list) and len(item.get("nodes", [])) <= 2048
               and all(isinstance(node, int) and not isinstance(node, bool) and 0 <= node <= 999999
                       for node in item.get("nodes", [])) for item in tree_specs):
        return False
    item_sets = build.get("item_sets", [])
    if not isinstance(item_sets, list) or len(item_sets) > 32:
        return False
    if not all(isinstance(item, dict) and isinstance(item.get("id"), str)
               and isinstance(item.get("title"), str) and isinstance(item.get("slots", []), list)
               and len(item.get("slots", [])) <= 128
               and all(isinstance(slot, dict) and all(isinstance(slot.get(key, ""), str)
                                                       for key in ("name", "item_id", "title"))
                       for slot in item.get("slots", [])) for item in item_sets):
        return False
    if sum(len(item.get("slots", [])) for item in item_sets) > 2048:
        return False
    item_texts = build.get("items", {})
    if not isinstance(item_texts, dict) or len(item_texts) > 512:
        return False
    if (not all(isinstance(key, str) and isinstance(value, str) and len(value) <= 32 * 1024
                for key, value in item_texts.items())
            or sum(map(len, item_texts.values())) > 512 * 1024):
        return False
    links = build.get("linked_loadouts", [])
    if not isinstance(links, list) or len(links) > MAX_LINKED_LOADOUTS:
        return False
    if not all(isinstance(item, dict) and all(isinstance(item.get(key, ""), str)
                                               for key in ("title", "link_id", "tree_spec_id", "item_set_id", "skill_set_id"))
               for item in links):
        return False
    return all(isinstance(build.get(key, ""), str) and len(build.get(key, "")) <= 1024
               for key in ("tree_version", "active_tree_spec", "selected_tree_spec", "active_item_set",
                           "selected_item_set", "selected_skill_set"))


class Store:
    def __init__(self, campaign: list):
        DATA.mkdir(parents=True, exist_ok=True)
        self.path = DATA / "state.json"
        self._campaign_steps = [len(act["steps"]) for act in campaign]
        self.read_only_reason = None
        raw = {}
        if self.path.exists():
            try:
                if self.path.stat().st_size > MAX_STATE_BYTES:
                    raise StateError(f"Saved planner data is larger than {MAX_STATE_BYTES // (1024 * 1024)} MiB. The original file was kept unchanged; move or back it up to recover it.")
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise StateError("Saved planner data has an unsupported format. The original file was kept unchanged.")
                # The first launcher persisted this HUD-mode preference. It
                # is intentionally ignored now: every new process starts
                # click-through, but its campaign/build data remains valid.
                unknown = set(raw) - {"builds", "act", "step", "selected_build", "clickable"}
                if unknown:
                    raise StateError("Saved planner data contains fields this version cannot read. The original file was kept unchanged.")
            except StateError as exc:
                self.read_only_reason = str(exc)
                raw = {}
            except (OSError, ValueError, TypeError) as exc:
                self.read_only_reason = f"Saved planner data could not be read ({exc}). The original file was kept unchanged."
                raw = {}
        builds = raw.get("builds", [])
        if not isinstance(builds, list) or len(builds) > MAX_SAVED_BUILDS or not all(_valid_saved_build(b) for b in builds):
            self.read_only_reason = self.read_only_reason or "Saved planner builds have an unsupported shape. The original file was kept unchanged."
            builds = []
        act = raw.get("act", 0)
        step = raw.get("step", 0)
        selected = raw.get("selected_build", -1)
        act_valid = isinstance(act, int) and not isinstance(act, bool) and 0 <= act < len(campaign)
        act = min(max(act, 0), len(campaign) - 1) if act_valid else 0
        step_valid = isinstance(step, int) and not isinstance(step, bool) and 0 <= step < len(campaign[act]["steps"])
        step = min(max(step, 0), len(campaign[act]["steps"]) - 1) if step_valid else 0
        selected_valid = isinstance(selected, int) and not isinstance(selected, bool) and -1 <= selected < len(builds)
        if not all((act_valid, step_valid, selected_valid)):
            self.read_only_reason = self.read_only_reason or "Saved planner position has an unsupported shape. The original file was kept unchanged."
            selected = -1
        self.data = {"builds": builds, "act": act, "step": step, "selected_build": selected}

    @staticmethod
    def encode(data: dict) -> bytes:
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    def save(self, data: dict | None = None):
        if self.read_only_reason:
            raise StateError(self.read_only_reason)
        candidate = self.data if data is None else data
        if (not isinstance(candidate, dict) or set(candidate) != {"builds", "act", "step", "selected_build"}
                or not isinstance(candidate.get("builds"), list) or len(candidate["builds"]) > MAX_SAVED_BUILDS
                or not all(_valid_saved_build(build) for build in candidate["builds"])
                or not isinstance(candidate.get("act"), int) or isinstance(candidate.get("act"), bool)
                or not isinstance(candidate.get("step"), int) or isinstance(candidate.get("step"), bool)
                or not isinstance(candidate.get("selected_build"), int) or isinstance(candidate.get("selected_build"), bool)
                or not 0 <= candidate["act"] < len(self._campaign_steps)
                or not 0 <= candidate["step"] < self._campaign_steps[candidate["act"]]
                or not -1 <= candidate["selected_build"] < len(candidate["builds"])):
            raise StateError("This change contains planner data that cannot be reopened safely; nothing was saved.")
        payload = self.encode(candidate)
        if len(payload) > MAX_STATE_BYTES:
            raise StateError(f"This change would make saved planner data exceed {MAX_STATE_BYTES // (1024 * 1024)} MiB. Import was not saved; remove a build with large notes or back up the data file first.")
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_bytes(payload)
            tmp.replace(self.path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StateError(f"Could not save planner data: {exc}") from exc


def notify(message: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(1)
            s.connect(str(SOCKET))
            s.sendall((message + "\n").encode())
        return True
    except OSError as e:
        print(f"PoE Companion is not running ({e})", file=sys.stderr)
        return False


def _dbus_value(value) -> GLib.Variant:
    if isinstance(value, bool):
        return GLib.Variant("b", value)
    if isinstance(value, int):
        return GLib.Variant("i", value)
    return GLib.Variant("s", str(value))


def _dbus_props(props: dict) -> dict:
    return {key: _dbus_value(value) for key, value in props.items()}


def _menu_node_tuple(node: tuple) -> tuple:
    item_id, props, children = node
    return (item_id, _dbus_props(props),
            [GLib.Variant("(ia{sv}av)", _menu_node_tuple(child)) for child in children])


def _property_variants(properties: dict) -> dict:
    return {name: GLib.Variant(signature, value) for name, (signature, value) in properties.items()}


def _handle_properties_call(properties: dict, method: str, params, invocation):
    if method == "Get":
        _interface, name = params.unpack()
        spec = properties.get(name)
        if spec is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownProperty", f"Unknown property {name}")
            return
        invocation.return_value(GLib.Variant("(v)", (GLib.Variant(spec[0], spec[1]),)))
    elif method == "GetAll":
        invocation.return_value(GLib.Variant("(a{sv})", (_property_variants(properties),)))
    elif method == "Set":
        invocation.return_dbus_error("org.freedesktop.DBus.Error.PropertyReadOnly", "Tray properties are read-only")
    else:
        invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", f"Unknown method {method}")


class Companion(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.campaign = json.loads(CAMPAIGN.read_text())
        self.store = Store(self.campaign)
        self.focus = {"app_id": "", "title": "", "output": "", "focused": False}
        self.focus_queue = queue.Queue()
        self.kde_tracker = False
        self.niri_tracker = False
        self.last_focus_update = 0.0
        self.dbus_owner_id = 0
        self.planner = None
        self.hud = None
        self.hud_css = None
        self.hud_prefs = load_hud_prefs()
        self.hud_preview = False
        self._updating_hud_controls = False
        self.client_log_tailer = None
        self._client_log_discovery_ticks = 0
        self.layer_ready = False
        self.clickable = False  # Every process starts click-through.
        self.current_output = None
        self.monitors = None
        self.build_combo = None
        self.notes_view = None
        self._updating_build_combo = False
        self.campaign_act = None
        self.step_label = None
        self.detail_label = None
        self.tip_label = None
        self.context_label = None
        self.import_request_id = 0
        self.import_pending = False
        self._updating_tree_combo = False
        self._updating_diff_combo = False
        self._updating_item_combo = False
        self._updating_skill_combo = False
        self._updating_loadout_combo = False
        self._updating_campaign_list = False
        self.tree_load_id = 0
        self.tree_graph = None
        self.tree_search_matches = []
        self.tree_search_index = -1
        self.tree_diff_specs = []
        self.note_tags = {}
        self.item_tags = {}
        self.skill_tags = {}
        self.run_timer = RunTimer()
        self.regex_presets = load_regex_presets()
        self.regex_category_filter = "All"
        self._nav_buttons = {}
        self._notes_raw_text = ""
        self._notes_search_offset = 0
        self.import_dialog = None
        self.import_status_label = None
        self._loadout_filter_query = ""
        self.detected_level = None
        self.detected_char = ""
        self._milestone_timer_id = None

    def do_activate(self):
        if self.planner is None:
            self.build_ui()
            self.start_control_socket()
            self.start_compositor_tracker()
        self.planner.present()
        if self.store.read_only_reason:
            self.status.set_text(self.store.read_only_reason)

    def do_shutdown(self):
        if self.dbus_owner_id:
            try:
                Gio.bus_unown_name(self.dbus_owner_id)
            except Exception:
                pass
            self.dbus_owner_id = 0
        try:
            SOCKET.unlink(missing_ok=True)
        except OSError:
            pass
        Gtk.Application.do_shutdown(self)

    def build_ui(self):
        self.planner = Gtk.ApplicationWindow(application=self, title="Wraeclast Companion")
        self.planner.set_default_size(1240, 820)
        self.planner.set_size_request(760, 560)
        self.planner.add_css_class("planner-window")

        # Modern integrated client-side HeaderBar
        headerbar = Gtk.HeaderBar()
        headerbar.add_css_class("app-headerbar")
        headerbar.set_show_title_buttons(True)

        left_box = Gtk.Box(spacing=8)
        brand_title = Gtk.Label(label="◆ Wraeclast")
        brand_title.add_css_class("brand-mark")
        left_box.append(brand_title)

        builds = ["Campaign only"] + [b.get("name") or f"Build {i + 1}" for i, b in enumerate(self.store.data["builds"])]
        self.build_combo = Gtk.DropDown.new_from_strings(builds)
        self.build_combo.set_selected(self.store.data["selected_build"] + 1)
        self.build_combo.set_size_request(180, -1)
        self.build_combo.set_tooltip_text("Active build")
        self.build_combo.connect("notify::selected", self.change_build)
        left_box.append(self.build_combo)

        self.header_import_button = Gtk.Button(label="+ Import")
        self.header_import_button.add_css_class("suggested-action")
        self.header_import_button.set_tooltip_text("Import a PoB link, share code, or XML (Ctrl+I)")
        self.header_import_button.connect("clicked", lambda *_: self.open_import_dialog())
        left_box.append(self.header_import_button)

        self.remove_build_button = Gtk.Button(label="✕")
        self.remove_build_button.add_css_class("destructive-subtle")
        self.remove_build_button.add_css_class("compact-btn")
        self.remove_build_button.set_tooltip_text("Remove the currently selected saved build")
        self.remove_build_button.connect("clicked", self.remove_current_build)
        left_box.append(self.remove_build_button)
        headerbar.pack_start(left_box)

        # Center segmented pill navigation
        nav_box = Gtk.Box(spacing=3)
        nav_box.add_css_class("nav-pill-bar")
        nav_items = (
            ("overview", "Overview", "Ctrl+1"),
            ("tree", "Tree", "Ctrl+2"),
            ("loadouts", "Loadouts", "Ctrl+3"),
            ("campaign", "Campaign", "Ctrl+4"),
            ("regex", "Regex", "Ctrl+5"),
        )
        for page_id, label, shortcut in nav_items:
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("nav-pill-btn")
            btn.set_tooltip_text(f"{label} ({shortcut})")
            btn.connect("clicked", self._on_nav_pill_clicked, page_id)
            nav_box.append(btn)
            self._nav_buttons[page_id] = btn
        headerbar.set_title_widget(nav_box)

        # Right side: global HUD preview/status pill
        self.header_hud_button = Gtk.ToggleButton(label="● HUD")
        self.header_hud_button.add_css_class("hud-status-pill")
        self.header_hud_button.set_tooltip_text("Toggle in-game HUD preview")
        self.header_hud_button.connect("toggled", self._on_hud_preview_toggled)
        headerbar.pack_end(self.header_hud_button)

        self.planner.set_titlebar(headerbar)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.planner.set_child(root)

        self.page_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                    transition_duration=140, hexpand=True, vexpand=True)
        self.page_stack.set_hhomogeneous(False)
        root.append(self.page_stack)

        self._build_overview_page()
        self._build_tree_page()
        self._build_loadouts_page()
        self._build_campaign_page()
        self._build_regex_page()
        self._select_page("overview")

        # Global window keyboard shortcuts (Ctrl+1..5, Ctrl+I)
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_window_key_pressed)
        self.planner.add_controller(key_ctrl)

        footer = Gtk.Box(spacing=12)
        footer.add_css_class("app-footer")
        self.status = Gtk.Label(label="No build needed: the campaign guide works on its own.")
        self.status.set_xalign(0); self.status.set_hexpand(True); self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.add_css_class("footer-status")
        footer.append(self.status)
        self.footer_shortcuts = Gtk.Label(label="Ctrl+1–5 tabs · Ctrl+I import")
        self.footer_shortcuts.add_css_class("footer-hint")
        footer.append(self.footer_shortcuts)
        root.append(footer)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .planner-window { background: #090d14; color: #e6edf5; }
            .app-headerbar {
                background: #0e141f;
                color: #e6edf5;
                border-bottom: 1px solid #1d2a3a;
                padding: 4px 10px;
                min-height: 46px;
            }
            .brand-mark {
                font-size: 14px;
                font-weight: 800;
                color: #e5b864;
                letter-spacing: 0.4px;
                margin-right: 4px;
            }
            .nav-pill-bar {
                background: #090e16;
                border: 1px solid #1d2a3b;
                border-radius: 10px;
                padding: 3px;
            }
            .planner-window button.nav-pill-btn {
                background: transparent;
                color: #93a6bc;
                border: 1px solid transparent;
                border-radius: 7px;
                min-height: 28px;
                padding: 3px 13px;
                font-size: 12.5px;
                font-weight: 600;
            }
            .planner-window button.nav-pill-btn:hover {
                background: #141e2c;
                color: #e6edf5;
            }
            .planner-window button.nav-pill-btn:checked {
                background: #1e2c3f;
                color: #f4cc7b;
                border-color: #364c68;
                font-weight: 700;
            }
            .planner-window button.hud-status-pill {
                background: #121b27;
                color: #8ea3b8;
                border: 1px solid #243447;
                border-radius: 16px;
                min-height: 28px;
                padding: 2px 11px;
                font-size: 11.5px;
                font-weight: 700;
            }
            .planner-window button.hud-status-pill:hover {
                border-color: #46607d;
                color: #e6edf5;
            }
            .planner-window button.hud-status-pill:checked {
                background: #1c2f24;
                color: #5ee396;
                border-color: #2f6c48;
            }

            .section-kicker { font-size: 10.5px; font-weight: 700; color: #7e92a8; letter-spacing: 0.9px; }
            .app-footer {
                background: #0c121b;
                border-top: 1px solid #1a2636;
                padding: 6px 16px;
            }
            .footer-status { font-size: 12px; color: #8da1b7; }
            .footer-hint { font-size: 11px; color: #5b6d82; }

            .page-container { padding: 16px 20px; }
            .planner-card {
                background: #111824;
                border: 1px solid #202e40;
                border-radius: 12px;
                padding: 16px;
            }
            .hero-card {
                background: linear-gradient(135deg, #151f2e 0%, #101723 100%);
                border: 1px solid #293a50;
                border-radius: 14px;
                padding: 18px 20px;
            }
            .tree-canvas-frame {
                background: #070b10;
                border: 1px solid #202e40;
                border-radius: 12px;
            }
            .tip-callout {
                background: #151e2c;
                border: 1px solid #2a3b52;
                border-left: 3px solid #dca74c;
                border-radius: 8px;
                padding: 12px 14px;
            }
            .layout-callout {
                background: #102028;
                border: 1px solid #214453;
                border-left: 3px solid #3ec8d8;
                border-radius: 8px;
                padding: 10px 14px;
            }
            .layout-text { color: #bfeef5; font-size: 13px; }
            .step-tag {
                border-radius: 12px;
                padding: 2px 9px;
                font-size: 11px;
                font-weight: 700;
            }
            .tag-passive { background: #2d2411; color: #f6ce74; border: 1px solid #6b5220; }
            .tag-trial { background: #241936; color: #cfa2ff; border: 1px solid #543782; }
            .tag-resist { background: #331919; color: #ff9a8a; border: 1px solid #753434; }
            .tag-vendor { background: #132c29; color: #67e6d2; border: 1px solid #25645c; }
            .tag-bench { background: #15253b; color: #8ac2ff; border: 1px solid #2d517f; }
            .milestone-banner {
                background: #152b1f;
                color: #82f0ae;
                border: 1px solid #2b6644;
                border-radius: 8px;
                padding: 8px 12px;
                font-size: 12.5px;
                font-weight: 600;
            }
            .context-card {
                background: #0e1520;
                border: 1px solid #1e2c3d;
                border-radius: 10px;
                padding: 11px 14px;
            }
            .planner-subtle { color: #8ea1b7; }
            .planner-accent { color: #ecc06c; }
            .node-stat-text { color: #8eb7ff; font-size: 13px; }
            .stat-pill {
                background: #182334;
                color: #d6e2f0;
                border: 1px solid #28384e;
                border-radius: 16px;
                padding: 4px 11px;
                font-size: 12px;
                font-weight: 600;
            }
            .stat-pill-gold {
                background: #2b2213;
                color: #f4cc7b;
                border: 1px solid #63491f;
            }
            .diff-pill-add {
                background: #132c1e;
                color: #4ee28b;
                border: 1px solid #255a3b;
                border-radius: 12px;
                padding: 3px 9px;
                font-size: 11.5px;
                font-weight: 700;
            }
            .diff-pill-rem {
                background: #2f1519;
                color: #f57378;
                border: 1px solid #622830;
                border-radius: 12px;
                padding: 3px 9px;
                font-size: 11.5px;
                font-weight: 700;
            }
            .zoom-readout {
                font-size: 12px;
                font-weight: 700;
                color: #a9b8c9;
                min-width: 44px;
            }
            .timer-readout {
                font-family: monospace;
                font-size: 15px;
                font-weight: 700;
                color: #f4cc7b;
                background: #0d141e;
                border: 1px solid #253447;
                border-radius: 8px;
                padding: 4px 10px;
            }
            .split-chip {
                background: #152030;
                color: #c8d6e6;
                border: 1px solid #25364c;
                border-radius: 7px;
                padding: 3px 8px;
                font-size: 11px;
                font-family: monospace;
            }
            .toolbar-bar {
                background: #0f1622;
                border: 1px solid #1e2c3e;
                border-radius: 11px;
                padding: 8px 11px;
            }

            /* PoE-styled Item Card */
            .poe-item-card {
                background: #0b1018;
                border: 1px solid #27364a;
                border-radius: 10px;
                padding: 0;
            }
            .poe-item-header-unique {
                background: linear-gradient(180deg, #3c1f0e 0%, #24130a 100%);
                color: #e88a3c;
                border-bottom: 1px solid #75411c;
                border-radius: 9px 9px 0 0;
                padding: 10px 14px;
            }
            .poe-item-header-rare {
                background: linear-gradient(180deg, #373012 0%, #221e0d 100%);
                color: #f6dc74;
                border-bottom: 1px solid #6d5f24;
                border-radius: 9px 9px 0 0;
                padding: 10px 14px;
            }
            .poe-item-header-magic {
                background: linear-gradient(180deg, #162244 0%, #10172b 100%);
                color: #88a4ff;
                border-bottom: 1px solid #30498b;
                border-radius: 9px 9px 0 0;
                padding: 10px 14px;
            }
            .poe-item-header-normal {
                background: linear-gradient(180deg, #1e2633 0%, #151b24 100%);
                color: #dce4ef;
                border-bottom: 1px solid #354357;
                border-radius: 9px 9px 0 0;
                padding: 10px 14px;
            }
            .poe-item-name { font-size: 14px; font-weight: 700; }
            .poe-item-base { font-size: 12px; opacity: 0.88; }
            .poe-item-body { padding: 10px 14px; }
            .poe-item-meta { color: #8c9fb5; font-size: 11.5px; }
            .poe-item-mod { color: #8fb8ff; font-size: 12.5px; }
            .poe-item-sep { background: #1e2b3c; min-height: 1px; margin: 4px 0; }

            /* Visual Gem Link Cards */
            .gem-group-card {
                background: #0e1520;
                border: 1px solid #202e40;
                border-radius: 10px;
                padding: 11px 13px;
            }
            .gem-group-title { font-size: 12.5px; font-weight: 700; color: #ecc06c; }
            .socket-dot {
                min-width: 10px;
                min-height: 10px;
                border-radius: 99px;
            }
            .socket-red { background: #e35656; border: 1px solid #ff9494; }
            .socket-green { background: #47d478; border: 1px solid #96f5b7; }
            .socket-blue { background: #5a9bff; border: 1px solid #a8ccff; }
            .socket-white { background: #e8eef6; border: 1px solid #ffffff; }
            .socket-link-bar {
                background: #c79a4a;
                min-width: 7px;
                min-height: 2px;
                border-radius: 2px;
            }
            .gem-row-active { font-size: 13px; font-weight: 600; color: #eef3f9; }
            .gem-row-support { font-size: 12.5px; color: #a7b9ce; }

            /* Regex cards */
            .regex-card {
                background: #101824;
                border: 1px solid #213043;
                border-radius: 11px;
                padding: 12px 14px;
            }
            .regex-code {
                font-family: monospace;
                font-size: 12px;
                color: #82d9ff;
                background: #090e15;
                border: 1px solid #1b2838;
                border-radius: 7px;
                padding: 6px 10px;
            }

            /* Campaign checklist */
            .campaign-step-done { color: #679c7e; }
            .campaign-step-active { color: #f6d388; font-weight: 700; }
            .campaign-step-pending { color: #a8b9cc; }

            .planner-window button {
                min-height: 31px;
                background: #172231;
                color: #e6edf5;
                border: 1px solid #29394d;
                border-radius: 8px;
                padding: 4px 12px;
            }
            .planner-window button:hover { background: #202f43; border-color: #3d5470; }
            .planner-window button:active { background: #141e2b; }
            .planner-window button:disabled { opacity: 0.45; }
            .planner-window button.compact-btn { min-height: 29px; min-width: 29px; padding: 2px 8px; }
            .planner-window button.suggested-action {
                background: #cb9442;
                color: #0f1319;
                border-color: #dfa958;
                font-weight: 700;
            }
            .planner-window button.suggested-action:hover { background: #daa250; }
            .planner-window button.destructive-subtle {
                background: #191a23;
                color: #d99b9b;
                border-color: #3a272f;
            }
            .planner-window button.destructive-subtle:hover {
                background: #351d23;
                color: #f7c5c5;
                border-color: #6b333c;
            }

            .planner-window switch {
                background: #1b2738;
                border: 1px solid #2d3e54;
                border-radius: 14px;
            }
            .planner-window switch:checked {
                background: #c79040;
                border-color: #dca655;
            }
            .planner-window switch slider {
                background: #e9eff7;
                border-radius: 12px;
            }
            .planner-window scale trough {
                background: #1a2636;
                border-radius: 6px;
                min-height: 6px;
            }
            .planner-window scale highlight {
                background: #d49e48;
                border-radius: 6px;
            }
            .planner-window scale slider {
                background: #f4d593;
                border: 1px solid #9e722b;
                border-radius: 99px;
                min-width: 15px;
                min-height: 15px;
            }
            .planner-window dropdown button { background: #15202e; color: #e6edf5; border-color: #29394d; }
            .planner-window entry, .planner-window searchentry {
                background: #0c121b;
                color: #e6edf5;
                border: 1px solid #263648;
                border-radius: 8px;
                min-height: 31px;
            }
            .planner-window entry:focus-within, .planner-window searchentry:focus-within {
                border-color: #d49e48;
            }
            .planner-window list { background: #0e1520; color: #e6edf5; }
            .planner-window listbox.boxed-list {
                background: #0c121b;
                border: 1px solid #1f2d3f;
                border-radius: 10px;
            }
            .planner-window listbox.boxed-list row {
                background: transparent;
                color: #dbe5f0;
                border-bottom: 1px solid #172130;
            }
            .planner-window listbox.boxed-list row:hover { background: #162131; }
            .planner-window listbox.boxed-list row:selected {
                background: #2b2315;
                color: #f8dfa6;
            }
            .planner-window textview, .planner-window textview text {
                background: #0c121b;
                color: #e6edf5;
            }
            .planner-window paned > separator {
                background: #1c2838;
                min-width: 5px;
            }
            .planner-window progressbar trough {
                background: #192434;
                border-radius: 6px;
                min-height: 7px;
            }
            .planner-window progressbar progress {
                background: linear-gradient(90deg, #c79040, #ecc06c);
                border-radius: 6px;
                min-height: 7px;
            }
            .planner-window expander title {
                padding: 4px 2px;
                font-weight: 700;
                color: #d7e2f0;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.planner_css = css
        self.build_hud()
        # Tick run timer display once per second.
        GLib.timeout_add(1000, self._tick_run_timer)
        # All widgets and restored selectors exist before refresh runs.
        self.refresh_build()
        self.refresh_campaign()
        self.refresh_regex_list()

    def _select_page(self, page_id: str):
        self.page_stack.set_visible_child_name(page_id)
        for key, btn in self._nav_buttons.items():
            if btn.get_active() != (key == page_id):
                btn.set_active(key == page_id)

    def _on_nav_pill_clicked(self, button, page_id: str):
        if button.get_active():
            self._select_page(page_id)
        elif self.page_stack.get_visible_child_name() == page_id:
            button.set_active(True)

    def _on_window_key_pressed(self, _ctrl, keyval, _keycode, state):
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        key_map = {
            Gdk.KEY_1: "overview",
            Gdk.KEY_2: "tree",
            Gdk.KEY_3: "loadouts",
            Gdk.KEY_4: "campaign",
            Gdk.KEY_5: "regex",
        }
        if keyval in key_map:
            self._select_page(key_map[keyval])
            return True
        if keyval in (Gdk.KEY_i, Gdk.KEY_I):
            self.open_import_dialog()
            return True
        return False

    def open_import_dialog(self):
        if self.import_dialog is not None:
            self.import_dialog.present()
            return
        dlg = Gtk.Window(
            title="Import Path of Building",
            transient_for=self.planner,
            modal=True,
            default_width=560,
            default_height=310,
        )
        dlg.add_css_class("planner-window")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18); box.set_margin_bottom(18)
        box.set_margin_start(18); box.set_margin_end(18)
        dlg.set_child(box)

        head_row = Gtk.Box(spacing=10)
        head_title = Gtk.Label(label="Paste a pobb.in link, PoB share code, or XML")
        head_title.add_css_class("heading"); head_title.set_xalign(0); head_title.set_hexpand(True)
        head_row.append(head_title)
        paste_btn = Gtk.Button(label="Paste from Clipboard")
        paste_btn.connect("clicked", self._paste_into_import)
        head_row.append(paste_btn)
        box.append(head_row)

        hint = Gtk.Label(label="pobb.in links fetch online; share codes and XML work completely offline (max 2 MiB input).")
        hint.set_xalign(0); hint.set_wrap(True); hint.add_css_class("planner-subtle")
        box.append(hint)

        self.code_entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True)
        self.code_entry.set_top_margin(8); self.code_entry.set_bottom_margin(8)
        self.code_entry.set_left_margin(10); self.code_entry.set_right_margin(10)
        entry_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=120)
        entry_scroll.set_child(self.code_entry)
        box.append(entry_scroll)

        self.import_status_label = Gtk.Label(label="")
        self.import_status_label.set_xalign(0); self.import_status_label.set_wrap(True)
        self.import_status_label.add_css_class("planner-accent")
        self.import_status_label.set_visible(False)
        box.append(self.import_status_label)

        actions = Gtk.Box(spacing=8)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", lambda *_: self.code_entry.get_buffer().set_text(""))
        actions.append(clear_btn)
        actions.append(Gtk.Box(hexpand=True))
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: dlg.close())
        actions.append(cancel_btn)
        self.import_button = Gtk.Button(label="Import build")
        self.import_button.add_css_class("suggested-action")
        self.import_button.connect("clicked", self.import_build)
        actions.append(self.import_button)
        box.append(actions)

        def on_close(_win):
            self.import_dialog = None
            self.import_status_label = None
            return False

        dlg.connect("close-request", on_close)
        self.import_dialog = dlg
        dlg.present()

    def _paste_into_import(self, *_):
        display = Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()

        def on_read(cb, result):
            try:
                text = cb.read_text_finish(result)
                if text and self.code_entry is not None:
                    self.code_entry.get_buffer().set_text(text.strip())
            except Exception:
                pass

        clipboard.read_text_async(None, on_read)

    def _build_overview_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, hexpand=True, vexpand=True)
        page.add_css_class("page-container")

        hero_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hero_card.add_css_class("hero-card")
        top_row = Gtk.Box(spacing=12)
        hero_left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        hero_kicker = Gtk.Label(label="ACTIVE BUILD DESK")
        hero_kicker.add_css_class("section-kicker"); hero_kicker.set_xalign(0); hero_left.append(hero_kicker)
        self.profile_label = Gtk.Label()
        self.profile_label.add_css_class("title-1"); self.profile_label.set_xalign(0); self.profile_label.set_wrap(True)
        hero_left.append(self.profile_label)
        self.overview_meta = Gtk.Label()
        self.overview_meta.set_xalign(0); self.overview_meta.set_wrap(True); self.overview_meta.add_css_class("planner-subtle")
        hero_left.append(self.overview_meta)
        top_row.append(hero_left)

        quick_actions = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        for label, target, primary in (
            ("Passive Tree", "tree", False),
            ("Loadouts", "loadouts", False),
            ("Campaign", "campaign", False),
            ("+ Import Build", None, True),
        ):
            btn = Gtk.Button(label=label)
            if primary:
                btn.add_css_class("suggested-action")
                btn.connect("clicked", lambda *_: self.open_import_dialog())
            else:
                btn.connect("clicked", lambda _b, t=target: self._select_page(t))
            quick_actions.append(btn)
        top_row.append(quick_actions)
        hero_card.append(top_row)

        self.overview_pills = Gtk.Box(spacing=8)
        hero_card.append(self.overview_pills)
        page.append(hero_card)

        notes_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, vexpand=True)
        notes_card.add_css_class("planner-card")
        notes_header = Gtk.Box(spacing=10)
        notes_title = Gtk.Label(label="Build notes")
        notes_title.add_css_class("heading"); notes_title.set_xalign(0); notes_title.set_hexpand(True)
        notes_header.append(notes_title)
        self.notes_search_entry = Gtk.SearchEntry()
        self.notes_search_entry.set_placeholder_text("Find in notes…")
        self.notes_search_entry.set_size_request(220, -1)
        self.notes_search_entry.connect("search-changed", self._on_notes_search)
        self.notes_search_entry.connect("activate", self._on_notes_search_next)
        notes_header.append(self.notes_search_entry)
        notes_card.append(notes_header)

        self.notes_view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.notes_view.set_top_margin(10); self.notes_view.set_bottom_margin(10)
        self.notes_view.set_left_margin(12); self.notes_view.set_right_margin(12)
        note_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=260)
        note_scroll.set_child(self.notes_view)
        notes_card.append(note_scroll)
        page.append(notes_card)

        # Keep fallback code_entry reference for non-dialog paths
        self.code_entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True)
        self.import_button = self.header_import_button

        self.page_stack.add_titled(page, "overview", "Overview")

    def _on_notes_search(self, entry):
        self._notes_search_offset = 0
        self._find_in_notes(entry.get_text().strip(), advance_next=False)

    def _on_notes_search_next(self, entry):
        self._find_in_notes(entry.get_text().strip(), advance_next=True)

    def _find_in_notes(self, query: str, advance_next: bool):
        if not query or self.notes_view is None:
            return
        buf = self.notes_view.get_buffer()
        full = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        low_full = full.lower()
        low_q = query.lower()
        start = self._notes_search_offset + (1 if advance_next else 0)
        idx = low_full.find(low_q, start)
        if idx == -1 and start > 0:
            idx = low_full.find(low_q, 0)
        if idx == -1:
            return
        self._notes_search_offset = idx
        it_start = buf.get_iter_at_offset(idx)
        it_end = buf.get_iter_at_offset(idx + len(query))
        buf.select_range(it_start, it_end)
        self.notes_view.scroll_to_iter(it_start, 0.2, False, 0.0, 0.0)

    def _build_tree_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, vexpand=True)
        page.add_css_class("page-container")

        toolbar = Gtk.Box(spacing=8)
        toolbar.add_css_class("toolbar-bar")
        self.tree_variant_combo = Gtk.DropDown.new_from_strings(["No tree selected"])
        self.tree_variant_combo.set_size_request(195, -1)
        self.tree_variant_combo.set_hexpand(False)
        self.tree_variant_combo.set_factory(self._tree_title_factory(ellipsize=True))
        self.tree_variant_combo.set_list_factory(self._tree_title_factory(ellipsize=False))
        self.tree_variant_combo.connect("notify::selected", self.change_tree_spec)
        toolbar.append(self.tree_variant_combo)

        self.tree_diff_combo = Gtk.DropDown.new_from_strings(["Diff: Off"])
        self.tree_diff_combo.set_size_request(165, -1)
        self.tree_diff_combo.set_tooltip_text("Compare allocated nodes against another tree variant (green = added, red = respecced)")
        self.tree_diff_combo.set_factory(self._tree_title_factory(ellipsize=True))
        self.tree_diff_combo.set_list_factory(self._tree_title_factory(ellipsize=False))
        self.tree_diff_combo.connect("notify::selected", self._on_tree_diff_changed)
        toolbar.append(self.tree_diff_combo)

        self.tree_diff_added_badge = Gtk.Label(label="")
        self.tree_diff_added_badge.add_css_class("diff-pill-add")
        self.tree_diff_added_badge.set_visible(False)
        toolbar.append(self.tree_diff_added_badge)

        self.tree_diff_removed_badge = Gtk.Label(label="")
        self.tree_diff_removed_badge.add_css_class("diff-pill-rem")
        self.tree_diff_removed_badge.set_visible(False)
        toolbar.append(self.tree_diff_removed_badge)

        self.tree_search_entry = Gtk.SearchEntry()
        self.tree_search_entry.set_placeholder_text("Search nodes or stats…")
        self.tree_search_entry.set_hexpand(True)
        self.tree_search_entry.connect("search-changed", self._on_tree_search_changed)
        self.tree_search_entry.connect("activate", self._on_tree_search_next)
        toolbar.append(self.tree_search_entry)

        self.tree_search_count = Gtk.Label(label="")
        self.tree_search_count.add_css_class("planner-subtle")
        self.tree_search_count.set_visible(False)
        toolbar.append(self.tree_search_count)

        fit_build = Gtk.Button(label="Fit build")
        fit_build.set_tooltip_text("Frame allocated passive nodes")
        fit_build.connect("clicked", lambda *_: self.tree_canvas.fit_allocated())
        self.tree_fit_build_button = fit_build
        toolbar.append(fit_build)

        fit = Gtk.Button(label="Fit all")
        fit.set_tooltip_text("Frame the entire passive tree (0)")
        fit.connect("clicked", lambda *_: self.tree_canvas.fit())
        self.tree_fit_button = fit
        toolbar.append(fit)

        all_asc = Gtk.ToggleButton(label="All asc.")
        all_asc.set_tooltip_text("Show all ascendancies (off by default to avoid sibling portrait overlaps)")
        all_asc.connect("toggled", self._on_toggle_all_ascendancies)
        self.tree_all_asc_button = all_asc
        toolbar.append(all_asc)

        zoom_out = Gtk.Button(label="−")
        zoom_out.add_css_class("compact-btn")
        zoom_out.set_tooltip_text("Zoom out (-)")
        zoom_out.connect("clicked", lambda *_: self.tree_canvas.zoom_by(1.0 / 1.25))
        toolbar.append(zoom_out)

        self.tree_zoom_label = Gtk.Label(label="100%")
        self.tree_zoom_label.add_css_class("zoom-readout")
        self.tree_zoom_label.set_xalign(0.5)
        toolbar.append(self.tree_zoom_label)

        zoom_in = Gtk.Button(label="+")
        zoom_in.add_css_class("compact-btn")
        zoom_in.set_tooltip_text("Zoom in (+)")
        zoom_in.connect("clicked", lambda *_: self.tree_canvas.zoom_by(1.25))
        toolbar.append(zoom_in)

        retry = Gtk.Button(label="Retry artwork")
        retry.connect("clicked", lambda *_: self.refresh_passive_tree(self.current_build()))
        retry.set_sensitive(False)
        retry.set_visible(False)
        self.tree_retry_button = retry
        toolbar.append(retry)
        page.append(toolbar)

        self.tree_status = Gtk.Label(label="Choose an imported build to view its passive tree.")
        self.tree_status.set_xalign(0); self.tree_status.set_wrap(True); self.tree_status.add_css_class("planner-subtle")
        page.append(self.tree_status)

        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True, hexpand=True, vexpand=True)
        self.tree_canvas = PassiveTreeCanvas(self.select_tree_node, on_zoom_changed=self._on_tree_zoom_changed)
        self.tree_canvas.add_css_class("tree-canvas-frame")
        body.set_start_child(self.tree_canvas)
        body.set_resize_start_child(True)
        body.set_shrink_start_child(False)

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        detail.add_css_class("planner-card")
        detail.set_size_request(240, -1)
        detail.set_margin_start(8)

        detail_kicker = Gtk.Label(label="INSPECTOR")
        detail_kicker.add_css_class("section-kicker"); detail_kicker.set_xalign(0); detail.append(detail_kicker)
        self.tree_node_title = Gtk.Label(label="Build summary")
        self.tree_node_title.add_css_class("title-3"); self.tree_node_title.set_xalign(0); self.tree_node_title.set_wrap(True)
        detail.append(self.tree_node_title)
        self.tree_node_flags = Gtk.Label(label="Hover any node for a tooltip, or click to pin details here.")
        self.tree_node_flags.set_xalign(0); self.tree_node_flags.set_wrap(True); self.tree_node_flags.add_css_class("planner-subtle")
        detail.append(self.tree_node_flags)
        self.tree_node_stats = Gtk.Label()
        self.tree_node_stats.add_css_class("node-stat-text")
        self.tree_node_stats.set_xalign(0); self.tree_node_stats.set_yalign(0)
        self.tree_node_stats.set_wrap(True); self.tree_node_stats.set_selectable(True)
        stats_scroll = Gtk.ScrolledWindow(vexpand=True)
        stats_scroll.set_child(self.tree_node_stats)
        detail.append(stats_scroll)

        body.set_end_child(detail)
        body.set_resize_end_child(False)
        body.set_shrink_end_child(False)

        def fit_tree_pane(paned, _pspec):
            width = paned.get_width()
            if width > 0:
                position = max(320, width - 290)
                if paned.get_position() != position:
                    paned.set_position(position)
        body.connect("notify::width", fit_tree_pane)
        body.set_position(720)
        page.append(body)

        note = Gtk.Label(label="Scroll to zoom at cursor · Drag to pan · Hover for tooltips · Keys: + / - / 0 / Esc · Use Diff to compare leveling trees.")
        note.set_xalign(0); note.set_wrap(True); note.add_css_class("planner-subtle"); page.append(note)
        self.page_stack.add_titled(page, "tree", "Passive tree")

    @staticmethod
    def _tree_title_factory(ellipsize):
        factory = Gtk.SignalListItemFactory()
        def setup(_factory, item):
            label = Gtk.Label(xalign=0, wrap=not ellipsize)
            if ellipsize:
                label.set_single_line_mode(True)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_max_width_chars(24)
            label.set_margin_start(6); label.set_margin_end(6)
            item.set_child(label)
        def bind(_factory, item):
            model_item = item.get_item()
            item.get_child().set_text(model_item.get_string() if model_item else "")
        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return factory

    def _build_loadouts_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True, vexpand=True)
        page.add_css_class("page-container")

        header_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_card.add_css_class("toolbar-bar")

        self.loadout_filter_entry = Gtk.SearchEntry()
        self.loadout_filter_entry.set_placeholder_text("Filter gear slots, item names, or gems…")
        self.loadout_filter_entry.set_hexpand(True)
        self.loadout_filter_entry.connect("search-changed", self._on_loadout_filter_changed)
        header_card.append(self.loadout_filter_entry)

        linked_lbl = Gtk.Label(label="LINKED LOADOUT")
        linked_lbl.add_css_class("section-kicker")
        header_card.append(linked_lbl)
        self.linked_loadout_combo = Gtk.DropDown.new_from_strings(["No linked loadouts"])
        self.linked_loadout_combo.set_size_request(240, -1)
        self.linked_loadout_combo.connect("notify::selected", self.change_linked_loadout)
        header_card.append(self.linked_loadout_combo)
        page.append(header_card)

        independent = Gtk.Box(spacing=14, hexpand=True, vexpand=True)
        gear = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, vexpand=True)
        gear.add_css_class("planner-card")
        gear_top = Gtk.Box(spacing=10)
        gear_header = Gtk.Label(label="Gear")
        gear_header.add_css_class("heading"); gear_header.set_xalign(0); gear_header.set_hexpand(True)
        gear_top.append(gear_header)
        self.item_set_combo = Gtk.DropDown.new_from_strings(["No gear sets"])
        self.item_set_combo.set_size_request(200, -1)
        self.item_set_combo.connect("notify::selected", self.change_item_set)
        gear_top.append(self.item_set_combo)
        gear.append(gear_top)

        self.item_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.item_list.add_css_class("boxed-list")
        self.item_list.connect("row-selected", self.select_item_slot)
        item_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=190)
        item_scroll.set_child(self.item_list)
        gear.append(item_scroll)

        self.item_card_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        detail_scroll = Gtk.ScrolledWindow(min_content_height=200)
        detail_scroll.set_child(self.item_card_container)
        gear.append(detail_scroll)

        skills = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, vexpand=True)
        skills.add_css_class("planner-card")
        skill_top = Gtk.Box(spacing=10)
        skill_header = Gtk.Label(label="Skills & Gem Links")
        skill_header.add_css_class("heading"); skill_header.set_xalign(0); skill_header.set_hexpand(True)
        skill_top.append(skill_header)
        self.skill_set_combo = Gtk.DropDown.new_from_strings(["No skill sets"])
        self.skill_set_combo.set_size_request(200, -1)
        self.skill_set_combo.connect("notify::selected", self.change_skill_set)
        skill_top.append(self.skill_set_combo)
        skills.append(skill_top)

        self.skill_cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        skill_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=340)
        skill_scroll.set_child(self.skill_cards_box)
        skills.append(skill_scroll)

        independent.append(gear); independent.append(skills)
        page.append(independent)
        self.page_stack.add_titled(page, "loadouts", "Loadouts")

    def _build_campaign_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True, vexpand=True)
        page.add_css_class("page-container")

        # Top toolbar: Act selector + Step nav + Live Run Timer
        nav = Gtk.Box(spacing=10)
        nav.add_css_class("toolbar-bar")
        act_labels = [f"Act {x['act']} · {x['title']}" for x in self.campaign]
        self.campaign_act = Gtk.DropDown.new_from_strings(act_labels)
        self.campaign_act.set_selected(self.store.data["act"])
        self.campaign_act.set_size_request(230, -1)
        self.campaign_act.connect("notify::selected", self.change_act)
        nav.append(self.campaign_act)

        prev = Gtk.Button(label="← Prev"); prev.connect("clicked", lambda *_: self.move_step(-1)); nav.append(prev)
        nxt = Gtk.Button(label="Next →"); nxt.add_css_class("suggested-action"); nxt.connect("clicked", lambda *_: self.move_step(1)); nav.append(nxt)

        nav.append(Gtk.Box(hexpand=True))

        self.timer_label = Gtk.Label(label="00:00")
        self.timer_label.add_css_class("timer-readout")
        nav.append(self.timer_label)

        self.timer_toggle_btn = Gtk.Button(label="▶ Start")
        self.timer_toggle_btn.set_tooltip_text("Start / pause campaign run timer (auto-splits on new Acts)")
        self.timer_toggle_btn.connect("clicked", self._on_timer_toggle)
        nav.append(self.timer_toggle_btn)

        timer_reset_btn = Gtk.Button(label="↺")
        timer_reset_btn.add_css_class("compact-btn")
        timer_reset_btn.set_tooltip_text("Reset run timer and Act splits")
        timer_reset_btn.connect("clicked", self._on_timer_reset)
        nav.append(timer_reset_btn)
        page.append(nav)

        # Split pane: Interactive Act Checklist (left) + Active Step & Splits & HUD Settings (right)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True, hexpand=True, vexpand=True)

        checklist_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        checklist_card.add_css_class("planner-card")
        checklist_card.set_size_request(260, -1)
        checklist_card.set_margin_end(8)
        cl_kicker = Gtk.Label(label="ACT CHECKLIST")
        cl_kicker.add_css_class("section-kicker"); cl_kicker.set_xalign(0); checklist_card.append(cl_kicker)
        self.campaign_step_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.campaign_step_list.add_css_class("boxed-list")
        self.campaign_step_list.connect("row-selected", self._on_campaign_row_selected)
        cl_scroll = Gtk.ScrolledWindow(vexpand=True)
        cl_scroll.set_child(self.campaign_step_list)
        checklist_card.append(cl_scroll)
        paned.set_start_child(checklist_card)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True, vexpand=True)
        right_box.set_margin_start(6)

        step_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        step_card.add_css_class("planner-card")
        self.campaign_progress = Gtk.ProgressBar()
        self.campaign_progress.set_show_text(False)
        step_card.append(self.campaign_progress)

        self.step_label = Gtk.Label()
        self.step_label.set_xalign(0); self.step_label.add_css_class("title-2"); self.step_label.set_wrap(True)
        step_card.append(self.step_label)

        self.step_tags_box = Gtk.Box(spacing=6)
        self.step_tags_box.set_visible(False)
        step_card.append(self.step_tags_box)

        self.milestone_banner = Gtk.Label(wrap=True, selectable=True)
        self.milestone_banner.set_xalign(0)
        self.milestone_banner.add_css_class("milestone-banner")
        self.milestone_banner.set_visible(False)
        step_card.append(self.milestone_banner)

        self.layout_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.layout_box.add_css_class("layout-callout")
        layout_kicker = Gtk.Label(label="WHERE TO GO (ZONE LAYOUT TELL)")
        layout_kicker.add_css_class("section-kicker"); layout_kicker.set_xalign(0); self.layout_box.append(layout_kicker)
        self.layout_label = Gtk.Label(wrap=True, selectable=True)
        self.layout_label.set_xalign(0); self.layout_label.add_css_class("layout-text")
        self.layout_box.append(self.layout_label)
        step_card.append(self.layout_box)

        self.detail_label = Gtk.Label(wrap=True, selectable=True)
        self.detail_label.set_xalign(0); self.detail_label.set_wrap(True)
        step_card.append(self.detail_label)

        tip_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        tip_box.add_css_class("tip-callout")
        tip_kicker = Gtk.Label(label="NEW PLAYER TIP")
        tip_kicker.add_css_class("section-kicker"); tip_kicker.set_xalign(0); tip_box.append(tip_kicker)
        self.tip_label = Gtk.Label(wrap=True, selectable=True)
        self.tip_label.set_xalign(0); self.tip_label.set_wrap(True)
        tip_box.append(self.tip_label)
        step_card.append(tip_box)
        right_box.append(step_card)

        # Act Survival Target & Level Breakpoint card
        target_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        target_card.add_css_class("context-card")
        target_head = Gtk.Box(spacing=10)
        target_kicker = Gtk.Label(label="ACT SURVIVAL TARGETS & BREAKPOINTS")
        target_kicker.add_css_class("section-kicker"); target_kicker.set_xalign(0); target_kicker.set_hexpand(True)
        target_head.append(target_kicker)
        self.level_status_label = Gtk.Label(label="Level: auto-detected from Client.txt")
        self.level_status_label.add_css_class("planner-subtle")
        target_head.append(self.level_status_label)
        target_card.append(target_head)

        target_pills = Gtk.Box(spacing=8)
        self.act_target_life = Gtk.Label(label="")
        self.act_target_life.add_css_class("stat-pill")
        target_pills.append(self.act_target_life)
        self.act_target_resists = Gtk.Label(label="")
        self.act_target_resists.add_css_class("stat-pill")
        self.act_target_resists.add_css_class("stat-pill-gold")
        target_pills.append(self.act_target_resists)
        target_card.append(target_pills)

        self.act_target_note = Gtk.Label(wrap=True, selectable=True)
        self.act_target_note.set_xalign(0); self.act_target_note.add_css_class("planner-subtle")
        target_card.append(self.act_target_note)
        right_box.append(target_card)

        ctx_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ctx_card.add_css_class("context-card")
        ctx_head = Gtk.Box(spacing=10)
        ctx_kicker = Gtk.Label(label="ACTIVE BUILD CONTEXT")
        ctx_kicker.add_css_class("section-kicker"); ctx_kicker.set_xalign(0); ctx_kicker.set_hexpand(True)
        ctx_head.append(ctx_kicker)
        self.copy_build_regex_btn = Gtk.Button(label="Copy Build Vendor Regex")
        self.copy_build_regex_btn.set_tooltip_text("Copy a vendor search regex matching your build's socket colors + movement speed boots")
        self.copy_build_regex_btn.connect("clicked", self._on_copy_build_vendor_regex)
        self.copy_build_regex_btn.set_visible(False)
        ctx_head.append(self.copy_build_regex_btn)
        ctx_card.append(ctx_head)
        self.context_label = Gtk.Label(wrap=True, selectable=True)
        self.context_label.set_xalign(0); self.context_label.set_wrap(True); self.context_label.add_css_class("planner-subtle")
        ctx_card.append(self.context_label)
        right_box.append(ctx_card)

        # Recorded Act splits strip
        self.splits_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.splits_card.add_css_class("context-card")
        splits_kicker = Gtk.Label(label="ACT SPLITS")
        splits_kicker.add_css_class("section-kicker"); splits_kicker.set_xalign(0); self.splits_card.append(splits_kicker)
        self.splits_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, row_spacing=6, column_spacing=6)
        self.splits_card.append(self.splits_flow)
        self.splits_card.set_visible(False)
        right_box.append(self.splits_card)

        # Collapsible HUD settings so the campaign workspace stays uncluttered
        hud_expander = Gtk.Expander(label="In-Game Overlay & Client.txt Tracking Settings")
        hud_expander.add_css_class("planner-card")
        hud_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hud_card.set_margin_top(10)

        hud_head = Gtk.Box(spacing=10)
        hud_sub = Gtk.Label(label="Shows automatically while PoE 1 is focused. Click-through by default except mini-buttons.")
        hud_sub.add_css_class("planner-subtle"); hud_sub.set_xalign(0); hud_sub.set_wrap(True); hud_sub.set_hexpand(True)
        hud_head.append(hud_sub)
        self.hud_preview_button = Gtk.ToggleButton(label="Preview HUD")
        self.hud_preview_button.set_tooltip_text("Temporarily show the overlay on screen to check placement and opacity")
        self.hud_preview_button.connect("toggled", self._on_hud_preview_toggled)
        hud_head.append(self.hud_preview_button)
        hud_card.append(hud_head)

        row1 = Gtk.Box(spacing=14)
        for text, attr, key in (
            ("Enable overlay", "hud_enabled_switch", "enabled"),
            ("Compact bar", "hud_compact_switch", "compact"),
            ("Mini-buttons", "hud_interactive_switch", "interactive_controls"),
            ("Timer on HUD", "hud_timer_switch", "show_timer"),
        ):
            grp = Gtk.Box(spacing=6)
            lbl = Gtk.Label(label=text); lbl.set_xalign(0); grp.append(lbl)
            sw = Gtk.Switch(active=self.hud_prefs[key], valign=Gtk.Align.CENTER)
            sw.connect("notify::active", self._on_hud_pref_changed)
            setattr(self, attr, sw)
            grp.append(sw)
            row1.append(grp)
        hud_card.append(row1)

        row2 = Gtk.Box(spacing=12)
        pos_lbl = Gtk.Label(label="Position"); pos_lbl.add_css_class("planner-subtle"); row2.append(pos_lbl)
        self.hud_position_combo = Gtk.DropDown.new_from_strings([label for _, label in HUD_POSITIONS])
        pos_keys = [key for key, _ in HUD_POSITIONS]
        self.hud_position_combo.set_selected(pos_keys.index(self.hud_prefs["position"]) if self.hud_prefs["position"] in pos_keys else 0)
        self.hud_position_combo.connect("notify::selected", self._on_hud_pref_changed)
        row2.append(self.hud_position_combo)

        op_lbl = Gtk.Label(label="Opacity"); op_lbl.add_css_class("planner-subtle"); row2.append(op_lbl)
        self.hud_opacity_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 45, 96, 1)
        self.hud_opacity_scale.set_value(round(self.hud_prefs["opacity"] * 100))
        self.hud_opacity_scale.set_draw_value(False)
        self.hud_opacity_scale.set_hexpand(True)
        self.hud_opacity_scale.connect("value-changed", self._on_hud_pref_changed)
        row2.append(self.hud_opacity_scale)
        self.hud_opacity_label = Gtk.Label(label=f"{round(self.hud_prefs['opacity'] * 100)}%")
        self.hud_opacity_label.add_css_class("zoom-readout")
        row2.append(self.hud_opacity_label)
        hud_card.append(row2)

        row3 = Gtk.Box(spacing=10)
        log_lbl = Gtk.Label(label="Auto-advance from Client.txt")
        log_lbl.set_xalign(0); row3.append(log_lbl)
        self.hud_autotrack_switch = Gtk.Switch(active=self.hud_prefs["auto_track_client_log"], valign=Gtk.Align.CENTER)
        self.hud_autotrack_switch.connect("notify::active", self._on_hud_pref_changed)
        row3.append(self.hud_autotrack_switch)
        self.client_log_status = Gtk.Label(label="")
        self.client_log_status.set_xalign(1); self.client_log_status.set_hexpand(True)
        self.client_log_status.set_ellipsize(Pango.EllipsizeMode.START)
        self.client_log_status.add_css_class("planner-subtle")
        row3.append(self.client_log_status)
        hud_card.append(row3)

        hud_expander.set_child(hud_card)
        right_box.append(hud_expander)

        paned.set_end_child(self._scrolled(right_box))
        paned.set_resize_end_child(True)
        paned.set_shrink_end_child(False)
        paned.set_position(300)
        page.append(paned)

        self.page_stack.add_titled(page, "campaign", "Campaign")

    def _build_regex_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True, vexpand=True)
        page.add_css_class("page-container")

        toolbar = Gtk.Box(spacing=8)
        toolbar.add_css_class("toolbar-bar")
        self.regex_search_entry = Gtk.SearchEntry()
        self.regex_search_entry.set_placeholder_text("Filter regex presets…")
        self.regex_search_entry.set_hexpand(True)
        self.regex_search_entry.connect("search-changed", lambda *_: self.refresh_regex_list())
        toolbar.append(self.regex_search_entry)

        self._regex_cat_buttons = {}
        for cat in ("All", "Build", "Leveling", "Vendors", "Gear", "Endgame", "Custom"):
            btn = Gtk.ToggleButton(label=cat)
            btn.add_css_class("nav-pill-btn")
            btn.set_active(cat == "All")
            btn.connect("clicked", self._on_regex_category_clicked, cat)
            toolbar.append(btn)
            self._regex_cat_buttons[cat] = btn

        self.regex_add_toggle = Gtk.ToggleButton(label="+ Add Regex")
        self.regex_add_toggle.add_css_class("suggested-action")
        self.regex_add_toggle.connect("toggled", lambda b: self.regex_add_box.set_visible(b.get_active()))
        toolbar.append(self.regex_add_toggle)
        page.append(toolbar)

        # Collapsible inline custom regex creator
        self.regex_add_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.regex_add_box.add_css_class("planner-card")
        self.regex_add_box.set_visible(False)
        add_row1 = Gtk.Box(spacing=8)
        self.regex_new_title = Gtk.Entry(placeholder_text="Preset title (e.g., B-B-G + Chaos Res)")
        self.regex_new_title.set_hexpand(True)
        add_row1.append(self.regex_new_title)
        self.regex_new_note = Gtk.Entry(placeholder_text="Optional short note")
        self.regex_new_note.set_hexpand(True)
        add_row1.append(self.regex_new_note)
        self.regex_add_box.append(add_row1)

        add_row2 = Gtk.Box(spacing=8)
        self.regex_new_pattern = Gtk.Entry(placeholder_text="PoE search regex (max 250 chars)")
        self.regex_new_pattern.set_hexpand(True)
        add_row2.append(self.regex_new_pattern)
        save_regex_btn = Gtk.Button(label="Save Preset")
        save_regex_btn.add_css_class("suggested-action")
        save_regex_btn.connect("clicked", self._on_save_custom_regex)
        add_row2.append(save_regex_btn)
        self.regex_add_box.append(add_row2)
        page.append(self.regex_add_box)

        self.regex_cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.regex_cards_box)
        page.append(scroll)

        self.page_stack.add_titled(page, "regex", "Regex")

    @staticmethod
    def _scrolled(child):
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(child)
        return scroll

    def build_hud(self):
        if Layer is None:
            self.status.set_text("Layer-shell support is unavailable; the planner works, but the HUD is disabled.")
            if hasattr(self, "hud_preview_button"):
                self.hud_preview_button.set_sensitive(False)
            if hasattr(self, "header_hud_button"):
                self.header_hud_button.set_sensitive(False)
            return
        try:
            if not Layer.is_supported():
                self.status.set_text("This compositor does not support Wayland layer-shell; the planner is still available.")
                if hasattr(self, "hud_preview_button"):
                    self.hud_preview_button.set_sensitive(False)
                if hasattr(self, "header_hud_button"):
                    self.header_hud_button.set_sensitive(False)
                return
            self.hud = Gtk.ApplicationWindow(application=self, title="Campaign HUD")
            self.hud.set_decorated(False)
            self.hud.set_resizable(False)
            Layer.init_for_window(self.hud)
            # Role and placement are set before realize so GTK never maps this
            # window as an ordinary toplevel, even briefly.
            Layer.set_layer(self.hud, Layer.Layer.OVERLAY)
            Layer.set_exclusive_zone(self.hud, 0)
            Layer.set_keyboard_mode(self.hud, Layer.KeyboardMode.NONE)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.add_css_class("hud-panel")
            box.set_size_request(420, -1)
            self.hud.set_child(box)

            top_row = Gtk.Box(spacing=6)
            self.hud_act = Gtk.Label()
            self.hud_act.add_css_class("hud-kicker"); self.hud_act.set_xalign(0); self.hud_act.set_hexpand(True)
            top_row.append(self.hud_act)

            self.hud_timer_label = Gtk.Label(label="00:00")
            self.hud_timer_label.add_css_class("hud-timer")
            self.hud_timer_label.set_visible(bool(self.hud_prefs.get("show_timer", False)))
            top_row.append(self.hud_timer_label)

            self.hud_mode = Gtk.Label(label="")
            self.hud_mode.add_css_class("hud-badge")
            self.hud_mode.set_visible(False)
            top_row.append(self.hud_mode)

            self.hud_mini_controls = Gtk.Box(spacing=3)
            self.hud_mini_controls.add_css_class("hud-mini-controls")
            for label, tip, delta in (("‹", "Previous step", -1), ("›", "Next step", 1)):
                btn = Gtk.Button(label=label)
                btn.add_css_class("hud-mini-btn")
                btn.set_tooltip_text(tip)
                btn.connect("clicked", lambda _b, d=delta: self.move_step(d))
                self.hud_mini_controls.append(btn)
            self.hud_compact_btn = Gtk.Button(label="▾")
            self.hud_compact_btn.add_css_class("hud-mini-btn")
            self.hud_compact_btn.set_tooltip_text("Expand / compact HUD")
            self.hud_compact_btn.connect("clicked", lambda *_: self.toggle_hud_compact())
            self.hud_mini_controls.append(self.hud_compact_btn)
            top_row.append(self.hud_mini_controls)
            box.append(top_row)

            self.hud_progress = Gtk.ProgressBar()
            self.hud_progress.add_css_class("hud-progress")
            self.hud_progress.set_show_text(False)
            box.append(self.hud_progress)

            self.hud_milestone = Gtk.Label()
            self.hud_milestone.add_css_class("hud-milestone"); self.hud_milestone.set_xalign(0)
            self.hud_milestone.set_wrap(True); self.hud_milestone.set_max_width_chars(56)
            self.hud_milestone.set_visible(False)
            box.append(self.hud_milestone)

            self.hud_title = Gtk.Label()
            self.hud_title.add_css_class("hud-title"); self.hud_title.set_xalign(0)
            self.hud_title.set_wrap(True); self.hud_title.set_max_width_chars(52)
            box.append(self.hud_title)

            self.hud_tags_box = Gtk.Box(spacing=4)
            self.hud_tags_box.set_visible(False)
            box.append(self.hud_tags_box)

            self.hud_layout_label = Gtk.Label()
            self.hud_layout_label.add_css_class("hud-layout"); self.hud_layout_label.set_xalign(0)
            self.hud_layout_label.set_wrap(True); self.hud_layout_label.set_max_width_chars(56)
            self.hud_layout_label.set_visible(False)
            box.append(self.hud_layout_label)

            self.hud_summary = Gtk.Label()
            self.hud_summary.add_css_class("hud-summary"); self.hud_summary.set_xalign(0)
            self.hud_summary.set_wrap(True); self.hud_summary.set_max_width_chars(56)
            box.append(self.hud_summary)

            self.hud_expanded_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self.hud_detail = Gtk.Label()
            self.hud_detail.add_css_class("hud-detail"); self.hud_detail.set_xalign(0)
            self.hud_detail.set_wrap(True); self.hud_detail.set_max_width_chars(56)
            self.hud_expanded_box.append(self.hud_detail)

            self.hud_tip = Gtk.Label()
            self.hud_tip.add_css_class("hud-tip"); self.hud_tip.set_xalign(0)
            self.hud_tip.set_wrap(True); self.hud_tip.set_max_width_chars(56)
            self.hud_expanded_box.append(self.hud_tip)

            self.hud_target = Gtk.Label()
            self.hud_target.add_css_class("hud-target"); self.hud_target.set_xalign(0)
            self.hud_target.set_wrap(True); self.hud_target.set_max_width_chars(56)
            self.hud_expanded_box.append(self.hud_target)
            box.append(self.hud_expanded_box)

            self.hud_context = Gtk.Label()
            self.hud_context.add_css_class("hud-context"); self.hud_context.set_xalign(0)
            self.hud_context.set_wrap(True); self.hud_context.set_max_width_chars(56)
            self.hud_context.set_ellipsize(Pango.EllipsizeMode.END)
            self.hud_context.set_lines(2)
            box.append(self.hud_context)

            self.hud.add_css_class("campaign-hud")
            self.hud.connect("realize", lambda *_: self.apply_input_region(self.clickable))
            self.hud.connect("map", lambda *_: GLib.idle_add(self._sync_hud_input_region))
            self.hud.set_visible(False)

            self.hud_css = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), self.hud_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            self.layer_ready = True
            self.apply_hud_layout()
            self.monitors = Gdk.Display.get_default().get_monitors()
            self.monitors.connect("items-changed", self.monitors_changed)
        except Exception as exc:
            self.layer_ready = False
            self.hud = None
            print(f"Wayland layer-shell initialization failed: {exc}", file=sys.stderr)
            self.status.set_text("The planner is available, but this session could not initialize the Wayland HUD.")

    def apply_hud_layout(self):
        if not self.layer_ready or self.hud is None:
            return
        pos = self.hud_prefs.get("position", "top-center")
        top = pos.startswith("top")
        bottom = pos.startswith("bottom")
        left = pos.endswith("left")
        right = pos.endswith("right")
        Layer.set_anchor(self.hud, Layer.Edge.TOP, top)
        Layer.set_anchor(self.hud, Layer.Edge.BOTTOM, bottom)
        Layer.set_anchor(self.hud, Layer.Edge.LEFT, left)
        Layer.set_anchor(self.hud, Layer.Edge.RIGHT, right)
        Layer.set_margin(self.hud, Layer.Edge.TOP, 14 if top else 0)
        Layer.set_margin(self.hud, Layer.Edge.BOTTOM, 24 if bottom else 0)
        Layer.set_margin(self.hud, Layer.Edge.LEFT, 20 if left else 0)
        Layer.set_margin(self.hud, Layer.Edge.RIGHT, 20 if right else 0)

        compact = bool(self.hud_prefs.get("compact", True))
        interactive_controls = bool(self.hud_prefs.get("interactive_controls", True))
        self.hud_summary.set_visible(compact)
        self.hud_expanded_box.set_visible(not compact)
        if hasattr(self, "hud_mini_controls"):
            self.hud_mini_controls.set_visible(self.clickable or interactive_controls)
        if hasattr(self, "hud_timer_label"):
            self.hud_timer_label.set_visible(bool(self.hud_prefs.get("show_timer", False)))
        if hasattr(self, "hud_compact_btn"):
            self.hud_compact_btn.set_label("▾" if compact else "▴")
        if hasattr(self, "hud_mode"):
            if self.clickable:
                self.hud_mode.set_text("INTERACTIVE")
                self.hud_mode.set_visible(True)
            elif not interactive_controls:
                self.hud_mode.set_text("LOCKED")
                self.hud_mode.set_visible(True)
            else:
                self.hud_mode.set_visible(False)

        alpha = min(0.96, max(0.45, float(self.hud_prefs.get("opacity", 0.88))))
        border_alpha = min(0.95, alpha + 0.06)
        css_text = f"""
            .campaign-hud {{ background: transparent; }}
            .hud-panel {{
                background: rgba(11, 16, 23, {alpha:.2f});
                color: #e9eff7;
                border: 1px solid rgba(199, 144, 64, {border_alpha * 0.55:.2f});
                border-radius: 10px;
                padding: 10px 14px;
            }}
            .hud-kicker {{ font-size: 10.5px; font-weight: 700; color: #ecc06c; letter-spacing: 0.7px; }}
            .hud-timer {{
                font-family: monospace;
                font-size: 10.5px;
                font-weight: 700;
                color: #f4d284;
                background: rgba(25, 36, 51, 0.85);
                border-radius: 5px;
                padding: 2px 6px;
            }}
            .hud-badge {{
                font-size: 9.5px;
                font-weight: 700;
                color: #93a7bd;
                background: rgba(25, 36, 51, 0.85);
                border-radius: 5px;
                padding: 2px 6px;
            }}
            .hud-title {{ font-size: 14px; font-weight: 700; color: #f6efe0; }}
            .hud-layout {{ font-size: 11.5px; font-weight: 600; color: #7be3f0; }}
            .hud-summary {{ font-size: 12px; color: #c7d5e5; }}
            .hud-detail {{ font-size: 12px; color: #d5e0ec; }}
            .hud-tip {{ font-size: 11.5px; color: #ecc06c; }}
            .hud-target {{ font-size: 11px; font-weight: 600; color: #9cd6b2; }}
            .hud-milestone {{
                font-size: 11.5px;
                font-weight: 700;
                color: #8bf2b5;
                background: rgba(20, 56, 36, 0.90);
                border: 1px solid rgba(64, 178, 112, 0.75);
                border-radius: 6px;
                padding: 4px 8px;
            }}
            .hud-tag {{
                font-size: 9.5px;
                font-weight: 700;
                border-radius: 9px;
                padding: 1px 6px;
            }}
            .hud-context {{ font-size: 11px; color: #8da2b8; }}
            .hud-progress trough {{ background: rgba(29, 42, 58, 0.85); min-height: 4px; border-radius: 4px; }}
            .hud-progress progress {{ background: #d9a44b; min-height: 4px; border-radius: 4px; }}
            .hud-mini-btn {{
                min-height: 20px;
                min-width: 22px;
                padding: 0 5px;
                font-size: 12px;
                font-weight: 700;
                background: rgba(26, 38, 54, 0.85);
                color: #d9e4f1;
                border: 1px solid rgba(90, 114, 140, 0.55);
                border-radius: 5px;
            }}
            .hud-mini-btn:hover {{
                background: rgba(45, 63, 86, 0.95);
                border-color: rgba(217, 164, 75, 0.85);
                color: #f8e5b8;
            }}
        """
        if self.hud_css is not None:
            self.hud_css.load_from_data(css_text.encode("utf-8"))
        GLib.idle_add(self._sync_hud_input_region)

    def _on_hud_pref_changed(self, *_):
        if self._updating_hud_controls or not hasattr(self, "hud_enabled_switch"):
            return
        pos_keys = [key for key, _ in HUD_POSITIONS]
        pos_idx = self.hud_position_combo.get_selected()
        self.hud_prefs["enabled"] = self.hud_enabled_switch.get_active()
        self.hud_prefs["compact"] = self.hud_compact_switch.get_active()
        if hasattr(self, "hud_interactive_switch"):
            self.hud_prefs["interactive_controls"] = self.hud_interactive_switch.get_active()
        if hasattr(self, "hud_timer_switch"):
            self.hud_prefs["show_timer"] = self.hud_timer_switch.get_active()
        if 0 <= pos_idx < len(pos_keys):
            self.hud_prefs["position"] = pos_keys[pos_idx]
        opacity_pct = round(self.hud_opacity_scale.get_value())
        self.hud_prefs["opacity"] = opacity_pct / 100.0
        self.hud_opacity_label.set_text(f"{opacity_pct}%")
        self.hud_prefs["auto_track_client_log"] = self.hud_autotrack_switch.get_active()
        save_hud_prefs(self.hud_prefs)
        self._init_client_log_tailer()
        self.apply_hud_layout()
        self.present_hud()

    def _on_hud_preview_toggled(self, button):
        if self._updating_hud_controls:
            return
        self.hud_preview = button.get_active()
        self._updating_hud_controls = True
        try:
            for btn_attr in ("hud_preview_button", "header_hud_button"):
                other = getattr(self, btn_attr, None)
                if other is not None and other is not button and other.get_active() != self.hud_preview:
                    other.set_active(self.hud_preview)
        finally:
            self._updating_hud_controls = False
        if self.hud_preview and self.hud is not None and self.layer_ready:
            self.apply_hud_layout()
            self.hud.set_visible(True)
            if self.hud.get_surface() is not None:
                self.apply_input_region(self.clickable)
        else:
            self.present_hud()

    def toggle_hud_compact(self):
        self.hud_prefs["compact"] = not self.hud_prefs.get("compact", True)
        save_hud_prefs(self.hud_prefs)
        if hasattr(self, "hud_compact_switch"):
            self._updating_hud_controls = True
            try:
                self.hud_compact_switch.set_active(self.hud_prefs["compact"])
            finally:
                self._updating_hud_controls = False
        self.apply_hud_layout()

    def _init_client_log_tailer(self):
        if not self.hud_prefs.get("auto_track_client_log", True):
            self.client_log_tailer = None
            if hasattr(self, "client_log_status"):
                self.client_log_status.set_text("Auto-tracking paused")
            return
        found = discover_client_log(self.hud_prefs.get("client_log_path") or None)
        if found is None:
            self.client_log_tailer = None
            if hasattr(self, "client_log_status"):
                self.client_log_status.set_text("Client.txt not found (manual / hotkey stepping active)")
            return
        if self.client_log_tailer is None or self.client_log_tailer.path != found:
            self.client_log_tailer = ClientLogTailer(found)
        if hasattr(self, "client_log_status"):
            self.client_log_status.set_text(f"Watching {found}")

    def poll_client_log(self):
        if not self.hud_prefs.get("auto_track_client_log", True):
            return GLib.SOURCE_CONTINUE
        if self.client_log_tailer is None:
            self._client_log_discovery_ticks += 1
            if self._client_log_discovery_ticks >= 10:
                self._client_log_discovery_ticks = 0
                self._init_client_log_tailer()
            return GLib.SOURCE_CONTINUE
        zones, level_ups = self.client_log_tailer.poll_events()
        for zone in zones:
            matched = match_campaign_step(
                self.campaign, self.store.data["act"], self.store.data["step"], zone
            )
            if matched is not None and matched != (self.store.data["act"], self.store.data["step"]):
                prev_act = self.store.data["act"]
                self.store.data["act"], self.store.data["step"] = matched
                if matched[0] > prev_act:
                    self.run_timer.record_act_transition(prev_act, matched[0])
                    self._refresh_splits_ui()
                if self.campaign_act is not None:
                    self.campaign_act.set_selected(matched[0])
                self.refresh_campaign()
                if self.status is not None:
                    self.status.set_text(f"Entered {zone} · advanced to Act {matched[0] + 1}, Step {matched[1] + 1}")
        for char_name, level in level_ups:
            self._handle_level_up(char_name, level)
        return GLib.SOURCE_CONTINUE

    def _handle_level_up(self, char_name: str, level: int):
        self.detected_char = char_name
        self.detected_level = level
        nxt = next_level_milestone(level)
        if hasattr(self, "level_status_label"):
            next_txt = f" · Next: Lv {nxt[0]}" if nxt else ""
            self.level_status_label.set_text(f"{char_name} · Lv {level}{next_txt}")
        cue = level_milestone_cue(level, self.current_build(), self.store.data["act"] + 1)
        if cue:
            banner_text = f"★ {cue}"
            if hasattr(self, "milestone_banner"):
                self.milestone_banner.set_text(banner_text)
                self.milestone_banner.set_visible(True)
            if hasattr(self, "hud_milestone"):
                self.hud_milestone.set_text(banner_text)
                self.hud_milestone.set_visible(True)
                GLib.idle_add(self._sync_hud_input_region)
            if self._milestone_timer_id is not None:
                GLib.source_remove(self._milestone_timer_id)
            self._milestone_timer_id = GLib.timeout_add(25000, self._clear_hud_milestone)
            if self.status is not None:
                self.status.set_text(banner_text)

    def _clear_hud_milestone(self):
        self._milestone_timer_id = None
        if hasattr(self, "hud_milestone"):
            self.hud_milestone.set_visible(False)
            GLib.idle_add(self._sync_hud_input_region)
        return GLib.SOURCE_REMOVE

    def _sync_hud_input_region(self):
        if self.hud is not None and self.hud.get_visible() and self.hud.get_surface() is not None:
            self.apply_input_region(self.clickable)
        return GLib.SOURCE_REMOVE

    def apply_input_region(self, clickable: bool) -> bool:
        if not self.layer_ready or self.hud is None:
            return False
        surface = self.hud.get_surface()
        if surface is None:
            return False
        try:
            if clickable:
                surface.set_input_region(None)  # Entire mapped HUD accepts pointer input.
                return True
            import cairo
            if self.hud_prefs.get("interactive_controls", True) and hasattr(self, "hud_mini_controls"):
                ok, rect = self.hud_mini_controls.compute_bounds(self.hud)
                if ok and rect.size.width > 1 and rect.size.height > 1:
                    region = cairo.Region(cairo.RectangleInt(
                        max(0, int(rect.origin.x) - 2),
                        max(0, int(rect.origin.y) - 2),
                        int(rect.size.width) + 4,
                        int(rect.size.height) + 4,
                    ))
                    surface.set_input_region(region)
                    return True
            surface.set_input_region(cairo.Region())
            return True
        except Exception as exc:
            print(f"Could not update HUD pointer region: {exc}", file=sys.stderr)
            try:
                import cairo
                surface.set_input_region(cairo.Region())
            except Exception as reset_exc:
                print(f"Could not fail closed for HUD pointer input: {reset_exc}", file=sys.stderr)
            return False

    def _set_import_feedback(self, message: str):
        self.status.set_text(message)
        if self.import_status_label is not None:
            self.import_status_label.set_text(message)
            self.import_status_label.set_visible(bool(message))

    def import_build(self, *_):
        buffer = self.code_entry.get_buffer()
        raw = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
        if looks_like_share_url(raw):
            self._begin_pobbin_import(raw)
            return
        try:
            build = parse_build(raw)
        except PobImportError as exc:
            self._set_import_feedback(str(exc))
            return
        self._save_imported_build(build, buffer)

    def _begin_pobbin_import(self, raw: str):
        if self.import_pending:
            return
        self.import_pending = True
        self.import_request_id += 1
        request_id = self.import_request_id
        self.import_button.set_sensitive(False)
        self._set_import_feedback("Fetching the public build from pobb.in…")

        def worker():
            try:
                result = download_pobbin_build(raw)
                error = None
            except PobImportError as exc:
                result, error = None, str(exc)
            except Exception as exc:
                result, error = None, f"Could not import this pobb.in link ({exc}). The input is still available to retry."
            GLib.idle_add(self._finish_pobbin_import, request_id, raw, result, error)

        threading.Thread(target=worker, name="pobb.in-import", daemon=True).start()

    def _finish_pobbin_import(self, request_id: int, raw: str, build: dict | None, error: str | None):
        if request_id != self.import_request_id:
            return GLib.SOURCE_REMOVE
        self.import_pending = False
        self.import_button.set_sensitive(True)
        buffer = self.code_entry.get_buffer()
        current = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
        if current != raw:
            self._set_import_feedback("The input changed while the link was loading; no build was imported. Review the current input and retry.")
            return GLib.SOURCE_REMOVE
        if error:
            self._set_import_feedback(error)
            return GLib.SOURCE_REMOVE
        self._save_imported_build(build, buffer)
        return GLib.SOURCE_REMOVE

    def _save_imported_build(self, build: dict, buffer):
        if len(self.store.data["builds"]) >= MAX_SAVED_BUILDS:
            self._set_import_feedback(f"You can keep up to {MAX_SAVED_BUILDS} imported builds. Remove an older saved build before importing another.")
            return
        build["name"] = build.get("ascendancy") or build.get("class") or f"Build {len(self.store.data['builds']) + 1}"
        candidate = dict(self.store.data)
        candidate["builds"] = [*self.store.data["builds"], build]
        candidate["selected_build"] = len(candidate["builds"]) - 1
        try:
            self.store.save(candidate)
        except StateError as exc:
            self._set_import_feedback(str(exc))
            return
        self.store.data = candidate
        buffer.set_text("")
        self.populate_build_model()
        self.status.set_text(f"Imported {build['name']} · saved on this computer.")
        if self.import_dialog is not None:
            self.import_dialog.close()
        self.refresh_build()

    def populate_build_model(self):
        labels = ["Campaign only"] + [b.get("name") or f"Build {i + 1}" for i, b in enumerate(self.store.data["builds"])]
        self._updating_build_combo = True
        try:
            self.build_combo.set_model(Gtk.StringList.new(labels))
            self.build_combo.set_selected(self.store.data["selected_build"] + 1)
        finally:
            self._updating_build_combo = False

    def current_build(self):
        index = self.store.data["selected_build"]
        builds = self.store.data["builds"]
        return builds[index] if 0 <= index < len(builds) else None

    def change_build(self, dropdown, _pspec):
        if self._updating_build_combo:
            return
        selected = dropdown.get_selected()
        self.store.data["selected_build"] = selected - 1 if selected != Gtk.INVALID_LIST_POSITION else -1
        self.persist_state()
        self.refresh_build()

    def remove_current_build(self, *_):
        index = self.store.data["selected_build"]
        builds = self.store.data["builds"]
        if not (0 <= index < len(builds)):
            return
        removed_name = builds[index].get("name") or f"Build {index + 1}"
        remaining = builds[:index] + builds[index + 1:]
        next_selected = min(index, len(remaining) - 1) if remaining else -1
        candidate = {**self.store.data, "builds": remaining, "selected_build": next_selected}
        try:
            self.store.save(candidate)
        except StateError as exc:
            self.status.set_text(str(exc))
            return
        self.store.data = candidate
        self.populate_build_model()
        self.status.set_text(f"Removed {removed_name}.")
        self.refresh_build()

    def persist_state(self):
        try:
            self.store.save()
        except StateError as exc:
            if self.status is not None:
                self.status.set_text(str(exc))
            print(f"Planner state was not changed on disk: {exc}", file=sys.stderr)
            return False
        return True

    @staticmethod
    def _clear_box(box: Gtk.Box):
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def refresh_build(self, *_):
        if self.notes_view is None:
            return
        build = self.current_build()
        if hasattr(self, "remove_build_button"):
            self.remove_build_button.set_sensitive(build is not None)
        if hasattr(self, "overview_pills"):
            self._clear_box(self.overview_pills)
        if build is None:
            self.profile_label.set_text("Wraeclast Companion")
            self.overview_meta.set_text("Import a public pobb.in link, share code, or XML — or jump straight into the Act 1–10 Campaign guide.")
            notes = "No build notes yet. Click '+ Import Build' (or press Ctrl+I) to load a Path of Building guide."
            if hasattr(self, "overview_pills"):
                for text, gold in (("Path of Exile 1", True), ("Act 1–10 Guide Ready", False), ("Wayland HUD", False)):
                    lbl = Gtk.Label(label=text)
                    lbl.add_css_class("stat-pill")
                    if gold:
                        lbl.add_css_class("stat-pill-gold")
                    self.overview_pills.append(lbl)
        else:
            hero = " · ".join(value for value in (build.get("class"), build.get("ascendancy")) if value)
            if build.get("level"):
                hero = f"{hero} · Level {build['level']}" if hero else f"Level {build['level']}"
            self.profile_label.set_text(build.get("name") or hero or "Imported build")
            spec_count = len(build.get("tree_specs", []))
            skill_count = len(build.get("skill_sets", []))
            gear_count = len(build.get("item_sets", []))
            selected_spec_id = build.get("selected_tree_spec", build.get("active_tree_spec", ""))
            active_spec = next((s for s in build.get("tree_specs", []) if s.get("id") == selected_spec_id),
                               (build.get("tree_specs") or [None])[0])
            node_count = len(active_spec.get("nodes", [])) if active_spec else 0
            self.overview_meta.set_text("Read-only Path of Building desk · passive tree, gear, gem links, and notes synced to your campaign HUD.")
            if hasattr(self, "overview_pills"):
                pills = []
                if build.get("ascendancy"):
                    pills.append((build["ascendancy"], True))
                elif build.get("class"):
                    pills.append((build["class"], True))
                if build.get("level"):
                    pills.append((f"Lv {build['level']}", False))
                if node_count:
                    pills.append((f"{node_count} passives", False))
                if spec_count:
                    pills.append((f"{spec_count} tree" + ("s" if spec_count != 1 else ""), False))
                if gear_count:
                    pills.append((f"{gear_count} gear set" + ("s" if gear_count != 1 else ""), False))
                if skill_count:
                    pills.append((f"{skill_count} skill set" + ("s" if skill_count != 1 else ""), False))
                for text, gold in pills:
                    lbl = Gtk.Label(label=text)
                    lbl.add_css_class("stat-pill")
                    if gold:
                        lbl.add_css_class("stat-pill-gold")
                    self.overview_pills.append(lbl)
            notes = build.get("notes") or "This build has no notes."
        self.render_notes(notes)
        self.populate_tree_specs(build)
        self.populate_item_sets(build)
        self.populate_skill_sets(build)
        self.populate_linked_loadouts(build)
        self.refresh_loadout_views(build)
        self.refresh_passive_tree(build)
        self.refresh_regex_list()
        self.update_context()

    def render_notes(self, notes: str):
        buffer = self.notes_view.get_buffer()
        buffer.set_text("")
        for text, color in format_note_spans(notes):
            tag = self.note_tag(color, buffer)
            buffer.insert_with_tags(buffer.get_end_iter(), text, tag)

    def note_tag(self, color: str, buffer):
        default = self.note_tags.get("default")
        if default is None:
            default = buffer.create_tag("pob-note-default", foreground="#e8edf4")
            self.note_tags["default"] = default
        tag = self.note_tags.get(color)
        if tag is not None:
            return tag
        if len(self.note_tags) >= MAX_COLOR_TAGS:
            return default
        tag = buffer.create_tag(f"pob-note-{len(self.note_tags)}", foreground=color)
        self.note_tags[color] = tag
        return tag

    def _set_dropdown(self, dropdown, labels, selected, updating_attr):
        setattr(self, updating_attr, True)
        try:
            dropdown.set_model(Gtk.StringList.new(labels or ["No data available"]))
            dropdown.set_selected(max(0, selected))
        finally:
            setattr(self, updating_attr, False)

    @staticmethod
    def _selected_index(items, key, value):
        return next((index for index, item in enumerate(items) if item.get(key) == value), 0)

    def populate_tree_specs(self, build):
        specs = build.get("tree_specs", []) if build else []
        selected_id = build.get("selected_tree_spec", build.get("active_tree_spec", "")) if build else ""
        labels = [f"{item['title']}  ·  {item.get('tree_version') or 'version unknown'}" for item in specs]
        selected_index = self._selected_index(specs, "id", selected_id)
        self._set_dropdown(self.tree_variant_combo, labels, selected_index, "_updating_tree_combo")
        self.tree_variant_combo.set_tooltip_text(labels[selected_index] if labels else None)
        self._populate_tree_diff_specs(build)

    def _populate_tree_diff_specs(self, build):
        if not hasattr(self, "tree_diff_combo"):
            return
        specs = build.get("tree_specs", []) if build else []
        selected_id = build.get("selected_tree_spec", build.get("active_tree_spec", "")) if build else ""
        others = [item for item in specs if item.get("id") != selected_id]
        self.tree_diff_specs = others
        labels = ["Diff: Off"] + [f"vs {item['title']}" for item in others]
        self._set_dropdown(self.tree_diff_combo, labels, 0, "_updating_diff_combo")
        self.tree_diff_combo.set_sensitive(bool(others))
        self._apply_tree_diff()

    def _on_tree_diff_changed(self, _dropdown, _pspec):
        if self._updating_diff_combo:
            return
        self._apply_tree_diff()

    def _apply_tree_diff(self):
        if not hasattr(self, "tree_diff_combo"):
            return
        idx = self.tree_diff_combo.get_selected()
        build = self.current_build()
        if not build or idx <= 0 or idx - 1 >= len(self.tree_diff_specs):
            self.tree_canvas.set_diff_nodes(set(), set(), "")
            self.tree_diff_added_badge.set_visible(False)
            self.tree_diff_removed_badge.set_visible(False)
            return
        specs = build.get("tree_specs", [])
        selected_id = build.get("selected_tree_spec", build.get("active_tree_spec", ""))
        cur_spec = next((s for s in specs if s.get("id") == selected_id), specs[0] if specs else None)
        cmp_spec = self.tree_diff_specs[idx - 1]
        if cur_spec is None:
            return
        added, removed = compute_tree_diff(cur_spec.get("nodes", []), cmp_spec.get("nodes", []))
        self.tree_canvas.set_diff_nodes(added, removed, cmp_spec.get("title", ""))
        self.tree_diff_added_badge.set_text(f"+{len(added)}")
        self.tree_diff_added_badge.set_visible(bool(added))
        self.tree_diff_removed_badge.set_text(f"−{len(removed)}")
        self.tree_diff_removed_badge.set_visible(bool(removed))

    def populate_item_sets(self, build):
        sets = build.get("item_sets", []) if build else []
        selected = build.get("selected_item_set", build.get("active_item_set", "")) if build else ""
        self._set_dropdown(self.item_set_combo, [item["title"] for item in sets],
                           self._selected_index(sets, "id", selected), "_updating_item_combo")

    def populate_skill_sets(self, build):
        sets = build.get("skill_sets", []) if build else []
        selected = build.get("selected_skill_set", build.get("active_skill_set", "")) if build else ""
        self._set_dropdown(self.skill_set_combo, [item["title"] for item in sets],
                           self._selected_index(sets, "id", selected), "_updating_skill_combo")

    def populate_linked_loadouts(self, build):
        links = build.get("linked_loadouts", []) if build else []
        labels = ["Apply a linked loadout…"] + [f"{item['title']}" + (f"  ·  {{{item['link_id']}}}" if item.get("link_id") else "") for item in links]
        index = 0
        if build:
            index = next((i + 1 for i, item in enumerate(links)
                          if item.get("tree_spec_id") == build.get("selected_tree_spec", build.get("active_tree_spec", ""))
                          and item.get("item_set_id") == build.get("selected_item_set", build.get("active_item_set", ""))
                          and item.get("skill_set_id") == build.get("selected_skill_set", build.get("active_skill_set", ""))), 0)
        self._set_dropdown(self.linked_loadout_combo, labels, index, "_updating_loadout_combo")
        self.linked_loadout_combo.set_sensitive(bool(links))

    def _on_loadout_filter_changed(self, entry):
        self._loadout_filter_query = entry.get_text().strip().lower()
        self.refresh_loadout_views(self.current_build())

    def _render_item_text(self, text: str):
        if not hasattr(self, "item_card_container"):
            return
        self._clear_box(self.item_card_container)
        raw = (text or "").strip()
        if not raw or raw.startswith("Choose an equipped item"):
            placeholder = Gtk.Label(label=raw or "Choose an equipped item to inspect its PoB details.")
            placeholder.add_css_class("planner-subtle")
            placeholder.set_xalign(0); placeholder.set_wrap(True)
            placeholder.set_margin_top(10); placeholder.set_margin_start(6)
            self.item_card_container.append(placeholder)
            return

        parsed = parse_item_card(raw)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("poe-item-card")

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header.add_css_class(f"poe-item-header-{parsed['rarity']}")
        name_lbl = Gtk.Label(label=parsed["name"], wrap=True, selectable=True)
        name_lbl.add_css_class("poe-item-name"); name_lbl.set_xalign(0.5)
        header.append(name_lbl)
        if parsed["base"]:
            base_lbl = Gtk.Label(label=parsed["base"], wrap=True, selectable=True)
            base_lbl.add_css_class("poe-item-base"); base_lbl.set_xalign(0.5)
            header.append(base_lbl)
        card.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.add_css_class("poe-item-body")
        if parsed["meta"]:
            meta_lbl = Gtk.Label(label="\n".join(parsed["meta"]), wrap=True, selectable=True)
            meta_lbl.add_css_class("poe-item-meta"); meta_lbl.set_xalign(0)
            body.append(meta_lbl)
        for sec_idx, section in enumerate(parsed["sections"]):
            if sec_idx > 0 or parsed["meta"]:
                sep = Gtk.Box()
                sep.add_css_class("poe-item-sep")
                body.append(sep)
            sec_lbl = Gtk.Label(label="\n".join(section), wrap=True, selectable=True)
            sec_lbl.add_css_class("poe-item-mod"); sec_lbl.set_xalign(0)
            body.append(sec_lbl)
        card.append(body)
        self.item_card_container.append(card)

    def _render_skill_groups(self, title: str, groups: list):
        if not hasattr(self, "skill_cards_box"):
            return
        self._clear_box(self.skill_cards_box)
        query = self._loadout_filter_query
        shown = 0
        for group in groups:
            label = group.get("label") or "Skill group"
            gems = group.get("gems") or []
            if query:
                if query not in label.lower() and not any(query in g.lower() for g in gems):
                    continue
            shown += 1
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            card.add_css_class("gem-group-card")

            head = Gtk.Box(spacing=8)
            g_lbl = Gtk.Label(label=label, xalign=0, hexpand=True, wrap=True)
            g_lbl.add_css_class("gem-group-title")
            head.append(g_lbl)

            # Visual linked socket chain
            sockets_row = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
            for idx, gem in enumerate(gems[:6]):
                if idx > 0:
                    link = Gtk.Box(valign=Gtk.Align.CENTER)
                    link.add_css_class("socket-link-bar")
                    sockets_row.append(link)
                dot = Gtk.Box(valign=Gtk.Align.CENTER)
                dot.add_css_class("socket-dot")
                dot.add_css_class(f"socket-{gem_socket_color(gem)}")
                dot.set_tooltip_text(gem)
                sockets_row.append(dot)
            head.append(sockets_row)
            card.append(head)

            for gem in gems:
                row = Gtk.Box(spacing=8)
                dot = Gtk.Box(valign=Gtk.Align.CENTER)
                dot.add_css_class("socket-dot")
                dot.add_css_class(f"socket-{gem_socket_color(gem)}")
                row.append(dot)
                gem_lbl = Gtk.Label(label=gem, xalign=0, wrap=True, selectable=True, hexpand=True)
                gem_lbl.add_css_class("gem-row-support" if is_support_gem(gem) else "gem-row-active")
                row.append(gem_lbl)
                card.append(row)

            self.skill_cards_box.append(card)

        if shown == 0:
            empty = Gtk.Label(label="No matching gem groups." if query else "No enabled gems in this set.")
            empty.add_css_class("planner-subtle")
            empty.set_xalign(0); empty.set_margin_top(8); empty.set_margin_start(4)
            self.skill_cards_box.append(empty)

    def refresh_loadout_views(self, build):
        self.item_list.remove_all()
        self._render_item_text("Choose an equipped item to inspect its PoB description.")
        query = self._loadout_filter_query
        if build:
            sets = build.get("item_sets", [])
            set_id = build.get("selected_item_set", build.get("active_item_set", ""))
            item_set = next((item for item in sets if item.get("id") == set_id), None)
            if item_set:
                item_texts = build.get("items", {})
                first_row = None
                for slot in item_set.get("slots", []):
                    if slot.get("item_id") == "0":
                        continue
                    item_text = item_texts.get(slot["item_id"], "Item details were not saved.")
                    slot_name = slot["name"]
                    item_title = slot.get("title") or "Saved item"
                    if query and query not in slot_name.lower() and query not in item_title.lower() and query not in item_text.lower():
                        continue
                    row_box = Gtk.Box(spacing=10)
                    row_box.set_margin_top(7); row_box.set_margin_bottom(7)
                    row_box.set_margin_start(10); row_box.set_margin_end(10)
                    slot_lbl = Gtk.Label(label=slot_name)
                    slot_lbl.add_css_class("planner-subtle")
                    slot_lbl.set_xalign(0); slot_lbl.set_size_request(92, -1)
                    name_lbl = Gtk.Label(label=item_title, wrap=True, hexpand=True)
                    name_lbl.set_xalign(0)
                    row_box.append(slot_lbl); row_box.append(name_lbl)
                    row = Gtk.ListBoxRow(child=row_box)
                    row.item_text = item_text
                    self.item_list.append(row)
                    first_row = first_row or row
                if first_row is not None:
                    self.item_list.select_row(first_row)
        skill_set = None
        if build:
            skill_set = next((item for item in build.get("skill_sets", [])
                              if item.get("id") == build.get("selected_skill_set", build.get("active_skill_set", ""))), None)
        groups = skill_set.get("groups", []) if skill_set else build.get("groups", []) if build else []
        title = skill_set.get("title", "Skill groups") if skill_set else "Enabled gem groups"
        self._render_skill_groups(title, groups)

    def refresh_passive_tree(self, build):
        self.tree_load_id += 1
        generation = self.tree_load_id
        self.tree_graph = None
        self.tree_canvas.set_graph(None)
        self.tree_node_title.set_text("Select a passive node")
        self.tree_node_flags.set_text("")
        self.tree_node_stats.set_text("Click any node to inspect its stats.")
        if build is None:
            self.tree_status.set_text("Choose an imported build to view its passive tree.")
            return
        specs = build.get("tree_specs", [])
        if not specs:
            if build.get("variants"):
                self.tree_status.set_text("This saved import has tree titles but no allocated node IDs. Re-import the PoB build to enable its passive tree.")
            else:
                self.tree_status.set_text("This build contains no passive tree specs.")
            return
        spec_id = build.get("selected_tree_spec", build.get("active_tree_spec", ""))
        spec = next((item for item in specs if item.get("id") == spec_id), specs[0])
        version = spec.get("tree_version", "")
        allocated = spec.get("nodes", [])
        self.tree_status.set_text(f"Loading saved tree {version or 'with unknown version'} · {len(allocated)} allocated node IDs…")

        def worker():
            try:
                graph, error = prepare_tree_graph(version, allocated,
                                                  build.get("class", ""), build.get("ascendancy", ""),
                                                  spec.get("mastery_effects", ""), spec.get("class_id"),
                                                  spec.get("ascend_class_id"), spec.get("secondary_ascend_class_id")), None
            except (TreeDataError, OSError, ValueError) as exc:
                graph, error = None, str(exc)
            GLib.idle_add(self._install_tree_graph, generation, spec_id, graph, error)

        threading.Thread(target=worker, name="passive-tree-data", daemon=True).start()

    def _on_tree_zoom_changed(self, zoom: float):
        if hasattr(self, "tree_zoom_label"):
            self.tree_zoom_label.set_text(f"{round(zoom * 100)}%")

    def _on_toggle_all_ascendancies(self, button):
        self.tree_canvas.set_show_all_ascendancies(button.get_active())
        if hasattr(self, "tree_search_entry") and self.tree_search_entry.get_text().strip():
            self._on_tree_search_changed(self.tree_search_entry)

    def _on_tree_search_changed(self, entry):
        query = entry.get_text().strip()
        self.tree_search_matches = self.tree_canvas.set_search_query(query)
        self.tree_search_index = -1
        if query:
            count = len(self.tree_search_matches)
            self.tree_search_count.set_text(f"{count} match" if count == 1 else f"{count} matches")
            self.tree_search_count.set_visible(True)
        else:
            self.tree_search_count.set_text("")
            self.tree_search_count.set_visible(False)

    def _on_tree_search_next(self, _entry):
        if not self.tree_search_matches:
            return
        self.tree_search_index = (self.tree_search_index + 1) % len(self.tree_search_matches)
        node_id = self.tree_search_matches[self.tree_search_index]
        self.tree_canvas.focus_node(node_id)
        self.tree_search_count.set_text(f"{self.tree_search_index + 1} / {len(self.tree_search_matches)}")

    def _render_tree_summary(self):
        graph = self.tree_graph
        if not graph:
            self.tree_node_title.set_text("Build summary")
            self.tree_node_flags.set_text("Choose a build to inspect passive allocations.")
            self.tree_node_stats.set_text("")
            return
        allocated = [n for n in graph["nodes"].values() if n.get("allocated")]
        keystones = sorted(n["name"] for n in allocated if n.get("keystone"))
        notables = sorted(n["name"] for n in allocated if n.get("notable") and not n.get("ascendancy"))
        asc_notables = sorted(n["name"] for n in allocated if n.get("ascendancy") and (n.get("notable") or n.get("keystone")))
        masteries = [n for n in allocated if n.get("mastery")]
        jewels = sum(1 for n in allocated if n.get("jewel"))
        self.tree_node_title.set_text("Build summary")
        self.tree_node_flags.set_text(f"{len(allocated)} allocated · {len(keystones)} keystones · {len(notables)} notables · {len(masteries)} masteries · {jewels} jewels")
        lines = []
        if keystones:
            lines.append("Keystones:\n" + "\n".join(f"  • {name}" for name in keystones))
        if asc_notables:
            lines.append("Ascendancy:\n" + "\n".join(f"  • {name}" for name in asc_notables))
        if notables:
            shown = notables[:14]
            block = "Notables:\n" + "\n".join(f"  • {name}" for name in shown)
            if len(notables) > len(shown):
                block += f"\n  … and {len(notables) - len(shown)} more"
            lines.append(block)
        if not lines:
            lines.append("Hover any node for a tooltip, or click a node to pin its full stats here.")
        self.tree_node_stats.set_text("\n\n".join(lines))

    def _install_tree_graph(self, generation, spec_id, graph, error):
        if generation != self.tree_load_id:
            return GLib.SOURCE_REMOVE
        build = self.current_build()
        if build is None or build.get("selected_tree_spec", build.get("active_tree_spec", "")) != spec_id:
            return GLib.SOURCE_REMOVE
        if error:
            self.tree_status.set_text(error)
            return GLib.SOURCE_REMOVE
        self.tree_graph = graph
        self.tree_canvas.set_graph(graph)
        self._apply_tree_diff()
        self.tree_retry_button.set_sensitive(False)
        self.tree_retry_button.set_visible(False)
        if hasattr(self, "tree_search_entry") and self.tree_search_entry.get_text().strip():
            self._on_tree_search_changed(self.tree_search_entry)
        self._render_tree_summary()
        unknown = len(graph["unknown_allocations"])
        self.tree_status.set_text(f"PoE 1 passive tree {graph['version']} · loading version-matched official artwork · {graph['allocated_count']} allocated nodes"
                                  + (f" · {unknown} IDs absent from this version" if unknown else "")
                                  + " · pinned GGG export")

        def art_worker():
            try:
                cache_base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
                images, errors = load_tree_atlases(graph, cache_base / "wraeclast-companion" / "passive-tree")
            except Exception as exc:
                images, errors = {}, [f"Artwork loading failed: {exc}"]
            GLib.idle_add(self._install_tree_art, generation, graph, images, errors)

        threading.Thread(target=art_worker, name="passive-tree-artwork", daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _install_tree_art(self, generation, graph, images, errors):
        if generation != self.tree_load_id or graph is not self.tree_graph:
            return GLib.SOURCE_REMOVE
        render_errors = self.tree_canvas.set_artwork(graph, images, errors)
        warnings = self.tree_canvas.art_warnings
        missing = render_errors
        self.tree_retry_button.set_sensitive(bool(missing))
        self.tree_retry_button.set_visible(bool(missing))
        loaded = len(self.tree_canvas._atlases)
        required = len({s["filename"] for s in graph["sprites"].values()})
        if missing:
            self.tree_status.set_text(f"PoE 1 passive tree {graph['version']} · {loaded} of {required} artwork atlases available; some artwork is unavailable. Retry online to complete this view." + (f" · Cache warning: {warnings[0]}" if warnings else ""))
        elif warnings:
            self.tree_status.set_text(f"PoE 1 passive tree {graph['version']} · official GGG artwork loaded · cache warning: {warnings[0]}")
        else:
            self.tree_status.set_text(f"PoE 1 passive tree {graph['version']} · official GGG artwork loaded · {graph['allocated_count']} allocated nodes"
                                      + (f" · {len(graph['unknown_allocations'])} IDs absent from this version" if graph["unknown_allocations"] else ""))
        return GLib.SOURCE_REMOVE

    def select_tree_node(self, node):
        if node is None:
            self._render_tree_summary()
            return
        self.tree_node_title.set_text(node["name"])
        flags = ["Allocated" if node["allocated"] else "Unallocated"]
        flags.extend(name for key, name in (("keystone", "Keystone"), ("notable", "Notable"),
                                            ("mastery", "Mastery"), ("jewel", "Jewel socket"),
                                            ("ascendancy", "Ascendancy")) if node[key])
        self.tree_node_flags.set_text(" · ".join(flags))
        lines = list(node.get("stats") or ())
        if node.get("mastery") and not lines:
            effects = node.get("mastery_effects") or ()
            if effects:
                lines.append("Available mastery options:")
                for eff in effects:
                    joined = "; ".join(eff.get("stats") or ())
                    if joined:
                        lines.append(f"• {joined}")
        if node.get("reminder_text"):
            lines.append("")
            lines.extend(node["reminder_text"])
        self.tree_node_stats.set_text("\n".join(lines) or "No stat text for this node.")

    def change_tree_spec(self, dropdown, _pspec):
        if self._updating_tree_combo:
            return
        build = self.current_build()
        if build is None:
            return
        specs = build.get("tree_specs", [])
        index = dropdown.get_selected()
        if index >= len(specs):
            return
        build["selected_tree_spec"] = specs[index]["id"]
        selected_title = dropdown.get_selected_item()
        dropdown.set_tooltip_text(selected_title.get_string() if selected_title else None)
        self.tree_node_title.set_text("Select a passive node")
        self.tree_node_flags.set_text("")
        self.tree_node_stats.set_text("Click any node to inspect its stats.")
        self._populate_tree_diff_specs(build)
        self.populate_linked_loadouts(build)
        self.persist_state()
        self.refresh_passive_tree(build)

    def change_item_set(self, dropdown, _pspec):
        if self._updating_item_combo:
            return
        build = self.current_build()
        if not build:
            return
        sets = build.get("item_sets", [])
        index = dropdown.get_selected()
        if index >= len(sets):
            return
        build["selected_item_set"] = sets[index]["id"]
        self.populate_linked_loadouts(build)
        self.persist_state(); self.refresh_loadout_views(build)

    def change_skill_set(self, dropdown, _pspec):
        if self._updating_skill_combo:
            return
        build = self.current_build()
        if not build:
            return
        sets = build.get("skill_sets", [])
        index = dropdown.get_selected()
        if index >= len(sets):
            return
        build["selected_skill_set"] = sets[index]["id"]
        self.populate_linked_loadouts(build)
        self.persist_state(); self.refresh_loadout_views(build); self.update_context()

    def change_linked_loadout(self, dropdown, _pspec):
        if self._updating_loadout_combo:
            return
        build = self.current_build()
        if not build:
            return
        links = build.get("linked_loadouts", [])
        index = dropdown.get_selected()
        if index == 0 or index - 1 >= len(links):
            return
        link = links[index - 1]
        build["selected_tree_spec"] = link["tree_spec_id"]
        build["selected_item_set"] = link["item_set_id"]
        build["selected_skill_set"] = link["skill_set_id"]
        self.populate_tree_specs(build); self.populate_item_sets(build); self.populate_skill_sets(build)
        self.persist_state(); self.refresh_loadout_views(build); self.refresh_passive_tree(build); self.update_context()

    def select_item_slot(self, _list_box, row):
        if row is not None:
            self._render_item_text(row.item_text)

    def update_context(self):
        build = self.current_build()
        if build is None:
            text = "No build imported · use the quest log and keep life/resistances on gear in mind."
        else:
            selected_id = build.get("selected_skill_set", build.get("active_skill_set", ""))
            active_set = next((s for s in build.get("skill_sets", []) if s.get("id") == selected_id), None)
            groups = active_set.get("groups", []) if active_set else build.get("groups", [])
            gems = [gem.split(" (Lv")[0] for group in groups for gem in group.get("gems", [])][:5]
            text = "Build: " + build.get("name", "Imported build")
            if active_set:
                text += " · Set: " + active_set.get("title", "")
            if gems:
                text += " · Gems: " + ", ".join(gems)
            if build.get("notes"):
                note = plain_note_summary(build["notes"])
                if note:
                    text += " · Note: " + note
        if self.context_label is not None:
            self.context_label.set_text(text)
        if hasattr(self, "hud_context"):
            self.hud_context.set_text(text)
        if hasattr(self, "copy_build_regex_btn"):
            auto_rx = generate_build_vendor_regex(build)
            self.copy_build_regex_btn.set_visible(auto_rx is not None)

    def _on_copy_build_vendor_regex(self, *_):
        auto_rx = generate_build_vendor_regex(self.current_build())
        if auto_rx:
            self._copy_regex(auto_rx["regex"], auto_rx["title"])

    @staticmethod
    def _tag_css_class(tag: str) -> str:
        low = tag.lower()
        if "passive" in low:
            return "tag-passive"
        if "trial" in low:
            return "tag-trial"
        if "resist" in low:
            return "tag-resist"
        if "bench" in low:
            return "tag-bench"
        return "tag-vendor"

    def _populate_step_tags(self, container: Gtk.Box, tags: list[str], hud_style: bool = False):
        self._clear_box(container)
        for tag in tags:
            lbl = Gtk.Label(label=tag)
            lbl.add_css_class("hud-tag" if hud_style else "step-tag")
            lbl.add_css_class(self._tag_css_class(tag))
            container.append(lbl)
        container.set_visible(bool(tags))

    def refresh_campaign(self):
        act = self.store.data["act"]
        act_data = self.campaign[act]
        steps = act_data["steps"]
        step = min(max(self.store.data["step"], 0), len(steps) - 1)
        self.store.data["step"] = step
        item = steps[step]
        fraction = (step + 1) / max(1, len(steps))
        tags = item.get("tags") or []
        layout_tell = (item.get("layout") or "").strip()
        target = act_data.get("target") or {}

        self.step_label.set_text(f"Act {act+1} · Step {step+1} of {len(steps)} · {item['title']}")
        self.detail_label.set_text(item["detail"])
        self.tip_label.set_text(item["tip"])
        if hasattr(self, "step_tags_box"):
            self._populate_step_tags(self.step_tags_box, tags, hud_style=False)
        if hasattr(self, "layout_box"):
            self.layout_label.set_text(layout_tell)
            self.layout_box.set_visible(bool(layout_tell))
        if hasattr(self, "act_target_life"):
            self.act_target_life.set_text(f"❤ {target.get('life', 'Stack Life')}")
            self.act_target_resists.set_text(f"🛡 {target.get('resists', 'Cap Elemental Resists')}")
            self.act_target_note.set_text(target.get("note", ""))
        if hasattr(self, "campaign_progress"):
            self.campaign_progress.set_fraction(fraction)
        if hasattr(self, "campaign_step_list"):
            self._updating_campaign_list = True
            try:
                self.campaign_step_list.remove_all()
                selected_row = None
                for idx, st in enumerate(steps):
                    row_box = Gtk.Box(spacing=8)
                    row_box.set_margin_top(7); row_box.set_margin_bottom(7)
                    row_box.set_margin_start(10); row_box.set_margin_end(10)
                    marker = "✓" if idx < step else ("▶" if idx == step else "○")
                    m_lbl = Gtk.Label(label=marker)
                    m_lbl.set_size_request(16, -1)
                    t_lbl = Gtk.Label(label=f"{idx + 1}. {st['title']}", xalign=0, wrap=True, hexpand=True)
                    css_cls = "campaign-step-done" if idx < step else ("campaign-step-active" if idx == step else "campaign-step-pending")
                    m_lbl.add_css_class(css_cls)
                    t_lbl.add_css_class(css_cls)
                    row_box.append(m_lbl)
                    row_box.append(t_lbl)
                    row = Gtk.ListBoxRow(child=row_box)
                    row.step_index = idx
                    self.campaign_step_list.append(row)
                    if idx == step:
                        selected_row = row
                if selected_row is not None:
                    self.campaign_step_list.select_row(selected_row)
            finally:
                self._updating_campaign_list = False
        if self.hud is not None:
            self.hud_act.set_text(f"ACT {act+1} · {act_data['title'].upper()} · {step+1}/{len(steps)}")
            self.hud_title.set_text(item["title"])
            if hasattr(self, "hud_tags_box"):
                self._populate_step_tags(self.hud_tags_box, tags, hud_style=True)
            if hasattr(self, "hud_layout_label"):
                self.hud_layout_label.set_text(f"🧭 {layout_tell}" if layout_tell else "")
                self.hud_layout_label.set_visible(bool(layout_tell))
            self.hud_summary.set_text(compact_step_summary(item["detail"]))
            self.hud_detail.set_text(item["detail"])
            self.hud_tip.set_text("Tip · " + item["tip"])
            if hasattr(self, "hud_target"):
                t_parts = [p for p in (target.get("life"), target.get("resists")) if p]
                self.hud_target.set_text("Target · " + " · ".join(t_parts) if t_parts else "")
                self.hud_target.set_visible(bool(t_parts))
            if hasattr(self, "hud_progress"):
                self.hud_progress.set_fraction(fraction)
            GLib.idle_add(self._sync_hud_input_region)
        self.update_context()
        self.persist_state()

    def _on_campaign_row_selected(self, _listbox, row):
        if self._updating_campaign_list or row is None:
            return
        idx = getattr(row, "step_index", None)
        if idx is None or idx == self.store.data["step"]:
            return
        self.store.data["step"] = idx
        self.refresh_campaign()

    def _tick_run_timer(self):
        formatted = self.run_timer.formatted()
        if hasattr(self, "timer_label"):
            self.timer_label.set_text(formatted)
        if hasattr(self, "hud_timer_label"):
            self.hud_timer_label.set_text(formatted)
        return GLib.SOURCE_CONTINUE

    def _on_timer_toggle(self, *_):
        self.run_timer.toggle()
        if hasattr(self, "timer_toggle_btn"):
            self.timer_toggle_btn.set_label("⏸ Pause" if self.run_timer.running else "▶ Resume")
        self._tick_run_timer()

    def _on_timer_reset(self, *_):
        self.run_timer.reset()
        if hasattr(self, "timer_toggle_btn"):
            self.timer_toggle_btn.set_label("▶ Start")
        self._tick_run_timer()
        self._refresh_splits_ui()

    def _refresh_splits_ui(self):
        if not hasattr(self, "splits_flow"):
            return
        child = self.splits_flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.splits_flow.remove(child)
            child = nxt
        splits = self.run_timer.splits
        self.splits_card.set_visible(bool(splits))
        for act_num in sorted(splits):
            chip = Gtk.Label(label=f"Act {act_num}  {format_duration(splits[act_num])}")
            chip.add_css_class("split-chip")
            self.splits_flow.append(chip)

    def _on_regex_category_clicked(self, button, category: str):
        if button.get_active():
            self.regex_category_filter = category
            for cat, btn in self._regex_cat_buttons.items():
                if btn is not button and btn.get_active():
                    btn.set_active(False)
            self.refresh_regex_list()
        elif self.regex_category_filter == category:
            button.set_active(True)

    def _on_save_custom_regex(self, *_):
        title = self.regex_new_title.get_text().strip()
        pattern = self.regex_new_pattern.get_text().strip()
        note = self.regex_new_note.get_text().strip()
        if not title or not pattern:
            self.status.set_text("Enter both a title and a regex pattern.")
            return
        if len(pattern) > 250:
            self.status.set_text("PoE search boxes allow at most 250 characters.")
            return
        self.regex_presets.append({
            "id": f"custom-{int(time.time() * 1000)}",
            "category": "Custom",
            "title": title[:120],
            "regex": pattern,
            "note": note[:240],
            "builtin": False,
        })
        try:
            save_custom_regexes(self.regex_presets)
        except OSError as exc:
            self.status.set_text(f"Could not save custom regex: {exc}")
            return
        self.regex_new_title.set_text("")
        self.regex_new_pattern.set_text("")
        self.regex_new_note.set_text("")
        self.regex_add_toggle.set_active(False)
        self.refresh_regex_list()
        self.status.set_text(f"Saved custom regex '{title}'.")

    def _copy_regex(self, pattern: str, title: str):
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(pattern)
        self.status.set_text(f"Copied regex for '{title}' to clipboard ({len(pattern)} chars).")

    def _delete_custom_regex(self, preset_id: str):
        self.regex_presets = [p for p in self.regex_presets if p.get("id") != preset_id or p.get("builtin")]
        try:
            save_custom_regexes(self.regex_presets)
        except OSError:
            pass
        self.refresh_regex_list()
        self.status.set_text("Removed custom regex preset.")

    def refresh_regex_list(self):
        if not hasattr(self, "regex_cards_box"):
            return
        self._clear_box(self.regex_cards_box)
        query = self.regex_search_entry.get_text().strip().lower() if hasattr(self, "regex_search_entry") else ""
        cat = self.regex_category_filter
        items = list(self.regex_presets)
        auto_rx = generate_build_vendor_regex(self.current_build())
        if auto_rx is not None:
            items.insert(0, auto_rx)
        shown = 0
        for preset in items:
            if cat != "All" and preset.get("category") != cat:
                continue
            hay = f"{preset.get('title', '')} {preset.get('regex', '')} {preset.get('note', '')}".lower()
            if query and query not in hay:
                continue
            shown += 1
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.add_css_class("regex-card")

            top = Gtk.Box(spacing=8)
            title_lbl = Gtk.Label(label=preset["title"], xalign=0, hexpand=True, wrap=True)
            title_lbl.add_css_class("heading")
            top.append(title_lbl)

            cat_pill = Gtk.Label(label=preset.get("category", "Custom"))
            cat_pill.add_css_class("stat-pill")
            top.append(cat_pill)

            copy_btn = Gtk.Button(label="Copy")
            copy_btn.add_css_class("suggested-action")
            copy_btn.connect("clicked", lambda _b, p=preset["regex"], t=preset["title"]: self._copy_regex(p, t))
            top.append(copy_btn)

            if not preset.get("builtin"):
                del_btn = Gtk.Button(label="✕")
                del_btn.add_css_class("destructive-subtle")
                del_btn.add_css_class("compact-btn")
                del_btn.set_tooltip_text("Delete custom regex")
                del_btn.connect("clicked", lambda _b, pid=preset["id"]: self._delete_custom_regex(pid))
                top.append(del_btn)
            card.append(top)

            code_lbl = Gtk.Label(label=preset["regex"], xalign=0, selectable=True, wrap=True)
            code_lbl.add_css_class("regex-code")
            card.append(code_lbl)

            if preset.get("note"):
                note_lbl = Gtk.Label(label=preset["note"], xalign=0, wrap=True)
                note_lbl.add_css_class("planner-subtle")
                card.append(note_lbl)

            self.regex_cards_box.append(card)

        if shown == 0:
            empty = Gtk.Label(label="No matching regex presets. Click '+ Add Regex' to save your own.")
            empty.add_css_class("planner-subtle")
            empty.set_xalign(0); empty.set_margin_top(10); empty.set_margin_start(4)
            self.regex_cards_box.append(empty)

    def change_act(self, dropdown, _pspec):
        act = dropdown.get_selected()
        if act == Gtk.INVALID_LIST_POSITION or act == self.store.data["act"]:
            return
        prev_act = self.store.data["act"]
        self.store.data["act"], self.store.data["step"] = act, 0
        if act > prev_act and self.run_timer.running:
            self.run_timer.record_act_transition(prev_act, act)
            self._refresh_splits_ui()
        self.refresh_campaign()

    def move_step(self, delta):
        prev_act = self.store.data["act"]
        act, step = advance(self.campaign, self.store.data["act"], self.store.data["step"], delta)
        self.store.data["act"], self.store.data["step"] = act, step
        if act > prev_act and self.run_timer.running:
            self.run_timer.record_act_transition(prev_act, act)
            self._refresh_splits_ui()
        self.campaign_act.set_selected(act)
        self.refresh_campaign()

    def start_control_socket(self):
        try:
            if SOCKET.exists():
                SOCKET.unlink()
        except OSError as exc:
            print(f"Could not clear the old control socket: {exc}", file=sys.stderr)
            return

        def serve():
            server = socket.socket(socket.AF_UNIX)
            try:
                server.bind(str(SOCKET)); os.chmod(SOCKET, 0o600); server.listen(4)
                while True:
                    connection, _ = server.accept()
                    with connection:
                        try:
                            message = connection.recv(2048).decode("utf-8", "replace").strip()
                        except OSError:
                            continue
                        GLib.idle_add(self.handle_command, message)
            except OSError as exc:
                print(f"PoE Companion control socket stopped: {exc}", file=sys.stderr)
            finally:
                server.close()

        threading.Thread(target=serve, daemon=True).start()

    def handle_command(self, message):
        if message == "toggle":
            if not self.refresh_focus_for_toggle():
                self.status.set_text("Focus the PoE 1 game window and ensure its output is connected before toggling HUD input.")
                return GLib.SOURCE_REMOVE
            next_state = toggle_clickable(self.clickable, self.focus, load_json(CONFIG, {}))
            if next_state is None or not self.layer_ready:
                self.status.set_text("The HUD is unavailable for the current focus/output; click-through state was kept.")
                return GLib.SOURCE_REMOVE
            if not self.apply_input_region(next_state):
                self.clickable = False
                self.hud_mode.set_text("CLICK-THROUGH")
                self.status.set_text("Could not enable HUD pointer input; the game retains pointer and keyboard input.")
                return GLib.SOURCE_REMOVE
            self.clickable = next_state
            self.hud_mode.set_text("INTERACTIVE" if self.clickable else "CLICK-THROUGH")
            self.apply_hud_layout()
            self.status.set_text("HUD is " + ("clickable" if self.clickable else "click-through"))
        elif message == "next-step":
            self.move_step(1)
        elif message == "prev-step":
            self.move_step(-1)
        elif message == "toggle-compact":
            self.toggle_hud_compact()
        elif message == "toggle-hud":
            self.hud_prefs["enabled"] = not self.hud_prefs.get("enabled", True)
            save_hud_prefs(self.hud_prefs)
            self._updating_hud_controls = True
            try:
                if hasattr(self, "hud_enabled_switch"):
                    self.hud_enabled_switch.set_active(self.hud_prefs["enabled"])
                if not self.hud_prefs["enabled"] and self.hud_preview:
                    self.hud_preview = False
                    if hasattr(self, "hud_preview_button"):
                        self.hud_preview_button.set_active(False)
                    if hasattr(self, "header_hud_button"):
                        self.header_hud_button.set_active(False)
            finally:
                self._updating_hud_controls = False
            self.present_hud()
        elif message.startswith("focus "):
            try:
                data = json.loads(message[6:])
            except ValueError:
                return GLib.SOURCE_REMOVE
            self.on_focus(data)
        return GLib.SOURCE_REMOVE

    def refresh_focus_for_toggle(self) -> bool:
        if self.niri_tracker:
            try:
                self.focus = self.read_niri_focus()
                self.last_focus_update = time.monotonic()
            except Exception as exc:
                self.focus = {"focused": False}
                print(f"Could not refresh niri focus for HUD toggle: {exc}", file=sys.stderr)
                self.present_hud(False)
                return False
        elif self.kde_tracker:
            if time.monotonic() - self.last_focus_update > 6:
                self.focus = {"focused": False}
                self.present_hud(False)
                return False
        else:
            return False
        if not self.game_matches(self.focus) or not self.focus.get("output"):
            self.present_hud(False)
            return False
        self.present_hud(True)
        return self.hud is not None and self.layer_ready and self.hud.get_visible()

    def on_focus(self, info):
        self.focus = info if isinstance(info, dict) else {"focused": False}
        self.last_focus_update = time.monotonic()
        self.present_hud(self.game_matches(self.focus))

    def game_matches(self, info):
        try:
            return is_game_focus(info, load_json(CONFIG, {}))
        except Exception as exc:
            # Malformed optional config must fail closed without breaking the
            # compositor event drain that is responsible for hiding the HUD.
            print(f"Invalid game-window matching configuration: {exc}", file=sys.stderr)
            return False

    def present_hud(self, _visible=None):
        if self.hud is None or not self.layer_ready:
            return GLib.SOURCE_REMOVE
        if self.hud_preview:
            self.hud.set_visible(True)
            if self.hud.get_surface() is not None:
                self.apply_input_region(self.clickable)
            return GLib.SOURCE_REMOVE
        if not self.hud_prefs.get("enabled", True) or not self.game_matches(self.focus):
            self.hide_hud()
            return GLib.SOURCE_REMOVE
        output = self.focus.get("output")
        monitor = self.resolve_monitor(output)
        if monitor is None:
            print(f"No connected GDK monitor matches focused game output {output!r}; keeping HUD hidden.", file=sys.stderr)
            self.hide_hud(clear_output=True)
            return GLib.SOURCE_REMOVE
        connector = monitor.get_connector()
        if connector != self.current_output:
            self.hud.set_visible(False)
            try:
                Layer.set_monitor(self.hud, monitor)
            except Exception as exc:
                print(f"Could not map HUD to game output {connector!r}: {exc}", file=sys.stderr)
                self.hide_hud(clear_output=True)
                return GLib.SOURCE_REMOVE
            self.current_output = connector
        self.hud.set_visible(True)
        if self.hud.get_surface() is not None and not self.apply_input_region(self.clickable):
            self.hide_hud()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(self._sync_hud_input_region)
        return GLib.SOURCE_REMOVE

    def hide_hud(self, clear_output: bool = False):
        if self.hud is not None:
            self.hud.set_visible(False)
        if clear_output:
            self.current_output = None

    def resolve_monitor(self, output: str | None):
        if not output or self.monitors is None:
            return None
        for index in range(self.monitors.get_n_items()):
            monitor = self.monitors.get_item(index)
            connector = monitor.get_connector()
            if connector and connector.casefold() == output.casefold():
                return monitor
        return None

    def monitors_changed(self, *_):
        if self.hud_preview or self.game_matches(self.focus):
            self.present_hud(True)
        else:
            self.hide_hud(clear_output=True)

    def start_compositor_tracker(self):
        desktop = os.getenv("XDG_CURRENT_DESKTOP", "").lower()
        if "niri" in desktop or os.getenv("NIRI_SOCKET"):
            self.niri_tracker = True
            threading.Thread(target=self.track_niri, daemon=True).start()
        if "kde" in desktop or "plasma" in desktop:
            self.start_kwin_dbus()
        self._init_client_log_tailer()
        GLib.timeout_add(120, self.drain_focus)
        GLib.timeout_add(1000, self.poll_client_log)

    def drain_focus(self):
        while True:
            try:
                payload = self.focus_queue.get_nowait()
            except queue.Empty:
                break
            self.on_focus(payload)
        if self.kde_tracker and time.monotonic() - self.last_focus_update > 6:
            self.focus = {"focused": False}
            self.hide_hud()
        return GLib.SOURCE_CONTINUE

    def read_niri_focus(self):
        def query(command):
            result = subprocess.run(["niri", "msg", "--json", command], capture_output=True,
                                    text=True, timeout=2, check=True)
            return json.loads(result.stdout)
        focused = query("focused-window")
        if not isinstance(focused, dict):
            return {"focused": False}
        workspaces = query("workspaces")
        workspace = next((x for x in workspaces if x.get("id") == focused.get("workspace_id")), {})
        return {"app_id": focused.get("app_id", ""), "title": focused.get("title", ""),
                "output": workspace.get("output", ""), "focused": bool(focused.get("is_focused", True))}

    def track_niri(self):
        while True:
            process = None
            try:
                self.focus_queue.put(self.read_niri_focus())
                process = subprocess.Popen(["niri", "msg", "--json", "event-stream"], stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, bufsize=1)
                for line in process.stdout:
                    event = json.loads(line)
                    if any(key in event for key in (
                        "WindowFocusChanged", "WindowsChanged", "WindowOpenedOrChanged", "WindowClosed",
                        "WorkspaceActivated", "WorkspaceActiveWindowChanged", "WorkspacesChanged",
                    )):
                        self.focus_queue.put(self.read_niri_focus())
                self.focus_queue.put({"focused": False})
            except Exception as exc:
                self.focus_queue.put({"focused": False})
                print(f"niri focus tracking lost: {exc}", file=sys.stderr)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                if process is not None and process.stdout is not None:
                    process.stdout.close()
                if process is not None and process.stderr is not None:
                    process.stderr.close()
            time.sleep(1)

    def start_kwin_dbus(self):
        self.kde_tracker = True
        xml = '<node><interface name="org.poecompanion.Companion"><method name="FocusChanged"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="in"/></method></interface></node>'
        try:
            node = Gio.DBusNodeInfo.new_for_xml(xml)

            def method(_conn, _sender, _path, _iface, _method, params, invocation):
                app_id, title, output, focused = params.unpack()
                self.focus_queue.put({"app_id": app_id, "title": title, "output": output, "focused": focused})
                invocation.return_value(GLib.Variant("()", ()))

            self.dbus_owner_id = Gio.bus_own_name(
                Gio.BusType.SESSION, "org.poecompanion.Companion", Gio.BusNameOwnerFlags.NONE,
                lambda connection, _name: connection.register_object(
                    "/org/poecompanion/Companion", node.interfaces[0], method, None, None),
                None, None,
            )
        except Exception as exc:
            self.kde_tracker = False
            print(f"KWin tracking unavailable: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toggle-clickable", action="store_true", help="Toggle HUD click-through while PoE 1 is focused")
    parser.add_argument("--next-step", action="store_true", help="Advance to the next campaign step in the running HUD")
    parser.add_argument("--prev-step", action="store_true", help="Go back to the previous campaign step in the running HUD")
    parser.add_argument("--toggle-hud", action="store_true", help="Show or hide the in-game campaign HUD")
    parser.add_argument("--toggle-compact", action="store_true", help="Toggle between compact bar and expanded HUD")
    args = parser.parse_args()
    for flag, command in (
        (args.toggle_clickable, "toggle"),
        (args.next_step, "next-step"),
        (args.prev_step, "prev-step"),
        (args.toggle_hud, "toggle-hud"),
        (args.toggle_compact, "toggle-compact"),
    ):
        if flag:
            return 0 if notify(command) else 1
    return Companion().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
