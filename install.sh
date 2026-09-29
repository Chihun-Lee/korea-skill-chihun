#!/bin/bash
# K-Rail (KTX + SRT 통합) 매크로 설치 스크립트 (macOS)
# 사용법:
#   curl -fsSL https://raw.githubusercontent.com/Chihun-Lee/k-rail-macro/main/install.sh | bash
set -e

REPO="https://github.com/Chihun-Lee/k-rail-macro.git"
INSTALL_DIR="${K_RAIL_HOME:-$HOME/.k-rail-macro}"
APP_DIR="$HOME/Applications"
RUN_APP="$APP_DIR/K-Rail 매크로.app"
QUIT_APP="$APP_DIR/K-Rail 매크로 종료.app"
PORT=8912

echo ""
echo "════════════════════════════════════════"
echo "  K-Rail 매크로 (KTX + SRT 통합) 설치"
echo "════════════════════════════════════════"
echo ""

echo "[1/5] Python 3.10+ 확인 (ensurepip + 아키텍처)..."
# Apple stub Python 은 ensurepip 빠져있어 venv 안에 pip 가 안 깔림 →
# ensurepip 모듈 import 가능한 인터프리터만 인정.
# 추가로 system arch 와 Python arch 가 같아야 함 (Rosetta x86_64 Python on
# Apple Silicon 인 경우 wheel 호환 안 됨 → "incompatible architecture" 임포트 실패)
SYS_ARCH=$(uname -m)
_check_py() {
  "$1" -c 'import sys, ensurepip; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null || return 1
  local pa
  pa=$("$1" -c 'import platform; print(platform.machine())' 2>/dev/null)
  [ "$pa" = "$SYS_ARCH" ]
}
PYTHON_BIN=""
for cand in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$cand" >/dev/null 2>&1 && _check_py "$cand"; then
    PYTHON_BIN="$(command -v "$cand")"; break
  fi
done
if [ -z "$PYTHON_BIN" ] && command -v python3 >/dev/null 2>&1 && _check_py python3; then
  PYTHON_BIN="$(command -v python3)"
fi

