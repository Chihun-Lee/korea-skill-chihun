"""자격증명 저장소 (macOS Keychain).

v3.0부터 코레일 계정 하나만 쓴다(코레일·SR 발매 통합 — 코레일 계정으로 SRT도
예약된다). 네임스페이스는 `config.rail` 하나.

⚠ Keychain 서비스명은 계속 "ktx-macro", 저장 키도 계속 ktx_id/ktx_password다 —
   이름을 바꾸면 이미 저장된 자격증명을 사용자가 다시 입력해야 하므로 그대로 둔다
   (코드에서만 rail_id/rail_password로 읽고 쓴다).
"""
from __future__ import annotations

import json
import sys
from typing import Optional

import keyring
from pydantic import BaseModel, ConfigDict, Field


def storage_label() -> str:
    """현재 OS의 자격증명 저장소 표시 이름 (keyring 백엔드에 대응)."""
    if sys.platform == "win32":
        return "Windows 자격 증명 관리자"
    if sys.platform == "darwin":
        return "macOS Keychain"
    return "시스템 자격증명 저장소"


# ─── 코레일(KTX + SRT 통합) ──────────────────────────────────────────────
class RailCredentials(BaseModel):
    """코레일 계정 + 결제카드. 별칭(ktx_*)으로 기존 Keychain 데이터와 호환."""

    model_config = ConfigDict(populate_by_name=True)

    rail_id: str = Field(min_length=1, alias="ktx_id")
    rail_password: str = Field(min_length=1, alias="ktx_password")
    card_number: str = Field(default="", max_length=19)
    card_password: str = Field(default="", max_length=2)
    card_validation: str = Field(default="", max_length=10)
    card_expire: str = Field(default="", max_length=4)
    card_installment: int = Field(default=0, ge=0, le=24)


class _Namespace:
    def __init__(self, service: str, model: type[BaseModel]):
        self.service = service
        self.model = model
        self.user = "config"

    def _read_blob(self) -> Optional[str]:
        return keyring.get_password(self.service, self.user)

    def exists(self) -> bool:
        return self._read_blob() is not None

    def load(self):
        blob = self._read_blob()
        if not blob:
            return None
        try:
            return self.model.model_validate_json(blob)
        except Exception:
            return None

    def save(self, creds) -> None:
        # by_alias=True — 저장 형식(ktx_id/ktx_password)을 v2와 동일하게 유지한다
        payload = creds.model_dump(by_alias=True)
        if "card_number" in payload:
            payload["card_number"] = payload["card_number"].replace("-", "").replace(" ", "")
        keyring.set_password(self.service, self.user, json.dumps(payload))

    def clear(self) -> None:
        try:
            keyring.delete_password(self.service, self.user)
        except keyring.errors.PasswordDeleteError:
            pass


class _Rail(_Namespace):
    def __init__(self):
        super().__init__("ktx-macro", RailCredentials)

    def public_status(self) -> dict:
        c = self.load()
        if not c:
            return {"configured": False, "storage": storage_label()}
        has_card = bool(c.card_number)
        out = {
            "configured": True,
            "id": c.rail_id,
            "has_card": has_card,
            "storage": storage_label(),
        }
        if has_card:
            out["card_last4"] = c.card_number[-4:]
            out["card_masked"] = "*" * (len(c.card_number) - 4) + c.card_number[-4:]
            out["card_installment"] = c.card_installment
        return out


rail = _Rail()
