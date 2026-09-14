from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Literal


@lru_cache
def load_system_prompt(locale: Literal["ko", "ja"]) -> str:
    return files(__package__).joinpath(locale, "system.txt").read_text(encoding="utf-8").strip()
