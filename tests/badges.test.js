// Tests for static/stela-badges.js: era bands, ranks, pins, storage. Run: node --test tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const B = require("../static/stela-badges.js");

const rockAt = (b_age, extra = {}) => ({ name: "Test Formation", b_int_name: "Cretaceous", b_age, t_age: b_age - 1, map_scale: "large", ...extra });
const idAt = (b_age) => (B.badgeFor(rockAt(b_age)) || {}).id;

function memoryStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), _m: m };
}
const shared = () => { const s = memoryStorage(); return [() => s, s]; };
const DATE = "2026-10-11";

// ---------------------------------------------------------------- the age bands (ICS v2024/12 boundaries)
test("the boundary constants are the ICS chart values", () => {
  assert.deepEqual(B.BOUNDARY_MA, { HOLOCENE: 0.0117, QUATERNARY: 2.58, CENOZOIC: 66, MESOZOIC: 251.902, PHANEROZOIC: 538.8 });
});

test("each age band maps to the right badge", () => {
  assert.equal(idAt(0), "fresh");
  assert.equal(idAt(0.005), "fresh");
  assert.equal(idAt(1), "ice");
  assert.equal(idAt(30), "mammal");
  assert.equal(idAt(100.5), "dino");        // White Chalk, Dover
  assert.equal(idAt(400), "seas");
  assert.equal(idAt(497.5), "seas");        // Wonewoc Formation, Madison
  assert.equal(idAt(1800), "deep");
});

test("boundaries: an oldest age exactly on a boundary belongs to the younger badge, just over goes to the older", () => {
  assert.equal(idAt(0.0117), "fresh");
  assert.equal(idAt(0.01171), "ice");
  assert.equal(idAt(2.58), "ice");
  assert.equal(idAt(2.5801), "mammal");
  assert.equal(idAt(66), "mammal");
  assert.equal(idAt(66.01), "dino");
  assert.equal(idAt(251.902), "dino");
  assert.equal(idAt(251.903), "seas");
  assert.equal(idAt(538.8), "seas");
  assert.equal(idAt(538.9), "deep");
});

test("the oldest age (b_age) chooses the badge, not the youngest", () => {
  assert.equal(B.badgeFor({ b_age: 100.5, t_age: 66 }).id, "dino");
  assert.equal(B.badgeFor({ b_age: 70, t_age: 0.5 }).id, "dino");
});

test("a missing or unusable age gives no badge", () => {
  assert.equal(B.badgeFor(null), null);
  assert.equal(B.badgeFor({}), null);
  assert.equal(B.badgeFor({ b_age: null }), null);
  assert.equal(B.badgeFor({ b_age: "100" }), null);
  assert.equal(B.badgeFor({ b_age: NaN }), null);
  assert.equal(B.badgeFor({ b_age: -1 }), null);
});

test("six era badges with the specified names, in order", () => {
  assert.deepEqual(B.ERA_BADGES.map((b) => b.name), ["Fresh Ground Walker", "Ice Age Wanderer", "Mammal Age Explorer",
    "Dino Era Traveler", "Ancient Seas Voyager", "Deep Time Pioneer"]);
});

// ---------------------------------------------------------------- ranks
test("rank titles by the number of distinct era badges", () => {
  assert.equal(B.rankFor(0), null);
  assert.equal(B.rankFor(1), "Noob Pre-Historic Traveler");
  assert.equal(B.rankFor(2), "Fossil Hunter");
  assert.equal(B.rankFor(3), "Fossil Hunter");
  assert.equal(B.rankFor(4), "Ancient Traveler");
  assert.equal(B.rankFor(5), "Ancient Traveler");
  assert.equal(B.rankFor(6), "Deep Time Master");
  assert.deepEqual(B.nextRank(1), { title: "Fossil Hunter", needs: 2 });
  assert.equal(B.nextRank(6), null);
});

test("device-location visits earn era badges; the same badge twice counts once", () => {
  const [get] = shared();
  const book = B.createBadgeBook(get);
  assert.equal(book.rank(), null);
  const a = book.record({ rock: rockAt(100.5), lat: 51.13, lng: 1.31, how: "here", date: DATE });
  assert.deepEqual([a.kind, a.badge.id, a.isNew], ["era", "dino", true]);
  const b = book.record({ rock: rockAt(120), lat: 51.2, lng: 1.4, how: "here", date: DATE });
  assert.equal(b.isNew, false);
  assert.equal(book.eraCount(), 1);
  assert.equal(book.rank(), "Noob Pre-Historic Traveler");
  book.record({ rock: rockAt(497.5), lat: 43.07, lng: -89.4, how: "here", date: DATE });
  assert.equal(book.eraCount(), 2);
  assert.equal(book.rank(), "Fossil Hunter");
});

