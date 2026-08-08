"""
End-to-End Integration Test for Standalone Production SQL Engine & HITL Flow.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add aml_builder root to path
sys.path.insert(0, str(Path(__file__).parent))

from web.services.oracle import init_pool, run_readonly
from web.services.production_registry import get_production_scenarios, init_production_scenarios_table, save_production_scenario

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_production_registry():
    """Verify table creation and CRUD operations in PIO_AML_PRODUCTION_SCENARIOS."""
    logger.info("=== 1. Testing Production Registry ===")
    init_pool()
    init_production_scenarios_table()

    test_id = "TEST_E2E_001"
    save_success = save_production_scenario(
        scenario_id=test_id,
        scenario_name="Smurfing Minors Outward Transfers > 5k",
        scenario_type="CUSTOMER",
        detection_logic="Flag minor accounts with multiple outward transfers exceeding 5k",
        raw_sql="SELECT C.CUS_NUM, COUNT(*) AS TX_COUNT, SUM(T.TRA_AMT) AS TOTAL_AMT FROM BI_DWH.PIO_CUSTOMERS C JOIN BI_DWH.PIO_TRANSACTIONS T ON C.CUS_NUM = T.CUS_NUM WHERE T.TRA_AMT > 5000 GROUP BY C.CUS_NUM HAVING COUNT(*) >= 3",
        time_window_days=30,
        created_by="COMPLIANCE_OFFICER_TEST",
    )
    assert save_success is True, "Failed to save production scenario"

    scenarios = get_production_scenarios(is_active_only=True)
    saved = next((s for s in scenarios if s["scenario_id"] == test_id), None)
    assert saved is not None, "Saved scenario not found in registry"
    assert saved["scenario_name"] == "Smurfing Minors Outward Transfers > 5k"
    logger.info("Production registry test PASSED cleanly!")

    # Clean up test row
    from web.services.oracle import run_write
    run_write("DELETE FROM PIO_AML_PRODUCTION_SCENARIOS WHERE SCENARIO_ID = :sid", {"sid": test_id})
    logger.info("Cleaned up test row.")


def test_graph_node_compilation():
    """Verify LangGraph graph structure with new standalone nodes."""
    logger.info("=== 2. Testing Graph Node Compilation ===")
    from web.services.agent import get_graph
    graph = asyncio.run(get_graph())
    nodes = list(graph.nodes.keys())
    logger.info("Compiled graph nodes: %s", nodes)
    
    assert "orchestrator" in nodes
    assert "intent_analyst" in nodes
    assert "planner" in nodes
    assert "sql_bridge" in nodes
    assert "direct_shadow_executor" in nodes
    assert "production_scenario_persister" in nodes
    
    # Assert legacy nodes are removed
    assert "decomposer" not in nodes
    assert "qb_writer" not in nodes
    assert "validator" not in nodes
    logger.info("Graph node compilation test PASSED cleanly!")


if __name__ == "__main__":
    test_production_registry()
    test_graph_node_compilation()
    print("ALL TESTS PASSED SUCCESSFULLY!")
