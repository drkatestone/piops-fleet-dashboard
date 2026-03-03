#!/usr/bin/env python3
"""PiOps ESP32 Fleet Dashboard

Subscribes to MQTT, tracks device state in memory, and serves a live
fleet view via SSE.  REST endpoints handle commands and deploy triggers.
"""

import json
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import serial.tools.list_ports
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
    client.subscribe("piops/#")


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
        elif kind not in ("cmd", "log"):
            if "telemetry" not in devices[device_id]:
                devices[device_id]["telemetry"] = {}
            devices[device_id]["telemetry"][kind] = {
                "value": msg.payload.decode(),
                "ts": time.time(),
            }


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


# ── LLM role suggestion ──────────────────────────────────────────────────────

_anthropic_key = None

def load_anthropic_key() -> str:
    """Load Anthropic API key from Eidolon's SQLite DB, with env var fallback."""
    global _anthropic_key
    if _anthropic_key:
        return _anthropic_key
    import os
    db_path = Path("/home/pi/eidolon/eidolon.db")
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT value FROM eidolon_state WHERE key = 'anthropic_api_key'")
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                _anthropic_key = row[0]
                return _anthropic_key
        except Exception:
            pass
    _anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    return _anthropic_key


def role_catalog() -> str:
    """Build a text catalog of available roles from first 50 lines of each main.cpp."""
    parts = []
    for p in sorted((REPO_ROOT / "devices").iterdir()):
        if not p.is_dir() or p.name == "golden":
            continue
        src = p / "src" / "main.cpp"
        if not src.exists():
            continue
        lines = src.read_text().splitlines()[:50]
        parts.append(f"=== Role: {p.name} ===\n" + "\n".join(lines))
    return "\n\n".join(parts)


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
    retain = bool((request.json or {}).get("retain", False))
    _mqtt_client.publish(f"piops/{device_id}/cmd", cmd, retain=retain)
    return jsonify(ok=True, cmd=cmd, retain=retain)


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


