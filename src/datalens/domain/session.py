from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    created_at: datetime
    last_accessed_at: datetime
    expires_at: datetime
    locale: Literal["ko", "ja"] = "ko"
    queryforge_session_id: str | None = None
    active_dataset_id: str | None = None
    active_table: str | None = None
    active_period: dict[str, str] | None = None
    turn_state: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def public(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "expires_at": self.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "locale": self.locale,
        }

    def with_queryforge_session(self, queryforge_session_id: str) -> "Session":
        if self.queryforge_session_id not in (None, queryforge_session_id):
            raise ValueError("QueryForge Application Session is already bound")
        return replace(self, queryforge_session_id=queryforge_session_id)
