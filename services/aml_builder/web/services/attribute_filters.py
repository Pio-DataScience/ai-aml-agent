"""
Customer, account and transaction attribute filters resolved from the DWH lookup tables.

Next to the explanation-code filter (which selects transaction types), a scenario can restrict
WHO or WHICH accounts it covers: "dormant accounts", "married customers", "corporate customers",
"debit transactions" ... Each attribute below is a source column with a lookup table of codes and
descriptions (or, for individual/corporate, a fixed list). The flow mirrors explanation codes:

1. ``resolve_attribute_filters`` (intent time): the request is matched against the live country /
   institution catalogs; a fast LLM picks the codes, and every code is validated against the
   catalog. The result is stored in the intent as ``attribute_filters``.
2. ``strip_attribute_phrases`` (shadow test): the resolved phrases are removed from the text the
   PioTech SQL service reads, so it neither refuses them nor filters them a second time.
3. ``apply_attribute_filters`` (shadow test): the filters are added to the generated SQL as
   semi-joins on the latest snapshot of each account or customer, so the saved rule and the alert
   engine run with them.

Coverage (OPENBI2, country 400 / institution 1, measured 2026-10-18): about 79% of the transacting
accounts are in PIO_ACCOUNTS, but under 1% of the transacting customers are in PIO_CUSTOMERS, so a
customer attribute currently matches only the customers that table holds.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Final, FrozenSet, List, Literal, Optional, Tuple

from services.aml_builder.web.services.llm_client import build_llm, safe_parse_json
from services.aml_builder.web.services.oracle import run_shadow_readonly
from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)

Entity = Literal["CUSTOMER", "ACCOUNT", "TRANSACTION"]

ENTITY_TABLES: Final[Dict[str, Tuple[str, str]]] = {
    "CUSTOMER": ("BI_DWH.PIO_CUSTOMERS", "CUS_NUM"),
    "ACCOUNT": ("BI_DWH.PIO_ACCOUNTS", "ACCOUNT_NUMBER"),
}


@dataclass(frozen=True)
class Attribute:
    """One filterable attribute.

    Attributes:
        key (str): Stable identifier stored in the intent.
        label (str): Business name shown to the user.
        entity (Entity): Where the column lives: the customer, the account, or the transaction row.
        column (str): The source column.
        lookup_table (Optional[str]): BI_DWH lookup table, or None for a fixed list.
        code_column (Optional[str]): Code column of the lookup table.
        desc_column (Optional[str]): English description column of the lookup table.
        desc_nat_column (Optional[str]): Native-language description column of the lookup table.
        fixed_values (Dict[str, str]): Codes and descriptions of a fixed (hard-coded) list.
        keywords (Tuple[str, ...]): Words that suggest the attribute is mentioned.
    """

    key: str
    label: str
    entity: Entity
    column: str
    lookup_table: Optional[str] = None
    code_column: Optional[str] = None
    desc_column: Optional[str] = None
    desc_nat_column: Optional[str] = None
    fixed_values: Dict[str, str] = field(default_factory=dict)
    keywords: Tuple[str, ...] = ()


ATTRIBUTES: Final[Tuple[Attribute, ...]] = (
    Attribute("account_status", "Account status", "ACCOUNT", "STA_CODE", "PIO_ACCOUNT_STATUS", "STATUS_CODE",
              "DES_ENG", "DES_NAT_LAN",
              keywords=("status", "dormant", "closed", "open", "blocked", "black", "stopped", "suspended",
                        "deleted", "inactive", "seized")),
    Attribute("account_nature", "Account nature", "ACCOUNT", "ACCT_NAT", "PIO_ACCOUNT_NATURE", "NATURE_CODE",
              "DES_ENG", "DES_NAT_LAN",
              keywords=("nature", "business account", "personal", "savings", "saving", "trust", "trading")),
    Attribute("account_product", "Account product", "ACCOUNT", "PRODUCT_CODE", "PIO_PRODUCTS", "PRODUCT_CODE",
              "PRODUCT_DES_ENG", "PRODUCT_DES_NAT",
              keywords=("product", "overdraft", "murabaha", "time deposit", "fx account", "visa", "placement")),
    Attribute("account_type", "Account type", "ACCOUNT", "ACCT_TYPE", "PIO_ACCOUNT_TYPE", "ACCT_TYPE",
              "DES_ENG", "DES_NAT_LAN",
              keywords=("account type", "resident", "non-resident", "nonresident", "non resident")),
    Attribute("individual_corporate", "Individual / corporate", "CUSTOMER", "INDV_CORP_IND",
              fixed_values={"1": "Individual", "I": "Individual", "2": "Corporate", "C": "Corporate"},
              keywords=("individual", "corporate", "company", "companies", "person", "persons", "entity",
                        "entities", "business customer", "legal")),
    Attribute("profession", "Profession", "CUSTOMER", "PROFESSION", "PIO_PROFFISSION", "PROF_ID",
              "PROF_DESC", "PROF_DESC_NAT", keywords=("profession", "job", "works as")),
    Attribute("occupation", "Occupation", "CUSTOMER", "CUST_OCCUPATION", "PIO_CUSTOMER_OCCUPATION", "CUST_OCCUP",
              "DES_ENG", "DES_NAT_LAN", keywords=("occupation", "employed", "self-employed", "unemployed")),
    Attribute("marital_status", "Marital status", "CUSTOMER", "MARITAL_ST", "PIO_MARITAL_STATUS", "ST_CODE",
              "DESC_ENG", "DESC_NAT", keywords=("marital", "married", "single", "divorced", "widowed", "unmarried")),
    Attribute("customer_class", "Customer classification", "CUSTOMER", "CUS_CLASS", "PIO_CUSTOMER_CLASSIFICATIONS",
              "CUS_CLASS", "DES_ENG", "DES_NAT_LAN", keywords=("class", "classification", "sme", "segment")),
    Attribute("city", "City", "CUSTOMER", "CITY_LOC_CODE", "PIO_CITIES", "CITY_CODE", "DES_ENG", "DES_NAT_LAN",
              keywords=("city", "cities", "living in", "located in", "based in", "resident of")),
    Attribute("debit_credit", "Debit / credit", "TRANSACTION", "DEB_CRE_IND", "PIO_DEP_CRED_LOOKUP",
              "DEBIT_CREDIT_IND", "DES_ENG", "DES_NAT_LAN", keywords=("debit", "credit", "debited", "credited")),
)
BY_KEY: Final[Dict[str, Attribute]] = {a.key: a for a in ATTRIBUTES}

_CATALOG_TTL_SECONDS: Final = 600
_catalog_cache: Dict[str, Tuple[float, Dict[str, str]]] = {}
_STOPWORDS: Final[FrozenSet[str]] = frozenset(
    {"other", "others", "unknown", "account", "accounts", "customer", "customers", "officer", "staff", "employee",
     "status", "code", "meaning", "name", "type", "with", "from", "into", "than", "more", "less", "total"}
)


# ─────────────────────────────── catalogs ────────────────────────────────


def load_catalog(attribute: Attribute) -> Dict[str, str]:
    """Codes and English descriptions of an attribute for the configured country and institution.

    Args:
        attribute (Attribute): The attribute.

    Returns:
        Dict[str, str]: {code: description}; empty when the lookup cannot be read.
    """
    if attribute.fixed_values:
        return dict(attribute.fixed_values)
    cached = _catalog_cache.get(attribute.key)
    if cached and time.monotonic() - cached[0] < _CATALOG_TTL_SECONDS:
        return dict(cached[1])
    sql = (
        f"SELECT TO_CHAR({attribute.code_column}), {attribute.desc_column} FROM BI_DWH.{attribute.lookup_table} "
        f"WHERE COUNTRY_CODE = :country AND INST_CODE = :inst AND {attribute.desc_column} IS NOT NULL"
    )
    try:
        _, rows = run_shadow_readonly(sql, {"country": settings.AML_COUNTRY_CODE, "inst": settings.AML_INST_CODE})
    except Exception as exc:  # noqa: BLE001 - a missing lookup must not stop intent parsing
        logger.warning("[ATTR_FILTER] Could not read %s: %s", attribute.lookup_table, exc)
        return {}
    catalog = {str(code).strip(): str(desc).strip() for code, desc in rows if code is not None}
    _catalog_cache[attribute.key] = (time.monotonic(), catalog)
    return dict(catalog)


def clear_catalog_cache() -> None:
    """Forget cached catalogs (e.g. after lookup rows were added)."""
    _catalog_cache.clear()


def _tokens(text: str) -> FrozenSet[str]:
    """Lower-case words of 4+ letters in a text, without generic words.

    Args:
        text (str): Any text.

    Returns:
        FrozenSet[str]: The words.
    """
    return frozenset(w for w in re.findall(r"[a-z][a-z\-]{3,}", text.lower()) if w not in _STOPWORDS)


def candidate_attributes(text: str) -> List[Attribute]:
    """Attributes the text may restrict: a keyword of the attribute, or a word of one of its descriptions.

    Args:
        text (str): The request and intent text.

    Returns:
        List[Attribute]: The attributes worth sending to the resolver, in registry order.
    """
    lowered = f" {text.lower()} "
    words = _tokens(text)
    found: List[Attribute] = []
    for attribute in ATTRIBUTES:
        if any(re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", lowered) for k in attribute.keywords):
            found.append(attribute)
            continue
        descriptions = " ".join(load_catalog(attribute).values()) if attribute.entity != "TRANSACTION" else ""
        if words & _tokens(descriptions):
            found.append(attribute)
    return found


# ─────────────────────────────── resolution ────────────────────────────────

_RESOLVER_PROMPT: Final = """You map an AML scenario request to attribute filters on bank lookup tables.

