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
from html import unescape

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
    "commons": {"name": "Wikimedia Commons (species photos)", "url": "https://commons.wikimedia.org",
                "license": "per photo, credited beside each one"},
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


async def _nominatim_cached(client, cache, lat, lng, parse, **params):
    """One throttled, cached Nominatim reverse lookup. `parse` turns the raw reply into what the caller wants.
    All callers share one lock and one clock, so the whole app stays under 1 request/second. Raises on failure."""
    key = cache_key(lat, lng)
    hit = cache.get(key)
    if hit:
        return hit["value"]
    async with _geo_lock():
        hit = cache.get(key)  # someone may have fetched it while we waited for the lock
        if hit:
            return hit["value"]
        wait = _geo_state["last"] + NOMINATIM_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            data = await get_json(client, NOMINATIM, FAST_TIMEOUT, lat=lat, lon=lng, format="jsonv2",
                                  addressdetails=1, **params, **{"accept-language": "en"})
        finally:
            _geo_state["last"] = time.monotonic()
        value = parse(data)
        cache.set(key, {"value": value})
        return value


async def reverse_geocode(client, lat, lng):
    """Nearest named place for a spot, or None. Cached, and spaced to 1 request/second. Raises on failure."""
    return await _nominatim_cached(client, _place_cache, lat, lng, parse_place, zoom=10, extratags=1)


# The place hierarchy (neighbourhood, city, county, state, country). Real `address` keys seen: neighbourhood, quarter,
# suburb, residential, hamlet, village, town, city, municipality, county, state_district, state, region, country,
# country_code. Which of them appear varies by place (Dover has both town and city, set to "Dover").
_address_cache = TTLCache(ttl_seconds=24 * 3600)


def parse_address(data):
    if not data or data.get("error"):
        return None
    return data.get("address") or None


async def reverse_address(client, lat, lng):
    """The `address` object (place hierarchy) for a spot, or None over open water. Same throttle and cache rules."""
    return await _nominatim_cached(client, _address_cache, lat, lng, parse_address, zoom=18)


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


# ---------- Layer 3: rare animal records (GBIF + IUCN) ----------
# GBIF holds observations, museum specimens, market finds and more. A record says where an animal
# was recorded, NOT that it lives there, so nothing in here (or in the narration) may say it lives there.
RARE_NOTE = ("Records show where an animal was observed, not necessarily where it lives. "
             "They can include dead animals, market finds and misplaced locations.")
GBIF = "https://api.gbif.org/v1"
COMMONS = "https://commons.wikimedia.org/w/api.php"
DETAIL_TIMEOUT = 5.0
SPECIMEN_BASES = {"PRESERVED_SPECIMEN", "MATERIAL_SAMPLE", "MATERIAL_CITATION", "LIVING_SPECIMEN", "FOSSIL_SPECIMEN"}
# The ladder: about 30 km first; if that holds nothing, ONE wider step with tighter caps (fewer species, fewer
# records inspected). detail_limit = records inspected per species (the exact count comes from the facet).
RARE_STEPS = [
    {"level": "exact_area", "km": 30, "species": 8, "detail_limit": 50},
    {"level": "wider_area", "km": 90, "species": 5, "detail_limit": 25},
]
_dataset_titles = {}  # dataset key -> title (public dataset metadata, not user data)


async def add_local_names(client, entries, lang):
    """Fill `local_names` for up to MAX_NAME_LOOKUPS species, in parallel. A failed lookup leaves that species with
    only its scientific name; it never breaks the list."""
    todo = [e for e in entries if e.get("species_key")][:MAX_NAME_LOOKUPS]
    results = await asyncio.gather(*(safe(fetch_vernacular(client, e["species_key"])) for e in todo))
    for e, (names, status) in zip(todo, results):
        if status == "ok" and names:
            e["local_names"] = choose_names(names, prefer_langs(lang, e.get("country_code")))


