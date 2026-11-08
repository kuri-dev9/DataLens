from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Literal


@lru_cache
def load_system_prompt(locale: Literal["ko", "ja"], kind: Literal["system", "evidence"] = "system") -> str:
    """`system` 은 QueryForge 데이터 에이전트 지침, `evidence` 는 증거 전용 모드 지침(2026-11-04)."""
    return files(__package__).joinpath(locale, f"{kind}.txt").read_text(encoding="utf-8").strip()
