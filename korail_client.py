"""Patched srtgo Korail with Dynapath anti-bot bypass.

Korail's mobile API enforces an `x-dynapath-m-token` header + `Sid`
form param on sensitive endpoints. Without them, requests fail with
"MACRO ERROR".

The bypass logic (DynaPathMasterEngine + Sid AES-CBC) is adapted from
nomadamas/k-skill (MIT, scripts/ktx_booking.py).

This module subclasses srtgo's Korail class so we get the upstream
`pay_with_card` implementation for free, but with a session hook
that injects the bypass on every Dynapath request.
"""
from __future__ import annotations

import base64
import json
import random
import string
import time
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from srtgo.ktx import (
    AdultPassenger,
    Korail as _SrtgoKorail,
    Passenger,
    ReserveOption,
    SoldOutError,
)
from srtgo.ktx import API_ENDPOINTS as _KTX_API

DYNAPATH_PATHS = [
    "/classes/com.korail.mobile.certification.TicketReservation",
    "/classes/com.korail.mobile.nonMember.NonMemTicket",
    "/classes/com.korail.mobile.seatMovie.ScheduleView",
    "/classes/com.korail.mobile.seatMovie.ScheduleViewSpecial",
    "/classes/com.korail.mobile.trn.prcFare.do",
    "/classes/com.korail.mobile.login.Login",
    "/classes/com.korail.mobile.payment.ReservationPayment",
]

_SID_KEY = b"2485dd54d9deaa36"
_DEVICE_ID = "558a4f02041657ea"
_VERSION = "250601002"
_USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 13; SM-S928N Build/UP1A.231005.007)"


class DynaPathMasterEngine:
    APP_ID = "com.korail.talk"
    AS_VALUE = "%5B38ff229cb34c7dda8e28220a2d750cce%5D"
    DEVICE_MODEL = "SM-S928N"
    OS_TYPE = "Android"
    SDK_VERSION = "v1"

    def __init__(self) -> None:
        self.table = "3FE9jgRD4KdCyuawklqGJYmvfMn15P7US8XbxeLQtWT6OicBAopINs2Vh0HZrz"
        self.i8 = 161
        self.i9 = 30
        self.i10 = 2
        self.app_start_ts = str(int(time.time() * 1000))

    def string2xa1s(self, data: str) -> list[int]:
        result: list[int] = []
        idx = 0
        while idx < len(data):
            cp = ord(data[idx]); idx += 1
            if cp < 128:
                result.append(cp)
            elif cp < 2048:
                result.append(128 | ((cp >> 7) & 15))
                result.append(cp & 127)
            elif cp >= 262144:
                result.append(160)
                result.append((cp >> 14) & 127)
                result.append((cp >> 7) & 127)
                result.append(cp & 127)
            elif (63488 & cp) != 55296:
                result.append(((cp >> 14) & 15) | 144)
                result.append((cp >> 7) & 127)
                result.append(cp & 127)
        return result

    def make_key(self, key: str) -> int:
        total = 0
        for char in key:
            cp = ord(char); bit = 32768
            for _ in range(16):
                if bit & cp:
                    break
                bit >>= 1
            total = (total * (bit << 1)) + cp
        return total

    def internal_char(self, base_table: str, remainder: int, current: str) -> str:
        seen = 0
        for char in base_table:
            if char in current:
                continue
            if seen == remainder:
                return char
            seen += 1
        return " "

    def make_encode_table(self, number: int, encode_size: int, base_table: str) -> str:
        chars = ""; temp = number
        for index in range(encode_size):
            divisor = encode_size - index
            remainder = temp % divisor
            chars += self.internal_char(base_table, remainder, chars)
            temp //= divisor
        return chars

    def encode_normal_be(self, data: str, table: str) -> str:
        values = self.string2xa1s(data)
        output: list[str] = []
        digits = [0] * (self.i10 + 1)
        idx = 0
        tail = len(values) % self.i10
        body_size = len(values) - tail
        while idx < body_size:
            value = 0
            for _ in range(self.i10):
                value = (value * self.i8) + values[idx]; idx += 1
            for di in range(self.i10 + 1):
                digits[di] = value % self.i9
                value //= self.i9
            for di in range(self.i10, -1, -1):
                output.append(table[digits[di]])
        if tail > 0:
            value = 0
            for _ in range(tail):
                value = (value * self.i8) + values[idx]; idx += 1
            for di in range(tail + 1):
                digits[di] = value % self.i9
                value //= self.i9
            while tail >= 0:
                output.append(table[digits[tail]])
                tail -= 1
        return "".join(output)

    def generate_token(self, device_id: str, timestamp_ms: int, nonce: str) -> str:
        plaintext = (
            f"ai={self.APP_ID}&di={device_id}&as={self.AS_VALUE}&su=false&dbg=false&emu=false&hk=false"
            f"&it={self.app_start_ts}&ts={timestamp_ms}&rt=0&os=13&dm={self.DEVICE_MODEL}"
            f"&st={self.OS_TYPE}&sv={self.SDK_VERSION}"
        )
        dyn_key = f"v1+{nonce}+{timestamp_ms}"
        key_encoded = self.encode_normal_be(dyn_key, self.table)
        table = self.make_encode_table(self.make_key(dyn_key), self.i9, self.table)
        body_encoded = self.encode_normal_be(plaintext, table)
        return f"bEeEP{self.table[len(key_encoded)]}{key_encoded}{body_encoded}"


