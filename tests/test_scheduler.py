import asyncio
from pathlib import Path

import pytest

from app import config, db
from app.browser import FetchResult
from app.scheduler import Scheduler, run_once

FIX = Path(__file__).parent / "fixtures"
HASH = "4e4d86cbee91db107def6d3fd24271afd28130e2"
PAGES = {
    "https://torrentsome268.com/v-4-16": "torrentsome/board.html",
    "https://torrentsome268.com/v/311439": "torrentsome/detail.html",
    "https://torrentzota206.com/t/4/16.html": "torrentzota/board.html",
    "https://torrentzota206.com/torrent/r6o77rg.html": "torrentzota/detail.html",
}
DETAILS = {"https://torrentsome268.com/v/311439", "https://torrentzota206.com/torrent/r6o77rg.html"}


DOWN = FetchResult(0, "", "", "net::ERR_CONNECTION_CLOSED")


class FakeBrowser:
    def __init__(self, script: dict[str, list] | None = None):
        self.urls: list[str] = []
        self.script = script or {}  # url -> results returned in order, the last one repeating

    async def fetch(self, url, **kw):
        self.urls.append(url)
        if seq := self.script.get(url):
            r = seq.pop(0) if len(seq) > 1 else seq[0]
            return FetchResult(200, url, (FIX / r).read_text(encoding="utf-8"), None) if isinstance(r, str) else r
        if url in PAGES:
            return FetchResult(200, url, (FIX / PAGES[url]).read_text(encoding="utf-8"), None)
        return FetchResult(0, url, "", "net::ERR_NAME_NOT_RESOLVED")


