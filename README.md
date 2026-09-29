# K-Rail 매크로 (K-Rail Macro)

**v3.1.0** · 개발자: **이치헌 (Chihun Lee)** — 버전·개발자 정보는 서버 `/api/meta`와 웹 UI 하단에도 표시된다.

**KTX + SRT를 코레일 계정 하나로** 잡는 매크로. 탭 없는 단일 화면.

> 🚄 **v3.0 (2026-09-21) — 엔진 통합.** 2026-09 코레일·SR 발매 통합으로 코레일 계정에서
> SRT 열차까지 조회·예약·결제가 된다(수서~동대구 SRT 335편 예약→취소로 실측 확인).
> SR 전용 엔진(SRTrain)과 SRT 탭을 걷어내고 코레일 엔진 하나로 합쳤다. 수서·동탄·평택지제
> 같은 SRT 전용역도 전국 45개 역 목록에 함께 들어간다.

> ⚠ **개인용 한정.** 본인 코레일 계정·본인 카드로만 사용하세요. 자격증명·카드정보는 **macOS Keychain**에 암호화 저장됩니다. 서버는 `127.0.0.1:8912`에만 바인딩됩니다.

---

## 친구한테 보낼 1줄 가이드 (설치)

친구가 본인 Mac에서 **터미널을 열어** 아래 한 줄 붙여넣고 엔터:

```bash
curl -fsSL https://raw.githubusercontent.com/Chihun-Lee/korea-skill-chihun/main/install.sh | bash
```

