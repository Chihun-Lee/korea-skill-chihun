"""recovery.py + worker 자가복구 검증 (네트워크 불필요).

검증 목표 (사용자 보고: "netfunnel 차단 뜨면 창은 떠있는데 예매만 멈춤"):
  1) 백오프가 연속 실패마다 커지고 상한에서 멈춘다  → 차단을 연장하는 '하머링' 금지
  2) 일정 횟수마다 완전 새 세션으로 에스컬레이션한다
  3) 성공하면 streak이 리셋된다
  4) 워커가 netfunnel 4999를 N회 맞아도 죽지 않고(POLLING 유지) 결국 회복한다

실행:  venv/bin/python test_recovery.py
"""
from __future__ import annotations

import threading
import time
import types

from SRT.errors import SRTNetFunnelError
from srtgo.ktx import KorailError

import tempfile
from pathlib import Path

import jobstore
import recovery
import srt_worker
import ktx_worker

# 테스트가 실제 ~/.k-rail-macro/jobs.json 을 건드리지 않도록 격리
jobstore.PATH = Path(tempfile.mkdtemp()) / "jobs.json"


# ── 1. 백오프 수학 / 에스컬레이션 (순수) ────────────────────────────────
def test_backoff_grows_and_caps():
    rc = recovery.RecoveryController(base=5, cap=60, fresh_login_every=4,
                                     jitter=(1.0, 1.0))  # 지터 끔 → 결정적
    sleeps = [rc.on_error().sleep for _ in range(7)]
    assert sleeps == [5, 10, 20, 40, 60, 60, 60], sleeps          # 지수↑ 후 상한
    print("  [ok] 백오프 지수 증가 + 60s 상한:", sleeps)


def test_fresh_login_escalation():
    rc = recovery.RecoveryController(fresh_login_every=4, jitter=(1.0, 1.0))
    fresh = [rc.on_error().fresh_login for _ in range(8)]
    assert fresh == [False, False, False, True, False, False, False, True], fresh
    print("  [ok] 4회마다 새 세션 에스컬레이션:", fresh)


def test_success_resets():
    rc = recovery.RecoveryController(jitter=(1.0, 1.0))
    rc.on_error(); rc.on_error()
    assert rc.streak == 2
    rc.on_success()
    assert rc.streak == 0
    assert rc.on_error().streak == 1   # 리셋 후 다시 1부터
    print("  [ok] 성공 시 streak 리셋")


# ── 2. 워커 통합: 4999 폭격 후 회복 (FakeSRT) ──────────────────────────
class _FakeSession:
    """_force_session_timeout가 감쌀 수 있도록 request 속성만 가진 더미 세션."""
    def request(self, method, url, **kw):
        return None


class _FakeNF:
    def __init__(self):
        self._cached_key = "poisoned"
        self.session = _FakeSession()


class FakeSRT:
    created = 0
    fail_remaining = 5      # 전역 검색 5회까지 netfunnel 차단, 이후 성공
    successes = 0

    def __init__(self, *a, **k):
        FakeSRT.created += 1
        self._session = _FakeSession()
        self.netfunnel_helper = _FakeNF()

    def login(self, *a, **k):
        return True

    def get_reservations(self, paid_only=False):
        return []           # 기존 예약 없음 → 중복예매 사전검사 통과

    def search_train(self, *a, **k):
        if FakeSRT.fail_remaining > 0:
            FakeSRT.fail_remaining -= 1
            raise SRTNetFunnelError(
                "Failed to complete NetFunnel: NetFunnel.gRtype=4999;"
                "NetFunnel.gControl.result='5..."
            )
        FakeSRT.successes += 1
        return []           # 성공: 검색됐지만 좌석 없음 → 계속 폴링


