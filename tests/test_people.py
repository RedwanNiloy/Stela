"""People and past civilizations. The real fetch pipeline runs against recorded conversations with Nominatim and the
MediaWiki API (tests/samples/people_<place>.json, made by tests/capture_people.py), so there is no network.
Unknown requests are recorded as misses and every pipeline test asserts there were none."""
import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import geo, main, narrate, people
from app.cache import RateLimiter, TTLCache

SAMPLES = Path(__file__).parent / "samples"
DOVER = (51.13, 1.31)
MADISON = (43.07, -89.40)
DHAKA = (23.81, 90.41)


def cassette(name):
    return json.loads((SAMPLES / f"people_{name}.json").read_text(encoding="utf-8"))


class Replay:
    """A fake internet that answers only what was recorded."""

    def __init__(self, entries, edit=None):
        self.table = {(e["host"], tuple(sorted(e["params"].items()))): e for e in entries}
        self.calls, self.misses, self.edit = [], [], edit

    def client(self):
        def handler(request):
            params = dict(request.url.params)
            self.calls.append((request.url.host, params))
            e = self.table.get((request.url.host, tuple(sorted(params.items()))))
            if not e:
                self.misses.append((request.url.host, params))
                return httpx.Response(404, json={"error": "not recorded"})
            body = e.get("json")
            if self.edit:
                body = self.edit(request.url.host, params, json.loads(json.dumps(body)))
            return httpx.Response(e["status"], json=body)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=geo.UA)

    def wiki_calls(self):
        return [c for c in self.calls if c[0] == "en.wikipedia.org"]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch):
    for mod, name in ((geo, "_place_cache"), (geo, "_address_cache"), (people, "_article_cache"), (people, "_titles_cache")):
        monkeypatch.setattr(mod, name, TTLCache())
    monkeypatch.setattr(geo, "NOMINATIM_GAP", 0.0)
    monkeypatch.setattr(people, "WIKI_GAP", 0.0)
    geo._geo_state.update(last=0.0)


def fetch(place, coords, edit=None):
    net = Replay(cassette(place), edit)
    facts, statuses = run(_fetch(net, coords))
    assert net.misses == [], net.misses
    return facts, statuses, net


async def _fetch(net, coords):
    async with net.client() as c:
        return await people.fetch_people(c, *coords)


def levels(facts):
    return [e["scope"]["level"] for e in facts["early_history"]]


# ---------------------------------------------------------------- the place hierarchy (real Nominatim fields)
# A real zoom-18 reply for Dhaka (kept inline: the fields that matter and the places they differ from Dover)
DHAKA_ADDRESS = {"road": "DOHS Road", "residential": "Baridhara DOHS", "quarter": "Baridhara DOHS", "suburb": "Joar Sahara",
                 "city": "Dhaka", "county": "Dhaka Metropolitan", "state_district": "Dhaka District", "state": "Dhaka Division",
                 "country": "Bangladesh", "country_code": "bd"}


def test_hierarchy_dover_dedupes_town_and_city():
    entry = next(e for e in cassette("dover") if e["host"] == "nominatim.openstreetmap.org")
    assert entry["params"]["zoom"] == "18" and entry["params"]["accept-language"] == "en" and entry["params"]["addressdetails"] == "1"
    h = people.parse_hierarchy(entry["json"]["address"])
    assert [(x["level"], x["names"]) for x in h] == [("your_area", ["Tower Hamlets"]), ("city", ["Dover"]), ("district", ["Kent"]),
                                                     ("region", ["England"]), ("country", ["United Kingdom"])]
    assert [x["parent"] for x in h] == ["Dover", "England", "England", "United Kingdom", None]


def test_hierarchy_dhaka_has_two_district_names_and_a_division():
    h = {x["level"]: x for x in people.parse_hierarchy(DHAKA_ADDRESS)}
    assert h["your_area"]["names"] == ["Baridhara DOHS", "Joar Sahara"]
    assert h["district"]["names"] == ["Dhaka Metropolitan", "Dhaka District"]
    assert h["region"]["names"] == ["Dhaka Division"] and h["country"]["names"] == ["Bangladesh"]
    assert h["city"]["parent"] == "Dhaka Division"


