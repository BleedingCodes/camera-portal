"""web_portal.py — the HTML portal.

Two locks on the door:
  1. The secret key in the link (?key=...). Without it, every route returns 404.
  2. The portal password. It unlocks a session cookie that lasts SESSION_DAYS.

Pages and files (key in the URL):
  /                       dashboard, or the login page if not logged in
  /login     (POST)       check the password, start a session
  /logout    (POST)
  /assets/<file>          style.css (also on the login page) and app.js (logged in only)
  /snapshot/<camera_id>   one JPEG still. Tiles refresh this about once a second.
  /stream/<camera_id>     live MJPEG video. Opened only in the live viewer, one at a time.

Why tiles use stills: browsers allow about 6 open connections per server over
plain HTTP. Each live MJPEG stream holds one open for as long as it plays, so
6+ live tiles would block every button and status update. A still is one
short request, so any number of tiles works.

JSON API used by app.js (key in the X-Portal-Key header, plus the session cookie):
  GET  /api/cameras                     status of every camera
  GET  /api/discover                    find ONVIF cameras on the modem network (takes ~3 s)
  POST /api/cameras                     add a camera  {ip_address, name, username, password,
                                          and either detect: true (ask the camera via ONVIF)
                                          or rtsp_path + rtsp_port}
  POST /api/cameras/<id>/record         {"on": true|false}
  POST /api/cameras/<id>/reconnect
  POST /api/cameras/<id>/remove         hide for this session (returns after restart, same as v3)
  POST /api/record-all                  {"on": true|false}
  POST /api/renew-link                  new port + key. The old link dies immediately.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path

import cv2
from flask import (Flask, Response, abort, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash

import onvif
from camera_core import RTSP_PORT, CameraManager

PROJECT_DIR = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_DIR / "static"
PUBLIC_ASSETS = {"style.css"}                 # Needed by the login page (still requires the key).
PRIVATE_ASSETS = {"app.js"}                   # Logged-in users only.
REQUIRED_FILES = [PROJECT_DIR / "templates" / "dashboard.html", PROJECT_DIR / "templates" / "login.html"] + \
                 [STATIC_DIR / name for name in PUBLIC_ASSETS | PRIVATE_ASSETS]

STREAM_FPS = 10          # Live viewer frames per second. Lower = less Wi-Fi load.
JPEG_QUALITY = 70        # 0-100. 70 looks good at roughly 1/3 the size of 95.
STREAM_MAX_WIDTH = 960   # Live viewer: downscale 1280x720 to 960x540. Recordings stay full size.
SNAPSHOT_MAX_WIDTH = 640 # Tile stills are smaller than the live viewer. Tiles are rarely wider than this.
SESSION_DAYS = 30        # How long a login lasts on one device.
MAX_LOGIN_FAILURES = 5   # Wrong passwords allowed per device (IP) before a lockout...
LOCKOUT_SECONDS = 60     # ...of this long.


def fingerprint(value: str) -> str:
    """Short one-way ID of a secret. The session stores this, never the secret itself."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def create_app(manager: CameraManager, renew_link, local_ip) -> Flask:
    """Build the Flask app.

    Settings are read from manager.config["portal"] on every request, so a
    renewed key or changed password takes effect immediately.
    renew_link() is supplied by run_server.py. It creates the new link,
    schedules the move to the new port, and returns the new ModemLink.
    local_ip() returns this computer's IP on the modem network (discovery is sent from it).
    """
    missing = [str(path.relative_to(PROJECT_DIR)) for path in REQUIRED_FILES if not path.exists()]
    if missing:  # Fail at startup with a clear message instead of a 500 error in the browser later.
        raise RuntimeError(f"Missing file(s): {', '.join(missing)}. Check the templates/ and static/ folders.")

    portal = lambda: manager.config["portal"]  # noqa: E731  (always read the live settings)

    app = Flask(__name__, static_folder=None)  # No public /static route. Every file needs the key.
    app.secret_key = portal()["session_secret"]   # Signs the session cookie so it can't be forged.
    app.config.update(
        SESSION_COOKIE_NAME="cam_portal_session",
        SESSION_COOKIE_HTTPONLY=True,          # JavaScript can't read the cookie.
        SESSION_COOKIE_SAMESITE="Strict",      # Other websites can't send it.
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # Request logs would print the secret key.

    login_failures: dict[str, list[float]] = {}   # client IP -> times of recent wrong passwords
    failures_lock = threading.Lock()
    discovered: dict[str, onvif.FoundCamera] = {} # camera IP -> last discovery result
    discovery_lock = threading.Lock()             # One network scan at a time.

    # ── Checks ──

    def key_is_valid(supplied: str) -> bool:
        return secrets.compare_digest(supplied, portal()["url_key"])   # Timing-safe comparison.

    def require_key() -> str:
        """Pages and video: the key comes from the URL."""
        supplied = request.args.get("key", "")
        if not key_is_valid(supplied):
            abort(404)
        return supplied

    def logged_in() -> bool:
        # The session is tied to BOTH the current key and the current password.
        # Renewing the link or changing the password logs everyone else out.
        return (session.get("key_fp") == fingerprint(portal()["url_key"])
                and session.get("pw_fp") == fingerprint(portal()["password_hash"]))

    def require_api() -> None:
        # API calls must use the X-Portal-Key header. A malicious web page can't add custom
        # headers to a request to this server without the browser blocking it, so this also
        # stops cross-site request forgery (another site making your browser press buttons here).
        if not key_is_valid(request.headers.get("X-Portal-Key", "")):
            abort(404)
        if not logged_in():
            abort(401)

    def start_session() -> None:
        session.clear()
        session.permanent = True
        session["key_fp"] = fingerprint(portal()["url_key"])
        session["pw_fp"] = fingerprint(portal()["password_hash"])

    def locked_out_seconds(client: str) -> int:
        now = time.monotonic()
        with failures_lock:
            recent = [t for t in login_failures.get(client, []) if now - t < LOCKOUT_SECONDS]
            login_failures[client] = recent
            if len(recent) >= MAX_LOGIN_FAILURES:
                return int(LOCKOUT_SECONDS - (now - recent[0])) + 1
        return 0

    def record_failure(client: str) -> None:
        with failures_lock:
            login_failures.setdefault(client, []).append(time.monotonic())

    def get_worker_or_404(camera_id: str):
        worker = manager.get_worker(camera_id)
        if worker is None:
            abort(404)
        return worker

    def json_body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def error(message: str, status: int = 400):
        return jsonify({"ok": False, "error": message}), status

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"          # Don't cache pages that contain the key.
        response.headers["Referrer-Policy"] = "no-referrer"     # Never leak the key to other sites.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(401)
    def unauthorized(_):
        return jsonify({"ok": False, "error": "Logged out. Reload the page and sign in."}), 401

    # ── Pages ──

    @app.route("/")
    def dashboard():
        url_key = require_key()
        if not logged_in():
            return render_template("login.html", key=url_key, error=None)
        return render_template("dashboard.html", cameras=manager.status_list(), key=url_key)

    @app.post("/login")
    def login():
        url_key = require_key()
        client = request.remote_addr or "?"
        wait = locked_out_seconds(client)
        if wait:
            return render_template("login.html", key=url_key,
                                   error=f"Too many wrong passwords. Try again in {wait} seconds."), 429
        if not check_password_hash(portal()["password_hash"], request.form.get("password", "")):
            record_failure(client)
            return render_template("login.html", key=url_key, error="Wrong password."), 401
        with failures_lock:
            login_failures.pop(client, None)
        start_session()
        return redirect(url_for("dashboard", key=url_key))

    @app.post("/logout")
    def logout():
        url_key = require_key()
        session.clear()
        return redirect(url_for("dashboard", key=url_key))

    @app.route("/assets/<name>")
    def asset(name: str):
        require_key()
        if name in PRIVATE_ASSETS and not logged_in():
            abort(404)
        if name not in PUBLIC_ASSETS | PRIVATE_ASSETS:
            abort(404)
        return send_from_directory(STATIC_DIR, name)

    @app.route("/snapshot/<camera_id>")
    def snapshot(camera_id: str):
        require_key()
        if not logged_in():
            abort(404)
        worker = get_worker_or_404(camera_id)
        _, frame = worker.get_latest_frame()
        jpeg = encode_jpeg(frame, SNAPSHOT_MAX_WIDTH) if frame is not None else None
        if jpeg is None:
            return Response(status=204)    # No picture yet. The tile keeps its overlay and tries again.
        return Response(jpeg, mimetype="image/jpeg")

    @app.route("/stream/<camera_id>")
    def stream(camera_id: str):
        url_key = require_key()
        if not logged_in():
            abort(404)
        worker = get_worker_or_404(camera_id)
        still_valid = lambda: key_is_valid(url_key)  # noqa: E731  (a renewed key ends open streams)
        return Response(mjpeg_frames(worker, still_valid), mimetype="multipart/x-mixed-replace; boundary=frame")

    # ── JSON API ──

    @app.get("/api/cameras")
    def api_list():
        require_api()
        return jsonify({"ok": True, "cameras": manager.status_list()})

    @app.get("/api/discover")
    def api_discover():
        require_api()
        with discovery_lock:
            try:
                found = onvif.discover(local_ip())
            except OSError as exc:
                return error(f"Could not search the network: {exc}", 500)
            discovered.clear()
            discovered.update({cam.ip_address: cam for cam in found})
        added = manager.configured_ips()
        return jsonify({"ok": True, "cameras": [
            {"ip_address": cam.ip_address, "name": cam.name, "hardware": cam.hardware,
             "added": cam.ip_address in added}
            for cam in found
        ]})

    @app.post("/api/cameras")
    def api_add():
        require_api()
        data = json_body()
        ip_address = str(data.get("ip_address", "")).strip()
        username = str(data.get("username", ""))
        password = str(data.get("password", ""))
        rtsp_port, rtsp_path = data.get("rtsp_port", RTSP_PORT), str(data.get("rtsp_path", ""))

        if data.get("detect"):
            try:
                is_local = ipaddress.IPv4Address(ip_address).is_private
            except ipaddress.AddressValueError:
                return error(f"'{ip_address}' is not a valid IP address (example: 192.168.12.108).")
            if not is_local:
                return error("Automatic detection only works for cameras on the local network.")
            if not username.strip():
                return error("IP address, camera name, and username are required.")
            found = discovered.get(ip_address)
            device_url = found.device_url if found else f"http://{ip_address}{onvif.DEFAULT_DEVICE_PATH}"
            try:
                rtsp_port, rtsp_path = onvif.get_stream_location(ip_address, device_url, username.strip(), password)
            except onvif.OnvifAuthError as exc:
                return error(f"{exc} Check the username and password. Some brands (for example Hikvision) "
                             "use a separate ONVIF user, set up in the camera's own settings. "
                             "Or choose your camera brand under Stream path instead.")
            except onvif.OnvifError as exc:
                return error(f"Automatic detection failed: {exc} "
                             "Choose your camera brand under Stream path, or pick Other and enter the path.")

        try:
            camera_id = manager.add_camera(ip_address, str(data.get("name", "")), username, password,
                                           rtsp_port, rtsp_path)
        except ValueError as exc:
            return error(str(exc))
        return jsonify({"ok": True, "id": camera_id, "rtsp_port": int(rtsp_port), "rtsp_path": rtsp_path})

    @app.post("/api/cameras/<camera_id>/record")
    def api_record(camera_id: str):
        require_api()
        worker = get_worker_or_404(camera_id)
        if json_body().get("on"):
            if not worker.start_recording():
                return error("Camera is not live, so it can't record.", 409)
        else:
            worker.stop_recording()
        return jsonify({"ok": True})

    @app.post("/api/cameras/<camera_id>/reconnect")
    def api_reconnect(camera_id: str):
        require_api()
        get_worker_or_404(camera_id).request_reconnect()
        return jsonify({"ok": True})

    @app.post("/api/cameras/<camera_id>/remove")
    def api_remove(camera_id: str):
        require_api()
        if not manager.remove_camera(camera_id):
            abort(404)
        return jsonify({"ok": True})

    @app.post("/api/record-all")
    def api_record_all():
        require_api()
        if json_body().get("on"):
            manager.start_all_recording()   # Same as v3: only cameras that are Live start recording.
        else:
            manager.stop_all_recording()
        return jsonify({"ok": True})

    @app.post("/api/renew-link")
    def api_renew_link():
        require_api()
        new_link = renew_link()
        start_session()   # Keep THIS device logged in under the new key. Cookies are shared across ports.
        return jsonify({"ok": True, "url": new_link.url})

    return app


def encode_jpeg(frame, max_width: int) -> bytes | None:
    """Downscale (if wider than max_width) and JPEG-encode one frame. Returns None if encoding fails."""
    height, width = frame.shape[:2]
    if width > max_width:
        frame = cv2.resize(frame, (max_width, int(height * max_width / width)), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return jpeg.tobytes() if ok else None


def mjpeg_frames(worker, still_valid):
    """Yield JPEG frames until the camera is removed or the link is renewed.
    Flask also stops this when the browser disconnects (the live viewer closes)."""
    last_number = -1
    frame_interval = 1 / STREAM_FPS
    while worker.is_alive() and still_valid():
        started = time.monotonic()
        number, frame = worker.get_latest_frame()
        if frame is not None and number != last_number:
            last_number = number
            jpeg = encode_jpeg(frame, STREAM_MAX_WIDTH)
            if jpeg is not None:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        time.sleep(max(0.0, frame_interval - (time.monotonic() - started)))
