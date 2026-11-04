# Ableton MCP v2

This is the active Ableton MCP v2 workspace: the upstream bschoepke/ableton-live-mcp foundation, extended with faster day-to-day Live controls, set lifecycle operations, and reliable fader automation. It is the repo to use when you want an agent that can actually operate inside Ableton, recover cleanly, and move from broad musical instructions to concrete clips, devices, routing, and mix changes.

![Ableton MCP v2 infographic](abletoninfographic.png)

Ever wanted to control Ableton with just your voice? Me too! I made this MCP server so I could just ask Codex to do anything in Ableton Live for me, while I was nap-trapped by my baby.
Unlike other Ableton MCPs I tried, this one can do pretty much anything that is possible via Ableton's Object model; the agent can just eval arbitrary python that runs inside Ableton. It also has some tools defined for common tasks so those work faster and more reliably. I had Codex CLI optimize this for hours with the new /goal command to prioritize low end-to-end latency, high reliability, low token usage, while maintaining full flexibility.
Things you can use it for: create MIDI clips, insert audio files, general Ableton questions (with this, your agent can see your whole live set), add tracks with different devices and effects, analyze harmony, analyze audio signals at any point in the signal chain, generate spectrograms, clip automation, setting up mastering or vocal processing chains, insert MIDI the agent finds from the web... it's very general purpose, I'm not sure what the limits are.
## How to setup
Just tell your AI agent (Codex, Claude Code, Cursor, Copilot, Gemini, etc.) to:
Set up the https://github.com/bschoepke/ableton-live-mcp MCP server for me
It should work on Mac and Windows with recent Ableton versions, but I have only tested it on Ableton Live Suite 12.3.8 on macOS Tahoe.
Back up your Live Set before using this. The MCP can edit your set directly and could corrupt it.
## How to update
git pull this repo or ask your agent to:
Update the https://github.com/bschoepke/ableton-live-mcp MCP server for me
## Provenance — source MCPs
This repo was consolidated from three Ableton MCP projects (full comparison in _Archive 2026-07 (pre-v2)/ABLETON_MCP_COMPARISON.md):
bschoepke/ableton-live-mcp — the base. All upstream code (stdio MCP server, Remote Script bridge on port 8765, generic Live object access, live_exec, browser/plugin search, clip automation, AgentAudioTap, AgentM4L, visual capture) comes from here, MIT-licensed (see LICENSE).
ahujasid/ableton-mcp — design reference for the ergonomic wrappers. live_create_midi_track, live_create_audio_track, live_rename_track, live_fire_clip, live_stop_clip, and live_set_tempo port the intent of its named tools onto the bschoepke bridge; no code was copied.
Simon-Kansara/ableton-live-mcp-server — evaluated as an AbletonOSC relay reference; nothing adopted (its local copy had a client/daemon protocol mismatch). Kept in the archive in case an OSC adapter is ever wanted.
The lifecycle tools (live_lifecycle_status, live_save_set, live_quit_ableton) and live_fade are original additions from the July 2026 handoff work; details below.
## Local additions (this fork)
This copy adds named lifecycle, convenience, and fade tools on top of upstream (server 0.2.0, Remote Script runtime lifecycle-fade-wrappers-1):
live_lifecycle_status, live_save_set, live_quit_ableton — save/quit via Live API when available, with GUI-workflow guidance returned when it isn't. Quit is scheduled so the RPC response can return first.
live_create_midi_track, live_create_audio_track, live_rename_track, live_fire_clip, live_stop_clip, live_set_tempo — ergonomic wrappers around common Live API calls.
live_fade — timed fader fade run inside Live (smoothstep default, linear optional); 0% = -inf dB, 100% = unity (0.85); target_percent > 100 requires allow_over_unity. Blocks Live's main thread for the duration; the server auto-raises the RPC timeout.
Reinstall the Remote Script and reload Live after updating (src/install_remote_script.py).
## Rough fork additions (server 0.3.0, Remote Script runtime rough-wrappers-2)
Twelve named wrappers for things the bridge previously only reached through `live_exec`, scoped from ahujasid/ableton-mcp and Tompz/ableton-mcp (see NOTICE.md):
live_song_context — compact musical context (tempo, signature, key/scale, loop, tracks with kind/devices/playing clip, returns, locators, grooves). Built for agents and plugins that generate material to fit the set.
live_track_set — mixer and state in one call: name, colour, volume, pan, sends, mute/solo/arm, monitoring, fold. Works on tracks, returns (`return_index`) and master.
live_clip_set — clip properties, loop/markers (writes ordered so Live never sees an invalid loop), launch settings, audio gain/pitch/warp, groove assignment.
live_clip_remove_notes — range-delete or clear MIDI notes. live_scene — list/create/fire/rename/duplicate/delete/stop_all. live_edit — delete or duplicate tracks, devices and clips.
live_locators — create/rename/jump/delete arrangement locators, confirmed after Live's next tick. live_track_routing, live_view, live_song_set (record, metronome, loop, punch, signature, groove, key/scale, tap tempo, capture MIDI), live_rack (inspect, macros, chains), live_undo.
`clip_select_notes` and `take_lanes` are available as `live_batch` methods to keep tools/list compact.
Track creation is guarded: if your default MIDI/Audio track template loads third-party plug-ins, `live_create_*_track` refuses until the call passes `allow_template_plugins: true` (a broken plug-in in that template can crash Live mid-call). `ABLETON_MCP_ALLOW_TEMPLATE_PLUGINS=1` disables the check.
## Demos
Here are a couple examples of live sets made from scratch with Codex in just a few minutes, along with their prompts. After it makes something, you can ask for follow up changes.

https://www.youtube.com/watch?v=8dRRrIY7NI0
The chat messages I sent to Codex to make this:
in ableton, make a self reflective song, with audio vocals (via macos say) and chip tunes and 80's drum machines. should be a real edm banger
Follow up prompts:
i want midi for everything but vocals please, with ableton devices. not prerendered audio for instruments
needs some fills
and should hit way harder after "3-2-1 i become the sound"
the vocals are squished too much (read too quickly), give them a little more length
the element is a bit muddy/indistinct. perhaps it needs simplification and more space, different instrument choice, i dunno

https://youtu.be/cLCHEV1jWQo
Prompt used to make this:
In Ableton, make a piano duet that tells the story of people debating the positive and negative merits of AI. The composition should be both beautiful and dynamic but surprising and fresh. Use Keyscape devices.
## Built in Agent Audio Tap Max for Live device
The MCP includes an "Agent Audio Tap" Max for Live device that enables the agent to capture audio signals at any part of the signal processing chain. This gives the agent a full feedback loop for mixing and mastering tasks: it can capture audio signals for further processing with custom python, then tweak your Ableton devices, and then repeat.

Example usage where I asked Codex to generate a spectrogram of two piano tracks I had:
<img width="3768" height="1028" alt="piano_tracks_first10_spectrograms" src="https://github.com/user-attachments/assets/6d2b6d9f-9a2c-4552-aa6c-91153de9df44" />
## Ideas
Control your external synthesizers and other hardware with the MCP
Ask it questions like "why does my mix sound muddy?" or "how do I sidechain my bass track?"
Ask it to do things like "add a chord track that fits with my melody" or "give me a basic backing track for me to noodle on my guitar with"
You can tell it use third party plugins (VSTs, audio units) like Serum and Keyscape
Tell your agent to incorporate your existing vocal samples, including asking it to trim silence and transcribe your audio samples before creatively incorporating them into your live set
Ask your agent to set up crazy user controlled DJ effects
Experiment with VJ plugins like Videosync to make music videos driven by your live set