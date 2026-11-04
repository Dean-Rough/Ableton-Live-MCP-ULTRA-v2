from __future__ import annotations

import gzip
from pathlib import Path

import pytest

import server as server_module
import template_guard

VST3_BLOCK = """<PluginDevice Id="{i}"><PluginDesc><Vst3PluginInfo Id="0"><WinPosX Value="1" />
<Preset><Vst3Preset Id="0"><Name Value="" /></Vst3Preset></Preset><Name Value="{name}" /></Vst3PluginInfo></PluginDesc></PluginDevice>"""
VST2_BLOCK = """<PluginDevice Id="9"><PluginDesc><VstPluginInfo Id="0"><PlugName Value="{name}" /></VstPluginInfo></PluginDesc></PluginDevice>"""


def write_template(library: Path, kind_folder: str, body: str, compress: bool = True) -> Path:
    folder = library / "Defaults" / "Creating Tracks" / kind_folder
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("Default %s.als" % kind_folder)
    xml = "<?xml version=\"1.0\"?><Ableton><LiveSet><Tracks>%s</Tracks></LiveSet></Ableton>" % body
    path.write_bytes(gzip.compress(xml.encode()) if compress else xml.encode())
    return path


class FakeBridge:
    def __init__(self):
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        return {"method": method, "params": params}


def call(server, name, arguments):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.delenv(template_guard.GUARD_DISABLE_ENV, raising=False)
    monkeypatch.setenv("ABLETON_USER_LIBRARY", str(tmp_path))
    template_guard._cache.clear()
    return tmp_path


def test_template_plugins_reads_vst3_and_vst2_names(library):
    body = VST3_BLOCK.format(i=1, name="Neutron 5") + VST3_BLOCK.format(i=2, name="Pro-Q 3") + VST2_BLOCK.format(name="Old Delay")
    path = write_template(library, "MIDI Track", body)
    assert template_guard.template_plugins(path) == ["Neutron 5", "Pro-Q 3", "Old Delay"]
    plain = write_template(library, "Audio Track", VST3_BLOCK.format(i=1, name="Wider"), compress=False)
    assert template_guard.template_plugins(plain) == ["Wider"]


def test_create_track_refuses_when_template_loads_plugins(library):
    write_template(library, "MIDI Track", VST3_BLOCK.format(i=1, name="Neutron 5"))
    bridge = FakeBridge()
    response = call(server_module.make_server(bridge), "live_create_midi_track", {"name": "Keys"})
    assert "error" in response
    assert "Neutron 5" in response["error"]["message"]
    assert "allow_template_plugins" in response["error"]["message"]
    assert bridge.calls == []


def test_create_track_allows_override_and_strips_flag(library):
    write_template(library, "MIDI Track", VST3_BLOCK.format(i=1, name="Neutron 5"))
    bridge = FakeBridge()
    response = call(server_module.make_server(bridge), "live_create_midi_track", {"name": "Keys", "allow_template_plugins": True})
    assert response["result"]["structuredContent"]["method"] == "create_midi_track"
    assert bridge.calls == [("create_midi_track", {"name": "Keys"})]


def test_create_track_passes_for_clean_or_missing_template(library, monkeypatch):
    bridge = FakeBridge()
    server = server_module.make_server(bridge)
    assert "result" in call(server, "live_create_audio_track", {})
    write_template(library, "Audio Track", "<AudioTrack />")
    assert "result" in call(server, "live_create_audio_track", {})
    write_template(library, "MIDI Track", VST3_BLOCK.format(i=1, name="Neutron 5"))
    monkeypatch.setenv(template_guard.GUARD_DISABLE_ENV, "1")
    assert "result" in call(server, "live_create_midi_track", {})
    assert [c[0] for c in bridge.calls] == ["create_audio_track", "create_audio_track", "create_midi_track"]


def test_unreadable_template_never_blocks(library, monkeypatch):
    write_template(library, "MIDI Track", VST3_BLOCK.format(i=1, name="Neutron 5"))
    monkeypatch.setattr(template_guard, "template_plugins", lambda _p: (_ for _ in ()).throw(PermissionError("nope")))
    bridge = FakeBridge()
    assert "result" in call(server_module.make_server(bridge), "live_create_midi_track", {})
