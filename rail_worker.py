"""코레일 단일 엔진 — 조회·폴링·예약·결제 워커 (v3.0).

2026-09 코레일·SR 발매 통합으로 코레일 계정 하나로 KTX와 SRT를 모두 조회·
예약·결제할 수 있게 됐다(실측: 수서~동대구 SRT 335편 예약→취소 성공). 그래서
v3.0부터 SR 전용 엔진(srt_worker)을 걷어내고 이 모듈 하나만 쓴다.

주의 — 코레일 조회 API는 한 번에 ~10편만 준다. 하루치를 보려면 출발시각을
밀어가며 여러 번 불러야 한다(`search_all`). 이걸 안 하면 늦은 시각 열차가
목록에서 통째로 빠져 "잡을 수 없는 열차"가 된다.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Deque, Optional

from srtgo.ktx import (
    AdultPassenger,
    KorailError,
    NeedToLoginError,
    NoResultsError,
    ReserveOption,
    SoldOutError,
    TrainType,
)

import config
import jobstore
import schedule_cache
import seatpref
import timetable as tt
from korail_client import PatchedKorail
from recovery import RecoveryController

# 정상 폴링 간격(랜덤). 속도제한을 덜 건드리도록 보수적으로 잡는다(안정성 우선 1.5배).
MIN_INTERVAL = 3.0
MAX_INTERVAL = 90.0
# 세션을 만료 전에 미리 갱신해 만료발(發) 오류를 예방한다(선제 재로그인).
SESSION_MAX_AGE = 600.0
# 정상 검색이 이 시간 이상 끊기면 세션이 꼬인 것으로 보고 강제로 새 세션을 만든다.
STALL_LIMIT = 240.0
LOG_LIMIT = 500
# 감시자(watchdog): 활성 잡의 스레드가 죽거나 멈추면 자동 재시작한다.
WATCHDOG_PERIOD = 30.0
# 최대 정상 sleep(90s) + HTTP 타임아웃(25s) 몇 번을 크게 웃도는 값 — 이보다
# 오래 심장박동이 없으면 스레드가 되살아날 수 없는 상태로 멈춘 것으로 본다.
HEARTBEAT_STALE = 480.0
# 중복예매 사전검사(기존 발권/예약 조회) 실패 시 재시도 간격 배수(초)
DEDUP_RETRY_BASE = 3.0
# 폴링 중에도 이 주기로 계정 이력을 재검사한다 — 수동 예매·타 세션 예매를
# 표 잡기 전에 감지해 중복예매를 막는다(사전검사 1회로는 긴 폴링을 못 지킨다).
DEDUP_RECHECK_PERIOD = 600.0
# 좌석 개선(취소→즉시 재예약) 최대 시도 횟수 — 남은 좌석이 확인될 때만 시도
SEAT_IMPROVE_MAX = 2
# 코레일 좌석위치속성코드 (txtSeatAttCd2): 000 무관 / 012 창측 / 013 내측
SEAT_LOC_ANY = "000"
SEAT_LOC_WINDOW = "012"
# 특정 열차 지정 잡에서 '당일인데 대상 열차가 조회 안 됨'이 이 횟수 연속되면
# 출발이 지난 것으로 보고 자동 종료한다(죽은 잡이 API만 두드리는 것 방지).
DEAD_TARGET_MISS_LIMIT = 20


def _safe_err(e: BaseException) -> str:
    """예외를 안전하게 문자열로 변환한다.

    일부 라이브러리는 네트워크 오류를 예외객체(문자열 아님)로 .msg에 감싸 던져,
    예외의 __str__가 비문자열을 반환해 `str(e)` 호출 자체가 TypeError를 던지는
    경우가 있다(폴링 스레드 사망). SRT 워커와 동일하게 안전 변환으로 막는다.
    """
    msg = getattr(e, "msg", None)
    if msg is not None and not isinstance(msg, str):
        return f"{type(e).__name__}: {msg!r}"
    try:
        return str(e)
    except Exception:
        return repr(e)


class JobStatus(str, Enum):
    PENDING = "pending"
    POLLING = "polling"
    RESERVED = "reserved"
    PAID = "paid"
    STOPPED = "stopped"
    ERROR = "error"


class PayMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


# 통합 후 기본값은 "all" — KTX·SRT를 한 목록에서 본다.
TRAIN_TYPE_MAP = {
    "all": TrainType.ALL,
    "ktx": TrainType.KTX,
    "itx-saemaeul": TrainType.ITX_SAEMAEUL,
    "mugunghwa": TrainType.MUGUNGHWA,
    "nuriro": TrainType.NURIRO,
    "tonggeun": TrainType.TONGGUEN,
    "itx-cheongchun": TrainType.ITX_CHEONGCHUN,
    "all": TrainType.ALL,
}


def _train_id(t) -> str:
    """Stable selector: train_type + train_no + dep_date."""
    return f"{t.train_type}|{t.train_no}|{t.dep_date}"


def operator(train_number) -> str:
    """운영사 표기 — 발매는 통합됐지만 사용자는 여전히 SRT/KTX로 구분해 부른다.

    통합 후 코레일 응답의 차종코드(0A/07/00/10)는 차량형식이라 운영사를 알 수
    없다. SRT 영업열차만 세 자리 3xx(경부계통)·6xx(호남계통) 번호를 쓰므로
    번호로 가른다. 그 외는 열차 이름(KTX/ITX-새마을/무궁화호…)을 그대로 쓴다.
    """
    n = str(train_number or "").strip()
    return "SRT" if (len(n) == 3 and n.isdigit() and n[0] in ("3", "6")) else ""


@dataclass
class JobSpec:
    dep: str
    arr: str
    date: str
    time: str
    # 열차 지정은 '열차번호'가 1차 선택자다. train_id(차종코드|번호|날짜)는
    # 차종코드가 편성에 따라 바뀌어(0A/07/00/10) 매칭이 깨진 적이 있어 보조용.
    train_number: Optional[str]
    train_id: Optional[str]
    train_type: str
    passengers: int
    seat_pref: str  # "general" | "special" | "any"
    pay_mode: PayMode
    include_waiting: bool = False
    # 좌석 위치 선호 — 1인 예매일 때만 적용(다인은 나란한 좌석이 더 중요).
    # 남은 좌석이 그것뿐이면 선호를 포기하고 그대로 예매한다.
    prefer_window: bool = True     # 창측 우선
    avoid_edge_rows: bool = True   # 호차 맨앞/맨뒷열 회피


def _same_train_no(a, b) -> bool:
    """열차번호 비교 — 검색('123')과 내역('00123')의 0-패딩 차이를 흡수한다."""
    try:
        return int(str(a)) == int(str(b))
    except (TypeError, ValueError):
        return str(a) == str(b)


def _spec_matches_entry(spec: JobSpec, t) -> bool:
    """코레일 발권(Ticket)/예약(Reservation) 내역이 이 잡과 '같은 표'인지 판정.

    같은 날짜·같은 구간이면 중복으로 본다. 잡이 특정 열차(train_id)를 지정한
    경우엔 그 열차번호와 일치할 때만 중복이다.
    """
    same_route = (
        getattr(t, "dep_name", None) == spec.dep
        and getattr(t, "arr_name", None) == spec.arr
        and getattr(t, "dep_date", None) == spec.date
    )
    if not same_route:
        return False
    if spec.train_id and "|" in spec.train_id:
        want_no = spec.train_id.split("|")[1]
        # 0-패딩 차이('123' vs '00123')로 중복을 놓치지 않도록 숫자 비교
        return _same_train_no(getattr(t, "train_no", None), want_no)
    return True


@dataclass
class Job:
    id: str
    spec: JobSpec
    status: JobStatus = JobStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    attempts: int = 0
    recoveries: int = 0
    reservation_summary: Optional[str] = None
    reservation_id: Optional[str] = None
    payment_deadline: Optional[str] = None
    error: Optional[str] = None
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    last_beat: float = field(default_factory=time.monotonic)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None
    _gen: int = 0  # 감시자 재시작 세대. 옛 스레드는 세대가 바뀌면 스스로 종료.
    _reservation: object = None
    _pay_event: threading.Event = field(default_factory=threading.Event)
    # 지정 열차를 한 번 찾으면 그 출발시각을 기억해, 다음 폴링부터는 거기서
    # 바로 조회한다 — 페이지를 다시 넘기지 않아 요청 수(=안티봇 부담)가 준다.
    _target_time: Optional[str] = None

    def log(self, msg: str) -> None:
        self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def beat(self) -> None:
        self.last_beat = time.monotonic()


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._watchdog: Optional[threading.Thread] = None

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def create(self, spec: JobSpec, persist: bool = True) -> Job:
        with self._lock:
            self._counter += 1
            jid = f"k{self._counter}"
        job = Job(id=jid, spec=spec)
        self._jobs[jid] = job
        self._ensure_watchdog()
        self._spawn(job)
        if persist:
            self._persist()
        return job

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job._stop.set()
        job._pay_event.set()
        self._persist()
        return True

    def _spawn(self, job: Job) -> None:
        job._gen += 1
        job.beat()
        t = threading.Thread(
            target=self._run, args=(job, job._gen), daemon=True,
            name=f"ktx-{job.id}-g{job._gen}",
        )
        job._thread = t
        t.start()

    def _ensure_watchdog(self) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog = threading.Thread(target=self._watch, daemon=True, name="ktx-watchdog")
        self._watchdog.start()

    def _watch(self) -> None:
        """스레드가 죽었거나(예외) 멈춘(심장박동 정지) 활성 잡을 자동 재시작한다.

        세대 토큰(_gen)을 올리고 새 스레드를 띄우면 옛 스레드는 다음 루프에서
        세대 불일치를 보고 스스로 빠진다. 진짜로 시스템콜에 갇힌 스레드는 죽일
        수 없지만, 모든 HTTP 호출에 타임아웃이 강제돼 있어 결국 풀려나 종료된다.
        """
        while True:
            time.sleep(WATCHDOG_PERIOD)
            for job in self.list():
                if job._stop.is_set() or job.status not in (JobStatus.PENDING, JobStatus.POLLING):
                    continue
                dead = job._thread is None or not job._thread.is_alive()
                stuck = time.monotonic() - job.last_beat > HEARTBEAT_STALE
                if dead or stuck:
                    job.recoveries += 1
                    job.log(f"감시자: {'스레드 사망' if dead else '응답 없음(멈춤)'} 감지 → 자동 재시작")
                    self._spawn(job)

    def _persist(self) -> None:
        active = [
            asdict(j.spec) for j in self.list()
            if not j._stop.is_set()
            and j.status in (JobStatus.PENDING, JobStatus.POLLING, JobStatus.RESERVED)
        ]
        jobstore.save("rail", active)

    def restore(self) -> int:
        """이전 서버 프로세스가 남긴 활성 잡을 되살린다(서버 시작 시).

        같은 프로세스에서 서버가 재기동돼 이미 동일 잡이 돌고 있으면
        건너뛴다(중복 폴링/중복 예매 방지).
        """
        existing = [
            asdict(j.spec) for j in self.list()
            if not j._stop.is_set()
            and j.status in (JobStatus.PENDING, JobStatus.POLLING, JobStatus.RESERVED)
        ]
        n = 0
        # v3.0 이전 잡(kind="ktx", train_id만 있음)도 함께 되살린다.
        saved = jobstore.load("rail") or jobstore.load("ktx")
        for d in saved:
            if d in existing:
                continue
            try:
                d = dict(d)
                d["pay_mode"] = PayMode(d["pay_mode"])
                d.setdefault("train_number", None)
                if d["train_number"] is None and d.get("train_id"):
                    d["train_number"] = str(d["train_id"]).split("|")[1:2] or None
                    d["train_number"] = d["train_number"][0] if d["train_number"] else None
                job = self.create(JobSpec(**d), persist=False)
            except Exception:
                continue
            job.log("서버 재시작 감지 → 저장된 작업 자동 복원")
            n += 1
        self._persist()
        return n

    def confirm_pay(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != JobStatus.RESERVED:
            return False
        job._pay_event.set()
        return True

    def find_active_duplicate(self, spec: JobSpec) -> Optional[Job]:
        """같은 구간·날짜를 노리는 활성 잡을 찾는다(이중 등록 방지)."""
        for j in self.list():
            if j._stop.is_set() or j.status not in (
                JobStatus.PENDING, JobStatus.POLLING, JobStatus.RESERVED,
            ):
                continue
            s = j.spec
            if s.dep != spec.dep or s.arr != spec.arr or s.date != spec.date:
                continue
            # 두 잡 모두 서로 다른 특정 열차를 지정했으면 중복이 아니다
            if spec.train_id and s.train_id and spec.train_id != s.train_id:
                continue
            return j
        return None

    def _preflight_dedup(self, client: PatchedKorail, job: Job, creds: config.RailCredentials) -> Optional[bool]:
        """폴링 시작 전 코레일 계정의 발권/예약 내역을 확인해 중복예매를 막는다.

        서버 크래시·재시작으로 잡이 복원되거나 감시자가 스레드를 재기동할 때,
        이전 세션이 이미 잡아둔(결제까지 끝낸) 표를 모르고 같은 표를 또
        예매하는 사고를 여기서 차단한다.
        반환: None=이력 없음(정상 폴링 진행), True=잡 종료, False=폴링 루프 재시작.
        """
        tickets = reservations = None
        for attempt in range(3):
            try:
                tickets = client.tickets() or []
                reservations = client.reservations() or []
                break
            except Exception as e:
                job.log(f"기존 예약 확인 실패({attempt + 1}/3): {_safe_err(e)}")
                if job._stop.wait(DEDUP_RETRY_BASE * (attempt + 1)):
                    return None
        if tickets is None or reservations is None:
            job.log("⚠ 기존 예약 확인 불가 — 중복 검사 없이 폴링 시작(다음 세션에서 재검사)")
            return None
        paid = next((t for t in tickets if _spec_matches_entry(job.spec, t)), None)
        if paid is not None:
            job.reservation_summary = f"[기존 발권 표 감지] {paid}"
            job.status = JobStatus.PAID
            job.log(f"이미 발권(결제)된 같은 구간 표 발견 → 중복예매 방지, 작업 종료: {paid}")
            return True
        match = next((r for r in reservations if _spec_matches_entry(job.spec, r)), None)
        if match is None:
            return None
        # 미결제 예약이 살아있음 → 새로 예매하지 않고 그 예약을 이어받아 결제 흐름 재개
        job._reservation = match
        job.reservation_summary = f"[기존 예약 이어받음] {match}"
        job.reservation_id = getattr(match, "rsv_id", None)
        d = getattr(match, "buy_limit_date", None)
        t = getattr(match, "buy_limit_time", None)
        if d and t and d != "00000000":
            job.payment_deadline = f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
        job.status = JobStatus.RESERVED
        if getattr(match, "is_waiting", False):
            job.log(f"예약대기 내역 발견 → 재예매하지 않고 이어받음(좌석 배정 전엔 결제 불가): {match}")
        else:
            job.log(f"미결제 예약 발견 → 재예매 대신 이어받아 결제 단계로: {match}")
        if self._handle_payment(client, job, creds):
            return True
        # 결제확인 시간초과 → 예약이 서버측에서 취소됐는지 다시 봐야 하므로
        # 폴링으로 바로 가지 않고 루프를 재시작해 이 검사를 다시 거친다.
        job._pay_event.clear()
        job._reservation = None
        job.reservation_summary = None
        job.reservation_id = None
        job.payment_deadline = None
        job.status = JobStatus.POLLING
        return False

    def _run(self, job: Job, gen: int) -> None:
        creds = config.rail.load()
        if not creds:
            job.status = JobStatus.ERROR
            job.error = "credentials not configured"
            job.log("ERROR: credentials missing")
            self._persist()
            return

        def active() -> bool:
            return not job._stop.is_set() and job._gen == gen

        # 어떤 예외로 폴링 루프가 깨져도 표를 잡기 전에는 스레드가 죽지 않는다.
        # 종료는 사용자 정지, 결제 흐름 종료(성공/오류), 감시자 교체뿐이다.
        while active():
            try:
                if self._poll_loop(job, gen, creds, active):
                    break
            except Exception as e:
                job.recoveries += 1
                job.log(f"워커 오류 → 15s 후 자동 재시작: {_safe_err(e)}")
                job._stop.wait(15.0)

        if job._gen != gen:
            return  # 감시자가 새 스레드로 교체함 — 상태는 새 스레드가 관리
        if job.status in (JobStatus.PENDING, JobStatus.POLLING):
            job.status = JobStatus.STOPPED
            job.log("stopped")
        self._persist()

    def _poll_loop(self, job: Job, gen: int, creds: config.RailCredentials, active) -> bool:
        """폴링 본체. True면 작업 종료(결제 흐름 완료/정지), False면 재시작 대상."""

        # 출발일이 지난 잡은 의미가 없다 — 로그인/폴링 없이 즉시 종료
        if job.spec.date < datetime.now().strftime("%Y%m%d"):
            job.status = JobStatus.STOPPED
            job.error = "출발일 경과 — 자동 종료"
            job.log("출발일이 지나 작업을 자동 종료합니다")
            return True

        def _new_client() -> PatchedKorail:
            c = PatchedKorail(creds.rail_id, creds.rail_password, auto_login=False)
            # 로그인 호출에도 타임아웃이 걸리도록 로그인 전에 패치한다(없으면
            # 인터넷 불안정 시 로그인에서 스레드가 영원히 멈춤). 폴링 루프의
            # 모든 HTTP 호출에도 같은 타임아웃이 적용된다.
            _force_session_timeout(c._session, 25)
            if not c.login():
                raise RuntimeError("login returned False")
            return c

        # 로그인은 실패해도 포기하지 않는다(인터넷 불안정 대비, 표 잡을 때까지).
        login_rc = RecoveryController(base=5.0, cap=120.0, fresh_login_every=10 ** 9)
        client: Optional[PatchedKorail] = None
        while active() and client is None:
            job.beat()
            try:
                client = _new_client()
            except Exception as e:
                rec = login_rc.on_error()
                job.log(f"로그인 실패 #{rec.streak} → {rec.sleep:.0f}s 후 재시도: {_safe_err(e)}")
                job._stop.wait(rec.sleep)
        if client is None:
            return False  # 정지/교체 요청됨 → 상위에서 정리
        session_started = time.monotonic()

        job.log(
            f"login ok ({getattr(client, 'name', creds.rail_id)}); "
            f"polling {job.spec.dep}->{job.spec.arr} {job.spec.date} {job.spec.time} "
            f"type={job.spec.train_type}"
        )
        job.status = JobStatus.POLLING

        # 중복예매 방지: 이 계정에 같은 표의 발권/예약 이력이 있으면 여기서 끝낸다
        preflight = self._preflight_dedup(client, job, creds)
        if preflight is not None:
            return preflight
        last_dedup = time.monotonic()

        seat_option = self._seat_pref_to_option(job.spec.seat_pref)
        train_type = TRAIN_TYPE_MAP.get(job.spec.train_type.lower(), TrainType.KTX)
        passengers = [AdultPassenger(job.spec.passengers)]
        rc = RecoveryController()
        last_ok = time.monotonic()
        dead_misses = 0  # 당일+특정열차 잡에서 대상이 연속으로 조회 안 된 횟수

        def _count_dead_miss() -> bool:
            """당일 특정 열차가 계속 조회되지 않으면(출발 경과 추정) 잡을 접는다."""
            nonlocal dead_misses
            if not job.spec.train_id or job.spec.date != datetime.now().strftime("%Y%m%d"):
                return False
            dead_misses += 1
            if dead_misses < DEAD_TARGET_MISS_LIMIT:
                return False
            job.status = JobStatus.STOPPED
            job.error = "대상 열차가 더 이상 조회되지 않음(출발 경과 추정) — 자동 종료"
            job.log(job.error)
            return True

        def _handle_antibot(msg: str) -> float:
            """안티봇/속도제한 차단 처리. 연속 실패에 비례한 백오프를 돌려준다.
            빠른 재시도는 차단을 연장하므로 즉시 재시도하지 않는다. 일정 횟수마다
            완전 새 세션으로 재로그인한다."""
            nonlocal client, session_started
            rec = rc.on_error()
            job.recoveries += 1
            if rec.fresh_login:
                job.log(f"안티봇 차단 {rec.streak}회 연속 → 완전 새 세션 + {rec.sleep:.0f}s 대기")
                try:
                    client = _new_client()
                    session_started = time.monotonic()
                except Exception as e2:
                    job.log(f"새 세션 실패(대기 후 재시도): {_safe_err(e2)}")
            else:
                job.log(
                    f"안티봇 차단 #{rec.streak} (속도제한) → "
                    f"{rec.sleep:.0f}s 백오프 후 재시도: {msg[:60]}"
                )
            return rec.sleep

        while active():
            job.beat()
            job.attempts += 1
            next_sleep: Optional[float] = None

            # 선제 세션 갱신: 오래된 세션은 만료로 오류 나기 전에 미리 새로 로그인
            if time.monotonic() - session_started > SESSION_MAX_AGE:
                job.log("세션 선제 갱신(만료 예방) → 재로그인")
                try:
                    client = _new_client()
                    session_started = time.monotonic()
                except Exception as e:
                    job.log(f"선제 재로그인 실패: {_safe_err(e)}")

            # 방어③ 주기적 재검사: 긴 폴링 중 수동 예매나 다른 세션이 이미 같은
            # 표를 잡았을 수 있다 — 10분마다 계정 이력을 다시 확인한다.
            if time.monotonic() - last_dedup > DEDUP_RECHECK_PERIOD:
                last_dedup = time.monotonic()
                recheck = self._preflight_dedup(client, job, creds)
                if recheck is not None:
                    return recheck

            try:
                # 지정 열차가 조회 첫 페이지(~10편) 밖에 있을 수 있다 —
                # 찾을 때까지만 페이지를 넘기고, 찾으면 즉시 멈춘다. 한 번 찾은
                # 뒤로는 그 열차 출발시각부터 조회해 한 번에 끝낸다.
                want = job.spec.train_number
                start = job._target_time or job.spec.time
                trains = search_all(
                    client, job.spec.dep, job.spec.arr,
                    job.spec.date, start,
                    train_type=train_type,
                    include_waiting_list=job.spec.include_waiting,
                    max_pages=(2 if job._target_time else 10) if want else 1,
                    stop_when=(lambda t: _same_train_no(t.train_no, want)) if want else None,
                )
                rc.on_success()
                last_ok = time.monotonic()
                target = self._pick_target(trains, job.spec)
                if target is None:
                    job.log(f"#{job.attempts} target not found")
                    if _count_dead_miss():
                        return True
                else:
                    dead_misses = 0
                    if want:
                        job._target_time = getattr(target, "dep_time", None) or job._target_time
                    gen = target.has_general_seat()
                    spc = target.has_special_seat()
                    job.log(f"#{job.attempts} {target.train_no} general={gen} special={spc}")
                    if self._can_take(gen, spc, job.spec.seat_pref):
                        # 방어① 예약 직전 재확인: 감시자가 이 스레드를 교체했거나
                        # 정지된 뒤라면 절대 예약하지 않는다(구세대 스레드가 새
                        # 스레드와 같은 표를 또 잡는 사고 차단).
                        if not active():
                            break
                        res = self._reserve_with_pref(client, job, target, passengers, seat_option)
                        if res is not None:
                            # 방어② 예약 직후 계정 이력 스윕: 어떤 경로로든 같은
                            # 표가 2건이 됐으면 여기서 정리한다.
                            res, done = self._post_reserve_sweep(client, job, res)
                            if done:
                                return True
                            res = self._improve_seat(client, job, passengers, seat_option, res, active)
                            if res is None:
                                continue  # 개선 중 좌석을 놓침 → 폴링 재개
                            job._reservation = res
                            job.reservation_summary = str(res)
                            job.reservation_id = getattr(res, "rsv_id", None)
                            d = getattr(res, "buy_limit_date", None)
                            t = getattr(res, "buy_limit_time", None)
                            if d and t and d != "00000000":
                                job.payment_deadline = (
                                    f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
                                )
                            job.status = JobStatus.RESERVED
                            job.log(f"RESERVED: {res}")
                            job.log(f"deadline: {job.payment_deadline}")
                            if self._handle_payment(client, job, creds):
                                return True
                            # 결제확인 시간초과 → 코레일이 예약을 자동취소하므로
                            # 처음 상태로 되돌려 표잡기를 재개한다(잡을 때까지).
                            job._pay_event.clear()
                            job._reservation = None
                            job.reservation_summary = None
                            job.reservation_id = None
                            job.payment_deadline = None
                            job.status = JobStatus.POLLING
            except NoResultsError:
                job.log(f"#{job.attempts} no results")
                rc.on_success()
                last_ok = time.monotonic()
                # 당일 막차까지 끝나면 NoResults로만 응답한다 → 출발 경과로 간주
                if _count_dead_miss():
                    return True
            except NeedToLoginError:
                job.log("세션 만료 감지 → 재로그인")
                try:
                    client = _new_client()
                    session_started = time.monotonic()
                    last_ok = time.monotonic()
                    rc.on_success()
                except Exception as e:
                    job.log(f"재로그인 실패: {_safe_err(e)}")
            except KorailError as e:
                msg = _safe_err(e)
                if any(p in msg for p in ("MACRO", "원활한 서비스", "최신 버전")):
                    next_sleep = _handle_antibot(msg)
                else:
                    job.log(f"korail error: {msg[:120]}")
            except Exception as e:
                # str(e)가 비문자열 msg를 감싼 예외에서 TypeError를 던져 스레드가
                # 죽는 것을 막는다(SRT 워커와 동일 가드).
                job.log(f"poll error: {_safe_err(e)}")

            # 정상 검색이 너무 오래 끊기면 세션이 꼬인 것 → 강제로 새 세션
            if next_sleep is None and time.monotonic() - last_ok > STALL_LIMIT:
                job.log(f"검색 {int(STALL_LIMIT)}s+ 정체 → 강제 새 세션")
                job.recoveries += 1
                try:
                    client = _new_client()
                    session_started = time.monotonic()
                    last_ok = time.monotonic()
                    rc.on_success()
                except Exception as e:
                    job.log(f"강제 재로그인 실패: {_safe_err(e)}")

            sleep_for = next_sleep if next_sleep is not None else random.uniform(MIN_INTERVAL, MAX_INTERVAL)
            job.log(f"sleep {sleep_for:.1f}s")
            job.beat()
            if job._stop.wait(sleep_for):
                break

        return False  # 정지/교체 요청 → 상위에서 정리

    def _reserve_with_pref(self, client: PatchedKorail, job: Job, target, passengers, option):
        """예약 실행 — 1인 예매면 창측(좌석위치속성 012)을 먼저 요청하고 폴백.

        창측이 없으면 코레일이 오류를 주므로 위치 무관(000)으로 즉시 한 번 더
        시도한다 — '어쩔 수 없으면 아무 자리라도 잡는다'는 원칙.
        반환: 예약 성공 시 Reservation, 실패(매진 경쟁 패배 등) 시 None.
        예약대기 신청(좌석 없음)에는 좌석위치 요청이 의미가 없어 건너뛴다.
        """
        if job.spec.prefer_window and job.spec.passengers == 1 and target.has_seat():
            try:
                return client.reserve(
                    target, passengers=passengers, option=option,
                    seat_location=SEAT_LOC_WINDOW,
                )
            except (SoldOutError, KorailError) as e:
                job.log(f"창측 예약 실패 → 좌석위치 무관으로 즉시 재시도: {_safe_err(e)[:80]}")
        try:
            return client.reserve(target, passengers=passengers, option=option)
        except SoldOutError:
            job.log("reserve race lost (sold out)")
            return None
        except KorailError as e:
            job.log(f"reserve error: {_safe_err(e)}")
            return None

    def _post_reserve_sweep(self, client: PatchedKorail, job: Job, res):
        """예약 직후 계정 이력을 다시 조회해 같은 표 중복을 정리한다(사후 검증).

        스레드 경쟁·서버 재시작·수동 예매 등 어떤 경로로 중복이 생겼든 결제
        전에 여기서 잡는다: 이미 발권(결제)된 같은 표가 있으면 방금 예약을
        취소하고 그 표를 쓰고, 같은 열차의 미결제 중복은 방금 것만 남기고
        취소한다. 다른 열차의 미결제 예약은 의도적일 수 있어 경고만 남긴다.
        반환: (유지할 예약, 작업종료 여부).
        """
        try:
            tickets = client.tickets() or []
            reservations = client.reservations() or []
        except Exception as e:
            job.log(f"사후 중복검사 실패(그대로 진행): {_safe_err(e)[:80]}")
            return res, False
        paid = next((t for t in tickets if _spec_matches_entry(job.spec, t)), None)
        if paid is not None:
            job.log(f"⚠ 이미 발권된 같은 표 발견 → 방금 잡은 예약을 취소하고 기존 표 사용: {paid}")
            try:
                client.cancel(res)
            except Exception as e:
                job.log(f"⚠ 신규예약 취소 실패 — 코레일톡에서 직접 취소 필요: {_safe_err(e)[:80]}")
            job.reservation_summary = f"[기존 발권 표 사용] {paid}"
            job.status = JobStatus.PAID
            return None, True
        others = [
            r for r in reservations
            if _spec_matches_entry(job.spec, r)
            and getattr(r, "rsv_id", None) != getattr(res, "rsv_id", None)
        ]
        for m in others:
            if _same_train_no(getattr(m, "train_no", None), getattr(res, "train_no", None)):
                job.log(f"⚠ 같은 열차 중복 예약 감지 → 초과분 취소: {m}")
                try:
                    client.cancel(m)
                except Exception as e:
                    job.log(f"⚠ 중복예약 취소 실패 — 코레일톡에서 직접 취소 필요: {_safe_err(e)[:80]}")
            else:
                job.log(f"ℹ 같은 구간 다른 열차의 미결제 예약이 계정에 있음(그대로 둠): {m}")
        return res, False

    def _improve_seat(self, client: PatchedKorail, job: Job, passengers, option, res, active):
        """배정 좌석이 선호(창측·중간열)에 못 미치면 취소→즉시 재예약으로 개선한다.

        반드시 '대상 열차에 다른 좌석이 남아있음'을 확인한 뒤에만 취소한다 —
        남은 자리가 그것뿐이면 그대로 진행(사용자 원칙). 재예약까지 실패하면
        None을 반환해 폴링으로 복귀한다(표를 잃은 상태, 계속 재도전).
        예약대기(좌석 미배정)는 개선 대상이 아니다.
        """
        spec = job.spec
        if spec.passengers != 1 or not (spec.prefer_window or spec.avoid_edge_rows):
            return res
        if getattr(res, "is_waiting", False):
            return res
        train_type = TRAIN_TYPE_MAP.get(spec.train_type.lower(), TrainType.KTX)
        for _ in range(SEAT_IMPROVE_MAX):
            if not active():
                return res
            seats_obj = list(getattr(res, "tickets", None) or [])
            seats = [s.seat for s in seats_obj]
            types = [getattr(s, "seat_type", None) for s in seats_obj]
            score = seatpref.group_score(
                seats, types, spec.prefer_window, spec.avoid_edge_rows)
            if not seats or score >= seatpref.max_score():
                if seats:
                    job.log(f"좌석 확인: {seatpref.describe(seats, types)} — 선호 충족")
                return res
            job.log(f"좌석 확인: {seatpref.describe(seats, types)} — 남은 좌석 있으면 개선 시도")
            # 우리가 예약을 쥔 상태에서 같은 열차가 여전히 '예약가능'이면
            # 우리 좌석 말고도 남은 자리가 있다는 뜻 — 그때만 바꿔치기한다.
            try:
                res_no = getattr(res, "train_no", None)
                trains = search_all(
                    client, spec.dep, spec.arr, spec.date, spec.time,
                    train_type=train_type, max_pages=10,
                    stop_when=lambda t: _same_train_no(t.train_no, res_no),
                )
            except NoResultsError:
                job.log("남은 좌석 없음 — 배정 좌석 그대로 진행")
                return res
            except Exception as e:
                job.log(f"남은 좌석 확인 실패 → 현재 좌석 유지: {_safe_err(e)[:60]}")
                return res
            now = next(
                (t for t in trains if _same_train_no(t.train_no, getattr(res, "train_no", None))),
                None)
            is_special = any("특" in str(t) for t in types if t)
            avail = now is not None and (
                now.has_special_seat() if is_special else now.has_general_seat()
            )
            if not avail:
                job.log("남은 좌석 없음 — 배정 좌석 그대로 진행")
                return res
            try:
                if not client.cancel(res):
                    job.log("개선용 취소 실패 → 현재 좌석 유지")
                    return res
            except Exception as e:
                job.log(f"개선용 취소 실패 → 현재 좌석 유지: {_safe_err(e)[:60]}")
                return res
            new_res = None
            for _retry in range(3):
                new_res = self._reserve_with_pref(client, job, now, passengers, option)
                if new_res is not None or job._stop.wait(0.8):
                    break
            if new_res is None:
                job.log("⚠ 개선 재예약 실패 — 좌석을 놓침. 폴링 재개")
                return None
            new_obj = list(getattr(new_res, "tickets", None) or [])
            new_seats = [s.seat for s in new_obj]
            new_types = [getattr(s, "seat_type", None) for s in new_obj]
            new_score = seatpref.group_score(
                new_seats, new_types, spec.prefer_window, spec.avoid_edge_rows)
            job.log(f"재배정 좌석: {seatpref.describe(new_seats, new_types)}")
            if new_score <= score:
                job.log("더 나은 좌석이 안 나옴 — 이대로 진행")
                return new_res
            res = new_res
        return res

    def _handle_payment(self, client: PatchedKorail, job: Job, creds: config.RailCredentials) -> bool:
        """결제 흐름. True=작업 종료(결제/오류/정지), False=폴링 재개(확인 시간초과)."""
        if job.spec.pay_mode == PayMode.MANUAL:
            job.log("manual mode: waiting for user '결제 진행' (~9min)")
            if job._pay_event.wait(timeout=540):
                if job._stop.is_set():
                    job.log("stopped before payment")
                    return True
                job.log("user confirmed → charging card")
                self._pay(client, job, creds)
                return True
            job.log("결제확인 시간초과(~9분) → 예약은 자동취소됨. 표잡기 폴링 재개")
            return False

        if not creds.card_number:
            job.status = JobStatus.ERROR
            job.error = "auto pay requested but card not configured"
            job.log("ERROR: auto pay requires card info")
            return True
        job.log("auto-pay → charging card")
        self._pay(client, job, creds)
        return True

    def _pay(self, client: PatchedKorail, job: Job, creds: config.RailCredentials) -> None:
        if not creds.card_number:
            job.status = JobStatus.ERROR
            job.error = "card info missing"
            job.log("ERROR: card info not in keychain")
            return
        try:
            ok = client.pay_with_card(
                job._reservation,
                card_number=creds.card_number,
                card_password=creds.card_password,
                birthday=creds.card_validation,
                card_expire=creds.card_expire,
                installment=creds.card_installment,
                card_type="J",
            )
        except Exception as e:
            job.status = JobStatus.ERROR
            job.error = f"pay error: {_safe_err(e)}"
            job.log(f"ERROR: pay error: {_safe_err(e)}")
            return
        if ok:
            job.status = JobStatus.PAID
            job.log("PAID OK")
        else:
            job.status = JobStatus.ERROR
            job.error = "pay_with_card returned False"
            job.log("ERROR: pay_with_card returned False")

    @staticmethod
    def _pick_target(trains, spec: JobSpec):
        if spec.train_number:
            for t in trains:
                if _same_train_no(t.train_no, spec.train_number):
                    return t
            return None
        if spec.train_id:  # 구버전 잡 호환
            for t in trains:
                if _train_id(t) == spec.train_id:
                    return t
            return None
        return trains[0] if trains else None

    @staticmethod
    def _can_take(gen: bool, spc: bool, pref: str) -> bool:
        if pref == "general":
            return gen
        if pref == "special":
            return spc
        return gen or spc

    @staticmethod
    def _seat_pref_to_option(pref: str):
        if pref == "special":
            return ReserveOption.SPECIAL_FIRST
        if pref == "general":
            return ReserveOption.GENERAL_FIRST
        return ReserveOption.GENERAL_FIRST


manager = JobManager()


def _force_session_timeout(session, seconds: float) -> None:
    if getattr(session, "_kt_timeout_patched", False):
        return
    orig = session.request
    def request(method, url, **kw):
        kw.setdefault("timeout", seconds)
        return orig(method, url, **kw)
    session.request = request
    session._kt_timeout_patched = True


def search_all(
    client: PatchedKorail,
    dep: str, arr: str, date: str, time_: str = "000000",
    train_type=TrainType.ALL,
    include_no_seats: bool = True,
    include_waiting_list: bool = False,
    max_pages: int = 10,
    stop_when=None,
) -> list:
    """코레일 조회를 출발시각을 밀어가며 반복해 하루치를 모은다.

    코레일 API는 한 요청에 ~10편만 준다(SR API와 달리 라이브러리에 반복 로직이
    없다). 그대로 쓰면 "수서→동대구 41편 중 10편"만 보여 늦은 열차는 아예 잡을
    수 없다. 마지막 열차 출발시각+1초를 다음 요청 시작시각으로 넣어 이어 받는다.

    stop_when(train)이 참인 열차를 만나면 즉시 멈춘다 — 폴링처럼 특정 열차 하나만
    필요할 때 불필요한 요청(=안티봇 부하)을 아낀다.
    """
    out: list = []
    seen: set = set()
    cur = time_ or "000000"
    for _ in range(max(1, max_pages)):
        try:
            page = client.search_train(
                dep, arr, date, cur, train_type=train_type,
                include_no_seats=include_no_seats,
                include_waiting_list=include_waiting_list,
            )
        except NoResultsError:
            break
        key = lambda t: (getattr(t, "train_no", None), getattr(t, "dep_time", ""))
        fresh = [t for t in page if key(t) not in seen]
        if not fresh:
            break
        seen.update(key(t) for t in fresh)
        out.extend(fresh)
        if stop_when is not None and any(stop_when(t) for t in fresh):
            break
        # 출발시각이 없는 응답이면 더 밀 수 없다 — 받은 만큼만 쓴다
        times = [getattr(t, "dep_time", "") for t in page]
        last = max((x for x in times if x), default="")
        if not last:
            break
        nxt = (datetime.strptime(last, "%H%M%S") + timedelta(seconds=1)).strftime("%H%M%S")
        if nxt <= cur:  # 자정을 넘겼거나 더 밀 수 없음
            break
        cur = nxt
    out.sort(key=lambda t: getattr(t, "dep_time", ""))
    return out


def _new_search_client() -> PatchedKorail:
    creds = config.rail.load()
    if not creds:
        raise RuntimeError("credentials not configured")
    client = PatchedKorail(creds.rail_id, creds.rail_password, auto_login=False)
    _force_session_timeout(client._session, 25)
    if not client.login():
        raise RuntimeError("login failed")
    return client


def search_preview(dep: str, arr: str, date: str, time_: str, train_type: str = "all") -> list[dict]:
    client = _new_search_client()
    ttype = TRAIN_TYPE_MAP.get(train_type.lower(), TrainType.ALL)
    trains = search_all(client, dep, arr, date, time_, train_type=ttype)
    return [{**_row(t), "train_no": t.train_no, "label": str(t)} for t in trains[:60]]


def _row(t) -> dict:
    """시간표/환승 조회용 구조화 행 (timetable.combine 입력 형식).

    구간별 예약 잡 등록에 필요한 train_id도 포함한다.
    """
    op = operator(t.train_no)
    return {
        "train_number": t.train_no,
        "train_id": _train_id(t),
        "train_name": t.train_type_name,
        # 화면 표기용 — SRT면 "SRT", 아니면 열차 이름(KTX, 무궁화호 …)
        "operator": op or t.train_type_name,
        "dep": t.dep_name,
        "arr": t.arr_name,
        "dep_time": t.dep_time,
        "arr_time": t.arr_time,
        "general": t.has_general_seat(),
        "special": t.has_special_seat(),
    }


def timetable(dep: str, arr: str, date: str, time_: str = "000000", train_type: str = "all") -> list[dict]:
    """해당 날짜 직행 시간표 (KTX+SRT, 좌석 유무 포함, 매진 열차도 포함)."""
    client = _new_search_client()
    ttype = TRAIN_TYPE_MAP.get(train_type.lower(), TrainType.ALL)
    return [_row(t) for t in search_all(client, dep, arr, date, time_, train_type=ttype)]


def transfer_search(
    dep: str, arr: str, date: str, time_: str,
    vias: list[str], min_gap_min: int = 6, limit: int = 10,
    train_type: str = "all",
) -> dict:
    """직행 + 구간별 환승 조합 조회 (로그인 1회로 전 구간 검색).

    공식 환승 조회가 아니라 구간별 검색을 조합한다 — 각 구간을 별도 잡으로
    예약하는 '구간별 예약' 방식 전제. 환승 대기 min_gap_min(기본 6분) 이상만.
    """
    client = _new_search_client()
    ttype = TRAIN_TYPE_MAP.get(train_type.lower(), TrainType.ALL)
    errors: list[str] = []

    def rows(d: str, a: str) -> list[dict]:
        try:
            trains = search_all(client, d, a, date, time_, train_type=ttype)
        except Exception as e:
            errors.append(f"{d}→{a}: {_safe_err(e)[:80]}")
            return []
        return [_row(t) for t in trains]

    direct = rows(dep, arr)
    transfers: list[dict] = []
    for via in vias:
        if via in (dep, arr):
            continue
        time.sleep(0.5)  # 연속 검색 부하 완화(안티봇)
        leg1 = rows(dep, via)
        if not leg1:
            continue
        time.sleep(0.5)
        leg2 = rows(via, arr)
        transfers.extend(tt.combine(leg1, leg2, via, min_gap_min))
    return {
        "direct": direct,
        "transfers": tt.sort_and_limit(transfers, limit),
        "errors": errors,
    }


# 자주 쓰는 구간(창원↔동대구/대전/오송 환승 구간 + 서울행 대안) — 프리페치 기본값
PREFETCH_ROUTES = [
    ("창원중앙", "동대구"), ("동대구", "창원중앙"),
    ("창원중앙", "대전"), ("대전", "창원중앙"),
    ("창원중앙", "오송"), ("오송", "창원중앙"),
    ("창원중앙", "서울"), ("서울", "창원중앙"),
]


def prefetch_timetables(routes=None, days: int = 30, delay_s: float = 2.5, train_type: str = "all") -> dict:
    """한달치 시간표를 미리 받아 schedule_cache에 저장한다(백그라운드 실행 전제).

    로그인 1회 재사용 + 검색 사이 delay_s(±지터) 간격, 세션 8분마다 선제 갱신.
    NoResultsError는 '그 날 열차 없음'으로 빈 목록을 캐시한다.
    """
    from datetime import datetime, timedelta

    route_list = [tuple(r) for r in (routes or PREFETCH_ROUTES)]
    dates = [(datetime.now() + timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]
    client = _new_search_client()
    ttype = TRAIN_TYPE_MAP.get(train_type.lower(), TrainType.ALL)
    session_started = time.monotonic()
    fetched, err_list = 0, []
    for dep, arr in route_list:
        for d in dates:
            if time.monotonic() - session_started > 480:  # 세션 만료 예방
                try:
                    client = _new_search_client()
                    session_started = time.monotonic()
                except Exception as e:
                    err_list.append(f"재로그인 실패: {_safe_err(e)[:60]}")
                    time.sleep(15)
                    continue
            try:
                trains = search_all(client, dep, arr, d, "000000", train_type=ttype)
                schedule_cache.put("rail", dep, arr, d, [_row(t) for t in trains])
                fetched += 1
            except NeedToLoginError:
                try:
                    client = _new_search_client()
                    session_started = time.monotonic()
                except Exception:
                    pass
            except Exception as e:
                err_list.append(f"{dep}→{arr} {d}: {_safe_err(e)[:60]}")
                time.sleep(15)  # 차단성 오류일 수 있어 여유를 두고 계속
            time.sleep(delay_s + random.uniform(0.0, 1.0))
    return {
        "service": "rail", "fetched": fetched, "days": days,
        "routes": [f"{d}→{a}" for d, a in route_list],
        "errors": len(err_list), "error_samples": err_list[:10],
    }
