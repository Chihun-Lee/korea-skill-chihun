"""한달치 시간표 사전 다운로드 캐시.

열차 '시각표'는 거의 바뀌지 않으므로 미리 받아두면 다음 조회가 즉시 된다.
좌석 유무(general/special)는 **받은 시점의 값**이라 참고용 — 예매 직전에는
라이브 조회로 확인해야 한다.

파일: ~/.k-rail-macro/timetable_cache.json
구조: {svc: {"dep→arr": {"YYYYMMDD": {"fetched_at": iso, "rows": [...]}}}}
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

PATH = Path.home() / ".k-rail-macro" / "timetable_cache.json"
_lock = threading.Lock()


def _read() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def put(svc: str, dep: str, arr: str, date: str, rows: list[dict]) -> None:
    with _lock:
        data = _read()
        route = data.setdefault(svc, {}).setdefault(f"{dep}→{arr}", {})
        route[date] = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "rows": rows,
        }
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 캐시 저장 실패가 조회를 막아서는 안 된다


def get(svc: str, dep: str, arr: str, date: str) -> dict | None:
    with _lock:
        data = _read()
        hit = data.get(svc, {}).get(f"{dep}→{arr}", {}).get(date)
        if hit is None and svc == "rail":
            # v3.0 이전에 받아둔 캐시(kind "ktx"/"srt")도 그대로 쓴다 —
            # 시각표는 운영사가 달라도 같은 열차 같은 시각이다.
            for legacy in ("ktx", "srt"):
                hit = data.get(legacy, {}).get(f"{dep}→{arr}", {}).get(date)
                if hit is not None:
                    break
        return hit


def status() -> dict:
    """캐시 현황 요약: 서비스별 구간별 (날짜 수, 최근 갱신 시각)."""
    with _lock:
        data = _read()
    out = {}
    for svc, routes in data.items():
        out[svc] = {}
        for route, dates in routes.items():
            fetched = [e["fetched_at"] for e in dates.values()]
            out[svc][route] = {
                "dates": len(dates),
                "first_date": min(dates) if dates else None,
                "last_date": max(dates) if dates else None,
                "last_fetched": max(fetched) if fetched else None,
            }
    return out
