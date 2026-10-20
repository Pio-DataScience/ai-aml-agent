"""SQL-only tests for the P7 A-side filter verification guard."""

from services.aml_builder.web.domain.filter_applied_check import (
    check_filter_application,
)


FILTER = {
    "dimension": "explanation_code",
    "level": "transaction",
    "target": {"table": "PIO_TRANSACTIONS", "column": "EXPL_CODE"},
    "values": ["110", "1414"],
    "polarity": "positive",
    "polarity_allowed": False,
}


def test_filter_check_accepts_exact_positive_set_with_table_alias():
    result = check_filter_application(
        "SELECT t.EXPL_CODE FROM PIO_TRANSACTIONS t WHERE t.EXPL_CODE IN ('110', '1414')",
        [FILTER],
        unverifiable="fail",
    )

    assert result["validation_success"] is True
    assert result["filters"][0]["status"] == "applied"


def test_filter_check_reports_missing_and_mismatched_values():
    missing = check_filter_application("SELECT 1 FROM PIO_TRANSACTIONS", [FILTER])
    mismatch = check_filter_application(
        "SELECT 1 FROM PIO_TRANSACTIONS WHERE EXPL_CODE IN ('110')", [FILTER]
    )

    assert missing["filters"][0]["status"] == "missing"
    assert mismatch["filters"][0]["status"] == "mismatch"
    assert mismatch["validation_success"] is False


def test_negative_polarity_requires_allowed_dimension_and_negative_predicate():
    negative = {**FILTER, "polarity": "negative", "polarity_allowed": True}
    good = check_filter_application(
        "SELECT 1 FROM PIO_TRANSACTIONS WHERE EXPL_CODE NOT IN ('110', '1414')",
        [negative],
    )
    wrong_polarity = check_filter_application(
        "SELECT 1 FROM PIO_TRANSACTIONS WHERE EXPL_CODE IN ('110', '1414')", [negative]
    )
    forbidden = check_filter_application(
        "SELECT 1 FROM PIO_TRANSACTIONS WHERE EXPL_CODE <> '110'",
        [{**negative, "polarity_allowed": False}],
    )

    assert good["filters"][0]["status"] == "applied"
    assert wrong_polarity["filters"][0]["status"] == "mismatch"
    assert forbidden["filters"][0]["status"] == "mismatch"


def test_unextractable_and_unparseable_sql_follow_warn_or_fail_policy():
    subquery = check_filter_application(
        "SELECT 1 FROM PIO_TRANSACTIONS WHERE EXPL_CODE IN (SELECT CODE FROM CODES)",
        [FILTER],
        unverifiable="warn",
    )
    wrapped = check_filter_application(
        "SELECT 1 FROM PIO_TRANSACTIONS WHERE UPPER(EXPL_CODE) IN ('110', '1414')",
        [FILTER],
        unverifiable="warn",
    )
    function_value = check_filter_application(
        "SELECT 1 FROM PIO_TRANSACTIONS WHERE EXPL_CODE = TO_CHAR(110)",
        [FILTER],
        unverifiable="warn",
    )
    parse_warn = check_filter_application(
        "SELECT FROM !!!", [FILTER], unverifiable="warn"
    )
    parse_fail = check_filter_application(
        "SELECT FROM !!!", [FILTER], unverifiable="fail"
    )

    assert subquery["filters"][0]["status"] == "unverified"
    assert wrapped["filters"][0]["status"] == "unverified"
    assert function_value["filters"][0]["status"] == "unverified"
    assert parse_warn["validation_success"] is True
    assert parse_warn["filters"][0]["status"] == "unverified"
    assert parse_fail["validation_success"] is False


def test_all_any_explanation_scope_skips_code_predicate_requirement():
    result = check_filter_application(
        "SELECT 1 FROM PIO_TRANSACTIONS",
        [FILTER],
        transaction_scope="ALL",
    )

    assert result["validation_success"] is True
    assert result["filters"] == []
