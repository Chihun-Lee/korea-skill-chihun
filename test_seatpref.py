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
import srt_worker
import ktx_worker
from SRT.errors import SRTError


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


def test_train_no_padding():
    assert srt_worker._same_train_no("323", "00323")
    assert ktx_worker._same_train_no("00123", "123")
    assert not srt_worker._same_train_no("323", "324")
    spec = srt_worker.JobSpec(
        dep="수서", arr="창원중앙", date="20991201", time="090000",
        train_number="323", passengers=1, seat_pref="general",
        pay_mode=srt_worker.PayMode.AUTO,
    )
    r = types.SimpleNamespace(
        dep_station_name="수서", arr_station_name="창원중앙",
        dep_date="20991201", train_number="00323", paid=False,
    )
    assert srt_worker._spec_matches_reservation(spec, r), \
        "0-패딩 차이로 같은 열차 예약을 중복으로 못 잡음"
    print("  [ok] 열차번호 0-패딩 차이 흡수('323'=='00323') — 중복검사 누락 버그 수정")


# ── 2. SRT 사후 스윕 / 좌석 개선 ────────────────────────────────────────
def _srt_spec(**kw):
    base = dict(
        dep="수서", arr="창원중앙", date="20991201", time="090000",
        train_number=None, passengers=1, seat_pref="general",
        pay_mode=srt_worker.PayMode.AUTO,
    )
    base.update(kw)
    return srt_worker.JobSpec(**base)


def _res(no, train="00301", seats=("7A",), paid=False, types_=("일반실",)):
    return types.SimpleNamespace(
        reservation_number=no, train_number=train, paid=paid,
        dep_station_name="수서", arr_station_name="창원중앙", dep_date="20991201",
        payment_date="20991201", payment_time="235900",
        tickets=[types.SimpleNamespace(seat=s, seat_type=t)
                 for s, t in zip(seats, types_)],
    )


class FakeSRTSweep:
    def __init__(self, history):
        self.history = history
        self.cancelled = []

    def get_reservations(self):
        return list(self.history)

    def cancel(self, r):
        self.cancelled.append(r.reservation_number)
        return True


def test_srt_sweep_cancels_same_train_extra():
    ours = _res("R1")
    extra = _res("R2")                      # 같은 열차 중복 (사고)
    other_train = _res("R3", train="00777")  # 다른 열차 (의도적일 수 있음)
    srt = FakeSRTSweep([ours, extra, other_train])
    job = srt_worker.Job(id="t1", spec=_srt_spec())
    kept, done = srt_worker.manager._post_reserve_sweep(srt, job, ours)
    assert kept is ours and not done
    assert srt.cancelled == ["R2"], f"초과분만 취소해야 함: {srt.cancelled}"
    print("  [ok] SRT 스윕: 같은 열차 중복(R2)만 취소, 다른 열차(R3)는 보존")


def test_srt_sweep_yields_to_paid_ticket():
    ours = _res("R1")
    paid = _res("R2", paid=True)
    srt = FakeSRTSweep([ours, paid])
    job = srt_worker.Job(id="t2", spec=_srt_spec())
    kept, done = srt_worker.manager._post_reserve_sweep(srt, job, ours)
    assert kept is None and done
    assert srt.cancelled == ["R1"], "이미 결제표가 있으면 방금 예약을 취소해야 함"
    assert job.status == srt_worker.JobStatus.PAID
    assert "[기존 결제 표 사용]" in (job.reservation_summary or "")
    print("  [ok] SRT 스윕: 기존 결제표 발견 → 신규예약 취소 + PAID 종료(중복결제 차단)")


class FakeSRTImprove:
    """개선 흐름용: 검색→(취소→재예약) 기록."""

    def __init__(self, first_seats, retry_seats, available=True, window_fails=False):
        self.available = available
        self.window_fails = window_fails
        self.retry_seats = retry_seats
        self.cancelled = []
        self.reserve_calls = []  # (window_seat,) 기록

    def search_train(self, *a, **kw):
        avail = self.available
        return [types.SimpleNamespace(
            train_number="301",
            general_seat_available=lambda: avail,
            special_seat_available=lambda: False,
        )]

    def cancel(self, r):
        self.cancelled.append(r.reservation_number)
        return True

    def reserve(self, target, passengers=None, special_seat=None, window_seat=None):
        self.reserve_calls.append(window_seat)
        if window_seat and self.window_fails:
            raise SRTError("창측 좌석 없음")
        return _res("R-NEW", seats=self.retry_seats)


def test_srt_improve_swaps_bad_seat():
    ours = _res("R1", seats=("1B",))  # 통로측 + 맨앞열 = 최악
    srt = FakeSRTImprove(("1B",), ("7A",))
    job = srt_worker.Job(id="t3", spec=_srt_spec())
    from SRT import SeatType, Adult
    out = srt_worker.manager._improve_seat(
        srt, job, [Adult(1)], SeatType.GENERAL_FIRST, ours, lambda: True)
    assert out.reservation_number == "R-NEW"
    assert srt.cancelled == ["R1"]
    assert srt.reserve_calls[0] is True, "창측 우선으로 재예약해야 함"
    print("  [ok] SRT 개선: 나쁜 좌석(1B) + 잔여석 있음 → 취소·재예약(창측)으로 교체")


