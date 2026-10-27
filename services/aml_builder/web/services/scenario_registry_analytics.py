"""
Read-only analytics and search over the agent's own scenario registry.

Answers compliance officer questions about scenarios this agent has
persisted — inventory counts, governance breakdowns, natural-language
search, deep-dive detail, and alert telemetry — without ever mutating
data. Every query is scoped to PIO_AML_PRODUCTION_SCENARIOS (this agent's
own registry), left-joined to PIO_AML_SCENARIO for governance metadata so
scenarios created by other systems are never accidentally surfaced.

Semantic search computes embeddings live, per call, against scenario text
fetched fresh from Oracle — no persistent cache. At the scale scenarios
exist at (dozens to low hundreds, not thousands), this is simpler and
strictly more correct than a cached index: no invalidation to get wrong,
no staleness across worker processes, always reflects the live registry.

Alert telemetry (PIO_AML_CUSTOMERS / PIO_AML_CUSTOMERS_DET) is populated by
the Standalone Alert Execution Engine (see Docs/04_standalone_alert_engine.md,
alert_engine.py, run_alert_engine.py) — it is not on an automatic schedule,
only triggered via CLI or POST /engine/run-scenarios. Every alert-telemetry
response carries a note explaining this, so a zero/low count is never
presented as proof a scenario is broken — it may simply mean the engine
hasn't been run yet for that scenario/date.
"""

import difflib
import logging
import re
from datetime import date, datetime
from typing import Any, Dict, Final, List, Optional, Tuple

import numpy as np
from langchain_openai import OpenAIEmbeddings

from services.aml_builder.web.services.oracle import run_readonly
from services.aml_builder.web.services.production_registry import sql_fingerprint
from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)

# How much each part of a rule counts when two rules are compared (sums to 1). The rest of the SQL
# text, compared as a whole, catches what the parts do not name.
_SIMILARITY_WEIGHTS: Final[Dict[str, float]] = {
    "codes": 0.30, "amounts": 0.20, "period": 0.15, "monitored": 0.15, "direction": 0.05, "text": 0.15,
}
_CODE_LIST_RE: Final = re.compile(r"EXPL_CODE\s*IN\s*\(([^)]*)\)|EXPL_CODE\s*=\s*('[^']*')", re.IGNORECASE)
_AMOUNT_RE: Final = re.compile(r"(?:EQU_TRA_AMT|TRA_AMT|SUM\([^)]*AMT[^)]*\))\s*(>=|>|<=|<|=)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_COUNT_RE: Final = re.compile(r"COUNT\([^)]*\)\s*(>=|>|<=|<|=)\s*(\d+)", re.IGNORECASE)
_DATE_RE: Final = re.compile(r"DATE\s*'(\d{4}-\d{2}-\d{2})'", re.IGNORECASE)
_WINDOW_RE: Final = re.compile(r":bd_minus_(\d+)|(\d+)\s+PRECEDING|INTERVAL\s*'(\d+)'\s*(DAY|MONTH|YEAR)", re.IGNORECASE)
_DIRECTION_RE: Final = re.compile(r"DEB_CRE_IND\s*=\s*'([12])'", re.IGNORECASE)
_GROUP_BY_RE: Final = re.compile(r"GROUP\s+BY\s+(.+?)(?:\bHAVING\b|\bORDER\b|\)|$)", re.IGNORECASE | re.DOTALL)


def rule_parts(sql: str) -> Dict[str, Any]:
    """The parts of a detection rule that decide what it flags, read from its SQL.

    Args:
        sql (str): A scenario's SQL.

    Returns:
        Dict[str, Any]: 'codes' (set), 'amounts' and 'counts' (sorted (operator, number) pairs),
            'period' (dates and window sizes), 'monitored' ('each transaction' / 'per customer' /
            'per account'), 'direction' ('debit' / 'credit' / None).
    """
    codes = set()
    for listed, single in _CODE_LIST_RE.findall(sql or ""):
        codes.update(re.findall(r"'([^']*)'", listed or single))
    grouped = _GROUP_BY_RE.search(sql or "")
    columns = grouped.group(1).upper() if grouped else ""
    monitored = ("per account" if "ACCOUNT_NUMBER" in columns or "SUB_ACCT_CODE" in columns
                 else "per customer" if "CUS_NUM" in columns else "each transaction")
    window = tuple(sorted({"".join(part for part in m if part) for m in _WINDOW_RE.findall(sql or "")}))
    direction = {"1": "debit", "2": "credit"}.get(next(iter(_DIRECTION_RE.findall(sql or "")), ""), None)
    return {
        "codes": codes,
        "amounts": tuple(sorted((op, float(n)) for op, n in _AMOUNT_RE.findall(sql or ""))),
        "counts": tuple(sorted((op, int(n)) for op, n in _COUNT_RE.findall(sql or ""))),
        "period": (tuple(sorted(set(_DATE_RE.findall(sql or "")))), window),
        "monitored": monitored,
        "direction": direction,
    }


