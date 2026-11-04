# Notices

## Rough named wrappers (server 0.3.0, Remote Script runtime `rough-wrappers-1`)

The named wrappers added in the Rough fork (`live_song_context`, `live_scene`,
`live_edit`, `live_track_set`, `live_undo`, `live_clip_set`,
`live_clip_remove_notes`, `live_locators`, `live_track_routing`, `live_view`,
`live_song_set`, `live_rack`, plus the batch-only `clip_select_notes` and
`take_lanes` methods) were written fresh on this bridge. Their scope, and
several Live Object Model findings they rely on, come from:

- **ahujasid/ableton-mcp** (MIT, Copyright (c) 2025 Siddharth Ahuja): scope of the
  scene, delete, transport, browser and arrangement tools.
- **Tompz/ableton-mcp** (MIT fork of the above): verified LOM behaviour against
  Live 12.4, notably:
  - `Clip.remove_notes_extended` argument order is `(from_pitch, pitch_span, from_time, time_span)`.
  - Setting `song.current_song_time` and calling `set_or_delete_cue()` in the same
    tick does not see the new position; the toggle must run one tick later.
  - `session_record` and similar transport flags read back one tick late.
  - Freeze, render, export, track reordering and mid-arrangement time-signature
    changes are not exposed to Remote Scripts at all.

Not ported, deliberately: the Supabase dataset recorder, telemetry, consent and
intent-rating tools (`submit_intent`, `rate_last_action`, etc.), and the
`investigate_*` introspection commands.