def test_srt_improve_keeps_last_seat():
    ours = _res("R1", seats=("1B",))
    srt = FakeSRTImprove(("1B",), ("7A",), available=False)  # 남은 좌석 없음
    job = srt_worker.Job(id="t4", spec=_srt_spec())
    from SRT import SeatType, Adult
    out = srt_worker.manager._improve_seat(
        srt, job, [Adult(1)], SeatType.GENERAL_FIRST, ours, lambda: True)
    assert out is ours and srt.cancelled == [], "마지막 남은 자리는 그대로 진행해야 함"
    print("  [ok] SRT 개선: 잔여석 없음 → 나쁜 좌석이라도 취소하지 않고 그대로 진행")


def test_srt_improve_skips_group_booking():
    ours = _res("R1", seats=("1B", "1C"))
    srt = FakeSRTImprove(("1B",), ("7A",))
    job = srt_worker.Job(id="t5", spec=_srt_spec(passengers=2))
    from SRT import SeatType, Adult
    out = srt_worker.manager._improve_seat(
        srt, job, [Adult(2)], SeatType.GENERAL_FIRST, ours, lambda: True)
    assert out is ours and srt.cancelled == [], "다인 예매는 개선 대상이 아님"
    print("  [ok] SRT 개선: 2인 이상 예매는 좌석 개선 생략(나란한 좌석 우선)")


def test_srt_window_fallback():
    srt = FakeSRTImprove(("7A",), ("7B",), window_fails=True)
    job = srt_worker.Job(id="t6", spec=_srt_spec())
    from SRT import SeatType, Adult
    target = types.SimpleNamespace(train_number="301")
    out = srt_worker.manager._reserve_with_pref(
        srt, job, target, [Adult(1)], SeatType.GENERAL_FIRST)
    assert out is not None
    assert srt.reserve_calls == [True, None], \
        f"창측 실패 후 위치 무관으로 재시도해야 함: {srt.reserve_calls}"
    print("  [ok] SRT 창측 폴백: 창측 실패 → 위치 무관 즉시 재시도(자리 놓치지 않음)")


# ── 3. KTX 사후 스윕 / 좌석 개선 ────────────────────────────────────────
def _ktx_spec(**kw):
    base = dict(
        dep="동대구", arr="서울", date="20991201", time="090000",
        train_id=None, train_type="ktx", passengers=1, seat_pref="general",
        pay_mode=ktx_worker.PayMode.AUTO,
    )
    base.update(kw)
    return ktx_worker.JobSpec(**base)


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
    job = ktx_worker.Job(id="k1", spec=_ktx_spec())
    kept, done = ktx_worker.manager._post_reserve_sweep(client, job, ours)
    assert kept is ours and not done
    assert client.cancelled == ["P2"]
    print("  [ok] KTX 스윕: 같은 열차 중복(P2)만 취소")


def test_ktx_sweep_yields_to_paid_ticket():
    ours = _krsv("P1")
    paid_ticket = types.SimpleNamespace(
        dep_name="동대구", arr_name="서울", dep_date="20991201", train_no="00123")
    client = FakeKorailSweep([paid_ticket], [ours])
    job = ktx_worker.Job(id="k2", spec=_ktx_spec())
    kept, done = ktx_worker.manager._post_reserve_sweep(client, job, ours)
    assert kept is None and done
    assert client.cancelled == ["P1"]
    assert job.status == ktx_worker.JobStatus.PAID
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
    job = ktx_worker.Job(id="k3", spec=_ktx_spec())
    from srtgo.ktx import AdultPassenger, ReserveOption
    out = ktx_worker.manager._improve_seat(
        client, job, [AdultPassenger(1)], ReserveOption.GENERAL_FIRST,
        ours, lambda: True)
    assert out.rsv_id == "P-NEW"
    assert client.cancelled == ["P1"]
    assert client.reserve_locs[0] == ktx_worker.SEAT_LOC_WINDOW
    print("  [ok] KTX 개선: 나쁜 좌석(15B) → 창측(012) 요청으로 재예약")


def test_ktx_improve_skips_waiting():
    ours = _krsv("P1", seats=(), waiting=True)
    client = FakeKorailImprove(("7D",))
    job = ktx_worker.Job(id="k4", spec=_ktx_spec(include_waiting=True))
    from srtgo.ktx import AdultPassenger, ReserveOption
    out = ktx_worker.manager._improve_seat(
        client, job, [AdultPassenger(1)], ReserveOption.GENERAL_FIRST,
        ours, lambda: True)
    assert out is ours and client.cancelled == []
    print("  [ok] KTX 개선: 예약대기(좌석 미배정)는 개선 시도 안 함")


if __name__ == "__main__":
    print("seatpref 순수 로직:")
    test_parse_and_window()
    test_edge_rows()
    test_scoring()
    test_train_no_padding()
    print("SRT 사후 스윕/좌석 개선:")
    test_srt_sweep_cancels_same_train_extra()
    test_srt_sweep_yields_to_paid_ticket()
    test_srt_improve_swaps_bad_seat()
    test_srt_improve_keeps_last_seat()
    test_srt_improve_skips_group_booking()
    test_srt_window_fallback()
    print("KTX 사후 스윕/좌석 개선:")
    test_ktx_sweep_cancels_same_train_extra()
    test_ktx_sweep_yields_to_paid_ticket()
    test_ktx_improve_swaps_bad_seat()
    test_ktx_improve_skips_waiting()
    print("\nALL PASS ✅")
