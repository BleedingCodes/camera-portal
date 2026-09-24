# Changelog

All notable changes to camera-portal are documented here.

---

## [1.1.0] — 2026-09-23

### Added
- **Find cameras** (`onvif.py`, standard library only): searches the modem network with ONVIF WS-Discovery and lists each camera's IP, name, and model. Cameras already in the config show as Added.
- **Automatic stream detection**: when adding a camera, the portal logs in over ONVIF and asks the camera for its highest-resolution RTSP stream. Supports WS-Security password digest and HTTP Digest, and corrects for camera clocks that are set wrong.
- **Per-camera RTSP port and path**, stored in the config as `rtsp_port` and `rtsp_path`
- Stream path presets in the add dialog: Dahua / Amcrest, Hikvision, Reolink, Axis, TP-Link Tapo, or Other (enter the path and port)
- `edit_cameras.py set-path <id> <path> [--port N]`
- `edit_cameras.py list` shows each camera's port and path; `check` tests each camera's own port
- `test_core.py` asks for the stream path and port when adding the first camera

### Changed
- Adding a camera that is already in the config (same IP, port, and path) is refused if it is showing, and brought back if it was removed this session. It is never saved twice.
- Configs from v1.0.0 and pyqt-camera-dashboard v3 need no changes. Cameras without a saved port and path keep the Dahua/Amcrest default.

### Security
- Addresses reported by a camera are always rebuilt with the camera's own IP, so a device on the network can't redirect the portal (or a camera password) to another host.
- The camera password is never sent in plain text during detection (password digest only).
- Detection works only for private (local network) IP addresses. Camera replies over 1 MB, or containing an XML DOCTYPE, are refused.
- Names and models from the network are shown as plain text in the portal, never as HTML.

---

## [1.0.0] — 2026-09-23

First release. Ported from [pyqt-camera-dashboard](https://github.com/BleedingCodes/pyqt-camera-dashboard) v3.0.1. The PyQt5 desktop window is replaced by a web portal served on the local modem network.

### Added
- Flask web portal: camera grid, per-camera record/reconnect/remove, Start All / Stop All, add camera from the browser
- Tile stills refreshed about once a second, plus a click-to-open live MJPEG viewer (one camera at a time), so any number of cameras works within the browser's ~6-connection limit
- Two-layer access control: secret key in the link (every route returns 404 without it) and a portal password (scrypt hash) with a 30-day session cookie
- Login lockout: 5 wrong passwords from one IP address within 60 seconds blocks that address for up to 60 seconds
- Renew link: generates a new port and key while the server runs; the old link and all other sessions stop working
- Modem-network binding (`modem_link.py`): serves only on this computer's private IP on the modem's network, never on a public IP
- `edit_cameras.py`: list cameras, change a camera's IP, check RTSP port reachability
- `test_core.py`: runs the camera engine without the portal to confirm cameras connect and record
- Security headers: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`

### Changed (compared with pyqt-camera-dashboard v3.0.1)
- Camera engine moved to `camera_core.py`; workers are `threading.Thread` instead of `QThread`
- An offline camera's worker now waits for Reconnect instead of exiting
- Config errors are reported by type (wrong key, invalid JSON, missing `cameras` list)
- Add-camera validation adds an IPv4 format check and a 40-character name limit
- Platform: Linux only (the modem detection reads `/proc/net/route`)

### Fixed (issues present in pyqt-camera-dashboard v3.0.1)
- Recording race: the GUI thread could release the `VideoWriter` while the camera thread was writing to it. The writer is now used only inside the camera thread.
- Plaintext config window: v3 wrote plaintext JSON to disk and then encrypted it. The config is now encrypted in memory and written once through a temporary file (permissions 0o600) with an atomic replace.

### Fixed (pre-release QA)
- `CameraManager._start_worker()` no longer starts a second worker for a camera that is already running. Previously the first run of `test_core.py` opened the first camera twice and could write duplicate recordings.
- Tiles no longer each hold an open MJPEG stream. With 6 or more cameras, the open streams used up the browser's connection limit and stalled status updates and buttons.
- `python modem_link.py --renew` now works when the saved port is taken. Previously it stopped with the same "port in use" error it was meant to fix.
- Removed development notes from user-facing messages and module docstrings.
