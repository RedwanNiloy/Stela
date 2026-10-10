"""People and past civilizations: what Wikipedia says about the early history of the user's place at five scopes
(your area, city, district, region, country). No LLM code in here.

How it works (all verified against the real services):
  1. Nominatim reverse geocoding (zoom 18) gives the place hierarchy in `address`.
  2. For each level we look for the English Wikipedia article "History of <place>"; if there is none we use the place's
     own article. Disambiguation pages are never used, and an unqualified own-article title must mention the place's
     surroundings, so "Madison" the disambiguation page or a homonym is rejected.
  3. With the MediaWiki API we read the article's introduction (action=query, prop=extracts) and its EARLIEST-PERIOD
     section (action=parse: the section list, then that one section's HTML).
  4. We keep at most 2 (intro) or 3 (early period) sentences per excerpt, with the article title, URL and scope level.
  5. Sentences that contain explicit folklore words ("legend", "myth", "according to tradition"...) are never put in the
     excerpts. They go to a separate "traditions" list, labelled "tradition, not verified history". Nothing is guessed.

Text is from Wikipedia, licensed CC BY-SA 4.0. Nothing is stored except an in-memory cache.
"""
import asyncio
import re
import time
from html.parser import HTMLParser
from urllib.parse import quote

from . import geo
from .cache import TTLCache

WIKI_SITE = "https://en.wikipedia.org"
LICENSE = {"name": "CC BY-SA 4.0", "url": "https://creativecommons.org/licenses/by-sa/4.0/"}
TRADITION_LABEL = "tradition, not verified history"

LEVELS = (("your_area", "your area"), ("city", "city"), ("district", "district"), ("region", "region"), ("country", "country"))
# Which Nominatim address keys feed which level (first match wins), and how many names we try per level.
ADDRESS_KEYS = {
    "your_area": ("neighbourhood", "quarter", "suburb"),
    "city": ("city", "town", "village", "municipality"),
    "district": ("county", "state_district"),
    "region": ("state", "region", "province"),
    "country": ("country",),
}
MAX_NAMES = {"your_area": 2, "city": 1, "district": 2, "region": 1, "country": 1}

INTRO_SENTENCES = 2
EARLY_SENTENCES = 3
MAX_EXCERPT_CHARS = 450
SECTION_PARAGRAPHS = 8      # only the first paragraphs of a section are read
BATCH = 20                  # titles per MediaWiki query (the extracts API serves 20 pages per call)
WIKI_TIMEOUT = 8.0
IMAGE_TOTAL = 6.0           # cap for the image licence lookup
PEOPLE_TOTAL = 25.0         # cap for the whole layer

MAX_PARALLEL = 3            # simultaneous Wikipedia requests (Wikimedia rate-limits anonymous bursts with HTTP 429)
WIKI_GAP = 0.3              # seconds between the starts of Wikipedia requests, to avoid a burst
_article_cache = TTLCache(ttl_seconds=24 * 3600, max_items=500)  # resolved title -> section details
_titles_cache = TTLCache(ttl_seconds=3600, max_items=200)        # tuple of titles -> MediaWiki reply


# ---------------------------------------------------------------- 1. the place hierarchy
def parse_hierarchy(address):
    """Nominatim `address` -> [{"level", "label", "names": [...], "parent": ...}] from your area up to the country.
    A name is used only at the most local level where it appears (Dover is both `town` and `city`)."""
    if not address:
        return []
    seen, out = set(), []
    for level, label in LEVELS:
        names = []
        for key in ADDRESS_KEYS[level]:
            n = (address.get(key) or "").strip()
            if n and n.lower() not in seen and n not in names and len(names) < MAX_NAMES[level]:
                names.append(n)
        seen.update(n.lower() for n in names)
        if names:
            out.append({"level": level, "label": label, "names": names})
    by_level = {h["level"]: h["names"][0] for h in out}
    for h in out:  # the next level up, used to tell apart places that share a name ("Madison, Wisconsin")
        h["parent"] = {"your_area": by_level.get("city"), "city": by_level.get("region") or by_level.get("country"),
                       "district": by_level.get("region") or by_level.get("country"), "region": by_level.get("country"),
                       "country": None}[h["level"]]
    return out


def candidate_titles(name, parent):
    """Article titles to try, best first, as (title, qualified). `qualified` titles name their parent."""
    cands = [(f"History of {name}", False)]
    if parent:
        cands += [(f"History of {name}, {parent}", True), (f"{name}, {parent}", True)]
    cands.append((name, False))
    return cands


