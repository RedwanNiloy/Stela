// Tests for static/stela-game.js: Guess the past and Dig sites nearby. Run: node --test tests/game.test.js
// Fixtures tests/samples/api_*.json are real /api/where + /api/fossils responses (Dover, Madison, Dhaka).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const G = require("../static/stela-game.js");

const load = (n) => require(`./samples/api_${n}.json`);
const DOVER = load("dover"), MADISON = load("madison"), DHAKA = load("dhaka");
const FULL = DOVER.where.facts, FOSSILS = DOVER.fossils;

// a tiny seeded random generator so tests are repeatable
const seeded = (seed) => () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
const clone = (x) => JSON.parse(JSON.stringify(x));
const EMPTY = { rock: null, ancient_setting: null, time_ruler: null, rare_animals_recorded_nearby: [], human_history_nearby: [], scope: {} };

// ---------------------------------------------------------------- which questions are possible
test("full data: every question type is possible (real Dover responses)", () => {
  assert.deepEqual(G.possibleQuestionTypes(FULL, FOSSILS).sort(), ["age", "fossil", "oldest", "setting", "species"]);
});

test("rock only: setting and age, nothing else", () => {
  const facts = { ...EMPTY, rock: FULL.rock, ancient_setting: FULL.ancient_setting, time_ruler: FULL.time_ruler };
  assert.deepEqual(G.possibleQuestionTypes(facts, undefined), ["setting", "age"]);
});

test("history only: just the oldest-site question", () => {
  const facts = { ...EMPTY, human_history_nearby: FULL.human_history_nearby };
  assert.deepEqual(G.possibleQuestionTypes(facts, undefined), ["oldest"]);
});

test("empty facts: nothing is possible", () => {
  assert.deepEqual(G.possibleQuestionTypes(EMPTY, undefined), []);
  assert.deepEqual(G.possibleQuestionTypes(null, undefined), []);
  assert.deepEqual(G.pickQuestions(EMPTY, undefined), []);
  assert.equal(G.pickQuestion(EMPTY, undefined), null);
});

test("the exact no-tablets message", () => {
  assert.equal(G.NO_TABLETS, "The scribe has no tablets for this spot. Try walking somewhere new or dropping a pin elsewhere.");
});

test("fossil question needs loaded fossil records with at least one group", () => {
  assert.equal(G.possibleQuestionTypes(FULL, undefined).includes("fossil"), false);                       // still loading
  assert.equal(G.possibleQuestionTypes(FULL, { fossils: null, scope: null }).includes("fossil"), false);   // source failed
  assert.equal(G.possibleQuestionTypes(FULL, { fossils: {}, scope: null }).includes("fossil"), false);     // nothing recorded
  assert.equal(G.possibleQuestionTypes(FULL, { fossils: { count: 3, classes: [] } }).includes("fossil"), false);
});

test("setting question needs a rock type that maps to sea / swamp / desert / river", () => {
  const igneous = { ...FULL, ancient_setting: { type: "igneous/metamorphic", meaning: "volcanoes" } };
  assert.equal(G.possibleQuestionTypes(igneous, undefined).includes("setting"), false);
  const unknown = { ...FULL, ancient_setting: { type: "unknown", meaning: null } };
  assert.equal(G.possibleQuestionTypes(unknown, undefined).includes("setting"), false);
  for (const type of Object.keys(G.SETTING_ANSWER)) {
    assert.equal(G.possibleQuestionTypes({ ...FULL, ancient_setting: { type, meaning: "x" } }, undefined).includes("setting"), true, type);
  }
});

test("age question needs both ends of the age range", () => {
  assert.equal(G.possibleQuestionTypes({ ...FULL, rock: { ...FULL.rock, b_age: null } }, undefined).includes("age"), false);
  assert.equal(G.possibleQuestionTypes({ ...FULL, rock: { ...FULL.rock, t_age: null } }, undefined).includes("age"), false);
  assert.equal(G.possibleQuestionTypes({ ...FULL, rock: { ...FULL.rock, t_age: 0 } }, undefined).includes("age"), true);  // 0 is a real value
});

