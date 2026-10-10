"""Fallback path, cache, rate limit and the API contract. No network: the layers and the LLM are faked."""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import geo, main, narrate
from app.cache import RateLimiter, TTLCache, cache_key

FACTS = {
    "rock": {"name": "White Chalk Subgroup", "lith": "chalk", "b_age": 100.5, "t_age": 66, "b_int_name": "Cenomanian",
             "map_scale": "large"},
    "ancient_setting": {"type": "marine", "meaning": "a sea or ocean floor"},
    "time_ruler": {"times_older_than_humans": 335},
    "fossils_found_nearby": "pending",
    "rare_animals_recorded_nearby": [],
    "human_history_nearby": None,  # source down
    "scope": {"rock": {"level": "exact_area", "note": "From a detailed local geological map."}},
}


def test_facts_only_text_is_honest_about_gaps():
    text = narrate.facts_only_text(FACTS)
    assert "White Chalk Subgroup" in text and "probably" in text
    assert "could not be checked" in text      # human history is null
    assert "nothing recorded nearby" in text   # rare animals are empty


def test_narrate_falls_back_when_llm_fails(monkeypatch):
    async def boom(client, prompt):
        raise httpx.ConnectError("llm is down")
    monkeypatch.setattr(narrate, "call_llm", boom)
    text, mode = asyncio.run(narrate.narrate(None, FACTS))
    assert mode == "facts-only" and "White Chalk Subgroup" in text


RARE_FACTS = {**FACTS, "human_history_nearby": [], "rare_animals_note": geo.RARE_NOTE,
              "rare_animals_recorded_nearby": [{"species": "Sphyrna lewini", "records": 48, "datasets": ["iNaturalist research-grade observations"],
                                                "records_checked": 48, "marked_dead": 5, "marked_alive": 0, "specimen_records": 0}]}


def test_fallback_text_says_recorded_never_alive():
    text = narrate.facts_only_text(RARE_FACTS)
    assert "Rare animals recorded nearby" in text and "not necessarily where it lives" in text
    assert not narrate.claims_animals_alive(text)


def test_prompt_forbids_alive_claims_and_gives_dataset_and_dead_counts():
    p = narrate.build_prompt(RARE_FACTS, "main")
    assert "NOT proof that an animal lives or is alive" in p.split("FACTS:")[0]
    assert "alive today" not in p
    facts = p.split("FACTS:")[1]
    assert "iNaturalist" in facts and '"marked_dead":5' in facts


@pytest.mark.parametrize("story", ["Rare animals still wander these hills.", "These sharks are alive today.",
                                   "The hammerhead lives nearby."])
def test_story_claiming_animals_are_alive_is_replaced_by_facts(monkeypatch, story):
    async def bad_llm(client, prompt):
        return story, "online"
    monkeypatch.setattr(narrate, "call_llm", bad_llm)
    text, mode = asyncio.run(narrate.narrate(None, RARE_FACTS))
    assert mode == "facts-only" and text != story


def test_story_about_records_is_kept(monkeypatch):
    good = ("Stand still. Records exist of the scalloped hammerhead nearby; five are marked dead. "
            "A record shows where an animal was observed, not necessarily where it lives.")

    async def ok_llm(client, prompt):
        return good, "online"
    monkeypatch.setattr(narrate, "call_llm", ok_llm)
    assert asyncio.run(narrate.narrate(None, RARE_FACTS)) == (good, "online")


def test_prompt_puts_rules_first_and_hides_pending():
    p = narrate.build_prompt(FACTS, "main")
    assert p.startswith("STRICT RULES")
    assert "pending" not in p.split("FACTS:")[1]


def test_cache_ttl_and_key_rounding():
    assert cache_key(51.1301, 1.3099) == cache_key(51.1304, 1.3096)
    c = TTLCache(ttl_seconds=0.01)
    c.set(cache_key(51.13, 1.31), "x")
    assert c.get(cache_key(51.13, 1.31)) == "x"
    time.sleep(0.03)
    assert c.get(cache_key(51.13, 1.31)) is None


def test_rate_limiter():
    r = RateLimiter(limit=2, window=60)
    assert r.allow("a") and r.allow("a") and not r.allow("a")
    assert r.allow("b")


