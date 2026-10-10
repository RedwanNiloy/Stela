"""Species thumbnails: the species' Wikipedia page image, with licence and author from Wikimedia Commons.
Real recorded responses (tests/samples/images_*.json) for the Dover, Dhaka and Madison species; the edge cases
(missing licence, non-commercial licence, failed lookup) are small synthetic variations in the real shape."""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app import geo

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch):
    for name in ("_image_cache", "_commons_cache", "_fossil_image_cache"):
        monkeypatch.setattr(geo, name, geo.TTLCache())
    monkeypatch.setattr(geo, "_phylopic_build", {"n": None, "until": 0.0})


def sample(place):
    return json.loads((SAMPLES / f"images_{place}.json").read_text(encoding="utf-8"))


def net(rec, fail=None, calls=None):
    """A fake network that answers like Wikipedia then Commons; `fail` = "wikipedia" or "commons" makes that call a 503."""
    def handler(request):
        kind = "commons" if "commons.wikimedia.org" in request.url.host else "wikipedia"
        if calls is not None:
            calls.append(kind)
        if fail == kind:
            return httpx.Response(503, json={})
        return httpx.Response(200, json=rec[kind])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def lookup(rec, **kw):
    async def go():
        async with net(rec, **kw) as c:
            return await geo.lookup_images(c, rec["species"])
    return run(go())


def test_dover_species_get_a_photo_with_author_licence_and_file_page():
    got = lookup(sample("dover"))
    kittiwake = got["Rissa tridactyla"]          # the scientific name redirects to the common-name article
    assert kittiwake["author"] == "Yathin S Krishnappa" and kittiwake["license"] == "CC BY-SA 3.0"
    assert kittiwake["file_page"].startswith("https://commons.wikimedia.org/wiki/File:Rissa_tridactyla")
    assert kittiwake["url"].startswith("https://thumb.wikimedia.org/") and kittiwake["width"] == 240
    assert kittiwake["license_url"].startswith("https://")
    assert sum(1 for v in got.values() if v) == 8


def test_dhaka_species_without_a_wikipedia_image_get_no_photo():
    got = lookup(sample("dhaka"))
    assert got["Glaucostegus younholeei"] is None           # article exists, no image
    assert got["Sphyrna lewini"]["license"] == "CC BY 2.0"
    assert sum(1 for v in got.values() if v) == 7


def test_madison_public_domain_and_unnamed_author_handling():
    got = lookup(sample("madison"))
    assert got["Chaetura pelagica"]["license"] == "CC0"
    # Commons says "No machine-readable author provided. Mdf assumed ...": the credit keeps the part that names someone
    assert got["Euphagus carolinus"]["author"].startswith("Mdf assumed")


def test_a_photo_without_a_licence_is_not_shown():
    rec = copy.deepcopy(sample("dover"))
    for p in rec["commons"]["query"]["pages"].values():
        p["imageinfo"][0]["extmetadata"].pop("LicenseShortName", None)
    assert all(v is None for v in lookup(rec).values())


def test_a_photo_without_an_author_is_not_shown():
    rec = copy.deepcopy(sample("dover"))
    for p in rec["commons"]["query"]["pages"].values():
        p["imageinfo"][0]["extmetadata"]["Artist"] = {"value": "<a href='x'></a>"}
    assert all(v is None for v in lookup(rec).values())


def test_non_commercial_and_unknown_licences_are_not_shown():
    page = {"thumbnail": {"source": "https://thumb.wikimedia.org/x.jpg", "width": 240, "height": 160}}
    for lic in ["CC BY-NC 4.0", "CC BY-ND 2.0", "CC BY-NC-SA 3.0", "Fair use", "All rights reserved", "Copyrighted free use"]:
        info = {"imageinfo": [{"descriptionurl": "https://commons.wikimedia.org/wiki/File:X.jpg",
                               "extmetadata": {"LicenseShortName": {"value": lic}, "Artist": {"value": "A. Person"}}}]}
        assert geo.image_from_pages(page, info) is None, lic
    for lic in ["CC BY 4.0", "CC BY-SA 3.0", "CC0", "Public domain", "GFDL 1.2"]:
        info = {"imageinfo": [{"descriptionurl": "https://commons.wikimedia.org/wiki/File:X.jpg",
                               "extmetadata": {"LicenseShortName": {"value": lic}, "Artist": {"value": "A. Person"}}}]}
        assert geo.image_from_pages(page, info)["license"] == lic, lic