test("oldest-site question needs 2+ dated pages with a clear oldest", () => {
  const entry = (place, summary) => ({ place, summary, distance_m: 100, lat: 1, lng: 1, pageid: 1 });
  const only = (list) => G.possibleQuestionTypes({ ...EMPTY, human_history_nearby: list }, undefined).includes("oldest");
  assert.equal(only([entry("A", "Built in 1200."), entry("B", "A pleasant place.")]), false);                // one dated page
  assert.equal(only([entry("A", "Built in 1200."), entry("B", "Completed in 1500.")]), true);
  assert.equal(only([entry("A", "Built in 1200."), entry("B", "Built in 1200.")]), false);                    // tie: order not certain
  assert.equal(only([entry("A", "Founded in the 13th century."), entry("B", "Built in 1262.")]), false);     // overlapping
  assert.equal(only([entry("A", "It was listed in 1973."), entry("B", "It was listed in 1980.")]), false);   // no construction cue
});

test("rare-animal question needs at least one record; a failed source (null) gives none", () => {
  assert.equal(G.possibleQuestionTypes({ ...FULL, rare_animals_recorded_nearby: null }, undefined).includes("species"), false);
  assert.equal(G.possibleQuestionTypes({ ...FULL, rare_animals_recorded_nearby: [] }, undefined).includes("species"), false);
  assert.equal(G.possibleQuestionTypes(DHAKA.where.facts, undefined).includes("species"), true);
});

test("real places: Dover has everything, Madison has no rare-animal question (GBIF failed), Dhaka only species", () => {
  assert.deepEqual(G.possibleQuestionTypes(MADISON.where.facts, MADISON.fossils).sort(), ["age", "fossil", "setting"]);
  assert.deepEqual(G.possibleQuestionTypes(DHAKA.where.facts, DHAKA.fossils), ["species"]);
});

// ---------------------------------------------------------------- building questions: answers come only from the facts
test("setting question: the answer follows the rock type and the wording keeps 'probably'", () => {
  const q = G.buildQuestion("setting", FULL, undefined, seeded(1));
  assert.equal(q.answerId, "sea");                                  // chalk -> marine
  assert.equal(q.options.length, 4);
  assert.ok(/probably/i.test(q.prompt) && /probably/i.test(q.note));
  assert.deepEqual(q.options.map((o) => o.id).sort(), ["desert", "river", "sea", "swamp"]);
  const swamp = G.buildQuestion("setting", { ...FULL, ancient_setting: { type: "swamp", meaning: "a swamp" } }, undefined, seeded(2));
  assert.equal(swamp.answerId, "swamp");
});

test("age question carries the real range and grades guesses", () => {
  const q = G.buildQuestion("age", FULL, undefined, seeded(1));
  assert.deepEqual(q.range, { b_age: FULL.rock.b_age, t_age: FULL.rock.t_age });
  assert.equal(q.options, null);
  assert.equal(G.gradeAge(80, q.range).verdict, "inside");
  assert.equal(G.gradeAge(100.5, q.range).verdict, "inside");
  assert.equal(G.gradeAge(66, q.range).verdict, "inside");
  assert.equal(G.gradeAge(120, q.range).verdict, "close");
  assert.equal(G.gradeAge(2000, q.range).verdict, "far");
  assert.equal(G.gradeAge(2, q.range).verdict, "far");
});

test("fossil question: one real group from the data, decoys never in the data, decoys never marked as the answer", () => {
  for (let seed = 1; seed <= 25; seed++) {
    const q = G.buildQuestion("fossil", FULL, FOSSILS, seeded(seed));
    const real = q.options.find((o) => o.id === "real");
    const dataNames = FOSSILS.fossils.classes.map(([n]) => n.toLowerCase());
    assert.ok(dataNames.includes(real.label.toLowerCase()), "the real option is from the data");
    assert.equal(q.answerId, "real");
    const decoys = q.options.filter((o) => o.id !== "real");
    assert.equal(decoys.length, 3);
    for (const d of decoys) { assert.ok(!dataNames.includes(d.label.toLowerCase()), "decoy is not in the data"); assert.ok(G.FOSSIL_DECOYS.includes(d.label)); }
    assert.equal(q.reveal.group, real.label);
    assert.equal(q.reveal.count, FOSSILS.fossils.classes.find(([n]) => n === real.label)[1]);
  }
});

test("a decoy that happens to be in the real data is removed from the decoy pool", () => {
  const fossils = { fossils: { count: 5, classes: [["Trilobita", 4], ["Cephalopoda", 1]], taxa: [] }, scope: null };
  for (let seed = 1; seed <= 20; seed++) {
    const q = G.buildQuestion("fossil", FULL, fossils, seeded(seed));
    const decoys = q.options.filter((o) => o.id !== "real").map((o) => o.label);
    assert.ok(!decoys.includes("Trilobita") && !decoys.includes("Cephalopoda"));
  }
});

