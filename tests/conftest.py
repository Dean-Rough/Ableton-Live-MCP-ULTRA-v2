import pytest


@pytest.fixture(autouse=True)
def _ignore_real_track_templates(monkeypatch):
    # Keep the suite hermetic: the create-track guard would otherwise read the
    # developer's real User Library default templates. test_template_guard.py
    # removes this to exercise the guard against fake libraries.
    monkeypatch.setenv("ABLETON_MCP_ALLOW_TEMPLATE_PLUGINS", "1")