def test_hierarchy_madison_and_empty():
    entry = next(e for e in cassette("madison") if e["host"] == "nominatim.openstreetmap.org")
    h = {x["level"]: x["names"] for x in people.parse_hierarchy(entry["json"]["address"])}
    assert h["city"] == ["Madison"] and h["district"] == ["Dane County"] and h["region"] == ["Wisconsin"]
    assert people.parse_hierarchy(None) == [] and people.parse_hierarchy({}) == []


def test_candidate_titles_order_and_qualification():
    assert people.candidate_titles("Madison", "Wisconsin") == [("History of Madison", False), ("History of Madison, Wisconsin", True),
                                                               ("Madison, Wisconsin", True), ("Madison", False)]
    assert people.candidate_titles("Bangladesh", None) == [("History of Bangladesh", False), ("Bangladesh", False)]


# ---------------------------------------------------------------- choosing the article (homonyms and disambiguation pages)
def page(title, extract="Some text about Dover and Kent.", **props):
    return {"title": title, "extract": extract, "pageprops": props}


def lookup_of(*pages):
    by = {p["title"]: p for p in pages}
    redirects = {}
    for p in pages:
        if "_from" in p:
            redirects[p["_from"]] = p
    return lambda t: redirects.get(t) or by.get(t)


ENTRY = {"level": "city", "label": "city", "names": ["Madison"], "parent": "Wisconsin"}


def test_disambiguation_page_is_never_used():
    got = people.pick_article(ENTRY, lookup_of(page("Madison", "Madison may refer to:", disambiguation=""),
                                               page("Madison, Wisconsin", "Madison is the capital of Wisconsin.")), [])
    assert got[1]["title"] == "Madison, Wisconsin" and got[2] == "place"       # the qualified article, not the disambiguation page
    assert people.pick_article({**ENTRY, "parent": None}, lookup_of(page("Madison", "Madison may refer to:", disambiguation="")), []) is None


def test_homonym_must_mention_the_parent_place():
    """Dover's suburb 'Tower Hamlets' resolved to the London borough because both mention England. The parent is 'Dover'."""
    suburb = {"level": "your_area", "label": "your area", "names": ["Tower Hamlets"], "parent": "Dover"}
    borough = page("London Borough of Tower Hamlets", "The London Borough of Tower Hamlets is a borough in London, England.", )
    borough["_from"] = "Tower Hamlets"
    assert people.pick_article(suburb, lookup_of(borough), ["Kent", "England", "United Kingdom"]) is None
    right = page("Tower Hamlets, Dover", "Tower Hamlets is a suburb of Dover.")
    assert people.pick_article(suburb, lookup_of(right), [])[1]["title"] == "Tower Hamlets, Dover"


def test_exact_history_article_is_accepted_but_a_redirect_must_mention_the_parent():
    city = {"level": "city", "label": "city", "names": ["Dover"], "parent": "England"}
    exact = page("History of Dover", "The History of Dover begins when Stone Age people crossed.")
    assert people.pick_article(city, lookup_of(exact), [])[2] == "history"
    moved = page("History of Elsewhere", "Something about Elsewhere.")
    moved["_from"] = "History of Dover"
    assert people.pick_article(city, lookup_of(moved), []) is None


def test_country_level_needs_no_parent_check():
    country = {"level": "country", "label": "country", "names": ["United Kingdom"], "parent": None}
    moved = page("History of the United Kingdom", "The history of the United Kingdom begins in 1707.")
    moved["_from"] = "History of United Kingdom"
    assert people.pick_article(country, lookup_of(moved), [])[1]["title"] == "History of the United Kingdom"


# ---------------------------------------------------------------- the earliest-period section
def sec(index, line, toclevel=1):
    return {"index": str(index), "line": line, "toclevel": toclevel, "level": toclevel + 1, "anchor": line.replace(" ", "_")}


def test_early_section_of_a_history_article_is_the_first_real_section():
    dhaka = [sec(1, "Etymology"), sec(2, "Early history"), sec(3, "Kamarupa kingdom"), sec(4, "Early modern history")]
    assert people.choose_early_section(dhaka, "history")["line"] == "Early history"           # Etymology is skipped
    uk = [sec(1, "18th century"), sec(2, "Early 20th century (1901–1918)")]
    assert people.choose_early_section(uk, "history")["line"] == "18th century"               # "early" later on must not win
    assert people.choose_early_section([sec(1, "See also"), sec(2, "References")], "history") is None


