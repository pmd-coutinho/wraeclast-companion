"""Native Cairo view of the exact GGG passive tree using its published artwork atlases."""
from __future__ import annotations

from io import BytesIO
import math
import sys

import cairo
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

MIN_ZOOM = 0.2
MAX_ZOOM = 12.0
DEFAULT_SPRITE_SCALE = 0.3835
TWO_PI = math.tau
CACHE_OVERSCAN = 160


def fit_scale(x_span: float, y_span: float, width: float, height: float, zoom: float = 1.0) -> float:
    """Aspect-correct fit scale, keeping both independently measured axes visible."""
    return min(width / max(1.0, x_span), height / max(1.0, y_span)) * zoom


def zoom_pan_offset(pan_x: float, pan_y: float, width: float, height: float,
                    anchor_x: float, anchor_y: float, old_zoom: float, new_zoom: float) -> tuple[float, float]:
    """Adjust pan offsets so the world point under (anchor_x, anchor_y) stays fixed."""
    if old_zoom <= 0 or new_zoom <= 0:
        return pan_x, pan_y
    ratio = new_zoom / old_zoom
    cx = width / 2.0 + pan_x
    cy = height / 2.0 + pan_y
    new_cx = anchor_x - (anchor_x - cx) * ratio
    new_cy = anchor_y - (anchor_y - cy) * ratio
    return new_cx - width / 2.0, new_cy - height / 2.0


def connector_stroke_width(line_height: float, sprite_world_scale: float, scale: float, active: bool) -> float:
    """Compute crisp screen-space connector thickness from the line sprite height (not orbit radius)."""
    world_thickness = max(4.0, line_height * sprite_world_scale * 0.42)
    screen_width = world_thickness * scale
    if active:
        return max(1.2, min(7.5, screen_width * 1.15))
    return max(0.75, min(5.0, screen_width * 0.85))


def node_world_radius(node: dict) -> float:
    """Approximate world-space radius of a passive node for hit-testing and highlight rings."""
    if node.get("class_start_index") is not None or node.get("ascendancy_start"):
        return 76.0
    if node.get("keystone"):
        return 62.0
    if node.get("notable") or node.get("jewel") or node.get("mastery"):
        return 46.0
    return 32.0


def node_hit_radius(node: dict, scale: float) -> float:
    return max(7.0, min(46.0, node_world_radius(node) * scale))


def search_matching_nodes(graph: dict | None, query: str, visible_ids: set[str] | None = None) -> list[str]:
    """Return node IDs matching a case-insensitive query against name, stats, or mastery options."""
    term = (query or "").strip().lower()
    if not graph or not term:
        return []
    matches = []
    for node_id, node in graph.get("nodes", {}).items():
        if visible_ids is not None and node_id not in visible_ids:
            continue
        haystack = [node.get("name", "")]
        haystack.extend(node.get("stats") or ())
        for effect in node.get("mastery_effects") or ():
            haystack.extend(effect.get("stats") or ())
        if any(term in text.lower() for text in haystack if text):
            matches.append(node_id)
    nodes = graph["nodes"]
    matches.sort(key=lambda nid: (
        not nodes[nid].get("allocated"),
        not nodes[nid].get("keystone"),
        not nodes[nid].get("notable"),
        nodes[nid].get("name", "").lower(),
    ))
    return matches


def is_ascendancy_visible(graph: dict, ascendancy_name: str | None, is_bloodline: bool = False,
                          show_all: bool = False, allocated_ascendancies: set[str] | None = None) -> bool:
    if not ascendancy_name:
        return True
    if show_all:
        return True
    if allocated_ascendancies and ascendancy_name in allocated_ascendancies:
        return True
    if is_bloodline:
        return ascendancy_name == graph.get("selected_alternate_ascendancy")
    return ascendancy_name == graph.get("selected_ascendancy")


def _is_dimmed_ascendancy(graph: dict, ascendancy_name: str | None, is_bloodline: bool = False) -> bool:
    if not ascendancy_name:
        return False
    if is_bloodline:
        return ascendancy_name != graph.get("selected_alternate_ascendancy")
    return ascendancy_name != graph.get("selected_ascendancy")


def _node_layer_rank(graph: dict, node: dict) -> int:
    dimmed = _is_dimmed_ascendancy(graph, node.get("ascendancy_name"), bool(node.get("is_bloodline")))
    allocated = bool(node.get("allocated"))
    if node.get("class_start_index") is not None:
        return 50
    if node.get("ascendancy_start"):
        return 10 if dimmed else 48
    base = 0 if dimmed else (30 if allocated else 15)
    if node.get("keystone"):
        return base + 4
    if node.get("mastery"):
        return base + 3
    if node.get("jewel"):
        return base + 2
    if node.get("notable"):
        return base + 1
    return base


