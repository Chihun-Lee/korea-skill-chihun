#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""K-Rail 매크로 원클릭 환승 예약: 수서→오송(SRT) + 오송→창원(KTX) 저녁 막차.

맥 터미널에서 한 줄:
  curl -fsSL https://raw.githubusercontent.com/Chihun-Lee/k-rail-macro/main/book_transfer.py | python3

동작:
  1. K-Rail 매크로 서버(127.0.0.1:8912)가 안 떠 있으면 앱을 실행해서 띄운다
  2. KTX 오송→창원 시간 조회 → 제일 늦은 열차(막차)를 목표로 확정
  3. SRT 수서→오송 조회 → KTX 출발보다 MIN_TRANSFER분 이상 먼저 오송에
     도착하는 열차 중 제일 늦은 것을 확정
  4. 두 잡을 자동 결제 모드로 등록하고 초기 로그를 출력

옵션(인자 순서대로, 전부 생략 가능):
  python3 book_transfer.py [YYYYMMDD(기본 내일)] [환승최소분(기본 5)] [auto|manual(기본 auto)]
환경변수:
  SEAT=any      일반실 매진 시 특실도 허용 (기본 general)
  SEARCH_FROM=HHMMSS  조회 시작 시각 (기본 150000)
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

BASE = "http://127.0.0.1:8912"
APP = os.path.expanduser("~/Applications/K-Rail 매크로.app")
INSTALL_DIR = os.path.expanduser(os.environ.get("K_RAIL_HOME", "~/.k-rail-macro"))

