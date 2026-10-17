# P0 Spikes

Offline findings for P0. No LLM, Oracle, or Service B calls were made. Service B
source was inspected read-only for S3.

## S1 — LangGraph 1.2.5 behavior

- The installed distribution is `langgraph==1.2.5` (also pinned in
  [`requirements.txt`](../requirements.txt#L26)). Calling the current
  `langgraph.prebuilt.create_react_agent` emits
  `LangGraphDeprecatedSinceV10`, directing callers to
  `langchain.agents.create_agent`. The warning was observed in a local call that
  used an invalid placeholder model and was caught before any model execution;
  the warning originates at the package decorator
  `.venv/Lib/site-packages/langgraph/prebuilt/chat_agent_executor.py:273-276`.
- `ToolNode` supports a tool returning `Command`. An offline compiled `StateGraph`
  test ran a `ToolNode` whose fake tool returned a `Command` containing the
  matching `ToolMessage`; the command's additional state update was merged.
  Validation is implemented in the installed package at
  `.venv/Lib/site-packages/langgraph/prebuilt/tool_node.py:1503`.
- `pre_model_hook` supports a transient model input through `llm_input_messages`.
  The hook documentation says that key is used as LLM input and does not update
  the state `messages`; returning `messages` instead updates state. At least one
  of the two keys is required. See
  `.venv/Lib/site-packages/langgraph/prebuilt/chat_agent_executor.py:396-413`.
- Service A currently imports `create_react_agent` at
  `services/aml_builder/web/services/graph.py:36`; this spike records the
  deprecation for a later package, without changing the orchestrator in P0.

## S2 — Oracle SQL parser

- `sqlglot==27.28.1` is installed in the local environment and is now pinned in
  [`requirements.txt`](../requirements.txt#L54).
- All five samples parsed with `sqlglot.parse(sql, read="oracle")`:
  1. Registry SELECT from `services/aml_builder/web/services/production_registry.py:349-354`.
  2. Active-scenario join SELECT from `services/aml_builder/web/services/alert_engine.py:36-54`.
  3. Alert count/group SELECT from `services/aml_builder/web/services/scenario_registry_analytics.py:397-405`.
  4. Explanation-code lookup SELECT from `services/aml_builder/web/services/explanation_code_search.py:64-71`.
  5. A representative WITH query using those same registry and alert tables. The
     repository has no existing WITH query in its Python SQL literals; this CTE
     sample was assembled from the tables and columns in samples 1 and 3.
- Result: the installed parser handles representative Oracle SELECT and WITH
  syntax. This is a parser spike only; it does not establish SQL safety or
  validate arbitrary generated SQL.

## S3 — Service B explicit-code check

Read-only inspection of
`PioTech-AI/services/dwh_scenario_agent/web/services/scenario_contract.py`:

- `transaction_code_error` gathers explicit codes from root `explanation_codes`,
  `explanation_codes_by_type`, thresholds, and semantic conditions
  (`scenario_contract.py:112-121`).
- It ignores `ALL`/`ANY` transaction labels when deciding whether a transaction
  type is specific, detects `EXPL_CODE =` or `EXPL_CODE IN (...)`, and rejects
  an invented explanation-code restriction for an all/no-type intent
  (`scenario_contract.py:122-132`).
- It extracts single-quoted values from equality and `IN` predicates and
  compares them with the requested code set, returning the missing values
  (`scenario_contract.py:133-139`).
- The check is regex-based, not SQL-AST-based. It recognizes only the listed
  predicate forms and does not prove that a predicate is connected to the
  correct table alias or query branch. Service A should retain its own
  post-generation validation when that later package is implemented.
