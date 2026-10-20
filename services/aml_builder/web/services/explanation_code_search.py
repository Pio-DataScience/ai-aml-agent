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

from services.aml_builder.web.services.oracle import run_readonly
from services.aml_builder.web.services.settings import settings
from services.aml_builder.web.services.llm_client import build_llm

logger = logging.getLogger(__name__)

CACHE_DIR = Path("artifacts")
CACHE_FILE = CACHE_DIR / "explanation_codes_vector_cache.pkl"

_VECTOR_CACHE: Optional[Dict[str, Any]] = None

_UPDATE_COLUMNS = (
    "UPDATED_AT",
    "UPDATED_DATE",
    "UPDATE_DATE",
    "LAST_UPDATED_AT",
    "LAST_UPDATE_DATE",
    "LAST_UPDATE",
)


def _get_vector_cache_key() -> dict[str, Any]:
    """Read the explanation-catalog version fingerprint without changing data.

    Returns:
        A row-count/update-time key, or a row-count/checksum fallback.

    Raises:
        Exception: If the key cannot be read; callers must rebuild instead of
            returning a possibly stale cached index.
    """
    params = {"cc": settings.AML_COUNTRY_CODE, "ic": settings.AML_INST_CODE}
    _, column_rows = run_readonly(
        "SELECT COLUMN_NAME, DATA_TYPE FROM USER_TAB_COLUMNS "
        "WHERE TABLE_NAME = 'PIO_EXPLANATION_CODE'"
    )
    column_types = {
        str(row[0]).upper(): str(row[1]).upper()
        for row in column_rows
        if row and row[0]
    }
    update_column = next(
        (column for column in _UPDATE_COLUMNS if column in column_types), None
    )
    where = "(COUNTRY_CODE = :cc OR COUNTRY_CODE IS NULL) AND (INST_CODE = :ic OR INST_CODE IS NULL)"
    if update_column:
        _, rows = run_readonly(
            f"SELECT COUNT(*), MAX({update_column}) FROM PIO_EXPLANATION_CODE "
            f"WHERE EXPLANATION_CODE IS NOT NULL AND {where}",
            params,
        )
        row_count, max_updated_at = rows[0] if rows else (0, None)
        timestamp = (
            max_updated_at.isoformat()
            if hasattr(max_updated_at, "isoformat")
            else str(max_updated_at) if max_updated_at is not None else None
        )
        key = {
            "basis": f"row_count+max:{update_column}",
            "row_count": int(row_count or 0),
            "max_updated_at": timestamp,
        }
    else:
        expressions: list[str] = []
        for column in ("EXPLANATION_CODE", "DESC_ENG", "DESC_NAT_LAN", "LONG_DES_ENG"):
            data_type = column_types.get(column, "VARCHAR2")
            raw = (
                f"DBMS_LOB.SUBSTR({column}, 1000, 1)"
                if data_type in {"CLOB", "NCLOB"}
                else f"SUBSTR(TO_CHAR({column}), 1, 1000)"
            )
            expressions.append(f"NVL({raw}, '~')")
        checksum_expr = " || '|' || ".join(expressions)
        _, rows = run_readonly(
            "SELECT COUNT(*), SUM(ORA_HASH(" + checksum_expr + ")) "
            "FROM PIO_EXPLANATION_CODE "
            f"WHERE EXPLANATION_CODE IS NOT NULL AND {where}",
            params,
        )
        row_count, checksum = rows[0] if rows else (0, 0)
        key = {
            "basis": "row_count+checksum",
            "row_count": int(row_count or 0),
            "checksum": int(checksum or 0),
        }
    logger.info("[EXPL_SEARCH] Vector cache fingerprint uses %s.", key["basis"])
    return key