REQUEST AND SCENARIO TEXT:
{text}

ATTRIBUTES (key | name | applies to) with their codes ("code = description"):
{catalogs}

Return ONLY a JSON object: {{"filters": [{{"attribute": "<key>", "operator": "IN" | "NOT IN", "codes": ["<code>", ...], "phrase": "<words>"}}]}}

Rules:
- Add a filter only when the text explicitly restricts WHICH customers, accounts or transactions are covered
  by one of these attributes (e.g. "dormant accounts", "married customers", "corporate customers", "debit transactions").
- Choose every code whose description means the requested value, including spelling and case variants
  (e.g. all codes described as married). Use only codes listed above.
- "NOT IN" when the text excludes the value ("non-dormant", "excluding closed accounts", "other than individuals").
- "phrase": the minimal words of the request that state the restriction, copied exactly (e.g. "dormant", "corporate").
- Transaction types and channels (cash, deposit, withdrawal, transfer, ATM, cheque, card, POS, wire) are NOT
  attributes: never map them to a product, a nature or debit/credit. Use debit_credit only when the words
  debit or credit are used.
- Dormancy or inactivity stated over a period ("no activity for 6 months", "inactive for a year then") is behaviour,
  not a status: no filter for it.
- When nothing qualifies, return {{"filters": []}}.
"""


def _catalog_lines(attributes: List[Attribute], catalogs: Dict[str, Dict[str, str]]) -> str:
    """Render the candidate catalogs for the resolver prompt.

    Args:
        attributes (List[Attribute]): Candidate attributes.
        catalogs (Dict[str, Dict[str, str]]): {attribute key: {code: description}}.

    Returns:
        str: One block per attribute.
    """
    blocks = []
    for attribute in attributes:
        entries = "; ".join(f"{code} = {desc}" for code, desc in sorted(catalogs[attribute.key].items()))
        blocks.append(f"{attribute.key} | {attribute.label} | {attribute.entity.lower()}\n  {entries}")
    return "\n".join(blocks)


def validate_filters(raw: Any) -> List[Dict[str, Any]]:
    """Keep only well-formed filters whose codes exist in the attribute's catalog.

    Args:
        raw (Any): Filters from the resolver or from an intent.

    Returns:
        List[Dict[str, Any]]: Normalized filters: attribute, label, entity, column, operator, codes,
            descriptions, phrase. One filter per attribute and operator; unknown codes are dropped.
    """
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or item.get("attribute") not in BY_KEY:
            continue
        attribute = BY_KEY[item["attribute"]]
        operator = "NOT IN" if str(item.get("operator", "IN")).upper().replace("_", " ") == "NOT IN" else "IN"
        catalog = load_catalog(attribute)
        codes = [str(c).strip() for c in item.get("codes") or [] if str(c).strip() in catalog]
        if not codes:
            continue
        entry = merged.setdefault((attribute.key, operator), {
            "attribute": attribute.key,
            "label": attribute.label,
            "entity": attribute.entity,
            "column": attribute.column,
            "operator": operator,
            "codes": [],
            "descriptions": [],
            "phrase": str(item.get("phrase") or "").strip() or None,
        })
        for code in codes:
            if code not in entry["codes"]:
                entry["codes"].append(code)
                entry["descriptions"].append(catalog[code])
    return list(merged.values())


def resolve_attribute_filters(*texts: str) -> List[Dict[str, Any]]:
    """Resolve the attribute restrictions stated in a request.

    Args:
        *texts (str): The user's request, then the intent's detection logic and condition phrases.

    Returns:
        List[Dict[str, Any]]: Validated filters (see validate_filters); empty when none is stated or
            the resolver fails.
    """
    text = "\n".join(t for t in texts if t and t.strip())
    if not text:
        return []
    attributes = candidate_attributes(text)
    catalogs = {a.key: load_catalog(a) for a in attributes}
    attributes = [a for a in attributes if catalogs[a.key]]
    if not attributes:
        return []
    prompt = _RESOLVER_PROMPT.format(text=text, catalogs=_catalog_lines(attributes, catalogs))
    try:
        response = build_llm(fast=True).invoke(prompt)
        parsed = safe_parse_json(str(response.content))
    except Exception as exc:  # noqa: BLE001 - an LLM failure means no attribute filter, not a failed intent
        logger.error("[ATTR_FILTER] Resolver failed: %s", exc)
        return []
    filters = validate_filters(parsed.get("filters") if isinstance(parsed, dict) else None)
    logger.info("[ATTR_FILTER] Resolved %s", json.dumps(
        [{k: f[k] for k in ("attribute", "operator", "codes", "phrase")} for f in filters], ensure_ascii=False))
    return filters


# ─────────────────────────────── SQL ────────────────────────────────

_TOP_LEVEL_KEYWORDS: Final = re.compile(r"\b(SELECT|FROM|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|UNION|JOIN)\b", re.IGNORECASE)
_TRANSACTIONS_FROM_RE: Final = re.compile(
    r"^FROM\s+(?:BI_DWH\.)?PIO_TRANSACTIONS(?:\s+(?:AS\s+)?(?!WHERE\b|GROUP\b|ORDER\b|HAVING\b)([A-Za-z_]\w*))?\s*$",
    re.IGNORECASE,
)


def top_level_clauses(sql: str) -> Optional[Dict[str, Tuple[int, int]]]:
    """Find where each top-level clause of a single SELECT starts and ends.

    Args:
        sql (str): A SQL statement.

    Returns:
        Optional[Dict[str, Tuple[int, int]]]: {'SELECT'|'FROM'|'WHERE'|'GROUP BY'|'HAVING'|'ORDER BY':
            (start of the keyword, end of the clause)}, or None for a statement this cannot split
            safely (WITH, UNION, a JOIN or a comma-joined FROM at the top level).
    """
    if not re.match(r"\s*SELECT\b", sql, re.IGNORECASE):
        return None
    depth, in_quote, starts = 0, False, []
    for match in re.finditer(r"'|\(|\)|" + _TOP_LEVEL_KEYWORDS.pattern, sql, re.IGNORECASE):
        token = match.group(0)
        if token == "'":
            in_quote = not in_quote
        elif in_quote:
            continue
        elif token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            starts.append((re.sub(r"\s+", " ", token.upper()), match.start()))
    keys = [k for k, _ in starts]
    if not keys or keys[0] != "SELECT" or keys.count("SELECT") > 1 or {"UNION", "JOIN"} & set(keys):
        return None
    clauses = {key: (start, starts[i + 1][1] if i + 1 < len(starts) else len(sql)) for i, (key, start) in enumerate(starts)}
    if "FROM" not in clauses:
        return None
    from_start, from_end = clauses["FROM"]
    if "," in strip_parens(sql[from_start + 4:from_end]):
        return None
    return clauses


def strip_parens(text: str) -> str:
    """Remove every parenthesised part of a SQL fragment.

    Args:
        text (str): SQL fragment.

    Returns:
        str: The fragment with nested '(...)' removed.
    """
    previous = None
    while previous != text:
        previous, text = text, re.sub(r"\([^()]*\)", "", text)
    return text


def _in_list(codes: List[str]) -> str:
    """Render codes as a quoted SQL list.

    Args:
        codes (List[str]): Codes.

    Returns:
        str: e.g. "'3', 'DMT'".
    """
    return ", ".join("'" + code.replace("'", "''") + "'" for code in codes)


def entity_subquery(entity: str, filters: List[Dict[str, Any]], key: Optional[str] = None) -> str:
    """Keys of the entities whose LATEST snapshot matches every filter.

    Args:
        entity (str): 'CUSTOMER' or 'ACCOUNT'.
        filters (List[Dict[str, Any]]): Filters on that entity.
        key (Optional[str]): Column to return; the entity key by default (CUS_NUM for the holder of an account).

    Returns:
        str: A SELECT returning the key, scoped to the configured country and institution.
    """
    table, entity_key = ENTITY_TABLES[entity]
    country, inst = int(settings.AML_COUNTRY_CODE), int(settings.AML_INST_CODE)
    conditions = " AND ".join(f"e.{f['column']} {f['operator']} ({_in_list(f['codes'])})" for f in filters)
    return (
        f"SELECT e.{key or entity_key} FROM {table} e "
        f"WHERE e.COUNTRY_CODE = {country} AND e.INST_CODE = {inst} AND {conditions} "
        f"AND (e.{entity_key}, e.DAY_DATE) IN (SELECT {entity_key}, MAX(DAY_DATE) FROM {table} "
        f"WHERE COUNTRY_CODE = {country} AND INST_CODE = {inst} GROUP BY {entity_key})"
    )


def _row_predicates(alias: str, filters: List[Dict[str, Any]]) -> List[str]:
    """Predicates on one transaction row.

    Args:
        alias (str): Alias (or name) of PIO_TRANSACTIONS in the statement.
        filters (List[Dict[str, Any]]): Validated filters.

    Returns:
        List[str]: One predicate per entity group.
    """
    predicates = []
    for entity, key in (("CUSTOMER", "CUS_NUM"), ("ACCOUNT", "ACCOUNT_NUMBER")):
        group = [f for f in filters if f["entity"] == entity]
        if group:
            predicates.append(f"{alias}.{key} IN ({entity_subquery(entity, group)})")
    for f in filters:
        if f["entity"] == "TRANSACTION":
            predicates.append(f"{alias}.{f['column']} {f['operator']} ({_in_list(f['codes'])})")
    return predicates


def apply_attribute_filters(sql: str, filters: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Add attribute filters to a scenario's SQL.

    A single-table SELECT on PIO_TRANSACTIONS gets the filters in its WHERE clause, so aggregates count
    only the matching transactions. Any other statement is wrapped and filtered on its CUS_NUM (an
    account filter then keeps customers who hold such an account); a transaction attribute cannot be
    applied to such a statement and is reported as skipped.

    Args:
        sql (str): The scenario SQL.
        filters (List[Dict[str, Any]]): Validated filters.

    Returns:
        Tuple[str, List[Dict[str, Any]]]: The SQL, and per filter {'attribute', 'label', 'operator',
            'descriptions', 'applied': 'where' | 'result' | 'skipped'}.
    """
    filters = validate_filters(filters)
    if not filters:
        return sql, []

    def report(f: Dict[str, Any], how: str) -> Dict[str, Any]:
        return {"attribute": f["attribute"], "label": f["label"], "operator": f["operator"],
                "descriptions": f["descriptions"], "applied": how}

    clauses = top_level_clauses(sql)
    from_match = None
    if clauses:
        start, end = clauses["FROM"]
        from_match = _TRANSACTIONS_FROM_RE.match(sql[start:end].strip())
    if clauses and from_match:
        alias = from_match.group(1) or "PIO_TRANSACTIONS"
        condition = "\n  AND ".join(_row_predicates(alias, filters))
        if "WHERE" in clauses:
            cut = clauses["WHERE"][1]
            out = f"{sql[:cut].rstrip()}\n  AND {condition}\n{sql[cut:].lstrip()}".rstrip()
        else:
            cut = clauses["FROM"][1]
            out = f"{sql[:cut].rstrip()}\nWHERE {condition}\n{sql[cut:].lstrip()}".rstrip()
        return out, [report(f, "where") for f in filters]

    predicates, applied = [], []
    for entity in ("CUSTOMER", "ACCOUNT"):
        group = [f for f in filters if f["entity"] == entity]
        if group:
            predicates.append(f"r.CUS_NUM IN ({entity_subquery(entity, group, key='CUS_NUM')})")
            applied += [report(f, "result") for f in group]
    applied += [report(f, "skipped") for f in filters if f["entity"] == "TRANSACTION"]
    if not predicates:
        return sql, applied
    return f"SELECT r.* FROM (\n{sql}\n) r\nWHERE " + "\n  AND ".join(predicates), applied


