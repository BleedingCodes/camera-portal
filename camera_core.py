"""camera_core.py — the camera engine, with no GUI.

Ported from pyqt-camera-dashboard v3.0.1. Capture, recording, reconnect,
disk cleanup and the encrypted config work the same way, but:
  * PyQt5 is gone. Workers are plain threading.Thread objects.
  * Workers store their latest frame and status as attributes. The web
    portal (web_portal.py) reads these directly.
  * CameraManager replaces MainWindow. It owns every worker.

An existing camera_config.json and secret.key from pyqt-camera-dashboard
v3 keep working. Copy both into this folder.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import os
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"  # RTSP over TCP, 5 s timeout. Must be set before cv2 is imported.

import cv2  # noqa: E402  (import must come after the env var above)
from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

__version__ = "1.1.0"

# ── Constants ────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "camera_config.json"
KEY_FILE = SCRIPT_DIR / "secret.key"
RECORDINGS_ROOT = SCRIPT_DIR / "recordings"
RECORD_DURATION_SECONDS = 15 * 60   # Each recording file is 15 minutes.
DEFAULT_FPS = 15.0                  # Fallback when the camera reports a bad FPS.
RECONNECT_DELAY_SECONDS = 3         # Wait between reconnect attempts.
MAX_CONNECT_FAILURES = 3            # After this many failures the camera goes Offline.
MAX_DISK_USAGE = 75                 # Delete oldest recordings above this disk-usage %.
CLEANUP_INTERVAL_SECONDS = 60 * 60  # Run disk cleanup every hour.
RTSP_PATH = "/cam/realmonitor?channel=1&subtype=0"  # Default path (Dahua/Amcrest), used when a camera has none saved.
RTSP_PORT = 554                                     # Default RTSP port, used when a camera has none saved.
MAX_RTSP_PATH_LENGTH = 200


class ConfigError(Exception):
    """Raised when the config can't be read or has the wrong shape."""


# ── Camera config ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CameraConfig:
    name: str         # Display name.
    url: str          # Full RTSP URL (contains credentials, never show it in the portal).
    file_prefix: str  # Unique ID. Also used for recording filenames.


# ── Encryption helpers ────────────────────────────────────────────────────────

def load_or_create_key() -> bytes:
    """Load secret.key, or create it (owner-only permissions) on first run."""
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    KEY_FILE.chmod(0o600)  # Owner read/write only.
    return key


def load_config(key: bytes) -> dict:
    """Decrypt and return the whole config dict. Returns {"cameras": []} if no file exists yet."""
    if not CONFIG_FILE.exists():
        return {"cameras": []}
    try:
        data = json.loads(Fernet(key).decrypt(CONFIG_FILE.read_bytes()).decode("utf-8"))
    except InvalidToken as exc:
        raise ConfigError(f"Could not decrypt {CONFIG_FILE.name}: wrong secret.key or corrupted file.") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{CONFIG_FILE.name} decrypted but is not valid JSON.") from exc
    if not isinstance(data.get("cameras"), list):
        raise ConfigError(f"{CONFIG_FILE.name} must contain a 'cameras' list.")
    return data


def save_config(data: dict, key: bytes) -> None:
    """Encrypt in memory, then write once. Plaintext never touches the disk
    (v3 briefly wrote plaintext before encrypting)."""
    encrypted = Fernet(key).encrypt(json.dumps(data, indent=4).encode("utf-8"))
    temp_file = CONFIG_FILE.with_suffix(".tmp")
    temp_file.write_bytes(encrypted)
    temp_file.chmod(0o600)
    temp_file.replace(CONFIG_FILE)  # Atomic swap, so a crash mid-save can't corrupt the config.


# ── Filename / URL helpers ────────────────────────────────────────────────────

def make_file_prefix(name: str) -> str:
    prefix = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip().lower()).strip("_")
    return prefix or "camera"


def unique_file_prefix(name: str, existing_prefixes: list[str]) -> str:
    base = make_file_prefix(name)
    prefix, counter = base, 2
    while prefix in existing_prefixes:
        prefix = f"{base}_{counter}"
        counter += 1
    return prefix


