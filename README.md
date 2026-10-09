# Stela

> Every place is a stone with a story carved in it.

Open Stela on your phone, tap **Use my location** (or drop a pin on the map), and put the phone away. A calm "stone scribe" tells you about the ground under your feet: the rock and its age, what the place probably was (sea, land, swamp), the fossils found nearby, rare animals recorded nearby, and human history close by. It can read the story aloud.

Built for the Hacktoberfest Open-Source AI Challenge, Week 1 "Touch Grass".

## The one rule

**Code collects facts from open datasets. The open-weight LLM may only narrate those facts.**
The model never looks anything up. If a source is empty, the story says "nothing recorded nearby". If a source is down, the story says it could not be checked. If the model itself is down or too slow, you get the same facts as a plain text summary (`mode: "facts-only"`), never an error screen.

## How it works

```
browser ──/api/where──▶  geo.py: Macrostrat + GBIF + Wikipedia, in parallel (about 1-2 s)
   │                      narrate.py: LLM tells the story from those facts
   └──/api/fossils──▶   geo.py: Paleobiology Database (slow, 3-30 s), then a short LLM paragraph
```

The page shows the fast results immediately and fills in the fossils card when the slow source answers.

### Scope: how far from you do the facts really come from?

Every layer reports a `scope` (`exact_area`, `wider_area`, `nearest_place`, `region` or `none`) with a distance or place name and a plain-words note. Each card on the page shows it, and the narrator must repeat it and never claim wider-area facts happened at your exact spot. Each ladder is capped, and a wider step only runs when the narrower one found nothing relevant:

| Layer | Steps (stops at the first that finds something) |
|---|---|
| Rock | Macrostrat map scale: large (detailed), medium (regional), small (continental). Reports which scale matched. |
| Fossils | same-age finds within about 55 km, then about 222 km, then about 222 km with the age match dropped, then same-age finds anywhere in your country (PBDB `cc` parameter). Reports the real distance to the nearest find. |
| Rare animals | records within about 30 km, then one wider step of about 90 km with fewer species and fewer records inspected. |
| Human history | Wikipedia pages within 10 km that match history keywords, then the nearest city or town (OpenStreetMap Nominatim) and its Wikipedia introduction, or the state/country if that is all there is. |

If every step is empty, the layer says "nothing recorded".

```
app/main.py      FastAPI: /api/where, /api/fossils, /health, serves the page; rate limit; privacy-safe logging
app/geo.py       the four data layers (fetch_* talks to the API, parse_* is a pure function). No LLM code
app/narrate.py   grounding rules + LLM call + plain-text fallback
app/cache.py     TTL cache and per-IP rate limiter (in memory)
static/index.html  one mobile-first page, vanilla JS, Leaflet map, no build step
static/stela-logic.js  walk-mode distance rule and the passport (pure logic, tested in Node)
tests/           parsing and fallback tests on saved real API responses, plus tests of the browser logic
```

## Passport and Walk mode

**Passport.** When you look up where you actually are (the "Use my location" button or Walk mode), Stela stamps the place in a passport: rounded coordinates (3 decimals), rock name, rock age range, era and the date. The Passport view lists your places and counts the distinct eras you have collected, and "Clear my passport" (two taps) wipes it. **The passport lives only in your browser (localStorage) and is never sent to the server.** There are no accounts. Pins dropped on the map do not count as visits. If your browser blocks storage, the app works as normal and the passport only lasts until you close the page (it tells you so).

**Walk mode.** A toggle that uses `watchPosition` (never a timer). It asks for a new story only when you have moved more than about 300 m from the last request, ignores GPS fixes less accurate than 200 m, and stops watching when you switch it off or the page is hidden (it resumes when the page is visible again). A small "You have moved into a new rock unit" message appears only when the rock unit or era really changed. It never speaks on its own: you either tick "Read each new story aloud automatically" or tap the offered Listen button. It reuses the normal `/api/where` and `/api/fossils` calls, so the server cache and rate limit apply as usual. **Walk mode keeps GPS on and uses more battery.**

