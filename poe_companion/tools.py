"""Pure-Python helpers for Wraeclast Companion QoL features (regexes, run timer, item cards, gems, tree diff)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

REGEX_PREFS_PATH = Path.home() / ".config/poe-companion/regexes.json"
MAX_CUSTOM_REGEXES = 40

DEFAULT_REGEX_PRESETS = [
    {
        "id": "ms-boots",
        "category": "Leveling",
        "title": "Movement speed boots + 3-links",
        "regex": "nne|rint|b-b-b|g-g-g|r-r-r|b-b-g|b-g-b|g-b-b|g-g-b|g-b-g|b-g-g",
        "note": "Highlights Runner's/Sprinter's boots and common 3-link socket colors at Act 1–3 vendors.",
    },
    {
        "id": "caster-3l",
        "category": "Leveling",
        "title": "Caster 3-links (B-B-B / B-B-G) + MS",
        "regex": "nne|rint|b-b-b|b-b-g|b-g-b|g-b-b|r-b-b|b-r-b|b-b-r",
        "note": "Great for witch/templar/shadow league starts.",
    },
    {
        "id": "attack-3l",
        "category": "Leveling",
        "title": "Attack 3-links (G-G-R / R-R-G) + MS",
        "regex": "nne|rint|g-g-g|r-r-r|g-g-r|g-r-g|r-g-g|r-r-g|r-g-r|g-r-r",
        "note": "Great for ranger/duelist/marauder league starts.",
    },
    {
        "id": "4-link",
        "category": "Leveling",
        "title": "Any 4-link item",
        "regex": ".-.-.-",
        "note": "Quickly spot 4-links from Act 3 onward.",
    },
    {
        "id": "caster-wand",
        "category": "Vendors",
        "title": "+1 Spell / Added damage wands & sceptres",
        "regex": "to level of all|adds .* to .* (fire|cold|lightning) damage to spells|spell damage",
        "note": "Paste in town vendor search to spot early caster weapon upgrades.",
    },
    {
        "id": "life-res",
        "category": "Gear",
        "title": "Life + Elemental Resistances",
        "regex": "to maximum life.*(fire|cold|lightning|all elemental) resistance",
        "note": "Filter stash or vendor gear with both life and resists.",
    },
    {
        "id": "safe-maps",
        "category": "Endgame",
        "title": "Avoid reflect & no-regen maps",
        "regex": "\"!reflect|cannot regenerate|less recovery\"",
        "note": "Highlights safe rolled maps (excludes reflect and no-regen).",
    },
]

_STR_KEYWORDS = (
    "strength", "melee", "brutality", "fortify", "molten", "cleave", "slam", "strike",
    "bleed", "rage", "determination", "pride", "vitality", "steelskin", "immortal",
    "ancestral", "warbanner", "blood", "enduring", "leap", "shield charge", "flame dash",
    "lifetap", "empower", "inspiration", "fire", "burning", "ignite", "totem", "herald of ash",
    "righteous", "sunder", "earthquake", "boneshatter", "cyclone", "smite", "vengeance",
    "multistrike", "ruthless", "pulverise", "fist of war", "iron", "maim", "stun",
)
_DEX_KEYWORDS = (
    "dexterity", "arrow", "shot", "bow", "trap", "mine", "poison", "toxic", "caustic",
    "grace", "haste", "precision", "hatred", "blood rage", "dash", "blink", "whirling",
    "flurry", "blade", "viper", "cobra", "helix", "spectral", "flicker", "cyclone",
    "faster attacks", "mirage", "barrage", "vicious", "void manipulation", "deadly ailments",
    "swift", "nightblade", "elusive", "withering", "despair", "sniper", "mark", "herald of ice",
    "herald of agony", "cold", "frost", "ice", "projectile", "pierce", "chain", "fork",
    "gmp", "multiple projectiles", "ballista", "culling", "momentum", "onslaught", "enhance",
)
_INT_KEYWORDS = (
    "intelligence", "spell", "arcane", "lightning", "spark", "arc", "ball", "orb", "nova",
    "curse", "hex", "aura", "wrath", "zealotry", "discipline", "clarity", "purity",
    "malevolence", "tempest", "frostblink", "flame", "fireball", "cremation", "detonate",
    "minion", "zombie", "skeleton", "spectre", "golem", "srs", "raging spirit", "brand",
    "storm", "conductivity", "flammability", "frostbite", "elemental weakness", "assassin",
    "concentrated", "controlled destruction", "spell echo", "unleash", "faster casting",
    "efficacy", "infused", "energy leech", "power charge", "critical", "penetration",
    "herald of thunder", "wither", "bane", "blight", "essence drain", "contagion", "enlighten",
    "conductivity", "bone offering", "flesh offering", "spirit offering", "convocation",
    "desecrate", "unearth", "hydrosphere", "frost bomb", "wave of conviction", "arcanist",
)


def gem_socket_color(gem_name: str) -> str:
    """Return 'red', 'green', 'blue', or 'white' for a PoE gem display string."""
    low = (gem_name or "").strip().lower()
    if not low:
        return "white"
    if "portal" in low or "detonate mines" in low:
        return "white"
    # Specific high-priority overrides before broad keyword checks
    if any(k in low for k in ("frostblink", "flame dash", "flame wall", "fireball", "firestorm",
                              "righteous fire", "cremation", "flammability", "combustion",
                              "elemental focus", "immolate", "infernal legion", "arcanist brand")):
        return "blue"
    if any(k in low for k in ("lifetap", "determination", "vitality", "pride", "steelskin",
                              "immortal call", "molten shell", "leap slam", "shield charge",
                              "enduring cry", "ancestral", "herald of ash", "empower", "inspiration",
                              "brutality", "fortify", "multistrike", "melee physical", "cruelty",
                              "arrogance", "generosity", "damage on full life", "fire penetration")):
        return "red"
    if any(k in low for k in ("grace", "haste", "precision", "hatred", "blood rage", "dash",
                              "whirling blades", "faster attacks", "mirage archer", "void manipulation",
                              "deadly ailments", "unbound ailments", "swift affliction", "vicious projectiles",
                              "greater multiple", "lesser multiple", "pierce", "chain", "fork",
                              "sniper's mark", "poacher's mark", "herald of ice", "herald of agony",
                              "withering step", "phase run", "smoke mine", "culling strike", "enhance",
                              "cold penetration", "hypothermia", "ice bite", "momentum")):
        return "green"
    if any(k in low for k in _INT_KEYWORDS):
        return "blue"
    if any(k in low for k in _DEX_KEYWORDS):
        return "green"
    if any(k in low for k in _STR_KEYWORDS):
        return "red"
    return "blue"


def is_support_gem(gem_name: str) -> bool:
    low = (gem_name or "").lower()
    return "support" in low or any(
        low.startswith(p) for p in ("awakened ", "empower", "enlighten", "enhance")
    )


def parse_item_card(text: str) -> dict:
    """Parse raw PoB item text into a structured dict for PoE-style rarity card rendering."""
    raw_lines = [line.rstrip() for line in (text or "").splitlines()]
    rarity = "normal"
    header_lines: list[str] = []
    meta_lines: list[str] = []
    sections: list[list[str]] = []
    current_section: list[str] = []
    in_header = True
    meta_prefixes = (
        "Sockets:", "LevelReq:", "Implicits:", "Quality:", "Item Level:", "Armour:",
        "Evasion:", "Energy Shield:", "Ward:", "Unique ID:", "Variant:", "Selected Variant:",
        "League:", "Radius:", "Limited to:", "Requires ", "Physical Damage:", "Elemental Damage:",
        "Critical Strike Chance:", "Attacks per Second:", "Weapon Range:", "Chance to Block:",
    )

    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if low.startswith("rarity:"):
            val = low.split(":", 1)[1].strip()
            if val in ("unique", "rare", "magic", "normal"):
                rarity = val
            continue
        if stripped.startswith("--------"):
            if in_header:
                in_header = False
            elif current_section:
                sections.append(current_section)
                current_section = []
            continue
        if in_header:
            if any(stripped.startswith(p) for p in meta_prefixes):
                in_header = False
                meta_lines.append(stripped)
            else:
                header_lines.append(stripped)
        elif any(stripped.startswith(p) for p in meta_prefixes):
            meta_lines.append(stripped)
        else:
            current_section.append(stripped)

    if current_section:
        sections.append(current_section)

    name = header_lines[0] if header_lines else "Item"
    base = header_lines[1] if len(header_lines) > 1 else ""
    return {
        "rarity": rarity,
        "name": name,
        "base": base,
        "meta": meta_lines,
        "sections": sections,
    }


def compute_tree_diff(current_nodes: list[int] | set[int], compare_nodes: list[int] | set[int]) -> tuple[set[str], set[str]]:
    """Return (added_ids, removed_ids) as strings when comparing current_nodes against compare_nodes."""
    cur = {str(n) for n in (current_nodes or ())}
    prev = {str(n) for n in (compare_nodes or ())}
    return cur - prev, prev - cur


def load_regex_presets(path: Path = REGEX_PREFS_PATH) -> list[dict]:
    """Return built-in presets plus any valid user-saved custom regex presets."""
    presets = [dict(item, builtin=True) for item in DEFAULT_REGEX_PRESETS]
    try:
        if not path.exists() or path.stat().st_size > 128 * 1024:
            return presets
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return presets
        for idx, item in enumerate(raw[:MAX_CUSTOM_REGEXES]):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            regex = str(item.get("regex", "")).strip()
            if not title or not regex or len(title) > 120 or len(regex) > 250:
                continue
            presets.append({
                "id": str(item.get("id") or f"custom-{idx}"),
                "category": str(item.get("category") or "Custom")[:32],
                "title": title,
                "regex": regex,
                "note": str(item.get("note") or "")[:240],
                "builtin": False,
            })
    except (OSError, ValueError, TypeError):
        pass
    return presets


def save_custom_regexes(presets: list[dict], path: Path = REGEX_PREFS_PATH) -> None:
    custom = [
        {
            "id": item.get("id", ""),
            "category": item.get("category", "Custom"),
            "title": item.get("title", ""),
            "regex": item.get("regex", ""),
            "note": item.get("note", ""),
        }
        for item in presets
        if not item.get("builtin")
    ][:MAX_CUSTOM_REGEXES]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(custom, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class RunTimer:
    """Lightweight campaign run timer with per-act splits."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.running = False
        self._started_at = 0.0
        self._accumulated = 0.0
        self.splits: dict[int, float] = {}  # act_number (1-based) -> elapsed seconds when completed

    def elapsed(self) -> float:
        if self.running:
            return self._accumulated + max(0.0, self._clock() - self._started_at)
        return self._accumulated

    def start(self):
        if not self.running:
            self._started_at = self._clock()
            self.running = True

    def pause(self):
        if self.running:
            self._accumulated = self.elapsed()
            self.running = False

    def toggle(self):
        if self.running:
            self.pause()
        else:
            self.start()

    def reset(self):
        self.running = False
        self._started_at = 0.0
        self._accumulated = 0.0
        self.splits.clear()

    def record_act_transition(self, previous_act_index: int, new_act_index: int):
        """Record a split when moving forward to a later act (0-based indices)."""
        if new_act_index <= previous_act_index:
            return
        if not self.running and self._accumulated == 0.0:
            self.start()
        now_elapsed = self.elapsed()
        for act_idx in range(previous_act_index, new_act_index):
            act_num = act_idx + 1
            if act_num not in self.splits:
                self.splits[act_num] = now_elapsed

    def formatted(self) -> str:
        return format_duration(self.elapsed())


