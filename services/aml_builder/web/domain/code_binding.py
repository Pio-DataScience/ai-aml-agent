"""Pure explanation-code binding helpers for normalized intents."""

from typing import Any, Dict, List


ALL_TRANSACTION_SCOPE_LABELS = {
    "ALL",
    "ALL TRANSACTION",
    "ALL TRANSACTIONS",
    "ALL TYPES",
    "ALL TRANSACTION TYPES",
    "ANY",
    "ANY TRANSACTION",
    "ANY TRANSACTIONS",
    "ANY TYPE",
    "ANY TYPES",
    "ANY TRANSACTION TYPE",
    "ANY TRANSACTION TYPES",
}


def specific_transaction_types(transaction_type: Any) -> List[str]:
    """Return specific transaction types, excluding all/any scope labels.

    Args:
        transaction_type: One transaction type or a list of transaction types.

    Returns:
        Specific transaction types that can be used for code discovery.
    """
    values = (
        transaction_type
        if isinstance(transaction_type, list)
        else [transaction_type]
    )
    return [
        normalized
        for value in values
        if value
        and (normalized := " ".join(str(value).strip().upper().split()))
        and normalized not in ALL_TRANSACTION_SCOPE_LABELS
    ]


def bind_channels_and_codes_to_intent(
    intent_dict: Dict[str, Any],
    discovered_codes: List[Dict[str, Any]],
    codes_by_type: Dict[str, List[str]],
) -> None:
    """Bind discovered codes to matching semantic conditions and thresholds.

    Args:
        intent_dict: Mutable normalized intent to enrich with discovered codes.
        discovered_codes: Search results containing channel and code values.
        codes_by_type: Explanation codes grouped by transaction channel.
    """
    if not codes_by_type:
        return

    semantic_conditions = intent_dict.get("semantic_conditions")
    if isinstance(semantic_conditions, list):
        for condition in semantic_conditions:
            if not isinstance(condition, dict):
                continue
            text = (
                f"{condition.get('raw_phrase', '')} "
                f"{condition.get('predicate', '')}"
            ).lower()
            for transaction_key, codes in codes_by_type.items():
                words = [
                    word
                    for word in transaction_key.lower().split()
                    if len(word) > 3
                ]
                if (
                    any(word in text for word in words)
                    or transaction_key.lower() in text
                ):
                    if not condition.get("transaction_type"):
                        condition["transaction_type"] = transaction_key
                    if not condition.get("explanation_codes"):
                        condition["explanation_codes"] = codes
                    break

    thresholds = intent_dict.get("thresholds")
    if not isinstance(thresholds, list):
        return
    for index, threshold in enumerate(thresholds):
        if not isinstance(threshold, dict):
            continue
        existing_type = threshold.get("transaction_type")
        if existing_type and existing_type in codes_by_type:
            threshold["explanation_codes"] = codes_by_type[existing_type]
            continue
        matched_type = None
        if isinstance(semantic_conditions, list):
            for condition in semantic_conditions:
                if not isinstance(condition, dict) or not condition.get(
                    "transaction_type"
                ):
                    continue
                raw_phrase = str(condition.get("raw_phrase", "")).lower()
                predicate = str(condition.get("predicate", "")).lower()
                value = str(threshold.get("value_from", ""))
                operator = str(threshold.get("operator", ""))
                if (
                    value in raw_phrase
                    or value in predicate
                    or (
                        ">" in operator
                        and ("exceed" in predicate or "over" in raw_phrase)
                    )
                    or (
                        "<" in operator
                        and ("below" in predicate or "under" in raw_phrase)
                    )
                ):
                    matched_type = condition["transaction_type"]
                    break
        if not matched_type and len(thresholds) == len(codes_by_type):
            keys = list(codes_by_type)
            if index < len(keys):
                matched_type = keys[index]
        if matched_type and matched_type in codes_by_type:
            threshold["transaction_type"] = matched_type
            threshold["explanation_codes"] = codes_by_type[matched_type]
