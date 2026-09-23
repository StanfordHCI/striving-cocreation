"""BM25 full-text search on Entity table.

Creates FTS5 virtual table on Entity.text + metadata_json (which contains
reasoning), with decay-weighted scoring and optional MMR diversity reranking.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from tempo.models import Entity

logger = logging.getLogger(__name__)

# Constants
K_DECAY = 2.0  # Decay weight factor (higher = faster decay for old items)
LAMBDA_MMR = 0.5  # MMR diversity-relevance tradeoff (1.0 = pure relevance)


async def setup_fts(session: AsyncSession) -> None:
    """Create FTS5 virtual table and sync triggers for Entity table.

    Should be called once after database creation for study databases.
    The FTS index covers both Entity.text and Entity.metadata_json so
    that BM25 queries match against both the entity label and its
    reasoning/evidence field.
    """
    # Create FTS5 virtual table — content-synced with entities table
    await session.execute(sql_text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS entity_fts
        USING fts5(
            text,
            metadata_json,
            content='entities',
            content_rowid='id',
            tokenize='porter ascii'
        )
    """))

    # Auto-sync triggers ------------------------------------------------

    await session.execute(sql_text("""
        CREATE TRIGGER IF NOT EXISTS entity_fts_ai AFTER INSERT ON entities BEGIN
            INSERT INTO entity_fts(rowid, text, metadata_json)
            VALUES (new.id, new.text, COALESCE(new.metadata_json, ''));
        END
    """))

    await session.execute(sql_text("""
        CREATE TRIGGER IF NOT EXISTS entity_fts_ad AFTER DELETE ON entities BEGIN
            INSERT INTO entity_fts(entity_fts, rowid, text, metadata_json)
            VALUES ('delete', old.id, old.text, COALESCE(old.metadata_json, ''));
        END
    """))

    await session.execute(sql_text("""
        CREATE TRIGGER IF NOT EXISTS entity_fts_au AFTER UPDATE ON entities BEGIN
            INSERT INTO entity_fts(entity_fts, rowid, text, metadata_json)
            VALUES ('delete', old.id, old.text, COALESCE(old.metadata_json, ''));
            INSERT INTO entity_fts(rowid, text, metadata_json)
            VALUES (new.id, new.text, COALESCE(new.metadata_json, ''));
        END
    """))

    # Rebuild FTS index from existing data (idempotent)
    await session.execute(sql_text(
        "INSERT INTO entity_fts(entity_fts) VALUES('rebuild')"
    ))


def _build_fts_query(query: str, mode: str = "OR") -> str:
    """Convert raw query string to FTS5 query syntax.

    Args:
        query: Raw search query.
        mode: "OR" (any token matches), "AND" (all tokens), "PHRASE" (exact).

    Returns:
        FTS5-compatible query string.
    """
    tokens = re.findall(r"\b\w+\b", query.lower())
    tokens = [t for t in tokens if len(t) > 1]

    if not tokens:
        return query

    if mode == "PHRASE":
        return '"' + " ".join(tokens) + '"'

    joiner = " OR " if mode == "OR" else " AND "
    return joiner.join(tokens)


