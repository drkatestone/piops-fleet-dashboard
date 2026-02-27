#!/usr/bin/env python3
"""PiOps ESP32 Fleet Dashboard

Subscribes to MQTT, tracks device state in memory, and serves a live
fleet view via SSE.  REST endpoints handle commands and deploy triggers.
"""

import json
import re
import subprocess
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

# ── Config ────────────────────────────────────────────────────────────────────
REPO_ROOT    = Path("/home/pi/projects/esp32-firmware")
MQTT_HOST    = "10.77.0.10"
MQTT_PORT    = 1883
MQTT_USER    = "esp32"
MQTT_PASS    = "iq2tz0QE9qVfjMZ"
OFFLINE_SECS = 90   # no heartbeat within this window → offline
PORT         = 5055

app = Flask(__name__)

# ── Shared device state ───────────────────────────────────────────────────────
# devices[device_id] = {fw, ip, rssi, uptime, mode, bri_strip, bri_matrix,
#                       last_seen, lw_status, raw}
devices: dict = {}
_lock = threading.Lock()
_mqtt_client = None

# ── Device registry ───────────────────────────────────────────────────────────

def load_registry() -> dict:
    """Scan devices/*/.device_id → {device_id: role}."""
    reg = {}
    for p in sorted((REPO_ROOT / "devices").glob("*/.device_id")):
        did = p.read_text().strip()
        if did:
            reg[did] = p.parent.name
    return reg


def build_mode(role: str) -> str:
    """Read .build_mode for a role, default 'local'."""
    p = REPO_ROOT / "devices" / role / ".build_mode"
    if p.exists():
        return p.read_text().strip() or "local"
    return "local"


def source_fw(role: str):
    """Read FW_VERSION string from source for a role, or None."""
    src = REPO_ROOT / "devices" / role / "src" / "main.cpp"
    if not src.exists():
        return None
    for line in src.read_text().splitlines():
        if "FW_VERSION" in line:
            m = re.search(r'"([^"]+)"', line)
            if m:
                return m.group(1)
    return None


# ── MQTT ──────────────────────────────────────────────────────────────────────

def _on_connect(client, userdata, flags, rc):
    print(f"[MQTT] Connected (rc={rc}), subscribing…", flush=True)
    client.subscribe("piops/+/heartbeat")
    client.subscribe("piops/+/status")


def _on_disconnect(client, userdata, rc):
    print(f"[MQTT] Disconnected (rc={rc}), will reconnect…", flush=True)


def _on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) < 3:
        return
    device_id, kind = parts[1], parts[2]
    with _lock:
        if device_id not in devices:
            devices[device_id] = {}
        if kind == "heartbeat":
            try:
                data = json.loads(msg.payload)
                devices[device_id].update(
                    last_seen=time.time(),
                    fw=data.get("fw", ""),
                    ip=data.get("ip", ""),
                    rssi=data.get("rssi"),
                    uptime=data.get("uptime"),
                    mode=data.get("mode"),
                    bri_strip=data.get("bri_strip"),
                    bri_matrix=data.get("bri_matrix"),
                    raw=data,
                )
            except Exception:
                pass
        elif kind == "status":
            devices[device_id]["lw_status"] = msg.payload.decode()


def _mqtt_thread():
    global _mqtt_client
    _mqtt_client = mqtt.Client()
    _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    _mqtt_client.on_connect = _on_connect
    _mqtt_client.on_message = _on_message
    _mqtt_client.on_disconnect = _on_disconnect
    _mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    while True:
        try:
            _mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            _mqtt_client.loop_forever(retry_first_connection=True)
        except Exception as e:
            print(f"[MQTT] Connection error: {e}, retrying in 5s…", flush=True)
            time.sleep(5)


# ── Snapshot helper ───────────────────────────────────────────────────────────