# ─────────────────────────────── text sent to PioTech ────────────────────────────────

_INDV_CORP_SEGMENTS: Final[FrozenSet[str]] = frozenset({"INDIVIDUAL", "INDIVIDUALS", "CORPORATE", "CORPORATES"})
# Words of a phrase that name the subject, not the restriction ("dormant ACCOUNTS").
_SUBJECT_WORDS: Final[FrozenSet[str]] = frozenset(
    {"customer", "customers", "client", "clients", "account", "accounts", "transaction", "transactions", "holder",
     "holders", "the", "with", "only", "all", "any", "type", "types", "status", "who", "are", "is", "of", "and"}
)
# Structured intent fields the SQL service needs verbatim.
_PROTECTED_FIELDS: Final[FrozenSet[str]] = frozenset(
    {"transaction_type", "transaction_types", "explanation_codes", "explanation_codes_by_type", "thresholds",
     "time_window", "baseline_window", "anchor_date", "aggregation", "scenario_type"}
)
# A restriction word followed by one of these is a transaction type ("credit card"), not the restriction.
_TYPE_NOUNS: Final = r"(?:cards?|transfers?|facilit(?:y|ies)|limits?|notes?|lines?|advances?)"


def _core_words(filters: List[Dict[str, Any]]) -> List[str]:
    """The words that state the restrictions: each phrase without its subject words.

    Args:
        filters (List[Dict[str, Any]]): Validated filters.

    Returns:
        List[str]: Lower-case words, longest first (e.g. ['dormant'], ['corporate']).
    """
    words = {
        w for f in filters for w in re.findall(r"[a-z][a-z\-]+", str(f.get("phrase") or "").lower())
        if w not in _SUBJECT_WORDS and len(w) > 2
    }
    return sorted(words, key=len, reverse=True)


