"""Advisory critique for generated DWH scenario SQL."""

from __future__ import annotations

import json
from typing import Any, Protocol

from services.aml_builder.web.domain.dwh_scenario.contracts import ExecutionEvidence
from services.aml_builder.web.services.dwh_scenario.prompt_loader import load_prompt


class Critic(Protocol):
    """Evaluate SQL using metadata-only execution evidence."""

    def invoke(self, messages: list[dict[str, str]]) -> Any: ...


def critique_candidate(
    critic: Critic, sql: str, evidence: ExecutionEvidence, findings: list[str]
) -> tuple[bool, str, int]:
    """Ask the configured critic for a bounded, advisory verdict.

    Args:
        critic: Role-configured critic model.
        sql: Candidate SQL under review.
        evidence: Columns, row count and sanitized execution error only.
        findings: Deterministic SQL-policy and intent findings.

    Returns:
        Approval boolean, concise repair instruction, and reported token count.
    """
    payload = {
        "sql": sql,
        "columns": evidence.columns,
        "row_count": evidence.row_count,
        "error": evidence.error,
        "deterministic_findings": findings,
    }
    response = critic.invoke(
        [
            {
                "role": "system",
                "content": load_prompt("critic_v1.md"),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
        ]
    )
    usage = getattr(response, "usage_metadata", {}) or {}
    tokens = int(usage.get("total_tokens", 0)) if isinstance(usage, dict) else 0
    content = getattr(response, "content", response)
    if isinstance(content, dict):
        verdict = content
    else:
        try:
            verdict = json.loads(str(content))
        except (TypeError, json.JSONDecodeError):
            return False, "Critic returned an invalid verdict", tokens
    approved = verdict.get("approved") is True
    feedback = verdict.get("feedback", "")
    return approved, str(feedback)[:2000], tokens
