"""v3.0 통합 엔진 핵심 로직 검증 (네트워크 불필요).

검증 목표:
  1) 조회 페이지네이션 — 코레일이 ~10편씩만 주므로 하루치를 이어 받아야 한다
     (이걸 안 하면 늦은 열차는 목록에 없어 "잡을 수 없는 열차"가 된다)
  2) 지정 열차를 찾으면 즉시 멈춘다 (불필요한 요청 = 안티봇 부하)
  3) 잡 대상 선택이 '열차번호' 기준이다 (차종코드는 편성마다 바뀌어 못 믿는다)
  4) 운영사 표기(SRT / KTX)가 열차번호로 갈린다
  5) 역명 표준화 — 별칭 흡수, 오타 차단

실행:  venv/bin/python test_v3.py
"""
from __future__ import annotations

import types

import rail_worker
import stations


def _train(no, hhmm):
    return types.SimpleNamespace(train_no=no, dep_time=hhmm + "00")


class _PagedClient:
    """코레일처럼 한 번에 page_size편씩만, 요청 시각 이후로 돌려주는 가짜 클라이언트."""

    def __init__(self, trains, page_size=10):
        self.trains = sorted(trains, key=lambda t: t.dep_time)
        self.page_size = page_size
        self.calls = []

    def search_train(self, dep, arr, date, time, **kw):
        self.calls.append(time)
        later = [t for t in self.trains if t.dep_time >= time]
        return later[: self.page_size]


DAY = [_train(str(300 + i), f"{6 + i:02d}{(i * 7) % 60:02d}") for i in range(16)]


def test_pagination_collects_whole_day():
    c = _PagedClient(DAY)
    got = rail_worker.search_all(c, "수서", "동대구", "20261015", "000000")
    assert len(got) == len(DAY), f"{len(got)}편만 받음 (기대 {len(DAY)}편)"
    assert [t.train_no for t in got] == [t.train_no for t in DAY], "시각순 정렬 깨짐"
    assert len(c.calls) >= 2, "페이지를 넘기지 않음"
    print(f"  [ok] 하루치 {len(got)}편 수집 (요청 {len(c.calls)}회, 페이지당 10편)")


def test_pagination_stops_at_target():
    c = _PagedClient(DAY)
    got = rail_worker.search_all(
        c, "수서", "동대구", "20261015", "000000",
        stop_when=lambda t: t.train_no == "303")
    assert any(t.train_no == "303" for t in got)
    assert len(c.calls) == 1, f"첫 페이지에 있는데 {len(c.calls)}회 요청"
    print("  [ok] 첫 페이지에서 대상 발견 → 추가 요청 없음")


def test_pagination_reaches_late_train():
    """v2의 실제 사고: 목록이 10편에서 잘려 늦은 열차를 영영 못 잡았다."""
    c = _PagedClient(DAY)
    late = DAY[-1].train_no
    got = rail_worker.search_all(
        c, "수서", "동대구", "20261015", "000000",
        stop_when=lambda t: t.train_no == late)
    assert any(t.train_no == late for t in got), "막차를 못 찾음"
    assert len(c.calls) >= 2
    print(f"  [ok] 10편 밖의 늦은 열차({late}편)도 도달 (요청 {len(c.calls)}회)")


def test_pagination_no_infinite_loop():
    """같은 페이지만 반복해 오는 서버에도 갇히지 않는다."""
    class _Stuck:
        def search_train(self, *a, **kw):
            return [_train("301", "0600")]
    got = rail_worker.search_all(_Stuck(), "수서", "부산", "20261015", "000000")
    assert len(got) == 1
    print("  [ok] 중복 응답 반복 시 즉시 종료 (무한루프 없음)")


def test_target_time_cache_semantics():
    """한 번 찾은 열차는 그 출발시각부터 조회 → 페이지를 다시 안 넘긴다."""
    c = _PagedClient(DAY)
    late = DAY[-1]
    rail_worker.search_all(c, "수서", "동대구", "20261015", late.dep_time,
                           stop_when=lambda t: t.train_no == late.train_no)
    assert len(c.calls) == 1, f"대상 시각부터 조회했는데 {len(c.calls)}회 요청"
    print("  [ok] 대상 출발시각부터 조회하면 요청 1회")


def _spec(**kw):
    base = dict(dep="수서", arr="동대구", date="20261015", time="060000",
                train_number=None, train_id=None, train_type="all", passengers=1,
                seat_pref="general", pay_mode=rail_worker.PayMode.AUTO)
    base.update(kw)
    return rail_worker.JobSpec(**base)


def test_pick_target_by_train_number():
    trains = [_train("305", "0654"), _train("381", "0654"), _train("215", "1347")]
    pick = rail_worker.JobManager._pick_target
    assert pick(trains, _spec(train_number="381")).train_no == "381"
    assert pick(trains, _spec(train_number="00381")).train_no == "381", "0-패딩 미흡수"
    assert pick(trains, _spec(train_number="999")) is None
    assert pick(trains, _spec()).train_no == "305", "미지정이면 첫차"
    print("  [ok] 열차번호로 대상 선택 (0-패딩 흡수, 미지정 시 첫차)")


def test_operator_label():
    assert rail_worker.operator("305") == "SRT"   # 경부 SRT
    assert rail_worker.operator("601") == "SRT"   # 호남 SRT
    assert rail_worker.operator("215") == ""      # 코레일 KTX(경전)
    assert rail_worker.operator("3") == ""        # 코레일 KTX(경부)
    assert rail_worker.operator("9069") == ""     # 임시열차
    print("  [ok] 운영사 표기: 3xx/6xx = SRT, 그 외 = 코레일")


def test_station_names():
    assert stations.canonical("김천구미") == "김천(구미)"
    assert stations.canonical("신경주") == "경주"
    assert stations.is_known("수서") and stations.is_known("동탄") and stations.is_known("평택지제")
    assert not stations.is_known("없는역")
    assert len(stations.ALL) == len(set(stations.ALL)), "역 목록에 중복"
    print(f"  [ok] 역 {len(stations.ALL)}개 · 별칭 흡수 · 오타 차단")


if __name__ == "__main__":
    print("조회 페이지네이션(v3.0 핵심):")
    test_pagination_collects_whole_day()
    test_pagination_stops_at_target()
    test_pagination_reaches_late_train()
    test_pagination_no_infinite_loop()
    test_target_time_cache_semantics()
    print("잡 대상 선택:")
    test_pick_target_by_train_number()
    print("표기·역명:")
    test_operator_label()
    test_station_names()
    print("\nALL PASS ✅")