def test_early_section_of_a_place_article_is_the_first_subsection_of_history():
    madison = [sec(1, "History"), sec(2, "Native Americans", 2), sec(3, "Founding", 2), sec(4, "Geography")]
    assert people.choose_early_section(madison, "place")["line"] == "Native Americans"
    assert people.choose_early_section([sec(1, "History"), sec(2, "Geography")], "place")["line"] == "History"  # no subsection
    assert people.choose_early_section([sec(1, "Etymology"), sec(2, "Economy")], "place") is None              # no history at all


# ---------------------------------------------------------------- reading the HTML and cutting sentences
def test_section_html_gives_only_running_prose():
    entry = next(e for e in cassette("dover") if e["params"].get("action") == "parse" and e["params"].get("prop") == "text")
    paras = people.html_paragraphs(entry["json"]["parse"]["text"]["*"])
    assert 1 <= len(paras) <= people.SECTION_PARAGRAPHS
    blob = " ".join(paras)
    assert "Main article:" not in blob and "mw-parser-output" not in blob and "{" not in blob      # hatnote and style blocks skipped
    assert "[1]" not in blob and "<" not in blob


def test_sentence_splitting_and_caps():
    text = "The church of St. Mary is old. It was built in 1100. It has a tower. It has a bell. It has a door."
    assert people.split_sentences(text)[0] == "The church of St. Mary is old."
    assert len(people.split_sentences(people.take_sentences(people.split_sentences(text), 3))) == 3   # at most 3 sentences
    assert people.take_sentences(people.split_sentences(text), 2).endswith("It was built in 1100.")
    assert len(people.take_sentences(["word " * 200], 3)) <= people.MAX_EXCERPT_CHARS                # one huge sentence is shortened
    assert people.take_sentences(["A. " * 3, "B " * 300], 3, 60).count("B") == 0                   # does not run past the length cap


# ---------------------------------------------------------------- traditions: explicit words only
@pytest.mark.parametrize("sentence", [
    "According to legend, the town was founded by a giant.",
    "The myth of the white cliffs is told to children.",
    "Local folklore says a ferryman lived here.",
    "According to tradition, the chapel marks the first landing.",
    "The well is said to have healing water.",
    "An oral tradition records the flood.",
    "The legendary king is buried nearby.",
])
def test_tradition_detection_positive(sentence):
    assert people.is_tradition(sentence)


@pytest.mark.parametrize("sentence", [
    "Dover was traditionally a fishing town.",
    "The borough spans the traditional East End.",
    "There is a long tradition of shipbuilding here.",
    "The earliest archaeological evidence is Neolithic flint.",
    "It was said by the council that rates would rise.",
])
def test_tradition_detection_negative_no_guessing(sentence):
    assert not people.is_tradition(sentence)


def test_traditions_are_separated_labelled_and_kept_out_of_the_excerpt():
    entry = {"level": "city", "label": "city", "names": ["Dover"], "parent": "England"}
    pg = {"title": "History of Dover", "extract": "Dover is a port. According to legend, a giant founded it. It faces France."}
    details = {"section": {"line": "Earliest inhabitants", "anchor": "Earliest_inhabitants"},
               "paragraphs": ["Flint tools were found. The myth of the sunken town is old. Pottery came later."]}
    ex, traditions = people.build_excerpt(entry, "Dover", pg, "history", details)
    assert "legend" not in ex["intro"] and "myth" not in ex["early"]["text"]
    assert ex["intro"] == "Dover is a port. It faces France."
    assert [t["trigger"] for t in traditions] == ["according to legend", "myth"]
    t = traditions[0]
    assert t["label"] == "tradition, not verified history" and t["scope"]["label"] == "city" and t["article"]["title"] == "History of Dover"


