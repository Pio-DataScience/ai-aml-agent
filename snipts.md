
Monitors high-value transactions conducted by minors or young adults that are inconsistent with their age and profile.

Rules:

customer intity type: Individual. customer age: (Age <= 20). transaction type: All thresholds (transaction accumativle amount) : >= 100 Million Transactions period :1 Month

{
"scenario_name": "Monitor High-Value Transactions by Minors or Young Adults",
"scenario_type": "TRANSACTION",
"transaction_type": null,
"detection_logic": "Identifies high-value transactions conducted by minors or young adults that do not align with their expected financial profile.",
"thresholds": [
{
"field": "transaction_amount",
"operator": ">",
"value_from": 10000.0,
"target_scope": "DETAIL"
}
],
"time_window": {
"unit": "DAY",
"value": 30,
"is_rolling": true
},
"aggregation": {
"metric": "transaction_amount",
"function": "NONE",
"grain": "PER TRANSACTION PER WINDOW"
},
"customer_segments": [
"MINOR",
"YOUNG ADULT"
],
"exclusions": null,
"semantic_conditions": [
{
"raw_phrase": "transactions inconsistent with their age and profile",
"logical_type": "BEHAVIORAL",
"subject": "transactions",
"predicate": "inconsistent with age and profile"
}
],
"anchor_date": null,
"explanation_codes": null
}