test("species question uses 'recorded' wording, never 'alive' or 'lives'", () => {
  const q = G.buildQuestion("species", FULL, undefined, seeded(3));
  const text = [q.prompt, ...q.options.map((o) => o.label)].join(" ");
  assert.ok(/records?/i.test(q.prompt));
  assert.equal(/\b(alive|living|lives|live here)\b/i.test(text), false);
  const real = q.options.find((o) => o.id === "real").label;
  assert.ok(FULL.rare_animals_recorded_nearby.some((r) => r.species === real));
  for (const d of q.options.filter((o) => o.id !== "real")) assert.ok(G.SPECIES_DECOYS.includes(d.label));
  assert.equal(q.note, FULL.rare_animals_note);   // the honesty note from the server travels with the question
});

test("oldest-site question: the answer is the clearly oldest dated place; dates come from the page text", () => {
  for (let seed = 1; seed <= 15; seed++) {
    const q = G.buildQuestion("oldest", FULL, undefined, seeded(seed));
    assert.equal(q.answerId, "Dover Bronze Age Boat");           // 1575-1520 BC beats 1262 and the 11th century
    const sites = q.reveal.sites;
    assert.ok(sites.length >= 2 && sites.length <= 4);
    assert.ok(sites.every((s) => FULL.human_history_nearby.some((e) => e.place === s.id) && s.dateText));
    assert.ok(/judged from/i.test(q.note));
  }
});

test("wider scope: questions carry their scope so the page can say it in the question and the reveal", () => {
  const madison = G.buildQuestion("setting", MADISON.where.facts, undefined, seeded(1));
  assert.equal(madison.scope.level, "exact_area");
  const dover = G.buildQuestion("age", FULL, undefined, seeded(1));
  assert.equal(dover.scope.level, "wider_area");                                  // regional map
  assert.match(dover.scope.note, /regional-scale map/);
  const fossil = G.buildQuestion("fossil", MADISON.where.facts, MADISON.fossils, seeded(1));
  assert.equal(fossil.scope.level, "wider_area");
  assert.match(fossil.scope.note, /widened to about 222 km/);
  assert.match(fossil.scope.note, /Nearest finds about \d+ km away/);
});

test("pickQuestions: one or two questions of different types, repeatable with a seed", () => {
  const seen = new Set();
  for (let seed = 1; seed <= 40; seed++) {
    const qs = G.pickQuestions(FULL, FOSSILS, seeded(seed));
    assert.ok(qs.length === 1 || qs.length === 2);
    if (qs.length === 2) assert.notEqual(qs[0].type, qs[1].type);
    seen.add(qs.length);
  }
  assert.deepEqual([...seen].sort(), [1, 2]);       // both happen
  assert.deepEqual(G.pickQuestions(FULL, FOSSILS, seeded(7)).map((q) => q.type), G.pickQuestions(FULL, FOSSILS, seeded(7)).map((q) => q.type));
  assert.equal(G.pickQuestions(DHAKA.where.facts, undefined, seeded(1)).length, 1);   // only one type possible
  assert.equal(G.pickQuestion(FULL, FOSSILS, seeded(1), ["setting", "age", "fossil", "oldest"]).type, "species");
});

test("a question is never built for a type that is not possible", () => {
  assert.equal(G.buildQuestion("fossil", FULL, undefined), null);
  assert.equal(G.buildQuestion("setting", DHAKA.where.facts, undefined), null);
  assert.equal(G.buildQuestion("nonsense", FULL, FOSSILS), null);
});

test("slider mapping is monotonic, covers 0 to the age of the Earth, and round-trips closely", () => {
  assert.equal(G.sliderToMa(0), 0);
  assert.equal(G.sliderToMa(1000), G.SLIDER_MAX_MA);
  let prev = -1;
  for (let v = 0; v <= 1000; v += 25) { const ma = G.sliderToMa(v); assert.ok(ma >= prev); prev = ma; }
  for (const ma of [1, 66, 100.5, 497.5, 2500]) assert.ok(Math.abs(G.sliderToMa(G.maToSlider(ma)) - ma) <= Math.max(0.15 * ma, 6), String(ma));
});

