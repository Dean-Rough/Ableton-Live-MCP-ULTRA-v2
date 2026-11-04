---
name: ableton-hermes-operator
description: Operate the user's local Hermes + Ableton Live MCP setup. Use when controlling Ableton through Hermes/MCP, opening or validating the SinChonies Live Set, using Ableton fader/fade conventions, fading tracks, launching the 80s beat clip, troubleshooting no sound, or saving/quitting Ableton safely.
---

# Ableton Hermes Operator

Use this skill for the local Hermes-driven Ableton workflow. The primary server is the bschoepke-style Ableton Live MCP (server name `ableton`), patched with named lifecycle, convenience, and fade tools (server version 0.3.0+, Remote Script runtime `rough-wrappers-2`; Rough named wrappers in AGENTS.md).

## MCP Setup

Known installed paths:

- MCP repo (installed): `/Users/tomservo/Documents/Codex/ableton-live-mcp`
- Canonical patched source: `/Volumes/Kenobi/Ableton MCPs/bschoepke:ableton-mcp/ableton-live-mcp-main`
- Remote Script: `/Users/tomservo/Music/Ableton/User Library/Remote Scripts/Ableton_Live_MCP`
- Agent Audio Tap device: `/Users/tomservo/Music/Ableton/User Library/Presets/Audio Effects/Max Audio Effect/AgentAudioTap.amxd`

If the new tools below are missing from the tool list, the installed copy is stale: sync it from the canonical patched source, reinstall the Remote Script (`src/install_remote_script.py`), and reload Live. `live_ping` reports the Remote Script runtime version; expect `rough-wrappers-2` or later.

Ableton must have Control Surface `Ableton_Live_MCP` selected in Preferences > Link, Tempo & MIDI.

Validate the bridge first:

```json
live_ping({})
```

Expected: `ok: true`. If a call stalls, check `live_bridge_status` before retrying mutations.

## Main Live Set

Primary set:

`/Users/tomservo/Desktop/Sin Chonies Gig Folder/SinChonies1 Project/SinChonies1.als`

Open it when needed:

```sh
open "/Users/tomservo/Desktop/Sin Chonies Gig Folder/SinChonies1 Project/SinChonies1.als"
```

Confirm through MCP:

```json
live_get({"ref": {"path": "live_set"}, "properties": ["name", "file_path"]})
```

Expected `name`: `SinChonies1`.

## Audio Interface

Ableton should use the Behringer XR18: CoreAudio, input and output `XR18 (18 In, 18 Out)`, Main Out `1/2`, Cue Out `1/2`. Verify through the Ableton UI if routing is suspect.

## Named Tools (preferred)

Prefer these named tools over raw `live_exec`/`live_call` equivalents:

| Tool | Use |
| --- | --- |
| `live_fade` | Timed fader fades (see Fade Convention) |
| `live_fire_clip` / `live_stop_clip` | Launch/stop Session clips by `track_index` + `clip_index` or ref |
| `live_set_tempo` | Set BPM (20–999) |
| `live_create_midi_track` / `live_create_audio_track` | Create tracks; `index: -1` appends; optional `name` |
| `live_rename_track` | Rename by `ref`, `track_index`, or `master: true` |
| `live_lifecycle_status` | Check whether Live API save/quit exists on this install |
| `live_save_set` | Save via `song.save()` if available; else returns the GUI workflow |
| `live_quit_ableton` | Save-then-quit; schedules quit so the response returns |

The generic tools (`live_get`, `live_set`, `live_call`, `live_exec`, `live_set_summary`) remain available for everything else.

## Track And Clip Context

Track 5 is the 80s beat track:

- UI track index: `5`; MCP/Python track index: `4`
- Track name: `5-80s Beat 90 bpm`; clip `80s Beat 90 bpm` in slot `0`

Launch it:

```json
live_fire_clip({"track_index": 4, "clip_index": 0})
live_transport({"action": "play"})
```

Locate tracks/clips when unsure:

```json
live_set_summary({"track_limit": 8, "clip_slot_limit": 12, "detail": true})
```

## Fader Convention

Use percent language for track faders:

- `0%` = off / `-inf dB`
- `100%` = unity / `0.0 dB` (Live fader value `0.8500000238418579`)
- Never set above `100%` unless the user explicitly overrides this rule (`live_fade` enforces this via `allow_over_unity`)

`50%` is an operational fader-position convention, not half perceived loudness.

## Fade Convention

`fade` means a smoothstep fade by default. Use the named tool — do not paste `live_exec` loops and do not implement fades as many MCP calls with sleeps:

```json
live_fade({"track_index": 4, "target_percent": 100, "duration": 10})
live_fade({"track_index": 4, "target_percent": 0, "duration": 10})
live_fade({"master": true, "target_percent": 100, "duration": 5})
```

Notes:

- Curve is smoothstep (`t*t*(3-2t)`); pass `curve: "linear"` only on request.
- Default `duration` 10s, default `steps` 40; max duration 60s.
- The fade runs inside Live and blocks Live's main thread for the duration (UI freezes; audio continues). The server auto-raises the RPC timeout to `duration + 10`.
- `target_percent` above 100 requires `allow_over_unity: true` (explicit user override only).

Fallback: if `live_fade` is unavailable (stale install), use the single `live_exec` smoothstep script from the handoff doc.

## Troubleshooting No Sound

Known prior causes: track 1 soloed (suppressing track 5) and Main volume at `-inf dB`.

Fixes:

```json
live_set({"ref": {"path": "live_set tracks 0"}, "property": "solo", "value": false})
live_parameter_set({"ref": {"path": "live_set master_track mixer_device volume"}, "value": 0.8500000238418579, "coerce": true})
```

## Save And Quit

Try the API path first, then fall back to GUI:

1. `live_lifecycle_status({})` — check `song_save_available` / `app_quit_available`.
2. `live_save_set({})` — if `saved: true`, done. If `saved: false`, the response includes the tested GUI workflow.
3. To quit: `live_quit_ableton({})` saves first by default and schedules the quit so the response can return. If save is impossible via API, it refuses unless `force_without_save: true`; prefer the GUI workflow in that case.
4. Expect the MCP connection to drop after a successful quit; treat `quit_requested: true` as success.

GUI fallback (tested, via Computer Use):

1. Inspect the Ableton Live window.
2. File menu > `Save Live Set` if enabled (disabled = already saved).
3. Live menu > `Quit Live`.
4. `App quit` = success.

Caveats: AppleScript save does not work; the GUI session must be active and capturable (fails if the Mac is locked or asleep).

## Operating Style

Be direct and operational. State the Ableton action, then perform it. Use the established language: `fade` = smoothstep fade, `0%` = off, `100%` = unity, no fader above `100%` without explicit override, save before quit.
