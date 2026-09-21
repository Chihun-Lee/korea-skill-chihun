#!/bin/bash
# 한달치 시간표 프리페치를 2주마다 자동 실행.
# launchd(com.chihunlee.k-rail-prefetch)가 매일 04:30에 호출하고, 이 스크립트가
# 14일 게이트로 실제 실행 여부를 결정한다 (절전/꺼짐으로 하루 놓쳐도 다음날 실행).
# 프리페치 자체는 K-Rail 서버(launchd 상주)가 백그라운드로 수행한다.
STAMP="$HOME/.k-rail-macro/prefetch_last_run"
LOG="/tmp/k-rail-prefetch.log"
PORT=8912

now=$(date +%s)
last=$(cat "$STAMP" 2>/dev/null || echo 0)
# 13.5일(1166400s) — 매일 04:30 트리거라 정확히 14일 주기로 수렴한다
if [ $((now - last)) -lt 1166400 ]; then
  exit 0
fi

if ! curl -fsS -m 5 "http://127.0.0.1:$PORT/api/meta" >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] K-Rail 서버 미응답 — 내일 재시도" >> "$LOG"
  exit 0
fi

# v3.0: 엔진이 코레일 하나로 합쳐져 프리페치도 한 번만 돈다(KTX+SRT 같이 받음).
Q=$(curl -s -m 10 "http://127.0.0.1:$PORT/api/rail/prefetch" -X POST -H 'Content-Type: application/json' -d '{"days":30}')
mkdir -p "$(dirname "$STAMP")"
echo "$now" > "$STAMP"
echo "[$(date '+%F %T')] 2주 주기 프리페치 시작: $Q" >> "$LOG"
