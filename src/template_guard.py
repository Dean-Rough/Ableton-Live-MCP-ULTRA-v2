"""Detect third-party plug-ins in Live's default track templates.

Live builds every new track (including tracks created through the Remote
Script) from User Library/Defaults/Creating Tracks/<kind>/*.als. If that
template loads a plug-in whose files are missing, Live shows a modal inside
the API call and can abort when the call unwinds through its Python bridge
(seen with Neutron 5 on Live 12.4.16). The server checks the template first
so an agent stops and asks instead of crashing the user's set.
"""
from __future__ import annotations

import gzip
import os
import re
from pathlib import Path

from ableton_paths import default_user_library

_TEMPLATE_FOLDERS = {"midi": "MIDI Track", "audio": "Audio Track"}
_PLUGIN_BLOCK = re.compile(r"<(Vst3PluginInfo|VstPluginInfo|AuPluginInfo)\b.*?</\1>", re.S)
_PLUGIN_NAME = re.compile(r'<(?:PlugName|Name)\s+Value="([^"]*)"')
_cache: dict[tuple[str, float], list[str]] = {}

GUARD_DISABLE_ENV = "ABLETON_MCP_ALLOW_TEMPLATE_PLUGINS"


def default_track_template(kind: str, user_library: Path | None = None) -> Path | None:
    root = Path(user_library) if user_library else default_user_library()
    folder = root / "Defaults" / "Creating Tracks" / _TEMPLATE_FOLDERS[kind]
    if not folder.is_dir():
        return None
    templates = sorted(folder.glob("*.als"))
    return templates[0] if templates else None


def template_plugins(path: Path) -> list[str]:
    path = Path(path)
    key = (str(path), path.stat().st_mtime)
    if key in _cache:
        return _cache[key]
    raw = path.read_bytes()
    try:
        text = gzip.decompress(raw).decode("utf-8", "replace")
    except OSError:
        text = raw.decode("utf-8", "replace")
    names = []
    for match in _PLUGIN_BLOCK.finditer(text):
        found = [name for name in _PLUGIN_NAME.findall(match.group(0)) if name]
        names.append(found[0] if found else match.group(1))
    _cache[key] = names
    return names


def guard_track_creation(kind: str, allow: bool = False) -> None:
    """Raise ValueError when the default template would load third-party plug-ins."""
    if allow or os.environ.get(GUARD_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        template = default_track_template(kind)
        plugins = template_plugins(template) if template else []
    except Exception:
        return  # A guard that cannot read the template must never block the call.
    if plugins:
        raise ValueError(
            "Default %s track template loads %d third-party plug-ins (%s). A missing or broken one can open a "
            "modal inside the call and crash Live. Ask the user before retrying with allow_template_plugins:true, "
            "or duplicate a plug-in-free track with live_edit and rename it." % (kind, len(plugins), ", ".join(plugins))
        )