# ---- Species thumbnails: the species' own Wikipedia page image, then its licence and author from Wikimedia Commons ----
# Real responses (checked): en.wikipedia.org/w/api.php?action=query&prop=pageimages&piprop=thumbnail|name&pithumbsize=240
# &redirects=1&titles=A|B gives pages[].{title, pageimage (file name), thumbnail{source,width,height}}; a page with no image has
# neither field. commons.wikimedia.org/w/api.php?action=query&prop=imageinfo&iiprop=extmetadata|url&titles=File:X gives
# imageinfo[0].{descriptionurl, extmetadata{LicenseShortName, LicenseUrl, Artist (HTML), AttributionRequired}}.
# Never GBIF occurrence photos: those show the individual record, which can be a dead animal at a market.
IMAGE_NOTE = "Photos show the species in general, not the animal recorded nearby."
IMAGE_THUMB_PX = 240          # shown at about 120 px wide, so this is sharp on high-density screens
IMAGE_TIMEOUT = 4.0
IMAGE_HOSTS = ("https://upload.wikimedia.org/", "https://thumb.wikimedia.org/")  # pageimages thumbnails live on thumb.
_image_cache = TTLCache(ttl_seconds=24 * 3600, max_items=2000)  # scientific name -> image dict or None (found nothing usable)
# Licences that allow showing the photo with credit. "NC" and "ND" variants and non-free files are not on this list.
_ALLOWED_LICENCE = re.compile(r"^(cc[ -]by(-sa)?[ -]\d(\.\d)?( \w+)?|cc0( 1\.0)?|public domain.*|pd[ -].*|gfdl.*|"
                              r"attribution.*|no restrictions.*)$", re.I)
_BAD_AUTHOR = re.compile(r"(unknown|anonymous|see (file|source|description)|^\W*$)", re.I)


def clean_html(text):
    return " ".join(re.sub(r"<[^>]+>", " ", unescape(text or "")).split())


def image_from_pages(page, info):
    """One species: its Wikipedia page object and the Commons imageinfo object -> image dict, or None when it cannot be
    shown with credit (no picture, no licence, licence not on the allow-list, no author, or an unexpected URL)."""
    thumb = (page or {}).get("thumbnail") or {}
    src, ii = thumb.get("source"), ((info or {}).get("imageinfo") or [{}])[0]
    meta = ii.get("extmetadata") or {}
    licence = ((meta.get("LicenseShortName") or {}).get("value") or "").strip()
    author = re.sub(r"^no machine-readable author provided\.?\s*", "", clean_html((meta.get("Artist") or {}).get("value")), flags=re.I)
    file_page = ii.get("descriptionurl") or ""
    if not (src and src.startswith(IMAGE_HOSTS) and file_page.startswith("https://commons.wikimedia.org/")):
        return None
    if not licence or "nc" in re.split(r"[\s-]+", licence.lower()) or "nd" in re.split(r"[\s-]+", licence.lower()):
        return None
    if not _ALLOWED_LICENCE.match(licence) or not author or _BAD_AUTHOR.search(author):
        return None
    lic_url = (meta.get("LicenseUrl") or {}).get("value") or ""
    return {"url": src, "width": thumb.get("width"), "height": thumb.get("height"),
            "author": author[:90], "license": licence, "license_url": re.sub(r"^http://", "https://", lic_url) if lic_url.startswith(("http://", "https://")) else None,
            "file_page": file_page}


async def lookup_images(client, names):
    """{scientific name: image dict or None} for the names, using two API calls in total. Raises on a failed call."""
    wiki = await get_json(client, WIKI, IMAGE_TIMEOUT, action="query", format="json", prop="pageimages",
                          piprop="thumbnail|name", pithumbsize=IMAGE_THUMB_PX, redirects=1, titles="|".join(names))
    q = wiki.get("query") or {}
    moved = {x["from"]: x["to"] for x in q.get("normalized") or []}
    moved.update({x["from"]: x["to"] for x in q.get("redirects") or []})
    # a normalised title may also be redirected, so follow the chain once more
    final = lambda n: moved.get(moved.get(n, n), moved.get(n, n))
    pages = {p.get("title"): p for p in (q.get("pages") or {}).values() if "missing" not in p}
    wanted = {n: pages.get(final(n)) for n in names}
    files = sorted({p["pageimage"] for p in wanted.values() if p and p.get("pageimage")})
    infos = {}
    if files:
        com = await get_json(client, COMMONS, IMAGE_TIMEOUT, action="query", format="json", prop="imageinfo",
                             iiprop="extmetadata|url", iiextmetadatafilter="LicenseShortName|LicenseUrl|Artist",
                             titles="|".join("File:" + f for f in files))
        cq = com.get("query") or {}
        cmoved = {x["from"]: x["to"] for x in cq.get("normalized") or []}
        by_title = {p.get("title"): p for p in (cq.get("pages") or {}).values() if "missing" not in p}
        for f in files:
            t = "File:" + f.replace("_", " ")
            infos[f] = by_title.get(cmoved.get("File:" + f, t)) or by_title.get(t)
    return {n: (image_from_pages(p, infos.get(p["pageimage"])) if p and p.get("pageimage") else None)
            for n, p in wanted.items()}


