"""v2.4.0 좌석 선호 + 중복예매 방어 강화 검증 (네트워크 불필요).

검증 목표:
  1) seatpref 순수 로직 — 좌석 파싱, 창측/맨앞·뒷열 판정, 채점
  2) 열차번호 0-패딩 비교 (기존 중복검사가 '323' vs '00323'을 놓치던 버그)
  3) 예약 직후 사후 스윕 — 같은 열차 중복 예약 초과분 취소 / 기존 결제표 발견
     시 신규예약 취소 후 종료 / 다른 열차 예약은 보존
  4) 좌석 개선 — 나쁜 좌석 + 잔여석 있음 → 취소·재예약으로 개선,
     잔여석 없음 → 그대로 유지(사용자 원칙)
  5) 창측 예약 폴백 — 창측 요청 실패 시 위치 무관으로 즉시 재시도

실행:  venv/bin/python test_seatpref.py
"""
from __future__ import annotations

import tempfile
import types
from pathlib import Path

import jobstore

# 테스트가 실제 ~/.k-rail-macro/jobs.json 을 건드리지 않도록 격리
jobstore.PATH = Path(tempfile.mkdtemp()) / "jobs.json"

import seatpref
import rail_worker


# ── 1. seatpref 순수 로직 ────────────────────────────────────────────────
def test_parse_and_window():
    assert seatpref.parse_seat("12A") == (12, "A")
    assert seatpref.parse_seat("3d") == (3, "D")
    assert seatpref.parse_seat("") == (None, None)
    assert seatpref.parse_seat("좌석없음") == (None, None)
    # 일반실 2+2: A/D 창측
    assert seatpref.is_window("5A") and seatpref.is_window("5D")
    assert not seatpref.is_window("5B") and not seatpref.is_window("5C")
    # 특실 2+1: A/C 창측
    assert seatpref.is_window("5C", "특실")
    assert not seatpref.is_window("5B", "특실")
    print("  [ok] 좌석 파싱 + 창측 판정 (일반실 A/D, 특실 A/C)")


def test_edge_rows():
    assert seatpref.is_edge_row("1A")            # 맨앞
    assert seatpref.is_edge_row("15C")           # 일반실 뒷열 추정
    assert not seatpref.is_edge_row("7B")
    assert seatpref.is_edge_row("8A", "특실")    # 특실 뒷열 추정
    assert not seatpref.is_edge_row("5A", "특실")
    print("  [ok] 맨앞(1열)/뒷열(일반실 15+, 특실 8+) 판정")


def test_scoring():
    assert seatpref.seat_score("7A") == 3        # 창측 + 중간열 = 만점
    assert seatpref.seat_score("7B") == 1        # 통로측 + 중간열
    assert seatpref.seat_score("1A") == 2        # 창측 + 맨앞열
    assert seatpref.seat_score("1B") == 0        # 통로측 + 맨앞열
    # 다인 예매는 최악 좌석 기준
    assert seatpref.group_score(["7A", "1B"]) == 0
    # 선호 끔 → 해당 감점 없음
    assert seatpref.seat_score("1B", want_window=False, avoid_edge=False) == 3
    print("  [ok] 채점(창측 2점 + 비가장자리 1점, 그룹은 최악 기준)")


def _res(no, train="00301", seats=("7A",), paid=False, types_=("일반실",)):
    return types.SimpleNamespace(
        reservation_number=no, train_number=train, paid=paid,
        dep_station_name="수서", arr_station_name="창원중앙", dep_date="20991201",
        payment_date="20991201", payment_time="235900",
        tickets=[types.SimpleNamespace(seat=s, seat_type=t)
                 for s, t in zip(seats, types_)],
    )


# ── 3. KTX 사후 스윕 / 좌석 개선 ────────────────────────────────────────
def _ktx_spec(**kw):
    base = dict(
        dep="동대구", arr="서울", date="20991201", time="090000",
        train_number=None, train_id=None, train_type="ktx", passengers=1, seat_pref="general",
        pay_mode=rail_worker.PayMode.AUTO,
    )
    base.update(kw)
    return rail_worker.JobSpec(**base)


def _krsv(rsv_id, train="00123", seats=("7A",), waiting=False):
    return types.SimpleNamespace(
        rsv_id=rsv_id, train_no=train, is_waiting=waiting,
        dep_name="동대구", arr_name="서울", dep_date="20991201",
        buy_limit_date="20991201", buy_limit_time="235900",
        tickets=[types.SimpleNamespace(seat=s, seat_type="일반실") for s in seats],
    )


