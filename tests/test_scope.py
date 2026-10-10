"""Scope fallback ladders. The real fetch_* functions run against a mock HTTP layer that serves saved real
responses (tests/samples), so there is no network. Each test also checks HOW MANY calls were made, because
every ladder must be capped and a wider step must only run when the narrower one found nothing."""
import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from app import geo
from app.cache import TTLCache

SAMPLES = Path(__file__).parent / "samples"
DOVER = (51.13, 1.31)
MADISON = (43.07, -89.40)


def load(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


UNIT = {"b_age": 100.5, "t_age": 66, "name": "White Chalk Subgroup", "lith": "chalk", "map_scale": "large"}


class Net:
    """A fake internet. `routes` maps a host to a function(params) -> (status, json). Records every call."""

    def __init__(self, **routes):
        self.routes, self.calls = routes, []

    def client(self):
        def handler(request):
            params = dict(request.url.params)
            self.calls.append((request.url.host, params, dict(request.headers)))
            status, body = self.routes[request.url.host](params)
            return httpx.Response(status, json=body)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=geo.UA)

    def to(self, host):
        return [c for c in self.calls if c[0] == host]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh_geocoder(monkeypatch):
    monkeypatch.setattr(geo, "_place_cache", TTLCache())
    monkeypatch.setattr(geo, "NOMINATIM_GAP", 0.0)
    geo._geo_state.update(last=0.0)


# ---------------------------------------------------------------- rock (map scale ladder)
def test_rock_scope_levels():
    for scale, level in (("large", "exact_area"), ("medium", "wider_area"), ("small", "region")):
        assert geo.rock_scope({"map_scale": scale})["level"] == level
    assert geo.rock_scope(None)["level"] == "none"


def test_rock_ladder_reports_the_scale_that_matched_and_stops_there():
    sample = load("macrostrat_dover.json")
    empty = {"success": {"data": []}}
    net = Net(**{"macrostrat.org": lambda p: (200, empty if p["scale"] == "large" else sample)})
    unit = run(geo.fetch_rock(net.client(), *DOVER))
    assert unit["map_scale"] == "medium"
    assert [c[1]["scale"] for c in net.calls] == ["large", "medium"]  # "small" was never asked
    assert geo.rock_scope(unit)["level"] == "wider_area"


def test_rock_ladder_cap_returns_none():
    net = Net(**{"macrostrat.org": lambda p: (200, {"success": {"data": []}})})
    assert run(geo.fetch_rock(net.client(), *DOVER)) is None
    assert len(net.calls) == 3


# ---------------------------------------------------------------- rare animals (30 km, then one wider step)
FACET = load("gbif_dover.json")
NO_FACET = {"count": 0, "facets": [{"field": "SCIENTIFIC_NAME", "counts": []}]}
DETAIL = {"results": load("gbif_dhaka_records.json")[:30]}


def gbif_route(narrow=True, wide=True):
    def route(p):
        if "facet" in p:  # the species-count call; the two steps differ by the size of the box
            lo, hi = map(float, p["decimalLatitude"].split(","))
            is_narrow = (hi - lo) < 1.0
            return 200, FACET if (narrow if is_narrow else wide) else NO_FACET
        return 200, DETAIL
    return route


def test_rare_exact_area_runs_one_step_only():
    net = Net(**{"api.gbif.org": gbif_route(narrow=True)})
    entries, scope = run(geo.fetch_rare(net.client(), *DOVER))
    assert scope["level"] == "exact_area" and scope["distance_km"] == 30
    assert entries and entries[0]["species"] == "Rissa tridactyla"
    assert len([c for c in net.calls if "facet" in c[1]]) == 1  # the wider step was NOT run


def test_rare_widens_once_when_nothing_within_30_km():
    net = Net(**{"api.gbif.org": gbif_route(narrow=False, wide=True)})
    entries, scope = run(geo.fetch_rare(net.client(), *DOVER))
    assert scope["level"] == "wider_area" and scope["distance_km"] == 90
    assert "NOT from your immediate area" in scope["note"]
    facet_calls = [c[1] for c in net.calls if "facet" in c[1]]
    assert len(facet_calls) == 2
    assert facet_calls[0]["facetLimit"] == "8" and facet_calls[1]["facetLimit"] == "5"  # tighter cap when wider
    detail_calls = [c[1] for c in net.calls if "scientificName" in c[1]]
    assert {c["limit"] for c in detail_calls} == {"25"}  # and fewer records inspected per species


def test_rare_cap_returns_nothing_recorded():
    net = Net(**{"api.gbif.org": gbif_route(narrow=False, wide=False)})
    entries, scope = run(geo.fetch_rare(net.client(), *DOVER))
    assert entries == [] and scope["level"] == "none" and "Nothing recorded" in scope["note"]
    assert len(net.calls) == 2  # exactly two facet calls, then stop


def test_rare_area_is_a_real_30km_radius():
    eq = geo.rare_area(0, 0, 30)
    north = geo.rare_area(60, 0, 30)

    def width(area):
        west, east = map(float, area["decimalLongitude"].split(","))
        return east - west

    assert 0.5 < width(eq) < 0.6          # about 60 km wide at the equator
    assert width(north) > width(eq) * 1.8  # degrees of longitude are about twice as wide in km at 60 N