> 또는 [`K-Rail_매크로_설치.command`](https://github.com/Chihun-Lee/korea-skill-chihun/raw/main/K-Rail_매크로_설치.command) 다운로드 → Finder에서 **우클릭 → 열기**

설치 끝나면 **Launchpad → "K-Rail 매크로"** 검색 → 더블클릭. 종료는 **"K-Rail 매크로 종료"**.

---

## 기능

- **탭 없는 단일 화면** — KTX·SRT가 한 목록에 섞여 나오고, 예매도 코레일 계정 하나로 한다
  (운영사는 열차번호로 구분해 배지로만 표시: 3xx/6xx = SRT, 나머지 = 코레일)
- **조회 페이지네이션 (v3.0)** — 코레일 조회 API는 한 번에 ~10편만 준다. 출발시각을
  밀어가며 이어 받아 **하루치를 전부** 보여준다. (v2에선 목록이 10편에서 잘려 늦은
  열차는 아예 잡을 수 없었다.) 특정 열차를 노리는 잡은 그 열차를 찾는 즉시 멈춘다.
- **전국 45개 역** — 수서·동탄·평택지제(SRT 전용역) 포함. 코레일 API에 실제로 넣어
  하나씩 확인한 목록이고, `김천구미`/`신경주` 같은 다른 표기도 자동 흡수한다.
- **경로우대 할인 (v3.1)** — 잡 등록 시 `어른`·`경로(65세+)` 인원을 따로 입력한다.
  경로 인원은 코레일 할인코드 131로 예약돼 KTX·SRT 30% 할인가로 결제된다(일반열차도 할인).
  API: `"passengers": 총원, "seniors": 경로 인원` (예: 어른1+경로1 → `passengers:2, seniors:1`).
  ⚠ 승차 시 경로 대상자 신분증 지참.
- 폴링 간격: **3~90초 균등 랜덤**
- 결제 모드: **자동 (즉시 결제, 기본)** / 수동 (사용자 확인) — v2.1.1부터 기본 자동
- **시간표/환승 조회** (v2.2.0): `POST /api/{srt,ktx}/timetable`(직행) ·
  `POST /api/{srt,ktx}/transfer`(직행+환승 조합) → `{"query_id"}` 즉시 반환,
  `GET /api/lookup/{id}` 폴링. 환승은 공식 환승조회가 아니라 **구간별 검색 조합**
  (환승 대기 6분 이상, via 지정) — 각 구간을 별도 잡으로 예약하는 구간별 예약 방식 전제.
- anti-bot 자동 회복: 코레일 MACRO ERROR → 클라이언트 재생성 (Dynapath 우회 토큰 자동 갱신)
- **표 잡을 때까지 안 멈춤** (세션 중단 방지 4중 장치):
  - 로그인 실패·인터넷 끊김 → 백오프 후 무한 재시도 (ERROR로 죽지 않음)
  - 감시자(watchdog)가 30초마다 검사 → 죽거나 멈춘 폴링 스레드 자동 재시작
  - 활성 잡을 `~/.k-rail-macro/jobs.json`에 저장 → 서버가 죽어도 재시작 시 자동 복원
  - macOS: 서버 크래시 시 2초 후 자동 재기동(`run_supervised.sh`) + 유휴 절전 방지(`caffeinate`)
  - 수동결제 확인 시간초과(~9분)로 예약이 자동취소되면 → 폴링 자동 재개
- **좌석 선호** (v2.4.0, 1인 예매만 · 기본 켜짐, 잡 등록 시 해제 가능):
  - **창측 우선**: 예약 요청에 창측 좌석속성(012)을 실어 보낸다 (`txtSeatAttCd2`).
    창측이 없어 실패하면 **위치 무관으로 즉시 재시도** —
    선호 때문에 자리를 놓치지 않는다.
  - **맨앞/맨뒷열 회피**: 배정 좌석이 1열이거나 호차 뒷열(일반실 15열+, 특실 8열+ 추정)
    또는 통로측이면, **그 열차에 다른 좌석이 남아있는 경우에만** 취소→즉시 재예약으로
    좌석을 바꾼다(최대 2회). 남은 자리가 그것뿐이면 그대로 진행. 재예약 경쟁에서
    지면 폴링으로 복귀해 계속 재도전.
- **중복예매 방지 6중 장치** (v2.1.0 3중 + v2.4.0 3중 추가):
  - **계정 이력 사전검사**: 폴링 시작 전(서버 재시작 복원·감시자 재기동 포함) 계정의 예약/발권 내역을 조회해, 같은 날짜·구간 표가 **이미 결제돼 있으면 재예매 없이 종료**(PAID), **미결제 예약이 살아있으면 재예매 대신 그 예약을 이어받아 결제 단계로** 진행한다. 크래시가 예약~결제 사이 어디서 나든 같은 표를 두 번 사지 않는다.
  - **활성 잡 이중 등록 차단**: 같은 구간·날짜의 활성 잡이 있으면 새 잡 등록을 409로 거부 (특정 열차번호가 서로 다르면 허용).
  - **서버 이중 실행 방지**: 0.0.0.0(launchd 상주)과 127.0.0.1(앱 실행) 바인딩이 공존해 서버 2개가 각자 잡을 복원·폴링하던 경로 차단 — 기동 전 기존 서버 응답을 확인하고 스스로 종료.
  - **예약 직전 재확인** (v2.4.0): 좌석을 발견해도 예약 API를 부르기 직전에 스레드 세대·정지 여부를 다시 확인 — 감시자가 교체한 구세대 스레드가 새 스레드와 같은 표를 또 잡는 경쟁 창을 제거.
  - **예약 직후 이력 스윕** (v2.4.0): 예약 성공 직후(결제 전) 계정 이력을 다시 조회해, **같은 열차 미결제 중복은 초과분을 즉시 취소**하고, **이미 결제/발권된 같은 표가 있으면 방금 예약을 취소**하고 기존 표를 쓴다. 어떤 경로로 중복이 생겼든 결제 전에 잡는 최후 방어선. (같은 구간 다른 열차 예약은 의도적일 수 있어 경고만 남기고 보존)
  - **폴링 중 주기 재검사** (v2.4.0): 긴 폴링 중에도 10분마다 계정 이력을 재검사 — 폰 앱으로 수동 예매했거나 다른 세션이 이미 표를 잡았으면 매크로가 표 잡기 전에 스스로 멈춘다. 열차번호 0-패딩 차이('323' vs '00323')로 중복검사가 같은 열차를 놓치던 버그도 함께 수정.
- **뚜껑 닫아도 계속** (macOS, 선택): `bash setup_lid_mode.sh` 를 한 번 실행하면
  (관리자 비밀번호 1회) 이후 **활성 잡이 도는 동안만** `pmset disablesleep`을 자동으로
  켜서 뚜껑을 닫아도 폴링이 계속된다. 잡이 없으면 자동으로 꺼져 평소 배터리엔 영향 없음.
  ⚠ 잡 도는 중 뚜껑 닫은 채 가방에 넣으면 발열 주의. 해제:
  `sudo rm /etc/sudoers.d/k-rail-pmset && sudo pmset -a disablesleep 0`
- 열차종류: KTX·SRT(기본 전체) + ITX-새마을/무궁화호/누리로/ITX-청춘
- 토스트 알림 + 실시간 로그

### 카드 테스트
서울→광명 25일 뒤 평일 첫차를 reserve→pay→refund 하며, 4겹 안전장치 (snapshot · PNR 일치 · route/date 검증 · post-audit)로 **남의 표 환불을 차단**한다. 위약금 약 400원/회.

## 폰에서 쓰기 (원격 상주 세팅, macOS)

맥에서 한 번 실행:

```bash
bash setup_remote.sh
```

이게 해주는 것:

1. **launchd 상주** — 로그인하면 서버 자동 시작, 죽으면 launchd가 자동 재시작 (재부팅에도 살아남음. `run_supervised.sh` nohup 방식 대체)
2. **테일넷 접속** — `K_RAIL_HOST=0.0.0.0` 바인딩 + 서버 미들웨어가 로컬호스트·Tailscale 대역(100.64/10) 외 접근을 전부 403 차단. 폰 브라우저(폰도 Tailscale ON)에서:

   ```
   http://<맥 테일스케일IP>:8912     # 맥에서 tailscale ip -4 로 확인
   ```

   Tailscale은 WireGuard 암호화 사설망이라 HTTP여도 안전하고, 회사망/공용망의 다른 기기는 접근이 차단된다.

여기에 `setup_lid_mode.sh`(뚜껑 닫힘 방지)까지 하면: **뚜껑 닫힌 맥북을 그대로 두고, 폰 브라우저나 폰의 Claude 원격 세션에서 잡을 걸고 표를 잡는다.**

폰 Claude 원격 세션에서 API로 직접 조작할 때:

```bash
# 잡 목록
curl -s http://127.0.0.1:8912/api/rail/jobs
# 잡 등록 (예: 수서→부산 8/1 08시 이후 — KTX·SRT 구분 없이 같은 경로)
curl -s -X POST http://127.0.0.1:8912/api/rail/jobs -H 'Content-Type: application/json' \
  -d '{"dep":"수서","arr":"부산","date":"20260801","time":"080000","pay_mode":"manual"}'
# 경로우대 1명(만 65세+ 부모님 표) — 30% 할인가로 예약
curl -s -X POST http://127.0.0.1:8912/api/rail/jobs -H 'Content-Type: application/json' \
  -d '{"dep":"서울","arr":"부산","date":"20261010","time":"080000","passengers":1,"seniors":1}'
# 특정 열차(SRT 305편)만 노릴 때 — train_number로 지정
curl -s -X POST http://127.0.0.1:8912/api/rail/jobs -H 'Content-Type: application/json' \
  -d '{"dep":"수서","arr":"부산","date":"20260801","time":"065400","train_number":"305"}'
# 예약 후 결제 진행 / 잡 중지
curl -s -X POST http://127.0.0.1:8912/api/rail/jobs/j1/pay
curl -s -X DELETE http://127.0.0.1:8912/api/rail/jobs/j1
# 구 경로 /api/ktx/* 도 같은 라우터라 그대로 동작한다(하위호환). /api/srt/* 는 제거됨.
```

관리 명령: 중지 `launchctl bootout gui/$(id -u)/com.chihunlee.k-rail-macro` · 전체 해제 `bash setup_remote.sh --remove` · 로그 `/tmp/k-rail-macro.log`

### 폰 Claude 디스패치로 예매 걸기 (`/krail` 스킬)

폰 Claude 앱에서 **이 맥으로 새 세션을 디스패치**한 뒤 기차정보만 말하면 된다:

```
/krail 수서→오송 8월1일 08시 이후 SRT
```

스킬(`~/.claude/skills/krail`)이 서버 확인(죽어있으면 launchd 재기동) → 잡 등록 → 표 잡히면 Claude 앱 푸시 알림까지 처리한다. 전제조건: ① 맥 전원/네트워크 ON (`setup_remote.sh` launchd 상주 + claude-keepawake) ② 폰 Claude 앱 ↔ 이 맥 연결(Claude Code 원격 세션) ③ 결제는 자동(pay_mode=auto)이 기본 — 수동 확인을 원하면 "수동결제"라고 명시(그 경우 표 잡힌 뒤 "결제 진행해" 답장으로 결제).

## v2에서 올라올 때

- **코레일 자격증명은 그대로 쓴다** — Keychain 항목(`ktx-macro`)·저장 형식을 안 바꿨다.
  SRT 계정 정보(`srt-macro`)는 이제 쓰지 않는다(Keychain에 남아있어도 무해).
- 활성 잡은 자동 승계된다 — 저장 파일의 구 `ktx` 항목도 기동 시 함께 복원한다.
  SR 엔진으로 돌던 잡은 승계되지 않으니 새로 등록해야 한다.
- Keychain 항목 이름이 같음 (`ktx-macro`) → **저장한 자격증명 그대로 마이그레이션됨**
- 단독 매크로(8910 / 8911)와 통합 매크로(8912)는 다른 포트라 동시에 실행해도 충돌 없음
- 단독 매크로 안 쓸 거면 `~/Applications/SRT 매크로.app` / `KTX 매크로.app` 삭제 + `kill $(lsof -ti tcp:8910 -sTCP:LISTEN)` 등으로 정리

---

## 직접 빌드 / 개발

```bash
git clone https://github.com/Chihun-Lee/korea-skill-chihun.git
cd korea-skill-chihun
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python server.py
# → http://127.0.0.1:8912
```

### 파일 구조

| 파일 | 용도 |
|------|------|
| `server.py` | FastAPI 엔트리, `/api/rail/*` (+ 구 `/api/ktx/*` 별칭) 라우팅 |
| `rail_worker.py` | 통합 polling/reserve/pay + 조회 페이지네이션 (`search_all`) |
| `korail_client.py` | srtgo Korail + Dynapath bypass + 좌석위치속성 reserve |
| `stations.py` | 코레일에서 동작 확인된 전국 45개 역 + 별칭 |
| `seatpref.py` | 좌석 선호 판정 (창측/맨앞·뒷열 채점, 순수 로직) |
| `config.py` | 코레일 자격증명 1개 namespace (`config.rail`) Keychain 저장 |
| `jobstore.py` | 활성 잡 디스크 저장/복원 (서버 재시작 시 자동 재개) |
| `run_supervised.sh` | macOS 서버 감시 루프 (죽으면 자동 재시작) |
| `setup_lid_mode.sh` | 뚜껑 닫아도 잡 유지용 1회 설정 (pmset sudoers) |
| `setup_remote.sh` | 폰 원격용 상주 세팅 (launchd + tailscale serve) |
| `static/index.html` | 단일 화면 UI (역 칩은 `/api/stations`가 출처) |
| `test_v3.py` | v3.0 핵심 로직 테스트 (페이지네이션·대상선택·역명) |
| `install.sh` | 친구용 원클릭 설치 |

### 라이선스 / 출처

- [srtgo](https://github.com/lapis42/srtgo) (MIT) — 코레일 클라이언트 / `pay_with_card` 구현
- Dynapath bypass — [nomadamas/k-skill](https://github.com/nomadamas/k-skill) (MIT)
