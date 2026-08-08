# AML Builder — Upgrade V2 Implementation Plan

**Purpose:** Five production-grade upgrades to the AML Builder agent service.  
**Status:** Approved for implementation.  
**Primary files touched:**
- `services/aml_builder/web/services/schemas.py`
- `services/aml_builder/web/services/agent.py`
- `services/aml_builder/web/services/oracle.py`
- `services/aml_builder/web/api/main.py`
- `services/aml_builder/web/services/prompts/planner_system.md` *(new)*

---

## Phase 0: Discovery Summary (Reference — Do Not Implement)

This section records what was read before planning. Each implementation phase cites
exact line numbers and patterns found here.

### Existing patterns

**Prompt loading pattern** (`agent.py:130-145`):
```python
def _load_prompt(filename: str) -> str:
    prompt_path = Path(__file__).parent / "prompts" / filename
    try:
        return prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("[PROMPT] Not found: %s — using empty prompt.", filename)
        return ""
```
New `planner_system.md` must live in `services/aml_builder/web/services/prompts/`.

**Oracle read pattern** (`oracle.py:101-130`):
```python
def run_readonly(sql, params=None) -> Tuple[List[str], List[tuple]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(stripped, params or {})
        columns = [col[0].lower() for col in cursor.description]
        rows = cursor.fetchall()
        return columns, rows
```
`PIO_AML_CUSTOMERS_DET` schema is NOT documented — use `cursor.description` at runtime (same pattern already used for `PIO_AML_CUSTOMERS` samples in `validator_node:1663-1668`).

**SSEEvent type literals** (`schemas.py:582-589`):
```python
type: Literal["tool_call","thinking","content","final_answer","scenario_result","error","done"]
```
Two new literals needed: `"plan_artifact"`, `"escalation_report"`.

**LangGraph state** (`agent.py:53-87`): All fields are plain TypedDict entries except
`messages` which uses `Annotated[List[BaseMessage], add_messages]`.

**main.py initial_state** (`main.py:178-194`): Currently resets ALL state fields on every
call — this BREAKS multi-turn persistence. Phase 8 fixes this with a thread-aware pattern.

**Routing states currently used:** `INTENT`, `CLARIFY`, `WAIT_USER`, `SQL_BRIDGE`,
`DECOMPOSE`, `QB_WRITE`, `VALIDATE`, `FINALIZE`, `ERROR`, `END`.

**No test files exist** in the project. No `tests/` directory.

**CHANGELOG.md exists** at project root — must be updated after implementation.

---

## Phase 1 — Schema Layer

**File:** `services/aml_builder/web/services/schemas.py`  
**Depends on:** Nothing (first phase).

### 1A — Extend `AMLScenarioState`

Add these fields to `AMLScenarioState` TypedDict (after the existing `error_log` field):

```python
# ── Plan layer ────────────────────────────────────────────────────────────────
plan_artifact: Optional[str]           # markdown plan text streamed to frontend
plan_conditions: Optional[List[Dict[str, Any]]]  # machine-readable conditions for drift check
plan_approved: bool                    # True once user says "proceed"

# ── Catalog auto-creation log ─────────────────────────────────────────────────
catalog_creations: List[Dict[str, Any]]  # one entry per auto-provisioned catalog row

# ── Post-write verification ───────────────────────────────────────────────────
write_verification: Optional[Dict[str, Any]]  # per-table row counts after INSERT

# ── Escalation ────────────────────────────────────────────────────────────────
escalation_report: Optional[str]       # markdown escalation report on terminal failure
failure_mode: Optional[str]            # "REDEFINE" | "ADJUST" | "ESCALATE"
```

### 1B — New Pydantic models

Add these models to `schemas.py` in a new section `# PLAN LAYER`:

```python
class PlanCondition(BaseModel):
    """A single parsed condition extracted from the planner's output.
    Used for pre-commit drift assertion against generated rule_details.

    Args:
        condition_id (str): Stable identifier — e.g. "filter_1", "aggregate_1".
        field (str): Business field name (e.g. "transaction_amount").
        operator (str): Comparison operator (">=", "IN", "BETWEEN", etc.).
        value_from (str): Threshold value as string (preserves precision).
        value_to (Optional[str]): Upper bound for BETWEEN operator only.
        condition_type (str): "filter" | "aggregate" | "sd" | "segment".
        description (str): Human-readable description for error messages.
    """
    condition_id: str
    field: str
    operator: str
    value_from: str
    value_to: Optional[str] = None
    condition_type: Literal["filter", "aggregate", "sd", "segment"]
    description: str


class CatalogCreation(BaseModel):
    """Log entry for a single auto-provisioned catalog record.

    Args:
        entity_type (str): "TABLE" | "COLUMN" | "PARAMETER".
        code (str): The new code assigned (TABLE_CODE, COLUMN_CODE, or PARAMETER_CODE).
        name (str): The physical name (table or column name).
        business_name (str): LLM-inferred business name.
        aggregation_code (Optional[str]): Inferred aggregation code.
        created_at (datetime): Timestamp of creation.
    """
    entity_type: Literal["TABLE", "COLUMN", "PARAMETER"]
    code: str
    name: str
    business_name: str
    aggregation_code: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class WriteVerification(BaseModel):
    """Row count verification for each Oracle table after atomic write.

    Args:
        scenario_rows (int): COUNT(*) from PIO_AML_SCENARIO for this scenario_code.
        rule_rows (int): COUNT(*) from PIO_AML_RULES for this rule_code.
        scenario_rule_rows (int): COUNT(*) from PIO_AML_SCENARIO_RULES.
        rule_detail_rows (int): COUNT(*) from PIO_AML_RULES_DETAILS.
        expected_detail_rows (int): Number of QBRuleDetail objects that were inserted.
        all_pass (bool): True if all counts match expected values.
        discrepancies (List[str]): Human-readable list of any count mismatches.
    """
    scenario_rows: int
    rule_rows: int
    scenario_rule_rows: int
    rule_detail_rows: int
    expected_detail_rows: int
    all_pass: bool
    discrepancies: List[str] = Field(default_factory=list)
```

### 1C — Extend `ValidationResult`

Add these fields to `ValidationResult` (after existing `confidence_score`):

```python
det_count: Optional[int] = Field(
    default=None,
    description="Number of transaction-level detail records in PIO_AML_CUSTOMERS_DET.",
)
det_samples: List[Dict[str, Any]] = Field(
    default_factory=list,
    description="Sample rows from PIO_AML_CUSTOMERS_DET (up to 5).",
)
alert_density_ratio: Optional[float] = Field(
    default=None,
    description="det_count / customer_count — flags overly broad scenarios.",
)
write_integrity: Optional[WriteVerification] = Field(
    default=None,
    description="Post-write row count verification across all 4 Oracle tables.",
)
catalog_integrity: bool = Field(
    default=True,
    description="False if any auto-created catalog entries failed the join-path check.",
)
threshold_sensitivity: Optional[Dict[str, Any]] = Field(
    default=None,
    description="Diagnostic: alert counts at ±20% threshold. Populated only when alert_count=0.",
)
```

### 1D — Update `SSEEvent` type literal

Change the `type` field in `SSEEvent` from:
```python
type: Literal["tool_call","thinking","content","final_answer","scenario_result","error","done"]
```
to:
```python
type: Literal[
    "tool_call", "thinking", "content", "final_answer",
    "scenario_result", "plan_artifact", "escalation_report", "error", "done"
]
```

### Verification checklist — Phase 1
- [ ] `AMLScenarioState` has all 7 new fields with correct types
- [ ] `PlanCondition`, `CatalogCreation`, `WriteVerification` classes exist with Google-style docstrings
- [ ] `ValidationResult` has 6 new fields, all Optional with defaults so existing code compiles
- [ ] `SSEEvent` literal includes `"plan_artifact"` and `"escalation_report"`
- [ ] `python -c "from web.services.schemas import *"` runs without error (set PYTHONPATH=services/aml_builder first)

---

## Phase 2 — Oracle Utility Layer

**File:** `services/aml_builder/web/services/oracle.py`  
**Depends on:** Phase 1 (schemas, for type imports if needed).

### 2A — Add `atomic_connection()` context manager

Add after the existing `get_connection()` function (around line 99):

```python
@contextmanager
def atomic_connection() -> Generator[oracledb.Connection, None, None]:
    """Yield a single Oracle connection for a multi-statement atomic transaction.

    All statements executed on the yielded connection share one transaction
    boundary: a clean exit commits everything; any exception triggers a full
    rollback before re-raising.

    Usage:
        with atomic_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(insert_sql_1, params_1)
            cursor.execute(insert_sql_2, params_2)
        # commit happens here automatically

    Yields:
        oracledb.Connection: A live connection from the pool.

    Raises:
        RuntimeError: If the pool has not been initialized.
        oracledb.Error: On Oracle execution error (triggers automatic rollback).
    """
    if _pool is None:
        raise RuntimeError(
            "Oracle pool is not initialized. Call init_pool() at startup."
        )
    conn = _pool.acquire()
    try:
        yield conn
        conn.commit()
        logger.debug("[ORACLE] Atomic transaction committed.")
    except Exception:
        conn.rollback()
        logger.error("[ORACLE] Atomic transaction rolled back due to exception.")
        raise
    finally:
        _pool.release(conn)
```

