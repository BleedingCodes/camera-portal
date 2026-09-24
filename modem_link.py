"""modem_link.py — ties the portal to the modem's network.

What it does:
  1. Finds the network interface that leads to the modem, meaning the one
     holding the default route (or one you pin, e.g. wlan0).
  2. Waits until the modem has given this computer an IP address on it.
  3. Generates the portal's port and secret key once and saves them in the
     encrypted camera_config.json, so the link stays the same across restarts.
  4. renew_link() generates a new port and key. The portal's Renew link
     button calls this.
  5. Writes the full link to portal_url.txt (owner-only permissions).

Linux only. It reads /proc/net/route and uses a Linux socket call, so no
extra packages are needed.

Test it on its own:
    python modem_link.py                    # detect and show the link
    python modem_link.py --interface wlan0  # pin a specific interface
    python modem_link.py --auto             # remove the pin
    python modem_link.py --renew            # generate a new link
"""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import secrets
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import camera_core as core

PORT_RANGE = (20000, 60999)         # Random high ports, well away from common services.
URL_FILE = core.SCRIPT_DIR / "portal_url.txt"
ROUTE_TABLE = Path("/proc/net/route")
SIOCGIFADDR = 0x8915                # Linux ioctl code: "get this interface's IPv4 address".
RTF_UP, RTF_GATEWAY = 0x1, 0x2      # Route flags in /proc/net/route.


@dataclass(frozen=True)
class ModemLink:
    interface: str   # e.g. wlan0
    ip_address: str  # This computer's IP on the modem's network, e.g. 192.168.8.23
    gateway: str     # The modem's own IP, e.g. 192.168.8.1 ("" if unknown)
    port: int
    url_key: str

    @property
    def url(self) -> str:
        return f"http://{self.ip_address}:{self.port}/?key={self.url_key}"


# ── Network detection ────────────────────────────────────────────────────────

def default_route() -> tuple[str, str] | None:
    """Return (interface, gateway_ip) of the default route with the lowest metric, or None."""
    best = None
    for line in ROUTE_TABLE.read_text().splitlines()[1:]:  # Skip the header row.
        fields = line.split()
        if len(fields) < 8:
            continue
        iface, destination, gateway_hex, flags_hex, metric = fields[0], fields[1], fields[2], fields[3], int(fields[6])
        flags = int(flags_hex, 16)
        if destination != "00000000" or not (flags & RTF_UP and flags & RTF_GATEWAY):
            continue                                        # Not a working default route.
        gateway = socket.inet_ntoa(struct.pack("<L", int(gateway_hex, 16)))  # The file stores it as little-endian hex.
        if best is None or metric < best[2]:
            best = (iface, gateway, metric)
    return (best[0], best[1]) if best else None