def _describe_period(period: Tuple[Tuple[str, ...], Tuple[str, ...]]) -> str:
    """A period as people read it.

    Args:
        period (Tuple[Tuple[str, ...], Tuple[str, ...]]): (dates, window sizes) from :func:`rule_parts`.

    Returns:
        str: e.g. '2019-01-01 to 2019-04-01', 'window 30', or 'no date limit'.
    """
    dates, window = period
    if len(dates) >= 2:
        return f"{dates[0]} to {dates[-1]}"
    if dates:
        return f"from {dates[0]}"
    return f"window {', '.join(window)}" if window else "no date limit"


def _describe_limits(amounts: Tuple[Tuple[str, float], ...], counts: Tuple[Tuple[str, int], ...]) -> str:
    """Thresholds as people read them.

    Args:
        amounts (Tuple[Tuple[str, float], ...]): (operator, value) amount limits.
        counts (Tuple[Tuple[str, int], ...]): (operator, value) count limits.

    Returns:
        str: e.g. 'amount > 50,000; count >= 3', or 'none'.
    """
    parts = [f"amount {op} {value:,.0f}" for op, value in amounts] + [f"count {op} {value}" for op, value in counts]
    return "; ".join(parts) or "none"


def compare_rules(new_sql: str, registered_sql: str) -> Tuple[float, List[str], List[str]]:
    """How similar two detection rules are, and what they share and what differs.

    Args:
        new_sql (str): The rule being generated.
        registered_sql (str): A registered scenario's RAW_SQL.

    Returns:
        Tuple[float, List[str], List[str]]: (similarity 0-100, what is the same, what differs).
    """
    new, old = rule_parts(new_sql), rule_parts(registered_sql)
    same: List[str] = []
    differs: List[str] = []
    score: Dict[str, float] = {}

    union = new["codes"] | old["codes"]
    score["codes"] = len(new["codes"] & old["codes"]) / len(union) if union else 1.0
    if union and new["codes"] == old["codes"]:
        same.append(f"transaction codes ({len(new['codes'])})")
    elif union:
        differs.append(f"transaction codes: {len(new['codes'] & old['codes'])} shared, "
                       f"{len(new['codes'] - old['codes'])} only in the new rule, "
                       f"{len(old['codes'] - new['codes'])} only in the registered one")

    limits_new, limits_old = new["amounts"] + new["counts"], old["amounts"] + old["counts"]
    new_limits = _describe_limits(new["amounts"], new["counts"])
    if limits_new == limits_old:
        score["amounts"] = 1.0
        same.append(f"thresholds: {new_limits}")
    else:
        # The same kind of threshold with another value is closer than a different kind of rule.
        same_kind = bool(limits_new) and {op for op, _ in limits_new} == {op for op, _ in limits_old}
        score["amounts"] = 0.5 if same_kind else 0.0
        differs.append(f"thresholds: {new_limits} vs {_describe_limits(old['amounts'], old['counts'])}")

    score["period"] = 1.0 if new["period"] == old["period"] else 0.0
    (same if score["period"] else differs).append(
        f"period: {_describe_period(new['period'])}" if score["period"] else
        f"period: {_describe_period(new['period'])} vs {_describe_period(old['period'])}")

    score["monitored"] = 1.0 if new["monitored"] == old["monitored"] else 0.0
    (same if score["monitored"] else differs).append(
        f"monitored: {new['monitored']}" if score["monitored"] else f"monitored: {new['monitored']} vs {old['monitored']}")

    score["direction"] = 1.0 if new["direction"] == old["direction"] else 0.0
    if new["direction"] or old["direction"]:
        (same if score["direction"] else differs).append(
            f"direction: {new['direction']}" if score["direction"] else
            f"direction: {new['direction'] or 'any'} vs {old['direction'] or 'any'}")

    score["text"] = difflib.SequenceMatcher(None, sql_fingerprint(new_sql), sql_fingerprint(registered_sql)).ratio()
    total = sum(_SIMILARITY_WEIGHTS[part] * value for part, value in score.items())
    if sql_fingerprint(new_sql) == sql_fingerprint(registered_sql):
        total = 1.0
    return round(100 * total, 1), same, differs


