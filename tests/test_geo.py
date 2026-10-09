"""geo.py parsing tests. They use saved real API responses (tests/samples), so no network is needed."""
import json
from pathlib import Path

from app import geo

SAMPLES = Path(__file__).parent / "samples"


def load(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def test_parse_rock_chalk():
    unit = geo.parse_rock(load("macrostrat_dover.json"), "medium")
    assert unit["name"] == "White Chalk Subgroup"
    assert unit["lith"] == "chalk"
    assert unit["b_age"] == 100.5 and unit["t_age"] == 66


def test_parse_rock_empty():
    assert geo.parse_rock({"success": {"data": []}}, "large") is None
    assert geo.parse_rock(None, "large") is None


def test_setting_is_flagged_as_inference():
    s = geo.setting({"lith": "chalk"})
    assert s["type"] == "marine"
    assert "probably" in s["confidence"]
    assert geo.setting({"lith": "mystery"})["type"] == "unknown"


def test_time_ruler():
    r = geo.time_ruler({"b_age": 100.5, "t_age": 66})
    assert r["times_older_than_humans"] == 335
    assert geo.time_ruler({"b_age": None}) is None


def test_parse_fossils_counts_and_hides_unclassified():
    data = load("pbdb_dover.json")
    f = geo.parse_fossils(data, 0.5, True)
    assert f["count"] == len(data["records"])
    assert "same age as the rock" in f["search"]
    assert all(name != "NO_CLASS_SPECIFIED" for name, _ in f["classes"])
    assert geo.parse_fossils({"records": []}, 0.5, True) is None


def test_parse_fossils_says_when_age_was_not_matched():
    f = geo.parse_fossils(load("pbdb_dover.json"), 2.0, False)
    assert "ANY age" in f["search"]


def test_parse_rare_strips_authors():
    rare = geo.parse_rare(load("gbif_dover.json"))
    assert rare[0]["species"] == "Rissa tridactyla"
    assert rare[0]["sighting_records"] > 0
    assert geo.parse_rare({}) == []


def test_parse_human_keeps_history_drops_schools():
    w = load("wikipedia_dover.json")
    places = [p["place"] for p in geo.parse_human(w["geosearch"], w["extracts"])]
    assert "Maison Dieu, Dover" in places
    assert not any("Catholic School" in p for p in places)


def test_history_words_need_whole_words():
    assert geo.HISTORY_WORDS.search("a roman villa")
    assert not geo.HISTORY_WORDS.search("roman catholic school")


def test_source_status():
    assert geo.source_status(None, "failed: ReadTimeout").startswith("failed")
    assert geo.source_status([], "ok") == "empty"
    assert geo.source_status([1], "ok") == "ok"
