import pytest

from app import geo


@pytest.fixture(autouse=True)
def no_picture_pacing(monkeypatch):
    """The picture lookups wait 0.5 s between requests in production; tests need no waiting."""
    monkeypatch.setattr(geo, "IMAGE_GAP", 0.0)
