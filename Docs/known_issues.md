# Temporary Disables & Active Code Modifications

This document contains a log of all active code modifications and system disables made to the **PioTech DWH & AML Agent** systems, detailing how to safely revert them.

---

## 1. Intent Classification (Disabled)
* **File:** [intent_library.py](file:///C:/Users/abura/Development/PioTech-AI/shared/services/intent_library.py)
* **Modification:** Added a short-circuit return at the start of the `classify_intent_with_llm` function.
* **Why:** Suspended intent templates checking to reduce LLM overhead and query matching complexity.
* **How to Revert:**
  Remove the first two lines inside `classify_intent_with_llm` (lines 578-579):
  ```python
  # Safely disabled for now
  return None
  ```

---

## 2. Submit Background Report Tool (Disabled)
* **File:** [agent.py](file:///C:/Users/abura/Development/PioTech-AI/services/dwh/web/services/agent.py)
* **Modification:** Commented out `submit_background_report` in the agent's `tools` list (around line 2851).
* **Why:** Stopped the agent from running long-running queries in Celery/Redis backgrounds.
* **How to Revert:**
  Uncomment the tool line:
  ```python
  # Change:
  # submit_background_report,
  # To:
  submit_background_report,
  ```

---

## 3. Query Cost Safety Limit Check (Bypassed)
* **File:** [agent.py](file:///C:/Users/abura/Development/PioTech-AI/services/dwh/web/services/agent.py)
* **Modification:** Bypassed the query cost estimation check gate inside `execute_sql_query` by changing the safety check to `if False: pass` (around line 880).
* **Why:** Since background reporting is off, the agent must be allowed to execute heavy queries directly without encountering `QUERY_TOO_EXPENSIVE` errors.
* **How to Revert:**
  Restore the safety condition check:
  ```python
  # Change:
  if False:  # not estimate_result.is_safe:
      pass
  # To:
  if not estimate_result.is_safe:
  ```

---

## 4. Critique Agent Graph Integration (Active)
* **File:** [agent.py](file:///C:/Users/abura/Development/PioTech-AI/services/dwh/web/services/agent.py)
* **Modification:** Added `call_critique_llm_node`, `critique_node`, and `route_after_critique` to the file, and registered the node and conditional edges in `data_agent_graph` (lines 3280-3320).
* **Why:** Forces all completed queries and answers to undergo business logic verification and auto-corrections.
* **How to Revert:**
  1. Restore `should_continue` to return `END` instead of `"critique"`:
     ```python
     # Change:
     return "critique"
     # To:
     return END
     ```
  2. Remove `"critique"` node additions and conditional edges from `data_agent_graph`:
     ```python
     # Remove these lines:
     data_agent_graph.add_node("critique", critique_node)
     ...
     data_agent_graph.add_conditional_edges("critique", route_after_critique, ...)
     ```

---

## 5. DWH Prompts Overrides (Active)
* **Files:**
  - [supervisor_system.md](file:///C:/Users/abura/Development/PioTech-AI/services/dwh/web/services/prompts/supervisor_system.md) (Added `AML SCENARIOS OVERRIDE`)
  - [data_agent_system.md](file:///C:/Users/abura/Development/PioTech-AI/services/dwh/web/services/prompts/data_agent_system.md) (Added `AML SCENARIO REQUESTS (CRITICAL OVERRIDE)`)
  - [critique_agent_system.md](file:///C:/Users/abura/Development/PioTech-AI/services/dwh/web/services/prompts/critique_agent_system.md) (Added `Stalker boundaries / non-micromanaging rule`)
* **How to Revert:**
  Delete the overrides sections at the top/bottom of the respective markdown prompt files.

---

## 6. Join Loop Prevention Hint (Active)
* **File:** [agent.py](file:///C:/Users/abura/Development/PioTech-AI/services/dwh/web/services/agent.py)
* **Modification:** Changed exception hint in `lookup_standard_join` to: `"Database lookup failed. Do NOT retry..."` (around line 1604).
* **How to Revert:**
  Restore the default exception hint:
  ```python
  # Change:
  hint="Database lookup failed. Do NOT retry this tool..."
  # To:
  hint=f"Failed to lookup join between {table_a} and {table_b}. Check table names."
  ```