### 2B — Add `get_next_numeric_code()` helper

Add after `atomic_connection()`:

```python
def get_next_numeric_code(table: str, code_column: str, where_clause: str = "") -> str:
    """Generate the next sequential numeric code for a catalog table.

    Queries MAX(TO_NUMBER(code_column)) and returns max+1 as a string.
    Falls back to '1000' if no rows exist or all codes are non-numeric.

    Args:
        table (str): Oracle table name (e.g., 'PIO_AML_PARAMETERS').
        code_column (str): Column name holding the current max code.
        where_clause (str): Optional additional WHERE filter (without the WHERE keyword).

    Returns:
        str: The next available numeric code as a string.

    Raises:
        oracledb.Error: On Oracle execution error.
    """
    where = f"WHERE {where_clause}" if where_clause else ""
    sql = f"SELECT MAX(TO_NUMBER({code_column})) FROM {table} {where}"
    try:
        _, rows = run_readonly(sql, {})
        current_max = rows[0][0] if rows and rows[0][0] is not None else 999
        return str(int(current_max) + 1)
    except Exception as exc:
        logger.warning(
            "[ORACLE] Could not determine max code for %s.%s: %s — using fallback 1000.",
            table, code_column, exc,
        )
        return "1000"
```

### Verification checklist — Phase 2
- [ ] `atomic_connection()` is importable: `from web.services.oracle import atomic_connection`
- [ ] `get_next_numeric_code()` is importable
- [ ] Calling `get_next_numeric_code` with a bad table raises `oracledb.Error`, not a silent fallback that hides DB issues (the log warning is acceptable; the function still returns a string)

---

## Phase 3 — Planner System Prompt

**File:** `services/aml_builder/web/services/prompts/planner_system.md` *(NEW)*  
**Depends on:** Nothing.

Create this file with exactly the following content:

```markdown
You are an expert AML Scenario Planner embedded in the PioTech AML Builder system.
Your job is to translate a structured AML intent into a clear, two-part output:

1. A **User-Facing Plan** — a structured markdown document the compliance manager reads and approves.
2. A **Machine-Readable Conditions Block** — a JSON array the system uses for pre-commit validation.

---

## OUTPUT FORMAT

Produce EXACTLY the following structure. Do not deviate from the section headers or the JSON block.

---

# AML Scenario Execution Plan

## Scenario Overview
| Field | Value |
|-------|-------|
| **Name** | {scenario_name} |
| **Type** | {scenario_type} |
| **Detection Ideology** | {one sentence plain English description of what this scenario catches and why} |

## Filters
List every WHERE-level condition in business language. Number each one.

1. **{business_field_name}**: {operator in plain English} **{value}** — _{business rationale}_

## Rules
List every aggregate threshold (COUNT, SUM, Standard Deviation). Number each one.

1. **{rule_name}**: The {aggregate_type} of {field} must be {operator} **{value}** — _{business rationale}_

## Time Interval
| Setting | Value |
|---------|-------|
| **Detection Window** | {n} {DAYS/MONTHS/YEARS} |
| **Window Type** | {Rolling from today / Fixed calendar period} |
| **Period Code** | {0=Last n Days / 3=Monthly / 6=Yearly} |

## Customer Scope
- **Included Segments:** {list or "All customer segments"}
- **Exclusions:** {list or "None"}

## Assumptions
List each assumption the system is making about the data or business logic.

- {assumption 1}
- {assumption 2}

## Parameter Mapping Preview
List what QB engine parameter will handle each condition. Use business names only — no Oracle codes.

| Condition | QB Parameter | Aggregation |
|-----------|-------------|-------------|
| {field} | {business name of QB parameter} | {Count / Sum / None} |

## Risk Flags
List any ambiguities, potential data quality issues, or calibration warnings.

- {risk or "No risk flags identified"}

---

## CONDITIONS_BLOCK
```json
[
  {
    "condition_id": "filter_1",
    "field": "{physical_field_name_from_intent}",
    "operator": "{>=|<=|>|<|=|IN|BETWEEN}",
    "value_from": "{threshold_value_as_string}",
    "value_to": null,
    "condition_type": "filter",
    "description": "{one line description}"
  },
  {
    "condition_id": "aggregate_1",
    "field": "{field}",
    "operator": ">=",
    "value_from": "{threshold}",
    "value_to": null,
    "condition_type": "aggregate",
    "description": "{description}"
  }
]
```

---

## RULES

### condition_type values
- `filter` — a WHERE clause condition (equality, range, IN list)
- `aggregate` — a HAVING clause condition (COUNT, SUM)
- `sd` — a standard deviation threshold
- `segment` — a customer segment or exclusion filter

### value_from
Always express as a plain string number or code list (e.g. "5000", "'CHW','CAA'").
Never include units or currency symbols.

### Do NOT include
- Oracle table names, column names, or procedure names in the user-facing sections
- PARAMETER_CODE numbers (these are internal)
- Technical implementation details of any kind in the user-facing plan

The compliance manager must be able to read the plan and approve it without any
technical knowledge of Oracle or the AML engine internals.
```

### Verification checklist — Phase 3
- [ ] File exists at `services/aml_builder/web/services/prompts/planner_system.md`
- [ ] `_load_prompt("planner_system.md")` returns non-empty string when called from agent.py

---

## Phase 4 — Planner Node and WAIT_APPROVAL Routing

**File:** `services/aml_builder/web/services/agent.py`  
**Depends on:** Phase 1 (new state fields), Phase 3 (prompt file).

### 4A — Add `planner_node()`

Insert after `intent_analyst_node()` (before `sql_bridge_node`):

```python
def planner_node(
    state: AMLScenarioState, config: RunnableConfig
) -> Dict[str, Any]:
    """Generate and emit the human-readable execution plan for user approval.

    Reads the enriched AMLIntent and uses the LLM to produce:
    1. A structured markdown plan artifact (for the frontend side panel).
    2. A machine-readable CONDITIONS_BLOCK JSON array (for drift assertion).

    Sets next_action to WAIT_APPROVAL. Execution halts until the user
    sends a message containing an approval keyword ("proceed", "yes",
    "approve", "go ahead", "confirm").

    Args:
        state (AMLScenarioState): Current agent state with enriched_intent set.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with plan_artifact, plan_conditions, next_action.
    """
    logger.info("[PLANNER] Generating execution plan for user approval.")

    intent_dict = state.get("enriched_intent") or {}
    try:
        intent = AMLIntent(**intent_dict)
    except Exception as exc:
        logger.error("[PLANNER] Could not deserialize AMLIntent: %s", exc)
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", []) + [f"Planner failed to read intent: {exc}"],
        }

    system_prompt = _load_prompt("planner_system.md")
    llm = _build_llm(fast=False)

    user_prompt = f"""Generate the AML Scenario Execution Plan for the following intent:

{json.dumps(intent.model_dump(), indent=2, default=str)}

Follow the OUTPUT FORMAT exactly. Produce the full markdown plan AND the CONDITIONS_BLOCK JSON.
"""

    try:
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ])
        raw = response.content.strip()

        # Extract the CONDITIONS_BLOCK JSON from the response
        conditions_match = re.search(
            r"## CONDITIONS_BLOCK\s*```json\s*([\s\S]+?)```",
            raw,
            re.IGNORECASE,
        )
        plan_conditions: List[Dict[str, Any]] = []
        if conditions_match:
            try:
                conditions_raw = json.loads(conditions_match.group(1).strip())
                # Validate each condition via Pydantic
                from web.services.schemas import PlanCondition
                plan_conditions = [PlanCondition(**c).model_dump() for c in conditions_raw]
                logger.info("[PLANNER] Extracted %d plan conditions.", len(plan_conditions))
            except Exception as exc:
                logger.warning("[PLANNER] Could not parse CONDITIONS_BLOCK: %s", exc)

        # The full raw response IS the plan artifact (markdown + JSON block)
        # Frontend renders everything before CONDITIONS_BLOCK as the user-facing plan
        logger.info("[PLANNER] Plan generated. Length=%d chars. Conditions=%d.",
                    len(raw), len(plan_conditions))

        return {
            "plan_artifact": raw,
            "plan_conditions": plan_conditions,
            "plan_approved": False,
            "next_action": "WAIT_APPROVAL",
        }

    except Exception as exc:
        logger.error("[PLANNER] LLM call failed: %s", exc, exc_info=True)
        return {
            "next_action": "ERROR",
            "error_log": state.get("error_log", []) + [f"Planner LLM failed: {exc}"],
        }
```

### 4B — Add `route_after_planner()`

```python
def route_after_planner(state: AMLScenarioState) -> str:
    """Route after the Planner node.

    Args:
        state (AMLScenarioState): Current state.

    Returns:
        str: END to wait for user approval, or 'orchestrator' on error.
    """
    action = state.get("next_action", "WAIT_APPROVAL")
    if action == "ERROR":
        return "orchestrator"
    return END  # pause — wait for user message
```