def _rounded_rect(cr, x: float, y: float, w: float, h: float, r: float):
    r = min(r, w / 2.0, h / 2.0)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class PassiveTreeCanvas(Gtk.DrawingArea):
    def __init__(self, on_select, on_zoom_changed=None):
        super().__init__()
        self.set_content_width(320)
        self.set_content_height(320)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_focusable(True)
        self.graph = None
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.selected_node = None
        self.hovered_node = None
        self._pointer = None
        self._on_select = on_select
        self._on_zoom_changed = on_zoom_changed
        self._drag_origin = (0.0, 0.0)
        self._drag_start = (0.0, 0.0)
        self._drag_moved = False
        self._dragging = False
        self._atlases = {}
        self._atlas_buffers = {}
        self._line_colors = {}
        self._render_cache_key = None
        self._render_cache_surface = None
        self._render_cache_view = None
        self._zoom_settle_id = None
        self._bounds_cache = (-1.0, 1.0, -1.0, 1.0)
        self._prepared_edges = []
        self._ordered_node_ids = []
        self._hit_nodes = []
        self._visible_node_ids: set[str] = set()
        self._allocated_ascendancies: set[str] = set()
        self.show_all_ascendancies = False
        self._search_query = ""
        self._search_matches: set[str] = set()
        self._diff_added: set[str] = set()
        self._diff_removed: set[str] = set()
        self._diff_label = ""
        self._pending_fit_mode = None
        self.art_errors = []
        self.art_warnings = []
        self.art_ready = False
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        self._update_cursor()

    def set_graph(self, graph):
        self.graph = graph
        self.selected_node = None
        self.hovered_node = None
        self._atlases = {}
        self._atlas_buffers = {}
        self._line_colors = {}
        self._render_cache_key = None
        self._render_cache_surface = None
        self._render_cache_view = None
        self._cancel_zoom_settle()
        self.art_errors = []
        self.art_warnings = []
        self.art_ready = False
        self._prepare_geometry()
        if self._search_query:
            self._search_matches = set(search_matching_nodes(self.graph, self._search_query, self._visible_node_ids))
        else:
            self._search_matches = set()
        has_allocated = bool(graph and any(n.get("allocated") for n in graph["nodes"].values()))
        self._pending_fit_mode = "allocated" if has_allocated else "all"
        self.fit()

    def set_show_all_ascendancies(self, show_all: bool):
        show_all = bool(show_all)
        if show_all == self.show_all_ascendancies:
            return
        self.show_all_ascendancies = show_all
        self._prepare_geometry()
        if self.selected_node and self.selected_node not in self._visible_node_ids:
            self.selected_node = None
            self._on_select(None)
        if self.hovered_node and self.hovered_node not in self._visible_node_ids:
            self.hovered_node = None
        if self._search_query:
            self._search_matches = set(search_matching_nodes(self.graph, self._search_query, self._visible_node_ids))
        self._render_cache_key = None
        self.queue_draw()

    def set_diff_nodes(self, added: set[str] | None, removed: set[str] | None, label: str = ""):
        self._diff_added = set(added or ())
        self._diff_removed = set(removed or ())
        self._diff_label = label or ""
        self._render_cache_key = None
        self.queue_draw()

    def _prepare_geometry(self):
        if not self.graph:
            self._bounds_cache = (-1.0, 1.0, -1.0, 1.0)
            self._prepared_edges = []
            self._ordered_node_ids = []
            self._visible_node_ids = set()
            self._allocated_ascendancies = set()
            return
        nodes = self.graph["nodes"]
        groups = self.graph["groups"]
        self._allocated_ascendancies = {
            node["ascendancy_name"]
            for node in nodes.values()
            if node.get("allocated") and node.get("ascendancy_name")
        }
        visible_ids = [
            nid for nid, node in nodes.items()
            if is_ascendancy_visible(
                self.graph, node.get("ascendancy_name"), bool(node.get("is_bloodline")),
                self.show_all_ascendancies, self._allocated_ascendancies,
            )
        ]
        self._visible_node_ids = set(visible_ids)
        xs = [nodes[nid]["x"] for nid in visible_ids]
        ys = [nodes[nid]["y"] for nid in visible_ids]
        if xs:
            padding = 900.0
            self._bounds_cache = (min(xs) - padding, max(xs) + padding, min(ys) - padding, max(ys) + padding)
        else:
            self._bounds_cache = (-1.0, 1.0, -1.0, 1.0)

        # Pre-sort visible nodes into deterministic visual layers once per graph load.
        self._ordered_node_ids = sorted(visible_ids, key=lambda nid: _node_layer_rank(self.graph, nodes[nid]))
        self._hit_nodes = [
            (nid, nodes[nid]["x"], nodes[nid]["y"], node_world_radius(nodes[nid]))
            for nid in reversed(self._ordered_node_ids)
        ]

        # Precompute connector geometry (especially arc angles) for visible edges.
        edges = []
        for left_id, right_id in self.graph["edges"]:
            if left_id not in self._visible_node_ids or right_id not in self._visible_node_ids:
                continue
            left, right = nodes[left_id], nodes[right_id]
            active = bool(left["allocated"] and right["allocated"])
            dimmed = _is_dimmed_ascendancy(self.graph, left.get("ascendancy_name"), bool(left.get("is_bloodline")))
            same_ring = (left["group_id"] == right["group_id"]
                         and left["orbit"] == right["orbit"]
                         and left["orbit_radius"] > 0)
            if same_ring:
                group = groups[left["group_id"]]
                angle1 = math.atan2(left["x"] - group["x"], group["y"] - left["y"]) % TWO_PI
                angle2 = math.atan2(right["x"] - group["x"], group["y"] - right["y"]) % TWO_PI
                if angle1 > angle2:
                    angle1, angle2 = angle2, angle1
                arc = angle2 - angle1
                if arc >= math.pi:
                    angle1, angle2 = angle2, angle1
                    arc = TWO_PI - arc
                edges.append({
                    "kind": "arc",
                    "active": active,
                    "dimmed": dimmed,
                    "gx": group["x"],
                    "gy": group["y"],
                    "radius": left["orbit_radius"],
                    "a1": angle1 - math.pi / 2.0,
                    "a2": angle1 + arc - math.pi / 2.0,
                })
            else:
                edges.append({
                    "kind": "line",
                    "active": active,
                    "dimmed": dimmed,
                    "x1": left["x"],
                    "y1": left["y"],
                    "x2": right["x"],
                    "y2": right["y"],
                })
        # Draw dimmed/unallocated edges first, then allocated edges on top.
        edges.sort(key=lambda e: (1 if e["active"] else 0, 0 if e["dimmed"] else 1))
        self._prepared_edges = edges

    def set_artwork(self, graph, image_bytes, errors):
        if graph is not self.graph:
            return
        self.art_warnings = [error for error in errors if "cache" in error.lower()
                             and ("could not be cached" in error.lower() or "cache is unavailable" in error.lower())]
        self.art_errors = [error for error in errors if error not in self.art_warnings]
        atlases = {}
        seen = set()
        for sprite in graph["sprites"].values():
            url = sprite["filename"]
            if url in seen or url not in image_bytes:
                continue
            seen.add(url)
            try:
                from PIL import Image
                sprite = next(item for item in graph["sprites"].values() if item["filename"] == url)
                with Image.open(BytesIO(image_bytes[url])) as image:
                    if (image.width > 5000 or image.height > 5000
                            or (image.width, image.height) != (sprite["w"], sprite["h"])):
                        raise ValueError("atlas dimensions do not match the pinned export")
                    premultiplied = image.convert("RGBa")
                red, green, blue, alpha = premultiplied.split()
                channels = (blue, green, red, alpha) if sys.byteorder == "little" else (alpha, red, green, blue)
                packed = Image.merge("RGBA", channels)
                pixels = bytearray(packed.tobytes())
                surface = cairo.ImageSurface.create_for_data(
                    pixels, cairo.FORMAT_ARGB32, sprite["w"], sprite["h"], sprite["w"] * 4)
                atlases[url] = surface
                self._atlas_buffers[url] = pixels
            except Exception as exc:
                self.art_errors.append(f"{url.rsplit('/', 1)[-1].split('?')[0]}: {exc}")
        self._atlases = atlases
        line = graph["sprites"].get("line", {})
        line_surface = atlases.get(line.get("filename"))
        if line_surface is not None:
            try:
                from PIL import Image
                with Image.open(BytesIO(image_bytes[line["filename"]])) as image:
                    for key in ("LineConnectorActive", "LineConnectorNormal"):
                        rect = line["coords"].get(key)
                        if not rect:
                            continue
                        crop = image.crop((rect["x"], rect["y"], rect["x"] + rect["w"], rect["y"] + rect["h"]))
                        visible = [pixel for pixel in crop.getdata() if pixel[3] > 64]
                        if visible:
                            self._line_colors[key] = tuple(
                                sum(px[channel] for px in visible) / len(visible) / 255.0
                                for channel in range(4)
                            )
            except Exception as exc:
                self.art_errors.append(f"line atlas sprite crops: {exc}")
        self.art_ready = len(atlases) == len({s["filename"] for s in graph["sprites"].values()})
        self._render_cache_key = None
        self.queue_draw()
        return self.art_errors

    def set_search_query(self, query: str) -> list[str]:
        self._search_query = query or ""
        matches = search_matching_nodes(self.graph, self._search_query, self._visible_node_ids or None)
        self._search_matches = set(matches)
        self._render_cache_key = None
        self.queue_draw()
        return matches

    def focus_node(self, node_id: str, min_zoom: float = 2.6):
        if not self.graph or node_id not in self.graph["nodes"]:
            return
        width = max(1, self.get_width())
        height = max(1, self.get_height())
        node = self.graph["nodes"][node_id]
        self.zoom = min(MAX_ZOOM, max(self.zoom, min_zoom))
        x1, x2, y1, y2 = self._bounds_cache
        scale = fit_scale(x2 - x1, y2 - y1, width, height, self.zoom)
        wx, wy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        self.pan_x = -(node["x"] - wx) * scale
        self.pan_y = -(node["y"] - wy) * scale
        self.selected_node = node_id
        self._on_select(node)
        self._notify_zoom()
        self.queue_draw()

    def fit(self):
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._notify_zoom()
        self.queue_draw()

    def fit_allocated(self):
        if not self.graph:
            self.fit()
            return
        allocated = [n for n in self.graph["nodes"].values() if n.get("allocated")]
        if not allocated:
            self.fit()
            return
        width = self.get_width()
        height = self.get_height()
        if width <= 1 or height <= 1:
            self._pending_fit_mode = "allocated"
            self.queue_draw()
            return
        padding = 950.0
        ax1 = min(n["x"] for n in allocated) - padding
        ax2 = max(n["x"] for n in allocated) + padding
        ay1 = min(n["y"] for n in allocated) - padding
        ay2 = max(n["y"] for n in allocated) + padding
        x1, x2, y1, y2 = self._bounds_cache
        base_scale = fit_scale(x2 - x1, y2 - y1, width, height, 1.0)
        target_scale = fit_scale(ax2 - ax1, ay2 - ay1, width, height, 1.0)
        self.zoom = min(4.5, max(0.85, target_scale / max(1e-9, base_scale)))
        scale = base_scale * self.zoom
        wx, wy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        target_cx, target_cy = (ax1 + ax2) / 2.0, (ay1 + ay2) / 2.0
        self.pan_x = -(target_cx - wx) * scale
        self.pan_y = -(target_cy - wy) * scale
        self._pending_fit_mode = None
        self._notify_zoom()
        self.queue_draw()

    def zoom_by(self, factor: float):
        width = max(1, self.get_width())
        height = max(1, self.get_height())
        self.zoom_at(factor, width / 2.0, height / 2.0)

    def zoom_at(self, factor: float, anchor_x: float, anchor_y: float):
        width = max(1, self.get_width())
        height = max(1, self.get_height())
        old_zoom = self.zoom
        new_zoom = min(MAX_ZOOM, max(MIN_ZOOM, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-6:
            return
        self.pan_x, self.pan_y = zoom_pan_offset(
            self.pan_x, self.pan_y, width, height, anchor_x, anchor_y, old_zoom, new_zoom
        )
        self.zoom = new_zoom
        self._notify_zoom()
        self.queue_draw()

    def _notify_zoom(self):
        if self._on_zoom_changed:
            self._on_zoom_changed(self.zoom)

    def _sprite_world_scale(self) -> float:
        sprite_scale = self.graph.get("sprite_scale", DEFAULT_SPRITE_SCALE) if self.graph else DEFAULT_SPRITE_SCALE
        return 1.0 / max(0.05, float(sprite_scale))

    def _transform(self, width, height):
        x1, x2, y1, y2 = self._bounds_cache
        scale = fit_scale(x2 - x1, y2 - y1, width, height, self.zoom)
        cx, cy = width / 2.0 + self.pan_x, height / 2.0 + self.pan_y
        wx, wy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        return scale, cx, cy, wx, wy

    @staticmethod
    def _screen(node, transform):
        scale, cx, cy, wx, wy = transform
        return cx + (node["x"] - wx) * scale, cy + (node["y"] - wy) * scale

    def _sprite(self, cr, family, key, center_x, center_y, scale, width_limit, height_limit,
                alpha=1.0, circular_clip=False):
        if not self.graph:
            return False
        sprite = self.graph["sprites"].get(family)
        if not sprite:
            return False
        rect = sprite["coords"].get(key)
        surface = self._atlases.get(sprite["filename"])
        if not rect or surface is None:
            return False
        world_mul = self._sprite_world_scale()
        width = max(1.5, rect["w"] * scale * world_mul)
        height = max(1.5, rect["h"] * scale * world_mul)
        half_w, half_h = width / 2.0, height / 2.0
        if (center_x + half_w < -2 or center_x - half_w > width_limit + 2
                or center_y + half_h < -2 or center_y - half_h > height_limit + 2):
            return False
        cr.save()
        if circular_clip:
            cr.arc(center_x, center_y, min(half_w, half_h) * 0.94, 0, TWO_PI)
            cr.clip()
            cr.translate(center_x - half_w, center_y - half_h)
            cr.scale(width / rect["w"], height / rect["h"])
            cr.set_source_surface(surface, -rect["x"], -rect["y"])
            cr.get_source().set_filter(cairo.FILTER_BILINEAR)
            if alpha >= 0.999:
                cr.paint()
            else:
                cr.paint_with_alpha(alpha)
        else:
            cr.translate(center_x - half_w, center_y - half_h)
            cr.scale(width / rect["w"], height / rect["h"])
            cr.set_source_surface(surface, -rect["x"], -rect["y"])
            cr.get_source().set_filter(cairo.FILTER_BILINEAR)
            cr.rectangle(0, 0, rect["w"], rect["h"])
            if alpha >= 0.999:
                cr.fill()
            else:
                cr.clip()
                cr.paint_with_alpha(alpha)
        cr.restore()
        return True

    def _cancel_zoom_settle(self):
        if self._zoom_settle_id is not None:
            GLib.source_remove(self._zoom_settle_id)
            self._zoom_settle_id = None

    def _schedule_zoom_settle(self):
        self._cancel_zoom_settle()
        self._zoom_settle_id = GLib.timeout_add(90, self._on_zoom_settled)

    def _on_zoom_settled(self):
        self._zoom_settle_id = None
        self.queue_draw()
        return GLib.SOURCE_REMOVE

    def _can_blit_interactive_cache(self, base_key, width: int, height: int) -> bool:
        if self._render_cache_surface is None or self._render_cache_view is None:
            return False
        if base_key != self._render_cache_key:
            return False
        c_zoom, c_pan_x, c_pan_y, cw, ch, overscan = self._render_cache_view
        if cw != width or ch != height or c_zoom <= 0:
            return False
        ratio = self.zoom / c_zoom
        if ratio < 0.45 or ratio > 2.2:
            return False
        # Keep the viewport covered by the cached overscan region.
        limit_x = overscan - 4
        limit_y = overscan - 4
        if ratio == 1.0 and (abs(self.pan_x - c_pan_x) > limit_x or abs(self.pan_y - c_pan_y) > limit_y):
            return False
        return True

    def _draw(self, area, cr, width, height):
        if self._pending_fit_mode and width > 1 and height > 1:
            mode = self._pending_fit_mode
            self._pending_fit_mode = None
            if mode == "allocated":
                self.fit_allocated()
        scale_factor = max(1, self.get_scale_factor())
        base_key = (id(self.graph), width, height, scale_factor,
                    len(self._atlases), tuple(self.art_errors), self._search_query,
                    self.show_all_ascendancies, len(self._diff_added), len(self._diff_removed),
                    self._diff_label)
        interactive = self._dragging or self._zoom_settle_id is not None

        if interactive and self._can_blit_interactive_cache(base_key, width, height):
            c_zoom, c_pan_x, c_pan_y, cw, ch, overscan = self._render_cache_view
            ratio = self.zoom / c_zoom
            cr.set_source_rgb(0.032, 0.042, 0.058)
            cr.paint()
            cr.save()
            cr.translate(width / 2.0 + self.pan_x, height / 2.0 + self.pan_y)
            if ratio != 1.0:
                cr.scale(ratio, ratio)
            cr.translate(-(overscan + cw / 2.0 + c_pan_x), -(overscan + ch / 2.0 + c_pan_y))
            cr.set_source_surface(self._render_cache_surface, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_FAST if ratio != 1.0 else cairo.FILTER_NEAREST)
            cr.paint()
            cr.restore()
            self._render_overlay(cr, width, height)
            return

        exact_view = (round(self.zoom, 5), round(self.pan_x, 2), round(self.pan_y, 2),
                      width, height, CACHE_OVERSCAN)
        cached_exact = (
            self._render_cache_surface is not None
            and self._render_cache_key == base_key
            and self._render_cache_view is not None
            and (round(self._render_cache_view[0], 5), round(self._render_cache_view[1], 2),
                 round(self._render_cache_view[2], 2), self._render_cache_view[3],
                 self._render_cache_view[4], self._render_cache_view[5]) == exact_view
        )
        if not cached_exact:
            overscan = CACHE_OVERSCAN
            surf_w = width + overscan * 2
            surf_h = height + overscan * 2
            pix_w = max(1, int(surf_w * scale_factor))
            pix_h = max(1, int(surf_h * scale_factor))
            surface = self._render_cache_surface
            if surface is None or surface.get_width() != pix_w or surface.get_height() != pix_h:
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pix_w, pix_h)
            surface.set_device_scale(scale_factor, scale_factor)
            renderer = cairo.Context(surface)
            renderer.set_antialias(cairo.ANTIALIAS_GOOD)
            self._render_base(area, renderer, surf_w, surf_h, viewport=(width, height, overscan))
            surface.flush()
            self._render_cache_key = base_key
            self._render_cache_surface = surface
            self._render_cache_view = (self.zoom, self.pan_x, self.pan_y, width, height, overscan)

        overscan = self._render_cache_view[5]
        cr.set_source_surface(self._render_cache_surface, -overscan, -overscan)
        cr.paint()
        self._render_overlay(cr, width, height)

    def _render_base(self, _area, cr, width, height, viewport=None):
        cr.set_source_rgb(0.032, 0.042, 0.058)
        cr.paint()
        if not self.graph:
            self._label(cr, width, height, "Choose a build and tree variant to inspect allocations")
            return
        if not self._atlases:
            msg = (f"Loading official {self.graph.get('version', 'PoE 1')} passive tree artwork…"
                   if not self.art_errors
                   else "Official tree artwork is unavailable · check the network and retry")
            self._label(cr, width, height, msg)
            return

        if viewport is not None:
            vw, vh, overscan = viewport
            scale, cx, cy, wx, wy = self._transform(vw, vh)
            transform = (scale, cx + overscan, cy + overscan, wx, wy)
        else:
            transform = self._transform(width, height)
            scale = transform[0]
        nodes = self.graph["nodes"]

        # Pan-aware subtle official background texture.
        background = self.graph["sprites"]["background"]
        bg_rect = next(iter(background["coords"].values()))
        bg_tile = self._atlases.get(background["filename"])
        if bg_tile:
            self._paint_tiled_background(cr, bg_tile, bg_rect, width, height)

        # Skill-group frames.
        group_spec = self.graph["sprites"]["groupBackground"]
        for group in self.graph.get("groups", {}).values():
            asc_name = group.get("ascendancy_name")
            if asc_name and not is_ascendancy_visible(
                self.graph, asc_name, bool(group.get("bloodline_family")),
                self.show_all_ascendancies, self._allocated_ascendancies,
            ):
                continue
            bg = group.get("background") or {}
            key = bg.get("image")
            if key and not group.get("is_proxy"):
                rect = group_spec["coords"].get(key)
                if rect:
                    gx, gy = self._screen(group, transform)
                    if bg.get("isHalfImage"):
                        self._sprite_mirrored_group(cr, group_spec, rect, gx, gy, scale, width, height)
                    else:
                        self._sprite(cr, "groupBackground", key, gx, gy, scale, width, height, alpha=0.82)

        # Ascendancy & bloodline portraits (only visible ones, eliminating sibling overlaps).
        for group in self.graph.get("groups", {}).values():
            name = group.get("ascendancy_sprite") if group.get("ascendancy_start") else None
            if name:
                asc_name = group.get("ascendancy_name")
                is_bloodline = bool(group.get("bloodline_family"))
                if not is_ascendancy_visible(
                    self.graph, asc_name, is_bloodline,
                    self.show_all_ascendancies, self._allocated_ascendancies,
                ):
                    continue
                family = group.get("bloodline_family") or "ascendancy"
                key = "Classes" + (asc_name if is_bloodline else name)
                if key in self.graph["sprites"].get(family, {}).get("coords", {}):
                    gx, gy = self._screen(group, transform)
                    selected = (asc_name == self.graph.get("selected_alternate_ascendancy")
                                if is_bloodline else asc_name == self.graph.get("selected_ascendancy"))
                    alpha = 0.94 if selected else 0.30
                    self._sprite(cr, family, key, gx, gy, scale, width, height, alpha=alpha)

        self._draw_connectors(cr, transform, width, height)

        # Nodes in deterministic layered order.
        selected_class = self.graph.get("selected_class", "")
        for node_id in self._ordered_node_ids:
            node = nodes[node_id]
            x, y = self._screen(node, transform)
            dimmed = _is_dimmed_ascendancy(self.graph, node.get("ascendancy_name"), bool(node.get("is_bloodline")))
            alpha = 0.30 if (dimmed and not node["allocated"]) else 1.0

            if node.get("class_start_index") is not None:
                class_info = self.graph.get("class_starts", {}).get(str(node["class_start_index"]), {})
                class_name = class_info.get("name", "")
                if node["allocated"] or (selected_class and class_name == selected_class):
                    self._sprite(cr, "startNode", "center" + class_name, x, y, scale, width, height)
                else:
                    self._sprite(cr, "startNode", "PSStartNodeBackgroundInactive", x, y, scale, width, height, alpha=0.85)
                continue

            if node.get("ascendancy_start"):
                family = node.get("bloodline_family") or "ascendancy"
                key = "AscendancyMiddle"
                if key not in self.graph["sprites"].get(family, {}).get("coords", {}):
                    family = "ascendancy"
                self._sprite(cr, family, key, x, y, scale, width, height, alpha=alpha)
                continue

            family = node["kind"]
            if family == "mastery":
                if node.get("selected_mastery_effect"):
                    self._sprite(cr, "masteryActiveEffect", node.get("active_effect_image"),
                                 x, y, scale, width, height, alpha=0.88 * alpha)
                if node["allocated"] and node.get("selected_mastery_effect"):
                    base_family, icon = "masteryActiveSelected", node.get("active_icon")
                else:
                    base_family, icon = "masteryInactive", node.get("inactive_icon")
                if icon not in self.graph["sprites"][base_family]["coords"]:
                    base_family, icon = "mastery", node.get("icon")
                if icon in self.graph["sprites"][base_family]["coords"]:
                    self._sprite(cr, base_family, icon, x, y, scale, width, height, alpha=alpha)
            else:
                art_family = family + ("Active" if node["allocated"] else "Inactive")
                icon = node.get("icon")
                if icon in self.graph["sprites"].get(art_family, {}).get("coords", {}):
                    # Clip opaque JPEG skill icons to a circle so square corners never bleed past frames.
                    self._sprite(cr, art_family, icon, x, y, scale, width, height,
                                 alpha=alpha, circular_clip=True)
                if node.get("ascendancy"):
                    frame_family = node.get("bloodline_family") or "ascendancy"
                    frame_key = (("AscendancyFrameLarge" if node["notable"] or node["keystone"]
                                  else "AscendancyFrameSmall")
                                 + ("Allocated" if node["allocated"] else "Normal"))
                else:
                    frame_family = "frame"
                    if node.get("jewel") and node.get("expansion_jewel"):
                        frame_key = "JewelSocketAltActive" if node["allocated"] else "JewelSocketAltNormal"
                    elif node["jewel"]:
                        frame_key = "JewelFrameAllocated" if node["allocated"] else "JewelFrameUnallocated"
                    elif node["keystone"]:
                        frame_key = "KeystoneFrameAllocated" if node["allocated"] else "KeystoneFrameUnallocated"
                    elif node["notable"]:
                        frame_key = "NotableFrameAllocated" if node["allocated"] else "NotableFrameUnallocated"
                    else:
                        frame_key = "PSSkillFrameActive" if node["allocated"] else "PSSkillFrame"
                self._sprite(cr, frame_family, frame_key, x, y, scale, width, height, alpha=alpha)

        # Diff highlights: emerald green for newly added nodes, crimson for respecced/removed nodes.
        if self._diff_added or self._diff_removed:
            for node_id in self._diff_added:
                node = nodes.get(node_id)
                if not node or node_id not in self._visible_node_ids:
                    continue
                x, y = self._screen(node, transform)
                if x < -40 or x > width + 40 or y < -40 or y > height + 40:
                    continue
                r = max(9.5, node_world_radius(node) * scale * 1.24)
                cr.set_source_rgba(0.05, 0.16, 0.09, 0.72)
                cr.set_line_width(4.8)
                cr.arc(x, y, r, 0, TWO_PI)
                cr.stroke()
                cr.set_source_rgba(0.24, 0.87, 0.50, 0.96)
                cr.set_line_width(2.4)
                cr.arc(x, y, r, 0, TWO_PI)
                cr.stroke()
            for node_id in self._diff_removed:
                node = nodes.get(node_id)
                if not node or node_id not in self._visible_node_ids:
                    continue
                x, y = self._screen(node, transform)
                if x < -40 or x > width + 40 or y < -40 or y > height + 40:
                    continue
                r = max(9.5, node_world_radius(node) * scale * 1.24)
                cr.set_source_rgba(0.20, 0.05, 0.06, 0.72)
                cr.set_line_width(4.8)
                cr.arc(x, y, r, 0, TWO_PI)
                cr.stroke()
                cr.set_source_rgba(0.95, 0.35, 0.37, 0.95)
                cr.set_line_width(2.3)
                cr.set_dash([5.0, 3.0])
                cr.arc(x, y, r, 0, TWO_PI)
                cr.stroke()
                cr.set_dash([])

        # Search highlights (drawn on base cache so they stay crisp during hover).
        if self._search_matches:
            for node_id in self._search_matches:
                node = nodes.get(node_id)
                if not node:
                    continue
                x, y = self._screen(node, transform)
                if x < -40 or x > width + 40 or y < -40 or y > height + 40:
                    continue
                r = max(10.0, node_world_radius(node) * scale * 1.25)
                cr.set_source_rgba(0.05, 0.14, 0.18, 0.72)
                cr.set_line_width(5.0)
                cr.arc(x, y, r, 0, TWO_PI)
                cr.stroke()
                cr.set_source_rgba(0.26, 0.88, 0.94, 0.95)
                cr.set_line_width(2.4)
                cr.arc(x, y, r, 0, TWO_PI)
                cr.stroke()

    def _render_overlay(self, cr, width, height):
        if not self.graph or not self._atlases:
            return
        transform = self._transform(width, height)
        scale = transform[0]
        nodes = self.graph["nodes"]

        if self.selected_node and self.selected_node in nodes:
            node = nodes[self.selected_node]
            x, y = self._screen(node, transform)
            r = max(9.0, node_world_radius(node) * scale * 1.15)
            cr.set_source_rgba(0.04, 0.05, 0.07, 0.75)
            cr.set_line_width(4.5)
            cr.arc(x, y, r, 0, TWO_PI)
            cr.stroke()
            cr.set_source_rgba(0.98, 0.82, 0.36, 0.98)
            cr.set_line_width(2.2)
            cr.arc(x, y, r, 0, TWO_PI)
            cr.stroke()

        if self.hovered_node and self.hovered_node in nodes and not self._dragging:
            node = nodes[self.hovered_node]
            x, y = self._screen(node, transform)
            r = max(9.0, node_world_radius(node) * scale * 1.18)
            cr.set_source_rgba(0.94, 0.96, 1.0, 0.92)
            cr.set_line_width(2.0)
            cr.arc(x, y, r, 0, TWO_PI)
            cr.stroke()
            if self._pointer is not None:
                self._draw_tooltip(cr, node, self._pointer[0], self._pointer[1], width, height)

        if self.art_errors:
            cr.set_source_rgba(0.38, 0.08, 0.08, 0.90)
            _rounded_rect(cr, 10, height - 36, width - 20, 26, 6)
            cr.fill()
            cr.set_source_rgb(1.0, 0.86, 0.84)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(11.5)
            cr.move_to(20, height - 19)
            cr.show_text("Some official artwork unavailable · retry online to complete this view")

    def _draw_tooltip(self, cr, node: dict, px: float, py: float, width: float, height: float):
        max_text_w = min(340, max(200, width - 40))
        pad_x, pad_y = 12, 10

        # Build header + badge + body lines.
        title_text = node.get("name", "Passive")
        flags = ["Allocated" if node.get("allocated") else "Unallocated"]
        node_id = str(node.get("id", ""))
        diff_label = getattr(self, "_diff_label", "")
        if node_id in getattr(self, "_diff_added", ()):
            flags.append(f"+ Added vs {diff_label}" if diff_label else "+ Added")
        elif node_id in getattr(self, "_diff_removed", ()):
            flags.append(f"− Respecced vs {diff_label}" if diff_label else "− Respecced")
        for key, label in (("keystone", "Keystone"), ("notable", "Notable"),
                           ("mastery", "Mastery"), ("jewel", "Jewel Socket"),
                           ("ascendancy", "Ascendancy")):
            if node.get(key):
                flags.append(label)
        subtitle_text = " · ".join(flags)

        body_lines = list(node.get("stats") or ())
        if node.get("mastery") and not body_lines:
            effects = node.get("mastery_effects") or ()
            if effects:
                body_lines.append("Available mastery options:")
                for eff in effects[:6]:
                    joined = "; ".join(eff.get("stats") or ())
                    if joined:
                        body_lines.append(f"• {joined}")
                if len(effects) > 6:
                    body_lines.append(f"… and {len(effects) - 6} more (click to inspect)")
        if not body_lines and node.get("class_start_index") is not None:
            body_lines = ["Starting point for this class."]

        def make_layout(text: str, font_desc: str, max_w: int):
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string(font_desc))
            layout.set_width(max_w * Pango.SCALE)
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            layout.set_text(text, -1)
            _, logical = layout.get_pixel_extents()
            return layout, logical.width, logical.height

        title_layout, tw, th = make_layout(title_text, "Sans Bold 10.5", max_text_w)
        sub_layout, sw, sh = make_layout(subtitle_text, "Sans 8.5", max_text_w)
        body_layout = None
        bw = bh = 0
        if body_lines:
            body_layout, bw, bh = make_layout("\n".join(body_lines), "Sans 9.5", max_text_w)

        box_w = max(tw, sw, bw) + pad_x * 2
        box_h = pad_y * 2 + th + 3 + sh + (8 + bh if body_layout else 0)

        # Position near cursor while keeping fully inside the canvas.
        tx = px + 18
        ty = py + 16
        if tx + box_w > width - 8:
            tx = px - box_w - 14
        if ty + box_h > height - 8:
            ty = py - box_h - 14
        tx = max(8.0, min(tx, width - box_w - 8.0))
        ty = max(8.0, min(ty, height - box_h - 8.0))

        # Drop shadow + dark card background + gold/slate border.
        cr.save()
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.45)
        _rounded_rect(cr, tx + 2, ty + 3, box_w, box_h, 8)
        cr.fill()

        cr.set_source_rgba(0.065, 0.09, 0.13, 0.96)
        _rounded_rect(cr, tx, ty, box_w, box_h, 8)
        cr.fill_preserve()
        if node.get("allocated") or node.get("keystone") or node.get("notable"):
            cr.set_source_rgba(0.84, 0.67, 0.34, 0.85)
        else:
            cr.set_source_rgba(0.32, 0.41, 0.52, 0.85)
        cr.set_line_width(1.2)
        cr.stroke()

        # Title color by node importance.
        if node.get("keystone"):
            cr.set_source_rgb(0.98, 0.84, 0.44)
        elif node.get("notable") or node.get("allocated"):
            cr.set_source_rgb(0.94, 0.80, 0.48)
        elif node.get("mastery"):
            cr.set_source_rgb(0.76, 0.72, 0.98)
        else:
            cr.set_source_rgb(0.92, 0.95, 0.98)
        cr.move_to(tx + pad_x, ty + pad_y)
        PangoCairo.show_layout(cr, title_layout)

        # Subtitle / status badge.
        if node.get("allocated"):
            cr.set_source_rgb(0.85, 0.72, 0.40)
        else:
            cr.set_source_rgb(0.60, 0.68, 0.77)
        cr.move_to(tx + pad_x, ty + pad_y + th + 3)
        PangoCairo.show_layout(cr, sub_layout)

        if body_layout:
            div_y = ty + pad_y + th + 3 + sh + 4
            cr.set_source_rgba(0.28, 0.36, 0.46, 0.55)
            cr.set_line_width(1.0)
            cr.move_to(tx + pad_x, div_y)
            cr.line_to(tx + box_w - pad_x, div_y)
            cr.stroke()

            cr.set_source_rgb(0.56, 0.74, 0.99)
            cr.move_to(tx + pad_x, div_y + 4)
            PangoCairo.show_layout(cr, body_layout)
        cr.restore()

    def _sprite_mirrored_group(self, cr, sprite, rect, x, y, scale, width_limit, height_limit):
        surface = self._atlases.get(sprite["filename"])
        if not surface:
            return
        world_mul = self._sprite_world_scale()
        half_w = rect["w"] * scale * world_mul / 2.0
        half_h = rect["h"] * scale * world_mul
        if (x + half_w < -2 or x - half_w > width_limit + 2
                or y + half_h < -2 or y - half_h > height_limit + 2):
            return
        for vertical_flip in (False, True):
            cr.save()
            cr.translate(x - half_w, y + (0 if vertical_flip else -half_h))
            cr.scale((2.0 * half_w) / rect["w"], (-1.0 if vertical_flip else 1.0) * half_h / rect["h"])
            if vertical_flip:
                cr.translate(0, -rect["h"])
            cr.rectangle(0, 0, rect["w"], rect["h"])
            cr.clip()
            cr.set_source_surface(surface, -rect["x"], -rect["y"])
            cr.get_source().set_filter(cairo.FILTER_BILINEAR)
            cr.paint_with_alpha(0.82)
            cr.restore()

    def _draw_connectors(self, cr, transform, width_limit, height_limit):
        scale, cx, cy, wx, wy = transform
        world_mul = self._sprite_world_scale()
        coords = self.graph["sprites"].get("line", {}).get("coords", {})
        active_h = coords.get("LineConnectorActive", {}).get("h", 13)
        normal_h = coords.get("LineConnectorNormal", {}).get("h", 13)
        active_w = connector_stroke_width(active_h, world_mul, scale, active=True)
        normal_w = connector_stroke_width(normal_h, world_mul, scale, active=False)

        margin = 16.0
        dimmed_normal = []
        normal = []
        active_edges = []

        for edge in self._prepared_edges:
            if edge["kind"] == "arc":
                gx = cx + (edge["gx"] - wx) * scale
                gy = cy + (edge["gy"] - wy) * scale
                radius = edge["radius"] * scale
                if (gx + radius < -margin or gx - radius > width_limit + margin
                        or gy + radius < -margin or gy - radius > height_limit + margin):
                    continue
                item = ("arc", gx, gy, radius, edge["a1"], edge["a2"])
            else:
                x1 = cx + (edge["x1"] - wx) * scale
                y1 = cy + (edge["y1"] - wy) * scale
                x2 = cx + (edge["x2"] - wx) * scale
                y2 = cy + (edge["y2"] - wy) * scale
                if (max(x1, x2) < -margin or min(x1, x2) > width_limit + margin
                        or max(y1, y2) < -margin or min(y1, y2) > height_limit + margin):
                    continue
                item = ("line", x1, y1, x2, y2)

            if edge["active"]:
                active_edges.append(item)
            elif edge["dimmed"]:
                dimmed_normal.append(item)
            else:
                normal.append(item)

        def trace_batch(items):
            for item in items:
                if item[0] == "arc":
                    cr.new_sub_path()
                    cr.arc(item[1], item[2], item[3], item[4], item[5])
                else:
                    cr.move_to(item[1], item[2])
                    cr.line_to(item[3], item[4])

        cr.save()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        if dimmed_normal:
            trace_batch(dimmed_normal)
            cr.set_source_rgba(0.34, 0.31, 0.27, 0.20)
            cr.set_line_width(normal_w)
            cr.stroke()
        if normal:
            trace_batch(normal)
            cr.set_source_rgba(0.34, 0.31, 0.27, 0.78)
            cr.set_line_width(normal_w)
            cr.stroke()
        if active_edges:
            trace_batch(active_edges)
            cr.set_source_rgba(0.05, 0.04, 0.02, 0.78)
            cr.set_line_width(active_w + 2.0)
            cr.stroke_preserve()
            cr.set_source_rgba(0.94, 0.77, 0.36, 0.96)
            cr.set_line_width(active_w)
            cr.stroke()
        cr.restore()

    @staticmethod
    def _screen_point(point, transform):
        scale, cx, cy, wx, wy = transform
        return cx + (point[0] - wx) * scale, cy + (point[1] - wy) * scale

    def _paint_tiled_background(self, cr, surface, rect, width, height):
        tile_w, tile_h = max(1, rect["w"]), max(1, rect["h"])
        ox = int(self.pan_x) % tile_w
        oy = int(self.pan_y) % tile_h
        cr.save()
        for y in range(oy - tile_h, height + tile_h, tile_h):
            for x in range(ox - tile_w, width + tile_w, tile_w):
                cr.set_source_surface(surface, x - rect["x"], y - rect["y"])
                cr.rectangle(x, y, tile_w, tile_h)
                cr.fill()
        # Subtle dark wash so passive nodes and gold paths stand out cleanly.
        cr.set_source_rgba(0.02, 0.03, 0.045, 0.28)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        cr.restore()

    @staticmethod
    def _label(cr, width, height, text):
        cr.set_source_rgb(0.68, 0.74, 0.82)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(14)
        extents = cr.text_extents(text)
        x = max(24.0, (width - extents.width) / 2.0)
        cr.move_to(x, height / 2.0)
        cr.show_text(text)

    def _hit_test(self, x: float, y: float) -> str | None:
        if not self.graph or not self._hit_nodes:
            return None
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return None
        scale, cx, cy, wx, wy = self._transform(width, height)
        if scale <= 0:
            return None
        inv_scale = 1.0 / scale
        px_w = wx + (x - cx) * inv_scale
        py_w = wy + (y - cy) * inv_scale
        min_r_w = 7.0 * inv_scale
        max_r_w = 46.0 * inv_scale
        closest = None
        best_score = float("inf")
        for node_id, nx, ny, base_r in self._hit_nodes:
            r = base_r
            if r < min_r_w:
                r = min_r_w
            elif r > max_r_w:
                r = max_r_w
            dx = nx - px_w
            if dx > r or dx < -r:
                continue
            dy = ny - py_w
            if dy > r or dy < -r:
                continue
            dist_sq = dx * dx + dy * dy
            r_sq = r * r
            if dist_sq <= r_sq:
                score = dist_sq / r_sq
                if score < best_score:
                    best_score = score
                    closest = node_id
        return closest

    def _update_cursor(self):
        if self._dragging:
            name = "grabbing"
        elif self.hovered_node:
            name = "pointer"
        else:
            name = "grab"
        self.set_cursor(Gdk.Cursor.new_from_name(name))

    def _on_motion(self, _controller, x, y):
        self._pointer = (x, y)
        if self._dragging:
            return
        hit = self._hit_test(x, y)
        if hit != self.hovered_node:
            self.hovered_node = hit
            self._update_cursor()
        if self.hovered_node or hit is not None:
            self.queue_draw()

    def _on_leave(self, _controller):
        self._pointer = None
        if self.hovered_node is not None:
            self.hovered_node = None
            self._update_cursor()
            self.queue_draw()

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.zoom_by(1.22)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.zoom_by(1.0 / 1.22)
            return True
        if keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self.fit()
            return True
        if keyval == Gdk.KEY_Escape and self.selected_node is not None:
            self.selected_node = None
            self._on_select(None)
            self.queue_draw()
            return True
        return False

    def _drag_begin(self, _gesture, x, y):
        self.grab_focus()
        self._drag_origin = (self.pan_x, self.pan_y)
        self._drag_start = (x, y)
        self._drag_moved = False
        self._dragging = False

    def _drag_update(self, _gesture, dx, dy):
        if abs(dx) + abs(dy) > 3:
            self._drag_moved = True
            if not self._dragging:
                self._dragging = True
                self._update_cursor()
        self.pan_x = self._drag_origin[0] + dx
        self.pan_y = self._drag_origin[1] + dy
        self.queue_draw()

    def _drag_end(self, _gesture, dx, dy):
        was_dragging = self._dragging
        self._dragging = False
        if not self._drag_moved:
            self._pick(self._drag_start[0] + dx, self._drag_start[1] + dy)
        elif self._pointer is not None:
            self.hovered_node = self._hit_test(*self._pointer)
        if was_dragging:
            self._update_cursor()
            self.queue_draw()

    def _pick(self, x, y):
        if not self.graph:
            return
        closest = self._hit_test(x, y)
        self.selected_node = closest
        self._on_select(self.graph["nodes"][closest] if closest else None)
        self.queue_draw()

    def _scroll(self, _controller, _dx, dy):
        if dy == 0:
            return False
        clamped_dy = max(-3.0, min(3.0, float(dy)))
        factor = 1.18 ** (-clamped_dy)
        if self._pointer is not None:
            ax, ay = self._pointer
        else:
            ax, ay = self.get_width() / 2.0, self.get_height() / 2.0
        self._schedule_zoom_settle()
        self.zoom_at(factor, ax, ay)
        return True

