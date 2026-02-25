# PiOps Fleet Dashboard

Live ESP32 device management web UI for the PiOps cluster.

**Running at**: http://192.168.0.200:5055
**Service**: `piops-fleet` (systemd)

## Features

- **Live device grid** — Known·Live / Unknown·Live / Known·Offline with MQTT telemetry
- **Device detail panel** — telemetry, quick MQTT commands, firmware status
- **OTA deploy** — stream `tools/deploy.sh` output line-by-line in the browser
- **Firmware History** — git log per device role; flash any historical version with one click (rollback)
- **Change Role** — deploy any role in the repo to any live device (repurposing / adoption)

## Architecture

```
app.py                  Flask app + MQTT subscriber + REST/SSE endpoints
templates/index.html    Single-page UI (vanilla JS, SSE for live updates)
requirements.txt        flask, paho-mqtt
piops-fleet.service     systemd unit
```

## API

```
GET  /api/devices                       Full snapshot (known_live, known_offline, unknown_live)
GET  /api/devices/<id>                  Single device
POST /api/devices/<id>/cmd              Publish MQTT command
POST /api/devices/<id>/deploy           SSE stream: OTA current source version
GET  /api/devices/<id>/versions         Git history for role's main.cpp
POST /api/devices/<id>/rollback         SSE stream: flash a historical commit
GET  /api/roles                         All roles in the firmware repo
POST /api/devices/<id>/assign_role      SSE stream: flash a different role to a live device
GET  /events                            SSE: full snapshot every 3s
```

## Dependencies

- MQTT broker on 192.168.0.200:1883 (Mosquitto)
- esp32-firmware repo at `/home/pi/projects/esp32-firmware`
- `tools/deploy.sh` in that repo (builds on studio server 192.168.0.220)

## Run

```bash
sudo systemctl start piops-fleet
sudo systemctl status piops-fleet
```
