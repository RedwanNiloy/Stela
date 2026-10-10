/* Stela UI sounds: optional, off by default, synthesized in the browser with the Web Audio API (oscillators and filtered
 * noise). No audio files, no libraries, and nothing is sent anywhere. The whole feature is this file plus the speaker
 * button and a few StelaSound.play(...) calls; delete them together to remove it.
 *
 *   StelaSound.play("tap" | "chime" | "low" | "era")   never throws, plays nothing unless sound is on
 *   StelaSound.enabled() / setEnabled(bool) / supported / stopAll()
 *   StelaSound.init({ isSpeaking })                     isSpeaking() -> true while the Listen voice talks
 *
 * Rules kept here: the AudioContext is created only after the first user tap; sound is off until the user turns it on
 * (the choice is remembered in localStorage when it is allowed); nothing plays while speech is speaking; every sound is
 * under 500 ms and quiet. A turned-on choice is an explicit choice, so it is honoured even with prefers-reduced-motion. */
(function (root) {
  "use strict";
  const KEY = "stela.sound";
  const Ctor = root.AudioContext || root.webkitAudioContext;
  let on = false, ctx = null, tapped = false, speaking = () => false;
  const live = new Set();    // sources currently playing, so they can be stopped when speech starts
  const lastAt = {};         // name -> time, to ignore a sound repeated within 120 ms

  try { on = !!Ctor && root.localStorage.getItem(KEY) === "on"; } catch (e) { on = false; }

  function context() {
    if (!Ctor || !tapped) return null;           // never before the first user tap
    try {
      if (!ctx) ctx = new Ctor();
      if (ctx.state === "suspended") ctx.resume();
      return ctx;
    } catch (e) { return null; }
  }

  // an envelope: a 5 ms attack, then an exponential fade to silence at `dur`
  function envelope(c, t, peak, dur) {
    const g = c.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(peak, t + 0.005);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    g.connect(c.destination);
    return g;
  }
  function track(src, stopAt) {
    live.add(src);
    src.onended = () => live.delete(src);
    src.stop(stopAt);
  }
  function tone(c, t, type, f0, f1, dur, peak) {
    const o = c.createOscillator();
    o.type = type;
    o.frequency.setValueAtTime(f0, t);
    if (f1 !== f0) o.frequency.exponentialRampToValueAtTime(f1, t + dur);
    o.connect(envelope(c, t, peak, dur));
    o.start(t);
    track(o, t + dur + 0.02);
  }
  function noise(c, t, dur, freq, q, peak) {     // a short filtered noise burst: the "stone" part of a tap
    const n = Math.max(1, Math.floor(c.sampleRate * dur));
    const buf = c.createBuffer(1, n, c.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
    const s = c.createBufferSource(), f = c.createBiquadFilter();
    s.buffer = buf; f.type = "bandpass"; f.frequency.value = freq; f.Q.value = q;
    s.connect(f); f.connect(envelope(c, t, peak, dur));
    s.start(t);
    track(s, t + dur + 0.02);
  }

  const SOUNDS = {
    tap(c, t)   { noise(c, t, 0.05, 1100, 3, 0.10); tone(c, t, "sine", 160, 90, 0.09, 0.07); },                    // 90 ms
    chime(c, t) { tone(c, t, "sine", 262, 262, 0.42, 0.06); tone(c, t + 0.02, "sine", 392, 392, 0.34, 0.035); },    // ~450 ms
    low(c, t)   { tone(c, t, "triangle", 120, 85, 0.32, 0.06); },                                                  // 320 ms
    era(c, t)   { tone(c, t, "sine", 523, 523, 0.16, 0.06); tone(c, t + 0.15, "sine", 784, 784, 0.26, 0.06); },    // ~410 ms
  };

  function play(name) {
    try {
      if (!on || !SOUNDS[name] || speaking()) return;
      const now = Date.now();
      if (now - (lastAt[name] || 0) < 120) return;
      const c = context();
      if (!c) return;
      lastAt[name] = now;
      SOUNDS[name](c, c.currentTime + 0.01);
    } catch (e) { /* audio is optional: fail silently */ }
  }

  function stopAll() {
    live.forEach((s) => { try { s.stop(); } catch (e) { /* already stopped */ } });
    live.clear();
  }

  function setEnabled(v) {
    on = !!v && !!Ctor;
    if (!on) stopAll();
    try { root.localStorage.setItem(KEY, on ? "on" : "off"); } catch (e) { /* not remembered: still works for this visit */ }
    return on;
  }

  // the first tap, press or key makes sound possible (an AudioContext needs a user gesture anyway)
  ["pointerdown", "keydown", "touchstart"].forEach((ev) => root.addEventListener(ev, () => { tapped = true; }, { capture: true, passive: true }));

  root.StelaSound = {
    play, stopAll, setEnabled,
    enabled: () => on,
    supported: !!Ctor,
    init(opts) { if (opts && typeof opts.isSpeaking === "function") speaking = opts.isSpeaking; },
  };
})(window);