### 4C — Update `route_after_intent()`

Change the existing `route_after_intent` to route to `"planner"` instead of `"sql_bridge"`:

```python
def route_after_intent(state: AMLScenarioState) -> str:
    action = state.get("next_action", "SQL_BRIDGE")
    if action == "CLARIFY":
        return "orchestrator"
    if action == "ERROR":
        return "orchestrator"
    return "planner"   # ← was "sql_bridge"
```

### 4D — Register node and edge in `build_graph()`

In `build_graph()`, after `graph.add_node("intent_analyst", intent_analyst_node)`:
```python
graph.add_node("planner", planner_node)
```

Replace:
```python
graph.add_conditional_edges("intent_analyst", route_after_intent)
```
With:
```python
graph.add_conditional_edges("intent_analyst", route_after_intent)
graph.add_conditional_edges("planner", route_after_planner)
```

### Verification checklist — Phase 4
- [ ] `planner_node` logs `[PLANNER]` entries
- [ ] On a valid intent, `plan_artifact` is a non-empty string in the returned state
- [ ] `plan_conditions` is a list (may be empty if LLM omits CONDITIONS_BLOCK)
- [ ] `next_action` is `"WAIT_APPROVAL"` on success
- [ ] `build_graph()` includes `"planner"` node with conditional edges

---

## Phase 5 — Orchestrator Upgrade: Approval Gate + Failure UX

**File:** `services/aml_builder/web/services/agent.py`  
**Depends on:** Phase 4 (new routing states).

### 5A — Approval keyword detection

At the START of `orchestrator_node()`, before the existing iteration guard, add:

```python
# ── Approval gate ─────────────────────────────────────────────────────────────
if state.get("next_action") == "WAIT_APPROVAL" and not state.get("plan_approved", False):
    messages = state.get("messages", [])
    last_user = next(
        (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
    ).lower().strip()

    APPROVAL_KEYWORDS = {"proceed", "yes", "approve", "go ahead", "confirm", "start", "execute"}
    if any(kw in last_user for kw in APPROVAL_KEYWORDS):
        logger.info("[ORCHESTRATOR] Plan approved by user. Routing to SQL_BRIDGE.")
        return {
            "plan_approved": True,
            "next_action": "SQL_BRIDGE",
            "iteration_count": state.get("iteration_count", 0) + 1,
        }
    else:
        # User sent a message but it wasn't an approval — remind them
        return {
            "messages": [AIMessage(content=(
                "I have your scenario plan ready in the side panel. "
                "Please review it and reply **proceed** when you're ready to create it, "
                "or let me know if you'd like to adjust anything."
            ))],
            "next_action": "WAIT_APPROVAL",
            "iteration_count": state.get("iteration_count", 0) + 1,
        }
```

### 5B — Route approval to sql_bridge

Update `route_after_orchestrator()` to include:
```python
route_map = {
    "INTENT": "intent_analyst",
    "SQL_BRIDGE": "sql_bridge",          # ← NEW: post-approval routing
    "CLARIFY": "orchestrator",
    "WAIT_USER": END,
    "WAIT_APPROVAL": END,               # ← NEW: pause for approval
    "END": END,
    "ERROR": "orchestrator",
    "FAILURE": "orchestrator",           # ← NEW: terminal failure menu
    "REDEFINE": "orchestrator",          # ← NEW: handled in orchestrator
    "ADJUST": "decomposer",             # ← NEW: loop back with new thresholds
    "ESCALATE": "orchestrator",         # ← NEW: generate escalation report
}
```

### 5C — Failure menu handling

In `orchestrator_node()`, add handling for `next_action == "FAILURE"`:

```python
if next_action == "FAILURE":
    error_log = state.get("error_log", [])
    last_error = error_log[-1] if error_log else "An unspecified error occurred."
    failure_message = _format_failure_menu(last_error)
    return {
        "messages": [AIMessage(content=failure_message)],
        "next_action": "WAIT_USER",
        "iteration_count": iteration,
    }
```

Add handling for `next_action == "WAIT_USER"` (user chose an option):

```python
if next_action == "WAIT_USER":
    messages_list = state.get("messages", [])
    last_user = next(
        (m.content for m in reversed(messages_list) if isinstance(m, HumanMessage)), ""
    ).lower().strip()

    # Detect user choice
    if any(k in last_user for k in ("1", "redefine", "re-define", "rephrase", "different")):
        return {
            "messages": [AIMessage(content=(
                "Understood. Please describe the scenario you'd like to create."
            ))],
            # Clear intent-layer state for fresh start
            "enriched_intent": None,
            "raw_sql": None,
            "sql_metadata": None,
            "scenario_parameters": None,
            "scenario_code": None,
            "plan_artifact": None,
            "plan_conditions": None,
            "plan_approved": False,
            "error_log": [],
            "next_action": "WAIT_USER",   # stay in WAIT_USER until user sends new intent
            "iteration_count": iteration,
        }

    elif any(k in last_user for k in ("2", "adjust", "threshold", "change", "modify")):
        return {
            "messages": [AIMessage(content=(
                "Of course. Please specify which threshold or value you'd like to adjust "
                "and what it should be changed to."
            ))],
            "next_action": "WAIT_USER",
            "iteration_count": iteration,
        }

    elif any(k in last_user for k in ("3", "escalat", "ticket", "team", "report")):
        report = _generate_escalation_report(state)
        return {
            "messages": [AIMessage(content=(
                "I've generated a full technical escalation report below. "
                "Please forward this to your implementation team."
            ))],
            "escalation_report": report,
            "next_action": "ESCALATE",
            "iteration_count": iteration,
        }

    # Check if user is now providing adjusted thresholds (post-choice-2 message)
    elif state.get("failure_mode") == "ADJUST":
        return {
            "user_intent": last_user,
            "next_action": "DECOMPOSE",
            "iteration_count": iteration,
        }

    else:
        # No recognisable choice yet — re-present the menu
        error_log = state.get("error_log", [])
        last_error = error_log[-1] if error_log else "An unspecified error occurred."
        return {
            "messages": [AIMessage(content=_format_failure_menu(last_error))],
            "next_action": "WAIT_USER",
            "iteration_count": iteration,
        }

if next_action == "ESCALATE":
    # Report was already generated — just end
    return {
        "next_action": "END",
        "iteration_count": iteration,
    }
```

### 5D — Add `_format_failure_menu()`

```python
def _format_failure_menu(error_detail: str) -> str:
    """Build the structured failure menu presented to the user on terminal error.

    Args:
        error_detail (str): The last error message from the error_log.

    Returns:
        str: Formatted markdown failure menu.
    """
    return (
        f"## Scenario Creation Failed\n\n"
        f"**What went wrong:**\n"
        f"```\n{error_detail}\n```\n\n"
        f"---\n\n"
        f"**Your options:**\n\n"
        f"1. **Redefine** — Describe your scenario differently and I'll start fresh.\n"
        f"2. **Adjust thresholds** — Tell me which values to change and what to change them to.\n"
        f"3. **Escalate** — I'll generate a full technical report for your implementation team.\n\n"
        f"_Reply with the number (1, 2, or 3) or describe your choice._"
    )
```

### 5E — Add `_generate_escalation_report()`

```python
def _generate_escalation_report(state: AMLScenarioState) -> str:
    """Build a structured markdown escalation report for the implementation team.

    Includes everything needed to reproduce and diagnose the failure:
    scenario intent, generated parameters, Oracle errors, and timestamps.

    Args:
        state (AMLScenarioState): Current agent state at time of escalation.

    Returns:
        str: Full markdown escalation report.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    intent_dict = state.get("enriched_intent") or {}
    scenario_code = state.get("scenario_code", "Not generated")
    error_log = state.get("error_log", [])
    catalog_creations = state.get("catalog_creations", [])
    params_dict = state.get("scenario_parameters") or {}
    write_verification = state.get("write_verification") or {}
    validation_result = state.get("validation_result") or {}

    errors_text = "\n".join(f"- {e}" for e in error_log) or "_No errors logged._"
    catalog_text = (
        "\n".join(
            f"- **{c.get('entity_type')}** `{c.get('code')}`: {c.get('name')} "
            f"({c.get('business_name')})"
            for c in catalog_creations
        )
        or "_None — all parameters were pre-existing in catalog._"
    )

    return (
        f"# AML Scenario Escalation Report\n\n"
        f"**Generated:** {now}  \n"
        f"**Scenario Code:** `{scenario_code}`  \n"
        f"**System:** PioTech AML Builder — Automated Agent\n\n"
        f"---\n\n"
        f"## Intent Submitted\n\n"
        f"```json\n{json.dumps(intent_dict, indent=2, default=str)}\n```\n\n"
        f"---\n\n"
        f"## Error Log (Chronological)\n\n"
        f"{errors_text}\n\n"
        f"---\n\n"
        f"## Catalog Auto-Provisions Attempted\n\n"
        f"{catalog_text}\n\n"
        f"---\n\n"
        f"## Scenario Parameters Generated\n\n"
        f"```json\n{json.dumps(params_dict, indent=2, default=str)}\n```\n\n"
        f"---\n\n"
        f"## Write Verification Results\n\n"
        f"```json\n{json.dumps(write_verification, indent=2, default=str)}\n```\n\n"
        f"---\n\n"
        f"## Validation Results\n\n"
        f"```json\n{json.dumps(validation_result, indent=2, default=str)}\n```\n\n"
        f"---\n\n"
        f"_This report was generated automatically by the AML Builder agent._  \n"
        f"_Please reference Scenario Code `{scenario_code}` in all correspondence._"
    )
```

