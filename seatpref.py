"""좌석 선호 판정 로직 (창측 우선 + 호차 맨앞/맨뒷열 회피).

예약 API는 창측 '요청'(좌석속성코드 012)만 지원하고 열(row) 지정은 불가능하다.
그래서 예약 후 배정된 좌석을 여기 로직으로 채점하고, 점수가 낮으면 워커가
취소→재예약(개선 시도)을 할지 판단한다. 순수 함수만 두어 단위테스트 대상으로 삼는다.

좌석 배치 전제(KTX-I/산천/SRT 공통 관행):
- 일반실 2+2 배열: A·D 창측 / B·C 통로측
- 특실 2+1 배열: A 창측(2인측), C 1인석(창측) / B 통로측
- 열 번호는 1부터 시작, 마지막 열은 차종·호차마다 다르다(대략 일반실 14~18열,
  특실 7~9열). 정확한 호차별 배치표가 없으므로 보수적 추정치로 판정한다 —
  오판해도 '남은 좌석이 있을 때만 1~2회 재시도'라 표를 잃을 위험은 없다.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

# 창측 좌석 문자
WINDOW_LETTERS = {
    "general": {"A", "D"},
    "special": {"A", "C"},
}

# 이 열 이상이면 호차 뒷문 쪽으로 판정(보수적 추정)
BACK_ROW_MIN = {"general": 15, "special": 8}
# 이 열 이하면 호차 앞문 쪽
FRONT_ROW_MAX = 1

_SEAT_RE = re.compile(r"^(\d+)\s*([A-Za-z])$")


class SeatInfo(NamedTuple):
    row: Optional[int]
    letter: Optional[str]


def parse_seat(seat: object) -> SeatInfo:
    """'12A' → (12, 'A'). 형식이 다르면 (None, None) — 판정 불가는 '양호' 취급."""
    if not seat:
        return SeatInfo(None, None)
    m = _SEAT_RE.match(str(seat).strip())
    if not m:
        return SeatInfo(None, None)
    return SeatInfo(int(m.group(1)), m.group(2).upper())


def _seat_class(seat_type: object) -> str:
    return "special" if seat_type and "특" in str(seat_type) else "general"


def is_window(seat: object, seat_type: object = None) -> bool:
    info = parse_seat(seat)
    if info.letter is None:
        return True  # 판정 불가 → 개선 시도 대상에서 제외
    return info.letter in WINDOW_LETTERS[_seat_class(seat_type)]


def is_edge_row(seat: object, seat_type: object = None) -> bool:
    info = parse_seat(seat)
    if info.row is None:
        return False
    cls = _seat_class(seat_type)
    return info.row <= FRONT_ROW_MAX or info.row >= BACK_ROW_MIN[cls]


def seat_score(seat: object, seat_type: object = None,
               want_window: bool = True, avoid_edge: bool = True) -> int:
    """단일 좌석 점수. 창측(사용자 1순위)=+2, 앞뒤열 아님=+1. 만점은 max_score()."""
    score = 0
    if not want_window or is_window(seat, seat_type):
        score += 2
    if not avoid_edge or not is_edge_row(seat, seat_type):
        score += 1
    return score


def max_score() -> int:
    return 3


def group_score(seats: list, seat_types: Optional[list] = None,
                want_window: bool = True, avoid_edge: bool = True) -> int:
    """여러 좌석(다인 예매)은 가장 나쁜 좌석 기준으로 채점한다."""
    if not seats:
        return max_score()
    types = seat_types or [None] * len(seats)
    return min(
        seat_score(s, t, want_window, avoid_edge)
        for s, t in zip(seats, types)
    )


def describe(seats: list, seat_types: Optional[list] = None) -> str:
    """로그용 좌석 평가 요약: 각 좌석에 채점 사유(창측/통로측, 맨앞·뒷열)를 붙인다."""
    parts = []
    types = seat_types or [None] * len(seats)
    for s, t in zip(seats, types):
        tags = []
        tags.append("창측" if is_window(s, t) else "통로측")
        if is_edge_row(s, t):
            tags.append("맨앞/뒷열")
        parts.append(f"{s}({','.join(tags)})")
    return ", ".join(parts)
