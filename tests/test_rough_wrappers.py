"""Fake-Live coverage for the Rough named wrappers in bridge.py."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_remote_bridge_fake_live import (  # noqa: E402
    FakeClip,
    FakeClipSlot,
    FakeDevice,
    FakeParameter,
    FakeTrack,
    FakeVector,
    make_bridge,
)


class Named:
    def __init__(self, name):
        self.name = name
        self.display_name = name


class FakeScene:
    def __init__(self, name):
        self.name = name
        self.color = 0
        self.fired = False

    def fire(self):
        self.fired = True


class FakeCue:
    def __init__(self, song, name, time):
        self._song = song
        self.name = name
        self.time = time

    def jump(self):
        self._song.current_song_time = self.time


def enrich_song(song):
    song.scenes = FakeVector([FakeScene("Intro"), FakeScene("Drop")])
    song.cue_points = FakeVector([])
    song.can_undo = True
    song.can_redo = False
    song.undo_calls = 0
    song.root_note = 9
    song.scale_name = "Minor"
    song.scale_intervals = [0, 2, 3, 5, 7, 8, 10]
    song.scale_mode = True
    song.metronome = False
    song.session_record = False
    song.loop = False
    song.groove_pool = types.SimpleNamespace(grooves=FakeVector([Named("MPC 16 Swing-56")]))
    song.view.selected_scene = song.scenes[0]

    def create_scene(index):
        scene = FakeScene("Scene %d" % (len(song.scenes) + 1))
        if index < 0:
            song.scenes.append(scene)
        else:
            song.scenes.insert(index, scene)
        return scene

    def duplicate_scene(index):
        song.scenes.insert(index + 1, FakeScene(song.scenes[index].name + " copy"))

    def delete_scene(index):
        del song.scenes[index]

    def undo():
        song.undo_calls += 1
        song.can_undo = song.undo_calls < 2
        song.can_redo = True

    def set_or_delete_cue():
        for cue in list(song.cue_points):
            if abs(cue.time - song.current_song_time) < 1e-3:
                song.cue_points.remove(cue)
                return
        song.cue_points.append(FakeCue(song, "", song.current_song_time))

    def delete_track(index):
        del song.tracks[index]

    def duplicate_track(index):
        song.tracks.insert(index + 1, FakeTrack(song.tracks[index].name + " copy"))

    song.create_scene = create_scene
    song.duplicate_scene = duplicate_scene
    song.delete_scene = delete_scene
    song.undo = undo
    song.set_or_delete_cue = set_or_delete_cue
    song.delete_track = delete_track
    song.duplicate_track = duplicate_track
    song.stop_all_clips = lambda quantized=True: setattr(song, "stopped_all", quantized)
    song.tap_tempo = lambda: setattr(song, "tapped", True)
    for track in list(song.tracks) + list(song.return_tracks) + [song.master_track]:
        track.mixer_device.panning = FakeParameter("Track Panning", 0.0, -1.0, 1.0)
        track.mixer_device.sends = FakeVector([FakeParameter("Send A", 0.0)])
        track.has_midi_input = True
        track.has_audio_input = False
        track.playing_slot_index = -1
    return song


def add_range_notes_api(clip):
    def get_notes_extended(from_pitch, pitch_span, from_time, time_span):
        return [
            note for note in clip._notes
            if from_pitch <= note.pitch < from_pitch + pitch_span and from_time <= note.start_time < from_time + time_span
        ]

    clip.get_notes_extended = get_notes_extended
    return clip


def test_scene_lifecycle(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    created = bridge._rpc_scene({"action": "create", "index": -1, "name": "Outro", "color": "#FF3366"})
    assert created["created"]["name"] == "Outro"
    assert song.scenes[-1].color == 0xFF3366
    assert bridge._rpc_scene({"action": "fire", "index": 1}) == {"fired": 1}
    assert song.scenes[1].fired is True
    bridge._rpc_scene({"action": "duplicate", "index": 0})
    assert song.scenes[1].name == "Intro copy"
    deleted = bridge._rpc_scene({"action": "delete", "index": 1})
    assert deleted["deleted"] == "Intro copy"
    assert [s["name"] for s in bridge._rpc_scene({})["scenes"]] == ["Intro", "Drop", "Outro"]
    with pytest.raises(ValueError):
        bridge._rpc_scene({"action": "fire"})


def test_delete_track_device_and_clip(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    song.tracks[0].clip_slots[0].delete_clip = lambda: setattr(song.tracks[0].clip_slots[0], "has_clip", False)
    result = bridge._rpc_delete({"target": "clip", "track_index": 0, "clip_index": 0})
    assert result["deleted"] == "Clip 1"
    assert bridge._rpc_delete({"target": "clip", "track_index": 0, "clip_index": 0})["deleted"] is None
    result = bridge._rpc_delete({"target": "device", "track_index": 0, "device_index": 0})
    assert result["device_count"] == 0
    result = bridge._rpc_delete({"target": "track", "track_index": 1})
    assert result["deleted"] == "Track 2"
    assert len(song.tracks) == 1


def test_duplicate_track(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    result = bridge._rpc_duplicate({"target": "track", "track_index": 0})
    assert result["new_index"] == 1
    assert song.tracks[1].name == "Track 1 copy"


def test_track_set_mixer_and_state(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    result = bridge._rpc_track_set({
        "track_index": 0, "volume": 0.6, "panning": -0.25,
        "sends": [{"index": 0, "value": 0.4}], "mute": True, "arm": True, "color": "0x112233",
    })
    changed = result["changed"]
    assert changed["volume"] == [0.85, 0.6]
    assert changed["panning"] == [0.0, -0.25]
    assert changed["send_0"] == [0.0, 0.4]
    assert song.tracks[0].mute is True and song.tracks[0].arm is True
    assert song.tracks[0].color == 0x112233
    assert result["track"] == {"kind": "track", "index": 0, "name": "Track 1"}

    song.return_tracks[0].can_be_armed = False
    with pytest.raises(ValueError):
        bridge._rpc_track_set({"return_index": 0, "arm": True})
    with pytest.raises(ValueError):
        bridge._rpc_track_set({"track_index": 0, "panning": 2.0})
    result = bridge._rpc_track_set({"return_index": 0, "solo": True})
    assert result["track"]["kind"] == "return"


def test_undo_stops_when_nothing_left(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    result = bridge._rpc_undo({"action": "undo", "count": 5})
    assert result["done"] == 2
    assert result["can_undo"] is False and result["can_redo"] is True


def test_clip_set_orders_loop_writes_and_rejects_audio_fields_on_midi(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    clip = song.tracks[0].clip_slots[0].clip
    original_setattr = FakeClip.__setattr__

    def guarded(self, name, value):
        if name == "loop_start" and hasattr(self, "loop_end") and value >= self.loop_end:
            raise RuntimeError("loop_start must be < loop_end")
        if name == "loop_end" and hasattr(self, "loop_start") and value <= self.loop_start:
            raise RuntimeError("loop_end must be > loop_start")
        original_setattr(self, name, value)

    monkeypatch.setattr(FakeClip, "__setattr__", guarded)
    result = bridge._rpc_clip_set({"track_index": 0, "clip_index": 0, "loop_start": 8.0, "loop_end": 12.0, "name": "Moved", "groove_index": 0})
    assert result["changed"]["loop"] == [[0.0, 4.0], [8.0, 12.0]]
    assert clip.name == "Moved"
    assert clip.groove.name == "MPC 16 Swing-56"
    with pytest.raises(ValueError):
        bridge._rpc_clip_set({"track_index": 0, "clip_index": 0, "gain": 0.5})


def test_clip_remove_notes_range_and_all(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    clip = add_range_notes_api(song.tracks[0].clip_slots[0].clip)
    template = clip._notes[0]
    clip._notes = [
        types.SimpleNamespace(**{**template.__dict__, "pitch": pitch, "start_time": start, "note_id": note_id})
        for note_id, (pitch, start) in enumerate([(60, 0.0), (60, 1.0), (64, 2.0), (67, 3.0)], start=1)
    ]
    total = len(clip._notes)
    pitch = 60
    result = bridge._rpc_clip_remove_notes({"track_index": 0, "clip_index": 0, "from_pitch": pitch, "pitch_span": 1})
    removed_first = result["removed"]
    assert removed_first == 2
    assert [note.pitch for note in clip._notes] == [64, 67]
    result = bridge._rpc_clip_remove_notes({"track_index": 0, "clip_index": 0})
    assert result["removed"] == total - removed_first
    assert clip._notes == []


def test_locators_create_rename_jump_delete(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    song.current_song_time = 2.0
    result = bridge._rpc_locators({"action": "create", "time": 16.0, "name": "Drop"})
    assert result == {"scheduled": "create", "time": 16.0}
    assert [(c.name, c.time) for c in song.cue_points] == [("Drop", 16.0)]
    assert song.current_song_time == 2.0
    again = bridge._rpc_locators({"action": "create", "time": 16.0, "name": "Big Drop"})
    assert again["existed"] is True and song.cue_points[0].name == "Big Drop"
    bridge._rpc_locators({"action": "rename", "index": 0, "new_name": "Chorus"})
    bridge._rpc_locators({"action": "jump", "name": "Chorus"})
    assert song.current_song_time == 16.0
    bridge._rpc_locators({"action": "delete", "time": 16.0})
    assert list(song.cue_points) == []
    song.is_playing = True
    with pytest.raises(ValueError):
        bridge._rpc_locators({"action": "create", "time": 4.0})


def test_track_routing_matches_by_name(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    track = song.tracks[0]
    options = [Named("Ext. In"), Named("Resampling"), Named("2-Bass")]
    track.available_input_routing_types = options
    track.input_routing_type = options[0]
    track.available_input_routing_channels = [Named("1/2"), Named("3")]
    track.input_routing_channel = Named("1/2")
    track.available_output_routing_types = [Named("Main")]
    track.output_routing_type = Named("Main")
    read = bridge._rpc_track_routing({"track_index": 0})
    assert read["input"]["type"] == "Ext. In" and read["output"]["type"] == "Main"
    result = bridge._rpc_track_routing({"track_index": 0, "direction": "input", "type": "resamp", "channel": "3"})
    assert result["changed"] == {"type": "Resampling", "channel": "3"}
    with pytest.raises(ValueError):
        bridge._rpc_track_routing({"track_index": 0, "direction": "input", "type": "nope"})


def test_song_set_accepts_note_names_and_actions(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    result = bridge._rpc_song_set({"root_note": "Eb", "scale_name": "Dorian", "metronome": True, "actions": ["tap_tempo", "stop_all_clips"]})
    assert song.root_note == 3 and song.scale_name == "Dorian" and song.metronome is True
    assert result["actions"] == ["tap_tempo", "stop_all_clips"]
    assert getattr(song, "tapped", False) is True
    result = bridge._rpc_song_set({"punch_in": True})
    assert result["changed"]["punch_in"] == "unsupported on this Live version"
    with pytest.raises(ValueError):
        bridge._rpc_song_set({"actions": ["explode"]})


def test_song_context_reports_key_tracks_and_grooves(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    song.view.selected_track = song.tracks[1]
    song.tracks[0].playing_slot_index = 0
    context = bridge._rpc_song_context({})
    assert context["key"]["root"] == "A" and context["key"]["scale"] == "Minor"
    assert context["signature"] == "4/4"
    assert context["tracks"][0]["playing_clip"] == "Clip 1"
    assert context["tracks"][0]["kind"] == "midi"
    assert context["selected_track"] == {"kind": "track", "index": 1, "name": "Track 2"}
    assert context["grooves"] == ["MPC 16 Swing-56"]
    assert context["selected_scene"] == "Intro"


def test_rack_inspect_and_set_macro(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    rack = FakeDevice()
    rack.name = "Audio Effect Rack"
    rack.class_name = "AudioEffectGroupDevice"
    rack.can_have_chains = True
    rack.visible_macro_count = 2
    rack.parameters = FakeVector([FakeParameter("Device On", 1.0), FakeParameter("Space", 0.0, 0.0, 127.0), FakeParameter("Drive", 10.0, 0.0, 127.0)])
    chain = types.SimpleNamespace(name="Wet", mute=False, solo=False, devices=FakeVector([FakeDevice()]))
    rack.chains = FakeVector([chain])
    song.tracks[1].devices = FakeVector([rack])
    inspected = bridge._rpc_rack({"track_index": 1, "device_index": 0})
    assert [m["name"] for m in inspected["macros"]] == ["Space", "Drive"]
    assert inspected["chains"][0]["devices"][0]["name"] == "Compressor"
    result = bridge._rpc_rack({"track_index": 1, "action": "set_macro", "macro": 0, "value": 64})
    assert result == {"macro": 0, "name": "Space", "before": 0.0, "value": 64.0}
    with pytest.raises(ValueError):
        bridge._rpc_rack({"track_index": 1, "action": "set_macro", "macro": 0, "value": 400})
    bridge._rpc_rack({"track_index": 1, "action": "chain", "chain": 0, "mute": True})
    assert chain.mute is True
    with pytest.raises(ValueError):
        bridge._rpc_rack({"track_index": 0, "device_index": 0})


def test_take_lanes_list_and_create(monkeypatch):
    bridge, song, _app = make_bridge(monkeypatch)
    enrich_song(song)
    track = song.tracks[0]
    track.take_lanes = FakeVector([])
    track.create_take_lane = lambda: track.take_lanes.append(Named("Take %d" % (len(track.take_lanes) + 1)))
    assert bridge._rpc_take_lanes({"track_index": 0})["take_lanes"] == []
    result = bridge._rpc_take_lanes({"track_index": 0, "action": "create"})
    assert result["take_lanes"] == [{"index": 0, "name": "Take 1"}]