def _load_or_build_vector_index() -> Dict[str, Any]:
    """Load or build persistent vector embeddings for PIO_EXPLANATION_CODE filtered by country and institution code."""
    global _VECTOR_CACHE
    try:
        current_key = _get_vector_cache_key()
    except Exception as exc:
        logger.warning(
            "[EXPL_SEARCH] Vector cache fingerprint failed; rebuilding the index. %s",
            type(exc).__name__,
        )
        current_key = None
    if (
        _VECTOR_CACHE is not None
        and current_key is not None
        and _VECTOR_CACHE.get("cache_key") == current_key
    ):
        return _VECTOR_CACHE
    _VECTOR_CACHE = None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"explanation_codes_vector_cache_{settings.AML_COUNTRY_CODE}_{settings.AML_INST_CODE}.pkl"

    if cache_file.exists():
        try:
            with open(cache_file, "rb") as f:
                cached = pickle.load(f)
            if current_key is not None and cached.get("cache_key") == current_key:
                _VECTOR_CACHE = cached
                logger.info(
                    "[EXPL_SEARCH] Loaded %d explanation codes from vector cache (%s).",
                    len(_VECTOR_CACHE["metadata"]),
                    cache_file.name,
                )
                return _VECTOR_CACHE
            logger.info("[EXPL_SEARCH] Vector cache key changed or unavailable; rebuilding.")
        except Exception as exc:
            logger.warning("[EXPL_SEARCH] Failed to load vector cache: %s. Rebuilding...", exc)

    logger.info(
        "[EXPL_SEARCH] Building vector index for PIO_EXPLANATION_CODE (COUNTRY_CODE=%s INST_CODE=%s)...",
        settings.AML_COUNTRY_CODE,
        settings.AML_INST_CODE,
    )
    from services.aml_builder.web.services.oracle import init_pool, run_readonly

    try:
        init_pool()
    except Exception:
        pass

    sql = """
    SELECT EXPLANATION_CODE, DESC_ENG, DESC_NAT_LAN, LONG_DES_ENG
    FROM PIO_EXPLANATION_CODE
    WHERE EXPLANATION_CODE IS NOT NULL
      AND (COUNTRY_CODE = :cc OR COUNTRY_CODE IS NULL)
      AND (INST_CODE = :ic OR INST_CODE IS NULL)
    """
    _, rows = run_readonly(sql, {"cc": settings.AML_COUNTRY_CODE, "ic": settings.AML_INST_CODE})
    if not rows:
        logger.error("[EXPL_SEARCH] No explanation codes found in PIO_EXPLANATION_CODE for CC=%s IC=%s!", settings.AML_COUNTRY_CODE, settings.AML_INST_CODE)
        _VECTOR_CACHE = {"embeddings": np.array([]), "metadata": [], "cache_key": current_key}
        return _VECTOR_CACHE

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
        "cache_key": current_key,
    }

    try:
        with open(cache_file, "wb") as f:
            pickle.dump(_VECTOR_CACHE, f)
        logger.info("[EXPL_SEARCH] Persistent vector cache saved to %s.", cache_file)
    except Exception as exc:
        logger.error("[EXPL_SEARCH] Failed to save vector cache file: %s", exc)

    return _VECTOR_CACHE


def search_explanation_candidates(
    query_text: str, min_similarity: float = 0.50, top_k: int = 100
) -> List[Dict[str, Any]]:
    """Vector similarity search returning candidate explanation codes meeting top 70% relative similarity cutoff up to top 100."""
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

    if len(top_indices) == 0:
        return []

    max_score = float(scores[top_indices[0]])
    relative_cutoff = max(max_score * 0.70, min_similarity)

    candidates = []
    for idx in top_indices:
        score = float(scores[idx])
        if score >= relative_cutoff:
            item = dict(metadata[idx])
            item["score"] = round(score, 3)
            item["similarity_pct"] = f"{int(round(score * 100))}%"
            candidates.append(item)

    return candidates


