import base64
import json
import math
import re
import tempfile
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from unittest import mock

from poe_companion.pob import (ImportError, _PobbinRedirectHandler, decode_share_code,
                               _linked_loadouts, download_pobbin_build, parse_build, pobbin_raw_url)
from poe_companion.policy import advance, is_game_focus, toggle_clickable
from poe_companion import app as app_module
from poe_companion.passive_tree import _load_json, prepare_tree_graph, supported_tree_versions
from poe_companion.tree_art import (_CDNRedirect, _PoBSourceRedirect, _pob_source_url,
                                    _trusted_url, _valid_image)
from poe_companion.tree_canvas import (PassiveTreeCanvas, connector_stroke_width, fit_scale,
                                       is_ascendancy_visible, search_matching_nodes, zoom_pan_offset)
from poe_companion.client_log import ClientLogTailer, discover_client_log, match_campaign_step, parse_entered_zone
from poe_companion.notes import format_note_spans, plain_note_summary, strip_color_escapes
from poe_companion import tray

ROOT = Path(__file__).parents[1]


class PathOfBuildingTests(unittest.TestCase):
    def setUp(self):
        self.xml = b'''<?xml version="1.0"?><PathOfBuilding><Build className="Witch" ascendClassName="Elementalist" level="90"/><Tree><Spec title="Campaign"/><Spec title="Mapping"/></Tree><Skills><Skill title="Main setup"><Gem nameSpec="Fireball" level="20"/><Gem nameSpec="Spell Echo"/></Skill><Skill title="Disabled"><Gem nameSpec="Bad Gem" enabled="false"/></Skill></Skills><Notes>Use life and resistance gear.</Notes></PathOfBuilding>'''

    def test_plain_xml_fields(self):
        build = parse_build(self.xml)
        self.assertEqual(build["class"], "Witch")
        self.assertEqual(build["ascendancy"], "Elementalist")
        self.assertEqual(build["notes"], "Use life and resistance gear.")
        self.assertEqual(build["groups"][0]["gems"], ["Fireball (Lv 20)", "Spell Echo"])
        self.assertEqual(build["variants"], ["Campaign", "Mapping"])
        self.assertEqual(len(build["groups"]), 1)

    def test_standard_base64_zlib_share_code(self):
        code = base64.b64encode(zlib.compress(self.xml)).decode().rstrip("=")
        self.assertEqual(parse_build(code)["class"], "Witch")
        self.assertTrue(decode_share_code(code).startswith(b"<?xml"))

    def test_modern_named_skill_sets_and_url_safe_share_code(self):
        modern = b'''<?xml version="1.0"?><PathOfBuilding><Build className="Ranger" ascendClassName="Deadeye" level="32"/><Tree><Spec title="Campaign tree"/></Tree><Skills activeSkillSet="2"><SkillSet id="1" title="Endgame"><Skill label="Tornado Shot" enabled="true"><Gem nameSpec="Tornado Shot"/><Gem nameSpec="Mirage Archer Support"/></Skill></SkillSet><SkillSet id="2" title="Acts 1-4"><Skill label="Leveling bow" enabled="true"><Gem nameSpec="Toxic Rain" level="12"/><Gem nameSpec="Ballista Totem Support"/></Skill><Skill label="Disabled weapon swap" enabled="false"><Gem nameSpec="Never show"/></Skill><Skill label="Disabled gem"><Gem nameSpec="Disabled Support" enabled="false"/></Skill></SkillSet></Skills><Notes>Use a 4-link before the next boss.</Notes></PathOfBuilding>'''
        code = base64.urlsafe_b64encode(zlib.compress(modern)).decode().rstrip("=")
        build = parse_build(code)
        self.assertEqual(build["active_skill_set"], "2")
        self.assertEqual([item["title"] for item in build["skill_sets"]], ["Endgame", "Acts 1-4"])
        self.assertEqual(build["groups"], [{"label": "Leveling bow", "gems": ["Toxic Rain (Lv 12)", "Ballista Totem Support"]}])
        self.assertEqual(build["skill_sets"][0]["groups"][0]["gems"][0], "Tornado Shot")

    def test_allocations_gear_items_and_linked_loadouts_import(self):
        xml = b'''<PathOfBuilding><Build className="Templar" ascendClassName="Hierophant" level="78"/>
          <Tree activeSpec="2"><Spec title="Campaign {ACTS}" treeVersion="3_29" nodes="14057,59005" masteryEffects="{1,2}"/>
            <Spec title="Endgame {MAPS}" treeVersion="3_29" nodes="5743"/></Tree>
          <Skills activeSkillSet="2"><SkillSet id="1" title="Campaign Gems {ACTS}"><Skill label="Totems"><Gem nameSpec="Holy Flame Totem"/></Skill></SkillSet>
            <SkillSet id="2" title="Mapping Gems {MAPS}"><Skill label="Ballista"><Gem nameSpec="Explosive Arrow"/></Skill></SkillSet></Skills>
          <Items activeItemSet="7"><Item id="1">Rarity: RARE&#10;Dawn Bind&#10;Leather Belt&#10;+# to maximum Life</Item>
            <ItemSet id="3" title="Campaign Gear {ACTS}"><Slot name="Belt" itemId="1"/><Slot name="Helmet" itemId="0"/></ItemSet>
            <ItemSet id="7" title="Mapping Gear {MAPS}"><Slot name="Belt" itemId="1"/></ItemSet></Items></PathOfBuilding>'''
        build = parse_build(xml)
        self.assertEqual(build["tree_version"], "3_29")
        self.assertEqual(build["active_tree_spec"], "2")
        self.assertEqual(build["selected_tree_spec"], "2")
        self.assertEqual(build["tree_specs"][0]["nodes"], [14057, 59005])
        self.assertEqual(build["selected_skill_set"], "2")
        self.assertEqual(build["selected_item_set"], "7")
        self.assertEqual(build["items"]["1"], "Rarity: RARE\nDawn Bind\nLeather Belt\n+# to maximum Life")
        self.assertEqual({entry["link_id"] for entry in build["linked_loadouts"]}, {"ACTS", "MAPS"})
        singleton = _linked_loadouts([{"id":"1", "title":"Solo tree"}],
                                     [{"id":"gear", "title":"Gear"}],
                                     [{"id":"skill", "title":"Skills"}])
        self.assertEqual(singleton[0]["item_set_id"], "gear")
        self.assertEqual(singleton[0]["skill_set_id"], "skill")
        exact = _linked_loadouts([{"id":"1", "title":"Campaign"}],
                                 [{"id":"g1", "title":"Campaign"}, {"id":"g2", "title":"Maps"}],
                                 [{"id":"s1", "title":"Campaign"}, {"id":"s2", "title":"Maps"}])
        self.assertEqual((exact[0]["item_set_id"], exact[0]["skill_set_id"]), ("g1", "s1"))

    def test_config_sets_gate_linked_loadouts(self):
        xml = b'''<PathOfBuilding><Tree><Spec title="Acts {alpha}" treeVersion="3_29"/><Spec title="Campaign" treeVersion="3_29"/></Tree>
          <Items><ItemSet id="i1" title="Gear {alpha}"/><ItemSet id="i2" title="Gear {other}"/></Items>
          <Skills><SkillSet id="s1" title="Gems {alpha}"/><SkillSet id="s2" title="Gems {other}"/></Skills>
          <Config><ConfigSet id="c1" title="Config {alpha}"/><ConfigSet id="c2" title="Config {other}"/></Config></PathOfBuilding>'''
        self.assertEqual([(item["tree_spec_id"], item["link_id"]) for item in parse_build(xml)["linked_loadouts"]], [("1", "alpha")])
        exact_versioned = parse_build(b'<PathOfBuilding><Tree><Spec treeVersion="3_29" title="Campaign"/></Tree><Items><ItemSet title="[3.29] Campaign"/></Items><Skills><SkillSet title="[3.29] Campaign"/></Skills><Config><ConfigSet title="[3.29] Campaign"/></Config></PathOfBuilding>')
        self.assertEqual(exact_versioned["tree_specs"][0]["title"], "Campaign")
        self.assertEqual(len(exact_versioned["linked_loadouts"]), 1)
        defaulted = parse_build(b'<PathOfBuilding><Tree><Spec/></Tree></PathOfBuilding>')
        self.assertEqual(defaulted["tree_specs"][0]["title"], "Default")

    def test_import_rejects_node_and_inferred_link_state_overflow(self):
        with self.assertRaisesRegex(ImportError, "node ID outside"):
            parse_build(b'<PathOfBuilding><Tree><Spec nodes="1000000"/></Tree></PathOfBuilding>')
        link_ids = ",".join(f"x{i}" for i in range(129))
        xml = f'<PathOfBuilding><Tree><Spec title="Tree {{{link_ids}}}"/></Tree><Items><ItemSet title="Gear"/></Items><Skills><SkillSet title="Skills"/></Skills></PathOfBuilding>'
        with self.assertRaisesRegex(ImportError, "more than 128 linked loadouts"):
            parse_build(xml)

    def test_passive_tree_graph_has_versioned_nodes_edges_and_nonuniform_orbits(self):
        official = _load_json("3_29")
        graph = prepare_tree_graph("3_29", [14057])
        self.assertEqual(len(official["nodes"]), 3390)
        self.assertEqual(len(graph["nodes"]), 2865)
        self.assertEqual(len(graph["edges"]), 2815)
        self.assertEqual(sum(node["allocated"] for node in graph["nodes"].values()), 1)
        mastery_id, mastery = next((node_id, node) for node_id, node in official["nodes"].items()
                                   if node.get("masteryEffects") and node.get("activeEffectImage"))
        effect_id = mastery["masteryEffects"][0]["effect"]
        rendered = prepare_tree_graph("3_29", [int(mastery_id)], mastery_effects=f"{{{mastery_id},{effect_id}}}")
        self.assertTrue(rendered["nodes"][mastery_id]["selected_mastery_effect"])
        self.assertEqual(rendered["nodes"][mastery_id]["stats"], next(e["stats"] for e in mastery["masteryEffects"] if e["effect"] == effect_id))
        self.assertTrue(any(group["ascendancy_start"] for group in rendered["groups"].values()))
        node_id = next(node_id for node_id, node in official["nodes"].items()
                       if node.get("orbit") == 4 and node.get("orbitIndex") == 1 and node.get("group") is not None)
        tree_node = official["nodes"][node_id]
        group = official["groups"][str(tree_node["group"])]
        radius = official["constants"]["orbitRadii"][4]
        expected_x = group["x"] + math.sin(math.radians(10)) * radius
        expected_y = group["y"] - math.cos(math.radians(10)) * radius
        self.assertAlmostEqual(graph["nodes"][node_id]["x"], expected_x, places=4)
        self.assertAlmostEqual(graph["nodes"][node_id]["y"], expected_y, places=4)
        with self.assertRaisesRegex(ValueError, "not bundled"):
            prepare_tree_graph("3_28", [])

    def test_passive_tree_artwork_is_pinned_per_declared_version_and_urls_are_scoped(self):
        from poe_companion.tree_art import _VERSIONED_ATLAS_SHA256
        self.assertEqual(supported_tree_versions(), ("3_29", "3_30"))
        self.assertEqual(_VERSIONED_ATLAS_SHA256["3_29"]["skills-3.jpg"], "840d0b3a701c3c129e9fc8d82b688df461490624d435902d4f1cf1b28cd531e3")
        self.assertEqual(_VERSIONED_ATLAS_SHA256["3_29"]["skills-disabled-3.jpg"], "28a5200d347eb488ed6a57a8e3b51c61716ec9fbb28bd776030cb3c286ec898a")
        self.assertEqual(_VERSIONED_ATLAS_SHA256["3_30"]["skills-3.jpg"], "314342fec3028206fc41d2a1a98c77fe4999fe942147bba96e76adce6ec72b78")
        self.assertEqual(_VERSIONED_ATLAS_SHA256["3_30"]["skills-disabled-3.jpg"], "398173bf9b877ea134141150cff1c5c7bb3aa6897380d81419954915d6263165")
        old = prepare_tree_graph("3_29", [14057], "Templar", "Hierophant")
        current = prepare_tree_graph("3_30", [], "Witch", "Elementalist")
        self.assertEqual(len(old["nodes"]), 2865)
        self.assertEqual(len(old["edges"]), 2815)
        self.assertEqual(len(current["nodes"]), 2865)
        self.assertEqual(len(current["edges"]), 2815)
        self.assertIn("3_29", old["version"])
        self.assertEqual(old["selected_ascendancy"], "Hierophant")
        self.assertEqual(old["nodes"]["14057"]["allocated"], True)
        self.assertEqual(current["version"], "3_30")
        self.assertIn("normalActive", current["sprites"])
        self.assertNotEqual(old["sprites"]["normalActive"]["filename"], current["sprites"]["normalActive"]["filename"])
        alternate = prepare_tree_graph("3_29", [], "Templar", "", "", 5, 0, 1)
        self.assertEqual(alternate["selected_alternate_ascendancy"], "")
        self.assertNotIn("azmeriBloodline", alternate["sprites"])
        for retired_id in ("9414", "42328", "10622"):
            self.assertNotIn(retired_id, old["nodes"])
        self.assertEqual(alternate["ascendancy_assets"]["Warden"], "Raider")
        live_bloodline = next(node for node in old["nodes"].values()
                              if node["is_bloodline"] and node["ascendancy_start"])
        self.assertTrue(live_bloodline["bloodline_family"] in old["sprites"])
        self.assertEqual(old["nodes"][live_bloodline["id"]]["bloodline_family"], live_bloodline["bloodline_family"])
        for graph in (old, current):
            trialmaster = next(node for node in graph["nodes"].values()
                               if node["ascendancy_name"] == "Trialmaster" and node["ascendancy_start"])
            art = graph["sprites"][trialmaster["bloodline_family"]]
            self.assertIn("AscendancyMiddle", art["coords"])
        self.assertTrue(_trusted_url(current["sprites"]["frame"]["filename"]))
        for url in ("http://web.poecdn.com/image/passive-skill/a.png",
                    "https://web.poecdn.com.evil/image/passive-skill/a.png",
                    "https://evil.example/image/passive-skill/a.png",
                    "https://web.poecdn.com/image/other/a.png",
                    "https://user@web.poecdn.com/image/passive-skill/a.png"):
            self.assertFalse(_trusted_url(url), url)
        redirect = _CDNRedirect()
        request = urllib.request.Request("https://web.poecdn.com/image/passive-skill/a.png")
        safe = redirect.redirect_request(request, None, 302, "Found", {},
                                         "https://web.poecdn.com/image/passive-skill/b.png")
        self.assertEqual(safe.full_url, "https://web.poecdn.com/image/passive-skill/b.png")
        with self.assertRaises(urllib.error.URLError):
            redirect.redirect_request(request, None, 302, "Found", {},
                                      "https://evil.example/image/passive-skill/b.png")
        fallback = _pob_source_url(old, old["sprites"]["line"]["filename"])
        self.assertEqual(fallback, "https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/5606cc64fa9f15694e69001a52b90aa968d01a86/src/TreeData/3_29/line-3.png")
        source_redirect = _PoBSourceRedirect()
        source_request = urllib.request.Request(fallback)
        self.assertEqual(source_redirect.redirect_request(source_request, None, 302, "Found", {}, fallback).full_url, fallback)
        with self.assertRaises(urllib.error.URLError):
            source_redirect.redirect_request(source_request, None, 302, "Found", {}, "https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/main/README.md")
        from io import BytesIO
        from PIL import Image
        png = BytesIO()
        Image.new("RGBA", (12, 9), (20, 30, 40, 255)).save(png, format="PNG")
        self.assertTrue(_valid_image(png.getvalue(), (12, 9)))
        self.assertFalse(_valid_image(png.getvalue(), (13, 9)))
        self.assertFalse(_valid_image(b"not an image", (12, 9)))
        with self.assertRaisesRegex(ValueError, "not bundled"):
            prepare_tree_graph("3_30_ruthless", [])
        imported = parse_build('<PathOfBuilding><Build className="Witch" ascendClassName="Elementalist"/><Tree><Spec title="Current" treeVersion="3_30" nodes="14057"/></Tree></PathOfBuilding>')
        self.assertEqual(imported["tree_specs"][0]["tree_version"], "3_30")

    def test_exact_linked_titles_follow_latest_tree_naming_and_default_sets(self):
        current = parse_build('<PathOfBuilding><Tree><Spec title="Campaign" treeVersion="3_30"/></Tree><Items><ItemSet title="Campaign"/><ItemSet title="Other"/></Items><Skills><SkillSet title="Campaign"/><SkillSet title="Other"/></Skills><Config><ConfigSet title="Campaign"/><ConfigSet title="Other"/></Config></PathOfBuilding>')
        self.assertEqual(current["tree_specs"][0]["tree_display_version"], None)
        self.assertEqual([(link["item_set_id"], link["skill_set_id"]) for link in current["linked_loadouts"]], [("1", "1")])
        falsely_prefixed = parse_build('<PathOfBuilding><Tree><Spec title="Campaign" treeVersion="3_30"/></Tree><Items><ItemSet title="[3.30] Campaign"/><ItemSet title="Other"/></Items><Skills><SkillSet title="[3.30] Campaign"/><SkillSet title="Other"/></Skills><Config><ConfigSet title="[3.30] Campaign"/><ConfigSet title="Other"/></Config></PathOfBuilding>')
        self.assertEqual(falsely_prefixed["linked_loadouts"], [])
        untitled = parse_build('<PathOfBuilding><Tree><Spec treeVersion="3_30"/><Spec title="Other" treeVersion="3_30"/></Tree><Items><ItemSet/><ItemSet title="Other"/></Items><Skills><SkillSet/><SkillSet title="Other"/></Skills></PathOfBuilding>')
        self.assertEqual((untitled["item_sets"][0]["title"], untitled["skill_sets"][0]["title"]), ("Default", "Default"))
        self.assertEqual(untitled["linked_loadouts"][0]["title"], "Default")

    def test_bad_same_dimension_329_atlas_cache_repairs_from_pinned_source_and_reloads_offline(self):
        from hashlib import sha256
        from io import BytesIO
        from PIL import Image
        from poe_companion.tree_art import _VERSIONED_ATLAS_SHA256, _cache_path, load_tree_atlases
        def jpeg(color):
            buffer = BytesIO()
            Image.new("RGB", (999, 1496), color).save(buffer, format="JPEG")
            return buffer.getvalue()
        correct, same_size_wrong = jpeg((180, 30, 20)), jpeg((20, 30, 180))
        graph = {"version": "3_29", "sprites": {"skill": {"filename": "https://web.poecdn.com/image/passive-skill/skills-3.jpg?1540b3b6", "w": 999, "h": 1496}}}
        url = graph["sprites"]["skill"]["filename"]
        key = "skills-3.jpg"
        with mock.patch.dict(_VERSIONED_ATLAS_SHA256["3_29"], {key: sha256(correct).hexdigest()}):
            class Response:
                headers = {}
                def __init__(self, body, final): self.body, self.final = body, final
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def geturl(self): return self.final
                def read(self, _limit): return self.body
            class Opener:
                def __init__(self, source=False): self.source = source
                def open(self, request, **_kwargs):
                    if self.source:
                        return Response(correct, "https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/5606cc64fa9f15694e69001a52b90aa968d01a86/src/TreeData/3_29/skills-3.jpg")
                    return Response(same_size_wrong, url)
            with tempfile.TemporaryDirectory() as directory:
                cache = Path(directory)
                cached = _cache_path(cache, "3_29", url)
                cached.parent.mkdir(parents=True)
                cached.write_bytes(same_size_wrong)
                opener = Opener()
                with mock.patch("poe_companion.tree_art.urllib.request.build_opener", side_effect=lambda handler: Opener(source=handler.__class__.__name__ == "_PoBSourceRedirect")):
                    images, errors = load_tree_atlases(graph, cache)
                self.assertEqual(images[url], correct)
                self.assertEqual(cached.read_bytes(), correct)
                self.assertEqual(errors, [])
                with mock.patch("poe_companion.tree_art.urllib.request.build_opener", side_effect=AssertionError("offline cache should avoid network")):
                    offline, offline_errors = load_tree_atlases(graph, cache)
                self.assertEqual(offline[url], correct)
                self.assertEqual(offline_errors, [])

    def test_narrow_tree_fit_keeps_both_axes_visible(self):
        sx, sy = 26907 + 3600, 21676 + 3600
        scale = fit_scale(sx, sy, 320, 400)
        self.assertLessEqual(sx * scale, 320.01)
        self.assertLessEqual(sy * scale, 400.01)
        self.assertLess(scale, fit_scale(sx, sy, 400, 320))

    def test_tree_artwork_cache_failure_still_returns_fetched_images(self):
        from poe_companion.tree_art import load_tree_atlases
        graph = prepare_tree_graph("3_29", [])
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def geturl(self): return "https://web.poecdn.com/image/passive-skill/test.png"
            def read(self, _limit): return b"validated test image"
        class Opener:
            def open(self, *_args, **_kwargs): return Response()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            cache.write_text("not a directory")
            with mock.patch("poe_companion.tree_art.urllib.request.build_opener", return_value=Opener()), \
                 mock.patch("poe_companion.tree_art._valid_image", return_value=True):
                images, errors = load_tree_atlases(graph, cache)
        self.assertEqual(set(images), {s["filename"] for s in graph["sprites"].values()})
        self.assertTrue(any("cache is unavailable" in error for error in errors))

    def test_selected_spec_class_and_mastery_are_bound_to_node(self):
        build = parse_build('<PathOfBuilding><Build className="Templar" ascendClassName="Hierophant"/><Tree><Spec title="Act 1" treeVersion="3_29" nodes="61525" classId="5" ascendClassId="0"/><Spec title="Acts 2–3" treeVersion="3_29" nodes="63268" classId="5" ascendClassId="2" masteryEffects="{63268,6216}"/></Tree></PathOfBuilding>')
        early, later = build["tree_specs"]
        self.assertEqual((early["class_id"], early["ascend_class_id"]), (5, 0))
        self.assertEqual((later["class_id"], later["ascend_class_id"]), (5, 2))
        early_graph = prepare_tree_graph("3_29", early["nodes"], "Templar", "Hierophant", early["mastery_effects"], 5, 0)
        late_graph = prepare_tree_graph("3_29", later["nodes"], "Templar", "Hierophant", later["mastery_effects"], 5, 2)
        self.assertEqual(early_graph["selected_ascendancy"], "")
        self.assertEqual(late_graph["selected_ascendancy"], "Hierophant")
        self.assertEqual(late_graph["nodes"]["63268"]["stats"], ["Critical Strikes do not inherently Ignite", "100% increased Damage with Hits against Ignited Enemies"])
        self.assertEqual(sum(n["selected_mastery_effect"] for n in late_graph["nodes"].values()), 1)

    def test_legacy_disabled_group_and_import_bounds(self):
        xml = b'<PathOfBuilding><Skills><Skill label="Off" enabled="false"><Gem nameSpec="Hidden"/></Skill><Skill label="On"><Gem nameSpec="Visible"/></Skill></Skills></PathOfBuilding>'
        self.assertEqual(parse_build(xml)["groups"], [{"label": "On", "gems": ["Visible"]}])
        huge_notes = b'<PathOfBuilding><Notes>' + b'x' * (128 * 1024 + 1) + b'</Notes></PathOfBuilding>'
        with self.assertRaisesRegex(ImportError, "notes"):
            parse_build(huge_notes)
        bomb = base64.b64encode(zlib.compress(b'<PathOfBuilding><Notes>' + b'x' * (8 * 1024 * 1024 + 10) + b'</Notes></PathOfBuilding>')).decode()
        with self.assertRaisesRegex(ImportError, "8 MiB"):
            parse_build(bomb)

    def test_rejects_non_pob(self):
        with self.assertRaises(ImportError):
            parse_build("<not-pob />")

    def test_notes_color_spans_reset_palette_and_literal_malformed_text(self):
        example = "^xB97123\n^xFFFF77Hello,^7 ^x70FF70welcome^7.\n\nhttps://example.com/guide"
        imported = parse_build(f"<PathOfBuilding><Notes>{example}</Notes></PathOfBuilding>")
        self.assertEqual(imported["notes"], example)
        spans = format_note_spans(example)
        self.assertEqual("".join(text for text, _color in spans), "Hello, welcome.\n\nhttps://example.com/guide")
        self.assertIn(("Hello,", "#ffff77"), spans)
        self.assertIn(("welcome", "#70ff70"), spans)
        self.assertTrue(any(text.startswith(".") and color == "default" for text, color in spans))
        self.assertEqual(plain_note_summary(example), "Hello, welcome. https://example.com/guide")
        malformed = "Price ^ and ^xGGGGGG and ^x12345; Unicode: 你好 👋 ^9dim"
        self.assertEqual(strip_color_escapes(malformed), "Price ^ and ^xGGGGGG and ^x12345; Unicode: 你好 👋 dim")
        malformed_spans = format_note_spans(malformed)
        self.assertEqual("".join(text for text, _color in malformed_spans), strip_color_escapes(malformed))
        self.assertEqual(format_note_spans("^1red^7white"), [("red", "#ff0000"), ("white", "default")])

    def test_pobb_in_link_forms_and_validation(self):
        self.assertEqual(pobbin_raw_url(" https://pobb.in/kQGOsJn7IbQX?utm_source=share#build "),
                         "https://pobb.in/kQGOsJn7IbQX/raw")
        self.assertEqual(pobbin_raw_url("www.pobb.in/u/BuildMaker_1/abc-123/?tab=skills"),
                         "https://pobb.in/u/BuildMaker_1/abc-123/raw")
        self.assertEqual(pobbin_raw_url("https://pobb.in/abc-123/raw"),
                         "https://pobb.in/abc-123/raw")
        for invalid in ("http://pobb.in/id", "https://evil.example/id", "https://pobb.in.evil/id",
                        "https://user@pobb.in/id", "https://pobb.in:443/id", "https://pobb.in/u/name",
                        "https://pobb.in/%2e%2e/raw"):
            with self.subTest(invalid=invalid), self.assertRaises(ImportError):
                pobbin_raw_url(invalid)

    def test_pobb_in_download_is_bounded_scoped_and_parses_raw_code(self):
        encoded = base64.b64encode(zlib.compress(self.xml)).decode().rstrip("=").encode()

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def geturl(self): return "https://pobb.in/kQGOsJn7IbQX/raw"
            def read(self, limit):
                self.read_limit = limit
                return encoded

        class Opener:
            def __init__(self): self.response = Response()
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return self.response

        opener = Opener()
        with mock.patch("poe_companion.pob.urllib.request.build_opener", return_value=opener):
            build = download_pobbin_build("https://pobb.in/kQGOsJn7IbQX?campaign=1")
        self.assertEqual(build["class"], "Witch")
        self.assertEqual(opener.request.full_url, "https://pobb.in/kQGOsJn7IbQX/raw")
        self.assertIn("WraeclastCompanion/", opener.request.get_header("User-agent"))
        self.assertEqual(opener.timeout, 12)
        self.assertEqual(opener.response.read_limit, 2 * 1024 * 1024 + 1)

    def test_pobb_in_download_reports_not_found_and_rejects_redirect_escape(self):
        class Missing:
            def open(self, *_args, **_kwargs):
                raise urllib.error.HTTPError("https://pobb.in/missing/raw", 404, "missing", {}, None)

        with mock.patch("poe_companion.pob.urllib.request.build_opener", return_value=Missing()):
            with self.assertRaisesRegex(ImportError, "not found"):
                download_pobbin_build("https://pobb.in/missing")
        handler = _PobbinRedirectHandler()
        request = urllib.request.Request("https://pobb.in/id/raw")
        with self.assertRaisesRegex(ImportError, "redirected outside"):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/id/raw")
        with self.assertRaisesRegex(ImportError, "redirected outside"):
            handler.redirect_request(request, None, 302, "Found", {}, "http://pobb.in/id/raw")