def find_similar_scenarios(raw_sql: str, limit: int = 3, min_similarity: float = 50.0) -> List[Dict[str, Any]]:
    """The registered scenarios most like a rule being generated, most similar first. Read-only.

    Args:
        raw_sql (str): The generated SQL (as shadow-tested).
        limit (int): How many to return.
        min_similarity (float): The lowest similarity (0-100) worth reporting.

    Returns:
        List[Dict[str, Any]]: 'scenario_id', 'scenario_name', 'is_active', 'similarity_pct',
            'identical', 'same', 'differences'. Empty when nothing is close or the registry cannot be read.
    """
    try:
        _, rows = run_readonly("SELECT SCENARIO_ID, SCENARIO_NAME, IS_ACTIVE, RAW_SQL FROM PIO_AML_PRODUCTION_SCENARIOS")
    except Exception as exc:  # the comparison is extra information; the shadow test stands without it
        logger.warning("[REGISTRY_ANALYTICS] Similar-scenario check skipped (%s)", type(exc).__name__)
        return []
    found = []
    for scenario_id, scenario_name, is_active, sql in rows:
        registered = _clob_to_str(sql)
        similarity, same, differs = compare_rules(raw_sql, registered)
        if similarity >= min_similarity:
            found.append({
                "scenario_id": scenario_id,
                "scenario_name": scenario_name,
                "is_active": is_active == 1,
                "similarity_pct": similarity,
                "identical": similarity == 100.0 and not differs,
                "same": same,
                "differences": differs,
            })
    found.sort(key=lambda item: (-item["similarity_pct"], str(item["scenario_id"])))
    return found[:limit]

_ALERT_DATA_NOTE = (
    "Alert tables (PIO_AML_CUSTOMERS / PIO_AML_CUSTOMERS_DET) are populated by the "
    "Standalone Alert Execution Engine (run_alert_engine.py / POST /engine/run-scenarios), "
    "which runs on demand rather than an automatic schedule. A zero or low alert count may "
    "mean no alerts were found, or that the engine has not yet been run for this scenario/date "
    "— it does not by itself indicate the scenario is broken or ineffective."
)


def _clob_to_str(value: Any) -> str:
    """Safely convert an Oracle CLOB (or an already-plain value) to a string.

    Args:
        value (Any): Raw cell value from a cursor fetch — may be a LOB
            object, a plain str, or None.

    Returns:
        str: The text content, or "" if value is None.
    """
    if value is None:
        return ""
    if hasattr(value, "read"):
        return value.read()
    return str(value)


def _parse_date(value: Optional[str], field_name: str) -> Optional[date]:
    """Parse a 'YYYY-MM-DD' date string into a date object.

    Args:
        value (Optional[str]): The date string to parse, or None.
        field_name (str): Field name used in the error message on failure.

    Returns:
        Optional[date]: The parsed date, or None if value was None/empty.

    Raises:
        ValueError: If value is present but not in 'YYYY-MM-DD' format.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be in 'YYYY-MM-DD' format, got '{value}'.") from exc


def _clamp_limit(limit: int, minimum: int = 1, maximum: int = 100) -> int:
    """Clamp a caller-supplied row limit into a sane range.

    Args:
        limit (int): The requested limit.
        minimum (int): Lowest allowed value.
        maximum (int): Highest allowed value.

    Returns:
        int: limit clamped to [minimum, maximum], or minimum if unparseable.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(limit, maximum))


# =============================================================================
# 1. SEMANTIC SEARCH — live per-query embeddings, no persistent cache
# =============================================================================


