from __future__ import absolute_import, print_function

import json
import hashlib
import os
import socket
import sys
import threading
import time
import traceback

import Live
from _Framework.ControlSurface import ControlSurface


HOST = "127.0.0.1"
PORT = 8765
CLIENT_TIMEOUT = 10
MAX_REQUEST_BYTES = 1024 * 1024
MAX_OBJECTS = 2000
DEFAULT_MAX_ITEMS = 200
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_STRING_LENGTH = 4096
DEFAULT_CHILD_LIMIT = 200
DEFAULT_MAIN_THREAD_TIMEOUT = 30
DEFAULT_MAIN_THREAD_STALL_COOLDOWN = 10
DEFAULT_BROWSER_ROOTS = ("instruments", "audio_effects", "midi_effects", "drums", "samples", "sounds", "packs", "plugins", "user_library", "user_folders", "current_project")
REMOTE_SCRIPT_RUNTIME_VERSION = "rough-wrappers-1"
FADE_UNITY_VALUE = 0.8500000238418579
FADE_MAX_DURATION = 60.0
FADE_DEFAULT_STEPS = 40
GUI_LIFECYCLE_WORKFLOW = {
    "save": [
        "Use Computer Use to inspect the Ableton Live window.",
        "Open the File menu.",
        "If Save Live Set is enabled, click it.",
        "If Save Live Set is disabled, treat the set as already saved.",
    ],
    "quit": [
        "Save first through the File menu if possible.",
        "Open the Live application menu.",
        "Click Quit Live.",
        "If Computer Use reports App quit, treat quit as successful.",
    ],
    "notes": [
        "AppleScript save did not work in prior testing.",
        "The GUI session must be active and capturable; locked/asleep displays block this workflow.",
    ],
}
AGENT_AUDIO_TAP_HOST = "127.0.0.1"
AGENT_AUDIO_TAP_PORT = 17654
AGENT_M4L_HOST = "127.0.0.1"
AGENT_M4L_PORT = 17655
AGENT_M4L_PORT_SPAN = 30000
AGENT_M4L_MAX_UDP_BYTES = 8192
AGENT_M4L_RECOVERY_PATCH_KEYS = (
    "objects", "connections", "ui_bindings", "bindings", "webui", "webuis",
    "device_width", "devicewidth", "width", "device_height", "deviceheight", "height",
)
LEGACY_NOTE_API_NAMES = (
    "set_notes",
    "get_notes",
    "remove_notes",
    "replace_selected_notes",
    "select_all_notes",
    "deselect_all_notes",
)


