"""Local animal names from GBIF's vernacular-names endpoint.
Real responses: tests/samples/gbif_vernacular_rabbit.json (many languages), gbif_vernacular_ray.json (few). GBIF has no
Bengali names for any of Dhaka's rare species, so the Bengali cases use a clearly synthetic response in the real shape."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import geo, main, narrate
from app.cache import RateLimiter, TTLCache

SAMPLES = Path(__file__).parent / "samples"


def load(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


RABBIT = load("gbif_vernacular_rabbit.json")["results"]
RAY = load("gbif_vernacular_ray.json")["results"]


def name(text, lang, dataset="Catalogue of Life", preferred=False):
    return {"name": text, "language": lang, "dataset": dataset, "preferred": preferred}


# Synthetic, in the real shape: a Bengali name is not available from GBIF for these species.
SHARK = [
    {"vernacularName": "Scalloped hammerhead", "language": "eng", "source": "Catalogue of Life"},
    {"vernacularName": "Scalloped Hammerhead", "language": "eng", "source": "TAXREF"},               # same name again
    {"vernacularName": "Bronze hammerhead shark", "language": "eng", "source": "EUNIS Biodiversity Database"},
    {"vernacularName": "Hammerhead", "language": "eng", "source": "The IUCN Red List of Threatened Species", "preferred": True},
    {"vernacularName": "হাঙর", "language": "ben", "source": "Example checklist"},
    {"vernacularName": "Tiburón martillo", "language": "spa", "source": "Catalogue of Life"},
    {"vernacularName": "no language given", "language": "", "source": "Catalogue of Life"},
]


# ---------------------------------------------------------------- what GBIF really returns
def test_real_reply_has_the_documented_fields():
    assert {"vernacularName", "language", "source", "taxonKey"} <= set(RABBIT[0])
    langs = {r["language"] for r in RABBIT}
    assert "eng" in langs and "spa" in langs and "" in langs      # ISO 639-3 codes, and some entries with no language


def test_species_with_a_local_name_real_data():
    names = geo.choose_names(geo.compact_names(RABBIT), geo.prefer_langs("en-GB", "GB"))
    assert 1 <= len(names) <= geo.MAX_NAMES
    supplied = {(r["vernacularName"].strip(), r["language"]) for r in RABBIT}
    for n in names:
        assert (n["name"], n["language"]) in supplied               # a name GBIF returned: never one we made up
        assert n["language_name"] == "English" and n["source"] == "GBIF" and n["dataset"]
    assert names[0]["name"] == "Common Rabbit"                       # the best-ranked source's English name


def test_names_without_a_language_are_never_shown():
    assert all(n["language"] for n in geo.compact_names(RABBIT))
    assert [n for n in geo.compact_names(SHARK) if n["name"] == "no language given"] == []


def test_species_with_no_names_shows_only_the_scientific_name():
    assert geo.compact_names([]) == []
    assert geo.choose_names([], ["ben", "eng"]) == []
    only_other = geo.compact_names([{"vernacularName": "Tiburón martillo", "language": "spa", "source": "x"}])
    assert geo.choose_names(only_other, ["ben", "eng"]) == []         # never falls back to a language nobody asked for


# ---------------------------------------------------------------- language preference order
def test_browser_language_first_then_country_then_english():
    names = geo.compact_names(SHARK)
    langs = lambda browser, country: [n["language"] for n in geo.choose_names(names, geo.prefer_langs(browser, country))]
    assert langs("bn-BD", "BD") == ["ben", "eng"]          # Bengali first, then English fills the second slot
    assert langs("en-US", "BD") == ["eng", "eng"]          # English wins; two English names, none from another language
    assert langs("fr-FR", "BD") == ["ben", "eng"]          # no French names: the country's language, then English
    assert langs("es", "BD") == ["spa", "ben"]             # Spanish browser: Spanish, then country (Bengali), English not needed


def test_prefer_langs_and_language_code_handling():
    assert geo.prefer_langs("bn-BD", "BD") == ["ben", "eng"]
    assert geo.prefer_langs("en-GB", "BD") == ["eng", "ben"]
    assert geo.prefer_langs(None, "IN") == ["hin", "eng"]
    assert geo.prefer_langs("zz", "XX") == ["eng"]          # unknown browser language and country: English only
    assert geo.norm_lang("pt-BR") == "por" and geo.norm_lang("ben") == "ben" and geo.norm_lang("") is None and geo.norm_lang(5) is None


def test_best_source_wins_within_a_language_and_names_are_not_repeated():
    out = geo.choose_names(geo.compact_names(SHARK), ["eng"], limit=3)
    assert [n["name"] for n in out] == ["Hammerhead", "Scalloped hammerhead", "Bronze hammerhead shark"]   # preferred, then source rank
    assert out[0]["dataset"] == "The IUCN Red List of Threatened Species"
    assert len({n["name"].lower() for n in out}) == 3


def test_at_most_two_names_per_species():
    many = [name(f"Name {i}", "eng") for i in range(10)]
    assert len(geo.choose_names(many, ["eng"])) == 2


def test_non_latin_names_pass_through_untouched():
    out = geo.choose_names(geo.compact_names(SHARK), ["ben"])
    assert out[0]["name"] == "হাঙর" and out[0]["language_name"] == "Bengali"
    assert "হাঙর" in json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------- the lookup: failure-proof, cached, capped
FACET = {"facets": [{"field": "SCIENTIFIC_NAME", "counts": [{"name": "Sphyrna lewini (Griffith & Smith, 1834)", "count": 48},
                                                            {"name": "Gymnura poecilura (Shaw, 1804)", "count": 25}]}]}


def gbif_net(vernacular, calls):
    """A fake GBIF. `vernacular` maps a species key to (status, json)."""
    records = {"Sphyrna lewini": 2418789, "Gymnura poecilura": 5216217}

    def handler(request):
        p = dict(request.url.params)
        path = request.url.path
        calls.append(path)
        if path.endswith("/vernacularNames"):
            key = int(path.split("/")[-2])
            status, body = vernacular.get(key, (404, {}))
            return httpx.Response(status, json=body)
        if "facet" in p:
            return httpx.Response(200, json=FACET)
        sci = p.get("scientificName", "").split(" (")[0]
        return httpx.Response(200, json={"results": [{"speciesKey": records[sci], "countryCode": "BD", "datasetKey": "k",
                                                      "datasetName": "iNaturalist", "basisOfRecord": "HUMAN_OBSERVATION"}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=geo.UA)


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(geo, "_vernacular_cache", TTLCache())


def run_rare(vernacular, lang="bn-BD"):
    calls = []
    entries, scope = asyncio.run(geo.fetch_rare(gbif_net(vernacular, calls), 23.81, 90.41, lang))
    return {e["species"]: e for e in entries}, calls


def test_names_are_looked_up_with_the_species_key_and_country_from_the_records():
    out, calls = run_rare({2418789: (200, {"results": SHARK, "endOfRecords": True}), 5216217: (200, {"results": RAY, "endOfRecords": True})})
    shark = out["Sphyrna lewini"]
    assert shark["species_key"] == 2418789 and shark["country_code"] == "BD"
    assert [(n["language"], n["name"]) for n in shark["local_names"]] == [("ben", "হাঙর"), ("eng", "Hammerhead")]
    assert out["Gymnura poecilura"]["local_names"][0]["language"] == "eng"        # no Bengali in the real ray reply: English
    assert any(c.endswith("/species/2418789/vernacularNames") for c in calls)


def test_a_failed_name_lookup_never_breaks_the_list():
    out, _ = run_rare({2418789: (500, {}), 5216217: (200, {"results": RAY, "endOfRecords": True})})
    assert set(out) == {"Sphyrna lewini", "Gymnura poecilura"}                     # both species are still listed
    assert out["Sphyrna lewini"]["local_names"] == [] and out["Sphyrna lewini"]["records"] == 48
    assert out["Gymnura poecilura"]["local_names"]                                 # the other lookup still worked


def test_a_species_with_no_names_has_an_empty_list():
    out, _ = run_rare({2418789: (200, {"results": [], "endOfRecords": True}), 5216217: (200, {"results": [], "endOfRecords": True})})
    assert out["Sphyrna lewini"]["local_names"] == [] and out["Sphyrna lewini"]["species"] == "Sphyrna lewini"


def test_lookups_are_cached_by_species_key():
    calls = []
    net = gbif_net({2418789: (200, {"results": SHARK, "endOfRecords": True}), 5216217: (200, {"results": RAY, "endOfRecords": True})}, calls)

    async def twice():
        await geo.fetch_rare(net, 23.81, 90.41, "bn")
        first = len([c for c in calls if c.endswith("/vernacularNames")])
        await geo.fetch_rare(net, 23.81, 90.41, "en")                               # another language: no new name requests
        return first, len([c for c in calls if c.endswith("/vernacularNames")])
    first, second = asyncio.run(twice())
    assert first == 2 and second == 2


def test_at_most_eight_species_are_looked_up():
    entries = [{"species": f"S{i}", "species_key": 1000 + i, "country_code": "GB", "local_names": []} for i in range(12)]
    calls = []
    body = (200, {"results": RABBIT[:5], "endOfRecords": True})
    net = gbif_net({1000 + i: body for i in range(12)}, calls)
    asyncio.run(geo.add_local_names(net, entries, "en"))
    assert len([c for c in calls if c.endswith("/vernacularNames")]) == geo.MAX_NAME_LOOKUPS == 8
    assert all(e["local_names"] == [] for e in entries[8:])


def test_species_records_give_the_key_and_country():
    d = geo.parse_rare_records(load("gbif_dhaka_records.json"))
    assert d["country_code"] == "BD" and isinstance(d["species_key"], int)


# ---------------------------------------------------------------- narration: only supplied names
RARE = {"rock": None, "human_history_nearby": [], "rare_animals_note": geo.RARE_NOTE,
        "rare_names_note": geo.RARE_NAMES_NOTE, "scope": {},
        "rare_animals_recorded_nearby": [{"species": "Sphyrna lewini", "records": 48, "datasets": ["iNaturalist"], "records_checked": 48,
                                          "marked_dead": 5, "marked_alive": 0, "specimen_records": 0,
                                          "local_names": geo.choose_names(geo.compact_names(SHARK), ["ben", "eng"])},
                                         {"species": "Mobula mobular", "records": 16, "datasets": ["iNaturalist"], "records_checked": 16,
                                          "marked_dead": 1, "marked_alive": 1, "specimen_records": 0, "local_names": []}]}


def test_prompt_gives_supplied_names_exactly_and_forbids_inventing_others():
    prompt = narrate.build_prompt(RARE, "main")
    rules, facts = prompt.split("FACTS:")
    flat = " ".join(rules.split())
    assert "only if it appears in that species' \"names\" list, exactly as written there" in flat
    assert "Never translate a name, and never make up or recall a common name from memory" in flat
    assert '"names":["Bengali name: হাঙর","English name: Hammerhead"]' in facts
    assert facts.count('"names"') == 1                      # the species with no names has no "names" key at all
    assert rules.startswith("STRICT RULES")                 # grounding rules first, and "recorded nearby" wording kept
    assert "recorded" in flat.lower() and "not necessarily where it lives" in flat.lower()


def test_story_naming_an_animal_with_a_name_that_was_not_supplied_is_refused(monkeypatch):
    def reply(text):
        async def llm(client, prompt):
            return text, "online"
        monkeypatch.setattr(narrate, "call_llm", llm)
        return asyncio.run(narrate.narrate(None, RARE, "main"))
    good = "Records exist of Sphyrna lewini, called Hammerhead in English, and of Mobula mobular."
    assert reply(good) == (good, "online")
    out, mode = reply("Records exist of Mobula mobular, also known as the Flying Devil, nearby.")
    assert mode == "facts-only" and "Flying Devil" not in out


def test_facts_only_text_shows_a_supplied_name_next_to_the_scientific_name():
    text = narrate.facts_only_text(RARE)
    assert "Sphyrna lewini (Bengali name: হাঙর)" in text and "Mobula mobular" in text
    assert "alive" not in text.lower().replace("not necessarily where it lives", "")


# ---------------------------------------------------------------- the API
@pytest.fixture
def api(monkeypatch):
    main.where_cache, main.fossil_cache = TTLCache(), TTLCache()
    main.limiter = RateLimiter(1000, 60)
    seen = []

    async def rock(c, lat, lng):
        return None

    async def rare(c, lat, lng, lang=None):
        seen.append(lang)
        return [{"species": "Sphyrna lewini", "records": 1, "local_names": [{"name": "x", "language": lang or "-", "language_name": "X", "source": "GBIF", "dataset": ""}]}], geo.rare_scope(None, None)

    async def human(c, lat, lng):
        return [], geo.human_scope("none", geo_note="x"), "empty"

    async def llm(c, prompt):
        raise RuntimeError("no model")
    monkeypatch.setattr(geo, "fetch_rock", rock)
    monkeypatch.setattr(geo, "fetch_rare", rare)
    monkeypatch.setattr(geo, "fetch_human", human)
    monkeypatch.setattr(narrate, "call_llm", llm)
    with TestClient(main.app) as client:
        client.seen = seen
        yield client


def test_api_uses_the_browser_language_and_caches_per_language(api):
    for lang in ("bn-BD", "bn-BD", "en-US", "garbage lang!", ""):
        assert api.get("/api/where", params={"lat": 23.81, "lng": 90.41, "lang": lang}).status_code == 200
    # mode "facts-only" is not cached, so every call reaches the layer; what matters is the language passed down
    assert api.seen == ["ben", "ben", "eng", None, None]


def test_api_response_carries_the_names_note(api):
    d = api.get("/api/where", params={"lat": 23.81, "lng": 90.41, "lang": "bn"}).json()
    assert d["facts"]["rare_names_note"] == "Local names come from GBIF and may be incomplete."
