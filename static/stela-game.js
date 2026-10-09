/* Stela: "Guess the past" and "Dig sites nearby" logic. Pure functions, no page access, tested in Node.
 *
 * GROUNDING: every question and every answer is computed from the facts the server collected (rock, setting, fossils,
 * rare-animal records, human-history pages). Nothing here comes from a language model. The only invented things are
 * wrong answer options ("decoys"), which come from fixed lists, are never in the data, and are never shown as facts. */
(function (root) {
  "use strict";

  const NO_TABLETS = "The scribe has no tablets for this spot. Try walking somewhere new or dropping a pin elsewhere.";
  const FAR_M = 10000;  // beyond this a site is "far": not presented as a walk
  const DIG_NOTE = "Check access and opening times before you go.";

  /* ---------- small helpers ---------- */
  function shuffle(list, rng = Math.random) {
    const a = list.slice();
    for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(rng() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
    return a;
  }
  const pick = (list, rng) => list[Math.floor(rng() * list.length)];

  /* ---------- distance wording and compass direction ---------- */
  const COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];

  function bearingDegrees(lat1, lng1, lat2, lng2) {  // initial bearing from point 1 to point 2, 0-360, 0 = north
    const rad = (d) => (d * Math.PI) / 180, deg = (r) => (r * 180) / Math.PI;
    const dLng = rad(lng2 - lng1);
    const y = Math.sin(dLng) * Math.cos(rad(lat2));
    const x = Math.cos(rad(lat1)) * Math.sin(rad(lat2)) - Math.sin(rad(lat1)) * Math.cos(rad(lat2)) * Math.cos(dLng);
    return (deg(Math.atan2(y, x)) + 360) % 360;
  }

  function compassDirection(lat1, lng1, lat2, lng2) {
    if (![lat1, lng1, lat2, lng2].every(Number.isFinite)) return null;
    const bearing = bearingDegrees(lat1, lng1, lat2, lng2);
    return { bearing: Math.round(bearing), name: COMPASS[Math.round(bearing / 45) % 8] };
  }

  /* Honest distance: always "straight line", never a walking time. Over about 10 km it says it is far. */
  function describeDistance(meters) {
    if (!Number.isFinite(meters) || meters < 0) return { text: "distance not known", far: false };
    if (meters < 1000) {
      const m = meters < 100 ? Math.round(meters) : Math.round(meters / 10) * 10;
      return { text: `about ${m} m away, straight line`, far: false };
    }
    const km = meters / 1000;
    const shown = km < 10 ? String(Math.round(km * 10) / 10) : String(Math.round(km));
    if (meters > FAR_M) return { text: `about ${shown} km away, straight line. That is far: plan transport rather than a walk`, far: true };
    return { text: `about ${shown} km away, straight line`, far: false };
  }

  /* One-line summary: the first sentence of the Wikipedia introduction (not splitting after "St." and similar). */
  const ABBREVIATION = /(?:^|\s)(?:St|Mt|Dr|Mr|Mrs|Ms|Jr|Sr|vs|c|ca|No|Fr|Rd)\.$/i;
  function firstSentence(text, max = 180) {
    const t = String(text || "").replace(/\s+/g, " ").trim();
    if (!t) return "";
    const re = /[.!?](?=\s+[A-Z("'])/g;
    let end = -1, m;
    while ((m = re.exec(t))) {
      const head = t.slice(0, m.index + 1);
      if (ABBREVIATION.test(head) || /(?:^|\s)[A-Z]\.$/.test(head)) continue;
      end = m.index + 1;
      break;
    }
    let s = end > 0 ? t.slice(0, end) : t;
    if (s.length > max) s = s.slice(0, max - 1).replace(/\s+\S*$/, "") + "…";
    return s;
  }

  /* ---------- dates found in the data (for "oldest nearby site") ----------
   * Wikipedia introductions have no structured dates, so we read them conservatively: a date only counts when it follows
   * a construction/founding cue ("completed in 1262", "built from 1867 to 1868", "dates to 1575-1520 BC", "founded in the
   * 11th century"). A bare year ("listed in 1973", "2020 census") is never used. Returns { lo, hi, text } in calendar
   * years (BC is negative), or null. */
  const CUES = /\b(?:built|constructed|completed|founded|established|erected|opened|consecrated|begun|began|dates? (?:back )?to|dated (?:back )?to|dating (?:back )?(?:from|to)|originat\w* (?:in|from))\b/gi;

  function parseDateAfterCue(rest) {
    const s = rest.slice(0, 60);
    let m = s.match(/^[^.;,\d]{0,25}?(\d{1,2})(?:st|nd|rd|th)[\s-]century(?:\s*(BC|BCE|AD|CE))?/i);
    if (m) {
      const n = +m[1];
      if (n < 1 || n > 21) return null;
      const bc = /^bce?$/i.test(m[2] || "");
      return { ...(bc ? { lo: -n * 100, hi: -(n - 1) * 100 - 1 } : { lo: (n - 1) * 100 + 1, hi: n * 100 }), len: m[0].length };
    }
    m = s.match(/^[^.;,\d]{0,25}?(\d{3,4})(?![\d,]\d)(?:\s*(?:–|—|-|to)\s*(\d{3,4})(?![\d,]\d))?(?:\s*(BC|BCE|AD|CE))?/i);
    if (!m) return null;
    const bc = /^bce?$/i.test(m[3] || "");
    let a = +m[1], b = m[2] ? +m[2] : a;
    if (bc) { a = -a; b = -b; }
    if (Math.abs(a) < 1 || Math.abs(b) < 1 || a > 2100 || b > 2100) return null;
    return { lo: Math.min(a, b), hi: Math.max(a, b), len: m[0].length };
  }

  function extractDate(text) {
    const t = String(text || "");
    let best = null, m;
    CUES.lastIndex = 0;
    while ((m = CUES.exec(t))) {
      const after = t.slice(m.index + m[0].length);
      const d = parseDateAfterCue(after);
      if (!d) continue;
      const snippet = (m[0] + after.slice(0, d.len)).replace(/\s+/g, " ").trim();  // just the cue and the date, e.g. "completed in 1262"
      if (!best || d.lo < best.lo) best = { lo: d.lo, hi: d.hi, text: snippet };
    }
    return best;
  }

  /* ---------- Guess the past: which questions are possible? ---------- */
  const SETTING_CHOICES = [
    { id: "sea", label: "A sea or ocean floor" },
    { id: "swamp", label: "A swamp or coastal forest" },
    { id: "desert", label: "A desert or dry salt flats" },
    { id: "river", label: "Rivers, floodplains or beaches" },
  ];
  // ancient_setting.type from the server -> the option that is the answer. Igneous/unknown rock gives no question.
  const SETTING_ANSWER = { marine: "sea", swamp: "swamp", arid: "desert", "land/river": "river" };

  // Wrong answers only. Fixed lists; anything that is in the real data is removed before use.
  const FOSSIL_DECOYS = ["Trilobita", "Cephalopoda", "Bivalvia", "Gastropoda", "Rhynchonellata", "Anthozoa", "Crinoidea",
    "Echinoidea", "Mammalia", "Reptilia", "Actinopterygii", "Chondrichthyes", "Insecta", "Malacostraca", "Ostracoda", "Graptolithina"];
  const SPECIES_DECOYS = ["Ailuropoda melanoleuca", "Gorilla gorilla", "Panthera tigris", "Diceros bicornis", "Ursus maritimus",
    "Elephas maximus", "Pongo pygmaeus", "Acinonyx jubatus", "Spheniscus demersus", "Phocoena sinus", "Ara glaucogularis", "Gymnogyps californianus"];

  const finite = Number.isFinite;
  const hasRock = (f) => !!f && !!f.rock;

  /* Human-history pages with a usable construction/founding date, and a clear oldest one (strictly older than every
   * option we keep). Options that overlap the oldest in time are dropped, because the order would not be certain. */
  function datedSites(facts) {
    const list = (facts && facts.human_history_nearby) || [];
    const dated = list.map((e) => ({ e, d: extractDate(e.summary) })).filter((x) => x.d);
    if (dated.length < 2) return null;
    dated.sort((a, b) => a.d.lo - b.d.lo);
    const oldest = dated[0];
    const younger = dated.slice(1).filter((x) => oldest.d.hi < x.d.lo);
    if (younger.length < 1) return null;
    return { oldest, younger };
  }

  function possibleQuestionTypes(facts, fossilData) {
    const types = [];
    if (hasRock(facts) && facts.ancient_setting && SETTING_ANSWER[facts.ancient_setting.type]) types.push("setting");
    if (hasRock(facts) && finite(facts.rock.b_age) && finite(facts.rock.t_age)) types.push("age");
    const fossils = fossilData && fossilData.fossils;
    if (fossils && fossils.count > 0 && Array.isArray(fossils.classes) && fossils.classes.length >= 1) types.push("fossil");
    if (datedSites(facts)) types.push("oldest");
    const rare = facts && facts.rare_animals_recorded_nearby;
    if (Array.isArray(rare) && rare.length >= 1) types.push("species");
    return types;
  }

  const scopeOf = (facts, layer) => (facts && facts.scope && facts.scope[layer]) || null;

  function buildQuestion(type, facts, fossilData, rng = Math.random) {
    if (!possibleQuestionTypes(facts, fossilData).includes(type)) return null;

    if (type === "setting") {
      return { type, layer: "rock", scope: scopeOf(facts, "rock"),
        prompt: "When this rock formed, what was this place probably like?",
        note: "This is a guess from the rock type, so the answer is only ever \"probably\".",
        options: shuffle(SETTING_CHOICES, rng), answerId: SETTING_ANSWER[facts.ancient_setting.type],
        reveal: { rock: facts.rock, setting: facts.ancient_setting } };
    }

    if (type === "age") {
      return { type, layer: "rock", scope: scopeOf(facts, "rock"),
        prompt: "How old is the rock under this spot? Slide to your guess, in millions of years ago.",
        note: null, options: null, answerId: null,
        range: { b_age: facts.rock.b_age, t_age: facts.rock.t_age },
        reveal: { rock: facts.rock, ruler: facts.time_ruler || null } };
    }

    if (type === "fossil") {
      const f = fossilData.fossils;
      const real = pick(f.classes, rng);                      // [name, count]: a real group from the data
      const taken = new Set(f.classes.map(([n]) => String(n).toLowerCase()));
      const decoys = shuffle(FOSSIL_DECOYS.filter((d) => !taken.has(d.toLowerCase())), rng).slice(0, 3);
      const options = shuffle([{ id: "real", label: real[0] }, ...decoys.map((d) => ({ id: "decoy:" + d, label: d }))], rng);
      return { type, layer: "fossils", scope: fossilData.scope || null,
        prompt: "Which of these animal groups appears in the fossil finds near here?",
        note: "Fossil records show only what has been found, so a group that is not listed here may still have lived nearby.",
        options, answerId: "real", reveal: { group: real[0], count: real[1], fossils: f } };
    }

    if (type === "oldest") {
      const { oldest, younger } = datedSites(facts);
      const others = shuffle(younger, rng).slice(0, 3);
      const mk = (x) => ({ id: x.e.place, label: x.e.place, dateText: x.d.text, summary: firstSentence(x.e.summary, 160) });
      const sites = [oldest, ...others].map(mk);
      return { type, layer: "human", scope: scopeOf(facts, "human"),
        prompt: "Which of these places nearby is the oldest?",
        note: "Judged from the construction or founding dates in each place's Wikipedia introduction. Those dates may not be when the place was first used.",
        options: shuffle(sites.map((s) => ({ id: s.id, label: s.label })), rng), answerId: oldest.e.place,
        reveal: { sites, oldestId: oldest.e.place } };
    }

    // species: "recorded nearby", never "alive"
    const rare = facts.rare_animals_recorded_nearby;
    const real = pick(rare, rng);
    const taken = new Set(rare.map((r) => String(r.species).toLowerCase()));
    const decoys = shuffle(SPECIES_DECOYS.filter((d) => !taken.has(d.toLowerCase())), rng).slice(0, 3);
    return { type, layer: "rare", scope: scopeOf(facts, "rare"),
      prompt: "Which of these species appears in the rare-animal records near here?",
      note: facts.rare_animals_note || null,
      options: shuffle([{ id: "real", label: real.species }, ...decoys.map((d) => ({ id: "decoy:" + d, label: d }))], rng),
      answerId: "real", reveal: { entry: real, listed: rare.map((r) => r.species) } };
  }

  /* One random question of a possible type (not one of excludeTypes), or null. */
  function pickQuestion(facts, fossilData, rng = Math.random, excludeTypes = []) {
    const types = possibleQuestionTypes(facts, fossilData).filter((t) => !excludeTypes.includes(t));
    return types.length ? buildQuestion(pick(types, rng), facts, fossilData, rng) : null;
  }

  /* One or two random questions of different types, all from the facts. [] when nothing is possible. */
  function pickQuestions(facts, fossilData, rng = Math.random) {
    const types = shuffle(possibleQuestionTypes(facts, fossilData), rng);
    if (!types.length) return [];
    const count = types.length > 1 && rng() < 0.5 ? 2 : 1;
    return types.slice(0, count).map((t) => buildQuestion(t, facts, fossilData, rng));
  }

  /* ---------- age slider ---------- */
  const SLIDER_MAX_MA = 4600;  // roughly the age of the Earth
  const sliderToMa = (v) => {  // cubic, so young ages are easy to pick; v is 0..1000
    const ma = SLIDER_MAX_MA * Math.pow(Math.max(0, Math.min(1000, v)) / 1000, 3);
    return ma < 10 ? Math.round(ma * 10) / 10 : ma < 100 ? Math.round(ma) : Math.round(ma / 5) * 5;
  };
  const maToSlider = (ma) => Math.round(1000 * Math.cbrt(Math.max(0, Math.min(SLIDER_MAX_MA, ma)) / SLIDER_MAX_MA));

  /* inside the rock's age range / close (within a quarter of its age) / far */
  function gradeAge(guessMa, range) {
    const { b_age: b, t_age: t } = range;
    if (guessMa >= t && guessMa <= b) return { verdict: "inside", diff: 0 };
    const diff = guessMa > b ? guessMa - b : t - guessMa;
    return { verdict: diff <= 0.25 * b ? "close" : "far", diff };
  }

  /* ---------- Dig sites nearby ----------
   * Reuses the human-history results. Individual sites only when that layer searched the user's own area; if it fell
   * back to a nearest city or region there are no individual sites, and we say what the history is about instead. */
  const mapUrl = (lat, lng) => `https://www.openstreetmap.org/?mlat=${lat.toFixed(5)}&mlon=${lng.toFixed(5)}#map=17/${lat.toFixed(5)}/${lng.toFixed(5)}`;
  const wikiUrl = (e) => finite(e.pageid) ? `https://en.wikipedia.org/?curid=${e.pageid}`
    : `https://en.wikipedia.org/wiki/Special:Search?search=${encodeURIComponent(e.place)}`;

  function digSites(facts, user) {
    const entries = facts && facts.human_history_nearby;
    if (entries == null) return { mode: "unavailable", sites: [], scope: null };
    const scope = (facts.scope && facts.scope.human) || null;
    if (scope && (scope.level === "nearest_place" || scope.level === "region")) {
      return { mode: "wider", sites: [], scope, placeName: scope.place_name || (entries[0] && entries[0].place) || null };
    }
    const sites = entries
      .filter((e) => finite(e.lat) && finite(e.lng))
      .map((e) => {
        const dist = describeDistance(e.distance_m);
        return { name: e.place, distance_m: e.distance_m, distanceText: dist.text, far: dist.far, lat: e.lat, lng: e.lng,
          direction: user && finite(user.lat) && finite(user.lng) ? compassDirection(user.lat, user.lng, e.lat, e.lng) : null,
          summary: firstSentence(e.summary, 180), wikiUrl: wikiUrl(e), mapUrl: mapUrl(e.lat, e.lng) };
      })
      .sort((a, b) => (a.distance_m ?? Infinity) - (b.distance_m ?? Infinity))
      .slice(0, 5);
    return { mode: sites.length ? "sites" : "none", sites, scope };
  }

  const api = { NO_TABLETS, FAR_M, DIG_NOTE, SETTING_CHOICES, SETTING_ANSWER, FOSSIL_DECOYS, SPECIES_DECOYS, SLIDER_MAX_MA,
    shuffle, bearingDegrees, compassDirection, describeDistance, firstSentence, extractDate,
    possibleQuestionTypes, buildQuestion, pickQuestion, pickQuestions, sliderToMa, maToSlider, gradeAge, digSites, mapUrl, wikiUrl };
  root.StelaGame = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