### Verification checklist — Phase 5
- [ ] Sending "proceed" on a WAIT_APPROVAL thread sets `plan_approved=True` and routes to `sql_bridge`
- [ ] Sending a non-approval message on WAIT_APPROVAL gets a polite reminder, not an error
- [ ] A FAILURE state shows the 3-option menu
- [ ] Choosing "1" clears intent-layer state fields (enriched_intent, raw_sql, etc.)
- [ ] Choosing "3" produces a non-empty `escalation_report` string

---

## Phase 6 — Parameter Auto-Creation (Decomposer Upgrade)

**File:** `services/aml_builder/web/services/agent.py`  
**Depends on:** Phase 2 (oracle utilities), Phase 1 (CatalogCreation schema).

### 6A — Add `_query_oracle_column_type()`

```python
def _query_oracle_column_type(table_name: str, column_name: str) -> Optional[str]:
    """Query Oracle's ALL_TAB_COLUMNS for the physical data type of a column.

    COLUMN_TYPE must never be LLM-inferred — always read from the DB schema.
    Returns None if the column is not found (caller should handle gracefully).

    Args:
        table_name (str): Physical Oracle table name (e.g., 'PIO_TRANSACTIONS').
        column_name (str): Physical column name (e.g., 'TRA_AMT').

    Returns:
        Optional[str]: Oracle DATA_TYPE string (e.g., 'NUMBER', 'VARCHAR2', 'DATE')
            or None if not found.
    """
    from web.services.oracle import run_readonly
    try:
        _, rows = run_readonly(
            """
            SELECT DATA_TYPE, DATA_LENGTH
            FROM ALL_TAB_COLUMNS
            WHERE UPPER(TABLE_NAME) = UPPER(:table_name)
              AND UPPER(COLUMN_NAME) = UPPER(:column_name)
            """,
            {"table_name": table_name, "column_name": column_name},
        )
        if rows:
            data_type = str(rows[0][0]).strip()
            data_length = str(rows[0][1]).strip()
            return f"{data_type}({data_length})" if data_length != "None" else data_type
        logger.warning(
            "[CATALOG] Column %s.%s not found in ALL_TAB_COLUMNS.", table_name, column_name
        )
        return None
    except Exception as exc:
        logger.error("[CATALOG] ALL_TAB_COLUMNS query failed: %s", exc)
        return None
```

### 6B — Add `_infer_column_metadata()`

```python
def _infer_column_metadata(
    col_name: str,
    col_type: str,
    table_name: str,
) -> Dict[str, Any]:
    """Use the LLM to infer business metadata for a new catalog column.

    Only infers: COLUMN_BUSINESS_NAME, COLUMN_BUSINESS_NAME_NAT (Arabic),
    AGGREGATION_CODE, SD_USED_FLAG, BALANCE_FLAG.
    COLUMN_TYPE is explicitly excluded — it is passed in, not inferred.

    Args:
        col_name (str): Physical column name (e.g., 'DAILY_LIMIT').
        col_type (str): Actual Oracle data type (from ALL_TAB_COLUMNS).
        table_name (str): Table this column belongs to.

    Returns:
        Dict[str, Any]: Inferred metadata keys. Falls back to safe defaults
            if LLM call fails.
    """
    llm = _build_llm(fast=True)
    prompt = f"""You are an AML compliance metadata expert for a banking system.

A new column needs to be registered in the AML parameter catalog.

Column: {col_name}
Table: {table_name}
Oracle Type: {col_type}

Return a JSON object with EXACTLY these fields:
{{
  "column_business_name": "<clear English business name, max 40 chars>",
  "column_business_name_nat": "<Arabic translation of the business name, max 200 chars>",
  "aggregation_code": "<one of: 1=None/Direct, 2=Count, 3=Sum, 4=Average, 5=StdDev — pick based on column type and name>",
  "sd_used_flag": "<0 or 1 — 1 only if this is a numeric amount/balance column where standard deviation analysis is meaningful>",
  "balance_flag": "<0 or 1 — 1 only if this column represents an account balance>"
}}

Return ONLY the JSON. No explanation. No markdown fences."""

    defaults = {
        "column_business_name": col_name.replace("_", " ").title()[:40],
        "column_business_name_nat": col_name.replace("_", " ").title()[:200],
        "aggregation_code": "1",
        "sd_used_flag": "0",
        "balance_flag": "0",
    }

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = response.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        inferred = json.loads(raw)
        # Merge with defaults to ensure all keys present
        return {**defaults, **inferred}
    except Exception as exc:
        logger.warning(
            "[CATALOG] LLM metadata inference failed for %s: %s — using defaults.", col_name, exc
        )
        return defaults
```

### 6C — Add `_provision_catalog_entry()`

```python
def _provision_catalog_entry(
    col_name: str,
    table_name: str,
    catalog_creations: List[Dict[str, Any]],
) -> Optional[Tuple[str, str]]:
    """Self-provision a missing column into the AML catalog and return its codes.

    Runs three sequential steps:
      1. Check / INSERT PIO_AML_TABLES for the source table.
      2. Query ALL_TAB_COLUMNS for the physical data type (never LLM-inferred).
      3. Check / INSERT PIO_AML_COLUMNS for the column.
      4. Generate new PARAMETER_CODE and INSERT PIO_AML_PARAMETERS.

    All writes use run_write (individual commits per step) so that a failure
    in step 3 does not prevent steps 1-2 from being usable on retry.

    Args:
        col_name (str): Physical column name to provision (e.g., 'DAILY_LIMIT').
        table_name (str): Physical Oracle table name (e.g., 'PIO_TRANSACTIONS').
        catalog_creations (List[Dict]): Mutable list to append creation log entries.

    Returns:
        Optional[Tuple[str, str]]: (parameter_code, aggregation_code) on success,
            or None if provisioning fails (caller should raise a meaningful error).
    """
    from web.services.oracle import run_readonly, run_write, get_next_numeric_code
    from web.services.schemas import CatalogCreation
    now = datetime.utcnow()

    logger.info("[CATALOG] Auto-provisioning catalog for col=%s table=%s", col_name, table_name)

    # ── Step 1: Ensure PIO_AML_TABLES has the source table ────────────────────
    _, table_rows = run_readonly(
        "SELECT TABLE_CODE FROM PIO_AML_TABLES WHERE UPPER(TABLE_NAME) = UPPER(:tn)",
        {"tn": table_name},
    )
    if table_rows:
        table_code = str(table_rows[0][0]).strip()
        logger.info("[CATALOG] PIO_AML_TABLES: found existing TABLE_CODE=%s", table_code)
    else:
        table_code = get_next_numeric_code("PIO_AML_TABLES", "TABLE_CODE")
        biz_name = table_name.replace("PIO_", "").replace("_", " ").title()[:40]
        run_write(
            """INSERT INTO PIO_AML_TABLES
               (TABLE_CODE, TABLE_NAME, BUSINESS_NAME, BUSINESS_NAME_NAT)
               VALUES (:tc, :tn, :bn, :bnn)""",
            {"tc": table_code, "tn": table_name, "bn": biz_name, "bnn": biz_name},
        )
        catalog_creations.append(CatalogCreation(
            entity_type="TABLE", code=table_code, name=table_name, business_name=biz_name
        ).model_dump(mode="json"))
        logger.info("[CATALOG] Inserted PIO_AML_TABLES: TABLE_CODE=%s NAME=%s", table_code, table_name)

    # ── Step 2: Query ALL_TAB_COLUMNS for actual data type ────────────────────
    col_type = _query_oracle_column_type(table_name, col_name)
    if col_type is None:
        logger.error(
            "[CATALOG] Column %s not found in ALL_TAB_COLUMNS for table %s. Cannot provision.",
            col_name, table_name,
        )
        return None

    # ── Step 3: Ensure PIO_AML_COLUMNS has the column ────────────────────────
    _, col_rows = run_readonly(
        """SELECT COLUMN_CODE FROM PIO_AML_COLUMNS
           WHERE UPPER(COLUMN_NAME) = UPPER(:cn) AND TABLE_CODE = :tc""",
        {"cn": col_name, "tc": table_code},
    )
    if col_rows:
        col_code = str(col_rows[0][0]).strip()
        logger.info("[CATALOG] PIO_AML_COLUMNS: found existing COLUMN_CODE=%s", col_code)
    else:
        col_code = get_next_numeric_code("PIO_AML_COLUMNS", "COLUMN_CODE")
        meta = _infer_column_metadata(col_name, col_type, table_name)
        run_write(
            """INSERT INTO PIO_AML_COLUMNS
               (COLUMN_CODE, COLUMN_TYPE, TABLE_CODE, COLUMN_NAME,
                COLUMN_BUSINESS_NAME, COLUMN_BUSINESS_NAME_NAT,
                LOOKUP_FLAG, SD_USED_FLAG, BALANCE_FLAG, HIS_FLAG)
               VALUES (:cc, :ct, :tc, :cn, :cbn, :cbnnat, '0', :sd, :bal, '0')""",
            {
                "cc": col_code, "ct": col_type[:40], "tc": table_code, "cn": col_name,
                "cbn": meta["column_business_name"][:200],
                "cbnnat": meta["column_business_name_nat"][:200],
                "sd": meta["sd_used_flag"], "bal": meta["balance_flag"],
            },
        )
        catalog_creations.append(CatalogCreation(
            entity_type="COLUMN", code=col_code, name=col_name,
            business_name=meta["column_business_name"]
        ).model_dump(mode="json"))
        logger.info("[CATALOG] Inserted PIO_AML_COLUMNS: COLUMN_CODE=%s NAME=%s TYPE=%s",
                    col_code, col_name, col_type)

    # ── Step 4: Create PARAMETER_CODE in PIO_AML_PARAMETERS ──────────────────
    # Check if one already exists for this table+column combination
    _, param_rows = run_readonly(
        """SELECT PARAMETER_CODE, AGGREGATION_CODE FROM PIO_AML_PARAMETERS
           WHERE TABLE_CODE = :tc AND COLUMN_CODE = :cc""",
        {"tc": table_code, "cc": col_code},
    )
    if param_rows:
        p_code = str(param_rows[0][0]).strip()
        agg_code = str(param_rows[0][1]).strip() if param_rows[0][1] else "1"
        logger.info("[CATALOG] PIO_AML_PARAMETERS: found existing PARAMETER_CODE=%s", p_code)
        return p_code, agg_code

    # Re-infer for aggregation_code since we may have it from column step
    meta = _infer_column_metadata(col_name, col_type, table_name)
    agg_code = meta.get("aggregation_code", "1")
    p_code = get_next_numeric_code("PIO_AML_PARAMETERS", "PARAMETER_CODE")
    param_element = meta["column_business_name"]
    run_write(
        """INSERT INTO PIO_AML_PARAMETERS
           (PARAMETER_CODE, PARAMETER_ELEMENT, TABLE_CODE, COLUMN_CODE,
            AGGREGATION_CODE, PARAMETER_PERC_FLAG, PARAMETER_ELEMENT_NAT,
            LGM_SCENARIO_BASED_FLAG, LGM_GROUP_BASED_FLAG,
            CREATED_BY, CREATED_DATE, UPDATED_BY, UPDATED_DATE)
           VALUES (:pc, :pe, :tc, :cc, :ac, '0', :penat, '0', '0',
                   :cb, :cd, :ub, :ud)""",
        {
            "pc": p_code, "pe": param_element[:400], "tc": table_code, "cc": col_code,
            "ac": agg_code, "penat": meta["column_business_name_nat"][:400],
            "cb": 999, "cd": now, "ub": 999, "ud": now,
        },
    )
    catalog_creations.append(CatalogCreation(
        entity_type="PARAMETER", code=p_code, name=col_name,
        business_name=param_element, aggregation_code=agg_code
    ).model_dump(mode="json"))
    logger.info(
        "[CATALOG] Inserted PIO_AML_PARAMETERS: PARAMETER_CODE=%s for %s.%s",
        p_code, table_name, col_name,
    )
    return p_code, agg_code
```

