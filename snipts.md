
{
  "scenario_name": "Rolling One-Week Outward Transfers or Cash Deposits",
  "scenario_type": "CUSTOMER",
  "transaction_type": "OUTWARD TRANSFER, CASH DEPOSIT",
  "transaction_types": [
    "OUTWARD TRANSFER",
    "CASH DEPOSIT"
  ],
  "explanation_codes": [
    "1414",
    "1"
  ],
  "detection_logic": "Flag customers during each rolling one-week period when they make an outward transfer over 10,000 or a cash deposit under 10,000.",
  "thresholds": [
    {
      "field": "transaction_amount",
      "operator": ">",
      "value_from": 10000.0,
      "value_to": null,
      "provenance": "stated",
      "target_scope": "DETAIL"
    },
    {
      "field": "transaction_amount",
      "operator": "<",
      "value_from": 10000.0,
      "value_to": null,
      "provenance": "stated",
      "target_scope": "DETAIL"
    }
  ],
  "time_window": {
    "unit": "WEEKS",
    "value": 1,
    "is_rolling": true,
    "provenance": "stated"
  },
  "baseline_window": null,
  "customer_segments": null,
  "exclusions": null,
  "mapped_keywords": null,
  "clarification_needed": false,
  "clarification_questions": [],
  "aggregation": {
    "metric": "transaction_amount",
    "function": "EXISTS",
    "grain": "PER CUSTOMER PER ROLLING WEEK",
    "provenance": "stated"
  },
  "semantic_conditions": [
    {
      "raw_phrase": "outward transfers",
      "logical_type": "BEHAVIORAL",
      "subject": "transaction",
      "predicate": "transaction_type is outward transfer",
      "provenance": "stated"
    },
    {
      "raw_phrase": "cash deposits",
      "logical_type": "BEHAVIORAL",
      "subject": "transaction",
      "predicate": "transaction_type is cash deposit",
      "provenance": "stated"
    },
    {
      "raw_phrase": "or",
      "logical_type": "OTHER",
      "subject": "detection_rule",
      "predicate": "flag when either the outward-transfer condition or the cash-deposit condition is satisfied",
      "provenance": "stated"
    }
  ],
  "clarifications": [],
  "applied_defaults": [],
  "ready_for_handoff": true,
  "expected_alert_range_min": null,
  "expected_alert_range_max": null,
  "anchor_date": null
}

SELECT
    T.DAY_DATE,
    T.CUS_NUM,
    T.ACCOUNT_NUMBER,
    T.TRA_DATE,
    T.TRA_SEQ1,
    T.TRA_SEQ2,
    T.BRA_CODE,
    T.CUR_CODE,
    T.LED_CODE,
    T.SUB_ACCT_CODE,
    T.TRA_AMT,
    T.EQU_TRA_AMT,
    T.EXPL_CODE
FROM BI_DWH.PIO_TRANSACTIONS T
WHERE T.TRA_DATE BETWEEN TRUNC(SYSDATE) - 7 AND TRUNC(SYSDATE)
  AND T.EXPL_CODE IN ('1414', '1')
  AND (
        (T.EXPL_CODE = '1414' AND T.EQU_TRA_AMT > 10000)
        OR
        (T.EXPL_CODE = '1' AND T.EQU_TRA_AMT < 10000)
      )