class CampaignDataTests(unittest.TestCase):
    def test_all_acts_have_usable_manual_steps(self):
        acts = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        self.assertEqual([a["act"] for a in acts], list(range(1, 11)))
        for act in acts:
            self.assertTrue(act["steps"])
            for step in act["steps"]:
                self.assertTrue(step["title"] and step["detail"] and step["tip"])

    def test_campaign_required_gates_and_penalties_are_explicit(self):
        acts = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        details = " ".join(step["detail"] + " " + step["tip"] for act in acts for step in act["steps"]).lower()
        for phrase in ("baleful gem", "weaver's spike", "tolman's bracelet", "ribbon spool", "thaumetic sulphite",
                       "tower key", "eye of fury", "eye of desire", "resonator", "dishonoured queen",
                       "seven fireflies", "sun orb", "moon orb", "basilisk", "trarthan powder", "garukhan"):
            self.assertIn(phrase, details)
        self.assertIn("entering act 10 still leaves the resistance penalty at 30", details)
        self.assertIn("after this fight the total resistance penalty is 60", details)

    def test_remaining_act_transitions_are_followable_in_order(self):
        acts = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        text = lambda act, i: acts[act-1]["steps"][i]["detail"].lower()
        self.assertIn("kraityn's camp is on broken bridge", text(2, 0))
        self.assertIn("oak is in the wetlands", text(2, 0))
        self.assertIn("alira is in the western forest", text(2, 0))
        self.assertIn("reach those areas from the riverways", text(2, 0))
        self.assertIn("captain arteri", text(2, 0))
        self.assertIn("thaumetic seal", text(2, 0))
        self.assertIn("return to lioneye's watch and speak with bestel", text(2, 0))
        seal = text(2, 0).index("thaumetic seal")
        bestel = text(2, 0).index("speak with bestel")
        self.assertLess(seal, bestel)
        self.assertIn("the apex", text(2, 2))
        self.assertIn("after completing the quest", text(2, 2))
        self.assertIn("dark altar", text(2, 3))
        self.assertIn("summon the vaal oversoul", text(2, 3))
        self.assertIn("use the apex on the dark altar", text(2, 3))
        self.assertIn("dialla at solaris temple level 2", text(3, 2))
        self.assertIn("infernal talc", text(3, 2))
        self.assertIn("undying blockage", text(3, 2))
        self.assertIn("lunaris temple to level 2", text(3, 3))
        self.assertIn("tower key", text(3, 3))
        self.assertIn("black venom", text(7, 1))
        self.assertIn("obsidian key", text(7, 1))
        self.assertIn("chamber of sins level 2", text(7, 1))
        self.assertIn("portal to silk in the chamber of sins", text(7, 1))
        self.assertIn("ashen fields", text(7, 1))
        self.assertIn("greust, lord of the forest (ralakesh)", text(7, 1))
        self.assertIn("northern forest", text(7, 1))
        turn_in = text(7, 1).index("portal to silk")
        key = text(7, 1).index("obsidian key")
        den = text(7, 1).index("to the den")
        greust = text(7, 1).index("defeat greust")
        northern = text(7, 1).index("northern forest")
        self.assertLess(turn_in, key)
        self.assertLess(key, den)
        self.assertLess(den, greust)
        self.assertLess(greust, northern)
        self.assertIn("all seven fireflies", text(7, 2))
        self.assertIn("causeway to vaal city", text(7, 2))
        self.assertIn("give her the fireflies", text(7, 2))
        self.assertNotIn("black venom", text(7, 2))
        self.assertIn("sin in the quarry", text(9, 0))
        sin_chat = text(9, 1).index("speak with sin")
        manifest = text(9, 1).index("manifest the depraved trinity")
        guardians = text(9, 1).index("three guardians")
        self.assertLess(sin_chat, manifest)
        self.assertLess(manifest, guardians)
        self.assertIn("after defeating the depraved trinity", text(9, 2))
        self.assertIn("speak with lilly roth", text(9, 2))
        self.assertEqual(sum(s["detail"].lower().count("depraved trinity") for s in acts[8]["steps"]), 2)
        self.assertIn("rescue bannon", text(10, 0))
        self.assertIn("staff of purity", text(10, 0))
        self.assertIn("ravaged square", text(10, 0))
        self.assertIn("torched courts", text(10, 0))
        self.assertIn("desecrated chambers", text(10, 0))
        self.assertIn("meet innocence by kitava's horns", text(10, 2))
        self.assertIn("open the canals", text(10, 2))

    def test_campaign_navigation_edges(self):
        acts = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        self.assertEqual(advance(acts, 0, 0, -1), (0, 0))
        self.assertEqual(advance(acts, 0, len(acts[0]["steps"])-1, 1), (1, 0))
        self.assertEqual(advance(acts, 9, len(acts[9]["steps"])-1, 1), (9, len(acts[9]["steps"])-1))