def _runtime_code_fingerprint():
    payload = {
        "constants": _runtime_constant_signatures(),
        "functions": _runtime_function_signatures(globals()),
        "methods": _runtime_function_signatures(AbletonLiveMCP.__dict__),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _runtime_constant_signatures():
    items = []
    for name, value in sorted(globals().items()):
        if name.isupper() and isinstance(value, (str, int, float, bool, tuple, list)):
            items.append((name, repr(value)))
    return items


def _runtime_function_signatures(namespace):
    items = []
    for name, value in sorted(namespace.items()):
        code = getattr(value, "__code__", None)
        if code is not None:
            items.append((name, _code_signature(code)))
    return items


def _code_signature(code):
    return {
        "argcount": code.co_argcount,
        "code": hashlib.sha256(code.co_code).hexdigest(),
        "consts": [_constant_signature(value) for value in code.co_consts],
        "flags": code.co_flags,
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
    }


def _constant_signature(value):
    if hasattr(value, "co_code"):
        return _code_signature(value)
    return repr(value)


class _EncodedResult(list):
    pass


def _temp_file(name):
    root = os.environ.get("ABLETON_MCP_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".ableton-live-mcp")
    try:
        os.makedirs(root)
    except OSError:
        pass
    return os.path.join(root, name)


class AbletonLiveMCP(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._objects = {}
        self._listeners = {}
        self._events = []
        self._server = None
        self._running = True
        self._main_thread_id = threading.current_thread().ident
        self._main_thread_timeout_count = 0
        self._main_thread_last_timeout_at = 0.0
        self._main_thread_last_timeout_method = None
        self._main_thread_last_success_at = 0.0
        self._main_thread_stall_until = 0.0
        self._main_thread_request_lock = threading.BoundedSemaphore(1)
        self._main_thread_busy_method = None
        self._main_thread_busy_since = 0.0
        self._main_thread_busy_timed_out = False
        self._handler_slots = threading.BoundedSemaphore(16)
        with self.component_guard():
            self._start_server()

    def disconnect(self):
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
        self._remove_all_listeners()
        ControlSurface.disconnect(self)

    def _start_server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, PORT))
        sock.listen(8)
        self._server = sock
        thread = threading.Thread(target=self._accept_loop)
        thread.daemon = True
        thread.start()
        self.log_message("Ableton_Live_MCP listening on %s:%s" % (HOST, PORT))

    def _accept_loop(self):
        while self._running:
            try:
                client, _addr = self._server.accept()
                thread = threading.Thread(target=self._handle_client, args=(client,))
                thread.daemon = True
                thread.start()
            except Exception:
                if self._running:
                    self.log_message("Ableton_Live_MCP accept error: %s" % traceback.format_exc())

    def _handle_client(self, client):
        try:
            client.settimeout(CLIENT_TIMEOUT)
            buffer = b""
            while self._running:
                data, buffer = self._read_line(client, buffer)
                if not data:
                    break
                request = json.loads(data.decode("utf-8"))
                acquired = False
                if request.get("method") == "bridge_status":
                    response = self._dispatch(request)
                else:
                    acquired = self._handler_slots.acquire(False)
                    if acquired:
                        try:
                            response = self._dispatch(request)
                        finally:
                            self._handler_slots.release()
                            acquired = False
                    else:
                        response = {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32000, "message": "Too many concurrent Ableton MCP requests"}}
                client.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
        except socket.timeout:
            pass
        except Exception as exc:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": str(exc)}}
            try:
                client.sendall((json.dumps(err, separators=(",", ":")) + "\n").encode("utf-8"))
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _read_line(self, client, buffer):
        chunks = [buffer] if buffer else []
        total = len(buffer)
        if b"\n" in buffer:
            line, remainder = buffer.split(b"\n", 1)
            return line, remainder
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                raise ValueError("Request exceeds maximum size")
            if b"\n" in chunk:
                break
        data = b"".join(chunks)
        if b"\n" in data:
            line, remainder = data.split(b"\n", 1)
            return line, remainder
        return data, b""

    def _dispatch(self, request):
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        try:
            if method == "bridge_status":
                result = self._rpc_bridge_status(params)
            else:
                result = self._run_on_main(method, params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            error = {"code": -32000, "message": str(exc)}
            if params.get("include_traceback"):
                error["data"] = traceback.format_exc()
            return {"jsonrpc": "2.0", "id": req_id, "error": error}

    def _run_on_main(self, method, params):
        if threading.current_thread().ident == self._main_thread_id:
            self._check_expected_set_signature(params)
            value = getattr(self, "_rpc_" + method)(params)
            if isinstance(value, _EncodedResult):
                return value
            return self._encode(value, self._encode_options(params))
        self._raise_if_main_thread_in_stall_cooldown(method, params)
        gate_released = self._acquire_main_thread_request_gate(method)
        done = threading.Event()
        result = {"value": None, "error": None}
        abandoned = {"value": False}
        started = {"value": False}

        def invoke():
            if abandoned["value"]:
                done.set()
                self._release_main_thread_request_gate(gate_released)
                return
            try:
                started["value"] = True
                self._check_expected_set_signature(params)
                value = getattr(self, "_rpc_" + method)(params)
                if isinstance(value, _EncodedResult):
                    result["value"] = value
                else:
                    result["value"] = self._encode(value, self._encode_options(params))
                self._mark_main_thread_success()
            except Exception:
                result["error"] = sys.exc_info()
            finally:
                done.set()
                self._release_main_thread_request_gate(gate_released)

        try:
            self.schedule_message(0, invoke)
        except Exception:
            self._release_main_thread_request_gate(gate_released)
            raise
        timeout = self._main_thread_timeout(params)
        if not done.wait(timeout):
            abandoned["value"] = True
            self._mark_main_thread_timeout(method, params)
            if not started["value"]:
                self._release_main_thread_request_gate(gate_released)
            raise RuntimeError("Timed out waiting for Live main thread during %s after %.3gs" % (method, timeout))
        if result["error"]:
            exc_type, exc, tb = result["error"]
            raise exc.with_traceback(tb)
        return result["value"]

    def _rpc_bridge_status(self, _params):
        now = time.time()
        last_timeout_at = getattr(self, "_main_thread_last_timeout_at", 0.0)
        last_success_at = getattr(self, "_main_thread_last_success_at", 0.0)
        stall_until = getattr(self, "_main_thread_stall_until", 0.0)
        busy_since = getattr(self, "_main_thread_busy_since", 0.0)
        busy_method = getattr(self, "_main_thread_busy_method", None)
        main_thread = {
            "thread_id": getattr(self, "_main_thread_id", None),
            "timeouts": getattr(self, "_main_thread_timeout_count", 0),
            "last_timeout_method": getattr(self, "_main_thread_last_timeout_method", None),
            "stall_cooldown_remaining": max(0.0, stall_until - now),
            "in_flight_method": busy_method,
            "in_flight_timed_out": bool(getattr(self, "_main_thread_busy_timed_out", False)),
        }
        if busy_method and busy_since:
            main_thread["in_flight_age"] = max(0.0, now - busy_since)
        if last_timeout_at:
            main_thread["last_timeout_age"] = max(0.0, now - last_timeout_at)
        if last_success_at:
            main_thread["last_success_age"] = max(0.0, now - last_success_at)
        return {
            "ok": True,
            "running": bool(getattr(self, "_running", False)),
            "server_thread_responsive": True,
            "handler_thread_id": threading.current_thread().ident,
            "main_thread": main_thread,
        }

    def _raise_if_main_thread_in_stall_cooldown(self, method, params):
        if params.get("force_main_thread_probe"):
            return
        remaining = getattr(self, "_main_thread_stall_until", 0.0) - time.time()
        if remaining > 0:
            previous = getattr(self, "_main_thread_last_timeout_method", None) or "unknown"
            raise RuntimeError(
                "Live main thread is in stall cooldown after %s timed out; refusing to enqueue %s for %.1fs. "
                "Use live_bridge_status for socket-thread health, wait for cooldown, or restart/reload Live before sending mutations."
                % (previous, method, remaining)
            )

    def _ensure_main_thread_request_gate(self):
        if not hasattr(self, "_main_thread_request_lock"):
            self._main_thread_request_lock = threading.BoundedSemaphore(1)
            self._main_thread_busy_method = None
            self._main_thread_busy_since = 0.0
            self._main_thread_busy_timed_out = False

    def _acquire_main_thread_request_gate(self, method):
        self._ensure_main_thread_request_gate()
        if self._main_thread_request_lock.acquire(False):
            self._main_thread_busy_method = method
            self._main_thread_busy_since = time.time()
            self._main_thread_busy_timed_out = False
            return {"value": False}
        busy_method = getattr(self, "_main_thread_busy_method", None) or "unknown"
        busy_since = getattr(self, "_main_thread_busy_since", 0.0)
        busy_for = max(0.0, time.time() - busy_since) if busy_since else 0.0
        suffix = " after that request already timed out" if getattr(self, "_main_thread_busy_timed_out", False) else ""
        raise RuntimeError(
            "Live main-thread request already in flight (%s for %.1fs%s); refusing to enqueue %s. "
            "Use live_bridge_status for socket-thread health, then recover or restart/reload Live before sending mutations."
            % (busy_method, busy_for, suffix, method)
        )

    def _release_main_thread_request_gate(self, released):
        if released["value"]:
            return
        released["value"] = True
        self._main_thread_busy_method = None
        self._main_thread_busy_since = 0.0
        self._main_thread_busy_timed_out = False
        self._main_thread_request_lock.release()

    def _mark_main_thread_success(self):
        self._main_thread_last_success_at = time.time()
        self._main_thread_stall_until = 0.0

    def _mark_main_thread_timeout(self, method, params):
        now = time.time()
        self._main_thread_timeout_count = getattr(self, "_main_thread_timeout_count", 0) + 1
        self._main_thread_last_timeout_at = now
        self._main_thread_last_timeout_method = method
        if getattr(self, "_main_thread_busy_method", None) == method:
            self._main_thread_busy_timed_out = True
        cooldown = float(params.get("stall_cooldown") or DEFAULT_MAIN_THREAD_STALL_COOLDOWN)
        if cooldown > 0:
            self._main_thread_stall_until = max(getattr(self, "_main_thread_stall_until", 0.0), now + cooldown)

    def _main_thread_timeout(self, params):
        timeout = float(params.get("timeout") or DEFAULT_MAIN_THREAD_TIMEOUT)
        if not (params.get("strict_timeout") or params.get("timeout_strict")):
            timeout = max(timeout, float(DEFAULT_MAIN_THREAD_TIMEOUT))
        return timeout

    def _rpc_ping(self, _params):
        app = Live.Application.get_application()
        version = app.get_version_string() if hasattr(app, "get_version_string") else "unknown"
        return {"ok": True, "version": version, "major": self._major_version(version), "remote_script": self._remote_script_info()}

    def _remote_script_info(self):
        path = globals().get("__file__", "")
        info = {"path": str(path), "runtime_version": REMOTE_SCRIPT_RUNTIME_VERSION}
        try:
            info["runtime_code_sha256"] = _runtime_code_fingerprint()
        except Exception as exc:
            info["runtime_code_error"] = str(exc)
        try:
            with open(path, "rb") as handle:
                info["bridge_sha256"] = hashlib.sha256(handle.read()).hexdigest()
        except Exception as exc:
            info["hash_error"] = str(exc)
        return info

    def _rpc_agent_audio_tap(self, params):
        command = params.get("command")
        if command not in ("open", "start", "stop", "status"):
            raise ValueError("command must be open, start, stop, or status")
        path = params.get("path")
        if command == "open" and not path:
            raise ValueError("path is required for open")
        args = [command]
        if path:
            args.append(path)
        request_id = params.get("id")
        command_id = params.get("command_id") or hashlib.sha1(json.dumps({
            "request_id": request_id,
            "command": command,
            "path": path,
            "time": time.time(),
        }, sort_keys=True).encode("utf-8")).hexdigest()
        command_file = params.get("command_file") or _temp_file("agent_audio_tap_command.json")
        with open(command_file, "w") as handle:
            payload = {"id": command_id, "command": command, "path": path}
            if request_id is not None:
                payload["request_id"] = request_id
            json.dump(payload, handle, separators=(",", ":"))
        sent = False
        payload_size = 0
        if params.get("udp", False):
            payload = self._osc_message("/agent_audio_tap", args)
            payload_size = len(payload)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(payload, (AGENT_AUDIO_TAP_HOST, int(params.get("port") or AGENT_AUDIO_TAP_PORT)))
                sent = True
            finally:
                sock.close()
        return {"sent": sent, "command": command, "path": path, "bytes": payload_size, "command_file": command_file, "command_id": command_id}

    def _rpc_agent_audio_tap_setup(self, params):
        song = self.song()
        placement = params.get("placement") or "master"
        if placement == "master":
            target_track = song.master_track
        elif params.get("target_track"):
            target_track = self._resolve(params.get("target_track"))
        else:
            raise ValueError("target_track is required unless placement is master")

        if params.get("remove_existing"):
            self._delete_named_devices("AgentAudioTap")

        loaded = False
        if not self._track_has_device(target_track, "AgentAudioTap"):
            item = self._find_browser_item_named("AgentAudioTap")
            if item is None:
                raise KeyError("Could not find AgentAudioTap in the Live browser; install/build the M4L device first")
            song.view.selected_track = target_track
            Live.Application.get_application().browser.load_item(item)
            loaded = True

        solo_ref = params.get("solo_track")
        if solo_ref:
            solo_track = self._resolve(solo_ref)
            if params.get("exclusive_solo", True):
                for track in song.tracks:
                    try:
                        track.solo = self._same_live_object(track, solo_track)
                    except Exception:
                        pass
            else:
                solo_track.solo = True

        if params.get("stop", True):
            self._stop_transport(song)
        if params.get("reset_time") is not None:
            self._seek_song(song, float(params.get("reset_time")))

        return {
            "target_track": getattr(target_track, "name", ""),
            "loaded": loaded,
            "devices": [getattr(device, "name", "") for device in getattr(target_track, "devices", [])],
            "time": getattr(song, "current_song_time", None),
            "playing": bool(getattr(song, "is_playing", False)),
        }

    def _rpc_agent_m4l_device(self, params):
        instance_id = self._agent_m4l_slug(params.get("instance_id") or params.get("name") or "device")
        role = self._agent_m4l_role(params.get("role"))
        device_slug = self._agent_m4l_slug(params.get("name")) if params.get("name") else instance_id
        device_name = str(params.get("device_name") or ("AgentM4L_%s_%s" % (role, device_slug)))
        command_file = str(params.get("command_file") or _temp_file("agent_m4l_%s.json" % instance_id))
        status_file = str(params.get("status_file") or _temp_file("agent_m4l_%s_status.json" % instance_id))
        command = params.get("command") or ("set" if params.get("values") or params.get("parameters") else ("update" if params.get("patch") or params.get("spec") or params.get("webui") or params.get("webuis") else "status"))
        patch = params.get("patch") or params.get("spec")
        if patch is None and (params.get("webui") or params.get("webuis")):
            patch = {}
        if patch is not None and params.get("webui"):
            patch = dict(patch)
            patch["webui"] = params.get("webui")
        if patch is not None and params.get("webuis"):
            patch = dict(patch)
            patch["webuis"] = params.get("webuis")
        if patch is not None and params.get("device_width") is not None:
            patch = dict(patch)
            patch.setdefault("device_width", params.get("device_width"))
        if patch is not None and params.get("device_height") is not None:
            patch = dict(patch)
            patch.setdefault("device_height", params.get("device_height"))
        if patch is None and command in ("set", "status"):
            patch = self._agent_m4l_recovery_patch(command_file)
        command_hash = {
            "command": command,
            "instance_id": instance_id,
            "patch": patch,
            "values": params.get("values"),
            "parameters": params.get("parameters"),
            "webui": params.get("webui"),
            "webuis": params.get("webuis"),
        }
        if not params.get("id"):
            command_hash["nonce"] = time.time()
        command_id = params.get("id") or hashlib.sha1(json.dumps(command_hash, sort_keys=True).encode("utf-8")).hexdigest()
        payload = {
            "id": command_id,
            "command": command,
            "role": role,
            "instance_id": instance_id,
            "patch": patch,
            "values": params.get("values"),
            "parameters": params.get("parameters"),
            "webui": params.get("webui"),
            "webuis": params.get("webuis"),
        }
        write_command_file = params.get("write_command_file")
        if write_command_file is None:
            write_command_file = True
        if write_command_file:
            self._agent_m4l_write_recovery_patch(command_file, patch)
            with open(command_file, "w") as handle:
                json.dump(self._agent_m4l_command_file_payload(payload), handle, separators=(",", ":"))

        sent = False
        udp_bytes = 0
        udp_skipped = False
        if params.get("udp", True):
            port = int(params.get("port") or self._agent_m4l_port(instance_id))
            raw = json.dumps(self._agent_m4l_udp_payload(payload), separators=(",", ":"))
            message = self._osc_message("/agent_m4l", [instance_id, raw])
            udp_bytes = len(message)
            if udp_bytes <= AGENT_M4L_MAX_UDP_BYTES:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    try:
                        sock.sendto(message, (AGENT_M4L_HOST, port))
                        sent = True
                    except OSError:
                        sent = False
                finally:
                    sock.close()
            else:
                udp_skipped = True
        else:
            port = int(params.get("port") or self._agent_m4l_port(instance_id))

        loaded = False
        load_error = None
        target_track = None
        if params.get("target_track"):
            target_track = self._resolve(params.get("target_track"))
        elif params.get("ref"):
            target_track = self._resolve(params.get("ref"))
        if target_track is not None and params.get("load", True):
            try:
                if not self._track_has_device(target_track, device_name, role):
                    loaded = self._load_agent_m4l_device(target_track, device_name, role, params)
            except Exception as exc:
                load_error = str(exc)
        triggered = False
        if target_track is not None:
            target_device = self._find_track_device(target_track, device_name, role)
            if target_device is not None:
                triggered = self._trigger_agent_m4l_poll(target_device)

        result = {
            "sent": sent,
            "command": command,
            "role": role,
            "instance_id": instance_id,
            "device_name": device_name,
            "command_id": command_id,
            "command_file": command_file,
            "command_file_written": bool(write_command_file),
            "status_file": status_file,
            "port": port,
            "loaded": loaded,
            "udp_bytes": udp_bytes,
            "udp_skipped": udp_skipped,
            "triggered": triggered,
        }
        if load_error:
            result["load_error"] = load_error
        if target_track is not None:
            result["track"] = self._track_summary(target_track, None, 0, 16, 0)
        return result

    def _rpc_agent_m4l_cleanup(self, params):
        delete = bool(params.get("delete", False))
        prefix = str(params.get("name_prefix") if params.get("name_prefix") is not None else "AgentM4L_")
        name_contains = str(params.get("name_contains") or "").lower()
        role = self._agent_m4l_role(params.get("role")) if params.get("role") else ""
        limit = int(params.get("limit") if params.get("limit") is not None else 128)
        max_delete = int(params.get("max_delete") if params.get("max_delete") is not None else 0)
        tracks = self._agent_m4l_cleanup_tracks(params)
        matches = []
        delete_items = []
        for track_info in tracks:
            track = track_info["track"]
            devices = list(getattr(track, "devices", []))
            for index, device in enumerate(devices):
                name = str(getattr(device, "name", ""))
                if prefix and not name.startswith(prefix):
                    continue
                if name_contains and name_contains not in name.lower():
                    continue
                if role and not name.startswith("AgentM4L_%s_" % role):
                    continue
                item = {
                    "track": track_info["name"],
                    "track_path": track_info["path"],
                    "device_index": index,
                    "device_name": name,
                    "class_name": self._device_class_name(device),
                }
                matches.append(item)
                delete_items.append({"track": track, "index": index, "item": item})
        deleted = []
        if delete:
            remaining = max_delete
            by_track = {}
            for entry in delete_items:
                if max_delete > 0 and remaining <= 0:
                    break
                key = id(entry["track"])
                by_track.setdefault(key, {"track": entry["track"], "entries": []})["entries"].append(entry)
                if max_delete > 0:
                    remaining -= 1
            for group in by_track.values():
                track = group["track"]
                for entry in sorted(group["entries"], key=lambda value: value["index"], reverse=True):
                    index = entry["index"]
                    if hasattr(track, "delete_device"):
                        track.delete_device(index)
                    else:
                        del getattr(track, "devices")[index]
                    deleted.append(entry["item"])
        return {
            "delete": delete,
            "matched_count": len(matches),
            "deleted_count": len(deleted),
            "matches": matches[:limit] if limit >= 0 else matches,
            "matches_truncated": bool(limit >= 0 and len(matches) > limit),
            "deleted": deleted[:limit] if limit >= 0 else deleted,
            "deleted_truncated": bool(limit >= 0 and len(deleted) > limit),
        }

    def _agent_m4l_cleanup_tracks(self, params):
        if params.get("target_track"):
            track = self._resolve(params.get("target_track"))
            return [{"name": getattr(track, "name", ""), "path": "", "track": track}]
        if params.get("ref"):
            track = self._resolve(params.get("ref"))
            return [{"name": getattr(track, "name", ""), "path": "", "track": track}]
        query = str(params.get("track_query") or "").lower()
        song = self.song()
        result = []
        for index, track in enumerate(list(song.tracks)):
            result.append({"name": getattr(track, "name", ""), "path": "live_set tracks %d" % index, "track": track})
        for index, track in enumerate(list(song.return_tracks)):
            result.append({"name": getattr(track, "name", ""), "path": "live_set return_tracks %d" % index, "track": track})
        result.append({"name": getattr(song.master_track, "name", ""), "path": "live_set master_track", "track": song.master_track})
        if query:
            result = [item for item in result if query in str(item["name"]).lower()]
        return result

    def _load_agent_m4l_device(self, target_track, device_name, role, params):
        errors = []
        if hasattr(target_track, "insert_device") and not params.get("prefer_browser_load"):
            try:
                self._insert_track_device(target_track, device_name, role, params)
                return True
            except Exception as exc:
                errors.append(str(exc))
        item = self._find_browser_item_named(device_name)
        if item is not None:
            before_devices = list(getattr(target_track, "devices", []))
            before_ids = set([self._object_id(device) for device in before_devices])
            self.song().view.selected_track = target_track
            Live.Application.get_application().browser.load_item(item)
            self._rename_new_track_device(target_track, before_ids, device_name)
            return True
        if hasattr(target_track, "insert_device") and params.get("prefer_browser_load"):
            try:
                self._insert_track_device(target_track, device_name, role, params)
                return True
            except Exception as exc:
                errors.append(str(exc))
        detail = "; ".join([error for error in errors if error])
        if detail:
            raise RuntimeError(detail)
        raise KeyError("Could not find %s in the Live browser; build/install it with live_agent_m4l_device first or reload Live's browser" % device_name)

    def _insert_track_device(self, target_track, device_name, role, params):
        before_devices = list(getattr(target_track, "devices", []))
        before_ids = set([self._object_id(device) for device in before_devices])
        target_track.insert_device(device_name, self._agent_m4l_insert_index(target_track, role, params))
        self._rename_new_track_device(target_track, before_ids, device_name)

    def _rename_new_track_device(self, target_track, before_ids, device_name):
        new_device = self._find_new_track_device(target_track, before_ids)
        if new_device is not None:
            try:
                new_device.name = device_name
            except Exception:
                pass

    def _agent_m4l_insert_index(self, track, role, params):
        if params.get("device_index") is not None:
            return int(params.get("device_index"))
        devices = list(getattr(track, "devices", []))
        if role == "midi_effect":
            for index, device in enumerate(devices):
                if self._device_class_name(device) in ("MxDeviceInstrument", "MxDeviceAudioEffect"):
                    return index
        if role == "instrument":
            for index, device in enumerate(devices):
                if self._device_class_name(device) == "MxDeviceAudioEffect":
                    return index
        return -1

    def _device_class_name(self, device):
        try:
            return str(device.class_name)
        except Exception:
            return ""

    def _agent_m4l_port(self, instance_id):
        digest = hashlib.sha1(self._agent_m4l_slug(instance_id).encode("utf-8")).hexdigest()
        return AGENT_M4L_PORT + (int(digest[:8], 16) % AGENT_M4L_PORT_SPAN)

    def _agent_m4l_udp_payload(self, payload):
        command = payload.get("command")
        if command not in ("set", "status"):
            return payload
        slim = dict(payload)
        for key in ("patch", "spec", "webui", "webuis"):
            if key in slim:
                del slim[key]
        return slim

    def _agent_m4l_command_file_payload(self, payload):
        slim = {}
        for key, value in payload.items():
            if value is not None:
                slim[key] = value
        if slim.get("command") in ("set", "status"):
            for key in ("patch", "spec", "webui", "webuis"):
                if key in slim:
                    del slim[key]
        return slim

    def _agent_m4l_recovery_patch(self, command_file):
        try:
            with open(command_file, "r") as handle:
                existing = json.load(handle)
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        patch = existing.get("patch") or existing.get("spec")
        if patch is not None:
            return patch
        recovered = {}
        for key in AGENT_M4L_RECOVERY_PATCH_KEYS:
            if key in existing:
                recovered[key] = existing[key]
        return recovered or self._agent_m4l_sidecar_recovery_patch(command_file)

    def _agent_m4l_sidecar_recovery_patch(self, command_file):
        try:
            with open(self._agent_m4l_recovery_file(command_file), "r") as handle:
                existing = json.load(handle)
        except Exception:
            return None
        if not isinstance(existing, dict):
            return None
        return existing.get("patch") or existing.get("spec")

    def _agent_m4l_write_recovery_patch(self, command_file, patch):
        if patch is None:
            return
        try:
            with open(self._agent_m4l_recovery_file(command_file), "w") as handle:
                json.dump({"patch": patch}, handle, separators=(",", ":"))
        except Exception:
            pass

    def _agent_m4l_recovery_file(self, command_file):
        return "%s.recovery.json" % command_file

    def _rpc_transport(self, params):
        song = self.song()
        if params.get("time") is not None:
            self._seek_song(song, float(params["time"]))
        action = params.get("action")
        if action == "play":
            self._start_transport(song)
        elif action == "continue":
            song.continue_playing()
            if not getattr(song, "is_playing", False):
                self._start_transport(song)
        elif action == "stop":
            self._stop_transport(song)
        elif action not in (None, "status"):
            raise ValueError("action must be play, continue, stop, or status")
        return self._transport_result(song, action)

    def _lifecycle_api_status(self):
        song = self.song()
        app = Live.Application.get_application()
        song_save_attrs = [name for name in dir(song) if "save" in name.lower()]
        app_lifecycle_attrs = [name for name in dir(app) if any(term in name.lower() for term in ("save", "quit", "close"))]
        return {
            "song_save_attrs": song_save_attrs,
            "app_lifecycle_attrs": app_lifecycle_attrs,
            "song_save_available": callable(getattr(song, "save", None)),
            "app_quit_available": callable(getattr(app, "quit", None)),
        }

    def _rpc_lifecycle_status(self, _params):
        status = self._lifecycle_api_status()
        status["gui_workflow"] = GUI_LIFECYCLE_WORKFLOW
        return status

    def _rpc_save_set(self, params):
        song = self.song()
        save = getattr(song, "save", None)
        if not callable(save):
            if params.get("require_api"):
                raise RuntimeError("Live Song object does not expose save(); use the GUI save workflow")
            return {
                "saved": False,
                "api_available": False,
                "gui_workflow": GUI_LIFECYCLE_WORKFLOW["save"],
                "gui_notes": GUI_LIFECYCLE_WORKFLOW["notes"],
            }
        result = save()
        return {"saved": True, "api_available": True, "result": result}

    def _rpc_quit_ableton(self, params):
        app = Live.Application.get_application()
        save_first = params.get("save", True)
        saved = None
        if save_first:
            save_result = self._rpc_save_set({})
            saved = bool(save_result.get("saved"))
            if not saved and not params.get("force_without_save"):
                return {
                    "quit_requested": False,
                    "saved_first": False,
                    "reason": "save API unavailable; pass force_without_save:true to quit anyway or use the GUI workflow",
                    "gui_workflow": GUI_LIFECYCLE_WORKFLOW,
                }
        quit_fn = getattr(app, "quit", None)
        if not callable(quit_fn):
            return {
                "quit_requested": False,
                "saved_first": saved,
                "api_available": False,
                "gui_workflow": GUI_LIFECYCLE_WORKFLOW["quit"],
                "gui_notes": GUI_LIFECYCLE_WORKFLOW["notes"],
            }
        delay = int(params.get("quit_delay_ticks") or 2)
        self.schedule_message(max(1, delay), quit_fn)
        return {"quit_requested": True, "saved_first": saved, "api_available": True, "scheduled": True}

    def _resolve_track_param(self, params):
        if params.get("master"):
            return self.song().master_track
        if params.get("track_index") is not None:
            return self.song().tracks[int(params["track_index"])]
        if params.get("ref"):
            return self._resolve(params["ref"])
        raise ValueError("Provide ref, track_index, or master:true")

    def _resolve_clip_slot_param(self, params):
        if params.get("ref"):
            return self._resolve(params["ref"])
        track_index = params.get("track_index")
        clip_index = params.get("clip_index")
        if track_index is None or clip_index is None:
            raise ValueError("Provide ref or both track_index and clip_index")
        return self.song().tracks[int(track_index)].clip_slots[int(clip_index)]

    def _create_track(self, method_name, params):
        song = self.song()
        fn = getattr(song, method_name, None)
        if not callable(fn):
            raise RuntimeError("Live Song object does not expose %s()" % method_name)
        index = int(params.get("index") if params.get("index") is not None else -1)
        before_ids = set(id(track) for track in song.tracks)
        fn(index)
        created = None
        created_index = None
        for position, track in enumerate(song.tracks):
            if id(track) not in before_ids:
                created = track
                created_index = position
                break
        result = {"track_count": len(song.tracks), "requested_index": index}
        if created is not None:
            if params.get("name"):
                created.name = str(params["name"])
            summary = self._object_summary(created, False)
            summary["name"] = getattr(created, "name", "")
            result["track"] = summary
            result["track_index"] = created_index
        return result

    def _rpc_create_midi_track(self, params):
        return self._create_track("create_midi_track", params)

    def _rpc_create_audio_track(self, params):
        return self._create_track("create_audio_track", params)

    def _rpc_rename_track(self, params):
        track = self._resolve_track_param(params)
        before = getattr(track, "name", "")
        track.name = str(params["name"])
        summary = self._object_summary(track, False)
        summary["name"] = getattr(track, "name", "")
        return {"track": summary, "before_name": before, "renamed": before != summary["name"]}

    def _rpc_fire_clip(self, params):
        slot = self._resolve_clip_slot_param(params)
        slot.fire()
        result = {"fired": True, "has_clip": bool(getattr(slot, "has_clip", False))}
        clip = getattr(slot, "clip", None)
        if clip is not None:
            result["clip_name"] = getattr(clip, "name", "")
        return result

    def _rpc_stop_clip(self, params):
        slot = self._resolve_clip_slot_param(params)
        slot.stop()
        return {"stopped": True, "has_clip": bool(getattr(slot, "has_clip", False))}

    def _rpc_set_tempo(self, params):
        song = self.song()
        tempo = float(params["tempo"])
        if tempo < 20.0 or tempo > 999.0:
            raise ValueError("tempo must be between 20 and 999 BPM")
        before = float(song.tempo)
        song.tempo = tempo
        return {"tempo": float(song.tempo), "before": before, "changed": before != float(song.tempo)}

    # ------------------------------------------------------------------
    # Rough additions: named wrappers for capabilities found in
    # ahujasid/ableton-mcp and Tompz/ableton-mcp that this bridge only
    # reached through live_exec. LOM quirks noted inline were verified by
    # Tompz/ableton-mcp against Live 12.4 (see NOTICE.md).
    # ------------------------------------------------------------------

    _NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

    def _resolve_any_track(self, params):
        if params.get("return_index") is not None:
            return self.song().return_tracks[int(params["return_index"])]
        return self._resolve_track_param(params)

    def _track_index_of(self, track):
        song = self.song()
        for index, candidate in enumerate(song.tracks):
            if candidate is track:
                return ("track", index)
        for index, candidate in enumerate(song.return_tracks):
            if candidate is track:
                return ("return", index)
        if track is song.master_track:
            return ("master", None)
        return (None, None)

    def _resolve_clip_any(self, params):
        if params.get("ref"):
            obj = self._resolve(params["ref"])
            if hasattr(obj, "has_clip"):
                if not obj.has_clip:
                    raise ValueError("Clip slot is empty")
                return obj.clip
            return obj
        if params.get("track_index") is None:
            raise ValueError("Provide ref, or track_index with clip_index or arrangement_clip_index")
        track = self.song().tracks[int(params["track_index"])]
        if params.get("arrangement_clip_index") is not None:
            return track.arrangement_clips[int(params["arrangement_clip_index"])]
        if params.get("clip_index") is None:
            raise ValueError("Provide clip_index or arrangement_clip_index")
        slot = track.clip_slots[int(params["clip_index"])]
        if not getattr(slot, "has_clip", False):
            raise ValueError("Clip slot %s is empty" % params["clip_index"])
        return slot.clip

    def _parse_color(self, value):
        if isinstance(value, str):
            text = value.strip().lstrip("#")
            if text.lower().startswith("0x"):
                text = text[2:]
            return int(text, 16)
        return int(value)

    def _apply_props(self, obj, params, spec):
        """Set only the provided fields; return {field: [before, after]}."""
        changed = {}
        for field, caster in spec:
            if field not in params or params[field] is None:
                continue
            before = getattr(obj, field, None)
            setattr(obj, field, caster(params[field]))
            changed[field] = [before, getattr(obj, field, None)]
        return changed

    def _scene_summary(self, scene, index):
        summary = {"index": index, "name": getattr(scene, "name", "")}
        for attr in ("color", "tempo", "is_empty"):
            if hasattr(scene, attr):
                summary[attr] = getattr(scene, attr)
        return summary

    def _rpc_scene(self, params):
        song = self.song()
        action = params.get("action") or "list"
        index = params.get("index")
        if action == "list":
            return {"scenes": [self._scene_summary(scene, i) for i, scene in enumerate(song.scenes)]}
        if action == "stop_all":
            song.stop_all_clips(bool(params.get("quantized", True)))
            return {"stopped_all": True}
        if action == "create":
            position = int(index if index is not None else -1)
            created = song.create_scene(position)
            new_index = len(song.scenes) - 1 if position < 0 else position
            scene = created if created is not None else song.scenes[new_index]
            if params.get("name"):
                scene.name = str(params["name"])
            if params.get("color") is not None:
                scene.color = self._parse_color(params["color"])
            return {"created": self._scene_summary(scene, new_index), "scene_count": len(song.scenes)}
        if index is None:
            raise ValueError("index is required for %s" % action)
        index = int(index)
        scene = song.scenes[index]
        if action == "fire":
            scene.fire()
            return {"fired": index}
        if action == "rename":
            before = getattr(scene, "name", "")
            scene.name = str(params.get("name") or "")
            if params.get("color") is not None:
                scene.color = self._parse_color(params["color"])
            return {"scene": self._scene_summary(scene, index), "before_name": before}
        if action == "duplicate":
            song.duplicate_scene(index)
            return {"duplicated": index, "new_index": index + 1, "scene_count": len(song.scenes)}
        if action == "delete":
            name = getattr(scene, "name", "")
            song.delete_scene(index)
            return {"deleted": name, "index": index, "scene_count": len(song.scenes)}
        raise ValueError("action must be list, create, fire, rename, duplicate, delete, or stop_all")

    def _rpc_delete(self, params):
        song = self.song()
        target = params.get("target")
        if target == "track":
            if params.get("return_index") is not None:
                index = int(params["return_index"])
                name = song.return_tracks[index].name
                song.delete_return_track(index)
                return {"deleted": name, "return_index": index}
            index = int(params["track_index"])
            name = song.tracks[index].name
            song.delete_track(index)
            return {"deleted": name, "track_index": index, "track_count": len(song.tracks)}
        if target == "device":
            track = self._resolve_any_track(params)
            index = int(params["device_index"])
            name = track.devices[index].name
            track.delete_device(index)
            return {"deleted": name, "device_index": index, "device_count": len(track.devices)}
        if target == "clip":
            track = song.tracks[int(params["track_index"])]
            if params.get("arrangement_clip_index") is not None:
                clip = track.arrangement_clips[int(params["arrangement_clip_index"])]
                name = clip.name
                track.delete_clip(clip)
                return {"deleted": name, "arrangement": True}
            slot = track.clip_slots[int(params["clip_index"])]
            if not getattr(slot, "has_clip", False):
                return {"deleted": None, "reason": "slot already empty"}
            name = slot.clip.name
            slot.delete_clip()
            return {"deleted": name, "clip_index": int(params["clip_index"])}
        raise ValueError("target must be track, device, or clip")

    def _rpc_duplicate(self, params):
        song = self.song()
        target = params.get("target")
        if target == "track":
            index = int(params["track_index"])
            song.duplicate_track(index)
            return {"duplicated": index, "new_index": index + 1, "track_count": len(song.tracks)}
        if target == "clip":
            track = song.tracks[int(params["track_index"])]
            clip_index = int(params["clip_index"])
            if params.get("to_track_index") is not None or params.get("to_clip_index") is not None:
                dest_track = song.tracks[int(params.get("to_track_index", params["track_index"]))]
                dest_slot = dest_track.clip_slots[int(params["to_clip_index"])]
                track.clip_slots[clip_index].duplicate_clip_to(dest_slot)
                return {"duplicated": clip_index, "to_clip_index": int(params["to_clip_index"])}
            new_index = track.duplicate_clip_slot(clip_index)
            return {"duplicated": clip_index, "new_index": new_index}
        raise ValueError("target must be track or clip (scenes: live_scene action=duplicate)")

    def _rpc_edit(self, params):
        op = params.get("op")
        if op == "delete":
            return self._rpc_delete(params)
        if op == "duplicate":
            return self._rpc_duplicate(params)
        raise ValueError("op must be delete or duplicate")

    def _rpc_track_set(self, params):
        track = self._resolve_any_track(params)
        mixer = track.mixer_device
        changed = {}
        if params.get("name") is not None:
            changed["name"] = [track.name, str(params["name"])]
            track.name = str(params["name"])
        if params.get("color") is not None:
            before = getattr(track, "color", None)
            track.color = self._parse_color(params["color"])
            changed["color"] = [before, track.color]
        if params.get("color_index") is not None:
            before = getattr(track, "color_index", None)
            track.color_index = int(params["color_index"])
            changed["color_index"] = [before, track.color_index]
        for field in ("volume", "panning"):
            if params.get(field) is not None:
                param = getattr(mixer, field)
                value = float(params[field])
                if not (float(param.min) <= value <= float(param.max)):
                    raise ValueError("%s %.3f outside %.3f..%.3f" % (field, value, param.min, param.max))
                before = float(param.value)
                param.value = value
                changed[field] = [before, float(param.value)]
        for send in params.get("sends") or []:
            index = int(send["index"])
            param = mixer.sends[index]
            before = float(param.value)
            param.value = float(send["value"])
            changed["send_%d" % index] = [before, float(param.value)]
        for field in ("mute", "solo"):
            if params.get(field) is not None:
                before = bool(getattr(track, field))
                setattr(track, field, bool(params[field]))
                changed[field] = [before, bool(getattr(track, field))]
        if params.get("arm") is not None:
            if not getattr(track, "can_be_armed", False):
                raise ValueError("Track cannot be armed (return, master, or group track)")
            before = bool(track.arm)
            track.arm = bool(params["arm"])
            changed["arm"] = [before, bool(track.arm)]
        if params.get("monitoring") is not None:
            states = {"in": 0, "auto": 1, "off": 2}
            value = params["monitoring"]
            value = states[value] if isinstance(value, str) else int(value)
            before = getattr(track, "current_monitoring_state", None)
            track.current_monitoring_state = value
            changed["monitoring"] = [before, track.current_monitoring_state]
        if params.get("fold") is not None:
            if not getattr(track, "is_foldable", False):
                raise ValueError("Only group tracks can fold")
            before = bool(track.fold_state)
            track.fold_state = 1 if params["fold"] else 0
            changed["fold"] = [before, bool(track.fold_state)]
        kind, index = self._track_index_of(track)
        return {"track": {"kind": kind, "index": index, "name": track.name}, "changed": changed}

    def _rpc_undo(self, params):
        song = self.song()
        action = params.get("action") or "undo"
        count = max(1, int(params.get("count") or 1))
        done = 0
        for _ in range(count):
            if action == "undo":
                if not song.can_undo:
                    break
                song.undo()
            elif action == "redo":
                if not song.can_redo:
                    break
                song.redo()
            else:
                raise ValueError("action must be undo or redo")
            done += 1
        return {"action": action, "requested": count, "done": done,
                "can_undo": bool(song.can_undo), "can_redo": bool(song.can_redo)}

    _CLIP_PROP_SPEC = (
        ("name", str),
        ("color_index", int),
        ("muted", bool),
        ("launch_mode", int),
        ("launch_quantization", int),
        ("legato", bool),
        ("velocity_amount", float),
        ("start_marker", float),
        ("end_marker", float),
        ("gain", float),
        ("pitch_coarse", int),
        ("pitch_fine", float),
        ("warping", bool),
        ("warp_mode", int),
        ("ram_mode", bool),
    )

    def _rpc_clip_set(self, params):
        clip = self._resolve_clip_any(params)
        audio_only = ("gain", "pitch_coarse", "pitch_fine", "warping", "warp_mode", "ram_mode")
        if not getattr(clip, "is_audio_clip", False):
            bad = [field for field in audio_only if params.get(field) is not None]
            if bad:
                raise ValueError("Audio-clip-only fields on a MIDI clip: %s" % ", ".join(bad))
        changed = {}
        if params.get("looping") is not None:
            before = bool(clip.looping)
            clip.looping = bool(params["looping"])
            changed["looping"] = [before, bool(clip.looping)]
        loop_start = params.get("loop_start")
        loop_end = params.get("loop_end")
        if loop_start is not None or loop_end is not None:
            before = [float(clip.loop_start), float(clip.loop_end)]
            # Live rejects loop_start >= loop_end at every intermediate step,
            # so order the writes to keep the interval valid throughout.
            if loop_start is not None and float(loop_start) >= float(clip.loop_end):
                clip.loop_end = float(loop_end if loop_end is not None else float(loop_start) + (before[1] - before[0]))
                clip.loop_start = float(loop_start)
            else:
                if loop_start is not None:
                    clip.loop_start = float(loop_start)
                if loop_end is not None:
                    clip.loop_end = float(loop_end)
            changed["loop"] = [before, [float(clip.loop_start), float(clip.loop_end)]]
        if params.get("color") is not None:
            before = getattr(clip, "color", None)
            clip.color = self._parse_color(params["color"])
            changed["color"] = [before, clip.color]
        changed.update(self._apply_props(clip, params, self._CLIP_PROP_SPEC))
        if params.get("groove_index") is not None:
            index = int(params["groove_index"])
            if index < 0:
                clip.groove = None
                changed["groove"] = "cleared"
            else:
                grooves = self.song().groove_pool.grooves
                clip.groove = grooves[index]
                changed["groove"] = getattr(grooves[index], "name", index)
        return {"clip": getattr(clip, "name", ""), "changed": changed}

    def _rpc_clip_remove_notes(self, params):
        clip = self._resolve_clip_any(params)
        if not getattr(clip, "is_midi_clip", False):
            raise ValueError("Clip is not a MIDI clip")
        from_pitch = int(params.get("from_pitch") or 0)
        pitch_span = int(params.get("pitch_span") or 128)
        from_time = float(params.get("from_time") or 0.0)
        span = params.get("time_span")
        time_span = float(span) if span is not None else max(float(getattr(clip, "length", 0.0)), float(getattr(clip, "loop_end", 0.0)), float(getattr(clip, "end_marker", 0.0))) + 1024.0
        before = len(clip.get_notes_extended(from_pitch, pitch_span, from_time, time_span))
        # Real C++ order is (from_pitch, pitch_span, from_time, time_span).
        clip.remove_notes_extended(from_pitch, pitch_span, from_time, time_span)
        after = len(clip.get_notes_extended(from_pitch, pitch_span, from_time, time_span))
        return {"removed": before - after, "remaining_in_range": after}

    def _rpc_clip_select_notes(self, params):
        clip = self._resolve_clip_any(params)
        if params.get("deselect"):
            clip.deselect_all_notes()
        elif params.get("select_all"):
            clip.select_all_notes()
        elif params.get("note_ids"):
            clip.select_notes_by_id([int(note_id) for note_id in params["note_ids"]])
        else:
            raise ValueError("Provide note_ids, select_all:true, or deselect:true")
        return {"selected_count": len(clip.get_selected_notes_extended())}

    def _cue_list(self):
        return [{"index": i, "name": getattr(cue, "name", ""), "time": float(cue.time)} for i, cue in enumerate(self.song().cue_points)]

    def _find_cue(self, params):
        cues = list(self.song().cue_points)
        if params.get("index") is not None:
            return cues[int(params["index"])]
        if params.get("time") is not None:
            for cue in cues:
                if abs(float(cue.time) - float(params["time"])) < 1e-3:
                    return cue
            return None
        if params.get("name"):
            for cue in cues:
                if getattr(cue, "name", "") == params["name"]:
                    return cue
            return None
        raise ValueError("Provide index, time, or name")

    def _toggle_cue_at(self, song, target_time, name=None):
        # Setting current_song_time and toggling a cue in the same tick does
        # not see the new position, so toggle one tick later and restore.
        original_time = float(song.current_song_time)

        def phase2():
            try:
                song.set_or_delete_cue()
                if name:
                    for cue in song.cue_points:
                        if abs(float(cue.time) - target_time) < 1e-3:
                            cue.name = str(name)
                            break
            finally:
                try:
                    song.current_song_time = original_time
                except Exception:
                    pass

        song.current_song_time = target_time
        self.schedule_message(1, phase2)

    def _rpc_locators(self, params):
        song = self.song()
        action = params.get("action") or "list"
        if action == "list":
            return {"locators": self._cue_list()}
        if action in ("create", "delete") and getattr(song, "is_playing", False):
            raise ValueError("Stop the transport before creating or deleting locators")
        if action == "create":
            if params.get("time") is None:
                raise ValueError("time (beats) is required")
            target_time = float(params["time"])
            existing = self._find_cue({"time": target_time})
            if existing is not None:
                if params.get("name"):
                    existing.name = str(params["name"])
                return {"locator": {"name": existing.name, "time": float(existing.time)}, "existed": True}
            self._toggle_cue_at(song, target_time, params.get("name"))
            return {"scheduled": "create", "time": target_time}
        cue = self._find_cue(params)
        if cue is None:
            raise ValueError("No locator matches %r" % {k: params.get(k) for k in ("index", "time", "name") if params.get(k) is not None})
        if action == "rename":
            before = cue.name
            cue.name = str(params.get("new_name") or params.get("name") or "")
            return {"locator": {"name": cue.name, "time": float(cue.time)}, "before_name": before}
        if action == "jump":
            cue.jump()
            return {"jumped": {"name": cue.name, "time": float(cue.time)}}
        if action == "delete":
            target_time = float(cue.time)
            self._toggle_cue_at(song, target_time)
            return {"scheduled": "delete", "time": target_time}
        raise ValueError("action must be list, create, rename, jump, or delete")

    def _match_routing(self, options, wanted):
        wanted_l = str(wanted).lower()
        options = list(options)
        for option in options:
            if option.display_name.lower() == wanted_l:
                return option
        for option in options:
            if wanted_l in option.display_name.lower():
                return option
        raise ValueError("No routing option matching %r; available: %s" % (wanted, [o.display_name for o in options]))

    def _routing_state(self, track, direction):
        state = {}
        for part in ("type", "channel"):
            current = getattr(track, "%s_routing_%s" % (direction, part), None)
            available = getattr(track, "available_%s_routing_%ss" % (direction, part), None)
            if current is not None:
                state[part] = current.display_name
            if available is not None:
                state["available_%ss" % part] = [option.display_name for option in available]
        return state

    def _rpc_track_routing(self, params):
        track = self._resolve_any_track(params)
        direction = params.get("direction")
        if direction not in (None, "input", "output"):
            raise ValueError("direction must be input or output")
        changed = {}
        if direction:
            if params.get("type"):
                option = self._match_routing(getattr(track, "available_%s_routing_types" % direction), params["type"])
                setattr(track, "%s_routing_type" % direction, option)
                changed["type"] = option.display_name
            if params.get("channel"):
                option = self._match_routing(getattr(track, "available_%s_routing_channels" % direction), params["channel"])
                setattr(track, "%s_routing_channel" % direction, option)
                changed["channel"] = option.display_name
        result = {"changed": changed}
        for side in ((direction,) if direction else ("input", "output")):
            result[side] = self._routing_state(track, side)
        return result

    def _rpc_view(self, params):
        app = Live.Application.get_application()
        song = self.song()
        result = {}
        if params.get("show"):
            app.view.show_view(str(params["show"]))
            result["shown"] = params["show"]
        if params.get("focus"):
            app.view.focus_view(str(params["focus"]))
            result["focused"] = params["focus"]
        if params.get("select_track") is not None or params.get("select_return") is not None or params.get("select_master"):
            selector = {"track_index": params.get("select_track"), "return_index": params.get("select_return"), "master": params.get("select_master")}
            track = self._resolve_any_track({key: value for key, value in selector.items() if value is not None})
            song.view.selected_track = track
            result["selected_track"] = track.name
        if params.get("select_scene") is not None:
            song.view.selected_scene = song.scenes[int(params["select_scene"])]
            result["selected_scene"] = int(params["select_scene"])
        if params.get("select_device") is not None:
            track = song.view.selected_track
            device = track.devices[int(params["select_device"])]
            song.view.select_device(device)
            result["selected_device"] = device.name
        if params.get("detail_clip"):
            clip = self._resolve_clip_any(params["detail_clip"])
            song.view.detail_clip = clip
            result["detail_clip"] = getattr(clip, "name", "")
        result["visible"] = {name: bool(app.view.is_view_visible(name)) for name in ("Session", "Arranger", "Detail", "Browser")}
        return result

    _SONG_PROP_SPEC = (
        ("record_mode", bool),
        ("session_record", bool),
        ("arrangement_overdub", bool),
        ("session_automation_record", bool),
        ("metronome", bool),
        ("loop", bool),
        ("loop_start", float),
        ("loop_length", float),
        ("punch_in", bool),
        ("punch_out", bool),
        ("signature_numerator", int),
        ("signature_denominator", int),
        ("groove_amount", float),
        ("swing_amount", float),
        ("clip_trigger_quantization", int),
        ("midi_recording_quantization", int),
        ("root_note", int),
        ("scale_name", str),
        ("scale_mode", bool),
    )

    def _rpc_song_set(self, params):
        song = self.song()
        values = dict(params)
        if isinstance(values.get("root_note"), str):
            values["root_note"] = self._NOTE_NAMES.index(values["root_note"].strip().upper().replace("DB", "C#").replace("EB", "D#").replace("GB", "F#").replace("AB", "G#").replace("BB", "A#"))
        # session_record and similar transport flags read back one tick late,
        # so report the requested value rather than trusting the read-back.
        changed = {}
        for field, caster in self._SONG_PROP_SPEC:
            if values.get(field) is None:
                continue
            if not hasattr(song, field):
                changed[field] = "unsupported on this Live version"
                continue
            before = getattr(song, field)
            setattr(song, field, caster(values[field]))
            changed[field] = [before, caster(values[field])]
        actions = []
        for action in params.get("actions") or []:
            if action == "tap_tempo":
                song.tap_tempo()
            elif action == "capture_midi":
                song.capture_midi()
            elif action == "re_enable_automation":
                song.re_enable_automation()
            elif action == "back_to_arranger":
                song.back_to_arranger = False
            elif action == "stop_all_clips":
                song.stop_all_clips()
            elif action == "jump_to_next_cue":
                song.jump_to_next_cue()
            elif action == "jump_to_prev_cue":
                song.jump_to_prev_cue()
            else:
                raise ValueError("Unknown song action %r" % action)
            actions.append(action)
        return {"changed": changed, "actions": actions}

    def _track_kind(self, track):
        if getattr(track, "is_foldable", False):
            return "group"
        if getattr(track, "has_midi_input", False):
            return "midi"
        if getattr(track, "has_audio_input", False):
            return "audio"
        return "other"

    def _context_track(self, track, index):
        entry = {
            "index": index,
            "name": getattr(track, "name", ""),
            "kind": self._track_kind(track),
            "devices": [getattr(device, "name", "") for device in list(getattr(track, "devices", []))[:12]],
        }
        for flag in ("mute", "solo", "arm"):
            if getattr(track, flag, False):
                entry[flag] = True
        playing = getattr(track, "playing_slot_index", -1)
        if playing is not None and playing >= 0:
            slot = track.clip_slots[playing]
            entry["playing_clip"] = getattr(slot.clip, "name", "") if getattr(slot, "has_clip", False) else None
        if getattr(track, "is_grouped", False) and getattr(track, "group_track", None) is not None:
            entry["group"] = track.group_track.name
        return entry

    def _rpc_song_context(self, params):
        song = self.song()
        context = {
            "tempo": float(song.tempo),
            "signature": "%d/%d" % (song.signature_numerator, song.signature_denominator),
            "is_playing": bool(getattr(song, "is_playing", False)),
            "current_song_time": float(song.current_song_time),
        }
        root = getattr(song, "root_note", None)
        if root is not None:
            context["key"] = {
                "root": self._NOTE_NAMES[int(root) % 12],
                "scale": getattr(song, "scale_name", None),
                "intervals": list(getattr(song, "scale_intervals", []) or []),
                "scale_mode": bool(getattr(song, "scale_mode", False)),
            }
        for attr in ("loop", "loop_start", "loop_length", "song_length", "groove_amount", "file_path"):
            if hasattr(song, attr):
                context[attr] = getattr(song, attr)
        view = getattr(song, "view", None)
        selected = getattr(view, "selected_track", None) if view is not None else None
        if selected is not None:
            kind, index = self._track_index_of(selected)
            context["selected_track"] = {"kind": kind, "index": index, "name": selected.name}
        selected_scene = getattr(view, "selected_scene", None) if view is not None else None
        if selected_scene is not None:
            context["selected_scene"] = getattr(selected_scene, "name", "")
        context["tracks"] = [self._context_track(track, i) for i, track in enumerate(song.tracks)]
        context["returns"] = [{"index": i, "name": t.name, "devices": [d.name for d in t.devices]} for i, t in enumerate(song.return_tracks)]
        context["locators"] = self._cue_list()
        pool = getattr(song, "groove_pool", None)
        if pool is not None:
            context["grooves"] = [getattr(groove, "name", "") for groove in pool.grooves]
        context["scene_count"] = len(song.scenes)
        return context

    def _resolve_rack(self, params):
        if params.get("device_ref"):
            device = self._resolve(params["device_ref"])
        else:
            track = self._resolve_any_track(params)
            device = track.devices[int(params.get("device_index") or 0)]
        if not getattr(device, "can_have_chains", False):
            raise ValueError("Device %r is not a rack" % getattr(device, "name", ""))
        return device

    def _rack_macros(self, rack):
        params = list(rack.parameters)
        visible = int(getattr(rack, "visible_macro_count", 8) or 8)
        mapped = list(getattr(rack, "macros_mapped", []) or [])
        macros = []
        for i in range(min(visible, len(params) - 1)):
            param = params[i + 1]
            macros.append({"index": i, "name": param.name, "value": float(param.value),
                           "min": float(param.min), "max": float(param.max),
                           "mapped": bool(mapped[i]) if i < len(mapped) else None})
        return macros

    def _rpc_rack(self, params):
        rack = self._resolve_rack(params)
        action = params.get("action") or "inspect"
        if action == "inspect":
            chains = []
            for chain_index, chain in enumerate(rack.chains):
                chains.append({
                    "index": chain_index,
                    "name": getattr(chain, "name", ""),
                    "mute": bool(getattr(chain, "mute", False)),
                    "solo": bool(getattr(chain, "solo", False)),
                    "devices": [{"name": d.name, "class_name": getattr(d, "class_name", "")} for d in chain.devices],
                })
            return {"rack": rack.name, "class_name": getattr(rack, "class_name", ""), "macros": self._rack_macros(rack), "chains": chains}
        if action == "set_macro":
            index = int(params["macro"])
            param = list(rack.parameters)[index + 1]
            value = float(params["value"])
            if not (float(param.min) <= value <= float(param.max)):
                raise ValueError("value %.3f outside %.3f..%.3f" % (value, param.min, param.max))
            before = float(param.value)
            param.value = value
            return {"macro": index, "name": param.name, "before": before, "value": float(param.value)}
        if action == "chain":
            chain = rack.chains[int(params["chain"])]
            changed = self._apply_props(chain, params, (("mute", bool), ("solo", bool), ("name", str)))
            return {"chain": int(params["chain"]), "changed": changed}
        raise ValueError("action must be inspect, set_macro, or chain")

    def _rpc_take_lanes(self, params):
        track = self._resolve_any_track(params)
        if not hasattr(track, "take_lanes"):
            raise RuntimeError("Take lanes need Live 12.2 or later")
        if params.get("action") == "create":
            track.create_take_lane()
        lanes = [{"index": i, "name": getattr(lane, "name", "")} for i, lane in enumerate(track.take_lanes)]
        return {"track": track.name, "take_lanes": lanes}

    # ------------------------------------------------------------------
    # End Rough additions
    # ------------------------------------------------------------------

    def _rpc_fade(self, params):
        track = self._resolve_track_param(params)
        mixer = getattr(track, "mixer_device", None)
        param = getattr(mixer, "volume", None)
        if param is None:
            raise RuntimeError("Track has no mixer_device.volume parameter")
        if params.get("target_value") is not None:
            target = float(params["target_value"])
        elif params.get("target_percent") is not None:
            percent = float(params["target_percent"])
            if percent < 0.0:
                raise ValueError("target_percent must be >= 0")
            if percent > 100.0 and not params.get("allow_over_unity"):
                raise ValueError("target_percent above 100 (unity) requires allow_over_unity:true")
            target = (percent / 100.0) * FADE_UNITY_VALUE
        else:
            raise ValueError("Provide target_percent or target_value")
        minimum = float(getattr(param, "min", 0.0))
        maximum = float(getattr(param, "max", 1.0))
        target = min(max(target, minimum), maximum)
        duration = float(params.get("duration") if params.get("duration") is not None else 10.0)
        if duration < 0.0 or duration > FADE_MAX_DURATION:
            raise ValueError("duration must be between 0 and %s seconds" % FADE_MAX_DURATION)
        steps = int(params.get("steps") if params.get("steps") is not None else FADE_DEFAULT_STEPS)
        if steps < 1:
            raise ValueError("steps must be >= 1")
        curve = params.get("curve") or "smoothstep"
        if curve not in ("smoothstep", "linear"):
            raise ValueError("curve must be smoothstep or linear")
        start = float(param.value)
        sleep_per_step = duration / steps
        for step in range(1, steps + 1):
            t = step / float(steps)
            shaped = t * t * (3.0 - 2.0 * t) if curve == "smoothstep" else t
            param.value = start + (target - start) * shaped
            if sleep_per_step > 0:
                time.sleep(sleep_per_step)
        final_value = float(param.value)
        result = {
            "track": getattr(track, "name", ""),
            "curve": curve,
            "duration": duration,
            "steps": steps,
            "start_value": start,
            "target_value": target,
            "final_value": final_value,
            "final_percent": round(final_value / FADE_UNITY_VALUE * 100.0, 3),
        }
        try:
            result["display"] = param.str_for_value(param.value)
        except Exception:
            pass
        return result

    def _osc_message(self, address, args):
        def pad(value):
            data = value.encode("utf-8") + b"\x00"
            return data + (b"\x00" * ((4 - (len(data) % 4)) % 4))

        tags = "," + ("s" * len(args))
        payload = pad(address) + pad(tags)
        for arg in args:
            payload += pad(str(arg))
        return payload

    def _seek_song(self, song, time_value):
        current = float(getattr(song, "current_song_time", 0.0))
        song.jump_by(time_value - current)

    def _start_transport(self, song):
        song.start_playing()
        if not getattr(song, "is_playing", False):
            try:
                song.continue_playing()
            except Exception:
                pass
        if not getattr(song, "is_playing", False):
            song.start_playing()

    def _stop_transport(self, song):
        song.stop_playing()
        if getattr(song, "is_playing", False):
            song.stop_playing()

    def _transport_result(self, song, action):
        raw_playing = bool(getattr(song, "is_playing", False))
        result = {"playing": raw_playing, "time": getattr(song, "current_song_time", None)}
        if action in ("play", "continue") and not raw_playing:
            result["playing"] = True
            result["raw_playing"] = False
            result["settled"] = False
        if action == "stop" and raw_playing:
            result["playing"] = False
            result["raw_playing"] = True
            result["settled"] = False
        return result

    def _track_has_device(self, track, name, role=None):
        return self._find_track_device(track, name, role) is not None

    def _find_track_device(self, track, name, role=None):
        for device in getattr(track, "devices", []):
            if getattr(device, "name", "") == name and self._device_matches_agent_m4l_role(device, role):
                return device
        return None

    def _trigger_agent_m4l_poll(self, device):
        poll_names = ("Agent Poll", "Agent M4L Poll", "command-trigger")
        for parameter in getattr(device, "parameters", []):
            name = str(getattr(parameter, "name", ""))
            if name not in poll_names:
                continue
            try:
                current = float(getattr(parameter, "value", 0.0) or 0.0)
            except Exception:
                current = 0.0
            try:
                minimum = float(getattr(parameter, "min", 0.0))
            except Exception:
                minimum = 0.0
            try:
                maximum = float(getattr(parameter, "max", 1.0))
            except Exception:
                maximum = 1.0
            next_value = current + 1.0
            if next_value > maximum:
                next_value = minimum
            try:
                parameter.value = next_value
                return True
            except Exception:
                return False
        return False

    def _find_new_track_device(self, track, before_ids):
        for device in getattr(track, "devices", []):
            try:
                obj_id = self._object_id(device)
            except Exception:
                obj_id = id(device)
            if obj_id not in before_ids:
                return device
        return None

    def _device_matches_agent_m4l_role(self, device, role):
        if role is None:
            return True
        expected = {
            "audio_effect": "MxDeviceAudioEffect",
            "instrument": "MxDeviceInstrument",
            "midi_effect": "MxDeviceMidiEffect",
        }.get(role)
        if expected is None:
            return True
        try:
            class_name = device.class_name
        except Exception:
            return True
        return class_name == expected

    def _agent_m4l_role(self, role):
        value = str(role or "audio_effect").lower().replace("-", "_").replace(" ", "_")
        aliases = {"audio": "audio_effect", "effect": "audio_effect", "fx": "audio_effect", "synth": "instrument", "midi": "midi_effect"}
        value = aliases.get(value, value)
        if value not in ("audio_effect", "instrument", "midi_effect"):
            raise ValueError("role must be audio_effect, instrument, or midi_effect")
        return value

    def _agent_m4l_slug(self, value):
        text = str(value or "device")
        result = []
        for char in text:
            if char.isalnum() or char in "_.-":
                result.append(char)
            else:
                result.append("_")
        slug = "".join(result).strip("._-")
        return slug[:80] or "device"

    def _delete_named_devices(self, name):
        song = self.song()
        tracks = list(song.tracks) + list(song.return_tracks) + [song.master_track]
        for track in tracks:
            devices = getattr(track, "devices", [])
            for index in range(len(devices) - 1, -1, -1):
                if getattr(devices[index], "name", "") != name:
                    continue
                if hasattr(track, "delete_device"):
                    track.delete_device(index)
                else:
                    del devices[index]

    def _find_browser_item_named(self, name):
        browser = Live.Application.get_application().browser

        def children_of(item):
            try:
                return item.iter_children
            except Exception:
                return ()

        def walk(item, depth):
            if getattr(item, "name", "") == name and bool(getattr(item, "is_loadable", False)):
                return item
            if depth >= DEFAULT_MAX_DEPTH:
                return None
            for child in children_of(item):
                found = walk(child, depth + 1)
                if found is not None:
                    return found
            return None

        for root_name in ("user_library", "audio_effects", "max_for_live"):
            if not hasattr(browser, root_name):
                continue
            root = getattr(browser, root_name)
            found = walk(root, 0)
            if found is not None:
                return found
        return None

    def _rpc_set_summary(self, params):
        song = self.song()
        track_limit = int(params.get("track_limit") if params.get("track_limit") is not None else 64)
        clip_slot_limit = int(params.get("clip_slot_limit") if params.get("clip_slot_limit") is not None else 16)
        device_limit = int(params.get("device_limit") if params.get("device_limit") is not None else 16)
        arrangement_clip_limit = int(params.get("arrangement_clip_limit") if params.get("arrangement_clip_limit") is not None else 0)
        track_query = (params.get("track_query") or "").strip().lower()
        include_returns = params.get("include_return_tracks")
        if include_returns is None:
            include_returns = True
        include_master = params.get("include_master_track")
        if include_master is None:
            include_master = True
        tracks = []
        tracks_scanned = 0
        for index, track in enumerate(song.tracks):
            tracks_scanned += 1
            if track_query and track_query not in getattr(track, "name", "").lower():
                continue
            if track_limit >= 0 and len(tracks) >= track_limit:
                tracks.append({"truncated": True})
                break
            tracks.append(self._track_summary(track, index, clip_slot_limit, device_limit, arrangement_clip_limit))
        returns = []
        if include_returns:
            for index, track in enumerate(song.return_tracks):
                if track_query and track_query not in getattr(track, "name", "").lower():
                    continue
                returns.append(self._track_summary(track, index, clip_slot_limit, device_limit, 0))
        result = {
            "tempo": song.tempo,
            "signature_numerator": song.signature_numerator,
            "signature_denominator": song.signature_denominator,
            "current_song_time": song.current_song_time,
            "set_signature": self._set_signature(),
            "tracks": tracks,
            "tracks_scanned": tracks_scanned,
            "return_tracks": returns,
            "scene_count": len(song.scenes),
        }
        if include_master:
            result["master_track"] = self._track_summary(song.master_track, None, 0, device_limit, 0)
        return result

    def _rpc_get(self, params):
        obj = self._resolve(params.get("ref"))
        props = {}
        for name in params.get("properties") or []:
            props[name] = getattr(obj, name)
        children = {}
        detail = self._detail(params)
        child_specs = params.get("children") or []
        if isinstance(child_specs, dict):
            child_items = child_specs.items()
        else:
            child_items = [(name, params.get("child_limit")) for name in child_specs]
        for name, limit in child_items:
            values, truncated = self._take(getattr(obj, name), limit)
            children[name] = [self._object_summary(child, detail) for child in values]
            if truncated:
                children[name].append({"truncated": True})
        summary = self._object_summary(obj, detail)
        summary["properties"] = props
        summary["children"] = children
        return summary

    def _rpc_set(self, params):
        obj = self._resolve(params.get("ref"))
        setattr(obj, params["property"], params.get("value"))
        return self._object_summary(obj, self._detail(params))

    def _rpc_call(self, params):
        obj = self._resolve(params.get("ref"))
        fn = getattr(obj, params["method"])
        return fn(*(params.get("args") or []), **(params.get("kwargs") or {}))

    def _rpc_children(self, params):
        obj = self._resolve(params.get("ref"))
        limit = params.get("limit")
        values, truncated = self._take(getattr(obj, params["child"]), limit)
        result = [self._object_summary(child, self._detail(params)) for child in values]
        if truncated:
            result.append({"truncated": True})
        return result

    def _rpc_device_parameters(self, params):
        device = self._resolve(params.get("ref"))
        query = (params.get("query") or "").strip().lower()
        terms = [term for term in query.split() if term]
        limit = params.get("limit")
        result = []
        max_matches = DEFAULT_CHILD_LIMIT if limit is None else limit
        unlimited = max_matches is not None and max_matches < 0
        truncated = False
        for param in getattr(device, "parameters"):
            name = getattr(param, "name", "")
            if terms and not all(term in name.lower() for term in terms):
                continue
            if not unlimited and len(result) >= max_matches:
                truncated = True
                break
            item = self._parameter_summary(param)
            result.append(item)
        if truncated:
            result.append({"truncated": True})
        return result

    def _rpc_parameter_set(self, params):
        param = self._resolve(params.get("ref"))
        before = self._parameter_summary(param)
        if not hasattr(param, "value"):
            raise AttributeError("Object has no value property")
        value = params.get("value")
        if value is None:
            raise ValueError("value is required")
        min_value = before.get("min")
        max_value = before.get("max")
        if params.get("coerce"):
            if min_value is not None and value < min_value:
                value = min_value
            if max_value is not None and value > max_value:
                value = max_value
            if before.get("is_quantized"):
                value = int(round(value))
        else:
            if min_value is not None and value < min_value:
                raise ValueError("value %s is below parameter min %s" % (value, min_value))
            if max_value is not None and value > max_value:
                raise ValueError("value %s is above parameter max %s" % (value, max_value))
            if before.get("is_quantized") and int(value) != value:
                raise ValueError("quantized parameter requires an integer value or coerce:true")
        param.value = value
        after = self._parameter_summary(param)
        return {
            "parameter": after,
            "before": before,
            "requested_value": params.get("value"),
            "applied_value": after.get("value"),
            "changed": after.get("value") != before.get("value"),
        }

    def _rpc_clip_notes(self, params):
        clip = self._resolve(params.get("ref"))
        if not hasattr(clip, "get_all_notes_extended"):
            raise RuntimeError("clip_notes requires get_all_notes_extended; refusing legacy note API")
        limit = int(params.get("limit") if params.get("limit") is not None else 512)
        start = params.get("start_time")
        end = params.get("end_time")
        notes = list(clip.get_all_notes_extended())
        if start is not None:
            notes = [note for note in notes if note.start_time >= float(start)]
        if end is not None:
            notes = [note for note in notes if note.start_time < float(end)]
        total = len(notes)
        truncated = False
        if limit >= 0 and len(notes) > limit:
            notes = notes[:limit]
            truncated = True
        return {
            "clip": self._clip_summary(clip, None),
            "note_api": "extended",
            "note_count": total,
            "truncated": truncated,
            "notes": [self._note_summary(note) for note in notes],
        }

    def _rpc_clip_update_notes(self, params):
        clip = self._resolve(params.get("ref"))
        if not hasattr(clip, "get_all_notes_extended") or not hasattr(clip, "apply_note_modifications"):
            raise RuntimeError("clip_update_notes requires extended note APIs; refusing legacy note API")
        updates = params.get("updates") or []
        notes = clip.get_all_notes_extended()
        by_id = dict((note.note_id, note) for note in notes)
        changed = []
        for update in updates:
            note_id = update.get("note_id")
            if note_id not in by_id:
                raise KeyError("Unknown note_id %s in clip %s" % (note_id, getattr(clip, "name", "")))
            note = by_id[note_id]
            for attr in ("pitch", "start_time", "duration", "velocity", "mute", "probability", "velocity_deviation", "release_velocity"):
                if attr in update:
                    setattr(note, attr, update[attr])
            changed.append(note)
        clip.apply_note_modifications(notes)
        return {
            "clip": self._clip_summary(clip, None),
            "note_api": "extended",
            "updated": len(changed),
            "notes": [self._note_summary(note) for note in changed],
        }

    def _rpc_clip_add_notes(self, params):
        target = self._resolve(params.get("ref"))
        created_clip = False
        replaced_clip = False
        launched = False
        legacy_note_api = False
        is_clip_slot, target_has_clip = self._clip_slot_state(target)
        if is_clip_slot:
            if params.get("replace_existing_clip"):
                length = params.get("create_clip_length")
                if length is None:
                    raise ValueError("clip_add_notes requires create_clip_length when replacing an existing clip")
                if target_has_clip:
                    if not hasattr(target, "delete_clip"):
                        raise ValueError("clip_add_notes replace_existing_clip requires a clip slot with delete_clip")
                    target.delete_clip()
                    replaced_clip = True
                    target_has_clip = False
                target.create_clip(float(length))
                created_clip = True
                target_has_clip = True
            if not target_has_clip:
                length = params.get("create_clip_length")
                if length is None:
                    raise ValueError("clip_add_notes requires create_clip_length when ref is an empty clip slot")
                target.create_clip(float(length))
                created_clip = True
                target_has_clip = True
            clip = target.clip
        else:
            if params.get("replace_existing_clip"):
                raise ValueError("clip_add_notes replace_existing_clip requires a clip slot ref, not a clip ref")
            clip = target
        if not getattr(clip, "is_midi_clip", False):
            raise ValueError("clip_add_notes requires a MIDI clip")
        if params.get("clip_name") is not None:
            try:
                clip.name = str(params.get("clip_name"))
            except Exception:
                pass
        for key, attr in (("loop_start", "loop_start"), ("loop_end", "loop_end")):
            if params.get(key) is not None:
                try:
                    setattr(clip, attr, float(params.get(key)))
                except Exception:
                    pass

        def remove_notes_extended(from_pitch, pitch_span, from_time, time_span):
            try:
                clip.remove_notes_extended(
                    from_pitch=int(from_pitch),
                    pitch_span=int(pitch_span),
                    from_time=float(from_time),
                    time_span=float(time_span),
                )
                return False
            except (AttributeError, TypeError):
                if not params.get("allow_legacy_note_api"):
                    raise RuntimeError("clip_add_notes clear requires remove_notes_extended; refusing legacy remove_notes unless allow_legacy_note_api is true")
                clip.remove_notes(float(from_time), int(from_pitch), float(time_span), int(pitch_span))
                return True

        if params.get("clear"):
            legacy_note_api = remove_notes_extended(0, 128, 0.0, float(getattr(clip, "length", 0.0))) or legacy_note_api
        clear_range = params.get("clear_range")
        if clear_range:
            legacy_note_api = remove_notes_extended(
                clear_range["from_pitch"],
                clear_range["pitch_span"],
                clear_range["from_time"],
                clear_range["time_span"],
            ) or legacy_note_api
        specs = []
        for note in params.get("notes") or []:
            specs.append(Live.Clip.MidiNoteSpecification(
                pitch=int(note["pitch"]),
                start_time=float(note["start_time"]),
                duration=float(note["duration"]),
                velocity=float(note["velocity"]),
                mute=bool(note.get("mute", False)),
            ))
        if specs:
            clip.add_new_notes(tuple(specs))
        if params.get("fire") and hasattr(target, "fire"):
            target.fire()
            launched = True
        return {
            "clip": self._clip_summary(clip, None),
            "note_api": "extended" if not legacy_note_api else "legacy",
            "added": len(specs),
            "created_clip": created_clip,
            "replaced_clip": replaced_clip,
            "launched": launched,
            "legacy_note_api": legacy_note_api,
            "note_count": len(list(clip.get_all_notes_extended())),
        }

    def _clip_slot_state(self, target):
        try:
            has_clip = bool(target.has_clip)
            return True, has_clip
        except Exception:
            pass
        try:
            clip = target.clip
            return True, clip is not None
        except Exception:
            return False, False

    def _rpc_clip_duplicate_to_arrangement(self, params):
        track = self._resolve(params.get("track"))
        clip = self._resolve(params.get("clip"))
        track.duplicate_clip_to_arrangement(clip, float(params["destination_time"]))
        return {
            "track": self._track_summary(track, None, 0, 0, 8),
            "clip": self._clip_summary(clip, None),
            "destination_time": float(params["destination_time"]),
        }

    def _rpc_clip_envelope(self, params):
        clip = self._resolve(params.get("ref"))
        parameter = self._resolve(params.get("parameter"))
        envelope = clip.automation_envelope(parameter)
        steps = params.get("insert_steps") or []
        if envelope is None and (params.get("create") or steps):
            envelope = clip.create_automation_envelope(parameter)
        if params.get("clear"):
            clip.clear_envelope(parameter)
            envelope = None
        if envelope is not None:
            delete_range = params.get("delete_range")
            if delete_range:
                envelope.delete_events_in_range(float(delete_range["start_time"]), float(delete_range["end_time"]))
            for step in steps:
                envelope.insert_step(float(step["time"]), float(step["duration"]), float(step["value"]))
        start = float(params.get("start_time") if params.get("start_time") is not None else 0.0)
        end = params.get("end_time")
        if end is None:
            end = getattr(clip, "length", start + 16.0)
        end = float(end)
        limit = int(params.get("limit") if params.get("limit") is not None else 128)
        events = []
        truncated = False
        total = 0
        if envelope is not None:
            all_events = list(envelope.events_in_range(start, end))
            total = len(all_events)
            if limit >= 0 and len(all_events) > limit:
                all_events = all_events[:limit]
                truncated = True
            events = [self._automation_event_summary(event) for event in all_events]
        return {
            "clip": self._clip_summary(clip, None),
            "parameter": self._parameter_summary(parameter),
            "has_envelope": envelope is not None,
            "event_count": total,
            "truncated": truncated,
            "events": events,
        }

    def _rpc_clip_velocity_envelope(self, params):
        clip = self._resolve(params.get("ref"))
        parameter = self._resolve(params.get("parameter"))
        if not getattr(clip, "is_midi_clip", False):
            raise ValueError("clip_velocity_envelope requires a MIDI clip")
        start = float(params.get("start_time") if params.get("start_time") is not None else 0.0)
        end = params.get("end_time")
        if end is None:
            end = getattr(clip, "length", start + 16.0)
        end = float(end)
        minimum = params.get("min_value")
        maximum = params.get("max_value")
        summary = self._parameter_summary(parameter)
        if minimum is None:
            minimum = summary.get("min", 0.0)
        if maximum is None:
            maximum = summary.get("max", 1.0)
        minimum = float(minimum)
        maximum = float(maximum)
        if params.get("invert"):
            minimum, maximum = maximum, minimum
        envelope = clip.automation_envelope(parameter)
        if envelope is None:
            envelope = clip.create_automation_envelope(parameter)
        if params.get("clear", True):
            envelope.delete_events_in_range(start, end)
        notes = [note for note in clip.get_all_notes_extended() if start <= note.start_time < end]
        for note in notes:
            velocity = float(getattr(note, "velocity", 0.0))
            normalized = max(0.0, min(1.0, velocity / 127.0))
            value = minimum + (maximum - minimum) * normalized
            duration = float(params.get("step_duration") if params.get("step_duration") is not None else getattr(note, "duration", 0.125))
            envelope.insert_step(float(note.start_time), duration, value)
        all_events = list(envelope.events_in_range(start, end))
        limit = int(params.get("limit") if params.get("limit") is not None else 128)
        truncated = False
        if limit >= 0 and len(all_events) > limit:
            all_events = all_events[:limit]
            truncated = True
        return {
            "clip": self._clip_summary(clip, None),
            "parameter": self._parameter_summary(parameter),
            "notes_mapped": len(notes),
            "event_count": len(list(envelope.events_in_range(start, end))),
            "truncated": truncated,
            "events": [self._automation_event_summary(event) for event in all_events],
        }

    def _rpc_clip_warp_markers(self, params):
        clip = self._resolve(params.get("ref"))
        if "warping" in params:
            clip.warping = bool(params.get("warping"))
        if params.get("warp_mode") is not None:
            clip.warp_mode = int(params.get("warp_mode"))
        for beat_time in params.get("remove_beat_times") or []:
            clip.remove_warp_marker(float(beat_time))
        for move in params.get("move_markers") or []:
            clip.move_warp_marker(float(move["beat_time"]), float(move["beat_time_delta"]))
        for marker in params.get("add_markers") or []:
            clip.add_warp_marker(Live.Clip.WarpMarker(float(marker["sample_time"]), float(marker["beat_time"])))
        limit = int(params.get("limit") if params.get("limit") is not None else 128)
        markers = list(getattr(clip, "warp_markers", []))
        total = len(markers)
        truncated = False
        if limit >= 0 and len(markers) > limit:
            markers = markers[:limit]
            truncated = True
        return {
            "clip": self._clip_summary(clip, None),
            "warping": getattr(clip, "warping", None),
            "warp_mode": getattr(clip, "warp_mode", None),
            "available_warp_modes": list(getattr(clip, "available_warp_modes", [])),
            "marker_count": total,
            "truncated": truncated,
            "markers": [self._warp_marker_summary(marker) for marker in markers],
        }

    def _rpc_track_create_audio_clip(self, params):
        track = self._resolve(params.get("ref"))
        clip = track.create_audio_clip(str(params["file_path"]), float(params["destination_time"]))
        if params.get("name"):
            clip.name = params.get("name")
        return {
            "track": self._track_summary(track, None, 0, 0, 8),
            "clip": self._clip_summary(clip, None),
            "destination_time": float(params["destination_time"]),
        }

    def _rpc_track_insert_device(self, params):
        track = self._resolve(params.get("ref"))
        index = int(params.get("device_index") if params.get("device_index") is not None else -1)
        before = len(getattr(track, "devices", []))
        result = track.insert_device(str(params["device_name"]), index)
        after = len(getattr(track, "devices", []))
        return {
            "track": self._track_summary(track, None, 0, 16, 0),
            "device_name": str(params["device_name"]),
            "device_index": index,
            "inserted": after > before,
            "result": result,
        }

    def _rpc_batch(self, params):
        results = []
        continue_on_error = bool(params.get("continue_on_error"))
        inherited = {}
        for name in ("detail", "include_repr", "max_items", "max_depth", "max_string_length", "timeout", "strict_timeout", "timeout_strict", "expected_set_signature"):
            if params.get(name) is not None:
                inherited[name] = params.get(name)
        for index, op in enumerate(params.get("operations") or []):
            method = op.get("method")
            op_params = op.get("params") or {}
            for name, value in inherited.items():
                if op_params.get(name) is None:
                    op_params[name] = value
            try:
                value = getattr(self, "_rpc_" + method)(op_params)
                results.append({"ok": True, "result": self._encode(value, self._encode_options(op_params))})
            except Exception as exc:
                item = {"ok": False, "index": index, "method": method, "error": str(exc)}
                if params.get("include_traceback"):
                    item["traceback"] = traceback.format_exc()
                results.append(item)
                if not continue_on_error:
                    break
        return _EncodedResult(results)

    def _rpc_browser_roots(self, _params):
        browser = Live.Application.get_application().browser
        roots = []
        for name in DEFAULT_BROWSER_ROOTS:
            if hasattr(browser, name):
                root = getattr(browser, name)
                roots.append({"name": name, "kind": root.__class__.__name__})
        return roots

    def _rpc_browser_capabilities(self, _params):
        browser = Live.Application.get_application().browser
        roots = self._rpc_browser_roots({})
        filter_types = {}
        try:
            for name, value in Live.Browser.FilterType.names.items():
                filter_types[str(name)] = int(value)
        except Exception:
            pass
        attrs = [name for name in dir(browser) if not name.startswith("_")]
        semantic_terms = ("semantic", "similar", "similarity")
        semantic_attrs = [name for name in attrs if any(term in name.lower() for term in semantic_terms)]
        return {
            "roots": roots,
            "filter_type": getattr(browser, "filter_type", None),
            "filter_types": filter_types,
            "semantic_search_exposed": bool(semantic_attrs),
            "semantic_search_attrs": semantic_attrs,
            "browser_attrs": attrs,
        }

    def _rpc_browser_search(self, params):
        browser = Live.Application.get_application().browser
        query = (params.get("query") or "").strip().lower()
        terms = [term for term in query.split() if term]
        root_names = params.get("roots") or list(DEFAULT_BROWSER_ROOTS)
        limit = int(params.get("limit") or 25)
        max_depth = int(params.get("max_depth") if params.get("max_depth") is not None else 8)
        max_visited = int(params.get("max_visited") or 5000)
        loadable_only = params.get("loadable_only")
        if loadable_only is None:
            loadable_only = True
        include_folders = bool(params.get("include_folders"))
        stop_on_limit = bool(params.get("stop_on_limit"))
        stop_score = int(params.get("stop_score") if params.get("stop_score") is not None else 0)
        match_all_terms = params.get("match_all_terms")
        if match_all_terms is None:
            match_all_terms = True

        matches = []
        visited = 0
        truncated = False

        def roots_for(name):
            if not hasattr(browser, name):
                return []
            root = getattr(browser, name)
            if self._is_browser_item(root):
                try:
                    return root.iter_children
                except Exception:
                    return (root,)
            try:
                return iter(root)
            except Exception:
                return ()

        def children_of(item):
            try:
                return item.iter_children
            except Exception:
                return ()

        def is_match(item, path_text):
            if not terms:
                return True
            haystack = (getattr(item, "name", "") + " " + path_text).lower()
            if match_all_terms:
                return all(term in haystack for term in terms)
            return any(term in haystack for term in terms)

        def score(item, path_text):
            name = getattr(item, "name", "").lower()
            if query and name == query:
                return 0
            if query and query in name:
                return 1
            if terms and all(term in name for term in terms):
                return 2
            if query and query in path_text.lower():
                return 3
            return 4

        def walk(root_name, item, path, depth):
            nonlocal visited, truncated
            if truncated or visited >= max_visited:
                truncated = True
                return
            visited += 1
            name = getattr(item, "name", "")
            current_path = path + [name]
            path_text = " > ".join([part for part in current_path if part])
            is_folder = bool(getattr(item, "is_folder", False))
            is_loadable = bool(getattr(item, "is_loadable", False))
            if is_match(item, path_text):
                if (include_folders or not is_folder) and (not loadable_only or is_loadable):
                    item_score = score(item, path_text)
                    matches.append((item_score, len(current_path), self._browser_item_result(root_name, item, path_text)))
                    good_matches = matches if not terms else [match for match in matches if match[0] <= stop_score]
                    if stop_on_limit and len(good_matches) >= limit:
                        truncated = True
                        return
            if depth >= max_depth:
                return
            for child in children_of(item):
                walk(root_name, child, current_path, depth + 1)
                if truncated:
                    return

        for root_name in root_names:
            for item in roots_for(root_name):
                walk(root_name, item, [root_name], 0)
                if truncated:
                    break
            if truncated:
                break

        matches.sort(key=lambda item: (item[0], item[1], item[2]["name"].lower()))
        results = [item[2] for item in matches[:limit]]
        return {"query": query, "roots": root_names, "visited": visited, "truncated": truncated, "results": results}

    def _rpc_browser_load(self, params):
        item = self._resolve_browser_item(params.get("item"))
        target = params.get("target_track")
        if target:
            self.song().view.selected_track = self._resolve(target)
        Live.Application.get_application().browser.load_item(item)
        return self._browser_item_result(None, item, None)

    def _rpc_browser_preview(self, params):
        browser = Live.Application.get_application().browser
        if params.get("stop"):
            browser.stop_preview()
            return {"previewing": False}
        item = self._resolve_browser_item(params.get("item"))
        browser.preview_item(item)
        return {"previewing": True, "item": self._browser_item_result(None, item, None)}

    def _rpc_eval(self, params):
        self._reject_legacy_note_api_code(params.get("expr"), params)
        ref = params.get("ref")
        obj = self._resolve(ref) if ref else None
        env = {
            "Live": Live,
            "song": self.song(),
            "app": Live.Application.get_application(),
            "obj": obj,
            "this": self,
        }
        return eval(params["expr"], env, {})

    def _rpc_exec(self, params):
        self._reject_legacy_note_api_code(params.get("code"), params)
        ref = params.get("ref")
        obj = self._resolve(ref) if ref else None
        env = {
            "Live": Live,
            "song": self.song(),
            "app": Live.Application.get_application(),
            "obj": obj,
            "this": self,
            "result": None,
        }
        exec(params["code"], env, env)
        return env.get("result")

    def _reject_legacy_note_api_code(self, code, params):
        if params.get("allow_legacy_note_api"):
            return
        compact = "".join(str(code or "").split())
        for name in LEGACY_NOTE_API_NAMES:
            if ("%s(" % name) in compact:
                raise RuntimeError("Refusing obsolete MIDI note API %s in live_exec/live_eval; use live_clip_add_notes/live_clip_update_notes or pass allow_legacy_note_api only for disposable compatibility testing" % name)

    def _rpc_observe(self, params):
        obj = self._resolve(params.get("ref"))
        prop = params["property"]
        key = (self._object_id(obj), prop)
        if params.get("enabled"):
            if key in self._listeners:
                return {"observing": True, "key": str(key)}
            callback = self._make_listener(obj, prop)
            add_name = "add_%s_listener" % prop
            getattr(obj, add_name)(callback)
            self._listeners[key] = (obj, prop, callback)
            return {"observing": True, "key": str(key)}
        if key in self._listeners:
            old_obj, old_prop, callback = self._listeners.pop(key)
            remove_name = "remove_%s_listener" % old_prop
            getattr(old_obj, remove_name)(callback)
        return {"observing": False, "key": str(key)}

    def _rpc_events(self, params):
        limit = params.get("limit") or 100
        events = self._events[:limit]
        self._events = self._events[limit:]
        return events

    def _resolve(self, ref):
        ref = ref or {"path": "live_set"}
        if "id" in ref:
            obj_id = int(ref["id"])
            if obj_id not in self._objects:
                raise KeyError("Unknown or stale object id %s; rerun get/search and use the new id" % obj_id)
            return self._objects[obj_id]
        return self._resolve_path(ref.get("path") or "live_set")

    def _resolve_browser_item(self, ref):
        if not ref:
            raise ValueError("Browser item ref is required")
        if "id" in ref:
            try:
                return self._resolve(ref)
            except KeyError:
                if not (ref.get("uri") or ref.get("path")):
                    raise
        uri = ref.get("uri")
        path = ref.get("path")
        if not (uri or path):
            return self._resolve(ref)
        item = self._find_browser_item(uri=uri, path=path)
        if item is None:
            raise KeyError("Could not resolve browser item by uri/path; rerun browser_search")
        return item

    def _find_browser_item(self, uri=None, path=None):
        browser = Live.Application.get_application().browser
        wanted_path = path.lower() if path else None

        def roots_for(name):
            if not hasattr(browser, name):
                return []
            root = getattr(browser, name)
            if self._is_browser_item(root):
                try:
                    return root.iter_children
                except Exception:
                    return (root,)
            try:
                return iter(root)
            except Exception:
                return ()

        def children_of(item):
            try:
                return item.iter_children
            except Exception:
                return ()

        def walk(item, current_path, depth):
            path_text = " > ".join([part for part in current_path if part])
            if uri:
                try:
                    if item.uri == uri:
                        return item
                except Exception:
                    pass
            if wanted_path and path_text.lower() == wanted_path:
                return item
            if depth >= DEFAULT_MAX_DEPTH:
                return None
            for child in children_of(item):
                found = walk(child, current_path + [getattr(child, "name", "")], depth + 1)
                if found is not None:
                    return found
            return None

        for root_name in DEFAULT_BROWSER_ROOTS:
            for item in roots_for(root_name):
                found = walk(item, [root_name, getattr(item, "name", "")], 0)
                if found is not None:
                    return found
        return None

    def _resolve_path(self, path):
        parts = path.split()
        if not parts:
            raise ValueError("Path must start with live_set, song, app, browser, or this")
        if parts[0] in ("live_set", "song"):
            obj = self.song()
        elif parts[0] == "app":
            obj = Live.Application.get_application()
        elif parts[0] == "browser":
            obj = Live.Application.get_application().browser
        elif parts[0] == "this":
            obj = self
        else:
            raise ValueError("Path must start with live_set, song, app, browser, or this")
        index = 1
        while index < len(parts):
            attr = parts[index]
            value = getattr(obj, attr)
            index += 1
            if index < len(parts):
                token = parts[index]
                try:
                    child_index = int(token)
                except ValueError:
                    obj = value
                    continue
                obj = value[child_index]
                index += 1
            else:
                obj = value
        return obj

    def _object_summary(self, obj, detail=False):
        obj_id = self._object_id(obj)
        self._remember_object(obj_id, obj)
        summary = {
            "id": obj_id,
            "class": obj.__class__.__name__,
        }
        if detail:
            summary["canonical_path"] = self._canonical_path(obj)
            summary["repr"] = repr(obj)
        return summary

    def _browser_item_result(self, root_name, item, path_text):
        obj_id = self._object_id(item)
        self._remember_object(obj_id, item)
        result = {
            "id": obj_id,
            "name": getattr(item, "name", ""),
            "class": item.__class__.__name__,
            "is_folder": bool(getattr(item, "is_folder", False)),
            "is_loadable": bool(getattr(item, "is_loadable", False)),
            "is_device": bool(getattr(item, "is_device", False)),
        }
        if root_name is not None:
            result["root"] = root_name
        if path_text:
            result["path"] = path_text
        try:
            result["uri"] = item.uri
        except Exception:
            pass
        try:
            result["source"] = item.source
        except Exception:
            pass
        return result

    def _parameter_summary(self, param):
        obj_id = self._object_id(param)
        self._remember_object(obj_id, param)
        result = {
            "id": obj_id,
            "name": getattr(param, "name", ""),
            "value": getattr(param, "value", None),
        }
        for attr in ("min", "max", "default_value", "display_value", "is_quantized"):
            try:
                result[attr] = getattr(param, attr)
            except Exception:
                pass
        try:
            result["display"] = param.str_for_value(param.value)
        except Exception:
            pass
        try:
            if getattr(param, "is_quantized", False):
                result["value_items"] = list(param.value_items)
        except Exception:
            pass
        return result

    def _track_summary(self, track, index, clip_slot_limit, device_limit, arrangement_clip_limit=0):
        summary = self._object_summary(track, False)
        if index is not None:
            summary["index"] = index
        summary["name"] = getattr(track, "name", "")
        for attr in ("is_foldable", "mute", "solo", "arm", "implicit_arm", "can_be_armed"):
            try:
                summary[attr] = getattr(track, attr)
            except Exception:
                pass
        devices = []
        try:
            device_values, device_truncated = self._take(track.devices, device_limit)
            devices = [self._device_summary(device) for device in device_values]
            if device_truncated:
                devices.append({"truncated": True})
        except Exception:
            pass
        summary["devices"] = devices
        clips = []
        try:
            clip_slots, slots_truncated = self._take(track.clip_slots, clip_slot_limit)
            summary["clip_slots_scanned"] = len(clip_slots)
            for slot_index, slot in enumerate(clip_slots):
                try:
                    if slot.has_clip:
                        clips.append(self._clip_summary(slot.clip, slot_index))
                except Exception:
                    pass
            if slots_truncated:
                summary["clip_slots_truncated"] = True
        except Exception:
            pass
        summary["clips"] = clips
        try:
            summary["arrangement_clip_count"] = len(track.arrangement_clips)
            if arrangement_clip_limit:
                arrangement_clips, arrangement_truncated = self._take(track.arrangement_clips, arrangement_clip_limit)
                summary["arrangement_clips"] = [self._clip_summary(clip, None) for clip in arrangement_clips]
                if arrangement_truncated:
                    summary["arrangement_clips"].append({"truncated": True})
        except Exception:
            pass
        return summary

    def _device_summary(self, device):
        summary = self._object_summary(device, False)
        summary["name"] = getattr(device, "name", "")
        try:
            summary["class_name"] = device.class_name
        except Exception:
            pass
        try:
            summary["can_have_chains"] = device.can_have_chains
        except Exception:
            pass
        return summary

    def _clip_summary(self, clip, slot_index):
        summary = self._object_summary(clip, False)
        if slot_index is not None:
            summary["slot"] = slot_index
        summary["name"] = getattr(clip, "name", "")
        for attr in ("is_midi_clip", "is_audio_clip", "is_session_clip", "is_arrangement_clip", "length", "loop_start", "loop_end", "muted", "has_envelopes"):
            try:
                summary[attr] = getattr(clip, attr)
            except Exception:
                pass
        return summary

    def _note_summary(self, note):
        return {
            "note_id": note.note_id,
            "pitch": note.pitch,
            "start_time": note.start_time,
            "duration": note.duration,
            "velocity": note.velocity,
            "mute": note.mute,
            "probability": note.probability,
            "velocity_deviation": note.velocity_deviation,
            "release_velocity": note.release_velocity,
        }

    def _automation_event_summary(self, event):
        return {
            "time": getattr(event, "time", None),
            "value": getattr(event, "value", None),
        }

    def _warp_marker_summary(self, marker):
        return {
            "beat_time": getattr(marker, "beat_time", None),
            "sample_time": getattr(marker, "sample_time", None),
        }

    def _object_id(self, obj):
        live_id = getattr(obj, "_live_ptr", None)
        if live_id is not None:
            return int(live_id)
        return id(obj)

    def _is_browser_item(self, obj):
        return hasattr(obj, "name") and hasattr(obj, "is_loadable") and hasattr(obj, "is_folder")

    def _canonical_path(self, obj):
        try:
            return obj.canonical_path
        except Exception:
            return None

    def _same_live_object(self, left, right):
        if left is right:
            return True
        left_path = self._canonical_path(left)
        right_path = self._canonical_path(right)
        if left_path and right_path:
            return left_path == right_path
        left_ptr = getattr(left, "_live_ptr", None)
        right_ptr = getattr(right, "_live_ptr", None)
        if left_ptr is not None and right_ptr is not None:
            return left_ptr == right_ptr
        return False

    def _detail(self, params):
        return bool(params and (params.get("detail") or params.get("include_repr")))

    def _encode_options(self, params):
        max_items = DEFAULT_MAX_ITEMS
        max_depth = DEFAULT_MAX_DEPTH
        max_string_length = DEFAULT_MAX_STRING_LENGTH
        if params:
            if params.get("max_items") is not None:
                max_items = params.get("max_items")
            if params.get("max_depth") is not None:
                max_depth = params.get("max_depth")
            if params.get("max_string_length") is not None:
                max_string_length = params.get("max_string_length")
        return {"detail": self._detail(params), "max_items": max_items, "max_depth": max_depth, "max_string_length": max_string_length, "depth": 0, "seen": set()}

    def _encode(self, value, options=None):
        if options is None:
            options = self._encode_options(None)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            limit = options.get("max_string_length")
            if limit is not None and limit >= 0 and len(value) > limit:
                return value[:limit] + "...<truncated %s chars>" % (len(value) - limit)
            return value
        obj_id = id(value)
        if obj_id in options["seen"]:
            return {"truncated": True, "reason": "cycle"}
        if options["max_depth"] is not None and options["max_depth"] >= 0 and options["depth"] >= options["max_depth"]:
            return {"truncated": True, "reason": "max_depth"}
        if isinstance(value, (list, tuple)):
            limit = options["max_items"]
            values = value if limit is None or limit < 0 else value[:limit]
            child_options = dict(options)
            child_options["depth"] = options["depth"] + 1
            child_options["seen"] = set(options["seen"])
            child_options["seen"].add(obj_id)
            result = [self._encode(item, child_options) for item in values]
            if limit is not None and limit >= 0 and len(value) > limit:
                result.append({"truncated": True, "omitted": len(value) - limit})
            return result
        if isinstance(value, dict):
            items = list(value.items())
            limit = options["max_items"]
            if limit is not None and limit >= 0:
                items = items[:limit]
            child_options = dict(options)
            child_options["depth"] = options["depth"] + 1
            child_options["seen"] = set(options["seen"])
            child_options["seen"].add(obj_id)
            result = dict((str(key), self._encode(item, child_options)) for key, item in items)
            if limit is not None and limit >= 0 and len(value) > limit:
                result["__truncated__"] = {"omitted": len(value) - limit}
            return result
        return self._object_summary(value, options["detail"])

    def _check_expected_set_signature(self, params):
        expected = params.get("expected_set_signature") if params else None
        if not expected:
            return
        current = self._set_signature()
        if current != expected:
            raise RuntimeError("Set changed since last inspection; expected set_signature %s but current is %s. Re-read live_set_summary before applying destructive edits." % (expected, current))

    def _set_signature(self):
        song = self.song()
        payload = {
            "tempo": getattr(song, "tempo", None),
            "signature_numerator": getattr(song, "signature_numerator", None),
            "signature_denominator": getattr(song, "signature_denominator", None),
            "scenes": [getattr(scene, "name", "") for scene in getattr(song, "scenes", [])],
            "tracks": [self._track_signature(track) for track in getattr(song, "tracks", [])],
            "returns": [self._track_signature(track) for track in getattr(song, "return_tracks", [])],
            "master": self._track_signature(getattr(song, "master_track", None)),
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    def _track_signature(self, track):
        if track is None:
            return None
        clips = []
        try:
            for index, slot in enumerate(track.clip_slots):
                if getattr(slot, "has_clip", False):
                    clips.append((index, self._clip_signature(slot.clip)))
        except Exception:
            pass
        arrangement = []
        try:
            arrangement = [self._clip_signature(clip) for clip in track.arrangement_clips]
        except Exception:
            pass
        devices = []
        try:
            devices = [(self._object_id(device), getattr(device, "name", ""), getattr(device, "class_name", "")) for device in track.devices]
        except Exception:
            pass
        state = {}
        for attr in ("name", "mute", "solo", "arm", "implicit_arm"):
            try:
                state[attr] = getattr(track, attr)
            except Exception:
                pass
        return {
            "id": self._object_id(track),
            "state": state,
            "devices": devices,
            "clips": clips,
            "arrangement": arrangement,
        }

    def _clip_signature(self, clip):
        state = {}
        for attr in ("name", "is_midi_clip", "is_audio_clip", "is_session_clip", "is_arrangement_clip", "length", "loop_start", "loop_end", "muted", "start_time", "end_time"):
            try:
                state[attr] = getattr(clip, attr)
            except Exception:
                pass
        return (self._object_id(clip), state)

    def _take(self, values, limit):
        if limit is None:
            limit = DEFAULT_CHILD_LIMIT
        if limit is not None and limit < 0:
            return list(values), False
        result = []
        for index, value in enumerate(values):
            if index >= limit:
                return result, True
            result.append(value)
        return result, False

    def _remember_object(self, obj_id, obj):
        if obj_id in self._objects:
            try:
                del self._objects[obj_id]
            except Exception:
                pass
        self._objects[obj_id] = obj
        while len(self._objects) > MAX_OBJECTS:
            try:
                first = next(iter(self._objects))
                del self._objects[first]
            except Exception:
                break

    def _make_listener(self, obj, prop):
        obj_id = self._object_id(obj)

        def listener():
            event = {"id": obj_id, "property": prop}
            try:
                event["value"] = self._encode(getattr(obj, prop))
            except Exception as exc:
                event["error"] = str(exc)
            self._events.append(event)
            if len(self._events) > 1000:
                self._events = self._events[-1000:]

        return listener

    def _remove_all_listeners(self):
        for _key, (obj, prop, callback) in list(self._listeners.items()):
            try:
                getattr(obj, "remove_%s_listener" % prop)(callback)
            except Exception:
                pass
        self._listeners = {}

    def _major_version(self, version):
        try:
            return int(str(version).split(".")[0])
        except Exception:
            return None


AbletonObjectMCP = AbletonLiveMCP
