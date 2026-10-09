"""Stela data layers. No LLM code in here: this module only collects facts from open datasets.

Each layer is split in two so it can be tested without the network:
  fetch_*  - talks to the API (async, raises on failure)
  parse_*  - turns the raw JSON into our small fact dict (pure function)
"""
import asyncio
import re
from collections import Counter

import httpx

UA = {"User-Agent": "stela/0.1 (hacktoberfest open-source project)"}
HUMAN_MA = 0.3  # Homo sapiens, roughly 300,000 years

FAST_TIMEOUT = 6.0    # Macrostrat, GBIF, Wikipedia usually answer in about 1 second
PBDB_TIMEOUT = 20.0   # PBDB takes 3 s normally but up to 30 s+ on sparse queries
PBDB_TOTAL = 45.0     # hard cap across its (up to) 3 attempts

SOURCES = {
    "macrostrat": {"name": "Macrostrat", "url": "https://macrostrat.org", "license": "CC-BY 4.0"},
    "pbdb": {"name": "Paleobiology Database", "url": "https://paleobiodb.org", "license": "CC-BY 4.0"},
    "gbif": {"name": "GBIF / IUCN Red List", "url": "https://www.gbif.org", "license": "CC0 / CC-BY, per dataset"},
    "wikipedia": {"name": "Wikipedia", "url": "https://en.wikipedia.org", "license": "CC-BY-SA 4.0"},
}


async def get_json(client, url, timeout, **params):
    r = await client.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------- Layer 1: rock under your feet (Macrostrat) ----------
ROCK_FIELDS = ("name", "strat_name", "lith", "descrip", "b_age", "t_age", "b_int_name", "t_int_name")


def parse_rock(data, scale):
    rows = ((data or {}).get("success") or {}).get("data") or []
    if not rows:
        return None
    unit = {k: rows[0].get(k) for k in ROCK_FIELDS}
    unit["map_scale"] = scale
    return unit


async def fetch_rock(client, lat, lng):
    # Ask for the most detailed map first, then fall back to coarser ones.
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


# ---------- Layer 2: ancient animals (Paleobiology Database) ----------
def parse_fossils(data, box, use_age):
    recs = (data or {}).get("records") or []
    if not recs:
        return None
    classes = Counter(r["cll"] for r in recs if r.get("cll") and r["cll"] != "NO_CLASS_SPECIFIED")
    taxa = Counter(r["tna"] for r in recs if r.get("tna"))
    return {
        "search": f"within about {int(box * 111)} km, " + ("same age as the rock" if use_age else "ANY age (not matched to the rock)"),
        "count": len(recs),
        "classes": classes.most_common(6),
        "taxa": taxa.most_common(10),
    }


async def fetch_fossils(client, lat, lng, unit):
    hi, lo = unit.get("b_age"), unit.get("t_age")
    attempts = [(0.5, True), (2.0, True), (2.0, False)]  # widen the box, then drop the age filter
    last_error, any_ok = None, False
    for box, use_age in attempts:
        if use_age and (hi is None or lo is None):
            continue
        p = dict(lngmin=lng - box, lngmax=lng + box, latmin=lat - box, latmax=lat + box,
                 show="class", limit=200)
        if use_age:
            # timerule=overlap: the default ("major") returned nothing for thin units
            p.update(max_ma=hi, min_ma=lo, timerule="overlap")
        try:
            data = await get_json(client, "https://paleobiodb.org/data1.2/occs/list.json", PBDB_TIMEOUT, **p)
        except Exception as e:  # one slow attempt should not hide the others
            last_error = e
            continue
        any_ok = True
        found = parse_fossils(data, box, use_age)
        if found:
            return found
    if not any_ok and last_error:
        raise last_error
    return None


# ---------- Layer 3: rare animals living nearby today (GBIF + IUCN) ----------
INTRODUCED_NOTE = ("Counts are GBIF sighting records, not population sizes. The list may include "
                   "introduced, feral or domestic animals (for example rabbits or feral horses), "
                   "because GBIF does not say which records are native.")


