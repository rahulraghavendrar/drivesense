"""Knowledge-base ingestion for RAG.

Markdown files in knowledge_base/<collection>/ are split at '## ' headings - each
section of a guide is one self-contained chunk, which keeps chunks meaningful and
lets answers cite "Guide > Section". Chunks are de-duplicated by a content hash, so
re-running ingestion only embeds what changed.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from ..config import settings
from ..db import db
from .llm import LLMError, llm

log = logging.getLogger("drivesense.kb")
KB_DIR = Path(__file__).resolve().parents[2] / "knowledge_base"


def parse_markdown(path: Path) -> tuple[str, list[tuple[str, str]]]:
    title = path.stem.replace("_", " ").title()
    sections: list[tuple[str, str]] = []
    current, buf = "Overview", []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            if "".join(buf).strip():
                sections.append((current, "\n".join(buf).strip()))
            current, buf = line[3:].strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((current, "\n".join(buf).strip()))
    return title, sections


async def sync_knowledge_base() -> dict:
    """Upsert all chunks, drop removed ones, and embed any chunk without an embedding."""
    seen: set[str] = set()
    added = 0
    for coll_dir in sorted(p for p in KB_DIR.iterdir() if p.is_dir()):
        for path in sorted(coll_dir.glob("*.md")):
            title, sections = parse_markdown(path)
            for section, content in sections:
                h = hashlib.sha256(f"{coll_dir.name}|{title}|{section}|{content}".encode()).hexdigest()
                seen.add(h)
                res = await db.pool.execute(
                    """INSERT INTO kb_chunks (collection, source, title, section, content, content_hash)
                       VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (content_hash) DO NOTHING""",
                    coll_dir.name, path.name, title, section, content, h)
                added += res.endswith("1")
    await db.pool.execute("DELETE FROM kb_chunks WHERE NOT (content_hash = ANY($1::text[]))", list(seen))

    embedded = 0
    if llm.enabled:
        rows = await db.pool.fetch(
            "SELECT id, title, section, content FROM kb_chunks WHERE embedding IS NULL OR embed_model IS DISTINCT FROM $1",
            f"{settings.gemini_embed_model}:{settings.embed_dim}")
        if rows:
            try:
                vectors = await llm.embed([f"{r['title']} - {r['section']}\n{r['content']}" for r in rows],
                                          "RETRIEVAL_DOCUMENT")
                for r, vec in zip(rows, vectors):
                    await db.pool.execute(
                        "UPDATE kb_chunks SET embedding = $2::text::vector, embed_model = $3 WHERE id = $1",
                        r["id"], _vec_literal(vec), f"{settings.gemini_embed_model}:{settings.embed_dim}")
                    embedded += 1
            except LLMError as exc:
                log.warning("Could not embed knowledge base (keyword search still works): %s", exc)
    total = await db.pool.fetchval("SELECT count(*) FROM kb_chunks")
    log.info("Knowledge base synced: %d chunks (%d new, %d embedded now)", total, added, embedded)
    return {"chunks": total, "added": added, "embedded": embedded}


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