## Run it locally

```bash
python -m venv .venv && .venv\Scripts\activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### Online mode (any OpenAI-compatible endpoint serving an open-weight model)

```bash
cp .env.example .env     # then fill in the three values; or just export them
export LLM_BASE_URL=https://your-provider.example/v1
export LLM_API_KEY=...
export LLM_MODEL=<the open-weight Gemma model name your provider uses>
uvicorn app.main:app --port 8000 --no-access-log
```

Open http://localhost:8000. Stela calls `POST {LLM_BASE_URL}/chat/completions`. The model name is never hard-coded.

### Offline mode (local Ollama)

Leave `LLM_BASE_URL` empty and run Ollama:

```bash
ollama pull gemma3:4b
export OLLAMA_MODEL=gemma3:4b        # default; use any model you have pulled
uvicorn app.main:app --port 8000 --no-access-log
```

On a slow CPU a local reply can take longer than the 30 s limit, and Stela then falls back to the facts-only summary. To wait longer while testing, set `LLM_TIMEOUT=120`.

> Always start uvicorn with `--no-access-log`. Its access log prints full URLs, which contain coordinates.

### Tests

```bash
python -m pytest -q          # no network needed (also runs the browser-logic tests if Node.js is installed)
node --test tests/           # the browser-logic tests on their own: walk-mode distance rule and passport storage
python tests/capture_samples.py   # optional: re-record the sample API responses
```

## Environment variables

| Name | Meaning |
|---|---|
| `LLM_BASE_URL` | OpenAI-compatible base URL (ending in `/v1`). Empty means use local Ollama. |
| `LLM_API_KEY` | Bearer token for that endpoint. Never commit it. |
| `LLM_MODEL` | Model name your endpoint expects. |
| `OLLAMA_URL` / `OLLAMA_MODEL` | Offline mode only. Default `http://localhost:11434` and `gemma3:4b`. |
| `LLM_TIMEOUT` | Seconds before the LLM call is abandoned. Default 30. |
| `STELA_CONTACT` | A URL or email added to the User-Agent of outgoing data requests. Nominatim's policy requires an identifying User-Agent, and Wikimedia rate-limits anonymous ones harder. Set it when you deploy. |

## Deploy on Render (free tier)