def interface_ip(interface: str) -> str | None:
    """Return the IPv4 address on `interface`, or None if it has none yet."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            packed_name = struct.pack("256s", interface[:15].encode())
            result = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, packed_name)
        except OSError:
            return None                                     # Interface missing or no IP assigned.
    return socket.inet_ntoa(result[20:24])


def detect(interface: str | None = None) -> tuple[str, str, str] | None:
    """Return (interface, ip, gateway) if the modem link is up, otherwise None."""
    route = default_route()
    if interface is None:
        if route is None:
            return None
        interface, gateway = route
    else:
        gateway = route[1] if route and route[0] == interface else ""
    ip_address = interface_ip(interface)
    if ip_address is None:
        return None                              # No IP yet.
    parsed = ipaddress.ip_address(ip_address)
    if parsed.is_loopback or not parsed.is_private:
        return None  # Loopback isn't the modem, and we refuse to serve directly on a public IP.
    return interface, ip_address, gateway


def wait_for_modem(interface: str | None = None, poll_seconds: float = 3.0) -> tuple[str, str, str]:
    """Block until the modem gives this computer an IP. Prints once while waiting."""
    announced = False
    while True:
        found = detect(interface)
        if found:
            return found
        if not announced:
            target = interface or "the default network"
            print(f"Waiting for the modem to assign an IP on {target}... (Ctrl+C to quit)")
            announced = True
        time.sleep(poll_seconds)


# ── Port + secret key ────────────────────────────────────────────────────────

def port_is_free(ip_address: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((ip_address, port))
            return True
        except OSError:
            return False


def pick_free_port(ip_address: str) -> int:
    for _ in range(50):
        port = secrets.randbelow(PORT_RANGE[1] - PORT_RANGE[0]) + PORT_RANGE[0]
        if port_is_free(ip_address, port):
            return port
    raise RuntimeError("Could not find a free port after 50 tries.")


def _new_link_settings(ip_address: str) -> dict:
    return {"port": pick_free_port(ip_address), "url_key": secrets.token_urlsafe(24)}  # 24 bytes = 192 random bits.


def get_link(config: dict, key: bytes, interface: str | None = None, auto: bool = False,
             check_port: bool = True) -> ModemLink:
    """Wait for the modem, then return the saved link (creating it the first time).

    interface: pin this interface and remember it.  auto=True: forget any pin.
    check_port=False skips the "port already in use" check. Used by --renew,
    which replaces the port anyway.
    """
    portal = config.setdefault("portal", {})
    pin_changed = False
    if interface:
        portal["interface"] = interface          # Remember a pinned interface for next time.
        pin_changed = True
    elif auto and "interface" in portal:
        del portal["interface"]                  # Back to following the default route.
        pin_changed = True
    iface, ip_address, gateway = wait_for_modem(portal.get("interface"))

    if "port" not in portal or "url_key" not in portal:
        portal.update(_new_link_settings(ip_address))
        pin_changed = True
    if pin_changed:
        core.save_config(config, key)
    if check_port and not port_is_free(ip_address, portal["port"]):
        raise RuntimeError(f"Port {portal['port']} is in use. Is the portal already running? "
                           "If not, run: python modem_link.py --renew")

    link = ModemLink(iface, ip_address, gateway, portal["port"], portal["url_key"])
    write_url_file(link)
    return link


def renew_link(config: dict, key: bytes, current: ModemLink) -> ModemLink:
    """Generate a new port and key, save them, and return the new link. The old link stops working."""
    portal = config.setdefault("portal", {})
    portal.update(_new_link_settings(current.ip_address))
    core.save_config(config, key)
    link = ModemLink(current.interface, current.ip_address, current.gateway, portal["port"], portal["url_key"])
    write_url_file(link)
    return link


def write_url_file(link: ModemLink) -> None:
    URL_FILE.write_text(link.url + "\n")
    URL_FILE.chmod(0o600)                        # Only your user account can read the link.


# ── Standalone test ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Detect the modem link and show the portal URL.")
    parser.add_argument("--interface", help="pin a network interface, e.g. wlan0")
    parser.add_argument("--auto", action="store_true", help="forget a pinned interface and follow the default route")
    parser.add_argument("--renew", action="store_true", help="generate a new port and secret key")
    args = parser.parse_args()

    key = core.load_or_create_key()
    try:
        config = core.load_config(key)
    except core.ConfigError as exc:
        sys.exit(f"Config error: {exc}")

    try:
        link = get_link(config, key, args.interface, args.auto, check_port=not args.renew)
        if args.renew:
            old_url = link.url
            link = renew_link(config, key, link)
            print(f"Old link (now dead): {old_url}")
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    except RuntimeError as exc:
        sys.exit(f"Error: {exc}")

    print(f"Interface : {link.interface}")
    print(f"Modem (gw): {link.gateway or 'unknown'}")
    print(f"This PC IP: {link.ip_address}")
    print(f"Portal URL: {link.url}")
    print(f"Saved to  : {URL_FILE}")


if __name__ == "__main__":
    main()