### 6D — Add `_verify_catalog_integrity()`

```python
def _verify_catalog_integrity(catalog_creations: List[Dict[str, Any]]) -> bool:
    """Verify that auto-created catalog entries are reachable via the QB join path.

    Checks that each new PARAMETER_CODE can be found via the expected join:
    PIO_AML_PARAMETERS → PIO_AML_COLUMNS → PIO_AML_TABLES.

    Args:
        catalog_creations (List[Dict]): Entries from _provision_catalog_entry.

    Returns:
        bool: True if all checks pass, False if any new entry is orphaned.
    """
    if not catalog_creations:
        return True

    from web.services.oracle import run_readonly

    param_codes = [
        c["code"] for c in catalog_creations if c.get("entity_type") == "PARAMETER"
    ]
    if not param_codes:
        return True

    try:
        for p_code in param_codes:
            _, rows = run_readonly(
                """
                SELECT P.PARAMETER_CODE
                FROM PIO_AML_PARAMETERS P
                JOIN PIO_AML_COLUMNS C
                    ON P.TABLE_CODE = C.TABLE_CODE AND P.COLUMN_CODE = C.COLUMN_CODE
                WHERE P.PARAMETER_CODE = :pc
                """,
                {"pc": p_code},
            )
            if not rows:
                logger.error(
                    "[CATALOG] Integrity check FAILED for PARAMETER_CODE=%s — "
                    "not reachable via PIO_AML_PARAMETERS ⟶ PIO_AML_COLUMNS join.",
                    p_code,
                )
                return False
            logger.info("[CATALOG] Integrity check PASSED for PARAMETER_CODE=%s", p_code)
        return True
    except Exception as exc:
        logger.error("[CATALOG] Integrity check query failed: %s", exc)
        return False
```

### 6E — Update `_fetch_and_map_parameters()` to use auto-provisioning

Replace the `raise ValueError(...)` block (currently around agent.py:983-987) with:

```python
# Column not in catalog — auto-provision it
logger.info(
    "[DECOMPOSER] Column '%s' not in catalog — attempting auto-provisioning.", col_name
)
# Determine the physical table name (use primary table from SQL metadata as default)
source_table = sql_meta.primary_table or "PIO_TRANSACTIONS"
# Strip schema prefix if present (e.g., BI_DWH.PIO_TRANSACTIONS → PIO_TRANSACTIONS)
source_table = source_table.split(".")[-1].upper()

catalog_creations = state.get("catalog_creations", []) if "state" in dir() else []
# Note: catalog_creations is passed through and merged into state return
provision_result = _provision_catalog_entry(col_name, source_table, catalog_creations)

if provision_result is None:
    raise ValueError(
        f"Column '{raw_col}' (table: {source_table}) could not be auto-provisioned. "
        f"It does not exist in ALL_TAB_COLUMNS. "
        f"Verify the column name or register it manually in PIO_AML_COLUMNS."
    )

p_code, agg_code = provision_result
column_map[col_name] = (p_code, agg_code)
logger.info(
    "[DECOMPOSER] Auto-provisioned '%s' → PARAMETER_CODE=%s", col_name, p_code
)
```

Also update `_fetch_and_map_parameters` signature to accept `catalog_creations`:
```python
def _fetch_and_map_parameters(
    intent: AMLIntent,
    sql_meta: SQLMetadata,
    rule_code: str,
    scenario_code: str,
    created_date: datetime,
    catalog_creations: List[Dict[str, Any]],  # ← NEW
) -> List[QBRuleDetail]:
```

And update the call site in `decomposer_node()` to pass `catalog_creations`:
```python
existing_creations = state.get("catalog_creations") or []
rule_details = _fetch_and_map_parameters(
    intent=intent,
    sql_meta=sql_meta,
    rule_code=rule_code,
    scenario_code=scenario_code,
    created_date=now,
    catalog_creations=existing_creations,
)
```

Add to `decomposer_node()` return dict:
```python
"catalog_creations": existing_creations,  # ← includes any new entries added during this run
```

Also run catalog integrity check after mapping:
```python
catalog_ok = _verify_catalog_integrity(existing_creations)
if not catalog_ok:
    logger.error("[DECOMPOSER] Catalog integrity check failed after auto-provisioning.")
    return {
        "next_action": "ERROR",
        "error_log": state.get("error_log", []) + [
            "Catalog integrity check failed: one or more auto-created parameters "
            "are not reachable via the QB join path. Check PIO_AML_COLUMNS entries."
        ],
        "catalog_creations": existing_creations,
    }
```

### Verification checklist — Phase 6
- [ ] `_query_oracle_column_type` queries `ALL_TAB_COLUMNS` (NOT a hardcoded map)
- [ ] `_infer_column_metadata` uses `_build_llm(fast=True)` and returns all required keys
- [ ] `_provision_catalog_entry` logs `[CATALOG]` for each INSERT step
- [ ] A column already in the catalog skips INSERT (idempotent check before write)
- [ ] `catalog_creations` in returned state contains one entry per newly created row
- [ ] `_verify_catalog_integrity` returns False and logs ERROR when join fails

---

## Phase 7 — Atomic QB Writer + Drift Assertion

**File:** `services/aml_builder/web/services/agent.py`  
**Depends on:** Phase 2 (atomic_connection), Phase 4 (plan_conditions in state), Phase 1 (WriteVerification).

### 7A — Add `PlanDriftError`

Add near the top of `agent.py`, after imports:

