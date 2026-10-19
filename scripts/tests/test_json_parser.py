from __future__ import annotations

import json

import pytest

import generate_daily


def test_parse_json_accepts_trailing_model_commentary() -> None:
    assert generate_daily.parse_json('{"status":"PASS"}\nتم التحقق.') == {"status": "PASS"}


def test_parse_json_accepts_fenced_json() -> None:
    assert generate_daily.parse_json('```json\n{"status":"PASS"}\n```') == {"status": "PASS"}


def test_parse_json_rejects_broken_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        generate_daily.parse_json('{"status":')


def test_parse_json_rejects_second_structured_value() -> None:
    with pytest.raises(json.JSONDecodeError, match="Extra data"):
        generate_daily.parse_json('{"status":"PASS"}\n{"status":"REJECT"}')