def validate_rtsp_path(path: str) -> str:
    """Return the cleaned path, or raise ValueError with a message the portal can show."""
    path = path.strip()
    if not path.startswith("/"):
        raise ValueError("The stream path must start with / (example: /stream1).")
    if len(path) > MAX_RTSP_PATH_LENGTH:
        raise ValueError(f"The stream path must be {MAX_RTSP_PATH_LENGTH} characters or fewer.")
    if any(ch.isspace() or not ch.isprintable() for ch in path) or "#" in path:
        raise ValueError("The stream path can't contain spaces, # or control characters.")
    return path


def validate_rtsp_port(port) -> int:
    try:
        number = int(str(port).strip())
    except ValueError:
        raise ValueError("The RTSP port must be a number (usually 554).") from None
    if not 1 <= number <= 65535:
        raise ValueError("The RTSP port must be between 1 and 65535.")
    return number


def build_rtsp_url(ip_address: str, username: str, password: str,
                   port: int = RTSP_PORT, path: str = RTSP_PATH) -> str:
    safe_username = quote(username, safe="")
    safe_password = quote(password, safe="")
    return f"rtsp://{safe_username}:{safe_password}@{ip_address}:{port}{path}"


def camera_details_to_config(details: dict) -> CameraConfig:
    name = str(details.get("name", "Camera")).strip() or "Camera"
    url = details.get("url") or build_rtsp_url(
        str(details.get("ip_address", "")).strip(),
        str(details.get("username", "")).strip(),
        str(details.get("password", "")),
        int(details.get("rtsp_port", RTSP_PORT)),      # Configs from v1.0.0 / pyqt v3 have no port or path:
        str(details.get("rtsp_path", RTSP_PATH)),      # they keep the Dahua/Amcrest defaults.
    )
    file_prefix = str(details.get("file_prefix", "")).strip() or make_file_prefix(name)
    return CameraConfig(name=name, url=url, file_prefix=file_prefix)


# ── Recording helpers ─────────────────────────────────────────────────────────

def hour_folder() -> Path:
    now = datetime.now()
    folder = RECORDINGS_ROOT / now.strftime("%Y-%m-%d") / now.strftime("%H")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def disk_used_percent() -> float:
    total, used, _ = shutil.disk_usage(RECORDINGS_ROOT)
    return used / total * 100


def cleanup_by_disk_usage() -> None:
    """Delete oldest recordings until disk usage drops below MAX_DISK_USAGE."""
    RECORDINGS_ROOT.mkdir(parents=True, exist_ok=True)
    if disk_used_percent() < MAX_DISK_USAGE:
        return
    print(f"Disk usage high: {disk_used_percent():.1f}% - deleting oldest recordings...")
    for file in sorted(RECORDINGS_ROOT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime):
        try:
            file.unlink()
            print(f"Deleted oldest recording: {file}")
        except OSError as exc:
            print(f"Could not delete {file}: {exc}")
            continue
        if disk_used_percent() < MAX_DISK_USAGE:
            print("Disk usage back to safe level.")
            break


# ── Camera worker ─────────────────────────────────────────────────────────────