// ---------------------------------------------------------------- dates read from the data
test("dates: only construction or founding cues count", () => {
  assert.deepEqual(G.extractDate("It was completed in 1262 as a wayside chapel."), { lo: 1262, hi: 1262, text: "completed in 1262" });
  assert.deepEqual(G.extractDate("It was built from 1867 to 1868."), { lo: 1867, hi: 1868, text: "built from 1867 to 1868" });
  assert.deepEqual(G.extractDate("It dates to 1575–1520 BC, which may make it old."), { lo: -1575, hi: -1520, text: "dates to 1575–1520 BC" });
  assert.equal(G.extractDate("Dover Castle was founded in the 11th century.").lo, 1001);
  assert.equal(G.extractDate("The temple was founded in the 5th century BC.").lo, -500);
  assert.equal(G.extractDate("Dating from 1911, it was a cinema.").lo, 1911);
});

test("dates: bare years, listings, populations and rebuilds are ignored", () => {
  assert.equal(G.extractDate("Grade II* listed in 1973; upgraded in 2017."), null);
  assert.equal(G.extractDate("A population of 269,840 at the 2020 census."), null);
  assert.equal(G.extractDate("It was rebuilt in 1700 after a fire."), null);
  assert.equal(G.extractDate("It was built with 1,200 stones."), null);
  assert.equal(G.extractDate(""), null);
  assert.equal(G.extractDate(null), null);
});

test("dates in the real Dover introductions", () => {
  const dates = Object.fromEntries(FULL.human_history_nearby.map((e) => [e.place, (G.extractDate(e.summary) || {}).text || null]));
  assert.equal(dates["St Edmund's Chapel"], "completed in 1262");
  assert.equal(dates["Maison Dieu, Dover"], null);       // "medieval", no year: not used
});

// ---------------------------------------------------------------- distance and compass helpers
test("compass direction: eight points from the user to the site", () => {
  const o = [51.13, 1.31];
  const dir = (dLat, dLng) => G.compassDirection(o[0], o[1], o[0] + dLat, o[1] + dLng).name;
  assert.equal(dir(0.01, 0), "north");
  assert.equal(dir(0, 0.01), "east");
  assert.equal(dir(-0.01, 0), "south");
  assert.equal(dir(0, -0.01), "west");
  assert.equal(dir(0.01, 0.0159), "north-east");   // longitude degrees are shorter than latitude degrees at 51 N
  assert.equal(dir(-0.01, 0.0159), "south-east");
  assert.equal(dir(-0.01, -0.0159), "south-west");
  assert.equal(dir(0.01, -0.0159), "north-west");
  assert.equal(G.compassDirection(1, 2, NaN, 2), null);
  const b = G.compassDirection(51.13, 1.31, 51.13, 1.40);
  assert.equal(b.bearing, 90);
});

test("distance wording is honest: straight line, no walking time, far flagged", () => {
  assert.equal(G.describeDistance(70).text, "about 70 m away, straight line");
  assert.equal(G.describeDistance(235).text, "about 240 m away, straight line");
  assert.equal(G.describeDistance(2900).text, "about 2.9 km away, straight line");
  assert.equal(G.describeDistance(3000).text, "about 3 km away, straight line");
  assert.equal(G.describeDistance(9990).far, false);
  assert.equal(G.describeDistance(10000).far, false);
  const far = G.describeDistance(12400);
  assert.equal(far.far, true);
  assert.match(far.text, /about 12 km away, straight line/);
  assert.match(far.text, /far/);
  for (const m of [70, 235, 2900, 9990, 12400]) assert.equal(/\d\s*(min|minute|hour)/i.test(G.describeDistance(m).text), false);
  assert.equal(G.describeDistance(NaN).far, false);
});

test("one-line summary: first sentence, not cut at St. or initials, and shortened", () => {
  assert.equal(G.firstSentence("St Edmund's Chapel is a church in Dover, England.  It was completed in 1262."), "St Edmund's Chapel is a church in Dover, England.");
  assert.equal(G.firstSentence("The Priory of St. Mary the Virgin and St."), "The Priory of St. Mary the Virgin and St.");
  assert.equal(G.firstSentence("The church of St. Mary is old. It is big."), "The church of St. Mary is old.");
  assert.ok(G.firstSentence("word ".repeat(100), 60).length <= 60);
  assert.equal(G.firstSentence(""), "");
});

// ---------------------------------------------------------------- dig sites nearby
const USER = { lat: 51.13, lng: 1.31 };

