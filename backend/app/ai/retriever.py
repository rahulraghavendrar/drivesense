"""Hybrid retrieval = keyword search + semantic (vector) search, fused with RRF.

Why both?
  * Keyword (PostgreSQL full-text search) is exact: it nails terms like "P0217",
    "TPMS" or "harsh braking" that embeddings can blur.
  * Semantic (pgvector cosine distance) understands meaning: "my car is running hot"
    finds the "Engine overheating" section even with zero shared words.

Reciprocal Rank Fusion merges the two ranked lists without having to calibrate their
very different scores:  rrf(doc) = sum over lists of 1 / (60 + rank_in_list)
Both searches run inside the same PostgreSQL database - no separate vector DB.
"""
from __future__ import annotations

import logging
import re

from ..db import db
from .kb import _vec_literal
from .llm import LLMError, llm

log = logging.getLogger("drivesense.retriever")
RRF_K = 60
STOPWORDS = {"the", "and", "for", "with", "that", "this", "what", "why", "how", "was", "are", "you", "your",
             "did", "does", "can", "should", "about", "from", "have", "has", "had", "into", "when", "which",
             "low", "high", "fix", "make", "get", "help", "tell", "please", "much", "many", "my", "its", "it's"}


def _or_query(text: str) -> str | None:
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text) if len(w) >= 3]
    terms = list(dict.fromkeys(w for w in words if w not in STOPWORDS))[:24]
    return " | ".join(terms) if terms else None


async def hybrid_search(query: str, collections: list[str], k: int = 4,
                        query_vector: list[float] | None = None) -> list[dict]:
    ranked: dict[int, dict] = {}

    def add(rows, via: str) -> None:
        for rank, r in enumerate(rows, start=1):
            item = ranked.setdefault(r["id"], {**dict(r), "rrf": 0.0, "via": []})
            item["rrf"] += 1.0 / (RRF_K + rank)
            item["via"].append(via)

    tsq = _or_query(query)
    if tsq:
        rows = await db.pool.fetch(
            """SELECT id, collection, source, title, section, content
               FROM kb_chunks, to_tsquery('english', $1) q
               WHERE collection = ANY($2::text[]) AND tsv @@ q
               ORDER BY ts_rank_cd(tsv, q) DESC LIMIT 10""", tsq, collections)
        add(rows, "keyword")

    if llm.enabled:
        try:
            vec = query_vector or (await llm.embed([query], "RETRIEVAL_QUERY"))[0]
            rows = await db.pool.fetch(
                """SELECT id, collection, source, title, section, content
                   FROM kb_chunks
                   WHERE collection = ANY($2::text[]) AND embedding IS NOT NULL
                   ORDER BY embedding <=> $1::text::vector LIMIT 10""", _vec_literal(vec), collections)
            add(rows, "semantic")
        except LLMError as exc:
            log.warning("Semantic search skipped: %s", exc)

    results = sorted(ranked.values(), key=lambda x: -x["rrf"])[:k]
    for i, r in enumerate(results, start=1):
        r["tag"] = f"S{i}"
    return results


async def multi_search(queries: dict[str, str], collections: list[str], per_query: int | dict[str, int] = 1,
                       k: int = 5) -> list[dict]:
    """Query decomposition: one focused search per topic (e.g. per driving event), merged.

    A single combined query lets one broad section win every slot; separate queries make
    sure each problem the driver had gets its own most relevant guidance. All query
    embeddings are fetched in ONE batched API call to save free-tier quota.
    Each result remembers which topic (`for_key`) it was retrieved for. `per_query` can be a
    dict to give some topics (e.g. the driver's own question) more slots than others."""
    keys = list(queries)
    vectors: list[list[float] | None] = [None] * len(keys)
    if llm.enabled and keys:
        try:
            vectors = await llm.embed([queries[key] for key in keys], "RETRIEVAL_QUERY")
        except LLMError as exc:
            log.warning("Semantic search skipped: %s", exc)
    merged: list[dict] = []
    seen: set[int] = set()
    for key, vec in zip(keys, vectors):
        if len(merged) >= k:
            break
        n = per_query.get(key, 1) if isinstance(per_query, dict) else per_query
        for r in await hybrid_search(queries[key], collections, n, query_vector=vec):
            if r["id"] not in seen and len(merged) < k:
                seen.add(r["id"])
                merged.append({**r, "for_key": key})
    for i, r in enumerate(merged, start=1):
        r["tag"] = f"S{i}"
    return merged
