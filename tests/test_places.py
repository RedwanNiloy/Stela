"""Places to visit: museums and historic sites from the Wikipedia geosearch the app already runs.
Fixtures tests/samples/places_<place>.json are real geosearch + extract responses (50 nearest pages each)."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import geo, main, narrate
from app.cache import RateLimiter, TTLCache

SAMPLES = Path(__file__).parent / "samples"
USER = {"dover": (51.13, 1.31), "madison": (43.07, -89.40), "dhaka": (23.81, 90.41)}


def load(name):
    return json.loads((SAMPLES / f"places_{name}.json").read_text(encoding="utf-8"))


def places(name):
    d = load(name)
    return geo.parse_places(d["geosearch"], d["extracts"], *USER[name])


# ---------------------------------------------------------------- the matcher on real responses
def test_place_with_several_results_dover():
    out = places("dover")
    names = [e["name"] for e in out]
    assert len(out) == 5 and "Dover Museum" in names and "Maison Dieu, Dover" in names
    assert [e["distance_m"] for e in out] == sorted(e["distance_m"] for e in out)        # nearest first
    # things the old history keywords wrongly kept or Dover's other pages must not become "places to visit"
    for not_a_place in ("River Dour", "Dover College", "Dover Priory railway station", "King's Hall, Dover", "Dover Immigration Removal Centre"):
        assert not_a_place not in names
    by = {e["name"]: e for e in out}
    assert by["Dover Museum"]["type"] == "Museum"
    assert by["St James' Church, Dover"]["type"] == "Archaeological site or ruins"          # "a ruined church"
    first = out[0]
    assert set(first) == {"name", "type", "distance_m", "label", "lat", "lng", "pageid", "summary", "source", "url", "map_url"}
    assert first["source"] == "Wikipedia" and first["summary"] and len(first["summary"]) <= 180


def test_museums_in_madison_are_found_and_a_building_that_only_houses_one_is_not():
    names = [e["name"] for e in places("madison")]
    assert {"Chazen Museum of Art", "UW–Madison Geology Museum", "L. R. Ingersoll Physics Museum"} <= set(names)
    assert "Chamberlin Hall" not in names                   # its first sentence is not about a museum


def test_a_fragment_from_a_cut_extract_is_not_shown_as_a_summary():
    e = next(x for x in places("madison") if x["name"] == "L. R. Ingersoll Physics Museum")
    assert e["summary"] == "" and e["type"] == "Museum"        # Wikipedia's extract here is just "The L.R."
    assert all(len(x["summary"]) >= 25 for x in places("madison") if x["summary"])


def test_dense_city_with_few_results_dhaka():
    out = places("dhaka")
    assert [e["name"] for e in out] == ["Gulshan Society Mosque"]
    assert out[0]["type"] == "Place of worship"
    pages = load("dhaka")["geosearch"]
    assert len(pages) == 50 and max(p["dist"] for p in pages) < 2000     # the 50-page cap, not the 10 km radius, is the limit here


def test_place_with_none_gives_an_empty_list_not_filler():
    assert geo.parse_places([], {}, 0, -30) == []
    stadium = [{"pageid": 1, "title": "Bangladesh Army Stadium", "lat": 23.8, "lon": 90.4, "dist": 700}]
    ex = {"1": {"extract": "The Bangladesh Army Stadium is a multi-purpose stadium in Dhaka. It hosts matches."}}
    assert geo.parse_places(stadium, ex, 23.81, 90.41) == []


def test_old_history_keywords_did_not_know_museums():
    d = load("dover")
    assert "Dover Museum" not in [p["place"] for p in geo.parse_human(d["geosearch"], d["extracts"])]


def test_links_are_plain_urls_with_the_places_own_coordinates():
    e = next(x for x in places("dover") if x["name"] == "Dover Museum")
    assert e["url"] == f"https://en.wikipedia.org/?curid={e['pageid']}"
    assert e["map_url"] == f"https://www.openstreetmap.org/?mlat={e['lat']:.5f}&mlon={e['lng']:.5f}#map=17/{e['lat']:.5f}/{e['lng']:.5f}"
    assert "route" not in e["map_url"] and "directions" not in e["map_url"]


def test_at_most_five_entries():
    pages = [{"pageid": i, "title": f"Museum {i}", "lat": 51.13, "lon": 1.31 + i / 1000, "dist": 100 + i * 10} for i in range(1, 12)]
    out = geo.parse_places(pages, {}, 51.13, 1.31)
    assert len(out) == 5 and [e["name"] for e in out] == [f"Museum {i}" for i in range(1, 6)]


# ---------------------------------------------------------------- distance and compass direction
# Known map positions: Notre-Dame (48.8530 N, 2.3499 E), the Louvre (48.8606, 2.3376), the Eiffel Tower (48.8584, 2.2945),
# the Arc de Triomphe (48.8738, 2.2950). On a map the Louvre is up and to the LEFT of Notre-Dame, and the Eiffel Tower is left.
NOTRE_DAME, LOUVRE, EIFFEL, ARC = (48.8530, 2.3499), (48.8606, 2.3376), (48.8584, 2.2945), (48.8738, 2.2950)


def test_compass_direction_against_known_map_positions_so_it_is_not_flipped():
    assert geo.compass_name(*NOTRE_DAME, *LOUVRE) == "north-west"
    assert geo.compass_name(*NOTRE_DAME, *EIFFEL) == "west"
    assert geo.compass_name(*LOUVRE, *NOTRE_DAME) == "south-east"        # the reverse trip is the opposite point
    assert geo.compass_name(*EIFFEL, *ARC) == "north"
    assert geo.compass_name(*ARC, *EIFFEL) == "south"


def test_compass_sign_conventions_and_eight_points():
    o = (51.13, 1.31)
    d = lambda dlat, dlng: geo.compass_name(*o, o[0] + dlat, o[1] + dlng)
    assert (d(0.01, 0), d(0, 0.01), d(-0.01, 0), d(0, -0.01)) == ("north", "east", "south", "west")
    assert d(0.01, 0.0159) == "north-east" and d(-0.01, 0.0159) == "south-east"
    assert d(-0.01, -0.0159) == "south-west" and d(0.01, -0.0159) == "north-west"


def test_distance_label_is_a_straight_line_with_a_direction_and_no_walking_time():
    assert geo.distance_label(70, "north") == "about 70 m north"
    assert geo.distance_label(235, "south-east") == "about 240 m south-east"
    assert geo.distance_label(2900, "west") == "about 2.9 km west"
    assert geo.distance_label(3000, "north-east") == "about 3 km north-east"
    assert geo.distance_label(12400, "east") == "about 12 km east"
    for m in (70, 235, 2900, 3000, 12400):
        label = geo.distance_label(m, "north")
        assert not any(w in label for w in ("minute", "min ", "walk", "hour", "away"))


def test_dover_museum_is_south_of_the_search_point():
    e = next(x for x in places("dover") if x["name"] == "Dover Museum")
    assert e["lat"] < 51.13 and e["label"].endswith("south")        # its latitude is lower, so it is south: not flipped


# ---------------------------------------------------------------- no extra requests, no reverse geocoding
def test_places_come_from_the_same_requests_with_no_geocoder():
    d = load("dover")
    calls = []

    def handler(request):
        p = dict(request.url.params)
        calls.append((request.url.host, p))
        if p.get("list") == "geosearch":
            return httpx.Response(200, json={"query": {"geosearch": d["geosearch"]}})
        ids = p["pageids"].split("|")
        return httpx.Response(200, json={"query": {"pages": {i: d["extracts"][i] for i in ids if i in d["extracts"]}}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=geo.UA)
    entries, scope, geo_status, found = asyncio.run(geo.fetch_human(client, 51.13, 1.31))
    assert {c[0] for c in calls} == {"en.wikipedia.org"}                       # no Nominatim, no Overpass
    assert len([c for c in calls if c[1].get("list") == "geosearch"]) == 1     # the one geosearch the app already runs
    assert geo_status is None and scope["level"] == "exact_area"
    assert [e["name"] for e in found] == [e["name"] for e in places("dover")]


def test_places_still_returned_when_history_falls_back_to_the_nearest_city():
    """Madison: no history keywords match, so the history layer uses the nearest city. The museums must still show."""
    d = load("madison")

    def handler(request):
        p = dict(request.url.params)
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=json.loads((SAMPLES / "nominatim_madison.json").read_text(encoding="utf-8")))
        if p.get("list") == "geosearch":
            return httpx.Response(200, json={"query": {"geosearch": d["geosearch"]}})
        if "titles" in p:
            return httpx.Response(200, json=json.loads((SAMPLES / "wikipedia_intro_madison.json").read_text(encoding="utf-8")))
        ids = p["pageids"].split("|")
        return httpx.Response(200, json={"query": {"pages": {i: d["extracts"][i] for i in ids if i in d["extracts"]}}})
    geo._geo_state.update(last=0.0)
    geo._place_cache = TTLCache()
    geo.NOMINATIM_GAP = 0.0
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=geo.UA)
    entries, scope, geo_status, found = asyncio.run(geo.fetch_human(client, 43.07, -89.40))
    assert scope["level"] == "nearest_place" and len(found) >= 3 and found[0]["type"] == "Museum"


# ---------------------------------------------------------------- the narration rules
DOVER_FACTS = {"rock": None, "human_history_nearby": [], "rare_animals_recorded_nearby": [], "rare_animals_note": geo.RARE_NOTE,
               "places_to_visit": places("dover")[:3], "scope": {}}


def test_prompt_lists_places_with_exact_names_and_distances_and_the_rule():
    prompt = narrate.build_prompt(DOVER_FACTS, "main")
    rules, facts = prompt.split("FACTS:")
    flat = " ".join(rules.split())
    assert "mention only places that are listed in places_to_visit" in flat
    assert "do not suggest, recommend or invent any place" in flat
    assert "never give opening hours, prices or walking times" in flat
    assert '"name":"Dover Museum"' in facts and '"distance":"about 600 m south"' in facts
    assert "map_url" not in facts and "openstreetmap" not in facts          # only name, type and distance reach the model
    assert rules.startswith("STRICT RULES")                                 # grounding rules still first


def test_empty_list_story_says_nothing_recorded_and_invented_places_are_refused(monkeypatch):
    empty = {**DOVER_FACTS, "places_to_visit": [], "human_history_nearby": [{"place": "Somewhere", "distance_m": 5, "summary": "x."}]}
    assert "No museums or historic sites recorded nearby." in narrate.facts_only_text(empty)

    def reply(text):
        async def llm(client, prompt):
            return text, "online"
        monkeypatch.setattr(narrate, "call_llm", llm)
        return asyncio.run(narrate.narrate(None, empty, "main"))
    out, mode = reply("Stand still. Nothing is recorded nearby, but you could try the city museum.")
    assert mode == "facts-only" and "city museum" not in out and "No museums or historic sites recorded nearby." in out
    ok, mode = reply("Stand still. Nothing to visit was recorded nearby.")   # says nothing was recorded, names no kind of place
    assert mode != "facts-only" and ok.startswith("Stand still")


def test_story_that_names_a_supplied_place_is_kept_and_an_unsupplied_castle_is_not(monkeypatch):
    def reply(text):
        async def llm(client, prompt):
            return text, "online"
        monkeypatch.setattr(narrate, "call_llm", llm)
        return asyncio.run(narrate.narrate(None, DOVER_FACTS, "main"))
    good = "Stand still. Dover Museum is recorded about 600 m south."
    assert reply(good) == (good, "online")
    text, mode = reply("Stand still. Dover Museum is about 600 m south, and a grand castle rises beyond it.")
    assert mode == "facts-only" and "castle" not in text.lower()


def test_facts_only_text_has_the_note_and_never_promises_opening():
    text = narrate.facts_only_text(DOVER_FACTS)
    assert "Dover Museum (about 600 m south)" in text and "Check access and opening times before you go." in text
    assert not any(w in text.lower() for w in ("is open", "opening hours", "admission", "ticket", "free entry"))
    assert "could not be checked" in narrate.facts_only_text({**DOVER_FACTS, "places_to_visit": None})


# ---------------------------------------------------------------- the API
def test_where_returns_places_and_survives_a_failed_history_source(monkeypatch):
    main.where_cache = TTLCache()
    main.limiter = RateLimiter(100, 60)

    async def fake_rock(c, lat, lng):
        return None

    async def fake_rare(c, lat, lng, lang=None):
        return [], geo.rare_scope(None, None)

    async def fake_human(c, lat, lng):
        return [], geo.human_scope("none", geo_note="x"), "empty", places("dover")

    async def llm(c, prompt):
        raise RuntimeError("no model")
    monkeypatch.setattr(geo, "fetch_rock", fake_rock)
    monkeypatch.setattr(geo, "fetch_rare", fake_rare)
    monkeypatch.setattr(geo, "fetch_human", fake_human)
    monkeypatch.setattr(narrate, "call_llm", llm)
    with TestClient(main.app) as api:
        d = api.get("/api/where", params={"lat": 51.13, "lng": 1.31}).json()
        assert [e["name"] for e in d["facts"]["places_to_visit"]][:2] == ["Maison Dieu, Dover", "St Mary's Church, Dover"]
        assert "Dover Museum" in d["narration"]            # even with no model, the plain summary names them
        main.where_cache = TTLCache()

        async def broken(c, lat, lng):
            raise httpx.ReadTimeout("slow")
        monkeypatch.setattr(geo, "fetch_human", broken)
        d2 = api.get("/api/where", params={"lat": 51.13, "lng": 1.31}).json()
        assert d2["facts"]["places_to_visit"] is None and d2["narration"]
