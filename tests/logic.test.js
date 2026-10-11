// Tests for static/stela-logic.js (walk-mode distance rules and the passport). Run: node --test tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const L = require("../static/stela-logic.js");

const ROCK = { name: "Wonewoc Formation (Cw)", b_int_name: "Cambrian", t_int_name: "Cambrian", b_age: 497.5, t_age: 493.575, map_scale: "large" };
const CHALK = { name: "White Chalk Subgroup", b_int_name: "Cenomanian", t_int_name: "Maastrichtian", b_age: 100.5, t_age: 66, map_scale: "large" };

// A storage that behaves like localStorage
function memoryStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), _m: m };
}

// ---------------------------------------------------------------- walk mode distance logic
test("distance: about 111 m per 0.001 degree of latitude", () => {
  const d = L.distanceMeters({ lat: 51.13, lng: 1.31 }, { lat: 51.133, lng: 1.31 });
  assert.ok(d > 330 && d < 337, d);
  assert.equal(L.distanceMeters({ lat: 1, lng: 2 }, { lat: 1, lng: 2 }), 0);
});

test("first fix always asks for a story", () => {
  assert.equal(L.shouldRequestStory(null, { lat: 43.07, lng: -89.4, accuracy: 20 }), true);
});

test("only a move of more than about 300 m asks again", () => {
  const last = { lat: 43.07, lng: -89.4 };
  const metres = (m) => ({ lat: 43.07 + m / 111195, lng: -89.4, accuracy: 10 });
  assert.equal(L.shouldRequestStory(last, metres(0)), false);
  assert.equal(L.shouldRequestStory(last, metres(40)), false);   // GPS jitter while standing still
  assert.equal(L.shouldRequestStory(last, metres(250)), false);
  assert.equal(L.shouldRequestStory(last, metres(290)), false);
  assert.equal(L.shouldRequestStory(last, metres(310)), true);
  assert.equal(L.shouldRequestStory(last, metres(2000)), true);
});

test("the threshold is measured from the LAST REQUEST, so a slow walk still triggers eventually", () => {
  let last = { lat: 43.07, lng: -89.4 };
  let asked = 0;
  for (let step = 1; step <= 10; step++) {          // ten 100 m steps = 1 km
    const pos = { lat: 43.07 + (step * 100) / 111195, lng: -89.4, accuracy: 15 };
    if (L.shouldRequestStory(last, pos)) { asked++; last = pos; }
  }
  assert.equal(asked, 2);                           // at 400 m, then again at 800 m (400 m after the last ask), never in between
});

test("poor GPS accuracy and bad values never trigger a request", () => {
  const far = { lat: 44, lng: -89.4 };
  assert.equal(L.shouldRequestStory({ lat: 43.07, lng: -89.4 }, { ...far, accuracy: 500 }), false);
  assert.equal(L.shouldRequestStory(null, { lat: 43.07, lng: -89.4, accuracy: 500 }), false);
  assert.equal(L.shouldRequestStory(null, { lat: NaN, lng: 1 }), false);
  assert.equal(L.shouldRequestStory(null, null), false);
  assert.equal(L.shouldRequestStory({ lat: 43.07, lng: -89.4 }, far), true);  // accuracy unknown is accepted
});

test("a custom threshold is respected", () => {
  assert.equal(L.shouldRequestStory({ lat: 0, lng: 0 }, { lat: 0.002, lng: 0 }, 100), true);
  assert.equal(L.shouldRequestStory({ lat: 0, lng: 0 }, { lat: 0.002, lng: 0 }, 1000), false);
});

// ---------------------------------------------------------------- rock change message
test("no message when the rock is the same, or when there is nothing to compare", () => {
  assert.equal(L.rockChange(ROCK, { ...ROCK }), null);
  assert.equal(L.rockChange(null, ROCK), null);
  assert.equal(L.rockChange(ROCK, null), null);
});

test("a different unit or era is reported", () => {
  const unit = L.rockChange(ROCK, { ...ROCK, name: "Tunnel City Group" });
  assert.deepEqual(unit, { unitChanged: true, eraChanged: false, era: null });
  const era = L.rockChange(ROCK, CHALK);
  assert.equal(era.eraChanged, true);
  assert.equal(era.era, "Cenomanian");
});

