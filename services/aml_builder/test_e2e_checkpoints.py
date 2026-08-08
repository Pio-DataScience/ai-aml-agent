"""
Integration Test for Checkpoint 1 (Domain Explanation Codes) and Checkpoint 3 (Direct Shadow Testing).
"""

import asyncio
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
    
    # Simulate turn 1 with "outward transfers"
    inputs = {
        "messages": [("user", "Flag outward transfers over 10,000 JD")],
        "iteration_count": 0,
        "error_log": [],
    }
    config = {"configurable": {"thread_id": "test_thread_checkpoint_1"}}
    
    state_output = await graph.ainvoke(inputs, config)
    logger.info("Graph execution paused at next_action: %s", state_output.get("next_action"))
    checkpoint = state_output.get("explanation_code_checkpoint")
    assert checkpoint is not None, "explanation_code_checkpoint is missing!"
    assert "PIO_EXPLANATION_CODE" in checkpoint
    logger.info("Checkpoint 1 explanation table generated cleanly!")

    print("\nCHECKPOINT 1 FLOW TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    test_shadow_executor_resilience()
    asyncio.run(test_checkpoint1_flow())
