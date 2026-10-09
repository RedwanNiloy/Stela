"""Fallback path, cache, rate limit and the API contract. No network: the layers and the LLM are faked."""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import geo, main, narrate
from app.cache import RateLimiter, TTLCache, cache_key

FACTS = {
    "rock": {"name": "White Chalk Subgroup", "lith": "chalk", "b_age": 100.5, "t_age": 66, "b_int_name": "Cenomanian"},
    "ancient_setting": {"type": "marine", "meaning": "a sea or ocean floor"},
    "time_ruler": {"times_older_than_humans": 335},
    "fossils_found_nearby": "pending",
    "rare_animals_alive_today": [],
    "human_history_nearby": None,  # source down
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

    async def fake_rare(c, lat, lng):
        raise httpx.ReadTimeout("slow")  # one source fails

    async def fake_human(c, lat, lng):
        return []

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
    assert d["facts"]["rare_animals_alive_today"] is None        # failed source -> null
    assert d["facts"]["rock"]["name"] == "White Chalk Subgroup"  # the others are unaffected
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
