"""edit_cameras.py — view and change cameras in the encrypted config.

    python edit_cameras.py list                                  # show cameras (no passwords)
    python edit_cameras.py set-ip front_door 192.168.12.201      # change a camera's IP
    python edit_cameras.py set-path front_door /stream1          # change a camera's stream path
    python edit_cameras.py set-path front_door /stream1 --port 8554
    python edit_cameras.py check                                 # test each camera's RTSP port

Stop run_server.py before using set-ip or set-path. Otherwise the running
server may save its own copy of the config over your change.
"""
import argparse
import ipaddress
import socket
import sys

import camera_core as core

CHECK_TIMEOUT_SECONDS = 2


def list_cameras(config: dict) -> None:
    print(f"{'ID':<16}{'Name':<22}{'IP address':<17}{'Port':<7}Stream path")
    for cam in config["cameras"]:
        print(f"{cam.get('file_prefix', '?'):<16}{cam.get('name', '?'):<22}{cam.get('ip_address', '(url)'):<17}"
              f"{cam.get('rtsp_port', core.RTSP_PORT):<7}{cam.get('rtsp_path', core.RTSP_PATH)}")


def find_camera(config: dict, camera_id: str) -> dict:
    for cam in config["cameras"]:
        if cam.get("file_prefix") == camera_id:
            return cam
    sys.exit(f"No camera with ID '{camera_id}'. Run: python edit_cameras.py list")


def set_ip(config: dict, key: bytes, camera_id: str, new_ip: str) -> None:
    try:
        parsed = ipaddress.IPv4Address(new_ip)
    except ipaddress.AddressValueError:
        sys.exit(f"'{new_ip}' is not a valid IPv4 address.")
    if not parsed.is_private:
        sys.exit(f"{new_ip} is not a private (local network) address.")

    cam = find_camera(config, camera_id)
    if "url" in cam:
        sys.exit("This camera was saved as a full RTSP URL, not an IP address. "
                 "set-ip only changes cameras saved by IP address.")
    old_ip = cam.get("ip_address")
    cam["ip_address"] = str(parsed)
    core.save_config(config, key)
    print(f"Camera '{camera_id}' ({cam.get('name')}): {old_ip} -> {parsed}. Saved.")


def set_path(config: dict, key: bytes, camera_id: str, new_path: str, new_port: str | None) -> None:
    try:
        path = core.validate_rtsp_path(new_path)
        port = core.validate_rtsp_port(new_port) if new_port is not None else None
    except ValueError as exc:
        sys.exit(str(exc))
    cam = find_camera(config, camera_id)
    if "url" in cam:
        sys.exit("This camera was saved as a full RTSP URL, not an IP address. "
                 "set-path only changes cameras saved by IP address.")
    cam["rtsp_path"] = path
    if port is not None:
        cam["rtsp_port"] = port
    core.save_config(config, key)
    print(f"Camera '{camera_id}' ({cam.get('name')}): port {cam.get('rtsp_port', core.RTSP_PORT)}, path {path}. Saved.")


def check_cameras(config: dict) -> None:
    """Open a TCP connection to each camera's RTSP port. This checks the network path, not the password."""
    for cam in config["cameras"]:
        ip_address = cam.get("ip_address")
        if not ip_address:
            continue
        port = int(cam.get("rtsp_port", core.RTSP_PORT))
        try:
            with socket.create_connection((ip_address, port), timeout=CHECK_TIMEOUT_SECONDS):
                result = "OK - reachable"
        except OSError as exc:
            result = f"NOT reachable ({exc.__class__.__name__})"
        print(f"{cam.get('name', '?'):<22}{ip_address + ':' + str(port):<23}{result}")


def main() -> None:
    parser = argparse.ArgumentParser(description="View/change camera IPs in the encrypted config.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("check")
    set_parser = sub.add_parser("set-ip")
    set_parser.add_argument("camera_id")
    set_parser.add_argument("new_ip")
    path_parser = sub.add_parser("set-path")
    path_parser.add_argument("camera_id")
    path_parser.add_argument("new_path", help="must start with /, e.g. /stream1 (quote it if it contains & or ?)")
    path_parser.add_argument("--port", help="RTSP port, if not the camera's current one")
    args = parser.parse_args()

    key = core.load_or_create_key()
    try:
        config = core.load_config(key)
    except core.ConfigError as exc:
        sys.exit(f"Config error: {exc}")

    if args.command == "list":
        list_cameras(config)
    elif args.command == "check":
        check_cameras(config)
    elif args.command == "set-path":
        set_path(config, key, args.camera_id, args.new_path, args.port)
    else:
        set_ip(config, key, args.camera_id, args.new_ip)


if __name__ == "__main__":
    main()
