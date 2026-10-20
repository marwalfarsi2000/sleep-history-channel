from __future__ import annotations

import pytest

import generate_daily


def test_parse_json_object_accepts_plain_object():
    assert generate_daily.parse_json_object('{"status":"PASS"}', "TEST_SCHEMA_FAILED") == {
        "status": "PASS"
    }


def test_parse_json_object_unwraps_singleton_object_list():
    assert generate_daily.parse_json_object('[{"status":"PASS"}]', "TEST_SCHEMA_FAILED") == {
        "status": "PASS"
    }


def test_parse_json_object_rejects_multi_item_list_with_context_code():
    with pytest.raises(ValueError, match=r"TEST_SCHEMA_FAILED: expected one JSON object, got list\[2\]"):
        generate_daily.parse_json_object(
            '[{"status":"PASS"},{"status":"FAIL"}]',
            "TEST_SCHEMA_FAILED",
        )


def test_parse_json_object_rejects_scalar_with_context_code():
    with pytest.raises(ValueError, match="TEST_SCHEMA_FAILED: expected one JSON object, got str"):
        generate_daily.parse_json_object('"PASS"', "TEST_SCHEMA_FAILED")
