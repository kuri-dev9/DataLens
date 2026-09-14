from __future__ import annotations

from datalens.domain.session import Session
from datalens.ports.queryforge import QueryForgeClient


class QueryForgeSessionCleanup:
    def __init__(self, client: QueryForgeClient, timeout_seconds: float) -> None:
        self._client = client
        self._timeout = timeout_seconds

    async def cleanup(self, session: Session) -> None:
        if session.queryforge_session_id is not None:
            await self._client.release_application_session(
                session.queryforge_session_id, self._timeout
            )
