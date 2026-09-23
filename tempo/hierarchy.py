"""Transactional hierarchy editing for Tempo's four-tier activity graph."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tempo.models import (
    Entity,
    EntityType,
    Relation,
    RelationSubtype,
    RelationType,
)
from tempo.store import Store


HIERARCHY_TYPES = (
    EntityType.OPERATION,
    EntityType.ACTION,
    EntityType.ACTIVITY,
    EntityType.GOAL,
)
PARENT_TYPE = {
    EntityType.OPERATION: EntityType.ACTION,
    EntityType.ACTION: EntityType.ACTIVITY,
    EntityType.ACTIVITY: EntityType.GOAL,
}
TYPE_RANK = {entity_type: rank for rank, entity_type in enumerate(HIERARCHY_TYPES)}


class SplitPart(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    child_ids: list[int] = Field(default_factory=list)


class HierarchyError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class HierarchyNotFound(HierarchyError):
    status_code = 404


class HierarchyConflict(HierarchyError):
    status_code = 409


class HierarchyValidationError(HierarchyError):
    status_code = 422


class HierarchyLocked(HierarchyConflict):
    pass


def _edited_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _revision(entity: Entity) -> int:
    try:
        return int((entity.metadata_dict or {}).get("revision", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() + ("Z" if value.tzinfo is None else "")


class HierarchyService:
    """Apply hierarchy edits inside the caller-owned database transaction."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.store = Store(session)

    async def _get(self, entity_id: int) -> Entity:
        entity = await self.store.entities.get(entity_id)
        if entity is None or entity.type not in HIERARCHY_TYPES:
            raise HierarchyNotFound(f"Entity {entity_id} not found")
        return entity

    @staticmethod
    def _assert_revision(entity: Entity, expected_revision: int) -> None:
        actual = _revision(entity)
        if actual != expected_revision:
            raise HierarchyConflict(
                f"Entity {entity.id} changed since it was loaded "
                f"(expected revision {expected_revision}, current revision {actual})"
            )

    @staticmethod
    def _assert_unlocked(entity: Entity) -> None:
        if (entity.metadata_dict or {}).get("user_locked"):
            raise HierarchyLocked(f"Entity {entity.id} is locked")

    @staticmethod
    def _validate_parent(child: Entity, parent: Entity) -> None:
        expected = PARENT_TYPE.get(child.type)
        if expected is None:
            raise HierarchyValidationError(f"{child.type} entities cannot have a parent")
        if parent.type != expected:
            raise HierarchyValidationError(
                f"A {child.type} must be parented to a {expected}, not a {parent.type}"
            )

    @staticmethod
    def _edit_metadata(entity: Entity, action: str, **values: Any) -> dict[str, Any]:
        metadata = dict(entity.metadata_dict or {})
        metadata["user_edited"] = True
        metadata["edited_at"] = _edited_at()
        metadata["last_user_edit"] = action
        metadata.update(values)
        return metadata

    @staticmethod
    def _entity_dict(entity: Entity) -> dict[str, Any]:
        metadata = entity.metadata_dict or {}
        return {
            "id": entity.id,
            "type": entity.type,
            "text": entity.text,
            "revision": _revision(entity),
            "locked": bool(metadata.get("user_locked")),
            "timestamp_start": _iso(entity.timestamp_start),
            "timestamp_end": _iso(entity.timestamp_end),
            "created_at": _iso(entity.created_at),
            "updated_at": _iso(entity.updated_at),
            "metadata": metadata,
        }

    async def _load_graph(self) -> tuple[dict[int, Entity], dict[int, list[int]], set[int]]:
        entity_result = await self.session.execute(
            select(Entity).where(Entity.type.in_(HIERARCHY_TYPES))
        )
        entities = {
            entity.id: entity
            for entity in entity_result.scalars().all()
            if not (entity.metadata_dict or {}).get("removed_by_user")
        }
        relation_result = await self.session.execute(
            select(Relation).where(
                Relation.relation_type == RelationType.STRUCTURAL,
                Relation.relation_subtype == RelationSubtype.PART_OF,
            )
        )
        children: dict[int, list[int]] = defaultdict(list)
        parented: set[int] = set()
        for relation in relation_result.scalars().all():
            child = entities.get(relation.source_id)
            parent = entities.get(relation.target_id)
            if child is None or parent is None:
                continue
            if PARENT_TYPE.get(child.type) != parent.type:
                continue
            children[parent.id].append(child.id)
            parented.add(child.id)
        for child_ids in children.values():
            child_ids.sort(key=lambda eid: (entities[eid].timestamp_start, eid))
        return entities, children, parented

    def _render_node(
        self,
        entity_id: int,
        entities: dict[int, Entity],
        children: dict[int, list[int]],
        *,
        max_depth: int,
        level: int = 1,
        path: frozenset[int] = frozenset(),
    ) -> dict[str, Any]:
        entity = entities[entity_id]
        node = self._entity_dict(entity)
        child_ids = children.get(entity_id, [])
        node["has_children"] = bool(child_ids)
        node["children"] = []
        if level >= max_depth:
            return node
        next_path = path | {entity_id}
        node["children"] = [
            self._render_node(
                child_id,
                entities,
                children,
                max_depth=max_depth,
                level=level + 1,
                path=next_path,
            )
            for child_id in child_ids
            if child_id not in next_path
        ]
        return node

    async def get_hierarchy(self, max_depth: int = 4) -> dict[str, Any]:
        if max_depth < 1 or max_depth > 4:
            raise HierarchyValidationError("max_depth must be between 1 and 4")
        entities, children, parented = await self._load_graph()
        root_ids = [entity_id for entity_id in entities if entity_id not in parented]
        root_ids.sort(
            key=lambda eid: (
                -TYPE_RANK[entities[eid].type],
                entities[eid].timestamp_start,
                eid,
            )
        )
        return {
            "roots": [
                self._render_node(
                    entity_id, entities, children, max_depth=max_depth
                )
                for entity_id in root_ids
            ],
            "count": len(entities),
            "max_depth": max_depth,
        }

    async def _subtree(self, entity_id: int, max_depth: int = 4) -> dict[str, Any]:
        entities, children, _ = await self._load_graph()
        if entity_id not in entities:
            raise HierarchyNotFound(f"Entity {entity_id} not found")
        return self._render_node(entity_id, entities, children, max_depth=max_depth)

    async def update_entity(
        self,
        entity_id: int,
        *,
        expected_revision: int,
        text: Optional[str] = None,
        locked: Optional[bool] = None,
    ) -> dict[str, Any]:
        if text is None and locked is None:
            raise HierarchyValidationError("Provide text and/or locked")
        entity = await self._get(entity_id)
        self._assert_revision(entity, expected_revision)
        currently_locked = bool((entity.metadata_dict or {}).get("user_locked"))
        if currently_locked and locked is not False:
            raise HierarchyLocked(f"Entity {entity_id} is locked; unlock it before editing")
        if currently_locked and text is not None:
            raise HierarchyLocked(f"Entity {entity_id} must be unlocked before its text is edited")

        new_text = None
        metadata = self._edit_metadata(entity, "update")
        if text is not None:
            new_text = text.strip()
            if not new_text:
                raise HierarchyValidationError("Entity text cannot be empty")
            metadata.setdefault("original_text", entity.text)
            metadata["user_text_override"] = new_text
        if locked is not None:
            metadata["user_locked"] = locked
        updated = await self.store.entities.update(
            entity_id, text=new_text, metadata=metadata
        )
        assert updated is not None
        return {"entity": await self._subtree(updated.id)}

    async def _would_cycle(self, entity_id: int, new_parent_id: int) -> bool:
        pending = [new_parent_id]
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if current == entity_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            parent_relations = await self.store.relations.get_by_source(
                current,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
            pending.extend(relation.target_id for relation in parent_relations)
        return False

    async def reparent_entity(
        self,
        entity_id: int,
        *,
        new_parent_id: int,
        expected_revision: int,
    ) -> dict[str, Any]:
        entity = await self._get(entity_id)
        parent = await self._get(new_parent_id)
        self._assert_revision(entity, expected_revision)
        self._assert_unlocked(entity)
        self._assert_unlocked(parent)
        self._validate_parent(entity, parent)
        if await self._would_cycle(entity.id, parent.id):
            raise HierarchyValidationError("Reparenting would create a cycle")

        old_relations = await self.store.relations.get_by_source(
            entity.id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        old_parent_ids = [relation.target_id for relation in old_relations]
        for relation in old_relations:
            await self.store.relations.delete(relation.id)
        await self.store.relations.create(
            source_id=entity.id,
            target_id=parent.id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        metadata = self._edit_metadata(
            entity,
            "reparent",
            user_reassigned=True,
            reassigned_from=old_parent_ids,
            reassigned_to=parent.id,
        )
        await self.store.entities.update(entity.id, metadata=metadata)
        return {"entity": await self._subtree(entity.id), "old_parent_ids": old_parent_ids}

    async def _relation_exists(
        self,
        source_id: int,
        target_id: int,
        relation_type: str,
        relation_subtype: Optional[str],
    ) -> bool:
        result = await self.session.execute(
            select(Relation.id).where(
                Relation.source_id == source_id,
                Relation.target_id == target_id,
                Relation.relation_type == relation_type,
                Relation.relation_subtype == relation_subtype,
            )
        )
        return result.scalar_one_or_none() is not None

    async def merge_entities(
        self,
        *,
        ids: list[int],
        text: Optional[str],
        expected_revisions: dict[int, int],
    ) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(ids))
        if len(unique_ids) < 2:
            raise HierarchyValidationError("Merge requires at least two distinct entity IDs")
        if set(expected_revisions) != set(unique_ids):
            raise HierarchyValidationError("expected_revisions must cover every merged entity")
        entities = [await self._get(entity_id) for entity_id in unique_ids]
        entity_type = entities[0].type
        if any(entity.type != entity_type for entity in entities):
            raise HierarchyValidationError("Only entities of the same type can be merged")
        for entity in entities:
            self._assert_revision(entity, expected_revisions[entity.id])
            self._assert_unlocked(entity)

        survivor = entities[0]
        merged_ids = set(unique_ids)
        relation_result = await self.session.execute(
            select(Relation).where(
                or_(Relation.source_id.in_(merged_ids), Relation.target_id.in_(merged_ids))
            )
        )
        incident = list(relation_result.scalars().all())
        moved_children: dict[int, list[int]] = defaultdict(list)
        for relation in incident:
            if (
                relation.target_id in merged_ids
                and relation.target_id != survivor.id
                and relation.source_id not in merged_ids
                and relation.relation_type == RelationType.STRUCTURAL
                and relation.relation_subtype == RelationSubtype.PART_OF
            ):
                child = await self._get(relation.source_id)
                if PARENT_TYPE.get(child.type) == entity_type:
                    self._assert_unlocked(child)
                    moved_children[child.id].append(relation.target_id)
        for relation in incident:
            await self.store.relations.delete(relation.id)

        relation_specs: list[
            tuple[int, int, str, Optional[str], Optional[float], dict]
        ] = []
        seen_specs: set[tuple[int, int, str, Optional[str]]] = set()
        for relation in incident:
            source_id = (
                survivor.id if relation.source_id in merged_ids else relation.source_id
            )
            target_id = (
                survivor.id if relation.target_id in merged_ids else relation.target_id
            )
            key = (source_id, target_id, relation.relation_type, relation.relation_subtype)
            if source_id == target_id or key in seen_specs:
                continue
            seen_specs.add(key)
            relation_specs.append(
                (*key, relation.confidence, relation.metadata_dict or {})
            )
        for (
            source_id,
            target_id,
            relation_type,
            subtype,
            confidence,
            metadata,
        ) in relation_specs:
            if await self._relation_exists(source_id, target_id, relation_type, subtype):
                continue
            await self.store.relations.create(
                source_id=source_id,
                target_id=target_id,
                relation_type=relation_type,
                relation_subtype=subtype,
                confidence=confidence,
                metadata=metadata or None,
            )

        for child_id, old_parent_ids in moved_children.items():
            child = await self._get(child_id)
            child_metadata = self._edit_metadata(
                child,
                "merge_reparent",
                user_reassigned=True,
                reassigned_from=old_parent_ids,
                reassigned_to=survivor.id,
            )
            await self.store.entities.update(child.id, metadata=child_metadata)

        metadata = self._edit_metadata(
            survivor,
            "merge",
            user_merged=True,
            merged_from=unique_ids[1:],
        )
        metadata.setdefault("original_text", survivor.text)
        for other in entities[1:]:
            other_metadata = other.metadata_dict or {}
            for key in (
                "people",
                "domains",
                "resources",
                "observation_screenshots",
                "context_screenshots",
                "new_screenshots",
            ):
                combined = list(
                    dict.fromkeys(
                        (metadata.get(key) or []) + (other_metadata.get(key) or [])
                    )
                )
                if combined:
                    metadata[key] = combined
        merged_text = text.strip() if text is not None else " — ".join(
            dict.fromkeys(entity.text for entity in entities)
        )
        if not merged_text:
            raise HierarchyValidationError("Merged text cannot be empty")
        await self.store.entities.update(survivor.id, text=merged_text, metadata=metadata)
        for entity in entities[1:]:
            await self.store.entities.delete(entity.id)
        return {"entity": await self._subtree(survivor.id), "merged_ids": unique_ids[1:]}

    async def split_entity(
        self,
        entity_id: int,
        *,
        expected_revision: int,
        into: list[SplitPart],
    ) -> dict[str, Any]:
        if len(into) < 2:
            raise HierarchyValidationError("Split requires at least two output entities")
        entity = await self._get(entity_id)
        self._assert_revision(entity, expected_revision)
        self._assert_unlocked(entity)

        child_relations = await self.store.relations.get_by_target(
            entity.id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        current_child_ids = {relation.source_id for relation in child_relations}
        provided_child_ids = [child_id for part in into for child_id in part.child_ids]
        if len(provided_child_ids) != len(set(provided_child_ids)):
            raise HierarchyValidationError("A child can appear in only one split output")
        if set(provided_child_ids) != current_child_ids:
            raise HierarchyValidationError(
                "Split child_ids must partition all current children exactly once"
            )
        for part in into:
            if not part.text.strip():
                raise HierarchyValidationError("Split text cannot be empty")

        outgoing = await self.store.relations.get_by_source(entity.id)
        incoming_non_children = [
            relation
            for relation in await self.store.relations.get_by_target(entity.id)
            if relation.id not in {child.id for child in child_relations}
        ]
        original_metadata = dict(entity.metadata_dict or {})
        created: list[Entity] = []
        for index, part in enumerate(into):
            group_children = [await self._get(child_id) for child_id in part.child_ids]
            starts = [child.timestamp_start for child in group_children]
            ends = [child.timestamp_end or child.timestamp_start for child in group_children]
            metadata = dict(original_metadata)
            metadata.update(
                {
                    "revision": 0,
                    "user_edited": True,
                    "user_provided": True,
                    "user_split_from": entity.id,
                    "split_index": index,
                    "edited_at": _edited_at(),
                    "last_user_edit": "split",
                    "user_locked": False,
                }
            )
            new_entity = await self.store.entities.create(
                entity_type=entity.type,
                text=part.text.strip(),
                timestamp_start=min(starts) if starts else entity.timestamp_start,
                timestamp_end=max(ends) if ends else entity.timestamp_end,
                metadata=metadata,
            )
            created.append(new_entity)
            for child in group_children:
                await self.store.relations.create(
                    source_id=child.id,
                    target_id=new_entity.id,
                    relation_type=RelationType.STRUCTURAL,
                    relation_subtype=RelationSubtype.PART_OF,
                )
                child_metadata = self._edit_metadata(
                    child,
                    "split_reparent",
                    user_reassigned=True,
                    reassigned_from=[entity.id],
                    reassigned_to=new_entity.id,
                )
                await self.store.entities.update(child.id, metadata=child_metadata)

            for relation in outgoing:
                if relation.target_id == entity.id:
                    continue
                await self.store.relations.create(
                    source_id=new_entity.id,
                    target_id=relation.target_id,
                    relation_type=relation.relation_type,
                    relation_subtype=relation.relation_subtype,
                    confidence=relation.confidence,
                    metadata=relation.metadata_dict or None,
                )
            for relation in incoming_non_children:
                if relation.source_id == entity.id:
                    continue
                await self.store.relations.create(
                    source_id=relation.source_id,
                    target_id=new_entity.id,
                    relation_type=relation.relation_type,
                    relation_subtype=relation.relation_subtype,
                    confidence=relation.confidence,
                    metadata=relation.metadata_dict or None,
                )

        await self.store.entities.delete(entity.id)
        return {
            "entities": [await self._subtree(created_entity.id) for created_entity in created],
            "split_from": entity.id,
        }

    async def delete_entity(
        self,
        entity_id: int,
        *,
        expected_revision: int,
        reparent_children_to: Optional[int],
    ) -> dict[str, Any]:
        entity = await self._get(entity_id)
        self._assert_revision(entity, expected_revision)
        self._assert_unlocked(entity)
        deleted_snapshot = self._entity_dict(entity)
        child_relations = await self.store.relations.get_by_target(
            entity.id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        replacement = None
        if reparent_children_to is not None:
            replacement = await self._get(reparent_children_to)
            self._assert_unlocked(replacement)
            if replacement.id == entity.id or replacement.type != entity.type:
                raise HierarchyValidationError(
                    "The replacement parent must be a different entity of the deleted entity's type"
                )

        affected_child_ids = []
        for relation in child_relations:
            child = await self._get(relation.source_id)
            await self.store.relations.delete(relation.id)
            if replacement is not None and not await self._relation_exists(
                child.id,
                replacement.id,
                RelationType.STRUCTURAL,
                RelationSubtype.PART_OF,
            ):
                self._validate_parent(child, replacement)
                if await self._would_cycle(child.id, replacement.id):
                    raise HierarchyValidationError("Child reparenting would create a cycle")
                await self.store.relations.create(
                    source_id=child.id,
                    target_id=replacement.id,
                    relation_type=RelationType.STRUCTURAL,
                    relation_subtype=RelationSubtype.PART_OF,
                )
            child_metadata = self._edit_metadata(
                child,
                "delete_reparent" if replacement else "delete_orphan",
                user_reassigned=True,
                reassigned_from=[entity.id],
                reassigned_to=replacement.id if replacement else None,
            )
            await self.store.entities.update(child.id, metadata=child_metadata)
            affected_child_ids.append(child.id)

        await self.store.entities.delete(entity.id)
        return {
            "deleted": deleted_snapshot,
            "children": [await self._subtree(child_id) for child_id in affected_child_ids],
        }

    async def create_entity(
        self,
        *,
        entity_type: str,
        text: str,
        parent_id: Optional[int],
    ) -> dict[str, Any]:
        if entity_type not in HIERARCHY_TYPES:
            raise HierarchyValidationError(f"Unsupported entity type: {entity_type}")
        clean_text = text.strip()
        if not clean_text:
            raise HierarchyValidationError("Entity text cannot be empty")
        parent = await self._get(parent_id) if parent_id is not None else None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        metadata = {
            "revision": 0,
            "user_edited": True,
            "user_provided": True,
            "edited_at": _edited_at(),
            "last_user_edit": "create",
        }
        entity = await self.store.entities.create(
            entity_type=entity_type,
            text=clean_text,
            timestamp_start=now,
            metadata=metadata,
        )
        if parent is not None:
            self._assert_unlocked(parent)
            self._validate_parent(entity, parent)
            await self.store.relations.create(
                source_id=entity.id,
                target_id=parent.id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
        return {"entity": await self._subtree(entity.id)}