@app.route("/api/suggest_role", methods=["POST"])
def api_suggest_role():
    desc = (request.json or {}).get("description", "").strip()
    if not desc:
        return jsonify(error="missing description"), 400

    key = load_anthropic_key()
    if not key:
        return jsonify(error="no Anthropic API key configured"), 500

    catalog = role_catalog()
    if not catalog:
        return jsonify(error="no roles found in device catalog"), 500

    import anthropic
    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=(
                "You are a device provisioning assistant for the PiOps ESP32 fleet. "
                "Given a user's description of their hardware or intended use, suggest the "
                "best matching role from the catalog below. Respond with JSON only:\n"
                '{"role": "<role_name>", "explanation": "<2-3 sentence explanation>"}\n\n'
                "If no role is a good match, set role to null and explain why.\n\n"
                f"Role catalog:\n{catalog}"
            ),
            messages=[{"role": "user", "content": desc}],
        )
        text = resp.content[0].text.strip()
        # Extract JSON from response (may be wrapped in markdown fences)
        json_match = re.search(r'\{[^}]+\}', text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            return jsonify(role=result.get("role"), explanation=result.get("explanation", ""))
        return jsonify(error="unexpected response format"), 500
    except anthropic.APIError as e:
        return jsonify(error=f"Claude API error: {e.message}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/api/devices/<device_id>/adopt", methods=["POST"])
def api_adopt(device_id):
    """Adopt an unknown live device: deploy role firmware, capture new identity,
    write .device_id, update platformio.ini, git commit."""
    role = (request.json or {}).get("role", "").strip()
    if not role:
        return jsonify(error="missing role"), 400
    if not (REPO_ROOT / "devices" / role / "src" / "main.cpp").exists():
        return jsonify(error=f"unknown role '{role}'"), 400

    # Reject if role already has a .device_id
    did_file = REPO_ROOT / "devices" / role / ".device_id"
    if did_file.exists() and did_file.read_text().strip():
        existing = did_file.read_text().strip()
        return jsonify(error=f"role '{role}' already assigned to {existing} — remove .device_id first"), 400

    with _lock:
        d = dict(devices.get(device_id, {}))
    ip = d.get("ip", "")
    if not ip or not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
        return jsonify(error="device has no IP — must be live"), 400

    mode = build_mode(role)

    expected_fw = source_fw(role)

    def generate():
        yield f"data: {json.dumps(f'▶ Adopting {device_id} as {role}')}\n\n"
        yield f"data: {json.dumps(f'  Target IP: {ip}')}\n\n"

        # Check if device is already running the correct firmware for this role
        with _lock:
            d_now = dict(devices.get(device_id, {}))
        already_correct = d_now.get("fw") == expected_fw

        if already_correct:
            yield f"data: {json.dumps(f'✓ Device already running {expected_fw} — skipping deploy')}\n\n"
            new_did = device_id
        else:
            # Step 1: Deploy role firmware to device IP
            yield f"data: {json.dumps(f'▶ deploy.sh {role} {ip} --{mode}')}\n\n"
            proc = subprocess.Popen(
                [str(REPO_ROOT / "tools" / "deploy.sh"), role, ip, f"--{mode}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in proc.stdout:
                yield f"data: {json.dumps(line.rstrip())}\n\n"
            proc.wait()

            if proc.returncode != 0:
                yield f"data: {json.dumps(f'✗ Deploy failed (exit {proc.returncode})')}\n\n"
                yield "data: __DONE__\n\n"
                return

            yield f"data: {json.dumps('✓ Deploy succeeded — waiting for identity...')}\n\n"

            # Step 2: Poll MQTT state for heartbeat from same IP (new or same device_id)
            new_did = None
            deadline = time.time() + 90
            while time.time() < deadline:
                time.sleep(3)
                with _lock:
                    for did, dev in devices.items():
                        if dev.get("ip") == ip:
                            age = time.time() - dev.get("last_seen", 0)
                            if age < OFFLINE_SECS and dev.get("fw", "") == expected_fw:
                                new_did = did
                                break
                if new_did:
                    break
                remaining = int(deadline - time.time())
                if remaining > 0 and remaining % 15 < 3:
                    yield f"data: {json.dumps(f'  Waiting for heartbeat... ({remaining}s remaining)')}\n\n"

            if not new_did:
                yield f"data: {json.dumps('✗ Timed out waiting for device identity')}\n\n"
                yield f"data: {json.dumps('  Device may have rebooted — check Unknown Live manually')}\n\n"
                yield "data: __DONE__\n\n"
                return

        yield f"data: {json.dumps(f'✓ Identity: {new_did}')}\n\n"

        # Step 3: Write .device_id
        try:
            did_file.write_text(new_did + "\n")
            yield f"data: {json.dumps(f'✓ Wrote devices/{role}/.device_id')}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps(f'✗ Failed to write .device_id: {exc}')}\n\n"
            yield "data: __DONE__\n\n"
            return

        # Step 4: Update platformio.ini upload_port
        try:
            updated = update_upload_port(role, ip)
            if updated:
                yield f"data: {json.dumps(f'✓ Updated devices/{role}/platformio.ini upload_port = {ip}')}\n\n"
            else:
                yield f"data: {json.dumps(f'  platformio.ini unchanged (upload_port already correct or not found)')}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps(f'⚠ Could not update platformio.ini: {exc}')}\n\n"

        # Step 5: Git add + commit
        try:
            files_to_add = [f"devices/{role}/.device_id"]
            ini_path = REPO_ROOT / "devices" / role / "platformio.ini"
            if ini_path.exists():
                files_to_add.append(f"devices/{role}/platformio.ini")
            for f in files_to_add:
                subprocess.run(
                    ["git", "-C", str(REPO_ROOT), "add", f],
                    capture_output=True,
                )
            result = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "commit", "-m",
                 f"Adopt {new_did} as {role}"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                yield f"data: {json.dumps(f'✓ Committed: Adopt {new_did} as {role}')}\n\n"
            else:
                yield f"data: {json.dumps(f'⚠ Git commit: {result.stderr.strip()}')}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps(f'⚠ Git error: {exc}')}\n\n"

        yield f"data: {json.dumps(f'✓ Adoption complete — {new_did} is now {role}')}\n\n"
        yield "data: __DONE__\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


def update_upload_port(role: str, ip: str) -> bool:
    """Update upload_port in platformio.ini for a role. Returns True if changed."""
    ini = REPO_ROOT / "devices" / role / "platformio.ini"
    if not ini.exists():
        return False
    text = ini.read_text()
    new_text = re.sub(r'(?m)^(upload_port\s*=\s*).*$', rf'\g<1>{ip}', text)
    if new_text == text:
        return False
    ini.write_text(new_text)
    return True


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


@app.route("/api/usb_devices")
def api_usb_devices():
    """List USB serial ports suitable for ESP32 flashing."""
    ports = []
    for p in serial.tools.list_ports.comports():
        if p.vid is not None:  # filter to USB devices only
            ports.append(dict(
                device=p.device,
                description=p.description or p.device,
                vid=f"{p.vid:04x}" if p.vid else None,
                pid=f"{p.pid:04x}" if p.pid else None,
            ))
    return jsonify(ports=ports)


@app.route("/api/usb_flash", methods=["POST"])
def api_usb_flash():
    """Flash firmware to a USB-connected ESP32 via deploy.sh --usb."""
    data = request.json or {}
    role = data.get("role", "").strip()
    port = data.get("port", "").strip()

    if not role:
        return jsonify(error="missing role"), 400
    if not (REPO_ROOT / "devices" / role / "src" / "main.cpp").exists():
        return jsonify(error=f"unknown role '{role}'"), 400
    if not port or not re.match(r'^/dev/tty\w+', port):
        return jsonify(error="invalid port"), 400

    def generate():
        yield f"data: {json.dumps(f'▶ deploy.sh {role} --usb {port}')}\n\n"
        proc = subprocess.Popen(
            [str(REPO_ROOT / "tools" / "deploy.sh"), role, "--usb", port],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            yield f"data: {json.dumps(line.rstrip())}\n\n"
        proc.wait()
        result = ("✓ USB flash succeeded"
                  if proc.returncode == 0
                  else f"✗ USB flash failed (exit {proc.returncode})")
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
