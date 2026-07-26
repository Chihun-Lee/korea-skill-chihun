"""세션 중단 방지 장치 검증 (네트워크 없이 가짜 클라이언트로).

검증 항목:
1. 최초 로그인 실패가 ERROR로 끝나지 않고 무한 재시도된다
2. 폴링 스레드가 죽으면 감시자가 자동 재시작한다
3. 활성 잡이 jobs.json에 저장되고 restore()로 복원된다 (중복 복원 방지 포함)
4. 결제확인 시간초과 후 폴링이 재개된다 (ERROR로 죽지 않음)

실행: venv/bin/python test_resilience.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

import jobstore
import srt_worker
from srt_worker import JobManager, JobSpec, JobStatus, PayMode

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class FakeCreds:
    srt_id = "tester"
    srt_password = "pw"
    card_number = "1234123412341234"
    card_password = "12"
    card_validation = "900101"
    card_expire = "2812"
    card_type = "J"
    card_installment = 0


class FakeSession:
    def request(self, *a, **kw):
        raise RuntimeError("no network in tests")


class FakeSRT:
    """login이 계속 실패하는 가짜 SRT 클라이언트."""
    fail_login = True

    def __init__(self, srt_id, srt_pw, auto_login=True, **kw):
        self._session = FakeSession()
        if auto_login:
            self.login()

    def login(self, *a, **kw):
        if type(self).fail_login:
            raise ConnectionError("network down")
        return True

    def get_reservations(self, paid_only=False):
        return []           # 기존 예약 없음 → 중복예매 사전검사 통과

    block = False

    def search_train(self, *a, **kw):
        while type(self).block:  # "멈춤"(무응답) 상황 흉내 — 플래그 해제 시 풀림
            time.sleep(0.1)
        return []


def wait_for(pred, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def spec():
    return JobSpec(
        dep="수서", arr="부산", date="20991201", time="080000",
        train_number=None, passengers=1, seat_pref="general",
        pay_mode=PayMode.MANUAL,
    )


def main() -> int:
    # 테스트 격리: 키체인/실 네트워크/실 jobs.json을 건드리지 않는다
    tmp = Path(tempfile.mkdtemp())
    jobstore.PATH = tmp / "jobs.json"
    srt_worker.config.srt.load = lambda: FakeCreds()
    srt_worker.SRT = FakeSRT
    srt_worker.WATCHDOG_PERIOD = 0.3
    srt_worker.HEARTBEAT_STALE = 2.0
    srt_worker.MIN_INTERVAL = 0.05
    srt_worker.MAX_INTERVAL = 0.1
    srt_worker.DEDUP_RETRY_BASE = 0.01

    # ── 1. 로그인 실패 = 무한 재시도 (ERROR로 죽지 않음) ──────────────
    mgr = JobManager()
    job = mgr.create(spec())
    ok = wait_for(lambda: sum("로그인 실패" in l for l in job.logs) >= 2, timeout=30)
    check("1a. 로그인 실패 후에도 계속 재시도", ok, f"logs={list(job.logs)}")
    check("1b. 상태가 ERROR가 아님", job.status != JobStatus.ERROR, str(job.status))

    # 로그인이 회복되면 폴링으로 진입
    FakeSRT.fail_login = False
    ok = wait_for(lambda: job.status == JobStatus.POLLING, timeout=30)
    check("1c. 네트워크 회복 후 폴링 진입", ok, str(job.status))

    # ── 2. 스레드가 죽으면 감시자가 재시작 ────────────────────────────
    gen_before = job._gen
    # 스레드 사망을 흉내: 살아있는 스레드 참조를 죽은 더미로 바꾼다
    dead = threading.Thread(target=lambda: None)
    dead.start(); dead.join()
    job._thread = dead
    ok = wait_for(lambda: job._gen > gen_before and job._thread.is_alive(), timeout=10)
    check("2a. 감시자가 죽은 스레드를 재시작", ok, f"gen {gen_before}->{job._gen}")
    ok = wait_for(lambda: any("감시자" in l for l in job.logs), timeout=5)
    check("2b. 재시작 로그 기록", ok)

    # 멈춤(심장박동 정지) 감지: search_train이 무한 대기하는 상황을 흉내
    gen_before = job._gen
    FakeSRT.block = True
    ok = wait_for(lambda: job._gen > gen_before, timeout=15)
    check("2c. 감시자가 무응답(멈춤) 잡을 재시작", ok, f"gen {gen_before}->{job._gen}")
    FakeSRT.block = False

    # ── 3. 영속화 + 복원 ──────────────────────────────────────────────
    ok = wait_for(lambda: jobstore.PATH.exists(), timeout=5)
    check("3a. 활성 잡이 jobs.json에 저장됨", ok and len(jobstore.load("srt")) == 1)

    mgr2 = JobManager()  # 새 프로세스를 흉내낸 새 매니저
    n = mgr2.restore()
    check("3b. restore()가 잡 1건 복원", n == 1, f"n={n}")
    n2 = mgr2.restore()  # 같은 매니저에서 재복원 → 중복 생성 금지
    check("3c. 중복 복원 방지", n2 == 0, f"n2={n2}")
    for j in mgr2.list():
        mgr2.stop(j.id)

    # 정지하면 저장소에서도 빠진다
    mgr.stop(job.id)
    ok = wait_for(lambda: len(jobstore.load("srt")) == 0, timeout=5)
    check("3d. 정지된 잡은 저장소에서 제거", ok, str(jobstore.load("srt")))

    # ── 4. 결제확인 시간초과 → 폴링 재개 ─────────────────────────────
    mgr4 = JobManager()
    job4 = mgr4.create(spec())
    wait_for(lambda: job4.status == JobStatus.POLLING, timeout=10)
    job4.status = JobStatus.RESERVED  # 예약 상태를 직접 시뮬레이션
    # _handle_payment의 시간초과 경로: _pay_event를 세우지 않고 timeout을 짧게
    orig_wait = job4._pay_event.wait
    job4._pay_event.wait = lambda timeout=None: orig_wait(0.2)
    done = mgr4._handle_payment(None, job4, FakeCreds())
    check("4a. 결제확인 시간초과 시 작업종료 아님(False)", done is False)
    check("4b. ERROR로 죽지 않음", job4.status != JobStatus.ERROR, str(job4.status))
    mgr4.stop(job4.id)

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)}개 실패: {FAILURES}")
        return 1
    print("✓ 전체 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
