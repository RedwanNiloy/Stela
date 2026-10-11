/* The "Try these places" list on the start screen. A small hard-coded list, written by hand (never by the LLM).
 *
 * Each place was checked in two steps (2026-10-11):
 *   1. The coordinates against OpenStreetMap Nominatim and Wikipedia (Wikipedia's page coordinates, or Nominatim's own
 *      search result for the named place).
 *   2. The real app's own layers run on them, so that every card opens a lookup that returns a rock.
 * The reasons are general and neutral. They use only the rock the app itself returned for the spot (its name, its
 * age band and its setting) and never name a specific fossil. Nothing here is stored and no data source is added. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.StelaPlaces = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const PLACES = [
    { id: "jurassic-coast", name: "Jurassic Coast", where: "Charmouth, UK",
      reason: "Cliffs of sea mud and limestone from the age of early dinosaurs.", lat: 50.738, lng: -2.907 },
    { id: "dover", name: "Dover", where: "Kent, UK",
      reason: "Chalk sea floor from the age of dinosaurs.", lat: 51.125, lng: 1.313 },
    { id: "cincinnati", name: "Cincinnati region", where: "Ohio, USA",
      reason: "Sea-floor rock about 450 million years old, long before the dinosaurs.", lat: 39.101, lng: -84.513 },
    { id: "hell-creek", name: "Hell Creek area", where: "Montana, USA",
      reason: "Rock from the last stretch of the dinosaur age.", lat: 47.0, lng: -107.0 },
    { id: "la-brea", name: "La Brea Tar Pits", where: "Los Angeles, USA",
      reason: "Young Ice Age ground in the middle of a modern city.", lat: 34.063, lng: -118.356 },
    { id: "new-orleans", name: "New Orleans", where: "Louisiana, USA",
      reason: "Very young river ground, less than 12 thousand years old.", lat: 29.951, lng: -90.072 },
  ];

  /* What the lookup is labelled when it was opened from this list. */
  const EXAMPLE_LABEL = "Example place";
  const EXAMPLE_NOTE = "Example places are not added to your passport and do not earn badges or rank.";
  const LIST_NOTE = "Dense fossil records often reflect where research has been done. A quiet place may be understudied, not empty.";

  return { PLACES, EXAMPLE_LABEL, EXAMPLE_NOTE, LIST_NOTE };
});
