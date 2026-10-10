// Run: node --test tests/sound.test.js   (a fake Web Audio, so no real sound and no browser)
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

function load({ stored = null, blockedStorage = false, noAudio = false } = {}) {
  const made = { contexts: 0, sources: 0 };
  const listeners = {};
  const node = () => ({ connect() {}, start() { made.sources++; }, stop() {}, frequency: { setValueAtTime() {}, exponentialRampToValueAtTime() {}, value: 0 }, Q: {}, gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} } });
  class Ctx {
    constructor() { made.contexts++; this.currentTime = 0; this.sampleRate = 8000; this.state = "running"; this.destination = {}; }
    createGain() { return node(); }
    createOscillator() { return node(); }
    createBiquadFilter() { return node(); }
    createBuffer(c, n) { return { getChannelData: () => new Float32Array(n) }; }
    createBufferSource() { return node(); }
    resume() {}
  }
  const store = { ...(stored ? { "stela.sound": stored } : {}) };
  const win = {
    addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
    localStorage: blockedStorage ? { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } }
      : { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
  };
  if (!noAudio) win.AudioContext = Ctx;
  global.window = win;
  const file = path.join(__dirname, "..", "static", "stela-sound.js");
  delete require.cache[require.resolve(file)];
  require(file);
  return { S: win.StelaSound, made, store, tap: () => (listeners.pointerdown || []).forEach((f) => f()) };
}

test("off by default: nothing is created or played", () => {
  const { S, made, tap } = load();
  assert.strictEqual(S.enabled(), false);
  tap(); S.play("tap"); S.play("chime"); S.play("low"); S.play("era");
  assert.deepStrictEqual(made, { contexts: 0, sources: 0 });
});

test("no AudioContext before the first tap, then sounds play when on", () => {
  const { S, made, tap } = load();
  S.setEnabled(true);
  S.play("chime");
  assert.strictEqual(made.contexts, 0);
  tap(); S.play("chime");
  assert.strictEqual(made.contexts, 1);
  assert.ok(made.sources >= 2);
});

test("nothing plays while speech is speaking", () => {
  const { S, made, tap } = load({ stored: "on" });
  let speaking = true;
  S.init({ isSpeaking: () => speaking });
  tap(); S.play("tap");
  assert.strictEqual(made.sources, 0);
  speaking = false; S.play("low");
  assert.ok(made.sources > 0);
});

test("the choice persists, and blocked storage or a missing audio API fail silently", () => {
  const a = load();
  a.S.setEnabled(true);
  assert.strictEqual(a.store["stela.sound"], "on");
  const b = load({ blockedStorage: true });
  assert.doesNotThrow(() => b.S.setEnabled(true));
  assert.strictEqual(b.S.enabled(), true);
  const c = load({ noAudio: true, stored: "on" });
  assert.strictEqual(c.S.supported, false);
  assert.strictEqual(c.S.enabled(), false);
  assert.doesNotThrow(() => c.S.play("tap"));
});

test("a repeated sound within 120 ms is ignored", () => {
  const { S, made, tap } = load({ stored: "on" });
  tap(); S.play("low"); const n = made.sources; S.play("low");
  assert.strictEqual(made.sources, n);
});
