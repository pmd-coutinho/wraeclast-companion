"""Bundled, version-pinned PoE 1 passive-tree geometry and graph preparation."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re

TREE_VERSIONS = {
    "3_29": ("3.29.1", "7e9f755e33152129ebf36c2ebdad639c527e4ad70d274b1fefb860f30ca01122"),
    "3_30": ("3.30.0", "9686b969bbd744a77f13eeadc57bb71be5032b0ed22be1ee4dbf2025649ccab0"),
}
MAX_TREE_JSON_BYTES = 12 * 1024 * 1024
_DATA_DIR = Path(__file__).with_name("data")
_TREE_CACHE: dict[str, dict] = {}


class TreeDataError(ValueError):
    """The saved PoB tree cannot be shown with the available exact tree data."""


def supported_tree_versions() -> tuple[str, ...]:
    return tuple(TREE_VERSIONS)


def _load_json(version: str) -> dict:
    if version not in TREE_VERSIONS:
        raise TreeDataError(f"Passive tree version {version or 'unknown'} is not bundled. Re-import with the matching tree data available.")
    cached = _TREE_CACHE.get(version)
    if cached is not None:
        return cached
    tag, expected_hash = TREE_VERSIONS[version]
    path = _DATA_DIR / f"skilltree-{tag}.json.gz"
    compressed = path.read_bytes()
    if len(compressed) > MAX_TREE_JSON_BYTES:
        raise TreeDataError("The bundled passive tree data exceeds its safety limit.")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            raw = stream.read(MAX_TREE_JSON_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise TreeDataError("The bundled passive tree data could not be decompressed.") from exc
    if len(raw) > MAX_TREE_JSON_BYTES or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise TreeDataError("The bundled passive tree data failed its pinned integrity check.")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TreeDataError("The bundled passive tree data is invalid JSON.") from exc
    if not isinstance(data.get("nodes"), dict) or not isinstance(data.get("groups"), dict):
        raise TreeDataError("The bundled passive tree data has an unsupported shape.")
    _TREE_CACHE[version] = data
    return data


def prepare_tree_graph(version: str, allocated_ids: list[int], class_name: str = "", ascendancy: str = "",
                       mastery_effects: str = "", class_id: int | None = None,
                       ascend_class_id: int | None = None, secondary_ascend_class_id: int | None = None) -> dict:
    """Prepare positions, labels, and adjacency using PoB's nonuniform orbit angles."""
    data = _load_json(version)
    constants = data["constants"]
    radii = constants["orbitRadii"]
    skills_per_orbit = constants["skillsPerOrbit"]
    # PoB stores mastery selections as {nodeId,effectId} pairs.  Never match an
    # effect globally: the same effect may be offered by several masteries.
    pairs = re.findall(r"\{\s*(\d+)\s*,\s*(\d+)\s*\}", mastery_effects or "")
    mastery_selections = {node: effect for node, effect in pairs}
    classes = data.get("classes", [])
    if not isinstance(class_id, int) or not (0 <= class_id < len(classes)):
        class_id = next((i for i, entry in enumerate(classes) if entry.get("name", "").lower() == class_name.lower()), None)
    selected_class = classes[class_id].get("name", "") if class_id is not None else class_name
    selected_ascendancy = ""
    if class_id is not None and isinstance(ascend_class_id, int) and ascend_class_id > 0:
        asc = classes[class_id].get("ascendancies", [])
        if ascend_class_id <= len(asc):
            selected_ascendancy = asc[ascend_class_id - 1].get("id", "")
    if not selected_ascendancy and ascend_class_id is None:
        selected_ascendancy = ascendancy
    alternate = data.get("alternate_ascendancies", [])
    retired_alternates = {"Warden", "Warlock", "Primalist"}
    selected_alternate = alternate[secondary_ascend_class_id - 1].get("id", "") if (
        isinstance(secondary_ascend_class_id, int) and 1 <= secondary_ascend_class_id <= len(alternate)) else ""
    if selected_alternate in retired_alternates:
        selected_alternate = ""
    bloodline_families = {item.get("id", ""): item.get("id", "")[:1].lower() + item.get("id", "")[1:] + "Bloodline"
                          for item in alternate if item.get("id") not in retired_alternates}
    nodes_by_id: dict[str, dict] = {}
    ascendancy_assets = {}
    for entry in classes:
        for asc in entry.get("ascendancies", []):
            ascendancy_assets[asc.get("id", "")] = asc.get("id", "")
            ascendancy_assets[asc.get("name", "")] = asc.get("id", "")
    # Some 3.29/3.30 exports use an internal display name while PoB's atlas key
    # remains the original ascendancy key (notably Warden -> Raider).
    ascendancy_assets["Warden"] = "Raider"
    for count in set(skills_per_orbit):
        if count == 16:
            angles = [0, 30, 45, 60, 90, 120, 135, 150, 180, 210, 225, 240, 270, 300, 315, 330]
        elif count == 40:
            angles = [0, 10, 20, 30, 40, 45, 50, 60, 70, 80, 90, 100, 110, 120, 130, 135, 140,
                      150, 160, 170, 180, 190, 200, 210, 220, 225, 230, 240, 250, 260, 270,
                      280, 290, 300, 310, 315, 320, 330, 340, 350]
        elif count > 0:
            angles = [360 * i / count for i in range(count)]
        else:
            angles = []
        if len(angles) < count:
            angles = [360 * i / count for i in range(count)]
        for node_id, node in data["nodes"].items():
            group_id = node.get("group")
            if group_id is None:
                continue
            group = data["groups"].get(str(group_id))
            expansion = node.get("expansionJewel") or {}
            if (node.get("isProxy") or node.get("ascendancyName") in retired_alternates
                    or not group or group.get("isProxy") or expansion.get("parent")):
                continue
            orbit = node.get("orbit")
            index = node.get("orbitIndex")
            if not group or not isinstance(orbit, int) or orbit >= len(radii) or not isinstance(index, int):
                continue
            orbit_count = skills_per_orbit[orbit] if orbit < len(skills_per_orbit) else 0
            if orbit_count != count or index >= len(angles):
                continue
            angle = math.radians(angles[index])
            radius = radii[orbit]
            x = group["x"] + math.sin(angle) * radius
            y = group["y"] - math.cos(angle) * radius
            selected_effect = mastery_selections.get(node_id)
            effect_data = next((effect for effect in node.get("masteryEffects", [])
                                if str(effect.get("effect")) == selected_effect), None)
            mastery_active = bool(node.get("isMastery") and int(node_id) in allocated_ids and effect_data)
            kind = "mastery" if node.get("isMastery") else "keystone" if node.get("isKeystone") else "notable" if node.get("isNotable") else "normal"
            nodes_by_id[node_id] = {"id": node_id, "x": x, "y": y,
                                    "name": node.get("name") or ("Mastery" if node.get("isMastery") else f"Passive {node_id}"),
                                    "stats": effect_data.get("stats", []) if mastery_active else node.get("stats", []),
                                    "mastery_effects": [
                                        {"effect": str(eff.get("effect", "")), "stats": list(eff.get("stats", []))}
                                        for eff in node.get("masteryEffects", [])
                                    ] if node.get("isMastery") else [],
                                    "reminder_text": list(node.get("reminderText", [])),
                                    "allocated": mastery_active if node.get("isMastery") else int(node_id) in allocated_ids,
                                    "mastery": bool(node.get("isMastery")), "notable": bool(node.get("isNotable")),
                                    "keystone": bool(node.get("isKeystone")), "jewel": bool(node.get("isJewelSocket")),
                                    "ascendancy": bool(node.get("ascendancyName") or node.get("isBloodline")),
                                    "class_start_index": node.get("classStartIndex"),
                                    "group_id": str(group_id), "orbit": orbit,
                                    "orbit_radius": radius,
                                    "icon": node.get("icon"), "active_icon": node.get("activeIcon"),
                                    "inactive_icon": node.get("inactiveIcon"),
                                    "active_effect_image": node.get("activeEffectImage"),
                                    "selected_mastery_effect": bool(mastery_active),
                                    "selected_mastery_id": selected_effect if mastery_active else None,
                                    "expansion_jewel": bool(node.get("expansionJewel")),
                                    "ascendancy_name": node.get("ascendancyName"),
                                    "is_bloodline": bool(node.get("isBloodline")),
                                    "bloodline_family": bloodline_families.get(node.get("ascendancyName")),
                                    "ascendancy_start": bool(node.get("isAscendancyStart")), "kind": kind}
    edges = set()
    for node_id, node in data["nodes"].items():
        if node_id not in nodes_by_id:
            continue
        for target in node.get("out", []):
            target = str(target)
            if target in nodes_by_id and target != node_id:
                left, right = data["nodes"][node_id], data["nodes"][target]
                lg = data["groups"].get(str(left.get("group")), {})
                rg = data["groups"].get(str(right.get("group")), {})
                if (left.get("type") != "ClassStart" and right.get("type") != "ClassStart"
                        and left.get("classStartIndex") is None and right.get("classStartIndex") is None
                        and not left.get("isMastery") and not right.get("isMastery")
                        and left.get("ascendancyName") == right.get("ascendancyName")
                        and not left.get("isProxy") and not right.get("isProxy")
                        and not lg.get("isProxy") and not rg.get("isProxy")):
                    edges.add(tuple(sorted((node_id, target))))
    unknown_allocations = sorted(set(map(str, allocated_ids)) - nodes_by_id.keys())
    # All image URLs and crop maps come from the checksum-pinned GGG export.
    # The UI only requests these fixed families and never trusts imported URLs.
    sprite_keys = ("background", "normalActive", "normalInactive", "notableActive", "notableInactive",
                   "keystoneActive", "keystoneInactive", "mastery", "masteryInactive", "masteryConnected",
                   "masteryActiveSelected", "masteryActiveEffect", "ascendancy", "startNode", "groupBackground", "frame")
    sprite_keys = sprite_keys + ("line",)
    sprite_keys = sprite_keys + tuple(dict.fromkeys(bloodline_families.values()))
    sprite_scale = 0.3835
    sprites = {key: data["sprites"][key][str(sprite_scale)] for key in sprite_keys}
    groups = {}
    for group_id, group in data["groups"].items():
        group_nodes = [data["nodes"].get(str(node_id), {}) for node_id in group.get("nodes", [])]
        ascendancy_start = next((node for node in group_nodes if node.get("isAscendancyStart")), None)
        if (group.get("isProxy") or any(node.get("ascendancyName") in retired_alternates
                                          for node in group_nodes)):
            continue
        groups[str(group_id)] = {"x": group["x"], "y": group["y"], "background": group.get("background"),
                                 "ascendancy_name": ascendancy_start.get("ascendancyName") if ascendancy_start else None,
                                 "ascendancy_sprite": ascendancy_assets.get(ascendancy_start.get("ascendancyName"), ascendancy_start.get("ascendancyName")) if ascendancy_start else None,
                                 "bloodline_family": bloodline_families.get(ascendancy_start.get("ascendancyName")) if ascendancy_start and ascendancy_start.get("isBloodline") else None,
                                 "ascendancy_start": bool(ascendancy_start),
                                 "is_half_image": bool((group.get("background") or {}).get("isHalfImage"))}
    class_starts = {str(index): {"name": item["name"].lower()} for index, item in enumerate(data["classes"])}
    return {"version": version, "nodes": nodes_by_id, "edges": sorted(edges),
            "groups": groups, "sprites": sprites, "sprite_scale": sprite_scale,
            "class_starts": class_starts,
            "ascendancy_assets": ascendancy_assets,
            "selected_class": selected_class.lower(), "selected_ascendancy": selected_ascendancy,
            "selected_alternate_ascendancy": selected_alternate,
            "alternate_sprite_family": bloodline_families.get(selected_alternate),
            "class_id": class_id,
            "allocated_count": len(allocated_ids), "unknown_allocations": unknown_allocations,
            "unsupported_transformations": True}