@pytest.fixture
def api(monkeypatch):
    main.where_cache = TTLCache()
    main.fossil_cache = TTLCache()
    main.limiter = RateLimiter(100, 60)

    async def fake_rock(c, lat, lng):
        return FACTS["rock"]

    async def fake_rare(c, lat, lng, lang=None):
        raise httpx.ReadTimeout("slow")  # one source fails

    async def fake_human(c, lat, lng):
        return [], geo.human_scope("none", geo_note="No named place was found nearby."), "empty", []

    async def fake_llm(c, prompt):
        raise RuntimeError("no llm")

    monkeypatch.setattr(geo, "fetch_rock", fake_rock)
    monkeypatch.setattr(geo, "fetch_rare", fake_rare)
    monkeypatch.setattr(geo, "fetch_human", fake_human)
    monkeypatch.setattr(narrate, "call_llm", fake_llm)
    with TestClient(main.app) as client:
        yield client


def test_where_survives_one_failed_source_and_llm_failure(api):
    r = api.get("/api/where", params={"lat": 51.13, "lng": 1.31})
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"facts", "narration", "mode", "sources", "cached"}
    assert d["mode"] == "facts-only" and d["narration"]
    assert d["facts"]["rare_animals_recorded_nearby"] is None        # failed source -> null
    assert d["facts"]["rock"]["name"] == "White Chalk Subgroup"  # the others are unaffected
    scopes = d["facts"]["scope"]
    assert scopes["rock"]["level"] == "exact_area" and scopes["human"]["level"] == "none"
    assert scopes["rare"] is None                                 # a failed source has no scope
    status = {s["id"]: s["status"] for s in d["sources"]}
    assert status["gbif"].startswith("failed")
    assert status["macrostrat"] == "ok" and status["pbdb"] == "pending"
    assert d["cached"] is False


@pytest.mark.parametrize("lat,lng", [(91, 0), (-91, 0), (0, 181), (0, -181), ("abc", 0)])
def test_where_validates_coordinates(api, lat, lng):
    assert api.get("/api/where", params={"lat": lat, "lng": lng}).status_code == 422


def test_fossils_failure_is_not_an_error_screen(api, monkeypatch):
    async def fail(c, lat, lng, unit):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(geo, "fetch_fossils", fail)
    d = api.get("/api/fossils", params={"lat": 51.13, "lng": 1.31}).json()
    assert d["fossils"] is None and d["source"]["status"].startswith("failed") and d["narration"]


def test_health(api):
    assert api.get("/health").text == "ok"


# ---------------------------------------------------------------- scope in the narration
DHAKA_NOTE = "About Dhaka, the nearest city (about 5.5 km away), not your exact spot. Nothing relevant is recorded within about 10 km."
SCOPED = {**FACTS, "rare_animals_recorded_nearby": [], "human_history_nearby": [
    {"place": "Dhaka", "distance_m": 5503, "summary": "Dhaka is the capital of Bangladesh."}],
    "scope": {"rock": {"level": "wider_area", "note": "Only a regional-scale map covers this spot, so the rock name is a broader grouping and less precise."},
              "human": {"level": "nearest_place", "note": DHAKA_NOTE, "distance_km": 5.5, "place_name": "Dhaka"},
              "rare": {"level": "none", "note": "Nothing recorded: no records of rare animals within about 90 km."}}}


def test_prompt_has_the_scope_rule_and_passes_scopes_to_the_model():
    p = narrate.build_prompt(SCOPED, "main")
    rules, facts = p.split("FACTS:")
    assert "NEVER claim those facts happened at, or are under, the walker's exact spot" in " ".join(rules.split())
    assert '"scopes"' in facts and "the nearest city" in facts and "nearest_place" in facts
    assert '"scope"' not in facts.replace('"scopes"', "")  # the raw key is not sent twice
    assert rules.startswith("STRICT RULES")  # grounding rules are still first


def test_fallback_text_says_the_facts_are_about_the_wider_place():
    text = narrate.facts_only_text(SCOPED)
    assert text.index("About Dhaka, the nearest city") < text.index("Dhaka is the capital")
    assert "broader grouping" in text                       # coarser rock map is admitted
    assert "no records of rare animals within about 90 km" in text


def test_fallback_for_fossils_includes_the_distance_note():
    facts = {"fossils_found_nearby": {"count": 99, "classes": [["Trilobita", 69]], "taxa": [], "nearest_km": 107},
             "scope": {"fossils": {"level": "wider_area", "note": "Nothing of this age within about 55 km. Nearest finds about 107 km away."}}}
    text = narrate.facts_only_text(facts, "fossils")
    assert "Nearest finds about 107 km away" in text and "not everything that lived here" in text
    none = {"fossils_found_nearby": {}, "scope": {"fossils": {"level": "none", "note": "Nothing recorded: no fossils of this rock's age were found within about 222 km."}}}
    assert "Nothing recorded" in narrate.facts_only_text(none, "fossils")