# ---------------------------------------------------------------- 2. which article is it?
def index_pages(query):
    """MediaWiki query result -> a function title -> page dict (or None), following normalisation and redirects."""
    query = query or {}
    norm = {x["from"]: x["to"] for x in query.get("normalized", [])}
    redir = {x["from"]: x["to"] for x in query.get("redirects", [])}
    pages = {p["title"]: p for p in (query.get("pages") or {}).values()}
    def lookup(title):
        t = norm.get(title, title)
        t = redir.get(t, t)
        p = pages.get(t)
        return None if not p or "missing" in p else p
    return lookup


def page_ok(page):
    props = page.get("pageprops") or {}
    return bool(page.get("extract", "").strip()) and "disambiguation" not in props


def mentions_any(text, names):
    t = text.lower()
    return any(n and n.lower() in t for n in names)


def pick_article(entry, lookup, other_names):
    """The first usable article for a hierarchy level: (name, page, kind) with kind "history" or "place", or None.

    Places share names ("Tower Hamlets" is a suburb of Dover and a London borough), so an article is accepted only if
    its title names the parent place ("Madison, Wisconsin"), or it is an exact "History of <name>" article, or its text
    mentions the parent place. A redirect from an unqualified title to something else must mention the parent too."""
    mention_targets = [entry["parent"]] if entry["parent"] else other_names
    for name in entry["names"]:
        for title, qualified in candidate_titles(name, entry["parent"]):
            page = lookup(title)
            if not page or not page_ok(page):
                continue
            is_history = page["title"].startswith("History of ")
            exact_history = is_history and page["title"] == title
            if not (qualified or exact_history or entry["level"] == "country"
                    or mentions_any(page["title"] + " " + page["extract"], mention_targets)):
                continue
            return name, page, ("history" if is_history else "place")
    return None


# ---------------------------------------------------------------- 3. the earliest-period section
SKIP_HEADINGS = {"etymology", "name", "names", "toponymy", "geography", "see also", "references", "notes", "footnotes",
                 "external links", "further reading", "bibliography", "sources", "citations", "historiography", "contents",
                 "overview", "gallery", "demographics", "climate", "economy", "government"}


def choose_early_section(sections, kind):
    """Pick the section of the earliest period from MediaWiki's section list (or None).
    "History of ..." articles are chronological, so it is the first real top-level section (after things like
    "Etymology"); matching on words such as "early" picked "Early 20th century" in a real article.
    A place's own article keeps its history under "History": we take that section's first subsection."""
    if kind == "history":
        return next((s for s in sections if int(s["toclevel"]) == 1 and s["line"].strip().lower() not in SKIP_HEADINGS), None)
    for i, s in enumerate(sections):
        if s["line"].strip().lower() == "history" and int(s["toclevel"]) == 1:
            nxt = sections[i + 1] if i + 1 < len(sections) else None
            if nxt and int(nxt["toclevel"]) == 2:
                return nxt  # the first subsection under History is the earliest period
            return s
    return None


class _ParagraphReader(HTMLParser):
    """Collects the text of the <p> paragraphs of a MediaWiki HTML section. Skips citations, tables, hatnotes,
    captions and style blocks, so only running prose is read."""
    VOID = {"br", "img", "hr", "meta", "link", "input", "wbr", "source", "col"}
    SKIP_TAGS = {"style", "script", "sup", "table", "figure", "figcaption"}
    SKIP_CLASSES = ("hatnote", "thumb", "infobox", "navbox", "mw-editsection", "reference", "mw-heading", "metadata", "sidebar")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.skip, self.paragraphs, self._buf = [], 0, [], None

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        cls = (dict(attrs).get("class") or "").lower()
        skipping = tag in self.SKIP_TAGS or any(c in cls for c in self.SKIP_CLASSES)
        self.stack.append((tag, skipping))
        if skipping:
            self.skip += 1
        if tag == "p" and not self.skip:
            self._buf = []

    def handle_endtag(self, tag):
        while self.stack:
            t, skipping = self.stack.pop()
            if skipping:
                self.skip -= 1
            if t == tag:
                break
        if tag == "p" and self._buf is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            text = re.sub(r"\[\s*\d+\s*\]|\[[a-z]\]", "", text)
            if text:
                self.paragraphs.append(text)
            self._buf = None

    def handle_data(self, data):
        if self._buf is not None and not self.skip:
            self._buf.append(data)


def html_paragraphs(html, limit=SECTION_PARAGRAPHS):
    reader = _ParagraphReader()
    reader.feed(html or "")
    return reader.paragraphs[:limit]


