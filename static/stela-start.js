/* The start screen's flow, with no DOM and no browser APIs of its own: the page passes in what it needs.
 * That keeps one promise easy to test: nothing here asks for the location until Start is tapped,
 * and "Explore the map instead" never asks at all. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.StelaStart = factory();
})(typeof self !== "undefined" ? self : this, function () {
  /* deps.requestLocation(onFound, onFailed): asks the browser (called only from start()).
   * deps.showStart(), deps.showApp(): switch views. deps.setBusy(bool): the Start button's waiting state.
   * deps.onFound(lat, lng): run a lookup. deps.onUnavailable(): show the map with the friendly pin message. */
  function create(deps) {
    let pending = 0;   // each Start tap gets a number; choosing the map (or Home) cancels the pending one
    return {
      start() {
        const me = ++pending;
        deps.setBusy(true);
        deps.requestLocation(
          (lat, lng) => { if (me !== pending) return; deps.setBusy(false); deps.showApp(); deps.onFound(lat, lng); },
          () => { if (me !== pending) return; deps.setBusy(false); deps.showApp(); deps.onUnavailable(); }
        );
      },
      exploreMap() { pending++; deps.setBusy(false); deps.showApp(); },
      /* An example place from the start screen's list: opens the map there and runs the normal lookup as a pin visit.
       * It never asks for the location, and it cancels a Start that is still waiting for the browser's answer. */
      example(place) { pending++; deps.setBusy(false); deps.showApp(); deps.onExample(place); },
      home() { pending++; deps.setBusy(false); deps.showStart(); },
    };
  }
  return { create };
});