def snapshot() -> dict:
    """Categorise all known/live/unknown devices into three lists."""
    reg = load_registry()
    now = time.time()
    known_live, known_offline, unknown_live = [], [], []

    with _lock:
        state = {did: dict(d) for did, d in devices.items()}

    # Known devices (have .device_id)
    for did, role in reg.items():
        d = state.get(did, {})
        age = now - d.get("last_seen", 0)
        rec = dict(device_id=did, role=role, source_fw=source_fw(role),
                   build_mode=build_mode(role), **d)
        (known_live if age < OFFLINE_SECS else known_offline).append(rec)

    # Unknown live devices (heartbeat seen, no .device_id)
    for did, d in state.items():
        if did not in reg and (now - d.get("last_seen", 0)) < OFFLINE_SECS:
            unknown_live.append(dict(device_id=did, role=None, **d))

    return dict(
        known_live=known_live,
        known_offline=known_offline,
        unknown_live=unknown_live,
        ts=now,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/devices")
def api_devices():
    return jsonify(snapshot())


@app.route("/api/devices/<device_id>")
def api_device(device_id):
    reg = load_registry()
    role = reg.get(device_id)
    with _lock:
        d = dict(devices.get(device_id, {}))
    return jsonify(dict(
        device_id=device_id,
        role=role,
        source_fw=source_fw(role) if role else None,
        live=(time.time() - d.get("last_seen", 0)) < OFFLINE_SECS,
        **d,
    ))


@app.route("/api/devices/<device_id>/cmd", methods=["POST"])
def api_cmd(device_id):
    cmd = (request.json or {}).get("cmd", "").strip()
    if not cmd:
        return jsonify(error="missing cmd"), 400
    if not _mqtt_client:
        return jsonify(error="MQTT not ready"), 503
    _mqtt_client.publish(f"piops/{device_id}/cmd", cmd)
    return jsonify(ok=True, cmd=cmd)


@app.route("/api/devices/<device_id>/deploy", methods=["POST"])
def api_deploy(device_id):
    reg = load_registry()
    role = reg.get(device_id)
    if not role:
        return jsonify(error="no role assigned — add devices/<role>/.device_id first"), 400

    mode = build_mode(role)

    def generate():
        yield f"data: {json.dumps(f'▶ deploy.sh {role} --{mode}')}\n\n"
        proc = subprocess.Popen(
            [str(REPO_ROOT / "tools" / "deploy.sh"), role, f"--{mode}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            yield f"data: {json.dumps(line.rstrip())}\n\n"
        proc.wait()
        result = "✓ Deploy succeeded" if proc.returncode == 0 else f"✗ Deploy failed (exit {proc.returncode})"
        yield f"data: {json.dumps(result)}\n\n"
        yield "data: __DONE__\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/devices/<device_id>/versions")
def api_versions(device_id):
    reg = load_registry()
    role = reg.get(device_id)
    if not role:
        return jsonify(error="no role assigned"), 400

    rel_path = f"devices/{role}/src/main.cpp"
    log_result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "--format=%H|%ai|%s", "--", rel_path],
        capture_output=True, text=True,
    )
    versions = []
    for line in log_result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        commit_hash, date, msg = parts
        show = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{commit_hash}:{rel_path}"],
            capture_output=True, text=True,
        )
        fw_ver = None
        if show.returncode == 0:
            for src_line in show.stdout.splitlines():
                if "FW_VERSION" in src_line:
                    m = re.search(r'"([^"]+)"', src_line)
                    if m:
                        fw_ver = m.group(1)
                        break
        versions.append(dict(
            hash=commit_hash,
            date=date[:10],
            fw_version=fw_ver,
            commit_msg=msg,
        ))
    return jsonify(versions=versions, current_fw=source_fw(role))


