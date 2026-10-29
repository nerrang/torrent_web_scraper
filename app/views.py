"""The one Jinja environment both main.py and doctor.py render with: globals, filters, redirect helper."""
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app import config
from app.db import RESOLUTIONS

LABEL = {"ok": "정상", "partial": "일부 실패", "error": "실패", "running": "실행 중", "stopped": "중지됨", "done": "완료",
         "domain_moved": "도메인 이전", "blocked": "차단", "site_down": "사이트 다운", "transient": "일시 장애",
         "cloudflare": "CF 인증 필요", "parser_broken": "파서 고장"}
# ?msg= flash texts: (flash-<kind>, text); {n} is filled by the page (e.g. interval minutes).
FLASH = {"started": ("info", "실행을 시작했습니다. 끝나면 결과가 여기에 나옵니다."),
         "already-running": ("info", "이미 실행 중입니다."),
         "run-stop-requested": ("info", "실행 중지를 요청했습니다. 현재 작업이 끝나면 멈춥니다."),
         "run-not-running": ("info", "실행 중인 작업이 없습니다."),
         "site-added": ("ok", "사이트를 추가했습니다. 확인 후 켜기를 눌러 크롤링 대상에 포함하세요."),
         "site-deleted": ("ok", "사이트와 해당 진단 기록을 삭제했습니다."),
         "mode-auto": ("ok", "자동 실행으로 변경했습니다. 설정한 주기마다 크롤링합니다."),
         "mode-manual": ("ok", "수동 실행으로 변경했습니다. 지금 실행을 눌렀을 때만 크롤링합니다."),
         "interval": ("ok", "자동 실행 주기를 변경했습니다."),
         "added": ("ok", "프로그램을 추가했습니다. 맨 아래, 가장 나중 순서에 들어갑니다."),
         "resolution": ("ok", "해상도를 바꿨습니다."),
         "model": ("ok", "진단 모델을 바꿨습니다."),
         "applied": ("ok", "설정을 바꿨습니다. 다음 실행에서 확인됩니다."),
         "runs-cleared": ("ok", "최근 실행 목록을 비웠습니다. 실행 로그와 자동 실행 주기는 유지됩니다."),
         "download-history-deleted": ("ok", "다운로드 이력을 DB에서 삭제했습니다."),
         "downloads-restored": ("ok", "기존 다운로드 이력을 다시 표시합니다."),
         "downloads-cleared": ("ok", "다운로드 목록을 비웠습니다. Transmission 항목과 파일은 그대로입니다."),
         "magnet-added": ("ok", "마그넷을 일시 정지 상태로 추가했습니다. 대시보드에서 확인 후 재실행을 누르면 다운로드가 시작됩니다."),
         "magnet-duplicate": ("info", "이미 Transmission에 있는 마그넷입니다. 다운로드 목록에서 상태를 확인하세요."),
         "download-stopped": ("ok", "Transmission에서 다운로드를 중지했습니다."),
         "download-started": ("ok", "Transmission에서 다운로드를 다시 시작했습니다.")}


def _d(v) -> datetime | None:
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def dt(v, now: datetime | None = None) -> str:
    if not (d := _d(v)):
        return "-"
    now = now or datetime.now()
    fmt = "%m-%d %H:%M" if d.year == now.year else "%Y-%m-%d %H:%M"
    iso = escape(d.isoformat(timespec="seconds"))
    return Markup(f'<time datetime="{iso}" title="{iso}">{d:{fmt}}</time>')


def ago(v, now: datetime | None = None) -> str:
    if not (d := _d(v)):
        return "-"
    s = int(((now or datetime.now()) - d).total_seconds())
    n, tail = abs(s), "전" if s >= 0 else "후"
    if n < 60:
        return "방금"
    for unit, size in (("일", 86400), ("시간", 3600), ("분", 60)):
        if n >= size:
            return f"{n // size}{unit} {tail}"


def dur(start, end) -> str:
    if not end:
        return "실행 중"
    m, s = divmod(int((_d(end) - _d(start)).total_seconds()), 60)
    return f"{m}분 {s}초" if m else f"{s}초"


BAD_LINE = re.compile(r"\b(FAIL|ERROR|error|exception|failed)\b")


def logmark(text: str | None) -> Markup:
    """Escape a log and wrap failure lines in <mark class="log-bad">."""
    return Markup("\n".join(Markup('<mark class="log-bad">%s</mark>') % line if BAD_LINE.search(line) else escape(line)
                             for line in (text or "").splitlines()))


HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
CSS_V = int((HERE / "static" / "app.css").stat().st_mtime)  # cache-buster; uvicorn --reload watches *.py only
templates.env.globals.update(APP_ENV=config.APP_ENV, LABEL=LABEL, FLASH=FLASH, CSS_V=CSS_V,
                             RESOLUTIONS=RESOLUTIONS)
templates.env.filters.update(dt=dt, ago=ago, dur=dur, logmark=logmark)
templates.env.policies["json.dumps_kwargs"] = {"ensure_ascii": False}  # readable confirm() text


def go(path: str, error: str | None = None, msg: str | None = None) -> RedirectResponse:
    q = urlencode({k: v for k, v in (("error", error), ("msg", msg)) if v})
    return RedirectResponse(path + (f"?{q}" if q else ""), status_code=303)
