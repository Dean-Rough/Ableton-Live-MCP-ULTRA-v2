from __future__ import annotations

import server as server_module

ROUGH_TOOLS = {
    "live_song_context": "song_context",
    "live_scene": "scene",
    "live_edit": "edit",
    "live_track_set": "track_set",
    "live_undo": "undo",
    "live_clip_set": "clip_set",
    "live_clip_remove_notes": "clip_remove_notes",
    "live_track_routing": "track_routing",
    "live_view": "view",
    "live_song_set": "song_set",
    "live_rack": "rack",
}

SAMPLE_ARGS = {
    "live_edit": {"op": "delete", "target": "track", "track_index": 1},
}


class FakeBridge:
    def __init__(self, scripted=None):
        self.calls = []
        self.scripted = list(scripted or [])

    def request(self, method, params):
        self.calls.append((method, params))
        if self.scripted:
            return self.scripted.pop(0)
        return {"method": method, "params": params}


def call(server, name, arguments):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})


def test_rough_tools_are_listed_with_strict_schemas():
    server = server_module.make_server(FakeBridge())
    tools = {tool["name"]: tool for tool in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    for name in list(ROUGH_TOOLS) + ["live_locators"]:
        assert name in tools, name
        assert tools[name]["inputSchema"].get("additionalProperties") is False, name


def test_rough_tools_forward_to_bridge_methods():
    bridge = FakeBridge()
    server = server_module.make_server(bridge)
    for name, method in ROUGH_TOOLS.items():
        response = call(server, name, SAMPLE_ARGS.get(name, {}))
        assert response["result"]["structuredContent"]["method"] == method, name
    assert [c[0] for c in bridge.calls] == list(ROUGH_TOOLS.values())


def test_locators_create_is_confirmed_by_follow_up_listing(monkeypatch):
    monkeypatch.setattr(server_module.time, "sleep", lambda _s: None)
    bridge = FakeBridge([
        {"scheduled": "create", "time": 16.0},
        {"locators": []},
        {"locators": [{"index": 0, "name": "Drop", "time": 16.0}]},
    ])
    server = server_module.make_server(bridge)
    result = call(server, "live_locators", {"action": "create", "time": 16, "name": "Drop"})["result"]["structuredContent"]
    assert result["confirmed"] is True
    assert result["locator"]["name"] == "Drop"
    assert [c[0] for c in bridge.calls] == ["locators", "locators", "locators"]


def test_locators_delete_reports_unconfirmed_without_retrying_mutation(monkeypatch):
    monkeypatch.setattr(server_module.time, "sleep", lambda _s: None)
    still_there = {"locators": [{"index": 0, "name": "Drop", "time": 16.0}]}
    bridge = FakeBridge([{"scheduled": "delete", "time": 16.0}] + [still_there] * 12)
    server = server_module.make_server(bridge)
    result = call(server, "live_locators", {"action": "delete", "time": 16})["result"]["structuredContent"]
    assert result["confirmed"] is False
    mutating = [c for c in bridge.calls if c[1].get("action") != "list"]
    assert len(mutating) == 1


def test_locators_list_passes_through():
    bridge = FakeBridge([{"locators": []}])
    server = server_module.make_server(bridge)
    result = call(server, "live_locators", {"action": "list"})["result"]["structuredContent"]
    assert result == {"locators": []}


def test_every_tool_schema_is_an_object():
    server = server_module.make_server(FakeBridge())
    tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    bad = [tool["name"] for tool in tools if tool["inputSchema"].get("type") != "object"]
    assert bad == []


def test_union_typed_arguments_pass_server_validation():
    bridge = FakeBridge()
    server = server_module.make_server(bridge)
    ok = [
        ("live_track_set", {"track_index": 1, "color": "#FF3366", "monitoring": "in"}),
        ("live_track_set", {"track_index": 1, "color": 16724838, "monitoring": 2}),
        ("live_song_set", {"root_note": "Eb", "scale_name": "Dorian"}),
        ("live_song_set", {"root_note": 3}),
        ("live_clip_set", {"track_index": 0, "clip_index": 0, "color": "#112233"}),
        ("live_scene", {"action": "create", "color": 255}),
    ]
    for name, arguments in ok:
        response = call(server, name, arguments)
        assert "error" not in response, (name, response)
    assert len(bridge.calls) == len(ok)


def test_union_typed_arguments_still_reject_wrong_types():
    server = server_module.make_server(FakeBridge())
    response = call(server, "live_track_set", {"track_index": 1, "color": 1.5})
    assert "error" in response
    assert "string or integer" in response["error"]["message"]