class FocusPolicyTests(unittest.TestCase):
    def test_focus_policy_filters_steam_browser_and_poe2(self):
        self.assertTrue(is_game_focus({"app_id":"PathOfExile_x64.exe", "title":"", "focused":True}))
        self.assertTrue(is_game_focus({"app_id":"PathOfExileSteam.exe", "title":"Path of Exile", "focused":True}))
        x64_steam = {"app_id":"PathOfExile_x64Steam.exe", "title":"Path of Exile", "focused":True}
        self.assertTrue(is_game_focus(x64_steam))
        self.assertTrue(is_game_focus(x64_steam, {"game_window_regex":r"(?i)^PathOfExile_x64Steam\.exe Path of Exile$"}))
        self.assertTrue(is_game_focus({"app_id":"steam_app_238960", "title":"", "focused":True}))
        self.assertFalse(is_game_focus({"app_id":"steam", "title":"Path of Exile", "focused":True}))
        self.assertFalse(is_game_focus({"app_id":"steam", "title":"Steam", "focused":True}))
        self.assertFalse(is_game_focus({"app_id":"steamwebhelper", "title":"Path of Exile", "focused":True}))
        self.assertFalse(is_game_focus({"app_id":"steam_app_1234", "title":"Path of Exile", "focused":True}))
        self.assertFalse(is_game_focus({"app_id":"steam_app_1234", "title":"Path of Exile", "focused":True}, {"game_window_regex":".*"}))
        self.assertFalse(is_game_focus({"app_id":"firefox", "title":"Path of Exile Wiki - Firefox", "focused":True}))
        self.assertFalse(is_game_focus({"app_id":"pathofexile2", "title":"Path of Exile 2", "focused":True}))
        self.assertFalse(is_game_focus({"app_id":"", "title":"Path of Exile 2", "focused":True}))
        self.assertTrue(is_game_focus({"app_id":"", "title":"Path of Exile", "focused":True}))
        self.assertTrue(is_game_focus({"app_id":"steam", "title":"Path of Exile", "focused":True}, {"allow_steam_window_identity":True}))
        self.assertFalse(is_game_focus({"app_id":"PathOfExile_x64.exe", "title":"", "focused":False}))
        self.assertFalse(is_game_focus({"app_id":"wrong", "title":"", "focused":True}, {"game_window_regex":"["}))
        self.assertTrue(is_game_focus({"app_id":"custom-window", "title":"Client", "focused":True}, {"game_window_regex":"custom-window"}))
        self.assertFalse(is_game_focus({"app_id":"firefox", "title":"Path of Exile Wiki", "focused":True}, {"game_app_id_regex":42}))
        self.assertTrue(is_game_focus({"app_id":"wine", "title":"Path of Exile", "focused":True}, {"game_title_regex":42}))

    def test_hotkey_toggles_only_while_game_is_focused(self):
        game = {"app_id":"PathOfExile_x64.exe", "title":"", "focused":True}
        elsewhere = {"app_id":"steam", "title":"Steam", "focused":True}
        self.assertIsNone(toggle_clickable(False, elsewhere))
        self.assertTrue(toggle_clickable(False, game))
        self.assertFalse(toggle_clickable(True, game))

    def test_kwin_uses_plasma_qtimer_interval_property(self):
        source = (ROOT / "kwin/poe-companion/contents/code/main.js").read_text()
        self.assertRegex(source, re.compile(r"focusHeartbeat\.interval\s*=\s*2000"))
        self.assertNotIn("focusHeartbeat.setInterval", source)