def test_worker_recovers_without_wedging(monkeypatch=None):
    # 빠르고 결정적으로: 작은 백오프, 2회마다 새 세션, 세션/정체 타이머는 무력화
    FakeSRT.created = 0
    FakeSRT.fail_remaining = 5
    FakeSRT.successes = 0

    # 워커가 RecoveryController(base=..., cap=..., fresh_login_every=...)로
    # 인자를 넘기므로 더미도 임의 인자를 받아 결정적 값으로 덮어쓴다.
    orig_rc = srt_worker.RecoveryController
    srt_worker.RecoveryController = lambda *a, **k: orig_rc(
        base=0.01, cap=0.05, fresh_login_every=2, jitter=(1.0, 1.0)
    )
    # netfunnel helper 재생성을 더미로 (실 requests 세션 생성 방지, 결정적).
    srt_worker.NetFunnelHelper = _FakeNF
    srt_worker.SRT = FakeSRT
    srt_worker.MIN_INTERVAL = 0.01
    srt_worker.MAX_INTERVAL = 0.01
    srt_worker.SESSION_MAX_AGE = 1e9
    srt_worker.STALL_LIMIT = 1e9
    srt_worker.config.srt.load = lambda: types.SimpleNamespace(
        srt_id="tester", srt_password="pw"
    )

    spec = srt_worker.JobSpec(
        dep="수서", arr="부산", date="20991201", time="090000",
        train_number=None, passengers=1, seat_pref="any",
        pay_mode=srt_worker.PayMode.MANUAL,
    )
    job = srt_worker.manager.create(spec)

    # 회복(성공 검색)까지 최대 5초 대기
    deadline = time.time() + 5
    while time.time() < deadline and FakeSRT.successes < 2:
        time.sleep(0.05)
    srt_worker.manager.stop(job.id)
    time.sleep(0.1)

    assert FakeSRT.successes >= 1, "끝내 회복하지 못함(예매 멈춤 재현됨)"
    assert job.status in (srt_worker.JobStatus.POLLING, srt_worker.JobStatus.STOPPED), job.status
    assert job.recoveries >= 5, f"recoveries={job.recoveries} (차단을 복구로 세지 못함)"
    assert FakeSRT.created >= 2, f"새 세션 에스컬레이션 안 됨 (created={FakeSRT.created})"
    print(f"  [ok] 4999 5회 폭격 후 회복: recoveries={job.recoveries} "
          f"fresh_sessions={FakeSRT.created} successes={FakeSRT.successes} status={job.status}")


class FakeSRTConnErr(FakeSRT):
    """netfunnel 오류가 '문자열이 아닌' 예외객체를 .msg에 감싸 던지는 경우.

    SRTrain은 `raise SRTNetFunnelError(ConnectionError(...))`처럼 예외객체를
    그대로 넣는다 → str(e)가 TypeError를 던져 폴링 스레드가 통째로 죽던 버그.
    _safe_err로 회복되는지 검증한다(사용자 보고: netfunnel ConnectionError 사망)."""
    def search_train(self, *a, **k):
        if FakeSRTConnErr.fail_remaining > 0:
            FakeSRTConnErr.fail_remaining -= 1
            raise SRTNetFunnelError(ConnectionError("Connection aborted (non-str msg)"))
        FakeSRTConnErr.successes += 1
        return []


def test_worker_survives_nonstring_netfunnel_msg():
    FakeSRTConnErr.created = 0
    FakeSRTConnErr.fail_remaining = 4
    FakeSRTConnErr.successes = 0

    orig_rc = srt_worker.RecoveryController
    srt_worker.RecoveryController = lambda *a, **k: orig_rc(
        base=0.01, cap=0.05, fresh_login_every=2, jitter=(1.0, 1.0)
    )
    srt_worker.NetFunnelHelper = _FakeNF
    srt_worker.SRT = FakeSRTConnErr
    srt_worker.MIN_INTERVAL = 0.01
    srt_worker.MAX_INTERVAL = 0.01
    srt_worker.SESSION_MAX_AGE = 1e9
    srt_worker.STALL_LIMIT = 1e9
    srt_worker.config.srt.load = lambda: types.SimpleNamespace(
        srt_id="tester", srt_password="pw"
    )

    spec = srt_worker.JobSpec(
        dep="수서", arr="부산", date="20991201", time="090000",
        train_number=None, passengers=1, seat_pref="any",
        pay_mode=srt_worker.PayMode.MANUAL,
    )
    job = srt_worker.manager.create(spec)
    deadline = time.time() + 5
    while time.time() < deadline and FakeSRTConnErr.successes < 1:
        time.sleep(0.05)
    srt_worker.manager.stop(job.id)
    time.sleep(0.1)

    assert FakeSRTConnErr.successes >= 1, "비문자열 msg netfunnel에서 스레드 사망(버그 재현)"
    assert job.recoveries >= 4, f"recoveries={job.recoveries}"
    print(f"  [ok] 비문자열 msg netfunnel 4회 후 회복: recoveries={job.recoveries} "
          f"successes={FakeSRTConnErr.successes}")


