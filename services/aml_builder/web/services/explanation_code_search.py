"""
Explanation Code Similarity Search & RAG Discovery Module.

Vector similarity search over Oracle DWH PIO_EXPLANATION_CODE table (6,900+ rows)
with lightweight 1-liner sub-LLM reranking & interactive table view formatting.
"""

import json
import logging
import os
import pickle
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from langchain_openai import OpenAIEmbeddings

from web.services.agent import _build_llm
from web.services.oracle import run_readonly
from web.services.settings import settings

logger = logging.getLogger(__name__)

CACHE_DIR = Path("artifacts")
CACHE_FILE = CACHE_DIR / "explanation_codes_vector_cache.pkl"

_VECTOR_CACHE: Optional[Dict[str, Any]] = None


def _load_or_build_vector_index() -> Dict[str, Any]:
    """Load or build persistent vector embeddings for PIO_EXPLANATION_CODE."""
    global _VECTOR_CACHE
    if _VECTOR_CACHE is not None:
        return _VECTOR_CACHE

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "rb") as f:
                _VECTOR_CACHE = pickle.load(f)
            logger.info(
                "[EXPL_SEARCH] Loaded %d explanation codes from vector cache.",
                len(_VECTOR_CACHE["metadata"]),
            )
            return _VECTOR_CACHE
        except Exception as exc:
            logger.warning("[EXPL_SEARCH] Failed to load vector cache: %s. Rebuilding...", exc)

    logger.info("[EXPL_SEARCH] Building vector index for PIO_EXPLANATION_CODE from Oracle DWH...")
    from web.services.oracle import init_pool, run_readonly

    try:
        init_pool()
    except Exception:
        pass

    sql = """
    SELECT EXPLANATION_CODE, DESC_ENG, DESC_NAT_LAN, LONG_DES_ENG
    FROM PIO_EXPLANATION_CODE
    WHERE EXPLANATION_CODE IS NOT NULL
    """
    _, rows = run_readonly(sql)
    if not rows:
        logger.error("[EXPL_SEARCH] No explanation codes found in PIO_EXPLANATION_CODE!")
        return {"embeddings": np.array([]), "metadata": []}

    metadata = []
    texts_to_embed = []
    for r in rows:
        code = str(r[0]).strip()
        desc = str(r[1]).strip() if r[1] else ""
        desc_nat = str(r[2]).strip() if r[2] else ""
        long_desc = str(r[3]).strip() if r[3] else ""

        full_text = f"Code {code}: {desc}. {long_desc} {desc_nat}".strip()
        metadata.append({
            "code": code,
            "desc_eng": desc,
            "desc_nat": desc_nat,
            "long_desc": long_desc,
        })
        texts_to_embed.append(full_text)

    logger.info("[EXPL_SEARCH] Embedding %d explanation codes...", len(texts_to_embed))
    emb = OpenAIEmbeddings(api_key=settings.OPENAI_API_KEY)
    
    # Embed in batches of 500
    batch_size = 500
    all_embeddings = []
    for i in range(0, len(texts_to_embed), batch_size):
        batch = texts_to_embed[i : i + batch_size]
        batch_vecs = emb.embed_documents(batch)
        all_embeddings.extend(batch_vecs)

    embeddings_matrix = np.array(all_embeddings, dtype=np.float32)
    # Normalize vectors for cosine similarity (dot product)
    norms = np.linalg.norm(embeddings_matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized_matrix = embeddings_matrix / norms

    _VECTOR_CACHE = {
        "embeddings": normalized_matrix,
        "metadata": metadata,
    }

    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(_VECTOR_CACHE, f)
        logger.info("[EXPL_SEARCH] Persistent vector cache saved to %s.", CACHE_FILE)
    except Exception as exc:
        logger.error("[EXPL_SEARCH] Failed to save vector cache file: %s", exc)

    return _VECTOR_CACHE


def search_explanation_candidates(query_text: str, top_k: int = 100) -> List[Dict[str, Any]]:
    """Vector similarity search returning top-k candidate explanation codes.

    Args:
        query_text (str): Natural language transaction type query (e.g. 'OUTWARD TRANSFERS').
        top_k (int): Number of nearest neighbors to retrieve. Defaults to 100.

    Returns:
        List[Dict[str, Any]]: Top-k metadata records sorted by similarity score.
    """
    index = _load_or_build_vector_index()
    matrix = index["embeddings"]
    metadata = index["metadata"]

    if len(metadata) == 0:
        return []

    emb = OpenAIEmbeddings(api_key=settings.OPENAI_API_KEY)
    q_vec = np.array(emb.embed_query(query_text), dtype=np.float32)
    q_norm = np.linalg.norm(q_vec)
    if q_norm > 0:
        q_vec /= q_norm

    # Cosine similarity dot product
    scores = np.dot(matrix, q_vec)
    top_indices = np.argsort(scores)[::-1][:top_k]

    candidates = []
    for idx in top_indices:
        item = dict(metadata[idx])
        item["score"] = float(scores[idx])
        candidates.append(item)

    return candidates


def select_relevant_explanation_codes(transaction_type: str) -> List[Dict[str, Any]]:
    """Standalone sub-LLM call to filter & rank top 100 vector candidates for a transaction_type.

    Uses a fast 1-liner prompt with LLM_MODEL_FAST (gpt-4o-mini).

    Args:
        transaction_type (str): The extracted transaction category.

    Returns:
        List[Dict[str, Any]]: Filtered, highly relevant explanation codes.
    """
    candidates = search_explanation_candidates(transaction_type, top_k=100)
    if not candidates:
        return []

    # Compact representation of top candidates
    candidate_lines = []
    for c in candidates[:60]:  # Top 60 for token efficiency
        candidate_lines.append(f"Code: {c['code']} | Description: {c['desc_eng']}")

    candidates_text = "\n".join(candidate_lines)

    prompt = (
        "You are an expert AML database classifier.\n"
        f"User Goal / Transaction Type: '{transaction_type}'\n\n"
        "Analyze the candidate explanation codes from the bank DWH catalog (PIO_EXPLANATION_CODE) below. "
        "Select up to 8 most relevant explanation codes that correspond directly to this transaction type.\n\n"
        f"CANDIDATES:\n{candidates_text}\n\n"
        "Respond ONLY with a valid JSON array of objects. Do not write any markdown fences, code blocks, or text:\n"
        '[\n'
        '  {"code": "<EXPLANATION_CODE>", "description": "<DESC_ENG>", "relevance": "High|Medium", "reason": "<brief reason>"}\n'
        ']'
    )

    try:
        llm = _build_llm(fast=True)
        resp = llm.invoke(prompt)
        text = str(resp.content).strip()

        # Clean JSON fences if present
        match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
        if match:
            text = match.group(1).strip()
        else:
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end != -1:
                text = text[start : end + 1].strip()

        parsed = json.loads(text)
        logger.info(
            "[EXPL_SEARCH] Sub-LLM selected %d explanation codes for '%s'.",
            len(parsed),
            transaction_type,
        )
        return parsed
    except Exception as exc:
        logger.error(
            "[EXPL_SEARCH] Sub-LLM reranking failed: %s. Returning top vector candidates.",
            exc,
        )
        fallback = []
        for c in candidates[:5]:
            fallback.append({
                "code": c["code"],
                "description": c["desc_eng"],
                "relevance": "High" if c["score"] > 0.7 else "Medium",
                "reason": "Top vector similarity match",
            })
        return fallback


def format_explanation_code_checkpoint(transaction_type: str, matches: List[Dict[str, Any]]) -> str:
    """Format discovered explanation codes into a clean interactive markdown table view.

    Args:
        transaction_type (str): The transaction category name.
        matches (List[Dict[str, Any]]): Relevant explanation codes.

    Returns:
        str: Formatted markdown checkpoint string.
    """
    if not matches:
        return ""

    table_rows = []
    for m in matches:
        code = m.get("code", "")
        desc = m.get("description", "")
        rel = m.get("relevance", "High")
        reason = m.get("reason", "")
        table_rows.append(f"| **`{code}`** | {desc} | **{rel}** | {reason} |")

    table_body = "\n".join(table_rows)

    return (
        f"### 🔍 Domain Discovery: Explanation Codes for **\"{transaction_type}\"**\n\n"
        f"I searched the bank DWH catalog (`PIO_EXPLANATION_CODE`) using vector similarity and identified the following explanation codes:\n\n"
        f"| Code | Description | Relevance | Match Reason |\n"
        f"| :--- | :--- | :--- | :--- |\n"
        f"{table_body}\n\n"
        f"> **Action Needed:** Reply **\"confirm codes\"** to proceed with all of these codes, or specify which codes to include/exclude (e.g., *\"only use code {matches[0].get('code')}\"*)."
    )
