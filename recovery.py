"""속도제한/안티봇 차단에 대한 적응형 백오프·세션 재생성 컨트롤러.

코레일 안티봇 차단(MACRO ERROR)과 같은 일시 차단은
둘 다 **IP 단위 속도 제한**이다. 빠르게 재시도할수록 차단이 유지되므로,
기존처럼 1~30초마다 새 세션으로 하던 재시도는 오히려 차단을 연장시켜
"창은 떠있는데 예매만 멈춤" 상태로 빠진다.

유일한 해법은 '천천히 물러나 제한이 풀릴 시간을 주는' 것이다. 이 컨트롤러는
연속 실패 횟수에 따라 대기시간을 지수적으로(상한 포함) 늘리고, 일정 횟수마다
완전히 새 세션 로그인으로 에스컬레이션한다. 순수 로직이라 네트워크 없이
단위 테스트가 가능하다(test_recovery.py).
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Recovery:
    sleep: float          # 이번 실패 후 대기할 초
    fresh_login: bool     # True면 완전 새 세션으로 재로그인할 차례
    streak: int           # 현재 연속 실패 횟수


class RecoveryController:
    """연속 실패에 대한 지수 백오프 + 주기적 새 세션 에스컬레이션.

    base=5, cap=60, fresh_login_every=4 기준 대기(지터 제외):
        1회 5s · 2회 10s · 3회 20s · 4회 40s(+새세션) · 5회 60s · 6회 60s ...
    한 번이라도 성공하면 on_success()로 streak이 0으로 리셋된다.
    """

    def __init__(
        self,
        base: float = 5.0,
        cap: float = 60.0,
        fresh_login_every: int = 4,
        jitter: tuple[float, float] = (0.7, 1.3),
    ) -> None:
        self.base = base
        self.cap = cap
        self.fresh_login_every = fresh_login_every
        self.jitter = jitter
        self.streak = 0

    def on_success(self) -> None:
        self.streak = 0

    def on_error(self) -> Recovery:
        self.streak += 1
        delay = min(self.cap, self.base * (2 ** (self.streak - 1)))
        lo, hi = self.jitter
        if hi > lo:
            delay *= random.uniform(lo, hi)
        fresh = self.streak % self.fresh_login_every == 0
        return Recovery(sleep=delay, fresh_login=fresh, streak=self.streak)
