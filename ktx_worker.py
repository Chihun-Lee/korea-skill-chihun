"""KTX/Korail polling/booking/payment worker.

Mirrors srt-macro/srt_worker.py but uses the patched srtgo Korail.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
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
import timetable as tt
from ktx_korail import PatchedKorail
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


TRAIN_TYPE_MAP = {
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


@dataclass
class JobSpec:
    dep: str
    arr: str
    date: str
    time: str
    train_id: Optional[str]
    train_type: str
    passengers: int
    seat_pref: str  # "general" | "special" | "any"
    pay_mode: PayMode
    include_waiting: bool = False


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
        return getattr(t, "train_no", None) == want_no
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
        jobstore.save("ktx", active)

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
        for d in jobstore.load("ktx"):
            if d in existing:
                continue
            try:
                d["pay_mode"] = PayMode(d["pay_mode"])
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

    def _preflight_dedup(self, client: PatchedKorail, job: Job, creds: config.KTXCredentials) -> Optional[bool]:
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
        creds = config.ktx.load()
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

    def _poll_loop(self, job: Job, gen: int, creds: config.KTXCredentials, active) -> bool:
        """폴링 본체. True면 작업 종료(결제 흐름 완료/정지), False면 재시작 대상."""

        # 출발일이 지난 잡은 의미가 없다 — 로그인/폴링 없이 즉시 종료
        if job.spec.date < datetime.now().strftime("%Y%m%d"):
            job.status = JobStatus.STOPPED
            job.error = "출발일 경과 — 자동 종료"
            job.log("출발일이 지나 작업을 자동 종료합니다")
            return True

        def _new_client() -> PatchedKorail:
            c = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
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
            f"login ok ({getattr(client, 'name', creds.ktx_id)}); "
            f"polling {job.spec.dep}->{job.spec.arr} {job.spec.date} {job.spec.time} "
            f"type={job.spec.train_type}"
        )
        job.status = JobStatus.POLLING

        # 중복예매 방지: 이 계정에 같은 표의 발권/예약 이력이 있으면 여기서 끝낸다
        preflight = self._preflight_dedup(client, job, creds)
        if preflight is not None:
            return preflight

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

            try:
                trains = client.search_train(
                    job.spec.dep, job.spec.arr,
                    job.spec.date, job.spec.time,
                    train_type=train_type,
                    include_no_seats=True,
                    include_waiting_list=job.spec.include_waiting,
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
                    gen = target.has_general_seat()
                    spc = target.has_special_seat()
                    job.log(f"#{job.attempts} {target.train_no} general={gen} special={spc}")
                    if self._can_take(gen, spc, job.spec.seat_pref):
                        try:
                            res = client.reserve(target, passengers=passengers, option=seat_option)
                        except SoldOutError:
                            job.log("reserve race lost (sold out)")
                        except KorailError as e:
                            job.log(f"reserve error: {_safe_err(e)}")
                        else:
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

    def _handle_payment(self, client: PatchedKorail, job: Job, creds: config.KTXCredentials) -> bool:
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

    def _pay(self, client: PatchedKorail, job: Job, creds: config.KTXCredentials) -> None:
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
        if spec.train_id:
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


def _new_search_client() -> PatchedKorail:
    creds = config.ktx.load()
    if not creds:
        raise RuntimeError("credentials not configured")
    client = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
    _force_session_timeout(client._session, 25)
    if not client.login():
        raise RuntimeError("login failed")
    return client


def search_preview(dep: str, arr: str, date: str, time_: str, train_type: str = "ktx") -> list[dict]:
    client = _new_search_client()
    ttype = TRAIN_TYPE_MAP.get(train_type.lower(), TrainType.KTX)
    try:
        trains = client.search_train(dep, arr, date, time_, train_type=ttype, include_no_seats=True)
    except NoResultsError:
        return []
    out = []
    for t in trains[:25]:
        out.append({
            "train_id": _train_id(t),
            "train_no": t.train_no,
            "label": str(t),
            "general": t.has_general_seat(),
            "special": t.has_special_seat(),
        })
    return out


def _row(t) -> dict:
    """시간표/환승 조회용 구조화 행 (timetable.combine 입력 형식).

    구간별 예약 잡 등록에 필요한 train_id도 포함한다.
    """
    return {
        "train_number": t.train_no,
        "train_id": _train_id(t),
        "train_name": t.train_type_name,
        "dep": t.dep_name,
        "arr": t.arr_name,
        "dep_time": t.dep_time,
        "arr_time": t.arr_time,
        "general": t.has_general_seat(),
        "special": t.has_special_seat(),
    }


def timetable(dep: str, arr: str, date: str, time_: str = "000000", train_type: str = "ktx") -> list[dict]:
    """해당 날짜 직행 시간표 (좌석 유무 포함, 매진 열차도 포함)."""
    client = _new_search_client()
    ttype = TRAIN_TYPE_MAP.get(train_type.lower(), TrainType.KTX)
    try:
        trains = client.search_train(dep, arr, date, time_, train_type=ttype, include_no_seats=True)
    except NoResultsError:
        return []
    return [_row(t) for t in trains]


def transfer_search(
    dep: str, arr: str, date: str, time_: str,
    vias: list[str], min_gap_min: int = 6, limit: int = 10,
    train_type: str = "ktx",
) -> dict:
    """직행 + 구간별 환승 조합 조회 (로그인 1회로 전 구간 검색).

    공식 환승 조회가 아니라 구간별 검색을 조합한다 — 각 구간을 별도 잡으로
    예약하는 '구간별 예약' 방식 전제. 환승 대기 min_gap_min(기본 6분) 이상만.
    """
    client = _new_search_client()
    ttype = TRAIN_TYPE_MAP.get(train_type.lower(), TrainType.KTX)
    errors: list[str] = []

    def rows(d: str, a: str) -> list[dict]:
        try:
            trains = client.search_train(d, a, date, time_, train_type=ttype, include_no_seats=True)
        except NoResultsError:
            return []
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
                trains = client.search_train(dep, arr, d, "000000", train_type=ttype, include_no_seats=True)
                schedule_cache.put("ktx", dep, arr, d, [_row(t) for t in trains])
                fetched += 1
            except NoResultsError:
                schedule_cache.put("ktx", dep, arr, d, [])
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
        "service": "ktx", "fetched": fetched, "days": days,
        "routes": [f"{d}→{a}" for d, a in route_list],
        "errors": len(err_list), "error_samples": err_list[:10],
    }