async def add_images(client, entries):
    """Fill `image` (dict or None) on each entry. Cached by scientific name; a failed lookup leaves None and is not cached,
    so the card shows a placeholder now and tries again next time."""
    todo = []
    for e in entries:
        e["image"] = None
        hit = _image_cache.get(e["species"])
        if hit is not None:
            e["image"] = hit["image"]
        else:
            todo.append(e)
    if not todo:
        return
    got, status = await safe(lookup_images(client, sorted({e["species"] for e in todo})[:MAX_NAME_LOOKUPS]))
    if status != "ok":
        for e in todo:
            e["image_status"] = status   # "failed: HTTPStatusError": the card shows the placeholder; this is for the sources list
        return
    for e in todo:
        if e["species"] in got:
            e["image"] = got[e["species"]]
            _image_cache.set(e["species"], {"image": e["image"]})


def images_status(rare):
    if not rare:
        return "not needed"
    if any(r.get("image") for r in rare):
        return "ok"
    failed = next((r["image_status"] for r in rare if r.get("image_status")), None)
    return failed or "empty"


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
    keys = Counter(r["speciesKey"] for r in records if r.get("speciesKey"))
    countries = Counter(r["countryCode"] for r in records if r.get("countryCode"))
    return {"species_key": keys.most_common(1)[0][0] if keys else None,
            "country_code": countries.most_common(1)[0][0] if countries else None,
            "records_checked": len(records),
            "datasets": [{"key": k, "name": names.get(k), "records": n} for k, n in top],
            "marked_dead": vitality["dead"], "marked_alive": vitality["alive"], "specimen_records": specimens}


def build_rare_entry(full_name, count, details):
    entry = {"species": " ".join(full_name.split()[:2]),  # "Rissa tridactyla (Linnaeus, 1758)" -> "Rissa tridactyla"
             "records": count}
    entry["local_names"] = []  # filled in by fetch_rare when GBIF returns names; never written by us or by the model
    if details:
        entry.update(species_key=details["species_key"], country_code=details["country_code"],
                     records_checked=details["records_checked"],
                     datasets=[d["name"] or "Unnamed dataset" for d in details["datasets"]],
                     marked_dead=details["marked_dead"], marked_alive=details["marked_alive"],
                     specimen_records=details["specimen_records"])
    else:  # the detail call failed: say we don't know, don't pretend zero
        entry.update(species_key=None, country_code=None, records_checked=None, datasets=None, marked_dead=None,
                     marked_alive=None, specimen_records=None)
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


# ---- Local (vernacular) names, from GBIF's species vernacular-names endpoint only ----
# Real response (checked): {"results": [{"vernacularName", "language" (ISO 639-3 such as "eng", sometimes ""), "source" (the
# underlying dataset), "preferred" (sometimes), "taxonKey", "sourceTaxonKey"}], "endOfRecords"}. There is no country field.
# We never translate, guess or generate a name: a name is shown only if GBIF returned it, labelled with language and source.
RARE_NAMES_NOTE = "Local names come from GBIF and may be incomplete."
MAX_NAME_LOOKUPS = 8       # species looked up per place
MAX_NAMES = 2              # names shown per species
NAMES_TIMEOUT = 4.0
_vernacular_cache = TTLCache(ttl_seconds=24 * 3600, max_items=2000)  # species key -> compact list of names