# ---------------------------------------------------------------- fossils (box, wider box, any age, country)
DOVER_RECORDS = load("pbdb_dover.json")
US_RECORDS = load("pbdb_country_us.json")
NOTHING = {"records": []}


def pbdb_route(a=False, b=False, c=False, d=False):
    """a/b/c/d say which ladder step returns records. Steps are told apart by their parameters."""
    def route(p):
        if "cc" in p:
            return 200, US_RECORDS if d else NOTHING
        wide = float(p["lngmax"]) - float(p["lngmin"]) > 2.0
        aged = "max_ma" in p
        if not wide:
            return 200, DOVER_RECORDS if a else NOTHING
        return 200, DOVER_RECORDS if (b if aged else c) else NOTHING
    return route


def fossil_net(geocoder="madison", **steps):
    routes = {"paleobiodb.org": pbdb_route(**steps)}
    if geocoder:
        routes["nominatim.openstreetmap.org"] = lambda p: (200, load(f"nominatim_{geocoder}.json"))
    return Net(**routes)


def test_fossils_exact_area_with_real_nearest_distance():
    net = fossil_net(a=True)
    found, scope = run(geo.fetch_fossils(net.client(), *DOVER, UNIT))
    assert scope["level"] == "exact_area" and found["count"] == len(DOVER_RECORDS["records"])
    assert scope["distance_km"] == pytest.approx(found["nearest_km"], abs=0.1) and scope["distance_km"] < 5
    assert "Nearest finds about" in scope["note"]
    assert len(net.to("paleobiodb.org")) == 1 and not net.to("nominatim.openstreetmap.org")


def test_fossils_wider_box_runs_only_after_the_narrow_one_is_empty():
    net = fossil_net(b=True)
    found, scope = run(geo.fetch_fossils(net.client(), *DOVER, UNIT))
    assert scope["level"] == "wider_area" and "widened" in scope["note"]
    assert len(net.to("paleobiodb.org")) == 2


def test_fossils_any_age_step_says_age_is_not_matched():
    net = fossil_net(c=True)
    found, scope = run(geo.fetch_fossils(net.client(), *DOVER, UNIT))
    assert scope["level"] == "wider_area" and "NOT matched to the rock's age" in scope["note"]
    assert "max_ma" not in net.to("paleobiodb.org")[-1][1]
    assert len(net.to("paleobiodb.org")) == 3


def test_fossils_country_step_uses_cc_from_the_geocoder_and_gives_no_distance():
    net = fossil_net(d=True)
    found, scope = run(geo.fetch_fossils(net.client(), *MADISON, UNIT))
    assert scope["level"] == "region" and scope["place_name"] == "United States" and scope["distance_km"] is None
    assert "not from near you" in scope["note"]
    assert net.to("paleobiodb.org")[-1][1]["cc"] == "US"
    assert len(net.to("paleobiodb.org")) == 4 and len(net.to("nominatim.openstreetmap.org")) == 1


def test_fossils_cap_returns_nothing_recorded_after_four_steps():
    net = fossil_net()
    found, scope = run(geo.fetch_fossils(net.client(), *MADISON, UNIT))
    assert found is None and scope["level"] == "none"
    assert "Nothing recorded" in scope["note"] and "elsewhere in United States" in scope["note"]
    assert len(net.to("paleobiodb.org")) == 4  # the cap


def test_fossils_open_ocean_skips_the_country_step():
    net = fossil_net(geocoder="ocean")
    found, scope = run(geo.fetch_fossils(net.client(), 0, -30, UNIT))
    assert found is None and scope["level"] == "none" and "elsewhere in" not in scope["note"]
    assert len(net.to("paleobiodb.org")) == 3


def test_fossils_all_calls_failing_raises():
    net = Net(**{"paleobiodb.org": lambda p: (500, {}), "nominatim.openstreetmap.org": lambda p: (500, {})})
    with pytest.raises(httpx.HTTPStatusError):
        run(geo.fetch_fossils(net.client(), *DOVER, UNIT))


def test_fossils_without_a_rock_age_cannot_be_matched():
    found, scope, status = run(geo.slow_fossils(None, *DOVER, None))
    assert found is None and scope["level"] == "none" and status == "empty"


# ---------------------------------------------------------------- human history (10 km, then nearest place)
WIKI_DOVER = load("wikipedia_dover.json")
WIKI_NOTHING = {"query": {"geosearch": []}}
INTRO = load("wikipedia_intro_madison.json")


def wiki_route(history=False, intro=True, geosearch_fails=False):
    def route(p):
        if p.get("list") == "geosearch":
            if geosearch_fails:
                return 500, {}
            return 200, {"query": {"geosearch": WIKI_DOVER["geosearch"]}} if history else WIKI_NOTHING
        if "pageids" in p:
            return 200, {"query": {"pages": WIKI_DOVER["extracts"]}}
        if "titles" in p:
            return 200, INTRO if intro else {"query": {"pages": {"-1": {"title": p["titles"], "missing": ""}}}}
        raise AssertionError(p)
    return route


