"""Re-record the sample API responses used by the tests (needs network). Run: python tests/capture_samples.py"""
import json
from pathlib import Path

import httpx

UA = {"User-Agent": "stela/0.1 (hacktoberfest open-source project)"}
OUT = Path(__file__).parent / "samples"
OUT.mkdir(exist_ok=True)


def get(url, **params):
    r = httpx.get(url, params=params, headers=UA, timeout=60)
    r.raise_for_status()
    return r.json()


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# Dover, UK
save("macrostrat_dover.json", get("https://macrostrat.org/api/v2/geologic_units/map",
                                  lat=51.13, lng=1.31, scale="medium", response="long"))
save("pbdb_dover.json", get("https://paleobiodb.org/data1.2/occs/list.json", lngmin=.81, lngmax=1.81,
                            latmin=50.63, latmax=51.63, max_ma=100.5, min_ma=66, timerule="overlap",
                            show="class,coords", limit=40))
save("gbif_dover.json", get("https://api.gbif.org/v1/occurrence/search", decimalLatitude="50.88,51.38",
                            decimalLongitude="1.06,1.56", iucnRedListCategory=["CR", "EN", "VU"], kingdomKey=1,
                            limit=0, facet="scientificName", facetLimit=8))
WIKI = "https://en.wikipedia.org/w/api.php"
pages = get(WIKI, action="query", list="geosearch", gscoord="51.13|1.31", gsradius=10000, gslimit=50,
            format="json")["query"]["geosearch"][:12]
ids = "|".join(str(p["pageid"]) for p in pages)
extracts = get(WIKI, action="query", prop="extracts", exintro=1, explaintext=1, exsentences=2, exlimit=20,
               pageids=ids, format="json")["query"]["pages"]
save("wikipedia_dover.json", {"geosearch": pages, "extracts": extracts})
print("saved to", OUT)

# Dhaka, Bangladesh: raw GBIF records for rare animals, trimmed to the fields Stela reads.
# (Dhaka showed marine sharks and rays, and some records are marked dead.)
KEEP = ("species", "datasetKey", "datasetName", "vitality", "basisOfRecord", "occurrenceRemarks")
area = dict(decimalLatitude="23.56,24.06", decimalLongitude="90.16,90.66", iucnRedListCategory=["CR", "EN", "VU"], kingdomKey=1)
raw = get("https://api.gbif.org/v1/occurrence/search", **area, limit=300)["results"]
save("gbif_dhaka_records.json", [{k: r.get(k) for k in KEEP} for r in raw])
print("saved dhaka records:", len(raw))


# Scope fallbacks: Nominatim (a city, a region, open ocean), a Wikipedia intro, and PBDB at country level.
import time

NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
for name, (la, ln) in {"madison": (43.07, -89.40), "uluru": (-25.34, 131.04), "ocean": (0, -30)}.items():
    save(f"nominatim_{name}.json", get(NOMINATIM, lat=la, lon=ln, format="jsonv2", zoom=10, addressdetails=1,
                                      extratags=1, **{"accept-language": "en"}))
    time.sleep(1.1)  # Nominatim policy: at most 1 request per second
save("wikipedia_intro_madison.json", get(WIKI, action="query", prop="extracts", exintro=1, explaintext=1,
                                         exsentences=3, titles="Madison, Wisconsin", redirects=1, format="json"))
save("pbdb_country_us.json", get("https://paleobiodb.org/data1.2/occs/list.json", cc="US", max_ma=497.5, min_ma=493.575,
                                 timerule="overlap", show="class,coords", limit=25))
print("saved scope samples")
