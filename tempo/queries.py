# queries.py

from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass

from tempo.models import Entity, Relation, EntityType, RelationType, RelationSubtype

if TYPE_CHECKING:
    from tempo.store import Store


@dataclass
class EntityResult:
    """A query result with entity and optional score."""
    entity: Entity
    score: Optional[float] = None
    relations: Optional[List[Relation]] = None


@dataclass
class TraceResult:
    """Result of a trace_up or trace_down query."""
    path: List[Entity]
    depth: int


class QueryInterface:
    """
    High-level query interface for Tempo.
    
    Provides methods for applications to query the behavioral graph.
    """
    
    def __init__(
        self,
        store: "Store",
    ):
        """
        Initialize the query interface.

        Args:
            store: Database store.
        """
        self.store = store
    
    async def get_entity(self, entity_id: int) -> Optional[Entity]:
        """
        Get an entity by ID.
        
        Args:
            entity_id: The entity ID.
            
        Returns:
            The entity, or None if not found.
        """
        return await self.store.entities.get(entity_id)
    
    async def get_entity_with_relations(
        self,
        entity_id: int,
    ) -> Optional[EntityResult]:
        """
        Get an entity with its relations.
        
        Args:
            entity_id: The entity ID.
            
        Returns:
            EntityResult with entity and relations, or None if not found.
        """
        entity = await self.store.entities.get_with_relations(entity_id)
        if not entity:
            return None
        
        relations = list(entity.outgoing_relations) + list(entity.incoming_relations)
        return EntityResult(entity=entity, relations=relations)
    
    async def search_entities(
        self,
        query: str,
        entity_type: Optional[str] = None,
        top_k: int = 10,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[EntityResult]:
        """BM25 search for entities using FTS5.

        Uses SQLite FTS5 full-text search with porter stemming, temporal
        decay weighting, and MMR diversity reranking (via audit/bm25).
        Falls back to simple substring matching if FTS5 is not available.

        Args:
            query: Search query.
            entity_type: Filter by entity type ('operation', 'action', 'activity', 'goal').
            top_k: Number of results to return.
            since: Only return entities after this time.
            until: Only return entities before this time.

        Returns:
            List of EntityResults sorted by relevance.
        """
        from tempo.pipelines.audit.bm25 import bm25_search

        logger = logging.getLogger(__name__)

        # If a specific type is given, search just that type.
        # Otherwise search across all meaningful types.
        types_to_search = (
            [entity_type]
            if entity_type
            else [EntityType.GOAL, EntityType.ACTIVITY, EntityType.ACTION]
        )

        all_results: List[EntityResult] = []
        for etype in types_to_search:
            try:
                hits = await bm25_search(
                    session=self.store.session,
                    query=query,
                    entity_type=etype,
                    limit=top_k,
                    enable_decay=True,
                    enable_mmr=len(types_to_search) == 1,  # MMR within single type
                )
                if not hits:
                    all_results.extend(
                        await self._text_search_fallback(query, etype, top_k, since, until)
                    )
                    continue
                for entity, score in hits:
                    all_results.append(EntityResult(entity=entity, score=score))
            except Exception as e:
                logger.debug("BM25 search failed for type=%s: %s, falling back to substring", etype, e)
                # Fallback: simple substring match for this type
                all_results.extend(
                    await self._text_search_fallback(query, etype, top_k, since, until)
                )

        # Sort by score descending, take top_k
        all_results.sort(key=lambda r: r.score or 0, reverse=True)
        return all_results[:top_k]

    async def _text_search_fallback(
        self,
        query: str,
        entity_type: str,
        top_k: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[EntityResult]:
        """Simple substring search fallback when FTS5 is unavailable."""
        query_lower = query.lower()
        candidates = await self.store.entities.get_by_type(
            entity_type,
            since=since,
            until=until,
            limit=100,
        )
        matches = []
        for entity in candidates:
            if query_lower in entity.text.lower():
                matches.append(EntityResult(entity=entity, score=1.0))
        return matches[:top_k]
    
    async def get_relations(
        self,
        source_id: int,
        relation_type: Optional[str] = None,
        relation_subtype: Optional[str] = None,
    ) -> List[Relation]:
        """
        Get relations from a source entity.
        
        Args:
            source_id: Source entity ID.
            relation_type: Filter by relation type.
            relation_subtype: Filter by relation subtype.
            
        Returns:
            List of relations.
        """
        return await self.store.relations.get_by_source(
            source_id,
            relation_type,
            relation_subtype,
        )
    
    async def trace_down(
        self,
        entity_id: int,
        max_depth: int = 10,
    ) -> List[TraceResult]:
        """
        Trace down from an entity to its constituent parts.
        
        For example, trace from Activity -> Actions -> Operations.
        
        Args:
            entity_id: Starting entity ID.
            max_depth: Maximum depth to traverse.
            
        Returns:
            List of TraceResults representing paths to leaf entities.
        """
        results = []
        await self._trace_down_recursive(entity_id, [], 0, max_depth, results)
        return results
    
    async def _trace_down_recursive(
        self,
        entity_id: int,
        current_path: List[Entity],
        depth: int,
        max_depth: int,
        results: List[TraceResult],
    ) -> None:
        """Recursive helper for trace_down."""
        if depth > max_depth:
            return
        
        entity = await self.store.entities.get(entity_id)
        if not entity:
            return
        
        path = current_path + [entity]
        
        # Get incoming structural relations (parts)
        relations = await self.store.relations.get_by_target(
            entity_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        
        if not relations:
            # Leaf node
            results.append(TraceResult(path=path, depth=depth))
        else:
            # Continue tracing
            for rel in relations:
                await self._trace_down_recursive(
                    rel.source_id,
                    path,
                    depth + 1,
                    max_depth,
                    results,
                )
    
    async def trace_up(
        self,
        entity_id: int,
        max_depth: int = 10,
    ) -> List[TraceResult]:
        """
        Trace up from an entity to higher-level abstractions.
        
        For example, trace from Operation -> Action -> Activity.
        
        Args:
            entity_id: Starting entity ID.
            max_depth: Maximum depth to traverse.
            
        Returns:
            List of TraceResults representing paths to root entities.
        """
        results = []
        await self._trace_up_recursive(entity_id, [], 0, max_depth, results)
        return results
    
    async def _trace_up_recursive(
        self,
        entity_id: int,
        current_path: List[Entity],
        depth: int,
        max_depth: int,
        results: List[TraceResult],
    ) -> None:
        """Recursive helper for trace_up."""
        if depth > max_depth:
            return
        
        entity = await self.store.entities.get(entity_id)
        if not entity:
            return
        
        path = current_path + [entity]
        
        # Get outgoing structural relations (parent)
        relations = await self.store.relations.get_by_source(
            entity_id,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        
        if not relations:
            # Root node
            results.append(TraceResult(path=path, depth=depth))
        else:
            # Continue tracing
            for rel in relations:
                await self._trace_up_recursive(
                    rel.target_id,
                    path,
                    depth + 1,
                    max_depth,
                    results,
                )
    
    async def get_activity_timeline(
        self,
        days: int = 7,
    ) -> List[Entity]:
        """
        Get activities from the past N days.
        
        Args:
            days: Number of days to look back.
            
        Returns:
            List of activities ordered by time.
        """
        since = datetime.utcnow() - timedelta(days=days)
        return await self.store.entities.get_by_type(
            EntityType.ACTIVITY,
            since=since,
        )
    
    async def get_behavioral_relations(
        self,
        activity_id: int,
    ) -> Dict[str, List[Entity]]:
        """
        Get behavioral relations for an activity.
        
        Args:
            activity_id: Activity entity ID.
            
        Returns:
            Dict with 'supports', 'hinders', 'maintains' keys mapping to related activities.
        """
        result = {
            "supports": [],
            "hinders": [],
            "maintains": [],
        }
        
        for subtype in result.keys():
            relations = await self.store.relations.get_by_source(
                activity_id,
                relation_type=RelationType.BEHAVIORAL,
                relation_subtype=subtype,
            )
            for rel in relations:
                entity = await self.store.entities.get(rel.target_id)
                if entity:
                    result[subtype].append(entity)
        
        return result
    
    async def find_contributors(
        self,
        goal_id: int,
    ) -> Dict[str, List[Entity]]:
        """
        Find activities that support or hinder a goal.
        
        Args:
            goal_id: Goal/activity entity ID.
            
        Returns:
            Dict with 'positive' and 'negative' contributors.
        """
        result = {
            "positive": [],
            "negative": [],
        }
        
        # Find supporting activities
        supports = await self.store.relations.get_by_target(
            goal_id,
            relation_type=RelationType.BEHAVIORAL,
            relation_subtype=RelationSubtype.SUPPORTS,
        )
        for rel in supports:
            entity = await self.store.entities.get(rel.source_id)
            if entity:
                result["positive"].append(entity)
        
        # Find hindering activities
        hinders = await self.store.relations.get_by_target(
            goal_id,
            relation_type=RelationType.BEHAVIORAL,
            relation_subtype=RelationSubtype.HINDERS,
        )
        for rel in hinders:
            entity = await self.store.entities.get(rel.source_id)
            if entity:
                result["negative"].append(entity)
        
        return result
    
    async def get_temporal_sequence(
        self,
        entity_id: int,
        direction: str = "both",
        limit: int = 10,
    ) -> Dict[str, List[Entity]]:
        """
        Get entities in temporal sequence with the given entity.
        
        Args:
            entity_id: Entity ID.
            direction: 'before', 'after', or 'both'.
            limit: Maximum entities per direction.
            
        Returns:
            Dict with 'before' and 'after' keys.
        """
        result = {"before": [], "after": []}
        
        if direction in ("before", "both"):
            # Get entities that this one follows
            relations = await self.store.relations.get_by_source(
                entity_id,
                relation_type=RelationType.TEMPORAL,
                relation_subtype=RelationSubtype.FOLLOWS,
            )
            for rel in relations[:limit]:
                entity = await self.store.entities.get(rel.target_id)
                if entity:
                    result["before"].append(entity)
        
        if direction in ("after", "both"):
            # Get entities that follow this one
            relations = await self.store.relations.get_by_target(
                entity_id,
                relation_type=RelationType.TEMPORAL,
                relation_subtype=RelationSubtype.FOLLOWS,
            )
            for rel in relations[:limit]:
                entity = await self.store.entities.get(rel.source_id)
                if entity:
                    result["after"].append(entity)
        
        return result

