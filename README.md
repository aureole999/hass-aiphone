# Aiphone WP-2MED — Monitor and two-way calls

Fork maintained by [aureole999](https://github.com/aureole999/hass-aiphone), based on [yufeikang/hass-aiphone](https://github.com/yufeikang/hass-aiphone).
[完整中文安装和操作说明](README.zh-CN.md).

v0.2.0b7 implements Monitor-to-talk, incoming-call answer, browser microphone uplink, downlink audio/video, mute, hangup and prerecorded replies using the official Type A app's protocol. Existing pairing and entity unique IDs are retained. Real local WebRTC tests pass; physical WP-2MED speaker playback and cloud interoperability still require device testing.

Install this repository through HACS (Integration category), select the prerelease and restart HA. Register `/aiphone-static/aiphone-intercom-card.js?v=0.2.0b7` as a JavaScript Module dashboard resource, then add:

```yaml
type: custom:aiphone-intercom-card
entity: camera.aiphone_wp_2med_entrance
```

Monitor only watches; Talk explicitly requests microphone permission and accepts the current ring or upgrades Monitor. Mute controls the local microphone. Hangup/closing a talking card stops capture and disconnects. HTTPS is required for browser microphone access. The native Entrance camera remains available for viewing; older HA camera players may not provide microphone controls. An Answer entity alone does not capture a microphone.

Upstream pairing and device documentation follows; the current audio workflow is documented in the Chinese guide above. This project does not provide RTSP, go2rtc or HomeKit talkback output.

---

# Aiphone WP-2MED — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

English | [日本語](README.ja.md)

> ⚠️ **Unofficial, independent project — not affiliated with Aiphone Co., Ltd.** Use at your own risk. By installing or using this software you accept the [DISCLAIMER](DISCLAIMER.md) in full.

Native Home Assistant integration for **Aiphone WP-2MED** video intercoms (single-family / 戸建 model, sold in Japan as part of the [VIXUS / VKZ-R](https://www.aiphone.co.jp/products/business/vkz/) series).

This integration talks directly to the unit's AWS IoT cloud over mTLS — the same channel the official iOS/Android app uses — and exposes doorbell events, live snapshots, on-demand monitor, recording, answer, and electric strike unlock as native HA entities. **No cloud relay, no scraping, no polling**: the integration receives push events from AWS IoT MQTT in real time.

> **Status**: Video and ring events have been used on hardware. The new two-way call implementation is locally tested and awaits user verification on the doorbell.

---

## Features

### Sensors

| Entity | Type | Description |
|---|---|---|
| `binary_sensor.<unit>_doorbell` | Occupancy | Flips `on` while the door station is ringing |
| `sensor.<unit>_doorbell_state` | string | `idle` / `ringing` / `answered` |
| `sensor.<unit>_last_caller` | string | DSP1 from the last ring (typically "玄関1") |

### Camera

| Entity | Description |
|---|---|
| `camera.<unit>_entrance` | Native WebRTC live video (v0.2.0b3) and received audio (v0.2.0b4), sharing incoming tracks with recordings. Opens Monitor when idle; snapshots use the live frame or the latest finalized mp4. Playback does not activate a microphone or answer calls. |

### Buttons

| Entity | Action |
|---|---|
| `button.<unit>_monitor` | On-demand 30-second active monitor (camera-on, no ring, doesn't bother other paired clients) |
| `button.<unit>_answer` | Answer a ring with `30021/31021 → 24002/25001`, or upgrade Monitor with `23022/24022`. |

### Lock

| Entity | Action |
|---|---|
| `lock.<unit>_door` | Send `MID 26021 SET_UNLOCK_REQ` to the unit's relay output (electric strike). Momentary — HA reverts to "locked" after 5 s, mirroring the unit's ~3 s auto-relock. |

### Auto-recording

Every doorbell ring auto-records to `/config/aiphone/recordings/<ts>-<caller>.mp4`. Active monitor sessions save as `monitor-<ts>.mp4`. Recording continues for 8 s after the call ends to flush mp4 mux.

---

## How it works

```
WP-2MED ──── AWS IoT Core (Tokyo) ───┐
   │  MQTT mTLS port 8883            │
   │  ALPN x-amzn-mqtt-ca            │
                                     ▼
                            HA (this integration)
                                     │
                                     ├─► binary_sensor / sensor
                                     ├─► button (monitor / answer)
                                     ├─► lock
                                     └─► camera + auto-recorder (aiortc)
```

- **Pairing**: same protocol the iOS app uses — LAN UDP discovery (port 51711/51712), TLS-52712 OTP exchange, HTTPS `/registClient` to get a client cert (RSA-2048, valid until 2070), then `01005` to register a display name. The whole sequence happens inside the **Home Assistant config flow** — no external tools.
- **Doorbell events**: subscribe to `<unit-mac>/#`, parse `MID 23001` / `24000` / `24002` to drive the doorbell state machine.
- **Video**: when a ring fires (or the monitor button is pressed), the integration speaks Aiphone's full SDP-over-MQTT WebRTC dance against the AWS Tokyo Janus VideoRoom SFU. We use `aiortc` as the local DTLS-SRTP peer; recordings land as MP4. A passthrough video track also exposes the live frame as a JPEG without waiting for the file to finalize.

---

## Requirements

- Home Assistant 2025.6 or newer (native asynchronous WebRTC camera API)
- An Aiphone **WP-2MED** unit on the same LAN as Home Assistant during initial pairing (subsequent operation is internet-only)
- Outbound HTTPS to `api.aiphone-app.net` (only during pairing)
- Outbound MQTT to `*.iot.ap-northeast-1.amazonaws.com:8883` (steady state)
- Free pairing slot on the unit (戸建 limit: 4 paired devices including all family phones)

The integration auto-installs `aiortc>=1.14.0` and `av>=10.0.0` on first load. On a Raspberry Pi, expect the first install to take a few minutes.

---

## Installation

### HACS (recommended once published)

1. Add this repository as a custom repository in HACS (category: integration)
2. Search for "Aiphone WP-2MED" and install
3. Restart Home Assistant

### Manual

```bash
# In your HA config directory
cd config/custom_components
git clone https://github.com/yufeikang/hass-aiphone.git aiphone-tmp
mv aiphone-tmp/custom_components/aiphone .
rm -rf aiphone-tmp
```

Restart Home Assistant.

---

## Pairing

1. **On the WP-2MED unit**: 設定 → 各種設定 → アプリ連携 → 端末追加 (or similar "add device" menu).
2. **In Home Assistant**: Settings → Devices & Services → Add Integration → search "Aiphone".
3. Enter a display name (this is what shows on the unit's call list — e.g. "HA").
4. Click Submit. Pairing finishes in 5–10 seconds. The new device appears on the unit's screen as one of the paired clients.

If pairing fails, make sure:
- HA is on the same LAN as the unit (UDP broadcast must reach 255.255.255.255:51711)
- The unit is currently in "端末追加" mode (it leaves this mode after ~30 s)
- No firewall is blocking outbound 443 to `api.aiphone-app.net`

---

## Status

| Feature | Status |
|---|---|
| Pairing in HA UI | ✅ Working |
| Doorbell ring events | ✅ Working in production |
| Auto-record on ring | ✅ Working in production |
| `camera.entrance` snapshots | ✅ Working — live frame + mp4 fallback |
| `button.monitor` (on-demand camera) | ✅ Working — 30 s mp4 captured end-to-end |
| `button.answer` | App-compatible Monitor/ring signaling; device verification pending |
| `lock.door` (26021 unlock) | ⚠️ Implemented, **physical effect not verifiable** by author (no electric strike wired) |
| Audio in recordings | Shared received audio is recorded when the device supplies packets |
| Hangup | `30031/31031 → 23003/24003`, plus local media cleanup |
| Browser two-way audio / prerecorded replies | Implemented; real local PCMU tests pass, physical speaker verification pending |
| HACS distribution | Custom integration repository with prereleases |

---

## Known issues

- **First snapshot lag**: ~3 seconds between doorbell ring and first JPEG frame, dominated by SDP exchange + DTLS handshake. The `camera` entity falls back to the previous mp4's last frame during this window.
- **Multi-client subscriber slot**: the cloud admits only one subscriber per ring. If the official phone app subscribes first, HA's recording for that ring may show `RSLT=400` and produce no video. In practice this hasn't been an issue because we typically subscribe within ~100 ms of the ring.

---

## Development

```bash
git clone https://github.com/yufeikang/hass-aiphone.git
cd hass-aiphone
uv venv
uv pip install -e ".[dev]"
```

Run tests (when there are any):

```bash
pytest
```

The integration is two layers:

```
custom_components/aiphone/
├── __init__.py            # config-entry setup + v1→v2 migration
├── manifest.json
├── const.py
├── config_flow.py         # multi-step pairing UI
├── pairing.py             # blocking LAN UDP + TLS + /registClient flow
├── coordinator.py         # MQTT (paho-mqtt) + state machine
├── media.py               # aiortc; passive + monitor capture; live frame tee
├── binary_sensor.py
├── sensor.py
├── button.py              # Monitor + Answer
├── camera.py              # Live frame → JPEG; mp4 fallback
└── lock.py                # 26021 unlock
```

Protocol-level questions (wire format, MID table, edge cases) are best raised on the issue tracker.

---

## Acknowledgements

This project was built from scratch via static analysis of the publicly distributed Android application and observation of the author's own network traffic on the author's own equipment. No proprietary documentation, internal source code, or non-public cryptographic material was used. There is no public Aiphone SDK.

---

## Disclaimer

This is an **independent, unofficial** community project. It is **not affiliated with, endorsed by, or sponsored by Aiphone Co., Ltd.** "Aiphone", "WP-2MED", "VKZ-R", and "VIXUS" are trademarks of Aiphone Co., Ltd.

Use of this integration **may violate** the manufacturer's Terms of Service. The user assumes all risk. Read the full [DISCLAIMER](DISCLAIMER.md) before installing.

For takedown requests from rights-holders, please open an issue or contact the maintainer.

---

## License

MIT — see [LICENSE](LICENSE). Use of the code is subject to the additional non-warranty and use-at-your-own-risk terms set out in [DISCLAIMER](DISCLAIMER.md).