# ---------------------------------------------------------------- the whole pipeline, replayed
def test_dover_place_with_a_history_article():
    facts, statuses, net = fetch("dover", DOVER)
    assert statuses == {"nominatim": "ok", "wikipedia": "ok"}
    assert levels(facts) == ["city", "district", "region", "country"]      # no "your area": see the homonym test below
    city = facts["early_history"][0]
    assert city["article"] == {"title": "History of Dover", "url": "https://en.wikipedia.org/wiki/History_of_Dover",
                               "history_url": "https://en.wikipedia.org/w/index.php?title=History_of_Dover&action=history", "kind": "history"}
    assert city["scope"] == {"level": "city", "label": "city", "place_name": "Dover"}
    assert city["early"]["section"] == "Earliest inhabitants"
    assert city["early"]["url"] == "https://en.wikipedia.org/wiki/History_of_Dover#Earliest_inhabitants"
    assert city["early"]["text"].startswith("The earliest archaeological evidence")
    assert city["license"] == {"name": "CC BY-SA 4.0", "url": "https://creativecommons.org/licenses/by-sa/4.0/"}
    assert [e["article"]["title"] for e in facts["early_history"]] == ["History of Dover", "History of Kent", "History of England", "History of the United Kingdom"]
    for e in facts["early_history"]:                                        # length caps and attribution data on every excerpt
        assert len(people.split_sentences(e["intro"] or "")) <= people.INTRO_SENTENCES
        assert len(people.split_sentences(e["early"]["text"] if e["early"] else "")) <= people.EARLY_SENTENCES
        assert e["article"]["url"].startswith("https://en.wikipedia.org/wiki/") and e["scope"]["label"] and e["license"]["name"] == "CC BY-SA 4.0"
    assert len([c for c in net.calls if c[0] == "nominatim.openstreetmap.org"]) == 1


def test_dover_suburb_homonym_is_not_used_as_your_area():
    """Regression: 'Tower Hamlets' (a Dover suburb) used to resolve to the London borough of the same name."""
    facts, _, _ = fetch("dover", DOVER)
    assert "your_area" not in levels(facts)
    assert not any("London Borough" in e["article"]["title"] for e in facts["early_history"])


def test_madison_place_without_a_history_article_falls_back_to_its_own_article():
    facts, statuses, net = fetch("madison", MADISON)
    assert statuses["wikipedia"] == "ok"
    by = {e["scope"]["level"]: e for e in facts["early_history"]}
    assert by["city"]["article"]["title"] == "Madison, Wisconsin" and by["city"]["article"]["kind"] == "place"   # 'History of Madison, Wisconsin' is a redirect
    assert by["city"]["early"]["section"] == "Native Americans"                                                    # first subsection of "History"
    assert by["district"]["article"]["title"] == "Dane County, Wisconsin"
    assert by["region"]["article"]["kind"] == "history" and by["region"]["early"]["section"] == "Pre-Columbian history"
    assert by["country"]["article"]["title"] == "History of the United States"
    assert "your_area" not in by                                      # nothing found for the neighbourhood: no excerpt, no guess
    assert all("Madison" != e["article"]["title"] for e in facts["early_history"])   # never the disambiguation page


def test_a_failed_wikipedia_lookup_is_reported_not_raised():
    net = Replay([e for e in cassette("dover") if e["host"] == "nominatim.openstreetmap.org"])
    facts, statuses = run(_fetch(net, DOVER))
    assert facts["early_history"] is None and statuses["wikipedia"].startswith("failed")      # 404s from the fake: every batch failed


def test_geocoder_failure_is_reported():
    net = Replay([])
    facts, statuses = run(_fetch(net, DOVER))
    assert facts["early_history"] is None and statuses["nominatim"].startswith("failed")
    assert statuses["wikipedia"] == "not needed"


def test_open_water_has_no_hierarchy_and_no_wikipedia_calls():
    net = Replay([{"host": "nominatim.openstreetmap.org", "status": 200, "json": {"error": "Unable to geocode"},
                   "params": {"lat": "0", "lon": "-30", "format": "jsonv2", "addressdetails": "1", "zoom": "18", "accept-language": "en"}}])
    facts, statuses = run(_fetch(net, (0, -30)))
    assert facts["early_history"] == [] and statuses["nominatim"] == "empty"
    assert net.wiki_calls() == []


def test_traditions_found_in_a_real_article_are_listed_separately():
    def add_legend(host, params, body):
        if params.get("action") == "parse" and params.get("prop") == "text" and params.get("page") == "History of Dover":
            body["parse"]["text"]["*"] = body["parse"]["text"]["*"].replace(
                "<p>", "<p>According to legend, a giant first built the harbour wall. ", 1)
        return body
    facts, _, _ = fetch("dover", DOVER, edit=add_legend)
    assert len(facts["traditions"]) == 1
    t = facts["traditions"][0]
    assert t["label"] == "tradition, not verified history" and t["scope"]["level"] == "city"
    assert "giant" in t["text"]
    assert "giant" not in json.dumps(facts["early_history"])           # not in any excerpt


