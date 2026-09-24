"""run_server.py — start everything.

    python run_server.py                     # normal start
    python run_server.py --reset-password    # choose a new portal password (logs everyone out)
    python run_server.py --interface wlan0   # pin the modem's interface (remembered)
    python run_server.py --auto              # remove the pinned interface

Order: load config -> portal password -> start cameras -> wait for modem IP
-> serve the portal on that IP only. The Renew link button moves the server
to a new port without restarting the cameras.
"""
import argparse
import getpass
import secrets
import sys
import threading

from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

import camera_core as core
import modem_link
from web_portal import create_app

MIN_PASSWORD_LENGTH = 8
RENEW_SWITCH_DELAY_SECONDS = 1.0   # Give the browser time to receive the new link before the old port closes.


def ensure_password(manager: core.CameraManager, key: bytes, reset: bool) -> None:
    """Ask for the portal password in the terminal on first run (or with --reset-password)."""
    portal = manager.config.setdefault("portal", {})
    if "password_hash" in portal and not reset:
        return
    if not sys.stdin.isatty():
        sys.exit("No portal password set. Run 'python run_server.py' once in a terminal to choose one.")

    print("Choose the password people will type to open the portal.")
    while True:
        first = getpass.getpass(f"  New portal password (min {MIN_PASSWORD_LENGTH} characters, hidden): ")
        if len(first) < MIN_PASSWORD_LENGTH:
            print(f"  Too short. Use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if getpass.getpass("  Type it again: ") != first:
            print("  They didn't match. Try again.")
            continue
        break
    with manager.config_lock:
        portal["password_hash"] = generate_password_hash(first)   # scrypt hash. The password itself is never stored.
        core.save_config(manager.config, key)
    print("  Password saved.\n")


def ensure_session_secret(manager: core.CameraManager, key: bytes) -> None:
    portal = manager.config.setdefault("portal", {})
    if "session_secret" not in portal:
        with manager.config_lock:
            portal["session_secret"] = secrets.token_hex(32)
            core.save_config(manager.config, key)


def print_banner(link: modem_link.ModemLink) -> None:
    print("=" * 70)
    print(f" Modem network : {link.interface}  (this PC: {link.ip_address}, modem: {link.gateway or '?'})")
    print(f" Portal link   : {link.url}")
    print(f" Also saved in : {modem_link.URL_FILE.name}")
    print(" Open it on a phone/laptop connected to the modem's Wi-Fi. Ctrl+C to stop.")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Camera portal server")
    parser.add_argument("--interface", help="pin the modem's network interface, e.g. wlan0")
    parser.add_argument("--auto", action="store_true", help="forget a pinned interface")
    parser.add_argument("--reset-password", action="store_true", help="choose a new portal password")
    args = parser.parse_args()

    key = core.load_or_create_key()
    try:
        manager = core.CameraManager(key)
    except core.ConfigError as exc:
        sys.exit(f"Config error: {exc}")

    ensure_password(manager, key, args.reset_password)
    ensure_session_secret(manager, key)

    if not manager.config["cameras"]:
        print("No cameras configured yet. Open the portal and click '+ Add camera'.")

    manager.start()   # Cameras start connecting while we wait for the modem.

    try:
        # manager.config is the same dict the cameras use, so every save keeps cameras + portal together.
        link = modem_link.get_link(manager.config, key, args.interface, args.auto)
    except (KeyboardInterrupt, RuntimeError) as exc:
        manager.shutdown()
        sys.exit(f"\nStopped: {exc}" if str(exc) else "\nStopped.")

    current = {"link": link}             # A dict so renew_link() below can replace the link.
    switch_port = threading.Event()      # Set when the server must move to a new port.

    def renew_link() -> modem_link.ModemLink:
        """Called by the Renew link button (from a web request thread)."""
        with manager.config_lock:
            new_link = modem_link.renew_link(manager.config, key, current["link"])
            current["link"] = new_link
        threading.Timer(RENEW_SWITCH_DELAY_SECONDS, switch_port.set).start()
        return new_link

    try:
        app = create_app(manager, renew_link)
    except RuntimeError as exc:
        manager.shutdown()
        sys.exit(f"Setup error: {exc}")

    try:
        while True:
            link = current["link"]
            server = make_server(link.ip_address, link.port, app, threaded=True)  # Modem's network ONLY.
            threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
            print_banner(link)

            while not switch_port.wait(0.5):   # Short waits keep Ctrl+C responsive.
                pass
            switch_port.clear()
            server.shutdown()
            server.server_close()
            print("\nLink renewed. The old link no longer works.")
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
        server.server_close()
    finally:
        manager.shutdown()
        print("Stopped.")


if __name__ == "__main__":
    main()
