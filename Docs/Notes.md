## Scenario 1

catch any customer whos transactions exceeded 10k in one day.

step one

1) login in into bankbi
2) go to scanrios managemnt.
3) hit the plus tab
4) fill the blanks.
5) approve it.

step 2.

we have big issue: if error is appened to the ""

## CURRENT SYSTEM STATE"" the llm stops me.

===================================================================================
7/16/2026

1) what is the diffrence between the intent_analyst_system_v2 and the prompt hardcoded in the intent_analyst_node function?

2) decomposer_node: this function perioid types isnt fully supported. check the code its if statments to days month year, add a live lookup to the table "PIO_PERIOD_TYPE". do that for the risk level too PIO_AML_DEGREE_RISK, then we might add llm to support it rather than hard coded. if its only a right decision.

3) All lookups tables should be filtered on country and inst code. this will return the items for the specific bank we deploying on rather than the duplicates and noise.

4) The database manager told me that the TRANS_TYPE column is a deprycated legacy column, here in scenarios we only need to fitler on EXPL_CODE. (This needs to be handeled on service b level, just mentioned it here so i rember it, dont do anything here).

===================================================================================
7/22/2026

- The infinte loop issue of intent planner issue is still persistant. only when i ask the agent to add a new logic.

===================================================================================
7/23/2026

- **Decomposer logical operator limitation (OR mapping gap):**
  * **The Problem:** The `QBRuleDetail` mapping engine in `agent.py` currently hardcodes the `combined_rule` parameter to `'AND'` for all rule condition rows (setting only the last sequence row to `'-'`). It does not support mapping `'OR'` connectors.
  * **Why it matters:** When a scenario requires alternative conditions (e.g. `Count >= 5 AND (Volume >= 50k OR Volume > 2x Baseline)`), the engine writes all rule rows with `'AND'` into the database table `PIO_AML_RULES_DETAILS`. This forces the database engine to require BOTH volume conditions, breaking the business logic.
  * **Solution to Implement:** Update the `decomposer` parsing logic in `agent.py` to inspect the generated SQL WHERE/HAVING clauses. If conditions/expressions are separated by an `OR` operator in the query, the decomposer must dynamically pass `combined="OR"` to `_make_detail` for the corresponding rule row.