def test_run_once_adds_once(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    db.update_site(conn, 1, boards=["/v-4-16"])
    db.update_site(conn, 2, boards=["/t/4/16.html"])
    db.update_site(conn, 3, enabled=False)
    calls: list[str] = []
    add = lambda m: calls.append(m) or "added"  # noqa: E731

    b = FakeBrowser()
    run = db.get_run(conn, asyncio.run(run_once(conn, b, add_magnet=add)))
    assert (run["status"], run["added"]) == ("ok", 1), run["log"]
    assert calls == [f"magnet:?xt=urn:btih:{HASH}"]
    assert "skip: episode exists" in run["log"]  # zota's copy never hits the detail page
    assert "https://torrentzota206.com/torrent/r6o77rg.html" not in b.urls

    b = FakeBrowser()
    run = db.get_run(conn, asyncio.run(run_once(conn, b, add_magnet=add)))
    assert run["added"] == 0 and len(calls) == 1
    assert not DETAILS & set(b.urls)


def _moved_site(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    db.update_site(conn, 1, url="https://torrentsome266.com", boards=["/v-4-16"])  # 268 is the +2 candidate
    for i in (2, 3):
        db.update_site(conn, i, enabled=False)
    return conn


def test_domain_probe_moves_site(tmp_path):
    conn = _moved_site(tmp_path)
    run = db.get_run(conn, asyncio.run(run_once(conn, FakeBrowser(), add_magnet=lambda m: "added")))
    assert "domain moved https://torrentsome266.com -> https://torrentsome268.com" in run["log"]
    assert run["added"] == 1 and run["status"] == "ok", run["log"]
    assert db.get_site(conn, 1)["url"] == "https://torrentsome268.com"


def test_domain_probe_dry_run_changes_nothing(tmp_path):
    conn = _moved_site(tmp_path)
    run = db.get_run(conn, asyncio.run(run_once(conn, FakeBrowser(), dry_run=True)))
    assert "would move https://torrentsome266.com -> https://torrentsome268.com (dry-run)" in run["log"]
    assert "dry-run: would add " + HASH in run["log"] and run["status"] == "ok"
    assert db.get_site(conn, 1)["url"] == "https://torrentsome266.com"
    assert not db.get_site(conn, 1)["last_ok_at"]


def test_domain_probe_original_recovers(tmp_path):
    conn = _moved_site(tmp_path)
    b = FakeBrowser({"https://torrentsome266.com/v-4-16": [DOWN, "torrentsome/board.html"]})
    run = db.get_run(conn, asyncio.run(run_once(conn, b, add_magnet=lambda m: "added")))
    assert "/v-4-16 recovered, not moving" in run["log"], run["log"]
    assert "domain moved" not in run["log"]
    assert db.get_site(conn, 1)["url"] == "https://torrentsome266.com"


def test_transmission_unconfigured_is_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRANSMISSION_URL", "")  # real add_magnet: Request("") raises ValueError
    conn = db.connect(tmp_path / "app.db")
    db.update_site(conn, 1, boards=["/v-4-16"])
    for i in (2, 3):
        db.update_site(conn, i, enabled=False)
    run = db.get_run(conn, asyncio.run(run_once(conn, FakeBrowser())))
    assert run["status"] == "partial" and "transmission FAIL" in run["log"], run["log"]
    assert db.list_downloads(conn) == []


def test_failed_later_board_is_partial(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    db.update_site(conn, 1, boards=["/v-4-16", "/v-2-13"])  # the fake browser only knows /v-4-16
    for i in (2, 3):
        db.update_site(conn, i, enabled=False)
    run = db.get_run(conn, asyncio.run(run_once(conn, FakeBrowser(), add_magnet=lambda m: "added")))
    assert run["status"] == "partial", run["log"]
    assert db.get_site(conn, 1)["last_ok_at"]


def test_scheduler_waits_for_manual_trigger(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "app.db")
    calls = []

    async def fake_run_once(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr("app.scheduler.run_once", fake_run_once)
    sched = Scheduler(conn, FakeBrowser())

    async def go():
        sched.start()
        await asyncio.sleep(0.05)
        assert calls == []
        assert sched.trigger_now()
        await asyncio.sleep(0.05)
        assert calls == [1]
        await sched.stop()

    asyncio.run(go())


def test_mode_switch_persists_and_auto_runs_when_due(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    path = tmp_path / "app.db"
    conn = db.connect(path)
    sched = Scheduler(conn, FakeBrowser())
    assert sched.mode == "manual"
    sched.set_mode("auto")
    sched.set_interval(5)
    assert db.get_setting(conn, "run_mode") == "auto"
    db.set_setting(conn, "auto_enabled_at", (datetime.now() - timedelta(minutes=6)).isoformat(timespec="seconds"))
    calls = []

    async def fake_run_once(conn, *args, **kwargs):
        calls.append(1)
        rid = db.create_run(conn)
        db.finish_run(conn, rid, "ok", 0, 0, "test")

    monkeypatch.setattr("app.scheduler.run_once", fake_run_once)

    async def go():
        sched.start()
        await asyncio.sleep(0.05)
        assert calls == [1]
        sched.set_mode("manual")
        await asyncio.sleep(0.05)
        assert sched.next_run_at is None
        await sched.stop()

    asyncio.run(go())
    conn.close()
    reopened = db.connect(path)
    assert db.get_setting(reopened, "run_mode") == "manual"
    reopened.close()


def test_older_board_pages_are_crawled(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    db.update_site(conn, 1, boards=["/v-4-16"])
    for i in (2, 3):
        db.update_site(conn, i, enabled=False)
    # the matching post has slid to page 2; page 1 shows other posts
    page1 = FetchResult(200, "https://torrentsome268.com/v-4-16", '<a href="/v/1">other show</a>', None)
    b = FakeBrowser({"https://torrentsome268.com/v-4-16": [page1],
                     "https://torrentsome268.com/v-4-16?page=2": ["torrentsome/board.html"]})
    run = db.get_run(conn, asyncio.run(run_once(conn, b, add_magnet=lambda m: "added")))
    assert run["added"] == 1 and run["status"] == "ok", run["log"]
    assert "https://torrentsome268.com/v-4-16?page=3" in b.urls and "page 3 FAIL" in run["log"]

    db.update_site(conn, 1, pages=1)  # this site set to page 1 only
    b = FakeBrowser()
    asyncio.run(run_once(conn, b, dry_run=True))
    assert not [u for u in b.urls if "?page=" in u]


def test_user_stop_finishes_run_without_more_fetches(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    db.update_site(conn, 1, boards=["/v-4-16"], pages=1)
    for i in (2, 3):
        db.update_site(conn, i, enabled=False)
    stop = asyncio.Event()

    class StoppingBrowser(FakeBrowser):
        async def fetch(self, url, **kw):
            result = await super().fetch(url, **kw)
            stop.set()
            return result

    browser = StoppingBrowser()
    rid = asyncio.run(run_once(conn, browser, add_magnet=lambda m: "added", stop_event=stop))
    run = db.get_run(conn, rid)
    assert run["status"] == "stopped" and "stopped by user" in run["log"]
    assert len(browser.urls) == 1 and run["added"] == 0


def test_scheduler_stop_request_requires_active_run(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    sched = Scheduler(conn, FakeBrowser())
    assert not sched.request_stop()

    async def go():
        async with sched._lock:
            assert sched.request_stop() and sched.stop_requested
    asyncio.run(go())