LANGUAGE_NAMES = {  # ISO 639-3 -> English name, for the label "Bengali name: ..."
    "eng": "English", "ben": "Bengali", "hin": "Hindi", "urd": "Urdu", "nep": "Nepali", "spa": "Spanish", "fra": "French",
    "deu": "German", "por": "Portuguese", "ita": "Italian", "nld": "Dutch", "rus": "Russian", "zho": "Chinese", "jpn": "Japanese",
    "kor": "Korean", "ara": "Arabic", "tur": "Turkish", "pol": "Polish", "ukr": "Ukrainian", "ind": "Indonesian", "msa": "Malay",
    "tha": "Thai", "vie": "Vietnamese", "swe": "Swedish", "dan": "Danish", "nor": "Norwegian", "fin": "Finnish", "tam": "Tamil",
    "sin": "Sinhala", "ell": "Greek", "heb": "Hebrew", "fas": "Persian", "ces": "Czech", "hun": "Hungarian", "ron": "Romanian"}
LANG_2_TO_3 = {"en": "eng", "bn": "ben", "hi": "hin", "ur": "urd", "ne": "nep", "es": "spa", "fr": "fra", "de": "deu", "pt": "por",
               "it": "ita", "nl": "nld", "ru": "rus", "zh": "zho", "ja": "jpn", "ko": "kor", "ar": "ara", "tr": "tur", "pl": "pol",
               "uk": "ukr", "id": "ind", "ms": "msa", "th": "tha", "vi": "vie", "sv": "swe", "da": "dan", "nb": "nor", "no": "nor",
               "fi": "fin", "ta": "tam", "si": "sin", "el": "ell", "he": "heb", "fa": "fas", "cs": "ces", "hu": "hun", "ro": "ron"}
LANG_3_TO_2 = {v: k for k, v in LANG_2_TO_3.items()}  # for the HTML lang attribute (browsers want the 2-letter tag where there is one)
COUNTRY_LANGUAGE = {  # ISO 3166 alpha-2 -> the country's main language (a rough, short list; unknown countries are skipped)
    "BD": "ben", "IN": "hin", "PK": "urd", "NP": "nep", "LK": "sin", "GB": "eng", "US": "eng", "AU": "eng", "CA": "eng", "NZ": "eng",
    "IE": "eng", "ZA": "eng", "FR": "fra", "DE": "deu", "AT": "deu", "ES": "spa", "MX": "spa", "AR": "spa", "CO": "spa", "CL": "spa",
    "PE": "spa", "PT": "por", "BR": "por", "IT": "ita", "NL": "nld", "RU": "rus", "UA": "ukr", "PL": "pol", "TR": "tur", "CN": "zho",
    "TW": "zho", "JP": "jpn", "KR": "kor", "SA": "ara", "EG": "ara", "AE": "ara", "ID": "ind", "MY": "msa", "TH": "tha", "VN": "vie",
    "SE": "swe", "DK": "dan", "NO": "nor", "FI": "fin", "GR": "ell", "IL": "heb", "IR": "fas", "CZ": "ces", "HU": "hun", "RO": "ron"}
# Many datasets give a name; these are the most trustworthy ones, tried first. (Some datasets mislabel languages.)
SOURCE_RANK = ["The IUCN Red List of Threatened Species", "Catalogue of Life", "Integrated Taxonomic Information System (ITIS)",
               "TAXREF", "EUNIS Biodiversity Database"]


def norm_lang(tag):
    """A browser language tag ("bn-BD", "en", "ben") -> ISO 639-3, or None if it is not one we know."""
    if not isinstance(tag, str):
        return None
    primary = tag.strip().lower().replace("_", "-").split("-")[0]
    if primary in LANG_2_TO_3:
        return LANG_2_TO_3[primary]
    return primary if len(primary) == 3 and primary.isalpha() else None


def prefer_langs(browser_lang, country_code):
    """Language order for names: the browser's language, then the country's main language, then English."""
    order = [norm_lang(browser_lang), COUNTRY_LANGUAGE.get((country_code or "").upper()), "eng"]
    return [x for i, x in enumerate(order) if x and x not in order[:i]]


