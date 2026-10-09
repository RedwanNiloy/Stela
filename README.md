# Stela

> Every place is a stone with a story carved in it.

Open Stela on your phone, tap **Use my location** (or drop a pin on the map), and put the phone away. A calm "stone scribe" tells you about the ground under your feet: the rock and its age, what the place probably was (sea, land, swamp), the fossils found nearby, rare animals alive today, and human history close by. It can read the story aloud.

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

```
app/main.py      FastAPI: /api/where, /api/fossils, /health, serves the page; rate limit; privacy-safe logging
app/geo.py       the four data layers (fetch_* talks to the API, parse_* is a pure function). No LLM code
app/narrate.py   grounding rules + LLM call + plain-text fallback
app/cache.py     TTL cache and per-IP rate limiter (in memory)
static/index.html  one mobile-first page, vanilla JS, Leaflet map, no build step
tests/           parsing tests on saved real API responses, plus the fallback path
```

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
python -m pytest -q          # no network needed
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

## Deploy on Render (free tier)

1. Push this repo to GitHub.
2. In Render: **New, Blueprint**, pick the repo. `render.yaml` sets up a Python web service with a health check on `/health`.
3. Fill in `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` in the service's Environment tab (they are marked `sync: false`, so they are never in the repo).
4. Deploy. The free tier sleeps when idle, so the first request after a pause is slow.

## Privacy

Stela does not store or log your location or IP address. Logs contain only the route, status, timing and error types. Coordinates are rounded to 3 decimals (about 110 m) for the in-memory cache, which is lost on restart. The rate limiter keeps IPs in memory only. Your coordinates are sent to Macrostrat, PBDB, GBIF and Wikipedia to fetch the facts, as part of the request URL.

## Data sources and licenses

| Source | Used for | License |
|---|---|---|
| [Macrostrat](https://macrostrat.org) | rock unit, age | CC-BY 4.0 |
| [Paleobiology Database](https://paleobiodb.org) | fossil finds | CC-BY 4.0 |
| [GBIF](https://www.gbif.org) with IUCN Red List categories | rare animals today | CC0 / CC-BY per dataset |
| [Wikipedia](https://en.wikipedia.org) | human history nearby | CC-BY-SA 4.0 |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) tiles, [Leaflet](https://leafletjs.com) | map | ODbL, BSD-2 |

Please check each provider's current terms before heavy use. All artwork is original CSS/SVG.

## Honest limitations

- **Fossils show what has been found, not everything that lived there.** Many places have few or no records.
- **Sea, land or swamp is a guess from the rock type**, using a short keyword list. Sandstone in particular can be either.
- **Rock maps vary in detail.** The unit shown is the best match at the map scale available, and ages are ranges.
- **Rare animals may include introduced or feral animals** (rabbits and feral horses show up), because GBIF does not say which records are native. Counts are sighting records, not populations.
- **Human history is whatever has a Wikipedia page within 10 km** and matches a list of history keywords. Many rich places show nothing, and the page says so rather than guessing.
- **Fossil searches can be wide.** If nothing matches nearby, the search widens up to about 220 km, and the page says how far it looked. The age match can also be dropped, and then it says "ANY age".
- **Small language models can slip.** Strict prompts reduce invented details but cannot guarantee zero. In testing, a 4B local model sometimes turned scientific names into common names. That is why the plain fact cards are always shown under the story.
- PBDB can take 30 seconds or more for sparse regions.

## Out of scope for v1

Sky and star views, AR/VR, accounts, databases, a time slider, animal images or 3D models, and storing any user location.
