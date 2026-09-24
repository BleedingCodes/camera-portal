"""edit_cameras.py — view and change camera IPs in the encrypted config.

    python edit_cameras.py list                    # show cameras (no passwords)
    python edit_cameras.py set-ip 1 192.168.12.201 # change camera "1" to a new IP
    python edit_cameras.py check                   # test whether each camera answers on port 554

Stop run_server.py before using set-ip. Otherwise the running server may
save its own copy of the config over your change.
"""
import argparse
import ipaddress
import socket
import sys

import camera_core as core

RTSP_PORT = 554
CHECK_TIMEOUT_SECONDS = 2


def list_cameras(config: dict) -> None:
    print(f"{'ID':<12}{'Name':<20}{'IP address':<18}")
    for cam in config["cameras"]:
        print(f"{cam.get('file_prefix', '?'):<12}{cam.get('name', '?'):<20}{cam.get('ip_address', '(url)'):<18}")


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


def check_cameras(config: dict) -> None:
    """Open a TCP connection to each camera's RTSP port. This checks the network path, not the password."""
    for cam in config["cameras"]:
        ip_address = cam.get("ip_address")
        if not ip_address:
            continue
        try:
            with socket.create_connection((ip_address, RTSP_PORT), timeout=CHECK_TIMEOUT_SECONDS):
                result = "OK - reachable"
        except OSError as exc:
            result = f"NOT reachable ({exc.__class__.__name__})"
        print(f"{cam.get('name', '?'):<20}{ip_address:<18}{result}")


def main() -> None:
    parser = argparse.ArgumentParser(description="View/change camera IPs in the encrypted config.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("check")
    set_parser = sub.add_parser("set-ip")
    set_parser.add_argument("camera_id")
    set_parser.add_argument("new_ip")
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
    else:
        set_ip(config, key, args.camera_id, args.new_ip)


if __name__ == "__main__":
    main()
