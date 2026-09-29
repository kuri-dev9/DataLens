from __future__ import annotations

from functools import lru_cache

from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATALENS_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    http_host: str = "0.0.0.0"
    http_port: int = Field(8000, ge=1, le=65535)
    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"
    request_deadline_seconds: float = Field(240.0, gt=0)
    session_ttl_seconds: float = Field(1800.0, gt=0)
    country: Literal["KR", "JP"] = "KR"
    queryforge_endpoint: AnyHttpUrl = "http://127.0.0.1:8080/mcp"
    queryforge_data_base_url: AnyHttpUrl = "http://127.0.0.1:8080"
    queryforge_api_key: SecretStr
    queryforge_timeout_seconds: float = Field(5.0, gt=0)
    agent_max_tool_calls: int = Field(12, gt=0)
    agent_recovery_budget: int = Field(5, ge=0)
    agent_preview_rows: int = Field(5, ge=1, le=100)
    llm_provider: Literal["ollama"] = "ollama"
    ollama_base_url: AnyHttpUrl = "http://127.0.0.1:11434"
    ollama_model: str = Field("gemma4:26b", min_length=1, max_length=128)
    ollama_request_timeout_seconds: float = Field(120.0, gt=0)
    # 유휴 판정 기준. 이 시간 동안 스트림 청크가 하나도 없을 때만 LLM 호출을 끊는다(ADR-033).
    ollama_idle_timeout_seconds: float = Field(120.0, gt=0)
    # 단일 생성의 토큰 상한. 반복 루프처럼 "살아있지만 끝나지 않는" 생성을 유일하게 끊는 장치다.
    ollama_num_predict: int = Field(2048, gt=0)
    # 생성 진행 중 progress 이벤트 발행 주기.
    llm_progress_interval_seconds: float = Field(15.0, gt=0)
    ollama_num_ctx: int = Field(8192, gt=0)
    ollama_temperature: float = Field(0.2, ge=0)
    ollama_top_p: float = Field(0.95, ge=0, le=1)
    ollama_top_k: int = Field(64, gt=0)
    enable_thinking: bool = False
    memory_enabled: bool = True
    memory_path: str = "/var/lib/datalens/memory.sqlite3"
    embedding_model: str = Field("bge-m3", min_length=1, max_length=128)
    # 비워두면 LLM과 같은 Ollama를 쓴다. CPU 전용 인스턴스를 따로 띄웠다면 그 주소를 넣는다.
    embedding_base_url: AnyHttpUrl | None = None
    embedding_api: Literal["ollama", "openai"] = "ollama"
    embedding_api_key: SecretStr | None = None
    memory_recipe_limit: int = Field(3, ge=0, le=10)
    memory_term_limit: int = Field(5, ge=0, le=20)
    memory_threshold: float = Field(0.55, ge=0, le=1)
    memory_merge_threshold: float = Field(0.93, ge=0, le=1)
    memory_max_recipes: int = Field(500, gt=0)
    memory_timeout_seconds: float = Field(5.0, gt=0)
    cors_origins: str = "*"
    api_key: SecretStr

    @property
    def allowed_cors_origins(self) -> tuple[str, ...]:
        origins = tuple(item.strip() for item in self.cors_origins.split(",") if item.strip())
        return origins or ("*",)

    @property
    def timezone(self) -> str:
        return {"KR": "Asia/Seoul", "JP": "Asia/Tokyo"}[self.country]

    @property
    def default_locale(self) -> Literal["ko", "ja"]:
        return {"KR": "ko", "JP": "ja"}[self.country]

    def queryforge_url(self) -> str:
        return str(self.queryforge_endpoint)

    def queryforge_data_url(self) -> str:
        return str(self.queryforge_data_base_url).rstrip("/")

    def ollama_url(self) -> str:
        return str(self.ollama_base_url).rstrip("/")

    def embedding_url(self) -> str:
        return str(self.embedding_base_url).rstrip("/") if self.embedding_base_url else self.ollama_url()


@lru_cache
def get_settings() -> Settings:
    return Settings()
