"""Two tiny in-memory helpers: a TTL cache and a per-IP rate limiter.

Nothing here is written to disk and nothing is logged. Coordinates are rounded to
3 decimals (about 110 m) before they become cache keys, so the cache never holds
a more precise location than that.
"""
import time
from collections import deque


def cache_key(lat, lng):
    return (round(lat, 3), round(lng, 3))


class TTLCache:
    def __init__(self, ttl_seconds=6 * 3600, max_items=2000):
        self.ttl, self.max_items = ttl_seconds, max_items
        self._data = {}  # key -> (expires_at, value)

    def get(self, key):
        item = self._data.get(key)
        if not item:
            return None
        if item[0] < time.monotonic():
            del self._data[key]
            return None
        return item[1]

    def set(self, key, value):
        if len(self._data) >= self.max_items:  # drop the oldest entry (dicts keep insertion order)
            self._data.pop(next(iter(self._data)))
        self._data[key] = (time.monotonic() + self.ttl, value)


class RateLimiter:
    """Allow `limit` requests per `window` seconds for each client."""

    def __init__(self, limit=20, window=60.0):
        self.limit, self.window = limit, window
        self._hits = {}  # client id -> deque of timestamps (kept in memory only)

    def allow(self, client_id):
        now = time.monotonic()
        hits = self._hits.setdefault(client_id, deque())
        while hits and hits[0] < now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 10_000:  # forget idle clients so memory stays bounded
            for k in [k for k, v in self._hits.items() if not v or v[-1] < now - self.window]:
                del self._hits[k]
        return True
