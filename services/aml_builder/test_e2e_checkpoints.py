"""
Integration Test for Checkpoint 1 (Domain Explanation Codes) and Checkpoint 3 (Direct Shadow Testing).
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent))

from web.services.oracle import init_pool
from web.services.agent import get_graph, direct_shadow_executor_node

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_shadow_executor_resilience():
    logger.info("=== Testing Direct Shadow Executor Resilience ===")
    init_pool()
    
    # Test 1: Standard query with CUS_NUM
    state_1 = {"raw_sql": "SELECT CUS_NUM, TRA_AMT FROM BI_DWH.PIO_TRANSACTIONS WHERE TRA_AMT > 100000"}
    res_1 = direct_shadow_executor_node(state_1, None)
    assert res_1["validation_result"]["success"] is True
    logger.info("Shadow Test 1 Passed! Sample Customers: %d, Extrapolated: %d", res_1["validation_result"]["header_alert_count"], res_1["validation_result"]["header_alert_count"] * 20)

    # Test 2: Query with aliased CUSTOMER_ID column
    state_2 = {"raw_sql": "SELECT CUS_NUM AS CUSTOMER_ID, TRA_AMT FROM BI_DWH.PIO_TRANSACTIONS WHERE TRA_AMT > 500000"}
    res_2 = direct_shadow_executor_node(state_2, None)
    assert res_2["validation_result"]["success"] is True
    logger.info("Shadow Test 2 Passed! Dynamic entity column detected successfully.")

    # Test 3: CTE WITH query (Regression test for ORA-32034)
    cte_sql = """
    WITH Summary AS (
        SELECT CUS_NUM, SUM(TRA_AMT) AS TOT FROM BI_DWH.PIO_TRANSACTIONS WHERE ROWNUM <= 100 GROUP BY CUS_NUM
    )
    SELECT CUS_NUM, TOT FROM Summary
    """
    state_3 = {"raw_sql": cte_sql}
    res_3 = direct_shadow_executor_node(state_3, None)
    assert res_3["validation_result"]["success"] is True
    logger.info("Shadow Test 3 (CTE WITH query) Passed! No ORA-32034 error.")

    print("\nSHADOW EXECUTOR RESILIENCE TEST PASSED SUCCESSFULLY!")


async def test_checkpoint1_flow():
    logger.info("=== Testing Checkpoint 1 Explanation Code Flow in Graph ===")
    graph = await get_graph()
    
    # Simulate turn 1: User asks for scenario
    inputs = {
        "messages": [("user", "Flag outward transfers over 10,000 JD")],
        "iteration_count": 0,
        "error_log": [],
    }
    import uuid
    thread_id = f"test_thread_cp1_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    
    state_turn1 = await graph.ainvoke(inputs, config)
    logger.info("Turn 1 execution paused at next_action: %s", state_turn1.get("next_action"))
    checkpoint = state_turn1.get("explanation_code_checkpoint")
    assert checkpoint is not None, "explanation_code_checkpoint is missing in Turn 1!"
    
    enriched_intent_1 = state_turn1.get("enriched_intent") or {}
    explanation_codes_1 = enriched_intent_1.get("explanation_codes")
    logger.info("Turn 1 Discovered explanation_codes (%d items): %s", len(explanation_codes_1 or []), explanation_codes_1)
    assert explanation_codes_1 is not None and len(explanation_codes_1) > 0, "explanation_codes was NOT attached to enriched_intent in Turn 1!"

    # Simulate turn 2: User confirms codes
    inputs_turn2 = {
        "messages": [("user", "confirm codes")],
    }
    state_turn2 = await graph.ainvoke(inputs_turn2, config)
    logger.info("Turn 2 execution state: next_action=%s, confirmed=%s", state_turn2.get("next_action"), state_turn2.get("explanation_codes_confirmed"))
    assert state_turn2.get("explanation_codes_confirmed") is True, "explanation_codes_confirmed was NOT set to True in Turn 2!"
    
    enriched_intent_2 = state_turn2.get("enriched_intent") or {}
    explanation_codes_2 = enriched_intent_2.get("explanation_codes")
    logger.info("Turn 2 Confirmed explanation_codes in intent payload: %s", explanation_codes_2)
    assert explanation_codes_2 == explanation_codes_1, "Confirmed explanation codes were lost in Turn 2!"

    print("\nCHECKPOINT 1 FLOW TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    test_shadow_executor_resilience()
    import time
    time.sleep(5)
    asyncio.run(test_checkpoint1_flow())
