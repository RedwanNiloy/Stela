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
app/people.py    People and past civilizations: place hierarchy, Wikipedia history articles, excerpts, traditions
app/narrate.py   grounding rules + LLM call + plain-text fallback
app/cache.py     TTL cache and per-IP rate limiter (in memory)
static/index.html  one mobile-first page, vanilla JS, Leaflet map, no build step
static/stela.css   the look: sandstone palette, stone-tablet cards, dig layers, animations (all optional)
static/stela-logic.js  walk-mode distance rule and the passport (pure logic, tested in Node)
static/stela-game.js   Guess the past (pure logic, tested in Node)
tests/           parsing and fallback tests on saved real API responses, plus tests of the browser logic
```

## The look

An ancient-discovery feel in original CSS/SVG only: sandstone, papyrus and deep brown with a muted turquoise accent, carved-style headings (Cinzel from Google Fonts, falling back to a system serif), stone-tablet result cards that carve in as they arrive, a simple scribe figure whose speech panel holds the narration with **Listen** as the main control, an era strip (rock age against today, with human history marked), and **Dig down**, which opens layered bands: surface, human history, the rock with its age, and fossils. The styling is decoration and does not claim to depict any real culture.

- **Dig down only draws what the data supports.** Fossils sit in the rock's layer only when the fossil search says they are of that rock's age; otherwise they go in a separate "found nearby, age not matched" layer. The layers are not to scale, and the page says so. Every scope label and honesty note stays visible.
- **Motion is optional.** Animations are short (under about a second, plus a looping sand-drift while the slow fossil source runs). With `prefers-reduced-motion` nothing animates. Content is never hidden waiting for an animation or a font, so if either fails the page still works.
- **Light and dark mode** both keep text contrast at 5:1 or better (most pairs are above 10:1) for reading outdoors. Buttons are at least 56 px tall.
- **Light on the wire.** No images; the page is HTML, one CSS file and one small JS file (about 19 KB gzipped from our server), plus Leaflet from a CDN. The server gzips responses.

## People and past civilizations

A card with what Wikipedia says about the early history of your place at up to five scopes: **your area, city, district, region, country**. It replaces the old "Human history nearby" card; the museums and historic sites themselves are listed in "Places to visit".

How it works (`app/people.py`, endpoints `/api/people` for the excerpts and `/api/people/story` for the scribe's paragraph):

1. **Place hierarchy.** One OpenStreetMap Nominatim reverse lookup (zoom 18, English names) gives the `address`: neighbourhood/quarter/suburb, city/town, county/state_district, state, country. (Same 1 request/second limit, User-Agent and cache as the other geocoding.)
2. **Articles.** For each level it looks for the English Wikipedia article "History of <place>"; if there is none it uses the place's own article (for example "Madison, Wisconsin", reached through a redirect). Disambiguation pages are never used, and an article that could be a homonym ("Tower Hamlets" is both a Dover suburb and a London borough) is accepted only if its title names the parent place or its text mentions it.
3. **Excerpts.** With the MediaWiki API it reads the article's introduction and its **earliest-period section** (the first real section of a "History of" article, or the first subsection of "History" in a place's own article). At most 2 sentences of the introduction and 3 of the early section are shown, each with the article title, link, scope label and the Wikipedia CC BY-SA 4.0 credit.
4. **Traditions.** Sentences containing explicit folklore words (legend, myth, folklore, "according to tradition", "is said to"...) are kept out of the excerpts and listed separately as "tradition, not verified history". Nothing is guessed: a sentence without those words is never classed as folklore.
5. **Narration.** The model may use only the supplied excerpts. The prompt requires attribution ("According to Wikipedia"), says which scope each fact belongs to, forbids adding peoples, kingdoms, rulers, dates or origins, forbids claiming an event happened at your exact spot unless the scope is "your area", and says to answer "no early history recorded for this area." when nothing was found (in that case the model is not called at all). A code check also discards a story that names a people, ruler or date that is not in the excerpts, writes "here" for a wider place, or leaves out the attribution, and shows the plain attributed excerpts instead.

The card always says "Histories of this area may be contested or incomplete." No indigenous-territory dataset or any other new source was added.

## Guess the past

**Guess the past** is an optional game under the story. A pure function (`possibleQuestionTypes`) looks at the facts and says which questions are possible; one or two are picked at random:

| Question | Needs | Answer comes from |
|---|---|---|
| What was this place *probably* like? (sea, swamp, desert, river) | a rock type that maps to a setting | the rock type |
| Slide to guess the rock's age | the rock's age range | Macrostrat |
| Which animal group appears in the fossil finds? | at least one fossil record (so it appears after the slow fossil source answers) | PBDB |
| Which nearby place is the oldest? | 2+ human-history pages with a construction or founding date, and a clear oldest | Wikipedia introductions |
| Which species appears in the rare-animal records? ("recorded", never "alive") | at least one rare-animal record | GBIF |

Questions and answers are computed only from the facts, never from the language model. The wrong options for the fossil and species questions ("decoys") come from fixed lists, are removed if they are in the real data, and are never shown as facts; the reveal says that "not in the records" does not prove something never lived there. After a guess the page reveals the real answer with its supporting facts. If a layer used a wider scope (a regional rock map, the nearest city, a wider fossil search), the question and the reveal say so and show the scope label. If no question is possible the card says: "The scribe has no tablets for this spot. Try walking somewhere new or dropping a pin elsewhere." There is a Skip button, nothing is spoken, and animations respect `prefers-reduced-motion`.

## Places to visit

Museums and historic sites within about 10 km, nearest first, up to 5 entries (this replaces the earlier "Dig sites nearby" card). It reuses the Wikipedia geosearch the app already runs: no new request, no reverse geocoding, no widening to a city, district or division.

- **What counts.** A Wikipedia page is kept when its title or its first sentence says it is a museum, castle or fort, palace, place of worship (temple, mosque, abbey, cathedral, monastery), ruins or archaeological site, or a historic site (for example "Grade II* listed", "medieval", "is a historic church", war memorial). Pages that only mention a museum in a later sentence, such as a university hall that houses one, are not kept. The rules are in `VISIT_RULES` in `app/geo.py`.
- **Each entry** shows the name, the type, the straight-line distance and compass direction ("about 3 km north-east"), the first sentence of the Wikipedia introduction, the source name, a Wikipedia link, and an "Open in map" link (a plain OpenStreetMap URL with the place's coordinates; no routing service). The entries are marked on the Leaflet map with an original SVG marker.
- **No promises.** The card says "Distances are straight-line. Check access and opening times before you go." It never states opening hours, prices or that a place is open to visitors.
- **Empty state.** If nothing is found it shows exactly "No museums or historic sites recorded near you. Try dropping a pin in a city." and nothing else is put in the list.
- **Narration.** The model sees only each place's name, type and distance text, and may mention only those, exactly as written. With an empty list it must say nothing was recorded nearby. A code check discards a story that names a kind of place (museum, castle, temple...) that is not in the facts.
- **Overpass was tried and left out.** OpenStreetMap's Overpass API (`tourism=museum`, `historic=*`) was too slow and flaky in testing (timeouts and a 504 from three places, one answer after 12 s) and its results were mostly memorial plaques, so it is not used. No coordinates are sent to it.

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
node --test tests/logic.test.js   # the browser-logic tests on their own: walk-mode distance rule and passport storage
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

Stela does not store or log your location or IP address. The passport is stored only in your own browser and is never sent to the server. In Walk mode your position is sent to the server like any other lookup, but only when you have moved about 300 m. Logs contain only the route, status, timing and error types. Coordinates are rounded to 3 decimals (about 110 m) for the in-memory cache, which is lost on restart. The rate limiter keeps IPs in memory only. Your coordinates are sent to Macrostrat, PBDB, GBIF and Wikipedia to fetch the facts, as part of the request URL. They are also sent to a geocoding service, [OpenStreetMap Nominatim](https://operations.osmfoundation.org/policies/nominatim/): always for "People and past civilizations" (to get your place hierarchy: neighbourhood, city, district, region, country), and when nothing is recorded within 10 km for the nearest named place (and, for fossils, your country). Wikipedia then receives only the place names that came back (for example "Dover" or "Kent"), not your coordinates, but those names are of course close to where you are. That is one extra third party, only in that case. Stela spaces those requests to at most one per second, and caches the result in memory by rounded coordinates.

## Data sources and licenses

| Source | Used for | License |
|---|---|---|
| [Macrostrat](https://macrostrat.org) | rock unit, age | CC-BY 4.0 |
| [Paleobiology Database](https://paleobiodb.org) | fossil finds | CC-BY 4.0 |
| [GBIF](https://www.gbif.org) with IUCN Red List categories | rare animal records | CC0 / CC-BY per dataset |
| [Wikipedia](https://en.wikipedia.org) / MediaWiki API | nearby pages for "Places to visit" (museums and historic sites); history excerpts for "People and past civilizations" | text under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/): excerpts are shortened and attributed on the page, with links to the article and its authors |
| [OpenStreetMap Nominatim](https://nominatim.org) | place hierarchy (neighbourhood to country), nearest city/town and country | ODbL |
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
- **"People and past civilizations" makes about 8-13 Wikipedia requests per new place** (cached afterwards). Wikipedia answered some of them with HTTP 429 during testing from one IP with an anonymous User-Agent. Stela limits itself to 3 requests at once, spaces them out, and when some fail it shows what it has with a note ("Some Wikipedia lookups failed just now...") and does not cache the incomplete result. Setting `STELA_CONTACT` matters here.
- **Matching a place to an article can still go wrong.** Place names are ambiguous and Wikipedia has no exact "this place" lookup from Nominatim's hierarchy. Disambiguation pages and homonyms are rejected as described above, but a wrong or missing article is possible, especially for neighbourhoods, which are skipped when no confident match exists.
- **"Earliest period" is a heuristic.** It is the first real section of a "History of" article (which are chronological) or the first subsection of "History". The excerpt is only the first 2-3 sentences, so it can start mid-story or be dry.
- **Only English Wikipedia and English place names**, so places whose history is mostly documented in other languages show little or nothing.
- **Histories are contested and incomplete.** Wikipedia reflects what its editors have written, often from one tradition or perspective, and indigenous and pre-colonial history is frequently thin or missing. The card says so, and no indigenous-territory dataset is used.
- **Folklore detection is by explicit words only**, so legends told without those words appear as ordinary text, and a sentence that merely contains the word "legend" (a hotel name, a film) is listed as a tradition.
- **A small language model can still blend or paraphrase the excerpts.** In testing it once joined a Wisconsin fact and a United States fact into one sentence, and once wrote "here" for a wider place. The prompt now forbids both and the code check catches "here" and invented peoples, rulers and dates, but it cannot catch every paraphrase. The excerpts are always shown next to the story, so check them.
- **Wikipedia may rate-limit (HTTP 429)**, especially for anonymous or shared-IP clients. The card then says it could not be checked. Setting `STELA_CONTACT` helps.
- **Small language models can slip.** Strict prompts reduce invented details but cannot guarantee zero. In testing, a 4B local model sometimes turned scientific names into common names, and once said rare animals "wander" the area. A code check now discards any story that calls the animals alive or living here and shows the plain facts instead. That is why the plain fact cards are always shown under the story.
- PBDB can take 30 seconds or more for sparse regions.
- **"Oldest nearby site" uses dates read from text.** Wikipedia introductions have no structured dates, so Stela only reads a date that follows a construction or founding cue ("completed in 1262", "dates to 1575-1520 BC", "founded in the 11th century"). Bare years ("listed in 1973") are ignored, and the question is dropped unless one place is clearly older than the others. The dates may be when a place was built, not when it was first used, and many pages simply have no date, so this question is often unavailable.
- **The game's decoys are not proof of absence.** "Not in the records" only means not in the records Stela fetched (the most-recorded species, and the fossil groups of one search).
- **Places to visit are not visit advice.** They are Wikipedia pages, so some are buildings or monuments you cannot enter or that no longer exist. The type comes from keywords in the first sentence, so it can be wrong or missing. Directions and distances are straight lines from the point you looked up, and Wikipedia's coordinates for a place can be approximate.
- **A one-line summary is the first sentence of the introduction**, which Wikipedia sometimes cuts short (for example after "St.").
- **The passport is per browser and per device.** Clearing site data, using a private window, or switching phones starts a new one. There is no sync, by design.
- **Walk mode needs the page open and visible.** Browsers pause GPS for hidden pages and locked screens, so it cannot track you in the background. GPS can be slow or inaccurate indoors, under tree cover and in cities; weak fixes are ignored rather than guessed from.
- **"New rock unit" is best effort.** Names differ between map scales for the same ground, so a change of unit is only reported when both lookups used the same map scale; a change of era (time interval) is always reported. Geological maps are coarse, so boundaries you walk across may not match what is on the ground.
- **Walk mode and the rate limit.** Each new story is four requests (where, fossils, people, people/story; about 20 stories a minute are allowed). If the server says slow down, Walk mode pauses for a minute.

- **Dense cities hide sites.** The geosearch returns only the 50 nearest Wikipedia pages. In a dense modern district they can all be within about 2 km (in the Dhaka test they were), so older sites farther out, still inside 10 km, are never looked at. A place can therefore show few or no entries even when there are sites in the wider city.

## Planned next steps

- Widening "Places to visit" to the city, district and division when nothing is found nearby (with clear scope labels, like the other cards).
- A way past the 50-page cap in dense cities, if it can be done without many more requests.

## Out of scope for v1

Sky and star views, AR/VR, accounts, databases, a time slider, animal images or 3D models, and storing any user location.
