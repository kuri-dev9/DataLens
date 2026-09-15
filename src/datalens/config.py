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
    request_deadline_seconds: float = Field(120.0, gt=0)
    session_ttl_seconds: float = Field(1800.0, gt=0)
    country: Literal["KR", "JP"] = "KR"
    queryforge_endpoint: AnyHttpUrl = "http://127.0.0.1:8080/mcp"
    queryforge_data_base_url: AnyHttpUrl = "http://127.0.0.1:8080"
    queryforge_api_key: SecretStr
    queryforge_timeout_seconds: float = Field(5.0, gt=0)
    agent_max_tool_calls: int = Field(3, gt=0)
    agent_recovery_budget: int = Field(1, ge=0)
    agent_preview_rows: int = Field(5, ge=1, le=100)
    llm_provider: Literal["ollama"] = "ollama"
    ollama_base_url: AnyHttpUrl = "http://127.0.0.1:11434"
    ollama_model: str = Field("gemma4:26b", min_length=1, max_length=128)
    ollama_request_timeout_seconds: float = Field(120.0, gt=0)
    ollama_num_ctx: int = Field(8192, gt=0)
    ollama_temperature: float = Field(1.0, ge=0)
    ollama_top_p: float = Field(0.95, ge=0, le=1)
    ollama_top_k: int = Field(64, gt=0)
    enable_thinking: bool = False
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