test("dig sites: up to 5, nearest first, with direction, honest distance, one-line summary and links", () => {
  const r = G.digSites(FULL, USER);
  assert.equal(r.mode, "sites");
  assert.ok(r.sites.length >= 2 && r.sites.length <= 5);
  const d = r.sites.map((s) => s.distance_m);
  assert.deepEqual(d, [...d].sort((a, b) => a - b));
  const chapel = r.sites.find((s) => s.name === "St Edmund's Chapel");
  assert.match(chapel.distanceText, /^about \d+ m away, straight line$/);
  assert.ok(["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"].includes(chapel.direction.name));
  assert.equal(chapel.summary, "St Edmund's Chapel is a church in Dover, England, dedicated to St Edmund.");
  assert.match(chapel.wikiUrl, /^https:\/\/en\.wikipedia\.org\/\?curid=\d+$/);
  assert.match(chapel.mapUrl, /^https:\/\/www\.openstreetmap\.org\/\?mlat=51\.\d{5}&mlon=1\.\d{5}#map=17\/51\.\d{5}\/1\.\d{5}$/);
  assert.equal(r.scope.level, "exact_area");
});

test("dig sites: no routing service, no walking time, no opening hours or prices anywhere in the helper output", () => {
  const blob = JSON.stringify(G.digSites(FULL, USER));
  assert.equal(/routing|directions|google|osrm|valhalla|graphhopper/i.test(blob), false);
  assert.equal(/opening hours|ticket|admission|open to visitors|minutes? walk/i.test(blob), false);
  assert.equal(G.DIG_NOTE, "Check access and opening times before you go.");
});

test("dig sites, wider scope (nearest city): no individual sites, and it says what the history is about", () => {
  for (const place of [MADISON, DHAKA]) {
    const r = G.digSites(place.where.facts, USER);
    assert.equal(r.mode, "wider");
    assert.deepEqual(r.sites, []);
    assert.equal(r.scope.level, "nearest_place");
    assert.equal(r.placeName, place === MADISON ? "Madison" : "Dhaka");
  }
});

test("dig sites, region scope and the other cases", () => {
  const region = { ...FULL, scope: { ...FULL.scope, human: { level: "region", place_name: "Northern Territory", note: "..." } },
    human_history_nearby: [{ place: "Northern Territory", distance_m: null, summary: "x", lat: null, lng: null, pageid: null }] };
  const r = G.digSites(region, USER);
  assert.equal(r.mode, "wider"); assert.equal(r.placeName, "Northern Territory"); assert.deepEqual(r.sites, []);
  assert.equal(G.digSites({ ...FULL, human_history_nearby: null }, USER).mode, "unavailable");                       // source failed
  assert.equal(G.digSites({ ...FULL, human_history_nearby: [], scope: { human: { level: "none", note: "Nothing recorded" } } }, USER).mode, "none");
  const noCoords = { ...FULL, human_history_nearby: FULL.human_history_nearby.map((e) => ({ ...e, lat: null, lng: null })) };
  assert.equal(G.digSites(noCoords, USER).mode, "none");                                                             // never invent positions
});

test("dig sites: a far site is flagged and not presented as a walk; no user position means no direction", () => {
  const far = { ...FULL, human_history_nearby: [{ place: "Far Fort", distance_m: 12400, summary: "A fort.", lat: 51.2, lng: 1.5, pageid: 5 }] };
  const [site] = G.digSites(far, USER).sites;
  assert.equal(site.far, true);
  assert.match(site.distanceText, /far: plan transport rather than a walk/);
  assert.equal(G.digSites(far, null).sites[0].direction, null);
});

test("dig sites: more than 5 results are cut to the 5 nearest", () => {
  const many = { ...FULL, human_history_nearby: Array.from({ length: 8 }, (_, i) => ({ place: "P" + i, distance_m: 800 - i * 50, summary: "S.", lat: 51.1 + i / 1000, lng: 1.3, pageid: i + 1 })) };
  const r = G.digSites(many, USER);
  assert.equal(r.sites.length, 5);
  assert.equal(r.sites[0].name, "P7");   // nearest
});

test("the pages never send anything about the game or sites to the server", () => {
  const src = fs.readFileSync(path.join(__dirname, "..", "static", "stela-game.js"), "utf8");
  assert.equal(/fetch\(|XMLHttpRequest|sendBeacon|localStorage/.test(src), false);
});
