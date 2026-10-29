import base64

import pytest

from app import transmission


def test_stop_targets_only_selected_hash(monkeypatch):
    info_hash = "a" * 40
    calls = []

    def fake_rpc(method, **arguments):
        calls.append((method, arguments))
        if method == "torrent-get":
            return {"torrents": [{"hashString": info_hash.upper()}]}
        return {}

    monkeypatch.setattr(transmission, "rpc", fake_rpc)
    assert transmission.stop_torrent(info_hash)
    assert calls == [("torrent-get", {"ids": [info_hash], "fields": ["hashString"]}),
                     ("torrent-stop", {"ids": [info_hash]})]


def test_stop_missing_torrent_does_not_send_stop(monkeypatch):
    calls = []

    def fake_rpc(method, **arguments):
        calls.append(method)
        return {"torrents": []}

    monkeypatch.setattr(transmission, "rpc", fake_rpc)
    assert not transmission.stop_torrent("a" * 40)
    assert calls == ["torrent-get"]


def test_torrent_statuses_keys_by_hash(monkeypatch):
    monkeypatch.setattr(transmission, "rpc", lambda method, **kwargs: {
        "torrents": [{"hashString": "A" * 40, "status": 0}]})
    assert transmission.torrent_statuses() == {"a" * 40: 0}


def test_parse_magnet_hex_and_base32():
    h = "a" * 40
    assert transmission.parse_magnet(f"magnet:?xt=urn:btih:{h.upper()}&dn=Sample+Show") == (h, "Sample Show")
    b32 = base64.b32encode(bytes.fromhex(h)).decode()
    assert transmission.parse_magnet(f"magnet:?xt=urn:btih:{b32}") == (h, h)


@pytest.mark.parametrize("value", ["https://example.com", "magnet:?dn=missing", "magnet:?xt=urn:btih:short", "magnet:?xt=urn:btih:" + "a" * 40 + "\nother"])
def test_parse_magnet_rejects_invalid(value):
    with pytest.raises(ValueError):
        transmission.parse_magnet(value)


def test_web_url_from_rpc(monkeypatch):
    monkeypatch.setattr(transmission.config, "TRANSMISSION_URL", "http://192.168.0.10:9091/transmission/rpc")
    assert transmission.web_url() == "http://192.168.0.10:9091/transmission/web/"
    monkeypatch.setattr(transmission.config, "TRANSMISSION_URL", "http://user:pass@host/transmission/rpc")
    assert transmission.web_url() is None


def test_torrent_items_includes_external_items(monkeypatch):
    rows = [{"hashString": "a" * 40, "name": "external", "status": 4, "addedDate": 123, "percentDone": 0.5}]
    monkeypatch.setattr(transmission, "rpc", lambda method, **kwargs: {"torrents": rows})
    assert transmission.torrent_items() == rows


def test_torrent_exists_checks_exact_hash(monkeypatch):
    h = "a" * 40
    monkeypatch.setattr(transmission, "rpc", lambda method, **kwargs: {
        "torrents": [{"hashString": "b" * 40}]})
    assert not transmission.torrent_exists(h)
    monkeypatch.setattr(transmission, "rpc", lambda method, **kwargs: {
        "torrents": [{"hashString": h.upper()}]})
    assert transmission.torrent_exists(h)


def test_start_targets_only_selected_hash(monkeypatch):
    info_hash = "a" * 40
    calls = []

    def fake_rpc(method, **arguments):
        calls.append((method, arguments))
        if method == "torrent-get":
            return {"torrents": [{"hashString": info_hash.upper()}]}
        return {}

    monkeypatch.setattr(transmission, "rpc", fake_rpc)
    assert transmission.start_torrent(info_hash)
    assert calls == [("torrent-get", {"ids": [info_hash], "fields": ["hashString"]}),
                     ("torrent-start", {"ids": [info_hash]})]


def test_start_missing_torrent_does_not_send_start(monkeypatch):
    calls = []

    def fake_rpc(method, **arguments):
        calls.append(method)
        return {"torrents": []}

    monkeypatch.setattr(transmission, "rpc", fake_rpc)
    assert not transmission.start_torrent("a" * 40)
    assert calls == ["torrent-get"]


def test_add_magnet_is_paused(monkeypatch):
    calls = []

    def fake_rpc(method, **arguments):
        calls.append((method, arguments))
        return {"torrent-added": {"id": 1}}

    monkeypatch.setattr(transmission, "rpc", fake_rpc)
    magnet = "magnet:?xt=urn:btih:" + "a" * 40
    assert transmission.add_magnet(magnet) == "added"
    assert calls == [("torrent-add", {"filename": magnet, "paused": True})]
