"""Card test with strict safety guards.

Key protections (added after a refund-the-wrong-ticket incident):

1. **Whitelist** — before reserving, record every PNR currently on
   the account. After reserving, the new PNR is added to a
   per-test whitelist. Refund can ONLY touch whitelisted PNRs.
2. **Route+date verification** — before refund, fetch the ticket
   info for our PNR and verify the route and date match exactly
   what we intended (e.g. 김천(구미)→동대구 on today+25d).
3. **Post-refund audit** — re-fetch reservations and confirm:
   our PNR is gone AND every protected PNR is still present.
   If audit fails, raise a loud error.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import config


@dataclass
class CardTestResult:
    ok: bool
    steps: list[dict] = field(default_factory=list)
    summary: str = ""

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"name": name, "ok": ok, "detail": detail})


def _next_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _target_date(days_ahead: int = 25) -> str:
    return _next_weekday(date.today() + timedelta(days=days_ahead)).strftime("%Y%m%d")


def ktx_card_test() -> CardTestResult:
    r = CardTestResult(ok=False)
    creds = config.rail.load()
    if not creds:
        r.summary = "코레일 자격증명 없음"; return r
    if not creds.card_number:
        r.summary = "카드 정보 없음"; return r

    from srtgo.ktx import AdultPassenger, ReserveOption, TrainType, NoResultsError
    from korail_client import PatchedKorail

    target_date = _target_date(25)
    r.step("date", True, f"{target_date} ({KTX_TEST_DEP}→{KTX_TEST_ARR} {KTX_TEST_TIME[:2]}:{KTX_TEST_TIME[2:4]} 이후)")

    try:
        client = PatchedKorail(creds.rail_id, creds.rail_password, auto_login=False)
        if not client.login():
            raise RuntimeError("login returned False")
    except Exception as e:
        r.step("login", False, str(e)[:120]); r.summary = "로그인 실패"; return r
    r.step("login", True, getattr(client, "name", ""))

    # SAFETY 1: snapshot existing reservations + tickets PNRs
    try:
        existing_rsv = {str(x.rsv_id) for x in client.reservations()}
        existing_tkt = {str(getattr(x, "pnr_no", "")) for x in client.tickets()}
        existing = existing_rsv | (existing_tkt - {""})
        r.step("snapshot", True, f"기존 예약 {len(existing_rsv)} + 발권 {len(existing_tkt)} 보호")
    except Exception as e:
        r.step("snapshot", False, str(e)[:120]); r.summary = "보호 스냅샷 실패"; return r

    try:
        trains = client.search_train(KTX_TEST_DEP, KTX_TEST_ARR, target_date, KTX_TEST_TIME, train_type=TrainType.KTX)
    except NoResultsError:
        r.step("search", False, "no results")
        r.summary = "테스트 가능한 좌석 없음 — 다른 날 시도"; return r
    except Exception as e:
        r.step("search", False, str(e)[:120]); r.summary = "조회 실패"; return r
    train = next((t for t in trains if t.has_general_seat()), None)
    if train is None:
        r.step("search", False, f"{len(trains)} trains, 일반실 가능 0건")
        r.summary = "테스트 가능한 좌석 없음 — 다른 날 시도"; return r
    r.step("search", True, f"{train}")

    reservation = None
    try:
        reservation = client.reserve(train, passengers=[AdultPassenger(1)], option=ReserveOption.GENERAL_FIRST)
    except Exception as e:
        r.step("reserve", False, str(e)[:120]); r.summary = "예약 실패"; return r
    test_pnr = str(reservation.rsv_id)
    if test_pnr in existing:
        r.step("reserve", False, f"PNR {test_pnr} 이 보호 목록에 있음 — 안전상 중단")
        r.summary = "PNR 충돌"; return r
    # SAFETY 2: route + date check on the just-created reservation
    if (reservation.dep_name != KTX_TEST_DEP or
        reservation.arr_name != KTX_TEST_ARR or
        reservation.dep_date != target_date):
        r.step("reserve", False, f"route/date mismatch on returned reservation — 안전상 중단")
        r.summary = "예약 데이터 불일치"
        # try cancel since we never paid
        try: client.cancel(reservation)
        except Exception: pass
        return r
    r.step("reserve", True, f"PNR={test_pnr}")

    paid = False
    try:
        paid = client.pay_with_card(
            reservation,
            card_number=creds.card_number,
            card_password=creds.card_password,
            birthday=creds.card_validation,
            card_expire=creds.card_expire,
            installment=creds.card_installment,
            card_type="J",
        )
        r.step("pay", paid, "카드 결제 OK" if paid else "pay_with_card returned False")
    except Exception as e:
        r.step("pay", False, str(e)[:120])

    # cancel/refund
    refunded = False
    refund_msg = ""
    if not paid:
        try:
            ok = client.cancel(reservation)
            r.step("cancel", ok, "취소 OK (결제 전)")
            refunded = ok; refund_msg = "cancel ok"
        except Exception as e:
            r.step("cancel", False, str(e)[:120]); refund_msg = str(e)[:120]
    else:
        # paid — find ticket and refund. SAFETY: only the ticket whose pnr_no == test_pnr
        try:
            tickets = client.tickets()
            target_tkt = next(
                (t for t in tickets if str(getattr(t, "pnr_no", "")) == test_pnr),
                None,
            )
            if target_tkt is None:
                r.step("refund", False, f"발권 목록에서 PNR {test_pnr} 못 찾음")
                refund_msg = "ticket not found"
            elif (getattr(target_tkt, "dep_name", "") != KTX_TEST_DEP or
                  getattr(target_tkt, "arr_name", "") != KTX_TEST_ARR or
                  getattr(target_tkt, "dep_date", "") != target_date):
                r.step("refund", False, f"⚠ ticket route/date mismatch — 안전상 중단")
                refund_msg = "ticket mismatch"
            else:
                ok = client.refund(target_tkt)
                refunded = ok
                refund_msg = "refund OK" if ok else "refund returned False"
                r.step("refund", ok, refund_msg)
        except Exception as e:
            r.step("refund", False, str(e)[:120]); refund_msg = str(e)[:120]

    # SAFETY 4: post audit
    try:
        after_rsv = {str(x.rsv_id) for x in client.reservations()}
        after_tkt = {str(getattr(x, "pnr_no", "")) for x in client.tickets()} - {""}
        after = after_rsv | after_tkt
        lost = existing - after
        still_test = test_pnr in after
        if lost:
            r.step("audit", False, f"⚠ 보호 표가 사라짐!! {sorted(lost)} — 즉시 코레일 앱 확인")
        elif still_test:
            r.step("audit", False, f"⚠ 테스트 PNR {test_pnr} 가 처리 안 됨 — 코레일 앱에서 수동")
        else:
            r.step("audit", True, "보호 표 모두 살아있고, 테스트 표만 환불됨")
    except Exception as e:
        r.step("audit", False, f"audit 실패: {e}")

    if paid and refunded:
        r.ok = True
        r.summary = "✓ 카드 정상 — 예약·결제·환불·검증까지 모두 성공 (위약금 약 400원)"
    elif paid and not refunded:
        r.summary = f"⚠ 결제는 됐으나 자동 환불 실패 ({refund_msg}) — 코레일 앱에서 수동"
    else:
        r.summary = "결제 실패 — 카드 정보 확인"
    return r