def search_scenarios_semantic(query: str, limit: int = 5) -> Dict[str, Any]:
    """Search this agent's persisted scenarios by natural-language concept.

    Fetches every scenario's name + detection logic fresh from Oracle, embeds
    them alongside the query, and ranks by cosine similarity. No cache is
    kept between calls — computing embeddings live is simpler and always
    correct at the scale scenarios exist at, with none of the staleness risk
    a persistent cache would carry across multiple worker processes.

    Args:
        query (str): Natural language concept to search for, e.g.
            "structuring under 10k" or "minors with high turnover".
        limit (int): Maximum number of results to return (clamped 1-50).

    Returns:
        Dict[str, Any]: {
            'query': str,
            'total_scenarios_searched': int,
            'results': [
                {scenario_id, scenario_name, similarity_score, is_active,
                 degree_risk_flag, detection_logic_preview}, ...
            ],
        }
    """
    limit = _clamp_limit(limit, minimum=1, maximum=50)

    if not query or not query.strip():
        return {"query": query, "total_scenarios_searched": 0, "results": [], "error": "query is empty."}

    sql = """
    SELECT P.SCENARIO_ID, P.SCENARIO_NAME, P.DETECTION_LOGIC, P.IS_ACTIVE, S.DEGREE_RISK_FLAG
    FROM PIO_AML_PRODUCTION_SCENARIOS P
    LEFT JOIN PIO_AML_SCENARIO S ON P.SCENARIO_ID = S.SCENARIO_CODE
    """
    try:
        _, rows = run_readonly(sql)
    except Exception as exc:
        logger.error("[REGISTRY_ANALYTICS] Failed to fetch scenarios for semantic search: %s", exc)
        return {"query": query, "total_scenarios_searched": 0, "results": [], "error": str(exc)}

    if not rows:
        return {"query": query, "total_scenarios_searched": 0, "results": []}

    scenario_rows = [
        {
            "scenario_id": r[0],
            "scenario_name": r[1] or "",
            "detection_logic": _clob_to_str(r[2]),
            "is_active": bool(r[3]),
            "degree_risk_flag": r[4],
        }
        for r in rows
    ]
    texts = [f"{s['scenario_name']}. {s['detection_logic']}".strip() for s in scenario_rows]

    try:
        embeddings = OpenAIEmbeddings(
            api_key=settings.OPENAI_API_KEY,
            max_retries=5,
            request_timeout=60.0,
        )
        doc_vectors = np.array(embeddings.embed_documents(texts), dtype=np.float32)
        query_vector = np.array(embeddings.embed_query(query), dtype=np.float32)
    except Exception as exc:
        logger.error("[REGISTRY_ANALYTICS] Embedding call failed during semantic search: %s", exc)
        return {
            "query": query,
            "total_scenarios_searched": len(scenario_rows),
            "results": [],
            "error": f"Embedding service unavailable: {exc}",
        }

    doc_norms = np.linalg.norm(doc_vectors, axis=1, keepdims=True)
    doc_norms[doc_norms == 0] = 1.0
    doc_vectors = doc_vectors / doc_norms

    query_norm = np.linalg.norm(query_vector)
    if query_norm > 0:
        query_vector = query_vector / query_norm

    scores = np.dot(doc_vectors, query_vector)
    ranked_indices = np.argsort(scores)[::-1][:limit]

    results = []
    for idx in ranked_indices:
        s = scenario_rows[idx]
        results.append(
            {
                "scenario_id": s["scenario_id"],
                "scenario_name": s["scenario_name"],
                "similarity_score": round(float(scores[idx]), 4),
                "is_active": s["is_active"],
                "degree_risk_flag": s["degree_risk_flag"],
                "detection_logic_preview": s["detection_logic"][:200],
            }
        )

    return {
        "query": query,
        "total_scenarios_searched": len(scenario_rows),
        "results": results,
    }


# =============================================================================
# 2. REGISTRY STATISTICS — deterministic aggregate KPIs
# =============================================================================


