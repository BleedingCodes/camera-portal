"""test_core.py — hardware check. Runs the camera engine with no portal.

Usage:
    python test_core.py              # watch status for 30 seconds
    python test_core.py --record     # also record for 30 seconds

If camera_config.json has no cameras yet, it asks for one camera in the
terminal. Stop run_server.py first: both would open the same cameras.
"""
import argparse
import getpass
import sys
import time
from pathlib import Path

import cv2

import camera_core as core

TEST_SECONDS = 30


def ask_for_first_camera(manager: core.CameraManager) -> None:
    print("No cameras configured. Enter one camera:")
    ip_address = input("  Camera IP address: ")
    name = input("  Camera name: ")
    username = input("  Username: ")
    password = getpass.getpass("  Password (hidden): ")
    rtsp_path = input(f"  Stream path [Enter = {core.RTSP_PATH}]: ").strip() or core.RTSP_PATH
    rtsp_port = input(f"  RTSP port [Enter = {core.RTSP_PORT}]: ").strip() or core.RTSP_PORT
    try:
        manager.add_camera(ip_address, name, username, password, rtsp_port, rtsp_path)
    except ValueError as exc:
        sys.exit(f"Not added: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true", help="record while testing")
    args = parser.parse_args()

    key = core.load_or_create_key()
    try:
        manager = core.CameraManager(key)
    except core.ConfigError as exc:
        sys.exit(f"Config error: {exc}")

    if not manager.config["cameras"]:
        ask_for_first_camera(manager)   # add_camera also starts that camera's worker.
    manager.start()                     # Starts every camera not already running (add_camera started the first).

    saved_snapshots: set[str] = set()
    record_requested = False
    try:
        for second in range(0, TEST_SECONDS, 2):
            for camera in manager.status_list():
                worker = manager.get_worker(camera["id"])
                frame_number, frame = worker.get_latest_frame()
                size = f"{frame.shape[1]}x{frame.shape[0]}" if frame is not None else "no frame"
                print(f"[{second:2d}s] {camera['name']:<15} {camera['status']:<45} frames={frame_number} {size}")

                if frame is not None and camera["id"] not in saved_snapshots:
                    snapshot = Path(f"snapshot_{camera['id']}.jpg")
                    cv2.imwrite(str(snapshot), frame)
                    saved_snapshots.add(camera["id"])
                    print(f"      saved {snapshot} — open it to confirm the picture")

            if args.record and not record_requested and any(c["online"] for c in manager.status_list()):
                manager.start_all_recording()
                record_requested = True
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        print("Shutting down...")
        manager.shutdown()
        recordings = list(core.RECORDINGS_ROOT.rglob("*.mp4"))
        print(f"Done. {len(recordings)} recording file(s) in {core.RECORDINGS_ROOT}")


if __name__ == "__main__":
    main()
