"""The "Try these places" list on the start screen: page structure and the rule that an example visit stores nothing."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

STATIC = Path(__file__).resolve().parent.parent / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_places_section_is_on_the_start_screen_before_the_app():
    start = HTML.split('id="app"')[0]
    assert 'id="places"' in start and "Try these places" in start
    assert 'id="places-row"' in start and 'id="places-note"' in start


def test_places_script_is_loaded_and_served():
    assert '<script src="stela-places.js"></script>' in HTML
    r = TestClient(app).get("/stela-places.js")
    assert r.status_code == 200 and "Dover" in r.text


def test_example_visit_skips_the_passport_and_badges():
    # in explore(): the example branch must come first and must not call stampPassport / awardBadge
    body = HTML.split("async function explore(")[1].split("function loadFossils")[0]
    branch, rest = body.split("if (opts.example) {")[1].split("} else {", 1)
    assert "stampPassport" not in branch and "awardBadge" not in branch
    assert "stampPassport(" in rest and "awardBadge(" in rest


def test_example_visit_never_asks_for_the_location_and_is_not_real():
    fn = HTML.split("function exampleVisit(")[1].split("(function buildPlaces")[0]
    assert "geolocation" not in fn and "requestPosition" not in fn and "locateMe" not in fn
    assert "real: true" not in fn and "example: place" in fn


def test_a_retry_after_a_failed_example_lookup_stays_an_example():
    assert "{ real: lastLookup.real, example: lastLookup.example }" in HTML


def test_example_label_text():
    assert "Example place" in (STATIC / "stela-places.js").read_text(encoding="utf-8")
    assert re.search(r"A quiet place may be understudied, not empty\.", (STATIC / "stela-places.js").read_text(encoding="utf-8"))


def test_start_backdrop_is_decorative_inline_svg_with_no_external_image():
    start = HTML.split('id="app"')[0]
    block = start.split('class="start-bg"')[1].split("</div>")[0]
    assert 'aria-hidden="true"' in start.split('class="start-bg"')[0][-60:] or "aria-hidden" in block
    assert "<svg" in block and "<img" not in block and "http" not in block.replace("http://www.w3.org", "")
    css = (STATIC / "stela.css").read_text(encoding="utf-8")
    assert "url(" not in css.split("Start screen: blurred fossil")[1]   # no image file, no new data source