test("a changed rock NAME caused only by a different map scale is not a real change", () => {
  assert.equal(L.rockChange(ROCK, { ...ROCK, name: "Cambrian, undivided", map_scale: "medium" }), null);
});

// ---------------------------------------------------------------- passport
function sharedStore() { const s = memoryStorage(); return () => s; }
test("passport stores rounded coordinates, rock, era, age range and date", () => {
  const s = memoryStorage();
  const pass = L.createPassport(() => s);
  assert.deepEqual(pass.add(43.07041, -89.40049, ROCK, "2026-10-10"), { added: true, newEra: true });
  const [e] = pass.list();
  assert.deepEqual(e, { lat: 43.07, lng: -89.4, rock: "Wonewoc Formation (Cw)", era: "Cambrian", b_age: 497.5, t_age: 493.575, date: "2026-10-10", how: "here" });
  assert.ok(s._m.has(L.KEY));
  assert.equal(pass.persisted, true);
});

test("the same rounded spot is stamped only once; a new era is flagged once", () => {
  const s = memoryStorage();
  const pass = L.createPassport(() => s);
  assert.equal(pass.add(43.0701, -89.4001, ROCK, "2026-10-10").added, true);
  assert.equal(pass.add(43.0704, -89.3996, ROCK, "2026-10-11").added, false);       // same place after rounding
  assert.deepEqual(pass.add(43.2, -89.4, ROCK, "2026-10-11"), { added: true, newEra: false });  // same era again
  assert.deepEqual(pass.add(51.13, 1.31, CHALK, "2026-10-12"), { added: true, newEra: true });
  assert.deepEqual({ ...pass.stats(), oldest: undefined }, { places: 3, eras: 2, oldest: undefined });
});

test("bad coordinates are not stamped", () => {
  const pass = L.createPassport(sharedStore());
  assert.equal(pass.add(NaN, -89.4, ROCK, "2026-10-10").added, false);
  assert.equal(pass.stats().places, 0);
});

test("a place with no rock is stamped too, but does not count as an era", () => {
  const pass = L.createPassport(sharedStore());
  assert.deepEqual(pass.add(23.81, 90.41, null, "2026-10-10", "pin"), { added: true, newEra: false });
  const [e] = pass.list();
  assert.equal(e.era, L.NO_ROCK);
  assert.equal(e.how, "pin");
  assert.equal(e.b_age, null);
  assert.deepEqual(pass.stats(), { places: 1, eras: 0, oldest: null });
});

test("a no-rock stamp is filled in when a later lookup at that spot finds a rock; a rock stamp is never replaced by no-rock", () => {
  const pass = L.createPassport(sharedStore());
  pass.add(43.07, -89.4, null, "2026-10-10");
  assert.deepEqual(pass.add(43.07, -89.4, ROCK, "2026-10-11"), { added: true, newEra: true });
  assert.equal(pass.list().length, 1);
  assert.equal(pass.list()[0].era, "Cambrian");
  assert.equal(pass.add(43.07, -89.4, null, "2026-10-12").added, false);
  assert.equal(pass.list()[0].era, "Cambrian");
});

test("the passport reports the oldest ground looked up (largest start age), ignoring no-rock stamps", () => {
  const pass = L.createPassport(sharedStore());
  pass.add(51.13, 1.31, CHALK, "2026-10-10", "pin");
  pass.add(23.81, 90.41, null, "2026-10-10", "here");
  assert.equal(pass.stats().oldest.era, "Cenomanian");
  pass.add(43.07, -89.4, ROCK, "2026-10-11", "pin");
  const o = pass.stats().oldest;
  assert.equal(o.era, "Cambrian");
  assert.equal(o.b_age, 497.5);
  assert.equal(o.how, "pin");
});

test("clear empties the passport", () => {
  const s = memoryStorage();
  const pass = L.createPassport(() => s);
  pass.add(43.07, -89.4, ROCK, "2026-10-10");
  pass.clear();
  assert.deepEqual(pass.stats(), { places: 0, eras: 0, oldest: null });
  assert.equal(s._m.has(L.KEY), false);
});

