"""Registry-backed conflict checking with deterministic policy decisions."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from services.aml_builder.web.domain.compare import compare_signatures
from services.aml_builder.web.domain.hashing import stable_hash
from services.aml_builder.web.domain.signature import build_signature

logger = logging.getLogger(__name__)

_SEVERITY = {"IDENTICAL": 0, "COVERED": 1, "COVERS": 1, "OVERLAP": 2, "RELATED": 3, "SIMILAR": 4}


class ConflictChecker:
    """Compare a draft signature with active registry signatures.

    Args:
        registry_repo: Read-only registry adapter exposing active signatures.
        policy: Validated conflict policy catalog or mapping.
        semantic_search: Optional legacy semantic-search function for rows without signatures.
    """

    def __init__(
        self,
        registry_repo: Any,
        policy: Any,
        semantic_search: Callable[[str, int], dict[str, Any]] | None = None,
        workflow_repo: Any = None,
    ) -> None:
        self.registry_repo = registry_repo
        self.policy = _mapping(policy)
        self.semantic_search = semantic_search
        self.workflow_repo = workflow_repo

    def check_intent(
        self,
        intent: dict[str, Any],
        *,
        thread_id: str | None = None,
        scenario_key: str | None = None,
        revision_id: str | None = None,
        provisional: bool = False,
    ) -> dict[str, Any]:
        """Build and compare an intent signature, returning an unavailable result on read failure.

        Args:
            intent: Candidate intent, confirmed or provisional.
            thread_id: Workflow thread to exclude from the registry scan.
            scenario_key: Stable key whose parent versions should be excluded except no-op copies.
            revision_id: Draft revision identifier, if one exists.
            provisional: Whether this is an early, non-authoritative hint.

        Returns:
            UI-ready conflict report with typed relations and a stable conflict hash.
        """
        signature, _ = build_signature(intent)
        return self.check_signature(
            signature,
            intent=intent,
            thread_id=thread_id,
            scenario_key=scenario_key,
            revision_id=revision_id,
            provisional=provisional,
        )

    def check(
        self,
        thread_id: str,
        revision_id: str,
        *,
        scenario_key: str | None = None,
    ) -> dict[str, Any]:
        """Implement the deployment conflict-gate protocol from stored revision state.

        Args:
            thread_id: Workflow thread identifier.
            revision_id: Exact current revision to compare.
            scenario_key: Optional stable key override.

        Returns:
            Authoritative report, or an unavailable report when registry reads fail.
        """
        workflow_repo = getattr(self, "workflow_repo", None)
        if workflow_repo is None:
            return _unavailable(revision_id, False)
        workflow = workflow_repo.get_workflow(thread_id)
        revision = workflow_repo.get_revision(revision_id)
        if workflow is None or revision is None:
            return _unavailable(revision_id, False)
        try:
            signature = json.loads(revision.signature_json or "null")
            card = json.loads(revision.card_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return _unavailable(revision_id, False)
        intent = card.get("intent") if isinstance(card, dict) else None
        if not isinstance(signature, dict) or not isinstance(intent, dict):
            return _unavailable(revision_id, False)
        return self.check_signature(
            signature,
            intent=intent,
            thread_id=thread_id,
            scenario_key=scenario_key or workflow.scenario_key,
            revision_id=revision_id,
            provisional=False,
        )

    def check_signature(
        self,
        signature: dict[str, Any],
        *,
        intent: dict[str, Any] | None = None,
        thread_id: str | None = None,
        scenario_key: str | None = None,
        revision_id: str | None = None,
        provisional: bool = False,
    ) -> dict[str, Any]:
        """Compare a canonical signature with live active registry rows.

        Args:
            signature: Candidate canonical signature.
            intent: Source intent used for legacy semantic fallback.
            thread_id: Workflow thread excluded from comparisons.
            scenario_key: Stable key excluded except for identical no-op copies.
            revision_id: Draft revision identifier.
            provisional: Whether this result is an early hint only.

        Returns:
            Conflict report containing blocking and acknowledgement flags.
        """
        scenario_type = str(signature.get("scenario_type") or "")
        try:
            rows = self.registry_repo.list_active_signatures(scenario_type or None)
        except Exception as exc:
            logger.warning("Conflict registry read failed: %s", exc)
            return _unavailable(revision_id, provisional)
        matches: list[dict[str, Any]] = []
        legacy_ids: set[str] = set()
        for row in rows:
            row_thread = str(row.get("thread_id") or "")
            row_key = str(row.get("scenario_key") or "")
            scenario_id = str(row.get("scenario_id") or "")
            if thread_id and row_thread == thread_id:
                continue
            existing = _parse_signature(row.get("signature_json"))
            if existing is None:
                if scenario_key and row_key == scenario_key:
                    continue
                if scenario_id:
                    legacy_ids.add(scenario_id)
                continue
            result = compare_signatures(
                signature,
                existing,
                dimension_weights=self.policy.get("dimension_weights"),
                window_mismatch_relation=self._options().get("window_mismatch_relation", "RELATED"),
            )
            if result.relation is None:
                continue
            same_key = bool(scenario_key and row_key == scenario_key)
            if same_key and result.relation != "IDENTICAL":
                continue
            if not same_key and result.relation == "IDENTICAL" and row_key == "":
                pass
            relation = result.relation
            action = "block" if same_key else self._relation_action(relation)
            matches.append(
                self._match(row, relation, result.diff, result.similarity_pct, action)
            )
        matches.extend(self._legacy_matches(intent, legacy_ids))
        matches.sort(key=lambda item: (_SEVERITY.get(item["relation"], 9), -item["similarity_pct"], item["scenario_id"]))
        matches = matches[: int(self.policy.get("max_matches_shown", 5))]
        blocking = any(item["action"] == "block" for item in matches)
        needs_ack = any(item["action"] == "warn_confirm" for item in matches)
        status = "blocked" if blocking else "needs_ack" if needs_ack else "clear"
        report = {
            "revision_id": revision_id,
            "provisional": provisional,
            "status": status,
            "blocking": blocking,
            "needs_ack": needs_ack,
            "matches": matches,
        }
        report["conflict_hash"] = stable_hash(matches)
        return report

    def _relation_action(self, relation: str) -> str:
        value = self.policy.get("relations", {}).get(relation, "info")
        return str(value)

    def _options(self) -> dict[str, Any]:
        return self.policy.get("conflict_check", {})

    def _match(
        self,
        row: dict[str, Any],
        relation: str,
        diff: list[dict[str, Any]],
        similarity_pct: int,
        action: str,
    ) -> dict[str, Any]:
        options = self._options()
        changed = [item for item in diff if not item["same"]]
        if changed:
            diff_text = "; ".join(
                options.get("diff_changed_template", "{dimension}: new {new}; existing {existing}").format(
                    dimension=item["dimension"].replace("_", " "),
                    new=_display(item.get("new")),
                    existing=_display(item.get("existing")),
                )
                for item in changed
            )
        else:
            diff_text = options.get("diff_same_template", "{dimension}: {new}").format(
                dimension="signature", new="all compared dimensions match"
            )
        return {
            "scenario_id": str(row.get("scenario_id") or ""),
            "name": str(row.get("scenario_name") or ""),
            "version": row.get("version_no"),
            "relation": relation,
            "similarity_pct": max(0, min(100, int(similarity_pct))),
            "diff": diff,
            "diff_text": diff_text,
            "action": action,
        }

    def _legacy_matches(self, intent: dict[str, Any] | None, legacy_ids: set[str]) -> list[dict[str, Any]]:
        if not intent or not legacy_ids or self.semantic_search is None:
            return []
        query = str(intent.get("detection_logic") or intent.get("scenario_name") or "")
        if not query:
            return []
        try:
            result = self.semantic_search(query, int(self.policy.get("max_matches_shown", 5)))
        except Exception as exc:
            logger.warning("Legacy semantic conflict fallback failed: %s", exc)
            return []
        minimum = float(self.policy.get("similar_min_score", 0.85))
        matches = []
        for row in result.get("results", []):
            score = float(row.get("similarity_score") or 0)
            scenario_id = str(row.get("scenario_id") or "")
            if scenario_id not in legacy_ids or score < minimum or not row.get("is_active"):
                continue
            matches.append({
                "scenario_id": scenario_id,
                "name": str(row.get("scenario_name") or ""),
                "version": row.get("version_no"),
                "relation": "SIMILAR",
                "similarity_pct": max(0, min(100, round(score * 100))),
                "diff": [],
                "diff_text": "Semantic match to a legacy scenario without a usable signature.",
                "action": "info",
            })
        return matches


def _mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    return value if isinstance(value, dict) else {}


def _parse_signature(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        signature = value
    else:
        try:
            signature = json.loads(value) if value else None
        except (json.JSONDecodeError, TypeError):
            return None
    required = {"scenario_type", "grain", "metric", "aggregation", "window", "comparator", "threshold", "transaction_scope", "filters"}
    return signature if isinstance(signature, dict) and required <= signature.keys() else None


def _unavailable(revision_id: str | None, provisional: bool) -> dict[str, Any]:
    return {
        "revision_id": revision_id,
        "provisional": provisional,
        "status": "unavailable",
        "conflict_hash": None,
        "blocking": False,
        "needs_ack": False,
        "warning": "The active scenario registry could not be checked.",
        "matches": [],
    }


def _display(value: Any) -> str:
    if value is None:
        return "all values"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def has_comparable_core(intent: dict[str, Any]) -> bool:
    """Return whether an extracted intent contains enough resolved dimensions for a useful hint.

    Args:
        intent: Extracted or confirmed intent mapping.

    Returns:
        True when type, grain, metric, aggregation, comparator, threshold, and window kind exist.
    """
    aggregation = intent.get("aggregation") if isinstance(intent.get("aggregation"), dict) else {}
    thresholds = intent.get("thresholds") if isinstance(intent.get("thresholds"), list) else []
    threshold = next((item for item in thresholds if isinstance(item, dict)), {})
    window = intent.get("time_window") if isinstance(intent.get("time_window"), dict) else {}
    return bool(
        intent.get("scenario_type")
        and aggregation.get("grain")
        and threshold.get("field")
        and aggregation.get("function")
        and threshold.get("operator")
        and (threshold.get("value_from") is not None or threshold.get("value") is not None)
        and window.get("unit")
        and window.get("value")
    )


def make_conflict_checker(workflow_repo: Any = None) -> ConflictChecker:
    """Build the configured checker with read-only Oracle and legacy-search adapters.

    Args:
        workflow_repo: Optional SQLite repository used by the deploy gate.

    Returns:
        Runtime conflict checker configured from the validated policy catalog.
    """
    from services.aml_builder.web.adapters.oracle_repo import OracleRegistryRepo
    from services.aml_builder.web.services.config_loader import load_catalogs
    from services.aml_builder.web.services.scenario_registry_analytics import search_scenarios_semantic
    from services.aml_builder.web.services.settings import settings

    policy = load_catalogs(settings.CATALOG_DIR).policy
    return ConflictChecker(
        OracleRegistryRepo(),
        policy,
        semantic_search=search_scenarios_semantic,
        workflow_repo=workflow_repo,
    )