def _mentions(text: Any, words: List[str]) -> bool:
    """Whether a text uses one of the restriction words (not as part of a transaction type).

    Args:
        text (Any): Any value.
        words (List[str]): Restriction words.

    Returns:
        bool: True when a word occurs on its own.
    """
    value = str(text or "")
    return any(
        re.search(rf"(?<![A-Za-z_]){re.escape(w)}(?![A-Za-z_])(?!\s+{_TYPE_NOUNS}\b)", value, re.IGNORECASE)
        for w in words
    )


def _scrub(value: Any, words: List[str]) -> Any:
    """Remove the restriction words from every string in a value (dicts and lists included).

    Args:
        value (Any): A JSON-like value.
        words (List[str]): Restriction words.

    Returns:
        Any: The value without those words; dict entries whose key states a restriction are dropped.
    """
    if isinstance(value, str):
        for w in words:
            value = re.sub(rf"(?<![A-Za-z_]){re.escape(w)}(?![A-Za-z_])(?!\s+{_TYPE_NOUNS}\b)", "", value,
                           flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", value).replace(" ,", ",").replace(" .", ".").strip()
    if isinstance(value, list):
        return [_scrub(v, words) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, words) for k, v in value.items() if not _mentions(k, words)}
    return value


def _restates(threshold: Dict[str, Any], filters: List[Dict[str, Any]], words: List[str]) -> bool:
    """Whether a threshold restates a resolved attribute (e.g. field 'account_status', value 'CLOSED').

    Args:
        threshold (Dict[str, Any]): One intent threshold.
        filters (List[Dict[str, Any]]): Validated filters.
        words (List[str]): Restriction words.

    Returns:
        bool: True when its field names the attribute (key, column or label) or a value states a restriction word.
    """
    field_name = re.sub(r"[^a-z]", "", str(threshold.get("field") or "").lower())
    for f in filters:
        attribute = BY_KEY[f["attribute"]]
        names = {attribute.key, attribute.column, attribute.label}
        if field_name and field_name in {re.sub(r"[^a-z]", "", n.lower()) for n in names}:
            return True
    return any(_mentions(threshold.get(k), words) for k in ("value_from", "value_to", "field"))