class CameraWorker(threading.Thread):
    """One background thread per camera.

    The web portal only reads `status`, `online`, `recording` and
    get_latest_frame(). It changes things only by calling the public
    methods. The VideoWriter is touched ONLY inside this thread, which
    removes a race in v3 where the GUI thread could release the writer
    while the worker thread was still writing to it.
    """

    def __init__(self, camera: CameraConfig):
        super().__init__(daemon=True, name=f"cam-{camera.file_prefix}")
        self.camera = camera
        self.status = "Starting..."
        self.online = False
        self.recording = False           # What the user asked for.
        self.connect_failures = 0

        self._stop_event = threading.Event()
        self._reconnect_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame = None
        self._frame_number = 0           # Increases with each new frame, so viewers can skip duplicates.

        self._cap = None
        self._writer = None
        self._writer_size = None
        self._record_start = None
        self._fps = DEFAULT_FPS

    # ── Public API (safe to call from any thread) ──

    def get_latest_frame(self):
        """Return (frame_number, frame) for the newest frame. frame is None before the first frame arrives."""
        with self._frame_lock:
            return self._frame_number, self._latest_frame

    def start_recording(self) -> bool:
        if not self.online:
            return False                 # Same rule as v3: no recording while offline.
        self.recording = True
        return True

    def stop_recording(self) -> None:
        self.recording = False           # The worker thread closes the file on its next frame.

    def request_reconnect(self) -> None:
        self.connect_failures = 0
        self._reconnect_event.set()      # Wakes the thread whether it's live or parked Offline.

    def stop(self) -> None:
        self.recording = False
        self._stop_event.set()
        self._reconnect_event.set()      # Unparks an Offline worker so it can exit.

    # ── Thread body ──

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self.connect_failures >= MAX_CONNECT_FAILURES:
                self._go_offline_and_wait()
                continue

            self.status = f"Connecting... attempt {self.connect_failures + 1}/{MAX_CONNECT_FAILURES}"
            self._cap = cv2.VideoCapture(self.camera.url, cv2.CAP_FFMPEG)
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)

            if not self._cap.isOpened():
                self.connect_failures += 1
                self._release_capture()
                if self.connect_failures < MAX_CONNECT_FAILURES:
                    self.status = "Connection failed. Retrying..."
                    self._stop_event.wait(RECONNECT_DELAY_SECONDS)
                continue

            manual_reconnect = self._stream_loop()
            self._release_capture()
            self.online = False
            if not self._stop_event.is_set() and not manual_reconnect:
                self._stop_event.wait(RECONNECT_DELAY_SECONDS)

        self._close_writer()
        self._release_capture()
        self.online = False
        self.status = "Stopped"

    def _stream_loop(self) -> bool:
        """Read frames until the stream drops. Returns True if a manual reconnect was requested."""
        self.connect_failures = 0
        self.online = True
        self.status = "Live"
        fps = self._cap.get(cv2.CAP_PROP_FPS)
        self._fps = fps if fps and 1 < fps <= 60 else DEFAULT_FPS

        while not self._stop_event.is_set():
            if self._reconnect_event.is_set():
                self._reconnect_event.clear()
                self.status = "Reconnecting..."
                return True

            ok, frame = self._cap.read()
            if not ok or frame is None:
                self.status = "Frame lost. Reconnecting..."
                return False

            with self._frame_lock:
                self._latest_frame = frame
                self._frame_number += 1

            if self.recording:
                self._write_frame(frame)
            elif self._writer is not None:
                self._close_writer()     # User pressed Stop. Close the file here, in this thread.
        return False

    def _go_offline_and_wait(self) -> None:
        self.online = False
        self.recording = False
        self._close_writer()
        self.status = "Offline"
        self._reconnect_event.wait()     # Sleep until Reconnect is pressed or stop() is called.
        self._reconnect_event.clear()
        self.connect_failures = 0

    # ── Recording internals (worker thread only) ──

    def _write_frame(self, frame) -> None:
        if self._writer is None:
            self._open_new_writer(frame)
            if self._writer is None:
                return
        width, height = self._writer_size
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        self._writer.write(frame)
        if (datetime.now() - self._record_start).total_seconds() >= RECORD_DURATION_SECONDS:
            self._close_writer()         # Next frame opens a fresh 15-minute file.

    def _open_new_writer(self, frame) -> None:
        self._record_start = datetime.now()
        filename = f"{self.camera.file_prefix}_{self._record_start:%Y-%m-%d_%H-%M-%S}.mp4"
        path = hour_folder() / filename
        height, width = frame.shape[:2]
        self._writer_size = (width, height)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), self._fps, self._writer_size)
        if not writer.isOpened():
            self.status = "Recording failed: VideoWriter did not open"
            self.recording = False
            return
        self._writer = writer
        self.status = f"Recording: {path.name}"
        print(f"[{self.camera.name}] Recording started: {path}")

    def _close_writer(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            self._writer_size = None
            if self.online:
                self.status = "Live"

    def _release_capture(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# ── Camera manager (replaces MainWindow) ─────────────────────────────────────

class CameraManager:
    """Owns the config and every CameraWorker. The web portal talks only to this class."""

    def __init__(self, key: bytes):
        self.key = key
        self.config = load_config(key)
        self.workers: dict[str, CameraWorker] = {}   # camera_id (file_prefix) -> worker
        self._lock = threading.Lock()          # Protects self.workers.
        self.config_lock = threading.Lock()    # Protects self.config changes + saves. Other modules use it too.
        self._cleanup_stop = threading.Event()

    def start(self) -> None:
        cleanup_by_disk_usage()
        for details in self.config["cameras"]:
            self._start_worker(details)
        threading.Thread(target=self._cleanup_loop, daemon=True, name="disk-cleanup").start()

    def _start_worker(self, details: dict) -> None:
        """Start a worker for this camera. Does nothing if one is already running,
        so a camera is never opened twice (two RTSP connections, duplicate recordings)."""
        camera = camera_details_to_config(details)
        with self._lock:
            existing = self.workers.get(camera.file_prefix)
            if existing is not None and existing.is_alive():
                return
            worker = CameraWorker(camera)
            self.workers[camera.file_prefix] = worker
        worker.start()

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(CLEANUP_INTERVAL_SECONDS):
            cleanup_by_disk_usage()

    # ── Camera management ──

    def add_camera(self, ip_address: str, name: str, username: str, password: str,
                   rtsp_port: int | str = RTSP_PORT, rtsp_path: str = RTSP_PATH) -> str:
        """Validate, save to the encrypted config, start streaming. Returns the camera_id.

        Adding a camera that's already in the config (same IP, port and path):
          * still showing in the portal -> ValueError
          * removed this session        -> it's restarted and shown again (not saved twice)
        """
        ip_address, name, username = ip_address.strip(), name.strip(), username.strip()
        if not ip_address or not name or not username:
            raise ValueError("IP address, camera name, and username are required.")
        try:
            ipaddress.IPv4Address(ip_address)
        except ipaddress.AddressValueError:
            raise ValueError(f"'{ip_address}' is not a valid IP address (example: 192.168.12.108).") from None
        if len(name) > 40:
            raise ValueError("Camera name must be 40 characters or fewer.")
        rtsp_port = validate_rtsp_port(rtsp_port)
        rtsp_path = validate_rtsp_path(rtsp_path)

        with self.config_lock:  # Two browsers adding at once can't corrupt the list or the file.
            for existing in self.config["cameras"]:
                same_stream = (str(existing.get("ip_address", "")).strip() == ip_address
                               and int(existing.get("rtsp_port", RTSP_PORT)) == rtsp_port
                               and str(existing.get("rtsp_path", RTSP_PATH)) == rtsp_path)
                if not same_stream:
                    continue
                camera_id = camera_details_to_config(existing).file_prefix
                worker = self.get_worker(camera_id)
                if worker is not None and worker.is_alive():
                    raise ValueError(f"This camera is already added as '{existing.get('name', camera_id)}'.")
                self._start_worker(existing)        # Removed earlier this session: bring it back.
                return camera_id

            existing_prefixes = [str(d.get("file_prefix", "")) for d in self.config["cameras"]]
            details = {
                "ip_address": ip_address,
                "name": name,
                "username": username,
                "password": password,
                "rtsp_port": rtsp_port,
                "rtsp_path": rtsp_path,
                "file_prefix": unique_file_prefix(name, existing_prefixes),
            }
            self.config["cameras"].append(details)
            save_config(self.config, self.key)
        self._start_worker(details)
        return details["file_prefix"]

    def configured_ips(self) -> set[str]:
        """IP addresses of every camera in the config (including ones removed this session)."""
        with self.config_lock:
            return {str(d.get("ip_address", "")).strip() for d in self.config["cameras"]}

    def remove_camera(self, camera_id: str) -> bool:
        """Stop and hide a camera for this session only. It stays in the config (same as v3)."""
        with self._lock:
            worker = self.workers.pop(camera_id, None)
        if worker is None:
            return False
        worker.stop()  # Don't join. The thread exits on its own, so the caller (a web request) isn't blocked.
        return True

    def get_worker(self, camera_id: str) -> CameraWorker | None:
        with self._lock:
            return self.workers.get(camera_id)

    def status_list(self) -> list[dict]:
        """Safe summary for the portal. It never includes URLs or passwords."""
        with self._lock:
            workers = list(self.workers.items())
        return [
            {
                "id": camera_id,
                "name": w.camera.name,
                "status": w.status,
                "online": w.online,
                "recording": w.recording,
            }
            for camera_id, w in workers
        ]

    def start_all_recording(self) -> None:
        with self._lock:
            workers = list(self.workers.values())
        for worker in workers:
            worker.start_recording()

    def stop_all_recording(self) -> None:
        with self._lock:
            workers = list(self.workers.values())
        for worker in workers:
            worker.stop_recording()

    def shutdown(self) -> None:
        self._cleanup_stop.set()
        with self._lock:
            workers = list(self.workers.values())
        for worker in workers:
            worker.stop()
        deadline = time.monotonic() + 6  # One shared 6 s budget. A blocked RTSP read can take up to its 5 s timeout.
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
