"""Unified FastAPI server for SRT + KTX macros.

- /api/srt/*  → SRT macro (SRTrain, NetFunnel recovery)
- /api/ktx/*  → KTX macro (srtgo + Dynapath bypass, anti-bot recovery)
- Both run independently in the same process; jobs from each side
  share nothing (separate JobManagers, separate Keychain entries).

Listens on 127.0.0.1:8912 (separate from the standalone 8910/8911).
"""
from __future__ import annotations

import sys
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

import card_test
import config
import schedule_cache
import srt_worker
import ktx_worker

# PyInstaller onefile로 묶이면 정적 파일은 임시 추출 경로(_MEIPASS)에 풀린다.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))

# 버전은 2.x = SRT+KTX 통합 GUI 세대. 2.1.0 중복예매 방지(계정 예약/발권
# 이력 사전검사 + 활성 잡 이중등록 차단), 2.1.1 결제 기본값 자동(auto),
# 2.2.0 시간표/환승 조회(구간별 조합, /api/*/timetable·transfer + lookup 폴링),
# 2.3.0 한달치 시간표 프리페치 캐시(/api/*/prefetch + /api/cache/*),
# 2.4.0 좌석 선호(창측 우선 + 맨앞/뒷열 회피, 잔여석 있을 때만 개선) +
#       중복예매 방어 3중 추가(예약 직전 재확인·직후 이력 스윕·10분 주기 재검사).
VERSION = "2.4.0"
DEVELOPER = "이치헌 (Chihun Lee)"
APP_NAME = "K-Rail Macro"

app = FastAPI(title=f"{APP_NAME} (SRT + KTX, 개인용)", version=VERSION)

# 원격(폰) 접속을 위해 K_RAIL_HOST=0.0.0.0 으로 바인딩하더라도, 계정·카드
# UI가 회사망/공용망에 노출되지 않도록 허용 대역 밖 접근은 전부 차단한다.
# 허용: 로컬호스트 + Tailscale 테일넷(CGNAT 100.64/10, ts IPv6) — WireGuard
# 암호화 사설망이라 평문 HTTP여도 안전하다.
_ALLOWED_NETS = (
    ip_network("127.0.0.0/8"),
    ip_network("::1/128"),
    ip_network("100.64.0.0/10"),          # Tailscale IPv4
    ip_network("fd7a:115c:a1e0::/48"),    # Tailscale IPv6
)


@app.middleware("http")
async def _restrict_to_local_and_tailnet(request: Request, call_next):
    try:
        client = ip_address(request.client.host)
    except Exception:
        return PlainTextResponse("forbidden", status_code=403)
    if not any(client in net for net in _ALLOWED_NETS):
        return PlainTextResponse("forbidden", status_code=403)
    return await call_next(request)


