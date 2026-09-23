# models.py

from __future__ import annotations
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
import json

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Float,
    LargeBinary,
    ForeignKey,
    Index,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class Entity(Base):
    """
    A unified table for all entity types in the Activity Theory hierarchy.
    
    Types:
    - 'operation': Atomic behavioral events (clicks, scrolls, keystrokes)
    - 'action': Goal-directed steps (writing an email, researching a topic)
    - 'activity': Motive-driven systems (writing a paper, improving health)
    """
    __tablename__ = "entities"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(20), nullable=False, index=True)  # 'operation' | 'action' | 'activity'
    text = Column(Text, nullable=False)
    embedding = Column(LargeBinary, nullable=True)  # Optional embedding bytes
    timestamp_start = Column(DateTime, nullable=False, index=True)
    timestamp_end = Column(DateTime, nullable=True)
    metadata_json = Column(Text, nullable=True)  # JSON string for flexible storage
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, onupdate=datetime.utcnow)
    
    # Relationships
    outgoing_relations = relationship(
        "Relation",
        foreign_keys="Relation.source_id",
        back_populates="source",
        cascade="all, delete-orphan",
    )
    incoming_relations = relationship(
        "Relation",
        foreign_keys="Relation.target_id",
        back_populates="target",
        cascade="all, delete-orphan",
    )
    
    # Indexes
    __table_args__ = (
        Index("idx_entity_type_timestamp", "type", "timestamp_start"),
    )
    
    @property
    def metadata_dict(self) -> dict:
        """Parse metadata_json as a dictionary."""
        if self.metadata_json:
            return json.loads(self.metadata_json)
        return {}
    
    @metadata_dict.setter
    def metadata_dict(self, value: dict):
        """Set metadata from a dictionary."""
        self.metadata_json = json.dumps(value) if value else None
    
    def __repr__(self):
        return f"<Entity(id={self.id}, type={self.type}, text={self.text[:50]}...)>"


class Relation(Base):
    """
    Relations between entities in the graph.
    
    Relation Types:
    - 'structural': Hierarchy (part_of: operation→action, action→activity)
    - 'temporal': Time ordering (follows, overlaps, during)
    - 'revision': Version control (supersedes, same_as)
    - 'behavioral': Goal relationships (supports, hinders, maintains, modifies_determinant)
    """
    __tablename__ = "relations"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True)
    target_id = Column(Integer, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True)
    relation_type = Column(String(20), nullable=False, index=True)  # 'structural' | 'temporal' | 'revision' | 'behavioral'
    relation_subtype = Column(String(30), nullable=True)  # e.g., 'part_of', 'follows', 'supports'
    confidence = Column(Float, nullable=True)  # 0.0 - 1.0
    metadata_json = Column(Text, nullable=True)  # JSON string for flexible storage
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    source = relationship("Entity", foreign_keys=[source_id], back_populates="outgoing_relations")
    target = relationship("Entity", foreign_keys=[target_id], back_populates="incoming_relations")
    
    # Constraints and indexes
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "relation_type", "relation_subtype", name="uq_relation"),
        Index("idx_relation_type_subtype", "relation_type", "relation_subtype"),
    )
    
    @property
    def metadata_dict(self) -> dict:
        """Parse metadata_json as a dictionary."""
        if self.metadata_json:
            return json.loads(self.metadata_json)
        return {}
    
    @metadata_dict.setter
    def metadata_dict(self, value: dict):
        """Set metadata from a dictionary."""
        self.metadata_json = json.dumps(value) if value else None
    
    def __repr__(self):
        return f"<Relation(id={self.id}, {self.source_id}--{self.relation_type}:{self.relation_subtype}-->{self.target_id})>"


# Relation type constants
class RelationType:
    STRUCTURAL = "structural"
    TEMPORAL = "temporal"
    REVISION = "revision"
    BEHAVIORAL = "behavioral"


