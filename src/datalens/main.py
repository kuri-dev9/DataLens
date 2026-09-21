from __future__ import annotations

import uvicorn

from datalens.api.app import build_app
from datalens.application.agent import BoundedAgent
from datalens.application.chat import ChatApplicationService
from datalens.application.sessions import SessionService
from datalens.config import get_settings
from datalens.application.memory import MemoryService
from datalens.infrastructure.ollama import OllamaProvider
from datalens.infrastructure.embeddings import HttpEmbedder
from datalens.infrastructure.sqlite_memory import SqliteMemoryStore
from datalens.infrastructure.queryforge_mcp import McpQueryForgeClient
from datalens.infrastructure.session_cleanup import QueryForgeSessionCleanup
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.observability import configure_logging


def create_app():
    settings = get_settings()
    store = InMemorySessionStore(settings.session_ttl_seconds, settings.default_locale)
    queryforge = McpQueryForgeClient(
        settings.queryforge_url(),
        settings.queryforge_api_key.get_secret_value(),
        data_base_url=settings.queryforge_data_url(),
        default_timeout_seconds=settings.queryforge_timeout_seconds,
    )
    sessions = SessionService(
        store, QueryForgeSessionCleanup(queryforge, settings.queryforge_timeout_seconds)
    )
    provider = OllamaProvider(
        settings.ollama_url(),
        settings.ollama_model,
        request_timeout_seconds=settings.ollama_request_timeout_seconds,
        num_ctx=settings.ollama_num_ctx,
        temperature=settings.ollama_temperature,
        top_p=settings.ollama_top_p,
        top_k=settings.ollama_top_k,
        enable_thinking=settings.enable_thinking,
    )
    agent = BoundedAgent(
        provider,
        queryforge,
        max_tool_calls=settings.agent_max_tool_calls,
        recovery_budget=settings.agent_recovery_budget,
        preview_rows=settings.agent_preview_rows,
        timezone=settings.timezone,
    )
    memory = None
    if settings.memory_enabled:
        memory = MemoryService(
            SqliteMemoryStore(settings.memory_path),
            HttpEmbedder(
                settings.embedding_url(),
                settings.embedding_model,
                api=settings.embedding_api,
                api_key=settings.embedding_api_key.get_secret_value() if settings.embedding_api_key else None,
            ),
            recipe_limit=settings.memory_recipe_limit,
            term_limit=settings.memory_term_limit,
            threshold=settings.memory_threshold,
            merge_threshold=settings.memory_merge_threshold,
            max_recipes=settings.memory_max_recipes,
            timeout_seconds=settings.memory_timeout_seconds,
        )
    chat = ChatApplicationService(sessions, agent, memory)
    return build_app(
        settings,
        session_service=sessions,
        readiness=queryforge,
        llm_readiness=provider,
        message_handler=chat,
        queryforge_client=queryforge,
        memory=memory,
        closeables=(provider,),
    )


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level.upper())
    uvicorn.run(
        create_app(),
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.log_level,
        workers=1,
    )


if __name__ == "__main__":
    main()