DATE = sys.argv[1] if len(sys.argv) > 1 else (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
MIN_TRANSFER = int(sys.argv[2]) if len(sys.argv) > 2 else 5
PAY_MODE = sys.argv[3] if len(sys.argv) > 3 else "auto"
SEAT_PREF = os.environ.get("SEAT", "general")
SEARCH_FROM = os.environ.get("SEARCH_FROM", "150000")

TIME_RE = re.compile(r"\((\d{2}):(\d{2})~(\d{2}):(\d{2})\)")


def api(path: str, payload=None, timeout=90):
    req = urllib.request.Request(BASE + path)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", "")
        except Exception:
            detail = str(e)
        raise RuntimeError(f"{path} → HTTP {e.code}: {detail}") from None


def server_up() -> bool:
    try:
        api("/api/srt/config/status", timeout=5)
        return True
    except Exception:
        return False


def ensure_server() -> None:
    if server_up():
        print("· 서버 이미 실행 중")
        return
    print("· 서버가 안 떠 있음 → 앱 실행 중...")
    if os.path.exists(APP):
        subprocess.Popen(["open", APP])
    elif os.path.isdir(INSTALL_DIR):
        log = open("/tmp/k-rail-macro.log", "a")
        subprocess.Popen(
            [os.path.join(INSTALL_DIR, "venv/bin/python"), "server.py"],
            cwd=INSTALL_DIR, stdout=log, stderr=subprocess.STDOUT,
        )
    else:
        sys.exit("✗ K-Rail 매크로가 설치돼 있지 않습니다. README의 install.sh를 먼저 실행하세요.")
    for _ in range(30):
        time.sleep(1)
        if server_up():
            print("· 서버 OK")
            return
    sys.exit("✗ 서버 시작 실패 (로그: /tmp/k-rail-macro.log)")


def parse_times(label: str):
    """열차 라벨의 '(HH:MM~HH:MM)'에서 (출발분, 도착분)을 꺼낸다."""
    m = TIME_RE.search(label)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    return h1 * 60 + m1, h2 * 60 + m2


def fmt(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def main() -> None:
    print(f"대상 날짜 {DATE} / 환승 최소 {MIN_TRANSFER}분 / 결제 {PAY_MODE} / 좌석 {SEAT_PREF}")
    ensure_server()

    for svc in ("srt", "ktx"):
        st = api(f"/api/{svc}/config/status")
        if not st.get("configured"):
            sys.exit(f"✗ {svc.upper()} 자격증명이 저장돼 있지 않습니다. {BASE} 에서 먼저 저장하세요.")
        print(f"· {svc.upper()} 자격증명 OK (id={st.get('id')})")

    print(f"\n[1/3] KTX 오송→창원 조회 ({SEARCH_FROM[:2]}시 이후)...")
    ktx_trains = api("/api/ktx/search", {
        "dep": "오송", "arr": "창원", "date": DATE, "time": SEARCH_FROM, "train_type": "ktx",
    })["trains"]
    if not ktx_trains:
        sys.exit("✗ 오송→창원 KTX가 검색되지 않았습니다. SEARCH_FROM을 앞당겨 다시 실행하세요.")
    for t in ktx_trains:
        print("    ", t["label"])
    ktx_pick = ktx_trains[-1]
    ktx_times = parse_times(ktx_pick["label"])
    if not ktx_times:
        sys.exit("✗ KTX 시간 파싱 실패: " + ktx_pick["label"])
    ktx_dep, ktx_arr = ktx_times
    print(f"→ KTX 막차 확정: {ktx_pick['label']}")

    print(f"\n[2/3] SRT 수서→오송 조회 (오송 {fmt(ktx_dep)} 출발 기준 환승 {MIN_TRANSFER}분+)...")
    srt_trains = api("/api/srt/search", {
        "dep": "수서", "arr": "오송", "date": DATE, "time": SEARCH_FROM,
    })["trains"]
    candidates = []
    for t in srt_trains:
        tm = parse_times(t["label"])
        if tm and tm[1] + MIN_TRANSFER <= ktx_dep:
            candidates.append((tm, t))
    if not candidates:
        sys.exit("✗ 환승 조건을 만족하는 SRT가 없습니다. SEARCH_FROM을 앞당겨 다시 실행하세요.")
    (srt_dep, srt_arr), srt_pick = max(candidates, key=lambda x: x[0][1])
    print(f"→ SRT 확정: {srt_pick['label']} (오송 도착 {fmt(srt_arr)}, 환승 여유 {ktx_dep - srt_arr}분)")

    print(f"\n[3/3] 잡 등록 (결제모드={PAY_MODE})")
    srt_job = api("/api/srt/jobs", {
        "dep": "수서", "arr": "오송", "date": DATE,
        "time": f"{srt_dep // 60:02d}{srt_dep % 60:02d}00",
        "train_number": srt_pick["train_number"],
        "passengers": 1, "seat_pref": SEAT_PREF, "pay_mode": PAY_MODE,
    })
    print(f"  ✓ SRT 잡 {srt_job['id']} 시작: {srt_pick['label']}")

    ktx_body = {
        "dep": "오송", "arr": "창원", "date": DATE, "time": SEARCH_FROM,
        "train_id": ktx_pick["train_id"], "train_type": "ktx",
        "passengers": 1, "seat_pref": SEAT_PREF, "pay_mode": PAY_MODE,
        "include_waiting": False,
    }
    try:
        ktx_job = api("/api/ktx/jobs", ktx_body)
    except RuntimeError as e:
        if PAY_MODE == "auto" and "카드" in str(e):
            print("  ! KTX 카드 미등록 → 수동 결제 모드로 대체 (잡히면 9분 내 '결제 진행' 필요)")
            ktx_body["pay_mode"] = "manual"
            ktx_job = api("/api/ktx/jobs", ktx_body)
        else:
            raise
    print(f"  ✓ KTX 잡 {ktx_job['id']} 시작: {ktx_pick['label']}")

    time.sleep(8)
    for svc, jid in (("srt", srt_job["id"]), ("ktx", ktx_job["id"])):
        lg = api(f"/api/{svc}/jobs/{jid}/log")
        print(f"\n── {svc.upper()} 잡 {jid} [{lg['status']}]")
        for line in lg["lines"][-5:]:
            print("   ", line)

    if sys.platform == "darwin":
        subprocess.Popen(["open", BASE])
    print(f"\n✅ 등록 완료. {BASE} 에서 실시간 로그를 확인하세요.")
    print("   자동 결제 모드는 좌석이 잡히는 즉시 Keychain의 카드로 결제됩니다.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"✗ {e}")