def strip_attribute_phrases(intent: Dict[str, Any], filters: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Copy of an intent without the wording of resolved attribute filters.

    The SQL service must neither refuse a restriction the AML Builder applies itself nor apply it a
    second time in its own way (e.g. "dormant" as an inactivity rule on top of the status filter).

    Args:
        intent (Dict[str, Any]): The scenario intent (not modified).
        filters (List[Dict[str, Any]]): Filters applied by the AML Builder itself.

    Returns:
        Dict[str, Any]: The copy, without 'attribute_filters'; semantic conditions, thresholds and customer
            segments that state a filter are dropped, and the restriction words are removed from every
            other free-text field (transaction types, codes, other thresholds and windows are kept verbatim).
    """
    cleaned = json.loads(json.dumps(intent, default=str))
    cleaned.pop("attribute_filters", None)
    words = _core_words(filters)
    if not filters or not words:
        return cleaned

    conditions = cleaned.get("semantic_conditions")
    if isinstance(conditions, list):
        cleaned["semantic_conditions"] = [
            c for c in conditions
            if not (isinstance(c, dict) and any(_mentions(c.get(k), words) for k in ("raw_phrase", "predicate")))
        ]
    thresholds = cleaned.get("thresholds")
    if isinstance(thresholds, list):
        cleaned["thresholds"] = [t for t in thresholds if not (isinstance(t, dict) and _restates(t, filters, words))]
    segments = cleaned.get("customer_segments")
    if isinstance(segments, list):
        indv_corp = any(f["attribute"] == "individual_corporate" for f in filters)
        cleaned["customer_segments"] = [
            s for s in segments
            if not (_mentions(s, words) or (indv_corp and str(s).strip().upper() in _INDV_CORP_SEGMENTS))
        ] or None
    for key in list(cleaned):
        if key not in _PROTECTED_FIELDS and key not in ("semantic_conditions", "customer_segments"):
            cleaned[key] = _scrub(cleaned[key], words)
    return cleaned


# ─────────────────────────────── priority over transaction types ────────────────────────────────

_CONNECTORS: Final[FrozenSet[str]] = frozenset({"or", "and", "both", "either", "either/or"})


def _filter_vocabulary(filters: List[Dict[str, Any]]) -> FrozenSet[str]:
    """Words that only state a resolved filter's value: its restriction words and its codes' descriptions.

    Args:
        filters (List[Dict[str, Any]]): Validated filters.

    Returns:
        FrozenSet[str]: Lower-case words (e.g. {'credit', 'debit'} for a debit/credit filter).
    """
    words = set(_core_words(filters))
    for f in filters:
        for description in f.get("descriptions") or []:
            words.update(re.findall(r"[a-z][a-z\-]+", str(description).lower()))
    return frozenset(words - _SUBJECT_WORDS)


def is_covered_type(transaction_type: Any, filters: List[Dict[str, Any]]) -> bool:
    """Whether a 'transaction type' is only an attribute value already handled by a filter.

    'CREDIT', 'DEBIT' or 'credit transactions' under a debit/credit filter are covered; 'CREDIT CARD',
    'CREDIT TRANSFER' and 'CASH DEPOSIT' are real transaction types and are not.

    Args:
        transaction_type (Any): One transaction type from the intent.
        filters (List[Dict[str, Any]]): Validated filters.

    Returns:
        bool: True when every meaningful word of the type is a value of a filter.
    """
    text = str(transaction_type or "").lower()
    if not filters or re.search(rf"\b{_TYPE_NOUNS}\b", text):
        return False
    words = {w for w in re.findall(r"[a-z][a-z\-/]+", text) if w not in _SUBJECT_WORDS and w not in _CONNECTORS}
    return bool(words) and words <= _filter_vocabulary(filters)


def drop_covered_transaction_types(
    intent: Dict[str, Any], filters: List[Dict[str, Any]]
) -> Tuple[Dict[str, Any], List[str]]:
    """Give attribute filters priority over explanation codes for the same words.

    A transaction type that is only a filter's value ("CREDIT", "DEBIT" under a debit/credit filter) is
    removed, with its explanation codes, from the intent and from every threshold and condition, so it is
    handled by the filter alone. When no type remains, the scenario covers all transaction types.

    Args:
        intent (Dict[str, Any]): The intent (not modified).
        filters (List[Dict[str, Any]]): Validated filters.

    Returns:
        Tuple[Dict[str, Any], List[str]]: The updated copy and the dropped types (empty: the intent unchanged).
    """
    types = intent.get("transaction_types")
    if not isinstance(types, list) or not types:
        single = intent.get("transaction_type")
        types = [p.strip() for p in re.split(r"[,;]", str(single)) if p.strip()] if single else []
    dropped = [t for t in types if is_covered_type(t, filters)]
    if not dropped:
        return intent, []
    updated = json.loads(json.dumps(intent, default=str))
    kept = [t for t in types if t not in dropped]
    updated["transaction_types"] = kept or None
    updated["transaction_type"] = ", ".join(kept) if kept else None

    dropped_keys = {str(t).strip().upper() for t in dropped}
    by_type = updated.get("explanation_codes_by_type")
    if not kept:
        updated["explanation_codes"] = None
        updated["explanation_codes_by_type"] = None
    elif isinstance(by_type, dict):
        removed = {c for k, codes in by_type.items() if str(k).strip().upper() in dropped_keys for c in codes or []}
        updated["explanation_codes_by_type"] = {
            k: v for k, v in by_type.items() if str(k).strip().upper() not in dropped_keys
        } or None
        if isinstance(updated.get("explanation_codes"), list):
            updated["explanation_codes"] = [c for c in updated["explanation_codes"] if c not in removed] or None

    for list_field in ("thresholds", "semantic_conditions"):
        for condition in updated.get(list_field) or []:
            if isinstance(condition, dict) and is_covered_type(condition.get("transaction_type"), filters):
                condition["transaction_type"] = None
                condition["explanation_codes"] = None
    logger.info("[ATTR_FILTER] Transaction types handled by attribute filters instead of codes: %s", dropped)
    return updated, dropped


def describe_filters(filters: List[Dict[str, Any]]) -> List[str]:
    """One plain line per filter, e.g. "Account status: DORMANT".

    Args:
        filters (List[Dict[str, Any]]): Validated filters.

    Returns:
        List[str]: The lines.
    """
    lines = []
    for f in filters:
        values = ", ".join(dict.fromkeys(f.get("descriptions") or f.get("codes") or []))
        lines.append(f"{f['label']}: {'not ' if f['operator'] == 'NOT IN' else ''}{values}")
    return lines
