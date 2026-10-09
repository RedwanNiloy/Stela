"""Record the real HTTP conversations of the People and past civilizations layer (needs network), for offline tests.
Run:  python tests/capture_people.py [place ...]   (it waits between places: Wikipedia rate-limits bursts)

Each tests/samples/people_<place>.json is a list of {host, params, status, json}: Nominatim and MediaWiki replies exactly
as received, except that section HTML is cut after its first 8 paragraphs and introductions after 1,200 characters (the layer reads
only the first 8 paragraphs and the first sentences of an introduction)."""
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import geo, people  # noqa: E402

OUT = Path(__file__).parent / "samples"
MAX_EXTRACT = 1200
PACE = 1.0  # seconds before each Wikipedia request while recording
PLACES = {"dover": (51.13, 1.31), "dhaka": (23.81, 90.41), "madison": (43.07, -89.40)}


def trim_html(html, paragraphs=people.SECTION_PARAGRAPHS):
    end = -1
    for _ in range(paragraphs):
        end = html.find("</p>", end + 1)
        if end < 0:
            return html
    return html[: end + 4]


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner, self.entries = httpx.AsyncHTTPTransport(), []

    async def handle_async_request(self, request):
        if "wikipedia" in request.url.host:
            await asyncio.sleep(PACE)  # spread the requests out: bursts of 13 got HTTP 429 from Wikipedia
        resp = await self.inner.handle_async_request(request)
        body = await resp.aread()
        entry = {"host": request.url.host, "params": dict(request.url.params), "status": resp.status_code}
        try:
            data = json.loads(body)
            text = (((data.get("parse") or {}).get("text") or {}) if isinstance(data, dict) else {}).get("*")
            if text:
                data["parse"]["text"]["*"] = trim_html(text)
            for page in ((data.get("query") or {}).get("pages") or {}).values() if isinstance(data, dict) else []:
                if len(page.get("extract", "")) > MAX_EXTRACT:  # long introductions: keep the part the layer reads
                    page["extract"] = page["extract"][:MAX_EXTRACT]
            entry["json"] = data
        except ValueError:
            entry["text"] = body.decode("utf-8", "replace")
        self.entries.append(entry)
        return httpx.Response(resp.status_code, content=json.dumps(entry.get("json")).encode() if "json" in entry else body)


async def main():
    chosen = {n: PLACES[n] for n in (sys.argv[1:] or PLACES)}  # e.g. `python tests/capture_people.py dhaka`
    for i, (name, (lat, lng)) in enumerate(chosen.items()):
        if i:
            time.sleep(75)
        rec = Recorder()
        async with httpx.AsyncClient(transport=rec, headers=geo.UA) as client:
            facts, statuses = await people.fetch_people(client, lat, lng)
        bad = [e for e in rec.entries if e["status"] != 200]
        print(name, statuses, "calls:", len(rec.entries), "non-200:", [(e["host"], e["status"]) for e in bad])
        if bad or statuses.get("wikipedia") != "ok":
            print("  NOT saved (rate-limited or failed): run again later")
            continue
        (OUT / f"people_{name}.json").write_text(json.dumps(rec.entries, ensure_ascii=False, indent=1), encoding="utf-8")


asyncio.run(main())
