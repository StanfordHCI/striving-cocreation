# store.py

from __future__ import annotations
from datetime import datetime, timedelta
import asyncio
import json
from typing import Optional, List, Tuple, Dict, Any

from sqlalchemy import select, delete, update, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import OperationalError

from tempo.models import Entity, Relation, BatchSnapshot, EntityType, RelationType, RelationSubtype


async def _flush_with_retry(session: AsyncSession, max_retries: int = 5, base_delay: float = 0.05):
    """
    Retry session.flush() on transient SQLite lock errors with backoff.
    """
    for attempt in range(max_retries):
        try:
            await session.flush()
            return
        except OperationalError as e:
            # Check for SQLite lock error text
            if "database is locked" in str(e).lower():
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(base_delay * (2 ** attempt))
            else:
                raise


class EntityStore:
    """CRUD operations for Entity objects."""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create(
        self,
        entity_type: str,
        text: str,
        timestamp_start: datetime,
        timestamp_end: Optional[datetime] = None,
        embedding: Optional[bytes] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Entity:
        """
        Create a new entity.
        
        Args:
            entity_type: Type of entity ('operation', 'action', 'activity').
            text: Text description of the entity.
            timestamp_start: Start timestamp.
            timestamp_end: End timestamp (optional).
            embedding: Optional embedding bytes.
            metadata: Additional metadata (optional).
            
        Returns:
            The created Entity.
        """
        entity = Entity(
            type=entity_type,
            text=text,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            embedding=embedding,
        )
        initial_metadata = dict(metadata or {})
        initial_metadata.setdefault("revision", 0)
        entity.metadata_dict = initial_metadata
        
        self.session.add(entity)
        await _flush_with_retry(self.session)
        return entity
    
    async def get(self, entity_id: int) -> Optional[Entity]:
        """Get an entity by ID."""
        result = await self.session.execute(
            select(Entity).where(Entity.id == entity_id)
        )
        return result.scalar_one_or_none()
    
    async def get_with_relations(self, entity_id: int) -> Optional[Entity]:
        """Get an entity with its relations preloaded."""
        result = await self.session.execute(
            select(Entity)
            .where(Entity.id == entity_id)
            .options(
                selectinload(Entity.outgoing_relations),
                selectinload(Entity.incoming_relations),
            )
        )
        return result.scalar_one_or_none()
    
    async def get_by_type(
        self,
        entity_type: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Entity]:
        """
        Get entities by type, optionally filtered by time range.

        Args:
            entity_type: Type of entity to retrieve.
            since: Only return entities with timestamp_start >= since.
            until: Only return entities with timestamp_start <= until.
            limit: Maximum number of entities to return.
            offset: Number of entities to skip (for pagination).

        Returns:
            List of entities.
        """
        query = select(Entity).where(Entity.type == entity_type)

        if since:
            query = query.where(Entity.timestamp_start >= since)
        if until:
            query = query.where(Entity.timestamp_start <= until)

        query = query.order_by(Entity.timestamp_start.desc())

        if offset:
            query = query.offset(offset)

        if limit:
            query = query.limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_by_type(
        self,
        entity_type: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> int:
        """Count entities of a given type."""
        from sqlalchemy import func
        query = select(func.count()).select_from(Entity).where(Entity.type == entity_type)
        if since:
            query = query.where(Entity.timestamp_start >= since)
        if until:
            query = query.where(Entity.timestamp_start <= until)
        result = await self.session.execute(query)
        return result.scalar() or 0
    
    async def update(
        self,
        entity_id: int,
        text: Optional[str] = None,
        embedding: Optional[bytes] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp_start: Optional[datetime] = None,
        timestamp_end: Optional[datetime] = None,
    ) -> Optional[Entity]:
        """
        Update an entity.

        Args:
            entity_id: ID of the entity to update.
            text: New text (optional).
            embedding: New embedding (optional).
            metadata: New metadata (optional).
            timestamp_start: New start timestamp (optional).
            timestamp_end: New end timestamp (optional).

        Returns:
            The updated Entity, or None if not found.
        """
        entity = await self.get(entity_id)
        if entity is None:
            return None

        current_metadata = entity.metadata_dict or {}
        try:
            current_revision = int(current_metadata.get("revision", 0) or 0)
        except (TypeError, ValueError):
            current_revision = 0

        if text is not None:
            entity.text = text
        if embedding is not None:
            entity.embedding = embedding
        next_metadata = dict(metadata) if metadata is not None else dict(current_metadata)
        next_metadata["revision"] = current_revision + 1
        entity.metadata_dict = next_metadata
        if timestamp_start is not None:
            entity.timestamp_start = timestamp_start
        if timestamp_end is not None:
            entity.timestamp_end = timestamp_end

        await _flush_with_retry(self.session)
        return entity
    
    async def delete(self, entity_id: int) -> bool:
        """
        Delete an entity.
        
        Args:
            entity_id: ID of the entity to delete.
            
        Returns:
            True if deleted, False if not found.
        """
        result = await self.session.execute(
            delete(Entity).where(Entity.id == entity_id)
        )
        return result.rowcount > 0
    
    async def get_unassigned_operations(
        self,
        since: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[Entity]:
        """
        Get operations that are not yet assigned to any action.
        
        Args:
            since: Only return operations with timestamp_start >= since.
            limit: Maximum number of operations to return.
            
        Returns:
            List of unassigned operations.
        """
        # Operations without any outgoing 'structural' relation
        subquery = (
            select(Relation.source_id)
            .where(Relation.relation_type == RelationType.STRUCTURAL)
            .where(Relation.relation_subtype == RelationSubtype.PART_OF)
        )
        
        query = (
            select(Entity)
            .where(Entity.type == EntityType.OPERATION)
            .where(~Entity.id.in_(subquery))
        )
        
        if since:
            query = query.where(Entity.timestamp_start >= since)
        
        query = query.order_by(Entity.timestamp_start.asc())
        
        if limit:
            query = query.limit(limit)
        
        result = await self.session.execute(query)
        return list(result.scalars().all())
    
    async def get_unassigned_actions(
        self,
        since: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[Entity]:
        """
        Get actions that are not yet assigned to any activity.
        
        Args:
            since: Only return actions with timestamp_start >= since.
            limit: Maximum number of actions to return.
            
        Returns:
            List of unassigned actions.
        """
        # Actions without any outgoing 'structural' relation to an activity
        subquery = (
            select(Relation.source_id)
            .join(Entity, Relation.target_id == Entity.id)
            .where(Relation.relation_type == RelationType.STRUCTURAL)
            .where(Relation.relation_subtype == RelationSubtype.PART_OF)
            .where(Entity.type == EntityType.ACTIVITY)
        )
        
        query = (
            select(Entity)
            .where(Entity.type == EntityType.ACTION)
            .where(~Entity.id.in_(subquery))
        )
        
        if since:
            query = query.where(Entity.timestamp_start >= since)
        
        query = query.order_by(Entity.timestamp_start.asc())
        
        if limit:
            query = query.limit(limit)
        
        result = await self.session.execute(query)
        return list(result.scalars().all())
    
    async def get_actions_in_unstable_activities(
        self,
        max_usage_count: int = 2,
        max_age_hours: int = 24,
        limit: Optional[int] = None,
    ) -> List[Entity]:
        """
        Get actions linked to brand-new or unstable activities.
        
        An activity is considered unstable if:
        - usage_count <= max_usage_count (few actions assigned to it)
        - created_at is within max_age_hours (very recently created)
        
        Args:
            max_usage_count: Maximum usage_count for an activity to be considered unstable.
            max_age_hours: Maximum age in hours for an activity to be considered unstable.
            limit: Maximum number of actions to return.
            
        Returns:
            List of actions in unstable activities.
        """
        # Get unstable activities
        cutoff_time = datetime.utcnow() - timedelta(hours=max_age_hours)
        
        activity_query = (
            select(Entity)
            .where(Entity.type == EntityType.ACTIVITY)
            .where(
                or_(
                    Entity.created_at >= cutoff_time,
                    Entity.metadata_json.isnot(None)
                )
            )
        )
        
        activity_result = await self.session.execute(activity_query)
        all_activities = list(activity_result.scalars().all())
        
        unstable_activity_ids = []
        for activity in all_activities:
            # Check age
            if activity.created_at >= cutoff_time:
                unstable_activity_ids.append(activity.id)
                continue
            
            # Check usage_count
            meta = activity.metadata_dict
            usage_count = meta.get("usage_count", 0)
            if usage_count <= max_usage_count:
                unstable_activity_ids.append(activity.id)
        
        if not unstable_activity_ids:
            return []
        
        # Get actions that are PART_OF these unstable activities
        action_query = (
            select(Entity)
            .distinct()
            .join(Relation, Relation.source_id == Entity.id)
            .where(Entity.type == EntityType.ACTION)
            .where(Relation.target_id.in_(unstable_activity_ids))
            .where(Relation.relation_type == RelationType.STRUCTURAL)
            .where(Relation.relation_subtype == RelationSubtype.PART_OF)
        )
        
        action_query = action_query.order_by(Entity.timestamp_start.desc())
        
        if limit:
            action_query = action_query.limit(limit)
        
        action_result = await self.session.execute(action_query)
        return list(action_result.scalars().all())


class RelationStore:
    """CRUD operations for Relation objects."""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create(
        self,
        source_id: int,
        target_id: int,
        relation_type: str,
        relation_subtype: Optional[str] = None,
        confidence: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Relation:
        """
        Create a new relation.
        
        Args:
            source_id: ID of the source entity.
            target_id: ID of the target entity.
            relation_type: Type of relation.
            relation_subtype: Subtype of relation (optional).
            confidence: Confidence score 0.0-1.0 (optional).
            metadata: Additional metadata (optional).
            
        Returns:
            The created Relation.
        """
        relation = Relation(
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
            relation_subtype=relation_subtype,
            confidence=confidence,
        )
        if metadata:
            relation.metadata_dict = metadata
        
        self.session.add(relation)
        await _flush_with_retry(self.session)
        return relation
    
    async def get(self, relation_id: int) -> Optional[Relation]:
        """Get a relation by ID."""
        result = await self.session.execute(
            select(Relation).where(Relation.id == relation_id)
        )
        return result.scalar_one_or_none()
    
    async def get_by_source(
        self,
        source_id: int,
        relation_type: Optional[str] = None,
        relation_subtype: Optional[str] = None,
    ) -> List[Relation]:
        """
        Get relations by source entity.
        
        Args:
            source_id: ID of the source entity.
            relation_type: Filter by relation type (optional).
            relation_subtype: Filter by relation subtype (optional).
            
        Returns:
            List of relations.
        """
        if relation_subtype and not relation_type:
            raise ValueError("relation_subtype requires relation_type to disambiguate shared subtypes.")
        query = select(Relation).where(Relation.source_id == source_id)
        
        if relation_type:
            query = query.where(Relation.relation_type == relation_type)
        if relation_subtype:
            query = query.where(Relation.relation_subtype == relation_subtype)
        
        result = await self.session.execute(query)
        return list(result.scalars().all())
    
    async def get_by_target(
        self,
        target_id: int,
        relation_type: Optional[str] = None,
        relation_subtype: Optional[str] = None,
    ) -> List[Relation]:
        """
        Get relations by target entity.
        
        Args:
            target_id: ID of the target entity.
            relation_type: Filter by relation type (optional).
            relation_subtype: Filter by relation subtype (optional).
            
        Returns:
            List of relations.
        """
        if relation_subtype and not relation_type:
            raise ValueError("relation_subtype requires relation_type to disambiguate shared subtypes.")
        query = select(Relation).where(Relation.target_id == target_id)
        
        if relation_type:
            query = query.where(Relation.relation_type == relation_type)
        if relation_subtype:
            query = query.where(Relation.relation_subtype == relation_subtype)
        
        result = await self.session.execute(query)
        return list(result.scalars().all())
    
    async def get_related_entities(
        self,
        entity_id: int,
        relation_types: Optional[List[str]] = None,
        direction: str = "both",
    ) -> List[Entity]:
        """
        Get entities related to the given entity.
        
        Args:
            entity_id: ID of the entity.
            relation_types: Filter by relation types (optional).
            direction: 'outgoing', 'incoming', or 'both'.
            
        Returns:
            List of related entities.
        """
        entities = []
        
        if direction in ("outgoing", "both"):
            query = (
                select(Entity)
                .join(Relation, Relation.target_id == Entity.id)
                .where(Relation.source_id == entity_id)
            )
            if relation_types:
                query = query.where(Relation.relation_type.in_(relation_types))
            result = await self.session.execute(query)
            entities.extend(result.scalars().all())
        
        if direction in ("incoming", "both"):
            query = (
                select(Entity)
                .join(Relation, Relation.source_id == Entity.id)
                .where(Relation.target_id == entity_id)
            )
            if relation_types:
                query = query.where(Relation.relation_type.in_(relation_types))
            result = await self.session.execute(query)
            entities.extend(result.scalars().all())
        
        # Deduplicate
        seen = set()
        unique_entities = []
        for e in entities:
            if e.id not in seen:
                seen.add(e.id)
                unique_entities.append(e)
        
        return unique_entities
    
    async def update(self, relation_id: int, metadata: Optional[dict] = None) -> Optional[Relation]:
        """Update a relation's metadata."""
        rel = await self.get(relation_id)
        if not rel:
            return None
        if metadata is not None:
            rel.metadata_json = json.dumps(metadata)
        await self.session.flush()
        return rel

    async def delete(self, relation_id: int) -> bool:
        """
        Delete a relation.

        Args:
            relation_id: ID of the relation to delete.

        Returns:
            True if deleted, False if not found.
        """
        result = await self.session.execute(
            delete(Relation).where(Relation.id == relation_id)
        )
        return result.rowcount > 0
    
    async def delete_by_source(self, source_id: int) -> int:
        """
        Delete all relations with the given source.
        
        Args:
            source_id: ID of the source entity.
            
        Returns:
            Number of deleted relations.
        """
        result = await self.session.execute(
            delete(Relation).where(Relation.source_id == source_id)
        )
        return result.rowcount


class Store:
    """Combined store for entities and relations."""
    
    def __init__(self, session: AsyncSession):
        self.session = session
        self.entities = EntityStore(session)
        self.relations = RelationStore(session)
    
    async def create_operation(
        self,
        text: str,
        timestamp: datetime,
        embedding: Optional[bytes] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Entity:
        """Convenience method to create an operation."""
        return await self.entities.create(
            entity_type=EntityType.OPERATION,
            text=text,
            timestamp_start=timestamp,
            embedding=embedding,
            metadata=metadata,
        )
    
    async def create_action(
        self,
        text: str,
        timestamp_start: datetime,
        timestamp_end: Optional[datetime],
        operation_ids: List[int],
        embedding: Optional[bytes] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Entity:
        """
        Create an action and link it to its operations.
        
        Args:
            text: Action description.
            timestamp_start: Start timestamp.
            timestamp_end: End timestamp (optional).
            operation_ids: IDs of operations that comprise this action.
            embedding: Optional embedding bytes.
            metadata: Additional metadata (optional).
            
        Returns:
            The created action Entity.
        """
        action = await self.entities.create(
            entity_type=EntityType.ACTION,
            text=text,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            embedding=embedding,
            metadata=metadata,
        )
        
        # Create structural relations from operations to action
        for op_id in operation_ids:
            await self.relations.create(
                source_id=op_id,
                target_id=action.id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
        
        return action
    
    async def create_activity(
        self,
        text: str,
        action_ids: List[int],
        embedding: Optional[bytes] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Entity:
        """
        Create an activity and link it to its actions.
        
        Args:
            text: Activity description.
            action_ids: IDs of actions that comprise this activity.
            embedding: Optional embedding bytes.
            metadata: Additional metadata (optional).
            
        Returns:
            The created activity Entity.
        """
        # Get timestamp range from actions
        actions = []
        for aid in action_ids:
            action = await self.entities.get(aid)
            if action:
                actions.append(action)
        
        if actions:
            timestamp_start = min(a.timestamp_start for a in actions)
            timestamp_end = max(a.timestamp_end or a.timestamp_start for a in actions)
        else:
            timestamp_start = datetime.utcnow()
            timestamp_end = None
        
        activity = await self.entities.create(
            entity_type=EntityType.ACTIVITY,
            text=text,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            embedding=embedding,
            metadata=metadata,
        )
        
        # Create structural relations from actions to activity
        for action_id in action_ids:
            await self.relations.create(
                source_id=action_id,
                target_id=activity.id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )
        
        return activity
    
    async def create_goal(
        self,
        text: str,
        activity_ids: List[int],
        embedding: Optional[bytes] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Entity:
        """
        Create a goal and link it to its constituent activities.

        Args:
            text: Goal description (e.g., "wants to ...").
            activity_ids: IDs of activities that serve this goal.
            embedding: Optional embedding bytes.
            metadata: Additional metadata (optional).

        Returns:
            The created goal Entity.
        """
        activities = []
        for aid in activity_ids:
            activity = await self.entities.get(aid)
            if activity:
                activities.append(activity)

        if activities:
            timestamp_start = min(a.timestamp_start for a in activities)
            timestamp_end = max(a.timestamp_end or a.timestamp_start for a in activities)
        else:
            timestamp_start = datetime.utcnow()
            timestamp_end = None

        goal = await self.entities.create(
            entity_type=EntityType.GOAL,
            text=text,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            embedding=embedding,
            metadata=metadata,
        )

        for activity_id in activity_ids:
            await self.relations.create(
                source_id=activity_id,
                target_id=goal.id,
                relation_type=RelationType.STRUCTURAL,
                relation_subtype=RelationSubtype.PART_OF,
            )

        return goal

    async def create_user_goal(
        self,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Entity:
        """
        Create a user-provided goal (no activity relations yet).

        Args:
            text: Goal description.
            metadata: Additional metadata (merged with user_provided defaults).

        Returns:
            The created goal Entity.
        """
        meta = metadata.copy() if metadata else {}
        meta.setdefault("user_provided", True)
        meta.setdefault("source", "user_provided")
        meta.setdefault("status", "active")

        return await self.entities.create(
            entity_type=EntityType.GOAL,
            text=text,
            timestamp_start=datetime.utcnow(),
            metadata=meta,
        )

    async def get_goals(
        self,
        status: Optional[str] = None,
    ) -> List[Entity]:
        """
        Get all goal entities, optionally filtered by status.

        Args:
            status: If provided, only return goals with this metadata status.

        Returns:
            List of goal entities.
        """
        goals = await self.entities.get_by_type(EntityType.GOAL)
        if status is not None:
            goals = [
                g for g in goals
                if (g.metadata_dict or {}).get("status") == status
            ]
        return goals

    async def get_user_provided_goals(self) -> List[Entity]:
        """Get all user-provided goals."""
        goals = await self.entities.get_by_type(EntityType.GOAL)
        return [
            g for g in goals
            if (g.metadata_dict or {}).get("user_provided") is True
        ]

    async def get_goal_with_activities(
        self,
        goal_id: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Get a goal entity with its constituent activities.

        Args:
            goal_id: ID of the goal.

        Returns:
            Dict with 'goal' and 'activities' keys, or None if not found.
        """
        goal = await self.entities.get(goal_id)
        if not goal or goal.type != EntityType.GOAL:
            return None

        relations = await self.relations.get_by_target(
            goal_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )

        activities = []
        for rel in (relations or []):
            activity = await self.entities.get(rel.source_id)
            if activity and activity.type == EntityType.ACTIVITY:
                activities.append(activity)

        return {"goal": goal, "activities": activities}

    async def add_temporal_relation(
        self,
        source_id: int,
        target_id: int,
        subtype: str,
        confidence: Optional[float] = None,
    ) -> Relation:
        """Add a temporal relation between entities."""
        return await self.relations.create(
            source_id=source_id,
            target_id=target_id,
            relation_type=RelationType.TEMPORAL,
            relation_subtype=subtype,
            confidence=confidence,
        )
    
    async def add_behavioral_relation(
        self,
        source_id: int,
        target_id: int,
        subtype: str,
        confidence: Optional[float] = None,
    ) -> Relation:
        """Add a behavioral relation between entities."""
        return await self.relations.create(
            source_id=source_id,
            target_id=target_id,
            relation_type=RelationType.BEHAVIORAL,
            relation_subtype=subtype,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Batch snapshots (stability metrics)
    # ------------------------------------------------------------------

    async def capture_snapshot(
        self,
        batch_id: int,
        stage: str,
        result: Dict[str, Any],
    ) -> BatchSnapshot:
        """Capture a snapshot of active entities after a reconcile batch.

        Args:
            batch_id: Sequential batch identifier.
            stage: "stage3" (activities) or "stage4" (goals).
            result: The reconcile result dict with births/matches/merges/revisions counts.

        Returns:
            The created BatchSnapshot row.
        """
        entity_type = EntityType.ACTIVITY if stage == "stage3" else EntityType.GOAL
        all_entities = await self.entities.get_by_type(entity_type, limit=500)

        active_entities = [
            e for e in all_entities
            if (e.metadata_dict or {}).get("status", "active") in ("active", "probation")
        ]
        active_ids = sorted(e.id for e in active_entities)
        label_map = {str(e.id): e.text for e in active_entities}

        snapshot = BatchSnapshot(
            batch_id=batch_id,
            timestamp=datetime.utcnow(),
            stage=stage,
            active_entity_ids=json.dumps(active_ids),
            entity_labels=json.dumps(label_map),
            births=result.get("created", 0),
            matches=result.get("matched", 0),
            merges=result.get("merged", 0),
            revisions=result.get("revised", 0),
            total_active=len(active_ids),
        )
        self.session.add(snapshot)
        await _flush_with_retry(self.session)
        return snapshot

    async def get_snapshots(
        self,
        stage: str,
        *,
        limit: int = 200,
    ) -> List[BatchSnapshot]:
        """Get snapshots for a stage ordered by batch_id ascending."""
        stmt = (
            select(BatchSnapshot)
            .where(BatchSnapshot.stage == stage)
            .order_by(BatchSnapshot.batch_id.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
