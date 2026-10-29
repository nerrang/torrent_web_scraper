"""FastAPI app: lifespan wiring and the plain-HTML screens."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from http import HTTPStatus
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config, db, doctor, transmission
from app.browser import Browser
from app.scheduler import Scheduler
from app.views import go, templates

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("APP_ENV=%s dry_run=%s db=%s", config.APP_ENV, config.DRY_RUN, config.DB_PATH)
    conn = db.connect(config.DB_PATH)
    browser = Browser()
    await browser.start()
    sched = Scheduler(conn, browser)
    sched.start()
    app.state.db, app.state.browser, app.state.scheduler = conn, browser, sched
    yield
    for t in list(doctor._tasks):
        t.cancel()
    await asyncio.gather(*doctor._tasks, return_exceptions=True)
    await sched.stop()
    await browser.stop()
    conn.close()


app = FastAPI(lifespan=lifespan)
app.include_router(doctor.router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def page(request: Request, name: str, status_code: int = 200, **ctx):
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    default = exc.detail == HTTPStatus(exc.status_code).phrase  # Starlette's English default, e.g. "Not Found"
    msg = ("페이지를 찾을 수 없습니다." if exc.status_code == 404 else "요청을 처리할 수 없습니다.") if default else exc.detail
    return page(request, "error.html", exc.status_code, code=exc.status_code, message=msg)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return page(request, "error.html", 422, code=422, message="입력값이 올바르지 않습니다.")


# dashboard
@app.get("/")
async def index(request: Request, msg: str | None = None):
    conn, sched = request.app.state.db, request.app.state.scheduler
    now = datetime.now()
    sites = db.list_sites(conn, enabled_only=True)
    downloads = db.list_downloads(conn, 20)
    current_downloads = None
    torrent_status = None
    if not config.DRY_RUN:
        try:
            items = await asyncio.to_thread(transmission.torrent_items)
            current_downloads = [dict(info_hash=row["hashString"].lower(), title=row.get("name") or row["hashString"],
                                      added_at=datetime.fromtimestamp(row.get("addedDate") or 0).isoformat(timespec="seconds")
                                      if row.get("addedDate") else None,
                                      status=row.get("status"), progress=round(100 * row.get("percentDone", 0)))
                                 for row in items if row.get("hashString")]
            current_downloads.sort(key=lambda row: row["added_at"] or "", reverse=True)
            torrent_status = {row["info_hash"]: row["status"] for row in current_downloads}
        except transmission.TransmissionError:
            pass
    return page(request, "index.html", running=sched.running or msg == "started",
                run_mode=sched.mode, interval_min=db.get_setting(conn, "interval_min", "60"),
                next_run_at=sched.next_run_at,
                can_stop=sched.running, stop_requested=sched.stop_requested,
                msg=msg,
                runs=db.list_runs(conn, 10), downloads=downloads, current_downloads=current_downloads,
                torrent_status=torrent_status,
                hidden_download_count=db.hidden_download_count(conn),
                transmission_web_url=transmission.web_url(),
                day_added=db.count_downloads_since(conn, (now - timedelta(days=1)).isoformat(timespec="seconds")),
                sites=sites, programs=db.list_programs(conn))


@app.post("/downloads/{info_hash}/stop")
async def stop_download(request: Request, info_hash: str):
    if len(info_hash) != 40 or any(c not in "0123456789abcdefABCDEF" for c in info_hash):
        raise HTTPException(400, "올바르지 않은 토렌트 해시입니다.")
    try:
        stopped = await asyncio.to_thread(transmission.stop_torrent, info_hash.lower())
    except transmission.TransmissionError as e:
        return go("/", f"Transmission 중지 실패: {e}")
    return go("/", msg="download-stopped") if stopped else go("/", "Transmission에서 해당 항목을 찾을 수 없습니다.")


@app.post("/downloads/{info_hash}/delete-history")
async def delete_download_history(request: Request, info_hash: str):
    if len(info_hash) != 40 or any(c not in "0123456789abcdefABCDEF" for c in info_hash):
        raise HTTPException(400, "올바르지 않은 토렌트 해시입니다.")
    info_hash = info_hash.lower()
    conn = request.app.state.db
    if not db.download_exists(conn, info_hash):
        raise HTTPException(404, "다운로드 이력을 찾을 수 없습니다.")
    try:
        if await asyncio.to_thread(transmission.torrent_exists, info_hash):
            return go("/", "Transmission에 항목이 남아 있어 이력을 삭제할 수 없습니다.")
    except transmission.TransmissionError as e:
        return go("/", f"Transmission 확인 실패: {e}")
    db.delete_download(conn, info_hash)
    return go("/", msg="download-history-deleted")


@app.post("/downloads/{info_hash}/start")
async def start_download(request: Request, info_hash: str):
    if len(info_hash) != 40 or any(c not in "0123456789abcdefABCDEF" for c in info_hash):
        raise HTTPException(400, "올바르지 않은 토렌트 해시입니다.")
    try:
        started = await asyncio.to_thread(transmission.start_torrent, info_hash.lower())
    except transmission.TransmissionError as e:
        return go("/", f"Transmission 재실행 실패: {e}")
    return go("/", msg="download-started") if started else go("/", "Transmission에서 해당 항목을 찾을 수 없습니다.")


@app.post("/runs/clear-list")
async def clear_run_list(request: Request):
    db.hide_run_list(request.app.state.db)
    return go("/", msg="runs-cleared")


@app.post("/downloads/show-list")
async def show_download_list(request: Request):
    db.show_download_list(request.app.state.db)
    return go("/", msg="downloads-restored")


@app.post("/downloads/clear-list")
async def clear_download_list(request: Request):
    db.hide_download_list(request.app.state.db)
    return go("/", msg="downloads-cleared")


@app.post("/run-stop")
async def run_stop(request: Request):
    requested = request.app.state.scheduler.request_stop()
    return go("/", msg="run-stop-requested" if requested else "run-not-running")


@app.post("/run-now")
async def run_now(request: Request):
    ok = request.app.state.scheduler.trigger_now()
    return RedirectResponse("/?msg=started" if ok else "/?msg=already-running", status_code=303)


@app.post("/settings/run-mode")
async def set_run_mode(request: Request, mode: str = Form()):
    if mode not in ("manual", "auto"):
        raise HTTPException(400, "실행 방식은 자동 또는 수동이어야 합니다.")
    request.app.state.scheduler.set_mode(mode)
    return go("/", msg="mode-auto" if mode == "auto" else "mode-manual")


@app.post("/settings/interval")
async def set_interval(request: Request, minutes: int = Form(ge=5, le=1440)):
    request.app.state.scheduler.set_interval(minutes)
    return go("/", msg="interval")


@app.post("/settings/doctor-model")
async def set_doctor_model(request: Request, model: str = Form()):
    if model not in db.MODELS:
        raise HTTPException(400, "모델은 sonnet 또는 opus만 됩니다.")
    db.set_setting(request.app.state.db, "doctor_model", model)
    return go("/sites", msg="model")


@app.get("/runs/{id}")
async def run_detail(request: Request, id: int):
    run = db.get_run(request.app.state.db, id)
    if not run:
        raise HTTPException(404, "실행 기록이 없습니다.")
    return page(request, "run.html", run=run)


# programs
@app.post("/magnets")
async def add_manual_magnet(request: Request, magnet: str = Form()):
    if config.DRY_RUN:
        return go("/programs", "개발 환경에서는 다운로드할 수 없습니다.")
    try:
        info_hash, title = transmission.parse_magnet(magnet)
    except ValueError as e:
        return go("/programs", str(e))
    try:
        result = await asyncio.to_thread(transmission.add_magnet, magnet.strip())
    except transmission.TransmissionError as e:
        return go("/programs", f"Transmission 추가 실패: {e}")
    conn = request.app.state.db
    db.add_download(conn, info_hash, None, None, title, "직접 추가")
    db.show_download(conn, info_hash)
    return go("/programs", msg="magnet-added" if result == "added" else "magnet-duplicate")


@app.get("/programs")
async def programs(request: Request, error: str | None = None, msg: str | None = None):
    return page(request, "programs.html", programs=db.list_programs(request.app.state.db), error=error, msg=msg)


@app.post("/programs")
async def add_program(request: Request, name: str = Form(), resolution: list[str] = Form([])):
    try:
        db.add_program(request.app.state.db, name, resolution)
    except ValueError as e:
        return go("/programs", str(e))
    return go("/programs", msg="added")


@app.post("/programs/{id}/resolution")
async def program_resolution(request: Request, id: int, resolution: list[str] = Form([])):
    try:
        db.set_program_resolution(request.app.state.db, id, resolution)
    except ValueError as e:
        return go("/programs", str(e))
    return go("/programs", msg="resolution")


@app.post("/programs/{id}/enabled")
async def program_enabled(request: Request, id: int, enabled: str = Form()):
    db.set_program_enabled(request.app.state.db, id, enabled == "1")
    return go("/programs")


@app.post("/programs/{id}/delete")
async def program_delete(request: Request, id: int):
    db.delete_program(request.app.state.db, id)
    return go("/programs")


# sites
@app.get("/sites")
async def sites(request: Request, error: str | None = None, msg: str | None = None):
    conn = request.app.state.db
    return page(request, "sites.html", sites=db.list_sites(conn), reports=db.latest_doctor_report_per_site(conn),
                doctor_model=db.get_setting(conn, "doctor_model", "sonnet"), error=error, msg=msg)


@app.post("/sites")
async def site_add(request: Request, name: str = Form(), url: str = Form(),
                   boards: str = Form(""), pages: int = Form(3)):
    try:
        db.add_site(request.app.state.db, name, url, boards.splitlines(), pages)
    except ValueError as e:
        return go("/sites", str(e))
    return go("/sites", msg="site-added")


@app.post("/sites/{id}/delete")
async def site_delete(request: Request, id: int):
    conn = request.app.state.db
    site = db.get_site(conn, id)
    if not site:
        raise HTTPException(404, "사이트를 찾을 수 없습니다.")
    if request.app.state.scheduler.running:
        return go("/sites", "크롤링 실행 중에는 사이트를 삭제할 수 없습니다.")
    report = db.latest_doctor_report_per_site(conn).get(id)
    if report and report["status"] == "running":
        return go("/sites", "진단 실행 중에는 사이트를 삭제할 수 없습니다.")
    db.delete_site(conn, id)
    return go("/sites", msg="site-deleted")


@app.post("/sites/{id}/enabled")
async def site_enabled(request: Request, id: int, enabled: str = Form()):
    db.update_site(request.app.state.db, id, enabled=enabled == "1")
    return go("/sites")


@app.post("/sites/{id}")
async def site_edit(request: Request, id: int, url: str = Form(), boards: str = Form(""), pages: int = Form(3)):
    try:
        url, lines = db.clean_site_fields(url, boards.splitlines())
        if not 1 <= pages <= 10:
            raise ValueError("읽을 페이지 수는 1~10입니다")
    except ValueError as e:
        return go("/sites", str(e))
    db.update_site(request.app.state.db, id, url=url, boards=lines, pages=pages)
    return go("/sites", msg="applied")
