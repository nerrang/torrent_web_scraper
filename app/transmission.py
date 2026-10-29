"""Transmission RPC over stdlib urllib. Blocking: call via asyncio.to_thread."""
import base64
import http.client
import json
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit
import urllib.error
import urllib.request

from app import config


class TransmissionError(Exception):
    pass


_sid = ""


def rpc(method: str, **arguments) -> dict:
    global _sid
    auth = base64.b64encode(f"{config.TRANSMISSION_USER}:{config.TRANSMISSION_PASSWORD}".encode()).decode()
    body = json.dumps({"method": method, "arguments": arguments}).encode()
    for _ in range(2):
        try:
            req = urllib.request.Request(config.TRANSMISSION_URL, data=body, headers={
                "Authorization": f"Basic {auth}", "Content-Type": "application/json",
                "X-Transmission-Session-Id": _sid})
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise TransmissionError(f"HTTPError {e.code} {e.reason}") from None
            _sid = e.headers.get("X-Transmission-Session-Id", "")  # stale/missing session id -> retry once
            continue
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            raise TransmissionError(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from None
        if resp.get("result") != "success":
            raise TransmissionError(f"result: {resp.get('result')}")
        return resp.get("arguments", {})
    raise TransmissionError("409 twice")


def parse_magnet(magnet: str) -> tuple[str, str]:
    """Validate a magnet and return its normalized BTIH hash and display name."""
    magnet = magnet.strip()
    if len(magnet) > 8192 or any(c in magnet for c in "\r\n\x00"):
        raise ValueError("마그넷 주소가 너무 길거나 잘못됐습니다.")
    try:
        parsed = urlsplit(magnet)
    except ValueError:
        raise ValueError("마그넷 주소 형식이 잘못됐습니다.") from None
    if parsed.scheme.lower() != "magnet" or parsed.netloc or parsed.path or parsed.fragment:
        raise ValueError("마그넷 주소 형식이 잘못됐습니다.")
    params = parse_qs(parsed.query)
    xt = [value[9:] for value in params.get("xt", []) if value.lower().startswith("urn:btih:")]
    if len(xt) != 1:
        raise ValueError("BTIH 마그넷 주소를 입력하세요.")
    info_hash = xt[0]
    if re.fullmatch(r"[A-Z2-7a-z2-7]{32}", info_hash):
        try:
            info_hash = base64.b32decode(info_hash.upper()).hex()
        except ValueError:
            raise ValueError("BTIH 해시가 잘못됐습니다.") from None
    if not re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        raise ValueError("BTIH 해시는 40자리 16진수 또는 32자리 Base32여야 합니다.")
    name = (params.get("dn") or [""])[0].strip()
    name = " ".join(name.split())[:150] or info_hash.lower()
    return info_hash.lower(), name


def add_magnet(magnet: str) -> str:
    res = rpc("torrent-add", filename=magnet, paused=True)
    if "torrent-added" in res:
        return "added"
    if "torrent-duplicate" in res:
        return "duplicate"
    raise TransmissionError(f"unexpected torrent-add result: {list(res)}")


def web_url() -> str | None:
    """Browser URL for the Transmission web client sharing this RPC endpoint."""
    parsed = urlsplit(config.TRANSMISSION_URL)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.path.endswith("/rpc"):
        return None
    if parsed.username or parsed.password:
        return None  # never put RPC credentials into a browser link
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path[:-4] + "/web/", "", ""))


def torrent_items() -> list[dict]:
    """Current items in Transmission, including torrents not added by this app."""
    return rpc("torrent-get", fields=["hashString", "name", "status", "addedDate", "percentDone"]).get("torrents", [])


def torrent_statuses() -> dict[str, int]:
    """Current Transmission status keyed by stable info hash (RPC v17 field name)."""
    rows = rpc("torrent-get", fields=["hashString", "status"]).get("torrents", [])
    return {row["hashString"].lower(): row["status"] for row in rows if "hashString" in row}


def torrent_exists(info_hash: str) -> bool:
    """Check a specific hash before deleting its local history."""
    rows = rpc("torrent-get", ids=[info_hash], fields=["hashString"]).get("torrents", [])
    return any(row.get("hashString", "").lower() == info_hash.lower() for row in rows)


def stop_torrent(info_hash: str) -> bool:
    """Stop one existing torrent; return False if it is no longer in Transmission."""
    rows = rpc("torrent-get", ids=[info_hash], fields=["hashString"]).get("torrents", [])
    if not any(row.get("hashString", "").lower() == info_hash.lower() for row in rows):
        return False
    rpc("torrent-stop", ids=[info_hash])
    return True


def start_torrent(info_hash: str) -> bool:
    """Resume one existing torrent; return False if it is no longer in Transmission."""
    if not torrent_exists(info_hash):
        return False
    rpc("torrent-start", ids=[info_hash])
    return True
