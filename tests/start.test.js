// Tests for static/stela-start.js: the start screen never asks for the location until Start is tapped.
// Run: node --test tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const S = require("../static/stela-start.js");

// A fake page: records every call. requestLocation stands in for navigator.geolocation.
function harness({ answer = "found" } = {}) {
  const calls = [];
  let reply = null;
  const deps = {
    requestLocation(onFound, onFailed) { calls.push("requestLocation"); reply = { onFound, onFailed }; },
    showStart: () => calls.push("showStart"),
    showApp: () => calls.push("showApp"),
    setBusy: (b) => calls.push("busy:" + b),
    onFound: (lat, lng) => calls.push(`found:${lat},${lng}`),
    onUnavailable: () => calls.push("unavailable"),
  };
  return { calls, flow: S.create(deps), reply: () => reply };
}

test("creating the start screen does not ask for the location", () => {
  const { calls } = harness();
  assert.deepEqual(calls, []);
});

test("Start asks for the location once, and only then", () => {
  const { calls, flow } = harness();
  flow.start();
  assert.deepEqual(calls, ["busy:true", "requestLocation"]);
});

test("a granted location opens the results view and looks up that spot", () => {
  const { calls, flow, reply } = harness();
  flow.start();
  reply().onFound(51.13, 1.31);
  assert.deepEqual(calls.slice(2), ["busy:false", "showApp", "found:51.13,1.31"]);
});

test("a denied location opens the map view with the friendly message, and does not look anything up", () => {
  const { calls, flow, reply } = harness();
  flow.start();
  reply().onFailed();
  assert.deepEqual(calls.slice(2), ["busy:false", "showApp", "unavailable"]);
  assert.ok(!calls.some((c) => c.startsWith("found")));
});

test("Explore the map never requests the location", () => {
  const { calls, flow } = harness();
  flow.exploreMap();
  assert.ok(!calls.includes("requestLocation"));
  assert.ok(calls.includes("showApp"));
});

test("choosing the map while a location request is pending ignores its late answer", () => {
  const { calls, flow, reply } = harness();
  flow.start();
  flow.exploreMap();
  reply().onFound(1, 2);
  assert.ok(!calls.some((c) => c.startsWith("found")));
  assert.equal(calls.filter((c) => c === "showApp").length, 1);
});

test("Home shows the start screen without asking for the location", () => {
  const { calls, flow } = harness();
  flow.home();
  assert.ok(calls.includes("showStart"));
  assert.ok(!calls.includes("requestLocation"));
});
