#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""K-Rail 매크로 단일 구간 예약: 도착시각 기준으로 열차를 골라 잡을 등록한다.

사용법 (맥 터미널):
  curl -fsSL https://raw.githubusercontent.com/Chihun-Lee/k-rail-macro/main/book_one.py \
    | python3 - ktx 창원중앙 대전 20260723 1120 auto

인자: <srt|ktx> <출발역> <도착역> <YYYYMMDD> <도착시각HHMM> [auto|manual(기본 auto)]
  도착시각까지(포함) 도착하는 열차 중 가장 늦은 열차를 목표로 잡는다.
환경변수:
  SEAT=any            일반실 매진 시 특실 허용 (기본 general)
  SEARCH_FROM=HHMMSS  조회 시작 시각 (기본 050000)
  EXACT=1             도착시각이 정확히 일치하는 열차만 허용
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8912"
APP = os.path.expanduser("~/Applications/K-Rail 매크로.app")
INSTALL_DIR = os.path.expanduser(os.environ.get("K_RAIL_HOME", "~/.k-rail-macro"))
TIME_RE = re.compile(r"\((\d{2}):(\d{2})~(\d{2}):(\d{2})\)")

SEAT_PREF = os.environ.get("SEAT", "general")
SEARCH_FROM = os.environ.get("SEARCH_FROM", "050000")
EXACT = os.environ.get("EXACT") == "1"


def api(path, payload=None, timeout=90):
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


def server_up():
    try:
        api("/api/srt/config/status", timeout=5)
        return True
    except Exception:
        return False


def ensure_server():
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
        sys.exit("✗ K-Rail 매크로가 설치돼 있지 않습니다. install.sh를 먼저 실행하세요.")
    for _ in range(30):
        time.sleep(1)
        if server_up():
            print("· 서버 OK")
            return
    sys.exit("✗ 서버 시작 실패 (로그: /tmp/k-rail-macro.log)")


def parse_times(label):
    m = TIME_RE.search(label)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    return h1 * 60 + m1, h2 * 60 + m2


def fmt(mins):
    return f"{mins // 60:02d}:{mins % 60:02d}"


def main():
    if len(sys.argv) < 6:
        sys.exit(__doc__)
    svc, dep, arr, date, arrive_by = sys.argv[1:6]
    pay_mode = sys.argv[6] if len(sys.argv) > 6 else "auto"
    if svc not in ("srt", "ktx"):
        sys.exit("✗ 첫 인자는 srt 또는 ktx")
    if not re.fullmatch(r"\d{8}", date) or not re.fullmatch(r"\d{4}", arrive_by):
        sys.exit("✗ 날짜는 YYYYMMDD, 도착시각은 HHMM 형식")
    limit = int(arrive_by[:2]) * 60 + int(arrive_by[2:])

    print(f"{svc.upper()} {dep}→{arr} {date}, {fmt(limit)}까지 도착 / 결제 {pay_mode} / 좌석 {SEAT_PREF}")
    ensure_server()

    st = api(f"/api/{svc}/config/status")
    if not st.get("configured"):
        sys.exit(f"✗ {svc.upper()} 자격증명이 저장돼 있지 않습니다. {BASE} 에서 먼저 저장하세요.")
    print(f"· {svc.upper()} 자격증명 OK (id={st.get('id')})")

    body = {"dep": dep, "arr": arr, "date": date, "time": SEARCH_FROM}
    if svc == "ktx":
        body["train_type"] = "ktx"
    trains = api(f"/api/{svc}/search", body)["trains"]
    if not trains:
        sys.exit(f"✗ {dep}→{arr} 열차가 검색되지 않았습니다.")

    candidates = []
    for t in trains:
        tm = parse_times(t["label"])
        if not tm:
            continue
        ok = tm[1] == limit if EXACT else tm[1] <= limit
        if ok:
            candidates.append((tm, t))
        print("   ", ("✓" if ok else " "), t["label"])
    if not candidates:
        sys.exit(f"✗ {fmt(limit)}까지 도착하는 열차가 없습니다. (EXACT=1이면 해제해 보세요)")
    (dep_m, arr_m), pick = max(candidates, key=lambda x: x[0][1])
    print(f"→ 확정: {pick['label']} ({arr} 도착 {fmt(arr_m)})")

    job_body = {
        "dep": dep, "arr": arr, "date": date,
        "time": f"{dep_m // 60:02d}{dep_m % 60:02d}00",
        "passengers": 1, "seat_pref": SEAT_PREF, "pay_mode": pay_mode,
    }
    if svc == "srt":
        job_body["train_number"] = pick["train_number"]
    else:
        job_body["train_id"] = pick["train_id"]
        job_body["train_type"] = "ktx"
        job_body["include_waiting"] = False
    try:
        job = api(f"/api/{svc}/jobs", job_body)
    except RuntimeError as e:
        if pay_mode == "auto" and "카드" in str(e):
            print("  ! 카드 미등록 → 수동 결제 모드로 대체 (잡히면 9분 내 '결제 진행' 필요)")
            job_body["pay_mode"] = "manual"
            job = api(f"/api/{svc}/jobs", job_body)
        else:
            raise
    print(f"  ✓ {svc.upper()} 잡 {job['id']} 시작: {pick['label']}")

    time.sleep(8)
    lg = api(f"/api/{svc}/jobs/{job['id']}/log")
    print(f"\n── {svc.upper()} 잡 {job['id']} [{lg['status']}]")
    for line in lg["lines"][-5:]:
        print("   ", line)

    if sys.platform == "darwin":
        subprocess.Popen(["open", BASE])
    print(f"\n✅ 등록 완료. {BASE} 에서 실시간 로그를 확인하세요.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"✗ {e}")
