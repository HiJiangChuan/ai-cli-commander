"""claude JSON 输出解析测试。"""
from __future__ import annotations

import pytest

from ai_commander.agents.claude import _parse_response


def test_clean_json():
    assert _parse_response('{"result": "hi"}') == {"result": "hi"}


def test_json_with_surrounding_noise():
    out = 'some warning line\n{"result": "ok", "total_cost_usd": 0.01}\ntrailing noise'
    data = _parse_response(out)
    assert data["result"] == "ok"
    assert data["total_cost_usd"] == 0.01


def test_invalid_json_raises():
    with pytest.raises(RuntimeError, match="无效 JSON"):
        _parse_response("not json at all")


def test_non_dict_json_raises():
    with pytest.raises(RuntimeError, match="无效 JSON"):
        _parse_response("42")


def test_broken_then_valid_line():
    out = '{"broken": \n{"result": "recovered"}'
    assert _parse_response(out)["result"] == "recovered"
