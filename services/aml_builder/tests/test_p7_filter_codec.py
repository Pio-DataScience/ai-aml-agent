"""Typed result envelope coverage for the P7 filter safety check."""

import json

from services.aml_builder.web.api.codec import encode_tool_result


def test_filter_not_applied_keeps_raw_sql_and_typed_per_filter_result():
    encoded = encode_tool_result(
        "execute_oracle_dwh_shadow_test",
        {
            "raw_sql": "SELECT 1 FROM PIO_TRANSACTIONS",
            "validation_success": False,
            "error_code": "FILTER_NOT_APPLIED",
            "filter_check": {
                "validation_success": False,
                "filters": [{"dimension": "explanation_code", "status": "missing"}],
            },
        },
    )

    result = json.loads(encoded)
    assert result["status"] == "error"
    assert result["error_code"] == "FILTER_NOT_APPLIED"
    assert result["payload"]["raw_sql"] == "SELECT 1 FROM PIO_TRANSACTIONS"
    assert result["payload"]["filter_check"]["filters"][0]["status"] == "missing"
