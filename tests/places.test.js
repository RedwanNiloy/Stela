// Tests for the "Try these places" list (static/stela-places.js) and how an example visit goes through the start flow.
// Run: node --test tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const P = require("../static/stela-places.js");
const S = require("../static/stela-start.js");
const B = require("../static/stela-badges.js");

test("the list loads: about six places, each with a name, a reason and valid coordinates", () => {
  assert.ok(P.PLACES.length >= 5 && P.PLACES.length <= 8);
  const ids = new Set();
  for (const p of P.PLACES) {
    assert.ok(p.id && p.name && p.where && p.reason, p.id);
    assert.ok(Number.isFinite(p.lat) && p.lat >= -90 && p.lat <= 90, p.id);
    assert.ok(Number.isFinite(p.lng) && p.lng >= -180 && p.lng <= 180, p.id);
    assert.ok(p.reason.length < 90, "one line: " + p.id);
    ids.add(p.id);
  }
  assert.equal(ids.size, P.PLACES.length, "ids are unique");
});

test("the list has the contrasting, fossil-rich and young-ground places that were verified", () => {
  const ids = P.PLACES.map((p) => p.id);
  for (const id of ["jurassic-coast", "dover", "cincinnati", "hell-creek", "la-brea"]) assert.ok(ids.includes(id), id);
  assert.ok(P.PLACES.some((p) => /young/i.test(p.reason)), "at least one young-ground contrast");
});

test("reasons are neutral: no specific fossil or species is named", () => {
  const taxa = /\b(trilobite|ammonite|brachiopod|t\.? ?rex|tyrannosaurus|triceratops|mammoth|sabre|saber|ichthyosaur|plesiosaur|mosasaur|bivalve|crinoid)\b/i;
  for (const p of P.PLACES) assert.doesNotMatch(p.reason, taxa, p.id);
});

test("the list carries the research-bias note under it", () => {
  assert.equal(P.LIST_NOTE, "Dense fossil records often reflect where research has been done. A quiet place may be understudied, not empty.");
  assert.equal(P.EXAMPLE_LABEL, "Example place");
});

function harness() {
  const calls = [];
  const deps = {
    requestLocation() { calls.push("requestLocation"); },
    showStart: () => calls.push("showStart"),
    showApp: () => calls.push("showApp"),
    setBusy: (b) => calls.push("busy:" + b),
    onFound: () => calls.push("found"),
    onUnavailable: () => calls.push("unavailable"),
    onExample: (p) => calls.push("example:" + p.id),
  };
  return { calls, flow: S.create(deps) };
}

test("tapping every example opens the app at that place and never requests the location", () => {
  for (const p of P.PLACES) {
    const { calls, flow } = harness();
    flow.example(p);
    assert.deepEqual(calls, ["busy:false", "showApp", "example:" + p.id]);
    assert.ok(!calls.includes("requestLocation"));
  }
});

test("an example tapped while Start is still waiting cancels the late location answer", () => {
  const calls = [];
  let reply;
  const flow = S.create({
    requestLocation(f, u) { reply = { f, u }; },
    showStart() {}, showApp() { calls.push("showApp"); }, setBusy() {},
    onFound() { calls.push("found"); }, onUnavailable() { calls.push("unavailable"); },
    onExample(p) { calls.push("example:" + p.id); },
  });
  flow.start();
  flow.example(P.PLACES[0]);
  reply.f(1, 2);
  assert.deepEqual(calls, ["showApp", "example:" + P.PLACES[0].id]);
});

test("example visits are pin visits: the badge rules give a pin no era badge and no rank", () => {
  // The page does not even record an example visit (see tests/test_places_page.py); and if it were recorded as a pin,
  // the badge rules would still give no era badge and no rank.
  const book = B.createBadgeBook(() => null);
  for (const rock of [{ b_age: 100.5 }, { b_age: 454.5 }, { b_age: 0.0117 }, { b_age: 0.781 }, { b_age: 67.525 }]) {
    const res = book.record({ rock, lat: 51.1, lng: 1.3, how: "pin", date: "2026-01-01" });
    assert.equal(res.kind, "armchair");
  }
  assert.equal(book.eraCount(), 0);
  assert.equal(book.rank(), null);
});