def compact_names(results):
    """GBIF reply -> the few fields we keep. Names with no language code are dropped: we could not label them."""
    return [{"name": r["vernacularName"].strip(), "language": r["language"], "dataset": r.get("source") or "",
             "preferred": bool(r.get("preferred"))}
            for r in results if (r.get("vernacularName") or "").strip() and (r.get("language") or "").strip()]


def choose_names(names, languages, limit=MAX_NAMES):
    """Up to `limit` names, taken language by language in the preferred order; best source first within a language."""
    def rank(n):
        src = SOURCE_RANK.index(n["dataset"]) if n["dataset"] in SOURCE_RANK else len(SOURCE_RANK)
        return (not n["preferred"], src)
    chosen, seen = [], set()
    for lang in languages:
        for n in sorted((n for n in names if n["language"] == lang), key=rank):
            key = " ".join(n["name"].lower().split())
            if key in seen:
                continue
            seen.add(key)
            chosen.append({"name": n["name"], "language": lang, "lang_tag": LANG_3_TO_2.get(lang),
                           "language_name": LANGUAGE_NAMES.get(lang, f"language code {lang}"),
                           "source": "GBIF", "dataset": n["dataset"]})
            if len(chosen) == limit:
                return chosen
    return chosen


async def fetch_vernacular(client, species_key):
    """All names GBIF has for a species (cached by species key). Raises on failure."""
    hit = _vernacular_cache.get(species_key)
    if hit is not None:
        return hit["names"]
    data = await get_json(client, f"{GBIF}/species/{species_key}/vernacularNames", NAMES_TIMEOUT, limit=300)
    names = compact_names((data or {}).get("results") or [])
    _vernacular_cache.set(species_key, {"names": names})
    return names


def rare_area(lat, lng, km):
    """A box around the spot that is `km` in every direction (longitude degrees shrink away from the equator)."""
    dlat = km / 111.0
    dlng = min(180.0, km / (111.0 * max(math.cos(math.radians(lat)), 0.05)))
    return dict(decimalLatitude=f"{max(-90, lat - dlat):.4f},{min(90, lat + dlat):.4f}",
                decimalLongitude=f"{max(-180, lng - dlng):.4f},{min(180, lng + dlng):.4f}",
                iucnRedListCategory=["CR", "EN", "VU"], kingdomKey=1)


async def fetch_rare(client, lat, lng, lang=None):
    """Returns (entries, scope). A wider step runs only if the narrower one found no species.
    `lang` is the browser's language tag, used only to choose which local names to show."""
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
        await asyncio.gather(add_local_names(client, entries, lang), add_images(client, entries))
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
            # lat/lng/pageid come from the same geosearch response; they let the page show a direction, a map link
            # and a Wikipedia link for each place. Nothing new is fetched.
            out.append({"place": p["title"], "distance_m": round(p["dist"]), "summary": text,
                        "lat": p.get("lat"), "lng": p.get("lon"), "pageid": p.get("pageid")})
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
    # lat/lng/pageid stay None: this is an introduction to a whole city or region, not an individual site
    return [{"place": place["name"], "distance_m": distance_m, "summary": intro,
             "lat": None, "lng": None, "pageid": None}], scope, "ok"


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


async def fast_facts(client, lat, lng, lang=None):
    """Rock + rare animals + human history, in parallel. Fossils come later (they are slow)."""
    (unit, s_rock), (rare_res, s_rare), (human_res, s_human) = await asyncio.gather(
        safe(fetch_rock(client, lat, lng)),
        safe(fetch_rare(client, lat, lng, lang)),
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
        "rare_names_note": RARE_NAMES_NOTE,
        "rare_images_note": IMAGE_NOTE,
        "human_history_nearby": human,
        # how far from the user each layer's facts really come from (None = that source failed)
        "scope": {"rock": rock_scope(unit) if s_rock == "ok" else None,
                  "rare": rare_scope_, "human": human_scope_},
    }
    statuses = {"macrostrat": source_status(unit, s_rock),
                "gbif": source_status(rare, s_rare),
                "wikipedia": source_status(human, s_human),
                "commons": images_status(rare),
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