class LocalStateTests(unittest.TestCase):
    def test_new_tree_and_loadout_fields_round_trip(self):
        campaign = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        build = {"name":"Tree build", "class":"Templar", "ascendancy":"Hierophant", "level":"78", "notes":"",
                 "groups":[], "skill_sets":[{"id":"s1", "title":"Acts", "groups":[]}],
                 "active_skill_set":"s1", "selected_skill_set":"s1", "tree_version":"3_29",
                 "tree_specs":[{"id":"1", "title":"Campaign", "tree_version":"3_29", "nodes":[14057], "mastery_effects":""}],
                 "active_tree_spec":"1", "selected_tree_spec":"1", "variants":["Campaign"],
                 "item_sets":[{"id":"i1", "title":"League Start", "slots":[{"name":"Belt", "item_id":"4", "title":"Dawn Bind"}]}],
                 "items":{"4":"Rarity: RARE\nDawn Bind\nLeather Belt"}, "active_item_set":"i1", "selected_item_set":"i1",
                 "linked_loadouts":[]}
        with tempfile.TemporaryDirectory() as directory:
            original = app_module.DATA
            try:
                app_module.DATA = Path(directory)
                store = app_module.Store(campaign)
                state = {**store.data, "builds":[build], "selected_build":0}
                store.save(state)
                reopened = app_module.Store(campaign)
                self.assertIsNone(reopened.read_only_reason)
                self.assertEqual(reopened.data["builds"][0], build)
            finally:
                app_module.DATA = original

    def test_legacy_clickable_state_migrates_builds_and_progress_but_starts_click_through(self):
        campaign = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        build = {"name":"Legacy starter", "class":"Ranger", "ascendancy":"Deadeye", "level":"32",
                 "notes":"Keep resists capped.", "groups":[{"label":"Main", "gems":["Toxic Rain"]}],
                 "skill_sets":[], "active_skill_set":"", "variants":[]}
        with tempfile.TemporaryDirectory() as directory:
            original = app_module.DATA
            try:
                app_module.DATA = Path(directory)
                app_module.DATA.mkdir(parents=True, exist_ok=True)
                source = {"builds":[build], "selected_build":0, "act":5, "step":1, "clickable":True}
                path = app_module.DATA / "state.json"
                path.write_text(json.dumps(source))

                migrated = app_module.Store(campaign)
                self.assertIsNone(migrated.read_only_reason)
                self.assertEqual(migrated.data["builds"], [build])
                self.assertEqual((migrated.data["act"], migrated.data["step"]), (5, 1))
                self.assertEqual(migrated.data["selected_build"], 0)
                self.assertNotIn("clickable", migrated.data)
                self.assertFalse(app_module.Companion().clickable)

                migrated.save()
                persisted = json.loads(path.read_text())
                self.assertNotIn("clickable", persisted)
                self.assertEqual(persisted["builds"], [build])
                self.assertEqual((persisted["act"], persisted["step"], persisted["selected_build"]), (5, 1, 0))
                reopened = app_module.Store(campaign)
                self.assertIsNone(reopened.read_only_reason)
                self.assertEqual(reopened.data, migrated.data)
            finally:
                app_module.DATA = original

    def test_saved_planner_models_restore_and_malformed_shapes_fail_closed(self):
        campaign = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        build = {"name":"Starter", "class":"Ranger", "ascendancy":"Deadeye", "level":"32", "notes":"Keep resists capped.",
                 "groups":[{"label":"Main", "gems":["Toxic Rain"]}], "skill_sets":[], "active_skill_set":"", "variants":[]}
        with tempfile.TemporaryDirectory() as directory:
            original = app_module.DATA
            try:
                app_module.DATA = Path(directory)
                app_module.DATA.mkdir(parents=True, exist_ok=True)
                (app_module.DATA / "state.json").write_text(json.dumps({"builds":[build], "selected_build":0, "act":5, "step":1}))
                restored_store = app_module.Store(campaign)
                restored = restored_store.data
                self.assertEqual(restored["selected_build"], 0)
                self.assertEqual(restored["builds"][0]["name"], "Starter")
                self.assertEqual((restored["act"], restored["step"]), (5, 1))
                restored_store.save()
                self.assertEqual(app_module.Store(campaign).data, restored)
                (app_module.DATA / "state.json").write_text(json.dumps({"builds":[{"name": [], "groups": "bad"}], "act": {}, "step": []}))
                safe = app_module.Store(campaign).data
                self.assertEqual(safe["builds"], [])
                self.assertEqual((safe["act"], safe["step"], safe["selected_build"]), (0, 0, -1))
                self.assertTrue(app_module.Store(campaign).read_only_reason)
                self.assertIn('"name": []', (app_module.DATA / "state.json").read_text())
            finally:
                app_module.DATA = original

    def test_aggregate_state_limit_is_transactional_and_oversize_is_preserved(self):
        campaign = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        build = {"name":"Large notes", "class":"Witch", "ascendancy":"", "level":"1", "notes":"n" * 131000,
                 "groups":[], "skill_sets":[], "active_skill_set":"", "variants":[]}
        with tempfile.TemporaryDirectory() as directory:
            original = app_module.DATA
            try:
                app_module.DATA = Path(directory)
                store = app_module.Store(campaign)
                candidate = {**store.data, "builds":[build.copy() for _ in range(33)], "selected_build":32}
                with self.assertRaisesRegex(app_module.StateError, "exceed 4 MiB"):
                    store.save(candidate)
                self.assertEqual(store.data["builds"], [])
                self.assertFalse(store.path.exists())

                invalid_node = {"name":"Bad import", "tree_specs":[{"id":"1", "title":"Tree", "tree_version":"3_29", "nodes":[1000000], "mastery_effects":""}]}
                with self.assertRaisesRegex(app_module.StateError, "cannot be reopened safely"):
                    store.save({**store.data, "builds":[invalid_node], "selected_build":0})
                self.assertFalse(store.path.exists())

                old_bytes = b"{" + b" " * app_module.MAX_STATE_BYTES
                store.path.write_bytes(old_bytes)
                recovered = app_module.Store(campaign)
                self.assertIn("original file was kept unchanged", recovered.read_only_reason)
                with self.assertRaisesRegex(app_module.StateError, "kept unchanged"):
                    recovered.save()
                self.assertEqual(store.path.read_bytes(), old_bytes)

                incompatible = b'{"builds": [], "schema_v2": true}'
                store.path.write_bytes(incompatible)
                protected = app_module.Store(campaign)
                self.assertIn("cannot read", protected.read_only_reason)
                with self.assertRaises(app_module.StateError):
                    protected.save()
                self.assertEqual(store.path.read_bytes(), incompatible)
            finally:
                app_module.DATA = original