LEVEL_MILESTONES: dict[int, tuple[str, str]] = {
    4: ("Nessa (Act 1)", "Movement skills & early utility gems unlocked — grab a Quicksilver Flask too."),
    8: ("Nessa (Act 1)", "First support gems unlocked — link them to your main skill."),
    10: ("Nessa (Act 1)", "Check Nessa for movement speed boots and 3-link items."),
    12: ("Nessa (Act 1)", "Act 1 main skills unlocked (after Cavern of Wrath) — big power spike!"),
    16: ("Yeena (Act 2)", "Heralds & Act 2 skills unlocked after Intruders in Black."),
    18: ("Yeena (Act 2)", "Key Act 2 support gems unlocked after The Weaver's Chambers."),
    24: ("Clarissa (Act 3)", "Auras & curses unlocked — check Clarissa after Lost in Love."),
    28: ("Clarissa (Act 3)", "Act 3 main skills unlocked after Sever the Right Hand — look for a 4-link."),
    31: ("Siosa (Act 3 Library)", "Library support gems unlocked — huge damage upgrade!"),
    34: ("Petarus & Vanja (Act 4)", "Golems & Act 4 skills unlocked."),
    38: ("Petarus & Vanja (Act 4)", "Endgame support gems (Multistrike, Spell Echo, GMP, etc.) unlocked."),
    45: ("Labyrinth", "Good level to run Normal/Cruel Labyrinth for your next Ascendancy points."),
    55: ("Lilly Roth (Act 6)", "Lilly sells almost every gem — fix your 4-link and cap resistances at Crafting Bench."),
    68: ("Merciless Lab / Maps", "Run Merciless Lab before Act 10 Kitava, then cap 75% resists for maps."),
}