def human_net(geocoder="madison", **wiki):
    routes = {"en.wikipedia.org": wiki_route(**wiki)}
    if geocoder == "fail":
        routes["nominatim.openstreetmap.org"] = lambda p: (500, {})
    elif geocoder:
        routes["nominatim.openstreetmap.org"] = lambda p: (200, load(f"nominatim_{geocoder}.json"))
    return Net(**routes)


def test_human_exact_area_never_calls_the_geocoder():
    net = human_net(history=True)
    entries, scope, geo_status = run(geo.fetch_human(net.client(), *DOVER))
    assert scope["level"] == "exact_area" and entries and geo_status is None
    assert not net.to("nominatim.openstreetmap.org")


def test_human_falls_back_to_the_nearest_city_and_its_intro():
    net = human_net()
    entries, scope, geo_status = run(geo.fetch_human(net.client(), *MADISON))
    assert scope["level"] == "nearest_place" and scope["place_name"] == "Madison"
    assert 0 < scope["distance_km"] < 10
    assert "not your exact spot" in scope["note"] and "the nearest city" in scope["note"]
    assert entries[0]["place"] == "Madison" and entries[0]["summary"].startswith("Madison is the capital")
    assert geo_status == "ok"
    # the geocoder was asked politely: identifying User-Agent, English names, city-level zoom
    host, params, headers = net.to("nominatim.openstreetmap.org")[0]
    assert headers["user-agent"].startswith("stela/") and params["accept-language"] == "en" and params["zoom"] == "10"
    # the Wikipedia title came from the OSM tag "en:Madison, Wisconsin"
    assert net.to("en.wikipedia.org")[-1][1]["titles"] == "Madison, Wisconsin"


def test_human_region_level_when_the_nearest_place_is_a_state():
    net = human_net(geocoder="uluru")
    entries, scope, _ = run(geo.fetch_human(net.client(), -25.34, 131.04))
    assert scope["level"] == "region" and scope["place_name"] == "Northern Territory"
    assert scope["distance_km"] is None and entries[0]["distance_m"] is None
    assert "the wider region" in scope["note"]
    assert net.to("en.wikipedia.org")[-1][1]["titles"] == "Northern Territory"


def test_human_cap_nothing_recorded_in_open_water():
    net = human_net(geocoder="ocean")
    entries, scope, geo_status = run(geo.fetch_human(net.client(), 0, -30))
    assert entries == [] and scope["level"] == "none" and "Nothing recorded" in scope["note"]
    assert "open water" in scope["note"] and geo_status == "empty"


def test_human_cap_when_the_geocoder_is_down():
    net = human_net(geocoder="fail")
    entries, scope, geo_status = run(geo.fetch_human(net.client(), *MADISON))
    assert entries == [] and scope["level"] == "none" and "could not be reached" in scope["note"]
    assert geo_status.startswith("failed")


def test_human_cap_when_the_place_has_no_wikipedia_page():
    net = human_net(intro=False)
    entries, scope, _ = run(geo.fetch_human(net.client(), *MADISON))
    assert entries == [] and scope["level"] == "none"


def test_human_wikipedia_failure_raises_so_the_source_is_reported_failed():
    net = human_net(geosearch_fails=True)
    with pytest.raises(httpx.HTTPStatusError):
        run(geo.fetch_human(net.client(), *MADISON))
    assert not net.to("nominatim.openstreetmap.org")  # no point asking for a fallback of a broken source


# ---------------------------------------------------------------- geocoder: cached and rate-limited
def test_geocoder_result_is_cached():
    net = human_net()

    async def twice():
        async with net.client() as c:
            await geo.fetch_human(c, *MADISON)
            await geo.fetch_human(c, 43.0704, -89.4003)  # same place after rounding to 3 decimals
    run(twice())
    assert len(net.to("nominatim.openstreetmap.org")) == 1


def test_geocoder_is_spaced_to_the_rate_limit(monkeypatch):
    monkeypatch.setattr(geo, "NOMINATIM_GAP", 0.3)
    stamps = []

    def route(p):
        stamps.append(time.monotonic())
        return 200, load("nominatim_madison.json")
    net = Net(**{"nominatim.openstreetmap.org": route})

    async def three_at_once():
        async with net.client() as c:
            await asyncio.gather(*(geo.reverse_geocode(c, 10 + i, 20) for i in range(3)))
    run(three_at_once())
    assert len(stamps) == 3
    assert all(b - a >= 0.28 for a, b in zip(stamps, stamps[1:]))


def test_parse_place_handles_city_region_and_nothing():
    city = geo.parse_place(load("nominatim_madison.json"))
    assert city["is_place"] and city["country_code"] == "US" and city["wiki_title"] == "Madison, Wisconsin"
    region = geo.parse_place(load("nominatim_uluru.json"))
    assert not region["is_place"] and region["name"] == "Northern Territory"
    assert geo.parse_place(load("nominatim_ocean.json")) is None