async def bm25_search(
    session: AsyncSession,
    query: str,
    entity_type: str,
    limit: int = 10,
    enable_decay: bool = True,
    enable_mmr: bool = True,
    mode: str = "OR",
) -> List[Tuple[Entity, float]]:
    """BM25 search on Entity table with decay weighting and MMR diversity.

    1. FTS5 BM25 ranking for initial relevance
    2. Temporal decay weighting (exponential)
    3. Score normalization to [0, 1]
    4. Optional MMR diversity reranking (TF-IDF + cosine similarity)

    Args:
        session: Active database session.
        query: Search query text.
        entity_type: Entity type to filter ("goal", "operation", etc.).
        limit: Maximum results to return.
        enable_decay: Apply temporal decay weighting.
        enable_mmr: Apply MMR diversity reranking.
        mode: FTS query mode ("OR", "AND", "PHRASE").

    Returns:
        List of (Entity, score) tuples sorted by relevance.
    """
    fts_query = _build_fts_query(query, mode)
    if not fts_query.strip():
        return []

    # Step 1: FTS5 query — get matching entity IDs with BM25 scores.
    # bm25() returns negative values where more-negative = more relevant.
    try:
        fts_result = await session.execute(
            sql_text("""
                SELECT entity_fts.rowid AS eid, bm25(entity_fts) AS rank
                FROM entity_fts
                JOIN entities e ON e.id = entity_fts.rowid
                WHERE entity_fts MATCH :query
                  AND e.type = :entity_type
                ORDER BY rank
                LIMIT :candidate_limit
            """),
            {
                "query": fts_query,
                "entity_type": entity_type,
                "candidate_limit": limit * 10,
            },
        )
        fts_rows = fts_result.fetchall()
    except Exception as e:
        logger.warning("FTS5 search failed (query=%r): %s", query, e)
        return []

    if not fts_rows:
        return []

    # Step 2: Load full Entity objects via ORM
    id_to_rank = {row.eid: -row.rank for row in fts_rows}  # flip sign: higher = better
    entity_ids = list(id_to_rank.keys())

    orm_result = await session.execute(
        select(Entity).where(Entity.id.in_(entity_ids))
    )
    entities_by_id = {e.id: e for e in orm_result.scalars().all()}

    candidates: List[Tuple[Entity, float]] = []
    for eid in entity_ids:
        entity = entities_by_id.get(eid)
        if entity:
            candidates.append((entity, id_to_rank[eid]))

    if not candidates:
        return []

    # Step 3: Decay weighting (exponential decay)
    if enable_decay:
        now = datetime.utcnow()
        weighted = []
        for entity, score in candidates:
            meta = entity.metadata_dict
            decay_value = meta.get("decay", 5)  # default mid-range
            days_old = max(
                (now - entity.timestamp_start).total_seconds() / 86400, 0.01
            )
            # Higher decay value → slower decay → entity stays relevant longer
            decay_factor = math.exp(-K_DECAY * days_old / max(decay_value, 1))
            weighted.append((entity, score * decay_factor))
        candidates = weighted

    # Step 4: Normalize scores to [0, 1]
    max_score = max(s for _, s in candidates)
    min_score = min(s for _, s in candidates)
    score_range = max_score - min_score if max_score > min_score else 1.0
    candidates = [(e, (s - min_score) / score_range) for e, s in candidates]

    # Step 5: MMR diversity reranking
    if enable_mmr and len(candidates) > 1:
        candidates = _mmr_rerank(candidates, limit, LAMBDA_MMR)
    else:
        candidates.sort(key=lambda x: x[1], reverse=True)
        candidates = candidates[:limit]

    return candidates


def _mmr_rerank(
    candidates: List[Tuple[Entity, float]],
    limit: int,
    lambda_param: float,
) -> List[Tuple[Entity, float]]:
    """Maximal Marginal Relevance reranking for diversity.

    Greedily selects results that balance relevance (BM25 score) with
    diversity (dissimilarity to already-selected results via TF-IDF cosine).

    Falls back to score-sorted if sklearn is unavailable.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        logger.debug("sklearn not available, skipping MMR diversity")
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[:limit]

    texts = [e.text for e, _ in candidates]
    scores = [s for _, s in candidates]

    vectorizer = TfidfVectorizer(stop_words="english")
    try:
        tfidf_matrix = vectorizer.fit_transform(texts)
    except ValueError:
        # Empty vocabulary (all stop words or empty texts)
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[:limit]

    sim_matrix = cosine_similarity(tfidf_matrix)

    # Greedy MMR selection
    selected: List[int] = []
    remaining = list(range(len(candidates)))

    # Start with highest-scored candidate
    best_idx = max(remaining, key=lambda i: scores[i])
    selected.append(best_idx)
    remaining.remove(best_idx)

    while len(selected) < limit and remaining:
        best_mmr = -float("inf")
        best_candidate = remaining[0]

        for idx in remaining:
            relevance = scores[idx]
            max_sim = max(sim_matrix[idx][j] for j in selected)
            mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim

            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_candidate = idx

        selected.append(best_candidate)
        remaining.remove(best_candidate)

    return [(candidates[i][0], candidates[i][1]) for i in selected]
