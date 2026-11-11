from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal


#: 질문 글자 — 한글 음절·자모와 히라가나·가타카나. **한자는 세지 않는다** — 일본어와 한국어가 모두 지명에
#: 한자를 쓴다(新宿区·千代田区)
_HANGUL = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]")
_KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u30fc]")


def question_locale(text: str | None) -> Literal["ko", "ja"] | None:
    """질문을 쓴 말 — 한글이 많으면 ko, 가나가 많으면 ja, 둘 다 없으면 None(세션 기본값을 쓴다).

    **답 언어는 세션이 아니라 질문한 언어를 따른다**(2026-11-11 사용자 결정). 세션 `locale` 은 만들 때 한 번
    정해져, ja 세션에 한국어로 물으면 일본어로 답했다 — 시연자는 한국어로 묻고 현장 운영자는 일본어로 묻는다.
    같은 세션에서 두 말이 섞여도 턴마다 자기 말로 답한다. 둘이 같은 수면 판정하지 않는다(None)
    """
    t = str(text or "")
    ko, ja = len(_HANGUL.findall(t)), len(_KANA.findall(t))
    if ko > ja:
        return "ko"
    if ja > ko:
        return "ja"
    return None


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
    # 이 세션에서 만들어진 데이터셋의 계보. 모델이 과거 데이터셋을 지어내지 않고
    # 그대로 재사용할 수 있도록 세션 컨텍스트로 노출된다 (ADR-035).
    dataset_lineage: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # 도구 정의 lazy 모드에서 이 세션이 이미 전체 정의를 받은 도구들. 한 번 펼친 도구는 계속 펼쳐 둔다.
    expanded_tools: tuple[str, ...] = field(default_factory=tuple)

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
