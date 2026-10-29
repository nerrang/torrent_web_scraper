"""One crawl pass (run_once) and the periodic loop around it."""
import asyncio
from datetime import datetime, timedelta
import logging
import sys
from contextlib import suppress
from urllib.parse import urlsplit

from app import config, crawler, db, transmission
from app.browser import Browser, FetchResult
from app.transmission import TransmissionError


class StopRequested(Exception):
    """Cooperative stop at a safe point between crawl operations."""


def _why(r: FetchResult) -> str:
    return (r.error or f"HTTP {r.status}") if not r.ok else "0 posts"


async def run_once(conn, browser, *, add_magnet=transmission.add_magnet, dry_run: bool = False,
                   stop_event: asyncio.Event | None = None) -> int:
    """Crawl every enabled site/board once. Returns the run id; never raises."""
    rid = db.create_run(conn)
    log: list[str] = []
    matched = added = 0
    site_fail = soft_fail = False

    def L(msg: str) -> None:
        log.append(f"{datetime.now():%H:%M:%S} {msg}")

    def check_stop() -> None:
        if stop_event is not None and stop_event.is_set():
            raise StopRequested

    def move(site, old: str, new: str) -> str:  # dry-run persists nothing but the run record
        if dry_run:
            L(f"site {site['name']}: would move {old} -> {new} (dry-run)")
        else:
            db.update_site(conn, site["id"], url=new)
            L(f"site {site['name']}: domain moved {old} -> {new}")
        return new

    try:
        check_stop()
        sites, progs = db.list_sites(conn, enabled_only=True), db.list_programs(conn, enabled_only=True)
        if not sites or not progs:
            L("nothing to do (no enabled sites/programs)")
            status = "ok"
        else:
            seen: set[str] = set()
            tr_down = False
            ok_sites = 0
            for site in sites:
                check_stop()
                name, base, site_ok, site_any = site["name"], site["url"], True, False
                for i, board in enumerate(site["boards"]):
                    check_stop()
                    r = await browser.fetch(base + board)
                    check_stop()
                    posts = crawler.parse_board(r.html, r.final_url) if r.ok and crawler.trusted_site_url(base, r.final_url) else []
                    if not posts and i == 0:
                        L(f"site {name}: {board} FAIL {_why(r)}")
                        L(f"site {name}: probing +1..+5")
                        for cand in crawler.domain_candidates(base):
                            check_stop()
                            r = await browser.fetch(cand + board, timeout_ms=15000, retries=1)
                            check_stop()
                            posts = crawler.parse_board(r.html, r.final_url) if r.ok and crawler.trusted_site_url(cand, r.final_url) else []
                            if posts:
                                # a transient error on the original (seen: ~20 s ERR_CONNECTION_CLOSED) must not
                                # move the site to a live mirror, so confirm the original is still down first
                                r0 = await browser.fetch(base + board, timeout_ms=15000, retries=0)
                                check_stop()
                                p0 = crawler.parse_board(r0.html, r0.final_url) if r0.ok and crawler.trusted_site_url(base, r0.final_url) else []
                                if p0:
                                    L(f"site {name}: {board} recovered, not moving")
                                    r, posts = r0, p0
                                else:
                                    base = move(site, base, cand)
                                break
                        else:
                            L(f"site {name}: probe failed")
                            site_ok = False
                            break  # skip remaining boards
                    elif not posts:
                        L(f"site {name}: {board} FAIL {_why(r)}")
                        site_ok = False
                        continue
                    if i == 0 and (origin := "{0.scheme}://{0.netloc}".format(urlsplit(r.final_url))) != base:
                        base = move(site, base, origin)  # followed a redirect to a new domain
                    site_any = True
                    for n in range(2, site["pages"] + 1):  # all seeded sites page with ?page=N
                        check_stop()
                        rn = await browser.fetch(f"{base}{board}{'&' if '?' in board else '?'}page={n}")
                        check_stop()
                        more = crawler.parse_board(rn.html, rn.final_url) if rn.ok and crawler.trusted_site_url(base, rn.final_url) else []
                        if not more:  # an older page is a bonus; failing it doesn't fail the board
                            L(f"site {name}: {board} page {n} FAIL {_why(rn)}")
                            break
                        posts += more
                    L(f"site {name}: {board} -> {len(posts)} posts")
                    for title, url in posts:
                        check_stop()
                        if url in seen:
                            continue
                        seen.add(url)
                        p = crawler.match_program(title, progs)
                        if not p:
                            continue
                        ep = crawler.episode_key(title)
                        matched += 1
                        L(f"match [{p['name']}] {title} ({ep or '-'})")
                        if ep and db.episode_exists(conn, p["id"], ep):
                            L("  skip: episode exists")
                            continue
                        if tr_down:
                            L("  skip: transmission down")
                            continue
                        d = await browser.fetch(url)
                        check_stop()
                        if not d.ok or not crawler.trusted_site_url(base, d.final_url):
                            L(f"  detail FAIL {d.error or d.status if not d.ok else 'unexpected domain'}")
                            soft_fail = True
                            continue
                        hashes = crawler.extract_magnets(d.html)
                        if not hashes:
                            L("  no magnet")
                            soft_fail = True
                            continue
                        h = hashes[0]
                        if db.download_exists(conn, h):
                            L(f"  skip: hash exists {h[:8]}")
                            continue
                        if dry_run:
                            L(f"  dry-run: would add {h}")
                            continue
                        try:
                            res = await asyncio.to_thread(add_magnet, crawler.magnet_uri(h))
                        except TransmissionError as e:
                            L(f"  transmission FAIL {e}")
                            tr_down = soft_fail = True
                            continue
                        db.add_download(conn, h, p["id"], ep, title, name)  # recorded for "duplicate" too
                        if res == "added":
                            added += 1
                        L(f"  {res} {h}")
                if site_any:  # one working board is enough; a failed later board only makes the run partial
                    if not dry_run:
                        db.update_site(conn, site["id"], last_ok_at=db.now())
                    ok_sites += 1
                site_fail = site_fail or not site_ok
            check_stop()
            status = "error" if ok_sites == 0 else "partial" if (site_fail or soft_fail) else "ok"
    except StopRequested:
        L("stopped by user")
        status = "stopped"
    except Exception as e:
        logging.exception("run failed")
        L(f"exception: {e!r}")
        status = "error"
    L(f"done: matched={matched} added={added} status={status}")
    db.finish_run(conn, rid, status, matched, added, "\n".join(log))
    return rid