def _select_codes_for_single_type(
    single_type: str, min_similarity: float = 0.55
) -> List[Dict[str, Any]]:
    """Sub-LLM selection & ranking for candidates meeting the similarity threshold for a single transaction type."""
    candidates = search_explanation_candidates(single_type, min_similarity=min_similarity, top_k=100)
    if not candidates:
        return []

    # Prepare candidates text for LLM ranking
    candidate_lines = []
    for c in candidates[:80]:
        candidate_lines.append(
            f"Code: {c['code']} | Description: {c['desc_eng']} | Similarity: {c['similarity_pct']}"
        )

    candidates_text = "\n".join(candidate_lines)

    prompt = (
        "You are an expert AML database classifier.\n"
        f"User Goal / Transaction Type: '{single_type}'\n\n"
        "Analyze the candidate explanation codes from the bank DWH catalog (PIO_EXPLANATION_CODE) below. "
        "Select and rank ALL explanation codes that correspond to this transaction type (or relevant sub-types).\n\n"
        f"CANDIDATES:\n{candidates_text}\n\n"
        "Respond ONLY with a valid JSON array of objects matching this schema (no markdown fences or preambles):\n"
        '[\n'
        '  {"code": "<code_string>", "description": "<desc_string>", "similarity": "<similarity_pct>", "relevance": "High|Medium", "reason": "<brief_reason>"}\n'
        ']'
    )

    try:
        llm = build_llm(fast=True)
        resp = llm.invoke(prompt)
        text = str(resp.content).strip()

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
            "[EXPL_SEARCH] Sub-LLM selected %d explanation codes for '%s' (min_similarity=%.2f).",
            len(parsed),
            single_type,
            min_similarity,
        )
        return parsed
    except Exception as exc:
        logger.error(
            "[EXPL_SEARCH] Sub-LLM reranking failed for '%s': %s. Returning top vector candidates.",
            single_type,
            exc,
        )
        fallback = []
        for c in candidates:
            fallback.append({
                "code": c["code"],
                "description": c["desc_eng"],
                "similarity": c["similarity_pct"],
                "relevance": "High" if c["score"] >= 0.7 else "Medium",
                "reason": f"Vector match ({c['similarity_pct']} similarity)",
            })
        return fallback


def select_relevant_explanation_codes(
    transaction_type: Any, min_similarity: float = 0.55
) -> List[Dict[str, Any]]:
    """Select and rank explanation codes for single or multiple transaction types with deduplication."""
    if not transaction_type:
        return []

    # Parse into a list of distinct query strings
    types_list: List[str] = []
    if isinstance(transaction_type, list):
        for item in transaction_type:
            if isinstance(item, str) and item.strip():
                types_list.append(item.strip())
    elif isinstance(transaction_type, str):
        # Split comma, semicolon, or 'or' / 'and' separated phrases if multiple types listed
        parts = re.split(r"[,;]|\b(?:or|and)\b", transaction_type, flags=re.IGNORECASE)
        cleaned = [p.strip() for p in parts if p.strip() and len(p.strip()) > 2 and p.strip().lower() not in ("or", "and")]
        if len(cleaned) > 1:
            types_list = cleaned
        else:
            types_list = [transaction_type.strip()]

    if not types_list:
        return []

    all_matches: List[Dict[str, Any]] = []
    seen_codes = set()

    for tx in types_list:
        codes = _select_codes_for_single_type(tx, min_similarity=min_similarity)
        for c in codes:
            code_val = str(c.get("code", "")).strip()
            if code_val and code_val not in seen_codes:
                seen_codes.add(code_val)
                c["channel"] = tx
                c["transaction_type"] = tx
                # Annotate reason with matched transaction type if multi-type search
                if len(types_list) > 1 and "reason" in c:
                    c["reason"] = f"[{tx}] {c.get('reason', '')}".strip()
                all_matches.append(c)

    return all_matches


def format_explanation_code_checkpoint(transaction_type: Any, matches: List[Dict[str, Any]]) -> str:
    """Format discovered transaction types into a clean executive markdown table view."""
    if not matches:
        return ""

    table_rows = []
    for m in matches:
        code = m.get("code", "")
        desc = m.get("description", "")
        sim = m.get("similarity", "—")
        rel = m.get("relevance", "High")
        reason = m.get("reason", "")
        table_rows.append(f"| **{code}** | {desc} | **{sim}** | **{rel}** | {reason} |")

    table_body = "\n".join(table_rows)

    count_str = f"({len(matches)} Matches Found)"
    label = ", ".join(transaction_type) if isinstance(transaction_type, list) else str(transaction_type)

    return (
        f"### 🔍 Discovered Transaction Types for **\"{label}\"** {count_str}\n\n"
        f"I searched our compliance transaction catalog and identified the following matching transaction codes:\n\n"
        f"| Code | Description | Match % | Relevance | Reason |\n"
        f"| :--- | :--- | :--- | :--- | :--- |\n"
        f"{table_body}\n\n"
        f"> **Action Needed:** Reply **\"confirm codes\"** to proceed with all {len(matches)} codes, or specify which codes to include/exclude (e.g., *\"only use code {matches[0].get('code')}\"*)."
    )