# Relation subtype constants
class RelationSubtype:
    # Structural
    PART_OF = "part_of"
    SAME_AS = "same_as"
    
    # Temporal
    FOLLOWS = "follows"
    OVERLAPS = "overlaps"    
    CO_OCCURS = "co_occurs"
    COMPETES = "competes"
    
    # Revision
    SUPERSEDES = "supersedes"    
    
    # Behavioral
    SUPPORTS = "supports"
    HINDERS = "hinders"
    MAINTAINS = "maintains"
    MODIFIES_DETERMINANT = "modifies_determinant"


class BatchSnapshot(Base):
    """
    Lightweight snapshot of entity state captured after each reconcile batch.

    Enables stability metrics over time:
    - Jaccard similarity of active entity sets between consecutive batches
    - Label stability (diff entity_labels between snapshots)
    - Churn rate (births + merges + revisions) / total_active
    - Convergence time (batches until churn drops below threshold)
    """
    __tablename__ = "batch_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(Integer, nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    stage = Column(String(20), nullable=False)  # "stage3" (activities) | "stage4" (goals)

    # The S_t set — JSON array of active entity IDs
    active_entity_ids = Column(Text, nullable=False)  # "[1, 3, 7, 12]"
    # Full label map for label-stability diffs — JSON dict {id: label}
    entity_labels = Column(Text, nullable=True)  # '{"1": "label...", "3": "label..."}'

    # Batch event counts
    births = Column(Integer, default=0)
    matches = Column(Integer, default=0)
    merges = Column(Integer, default=0)
    revisions = Column(Integer, default=0)

    total_active = Column(Integer, default=0)

    __table_args__ = (
        Index("idx_snapshot_stage_batch", "stage", "batch_id"),
    )

    @property
    def active_ids_set(self) -> set:
        """Parse active_entity_ids as a Python set."""
        if self.active_entity_ids:
            return set(json.loads(self.active_entity_ids))
        return set()

    @property
    def labels_dict(self) -> dict:
        """Parse entity_labels as a Python dict."""
        if self.entity_labels:
            return json.loads(self.entity_labels)
        return {}


class ReviewEditLog(Base):
    """Append-only audit log for goal review edits.

    Each row is one edit operation during a review session.
    The sequence of edits gives the edit-distance metric.
    """
    __tablename__ = "review_edit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(50), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)

    operation = Column(String(20), nullable=False)  # rename | delete | add | merge | split

    goal_id = Column(Integer, nullable=True)
    goal_text_before = Column(Text, nullable=True)
    goal_text_after = Column(Text, nullable=True)

    merge_with_goal_id = Column(Integer, nullable=True)
    merge_with_text = Column(Text, nullable=True)

    split_group_a_ids = Column(Text, nullable=True)  # JSON array
    split_group_b_ids = Column(Text, nullable=True)  # JSON array

    metadata_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_review_edit_session", "session_id", "timestamp"),
    )


class ReviewJudgment(Base):
    """Participant judgments during goal review (evidence + relation accuracy)."""
    __tablename__ = "review_judgments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(50), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)

    judgment_type = Column(String(20), nullable=False)  # evidence | relation

    goal_id = Column(Integer, nullable=True)
    activity_id = Column(Integer, nullable=True)
    belongs = Column(Integer, nullable=True)  # 1 = belongs, 0 = doesn't belong

    relation_id = Column(Integer, nullable=True)
    relation_label = Column(String(20), nullable=True)  # supports | hinders
    correct = Column(Integer, nullable=True)  # 1 = correct, 0 = incorrect

    metadata_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_review_judgment_session", "session_id", "timestamp"),
    )


# Entity type constants
class EntityType:
    OPERATION = "operation"
    ACTION = "action"
    ACTIVITY = "activity"
    GOAL = "goal"
    PROPOSITION = "proposition"  # Legacy; kept for existing database compatibility