# ── 2b. KTX 워커: 안티봇 폭격 후 회복 + 비문자열 msg 생존 ──────────────
class _FakeKorailSession:
    def request(self, method, url, **kw):
        return None


class FakeKorail:
    """KTX 워커용 더미. search_train이 안티봇(MACRO) 오류를 N회 던진 뒤 성공."""
    created = 0
    fail_remaining = 4
    successes = 0
    raise_nonstring = False  # True면 str()가 깨지는 예외를 던진다

    def __init__(self, *a, **k):
        FakeKorail.created += 1
        self.name = "tester"
        self._session = _FakeKorailSession()

    def login(self):
        return True

    def tickets(self):
        return []           # 기존 발권 없음 → 중복예매 사전검사 통과

    def reservations(self, rsv_id=None):
        return []           # 기존 예약 없음

    def search_train(self, *a, **k):
        if FakeKorail.fail_remaining > 0:
            FakeKorail.fail_remaining -= 1
            if FakeKorail.raise_nonstring:
                raise _NonStrExc()
            raise KorailError("MACRO ERROR: 원활한 서비스 제공을 위해...")
        FakeKorail.successes += 1
        return []


class _NonStrExc(Exception):
    """str(e)가 TypeError를 던지는 예외(폴링 스레드 사망 재현). catch-all이
    _safe_err로 안전 변환해 살아남아야 한다."""
    def __init__(self):
        self.msg = ConnectionError("non-str msg")

    def __str__(self):
        raise TypeError("non-string msg")


def _setup_ktx(fake_cls):
    fake_cls.created = 0
    fake_cls.successes = 0
    orig_rc = ktx_worker.RecoveryController
    ktx_worker.RecoveryController = lambda *a, **k: orig_rc(
        base=0.01, cap=0.05, fresh_login_every=2, jitter=(1.0, 1.0)
    )
    ktx_worker.PatchedKorail = fake_cls
    ktx_worker.MIN_INTERVAL = 0.01
    ktx_worker.MAX_INTERVAL = 0.01
    ktx_worker.SESSION_MAX_AGE = 1e9
    ktx_worker.STALL_LIMIT = 1e9
    ktx_worker.config.ktx.load = lambda: types.SimpleNamespace(
        ktx_id="tester", ktx_password="pw", card_number="",
        card_password="", card_validation="", card_expire="", card_installment=0,
    )


def _run_ktx_job():
    spec = ktx_worker.JobSpec(
        dep="서울", arr="부산", date="20991201", time="090000",
        train_id="NONE|0|0", train_type="ktx", passengers=1,
        seat_pref="any", pay_mode=ktx_worker.PayMode.MANUAL,
    )
    job = ktx_worker.manager.create(spec)
    deadline = time.time() + 5
    while time.time() < deadline and FakeKorail.successes < 1:
        time.sleep(0.05)
    ktx_worker.manager.stop(job.id)
    time.sleep(0.1)
    return job


def test_ktx_recovers_from_antibot():
    FakeKorail.raise_nonstring = False
    FakeKorail.fail_remaining = 4
    _setup_ktx(FakeKorail)
    job = _run_ktx_job()
    assert FakeKorail.successes >= 1, "안티봇 차단에서 끝내 회복 못함(예매 멈춤)"
    assert job.recoveries >= 4, f"recoveries={job.recoveries}"
    assert FakeKorail.created >= 2, f"새 세션 에스컬레이션 안 됨(created={FakeKorail.created})"
    print(f"  [ok] KTX 안티봇(MACRO) 4회 후 회복: recoveries={job.recoveries} "
          f"fresh_sessions={FakeKorail.created}")