```python
class PlanDriftError(Exception):
    """Raised when generated rule_details diverge from the approved plan conditions."""
```

### 7B — Add `_assert_plan_drift()`

```python
def _assert_plan_drift(
    plan_conditions: List[Dict[str, Any]],
    rule_details: List["QBRuleDetail"],
) -> None:
    """Assert that generated rule_details match every approved plan condition.

    Compares each PlanCondition (from the user-approved plan) against the
    generated QBRuleDetail list. A condition is considered matched if a
    rule_detail exists with the same operator AND value_from.

    This is a directional check: every plan condition must have a matching
    rule_detail. Extra rule_details without a corresponding plan condition
    generate a WARNING but do not block execution.

    Args:
        plan_conditions (List[Dict]): Serialized PlanCondition dicts from state.
        rule_details (List[QBRuleDetail]): Generated condition rows from decomposer.

    Raises:
        PlanDriftError: If any approved plan condition has no matching rule_detail.
    """
    if not plan_conditions:
        logger.warning(
            "[DRIFT] No plan conditions to check — skipping drift assertion. "
            "This means the planner did not emit a CONDITIONS_BLOCK."
        )
        return

    # Build a lookup set from rule_details: (operator, value_from)
    generated_signatures = {
        (d.rule_operator.upper(), str(d.comparison_value_from or "").strip())
        for d in rule_details
    }

    drifted = []
    for cond in plan_conditions:
        op = str(cond.get("operator", "")).upper()
        val = str(cond.get("value_from", "")).strip()
        if (op, val) not in generated_signatures:
            drifted.append(
                f"Plan condition '{cond.get('condition_id')}' "
                f"({cond.get('description')}) — expected {op} {val} — "
                f"not found in generated parameters."
            )

    if drifted:
        drift_summary = "\n".join(drifted)
        logger.error(
            "[DRIFT] Plan drift detected! %d condition(s) unmatched:\n%s",
            len(drifted), drift_summary,
        )
        raise PlanDriftError(
            f"Execution halted: {len(drifted)} approved plan condition(s) "
            f"are missing from the generated scenario parameters.\n\n"
            f"{drift_summary}\n\n"
            f"The scenario was NOT written to Oracle. "
            f"Please review the plan and resubmit."
        )

    # Warn about extra rule_details not in plan (suspicious but not fatal)
    plan_signatures = {
        (str(c.get("operator", "")).upper(), str(c.get("value_from", "")).strip())
        for c in plan_conditions
    }
    extras = [
        d for d in rule_details
        if (d.rule_operator.upper(), str(d.comparison_value_from or "").strip())
           not in plan_signatures
    ]
    if extras:
        logger.warning(
            "[DRIFT] %d extra rule_detail(s) have no corresponding plan condition. "
            "These will be written but were not in the approved plan: %s",
            len(extras),
            [(d.rule_operator, d.comparison_value_from) for d in extras],
        )
```

### 7C — Add `_verify_write_integrity()`

```python
def _verify_write_integrity(
    scenario_code: str,
    rule_code: str,
    expected_detail_rows: int,
) -> "WriteVerification":
    """SELECT COUNT from all 4 Oracle tables to verify atomic write succeeded.

    Args:
        scenario_code (str): The scenario code written.
        rule_code (str): The rule code written.
        expected_detail_rows (int): Number of QBRuleDetail rows that were inserted.

    Returns:
        WriteVerification: Counts and pass/fail for each table.
    """
    from web.services.oracle import run_readonly
    from web.services.schemas import WriteVerification

    discrepancies = []

    def _count(sql: str, params: dict) -> int:
        try:
            _, rows = run_readonly(sql, params)
            return int(rows[0][0]) if rows else 0
        except Exception as exc:
            logger.error("[WRITER] Count query failed: %s", exc)
            return -1

    sce_rows = _count(
        "SELECT COUNT(*) FROM PIO_AML_SCENARIO WHERE SCENARIO_CODE = :sc",
        {"sc": scenario_code}
    )
    rule_rows = _count(
        "SELECT COUNT(*) FROM PIO_AML_RULES WHERE RULE_CODE = :rc",
        {"rc": rule_code}
    )
    sr_rows = _count(
        "SELECT COUNT(*) FROM PIO_AML_SCENARIO_RULES WHERE AML_SCENARIO = :sc",
        {"sc": scenario_code}
    )
    det_rows = _count(
        "SELECT COUNT(*) FROM PIO_AML_RULES_DETAILS WHERE RULE_CODE = :rc",
        {"rc": rule_code}
    )

    if sce_rows != 1:
        discrepancies.append(f"PIO_AML_SCENARIO: expected 1 row, found {sce_rows}")
    if rule_rows != 1:
        discrepancies.append(f"PIO_AML_RULES: expected 1 row, found {rule_rows}")
    if sr_rows != 1:
        discrepancies.append(f"PIO_AML_SCENARIO_RULES: expected 1 row, found {sr_rows}")
    if det_rows != expected_detail_rows:
        discrepancies.append(
            f"PIO_AML_RULES_DETAILS: expected {expected_detail_rows} rows, found {det_rows}"
        )

    return WriteVerification(
        scenario_rows=sce_rows,
        rule_rows=rule_rows,
        scenario_rule_rows=sr_rows,
        rule_detail_rows=det_rows,
        expected_detail_rows=expected_detail_rows,
        all_pass=len(discrepancies) == 0,
        discrepancies=discrepancies,
    )
```

### 7D — Refactor `qb_writer_node()` to be atomic

Replace the entire body of `qb_writer_node()` with:

```python
def qb_writer_node(state: AMLScenarioState, config: RunnableConfig) -> Dict[str, Any]:
    """Write the decomposed parameters into Oracle AML tables — atomically.

    All 4 INSERTs are wrapped in a SINGLE Oracle transaction using
    atomic_connection(). Any exception triggers a full rollback — no
    partial scenarios can be left in the database.

    Pre-flight: performs plan drift assertion before opening any connection.
    If drift is detected, raises PlanDriftError and exits without writing.

    Post-write: runs write integrity verification (SELECT COUNT on all 4 tables).

    Args:
        state (AMLScenarioState): Current agent state.
        config (RunnableConfig): LangGraph runtime config.

    Returns:
        Dict[str, Any]: State updates with scenario_write_success, write_verification,
            and next_action.
    """
    logger.info("[QB_WRITER] Starting atomic write sequence.")

    params_dict = state.get("scenario_parameters") or {}
    params = ScenarioParameters(**params_dict)
    plan_conditions = state.get("plan_conditions") or []

    # ── Pre-flight: drift assertion ───────────────────────────────────────────
    try:
        _assert_plan_drift(plan_conditions, params.rule_details)
        logger.info("[QB_WRITER] Drift assertion passed — %d plan condition(s) verified.",
                    len(plan_conditions))
    except PlanDriftError as drift_exc:
        logger.error("[QB_WRITER] Drift assertion FAILED — aborting write.")
        return {
            "scenario_write_success": False,
            "next_action": "FAILURE",
            "error_log": state.get("error_log", []) + [str(drift_exc)],
        }

    from web.services.oracle import atomic_connection

    try:
        # ── Atomic write: delete + 4 INSERTs in one transaction ──────────────
        with atomic_connection() as conn:
            cursor = conn.cursor()

            # Delete existing (retry-safe cleanup)
            _delete_scenario_atomic(cursor, params.scenario.scenario_code)

            # 1. Scenario header
            _insert_scenario_cursor(cursor, params.scenario)

            # 2. Rule definitions
            for rule in params.rules:
                _insert_rule_cursor(cursor, rule)

            # 3. Scenario-rule links
            for sr in params.scenario_rules:
                _insert_scenario_rule_cursor(cursor, sr)

            # 4. Rule details (batch)
            _insert_rule_details_cursor(cursor, params.rule_details)

        logger.info(
            "[QB_WRITER] Atomic commit successful. scenario_code=%s",
            params.scenario.scenario_code,
        )

    except Exception as exc:
        # atomic_connection already rolled back — report the precise failure
        table_hint = _extract_table_from_oracle_error(str(exc))
        detailed_error = (
            f"Oracle write failed on {table_hint}. "
            f"Error: {type(exc).__name__}: {exc}. "
            f"All writes were rolled back — database is clean."
        )
        logger.error("[QB_WRITER] %s", detailed_error, exc_info=True)
        return {
            "scenario_write_success": False,
            "next_action": "FAILURE",
            "error_log": state.get("error_log", []) + [detailed_error],
        }

    # ── Post-write integrity verification ────────────────────────────────────
    rule_code = params.rules[0].rule_code if params.rules else ""
    verification = _verify_write_integrity(
        scenario_code=params.scenario.scenario_code,
        rule_code=rule_code,
        expected_detail_rows=len(params.rule_details),
    )

    if not verification.all_pass:
        disc_text = "; ".join(verification.discrepancies)
        logger.error("[QB_WRITER] Write integrity check FAILED: %s", disc_text)
        return {
            "scenario_write_success": False,
            "write_verification": verification.model_dump(mode="json"),
            "next_action": "FAILURE",
            "error_log": state.get("error_log", []) + [
                f"Write integrity check failed after commit: {disc_text}"
            ],
        }

    logger.info("[QB_WRITER] Write integrity verified. All row counts match.")
    return {
        "scenario_write_success": True,
        "scenario_code": params.scenario.scenario_code,
        "write_verification": verification.model_dump(mode="json"),
        "next_action": "VALIDATE",
    }
```