def test_an_image_from_an_unexpected_host_is_not_used():
    page = {"thumbnail": {"source": "https://tracker.example/x.jpg", "width": 240, "height": 160}}
    info = {"imageinfo": [{"descriptionurl": "https://commons.wikimedia.org/wiki/File:X.jpg",
                           "extmetadata": {"LicenseShortName": {"value": "CC0"}, "Artist": {"value": "A"}}}]}
    assert geo.image_from_pages(page, info) is None


def entries(*names):
    return [{"species": n, "records": 1, "local_names": []} for n in names]


def test_a_failed_lookup_leaves_the_list_intact_and_is_not_cached():
    geo._image_cache = geo.TTLCache()
    rec = sample("dover")
    es = entries("Rissa tridactyla", "Aythya ferina")
    for fail in ("wikipedia", "commons"):
        async def go():
            async with net(rec, fail=fail) as c:
                await geo.add_images(c, es)
        run(go())
        assert [e["image"] for e in es] == [None, None]
    calls = []
    async def ok():
        async with net(rec, calls=calls) as c:
            await geo.add_images(c, es)
    run(ok())
    assert all(e["image"] for e in es) and calls == ["wikipedia", "commons"]   # a retry works, so nothing bad was cached


def test_lookups_are_cached_by_species_and_use_two_calls():
    geo._image_cache = geo.TTLCache()
    rec, calls = sample("dover"), []
    names = rec["species"]
    async def go():
        async with net(rec, calls=calls) as c:
            first = entries(*names)
            await geo.add_images(c, first)
            second = entries(*names)
            await geo.add_images(c, second)
            return first, second
    first, second = run(go())
    assert calls == ["wikipedia", "commons"]
    assert [e["image"] for e in first] == [e["image"] for e in second] and all(e["image"] for e in second)


def test_at_most_eight_species_are_looked_up_for_photos():
    geo._image_cache = geo.TTLCache()
    seen = []
    def handler(request):
        seen.append(request.url.params.get("titles", ""))
        return httpx.Response(200, json={"query": {}})
    es = entries(*[f"Genus sp{i}" for i in range(12)])
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await geo.add_images(c, es)
    run(go())
    assert len(seen[0].split("|")) == 8


def test_the_llm_never_sees_the_image_fields():
    from app import narrate
    facts = {"rock": None, "human_history_nearby": [], "scope": {}, "rare_images_note": geo.IMAGE_NOTE,
             "rare_animals_recorded_nearby": [{"species": "Rissa tridactyla", "records": 3, "local_names": [],
                                               "image": {"url": "https://thumb.wikimedia.org/x.jpg", "author": "A. Person",
                                                         "license": "CC0", "file_page": "https://commons.wikimedia.org/wiki/File:X.jpg"}}]}
    prompt = narrate.build_prompt(facts, "main")
    for word in ("thumb.wikimedia", "A. Person", "commons.wikimedia", "Photos show", "photo"):
        assert word not in prompt.split("FACTS:")[1], word
    assert "photo" not in narrate.facts_only_text(facts).lower()


def test_one_429_is_retried_once():
    geo._image_cache = geo.TTLCache()
    rec, hits = sample("dover"), {"n": 0}
    def handler(request):
        if "wikipedia" in request.url.host:
            hits["n"] += 1
            if hits["n"] == 1:
                return httpx.Response(429, json={})
            return httpx.Response(200, json=rec["wikipedia"])
        return httpx.Response(200, json=rec["commons"])
    es = entries(*rec["species"])
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await geo.add_images(c, es)
    run(go())
    assert hits["n"] == 2 and all(e["image"] for e in es)


# ---------------------------------------------------------------- fossil group illustrations (PhyloPic, then Wikipedia)
PHYLO = json.loads((SAMPLES / "phylopic.json").read_text(encoding="utf-8"))


def phylopic_net(fail=False, calls=None):
    """Answers like api.phylopic.org from the recorded replies; every other host (Wikipedia, Commons) answers like the Dover
    sample, with `Gadus morhua` standing in for a fallback hit."""
    def handler(request):
        host, path = request.url.host, request.url.path
        if calls is not None:
            calls.append(host + path)
        if host == "api.phylopic.org":
            if fail:
                return httpx.Response(503, json={})
            if path == "/":
                return httpx.Response(200, json=PHYLO["root"])
            if path == "/nodes":
                return httpx.Response(200, json=PHYLO["nodes"][request.url.params["filter_name"]])
            return httpx.Response(200, json=PHYLO["node"][path])
        rec = sample("dover")
        if "wikipedia" in host:
            titles = request.url.params["titles"].split("|")
            body = copy.deepcopy(rec["wikipedia"])
            body["query"]["redirects"] = [{"from": t, "to": "Atlantic cod"} for t in titles]
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=rec["commons"])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def fossils(names, **kw):
    async def go():
        async with phylopic_net(**kw) as c:
            return (await geo.fossil_images(c, names))[0]
    return run(go())


