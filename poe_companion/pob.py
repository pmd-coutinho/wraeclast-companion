"""Bounded, read-only import support for Path of Building Community exports."""
from __future__ import annotations

import base64
import binascii
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
import xml.etree.ElementTree as ET

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_NOTES_CHARS = 128 * 1024
MAX_SETS = 32
MAX_GROUPS_PER_SET = 128
MAX_GEMS_PER_GROUP = 64
MAX_GEMS_TOTAL = 2048
MAX_FIELD_CHARS = 1024
MAX_TREE_NODES = 2048
MAX_TREE_SPECS = 32
MAX_LINKED_LOADOUTS = 128
MAX_ITEM_SETS = 32
MAX_ITEMS = 512
MAX_SLOTS_PER_ITEM_SET = 128
MAX_ALL_ITEM_SLOTS = 2048
MAX_ITEM_TEXT_CHARS = 32 * 1024
MAX_ALL_ITEM_TEXT_CHARS = 512 * 1024
POBBIN_TIMEOUT_SECONDS = 12
POBBIN_HOSTS = {"pobb.in", "www.pobb.in"}
POBBIN_USER_AGENT = "WraeclastCompanion/0.1 (Path of Exile build import)"


class ImportError(ValueError):
    """A user-facing build import error."""


def _pobb_path(path: str) -> str | None:
    """Return the API raw path for a supported public pobb.in share path."""
    parts = path.split("/")
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if len(parts) == 2 and parts[1] == "raw" and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[0]):
        return f"/{parts[0]}/raw"
    if (len(parts) == 4 and parts[0] == "u" and parts[3] == "raw"
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", parts[1])
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[2])):
        return f"/u/{parts[1]}/{parts[2]}/raw"
    if len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[0]):
        return f"/{parts[0]}/raw"
    if (len(parts) == 3 and parts[0] == "u"
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", parts[1])
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[2])):
        return f"/u/{parts[1]}/{parts[2]}/raw"
    return None


def _is_pobb_raw_path(path: str) -> bool:
    parts = path.split("/")
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return ((len(parts) == 2 and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[0]) and parts[1] == "raw")
            or (len(parts) == 4 and parts[0] == "u"
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", parts[1])
                and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[2]) and parts[3] == "raw"))


def pobbin_raw_url(value: str) -> str:
    """Validate a public share link and map it to pobb.in's documented raw API."""
    value = value.strip()
    if not value:
        raise ImportError("Paste a public pobb.in share link.")
    candidate = value if "://" in value else f"https://{value}"
    try:
        parts = urllib.parse.urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise ImportError("That pobb.in link is malformed.") from exc
    if (parts.scheme.lower() != "https" or parts.hostname is None
            or parts.hostname.lower() not in POBBIN_HOSTS or parts.username is not None
            or parts.password is not None or port is not None):
        raise ImportError("Only HTTPS links hosted by pobb.in can be imported.")
    raw_path = _pobb_path(parts.path)
    if raw_path is None:
        raise ImportError("Use a public pobb.in build link such as https://pobb.in/BUILD_ID.")
    return f"https://pobb.in{raw_path}"


def looks_like_share_url(value: str) -> bool:
    """Recognize URL-shaped input so unsupported URLs get a clear error."""
    value = value.strip()
    return (value.lower().startswith(("https://", "http://"))
            or bool(re.match(r"(?i)^(?:www\.)?pobb\.in(?:/|$)", value)))