# ---------------------------------------------------------------- 4. sentences and traditions
_ABBREV = re.compile(r"(?:^|\s)(?:St|Mt|Dr|Mr|Mrs|Ms|Jr|Sr|vs|c|ca|No|Fr|Rd|Co|Ltd|Inc|approx)\.$", re.I)


def split_sentences(text):
    t = re.sub(r"\s+", " ", text or "").strip()
    out, start = [], 0
    for m in re.finditer(r"[.!?](?=\s+[A-Z(\"'‘“])", t):
        head = t[start:m.end()]
        if _ABBREV.search(head) or re.search(r"(?:^|\s)[A-Z]\.$", head):
            continue
        out.append(head.strip())
        start = m.end()
    if t[start:].strip():
        out.append(t[start:].strip())
    return out


# Explicit folklore words only. "traditionally" is NOT here: it is used for plain facts ("traditionally a fishing town").
TRADITION_RE = re.compile(
    r"(?<!\w)(legends?|legendary|myths?|mythical|mythology|mythological|folklore|folk ?tales?|oral tradition|"
    r"according to (?:\w+ )?(?:tradition|legend|myth|folklore)|tradition (?:holds|says|states|has it)|"
    r"(?:is|are|was|were) said to|said to have)(?!\w)", re.I)


def is_tradition(sentence):
    return bool(TRADITION_RE.search(sentence))


def take_sentences(sentences, n, max_chars=MAX_EXCERPT_CHARS):
    """The first n non-tradition sentences, shortened to max_chars at a sentence boundary (or a word if one is huge)."""
    picked, size = [], 0
    for s in sentences:
        if is_tradition(s):
            continue
        if picked and size + len(s) + 1 > max_chars:
            break
        picked.append(s if len(s) <= max_chars else s[:max_chars - 1].rsplit(" ", 1)[0] + "…")
        size += len(s) + 1
        if len(picked) == n:
            break
    return " ".join(picked)


def tradition_items(sentences, scope, article, limit=2):
    out = []
    for s in sentences:
        m = TRADITION_RE.search(s)
        if m and len(out) < limit:
            text = s if len(s) <= 320 else s[:319].rsplit(" ", 1)[0] + "…"
            out.append({"text": text, "trigger": m.group(0).lower(), "label": TRADITION_LABEL, "scope": scope, "article": article})
    return out


def wiki_url(title, anchor=None):
    url = WIKI_SITE + "/wiki/" + quote(title.replace(" ", "_"), safe="_,()'!*~:")
    return url + ("#" + quote(anchor, safe="_.-()") if anchor else "")


def history_url(title):
    return f"{WIKI_SITE}/w/index.php?title={quote(title.replace(' ', '_'), safe='_,()')}&action=history"


def build_excerpt(entry, name, page, kind, details):
    """One excerpt (and its traditions) from the introduction and the earliest-period section."""
    scope = {"level": entry["level"], "label": entry["label"], "place_name": name}
    title = page["title"]
    article = {"title": title, "url": wiki_url(title), "history_url": history_url(title), "kind": kind}
    intro_sents = split_sentences(page.get("extract", ""))
    early = None
    early_sents = []
    if details and details.get("section"):
        early_sents = split_sentences(" ".join(details["paragraphs"]))
        text = take_sentences(early_sents, EARLY_SENTENCES)
        if text:
            sec = details["section"]
            early = {"section": sec["line"], "url": wiki_url(title, sec.get("anchor")), "text": text}
    excerpt = {"scope": scope, "article": article, "intro": take_sentences(intro_sents, INTRO_SENTENCES) or None,
               "early": early, "license": LICENSE}
    traditions = tradition_items(intro_sents + early_sents, scope, article)
    return excerpt, traditions


# ---------------------------------------------------------------- fetching
async def fetch_titles(client, titles):
    """Intro extracts, disambiguation flags and redirects for up to 20 titles in one MediaWiki call."""
    key = tuple(titles)
    hit = _titles_cache.get(key)
    if hit:
        return hit["value"]
    value = await geo.get_json(client, geo.WIKI, WIKI_TIMEOUT, action="query", titles="|".join(titles), prop="extracts|pageprops|pageimages",
                               exintro=1, explaintext=1, exlimit="max", redirects=1, format="json",
                               piprop="thumbnail|name", pithumbsize=geo.IMAGE_THUMB_PX, pilimit="max")
    _titles_cache.set(key, {"value": value})
    return value