def test_phylopic_licensed_image_names_the_member_drawn_and_credits_the_author():
    got = fossils(["Cephalopoda"])["Cephalopoda"]
    assert got["source"] == "PhyloPic" and got["kind"] == "illustration"
    assert got["subject"] == "Cassinoceras" and got["author"] == "Antonov" and got["license"] == "Public domain"
    assert got["url"].startswith("https://images.phylopic.org/") and got["url"].endswith("128x128.png")
    assert got["file_page"].startswith("https://www.phylopic.org/images/")


def test_phylopic_non_commercial_licence_is_not_shown_and_wikipedia_is_tried():
    got = fossils(["Gastropoda"])["Gastropoda"]             # PhyloPic's picture is CC BY-NC-SA 3.0
    assert got is None or got["source"] == "Wikimedia Commons"
    assert geo.phylopic_license("https://creativecommons.org/licenses/by-nc-sa/3.0/") is None
    assert geo.phylopic_license("https://creativecommons.org/licenses/by/4.0/")[0] == "CC BY 4.0"
    assert geo.phylopic_license("https://creativecommons.org/publicdomain/zero/1.0/")[0] == "CC0 1.0"


def test_phylopic_picture_without_an_author_is_not_shown():
    im = PHYLO["node"][next(h for h in PHYLO["node"] if "primaryImage" in (PHYLO["node"][h]["_embedded"] or {}) and
                            not PHYLO["node"][h]["_embedded"]["primaryImage"].get("attribution"))]["_embedded"]["primaryImage"]
    assert geo.phylopic_from_image(im) is None


def test_no_phylopic_node_falls_back_to_the_wikipedia_image_with_its_credit():
    got = fossils(["Schloenbachia"])["Schloenbachia"]
    assert got and got["source"] == "Wikimedia Commons" and got["kind"] == "photo" and got["author"]


def test_a_failed_phylopic_lookup_gives_placeholders_not_errors_and_is_not_cached():
    names = ["Cephalopoda", "Bivalvia"]
    async def go():
        async with phylopic_net(fail=True) as c:
            return await geo.fossil_images(c, names)
    got, complete = run(go())
    assert complete is False
    assert set(got) == set(names) and all(v is None or v["source"] == "Wikimedia Commons" for v in got.values())
    assert geo._fossil_image_cache.get("Cephalopoda") is None   # a retry later can still find it
    assert fossils(names)["Cephalopoda"]["source"] == "PhyloPic"


def test_at_most_six_groups_are_looked_up_and_results_are_cached():
    calls = []
    async def go():
        async with phylopic_net(calls=calls) as c:
            return (await geo.fossil_images(c, ["Cephalopoda"] + [f"Group{i}" for i in range(8)]))[0]
    got = run(go())
    assert len(got) == geo.MAX_FOSSIL_IMAGES == 6 and got["Cephalopoda"]
    calls.clear()
    async def again():
        async with phylopic_net(calls=calls) as c:
            return await geo.fossil_images(c, ["Cephalopoda"])
    assert run(again())[1] is True
    assert calls == []                                       # served from the cache


def test_the_llm_never_sees_fossil_or_history_images():
    from app import narrate
    img = {"url": "https://images.phylopic.org/x.png", "author": "A. Person", "license": "CC0 1.0", "file_page": "https://www.phylopic.org/images/x"}
    facts = {"rock": None, "human_history_nearby": [], "scope": {}, "fossils_found_nearby": {"count": 3, "classes": [["Cephalopoda", 3]], "taxa": []},
             "early_history": [{"scope": {"label": "city", "place_name": "Dover", "level": "city"}, "article": {"title": "History of Dover"},
                                "intro": "Dover is a port.", "early": None, "image": img}], "traditions": []}
    for part in ("fossils", "people"):
        prompt = narrate.build_prompt(facts, part).split("FACTS:")[1]
        assert "phylopic" not in prompt.lower() and "A. Person" not in prompt and "image" not in prompt.lower()
