"""Stela data layers. No LLM code in here: this module only collects facts from open datasets.

Each layer is split in two so it can be tested without the network:
  fetch_*  - talks to the API (async, raises on failure)
  parse_*  - turns the raw JSON into our small fact dict (pure function)

Every layer also reports a SCOPE: how far from the user the facts really come from.
  exact_area    the layer's normal search area around the spot
  wider_area    the normal area was empty, so one wider step was used
  nearest_place facts about the nearest named city/town, not the spot itself
  region        facts about a whole country/state/region, or a continental-scale map
  none          every step of the ladder was tried (each is capped) and nothing was recorded
A wider step only runs when the narrower one found nothing relevant.
"""
import asyncio
import math
import os
import re
import time
from collections import Counter

import httpx

from .cache import TTLCache, cache_key

# Nominatim's usage policy asks for an identifying User-Agent. Set STELA_CONTACT (a URL or email) in production.
_contact = os.environ.get("STELA_CONTACT", "").strip()
UA = {"User-Agent": "stela/0.1 (hacktoberfest open-source project" + (f"; {_contact}" if _contact else "") + ")"}
HUMAN_MA = 0.3  # Homo sapiens, roughly 300,000 years

FAST_TIMEOUT = 6.0    # Macrostrat, GBIF, Wikipedia, Nominatim usually answer in about 1 second
PBDB_TIMEOUT = 20.0   # PBDB takes 3 s normally but up to 30 s+ on sparse queries
PBDB_TOTAL = 45.0     # hard cap across all its attempts

SOURCES = {
    "macrostrat": {"name": "Macrostrat", "url": "https://macrostrat.org", "license": "CC-BY 4.0"},
    "pbdb": {"name": "Paleobiology Database", "url": "https://paleobiodb.org", "license": "CC-BY 4.0"},
    "gbif": {"name": "GBIF / IUCN Red List", "url": "https://www.gbif.org", "license": "CC0 / CC-BY, per dataset"},
    "wikipedia": {"name": "Wikipedia", "url": "https://en.wikipedia.org", "license": "CC-BY-SA 4.0"},
    "nominatim": {"name": "OpenStreetMap Nominatim (nearest place)", "url": "https://nominatim.openstreetmap.org",
                  "license": "ODbL"},
}


