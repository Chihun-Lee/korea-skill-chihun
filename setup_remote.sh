#!/bin/bash
# 폰(원격)에서도 K-Rail을 쓸 수 있게 맥에 상주 설정 (macOS 전용).
#
#  1) launchd 상주: 로그인 시 자동 시작 + 죽으면 launchd가 자동 재시작.
#     (nohup run_supervised.sh 방식을 대체 — 재부팅에도 살아남는다)
#  2) 테일넷 접속: K_RAIL_HOST=0.0.0.0 으로 바인딩하고 서버 미들웨어가
#     로컬호스트+Tailscale 대역(100.64/10) 외 접근을 전부 403 차단한다.
#     → 폰 브라우저에서 http://<맥 테일스케일IP>:8912 (WireGuard 암호화)
#     (tailscale serve/HTTPS 인증서 불필요 — 테일넷 HTTPS 미활성이어도 동작)
#
# 사용:   bash setup_remote.sh          # 설치
#         bash setup_remote.sh --remove # 해제 (launchd + tailscale serve 끔)
#
# 폰에서 Claude 원격 세션으로 조작할 때도 서버가 이미 떠있으므로
# curl http://127.0.0.1:8912/api/... 로 바로 잡 등록/중지가 된다.
set -e
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.chihunlee.k-rail-macro"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=8912
LOG="/tmp/k-rail-macro.log"

if [ "$(uname)" != "Darwin" ]; then
  echo "macOS 전용입니다."; exit 1
fi

# Tailscale CLI 경로 (App Store/독립 앱 모두 대응)
TS="$(command -v tailscale || true)"
[ -z "$TS" ] && [ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ] \
  && TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"

if [ "$1" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✓ 상주 해제 완료 (서버 종료 + 자동시작 제거)"
  exit 0
fi

echo "[1/3] 기존 실행분 정리 (이중 실행 방지)..."
touch /tmp/k-rail-macro.stop
pkill -f "$INSTALL_DIR/run_supervised.sh" 2>/dev/null || true
EXISTING=$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null || true)
[ -n "$EXISTING" ] && kill $EXISTING 2>/dev/null || true
sleep 1

echo "[2/3] launchd 상주 등록 (로그인 자동시작 + 크래시 자동재시작)..."
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$INSTALL_DIR/venv/bin/python</string>
    <string>server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$INSTALL_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>K_RAIL_HOST</key><string>0.0.0.0</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- 크래시(비정상 종료) 때만 재시작. 이중 실행 가드가 exit 0으로 정상
       종료하는 경우까지 true로 무한 재스폰하면 5초마다 스팸이 된다. -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
# bootout 직후 bootstrap은 IO error(5)가 날 수 있어 잠깐 쉬며 재시도
BOOTSTRAPPED=""
for i in 1 2 3; do
  if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    BOOTSTRAPPED=1; break
  fi
  sleep 2
done
[ -z "$BOOTSTRAPPED" ] && { echo "  ✗ launchd 등록 실패"; exit 1; }

for i in $(seq 1 15); do
  curl -fsS "http://127.0.0.1:$PORT/api/meta" >/dev/null 2>&1 && break
  sleep 1
done
if curl -fsS "http://127.0.0.1:$PORT/api/meta" >/dev/null 2>&1; then
  echo "  ✓ 서버 상주 시작됨 (http://127.0.0.1:$PORT)"
else
  echo "  ✗ 서버가 안 뜸 — 로그 확인: $LOG"; exit 1
fi

echo "[3/3] 폰 접속 경로 (Tailscale)..."
if [ -z "$TS" ]; then
  echo "  ⚠ Tailscale이 설치돼 있지 않음 — 폰 접속은 Tailscale 설치 후 재확인"
elif ! "$TS" status >/dev/null 2>&1; then
  echo "  ⚠ Tailscale이 꺼져 있음 — 메뉴막대에서 켜면 폰에서 접속 가능"
  echo "    (서버는 이미 대기 중이라 재실행 불필요)"
else
  TS_IP=$("$TS" ip -4 2>/dev/null | head -1)
  echo "  ✓ 폰 브라우저에서 접속: http://${TS_IP:-<맥 테일스케일IP>}:$PORT"
  echo "    (폰도 Tailscale 켜져 있어야 함. 테일넷 밖에서는 403 차단)"
fi

echo ""
echo "완료. 종료/재시작이 필요하면:"
echo "  중지:   launchctl bootout gui/$(id -u)/$LABEL"
echo "  시작:   launchctl bootstrap gui/$(id -u) $PLIST"
echo "  전체해제: bash setup_remote.sh --remove"
