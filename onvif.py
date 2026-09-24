"""onvif.py — find cameras on the local network and ask them for their stream path.

Standard library only. Two jobs:

  1. discover(local_ip)
     WS-Discovery. Sends a multicast "any ONVIF cameras here?" probe on the
     modem network and collects the answers (IP, name, model).

  2. get_stream_location(ip_address, device_url, username, password)
     Logs in to the camera's ONVIF service, asks for its RTSP stream address
     (highest-resolution profile), and returns only the port and path.

Safety rules:
  * Every address a camera reports is rebuilt with the camera's own IP. A
    device can't point the portal (or the camera password) at another host.
  * The password is never sent in plain text. ONVIF logins use a WS-Security
    password digest, with HTTP Digest as the fallback some cameras require.
  * Replies are size-limited, and XML with a DOCTYPE is refused.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import secrets
import socket
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

WS_DISCOVERY_ADDRESS = ("239.255.255.250", 3702)   # Standard WS-Discovery multicast group and port.
DISCOVERY_SECONDS = 3.0                            # How long to listen for camera replies.
SOAP_TIMEOUT_SECONDS = 5                           # Per request to a camera.
MAX_REPLY_BYTES = 1_000_000                        # Ignore anything larger. Real replies are a few kB.
DEFAULT_DEVICE_PATH = "/onvif/device_service"      # Where most cameras put the ONVIF service.
MAX_NAME_LENGTH = 40                               # Matches the portal's camera name limit.

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
}
PASSWORD_DIGEST_TYPE = ("http://docs.oasis-open.org/wss/2004/01/"
                        "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
BASE64_ENCODING_TYPE = ("http://docs.oasis-open.org/wss/2004/01/"
                        "oasis-200401-wss-soap-message-security-1.0#Base64Binary")

PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <s:Header>
    <a:Action s:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
    <a:MessageID>uuid:{message_id}</a:MessageID>
    <a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>
    <a:To s:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>
  </s:Header>
  <s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body>
</s:Envelope>"""


class OnvifError(Exception):
    """Raised with a plain-language message the portal can show as-is."""


class OnvifAuthError(OnvifError):
    """The camera answered but rejected the username or password."""


@dataclass(frozen=True)
class FoundCamera:
    ip_address: str
    device_url: str   # The camera's ONVIF device service, always on ip_address.
    name: str         # From the camera's ONVIF "name" scope, or its IP.
    hardware: str     # Model, from the "hardware" scope. "" if not reported.


# ── XML helpers (match by local name, so any namespace prefix works) ──────────

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data.upper():
        raise OnvifError("The camera sent a reply this portal refuses to read (XML DOCTYPE).")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise OnvifError("The camera sent a reply that isn't valid XML.") from exc


def _iter_named(root: ET.Element, name: str):
    return (element for element in root.iter() if _local(element.tag) == name)


def _text(root: ET.Element, name: str) -> str | None:
    for element in _iter_named(root, name):
        if element.text and element.text.strip():
            return element.text.strip()
    return None


# ── Address helpers ──────────────────────────────────────────────────────────