test("the passport survives a reload (new passport object, same storage)", () => {
  const s = memoryStorage();
  L.createPassport(() => s).add(43.07, -89.4, ROCK, "2026-10-10");
  assert.equal(L.createPassport(() => s).stats().places, 1);
});

// ---------------------------------------------------------------- storage blocked
test("blocked storage: looking up localStorage throws -> passport still works in memory", () => {
  const pass = L.createPassport(() => { throw new DOMException("blocked", "SecurityError"); });
  assert.deepEqual(pass.list(), []);
  assert.equal(pass.add(43.07, -89.4, ROCK, "2026-10-10").added, true);
  assert.equal(pass.add(51.13, 1.31, CHALK, "2026-10-10").added, true);
  assert.equal(pass.stats().places, 2);
  assert.equal(pass.stats().eras, 2);   // kept for this visit
  assert.equal(pass.persisted, false);                      // so the page can say it will not be remembered
  pass.clear();                                             // does not throw
  assert.equal(pass.stats().places, 0);
});

test("storage returning null (some privacy modes) behaves the same", () => {
  const pass = L.createPassport(() => null);
  assert.equal(pass.add(43.07, -89.4, ROCK, "2026-10-10").added, true);
  assert.equal(pass.persisted, false);
  assert.equal(pass.stats().places, 1);
});

test("quota error on write: the stamp is kept in memory, nothing throws", () => {
  const s = memoryStorage();
  s.setItem = () => { throw new DOMException("full", "QuotaExceededError"); };
  const pass = L.createPassport(() => s);
  assert.equal(pass.add(43.07, -89.4, ROCK, "2026-10-10").added, true);
  assert.equal(pass.persisted, false);
  assert.equal(pass.stats().places, 1);
});

test("getItem throwing is survived", () => {
  const s = memoryStorage();
  s.getItem = () => { throw new Error("boom"); };
  const pass = L.createPassport(() => s);
  assert.deepEqual(pass.list(), []);
  assert.equal(pass.add(43.07, -89.4, ROCK, "2026-10-10").added, true);
});

test("corrupted or tampered storage is ignored, not trusted", () => {
  const s = memoryStorage();
  s.setItem(L.KEY, "{not json");
  assert.deepEqual(L.createPassport(() => s).list(), []);
  s.setItem(L.KEY, JSON.stringify([{ lat: "x" }, null, 5, { lat: 1, lng: 2, rock: "ok", era: "E", date: "2026-01-01" },
    { lat: 1, lng: 2, rock: "<img src=x onerror=alert(1)>".repeat(20), era: "E2", date: "2026-01-01" }]));
  const list = L.createPassport(() => s).list();
  assert.equal(list.length, 2);                 // malformed entries dropped
  assert.ok(list[1].rock.length <= 120);        // long text clipped
  s.setItem(L.KEY, JSON.stringify({ not: "an array" }));
  assert.deepEqual(L.createPassport(() => s).list(), []);
});

test("the passport is capped so it cannot grow without limit", () => {
  const s = memoryStorage();
  const pass = L.createPassport(() => s);
  for (let i = 0; i < L.MAX_ENTRIES + 20; i++) pass.add(10 + i * 0.01, 20, ROCK, "2026-10-10");
  assert.equal(pass.list().length, L.MAX_ENTRIES);
});

// ---------------------------------------------------------------- the page must not poll on a timer
test("index.html never polls on a timer and never sends the passport to the server", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8");
  assert.equal(/setInterval/.test(html), false);
  assert.equal(/requestAnimationFrame/.test(html), false);
  assert.ok(/watchPosition/.test(html) && /clearWatch/.test(html));
  assert.ok(/visibilitychange/.test(html));
  // fetch is used only inside api(); the passport object is never passed to it
  assert.equal(/fetch\([^)]*passport/i.test(html), false);
  const logic = fs.readFileSync(path.join(__dirname, "..", "static", "stela-logic.js"), "utf8");
  assert.equal(/fetch\(|XMLHttpRequest|sendBeacon/.test(logic), false);
});