test("a location visit with no rock or no age awards nothing", () => {
  const book = B.createBadgeBook(shared()[0]);
  assert.equal(book.record({ rock: null, lat: 23.81, lng: 90.41, how: "here", date: DATE }).kind, "none");
  assert.equal(book.record({ rock: { name: "x" }, lat: 1, lng: 1, how: "here", date: DATE }).kind, "none");
  assert.equal(book.eraCount(), 0);
});

// ---------------------------------------------------------------- pins
test("pin visits earn only Armchair Explorer and never count toward the rank", () => {
  const [get, s] = shared();
  const book = B.createBadgeBook(get);
  const r = book.record({ rock: rockAt(497.5), lat: 43.07, lng: -89.4, how: "pin", date: DATE });
  assert.deepEqual([r.kind, r.badge.id, r.isNew], ["armchair", "armchair-seas", true]);   // classified by the rock's age band
  assert.equal(r.badge.name, "Armchair Explorer: Ancient Seas");
  assert.equal(r.preview.id, "seas");                 // the era badge it would have given: shown, not stored
  assert.equal(book.eraCount(), 0);
  assert.equal(book.rank(), null);
  assert.equal(book.hasArmchair(), true);
  assert.deepEqual(Object.keys(JSON.parse(s._m.get(B.KEY))).sort(), ["armchair", "armchair-seas"]);
  for (let age of [0.001, 1, 30, 100, 400, 1800]) book.record({ rock: rockAt(age), lat: 10, lng: 10, how: "pin", date: DATE });
  assert.equal(book.eraCount(), 0);
  assert.equal(book.rank(), null);
  assert.equal(book.armchairCount(), 7);              // the base badge plus one division per age band
  assert.equal(book.record({ rock: null, lat: 1, lng: 1, how: "pin", date: DATE }).isNew, false);   // only once
});

test("anything other than \"here\" or \"pin\" earns nothing", () => {
  const book = B.createBadgeBook(shared()[0]);
  assert.equal(book.record({ rock: rockAt(100), lat: 1, lng: 1, how: undefined, date: DATE }).kind, "none");
  assert.equal(book.eraCount(), 0);
});

// ---------------------------------------------------------------- what is stored
test("only era, rock name, date and a coarse place label are stored, never exact coordinates", () => {
  const [get, s] = shared();
  B.createBadgeBook(get).record({ rock: rockAt(100.5, { name: "White Chalk Subgroup", b_int_name: "Cenomanian" }), lat: 51.13417, lng: 1.31233, how: "here", date: DATE });
  const raw = s._m.get(B.KEY);
  const stored = JSON.parse(raw).dino;
  assert.deepEqual(Object.keys(stored).sort(), ["date", "era", "place", "rock"]);
  assert.deepEqual(stored, { era: "Cenomanian", rock: "White Chalk Subgroup", date: DATE, place: "51.1° N, 1.3° E" });
  assert.ok(!raw.includes("51.13") && !raw.includes("1.312"));
});

test("place labels are one decimal, with hemispheres", () => {
  assert.equal(B.placeLabel(-33.86, 151.21), "33.9° S, 151.2° E");
  assert.equal(B.placeLabel(43.07, -89.4), "43.1° N, 89.4° W");
  assert.equal(B.placeLabel(NaN, 1), "");
});

test("badges survive a reload and tampered storage is ignored", () => {
  const [get, s] = shared();
  B.createBadgeBook(get).record({ rock: rockAt(100), lat: 1, lng: 1, how: "here", date: DATE });
  assert.equal(B.createBadgeBook(get).eraCount(), 1);
  s.setItem(B.KEY, "{not json");
  assert.equal(B.createBadgeBook(get).eraCount(), 0);
  s.setItem(B.KEY, JSON.stringify({ dino: { era: "<img onerror=x>".repeat(20), rock: "r", date: "bad", place: "p" }, hacker: { era: "x" }, ice: 5 }));
  const e = B.createBadgeBook(get).earned();
  assert.deepEqual(Object.keys(e), ["dino"]);
  assert.ok(e.dino.era.length <= 60);
  assert.equal(e.dino.date, "");
});