class _PobbinRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        try:
            valid = (parts.scheme.lower() == "https" and parts.hostname is not None
                     and parts.hostname.lower() in POBBIN_HOSTS and parts.port is None
                     and parts.username is None and parts.password is None
                     and _is_pobb_raw_path(parts.path))
        except ValueError:
            valid = False
        if not valid:
            raise ImportError("The pobb.in server redirected outside its HTTPS raw-share endpoint; import was stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_pobbin_build(value: str) -> dict:
    """Fetch and parse a bounded public raw share from pobb.in."""
    endpoint = pobbin_raw_url(value)
    request = urllib.request.Request(endpoint, headers={
        "User-Agent": POBBIN_USER_AGENT,
        "Accept": "text/plain, application/octet-stream;q=0.9, */*;q=0.1",
    })
    opener = urllib.request.build_opener(_PobbinRedirectHandler())
    try:
        with opener.open(request, timeout=POBBIN_TIMEOUT_SECONDS) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if (final.scheme != "https" or final.hostname not in POBBIN_HOSTS or final.port is not None
                    or final.username is not None or final.password is not None
                    or not _is_pobb_raw_path(final.path)):
                raise ImportError("The pobb.in server returned a response outside its HTTPS raw-share endpoint.")
            body = response.read(MAX_INPUT_BYTES + 1)
    except ImportError:
        raise
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            raise ImportError("That pobb.in build was not found. Check the link and try again.") from exc
        raise ImportError(f"pobb.in could not return this build (HTTP {exc.code}). Try again later.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ImportError(f"Could not reach pobb.in ({exc.reason if isinstance(exc, urllib.error.URLError) else 'network timeout or error'}). Check your connection and retry.") from exc
    if len(body) > MAX_INPUT_BYTES:
        raise ImportError(f"The pobb.in response is larger than the {MAX_INPUT_BYTES // (1024 * 1024)} MiB import limit.")
    try:
        raw = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportError("pobb.in returned data that was not valid text.") from exc
    return parse_build(raw)


def _bounded_text(value: str | None, field: str, limit: int = MAX_FIELD_CHARS) -> str:
    value = value or ""
    if len(value) > limit:
        raise ImportError(f"This build has an unusually large {field} field; shorten it in Path of Building and try again.")
    return value


def _decompress_limited(raw: bytes, window: int) -> bytes | None:
    try:
        decoder = zlib.decompressobj(window)
        data = decoder.decompress(raw, MAX_XML_BYTES + 1)
    except zlib.error:
        return None
    if len(data) > MAX_XML_BYTES or decoder.unconsumed_tail or not decoder.eof:
        raise ImportError(f"The decoded build is larger than the {MAX_XML_BYTES // (1024 * 1024)} MiB XML limit.")
    return data


def decode_share_code(value: str) -> bytes:
    """Decode standard Base64(zlib(XML)) share codes or pasted XML under strict caps."""
    value = value.strip()
    if not value:
        raise ImportError("Paste a Path of Building share code or XML export.")
    if value.startswith("<?xml") or value.startswith("<PathOfBuilding"):
        data = value.encode("utf-8")
        if len(data) > MAX_INPUT_BYTES:
            raise ImportError(f"Pasted XML is larger than the {MAX_INPUT_BYTES // (1024 * 1024)} MiB import limit.")
        return data
    if "#" in value:
        value = value.rsplit("#", 1)[-1]
    value = re.sub(r"\s+", "", value)
    if len(value) > MAX_INPUT_BYTES:
        raise ImportError(f"Share code is larger than the {MAX_INPUT_BYTES // (1024 * 1024)} MiB import limit.")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImportError("That does not look like a PoB share code or XML.") from exc
    for window in (zlib.MAX_WBITS, -zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
        data = _decompress_limited(raw, window)
        if data is not None:
            if not data.lstrip().startswith(b"<"):
                continue
            return data
    raise ImportError("Could not decompress this code. Copy the full code from Path of Building Community.")


def _text(el: ET.Element | None) -> str:
    return "\n".join(t.strip() for t in el.itertext() if t.strip()) if el is not None else ""


def _read_gem(gem: ET.Element) -> str | None:
    if gem.get("enabled", "true").lower() == "false":
        return None
    name = gem.get("nameSpec") or gem.get("skillId") or gem.get("name") or "Gem"
    name = _bounded_text(name, "gem name")
    if gem.get("level"):
        name += f" (Lv {gem.get('level')})"
    return name


def _read_skill_groups(nodes: list[ET.Element]) -> list[dict]:
    if len(nodes) > MAX_GROUPS_PER_SET:
        raise ImportError("This build has too many skill groups to display.")
    groups = []
    total_gems = 0
    for index, group in enumerate(nodes, 1):
        if group.get("enabled", "true").lower() == "false":
            continue
        gems = []
        for gem in group.findall("Gem"):
            value = _read_gem(gem)
            if value:
                gems.append(value)
                total_gems += 1
                if len(gems) > MAX_GEMS_PER_GROUP or total_gems > MAX_GEMS_TOTAL:
                    raise ImportError("This build has too many enabled gems to display.")
        if gems:
            label = group.get("label") or group.get("title") or f"Skill group {index}"
            groups.append({"label": _bounded_text(label, "skill group label"), "gems": gems})
    return groups


def _read_tree_specs(root: ET.Element) -> tuple[list[dict], str]:
    tree = root.find("Tree")
    if tree is None:
        return [], ""
    nodes = tree.findall("Spec")
    if len(nodes) > MAX_TREE_SPECS:
        raise ImportError("This build has too many passive tree variants to display.")
    active = tree.get("activeSpec", "1")
    specs = []
    for index, spec in enumerate(nodes, 1):
        raw_allocated = re.findall(r"\d+", spec.get("nodes", ""))
        if any(int(value) > 999999 for value in raw_allocated):
            raise ImportError("This build contains a passive node ID outside the supported range (0–999999). Nothing was imported.")
        allocated = [int(value) for value in raw_allocated]
        if len(allocated) > MAX_TREE_NODES:
            raise ImportError("This build has too many allocated passive nodes to display.")
        def optional_id(key):
            value = spec.get(key)
            if value is None or value.strip().lower() in ("", "nil"):
                return None
            if not value.isdigit() or int(value) > 255:
                raise ImportError(f"This tree variant has an invalid {key} value.")
            return int(value)
        specs.append({"id": str(index), "title": _bounded_text(spec.get("title") or "Default", "tree title"),
                      "tree_version": _bounded_text(spec.get("treeVersion", ""), "tree version"),
                      "nodes": list(dict.fromkeys(allocated)),
                      "mastery_effects": _bounded_text(spec.get("masteryEffects", ""), "mastery selection"),
                      "class_id": optional_id("classId"), "ascend_class_id": optional_id("ascendClassId"),
                      "secondary_ascend_class_id": optional_id("secondaryAscendClassId")})
    active_index = int(active) if active.isdigit() else 1
    active_index = min(max(active_index, 1), len(specs)) if specs else 0
    return specs, str(active_index) if active_index else ""


def _read_item_sets(root: ET.Element) -> tuple[list[dict], str, dict[str, str]]:
    items_root = root.find("Items")
    if items_root is None:
        return [], "", {}
    item_nodes = items_root.findall("Item")
    if len(item_nodes) > MAX_ITEMS:
        raise ImportError("This build has too many saved items to display.")
    items = {item.get("id", ""): _text(item) for item in item_nodes}
    if any(len(text) > MAX_ITEM_TEXT_CHARS for text in items.values()) or sum(map(len, items.values())) > MAX_ALL_ITEM_TEXT_CHARS:
        raise ImportError("The saved item descriptions exceed the viewing size limit.")
    sets = items_root.findall("ItemSet")
    if len(sets) > MAX_ITEM_SETS:
        raise ImportError("This build has too many gear sets to display.")
    result = []
    total_slots = 0
    for index, item_set in enumerate(sets, 1):
        slot_nodes = item_set.findall("Slot")
        total_slots += len(slot_nodes)
        if len(slot_nodes) > MAX_SLOTS_PER_ITEM_SET or total_slots > MAX_ALL_ITEM_SLOTS:
            raise ImportError("This build has too many gear slots to display.")
        slots = []
        for slot in slot_nodes:
            item_id = slot.get("itemId", "0")
            if not item_id.isdigit():
                continue
            full_text = items.get(item_id, "") if item_id != "0" else ""
            lines = [line.strip() for line in full_text.splitlines() if line.strip()]
            title = next((line for line in lines if not line.lower().startswith(("rarity:", "armour:", "evasion:", "energy shield:"))), "")
            slots.append({"name": _bounded_text(slot.get("name") or f"Slot {len(slots) + 1}", "item slot name"),
                          "item_id": item_id, "title": _bounded_text(title, "item title")})
        result.append({"id": item_set.get("id", str(index)),
                       "title": _bounded_text(item_set.get("title") or "Default", "gear set title"),
                       "slots": slots})
    return result, items_root.get("activeItemSet", result[0]["id"] if result else ""), items


def _linked_loadouts(tree_specs: list[dict], item_sets: list[dict], skill_sets: list[dict], config_sets=None) -> list[dict]:
    """Mirror PoB SyncLoadouts' exact-title, explicit-ID, and singleton matching."""
    def title_map(values):
        return {item["title"]: item for item in values}

    items_by_title = title_map(item_sets)
    skills_by_title = title_map(skill_sets)
    config_sets = config_sets or []
    configs_by_title = title_map(config_sets)
    one_config = len(config_sets) <= 1
    links = []
    for spec in tree_specs:
        spec_title = spec["title"]
        match = re.search(r"\{([\w,]+)\}", spec_title)
        explicit = match.group(1).split(",") if match else []
        clean_title = re.sub(r"\{[\w,]+\}", "", spec_title).strip() or "Default"
        if explicit:
            for link_id in explicit:
                item = next((value for value in item_sets if (m := re.search(r"\{([\w,]+)\}", value["title"])) and link_id in m.group(1).split(",")), None)
                skill = next((value for value in skill_sets if (m := re.search(r"\{([\w,]+)\}", value["title"])) and link_id in m.group(1).split(",")), None)
                config = next((value for value in config_sets if (m := re.search(r"\{([\w,]+)\}", value["title"])) and link_id in m.group(1).split(",")), None)
                candidate_item = item or (item_sets[0] if len(item_sets) == 1 else None)
                candidate_skill = skill or (skill_sets[0] if len(skill_sets) == 1 else None)
                if candidate_item is not None and candidate_skill is not None and (one_config or config is not None):
                    links.append({"title": clean_title, "link_id": link_id, "tree_spec_id": spec["id"],
                                  "item_set_id": candidate_item["id"], "skill_set_id": candidate_skill["id"]})
            continue
        spec_title_effective = spec_title or "Default"
        title_for_match = f"[{spec.get('tree_display_version')}] {spec_title_effective}" if spec.get("tree_display_version") else spec_title_effective
        item = items_by_title.get(title_for_match)
        skill = skills_by_title.get(title_for_match)
        config = configs_by_title.get(title_for_match)
        candidate_item = item or (item_sets[0] if len(item_sets) == 1 else None)
        candidate_skill = skill or (skill_sets[0] if len(skill_sets) == 1 else None)
        if candidate_item is not None and candidate_skill is not None and (one_config or config is not None):
            links.append({"title": spec_title, "link_id": "", "tree_spec_id": spec["id"],
                          "item_set_id": candidate_item["id"], "skill_set_id": candidate_skill["id"]})
    if len(links) > MAX_LINKED_LOADOUTS:
        raise ImportError(f"This build defines more than {MAX_LINKED_LOADOUTS} linked loadouts to display.")
    return links


def _read_config_sets(root: ET.Element) -> list[dict]:
    config = root.find("Config")
    if config is None:
        return []
    sets = config.findall("ConfigSet")
    if len(sets) > MAX_SETS:
        raise ImportError("This build has too many configuration sets to display.")
    return [{"id": item.get("id", str(i)), "title": _bounded_text(item.get("title") or "Default", "configuration set title")}
            for i, item in enumerate(sets, 1)]


def parse_build(source: str | bytes, name: str = "Imported build") -> dict:
    data = source if isinstance(source, bytes) else decode_share_code(source)
    if len(data) > MAX_XML_BYTES:
        raise ImportError(f"The decoded build is larger than the {MAX_XML_BYTES // (1024 * 1024)} MiB XML limit.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ImportError(f"Invalid Path of Building XML: {exc}") from exc
    if root.tag != "PathOfBuilding":
        raise ImportError("This XML is not a Path of Building export.")
    build = root.find("Build")
    skills = root.find("Skills")
    tree = root.find("Tree")
    character = _bounded_text(build.get("className", "") if build is not None else "", "class")
    ascendancy = _bounded_text(build.get("ascendClassName", "") if build is not None else "", "ascendancy")
    level = _bounded_text(build.get("level", "") if build is not None else "", "level")
    notes = _text(root.find("Notes"))
    if len(notes) > MAX_NOTES_CHARS:
        raise ImportError(f"Build notes are larger than the {MAX_NOTES_CHARS // 1024} KiB display limit.")

    skill_sets = []
    active_set_id = ""
    if skills is not None:
        active_set_id = skills.get("activeSkillSet", "")
        set_nodes = skills.findall("SkillSet")
        if len(set_nodes) > MAX_SETS:
            raise ImportError("This build has too many named skill sets to display.")
        if set_nodes:
            for index, skill_set in enumerate(set_nodes, 1):
                set_id = skill_set.get("id", str(index))
                title = _bounded_text(skill_set.get("title") or "Default", "skill set title")
                groups = _read_skill_groups(skill_set.findall("Skill"))
                skill_sets.append({"id": set_id, "title": title, "groups": groups})
        else:
            # Older PoB exports stored socket groups directly under Skills.
            groups = _read_skill_groups(skills.findall("Skill"))
            skill_sets.append({"id": "1", "title": "Skill groups", "groups": groups})

    active_set = next((item for item in skill_sets if item["id"] == active_set_id), None)
    if active_set is None and skill_sets:
        active_set = skill_sets[0]
        active_set_id = active_set["id"]
    groups = active_set["groups"] if active_set else []

    tree_specs, active_tree_spec = _read_tree_specs(root)
    variants = [spec["title"] for spec in tree_specs]
    item_sets, active_item_set, items = _read_item_sets(root)
    # PoB prefixes exact tree titles with the display version for non-latest trees.
    from .passive_tree import TREE_VERSIONS
    for spec in tree_specs:
        tag = TREE_VERSIONS.get(spec.get("tree_version"), (None,))[0]
        # PoB's current tree title is plain; older tree exports retain the
        # version prefix when matching corresponding gear/skill/config titles.
        spec["tree_display_version"] = tag.rsplit(".", 1)[0] if tag and spec.get("tree_version") != "3_30" else None
    linked_loadouts = _linked_loadouts(tree_specs, item_sets, skill_sets, _read_config_sets(root))
    return {"name": _bounded_text(name, "build name"), "class": character, "ascendancy": ascendancy,
            "level": level, "notes": notes, "groups": groups, "skill_sets": skill_sets,
            "active_skill_set": active_set_id, "selected_skill_set": active_set_id,
            "tree_specs": tree_specs, "active_tree_spec": active_tree_spec,
            "selected_tree_spec": active_tree_spec,
            "tree_version": tree_specs[int(active_tree_spec) - 1]["tree_version"] if active_tree_spec else "",
            "variants": variants, "item_sets": item_sets, "items": items, "active_item_set": active_item_set,
            "selected_item_set": active_item_set, "linked_loadouts": linked_loadouts}