class Scheduler:
    def __init__(self, conn, browser) -> None:
        self.conn, self.browser = conn, browser
        self.next_run_at: datetime | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._now = False
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def trigger_now(self) -> bool:
        if self.running or self._now:
            return False
        self._now = True
        self._wake.set()
        return True

    @property
    def stop_requested(self) -> bool:
        return self.running and self._stop_event.is_set()

    def request_stop(self) -> bool:
        if not self.running:
            return False
        self._stop_event.set()
        return True

    @property
    def mode(self) -> str:
        return db.get_setting(self.conn, "run_mode", "manual")

    def set_mode(self, mode: str) -> None:
        if mode not in ("manual", "auto"):
            raise ValueError("mode must be manual or auto")
        if mode == self.mode:
            return
        db.set_setting(self.conn, "run_mode", mode)
        if mode == "auto":
            db.set_setting(self.conn, "auto_enabled_at", datetime.now().isoformat(timespec="seconds"))
        else:
            self.next_run_at = None
        self._wake.set()

    def set_interval(self, minutes: int) -> None:
        if not 5 <= minutes <= 1440:
            raise ValueError("interval must be 5..1440 minutes")
        db.set_setting(self.conn, "interval_min", str(minutes))
        self._wake.set()

    async def _run(self) -> None:
        async with self._lock:
            self._stop_event = asyncio.Event()
            await run_once(self.conn, self.browser, dry_run=config.DRY_RUN, stop_event=self._stop_event)

    async def _loop(self) -> None:
        while True:
            auto = self.mode == "auto"
            if auto:
                interval = max(5, int(db.get_setting(self.conn, "interval_min", "60")))
                enabled_at = db.get_setting(self.conn, "auto_enabled_at", "")
                last = db.latest_run(self.conn)
                anchor = max(filter(None, (last["started"] if last else "", enabled_at)), default="")
                self.next_run_at = (datetime.fromisoformat(anchor) if anchor else datetime.now()) + timedelta(minutes=interval)
            else:
                self.next_run_at = None
            due = auto and self.next_run_at <= datetime.now()
            if self._now or due:
                self._now = False
                try:
                    await self._run()
                except Exception:
                    logging.exception("scheduler")
                    await asyncio.sleep(60)
                continue
            self._wake.clear()
            try:
                timeout = min(60, max(0, (self.next_run_at - datetime.now()).total_seconds())) if auto else None
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass


async def _main() -> None:
    conn = db.connect(config.DB_PATH)
    browser = Browser()
    await browser.start()
    try:
        rid = await run_once(conn, browser, dry_run=config.DRY_RUN or "--dry-run" in sys.argv)
        print(db.get_run(conn, rid)["log"])
    finally:
        await browser.stop()
        conn.close()


if __name__ == "__main__":
    asyncio.run(_main())