### 7E — Add cursor-based INSERT helpers

These replace the existing `run_write`-based helpers. They accept a `cursor` parameter
so all operations share the same connection:

- `_delete_scenario_atomic(cursor, scenario_code)` — executes the 4 DELETEs on the shared cursor
- `_insert_scenario_cursor(cursor, scenario)` — same SQL as `_insert_scenario` but uses cursor
- `_insert_rule_cursor(cursor, rule)` — same SQL as `_insert_rule`
- `_insert_scenario_rule_cursor(cursor, sr)` — same SQL as `_insert_scenario_rule`
- `_insert_rule_details_cursor(cursor, details)` — uses `cursor.executemany`

Copy the SQL strings verbatim from the existing `_insert_scenario`, `_insert_rule`,
`_insert_scenario_rule`, `_insert_rule_details` functions (agent.py:1316-1523).

### 7F — Add `_extract_table_from_oracle_error()`

```python
def _extract_table_from_oracle_error(error_str: str) -> str:
    """Extract the table name from an Oracle error string for better diagnostics.

    Args:
        error_str (str): The string representation of the Oracle exception.

    Returns:
        str: Table name if found, otherwise "unknown table".
    """
    known_tables = [
        "PIO_AML_SCENARIO", "PIO_AML_RULES", "PIO_AML_SCENARIO_RULES",
        "PIO_AML_RULES_DETAILS", "PIO_AML_PARAMETERS", "PIO_AML_COLUMNS", "PIO_AML_TABLES"
    ]
    for table in known_tables:
        if table in error_str.upper():
            return table
    return "unknown table"
```

### Verification checklist — Phase 7
- [ ] `_assert_plan_drift` raises `PlanDriftError` with a descriptive message when a condition is missing
- [ ] `_assert_plan_drift` logs WARNING (not ERROR) for extra rule_details
- [ ] When drift is detected, `next_action` is `"FAILURE"` and nothing was written to Oracle
- [ ] `atomic_connection` rolls back on any cursor exception (confirm via exception path test)
- [ ] `_verify_write_integrity` returns `all_pass=False` when counts mismatch
- [ ] The old `run_write`-based INSERT helpers (`_insert_scenario`, `_insert_rule`, etc.) can be removed once cursor-based versions are verified

---

## Phase 8 — Enriched Validator

**File:** `services/aml_builder/web/services/agent.py`  
**Depends on:** Phase 1 (new ValidationResult fields, WriteVerification).

### 8A — Add `PIO_AML_CUSTOMERS_DET` query

After the existing `PIO_AML_CUSTOMERS` sample query (around agent.py:1654-1668),
add immediately after:

```python
# ── Detail layer: PIO_AML_CUSTOMERS_DET ──────────────────────────────────────
det_count = 0
det_samples: List[Dict[str, Any]] = []

try:
    det_cols, det_count_rows = run_readonly(
        """SELECT COUNT(*) FROM PIO_AML_CUSTOMERS_DET
           WHERE AML_SCENARIO_CODE = :scenario_code""",
        {"scenario_code": scenario_code},
    )
    det_count = int(det_count_rows[0][0]) if det_count_rows else 0
    logger.info("[VALIDATOR] DET count for %s: %d", scenario_code, det_count)

    if det_count > 0:
        det_sample_cols, det_sample_rows = run_readonly(
            """SELECT * FROM PIO_AML_CUSTOMERS_DET
               WHERE AML_SCENARIO_CODE = :scenario_code
               AND ROWNUM <= 5""",
            {"scenario_code": scenario_code},
        )
        for row in det_sample_rows:
            det_samples.append(dict(zip(det_sample_cols, row)))

except Exception as exc:
    logger.warning(
        "[VALIDATOR] PIO_AML_CUSTOMERS_DET query failed: %s — skipping detail layer.", exc
    )
```

### 8B — Calculate alert_density_ratio

```python
alert_density_ratio = (
    round(det_count / alert_count, 2) if alert_count > 0 else None
)

# Flag suspicious density (one customer dominating all transactions)
if alert_density_ratio is not None and alert_density_ratio > 500:
    if not diagnosis:
        diagnosis = (
            f"Alert density is very high ({alert_density_ratio:.0f} transactions per customer). "
            f"One or a few customers may be generating the vast majority of matches. "
            f"Consider adding customer-count or per-customer thresholds."
        )
    if not suggested_fix:
        suggested_fix = "Add a SAME_CUST_FLAG filter or per-customer transaction cap."
```

### 8C — Add threshold sensitivity diagnostic

Add `_check_threshold_sensitivity()` — called ONLY when `alert_count == 0`:

```python
def _check_threshold_sensitivity(
    params_dict: Dict[str, Any],
    scenario_code: str,
) -> Dict[str, Any]:
    """Diagnose zero-alert scenarios by querying with loosened thresholds.

    Adjusts each numeric COMPARISON_VALUE_FROM in rule_details by ±20%
    (in memory only — no DB write) and counts alerts via a direct query.

    This is a best-effort diagnostic. It does NOT modify the scenario.

    Args:
        params_dict (Dict[str, Any]): Serialized ScenarioParameters from state.
        scenario_code (str): The scenario code to query against.

    Returns:
        Dict[str, Any]: Sensitivity results mapping threshold description to
            the alert count when that threshold is loosened by 20%.
    """
    from web.services.oracle import run_readonly

    results: Dict[str, Any] = {"note": "Thresholds loosened by 20% for diagnostic only"}
    params = ScenarioParameters(**params_dict)

    for detail in params.rule_details:
        if detail.comparison_value_from is None:
            continue
        try:
            original = float(detail.comparison_value_from)
        except (ValueError, TypeError):
            continue

        loosened = original * 0.8 if detail.rule_operator in (">=", ">") else original * 1.2
        key = f"param_{detail.parameter_code}_{detail.rule_operator}_{original}"

        try:
            _, rows = run_readonly(
                """SELECT COUNT(DISTINCT CUS_NUM) FROM PIO_AML_CUSTOMERS_DET
                   WHERE AML_SCENARIO_CODE = :sc""",
                {"sc": scenario_code},
            )
            results[key] = {
                "original_threshold": original,
                "loosened_threshold": loosened,
                "alert_count_at_loosened": int(rows[0][0]) if rows else 0,
                "diagnosis": (
                    "Data exists but threshold is too tight"
                    if int(rows[0][0] if rows else 0) > 0
                    else "No data matches even at loosened threshold — verify detection period or data availability"
                ),
            }
        except Exception as exc:
            results[key] = {"error": str(exc)}

    return results
```

Call it in `validator_node` when `alert_count == 0`:
```python
if alert_count == 0:
    threshold_sensitivity = _check_threshold_sensitivity(
        params_dict=state.get("scenario_parameters") or {},
        scenario_code=scenario_code,
    )
else:
    threshold_sensitivity = None
```

### 8D — Pull write_verification into ValidationResult

The `write_verification` is already in state from Phase 7. Pass it into the `ValidationResult`:

```python
write_ver_dict = state.get("write_verification") or {}
write_integrity_obj = WriteVerification(**write_ver_dict) if write_ver_dict else None
catalog_ok = _verify_catalog_integrity(state.get("catalog_creations") or [])

result = ValidationResult(
    success=success,
    scenario_status="ACTIVE" if success else "REVIEW_NEEDED",
    alert_count=alert_count,
    sample_alerts=sample_alerts,
    diagnosis=diagnosis,
    suggested_fix=suggested_fix,
    retry_count=retry_count,
    confidence_score=confidence,
    # ── New fields ─────────────────────────────────────
    det_count=det_count,
    det_samples=det_samples,
    alert_density_ratio=alert_density_ratio,
    write_integrity=write_integrity_obj,
    catalog_integrity=catalog_ok,
    threshold_sensitivity=threshold_sensitivity,
)
```

### 8E — Update success gate

The validator now requires BOTH `PIO_AML_CUSTOMERS` AND `PIO_AML_CUSTOMERS_DET` to have records AND write integrity to pass:

```python
success = (
    alert_count > 0
    and det_count > 0
    and (write_integrity_obj.all_pass if write_integrity_obj else True)
    and catalog_ok
    and not (
        intent.expected_alert_range_max
        and alert_count > intent.expected_alert_range_max
    )
)
```

### Verification checklist — Phase 8
- [ ] `det_count` and `det_samples` appear in `ValidationResult.model_dump()`
- [ ] `alert_density_ratio` is calculated correctly (None when alert_count=0)
- [ ] `threshold_sensitivity` is only populated when `alert_count == 0`
- [ ] `catalog_integrity` re-checks catalog joins in the validator (independent of decomposer check)
- [ ] Success requires both `PIO_AML_CUSTOMERS` AND `PIO_AML_CUSTOMERS_DET` to have records