class TreeCanvasTests(unittest.TestCase):
    def test_zoom_pan_offset_keeps_cursor_world_point_fixed(self):
        width, height = 1000.0, 700.0
        base_scale = 0.05
        wx, wy = 100.0, -50.0
        pan_x, pan_y = 120.0, -80.0
        old_zoom, new_zoom = 1.5, 2.7
        anchor_x, anchor_y = 760.0, 210.0

        # World point under cursor before zoom:
        s1 = base_scale * old_zoom
        cx1, cy1 = width / 2.0 + pan_x, height / 2.0 + pan_y
        world_x = wx + (anchor_x - cx1) / s1
        world_y = wy + (anchor_y - cy1) / s1

        new_pan_x, new_pan_y = zoom_pan_offset(pan_x, pan_y, width, height, anchor_x, anchor_y, old_zoom, new_zoom)
        s2 = base_scale * new_zoom
        cx2, cy2 = width / 2.0 + new_pan_x, height / 2.0 + new_pan_y
        screen_x = cx2 + (world_x - wx) * s2
        screen_y = cy2 + (world_y - wy) * s2
        self.assertAlmostEqual(screen_x, anchor_x, places=6)
        self.assertAlmostEqual(screen_y, anchor_y, places=6)

    def test_connector_stroke_width_stays_proportional_and_bounded(self):
        world_mul = 1.0 / 0.3835
        active_w = connector_stroke_width(13.0, world_mul, 0.08, active=True)
        normal_w = connector_stroke_width(13.0, world_mul, 0.08, active=False)
        self.assertGreater(active_w, normal_w)
        self.assertLessEqual(active_w, 7.5)
        self.assertGreaterEqual(normal_w, 0.75)

    def test_search_matching_nodes_covers_names_stats_and_unallocated_masteries(self):
        graph = prepare_tree_graph("3_30", [14057], "Templar", "Hierophant")
        unallocated_mastery = next(n for n in graph["nodes"].values()
                                   if n["mastery"] and not n["allocated"] and n.get("mastery_effects"))
        sample_stat = unallocated_mastery["mastery_effects"][0]["stats"][0]
        matches = search_matching_nodes(graph, sample_stat[:18])
        self.assertIn(unallocated_mastery["id"], matches)
        self.assertEqual(search_matching_nodes(graph, "   "), [])

    def test_offscreen_tooltip_render_handles_normal_and_unallocated_mastery(self):
        import cairo
        graph = prepare_tree_graph("3_30", [14057], "Templar", "Hierophant")
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 640, 480)
        cr = cairo.Context(surface)
        canvas = PassiveTreeCanvas.__new__(PassiveTreeCanvas)
        canvas.graph = graph
        allocated_node = graph["nodes"]["14057"]
        unallocated_mastery = next(n for n in graph["nodes"].values()
                                   if n["mastery"] and not n["allocated"] and n.get("mastery_effects"))
        canvas._draw_tooltip(cr, allocated_node, 620.0, 460.0, 640.0, 480.0)
        canvas._draw_tooltip(cr, unallocated_mastery, 12.0, 12.0, 640.0, 480.0)
        surface.flush()

    def test_unselected_sibling_ascendancies_are_hidden_by_default(self):
        graph = prepare_tree_graph("3_30", [14057], "Templar", "Hierophant")
        self.assertTrue(is_ascendancy_visible(graph, "Hierophant", False, show_all=False))
        self.assertFalse(is_ascendancy_visible(graph, "Inquisitor", False, show_all=False))
        self.assertFalse(is_ascendancy_visible(graph, "Guardian", False, show_all=False))
        self.assertTrue(is_ascendancy_visible(graph, "Inquisitor", False, show_all=True))
        self.assertTrue(is_ascendancy_visible(graph, "Inquisitor", False, show_all=False, allocated_ascendancies={"Inquisitor"}))
        self.assertTrue(is_ascendancy_visible(graph, None, False, show_all=False))