_ACT_VENDORS = {
    1: "Nessa",
    2: "Yeena",
    3: "Clarissa / Siosa",
    4: "Petarus & Vanja",
    5: "Lani",
}


def _active_build_gem_names(build: dict | None) -> list[str]:
    if not build:
        return []
    selected_id = build.get("selected_skill_set", build.get("active_skill_set", ""))
    skill_set = next((s for s in build.get("skill_sets", []) if s.get("id") == selected_id), None)
    groups = skill_set.get("groups", []) if skill_set else build.get("groups", [])
    names: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for gem in group.get("gems", []):
            clean = gem.split(" (Lv")[0].strip()
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                names.append(clean)
    return names


def level_milestone_cue(level: int, build: dict | None = None, act_number: int = 1) -> str | None:
    """Return a concise hand-hold alert string when hitting a PoE leveling breakpoint."""
    entry = LEVEL_MILESTONES.get(level)
    if entry is None:
        return None
    vendor, tip = entry
    gems = _active_build_gem_names(build)
    if gems and level in (4, 8, 12, 16, 18, 24, 28, 31, 34, 38, 55):
        preview = ", ".join(gems[:3])
        return f"Lv {level} · {vendor}: {tip} (Build gems: {preview})"
    return f"Lv {level} · {vendor}: {tip}"