def test_caching_means_a_second_lookup_makes_no_requests():
    net = Replay(cassette("dover"))

    async def twice():
        async with net.client() as c:
            await people.fetch_people(c, *DOVER)
            n = len(net.calls)
            await people.fetch_people(c, 51.1301, 1.3099)           # the same place after rounding
            return n
    first = run(twice())
    assert len(net.calls) == first and net.misses == []


def test_requests_are_limited_in_parallel_and_the_hierarchy_call_is_polite():
    peak, live = [0], [0]
    net = Replay(cassette("dover"))
    real = net.client

    def client():
        c = real()
        inner = c._transport

        class Counting(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if "wikipedia" in request.url.host:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                    await asyncio.sleep(0.02)
                    live[0] -= 1
                return await inner.handle_async_request(request)
        return httpx.AsyncClient(transport=Counting(), headers=geo.UA)
    net.client = client
    run(_fetch(net, DOVER))
    assert peak[0] <= people.MAX_PARALLEL
    nominatim = next(c for c in net.calls if c[0] == "nominatim.openstreetmap.org")
    assert nominatim[1]["zoom"] == "18"


# ---------------------------------------------------------------- the narration rules
def excerpt(level, label, place, title, intro, early=None, kind="history"):
    return {"scope": {"level": level, "label": label, "place_name": place},
            "article": {"title": title, "url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"), "kind": kind},
            "intro": intro, "early": early and {"section": "Prehistory", "url": "x", "text": early}, "license": people.LICENSE}


CITY = excerpt("city", "city", "Dover", "History of Dover", "The History of Dover begins when Stone Age people crossed a land bridge.",
               "Neolithic flint implements were found in the market square.")
FACTS = {"early_history": [CITY], "traditions": [], "license": people.LICENSE}


def test_people_prompt_has_the_history_rules_and_grounding_rules_first():
    prompt = narrate.build_prompt(FACTS, "people")
    rules, facts = prompt.split("FACTS:")
    flat = " ".join(rules.split())
    assert rules.startswith("STRICT RULES")
    assert "The only facts allowed are in the supplied excerpts" in flat
    assert "peoples, kingdoms, rulers, dates, origins" in flat
    assert 'say "According to Wikipedia"' in flat
    assert "Say which scope each fact belongs to" in flat
    assert 'unless the excerpt\'s scope is "your area"' in flat
    assert "not verified history" in flat
    assert '"no early history recorded for this area."' in flat
    assert '"scope":"city"' in facts and "Neolithic flint" in facts and "https://" not in facts   # scope labels in, URLs out
    assert "Use ONLY the facts in the JSON" in rules                                                # the original grounding rules remain


def test_nothing_found_never_calls_the_model():
    called = []

    async def llm(client, prompt):
        called.append(prompt)
        return "A kingdom of giants.", "online"
    narrate_call = narrate.call_llm
    narrate.call_llm = llm
    try:
        text, mode = run(narrate.narrate(None, {"early_history": [], "traditions": [], "license": people.LICENSE}, "people"))
    finally:
        narrate.call_llm = narrate_call
    assert called == [] and mode == "facts-only"
    assert text.lower().rstrip(".") == "no early history recorded for this area"
    assert "could not be looked up" in narrate.facts_only_text({"early_history": None, "traditions": []}, "people")


def test_grounded_story_is_kept_and_invented_history_is_replaced(monkeypatch):
    def with_reply(reply):
        async def llm(client, prompt):
            return reply, "online"
        monkeypatch.setattr(narrate, "call_llm", llm)
        return run(narrate.narrate(None, FACTS, "people"))
    good = "According to Wikipedia, the history of the city of Dover begins when Stone Age people crossed a land bridge."
    assert with_reply(good) == (good, "online")
    for bad in ("According to Wikipedia, a Roman kingdom ruled Dover.",                  # invents a people and a kingdom
                "According to Wikipedia, the city was founded in 1262 by a king.",       # invents a date and a ruler
                "According to Wikipedia, you are standing right here where it began.",   # exact-spot claim, scope is "city"
                "Stone Age people crossed a land bridge near Dover."):                   # no attribution
        text, mode = with_reply(bad)
        assert mode == "facts-only" and text != bad and text.startswith("According to Wikipedia")