// ---------------------------------------------------------------- storage blocked
test("blocked storage: still works in memory, says it is not remembered, and clear does not throw", () => {
  const book = B.createBadgeBook(() => { throw new DOMException("blocked", "SecurityError"); });
  assert.equal(book.eraCount(), 0);
  assert.equal(book.record({ rock: rockAt(100), lat: 1, lng: 1, how: "here", date: DATE }).isNew, true);
  assert.equal(book.record({ rock: rockAt(500), lat: 2, lng: 2, how: "here", date: DATE }).isNew, true);
  assert.equal(book.eraCount(), 2);
  assert.equal(book.rank(), "Fossil Hunter");
  assert.equal(book.persisted, false);
  book.clear();
  assert.equal(book.eraCount(), 0);
});

test("storage returning null, or a failing write, also keeps working", () => {
  const nul = B.createBadgeBook(() => null);
  assert.equal(nul.record({ rock: rockAt(100), lat: 1, lng: 1, how: "here", date: DATE }).isNew, true);
  assert.equal(nul.persisted, false);
  const s = memoryStorage();
  s.setItem = () => { throw new DOMException("full", "QuotaExceededError"); };
  const full = B.createBadgeBook(() => s);
  assert.equal(full.record({ rock: rockAt(100), lat: 1, lng: 1, how: "here", date: DATE }).isNew, true);
  assert.equal(full.eraCount(), 1);
  assert.equal(full.persisted, false);
});

// ---------------------------------------------------------------- clearing
test("clear empties the badges and removes the stored key", () => {
  const [get, s] = shared();
  const book = B.createBadgeBook(get);
  book.record({ rock: rockAt(100), lat: 1, lng: 1, how: "here", date: DATE });
  book.record({ rock: rockAt(100), lat: 1, lng: 1, how: "pin", date: DATE });
  assert.equal(book.eraCount(), 1);
  book.clear();
  assert.equal(book.eraCount(), 0);
  assert.equal(book.hasArmchair(), false);
  assert.equal(book.rank(), null);
  assert.equal(s._m.has(B.KEY), false);
});

// ---------------------------------------------------------------- the card's caveats
test("a wide age range or a wider map scale is said on the badge card", () => {
  assert.deepEqual(B.basisNotes({ b_age: 100.5, t_age: 66, map_scale: "large" }), ["The rock's age is a wide range, so the badge uses its oldest age."]);
  assert.deepEqual(B.basisNotes({ b_age: 497.5, t_age: 493.575, map_scale: "large" }), []);
  assert.match(B.basisNotes({ b_age: 100, t_age: 99, map_scale: "medium" })[0], /regional rock data/);
  assert.match(B.basisNotes({ b_age: 100, t_age: 99, map_scale: "small" })[0], /continental-scale/);
  assert.equal(B.basisNotes(null).length, 0);
});

// ---------------------------------------------------------------- privacy: never sent to the server
test("index.html never passes badge data to fetch", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8");
  assert.equal(/fetch\([^)]*badge/i.test(html), false);
  const src = fs.readFileSync(path.join(__dirname, "..", "static", "stela-badges.js"), "utf8");
  assert.equal(/fetch\(|XMLHttpRequest|sendBeacon/.test(src), false);
});

test("a pin on a rock with no age earns plain Armchair Explorer; each age band has its own armchair division", () => {
  const book = B.createBadgeBook(shared()[0]);
  const r = book.record({ rock: null, lat: 23.81, lng: 90.41, how: "pin", date: DATE });
  assert.deepEqual([r.badge.id, r.isNew, r.preview], ["armchair", true, null]);
  assert.equal(B.ARMCHAIR_DIVISIONS.length, 6);
  assert.deepEqual(B.ARMCHAIR_DIVISIONS.map((d) => d.eraId), B.ERA_BADGES.map((b) => b.id));
  const d = book.record({ rock: rockAt(100.5), lat: 51.13, lng: 1.31, how: "pin", date: DATE });
  assert.deepEqual([d.badge.id, d.isNew], ["armchair-dino", true]);
  assert.equal(book.record({ rock: rockAt(120), lat: 51.2, lng: 1.4, how: "pin", date: DATE }).isNew, false);
  assert.equal(book.eraCount(), 0);
});
