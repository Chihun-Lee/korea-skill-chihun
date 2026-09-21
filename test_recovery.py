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

from srtgo.ktx import KorailError

import tempfile
from pathlib import Path

import jobstore
import recovery
import rail_worker

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


class _FakeSession:
    """_force_session_timeout가 감쌀 수 있도록 request 속성만 가진 더미 세션."""
    def request(self, method, url, **kw):
        return None


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
    orig_rc = rail_worker.RecoveryController
    rail_worker.RecoveryController = lambda *a, **k: orig_rc(
        base=0.01, cap=0.05, fresh_login_every=2, jitter=(1.0, 1.0)
    )
    rail_worker.PatchedKorail = fake_cls
    rail_worker.MIN_INTERVAL = 0.01
    rail_worker.MAX_INTERVAL = 0.01
    rail_worker.SESSION_MAX_AGE = 1e9
    rail_worker.STALL_LIMIT = 1e9
    rail_worker.config.rail.load = lambda: types.SimpleNamespace(
        rail_id="tester", rail_password="pw", card_number="",
        card_password="", card_validation="", card_expire="", card_installment=0,
    )


def _run_ktx_job():
    spec = rail_worker.JobSpec(
        dep="서울", arr="부산", date="20991201", time="090000",
        train_number=None, train_id="NONE|0|0", train_type="ktx", passengers=1,
        seat_pref="any", pay_mode=rail_worker.PayMode.MANUAL,
    )
    job = rail_worker.manager.create(spec)
    deadline = time.time() + 5
    while time.time() < deadline and FakeKorail.successes < 1:
        time.sleep(0.05)
    rail_worker.manager.stop(job.id)
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


def _wait_status(job, statuses, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and job.status not in statuses:
        time.sleep(0.05)
    return job.status


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
    rail_worker.DEDUP_RETRY_BASE = 0.01
    FakeKorailWithHistory.ticket_list = [types.SimpleNamespace(
        dep_name="서울", arr_name="부산", dep_date="20991201", train_no="101",
    )]
    FakeKorailWithHistory.searched = 0
    spec = rail_worker.JobSpec(
        dep="서울", arr="부산", date="20991201", time="090000",
        train_number=None, train_id=None, train_type="ktx", passengers=1,
        seat_pref="any", pay_mode=rail_worker.PayMode.MANUAL,
    )
    job = rail_worker.manager.create(spec)
    st = _wait_status(job, (rail_worker.JobStatus.PAID,))
    rail_worker.manager.stop(job.id)
    assert st == rail_worker.JobStatus.PAID, f"status={st}"
    assert FakeKorailWithHistory.searched == 0, "발권된 표가 있는데 검색(재예매 시도)함"
    print("  [ok] KTX 발권완료 표 감지 → 재예매 없이 즉시 종료(PAID)")


# ── 3. HTTP 타임아웃 강제 (행 방지) ────────────────────────────────────


if __name__ == "__main__":
    print("recovery 백오프/에스컬레이션:")
    test_backoff_grows_and_caps()
    test_fresh_login_escalation()
    test_success_resets()
    print("HTTP 타임아웃(행 방지):")
    print("KTX 워커 자가복구 통합:")
    test_ktx_recovers_from_antibot()
    test_ktx_survives_nonstring_msg()
    print("중복예매 방지(v2.1.0):")
    test_ktx_dedup_blocks_when_already_ticketed()
    print("죽은 잡 자동 정리(v2.3.2):")
    print("\nALL PASS ✅")
