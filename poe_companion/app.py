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
        self._updating_item_combo = False
        self._updating_skill_combo = False
        self._updating_loadout_combo = False
        self.tree_load_id = 0
        self.tree_graph = None
        self.tree_search_matches = []
        self.tree_search_index = -1
        self.note_tags = {}
        self.item_tags = {}
        self.skill_tags = {}

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
        self.planner.set_default_size(1160, 780)
        self.planner.set_size_request(680, 520)
        self.planner.add_css_class("planner-window")
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.planner.set_child(root)

        header = Gtk.Box(spacing=14)
        header.add_css_class("app-header")
        brand = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Wraeclast Companion")
        title.add_css_class("app-title"); title.set_xalign(0); brand.append(title)
        intro = Gtk.Label(label="Build desk · Path of Exile 1")
        intro.set_xalign(0); intro.add_css_class("app-subtitle"); brand.append(intro)
        header.append(brand)
        header.append(Gtk.Box(hexpand=True))
        build_label = Gtk.Label(label="ACTIVE BUILD")
        build_label.add_css_class("section-kicker"); header.append(build_label)
        builds = ["Campaign only"] + [b.get("name") or f"Build {i + 1}" for i, b in enumerate(self.store.data["builds"])]
        self.build_combo = Gtk.DropDown.new_from_strings(builds)
        self.build_combo.set_selected(self.store.data["selected_build"] + 1)
        self.build_combo.set_size_request(210, -1)
        self.build_combo.connect("notify::selected", self.change_build)
        header.append(self.build_combo)
        self.remove_build_button = Gtk.Button(label="Remove")
        self.remove_build_button.add_css_class("destructive-subtle")
        self.remove_build_button.set_tooltip_text("Remove the currently selected saved build")
        self.remove_build_button.connect("clicked", self.remove_current_build)
        header.append(self.remove_build_button)
        root.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, hexpand=True, vexpand=True)
        self.page_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                    transition_duration=120, hexpand=True, vexpand=True)
        self.page_stack.set_hhomogeneous(False)
        sidebar = Gtk.StackSidebar(stack=self.page_stack)
        sidebar.add_css_class("app-sidebar")
        sidebar.set_size_request(156, -1)
        body.append(sidebar)
        body.append(self.page_stack)
        root.append(body)

        self._build_overview_page()
        self._build_tree_page()
        self._build_loadouts_page()
        self._build_campaign_page()

        footer = Gtk.Box(spacing=10)
        footer.add_css_class("app-footer")
        self.status = Gtk.Label(label="No build needed: the campaign guide works on its own.")
        self.status.set_xalign(0); self.status.set_wrap(True); self.status.add_css_class("footer-status")
        footer.append(self.status)
        root.append(footer)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .planner-window { background: #0b1017; color: #e6edf5; }
            .planner-window headerbar { background: #101722; color: #e6edf5; border-bottom: 1px solid #1e2a38; }

            .app-header {
                background: #101722;
                border-bottom: 1px solid #1f2b3a;
                padding: 12px 18px;
            }
            .app-title { font-size: 18px; font-weight: 700; color: #f3e4c3; letter-spacing: 0.2px; }
            .app-subtitle { font-size: 12px; color: #7e90a4; }
            .section-kicker { font-size: 10.5px; font-weight: 700; color: #7e90a4; letter-spacing: 0.8px; }

            .app-sidebar {
                background: #0e141e;
                border-right: 1px solid #1d2938;
                padding: 10px 8px;
            }
            .app-sidebar list { background: transparent; }
            .app-sidebar row {
                background: transparent;
                color: #a9b8c9;
                border-radius: 8px;
                margin: 2px 0;
                padding: 9px 12px;
                font-weight: 500;
            }
            .app-sidebar row:hover { background: #162130; color: #e6edf5; }
            .app-sidebar row:selected {
                background: #233042;
                color: #f3ca78;
                font-weight: 700;
                border-left: 3px solid #d9a44b;
            }

            .app-footer {
                background: #0e141e;
                border-top: 1px solid #1d2938;
                padding: 8px 18px;
            }
            .footer-status { font-size: 12px; color: #8b9db1; }

            .page-container { padding: 16px 18px; }
            .planner-card {
                background: #131c27;
                border: 1px solid #243345;
                border-radius: 12px;
                padding: 16px;
            }
            .tree-canvas-frame {
                background: #080c12;
                border: 1px solid #243345;
                border-radius: 12px;
            }
            .tip-callout {
                background: #172130;
                border: 1px solid #2d3f56;
                border-left: 3px solid #d9a44b;
                border-radius: 8px;
                padding: 12px 14px;
            }
            .context-card {
                background: #111924;
                border: 1px solid #223042;
                border-radius: 10px;
                padding: 12px 14px;
            }
            .planner-subtle { color: #94a6ba; }
            .planner-accent { color: #ecc06c; }
            .node-stat-text { color: #8eb7ff; font-size: 13px; }
            .node-badge {
                background: #1d2a3a;
                color: #c7d5e5;
                border-radius: 6px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 600;
            }
            .node-badge-gold {
                background: #332815;
                color: #f3ca78;
                border: 1px solid #6b5023;
            }
            .zoom-readout {
                font-size: 12px;
                font-weight: 600;
                color: #a9b8c9;
                min-width: 44px;
            }
            .toolbar-bar {
                background: #111924;
                border: 1px solid #223042;
                border-radius: 10px;
                padding: 8px 10px;
            }

            .planner-window button {
                min-height: 32px;
                background: #192433;
                color: #e6edf5;
                border: 1px solid #2c3c50;
                border-radius: 8px;
                padding: 4px 12px;
            }
            .planner-window button:hover { background: #223144; border-color: #3d526c; }
            .planner-window button:active { background: #16202d; }
            .planner-window button:disabled { opacity: 0.45; }
            .planner-window button.compact-btn { min-height: 30px; min-width: 30px; padding: 2px 8px; }
            .planner-window button.suggested-action {
                background: #c79040;
                color: #10141a;
                border-color: #dca655;
                font-weight: 700;
            }
            .planner-window button.suggested-action:hover { background: #d69f4e; }
            .planner-window button.destructive-subtle {
                background: #1b1c24;
                color: #d99b9b;
                border-color: #3c2930;
            }
            .planner-window button.destructive-subtle:hover {
                background: #341d22;
                color: #f7c5c5;
                border-color: #6b333c;
            }

            .planner-window dropdown button { background: #172231; color: #e6edf5; border-color: #2c3c50; }
            .planner-window entry, .planner-window searchentry {
                background: #0f1620;
                color: #e6edf5;
                border: 1px solid #29384a;
                border-radius: 8px;
                min-height: 32px;
            }
            .planner-window entry:focus-within, .planner-window searchentry:focus-within {
                border-color: #c79040;
            }
            .planner-window list { background: #101722; color: #e6edf5; }
            .planner-window listbox.boxed-list {
                background: #0f1620;
                border: 1px solid #223042;
                border-radius: 8px;
            }
            .planner-window listbox.boxed-list row {
                background: transparent;
                color: #dbe5f0;
                border-bottom: 1px solid #1a2533;
            }
            .planner-window listbox.boxed-list row:hover { background: #182332; }
            .planner-window listbox.boxed-list row:selected {
                background: #2f2616;
                color: #f8dfa6;
            }
            .planner-window textview, .planner-window textview text {
                background: #0f1620;
                color: #e6edf5;
            }
            .planner-window paned > separator {
                background: #1f2b3a;
                min-width: 5px;
            }
            .planner-window progressbar trough {
                background: #1b2635;
                border-radius: 6px;
                min-height: 7px;
            }
            .planner-window progressbar progress {
                background: #d49e48;
                border-radius: 6px;
                min-height: 7px;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.planner_css = css
        self.build_hud()
        # All widgets and restored selectors exist before refresh runs.
        self.refresh_build()
        self.refresh_campaign()

    def _build_overview_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.add_css_class("page-container")

        hero_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        hero_card.add_css_class("planner-card")
        hero_kicker = Gtk.Label(label="SELECTED BUILD")
        hero_kicker.add_css_class("section-kicker"); hero_kicker.set_xalign(0); hero_card.append(hero_kicker)
        self.profile_label = Gtk.Label()
        self.profile_label.add_css_class("title-2"); self.profile_label.set_xalign(0); self.profile_label.set_wrap(True)
        hero_card.append(self.profile_label)
        self.overview_meta = Gtk.Label()
        self.overview_meta.set_xalign(0); self.overview_meta.set_wrap(True); self.overview_meta.add_css_class("planner-subtle")
        hero_card.append(self.overview_meta)
        page.append(hero_card)

        import_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        import_card.add_css_class("planner-card")
        import_title = Gtk.Label(label="Import a build")
        import_title.add_css_class("heading"); import_title.set_xalign(0); import_card.append(import_title)
        import_hint = Gtk.Label(label="Paste a public pobb.in link, PoB Community share code, or XML. Links need internet; codes and XML work offline.")
        import_hint.set_xalign(0); import_hint.set_wrap(True); import_hint.add_css_class("planner-subtle"); import_card.append(import_hint)
        self.code_entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True)
        self.code_entry.set_top_margin(8); self.code_entry.set_bottom_margin(8)
        self.code_entry.set_left_margin(10); self.code_entry.set_right_margin(10)
        entry_scroll = Gtk.ScrolledWindow(); entry_scroll.set_min_content_height(68); entry_scroll.set_max_content_height(96)
        entry_scroll.set_child(self.code_entry); import_card.append(entry_scroll)
        import_row = Gtk.Box(spacing=8)
        self.import_button = Gtk.Button(label="Import build")
        self.import_button.add_css_class("suggested-action")
        self.import_button.connect("clicked", self.import_build)
        import_row.append(self.import_button)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", lambda *_: self.code_entry.get_buffer().set_text(""))
        import_row.append(clear_btn)
        input_limits = Gtk.Label(label="2 MiB input · 8 MiB decoded build")
        input_limits.add_css_class("planner-subtle"); input_limits.set_hexpand(True); input_limits.set_xalign(1)
        import_row.append(input_limits); import_card.append(import_row)
        page.append(import_card)

        notes_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, vexpand=True)
        notes_card.add_css_class("planner-card")
        self.notes_view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.notes_view.set_top_margin(10); self.notes_view.set_bottom_margin(10)
        self.notes_view.set_left_margin(12); self.notes_view.set_right_margin(12)
        notes_title = Gtk.Label(label="Build notes")
        notes_title.add_css_class("heading"); notes_title.set_xalign(0); notes_card.append(notes_title)
        note_scroll = Gtk.ScrolledWindow(); note_scroll.set_min_content_height(180); note_scroll.set_vexpand(True)
        note_scroll.set_child(self.notes_view); notes_card.append(note_scroll)
        page.append(notes_card)

        hint = Gtk.Label(label="Passive allocations and loadouts are read-only. No build calculations are performed.")
        hint.set_xalign(0); hint.set_wrap(True); hint.add_css_class("planner-subtle"); page.append(hint)
        self.page_stack.add_titled(self._scrolled(page), "overview", "Overview")

    def _build_tree_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, vexpand=True)
        page.add_css_class("page-container")

        toolbar = Gtk.Box(spacing=8)
        toolbar.add_css_class("toolbar-bar")
        self.tree_variant_combo = Gtk.DropDown.new_from_strings(["No tree selected"])
        self.tree_variant_combo.set_size_request(210, -1)
        self.tree_variant_combo.set_hexpand(False)
        self.tree_variant_combo.set_factory(self._tree_title_factory(ellipsize=True))
        self.tree_variant_combo.set_list_factory(self._tree_title_factory(ellipsize=False))
        self.tree_variant_combo.connect("notify::selected", self.change_tree_spec)
        toolbar.append(self.tree_variant_combo)

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
        all_asc.set_tooltip_text("Show all ascendancies (off by default to avoid GGG sibling portrait overlaps)")
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
        detail.set_size_request(230, -1)
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
                position = max(320, width - 280)
                if paned.get_position() != position:
                    paned.set_position(position)
        body.connect("notify::width", fit_tree_pane)
        body.set_position(660)
        page.append(body)

        note = Gtk.Label(label="Scroll to zoom at cursor · Drag to pan · Hover for tooltips · Keys: + / - / 0 / Esc. Cluster-jewel, tattoo, and override transformations are not applied.")
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
                label.set_max_width_chars(26)
            label.set_margin_start(6); label.set_margin_end(6)
            item.set_child(label)
        def bind(_factory, item):
            model_item = item.get_item()
            item.get_child().set_text(model_item.get_string() if model_item else "")
        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return factory

    def _build_loadouts_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.add_css_class("page-container")

        header_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        header_card.add_css_class("toolbar-bar")
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        heading = Gtk.Label(label="Gear and skill sets")
        heading.add_css_class("title-3"); heading.set_xalign(0); title_box.append(heading)
        sub = Gtk.Label(label="Switch linked PoB loadouts or inspect gear and gem setups independently.")
        sub.add_css_class("planner-subtle"); sub.set_xalign(0); sub.set_wrap(True); title_box.append(sub)
        header_card.append(title_box)

        linked_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        linked_label = Gtk.Label(label="LINKED LOADOUT")
        linked_label.set_xalign(0); linked_label.add_css_class("section-kicker"); linked_box.append(linked_label)
        self.linked_loadout_combo = Gtk.DropDown.new_from_strings(["No linked loadouts"])
        self.linked_loadout_combo.set_size_request(240, -1)
        self.linked_loadout_combo.connect("notify::selected", self.change_linked_loadout)
        linked_box.append(self.linked_loadout_combo)
        header_card.append(linked_box)
        page.append(header_card)

        independent = Gtk.Box(spacing=14, hexpand=True, vexpand=True)
        gear = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, vexpand=True)
        gear.add_css_class("planner-card")
        gear_header = Gtk.Label(label="Gear")
        gear_header.add_css_class("heading"); gear_header.set_xalign(0); gear.append(gear_header)
        self.item_set_combo = Gtk.DropDown.new_from_strings(["No gear sets"])
        self.item_set_combo.connect("notify::selected", self.change_item_set)
        gear.append(self.item_set_combo)
        self.item_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.item_list.add_css_class("boxed-list")
        self.item_list.connect("row-selected", self.select_item_slot)
        item_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=220); item_scroll.set_child(self.item_list)
        gear.append(item_scroll)
        self.item_detail_view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.item_detail_view.set_top_margin(8); self.item_detail_view.set_bottom_margin(8)
        self.item_detail_view.set_left_margin(10); self.item_detail_view.set_right_margin(10)
        detail_scroll = Gtk.ScrolledWindow(min_content_height=160); detail_scroll.set_child(self.item_detail_view)
        gear.append(detail_scroll)

        skills = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, vexpand=True)
        skills.add_css_class("planner-card")
        skill_header = Gtk.Label(label="Skills and gems")
        skill_header.add_css_class("heading"); skill_header.set_xalign(0); skills.append(skill_header)
        self.skill_set_combo = Gtk.DropDown.new_from_strings(["No skill sets"])
        self.skill_set_combo.connect("notify::selected", self.change_skill_set)
        skills.append(self.skill_set_combo)
        self.skill_detail = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.skill_detail.set_top_margin(10); self.skill_detail.set_bottom_margin(10)
        self.skill_detail.set_left_margin(12); self.skill_detail.set_right_margin(12)
        skill_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=340); skill_scroll.set_child(self.skill_detail)
        skills.append(skill_scroll)

        independent.append(gear); independent.append(skills)
        page.append(independent)
        link_note = Gtk.Label(label="Independent selectors preserve the build's own active gear and skill sets. A linked loadout appears only when PoB titles or explicit identifiers establish a match.")
        link_note.set_xalign(0); link_note.set_wrap(True); link_note.add_css_class("planner-subtle"); page.append(link_note)
        self.page_stack.add_titled(self._scrolled(page), "loadouts", "Loadouts")

    def _build_campaign_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.add_css_class("page-container")

        nav = Gtk.Box(spacing=10)
        nav.add_css_class("toolbar-bar")
        act_labels = [f"Act {x['act']} · {x['title']}" for x in self.campaign]
        self.campaign_act = Gtk.DropDown.new_from_strings(act_labels)
        self.campaign_act.set_selected(self.store.data["act"])
        self.campaign_act.set_size_request(240, -1)
        self.campaign_act.connect("notify::selected", self.change_act)
        nav.append(self.campaign_act)
        nav.append(Gtk.Box(hexpand=True))
        prev = Gtk.Button(label="← Previous"); prev.connect("clicked", lambda *_: self.move_step(-1)); nav.append(prev)
        nxt = Gtk.Button(label="Next →"); nxt.add_css_class("suggested-action"); nxt.connect("clicked", lambda *_: self.move_step(1)); nav.append(nxt)
        page.append(nav)

        step_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        step_card.add_css_class("planner-card")
        self.campaign_progress = Gtk.ProgressBar()
        self.campaign_progress.set_show_text(False)
        step_card.append(self.campaign_progress)

        self.step_label = Gtk.Label()
        self.step_label.set_xalign(0); self.step_label.add_css_class("title-2"); self.step_label.set_wrap(True)
        step_card.append(self.step_label)

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
        page.append(step_card)

        ctx_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ctx_card.add_css_class("context-card")
        ctx_kicker = Gtk.Label(label="ACTIVE BUILD CONTEXT")
        ctx_kicker.add_css_class("section-kicker"); ctx_kicker.set_xalign(0); ctx_card.append(ctx_kicker)
        self.context_label = Gtk.Label(wrap=True, selectable=True)
        self.context_label.set_xalign(0); self.context_label.set_wrap(True); self.context_label.add_css_class("planner-subtle")
        ctx_card.append(self.context_label)
        page.append(ctx_card)

        hud_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hud_card.add_css_class("planner-card")
        hud_head = Gtk.Box(spacing=10)
        hud_title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        hud_kicker = Gtk.Label(label="IN-GAME OVERLAY (WAYLAND HUD)")
        hud_kicker.add_css_class("section-kicker"); hud_kicker.set_xalign(0); hud_title_box.append(hud_kicker)
        hud_sub = Gtk.Label(label="Shows automatically while PoE 1 is focused. Click-through by default.")
        hud_sub.add_css_class("planner-subtle"); hud_sub.set_xalign(0); hud_sub.set_wrap(True); hud_title_box.append(hud_sub)
        hud_head.append(hud_title_box)
        self.hud_preview_button = Gtk.ToggleButton(label="Preview HUD")
        self.hud_preview_button.set_tooltip_text("Temporarily show the overlay on screen to check placement and opacity")
        self.hud_preview_button.connect("toggled", self._on_hud_preview_toggled)
        hud_head.append(self.hud_preview_button)
        hud_card.append(hud_head)

        row1 = Gtk.Box(spacing=16)
        en_box = Gtk.Box(spacing=8)
        en_lbl = Gtk.Label(label="Enable overlay"); en_lbl.set_xalign(0); en_box.append(en_lbl)
        self.hud_enabled_switch = Gtk.Switch(active=self.hud_prefs["enabled"], valign=Gtk.Align.CENTER)
        self.hud_enabled_switch.connect("notify::active", self._on_hud_pref_changed)
        en_box.append(self.hud_enabled_switch)
        row1.append(en_box)

        cmp_box = Gtk.Box(spacing=8)
        cmp_lbl = Gtk.Label(label="Compact bar"); cmp_lbl.set_xalign(0); cmp_box.append(cmp_lbl)
        self.hud_compact_switch = Gtk.Switch(active=self.hud_prefs["compact"], valign=Gtk.Align.CENTER)
        self.hud_compact_switch.connect("notify::active", self._on_hud_pref_changed)
        cmp_box.append(self.hud_compact_switch)
        row1.append(cmp_box)

        mini_box = Gtk.Box(spacing=8)
        mini_lbl = Gtk.Label(label="Clickable mini-buttons"); mini_lbl.set_xalign(0); mini_box.append(mini_lbl)
        self.hud_interactive_switch = Gtk.Switch(active=self.hud_prefs["interactive_controls"], valign=Gtk.Align.CENTER)
        self.hud_interactive_switch.set_tooltip_text("Allow clicking the small ← / → / ▾ buttons on the HUD while keeping the rest of the overlay click-through")
        self.hud_interactive_switch.connect("notify::active", self._on_hud_pref_changed)
        mini_box.append(self.hud_interactive_switch)
        row1.append(mini_box)

        pos_box = Gtk.Box(spacing=8, hexpand=True)
        pos_lbl = Gtk.Label(label="Position"); pos_lbl.add_css_class("planner-subtle"); pos_box.append(pos_lbl)
        self.hud_position_combo = Gtk.DropDown.new_from_strings([label for _, label in HUD_POSITIONS])
        pos_keys = [key for key, _ in HUD_POSITIONS]
        self.hud_position_combo.set_selected(pos_keys.index(self.hud_prefs["position"]) if self.hud_prefs["position"] in pos_keys else 0)
        self.hud_position_combo.connect("notify::selected", self._on_hud_pref_changed)
        pos_box.append(self.hud_position_combo)
        row1.append(pos_box)
        hud_card.append(row1)

        row2 = Gtk.Box(spacing=14)
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

        hotkey_hint = Gtk.Label(label="CLI hotkeys (work while click-through): --next-step · --prev-step · --toggle-compact · --toggle-hud · --toggle-clickable")
        hotkey_hint.set_xalign(0); hotkey_hint.set_wrap(True); hotkey_hint.add_css_class("planner-subtle")
        hud_card.append(hotkey_hint)
        page.append(hud_card)

        page.append(Gtk.Box(vexpand=True))
        self.page_stack.add_titled(self._scrolled(page), "campaign", "Campaign")

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
            return
        try:
            if not Layer.is_supported():
                self.status.set_text("This compositor does not support Wayland layer-shell; the planner is still available.")
                if hasattr(self, "hud_preview_button"):
                    self.hud_preview_button.set_sensitive(False)
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

            self.hud_title = Gtk.Label()
            self.hud_title.add_css_class("hud-title"); self.hud_title.set_xalign(0)
            self.hud_title.set_wrap(True); self.hud_title.set_max_width_chars(52)
            box.append(self.hud_title)

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
            .hud-badge {{
                font-size: 9.5px;
                font-weight: 700;
                color: #93a7bd;
                background: rgba(25, 36, 51, 0.85);
                border-radius: 5px;
                padding: 2px 6px;
            }}
            .hud-title {{ font-size: 14px; font-weight: 700; color: #f6efe0; }}
            .hud-summary {{ font-size: 12px; color: #c7d5e5; }}
            .hud-detail {{ font-size: 12px; color: #d5e0ec; }}
            .hud-tip {{ font-size: 11.5px; color: #ecc06c; }}
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
        self.hud_preview = button.get_active()
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
        zones = self.client_log_tailer.poll_zones()
        for zone in zones:
            matched = match_campaign_step(
                self.campaign, self.store.data["act"], self.store.data["step"], zone
            )
            if matched is not None and matched != (self.store.data["act"], self.store.data["step"]):
                self.store.data["act"], self.store.data["step"] = matched
                if self.campaign_act is not None:
                    self.campaign_act.set_selected(matched[0])
                self.refresh_campaign()
                if self.status is not None:
                    self.status.set_text(f"Entered {zone} · advanced to Act {matched[0] + 1}, Step {matched[1] + 1}")
        return GLib.SOURCE_CONTINUE

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

    def import_build(self, *_):
        buffer = self.code_entry.get_buffer()
        raw = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
        if looks_like_share_url(raw):
            self._begin_pobbin_import(raw)
            return
        try:
            build = parse_build(raw)
        except PobImportError as exc:
            self.status.set_text(str(exc))
            return
        self._save_imported_build(build, buffer)

    def _begin_pobbin_import(self, raw: str):
        if self.import_pending:
            return
        self.import_pending = True
        self.import_request_id += 1
        request_id = self.import_request_id
        self.import_button.set_sensitive(False)
        self.status.set_text("Fetching the public build from pobb.in…")

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
            self.status.set_text("The input changed while the link was loading; no build was imported. Review the current input and retry.")
            return GLib.SOURCE_REMOVE
        if error:
            self.status.set_text(error)
            return GLib.SOURCE_REMOVE
        self._save_imported_build(build, buffer)
        return GLib.SOURCE_REMOVE

    def _save_imported_build(self, build: dict, buffer):
        if len(self.store.data["builds"]) >= MAX_SAVED_BUILDS:
            self.status.set_text(f"You can keep up to {MAX_SAVED_BUILDS} imported builds. Remove an older saved build before importing another.")
            return
        build["name"] = build.get("ascendancy") or build.get("class") or f"Build {len(self.store.data['builds']) + 1}"
        candidate = dict(self.store.data)
        candidate["builds"] = [*self.store.data["builds"], build]
        candidate["selected_build"] = len(candidate["builds"]) - 1
        try:
            self.store.save(candidate)
        except StateError as exc:
            self.status.set_text(str(exc))
            return
        self.store.data = candidate
        buffer.set_text("")
        self.populate_build_model()
        self.status.set_text(f"Imported {build['name']} · saved on this computer.")
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

    def refresh_build(self, *_):
        if self.notes_view is None:
            return
        build = self.current_build()
        if hasattr(self, "remove_build_button"):
            self.remove_build_button.set_sensitive(build is not None)
        if build is None:
            self.profile_label.set_text("Start a build desk")
            self.overview_meta.set_text("Import a public pobb.in link, share code, or XML. The campaign guide works without a build.")
            notes = "No build notes yet."
        else:
            hero = " · ".join(value for value in (build.get("class"), build.get("ascendancy")) if value)
            if build.get("level"):
                hero = f"{hero} · Level {build['level']}" if hero else f"Level {build['level']}"
            self.profile_label.set_text(build.get("name") or hero or "Imported build")
            spec_count = len(build.get("tree_specs", []))
            skill_count = len(build.get("skill_sets", []))
            gear_count = len(build.get("item_sets", []))
            self.overview_meta.set_text(" · ".join(x for x in (hero, f"{spec_count} tree variants" if spec_count else "No tree data",
                                                                 f"{gear_count} gear sets" if gear_count else "No gear sets",
                                                                 f"{skill_count} skill sets" if skill_count else "No named skill sets") if x))
            notes = build.get("notes") or "This build has no notes."
        self.render_notes(notes)
        self.populate_tree_specs(build)
        self.populate_item_sets(build)
        self.populate_skill_sets(build)
        self.populate_linked_loadouts(build)
        self.refresh_loadout_views(build)
        self.refresh_passive_tree(build)
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

    def _render_item_text(self, text: str):
        buf = self.item_detail_view.get_buffer()
        buf.set_text("")
        if not self.item_tags:
            self.item_tags = {
                "unique": buf.create_tag("item-unique", foreground="#af6025", weight=Pango.Weight.BOLD),
                "rare": buf.create_tag("item-rare", foreground="#f5d76e", weight=Pango.Weight.BOLD),
                "magic": buf.create_tag("item-magic", foreground="#8888ff", weight=Pango.Weight.BOLD),
                "normal": buf.create_tag("item-normal", foreground="#e6edf5", weight=Pango.Weight.BOLD),
                "meta": buf.create_tag("item-meta", foreground="#8a9cb0"),
                "mod": buf.create_tag("item-mod", foreground="#8eb7ff"),
                "sep": buf.create_tag("item-sep", foreground="#364659"),
            }
        lines = (text or "").splitlines()
        rarity = "normal"
        for line in lines[:3]:
            low = line.strip().lower()
            if low.startswith("rarity:"):
                val = low.split(":", 1)[1].strip()
                if val in ("unique", "rare", "magic", "normal"):
                    rarity = val
                break
        header_done = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            prefix = "\n" if i > 0 else ""
            if stripped.startswith("Rarity:"):
                buf.insert_with_tags(buf.get_end_iter(), prefix + line, self.item_tags["meta"])
            elif stripped.startswith("--------") or stripped == "":
                header_done = True
                buf.insert_with_tags(buf.get_end_iter(), prefix + ("────────────────────────" if stripped else ""), self.item_tags["sep"])
            elif not header_done:
                buf.insert_with_tags(buf.get_end_iter(), prefix + line, self.item_tags[rarity])
            elif any(stripped.startswith(p) for p in ("Sockets:", "LevelReq:", "Implicits:", "Quality:", "Item Level:", "Armour:", "Evasion:", "Energy Shield:", "Crafted:", "Unique ID:", "Variant:", "Selected Variant:")):
                buf.insert_with_tags(buf.get_end_iter(), prefix + line, self.item_tags["meta"])
            else:
                buf.insert_with_tags(buf.get_end_iter(), prefix + line, self.item_tags["mod"])

    def _render_skill_groups(self, title: str, groups: list):
        buf = self.skill_detail.get_buffer()
        buf.set_text("")
        if not self.skill_tags:
            self.skill_tags = {
                "title": buf.create_tag("skill-title", foreground="#94a6ba", weight=Pango.Weight.BOLD),
                "group": buf.create_tag("skill-group", foreground="#ecc06c", weight=Pango.Weight.BOLD),
                "gem": buf.create_tag("skill-gem", foreground="#e6edf5"),
                "empty": buf.create_tag("skill-empty", foreground="#8a9cb0"),
            }
        if not groups:
            buf.insert_with_tags(buf.get_end_iter(), "No enabled gems in this set.", self.skill_tags["empty"])
            return
        buf.insert_with_tags(buf.get_end_iter(), title + "\n", self.skill_tags["title"])
        for group in groups:
            label = group.get("label") or "Skill group"
            buf.insert_with_tags(buf.get_end_iter(), f"\n{label}\n", self.skill_tags["group"])
            gems = group.get("gems") or []
            for gem in gems:
                buf.insert_with_tags(buf.get_end_iter(), f"  • {gem}\n", self.skill_tags["gem"])

    def refresh_loadout_views(self, build):
        self.item_list.remove_all()
        self._render_item_text("Choose an equipped item to inspect its PoB description.")
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
                    row_box = Gtk.Box(spacing=10)
                    row_box.set_margin_top(7); row_box.set_margin_bottom(7)
                    row_box.set_margin_start(10); row_box.set_margin_end(10)
                    slot_lbl = Gtk.Label(label=slot["name"])
                    slot_lbl.add_css_class("planner-subtle")
                    slot_lbl.set_xalign(0); slot_lbl.set_size_request(92, -1)
                    name_lbl = Gtk.Label(label=slot.get("title") or "Saved item", wrap=True, hexpand=True)
                    name_lbl.set_xalign(0)
                    row_box.append(slot_lbl); row_box.append(name_lbl)
                    row = Gtk.ListBoxRow(child=row_box)
                    row.item_text = item_texts.get(slot["item_id"], "Item details were not saved.")
                    self.item_list.append(row)
                    first_row = first_row or row
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

    def refresh_campaign(self):
        act = self.store.data["act"]
        steps = self.campaign[act]["steps"]
        step = min(max(self.store.data["step"], 0), len(steps) - 1)
        self.store.data["step"] = step
        item = steps[step]
        fraction = (step + 1) / max(1, len(steps))
        self.step_label.set_text(f"Act {act+1} · Step {step+1} of {len(steps)} · {item['title']}")
        self.detail_label.set_text(item["detail"])
        self.tip_label.set_text(item["tip"])
        if hasattr(self, "campaign_progress"):
            self.campaign_progress.set_fraction(fraction)
        if self.hud is not None:
            self.hud_act.set_text(f"ACT {act+1} · {self.campaign[act]['title'].upper()} · {step+1}/{len(steps)}")
            self.hud_title.set_text(item["title"])
            self.hud_summary.set_text(compact_step_summary(item["detail"]))
            self.hud_detail.set_text(item["detail"])
            self.hud_tip.set_text("Tip · " + item["tip"])
            if hasattr(self, "hud_progress"):
                self.hud_progress.set_fraction(fraction)
        self.update_context()
        self.persist_state()

    def change_act(self, dropdown, _pspec):
        act = dropdown.get_selected()
        if act == Gtk.INVALID_LIST_POSITION or act == self.store.data["act"]:
            return
        self.store.data["act"], self.store.data["step"] = act, 0
        self.refresh_campaign()

    def move_step(self, delta):
        act, step = advance(self.campaign, self.store.data["act"], self.store.data["step"], delta)
        self.store.data["act"], self.store.data["step"] = act, step
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
