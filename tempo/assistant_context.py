"""Local context assembly and refresh decisions for the contextual assistant.

This module makes no model or network calls. It prepares the evidence a
suggestion generator would use and decides when a cached result is obsolete.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select

from tempo.assistant_inputs import SuggestionHistoryItem, personal_context, recent_suggestions
from tempo.db import Database
from tempo.models import Entity, EntityType, Relation, RelationSubtype, RelationType

MAX_GOALS = 40
MAX_NODES = 96
MAX_EDGES = 180
MAX_TEXT = 1200
MAX_NODE_TEXT = 32000
RECENT_LIMITS = {EntityType.OPERATION: 20, EntityType.ACTION: 8, EntityType.ACTIVITY: 6}


def _visible(entity: Entity) -> bool:
    metadata = entity.metadata_dict or {}
    return not metadata.get("removed_by_user") and metadata.get("status") not in {
        "retired", "archived", "deleted",
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _node(entity: Entity) -> dict:
    metadata = entity.metadata_dict or {}
    return {
        "id": entity.id, "type": entity.type, "text": entity.text[:MAX_TEXT],
        "text_truncated": len(entity.text) > MAX_TEXT,
        "observed_at": _utc(entity.timestamp_end or entity.timestamp_start).isoformat(),
        "revision": int(metadata.get("revision", 0) or 0),
        "user_edited": bool(metadata.get("user_edited") or metadata.get("user_locked")),
        "user_annotations": [
            {"type": str(item.get("type", "note"))[:40], "text": str(item.get("text", ""))[:400],
             "text_truncated": len(str(item.get("text", ""))) > 400}
            for item in metadata.get("user_annotations", [])[-3:] if isinstance(item, dict)
        ],
        "status": metadata.get("status", "active"),
    }


def _available():
    """Filter before SQL limits so retired rows cannot crowd out visible evidence."""
    metadata = func.coalesce(Entity.metadata_json, "{}")
    return and_(
        func.coalesce(func.json_extract(metadata, "$.removed_by_user"), 0) != 1,
        func.coalesce(func.json_extract(metadata, "$.status"), "active").notin_(["retired", "archived", "deleted"]),
    )


def bound_node_text(nodes: list[dict], budget: int = MAX_NODE_TEXT) -> bool:
    """Share the text budget across nodes without dropping IDs or edges."""
    fields = [(node, "text") for node in nodes]
    fields += [(note, "text") for node in nodes for note in node.get("user_annotations", [])]
    if not fields or sum(len(item[key]) for item, key in fields) <= budget:
        return any(item.get("text_truncated", False) for item, _ in fields)
    allowance = budget // len(fields)
    for item, key in fields:
        if len(item[key]) > allowance:
            item[key] = item[key][:allowance]
            item["text_truncated"] = True
    return True


async def _sample_recent(session, kind: str, cutoff: datetime, end: datetime, limit: int) -> tuple[list[dict], bool]:
    """Keep the latest observations plus coverage of earlier parts of the window.

    Eight bounded time slices prevent a busy last minute from consuming the
    entire candidate pool. Exact repeated labels are condensed only when
    their saved structural parents also match.
    """
    seen_at = func.coalesce(Entity.timestamp_end, Entity.timestamp_start)
    width = (end - cutoff) / 8
    candidates: dict[int, Entity] = {}
    truncated = False
    for index in range(8):
        lower, upper = end - width * (index + 1), end - width * index
        rows = list(await session.scalars(select(Entity).where(
            Entity.type == kind, _available(), seen_at >= lower,
            seen_at <= upper if index == 0 else seen_at < upper,
        ).order_by(seen_at.desc(), Entity.id.desc()).limit(129)))
        truncated |= len(rows) > 128
        candidates.update((entity.id, entity) for entity in rows[:128])
    if not candidates:
        return [], truncated
    parents: dict[int, set[int]] = {}
    for edge in await session.scalars(select(Relation).where(
        Relation.source_id.in_(candidates), Relation.relation_type == RelationType.STRUCTURAL,
        Relation.relation_subtype == RelationSubtype.PART_OF,
    )):
        parents.setdefault(edge.source_id, set()).add(edge.target_id)
    groups: dict[tuple, dict] = {}
    for entity in sorted(candidates.values(), key=lambda entity: (_utc(entity.timestamp_end or entity.timestamp_start), entity.id), reverse=True):
        # Separate actions/activities can have identical labels. Only compact
        # operations, preserving differences in the person's annotations.
        node = _node(entity)
        key = (" ".join(entity.text.casefold().split()), tuple(sorted(parents.get(entity.id, ()))),
               json.dumps(node["user_annotations"], sort_keys=True), node["user_edited"],
               entity.id if kind != EntityType.OPERATION else None)
        if key not in groups:
            groups[key] = {**node, "sampled_occurrences": 1}
        else:
            groups[key]["sampled_occurrences"] += 1
        groups[key]["first_sampled_at"] = _utc(entity.timestamp_end or entity.timestamp_start).isoformat()
    ordered = list(groups.values())
    selected = ordered[:max(1, limit // 2)]
    selected_ids = {node["id"] for node in selected}
    remaining = [node for node in ordered if node["id"] not in selected_ids]
    times = {node["id"]: datetime.fromisoformat(node["observed_at"]).timestamp() for node in ordered}
    # Fill remaining slots from the largest uncovered temporal gaps. Iterating
    # newest-first again would still let a busy burst dominate the sample.
    while len(selected) < limit and remaining:
        chosen = max(remaining, key=lambda node: (
            min(abs(times[node["id"]] - times[other["id"]]) for other in selected),
            times[node["id"]], node["id"],
        ))
        selected.append(chosen)
        remaining.remove(chosen)
    return selected, truncated or len(ordered) > limit


async def _recent_visible(session, statement, limit: int) -> list[Entity]:
    rows = await session.stream_scalars(statement.execution_options(yield_per=64))
    selected: list[Entity] = []
    seen: set[int] = set()
    try:
        async for entity in rows:
            if entity.id not in seen and _visible(entity):
                selected.append(entity)
                seen.add(entity.id)
                if len(selected) >= limit:
                    break
    finally:
        await rows.close()
    return selected


async def build_context(
    db: Database, *, now: datetime | None = None,
    person_context: str | None = None, user_note: str = "",
    focus_goal_ids: list[int] | None = None,
    recent_hours: float = 4, operation_minutes: float = 30,
    suggestion_history: list[SuggestionHistoryItem] | None = None,
) -> dict:
    """Combine current observations, goal knowledge, and an actual subgraph.

    Include observations even before they have been linked to a goal. Keep
    other goals available so the generator can recommend a change of activity,
    as well as continuing what the person was just doing. Only summary text
    and selected entity metadata are included, never screenshot files.
    """
    if recent_hours <= 0 or operation_minutes <= 0:
        raise ValueError("Context windows must be positive")
    now = _utc(now or datetime.now(timezone.utc))
    person, person_truncated = await personal_context(db.data_directory, person_context)
    cutoff = (now - timedelta(hours=recent_hours)).replace(tzinfo=None)
    end = now.replace(tzinfo=None)
    nodes: dict[int, dict] = {}
    recent_ids: list[int] = []
    truncated = False
    seen_at = func.coalesce(Entity.timestamp_end, Entity.timestamp_start)

    def include(node: dict) -> bool:
        nonlocal truncated
        if node["id"] not in nodes and len(nodes) >= MAX_NODES:
            truncated = True
            return False
        if node["id"] not in nodes:
            nodes[node["id"]] = node
        return True

    async with db.session() as session:
        goals = [goal for goal in await session.scalars(
            select(Entity).where(Entity.type == EntityType.GOAL)
            .order_by(func.coalesce(Entity.updated_at, Entity.created_at).desc(), Entity.id.desc())
        ) if _visible(goal)]
        valid_goals = {goal.id for goal in goals}
        if focus_goal_ids is not None:
            if not focus_goal_ids or len(focus_goal_ids) > 3 or len(set(focus_goal_ids)) != len(focus_goal_ids):
                raise ValueError("Choose between one and three distinct goals")
            if not set(focus_goal_ids).issubset(valid_goals):
                raise ValueError("A chosen goal is no longer available")
            # Explicit choices must survive the background-context budget.
            goals.sort(key=lambda goal: goal.id not in focus_goal_ids)
        omitted_goals = max(0, len(goals) - MAX_GOALS)
        goal_ids = [goal.id for goal in goals[:MAX_GOALS]]
        for goal in goals[:MAX_GOALS]:
            include(_node(goal))

        for kind, limit in RECENT_LIMITS.items():
            start = max(cutoff, end - timedelta(minutes=operation_minutes)) if kind == EntityType.OPERATION else cutoff
            candidates, sampled = await _sample_recent(session, kind, start, end, limit)
            truncated |= sampled
            for node in candidates:
                if include(node):
                    recent_ids.append(node["id"])
        recent_ids.sort(key=lambda eid: (nodes[eid]["observed_at"], eid), reverse=True)

        # Corrections remain available even when their original observation is old.
        metadata = func.coalesce(Entity.metadata_json, "{}")
        corrections = await _recent_visible(session, select(Entity).where(
            Entity.type.in_(RECENT_LIMITS), _available(), or_(
                func.json_extract(metadata, "$.user_edited") == 1,
                func.json_extract(metadata, "$.user_locked") == 1,
                func.json_array_length(func.json_extract(metadata, "$.user_annotations")) > 0,
            ),
        ).order_by(func.coalesce(Entity.updated_at, Entity.created_at).desc(), Entity.id.desc()), 6)
        correction_ids = [entity.id for entity in corrections if include(_node(entity))]

        # At most two background activities per goal, selected round-robin.
        # A busy pursuit must not use all the historical evidence slots.
        ranked = select(Entity.id.label("entity_id"), Relation.target_id.label("goal_id"),
                        func.row_number().over(partition_by=Relation.target_id, order_by=(seen_at.desc(), Entity.id.desc())).label("rank")).join(
            Relation, Relation.source_id == Entity.id,
        ).where(Relation.target_id.in_(goal_ids), Relation.relation_type == RelationType.STRUCTURAL,
                Relation.relation_subtype == RelationSubtype.PART_OF, Entity.type == EntityType.ACTIVITY,
                _available(), seen_at <= end).subquery()
        background_rows = list(await session.execute(select(Entity, ranked.c.goal_id, ranked.c.rank).join(
            ranked, ranked.c.entity_id == Entity.id,
        ).where(ranked.c.rank <= 2)))
        goal_order = {eid: index for index, eid in enumerate(goal_ids)}
        background_rows.sort(key=lambda row: (row[2], goal_order[row[1]]))
        background_ids = []
        for entity, _, _ in background_rows:
            if entity.id in recent_ids or entity.id in background_ids:
                continue
            if len(background_ids) >= 8:
                truncated = True
                break
            if include(_node(entity)):
                background_ids.append(entity.id)

        frontier = set(recent_ids) | set(background_ids) | set(correction_ids) | set(goal_ids)
        visited: set[int] = set()
        for _ in range(3):
            if not frontier:
                break
            visited.update(frontier)
            links = list(await session.scalars(select(Relation).where(or_(
                and_(Relation.source_id.in_(frontier), Relation.relation_type == RelationType.STRUCTURAL,
                     Relation.relation_subtype == RelationSubtype.PART_OF),
                and_(Relation.relation_type == RelationType.BEHAVIORAL,
                     or_(Relation.source_id.in_(frontier), Relation.target_id.in_(frontier))),
            )).order_by(Relation.id.desc()).limit(MAX_EDGES + 1)))
            truncated |= len(links) > MAX_EDGES
            neighbors = sorted({eid for edge in links[:MAX_EDGES] for eid in (edge.source_id, edge.target_id)} - visited)
            next_frontier: set[int] = set()
            for entity in await session.scalars(select(Entity).where(Entity.id.in_(neighbors)).order_by(Entity.id)):
                if not _visible(entity):
                    continue
                if entity.type not in {*RECENT_LIMITS, EntityType.GOAL}:
                    continue
                if entity.id not in nodes and len(nodes) >= MAX_NODES:
                    truncated = True
                    continue
                # Retain the declared, bounded set of candidate goals.
                if entity.type == EntityType.GOAL and entity.id not in goal_ids:
                    continue
                include(_node(entity))
                next_frontier.add(entity.id)
            frontier = next_frontier

        relations = list(await session.scalars(select(Relation).where(
            Relation.source_id.in_(nodes), Relation.target_id.in_(nodes),
            or_(and_(Relation.relation_type == RelationType.STRUCTURAL,
                     Relation.relation_subtype == RelationSubtype.PART_OF),
                Relation.relation_type.in_([RelationType.BEHAVIORAL, RelationType.TEMPORAL])),
        ).order_by(Relation.id.desc()).limit(MAX_EDGES + 1)))
        truncated |= len(relations) > MAX_EDGES
        edges = [{"id": edge.id, "source_id": edge.source_id, "target_id": edge.target_id,
                  "type": edge.relation_type, "subtype": edge.relation_subtype,
                  "confidence": edge.confidence} for edge in relations[:MAX_EDGES]]

    text_truncated = bound_node_text(list(nodes.values()))
    context = {
        "recent_ids": recent_ids, "goal_ids": goal_ids,
        "background_ids": background_ids, "correction_ids": correction_ids,
        "nodes": [nodes[eid] for eid in sorted(nodes)], "edges": edges,
        "person_context": person, "person_context_truncated": person_truncated,
        "user_note": user_note[:2000], "user_note_truncated": len(user_note) > 2000,
        "focus_goal_ids": focus_goal_ids,
        "suggestion_history": recent_suggestions(suggestion_history or [], now),
        "windows": {"operations_minutes": min(operation_minutes, recent_hours * 60), "session_hours": recent_hours},
        "omitted_goal_count": omitted_goals, "graph_truncated": truncated,
        "text_truncated": text_truncated,
    }
    fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    return {**context, "context_id": fingerprint, "prepared_at": now.isoformat(),
            "last_observed_at": nodes[recent_ids[0]]["observed_at"] if recent_ids else None}


@dataclass
class RefreshGate:
    """Debounce graph/context changes; reject results for superseded context.

    Times are monotonic seconds supplied by the caller. This class schedules
    no tasks and invokes no model. A future authorized runtime can consume its
    decisions without tying requests to every screenshot or renderer update.
    """

    quiet_seconds: float = 30
    minimum_interval: float = 120
    maximum_wait: float = 90
    latest: str | None = None
    changed_at: float = 0
    last_started: float | None = None
    in_flight: str | None = None
    cached: str | None = None
    pending_since: float | None = None

    def observe(self, context_id: str, *, at: float) -> None:
        if context_id != self.latest:
            self.latest = context_id
            self.changed_at = at
        if context_id == self.cached:
            self.pending_since = None
        elif self.pending_since is None:
            self.pending_since = at

    def begin(self, *, at: float, explicit: bool = False) -> str | None:
        if self.latest is None or self.in_flight is not None:
            return None
        if not explicit:
            if self.latest == self.cached:
                return None
            if self.last_started is not None and at - self.last_started < self.minimum_interval:
                return None
            quiet = at - self.changed_at >= self.quiet_seconds
            waited = self.pending_since is not None and at - self.pending_since >= self.maximum_wait
            if not quiet and not waited:
                return None
        self.in_flight = self.latest
        self.last_started = at
        return self.in_flight

    def finish(self, context_id: str, *, success: bool = True) -> bool:
        if self.in_flight != context_id:
            return False
        self.in_flight = None
        if not success or context_id != self.latest:
            return False
        self.cached = context_id
        self.pending_since = None
        return True

    @property
    def cache_is_current(self) -> bool:
        return self.cached is not None and self.cached == self.latest