def get_registry_statistics() -> Dict[str, Any]:
    """Compute inventory and governance KPIs across the scenario registry.

    Each aggregation is queried independently and defensively — a failure
    in one (e.g. PIO_AML_CUSTOMERS not existing yet) never blocks the others.

    Returns:
        Dict[str, Any]: {
            'total_scenarios': int,
            'active_count': int,
            'inactive_count': int,
            'risk_degree_breakdown': [{risk_degree, count}, ...],
            'category_breakdown': [{category_code, category_name, count}, ...],
            'lifetime_total_alerts': Optional[int],
            'alert_data_note': str,
        }
    """
    stats: Dict[str, Any] = {
        "total_scenarios": 0,
        "active_count": 0,
        "inactive_count": 0,
        "risk_degree_breakdown": [],
        "category_breakdown": [],
        "lifetime_total_alerts": None,
    }

    try:
        _, rows = run_readonly(
            "SELECT IS_ACTIVE, COUNT(*) FROM PIO_AML_PRODUCTION_SCENARIOS GROUP BY IS_ACTIVE"
        )
        for is_active, count in rows:
            count = int(count)
            stats["total_scenarios"] += count
            if int(is_active or 0) == 1:
                stats["active_count"] = count
            else:
                stats["inactive_count"] = count
    except Exception as exc:
        logger.error("[REGISTRY_ANALYTICS] Failed to compute active/inactive counts: %s", exc)

    try:
        _, rows = run_readonly(
            """
            SELECT S.DEGREE_RISK_FLAG, COUNT(*)
            FROM PIO_AML_PRODUCTION_SCENARIOS P
            LEFT JOIN PIO_AML_SCENARIO S ON P.SCENARIO_ID = S.SCENARIO_CODE
            GROUP BY S.DEGREE_RISK_FLAG
            """
        )
        stats["risk_degree_breakdown"] = [
            {"risk_degree": risk or "UNKNOWN", "count": int(count)} for risk, count in rows
        ]
    except Exception as exc:
        logger.error("[REGISTRY_ANALYTICS] Failed to compute risk degree breakdown: %s", exc)

    try:
        _, rows = run_readonly(
            """
            SELECT S.CATEGORY_CODE, C.CATEGORY_ENG_NAME, COUNT(*)
            FROM PIO_AML_PRODUCTION_SCENARIOS P
            LEFT JOIN PIO_AML_SCENARIO S ON P.SCENARIO_ID = S.SCENARIO_CODE
            LEFT JOIN PIO_AML_SCENARIO_CATEGORY C ON S.CATEGORY_CODE = C.CATEGORY_CODE
            GROUP BY S.CATEGORY_CODE, C.CATEGORY_ENG_NAME
            """
        )
        stats["category_breakdown"] = [
            {
                "category_code": code or "UNKNOWN",
                "category_name": name or (code or "Unknown"),
                "count": int(count),
            }
            for code, name, count in rows
        ]
    except Exception as exc:
        logger.error("[REGISTRY_ANALYTICS] Failed to compute category breakdown: %s", exc)

    try:
        _, rows = run_readonly(
            """
            SELECT COUNT(*) FROM PIO_AML_CUSTOMERS
            WHERE AML_SCENARIO_CODE IN (SELECT SCENARIO_ID FROM PIO_AML_PRODUCTION_SCENARIOS)
            """
        )
        stats["lifetime_total_alerts"] = int(rows[0][0]) if rows else 0
    except Exception as exc:
        logger.warning(
            "[REGISTRY_ANALYTICS] Could not compute lifetime alert total (table may not exist yet): %s",
            exc,
        )

    return stats


# =============================================================================
# 3. ALERT TELEMETRY — live PIO_AML_CUSTOMERS / PIO_AML_CUSTOMERS_DET data
# =============================================================================


