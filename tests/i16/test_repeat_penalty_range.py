"""`repeat_penalty` 설정 — 범위·전달 (2026-11-07).

반복을 막는 값이지만 올리면 수치·이름을 다르게 옮길 위험이 있어 **기본은 1.0(끔)** 이다. 화면(/ui)에서 모드별로 덮어쓸 수 있고, 범위는 1.0~2.0 이다.
"""
from __future__ import annotations

import pytest

from datalens.application import runtime_config as rc
from datalens.config import Settings


@pytest.mark.parametrize("ok", [1.0, 1.1, 1.5, 2.0, 1, 2])
def test_values_inside_the_range_are_accepted_as_floats(ok):
    got = rc.RuntimeConfig._check_param("repeat_penalty", ok)
    assert got == float(ok) and isinstance(got, float)


@pytest.mark.parametrize("bad", [0.99, 0.0, -1.0, 2.01, 3, 100])
def test_values_outside_the_range_are_rejected(bad):
    with pytest.raises(rc.RuntimeConfigError, match="repeat_penalty must be within 1.0..2.0"):
        rc.RuntimeConfig._check_param("repeat_penalty", bad)


@pytest.mark.parametrize("bad", ["1.1", True, False, [1.1]])
def test_non_numbers_are_rejected(bad):
    with pytest.raises(rc.RuntimeConfigError, match="must be a number"):
        rc.RuntimeConfig._check_param("repeat_penalty", bad)


def test_none_clears_the_override():
    assert rc.RuntimeConfig._check_param("repeat_penalty", None) is None


def test_the_range_and_the_field_are_registered_together():
    assert rc._PARAM_FIELDS["repeat_penalty"] == "ollama_repeat_penalty"
    assert rc._PARAM_RANGES["repeat_penalty"] == (1.0, 2.0)


def test_the_startup_default_is_off_and_its_range_matches():
    f = Settings.model_fields["ollama_repeat_penalty"]
    assert f.default == 1.0
    metas = {type(m).__name__: m for m in f.metadata}
    assert metas["Ge"].ge == 1.0 and metas["Le"].le == 2.0