class ClientLogAndHudTests(unittest.TestCase):
    def test_parse_entered_zone_and_campaign_step_matching(self):
        acts = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        self.assertEqual(
            parse_entered_zone("2026/03/15 14:22:01 1234 cff9 [INFO Client 1] : You have entered The Mud Flats.\n"),
            "The Mud Flats",
        )
        self.assertIsNone(parse_entered_zone("2026/03/15 14:22:01 [INFO Client 1] : Something else\n"))
        self.assertEqual(match_campaign_step(acts, 0, 0, "The Mud Flats"), (0, 2))
        self.assertEqual(match_campaign_step(acts, 0, 4, "The Forest Encampment"), (1, 0))
        self.assertEqual(match_campaign_step(acts, 1, 0, "The Chamber of Sins Level 1"), (1, 1))
        self.assertEqual(match_campaign_step(acts, 2, 2, "The Lunaris Temple Level 2"), (2, 3))
        self.assertIsNone(match_campaign_step(acts, 1, 2, "The Mud Flats"))

    def test_discover_client_log_reads_steam_libraryfolders_vdf(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            vdf = home / ".local/share/Steam/steamapps/libraryfolders.vdf"
            vdf.parent.mkdir(parents=True)
            secondary = Path(directory) / "games_ssd/SteamLibrary"
            log_path = secondary / "steamapps/common/Path of Exile/logs/Client.txt"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("", encoding="utf-8")
            vdf.write_text(f'"libraryfolders"\n{{\n\t"1"\n\t{{\n\t\t"path"\t\t"{secondary}"\n\t}}\n}}\n', encoding="utf-8")
            self.assertEqual(discover_client_log(home=home), log_path)

    def test_client_log_tailer_reads_only_appended_zones(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "Client.txt"
            log_path.write_text("2026/03/15 14:00:00 [INFO Client 1] : You have entered The Coast.\n")
            tailer = ClientLogTailer(log_path)
            self.assertEqual(tailer.poll_zones(), [])
            with log_path.open("a") as handle:
                handle.write("2026/03/15 14:01:00 [INFO Client 1] : You have entered The Mud Flats.\n")
            self.assertEqual(tailer.poll_zones(), ["The Mud Flats"])
            self.assertEqual(tailer.poll_zones(), [])

    def test_hud_prefs_round_trip_and_compact_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hud.json"
            prefs = {**app_module.DEFAULT_HUD_PREFS, "position": "top-right", "opacity": 0.75, "compact": False,
                     "interactive_controls": False, "show_timer": True}
            app_module.save_hud_prefs(prefs, path)
            loaded = app_module.load_hud_prefs(path)
            self.assertEqual(loaded["position"], "top-right")
            self.assertAlmostEqual(loaded["opacity"], 0.75)
            self.assertFalse(loaded["compact"])
            self.assertFalse(loaded["interactive_controls"])
            self.assertTrue(loaded["show_timer"])


class ToolsAndQoLTests(unittest.TestCase):
    def test_parse_item_card_and_gem_colors(self):
        from poe_companion.tools import gem_socket_color, is_support_gem, parse_item_card
        card = parse_item_card(
            "Rarity: UNIQUE\nGoldrim\nLeather Cap\n--------\nQuality: +20%\nEvasion: 50\n--------\n+35% to all Elemental Resistances\n"
        )
        self.assertEqual(card["rarity"], "unique")
        self.assertEqual(card["name"], "Goldrim")
        self.assertEqual(card["base"], "Leather Cap")
        self.assertIn("Quality: +20%", card["meta"])
        self.assertEqual(card["sections"], [["+35% to all Elemental Resistances"]])
        self.assertEqual(gem_socket_color("Determination (Lv 20)"), "red")
        self.assertEqual(gem_socket_color("Grace (Lv 20)"), "green")
        self.assertEqual(gem_socket_color("Frostblink (Lv 20)"), "blue")
        self.assertEqual(gem_socket_color("Portal (Lv 1)"), "white")
        self.assertTrue(is_support_gem("Lifetap Support (Lv 20)"))
        self.assertFalse(is_support_gem("Righteous Fire (Lv 20)"))

    def test_tree_diff_run_timer_and_regex_persistence(self):
        from poe_companion.tools import RunTimer, compute_tree_diff, load_regex_presets, save_custom_regexes
        added, removed = compute_tree_diff([1, 2, 3], [2, 3, 4, 5])
        self.assertEqual(added, {"1"})
        self.assertEqual(removed, {"4", "5"})

        now = [100.0]
        timer = RunTimer(clock=lambda: now[0])
        timer.start()
        now[0] = 165.0
        timer.record_act_transition(0, 1)
        self.assertEqual(timer.formatted(), "01:05")
        self.assertAlmostEqual(timer.splits[1], 65.0)
        timer.pause()
        now[0] = 200.0
        self.assertAlmostEqual(timer.elapsed(), 65.0)
        timer.reset()
        self.assertEqual(timer.splits, {})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "regexes.json"
            presets = load_regex_presets(path)
            builtin_count = len(presets)
            presets.append({"id": "c1", "category": "Custom", "title": "My Boots", "regex": "nne|rint", "note": "fast", "builtin": False})
            save_custom_regexes(presets, path)
            reloaded = load_regex_presets(path)
            self.assertEqual(len(reloaded), builtin_count + 1)
            self.assertEqual(reloaded[-1]["title"], "My Boots")

    def test_level_up_parsing_milestones_and_auto_build_regex(self):
        from poe_companion.client_log import parse_level_up
        from poe_companion.tools import generate_build_vendor_regex, level_milestone_cue, next_level_milestone

        self.assertEqual(
            parse_level_up("2026/03/15 14:25:01 1234 cff9 [INFO Client 1] : ExileOne (Witch) is now level 12\n"),
            ("ExileOne", 12),
        )
        self.assertIsNone(parse_level_up("2026/03/15 14:25:01 [INFO Client 1] : Random chat line\n"))

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "Client.txt"
            log_path.write_text("", encoding="utf-8")
            tailer = ClientLogTailer(log_path)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("2026/03/15 14:26:00 [INFO Client 1] : You have entered The Mud Flats.\n")
                handle.write("2026/03/15 14:26:10 [INFO Client 1] : ExileOne (Witch) is now level 12\n")
            zones, levels = tailer.poll_events()
            self.assertEqual(zones, ["The Mud Flats"])
            self.assertEqual(levels, [("ExileOne", 12)])

        build = {
            "name": "RF Chieftain",
            "groups": [
                {"label": "Main", "gems": ["Righteous Fire (Lv 20)", "Elemental Focus Support (Lv 20)", "Lifetap Support (Lv 20)"]}
            ],
        }
        cue = level_milestone_cue(12, build)
        self.assertIsNotNone(cue)
        self.assertIn("Nessa", cue)
        self.assertIn("Righteous Fire", cue)
        self.assertIsNone(level_milestone_cue(13, build))
        self.assertEqual(next_level_milestone(12)[0], 16)

        auto_rx = generate_build_vendor_regex(build)
        self.assertIsNotNone(auto_rx)
        self.assertLessEqual(len(auto_rx["regex"]), 250)
        self.assertIn("nne|rint", auto_rx["regex"])
        self.assertIn("b-b-r", auto_rx["regex"])

    def test_campaign_hand_hold_metadata_complete(self):
        acts = json.loads((ROOT / "poe_companion/campaign.json").read_text())
        for act in acts:
            target = act.get("target")
            self.assertIsInstance(target, dict)
            self.assertTrue(target.get("life"))
            self.assertTrue(target.get("resists"))
            self.assertTrue(target.get("note"))
            for step in act["steps"]:
                self.assertTrue((step.get("layout") or "").strip(), f"Missing layout tell for {step['title']}")
                self.assertIsInstance(step.get("tags"), list)
        self.assertEqual(app_module.compact_step_summary("First sentence. Second sentence."), "First sentence.")


class _FakeInvocation:
    def __init__(self):
        self.value = None
        self.error = None

    def return_value(self, value):
        self.value = value

    def return_dbus_error(self, name, message):
        self.error = (name, message)


class TrayTests(unittest.TestCase):
    def test_toggle_label_and_menu_layout(self):
        self.assertEqual(tray.toggle_label(True), "Hide planner")
        self.assertEqual(tray.toggle_label(False), "Show planner")
        self.assertEqual(tray.menu_layout(False), (0, {"children-display": "submenu"}, [
            (1, {"label": "Show planner"}, []),
            (2, {"type": "separator"}, []),
            (3, {"label": "Quit"}, []),
        ]))
        self.assertEqual(tray.menu_layout(True, 1), (1, {"label": "Hide planner"}, []))
        self.assertIsNone(tray.menu_layout(True, 99))

    def test_menu_item_properties_filters_and_bounds(self):
        self.assertEqual([i for i, _ in tray.menu_item_properties(True, [])], [1, 2, 3])
        self.assertEqual([i for i, _ in tray.menu_item_properties(True, None)], [1, 2, 3])
        self.assertEqual(tray.menu_item_properties(True, [3, 42]), [(3, {"label": "Quit"})])
        self.assertEqual(tray.menu_item_properties(True, [0] * tray.MAX_MENU_IDS + [3]), [])

    def test_menu_action_mapping(self):
        self.assertEqual(tray.menu_action(1, "clicked"), "toggle-planner")
        self.assertEqual(tray.menu_action(3, "clicked"), "quit")
        self.assertIsNone(tray.menu_action(2, "clicked"))
        self.assertIsNone(tray.menu_action(0, "clicked"))
        self.assertIsNone(tray.menu_action(42, "clicked"))
        self.assertIsNone(tray.menu_action(3, "hovered"))

    def test_item_identity_and_bus_name(self):
        self.assertEqual(tray.item_bus_name(1234), "org.kde.StatusNotifierItem-1234-1")
        self.assertEqual(tray.ITEM_PROPERTIES["IconName"], ("s", "applications-games"))
        self.assertEqual(tray.ITEM_PROPERTIES["Title"][1], "Wraeclast Companion")
        self.assertEqual(tray.ITEM_PROPERTIES["ToolTip"][1][2], "Wraeclast Companion")
        self.assertEqual(tray.ITEM_PROPERTIES["Menu"], ("o", "/MenuBar"))
        self.assertEqual(tray.ITEM_PROPERTIES["ItemIsMenu"], ("b", False))

    def test_interface_xml_matches_property_tables(self):
        cases = [
            (tray.ITEM_INTERFACE_XML, "org.kde.StatusNotifierItem", tray.ITEM_PROPERTIES),
            (tray.MENU_INTERFACE_XML, "com.canonical.dbusmenu", tray.MENU_PROPERTIES),
        ]
        for xml_text, iface_name, table in cases:
            root = ET.fromstring(xml_text)
            iface = next(i for i in root.findall("interface") if i.get("name") == iface_name)
            props = iface.findall("property")
            self.assertEqual({p.get("name"): p.get("type") for p in props},
                             {k: v[0] for k, v in table.items()})
            for p in props:
                self.assertEqual(p.get("access"), "read")
            if iface_name == "org.kde.StatusNotifierItem":
                methods = {m.get("name") for m in iface.findall("method")}
                self.assertEqual(methods, {"Activate", "SecondaryActivate", "ContextMenu", "Scroll"})
            else:
                methods = {m.get("name") for m in iface.findall("method")}
                self.assertEqual(methods, {"GetLayout", "GetGroupProperties", "GetProperty", "Event",
                                           "EventGroup", "AboutToShow", "AboutToShowGroup"})
                signals = {s.get("name") for s in iface.findall("signal")}
                self.assertIn("LayoutUpdated", signals)

    def test_tray_module_is_gtk_free(self):
        source = (ROOT / "poe_companion/tray.py").read_text()
        self.assertNotIn("import gi", source)
        self.assertNotIn("from gi", source)

    def test_menu_layout_variant_round_trips(self):
        v = app_module.GLib.Variant("(u(ia{sv}av))", (5, app_module._menu_node_tuple(tray.menu_layout(True))))
        self.assertEqual(v.get_type_string(), "(u(ia{sv}av))")
        self.assertEqual(v.unpack(), (5, tray.menu_layout(True)))

    def test_interface_xml_parses_with_gio(self):
        item = app_module.Gio.DBusNodeInfo.new_for_xml(tray.ITEM_INTERFACE_XML)
        self.assertEqual(item.interfaces[0].name, "org.kde.StatusNotifierItem")
        menu = app_module.Gio.DBusNodeInfo.new_for_xml(tray.MENU_INTERFACE_XML)
        self.assertEqual(menu.interfaces[0].name, "com.canonical.dbusmenu")

    def test_properties_get_getall_and_set(self):
        GLib = app_module.GLib
        inv = _FakeInvocation()
        app_module._handle_properties_call(tray.ITEM_PROPERTIES, "Get",
                                           GLib.Variant("(ss)", (tray.ITEM_INTERFACE, "IconName")), inv)
        self.assertEqual(inv.value.unpack(), ("applications-games",))

        inv = _FakeInvocation()
        app_module._handle_properties_call(tray.ITEM_PROPERTIES, "GetAll",
                                           GLib.Variant("(s)", (tray.ITEM_INTERFACE,)), inv)
        self.assertEqual(inv.value.unpack()[0]["ToolTip"], ("applications-games", [], "Wraeclast Companion", ""))

        inv = _FakeInvocation()
        app_module._handle_properties_call(tray.ITEM_PROPERTIES, "Get",
                                           GLib.Variant("(ss)", (tray.ITEM_INTERFACE, "Nope")), inv)
        self.assertEqual(inv.error[0], "org.freedesktop.DBus.Error.UnknownProperty")

        inv = _FakeInvocation()
        app_module._handle_properties_call(tray.ITEM_PROPERTIES, "Set",
                                           GLib.Variant("(ssv)", (tray.ITEM_INTERFACE, "IconName",
                                                                  GLib.Variant("s", "x"))), inv)
        self.assertEqual(inv.error[0], "org.freedesktop.DBus.Error.PropertyReadOnly")

        inv = _FakeInvocation()
        app_module._handle_properties_call(tray.MENU_PROPERTIES, "GetAll",
                                           GLib.Variant("(s)", (tray.MENU_INTERFACE,)), inv)
        self.assertEqual(inv.value.unpack()[0], {"Version": 3, "TextDirection": "ltr",
                                                 "Status": "normal", "IconThemePath": []})


if __name__ == "__main__":
    unittest.main()