def next_level_milestone(level: int) -> tuple[int, str] | None:
    for lvl in sorted(LEVEL_MILESTONES):
        if lvl > level:
            vendor, tip = LEVEL_MILESTONES[lvl]
            return lvl, f"{vendor} — {tip}"
    return None


def _color_letter(color: str) -> str:
    return {"red": "r", "green": "g", "blue": "b"}.get(color, "")


def _unique_link_patterns(letters: list[str]) -> list[str]:
    """Return deduplicated hyphenated socket-link permutations for 3- or 4-link vendor search."""
    from itertools import permutations
    seen: set[str] = set()
    out: list[str] = []
    for perm in permutations(letters):
        pat = "-".join(perm)
        if pat not in seen:
            seen.add(pat)
            out.append(pat)
    return out


def generate_build_vendor_regex(build: dict | None) -> dict | None:
    """Auto-generate a PoE vendor search regex (<= 250 chars) from the active build's socket colors."""
    if not build:
        return None
    selected_id = build.get("selected_skill_set", build.get("active_skill_set", ""))
    skill_set = next((s for s in build.get("skill_sets", []) if s.get("id") == selected_id), None)
    groups = skill_set.get("groups", []) if skill_set else build.get("groups", [])
    if not groups:
        return None

    # Sort groups by gem count descending so the main skill setup comes first.
    ranked = sorted((g for g in groups if g.get("gems")), key=lambda g: len(g.get("gems", [])), reverse=True)
    tokens: list[str] = ["nne", "rint"]  # Runner's / Sprinter's movement speed boots
    color_summaries: list[str] = []
    seen_tokens: set[str] = set(tokens)

    for group in ranked[:2]:
        gems = group.get("gems", [])
        letters = [_color_letter(gem_socket_color(g)) for g in gems[:4]]
        letters = [c for c in letters if c]
        if len(letters) < 3:
            continue
        # Use 3-link prefix for early leveling (always fits comfortably within 250 chars).
        three = letters[:3]
        summary = "-".join(c.upper() for c in three)
        if summary not in color_summaries:
            color_summaries.append(summary)
        for pat in _unique_link_patterns(three):
            if pat not in seen_tokens:
                candidate = "|".join(tokens + [pat])
                if len(candidate) <= 245:
                    tokens.append(pat)
                    seen_tokens.add(pat)

    if len(tokens) <= 2:
        # Fallback if all groups have < 3 gems: still offer MS boots + any 3-link
        tokens.append(".-.-")
        color_summaries.append("Any 3L")

    regex = "|".join(tokens)
    build_name = build.get("name") or "Active build"
    colors_text = " / ".join(color_summaries)
    return {
        "id": "build-auto-regex",
        "category": "Build",
        "title": f"{build_name} · Leveling Vendor ({colors_text} + MS Boots)",
        "regex": regex,
        "note": f"Auto-generated from your active skill set's main gem colors ({colors_text}) plus Runner's/Sprinter's movement speed boots.",
        "builtin": True,
    }

