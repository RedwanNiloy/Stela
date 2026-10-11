/* Era badges and traveler ranks. No DOM here, so it can be tested in Node (tests/badges.test.js).
 *
 * PRIVACY: badges live only in this browser's localStorage and are never sent anywhere. What is stored per badge:
 * the era name, the rock name, the date and a coarsely rounded place label (one decimal of latitude and longitude,
 * about 11 km). Exact coordinates are never stored. */
(function (root) {
  "use strict";

  /* Boundary ages in millions of years (Ma), from the International Commission on Stratigraphy
   * International Chronostratigraphic Chart, v2024/12 (stratigraphy.org/chart): base of the Holocene 0.0117 Ma (11.7 ka),
   * base of the Quaternary (Pleistocene) 2.58 Ma, base of the Cenozoic (Paleogene) 66.0 Ma, base of the Mesozoic (Triassic)
   * 251.902 Ma, base of the Phanerozoic (Cambrian) 538.8 Ma. The ICS revises these numbers from time to time. */
  const BOUNDARY_MA = { HOLOCENE: 0.0117, QUATERNARY: 2.58, CENOZOIC: 66.0, MESOZOIC: 251.902, PHANEROZOIC: 538.8 };

  /* The one badge table. A rock gets the first badge whose `upTo` is at least the rock's OLDEST age (b_age), so a rock whose
   * oldest age is exactly on a boundary belongs to the younger badge (a rock that begins at 66.0 Ma is Cenozoic). */
  const ERA_BADGES = [
    { id: "fresh",  name: "Fresh Ground Walker",  upTo: BOUNDARY_MA.HOLOCENE,    art: "bd-fresh",  range: "under about 12 thousand years old" },
    { id: "ice",    name: "Ice Age Wanderer",     upTo: BOUNDARY_MA.QUATERNARY,  art: "bd-ice",    range: "about 12 thousand to 2.6 million years old" },
    { id: "mammal", name: "Mammal Age Explorer",  upTo: BOUNDARY_MA.CENOZOIC,    art: "bd-mammal", range: "about 2.6 to 66 million years old" },
    { id: "dino",   name: "Dino Era Traveler",    upTo: BOUNDARY_MA.MESOZOIC,    art: "bd-dino",   range: "about 66 to 252 million years old" },
    { id: "seas",   name: "Ancient Seas Voyager", upTo: BOUNDARY_MA.PHANEROZOIC, art: "bd-seas",   range: "about 252 to 539 million years old" },
    { id: "deep",   name: "Deep Time Pioneer",    upTo: Infinity,                art: "bd-deep",   range: "over about 539 million years old" },
  ];

  /* The separate badge for pins dropped on the map. It never counts toward the rank. */
  const ARMCHAIR = { id: "armchair", name: "Armchair Explorer", art: "bd-armchair",
    how: "Look up any place by dropping a pin on the map" };

  /* Rank by the number of distinct era badges collected with the device's location. */
  const RANKS = [
    { min: 6, title: "Deep Time Master" },
    { min: 4, title: "Ancient Traveler" },
    { min: 2, title: "Fossil Hunter" },
    { min: 1, title: "Noob Pre-Historic Traveler" },
  ];

  /* A Stela choice (not from the ICS): a rock mapped over more than this many millions of years is called "a wide range". */
  const WIDE_RANGE_MA = 25;

  const KEY = "stela.badges.v1";
  const clip = (s, n) => String(s == null ? "" : s).slice(0, n);

  /* The badge for a rock, from its oldest age. No rock, or no usable age: no badge. */
  function badgeFor(rock) {
    if (!rock || typeof rock.b_age !== "number" || !Number.isFinite(rock.b_age) || rock.b_age < 0) return null;
    return ERA_BADGES.find((b) => rock.b_age <= b.upTo) || null;
  }

  /* Plain-words caveats for the badge card: a wide age range, or a wider map scale than the detailed one. */
  function basisNotes(rock) {
    const notes = [];
    if (!rock) return notes;
    if (Number.isFinite(rock.b_age) && Number.isFinite(rock.t_age) && rock.b_age - rock.t_age > WIDE_RANGE_MA) {
      notes.push("The rock's age is a wide range, so the badge uses its oldest age.");
    }
    if (rock.map_scale === "medium") notes.push("Based on regional rock data, so it is less precise.");
    else if (rock.map_scale === "small") notes.push("Based on continental-scale rock data, so it is much less precise.");
    return notes;
  }

  function rankFor(count) {
    const r = RANKS.find((x) => count >= x.min);
    return r ? r.title : null;
  }
  /* The next rank above this count, for a hint: { title, needs } or null at the top. */
  function nextRank(count) {
    const higher = RANKS.filter((x) => x.min > count).sort((a, b) => a.min - b.min)[0];
    return higher ? { title: higher.title, needs: higher.min } : null;
  }

  /* One decimal of latitude and longitude: about 11 km, a neighbourhood-to-town sized area, never an exact spot. */
  function placeLabel(lat, lng) {
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return "";
    const f = (x, pos, neg) => `${Math.abs(x).toFixed(1)}° ${x >= 0 ? pos : neg}`;
    return `${f(lat, "N", "S")}, ${f(lng, "E", "W")}`;
  }

  const KNOWN = new Set([...ERA_BADGES.map((b) => b.id), ARMCHAIR.id]);
  function sanitize(obj) {
    const out = {};
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return out;
    for (const id of Object.keys(obj)) {
      const e = obj[id];
      if (!KNOWN.has(id) || !e || typeof e !== "object") continue;
      out[id] = { era: clip(e.era, 60), rock: clip(e.rock, 120), date: /^\d{4}-\d{2}-\d{2}$/.test(e.date) ? e.date : "", place: clip(e.place, 40) };
    }
    return out;
  }

  /* getStorage is a function so that even looking up window.localStorage happens inside try/catch. If storage fails, the
   * badges keep working in memory for this visit and `persisted` becomes false so the page can say so. */
  function createBadgeBook(getStorage) {
    let memory = {};
    let persisted = true;
    let unsaved = false;   // the last write failed, so memory is newer than whatever storage holds

    function read() {
      if (unsaved) return { ...memory };
      try {
        const s = getStorage();
        if (!s) throw new Error("no storage");
        const raw = s.getItem(KEY);
        memory = raw ? sanitize(JSON.parse(raw)) : {};
        persisted = true;
      } catch (e) {
        persisted = false;
      }
      return { ...memory };
    }
    function write(book) {
      memory = book;
      try {
        const s = getStorage();
        if (!s) throw new Error("no storage");
        s.setItem(KEY, JSON.stringify(book));
        persisted = true;
        unsaved = false;
      } catch (e) {
        persisted = false;
        unsaved = true;
      }
    }

    return {
      get persisted() { return persisted; },
      earned: read,                                             // { badgeId: { era, rock, date, place } }
      eraCount() { const b = read(); return ERA_BADGES.filter((x) => b[x.id]).length; },
      rank() { return rankFor(this.eraCount()); },
      hasArmchair() { return !!read()[ARMCHAIR.id]; },

      /* Records one lookup. how = "here" (the device's location, which includes Walk mode) or "pin" (dropped on the map).
       * "here" can earn an era badge; "pin" can earn only Armchair Explorer. Returns
       * { kind: "era" | "armchair" | "none", badge, isNew, preview } where preview (pins only) is the era badge the rock
       * would have given, which is shown but never stored or counted. */
      record({ rock, lat, lng, how, date }) {
        const book = read();
        const entry = (r) => ({ era: clip(r && (r.b_int_name || r.t_int_name) || "", 60), rock: clip(r && (r.name || r.strat_name) || "", 120),
          date: /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : "", place: placeLabel(lat, lng) });
        if (how === "pin") {
          const isNew = !book[ARMCHAIR.id];
          if (isNew) { book[ARMCHAIR.id] = entry(rock); write(book); }
          return { kind: "armchair", badge: ARMCHAIR, isNew, preview: badgeFor(rock) };
        }
        const badge = how === "here" ? badgeFor(rock) : null;   // era badges only for the device's location
        if (!badge) return { kind: "none", badge: null, isNew: false, preview: null };
        const isNew = !book[badge.id];
        if (isNew) { book[badge.id] = entry(rock); write(book); }
        return { kind: "era", badge, isNew, preview: null };
      },

      clear() {
        memory = {};
        unsaved = false;
        try {
          const s = getStorage();
          if (s) s.removeItem(KEY);
        } catch (e) { /* storage blocked: nothing stored, nothing to clear */ }
      },
    };
  }

  const api = { BOUNDARY_MA, ERA_BADGES, ARMCHAIR, RANKS, WIDE_RANGE_MA, KEY, badgeFor, basisNotes, rankFor, nextRank, placeLabel,
    sanitize, createBadgeBook };
  root.StelaBadges = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