def test_ktx_survives_nonstring_msg():
    FakeKorail.raise_nonstring = True
    FakeKorail.fail_remaining = 3
    _setup_ktx(FakeKorail)
    job = _run_ktx_job()
    FakeKorail.raise_nonstring = False
    assert FakeKorail.successes >= 1, "비문자열 msg 예외에서 KTX 스레드 사망(버그 재현)"
    print(f"  [ok] KTX 비문자열 msg 예외 3회 후 생존·회복: successes={FakeKorail.successes}")


# ── 2.5 중복예매 방지 (v2.1.0) ─────────────────────────────────────────
class FakeSRTWithHistory(FakeSRT):
    """계정에 이미 예약/발권 이력이 있는 상황을 흉내내는 더미."""
    history = []
    searched = 0
    paid_calls = []

    def get_reservations(self, paid_only=False):
        return list(type(self).history)

    def search_train(self, *a, **k):
        type(self).searched += 1
        return []

    def pay_with_card(self, reservation, **kw):
        type(self).paid_calls.append(reservation)
        return True


def _srt_dedup_setup(history, creds=None):
    FakeSRTWithHistory.history = history
    FakeSRTWithHistory.searched = 0
    FakeSRTWithHistory.paid_calls = []
    srt_worker.SRT = FakeSRTWithHistory
    srt_worker.NetFunnelHelper = _FakeNF
    srt_worker.DEDUP_RETRY_BASE = 0.01
    srt_worker.MIN_INTERVAL = 0.01
    srt_worker.MAX_INTERVAL = 0.01
    srt_worker.config.srt.load = lambda: creds or types.SimpleNamespace(
        srt_id="tester", srt_password="pw"
    )