class FakeKorailSweep:
    def __init__(self, tickets_, rsvs):
        self._tickets = tickets_
        self._rsvs = rsvs
        self.cancelled = []

    def tickets(self):
        return list(self._tickets)

    def reservations(self, rsv_id=None):
        return list(self._rsvs)

    def cancel(self, r):
        self.cancelled.append(r.rsv_id)
        return True


def test_ktx_sweep_cancels_same_train_extra():
    ours = _krsv("P1")
    extra = _krsv("P2")
    client = FakeKorailSweep([], [ours, extra])
    job = rail_worker.Job(id="k1", spec=_ktx_spec())
    kept, done = rail_worker.manager._post_reserve_sweep(client, job, ours)
    assert kept is ours and not done
    assert client.cancelled == ["P2"]
    print("  [ok] KTX 스윕: 같은 열차 중복(P2)만 취소")


def test_ktx_sweep_yields_to_paid_ticket():
    ours = _krsv("P1")
    paid_ticket = types.SimpleNamespace(
        dep_name="동대구", arr_name="서울", dep_date="20991201", train_no="00123")
    client = FakeKorailSweep([paid_ticket], [ours])
    job = rail_worker.Job(id="k2", spec=_ktx_spec())
    kept, done = rail_worker.manager._post_reserve_sweep(client, job, ours)
    assert kept is None and done
    assert client.cancelled == ["P1"]
    assert job.status == rail_worker.JobStatus.PAID
    print("  [ok] KTX 스윕: 기존 발권표 발견 → 신규예약 취소 + PAID 종료")


class FakeKorailImprove:
    def __init__(self, retry_seats, available=True):
        self.available = available
        self.retry_seats = retry_seats
        self.cancelled = []
        self.reserve_locs = []

    def search_train(self, *a, **kw):
        avail = self.available
        return [types.SimpleNamespace(
            train_no="123",
            has_general_seat=lambda: avail,
            has_special_seat=lambda: False,
            has_seat=lambda: avail,
        )]

    def cancel(self, r):
        self.cancelled.append(r.rsv_id)
        return True

    def reserve(self, target, passengers=None, option=None, seat_location="000"):
        self.reserve_locs.append(seat_location)
        return _krsv("P-NEW", seats=self.retry_seats)


def test_ktx_improve_swaps_bad_seat():
    ours = _krsv("P1", seats=("15B",))  # 통로측 + 뒷열
    client = FakeKorailImprove(("7D",))
    job = rail_worker.Job(id="k3", spec=_ktx_spec())
    from srtgo.ktx import AdultPassenger, ReserveOption
    out = rail_worker.manager._improve_seat(
        client, job, [AdultPassenger(1)], ReserveOption.GENERAL_FIRST,
        ours, lambda: True)
    assert out.rsv_id == "P-NEW"
    assert client.cancelled == ["P1"]
    assert client.reserve_locs[0] == rail_worker.SEAT_LOC_WINDOW
    print("  [ok] KTX 개선: 나쁜 좌석(15B) → 창측(012) 요청으로 재예약")


def test_ktx_improve_skips_waiting():
    ours = _krsv("P1", seats=(), waiting=True)
    client = FakeKorailImprove(("7D",))
    job = rail_worker.Job(id="k4", spec=_ktx_spec(include_waiting=True))
    from srtgo.ktx import AdultPassenger, ReserveOption
    out = rail_worker.manager._improve_seat(
        client, job, [AdultPassenger(1)], ReserveOption.GENERAL_FIRST,
        ours, lambda: True)
    assert out is ours and client.cancelled == []
    print("  [ok] KTX 개선: 예약대기(좌석 미배정)는 개선 시도 안 함")


if __name__ == "__main__":
    print("seatpref 순수 로직:")
    test_parse_and_window()
    test_edge_rows()
    test_scoring()
    print("KTX 사후 스윕/좌석 개선:")
    test_ktx_sweep_cancels_same_train_extra()
    test_ktx_sweep_yields_to_paid_ticket()
    test_ktx_improve_swaps_bad_seat()
    test_ktx_improve_skips_waiting()
    print("\nALL PASS ✅")
