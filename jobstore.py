"""활성 작업(잡) 스펙의 디스크 저장/복원.

잡이 메모리에만 있으면 서버 프로세스가 죽는 순간 전부 사라져, 앱을 다시 켜도
표잡기가 재개되지 않는다. 활성 잡 스펙을 JSON으로 저장해 서버가 다시 뜰 때
자동 복원한다(표 잡을 때까지 계속). v3.0부터 엔진이 코레일 하나라
kind는 "rail" 하나지만, 구버전이 남긴 "ktx" 항목도 복원 시 함께 읽는다.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

PATH = Path.home() / ".k-rail-macro" / "jobs.json"
_lock = threading.Lock()


def save(kind: str, specs: list[dict]) -> None:
    with _lock:
        data = _read()
        data[kind] = specs
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass  # 저장 실패가 표잡기를 멈춰서는 안 된다


def load(kind: str) -> list[dict]:
    with _lock:
        return _read().get(kind, [])


def _read() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