@app.route("/api/devices/<device_id>/rollback", methods=["POST"])
def api_rollback(device_id):
    reg = load_registry()
    role = reg.get(device_id)
    if not role:
        return jsonify(error="no role assigned"), 400

    commit_hash = (request.json or {}).get("commit_hash", "").strip()
    if not commit_hash or not re.match(r'^[0-9a-f]{40}$', commit_hash):
        return jsonify(error="invalid commit_hash"), 400

    rel_path = f"devices/{role}/src/main.cpp"

    # Security: reject hashes not in this file's history
    check = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "--format=%H", "--", rel_path],
        capture_output=True, text=True,
    )
    if commit_hash not in set(check.stdout.strip().splitlines()):
        return jsonify(error="commit not in file history"), 400

    show = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{commit_hash}:{rel_path}"],
        capture_output=True, text=True,
    )
    if show.returncode != 0:
        return jsonify(error="could not read source from commit"), 500

    fw_ver = None
    for src_line in show.stdout.splitlines():
        if "FW_VERSION" in src_line:
            m = re.search(r'"([^"]+)"', src_line)
            if m:
                fw_ver = m.group(1)
                break

    rolled_src = show.stdout
    target = REPO_ROOT / "devices" / role / "src" / "main.cpp"
    backup = target.read_text()
    mode = build_mode(role)

    def generate():
        label = fw_ver or commit_hash[:8]
        yield f"data: {json.dumps(f'▶ rollback {role} --{mode} to {label}')}\n\n"
        try:
            target.write_text(rolled_src)
        except Exception as exc:
            yield f"data: {json.dumps(f'✗ Could not write source: {exc}')}\n\n"
            yield "data: __DONE__\n\n"
            return

        proc = subprocess.Popen(
            [str(REPO_ROOT / "tools" / "deploy.sh"), role, f"--{mode}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            yield f"data: {json.dumps(line.rstrip())}\n\n"
        proc.wait()

        if proc.returncode == 0:
            yield f"data: {json.dumps('✓ Rollback succeeded')}\n\n"
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "add", rel_path],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "commit", "-m",
                 f"Rollback {role} to {label}"],
                capture_output=True,
            )
        else:
            yield f"data: {json.dumps(f'✗ Deploy failed (exit {proc.returncode})')}\n\n"
            try:
                target.write_text(backup)
                yield f"data: {json.dumps('  Source restored to previous state')}\n\n"
            except Exception:
                pass

        yield "data: __DONE__\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/roles")
def api_roles():
    roles = []
    for p in sorted((REPO_ROOT / "devices").iterdir()):
        if p.is_dir() and (p / "src" / "main.cpp").exists():
            roles.append(dict(role=p.name, source_fw=source_fw(p.name)))
    return jsonify(roles=roles)


@app.route("/api/devices/<device_id>/assign_role", methods=["POST"])
def api_assign_role(device_id):
    role = (request.json or {}).get("role", "").strip()
    if not role:
        return jsonify(error="missing role"), 400
    if not (REPO_ROOT / "devices" / role / "src" / "main.cpp").exists():
        return jsonify(error="unknown role"), 400

    with _lock:
        d = dict(devices.get(device_id, {}))
    ip = d.get("ip", "")
    if not ip or not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
        return jsonify(error="device has no IP — must be live"), 400

    mode = build_mode(role)

    def generate():
        yield f"data: {json.dumps(f'▶ assign {role} {ip} --{mode} → {device_id}')}\n\n"
        proc = subprocess.Popen(
            [str(REPO_ROOT / "tools" / "deploy.sh"), role, ip, f"--{mode}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            yield f"data: {json.dumps(line.rstrip())}\n\n"
        proc.wait()
        result = ("✓ Role assigned — device will reboot with new identity"
                  if proc.returncode == 0
                  else f"✗ Role assignment failed (exit {proc.returncode})")
        yield f"data: {json.dumps(result)}\n\n"
        yield "data: __DONE__\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/events")
def events():
    """SSE: push full device snapshot every 3 s."""
    def generate():
        while True:
            yield f"data: {json.dumps(snapshot())}\n\n"
            time.sleep(3)
    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ── Boot ──────────────────────────────────────────────────────────────────────
threading.Thread(target=_mqtt_thread, daemon=True).start()
time.sleep(1.5)   # wait for MQTT connect + retained heartbeats to arrive

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
