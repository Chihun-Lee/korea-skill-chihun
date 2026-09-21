#!/bin/bash
# 서버 감시 루프 (macOS): 서버가 어떤 이유로든 죽으면 2초 후 자동 재시작한다.
# 죽기 전의 활성 잡은 ~/.k-rail-macro/jobs.json 에서 서버가 다시 뜰 때 자동
# 복원되므로, 크래시가 나도 표잡기는 계속된다.
# "K-Rail 매크로 종료" 앱이 STOP_FLAG 파일을 만들면 루프를 멈춘다.
# 사용법: run_supervised.sh [ARCH_PREFIX]   (예: "arch -arm64")
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${K_RAIL_LOG:-/tmp/k-rail-macro.log}"
STOP_FLAG="/tmp/k-rail-macro.stop"
ARCH_PREFIX="$1"

rm -f "$STOP_FLAG"
cd "$INSTALL_DIR"
while :; do
  $ARCH_PREFIX "$INSTALL_DIR/venv/bin/python" server.py >> "$LOG" 2>&1
  [ -f "$STOP_FLAG" ] && break
  # 다른 K-Rail 서버(launchd 상주 등)가 이미 응답하면 — 서버가 이중 실행
  # 방지로 스스로 종료한 경우 — 되살리지 않고 루프를 끝낸다(중복예매 방지).
  if curl -fsS -m 3 "http://127.0.0.1:8912/api/meta" >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] 다른 K-Rail 서버 실행 중 → 감시 루프 종료(이중 실행 방지)" >> "$LOG"
    break
  fi
  echo "[$(date '+%F %T')] 서버 종료 감지 → 2초 후 자동 재시작" >> "$LOG"
  sleep 2
done