async def get_json(client, url, timeout, **params):
    r = await client.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def haversine_km(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def make_scope(level, note, distance_km=None, place_name=None):
    """distance_km: real distance to the nearest thing used. place_name: the named place the facts are about."""
    if distance_km is not None:
        distance_km = round(distance_km, 1) if distance_km < 10 else round(distance_km)
    return {"level": level, "distance_km": distance_km, "place_name": place_name, "note": note}


def _km(x):
    """Distance for a sentence: '0.6', '4.2' or '107'."""
    return f"{round(x, 1):g}" if x < 10 else f"{round(x)}"


# ---------- Layer 1: rock under your feet (Macrostrat) ----------
ROCK_FIELDS = ("name", "strat_name", "lith", "descrip", "b_age", "t_age", "b_int_name", "t_int_name")

# The ladder here is the map scale, not a distance: a coarser map still covers your spot, but says less about it.
ROCK_SCOPES = {
    "large": ("exact_area", "From a detailed local geological map."),
    "medium": ("wider_area", "Only a regional-scale map covers this spot, so the rock name is a broader grouping and less precise."),
    "small": ("region", "Only a continental-scale map covers this spot, so this is a broad guess about the rock."),
}


def rock_scope(unit):
    if not unit:
        return make_scope("none", "Nothing recorded: no rock map covers this spot (it may be open water or unmapped).")
    level, note = ROCK_SCOPES.get(unit.get("map_scale"), ("wider_area", "The detail of the rock map here is not known."))
    return make_scope(level, note)


def parse_rock(data, scale):
    rows = ((data or {}).get("success") or {}).get("data") or []
    if not rows:
        return None
    unit = {k: rows[0].get(k) for k in ROCK_FIELDS}
    unit["map_scale"] = scale
    return unit


async def fetch_rock(client, lat, lng):
    # Ask for the most detailed map first, then fall back to coarser ones (3 steps, then give up).
    for scale in ("large", "medium", "small"):
        data = await get_json(client, "https://macrostrat.org/api/v2/geologic_units/map", FAST_TIMEOUT,
                              lat=lat, lng=lng, scale=scale, response="long")
        unit = parse_rock(data, scale)
        if unit:
            return unit
    return None


# Rough hint only: a keyword guess from the rock type, always flagged as an inference.
SETTINGS = [
    (("limestone", "dolostone", "dolomite", "chalk", "marl", "shale", "chert", "mudstone", "reef"),
     "marine", "a sea or ocean floor"),
    (("coal", "peat", "lignite"), "swamp", "a lush swamp or coastal forest"),
    (("evaporite", "gypsum", "halite", "salt"), "arid", "dry salt flats or a shallow evaporating sea"),
    (("sandstone", "conglomerate", "alluvium", "gravel", "sand", "silt", "clay", "loess"),
     "land/river", "rivers, floodplains, deltas or beaches"),
    (("basalt", "granite", "gneiss", "schist", "volcanic", "andesite", "rhyolite", "tuff"),
     "igneous/metamorphic", "volcanoes or deep crust, not a habitat of its own"),
]


def setting(unit):
    text = " ".join(str(unit.get(k) or "") for k in ("lith", "name", "descrip")).lower()
    for words, kind, desc in SETTINGS:
        if any(w in text for w in words):
            return {"type": kind, "meaning": desc, "confidence": "inferred from rock type, so only 'probably'. Sandstone and conglomerate can also form in shallow seas."}
    return {"type": "unknown", "meaning": None, "confidence": "rock type not recognised"}


def time_ruler(unit):
    ma = unit.get("b_age")
    if ma is None:
        return None
    return {"rock_start_Ma": ma,
            "rock_end_Ma": unit.get("t_age"),
            "times_older_than_humans": round(ma / HUMAN_MA),
            "note": "Homo sapiens is about 0.3 million years old"}


# ---------- Shared: nearest named place (OpenStreetMap Nominatim) ----------
# Used by the human-history and fossil ladders. Policy: identifying User-Agent, at most 1 request/second,
# and cache results. Real response fields (checked): category, type, addresstype, name, display_name, lat, lon,
# address{country, country_code, state, ...}, extratags{wikipedia: "en:Dhaka", wikidata, population, ...}.
# Open ocean returns {"error": "Unable to geocode"}.
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_GAP = 1.1  # seconds between requests (policy: max 1 per second)
PLACE_KINDS = {"city", "town", "village", "hamlet", "suburb", "municipality", "borough", "city_district",
               "quarter", "neighbourhood", "locality", "island"}
_place_cache = TTLCache(ttl_seconds=24 * 3600)  # rounded coordinates -> {"place": ...}
_geo_state = {"loop": None, "lock": None, "last": 0.0}


def _geo_lock():
    loop = asyncio.get_running_loop()
    if _geo_state["loop"] is not loop:  # a lock belongs to one event loop (matters in tests)
        _geo_state.update(loop=loop, lock=asyncio.Lock())
    return _geo_state["lock"]


def parse_place(data):
    """Nominatim reverse result -> our small place dict, or None when nothing was found."""
    if not data or data.get("error") or not data.get("name"):
        return None
    addr, extra = data.get("address") or {}, data.get("extratags") or {}
    wiki = extra.get("wikipedia") or ""
    lang, _, title = wiki.partition(":")
    kind = data.get("addresstype") or data.get("type") or "place"
    return {
        "name": data["name"],
        "kind": kind,
        "is_place": kind in PLACE_KINDS,  # a city/town/village, as opposed to a state or country
        "lat": float(data["lat"]), "lng": float(data["lon"]),
        "country": addr.get("country"),
        "country_code": (addr.get("country_code") or "").upper() or None,
        "wiki_title": title if lang == "en" and title else data["name"],
    }


async def reverse_geocode(client, lat, lng):
    """Nearest named place for a spot, or None. Cached, and spaced to 1 request/second. Raises on failure."""
    key = cache_key(lat, lng)
    hit = _place_cache.get(key)
    if hit:
        return hit["place"]
    async with _geo_lock():
        hit = _place_cache.get(key)  # someone may have fetched it while we waited for the lock
        if hit:
            return hit["place"]
        wait = _geo_state["last"] + NOMINATIM_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            data = await get_json(client, NOMINATIM, FAST_TIMEOUT, lat=lat, lon=lng, format="jsonv2", zoom=10,
                                  addressdetails=1, extratags=1, **{"accept-language": "en"})
        finally:
            _geo_state["last"] = time.monotonic()
        place = parse_place(data)
        _place_cache.set(key, {"place": place})
        return place


# ---------- Layer 2: ancient animals (Paleobiology Database) ----------
PBDB = "https://paleobiodb.org/data1.2/occs/list.json"
# The ladder. `cc` was verified in the PBDB docs: ISO 3166-1 alpha-2 country codes, comma-separated.
FOSSIL_STEPS = [
    {"level": "exact_area", "box": 0.5, "age": True},
    {"level": "wider_area", "box": 2.0, "age": True},
    {"level": "wider_area", "box": 2.0, "age": False},   # same area, age NOT matched to the rock
    {"level": "region", "country": True, "age": True},   # same-age finds anywhere in the country
]
FOSSIL_WIDEST_KM = int(2.0 * 111)


def parse_fossils(data, lat, lng):
    """Summarise PBDB records. nearest_km is the real distance to the closest find (None if no coordinates)."""
    recs = (data or {}).get("records") or []
    if not recs:
        return None
    classes = Counter(r["cll"] for r in recs if r.get("cll") and r["cll"] != "NO_CLASS_SPECIFIED")
    taxa = Counter(r["tna"] for r in recs if r.get("tna"))
    dists = [haversine_km(lat, lng, float(r["lat"]), float(r["lng"])) for r in recs if r.get("lat") and r.get("lng")]
    return {"count": len(recs), "classes": classes.most_common(6), "taxa": taxa.most_common(10),
            "nearest_km": min(dists) if dists else None}


def fossil_scope(step, found, country=None, incomplete=False):
    if found is None:
        tail = " Some searches timed out, so this is not conclusive." if incomplete else ""
        where = f" within about {FOSSIL_WIDEST_KM} km" + (f" or elsewhere in {country}" if country else "")
        return make_scope("none", f"Nothing recorded: no fossils of this rock's age were found{where}.{tail}")
    near = found["nearest_km"]
    nearest = f" Nearest finds about {_km(near)} km away." if near is not None else ""
    if step.get("country"):
        return make_scope("region", f"Fossils of the same age as this rock from anywhere in {country}, not from near you.", None, country)
    radius = int(step["box"] * 111)
    if step["level"] == "exact_area":
        return make_scope("exact_area", f"Fossils of the same age as this rock, found within about {radius} km.{nearest}", near)
    if step["age"]:
        return make_scope("wider_area", f"Nothing of this age within about 55 km, so the search widened to about {radius} km around you.{nearest}", near)
    return make_scope("wider_area", f"Nothing of this rock's age within about {radius} km. These finds are NOT matched to the rock's age.{nearest}", near)


async def fetch_fossils(client, lat, lng, unit):
    """Returns (found_or_None, scope). Raises only if every attempt failed."""
    hi, lo = unit.get("b_age"), unit.get("t_age")
    last_error, any_ok, country, geocoded = None, False, None, False
    for step in FOSSIL_STEPS:
        if step["age"] and (hi is None or lo is None):
            continue
        p = dict(show="class,coords", limit=200)
        if step.get("country"):
            try:  # the region step needs the country code, from the shared (cached, rate-limited) geocoder
                place = await reverse_geocode(client, lat, lng)
            except Exception as e:
                last_error = e
                continue
            geocoded = True
            if not place or not place["country_code"]:
                continue  # open ocean or unknown: no country to search
            country = place["country"]
            p["cc"] = place["country_code"]
        else:
            b = step["box"]
            p.update(lngmin=lng - b, lngmax=lng + b, latmin=lat - b, latmax=lat + b)
        if step["age"]:
            # timerule=overlap: the default ("major") returned nothing for thin units
            p.update(max_ma=hi, min_ma=lo, timerule="overlap")
        try:
            data = await get_json(client, PBDB, PBDB_TIMEOUT, **p)
        except Exception as e:  # one slow attempt should not hide the others
            last_error = e
            continue
        any_ok = True
        found = parse_fossils(data, lat, lng)
        if found:
            return found, fossil_scope(step, found, country)
    if not any_ok and last_error:
        raise last_error
    return None, fossil_scope(None, None, country, incomplete=last_error is not None)


# ---------- Layer 3: rare animals RECORDED nearby (GBIF + IUCN) ----------
# GBIF holds observations, museum specimens, market finds and more. A record says where an animal
# was recorded, NOT that it lives there, so nothing in here (or in the narration) may say "alive".
RARE_NOTE = ("Records show where an animal was observed, not necessarily where it lives. "
             "They can include dead animals, market finds and misplaced locations.")
GBIF = "https://api.gbif.org/v1"
DETAIL_TIMEOUT = 5.0
SPECIMEN_BASES = {"PRESERVED_SPECIMEN", "MATERIAL_SAMPLE", "MATERIAL_CITATION", "LIVING_SPECIMEN", "FOSSIL_SPECIMEN"}
# The ladder: about 30 km first; if that holds nothing, ONE wider step with tighter caps (fewer species, fewer
# records inspected). detail_limit = records inspected per species (the exact count comes from the facet).
RARE_STEPS = [
    {"level": "exact_area", "km": 30, "species": 8, "detail_limit": 50},
    {"level": "wider_area", "km": 90, "species": 5, "detail_limit": 25},
]
_dataset_titles = {}  # dataset key -> title (public dataset metadata, not user data)


def rare_scope(step, found):
    if found is None:  # every step was empty
        return make_scope("none", f"Nothing recorded: no records of rare animals within about {RARE_STEPS[-1]['km']} km.")
    km = step["km"]
    if step["level"] == "exact_area":
        return make_scope("exact_area", f"Records within about {km} km of you.", km)
    return make_scope("wider_area", f"Nothing recorded within {RARE_STEPS[0]['km']} km, so the search widened to about {km} km. "
                                    "These records are NOT from your immediate area.", km)


def parse_rare(data):
    """From the facet response: [(full scientific name, record count)], most-recorded first."""
    facets = (data or {}).get("facets") or []
    counts = facets[0]["counts"] if facets else []
    return [(c["name"], c["count"]) for c in counts]


def parse_rare_records(records):
    """Summarise what the raw GBIF records themselves say. Only explicit fields are used, nothing is guessed:
    `vitality` (alive/dead, when the publisher filled it in) and `basisOfRecord` (specimen vs observation).
    Free-text remarks are not interpreted."""
    datasets, names = {}, {}
    vitality, specimens = {"dead": 0, "alive": 0}, 0
    for r in records:
        key = r.get("datasetKey")
        datasets[key] = datasets.get(key, 0) + 1
        if r.get("datasetName"):
            names[key] = r["datasetName"]
        v = str(r.get("vitality") or "").lower()
        if v in vitality:
            vitality[v] += 1
        if r.get("basisOfRecord") in SPECIMEN_BASES:
            specimens += 1
    top = sorted(datasets.items(), key=lambda kv: -kv[1])[:3]
    return {"records_checked": len(records),
            "datasets": [{"key": k, "name": names.get(k), "records": n} for k, n in top],
            "marked_dead": vitality["dead"], "marked_alive": vitality["alive"], "specimen_records": specimens}


def build_rare_entry(full_name, count, details):
    entry = {"species": " ".join(full_name.split()[:2]),  # "Rissa tridactyla (Linnaeus, 1758)" -> "Rissa tridactyla"
             "records": count}
    if details:
        entry.update(records_checked=details["records_checked"],
                     datasets=[d["name"] or "Unnamed dataset" for d in details["datasets"]],
                     marked_dead=details["marked_dead"], marked_alive=details["marked_alive"],
                     specimen_records=details["specimen_records"])
    else:  # the detail call failed: say we don't know, don't pretend zero
        entry.update(records_checked=None, datasets=None, marked_dead=None, marked_alive=None, specimen_records=None)
    return entry


async def dataset_title(client, key):
    if key not in _dataset_titles:
        data = await get_json(client, f"{GBIF}/dataset/{key}", DETAIL_TIMEOUT)
        _dataset_titles[key] = data.get("title")
    return _dataset_titles[key]


async def fetch_rare_details(client, area, full_name, limit):
    data = await get_json(client, f"{GBIF}/occurrence/search", DETAIL_TIMEOUT, **area,
                          scientificName=full_name, limit=limit)
    details = parse_rare_records(data.get("results") or [])
    for d in details["datasets"]:  # some records carry only the dataset key, so look the name up
        if not d["name"] and d["key"]:
            d["name"] = (await safe(dataset_title(client, d["key"])))[0]
    return details


def rare_area(lat, lng, km):
    """A box around the spot that is `km` in every direction (longitude degrees shrink away from the equator)."""
    dlat = km / 111.0
    dlng = min(180.0, km / (111.0 * max(math.cos(math.radians(lat)), 0.05)))
    return dict(decimalLatitude=f"{max(-90, lat - dlat):.4f},{min(90, lat + dlat):.4f}",
                decimalLongitude=f"{max(-180, lng - dlng):.4f},{min(180, lng + dlng):.4f}",
                iucnRedListCategory=["CR", "EN", "VU"], kingdomKey=1)


async def fetch_rare(client, lat, lng):
    """Returns (entries, scope). A wider step runs only if the narrower one found no species."""
    for step in RARE_STEPS:
        area = rare_area(lat, lng, step["km"])
        # facet=scientificName counts species across ALL matches. The first 100 raw
        # records were dominated by whichever species happened to be listed first.
        data = await get_json(client, f"{GBIF}/occurrence/search", FAST_TIMEOUT, **area, limit=0,
                              facet="scientificName", facetLimit=step["species"])
        species = parse_rare(data)
        if not species:
            continue
        # one detail call per species, in parallel; a failed one only loses that species' extra detail
        results = await asyncio.gather(*(safe(fetch_rare_details(client, area, name, step["detail_limit"]))
                                         for name, _ in species))
        entries = [build_rare_entry(name, n, details) for (name, n), (details, _) in zip(species, results)]
        return entries, rare_scope(step, entries)
    return [], rare_scope(None, None)


# ---------- Layer 4: human history (Wikipedia geosearch, then the nearest place) ----------
# Whole-word match: a plain "roman" also matched "Roman Catholic" schools.
HISTORY_WORDS = re.compile(
    r"(?<!\w)(ancient|archaeolog\w*|ruins?|prehistoric|neolithic|bronze age|iron age|"
    r"roman (?:villa|fort|road|temple|britain|empire|period)|settlement|hillfort|temple|"
    r"civili[sz]ation|burial|megalith\w*|stone age|paleolithic|medieval|castle|"
    r"aboriginal|indigenous|native american|effigy|mounds?|petroglyphs?|rock art|sacred)(?!\w)")
WIKI = "https://en.wikipedia.org/w/api.php"
HUMAN_RADIUS_KM = 10


def parse_human(pages, extracts):
    out = []
    for p in pages:
        text = (extracts.get(str(p["pageid"])) or {}).get("extract", "")
        if HISTORY_WORDS.search((p["title"] + " " + text).lower()):
            out.append({"place": p["title"], "distance_m": round(p["dist"]), "summary": text})
    return out[:5]


def parse_intro(data):
    """Wikipedia extract response (titles=...) -> intro text, or None if the page does not exist."""
    for page in ((data or {}).get("query") or {}).get("pages", {}).values():
        if not page.get("missing") and page.get("extract"):
            return page["extract"].strip()
    return None


def human_scope(kind, place=None, dist_km=None, geo_note=None):
    r = HUMAN_RADIUS_KM
    if kind == "exact_area":
        return make_scope("exact_area", f"History pages within about {r} km of you.")
    if kind == "nearest_place":
        return make_scope("nearest_place", f"About {place['name']}, the nearest {place['kind'].replace('_', ' ')} "
                          f"(about {_km(dist_km)} km away), not your exact spot. Nothing relevant is recorded within about {r} km.",
                          dist_km, place["name"])
    if kind == "region":
        return make_scope("region", f"About {place['name']}, the wider region, not your exact spot. "
                          f"Nothing relevant is recorded within about {r} km.", None, place["name"])
    return make_scope("none", f"Nothing recorded within about {r} km. {geo_note}")


async def fetch_human(client, lat, lng):
    """Returns (entries, scope, geocoder_status). Raises if the Wikipedia search itself fails."""
    g = await get_json(client, WIKI, FAST_TIMEOUT, action="query", list="geosearch",
                       gscoord=f"{lat}|{lng}", gsradius=HUMAN_RADIUS_KM * 1000, gslimit=50, format="json")
    pages = ((g or {}).get("query") or {}).get("geosearch") or []
    extracts = {}
    for i in range(0, len(pages), 20):  # the extracts API returns at most 20 pages per call
        ids = "|".join(str(p["pageid"]) for p in pages[i:i + 20])
        e = await get_json(client, WIKI, FAST_TIMEOUT, action="query", prop="extracts", exintro=1,
                           explaintext=1, exsentences=2, exlimit=20, pageids=ids, format="json")
        extracts.update(((e or {}).get("query") or {}).get("pages") or {})
    entries = parse_human(pages, extracts)
    if entries:
        return entries, human_scope("exact_area"), None

    # Nothing relevant nearby: fall back to the nearest named place and its Wikipedia introduction.
    place, geo_status = await safe(reverse_geocode(client, lat, lng))
    if geo_status != "ok":
        return [], human_scope("none", geo_note="The nearest-place lookup could not be reached."), geo_status
    if not place:
        return [], human_scope("none", geo_note="No named place was found nearby (this may be open water)."), "empty"
    intro, intro_status = await safe(fetch_intro(client, place["wiki_title"]))
    if not intro:
        note = ("The nearest-place Wikipedia page could not be reached." if intro_status != "ok"
                else f"{place['name']} has no Wikipedia introduction to use.")
        return [], human_scope("none", geo_note=note), "ok" if intro_status == "ok" else intro_status
    dist = haversine_km(lat, lng, place["lat"], place["lng"])
    if place["is_place"]:
        scope, distance_m = human_scope("nearest_place", place, dist), round(dist * 1000)
    else:  # a state or country: its centre says nothing useful about distance
        scope, distance_m = human_scope("region", place), None
    return [{"place": place["name"], "distance_m": distance_m, "summary": intro}], scope, "ok"


async def fetch_intro(client, title):
    data = await get_json(client, WIKI, FAST_TIMEOUT, action="query", prop="extracts", exintro=1, explaintext=1,
                          exsentences=3, titles=title, redirects=1, format="json")
    return parse_intro(data)


# ---------- Putting the layers together ----------
async def safe(coro):
    """Run one layer. Returns (value, status). A failure gives (None, 'failed: ErrorType'), never an exception."""
    try:
        return await coro, "ok"
    except Exception as e:
        return None, f"failed: {type(e).__name__}"


def source_status(value, status):
    if status != "ok":
        return status
    return "empty" if value in (None, [], {}) else "ok"


async def fast_facts(client, lat, lng):
    """Rock + rare animals + human history, in parallel. Fossils come later (they are slow)."""
    (unit, s_rock), (rare_res, s_rare), (human_res, s_human) = await asyncio.gather(
        safe(fetch_rock(client, lat, lng)),
        safe(fetch_rare(client, lat, lng)),
        safe(fetch_human(client, lat, lng)),
    )
    rare, rare_scope_ = rare_res if rare_res else (None, None)
    human, human_scope_, geo_status = human_res if human_res else (None, None, None)
    facts = {
        "rock": unit,
        "ancient_setting": setting(unit) if unit else None,
        "time_ruler": time_ruler(unit) if unit else None,
        "fossils_found_nearby": "pending",  # filled in by /api/fossils
        "rare_animals_recorded_nearby": rare,
        "rare_animals_note": RARE_NOTE,
        "human_history_nearby": human,
        # how far from the user each layer's facts really come from (None = that source failed)
        "scope": {"rock": rock_scope(unit) if s_rock == "ok" else None,
                  "rare": rare_scope_, "human": human_scope_},
    }
    statuses = {"macrostrat": source_status(unit, s_rock),
                "gbif": source_status(rare, s_rare),
                "wikipedia": source_status(human, s_human),
                "nominatim": geo_status or "not needed"}
    return facts, statuses


async def slow_fossils(client, lat, lng, unit):
    """PBDB fossils, capped at PBDB_TOTAL seconds. Returns (value, scope, status)."""
    if not unit:
        return None, make_scope("none", "Nothing recorded: there is no rock unit to match fossils to."), "empty"
    res, status = await safe(asyncio.wait_for(fetch_fossils(client, lat, lng, unit), PBDB_TOTAL))
    if status != "ok":
        return None, None, status
    found, scope = res
    return found, scope, source_status(found, "ok")
