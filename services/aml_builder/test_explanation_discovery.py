"""
Unit Test for Vector Similarity Search & Sub-LLM Reranker on PIO_EXPLANATION_CODE.
"""

import logging
import sys
from pathlib import Path

# Fix Windows console UTF-8 printing
sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, str(Path(__file__).parent))

from web.services.explanation_code_search import format_explanation_code_checkpoint, select_relevant_explanation_codes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_explanation_code_discovery():
    logger.info("=== Testing PIO_EXPLANATION_CODE RAG Vector Discovery ===")
    matches = select_relevant_explanation_codes("OUTWARD TRANSFERS")
    assert len(matches) > 0, "No explanation codes discovered!"
    
    logger.info("Discovered %d relevant explanation codes:", len(matches))
    for m in matches:
        logger.info("  Code: %s | Description: %s | Relevance: %s", m.get("code"), m.get("description"), m.get("relevance"))

    formatted_md = format_explanation_code_checkpoint("OUTWARD TRANSFERS", matches)
    assert "| Code | Description | Match % | Relevance | Reason |" in formatted_md
    logger.info("Formatted Checkpoint Table View:\n%s", formatted_md)
    print("\nEXPLANATION CODE DISCOVERY TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    test_explanation_code_discovery()