def _rebase_url(url: str, ip_address: str) -> str | None:
    """Keep the scheme, port and path of `url`, but force the host to `ip_address`."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    netloc = ip_address if port is None else f"{ip_address}:{port}"
    return f"{parts.scheme.lower()}://{netloc}{parts.path or DEFAULT_DEVICE_PATH}"


def _clean_label(value: str) -> str:
    text = "".join(ch for ch in unquote(value) if ch.isprintable()).strip()
    return text[:MAX_NAME_LENGTH]


def _scope_value(scopes: list[str], kind: str) -> str:
    prefix = f"onvif://www.onvif.org/{kind}/"
    for scope in scopes:
        if scope.lower().startswith(prefix):
            return _clean_label(scope[len(prefix):])
    return ""


# ── 1. Discovery ─────────────────────────────────────────────────────────────

def discover(local_ip: str, seconds: float = DISCOVERY_SECONDS) -> list[FoundCamera]:
    """Probe the network that `local_ip` is on and return every ONVIF camera that answers, sorted by IP.

    Only cameras on the same network segment answer (routers don't forward the probe).
    """
    probe = PROBE_TEMPLATE.format(message_id=uuid.uuid4()).encode("utf-8")
    found: dict[str, FoundCamera] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)   # Never leave the local network.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        sock.bind((local_ip, 0))                                         # Send from the modem network only.
        for _ in range(2):                                               # UDP can drop a packet. Cameras ignore repeats.
            sock.sendto(probe, WS_DISCOVERY_ADDRESS)
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, (sender, _port) = sock.recvfrom(65535)
            except socket.timeout:
                break
            camera = _parse_probe_match(data, sender)
            if camera is not None and camera.ip_address not in found:
                found[camera.ip_address] = camera
    return sorted(found.values(), key=lambda cam: ipaddress.ip_address(cam.ip_address))


def _parse_probe_match(data: bytes, sender: str) -> FoundCamera | None:
    try:
        if not ipaddress.ip_address(sender).is_private:
            return None                                   # Only local-network devices.
        root = _parse(data)
    except (ValueError, OnvifError):
        return None
    if next(_iter_named(root, "ProbeMatch"), None) is None:
        return None
    device_url = None
    for address in (_text(root, "XAddrs") or "").split():
        device_url = _rebase_url(address, sender)         # Ignore the reported host. Use who actually answered.
        if device_url:
            break
    scopes = (_text(root, "Scopes") or "").split()
    return FoundCamera(
        ip_address=sender,
        device_url=device_url or f"http://{sender}{DEFAULT_DEVICE_PATH}",
        name=_scope_value(scopes, "name") or sender,
        hardware=_scope_value(scopes, "hardware"),
    )


# ── 2. Stream path lookup ────────────────────────────────────────────────────

def _envelope(body: str, username: str = "", password: str = "",
              clock_offset: timedelta = timedelta(0)) -> bytes:
    """Build a SOAP 1.2 request. With a username, add a WS-Security UsernameToken.

    The digest formula (Base64(SHA-1(nonce + created + password))) is fixed by the
    WS-Security UsernameToken standard that ONVIF requires. It is not a design choice here.
    """
    header = ""
    if username:
        nonce = secrets.token_bytes(16)
        created = (datetime.now(timezone.utc) + clock_offset).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        header = (
            f'<s:Header><wsse:Security s:mustUnderstand="1">'
            f"<wsse:UsernameToken>"
            f"<wsse:Username>{escape(username)}</wsse:Username>"
            f'<wsse:Password Type="{PASSWORD_DIGEST_TYPE}">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{BASE64_ENCODING_TYPE}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f"<wsu:Created>{created}</wsu:Created>"
            f"</wsse:UsernameToken></wsse:Security></s:Header>"
        )
    namespaces = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NS.items())
    return f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {namespaces}>{header}<s:Body>{body}</s:Body></s:Envelope>'.encode()


def _opener(url: str, username: str, password: str) -> urllib.request.OpenerDirector:
    """No proxies (the camera is local). HTTP Digest credentials only for this camera's URL."""
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if username:
        passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, url, username, password)
        handlers.append(urllib.request.HTTPDigestAuthHandler(passwords))
    return urllib.request.build_opener(*handlers)


