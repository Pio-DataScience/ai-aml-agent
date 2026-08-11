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

===================================================================================
8/10/2026

4. Interface — what a production AML console needs beyond the chat

Your chat + side-panel pattern is right for the build experience. Production-ready means adding these surfaces:

Screen	Purpose
Scenario Library / Catalog	Every persisted scenario, searchable, with status (active/paused/deprecated), owner, last-run date, alert volume trend
Version History & Diff Viewer	Side-by-side comparison of scenario versions — what threshold changed, who changed it
Approval Queue	Pending scenarios awaiting checker sign-off, with SLA countdown
Shadow Test Sandbox	Re-run any historical scenario against a chosen date range without touching production
Rollback / Deactivate	One-click disable of a live scenario if it's generating false-positive floods, with reason logging
Explainability Panel	Plain-English "why did this alert" trace-back from a production alert to the originating AMLIntent field
Alert Volume Dashboard	Post-deployment monitoring — is this scenario's daily alert count drifting from shadow test baseline?
Admin / Config Console	Manage Extraction Laws, explanation code catalog, embedding cache refresh — without redeploying code
Escalation Report Center	You already have this artifact — needs a persistent inbox view, not just chat-ephemeral

- huge feature integration completing "`Production Scenario Registry Module`":

what is next?

The tables need to expand PIO_AML_PRODUCTION_SCENARIOS beyound this created one, currently we insert into it the query only.

we need to start persisting scnarios deatlis along with audit deatlis e.g. the exact intent payload that generated this query, along with the implementaion plan, with versioning of the schema pydantc that was in service when it was created etc...
thats one.

 another tables should be created for scenario deatils and that will be used to insert the alerts on the alerts tables "PIO_AML_CUSTOMERS", "PIO_AML_CUSTOMERS_DET"

PIO_AML_SCENARIO IS ALSO NEEDED NOT BEACUSE WE NEED THIS TABE EXACTLY RATHER THE INFORMATION WE WILL fill in it is all scenario descriptions etc. used by other modules in the company to show alerts with their deatils so we need to stick to these 3 tables for now.

we might but still i need to confrim fill the minum filed from these tables SELECT * FROM PIO_AML_RULES
SELECT * FROM PIO_AML_SCENARIO_RULES why? beeacuse they are connected to the other tables as primary keys and existing legacy systems might fail on them if they are empty. (not important now even if we dont know their logic).

## Interactive scenario creation

once we reach the final level of this scenario life cycle "`Registry Module`" a determanistic scenario fileds filling layer should start, whith the help of the llm so it is informed with meta data about every thing the user selects. or keep it fully handeled by the llm, this needs to be disscussed.

to make sure o undertsand this layer correctly ill give you examples:

a scenario needs a defined risk degree flag (values defined in a lookup table called PIO_AML_DEGREE_RISK) an officer picks one of the values in it for the scenario he creetaed. thats one example of the things we need to fill to create the scnario workflow.
