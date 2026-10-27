# Aiphone WP-2MED for Home Assistant

Live video, cached snapshots, ringing notifications, two-way audio and quick replies for Aiphone WP-2MED doorbells.

## Setup

Install through HACS as a custom integration, restart Home Assistant, then add **Aiphone WP-2MED** under **Settings → Devices & Services**. Put the indoor station in **Add Terminal** mode and follow the pairing flow.

Pairing needs LAN access to UDP 51711/51712. Normal operation uses Aiphone cloud services. Pair a separate terminal if you also use Scrypted.

For the intercom card, add `/aiphone-static/aiphone-intercom-card.js?v=0.2.0b8` as a JavaScript Module dashboard resource:

```yaml
type: custom:aiphone-intercom-card
entity: camera.aiphone_wp_2med_entrance
```

Open **Monitor** to view video. **Talk / Answer** enables the microphone; HTTPS is required. The card also provides quick reply, mute and hangup controls.

## Options

- Enable unlock controls, quick reply and the ring sensor individually.
- Quick reply works from idle, monitoring or an active call. It waits for audio, temporarily takes over the microphone, and leaves the call connected after playback.
- Leave **Reply audio filename** blank for the included Japanese message, or place your recording under `/config/aiphone/replies/`.
- **Auto answer**: `off`, `ringing`, or `always` when opening live video. The microphone requires an explicit action.
- **Video output**: `direct` or experimental `status_osd`, which shows a cached image while connecting and adds call status to video.
- Enable **Save video recordings** to retain incoming and monitor video.

Camera and image requests use the cached snapshot and do not start a video session. Existing pairing and entity IDs are retained when upgrading.

## Development

Install `requirements-test.txt`, then run `python -m pytest tests` and `node --test tests/frontend.test.cjs`. Tests use local simulated devices.

MIT — see [LICENSE](LICENSE). This is an unofficial community integration; see [DISCLAIMER](DISCLAIMER.md).
