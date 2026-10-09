/* Stela browser logic that does not touch the page: distance rules for walk mode and the localStorage passport.
 * Kept in its own file so it can be tested in Node (tests/logic.test.js). Loaded by index.html as a plain script.
 * PRIVACY: the passport lives only in this browser's localStorage. Nothing here ever sends it anywhere. */
(function (root) {
  "use strict";

  const WALK_THRESHOLD_M = 300;  // only ask for a new story after moving more than this
  const MAX_ACCURACY_M = 200;    // ignore GPS fixes less accurate than this: they would look like movement
  const KEY = "stela.passport.v1";
  const MAX_ENTRIES = 300;

  /* ---------- Walk mode ---------- */
  function distanceMeters(a, b) {  // haversine, a and b are {lat, lng}
    const R = 6371000, rad = (d) => (d * Math.PI) / 180;
    const dLat = rad(b.lat - a.lat), dLng = rad(b.lng - a.lng);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  /* Should this GPS fix trigger a new story? `last` is where we last asked (or null). Never based on time. */
  function shouldRequestStory(last, pos, thresholdM = WALK_THRESHOLD_M) {
    if (!pos || !Number.isFinite(pos.lat) || !Number.isFinite(pos.lng)) return false;
    if (pos.accuracy != null && pos.accuracy > MAX_ACCURACY_M) return false;
    if (!last) return true;
    return distanceMeters(last, pos) > thresholdM;
  }

  /* Did the walker cross into a different rock unit or era? Returns null when nothing changed (stay quiet).
   * Rock names differ between map scales for the same ground ("Wonewoc Formation" vs "Cambrian, undivided"),
   * so a change of unit only counts when both results came from the same map scale. */
  function rockChange(prev, next) {
    if (!prev || !next) return null;
    const unitChanged = !!prev.name && !!next.name && prev.name !== next.name && prev.map_scale === next.map_scale;
    const eraChanged = !!prev.b_int_name && !!next.b_int_name && prev.b_int_name !== next.b_int_name;
    if (!unitChanged && !eraChanged) return null;
    return { unitChanged, eraChanged, era: eraChanged ? next.b_int_name : null };
  }

  /* ---------- Passport ---------- */
  const round3 = (x) => Math.round(x * 1000) / 1000;
  const clip = (s, n) => String(s == null ? "" : s).slice(0, n);

  function eraOf(rock) { return clip(rock.b_int_name || rock.t_int_name || "Unknown era", 60); }

  function makeEntry(lat, lng, rock, date) {
    return {
      lat: round3(lat), lng: round3(lng),                    // rounded to 3 decimals (about 110 m)
      rock: clip(rock.name || rock.strat_name || "Unnamed rock", 120),
      era: eraOf(rock),
      b_age: Number.isFinite(rock.b_age) ? rock.b_age : null,
      t_age: Number.isFinite(rock.t_age) ? rock.t_age : null,
      date: clip(date, 10),                                   // YYYY-MM-DD
    };
  }

  /* Drop anything malformed (storage can be edited or corrupted by the user or another script). */
  function sanitize(list) {
    if (!Array.isArray(list)) return [];
    return list.filter((e) => e && Number.isFinite(e.lat) && Number.isFinite(e.lng) && typeof e.rock === "string"
      && typeof e.era === "string" && typeof e.date === "string")
      .map((e) => ({ lat: round3(e.lat), lng: round3(e.lng), rock: clip(e.rock, 120), era: clip(e.era, 60),
        b_age: Number.isFinite(e.b_age) ? e.b_age : null, t_age: Number.isFinite(e.t_age) ? e.t_age : null, date: clip(e.date, 10) }))
      .slice(-MAX_ENTRIES);
  }

  const distinctEras = (list) => new Set(list.map((e) => e.era)).size;

  /* getStorage is a function so that even *looking up* window.localStorage happens inside try/catch
   * (browsers throw there when site data is blocked). If storage fails, the passport keeps working in
   * memory for this visit and `persisted` becomes false so the page can say it will not be remembered. */
  function createPassport(getStorage) {
    let memory = [];
    let persisted = true;
    let unsaved = false;  // the last write failed, so memory is newer than whatever storage holds

    function read() {
      if (unsaved) return memory.slice();  // never overwrite a stamp we could not save with an older stored copy
      try {
        const s = getStorage();
        if (!s) throw new Error("no storage");
        const raw = s.getItem(KEY);
        memory = raw ? sanitize(JSON.parse(raw)) : [];
        persisted = true;
      } catch (e) {
        persisted = false;  // keep whatever we already hold in memory
      }
      return memory.slice();
    }

    function write(list) {
      memory = list;
      try {
        const s = getStorage();
        if (!s) throw new Error("no storage");
        s.setItem(KEY, JSON.stringify(list));
        persisted = true;
        unsaved = false;
      } catch (e) {
        persisted = false;
        unsaved = true;
      }
    }

    return {
      list: read,
      get persisted() { return persisted; },
      /* Adds a place unless that rounded spot is already stamped. Returns { added, newEra }. */
      add(lat, lng, rock, date) {
        if (!rock || !Number.isFinite(lat) || !Number.isFinite(lng)) return { added: false, newEra: false };
        const list = read();
        const entry = makeEntry(lat, lng, rock, date);
        if (list.some((e) => e.lat === entry.lat && e.lng === entry.lng)) return { added: false, newEra: false };
        const newEra = !list.some((e) => e.era === entry.era);
        list.push(entry);
        write(list.slice(-MAX_ENTRIES));
        return { added: true, newEra };
      },
      clear() {
        memory = [];
        unsaved = false;
        try {
          const s = getStorage();
          if (s) s.removeItem(KEY);
        } catch (e) { /* storage blocked: nothing stored, nothing to clear */ }
      },
      stats() {
        const list = read();
        return { places: list.length, eras: distinctEras(list) };
      },
    };
  }

  const api = { WALK_THRESHOLD_M, MAX_ACCURACY_M, KEY, MAX_ENTRIES, distanceMeters, shouldRequestStory, rockChange,
    makeEntry, sanitize, distinctEras, createPassport };
  root.StelaLogic = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
