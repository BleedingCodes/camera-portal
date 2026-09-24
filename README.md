# camera-portal

A self-hosted web portal for RTSP IP cameras: live view, recording, and encrypted credentials, served only on your local modem network.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![Flask](https://img.shields.io/badge/Web-Flask-green)
![OpenCV](https://img.shields.io/badge/OpenCV-Video-red)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Platform](https://img.shields.io/badge/Platform-Linux-lightgrey)

One Linux computer connects to your cameras and records them. Any phone or laptop on the same modem Wi-Fi opens the portal in a browser. No app to install, no cloud account, no port forwarding.

camera-portal is the successor to [pyqt-camera-dashboard](https://github.com/BleedingCodes/pyqt-camera-dashboard). It uses the same camera engine and the same encrypted config file. The PyQt5 desktop window is replaced by a browser portal.

---

## What It Does

- Finds ONVIF cameras on the network and asks each one for its own stream address, so you don't need to know IPs or RTSP paths
- Works with any RTSP camera. Built-in path presets cover Dahua/Amcrest, Hikvision, Reolink, Axis, and TP-Link Tapo; any other path can be entered by hand.
- Shows every camera in a grid of still images that refresh about once a second. Click a camera for live video.
- Records each camera to MP4 in 15-minute files, sorted by date and hour
- Reconnects dropped cameras automatically. After 3 failed attempts a camera shows **Offline** until you press Reconnect.
- Deletes the oldest recordings when the disk passes 75% full
- Stores camera credentials encrypted (Fernet: AES-128-CBC + HMAC-SHA256). Passwords are never shown in the portal.
- Protects the portal with a secret link plus a password, and serves it only on the modem network

## Who It's For

Home and small-site users running RTSP IP cameras on a local network, who want to watch and record from any device on that network without a vendor app or cloud service. It also suits temporary and portable setups (job sites, events, labs, integrators commissioning cameras): plug a Linux machine into the site's router, click Find cameras, and share the link.

---

## Requirements

| Item | Requirement |
|---|---|
| OS | Linux (Ubuntu/Debian tested). **Windows and macOS are not supported.** The network detection reads `/proc/net/route`. |
| Python | 3.11 or higher |
| Cameras | Any camera with an RTSP stream. ONVIF support (Profile S) is needed only for Find cameras and automatic stream detection. |
| Network | The Linux computer and the viewing devices on the same modem/router network. The computer must get a **private** IP address (for example `192.168.x.x`, `10.x.x.x`). |
| Browser | Any current Chrome, Edge, Firefox, or Safari (desktop or phone) |

Python packages (`requirements.txt`): `opencv-python-headless`, `cryptography`, `numpy`, `flask` (3.0 or later).

---

## Installation

1. Install system packages:

   ```bash
   sudo apt update
   sudo apt install python3 python3-pip python3-venv git
   ```

2. Clone the repo:

   ```bash
   git clone https://github.com/BleedingCodes/camera-portal.git
   cd camera-portal
   ```

3. Create and activate a **new** virtual environment. Do not reuse a pyqt-camera-dashboard venv: it contains `opencv-python`, which conflicts with `opencv-python-headless` and breaks `import cv2`.

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

4. Install the Python packages:

   ```bash
   pip install -r requirements.txt
   ```

---

## Moving From pyqt-camera-dashboard

Skip this section if you are a new user.

1. Stop pyqt-camera-dashboard.
2. Copy `camera_config.json` from the pyqt-camera-dashboard folder into the `camera-portal` folder.
3. Copy `secret.key` from the pyqt-camera-dashboard folder into the `camera-portal` folder. Without this exact file the config cannot be decrypted.
4. Optional: move the `recordings/` folder too. New recordings go to `camera-portal/recordings/`.

Your cameras, names, and recording filename prefixes carry over unchanged.

---

## First Run

1. Connect the Linux computer to the modem network.
2. From the `camera-portal` folder, with the venv active, run:

   ```bash
   python run_server.py
   ```

3. When asked, choose the portal password (8 characters minimum) and type it twice. The password is stored only as a scrypt hash inside the encrypted config.
4. Wait for the banner. It shows the portal link, for example:

   ```text
    Portal link   : http://192.168.12.23:41873/?key=Xy3...
   ```

   The same link is saved in `portal_url.txt` (readable only by your user account).
5. Open that link on a phone or laptop connected to the modem's Wi-Fi.
6. Sign in with the portal password.
7. New users: add your cameras (see [Adding Cameras](#adding-cameras)).

The first run must happen in a terminal, because the password prompt needs one. After that, the port and key stay the same across restarts until you press **Renew link**.

**Bookmark the full link, including `?key=...`.** Without the key, the server answers every request with "not found".

---

## Adding Cameras

### Find cameras (ONVIF)

1. Click **Find cameras**. The portal searches the modem network for about 3 seconds and lists every ONVIF camera that answers, with its IP, name, and model.
2. Click **Add** next to a camera. The add dialog opens with the IP and name filled in.
3. Enter the camera's username and password.
4. Leave **Stream path** on **Detect automatically (ONVIF)**.
5. Click **Add**. The portal logs in to the camera, asks for its highest-resolution stream, saves it, and starts the camera.

### Add a camera by IP address

Use this when Find cameras doesn't list the camera (ONVIF turned off, or the camera is on a different network segment).

1. Click **+ Add camera**.
2. Enter the IP address, a name, the username, and the password.
3. Choose **Stream path**:

| Choice | Path used (port 554) |
|---|---|
| Detect automatically (ONVIF) | Whatever the camera reports |
| Dahua / Amcrest | `/cam/realmonitor?channel=1&subtype=0` |
| Hikvision | `/Streaming/Channels/101` |
| Reolink | `/h264Preview_01_main` |
| Axis | `/axis-media/media.amp` |
| TP-Link Tapo | `/stream1` |
| Other (enter the path) | The path and port you type |

The presets are each brand's usual main-stream path. Firmware varies, so if a preset camera stays Offline, check the RTSP path in its manual or web settings and use **Other**. For example, some newer Reolink H.265 models use `/Preview_01_main` instead.

4. Click **Add**.

### ONVIF notes

- **ONVIF may be off by default.** Turn it on in the camera's own web settings. Reolink, for example, has it under network/advanced server settings.
- **Some brands use a separate ONVIF account.** Hikvision requires an ONVIF user created in the camera's settings. Enter that user in the add dialog.
- **Find cameras only reaches the local network segment.** Routers do not pass the search message between subnets. Cameras on another subnet can still be added by IP with a preset or Other.
- **Detection picks the highest-resolution stream.** On a busy Wi-Fi network, or with many cameras, a camera's lower-resolution sub-stream may run better. Enter it with **Other** (for example Hikvision `/Streaming/Channels/102`).
- A camera already in the config shows as **Added** in the search results. Adding the same camera again is refused while it's showing. If you removed it this session, adding it again brings it back using its saved login.

---

## Using the Portal

### Camera tiles

| Control | What it does |
|---|---|
| Click the image | Opens live video for that camera. Only one camera plays live at a time. |
| Start / Stop Recording | Records that camera only. Disabled while the camera is offline. |
| Reconnect | Retries that camera now, including one marked **Offline** |
| Remove | Stops and hides the camera until the server restarts. It stays in the config (see [Known limitations](#known-limitations)). |

### Toolbar

| Button | What it does |
|---|---|
| Find cameras | Searches the modem network for ONVIF cameras |
| + Add camera | Adds a camera by IP address, saves it to the encrypted config, and starts it |
| Start All | Starts recording on every camera that is live |
| Stop All | Stops recording on every camera |
| Renew link | Makes a new port and key. The old link stops working, every other device is signed out, and this browser moves to the new link. |
| Log out | Ends the session on this device |

Several browsers can use the portal at once. Changes made in one browser show up in the others within about 2 seconds.

---

## Command-Line Tools

Run these from the `camera-portal` folder with the venv active.

### run_server.py

```bash
python run_server.py                     # normal start
python run_server.py --reset-password    # choose a new portal password (signs everyone out)
python run_server.py --interface wlan0   # always use this network interface (remembered)
python run_server.py --auto              # forget the pinned interface, follow the default route
```

Stop the server with **Ctrl+C**.

### edit_cameras.py

Stop `run_server.py` before running `set-ip` or `set-path`. The running server keeps its own copy of the config and would overwrite your change on its next save.

```bash
python edit_cameras.py list                                     # IDs, names, IPs, ports, paths (no passwords)
python edit_cameras.py set-ip front_door 192.168.12.201
python edit_cameras.py set-path front_door '/Streaming/Channels/102'
python edit_cameras.py set-path front_door '/stream1' --port 8554
python edit_cameras.py check                                    # can this computer reach each camera's RTSP port?
```

- The camera ID is the first column of `list`.
- Put the path in single quotes. Paths containing `&` or `?` otherwise break in the shell.
- `check` tests the network path only. It does not test the camera password or the stream path.

### modem_link.py

```bash
python modem_link.py            # show which interface and IP the portal will use
python modem_link.py --renew    # make a new port and key without starting the portal
```

### test_core.py

Checks that cameras connect and record, without the portal. Stop `run_server.py` first.

```bash
python test_core.py             # prints status for 30 seconds, saves one snapshot_<id>.jpg per camera
python test_core.py --record    # also records for 30 seconds
```

Open the `snapshot_*.jpg` files to confirm each picture.

---

## Recordings

```text
recordings/
└── YYYY-MM-DD/
    └── HH/
        └── <camera_id>_YYYY-MM-DD_HH-MM-SS.mp4
```

- Each file covers up to 15 minutes. A new file starts automatically.
- Recordings are full camera resolution. Only the browser view is scaled down.
- Disk cleanup runs at startup and every hour. When the **whole drive** holding `recordings/` passes 75% used, the oldest `.mp4` files under `recordings/` are deleted until usage drops below 75%. If other data fills that drive, recordings are deleted to make room. Put `recordings/` on its own drive, or change `MAX_DISK_USAGE`, if that is a problem.

---

## Security

### How access works

1. **Secret link key.** A 192-bit random key in the link. Any request without it gets 404, so the portal is invisible to anyone who does not have the link.
2. **Portal password.** Unlocks a signed session cookie for 30 days per device. 5 wrong passwords from one IP address within 60 seconds blocks that address for up to 60 seconds.
3. **Network binding.** The server listens only on this computer's private IP on the modem network. It refuses to start on a public IP.

Renewing the link or changing the password signs out every other device.

### Files that must never be shared or committed

| File | Contains |
|---|---|
| `secret.key` | The encryption key for the config |
| `camera_config.json` | Encrypted camera credentials, portal password hash, link key, session secret |
| `portal_url.txt` | The full portal link, including the key |
| `recordings/` | Video |

All of these are listed in `.gitignore`. Do not remove them from it.

---

## Known Limitations

- **Plain HTTP, no TLS.** The link key and the password cross the Wi-Fi unencrypted at the HTTP level. The Wi-Fi's own WPA2/WPA3 encryption protects them from outsiders, but another device on the same network that captures traffic could read them. Use this only on a network where you trust every connected device. Never forward the portal's port on the modem.
- **Linux only.**
- **Remove is session-only.** There is no permanent delete. A removed camera comes back when the server restarts, or sooner if you add it again.
- **Find cameras needs ONVIF and the same network segment.** Everything else works with plain RTSP.
- **No autostart.** The server runs while the terminal session runs. Setting it up as a system service is not covered here.
- **Hard stop can cut the last file.** If the process is killed (not stopped with Ctrl+C), the MP4 being written may not play.

---

## Hardcoded Values

Change these in the source if your setup differs.

| Value | File | Constant | Default |
|---|---|---|---|
| Default RTSP path (cameras with none saved) | `camera_core.py` | `RTSP_PATH` | `/cam/realmonitor?channel=1&subtype=0` |
| Default RTSP port (cameras with none saved) | `camera_core.py` | `RTSP_PORT` | `554` |
| Brand path presets | `templates/dashboard.html` | `<select id="stream-select">` | See [Adding Cameras](#adding-cameras) |
| Find cameras listen time | `onvif.py` | `DISCOVERY_SECONDS` | `3.0` s |
| ONVIF request timeout | `onvif.py` | `SOAP_TIMEOUT_SECONDS` | `5` s |
| ONVIF service path (when not discovered) | `onvif.py` | `DEFAULT_DEVICE_PATH` | `/onvif/device_service` |
| Recording file length | `camera_core.py` | `RECORD_DURATION_SECONDS` | `900` (15 min) |
| Disk cleanup threshold | `camera_core.py` | `MAX_DISK_USAGE` | `75` (%) |
| Failures before Offline | `camera_core.py` | `MAX_CONNECT_FAILURES` | `3` |
| Live viewer frame rate | `web_portal.py` | `STREAM_FPS` | `10` |
| Live viewer width | `web_portal.py` | `STREAM_MAX_WIDTH` | `960` px |
| Tile still width | `web_portal.py` | `SNAPSHOT_MAX_WIDTH` | `640` px |
| Tile refresh interval | `static/app.js` | `SNAPSHOT_MS` | `1000` ms |
| Session length | `web_portal.py` | `SESSION_DAYS` | `30` |
| Port range | `modem_link.py` | `PORT_RANGE` | `20000–60999` |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Waiting for the modem to assign an IP on ...` never ends | This computer has no private IP on the modem network | Check the cable or Wi-Fi connection. If the computer has several networks, pin the right one: `python run_server.py --interface <name>` (list names with `ip -br addr`). |
| `Port NNNNN is in use` | The portal is already running, or another program took the port | Stop the other copy. If none is running: `python modem_link.py --renew` |
| `Config error: Could not decrypt camera_config.json` | `secret.key` does not match the config | Copy the `secret.key` that was used with this config into the folder |
| `No portal password set. Run 'python run_server.py' once in a terminal` | First run was started without a terminal (for example from a script) | Run it once from a terminal |
| Other devices can't open the link at all (times out) | A firewall on the Linux machine blocks the portal port | Check with `sudo ufw status`. If active, allow the port from the banner: `sudo ufw allow <port>/tcp`. Repeat after every Renew link (it changes the port). |
| Find cameras finds nothing | ONVIF is off, the cameras are on another subnet, or a firewall drops the replies | Turn ONVIF on in each camera. If `sudo ufw status` shows active, allow UDP from the local network, for example `sudo ufw allow proto udp from 192.168.12.0/24` (use your network). Or add cameras by IP. |
| "The camera rejected the username or password for ONVIF" | Wrong login, or the brand needs a separate ONVIF user | Re-check the login. For Hikvision, create an ONVIF user in the camera's settings. Or choose the brand preset instead. |
| "Automatic detection failed: No ONVIF service answered" | ONVIF is off, or the camera uses a non-standard ONVIF port | Turn ONVIF on, use **Find cameras** first (it learns the right port), or choose a brand preset / Other |
| Camera added with a preset stays **Offline** | The preset path doesn't match this model's firmware | Look up the RTSP path in the camera's manual, then `python edit_cameras.py set-path <id> '<path>'` with the server stopped |
| Browser shows "Not Found" | The link is missing the key, or the link was renewed | Use the newest link from the banner or `portal_url.txt` |
| Tile shows **Offline** | Camera unreachable or wrong credentials | Run `python edit_cameras.py check`. If reachable, re-check the username and password. |
| `ImportError` for `cv2` | Both `opencv-python` and `opencv-python-headless` are installed | `pip uninstall -y opencv-python opencv-python-headless`, then `pip install -r requirements.txt` |

---

## License

MIT License — see [LICENSE](LICENSE).

## Built by MainbyteLabs

Technical documentation and Python tooling for electronics labs and hardware teams.
https://github.com/MR-MainbyteLabs
