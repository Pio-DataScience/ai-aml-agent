---
trigger: always_on
---

# System Prompt & Agent Instruction Engineering Standards

Whenever creating, editing, or refactoring system prompts, sub-agent instructions, or auditor checklists, you MUST strictly adhere to the following principles:

## 1. Abstract Placeholders over Hardcoded Literals (`<X>` Syntax)
- **ALWAYS** use generic, parametric placeholders (e.g. `<N_PERIODS>`, `<THRESHOLD_VALUE>`, `<COLUMN_NAME>`, `<TRANSACTION_TYPE>`, `<METRIC_A>`, `<METRIC_B>`).
- **NEVER** use real numbers, specific business codes, or concrete table/column names in system prompts or prompt examples (e.g., do NOT write `50000`, `6 months`, `1316`, or `PIO_TRANSACTIONS`).
- **Rationale**: Hardcoding concrete values biases the LLM toward memorizing specific numbers or table names, causing it to overfit or fail when encountering different business scenarios.

## 2. Rule Non-Contradiction & Single Source of Truth
- **NEVER** write a prompt rule that contradicts an existing prompt rule, database security constraint, or system invariant.
- Ensure that contract expectations between agents (e.g., Data Agent vs. Critique Auditor) are 100% harmonized.
- **Rationale**: Conflicting instructions create internal prompt friction, causing LLMs to hallucinate or stall in infinite retry loops.

## 3. High-Level Architectural Directing (No Micromanagement)
- Direct the LLM using universal engineering principles (e.g., Dimensional Analysis, Staged CTE Architecture, Intent Contract Diffing).
- Avoid forcing rigid multi-level forms or over-specified templates inside reasoning scratchpads (like the `think` tool).
- **Rationale**: Over-constrained templates degrade the LLM's natural reasoning capacity and burn context tokens on formatting rather than problem-solving.

## 4. Strict Role Partitioning (No Domain Overlap)
- In multi-agent or multi-auditor setups, partition responsibilities so that each agent owns a single, non-overlapping evaluation domain.
- Ensure that Auditor A (e.g., Completeness) diffs intent inputs, Auditor B (e.g., Logic & Math) checks formulas/booleans, and Auditor C (e.g., DWH Mechanics) checks execution & performance.
- **Rationale**: Clear domain boundaries eliminate redundant LLM calls and prevent contradictory audit verdicts.