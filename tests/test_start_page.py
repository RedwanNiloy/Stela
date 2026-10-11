"""The start screen and icons: page structure and static files (the flow itself is in tests/start.test.js)."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

STATIC = Path(__file__).resolve().parent.parent / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_start_screen_comes_before_the_app_and_the_app_starts_hidden():
    assert HTML.index('id="start"') < HTML.index('id="app"')
    assert '<div id="app" hidden>' in HTML
    assert '<body data-view="start">' in HTML


def test_start_screen_has_the_required_text_and_buttons():
    assert "Want to know what was here before you?" in HTML
    assert "seas, swamps, ancient animals and early people" in HTML
    assert "millions of years" not in HTML.split('id="app"')[0]          # some rocks are young
    assert "Stela uses your location only to look up the ground under you. It is not stored." in HTML
    for text in (">Start<", ">Explore the map instead<", ">How it works<"):
        assert text in HTML


def test_geolocation_is_never_called_while_the_page_loads():
    # every use of the geolocation API must sit inside a function (a tap handler), never at the top level of the script
    script = HTML.split('<script src="stela-start.js"></script>')[1]
    for m in re.finditer(r"navigator\.geolocation\.(getCurrentPosition|watchPosition)", script):
        before = script[: m.start()]
        last_fn = max(before.rfind("function "), before.rfind("=>"))
        assert last_fn != -1, f"{m.group(0)} outside a function"


def test_head_links_icons_theme_colour_and_title():
    assert "<title>Stela: what was here before you?</title>" in HTML
    assert 'rel="icon" href="favicon.svg" type="image/svg+xml"' in HTML
    assert 'rel="icon" href="favicon-32.png"' in HTML
    assert 'rel="apple-touch-icon" href="apple-touch-icon.png"' in HTML
    assert 'name="theme-color"' in HTML and 'rel="manifest"' in HTML


def test_icons_and_favicon_ico_are_served_and_manifest_files_exist():
    client = TestClient(app)
    for path in ("/favicon.ico", "/favicon.svg", "/favicon-32.png", "/favicon-48.png", "/apple-touch-icon.png",
                 "/icon-192.png", "/icon-512.png", "/manifest.webmanifest", "/stela-start.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert len(r.content) > 100, path