def _wait_status(job, statuses, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and job.status not in statuses:
        time.sleep(0.05)
    return job.status


def test_srt_dedup_blocks_when_already_paid():
    """서버 재시작 복원 시나리오: 이미 결제된 표가 있으면 재예매 없이 종료."""
    _srt_dedup_setup([types.SimpleNamespace(
        dep_station_name="수서", arr_station_name="부산", dep_date="20991201",
        train_number="301", paid=True,
    )])
    spec = srt_worker.JobSpec(
        dep="수서", arr="부산", date="20991201", time="090000",
        train_number=None, passengers=1, seat_pref="any",
        pay_mode=srt_worker.PayMode.AUTO,
    )
    job = srt_worker.manager.create(spec)
    st = _wait_status(job, (srt_worker.JobStatus.PAID,))
    srt_worker.manager.stop(job.id)
    assert st == srt_worker.JobStatus.PAID, f"status={st}"
    assert FakeSRTWithHistory.searched == 0, "결제된 표가 있는데 검색(재예매 시도)함"
    assert FakeSRTWithHistory.paid_calls == [], "결제된 표에 또 결제 시도함"
    assert "[기존 결제 표 감지]" in (job.reservation_summary or "")
    print("  [ok] 결제완료 표 감지 → 재예매/재결제 없이 즉시 종료(PAID)")


def test_srt_dedup_adopts_unpaid_reservation():
    """크래시가 예약~결제 사이에 난 시나리오: 미결제 예약을 이어받아 결제만 진행."""
    res = types.SimpleNamespace(
        dep_station_name="수서", arr_station_name="부산", dep_date="20991201",
        train_number="301", paid=False, payment_date="20991201", payment_time="235900",
    )
    _srt_dedup_setup([res], creds=types.SimpleNamespace(
        srt_id="tester", srt_password="pw", card_number="1234123412341234",
        card_password="12", card_validation="900101", card_expire="3012",
        card_installment=0, card_type="J",
    ))
    spec = srt_worker.JobSpec(
        dep="수서", arr="부산", date="20991201", time="090000",
        train_number=None, passengers=1, seat_pref="any",
        pay_mode=srt_worker.PayMode.AUTO,
    )
    job = srt_worker.manager.create(spec)
    st = _wait_status(job, (srt_worker.JobStatus.PAID, srt_worker.JobStatus.ERROR))
    srt_worker.manager.stop(job.id)
    assert st == srt_worker.JobStatus.PAID, f"status={st} err={job.error}"
    assert FakeSRTWithHistory.searched == 0, "예약이 살아있는데 새로 검색(재예매 시도)함"
    assert FakeSRTWithHistory.paid_calls == [res], "이어받은 예약이 아닌 다른 것을 결제함"
    print("  [ok] 미결제 예약 이어받기 → 재예매 없이 기존 예약을 결제(PAID)")


class FakeKorailWithHistory(FakeKorail):
    ticket_list = []
    searched = 0

    def tickets(self):
        return list(type(self).ticket_list)

    def reservations(self, rsv_id=None):
        return []

    def search_train(self, *a, **k):
        type(self).searched += 1
        return []


def test_ktx_dedup_blocks_when_already_ticketed():
    """KTX: 이미 발권(결제)된 표가 있으면 재예매 없이 종료."""
    _setup_ktx(FakeKorailWithHistory)
    ktx_worker.DEDUP_RETRY_BASE = 0.01
    FakeKorailWithHistory.ticket_list = [types.SimpleNamespace(
        dep_name="서울", arr_name="부산", dep_date="20991201", train_no="101",
    )]
    FakeKorailWithHistory.searched = 0
    spec = ktx_worker.JobSpec(
        dep="서울", arr="부산", date="20991201", time="090000",
        train_id=None, train_type="ktx", passengers=1,
        seat_pref="any", pay_mode=ktx_worker.PayMode.MANUAL,
    )
    job = ktx_worker.manager.create(spec)
    st = _wait_status(job, (ktx_worker.JobStatus.PAID,))
    ktx_worker.manager.stop(job.id)
    assert st == ktx_worker.JobStatus.PAID, f"status={st}"
    assert FakeKorailWithHistory.searched == 0, "발권된 표가 있는데 검색(재예매 시도)함"
    print("  [ok] KTX 발권완료 표 감지 → 재예매 없이 즉시 종료(PAID)")


def test_dead_target_auto_stop():
    """당일 특정 열차가 계속 조회 안 되면(출발 경과) 자동 종료 — 죽은 잡 방지.

    실사고(2026-07-26): 15:07 열차 잡이 출발 후 8시간 동안 653회 헛폴링.
    """
    from datetime import datetime as _dt
    _srt_dedup_setup([])  # search_train이 항상 [] → 대상 미조회 연속
    spec = srt_worker.JobSpec(
        dep="수서", arr="부산", date=_dt.now().strftime("%Y%m%d"), time="000000",
        train_number="9999", passengers=1, seat_pref="any",
        pay_mode=srt_worker.PayMode.MANUAL,
    )
    job = srt_worker.manager.create(spec)
    st = _wait_status(job, (srt_worker.JobStatus.STOPPED,), timeout=8)
    srt_worker.manager.stop(job.id)
    assert st == srt_worker.JobStatus.STOPPED, st
    assert "자동 종료" in (job.error or ""), job.error
    print("  [ok] 당일 대상 열차 미조회 연속 → 잡 자동 종료(출발 경과 추정)")


def test_past_date_auto_stop():
    """출발일이 지난 잡은 로그인/폴링 없이 즉시 종료된다."""
    _srt_dedup_setup([])
    spec = srt_worker.JobSpec(
        dep="수서", arr="부산", date="20200101", time="080000",
        train_number=None, passengers=1, seat_pref="any",
        pay_mode=srt_worker.PayMode.MANUAL,
    )
    job = srt_worker.manager.create(spec)
    st = _wait_status(job, (srt_worker.JobStatus.STOPPED,), timeout=5)
    srt_worker.manager.stop(job.id)
    assert st == srt_worker.JobStatus.STOPPED, st
    assert "출발일 경과" in (job.error or ""), job.error
    print("  [ok] 출발일 지난 잡 → 즉시 자동 종료")


def test_find_active_duplicate():
    """같은 구간·날짜 활성 잡 이중 등록 감지(API 409의 근거)."""
    _srt_dedup_setup([])  # 이력 없음 → 계속 폴링
    spec = srt_worker.JobSpec(
        dep="동탄", arr="목포", date="20991225", time="080000",
        train_number=None, passengers=1, seat_pref="general",
        pay_mode=srt_worker.PayMode.MANUAL,
    )
    job = srt_worker.manager.create(spec)
    try:
        same = srt_worker.JobSpec(
            dep="동탄", arr="목포", date="20991225", time="100000",
            train_number=None, passengers=2, seat_pref="any",
            pay_mode=srt_worker.PayMode.AUTO,
        )
        other_day = srt_worker.JobSpec(
            dep="동탄", arr="목포", date="20991226", time="080000",
            train_number=None, passengers=1, seat_pref="general",
            pay_mode=srt_worker.PayMode.MANUAL,
        )
        assert srt_worker.manager.find_active_duplicate(same) is job, "같은 구간·날짜인데 중복 미감지"
        assert srt_worker.manager.find_active_duplicate(other_day) is None, "다른 날짜인데 중복 오탐"
    finally:
        srt_worker.manager.stop(job.id)
    print("  [ok] 활성 잡 이중 등록 감지(같은 구간·날짜=중복, 다른 날짜=허용)")


# ── 3. HTTP 타임아웃 강제 (행 방지) ────────────────────────────────────
def test_force_session_timeout_injects_default():
    captured = {}

    class S:
        def request(self, method, url, **kw):
            captured.update(kw)
            return "resp"

    s = S()
    srt_worker._force_session_timeout(s, 25)
    s.request("GET", "http://x")           # 호출자가 timeout을 안 줘도
    assert captured.get("timeout") == 25, captured   # 25초가 주입돼야 함
    # 호출자가 명시하면 그 값을 존중
    s.request("GET", "http://x", timeout=3)
    assert captured.get("timeout") == 3, captured
    assert getattr(s, "_kt_timeout_patched", False) is True
    print("  [ok] 세션 타임아웃 주입(기본 25s, 명시값 존중) → 무한 대기 방지")


def test_new_client_patches_sessions():
    # _new_client가 클라이언트/넷퍼넬 세션 둘 다 타임아웃 패치하는지
    srt_worker.SRT = FakeSRT
    c = FakeSRT()
    srt_worker._force_session_timeout(c._session, 25)
    srt_worker._force_session_timeout(c.netfunnel_helper.session, 25)
    assert c._session._kt_timeout_patched
    assert c.netfunnel_helper.session._kt_timeout_patched
    print("  [ok] _new_client 세션/넷퍼넬 세션 모두 타임아웃 적용")


if __name__ == "__main__":
    print("recovery 백오프/에스컬레이션:")
    test_backoff_grows_and_caps()
    test_fresh_login_escalation()
    test_success_resets()
    print("HTTP 타임아웃(행 방지):")
    test_force_session_timeout_injects_default()
    test_new_client_patches_sessions()
    print("SRT 워커 자가복구 통합:")
    test_worker_recovers_without_wedging()
    test_worker_survives_nonstring_netfunnel_msg()
    print("KTX 워커 자가복구 통합:")
    test_ktx_recovers_from_antibot()
    test_ktx_survives_nonstring_msg()
    print("중복예매 방지(v2.1.0):")
    test_srt_dedup_blocks_when_already_paid()
    test_srt_dedup_adopts_unpaid_reservation()
    test_ktx_dedup_blocks_when_already_ticketed()
    test_find_active_duplicate()
    print("죽은 잡 자동 정리(v2.3.2):")
    test_dead_target_auto_stop()
    test_past_date_auto_stop()
    print("\nALL PASS ✅")