def get_alert_telemetry(
    scenario_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    customer_number: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """Query live alert telemetry from PIO_AML_CUSTOMERS / PIO_AML_CUSTOMERS_DET.

    Args:
        scenario_id (Optional[str]): Restrict to alerts fired by this scenario.
        date_from (Optional[str]): Inclusive lower bound, 'YYYY-MM-DD'.
        date_to (Optional[str]): Inclusive upper bound, 'YYYY-MM-DD'.
        customer_number (Optional[str]): If given, lists individual alerts for
            this customer instead of the aggregate breakdowns.
        limit (int): Max rows in each breakdown list (clamped 1-100).

    Returns:
        Dict[str, Any]: {
            'filters_applied': {...},
            'total_alerts_matched': int,
            'header_alert_counts_by_scenario': [{scenario_id, scenario_name,
                header_alert_count, transaction_detail_count}, ...],
            'alert_volume_by_date': [{day_date, alert_count}, ...],
            'customer_alerts': Optional[list] — populated only when customer_number is given,
            'data_available': bool,
            'note': str,
        }

    Raises:
        ValueError: If date_from/date_to are present but not 'YYYY-MM-DD'.
    """
    limit = _clamp_limit(limit, minimum=1, maximum=100)
    parsed_from = _parse_date(date_from, "date_from")
    parsed_to = _parse_date(date_to, "date_to")

    filters_applied = {
        "scenario_id": scenario_id,
        "date_from": date_from,
        "date_to": date_to,
        "customer_number": customer_number,
    }

    where_parts: List[str] = []
    params: Dict[str, Any] = {}
    if scenario_id:
        where_parts.append("C.AML_SCENARIO_CODE = :scenario_id")
        params["scenario_id"] = scenario_id
    if parsed_from:
        where_parts.append("C.DAY_DATE >= :date_from")
        params["date_from"] = parsed_from
    if parsed_to:
        where_parts.append("C.DAY_DATE <= :date_to")
        params["date_to"] = parsed_to
    if customer_number:
        where_parts.append("C.CUS_NUM = :customer_number")
        params["customer_number"] = customer_number

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    result: Dict[str, Any] = {
        "filters_applied": filters_applied,
        "total_alerts_matched": 0,
        "header_alert_counts_by_scenario": [],
        "alert_volume_by_date": [],
        "customer_alerts": None,
        "data_available": False,
    }

    try:
        _, rows = run_readonly(f"SELECT COUNT(*) FROM PIO_AML_CUSTOMERS C {where_clause}", params)
        result["total_alerts_matched"] = int(rows[0][0]) if rows else 0
        result["data_available"] = True
    except Exception as exc:
        logger.warning(
            "[REGISTRY_ANALYTICS] Alert telemetry query failed (table may not exist yet): %s", exc
        )
        return result

    try:
        _, rows = run_readonly(
            f"""
            SELECT P.SCENARIO_ID, P.SCENARIO_NAME, COUNT(C.SEQ) AS HEADER_COUNT
            FROM PIO_AML_CUSTOMERS C
            JOIN PIO_AML_PRODUCTION_SCENARIOS P ON C.AML_SCENARIO_CODE = P.SCENARIO_ID
            {where_clause}
            GROUP BY P.SCENARIO_ID, P.SCENARIO_NAME
            ORDER BY HEADER_COUNT DESC
            FETCH FIRST :row_limit ROWS ONLY
            """,
            {**params, "row_limit": limit},
        )
        scenario_breakdown = []
        for sid, sname, header_count in rows:
            detail_count = 0
            try:
                _, det_rows = run_readonly(
                    "SELECT COUNT(*) FROM PIO_AML_CUSTOMERS_DET WHERE AML_SCENARIO_CODE = :sid",
                    {"sid": sid},
                )
                detail_count = int(det_rows[0][0]) if det_rows else 0
            except Exception:
                pass
            scenario_breakdown.append(
                {
                    "scenario_id": sid,
                    "scenario_name": sname,
                    "header_alert_count": int(header_count),
                    "transaction_detail_count": detail_count,
                }
            )
        result["header_alert_counts_by_scenario"] = scenario_breakdown
    except Exception as exc:
        logger.warning("[REGISTRY_ANALYTICS] Per-scenario alert breakdown failed: %s", exc)

    try:
        _, rows = run_readonly(
            f"""
            SELECT DAY_DATE, COUNT(*) FROM PIO_AML_CUSTOMERS C
            {where_clause}
            GROUP BY DAY_DATE
            ORDER BY DAY_DATE DESC
            FETCH FIRST :row_limit ROWS ONLY
            """,
            {**params, "row_limit": limit},
        )
        result["alert_volume_by_date"] = [
            {"day_date": d.isoformat() if hasattr(d, "isoformat") else str(d), "alert_count": int(c)}
            for d, c in rows
        ]
    except Exception as exc:
        logger.warning("[REGISTRY_ANALYTICS] Alert volume by date query failed: %s", exc)

    if customer_number:
        try:
            _, rows = run_readonly(
                f"""
                SELECT C.DAY_DATE, C.AML_SCENARIO_CODE, P.SCENARIO_NAME, C.CUS_NAME,
                       C.RISK_DEGREE, C.CURRENT_STATUS
                FROM PIO_AML_CUSTOMERS C
                LEFT JOIN PIO_AML_PRODUCTION_SCENARIOS P ON C.AML_SCENARIO_CODE = P.SCENARIO_ID
                {where_clause}
                ORDER BY C.DAY_DATE DESC
                FETCH FIRST :row_limit ROWS ONLY
                """,
                {**params, "row_limit": limit},
            )
            result["customer_alerts"] = [
                {
                    "day_date": d.isoformat() if hasattr(d, "isoformat") else str(d),
                    "scenario_id": sid,
                    "scenario_name": sname,
                    "customer_name": cname,
                    "risk_degree": risk,
                    "status": status,
                }
                for d, sid, sname, cname, risk, status in rows
            ]
        except Exception as exc:
            logger.warning("[REGISTRY_ANALYTICS] Customer-specific alert lookup failed: %s", exc)
            result["customer_alerts"] = []

    return result


# =============================================================================
# 4. SCENARIO DETAIL — full record for one scenario_id
# =============================================================================


def get_scenario_full_details(scenario_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the full record for one scenario, including governance metadata.

    Args:
        scenario_id (str): The SCENARIO_ID/SCENARIO_CODE to look up (e.g. 'PRD_50FC063C').

    Returns:
        Optional[Dict[str, Any]]: The full scenario record, or None if not found.
    """
    if not scenario_id or not scenario_id.strip():
        return None

    sql = """
    SELECT
        P.SCENARIO_ID, P.SCENARIO_NAME, P.SCENARIO_TYPE, P.DETECTION_LOGIC, P.RAW_SQL,
        P.TIME_WINDOW_DAYS, P.CREATED_BY, P.CREATED_AT, P.IS_ACTIVE,
        S.DEGREE_RISK_FLAG, S.CATEGORY_CODE, C.CATEGORY_ENG_NAME, S.VIOLATION_LEVEL
    FROM PIO_AML_PRODUCTION_SCENARIOS P
    LEFT JOIN PIO_AML_SCENARIO S ON P.SCENARIO_ID = S.SCENARIO_CODE
    LEFT JOIN PIO_AML_SCENARIO_CATEGORY C ON S.CATEGORY_CODE = C.CATEGORY_CODE
    WHERE P.SCENARIO_ID = :scenario_id
    """
    try:
        _, rows = run_readonly(sql, {"scenario_id": scenario_id})
    except Exception as exc:
        logger.error("[REGISTRY_ANALYTICS] Failed to fetch scenario detail for %s: %s", scenario_id, exc)
        return None

    if not rows:
        return None

    r = rows[0]
    detail: Dict[str, Any] = {
        "scenario_id": r[0],
        "scenario_name": r[1],
        "scenario_type": r[2],
        "detection_logic": _clob_to_str(r[3]),
        "raw_sql": _clob_to_str(r[4]),
        "time_window_days": r[5],
        "created_by": r[6],
        "created_at": r[7].isoformat() if hasattr(r[7], "isoformat") else (str(r[7]) if r[7] else None),
        "is_active": bool(r[8]),
        "degree_risk_flag": r[9],
        "category_code": r[10],
        "category_name": r[11] or r[10],
        "violation_level": r[12],
        "total_alerts_fired": None,
    }

    try:
        _, alert_rows = run_readonly(
            "SELECT COUNT(*) FROM PIO_AML_CUSTOMERS WHERE AML_SCENARIO_CODE = :sid",
            {"sid": scenario_id},
        )
        detail["total_alerts_fired"] = int(alert_rows[0][0]) if alert_rows else 0
    except Exception as exc:
        logger.warning(
            "[REGISTRY_ANALYTICS] Could not fetch alert count for %s (table may not exist yet): %s",
            scenario_id,
            exc,
        )

    return detail