def _prevent_mac_sleep() -> None:
    """맥이 유휴 절전에 들어가 폴링이 통째로 멈추는 것을 방지.

    caffeinate -w 는 이 서버 프로세스가 살아있는 동안만 유휴/시스템 절전을
    억제하고 서버가 죽으면 스스로 종료된다. (뚜껑 닫힘 절전은 caffeinate로
    막을 수 없다 → 아래 lid guard가 pmset disablesleep으로 처리.)
    """
    if sys.platform != "darwin":
        return
    import os
    import subprocess
    try:
        subprocess.Popen(
            ["caffeinate", "-i", "-s", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _any_active_jobs() -> bool:
    for w in (srt_worker, ktx_worker):
        for j in w.manager.list():
            if not j._stop.is_set() and j.status in (
                w.JobStatus.PENDING, w.JobStatus.POLLING, w.JobStatus.RESERVED,
            ):
                return True
    return False


def _set_disablesleep(on: bool) -> bool:
    import subprocess
    r = subprocess.run(
        ["sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", "1" if on else "0"],
        capture_output=True, timeout=10,
    )
    return r.returncode == 0


def _lid_guard_loop() -> None:
    """뚜껑을 닫아도 활성 잡이 도는 동안은 맥이 잠들지 않게 한다.

    macOS에서 뚜껑 닫힘 절전을 막는 방법은 pmset disablesleep(root)뿐이다.
    setup_lid_mode.sh 로 passwordless sudo 규칙을 설치한 경우에만 동작하며,
    없으면 안내 한 번 남기고 건너뛴다. 배터리/발열을 아끼려고 '활성 잡이
    있는 동안만' 켜고 잡이 끝나면 자동으로 끈다.
    """
    import atexit
    import time as _time

    state = {"on": False}

    def _off_at_exit() -> None:
        if state["on"]:
            _set_disablesleep(False)

    atexit.register(_off_at_exit)

    warned = False
    first = True  # 크래시 후 재시작이면 실제 pmset 값이 남아있을 수 있어 1회 강제 동기화
    while True:
        want = _any_active_jobs()
        if first or want != state["on"]:
            first = False
            if _set_disablesleep(want):
                state["on"] = want
                print(f"[k-rail] 뚜껑 닫힘 절전 방지 {'ON (잡 실행 중)' if want else 'OFF (활성 잡 없음)'}", flush=True)
            elif not warned:
                warned = True
                print("[k-rail] 뚜껑 닫힘 절전 방지 불가 — setup_lid_mode.sh 를 한 번 실행하면 활성화됩니다", flush=True)
        _time.sleep(15)


@app.on_event("startup")
def _on_startup() -> None:
    _prevent_mac_sleep()
    if sys.platform == "darwin":
        import threading
        threading.Thread(target=_lid_guard_loop, daemon=True, name="lid-guard").start()
    # 이전 프로세스가 죽으며 남긴 활성 잡을 자동 복원 — 표 잡을 때까지 계속.
    n_srt = srt_worker.manager.restore()
    n_ktx = ktx_worker.manager.restore()
    if n_srt or n_ktx:
        print(f"[k-rail] 이전 세션 작업 자동 복원: SRT {n_srt}건, KTX {n_ktx}건", flush=True)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/meta")
def meta():
    return {"name": APP_NAME, "version": VERSION, "developer": DEVELOPER}


# ─── 비동기 조회(시간표/환승) 레지스트리 ────────────────────────────────
# 환승 조회는 로그인 + 구간별 검색 여러 번이라 수 초~수십 초 걸린다. HTTP를
# 오래 붙잡는 대신 즉시 query_id를 돌려주고 /api/lookup/{id}로 폴링하게 한다.
import itertools
import threading as _threading

_lookups: dict[str, dict] = {}
_lookup_lock = _threading.Lock()
_lookup_counter = itertools.count(1)


def _start_lookup(fn, /, *args, **kw) -> str:
    with _lookup_lock:
        qid = f"q{next(_lookup_counter)}"
        _lookups[qid] = {"status": "running", "result": None, "error": None}
        # 오래된 결과 정리(최근 20개만 유지)
        for k in list(_lookups)[:-20]:
            if _lookups[k]["status"] != "running":
                _lookups.pop(k, None)
    def run() -> None:
        try:
            r = fn(*args, **kw)
            _lookups[qid].update(status="done", result=r)
        except Exception as e:
            _lookups[qid].update(status="error", error=_safe_err(e))
    _threading.Thread(target=run, daemon=True, name=f"lookup-{qid}").start()
    return qid


@app.get("/api/lookup/{qid}")
def lookup_result(qid: str):
    entry = _lookups.get(qid)
    if entry is None:
        raise HTTPException(status_code=404, detail="query not found (만료됐거나 잘못된 id)")
    return {"query_id": qid, **entry}


# ─── 시간표 캐시 (한달치 사전 다운로드) ─────────────────────────────────
class PrefetchIn(BaseModel):
    days: int = Field(default=30, ge=1, le=32)
    routes: Optional[list[list[str]]] = None  # [[dep, arr], ...] 미지정 시 워커 기본 구간


@app.get("/api/cache/timetable")
def cache_timetable(svc: str, dep: str, arr: str, date: str):
    """캐시된 시간표 조회 (즉시 응답). 좌석 정보는 fetched_at 시점 값."""
    entry = schedule_cache.get(svc, dep, arr, date)
    if entry is None:
        raise HTTPException(status_code=404, detail="캐시 없음 — /api/{svc}/prefetch로 미리 받거나 라이브 조회 사용")
    return {"svc": svc, "dep": dep, "arr": arr, "date": date, **entry}


@app.get("/api/cache/status")
def cache_status():
    return schedule_cache.status()


app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def _normalize_expire(raw: str) -> tuple[str, bool]:
    """카드 유효기간을 YYMM으로 정규화한다.

    SRT(SRTrain)·KTX(srtgo) 둘 다 결제 시 YYMM(연-월)을 요구하는데, 사용자가
    카드 표면의 MM/YY 순서대로 MMYY로 넣는 실수가 잦아 결제가 실패한다.
    뒤 2자리가 월(01~12)이 아니고 앞 2자리가 월이면 명백한 MMYY이므로 두 쪽을
    뒤집어 YYMM으로 고친다. 앞뒤 둘 다 월로 해석 가능한 애매한 값은 건드리지 않는다.
    반환: (정규화값, 교정여부)
    """
    d = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(d) != 4:
        return raw, False
    front, back = d[:2], d[2:]
    front_is_month = 1 <= int(front) <= 12
    back_is_month = 1 <= int(back) <= 12
    if front_is_month and not back_is_month:   # 명백한 MMYY → 뒤집어 YYMM
        return back + front, True
    return d, False


# ─── SRT routes ─────────────────────────────────────────────────────────
srt_router = APIRouter(prefix="/api/srt")


class SRTCredsIn(BaseModel):
    srt_id: str
    srt_password: str
    card_number: str
    card_password: str
    card_validation: str
    card_expire: str
    card_type: str = "J"
    card_installment: int = 0


class SRTSearchIn(BaseModel):
    dep: str
    arr: str
    date: str
    time: str


class TimetableIn(BaseModel):
    dep: str
    arr: str
    date: str = Field(pattern=r"^\d{8}$")
    time: str = Field(default="000000", pattern=r"^\d{6}$")


class TransferIn(TimetableIn):
    vias: list[str] = Field(min_length=1, max_length=4)
    min_gap_min: int = Field(default=6, ge=0, le=120)
    limit: int = Field(default=10, ge=1, le=30)


class SRTJobIn(BaseModel):
    dep: str
    arr: str
    date: str = Field(pattern=r"^\d{8}$")
    time: str = Field(pattern=r"^\d{6}$")
    train_number: Optional[str] = None
    passengers: int = Field(ge=1, le=9, default=1)
    seat_pref: str = Field(default="general", pattern="^(general|special|any)$")
    # 기본 자동결제 — 카드정보가 Keychain에 저장돼 있어야 한다
    pay_mode: str = Field(default="auto", pattern="^(auto|manual)$")
    # 좌석 위치 선호(1인 예매만 적용) — 남은 자리가 그것뿐이면 포기하고 예매
    prefer_window: bool = True
    avoid_edge_rows: bool = True


@srt_router.get("/config/status")
def srt_config_status():
    return config.srt.public_status()


@srt_router.post("/config")
def srt_config_save(body: SRTCredsIn):
    expire, expire_corrected = _normalize_expire(body.card_expire)
    try:
        creds = config.SRTCredentials(
            srt_id=body.srt_id,
            srt_password=body.srt_password,
            card_number=body.card_number.replace("-", "").replace(" ", ""),
            card_password=body.card_password,
            card_validation=body.card_validation,
            card_expire=expire,
            card_type=body.card_type,
            card_installment=body.card_installment,
        )
    except ValidationError as e:
        msgs = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        raise HTTPException(status_code=422, detail="; ".join(msgs))
    config.srt.save(creds)
    out = config.srt.public_status()
    out["expire_corrected"], out["card_expire"] = expire_corrected, expire
    out["login_ok"], out["login_error"] = _srt_login_test(creds)
    return out


@srt_router.delete("/config")
def srt_config_delete():
    config.srt.clear()
    return {"ok": True}


@srt_router.get("/config/edit")
def srt_config_edit():
    c = config.srt.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    return {
        "srt_id": c.srt_id,
        "card_number": c.card_number,
        "card_validation": c.card_validation,
        "card_expire": c.card_expire,
        "card_type": c.card_type,
        "card_installment": c.card_installment,
    }


def _srt_login_test(creds: config.SRTCredentials) -> tuple[bool, Optional[str]]:
    from SRT import SRT
    try:
        SRT(creds.srt_id, creds.srt_password)
        return True, None
    except Exception as e:
        return False, str(e)[:200]


@srt_router.post("/config/test")
def srt_config_test():
    c = config.srt.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    ok, err = _srt_login_test(c)
    return {"login_ok": ok, "login_error": err}


@srt_router.post("/config/card-test")
def srt_card_test():
    c = config.srt.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    if not c.card_number:
        raise HTTPException(status_code=400, detail="카드 정보가 없습니다")
    # SRT는 reserve_info가 referer를 무시해 다른 표를 돌려줄 수 있어, 환불 전
    # PNR/노선/날짜 일치를 검증하고 보호표가 오면 즉시 중단한다(card_test.py의
    # 4겹 안전장치). 그래도 자동 환불이 실패하면 결제만 되고 수동 환불이 필요할
    # 수 있어 summary/steps에 그대로 노출한다.
    try:
        r = card_test.srt_card_test()
    except Exception as e:
        detail = _safe_err(e)
        return {
            "ok": False,
            "summary": f"카드 테스트 내부 오류: {detail}",
            "steps": [{"name": "error", "ok": False, "detail": detail}],
        }
    return {"ok": r.ok, "summary": r.summary, "steps": r.steps}


@srt_router.post("/timetable")
def srt_timetable(body: TimetableIn):
    """직행 시간표 비동기 조회 시작 → /api/lookup/{query_id} 폴링."""
    return {"query_id": _start_lookup(srt_worker.timetable, body.dep, body.arr, body.date, body.time)}


@srt_router.post("/transfer")
def srt_transfer(body: TransferIn):
    """직행+환승(구간별 조합) 비동기 조회 시작 → /api/lookup/{query_id} 폴링."""
    return {"query_id": _start_lookup(
        srt_worker.transfer_search, body.dep, body.arr, body.date, body.time,
        body.vias, body.min_gap_min, body.limit,
    )}


@srt_router.post("/prefetch")
def srt_prefetch(body: PrefetchIn):
    """한달치 시간표 사전 다운로드 시작(수 분~수십 분) → /api/lookup/{id} 폴링."""
    return {"query_id": _start_lookup(srt_worker.prefetch_timetables, body.routes, body.days)}


@srt_router.post("/search")
def srt_search(body: SRTSearchIn):
    try:
        return {"trains": srt_worker.search_preview(body.dep, body.arr, body.date, body.time)}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=_safe_err(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SRT 조회 실패: {_safe_err(e)}")


def _safe_err(e: Exception) -> str:
    """Some exception classes (e.g. requests.ConnectTimeout) have buggy
    __str__ that raises TypeError. Use repr as a guaranteed string."""
    try:
        s = str(e)
        if not isinstance(s, str):
            raise TypeError
        return s or f"{type(e).__name__}"
    except Exception:
        return f"{type(e).__name__}: {e!r}"


def _srt_to_dict(j: srt_worker.Job) -> dict:
    return {
        "id": j.id, "status": j.status,
        "spec": {
            "dep": j.spec.dep, "arr": j.spec.arr,
            "date": j.spec.date, "time": j.spec.time,
            "train_number": j.spec.train_number,
            "passengers": j.spec.passengers,
            "seat_pref": j.spec.seat_pref,
            "pay_mode": j.spec.pay_mode,
            "prefer_window": j.spec.prefer_window,
            "avoid_edge_rows": j.spec.avoid_edge_rows,
        },
        "created_at": j.created_at,
        "attempts": j.attempts,
        "recoveries": j.recoveries,
        "reservation": j.reservation_summary,
        "payment_deadline": j.payment_deadline,
        "error": j.error,
    }


@srt_router.get("/jobs")
def srt_jobs_list():
    return {"jobs": [_srt_to_dict(j) for j in srt_worker.manager.list()]}


@srt_router.post("/jobs")
def srt_jobs_create(body: SRTJobIn):
    if not config.srt.exists():
        raise HTTPException(status_code=400, detail="SRT 자격증명을 먼저 저장해주세요")
    spec = srt_worker.JobSpec(
        dep=body.dep, arr=body.arr, date=body.date, time=body.time,
        train_number=body.train_number, passengers=body.passengers,
        seat_pref=body.seat_pref, pay_mode=srt_worker.PayMode(body.pay_mode),
        prefer_window=body.prefer_window, avoid_edge_rows=body.avoid_edge_rows,
    )
    dup = srt_worker.manager.find_active_duplicate(spec)
    if dup:
        raise HTTPException(
            status_code=409,
            detail=f"같은 구간·날짜의 활성 작업이 이미 있습니다: {dup.id} ({dup.status}) — 중복예매 방지",
        )
    return _srt_to_dict(srt_worker.manager.create(spec))


@srt_router.delete("/jobs/{job_id}")
def srt_jobs_stop(job_id: str):
    if not srt_worker.manager.stop(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True}


@srt_router.post("/jobs/{job_id}/pay")
def srt_jobs_confirm_pay(job_id: str):
    if not srt_worker.manager.confirm_pay(job_id):
        raise HTTPException(status_code=400, detail="job not in RESERVED state")
    return {"ok": True}


@srt_router.get("/jobs/{job_id}/log")
def srt_jobs_log(job_id: str, since: int = 0):
    job = srt_worker.manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    lines = list(job.logs)
    return {"lines": lines[since:], "next": len(lines), "status": job.status}


# ─── KTX routes ─────────────────────────────────────────────────────────
ktx_router = APIRouter(prefix="/api/ktx")


class KTXCredsIn(BaseModel):
    ktx_id: str
    ktx_password: str
    card_number: str = ""
    card_password: str = ""
    card_validation: str = ""
    card_expire: str = ""
    card_installment: int = 0


class KTXSearchIn(BaseModel):
    dep: str
    arr: str
    date: str
    time: str
    train_type: str = "ktx"


class KTXJobIn(BaseModel):
    dep: str
    arr: str
    date: str = Field(pattern=r"^\d{8}$")
    time: str = Field(pattern=r"^\d{6}$")
    train_id: Optional[str] = None
    train_type: str = "ktx"
    passengers: int = Field(ge=1, le=9, default=1)
    seat_pref: str = Field(default="general", pattern="^(general|special|any)$")
    # 기본 자동결제 — 카드정보가 Keychain에 저장돼 있어야 한다
    pay_mode: str = Field(default="auto", pattern="^(auto|manual)$")
    include_waiting: bool = False
    # 좌석 위치 선호(1인 예매만 적용) — 남은 자리가 그것뿐이면 포기하고 예매
    prefer_window: bool = True
    avoid_edge_rows: bool = True


@ktx_router.get("/config/status")
def ktx_config_status():
    return config.ktx.public_status()


@ktx_router.post("/config")
def ktx_config_save(body: KTXCredsIn):
    expire, expire_corrected = _normalize_expire(body.card_expire)
    try:
        creds = config.KTXCredentials(
            ktx_id=body.ktx_id,
            ktx_password=body.ktx_password,
            card_number=body.card_number.replace("-", "").replace(" ", ""),
            card_password=body.card_password,
            card_validation=body.card_validation,
            card_expire=expire,
            card_installment=body.card_installment,
        )
    except ValidationError as e:
        msgs = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        raise HTTPException(status_code=422, detail="; ".join(msgs))
    config.ktx.save(creds)
    out = config.ktx.public_status()
    out["expire_corrected"], out["card_expire"] = expire_corrected, expire
    out["login_ok"], out["login_error"], out["login_name"] = _ktx_login_test(creds)
    return out


@ktx_router.delete("/config")
def ktx_config_delete():
    config.ktx.clear()
    return {"ok": True}


@ktx_router.get("/config/edit")
def ktx_config_edit():
    c = config.ktx.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    return {
        "ktx_id": c.ktx_id,
        "card_number": c.card_number,
        "card_validation": c.card_validation,
        "card_expire": c.card_expire,
        "card_installment": c.card_installment,
    }


def _ktx_login_test(creds: config.KTXCredentials) -> tuple[bool, Optional[str], Optional[str]]:
    from ktx_korail import PatchedKorail
    try:
        c = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
        if c.login():
            return True, None, getattr(c, "name", None)
        return False, "login returned False (잘못된 아이디/비밀번호)", None
    except Exception as e:
        return False, str(e)[:200], None


@ktx_router.post("/config/test")
def ktx_config_test():
    c = config.ktx.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    ok, err, name = _ktx_login_test(c)
    return {"login_ok": ok, "login_error": err, "login_name": name}


@ktx_router.post("/config/card-test")
def ktx_card_test():
    c = config.ktx.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    if not c.card_number:
        raise HTTPException(status_code=400, detail="카드 정보가 없습니다")
    # 카드 테스트 중 예기치 못한 예외는 불투명한 500("인터널 에러") 대신
    # 실제 원인을 화면에 보여줘 진단 가능하게 한다.
    try:
        r = card_test.ktx_card_test()
    except Exception as e:
        detail = _safe_err(e)
        return {
            "ok": False,
            "summary": f"카드 테스트 내부 오류: {detail}",
            "steps": [{"name": "error", "ok": False, "detail": detail}],
        }
    return {"ok": r.ok, "summary": r.summary, "steps": r.steps}


class KTXTimetableIn(TimetableIn):
    train_type: str = "ktx"


class KTXTransferIn(TransferIn):
    train_type: str = "ktx"


@ktx_router.post("/timetable")
def ktx_timetable(body: KTXTimetableIn):
    """직행 시간표 비동기 조회 시작 → /api/lookup/{query_id} 폴링."""
    return {"query_id": _start_lookup(
        ktx_worker.timetable, body.dep, body.arr, body.date, body.time, body.train_type,
    )}


@ktx_router.post("/transfer")
def ktx_transfer(body: KTXTransferIn):
    """직행+환승(구간별 조합) 비동기 조회 시작 → /api/lookup/{query_id} 폴링."""
    return {"query_id": _start_lookup(
        ktx_worker.transfer_search, body.dep, body.arr, body.date, body.time,
        body.vias, body.min_gap_min, body.limit, body.train_type,
    )}


@ktx_router.post("/prefetch")
def ktx_prefetch(body: PrefetchIn):
    """한달치 시간표 사전 다운로드 시작(수 분~수십 분) → /api/lookup/{id} 폴링."""
    return {"query_id": _start_lookup(ktx_worker.prefetch_timetables, body.routes, body.days)}


@ktx_router.post("/search")
def ktx_search(body: KTXSearchIn):
    try:
        return {"trains": ktx_worker.search_preview(body.dep, body.arr, body.date, body.time, body.train_type)}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=_safe_err(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"KTX 조회 실패: {_safe_err(e)}")


def _ktx_to_dict(j: ktx_worker.Job) -> dict:
    return {
        "id": j.id, "status": j.status,
        "spec": {
            "dep": j.spec.dep, "arr": j.spec.arr,
            "date": j.spec.date, "time": j.spec.time,
            "train_id": j.spec.train_id,
            "train_type": j.spec.train_type,
            "passengers": j.spec.passengers,
            "seat_pref": j.spec.seat_pref,
            "pay_mode": j.spec.pay_mode,
            "include_waiting": j.spec.include_waiting,
            "prefer_window": j.spec.prefer_window,
            "avoid_edge_rows": j.spec.avoid_edge_rows,
        },
        "created_at": j.created_at,
        "attempts": j.attempts,
        "recoveries": j.recoveries,
        "reservation": j.reservation_summary,
        "reservation_id": j.reservation_id,
        "payment_deadline": j.payment_deadline,
        "error": j.error,
    }


@ktx_router.get("/jobs")
def ktx_jobs_list():
    return {"jobs": [_ktx_to_dict(j) for j in ktx_worker.manager.list()]}


@ktx_router.post("/jobs")
def ktx_jobs_create(body: KTXJobIn):
    if not config.ktx.exists():
        raise HTTPException(status_code=400, detail="KTX 자격증명을 먼저 저장해주세요")
    creds = config.ktx.load()
    if body.pay_mode == "auto" and (not creds or not creds.card_number):
        raise HTTPException(status_code=400, detail="자동 결제 모드는 카드정보 저장이 필요합니다")
    spec = ktx_worker.JobSpec(
        dep=body.dep, arr=body.arr, date=body.date, time=body.time,
        train_id=body.train_id, train_type=body.train_type,
        passengers=body.passengers, seat_pref=body.seat_pref,
        pay_mode=ktx_worker.PayMode(body.pay_mode),
        include_waiting=body.include_waiting,
        prefer_window=body.prefer_window, avoid_edge_rows=body.avoid_edge_rows,
    )
    dup = ktx_worker.manager.find_active_duplicate(spec)
    if dup:
        raise HTTPException(
            status_code=409,
            detail=f"같은 구간·날짜의 활성 작업이 이미 있습니다: {dup.id} ({dup.status}) — 중복예매 방지",
        )
    return _ktx_to_dict(ktx_worker.manager.create(spec))


@ktx_router.delete("/jobs/{job_id}")
def ktx_jobs_stop(job_id: str):
    if not ktx_worker.manager.stop(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True}


@ktx_router.post("/jobs/{job_id}/pay")
def ktx_jobs_confirm_pay(job_id: str):
    if not ktx_worker.manager.confirm_pay(job_id):
        raise HTTPException(status_code=400, detail="job not in RESERVED state")
    return {"ok": True}


@ktx_router.get("/jobs/{job_id}/log")
def ktx_jobs_log(job_id: str, since: int = 0):
    job = ktx_worker.manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    lines = list(job.logs)
    return {"lines": lines[since:], "next": len(lines), "status": job.status}


app.include_router(srt_router)
app.include_router(ktx_router)


def _another_instance_running() -> bool:
    """이미 K-Rail 서버가 응답 중인지 확인한다(이중 실행 방지).

    0.0.0.0 바인딩(launchd 상주)과 127.0.0.1 바인딩(앱 실행)은 포트 충돌 없이
    동시에 뜰 수 있다 — 그러면 서버 2개가 각자 jobs.json을 복원해 같은 표를
    두 번 예매할 수 있다(실제 발생). 그래서 기동 전에 기존 서버 응답을 확인한다.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8912/api/srt/config/status", timeout=3
        ) as r:
            return r.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    import os

    import uvicorn

    if _another_instance_running():
        print("[k-rail] 이미 실행 중인 서버 감지(127.0.0.1:8912) → 이중 실행(중복예매 위험) 방지, 종료", flush=True)
        sys.exit(0)

    # 기본은 로컬 전용. 폰(테일넷) 접속용 상주 세팅(setup_remote.sh)이
    # K_RAIL_HOST=0.0.0.0 을 넣어주면 위 미들웨어가 접근 대역을 제한한다.
    host = os.environ.get("K_RAIL_HOST", "127.0.0.1")
    uvicorn.run("server:app", host=host, port=8912, reload=False)
