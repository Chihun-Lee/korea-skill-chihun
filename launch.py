"""Windows 단일 exe 진입점 (KTX + SRT 통합 — 코레일 단일 엔진).

서버를 127.0.0.1:8912에 띄우고, 잠시 뒤 기본 브라우저로 GUI를 연다.
이 콘솔 창을 닫으면(또는 Ctrl+C) 서버가 종료된다.

PyInstaller --onefile로 묶일 때를 위한 엔트리이며, import string 대신
app 객체를 직접 넘겨 frozen 환경에서 안전하게 동작한다.

콘솔 창은 서버가 (정상이든 오류든) 종료될 때 바로 닫지 않고 메시지를
보여준 뒤 엔터를 기다린다. 오류 traceback이 창과 함께 사라져 읽지 못하는
문제를 막기 위함이다.
"""
from __future__ import annotations

import socket
import sys
import threading
import traceback
import webbrowser

import uvicorn

from server import app

PORT = 8912
URL = f"http://127.0.0.1:{PORT}"


def _open_browser() -> None:
    webbrowser.open(URL)


def _port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pause(msg: str = "\n엔터를 누르면 이 창을 닫습니다... ") -> None:
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    print("=" * 56)
    print("  K-Rail 매크로 (KTX + SRT 통합) 실행 중")
    print(f"  브라우저에서 {URL} 가 열립니다.")
    print("  종료하려면 이 창을 닫거나 Ctrl+C 를 누르세요.")
    print("=" * 56)

    # 이미 실행 중이면(옛 서버가 포트를 잡고 있으면) 새 서버를 띄우지 않고
    # 브라우저만 연다. 안 그러면 새 exe는 포트 충돌로 죽고 브라우저는 옛
    # 서버에 붙어 "고친 게 반영 안 됨"처럼 보인다.
    if _port_in_use(PORT):
        print("\n[알림] 이미 실행 중인 창이 있습니다 → 브라우저만 엽니다.")
        print("       완전히 새로 켜려면 기존 검은 창을 모두 닫고 다시 실행하세요.")
        _open_browser()
        _pause()
        sys.exit(0)

    threading.Timer(1.5, _open_browser).start()
    # 서버가 어떤 이유로든 죽어도 자동 재시작해 표잡기를 계속한다.
    # (활성 잡은 jobs.json에서 자동 복원) 종료는 Ctrl+C 또는 창 닫기.
    while True:
        try:
            uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
            print("\n서버가 종료됐습니다.")
            break
        except KeyboardInterrupt:
            print("\n종료합니다.")
            break
        except Exception:
            print("\n[오류] 서버가 예기치 않게 종료됐습니다 → 2초 후 자동 재시작:\n")
            traceback.print_exc()
            import time
            time.sleep(2)
    _pause()