async def fetch_details(client, title, kind):
    """The earliest-period section of one article: {"section", "paragraphs"} (cached by title)."""
    hit = _article_cache.get(title)
    if hit:
        return hit["value"]
    data = await geo.get_json(client, geo.WIKI, WIKI_TIMEOUT, action="parse", page=title, prop="sections", redirects=1, format="json")
    sections = ((data or {}).get("parse") or {}).get("sections") or []
    section = choose_early_section(sections, kind)
    paragraphs = []
    if section:
        sec = await geo.get_json(client, geo.WIKI, WIKI_TIMEOUT, action="parse", page=title, section=section["index"], prop="text",
                                 disabletoc=1, disableeditsection=1, redirects=1, format="json")
        paragraphs = html_paragraphs((((sec or {}).get("parse") or {}).get("text") or {}).get("*", ""))
    value = {"section": section, "paragraphs": paragraphs}
    _article_cache.set(title, {"value": value})
    return value


def empty_facts():
    return {"early_history": [], "traditions": [], "license": LICENSE, "incomplete": False}


async def fetch_people(client, lat, lng):
    """Returns (facts, statuses). facts["early_history"] is None only when nothing could be looked up at all.
    One failing level or article never breaks the others."""
    address, s_geo = await geo.safe(geo.reverse_address(client, lat, lng))
    statuses = {"nominatim": geo.source_status(address, s_geo)}
    if s_geo != "ok":
        return {"early_history": None, "traditions": [], "license": LICENSE, "incomplete": True}, {**statuses, "wikipedia": "not needed"}
    hierarchy = parse_hierarchy(address)
    if not hierarchy:
        return empty_facts(), {**statuses, "wikipedia": "not needed"}

    all_names = [n for h in hierarchy for n in h["names"]]
    titles = []
    for h in hierarchy:
        for name in h["names"]:
            titles += [t for t, _ in candidate_titles(name, h["parent"]) if t not in titles]
    sem = asyncio.Semaphore(MAX_PARALLEL)

    last_start = [0.0]

    async def limited(coro):  # at most MAX_PARALLEL requests at once, started WIKI_GAP apart; an article's two calls share a permit
        async with sem:
            wait = last_start[0] + WIKI_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            last_start[0] = time.monotonic()
            return await coro
    batches = await asyncio.gather(*(geo.safe(limited(fetch_titles(client, titles[i:i + BATCH]))) for i in range(0, len(titles), BATCH)))
    ok = [b for b, st in batches if st == "ok"]
    if not ok:
        return {"early_history": None, "traditions": [], "license": LICENSE, "incomplete": True}, {**statuses, "wikipedia": batches[0][1]}
    lookups = [index_pages((b or {}).get("query")) for b in ok]
    lookup = lambda title: next((p for p in (f(title) for f in lookups) if p), None)

    chosen, used = [], set()
    for h in hierarchy:
        found = pick_article(h, lookup, [n for n in all_names if n not in h["names"]])
        if found and found[1]["title"] not in used:  # the same article never appears at two levels
            used.add(found[1]["title"])
            chosen.append((h, *found))
    details = await asyncio.gather(*(geo.safe(limited(fetch_details(client, page["title"], kind))) for _, _, page, kind in chosen))

    # The article's lead image (already in the titles reply) plus its licence from Commons: one extra call, and a failure
    # only means the excerpts show no picture.
    images, _ = await geo.safe(asyncio.wait_for(geo.images_for_pages(client, [page for _, _, page, _ in chosen]), IMAGE_TOTAL))
    images = images or [None] * len(chosen)

    excerpts, traditions = [], []
    for (h, name, page, kind), (det, _), image in zip(chosen, details, images):
        ex, tr = build_excerpt(h, name, page, kind, det)  # a failed section lookup still gives the introduction
        ex["image"] = image   # shown on the page only; llm_view never passes it to the model
        if ex["intro"] or ex["early"]:
            excerpts.append(ex)
        traditions += tr
    failed_batches = len(batches) - len(ok)
    # incomplete = some lookups failed (typically HTTP 429), so levels or early-period sections may be missing.
    # An incomplete result is shown with a note and never cached, so the next lookup can fill the gaps.
    incomplete = bool(failed_batches) or any(st != "ok" for _, st in details)
    statuses["wikipedia"] = "ok" if excerpts else "empty"
    if failed_batches and not excerpts:
        statuses["wikipedia"] = batches[0][1]
    return {"early_history": excerpts, "traditions": traditions[:6], "license": LICENSE, "incomplete": incomplete}, statuses