1. Push this repo to GitHub.
2. In Render: **New, Blueprint**, pick the repo. `render.yaml` sets up a Python web service with a health check on `/health`.
3. Fill in `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (and `STELA_CONTACT`) in the service's Environment tab (they are marked `sync: false`, so they are never in the repo).
4. Deploy. The free tier sleeps when idle, so the first request after a pause is slow.

## Privacy

Stela does not store or log your location or IP address. The passport is stored only in your own browser and is never sent to the server. In Walk mode your position is sent to the server like any other lookup, but only when you have moved about 300 m. Logs contain only the route, status, timing and error types. Coordinates are rounded to 3 decimals (about 110 m) for the in-memory cache, which is lost on restart. The rate limiter keeps IPs in memory only. Your coordinates are sent to Macrostrat, PBDB, GBIF and Wikipedia to fetch the facts, as part of the request URL. When nothing is recorded within 10 km, they are also sent to a geocoding service, [OpenStreetMap Nominatim](https://operations.osmfoundation.org/policies/nominatim/), to find the nearest named place (and, for fossils, your country). That is one extra third party, only in that case. Stela spaces those requests to at most one per second, and caches the result in memory by rounded coordinates.

## Data sources and licenses

| Source | Used for | License |
|---|---|---|
| [Macrostrat](https://macrostrat.org) | rock unit, age | CC-BY 4.0 |
| [Paleobiology Database](https://paleobiodb.org) | fossil finds | CC-BY 4.0 |
| [GBIF](https://www.gbif.org) with IUCN Red List categories | rare animal records | CC0 / CC-BY per dataset |
| [Wikipedia](https://en.wikipedia.org) | human history nearby | CC-BY-SA 4.0 |
| [OpenStreetMap Nominatim](https://nominatim.org) | nearest city/town and country, only when needed | ODbL |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) tiles, [Leaflet](https://leafletjs.com) | map | ODbL, BSD-2 |

Please check each provider's current terms before heavy use. All artwork is original CSS/SVG.

## Honest limitations

- **Fossils show what has been found, not everything that lived there.** Many places have few or no records.
- **Sea, land or swamp is a guess from the rock type**, using a short keyword list. Sandstone in particular can be either.
- **Rock maps vary in detail.** The unit shown is the best match at the map scale available, and ages are ranges. When only a regional or continental map exists, the card says the rock name is a broader grouping.
- **Rare-animal records are not proof an animal lives nearby.** A GBIF record says where an animal was recorded. It can be a dead animal, a market or fish-landing find, a museum specimen, or a misplaced location. In Dhaka, the results were mostly marine sharks and rays, with some records marked dead. Stela therefore says "recorded nearby", never "alive" or "lives here".
- **Alive/dead is only shown when the record says so.** It reads GBIF's `vitality` field and `basisOfRecord` (specimen or sample). Most records leave `vitality` empty, so most entries say "do not say". Free-text remarks are not interpreted, and nothing is guessed. The check covers up to 50 records per species.
- **Records may include introduced or feral animals** (rabbits and feral horses show up), because GBIF does not say which records are native. Counts are numbers of records, not of animals.
- **Human history is whatever has a Wikipedia page within 10 km** and matches a list of history keywords. If there is none, the card shows a general introduction to the nearest city (or state), clearly labelled as not your exact spot. That is not the history of your spot, and it is not keyword-checked.
- **Wider-scope facts are about somewhere else.** Fossils can come from 100+ km away or from anywhere in your country (then no distance is given, because the country step returns an arbitrary sample, not the nearest finds). Rare-animal records can come from up to about 90 km away. The cards and the narration say so, but the distance in the fossil note is to the nearest find among the (at most 200) records returned.
- **Search areas are boxes, not circles** for fossils, so a find at a box corner can be a little farther than the stated size.
- **The geocoder can be wrong or empty.** Nominatim returns nothing over open water, and its Wikipedia tag sometimes points to a district page rather than the town itself (for example "Dover District").
- **Wikipedia may rate-limit (HTTP 429)**, especially for anonymous or shared-IP clients. The card then says it could not be checked. Setting `STELA_CONTACT` helps.
- **Small language models can slip.** Strict prompts reduce invented details but cannot guarantee zero. In testing, a 4B local model sometimes turned scientific names into common names, and once said rare animals "wander" the area. A code check now discards any story that calls the animals alive or living here and shows the plain facts instead. That is why the plain fact cards are always shown under the story.
- PBDB can take 30 seconds or more for sparse regions.
- **The passport is per browser and per device.** Clearing site data, using a private window, or switching phones starts a new one. There is no sync, by design.
- **Walk mode needs the page open and visible.** Browsers pause GPS for hidden pages and locked screens, so it cannot track you in the background. GPS can be slow or inaccurate indoors, under tree cover and in cities; weak fixes are ignored rather than guessed from.
- **"New rock unit" is best effort.** Names differ between map scales for the same ground, so a change of unit is only reported when both lookups used the same map scale; a change of era (time interval) is always reported. Geological maps are coarse, so boundaries you walk across may not match what is on the ground.
- **Walk mode and the rate limit.** Each new story is two requests (about 20 stories a minute are allowed). If the server says slow down, Walk mode pauses for a minute.

## Out of scope for v1

Sky and star views, AR/VR, accounts, databases, a time slider, animal images or 3D models, and storing any user location.