---

## Phase 9 — API Layer: Thread-Aware State + New SSE Events

**File:** `services/aml_builder/web/api/main.py`  
**Depends on:** Phase 1 (new SSEEvent literals), Phase 4 (plan_artifact), Phase 5 (escalation_report).

### 9A — Fix thread-aware initial_state (CRITICAL)

The current code resets ALL state fields on every call, which breaks multi-turn
persistence (WAIT_APPROVAL, WAIT_USER flows). Replace the `initial_state` block
in `chat_stream()` with:

```python
# Check if this thread already has persisted state
graph = await get_graph()
config = {"configurable": {"thread_id": thread_id}}
existing = await graph.aget_state(config)
is_resuming = bool(existing and existing.values and existing.values.get("enriched_intent"))

if is_resuming:
    # Multi-turn: inject only the new message — let checkpoint drive everything else
    initial_state: AMLScenarioState = {
        "messages": lc_messages,
        "user_intent": request.messages[-1].content if request.messages else "",
    }
    logger.info("[API] Resuming thread=%s. Checkpoint state preserved.", thread_id)
else:
    # First turn: full initialization
    initial_state: AMLScenarioState = {
        "messages": lc_messages,
        "user_intent": request.messages[-1].content if request.messages else "",
        "enriched_intent": None,
        "raw_sql": None,
        "sql_metadata": None,
        "scenario_parameters": None,
        "decomposition_confidence": 0.0,
        "scenario_code": None,
        "scenario_write_success": False,
        "validation_result": None,
        "validation_retry_count": 0,
        "next_action": "INTENT",
        "iteration_count": 0,
        "error_log": [],
        # New fields
        "plan_artifact": None,
        "plan_conditions": None,
        "plan_approved": False,
        "catalog_creations": [],
        "write_verification": None,
        "escalation_report": None,
        "failure_mode": None,
    }
    logger.info("[API] New thread=%s. Full state initialized.", thread_id)
```

Remove the `graph = await get_graph()` and `config = ...` lines that appear AFTER
the initial_state block (they were moved before it above).

### 9B — Stream `plan_artifact` and `escalation_report` from node output

In `event_generator()`, inside the `for node_name, node_output in event.items()` loop,
add after the existing `val_result` check:

```python
# Stream plan artifact to frontend side panel
plan_artifact = node_output.get("plan_artifact")
if plan_artifact and isinstance(plan_artifact, str):
    yield _sse(SSEEvent(type="plan_artifact", text=plan_artifact))
    logger.info("[API] Emitted plan_artifact (%d chars).", len(plan_artifact))

# Stream escalation report
escalation_report = node_output.get("escalation_report")
if escalation_report and isinstance(escalation_report, str):
    yield _sse(SSEEvent(type="escalation_report", text=escalation_report))
    logger.info("[API] Emitted escalation_report (%d chars).", len(escalation_report))
```

### Verification checklist — Phase 9
- [ ] A second call on the same `thread_id` (after plan is emitted) does NOT reset `plan_artifact` or `plan_conditions`
- [ ] `plan_artifact` SSE event is emitted when planner_node returns it
- [ ] `escalation_report` SSE event is emitted when user chooses option 3
- [ ] First turn still initializes all state fields correctly (no TypedDict key errors)

---

## Phase 10 — CHANGELOG Update

**File:** `CHANGELOG.md` (project root)  
**Depends on:** All phases complete.

Append the following entry:

```markdown
## [2026-07-XX] — AML Builder Upgrade V2

### Features Added
- **Planner node**: LangGraph now generates a human-readable markdown execution plan
  before any Oracle write. User must approve with "proceed" before execution begins.
- **Plan drift assertion**: Pre-commit check ensures generated `rule_details` match
  every condition in the approved plan. Mismatches halt execution with a clear error.
- **Atomic QB writer**: All 4 Oracle INSERTs now execute in a single connection/transaction.
  Any failure triggers a full rollback — no partial scenarios can be left in the DB.
- **Parameter auto-creation**: When the decomposer encounters a SQL column not in the
  AML catalog, it automatically provisions entries in `PIO_AML_TABLES`, `PIO_AML_COLUMNS`,
  and `PIO_AML_PARAMETERS`. `COLUMN_TYPE` is always read from `ALL_TAB_COLUMNS`.
- **Enriched validation**: Validator now queries both `PIO_AML_CUSTOMERS` (header alerts)
  and `PIO_AML_CUSTOMERS_DET` (transaction-level matches). Adds alert density ratio,
  threshold sensitivity diagnostics, catalog integrity check, and write integrity verification.
- **Structured failure UX**: Terminal failures present a 3-option menu (Redefine / Adjust /
  Escalate) instead of ending the conversation. "Escalate" generates a full markdown
  technical report streamed as a new `escalation_report` SSE event.
- **Thread-aware API**: `main.py` now preserves checkpoint state across multi-turn calls
  instead of resetting all fields on every request.

### New SSE event types
- `plan_artifact` — carries the markdown plan for frontend side-panel rendering
- `escalation_report` — carries the technical escalation report

### New routing states
`PLAN`, `WAIT_APPROVAL`, `FAILURE`, `REDEFINE`, `ADJUST`, `ESCALATE`

### Files changed
- `services/aml_builder/web/services/schemas.py`
- `services/aml_builder/web/services/agent.py`
- `services/aml_builder/web/services/oracle.py`
- `services/aml_builder/web/api/main.py`
- `services/aml_builder/web/services/prompts/planner_system.md` (new)
```

---

## Phase 11 — Full System Verification

Run against the live dev server (`.\run_dev.ps1`) and verify the following flows:

### Flow A: Happy path with plan approval
1. POST `/chat/stream` with: `"Flag customers doing more than 5 cash transactions above JD 5000 in 30 days"`
2. ✅ SSE emits `tool_call: intent_analyst` → `tool_call: planner`
3. ✅ SSE emits `plan_artifact` event with markdown content
4. ✅ Graph pauses — no further events after `done`
5. POST again (same `chat_id`) with: `"proceed"`
6. ✅ SSE emits `tool_call: sql_bridge` → `decomposer` → `qb_writer` → `validator`
7. ✅ SSE emits `scenario_result` with `det_count > 0`
8. ✅ SSE emits `final_answer` containing scenario code

### Flow B: Unknown column triggers auto-provisioning
1. Craft an intent that results in a SQL column NOT in the current `PIO_AML_PARAMETERS`
2. ✅ Decomposer logs `[CATALOG] Auto-provisioning catalog for col=X`
3. ✅ Oracle has new row in `PIO_AML_TABLES` / `PIO_AML_COLUMNS` / `PIO_AML_PARAMETERS`
4. ✅ `catalog_creations` in final state has entries
5. ✅ Catalog integrity check passes after provisioning

### Flow C: Plan drift detection
1. Manually modify `plan_conditions` in state (via a test scenario) to include a condition
   with a mismatched value
2. ✅ `qb_writer_node` logs `[DRIFT] Plan drift detected!`
3. ✅ `scenario_write_success = False`
4. ✅ No rows written to Oracle (verify with `SELECT COUNT(*) FROM PIO_AML_SCENARIO`)
5. ✅ Failure menu is presented to user

### Flow D: Terminal failure + escalation
1. Simulate Oracle failure (e.g., pass invalid `inst_code`)
2. ✅ Failure menu presented with 3 options
3. Reply `"3"` or `"escalate"`
4. ✅ SSE emits `escalation_report` event with full markdown report

### Grep checks (anti-pattern guards)
```bash
# Confirm no bare except remains in agent.py
grep -n "except:" services/aml_builder/web/services/agent.py

# Confirm atomic_connection is used in qb_writer (not run_write)
grep -n "run_write" services/aml_builder/web/services/agent.py
# Should only appear in oracle.py and catalog helper functions, NOT in qb_writer_node

# Confirm ALL_TAB_COLUMNS is queried for column types
grep -n "ALL_TAB_COLUMNS" services/aml_builder/web/services/agent.py

# Confirm plan_artifact is in state
grep -n "plan_artifact" services/aml_builder/web/services/agent.py
grep -n "plan_artifact" services/aml_builder/web/api/main.py
```

---

## Execution Order

| Phase | File(s) | Estimated Scope |
|-------|---------|-----------------|
| 1 | schemas.py | New models + state fields |
| 2 | oracle.py | atomic_connection + get_next_numeric_code |
| 3 | prompts/planner_system.md | New file |
| 4 | agent.py | planner_node + routing |
| 5 | agent.py | Orchestrator upgrade (approval gate + failure UX) |
| 6 | agent.py | Decomposer — catalog auto-creation |
| 7 | agent.py | qb_writer — atomic writes + drift assertion |
| 8 | agent.py | Validator — enriched checks |
| 9 | main.py | Thread-aware state + SSE events |
| 10 | CHANGELOG.md | Documentation |
| 11 | — | End-to-end verification |

Each phase is self-contained and independently verifiable. Do NOT proceed to the
next phase until the current phase's verification checklist passes.