def parse_rare(data):
    facets = (data or {}).get("facets") or []
    counts = facets[0]["counts"] if facets else []
    merged = {}
    for c in counts:
        species = " ".join(c["name"].split()[:2])  # drop author and year: "Rissa tridactyla (Linnaeus, 1758)"
        merged[species] = merged.get(species, 0) + c["count"]
    return [{"species": s, "sighting_records": n} for s, n in merged.items()]


async def fetch_rare(client, lat, lng, box=0.25):
    # facet=scientificName counts species across ALL matches. The first 100 raw
    # records were dominated by whichever species happened to be listed first.
    data = await get_json(client, "https://api.gbif.org/v1/occurrence/search", FAST_TIMEOUT,
                          decimalLatitude=f"{lat - box},{lat + box}",
                          decimalLongitude=f"{lng - box},{lng + box}",
                          iucnRedListCategory=["CR", "EN", "VU"], kingdomKey=1, limit=0,
                          facet="scientificName", facetLimit=8)
    return parse_rare(data)


# ---------- Layer 4: human history (Wikipedia geosearch) ----------
# Whole-word match: a plain "roman" also matched "Roman Catholic" schools.
HISTORY_WORDS = re.compile(
    r"(?<!\w)(ancient|archaeolog\w*|ruins?|prehistoric|neolithic|bronze age|iron age|"
    r"roman (?:villa|fort|road|temple|britain|empire|period)|settlement|hillfort|temple|"
    r"civili[sz]ation|burial|megalith\w*|stone age|paleolithic|medieval|castle|"
    r"aboriginal|indigenous|native american|effigy|mounds?|petroglyphs?|rock art|sacred)(?!\w)")
WIKI = "https://en.wikipedia.org/w/api.php"


def parse_human(pages, extracts):
    out = []
    for p in pages:
        text = (extracts.get(str(p["pageid"])) or {}).get("extract", "")
        if HISTORY_WORDS.search((p["title"] + " " + text).lower()):
            out.append({"place": p["title"], "distance_m": round(p["dist"]), "summary": text})
    return out[:5]


async def fetch_human(client, lat, lng):
    g = await get_json(client, WIKI, FAST_TIMEOUT, action="query", list="geosearch",
                       gscoord=f"{lat}|{lng}", gsradius=10000, gslimit=50, format="json")
    pages = ((g or {}).get("query") or {}).get("geosearch") or []
    extracts = {}
    for i in range(0, len(pages), 20):  # the extracts API returns at most 20 pages per call
        ids = "|".join(str(p["pageid"]) for p in pages[i:i + 20])
        e = await get_json(client, WIKI, FAST_TIMEOUT, action="query", prop="extracts", exintro=1,
                           explaintext=1, exsentences=2, exlimit=20, pageids=ids, format="json")
        extracts.update(((e or {}).get("query") or {}).get("pages") or {})
    return parse_human(pages, extracts)


# ---------- Putting the layers together ----------
async def safe(coro):
    """Run one layer. Returns (value, status). A failure gives (None, 'failed'), never an exception."""
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
    (unit, s_rock), (rare, s_rare), (human, s_human) = await asyncio.gather(
        safe(fetch_rock(client, lat, lng)),
        safe(fetch_rare(client, lat, lng)),
        safe(fetch_human(client, lat, lng)),
    )
    facts = {
        "rock": unit,
        "ancient_setting": setting(unit) if unit else None,
        "time_ruler": time_ruler(unit) if unit else None,
        "fossils_found_nearby": "pending",  # filled in by /api/fossils
        "rare_animals_alive_today": rare,
        "rare_animals_note": INTRODUCED_NOTE,
        "human_history_nearby": human,
    }
    statuses = {"macrostrat": source_status(unit, s_rock),
                "gbif": source_status(rare, s_rare),
                "wikipedia": source_status(human, s_human)}
    return facts, statuses


async def slow_fossils(client, lat, lng, unit):
    """PBDB fossils, capped at PBDB_TOTAL seconds. Returns (value, status)."""
    if not unit:
        return None, "empty"
    value, status = await safe(asyncio.wait_for(fetch_fossils(client, lat, lng, unit), PBDB_TOTAL))
    return value, source_status(value, status)
