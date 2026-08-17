"""
API Routes for the Standalone AML Alert Execution Engine.

Allows triggering scenario evaluation on-demand and inspecting execution status.
"""

from datetime import datetime
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.aml_builder.web.services.alert_engine import (
    get_active_scenarios,
    run_scenario_alert_engine,
)

router = APIRouter(prefix="/engine", tags=["Alert Engine"])


class RunEngineRequest(BaseModel):
    scenario_id: Optional[str] = Field(
        None, description="Optional specific scenario ID to execute."
    )
    evaluation_date: Optional[str] = Field(
        None, description="Optional target date in YYYY-MM-DD format."
    )


@router.post("/run-scenarios")
async def trigger_engine_run(request: RunEngineRequest) -> Dict[str, Any]:
    """Trigger the AML alert execution engine synchronously or for a specific scenario."""
    eval_date = None
    if request.evaluation_date:
        try:
            eval_date = datetime.strptime(request.evaluation_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid date format '{request.evaluation_date}'. Expected YYYY-MM-DD.",
            )

    try:
        summary = run_scenario_alert_engine(
            scenario_id=request.scenario_id, eval_date=eval_date
        )
        return summary
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/active-scenarios")
async def list_active_scenarios() -> Dict[str, Any]:
    """List all scenarios currently registered and active for execution."""
    scenarios = get_active_scenarios()
    return {
        "count": len(scenarios),
        "scenarios": [
            {
                "scenario_id": sc["scenario_id"],
                "scenario_name": sc["scenario_name"],
                "scenario_type": sc["scenario_type"],
                "degree_risk": sc["degree_risk"],
                "category_code": sc["category_code"],
                "violation_level": sc["violation_level"],
                "time_window_days": sc["time_window_days"],
            }
            for sc in scenarios
        ],
    }
