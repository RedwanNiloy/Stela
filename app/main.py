"""Stela API. Serves /api/where, /api/fossils, /health and the static page.

PRIVACY: this app never logs coordinates or IP addresses. It logs only the route
name, status code, duration and error *types*. Run uvicorn with --no-access-log
(its access log prints the full URL, including the coordinates).
"""
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import geo, narrate
from .cache import RateLimiter, TTLCache, cache_key

log = logging.getLogger("stela")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("httpx", "httpcore", "uvicorn.access"):  # these would print URLs containing coordinates
    logging.getLogger(noisy).setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").disabled = True

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

where_cache = TTLCache()    # fast facts + narration
fossil_cache = TTLCache()   # PBDB fossils + narration
limiter = RateLimiter(limit=40, window=60)  # 2 calls per lookup (/where + /fossils) = about 20 lookups a minute


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(headers=geo.UA, follow_redirects=True) as client:
        app.state.http = client
        yield


app = FastAPI(title="Stela", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)  # keep the page and JSON small on phones and the free tier


@app.middleware("http")
async def guard_and_log(request: Request, call_next):
    start = time.perf_counter()
    path = request.url.path  # path only, never the query string
    if path.startswith("/api/"):
        client_id = request.client.host if request.client else "unknown"  # used in memory only, never logged
        if not limiter.allow(client_id):
            log.info("%s 429 rate-limited", path)
            return JSONResponse({"detail": "Too many requests. Please wait a minute and try again."}, status_code=429)
    try:
        response = await call_next(request)
    except Exception as e:
        log.error("%s 500 %s", path, type(e).__name__)
        return JSONResponse({"detail": "Something went wrong. Please try again."}, status_code=500)
    if path != "/health":
        log.info("%s %s %.0fms", path, response.status_code, (time.perf_counter() - start) * 1000)
    return response


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


def source_list(statuses):
    return [{"id": k, **geo.SOURCES[k], "status": statuses.get(k, "n/a")} for k in geo.SOURCES]


@app.get("/api/where")
async def where(request: Request, lat: float = Query(ge=-90, le=90), lng: float = Query(ge=-180, le=180)):
    key = cache_key(lat, lng)
    hit = where_cache.get(key)
    if hit:
        return {**hit, "cached": True}

    client = request.app.state.http
    t0 = time.perf_counter()
    facts, statuses = await geo.fast_facts(client, lat, lng)
    text, mode = await narrate.narrate(client, facts, "main")
    statuses["pbdb"] = "pending"
    failed = [k for k, v in statuses.items() if v.startswith("failed")]
    log.info("where ok=%s failed=%s mode=%s %.0fms", not failed, failed, mode, (time.perf_counter() - t0) * 1000)

    result = {"facts": facts, "narration": text, "mode": mode, "sources": source_list(statuses)}
    if not failed and mode != "facts-only":  # never cache a half-broken answer
        where_cache.set(key, result)
    return {**result, "cached": False}


@app.get("/api/fossils")
async def fossils(request: Request, lat: float = Query(ge=-90, le=90), lng: float = Query(ge=-180, le=180)):
    """The slow part (PBDB). The page calls this after showing the fast results."""
    key = cache_key(lat, lng)
    hit = fossil_cache.get(key)
    if hit:
        return {**hit, "cached": True}

    client = request.app.state.http
    t0 = time.perf_counter()
    cached_where = where_cache.get(key)
    if cached_where:
        unit = cached_where["facts"]["rock"]
    else:
        unit, _ = await geo.safe(geo.fetch_rock(client, lat, lng))

    found, scope, status = await geo.slow_fossils(client, lat, lng, unit)
    # None = source unreachable, {} = reachable but nothing recorded (the narrator treats these differently)
    fossil_fact = None if status.startswith("failed") else (found or {})
    facts = {"rock": unit and unit.get("name"), "rock_began_forming": unit and unit.get("b_age") is not None and f"{unit['b_age']:g} million years ago",
             "ancient_setting": geo.setting(unit) if unit else None, "fossils_found_nearby": fossil_fact,
             "scope": {"fossils": scope}}
    text, mode = await narrate.narrate(client, facts, "fossils")
    log.info("fossils status=%s mode=%s %.0fms", status.split(":")[0], mode, (time.perf_counter() - t0) * 1000)

    result = {"fossils": fossil_fact, "scope": scope, "narration": text, "mode": mode,
              "source": {"id": "pbdb", **geo.SOURCES["pbdb"], "status": status}}
    if not status.startswith("failed") and mode != "facts-only":
        fossil_cache.set(key, result)
    return {**result, "cached": False}


# Must come last: it catches everything that is not an API route.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