def test_exact_spot_wording_is_allowed_only_for_the_your_area_scope(monkeypatch):
    area = excerpt("your_area", "your area", "State-Langdon", "State-Langdon", "A neighbourhood in Madison.", kind="place")
    facts = {"early_history": [area], "traditions": [], "license": people.LICENSE}
    assert narrate.people_violations("According to Wikipedia, right here is a neighbourhood in Madison.", facts) == []
    assert narrate.people_violations("According to Wikipedia, right here is a neighbourhood in Madison.", FACTS) != []


def test_facts_only_text_attributes_and_names_the_scope():
    text = narrate.people_facts_only_text({"early_history": [CITY, excerpt("country", "country", "United Kingdom", "History of the United Kingdom",
                                                                           "The history of the United Kingdom begins in 1707.")],
                                           "traditions": [{"text": "A giant built it.", "label": people.TRADITION_LABEL,
                                                           "scope": CITY["scope"], "article": CITY["article"]}], "license": people.LICENSE})
    assert text.count("According to Wikipedia") == 2
    assert "the city (Dover), not your exact spot" in text and "the country (United Kingdom), not your exact spot" in text
    assert "tradition, not verified history" in text
    area = narrate.people_facts_only_text({"early_history": [excerpt("your_area", "your area", "X", "X", "X is old.", kind="place")], "traditions": []})
    assert "about your area:" in area and "not your exact spot" not in area


# ---------------------------------------------------------------- the API
@pytest.fixture
def api(monkeypatch):
    main.people_cache, main.story_cache = TTLCache(), TTLCache()
    main.limiter = RateLimiter(1000, 60)
    calls = []

    async def fake_people(client, lat, lng):
        calls.append(1)
        return FACTS, {"nominatim": "ok", "wikipedia": "ok"}

    async def llm(client, prompt):
        raise RuntimeError("no llm")
    monkeypatch.setattr(people, "fetch_people", fake_people)
    monkeypatch.setattr(narrate, "call_llm", llm)
    with TestClient(main.app) as client:
        client.calls = calls
        yield client


def test_api_people_returns_excerpts_sources_and_caches(api):
    d = api.get("/api/people", params={"lat": 51.13, "lng": 1.31}).json()
    assert set(d) == {"people", "sources", "cached"} and d["cached"] is False
    assert d["people"]["early_history"][0]["scope"]["label"] == "city"
    assert {s["id"]: s["status"] for s in d["sources"]} == {"wikipedia": "ok", "nominatim": "ok"}
    assert api.get("/api/people", params={"lat": 51.1301, "lng": 1.3099}).json()["cached"] is True
    assert len(api.calls) == 1


def test_api_people_failure_is_not_an_error_screen(api, monkeypatch):
    async def boom(client, lat, lng):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(people, "fetch_people", boom)
    r = api.get("/api/people", params={"lat": 1, "lng": 2})
    assert r.status_code == 200
    d = r.json()
    assert d["people"]["early_history"] is None and d["sources"][0]["status"].startswith("failed")
    story = api.get("/api/people/story", params={"lat": 1, "lng": 2}).json()
    assert story["mode"] == "facts-only" and "could not be looked up" in story["narration"]


def test_api_story_falls_back_to_attributed_facts_when_the_model_fails(api):
    d = api.get("/api/people/story", params={"lat": 51.13, "lng": 1.31}).json()
    assert d["mode"] == "facts-only" and d["narration"].startswith("According to Wikipedia")


@pytest.mark.parametrize("path", ["/api/people", "/api/people/story"])
@pytest.mark.parametrize("lat,lng", [(91, 0), (0, 181), ("x", 0)])
def test_api_people_validates_coordinates(api, path, lat, lng):
    assert api.get(path, params={"lat": lat, "lng": lng}).status_code == 422


def test_logs_never_contain_coordinates_or_place_names(api, caplog):
    caplog.set_level(logging.INFO)
    api.get("/api/people", params={"lat": 51.1301, "lng": 1.3099})
    api.get("/api/people/story", params={"lat": 51.1301, "lng": 1.3099})
    for secret in ("51.13", "1.3099", "Dover", "Kent", "England", "Stone Age"):
        assert secret not in caplog.text
    assert "people" in caplog.text   # it did log: only counts, statuses and timing