def _call(url: str, body: str, username: str = "", password: str = "",
          clock_offset: timedelta = timedelta(0), ws_security: bool = True) -> ET.Element:
    """POST one SOAP request. HTTP Digest is answered automatically if the camera asks for it.
    ws_security=False leaves out the WS-Security login header (HTTP Digest still works)."""
    request = urllib.request.Request(
        url,
        data=_envelope(body, username if ws_security else "", password, clock_offset),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        method="POST",
    )
    try:
        with _opener(url, username, password).open(request, timeout=SOAP_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(MAX_REPLY_BYTES)
        if exc.code == 401 or b"NotAuthorized" in detail:
            raise OnvifAuthError("The camera rejected the username or password for ONVIF.") from None
        raise OnvifError(f"The camera's ONVIF service answered with HTTP error {exc.code}.") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise OnvifError(f"No ONVIF service answered at {url} ({reason}). ONVIF may be turned off on the camera.") from None
    if len(data) > MAX_REPLY_BYTES:
        raise OnvifError("The camera's reply was too large.")
    root = _parse(data)
    if next(_iter_named(root, "Fault"), None) is not None:
        if b"NotAuthorized" in data:
            raise OnvifAuthError("The camera rejected the username or password for ONVIF.")
        raise OnvifError(f"The camera returned an ONVIF error: {_text(root, 'Text') or 'no details'}.")
    return root


def _camera_clock_offset(device_url: str, username: str, password: str) -> timedelta:
    """Camera time minus our time. The login digest includes a timestamp, and many
    cameras reject it if their clock differs from ours by more than a few seconds.

    Sent without the WS-Security header (it would need the offset we're measuring).
    Most cameras answer this without a login; the rest ask for HTTP Digest, which is supplied."""
    try:
        root = _call(device_url, "<tds:GetSystemDateAndTime/>", username, password, ws_security=False)
        utc = next(_iter_named(root, "UTCDateTime"))
        parts = [int(_text(utc, name) or "") for name in ("Year", "Month", "Day", "Hour", "Minute", "Second")]
        return datetime(*parts, tzinfo=timezone.utc) - datetime.now(timezone.utc)
    except (OnvifError, StopIteration, ValueError):
        return timedelta(0)                                        # Unknown: assume clocks agree.


def _best_profile_token(profiles: ET.Element) -> str:
    """Pick the profile with the largest resolution (usually the main stream)."""
    best_token, best_area = None, -1
    for profile in _iter_named(profiles, "Profiles"):
        token = profile.get("token")
        if not token:
            continue
        resolution = next(_iter_named(profile, "Resolution"), None)
        try:
            area = int(_text(resolution, "Width")) * int(_text(resolution, "Height")) if resolution is not None else 0
        except (TypeError, ValueError):
            area = 0
        if area > best_area:
            best_token, best_area = token, area
    if best_token is None:
        raise OnvifError("The camera reported no video profiles.")
    return best_token


def get_stream_location(ip_address: str, device_url: str, username: str, password: str) -> tuple[int, str]:
    """Ask the camera for its RTSP stream. Returns (port, path). The host is always ip_address."""
    device_url = _rebase_url(device_url, ip_address) or f"http://{ip_address}{DEFAULT_DEVICE_PATH}"
    offset = _camera_clock_offset(device_url, username, password)

    capabilities = _call(device_url,
                         "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
                         username, password, offset)
    media = next(_iter_named(capabilities, "Media"), None)
    media_xaddr = _text(media, "XAddr") if media is not None else None
    media_url = (_rebase_url(media_xaddr, ip_address) if media_xaddr else None) or device_url

    profiles = _call(media_url, "<trt:GetProfiles/>", username, password, offset)
    token = _best_profile_token(profiles)

    reply = _call(media_url,
                  "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                  "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                  f"<trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetStreamUri>",
                  username, password, offset)
    uri = _text(reply, "Uri")
    if not uri:
        raise OnvifError("The camera did not report a stream address.")
    try:
        parts = urlsplit(uri)
        port = parts.port or 554
    except ValueError:
        raise OnvifError(f"The camera reported an unusable stream address: {uri}") from None
    if parts.scheme.lower() != "rtsp":
        raise OnvifError(f"The camera reported a non-RTSP stream ({parts.scheme}).")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return port, path