def _generate_sid(timestamp_ms: int) -> str:
    plaintext = f"AD{timestamp_ms}".encode("utf-8")
    cipher = AES.new(_SID_KEY, AES.MODE_CBC, iv=_SID_KEY)
    return base64.b64encode(cipher.encrypt(pad(plaintext, 16))).decode("utf-8") + "\n"


def _patch_session(session) -> None:
    """Wrap session.get/post so Dynapath endpoints get token + Sid."""
    engine = DynaPathMasterEngine()
    orig_post = session.post
    orig_get = session.get

    def _maybe_inject(url: str, kwargs: dict, payload_key: str) -> dict:
        if not any(p in url for p in DYNAPATH_PATHS):
            return kwargs
        ts = int(time.time() * 1000)
        nonce = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        token = engine.generate_token(_DEVICE_ID, ts, nonce)
        sid = _generate_sid(ts)
        headers = dict(kwargs.get("headers") or {})
        headers["x-dynapath-m-token"] = token
        kwargs["headers"] = headers
        if kwargs.get(payload_key) is not None:
            payload = dict(kwargs[payload_key])
            payload["Sid"] = sid
            kwargs[payload_key] = payload
        return kwargs

    def post(url, data=None, **kwargs):
        kwargs["data"] = data
        kwargs = _maybe_inject(url, kwargs, "data")
        return orig_post(url, **kwargs)

    def get(url, params=None, **kwargs):
        kwargs["params"] = params
        kwargs = _maybe_inject(url, kwargs, "params")
        return orig_get(url, **kwargs)

    session.post = post
    session.get = get


class PatchedKorail(_SrtgoKorail):
    """srtgo's Korail + Dynapath bypass."""

    def __init__(self, korail_id: str, korail_pw: str, auto_login: bool = True, verbose: bool = False):
        # init upstream WITHOUT auto_login so we can patch session first
        super().__init__(korail_id, korail_pw, auto_login=False, verbose=verbose)
        self._version = _VERSION
        # bump UA to match Dynapath device profile
        self._session.headers.update({"User-Agent": _USER_AGENT})
        _patch_session(self._session)
        if auto_login:
            self.login(korail_id, korail_pw)

    def reserve(self, train, passengers=None, option=ReserveOption.GENERAL_FIRST,
                seat_location: str = "000"):
        """upstream reserve + 좌석위치속성코드(txtSeatAttCd2) 지정.

        seat_location: "000" 무관(기본) / "012" 창측 / "013" 내측 — SRT와 같은
        KROSS 좌석속성코드 계열. 창측을 요청했는데 창측이 없으면 코레일이
        오류를 주므로, 호출측(워커)이 "000"으로 즉시 재시도해 폴백한다.
        본문은 srtgo.ktx.Korail.reserve를 그대로 옮기고 txtSeatAttCd2만
        파라미터화했다 — upstream이 바뀌면 여기도 맞춰야 한다.
        """
        reserving_seat = train.has_seat() or train.wait_reserve_flag < 0
        if reserving_seat:
            is_special_seat = {
                ReserveOption.GENERAL_ONLY: False,
                ReserveOption.SPECIAL_ONLY: True,
                ReserveOption.GENERAL_FIRST: not train.has_general_seat(),
                ReserveOption.SPECIAL_FIRST: train.has_special_seat(),
            }[option]
        else:
            is_special_seat = {
                ReserveOption.GENERAL_ONLY: False,
                ReserveOption.SPECIAL_ONLY: True,
                ReserveOption.GENERAL_FIRST: False,
                ReserveOption.SPECIAL_FIRST: True,
            }[option]

        passengers = passengers or [AdultPassenger()]
        passengers = Passenger.reduce(passengers)
        cnt = sum(p.count for p in passengers)

        data = {
            "Device": self._device,
            "Version": self._version,
            "Key": self._key,
            "txtMenuId": "11",
            "txtJobId": "1101" if reserving_seat else "1102",
            "txtGdNo": "",
            "hidFreeFlg": "N",
            "txtTotPsgCnt": cnt,
            "txtSeatAttCd1": "000",
            "txtSeatAttCd2": seat_location,
            "txtSeatAttCd3": "000",
            "txtSeatAttCd4": "015",
            "txtSeatAttCd5": "000",
            "txtStndFlg": "N",
            "txtSrcarCnt": "0",
            "txtJrnyCnt": "1",
            "txtJrnySqno1": "001",
            "txtJrnyTpCd1": "11",
            "txtDptDt1": train.dep_date,
            "txtDptRsStnCd1": train.dep_code,
            "txtDptTm1": train.dep_time,
            "txtArvRsStnCd1": train.arr_code,
            "txtTrnNo1": train.train_no,
            "txtRunDt1": train.run_date,
            "txtTrnClsfCd1": train.train_type,
            "txtTrnGpCd1": train.train_group,
            "txtPsrmClCd1": "2" if is_special_seat else "1",
            "txtChgFlg1": "",
            "txtJrnySqno2": "",
            "txtJrnyTpCd2": "",
            "txtDptDt2": "",
            "txtDptRsStnCd2": "",
            "txtDptTm2": "",
            "txtArvRsStnCd2": "",
            "txtTrnNo2": "",
            "txtRunDt2": "",
            "txtTrnClsfCd2": "",
            "txtPsrmClCd2": "",
            "txtChgFlg2": "",
        }

        for i, psg in enumerate(passengers, 1):
            data.update(psg.get_dict(i))

        r = self._session.get(_KTX_API["reserve"], params=data)
        self._log(r.text)
        j = json.loads(r.text)
        if self._result_check(j):
            rsv_id = j.get("h_pnr_no")
            reservation = self.reservations(rsv_id)
            return reservation
        else:
            raise SoldOutError()