# 그래도 없으면 Python.org 공식 .pkg로 자동 설치 (Homebrew 안 거침)
if [ -z "$PYTHON_BIN" ]; then
  echo "  → Python 3.10 이상이 없습니다. Python 공식 인스톨러로 설치합니다."
  PY_VERSION="3.13.1"
  ARCH=$(uname -m)
  PKG_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-macos11.pkg"
  TMP_PKG="/tmp/python-${PY_VERSION}.pkg"
  echo "  → 다운로드 (~40MB): $PKG_URL"
  if ! curl -fsSL "$PKG_URL" -o "$TMP_PKG"; then
    echo "  ✗ 다운로드 실패. 네트워크 확인 후 재시도, 또는 직접:"
    echo "    https://www.python.org/downloads/macos/ 에서 .pkg 다운로드 → 더블클릭"
    read -p "  엔터로 종료..." < /dev/tty || true
    exit 1
  fi
  echo "  → 설치 (관리자 비밀번호 1회 필요, 1~2분)"
  if sudo installer -pkg "$TMP_PKG" -target /; then
    rm -f "$TMP_PKG"
    # 설치 후 PATH 갱신 — Python.framework 위치
    for cand in /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
                /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
                python3.13 python3.12 python3; do
      if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then
        PYTHON_BIN="${cand}"
        # absolute path 가 아니면 command -v 로 변환
        case "$PYTHON_BIN" in
          /*) ;;
          *) PYTHON_BIN="$(command -v "$cand")" ;;
        esac
        if "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
          break
        fi
        PYTHON_BIN=""
      fi
    done
  fi
fi

if [ -z "$PYTHON_BIN" ]; then
  echo ""
  echo "  ✗ Python 3.10+ 자동 설치 실패."
  echo "    https://www.python.org/downloads/macos/ 에서 macOS installer 다운로드 → 더블클릭 설치"
  echo "    설치 완료 후 이 .command 파일을 다시 더블클릭하세요."
  read -p "  엔터로 종료..." < /dev/tty || true
  exit 1
fi
PY_VER=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  ✓ Python $PY_VER ($PYTHON_BIN)"

echo "[2/5] 코드 다운로드..."
mkdir -p "$APP_DIR"
# git 이 실제로 쓸 수 있는지 확인 — 새 맥은 /usr/bin/git 이 Xcode CLT 설치 팝업만
# 띄우는 껍데기라 `command -v` 만으로는 판별이 안 된다.
_has_git() { git --version >/dev/null 2>&1 && xcode-select -p >/dev/null 2>&1; }
if [ -d "$INSTALL_DIR/.git" ] && _has_git; then
  git -C "$INSTALL_DIR" fetch --quiet
  git -C "$INSTALL_DIR" reset --hard origin/main --quiet
elif _has_git && [ ! -d "$INSTALL_DIR" ]; then
  git clone --quiet "$REPO" "$INSTALL_DIR"
else
  # git 없음(또는 비 git 설치본 업데이트) → main 브랜치 tarball 로 덮어쓴다.
  # jobs.json·venv 등 코드 밖 파일은 건드리지 않는다.
  TMP_TGZ="$(mktemp -d)"
  curl -fsSL "https://codeload.github.com/Chihun-Lee/k-rail-macro/tar.gz/refs/heads/main" \
    | tar -xz -C "$TMP_TGZ"
  mkdir -p "$INSTALL_DIR"
  cp -R "$TMP_TGZ"/k-rail-macro-main/. "$INSTALL_DIR"/
  rm -rf "$TMP_TGZ"
  chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true
fi
echo "  ✓ $INSTALL_DIR"

echo "[3/5] Python 환경 구성 (1~3분 소요)..."
# venv 가 호환 안 되면 (낮은 파이썬, pip 없음, arch mismatch) 재생성
_venv_ok() {
  local p="$INSTALL_DIR/venv/bin/python"
  [ -x "$p" ] || return 1
  "$p" -c 'import sys, pip; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null || return 1
  local pa
  pa=$("$p" -c 'import platform; print(platform.machine())' 2>/dev/null)
  [ "$pa" = "$SYS_ARCH" ]
}
if [ -x "$INSTALL_DIR/venv/bin/python" ] && ! _venv_ok; then
  echo "  → 기존 venv 가 3.10 미만 / pip 없음 / arch mismatch → 재생성"
  rm -rf "$INSTALL_DIR/venv"
fi
# Apple Silicon에서는 universal binary 가 x86_64 로 fork 될 수 있음 →
# 모든 Python 호출을 arch -arm64 로 강제해서 native arm64 로 실행
ARCH_PREFIX=""
if [ "$SYS_ARCH" = "arm64" ]; then
  ARCH_PREFIX="arch -arm64"
fi
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  $ARCH_PREFIX "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
fi
# pip 부트스트랩 3단 fallback (ensurepip → get-pip.py)
VENV_PY="$INSTALL_DIR/venv/bin/python"
if ! $ARCH_PREFIX "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  $ARCH_PREFIX "$VENV_PY" -m ensurepip --upgrade 2>/dev/null || true
  if ! $ARCH_PREFIX "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    echo "  → pip 미설치 → get-pip.py 부트스트랩"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | $ARCH_PREFIX "$VENV_PY"
  fi
fi
if ! $ARCH_PREFIX "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  echo "  ✗ pip 부트스트랩 실패. https://www.python.org/downloads/macos/ 에서 Python 재설치 후 다시 시도."
  read -p "  엔터로 종료..." < /dev/tty || true
  exit 1
fi
$ARCH_PREFIX "$VENV_PY" -m pip install --quiet --upgrade pip
$ARCH_PREFIX "$VENV_PY" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"
echo "  ✓ 의존성 설치 완료"

echo "[4/5] 앱 번들 생성..."
rm -rf "$RUN_APP" "$QUIT_APP"

mkdir -p "$RUN_APP/Contents/MacOS"
cat > "$RUN_APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>K-Rail 매크로</string>
  <key>CFBundleDisplayName</key><string>K-Rail 매크로</string>
  <key>CFBundleIdentifier</key><string>com.chihunlee.k-rail-macro</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>k-rail-macro</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF
mkdir -p "$RUN_APP/Contents/Resources"
if [ -f "$INSTALL_DIR/icon.png" ]; then
  ICONSET="$(mktemp -d)/icon.iconset"
  mkdir -p "$ICONSET"
  for sz in 16 32 64 128 256 512 1024; do
    sips -z $sz $sz "$INSTALL_DIR/icon.png" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1
  done
  iconutil -c icns "$ICONSET" -o "$RUN_APP/Contents/Resources/icon.icns" >/dev/null 2>&1 || true
  rm -rf "$(dirname "$ICONSET")"
fi

cat > "$RUN_APP/Contents/MacOS/k-rail-macro" <<EOF
#!/bin/bash
INSTALL_DIR="$INSTALL_DIR"
PORT=$PORT
LOG="/tmp/k-rail-macro.log"
ARCH_PREFIX="$ARCH_PREFIX"

# 재실행 시 기존 감시 루프까지 깨끗이 정리(그냥 서버만 죽이면 옛 루프가 되살림)
touch /tmp/k-rail-macro.stop
pkill -f "\$INSTALL_DIR/run_supervised.sh" 2>/dev/null
EXISTING=\$(lsof -ti tcp:\$PORT -sTCP:LISTEN 2>/dev/null)
if [ -n "\$EXISTING" ]; then
  kill \$EXISTING 2>/dev/null
  sleep 1
fi

cd "\$INSTALL_DIR"
: > "\$LOG"
# 감시 루프로 실행: 서버가 죽어도 자동 재시작 + 잡은 jobs.json에서 자동 복원
nohup "\$INSTALL_DIR/run_supervised.sh" "\$ARCH_PREFIX" > /dev/null 2>&1 &

for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS http://127.0.0.1:\$PORT/api/meta > /dev/null 2>&1; then
    open "http://127.0.0.1:\$PORT"
    osascript -e 'display notification "KTX·SRT를 한 화면에서 예매합니다." with title "K-Rail 매크로 시작됨" sound name "Glass"'
    exit 0
  fi
  sleep 1
done

osascript -e 'display alert "K-Rail 매크로 시작 실패" message "로그: /tmp/k-rail-macro.log" as critical'
EOF
chmod +x "$RUN_APP/Contents/MacOS/k-rail-macro"

mkdir -p "$QUIT_APP/Contents/MacOS"
cat > "$QUIT_APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>K-Rail 매크로 종료</string>
  <key>CFBundleDisplayName</key><string>K-Rail 매크로 종료</string>
  <key>CFBundleIdentifier</key><string>com.chihunlee.k-rail-macro-quit</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>quit</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

cat > "$QUIT_APP/Contents/MacOS/quit" <<EOF
#!/bin/bash
PORT=$PORT
# 감시 루프가 서버를 되살리지 않도록 정지 깃발부터 세운다
touch /tmp/k-rail-macro.stop
PIDS=\$(lsof -ti tcp:\$PORT -sTCP:LISTEN 2>/dev/null)
if [ -n "\$PIDS" ]; then
  kill \$PIDS
  osascript -e 'display notification "K-Rail 매크로 종료됨" with title "K-Rail 매크로" sound name "Pop"'
else
  osascript -e 'display notification "이미 종료된 상태입니다" with title "K-Rail 매크로"'
fi
EOF
chmod +x "$QUIT_APP/Contents/MacOS/quit"

xattr -dr com.apple.quarantine "$RUN_APP" 2>/dev/null || true
xattr -dr com.apple.quarantine "$QUIT_APP" 2>/dev/null || true

echo "  ✓ $RUN_APP"
echo "  ✓ $QUIT_APP"

echo "[5/5] 완료!"
echo ""
echo "════════════════════════════════════════"
echo "  ✅ 설치 완료"
echo "════════════════════════════════════════"
echo ""
echo "  사용법:"
echo "    1. Launchpad → 'K-Rail 매크로' 검색 → 더블클릭"
echo "    2. 브라우저가 자동으로 열림 (KTX·SRT 통합 화면)"
echo "    3. 경로우대(만 65세+) 할인은 잡 등록 시 '경로' 인원으로 입력"
echo ""
echo "  종료: Launchpad → 'K-Rail 매크로 종료' 더블클릭"
echo ""

read -p "  지금 바로 실행할까요? [y/N] " -n 1 -r < /dev/tty || REPLY=""
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  open "$RUN_APP"
fi
