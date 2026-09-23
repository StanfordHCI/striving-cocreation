# schemas.py

from __future__ import annotations
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, ConfigDict


class Update(BaseModel):
    """An update from an observer."""
    content: str = Field(..., description="The content of the update")
    content_type: Literal["input_text", "input_image"] = Field(..., description="The type of the update")
    metadata: Optional[dict] = Field(default=None, description="Optional observer metadata")


class OperationItem(BaseModel):
    """An atomic behavioral operation extracted from an observation."""
    text: str = Field(..., description="Description of the operation")
    reasoning: Optional[str] = Field(default=None, description="Evidence supporting the operation")
    timestamp: str = Field(..., description="ISO format timestamp of when the operation occurred")
    confidence: Optional[str] = Field(default=None, description="Confidence score (1–10)")
    decay: Optional[str] = Field(default=None, description="Decay score (1–10)")
    context: Optional[dict] = Field(default=None, description="Additional context (app, URL, etc.)")

    model_config = ConfigDict(extra="forbid")


class OperationExtractionResult(BaseModel):
    """Result of extracting operations from an observation."""
    operations: List[OperationItem] = Field(..., description="List of extracted operations")

    model_config = ConfigDict(extra="forbid")


class ActionItem(BaseModel):
    """A goal-directed action built from operations."""
    text: str = Field(..., description="Description of the action")
    reasoning: Optional[str] = Field(default=None, description="Evidence supporting the action")
    timestamp_start: str = Field(..., description="Start timestamp")
    timestamp_end: str = Field(..., description="End timestamp")
    confidence: Optional[str] = Field(default=None, description="Confidence score (1–10)")
    decay: Optional[str] = Field(default=None, description="Decay score (1–10)")
    operation_ids: List[int] = Field(..., description="IDs of operations that comprise this action")
    goal_hint: Optional[str] = Field(default=None, description="Hint about the underlying goal")
    metadata: Optional[dict] = Field(
        default=None,
        description="Additional Activity Theory / behavior-change metadata for this action",
    )

    model_config = ConfigDict(extra="forbid")


class ActionGenerationResult(BaseModel):
    """Result of generating actions from operations."""
    actions: List[ActionItem] = Field(..., description="List of generated actions")

    model_config = ConfigDict(extra="forbid")


class SegmentItem(BaseModel):
    """A contiguous segment of operations."""
    label: str = Field(..., description="Segment label (e.g., S1)")
    start_index: int = Field(..., description="1-based start index inclusive")
    end_index: int = Field(..., description="1-based end index inclusive")

    model_config = ConfigDict(extra="forbid")


class SegmentationResult(BaseModel):
    """Result of segmenting operations into action units."""
    segments: List[SegmentItem] = Field(..., description="List of contiguous segments covering all operations")

    model_config = ConfigDict(extra="forbid")


class ActivityItem(BaseModel):
    """A motive-driven activity inferred from actions."""
    text: str = Field(..., description="Description of the activity/goal")
    is_new: bool = Field(..., description="Whether this is a new activity")
    matches_activity_id: Optional[int] = Field(default=None, description="ID of existing activity if matched")
    action_ids: List[int] = Field(..., description="IDs of actions that comprise this activity")
    behavioral_relations: Optional[List[dict]] = Field(default=None, description="Behavioral relations to other activities")

    model_config = ConfigDict(extra="forbid")


class ActivityInferenceResult(BaseModel):
    """Result of inferring activities from actions."""
    activity: ActivityItem = Field(..., description="The inferred activity")

    model_config = ConfigDict(extra="forbid")


class ActivityAssignmentResult(BaseModel):
    """Decision for assigning an action to an activity."""
    choices: List[str] = Field(default_factory=list, description="Candidate labels (e.g., C1) and/or 'NEW'")
    reasoning: Optional[str] = Field(default=None, description="Why this assignment")
    confidence: Optional[int] = Field(default=None, description="Confidence 1-10")
    decay: Optional[int] = Field(default=None, description="Decay 1-10")
    new_activity: Optional[dict] = Field(
        default=None,
        description="If NEW is chosen: {text, goal_hint}",
    )

    model_config = ConfigDict(extra="forbid")


class ActivityMergeDecision(BaseModel):
    """Decision to merge or keep a candidate pair."""
    pair_id: str = Field(..., description="Merge pair label (e.g., M1)")
    decision: str = Field(..., description="'merge' or 'keep'")
    new_title: Optional[str] = Field(default=None, description="Optional new merged title")
    reasoning: Optional[str] = Field(default=None, description="Why this decision")

    model_config = ConfigDict(extra="forbid")


class ActivitySplitCluster(BaseModel):
    """Cluster of actions proposed for a split."""
    title: str = Field(..., description="Proposed new activity title")
    action_ids: List[int] = Field(..., description="Action IDs belonging to this cluster")

    model_config = ConfigDict(extra="forbid")


class ActivitySplitDecision(BaseModel):
    """Decision to split an activity."""
    split_id: str = Field(..., description="Split label (e.g., S1)")
    clusters: List[ActivitySplitCluster] = Field(..., description="Proposed clusters")
    reasoning: Optional[str] = Field(default=None, description="Why this split")

    model_config = ConfigDict(extra="forbid")


class ActivityRefinementResult(BaseModel):
    """Result of refinement pass."""
    merges: List[ActivityMergeDecision] = Field(default_factory=list)
    splits: List[ActivitySplitDecision] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class GoalRelation(BaseModel):
    """A relation between two goals (by index in the goals array)."""
    goal_a_idx: int = Field(..., description="Index of goal A in the goals array")
    goal_b_idx: int = Field(..., description="Index of goal B in the goals array")
    relation: str = Field(..., description="Type: conflict, facilitation, instrumental")
    evidence: Optional[str] = Field(default=None, description="Behavioral evidence for the relation")

    model_config = ConfigDict(extra="allow")


class GoalItem(BaseModel):
    """A life goal synthesized from activities."""
    text: str = Field(..., description="Description of the life goal ('{user_name} is ...')")
    activity_ids: List[int] = Field(..., description="IDs of activities that serve this goal")
    reasoning: Optional[str] = Field(default=None, description="Why these activities form this goal")
    confidence: Optional[int] = Field(default=None, description="Confidence score (1-10)")
    matches_goal_id: Optional[int] = Field(default=None, description="ID of existing goal if matched")
    evidence_summary: Optional[str] = Field(default=None, description="Summary of supporting evidence")
    # Psychological dimensions (Phase 3)
    needs: Optional[List[str]] = Field(default=None, description="Needs from 12-dimension taxonomy")
    orientation: Optional[str] = Field(default=None, description="approach, avoidance, or mixed")
    aspiration_vs_obligation: Optional[str] = Field(default=None, description="ideal, ought, or mixed")
    autonomy: Optional[str] = Field(default=None, description="autonomous, controlled, or mixed")
    identity_link: Optional[str] = Field(default=None, description="Connection to identity/sense of self")
    feared_self: Optional[str] = Field(default=None, description="Feared self being avoided (avoidance and mixed-orientation goals)")
    # Aggregated activity metadata (Phase 3b)
    people: Optional[List[str]] = Field(default=None, description="Key people/teams across constituent activities")
    domains: Optional[List[str]] = Field(default=None, description="Life domains spanned (work, personal, creative, social, health)")
    engagement_signature: Optional[str] = Field(default=None, description="How the user engages with activities serving this goal")
    initiation_signature: Optional[str] = Field(default=None, description="How activities serving this goal are typically initiated")
    temporal_context: Optional[str] = Field(default=None, description="When this goal tends to be served")

    model_config = ConfigDict(extra="allow")


class ContrastEntry(BaseModel):
    """A single entry in the contrast analysis."""
    user_goal_id: Optional[int] = Field(default=None, description="ID of user-provided goal (for supported/unsupported)")
    user_goal_text: Optional[str] = Field(default=None, description="Text of user-provided goal")
    inferred_goal: Optional[str] = Field(default=None, description="Text of inferred goal (for unstated)")
    evidence_strength: str = Field(..., description="'strong', 'moderate', or 'weak'")
    activity_ids: List[int] = Field(default_factory=list, description="Supporting activity IDs")
    action_count: int = Field(default=0, description="Number of supporting actions")
    note: Optional[str] = Field(default=None, description="Explanation of the contrast")

    model_config = ConfigDict(extra="forbid")


class ContrastAnalysis(BaseModel):
    """Contrast between user-stated goals and observed behavior."""
    supported: List[ContrastEntry] = Field(default_factory=list, description="User goals with strong evidence")
    unsupported: List[ContrastEntry] = Field(default_factory=list, description="User goals with weak/no evidence")
    unstated: List[ContrastEntry] = Field(default_factory=list, description="Inferred goals the user didn't mention")

    model_config = ConfigDict(extra="forbid")


class NeedLandscape(BaseModel):
    """Need-level motive congruence analysis from self-refine."""
    congruence_insights: List[dict] = Field(default_factory=list, description="Needs with strong behavioral alignment")
    incongruence_insights: List[dict] = Field(default_factory=list, description="Needs that are starved or inconsistent")
    leverage_points: List[dict] = Field(default_factory=list, description="Multifinal activities serving multiple needs")

    model_config = ConfigDict(extra="allow")


class DroppedGoal(BaseModel):
    """An existing goal that was dropped during synthesis, with rationale."""
    goal_id: int = Field(..., description="Database ID of the dropped goal")
    goal_text: Optional[str] = Field(default=None, description="Original text of the dropped goal")
    reason: Optional[str] = Field(default=None, description="Category: merged, evidence_changed, redundant, absorbed")
    explanation: Optional[str] = Field(default=None, description="Why this goal was dropped")

    model_config = ConfigDict(extra="allow")


class GoalSynthesisResult(BaseModel):
    """Result of synthesizing life goals from activities."""
    goals: List[GoalItem] = Field(..., description="List of synthesized life goals")
    goal_relations: List[GoalRelation] = Field(default_factory=list, description="Relations between goals")
    dropped_goals: List[DroppedGoal] = Field(default_factory=list, description="Existing goals not carried forward, with rationale")
    contrast: Optional[ContrastAnalysis] = Field(default=None, description="Contrast analysis (when user-provided goals exist)")
    need_landscape: Optional[NeedLandscape] = Field(default=None, description="Need-level motive congruence analysis")

    model_config = ConfigDict(extra="allow")


# ─── Stage 2: Segments with actions (operations_to_actions) ─────────────────

class SegmentActionPayload(BaseModel):
    """Action payload embedded within a segment."""
    text: str = Field(..., description="Description of the action")
    reasoning: Optional[str] = Field(default=None, description="Evidence for the action")
    confidence: Optional[int] = Field(default=None, description="Confidence 1-10")
    decay: Optional[int] = Field(default=None, description="Decay 1-10")
    goal_hint: Optional[str] = Field(default=None, description="Hint about underlying goal")

    model_config = ConfigDict(extra="allow")


class SegmentWithAction(BaseModel):
    """A segment of operations with its associated action."""
    label: str = Field(..., description="Segment label (e.g., S1)")
    start_index: int = Field(..., description="1-based start index inclusive")
    end_index: int = Field(..., description="1-based end index inclusive")
    action: Optional[SegmentActionPayload] = Field(default=None, description="Action for this segment")

    model_config = ConfigDict(extra="allow")


class SegmentationWithActionsResult(BaseModel):
    """Result of segmenting operations and generating one action per segment."""
    segments: List[SegmentWithAction] = Field(..., description="Segments with actions")

    model_config = ConfigDict(extra="allow")


# ─── Stage 3: PAS propose + assign (action_to_activities) ──────────────────

class PasCandidateItem(BaseModel):
    """A candidate activity proposed by PAS."""
    candidate_id: Optional[str] = Field(default=None, description="Candidate label (e.g., C1)")
    description: Optional[str] = Field(default=None, description="Activity description")
    action_ids: Optional[List[int]] = Field(default=None, description="Example action IDs")
    example_action_ids: Optional[List[int]] = Field(default=None, description="Alt key for action IDs")
    existing_activity_id: Optional[int] = Field(default=None, description="Matched existing activity ID")
    reasoning: Optional[str] = Field(default=None, description="Why this candidate")
    purpose: Optional[str] = Field(default=None, description="Working-sphere purpose")
    people: Optional[List[str]] = Field(default=None, description="People involved")
    resources: Optional[List[str]] = Field(default=None, description="Resources used")
    temporal_pattern: Optional[str] = Field(default=None, description="When this activity occurs")
    engagement_profile: Optional[str] = Field(default=None, description="How user engages")
    initiation_profile: Optional[str] = Field(default=None, description="How activity is initiated")
    identity_context: Optional[str] = Field(default=None, description="Identity relevance")

    model_config = ConfigDict(extra="allow")


class PasProposeResult(BaseModel):
    """Result of PAS propose step."""
    candidates: List[PasCandidateItem] = Field(..., description="Proposed candidate activities")

    model_config = ConfigDict(extra="allow")


# ─── Goal Reconcile ────────────────────────────────────────────────────────

class ReconcileMultiDecisionItem(BaseModel):
    """A single decision in a multi-candidate reconciliation."""
    candidate_id: int = Field(..., description="Activity ID of the candidate")
    decision: str = Field(..., description="assign_existing, create_new, merge, etc.")
    target_goal_id: Optional[int] = Field(default=None, description="Target goal ID for assign")
    new_goal_text: Optional[str] = Field(default=None, description="Text for new goal")
    confidence: Optional[int] = Field(default=None, description="Confidence 1-10")
    reasoning: Optional[str] = Field(default=None, description="Why this decision")

    model_config = ConfigDict(extra="allow")


class ReconcileMultiResult(BaseModel):
    """Result of multi-candidate goal reconciliation."""
    decisions: List[ReconcileMultiDecisionItem] = Field(..., description="Per-candidate decisions")

    model_config = ConfigDict(extra="allow")


# ─── Goal Propose + Reconcile (B1 incremental goal synthesis) ─────────────

class GoalProposeCandidate(BaseModel):
    """A candidate life goal from the propose step."""
    candidate_id: Optional[str] = Field(default=None, description="Candidate label (e.g., G1)")
    text: str = Field(..., description="Goal striving text ('{user_name} is ...')")
    needs: Optional[List[str]] = Field(default=None, description="Needs from 12-dimension taxonomy")
    orientation: Optional[str] = Field(default=None, description="approach, avoidance, or mixed")
    aspiration_vs_obligation: Optional[str] = Field(default=None, description="ideal, ought, or mixed")
    autonomy: Optional[str] = Field(default=None, description="autonomous, controlled, or mixed")
    identity_link: Optional[str] = Field(default=None, description="Connection to identity/sense of self")
    feared_self: Optional[str] = Field(default=None, description="Feared self being avoided")
    activity_ids: List[int] = Field(default_factory=list, description="Activity IDs that serve this goal")
    reasoning: Optional[str] = Field(default=None, description="Why these activities form this goal")
    synthesis_evidence: Optional[str] = Field(default=None, description="Cross-activity pattern evidence")
    confidence: Optional[int] = Field(default=None, description="Confidence 1-10")
    evidence_summary: Optional[str] = Field(default=None, description="Brief evidence summary")
    people: Optional[List[str]] = Field(default=None, description="People/teams involved")
    domains: Optional[List[str]] = Field(default=None, description="Life domains spanned")
    engagement_signature: Optional[str] = Field(default=None, description="How user engages")
    initiation_signature: Optional[str] = Field(default=None, description="How activities are initiated")
    temporal_context: Optional[str] = Field(default=None, description="When this goal is served")

    model_config = ConfigDict(extra="allow")


class GoalProposeResult(BaseModel):
    """Result of goal propose step."""
    candidates: List[GoalProposeCandidate] = Field(..., description="Proposed candidate goals")

    model_config = ConfigDict(extra="allow")


class GoalReconcileDecisionItem(BaseModel):
    """A single decision in goal reconciliation."""
    candidate_id: Optional[int] = Field(default=None, description="Candidate database entity ID (integer)")
    decision: str = Field(..., description="match, revise, new, or merge")
    target_goal_id: Optional[int] = Field(default=None, description="Existing goal ID for match/revise/merge")
    revised_label: Optional[str] = Field(default=None, description="Updated label for revise/merge")
    goal_summary: Optional[str] = Field(default=None, description="Updated summary")
    needs: Optional[List[str]] = Field(default=None, description="Needs for the resulting goal")
    orientation: Optional[str] = Field(default=None, description="approach, avoidance, or mixed")
    aspiration_vs_obligation: Optional[str] = Field(default=None, description="ideal, ought, or mixed")
    autonomy_dim: Optional[str] = Field(default=None, description="autonomous, controlled, or mixed")
    identity_link: Optional[str] = Field(default=None, description="Connection to identity")
    feared_self: Optional[str] = Field(default=None, description="Feared self being avoided")
    people: Optional[List[str]] = Field(default=None, description="People/teams involved")
    domains: Optional[List[str]] = Field(default=None, description="Life domains")
    engagement_signature: Optional[str] = Field(default=None, description="How user engages")
    initiation_signature: Optional[str] = Field(default=None, description="How initiated")
    temporal_context: Optional[str] = Field(default=None, description="When served")
    evidence_summary: Optional[str] = Field(default=None, description="Evidence summary")
    merge_goal_ids: Optional[List[int]] = Field(default=None, description="Goal IDs to merge (for merge)")
    relations: Optional[List[dict]] = Field(default=None, description="Relations to other goals")
    confidence: Optional[int] = Field(default=None, description="Confidence 1-10")
    decision_reasoning: Optional[str] = Field(default=None, description="Explanation of decision")

    model_config = ConfigDict(extra="allow")


class GoalReconcileResult(BaseModel):
    """Result of goal reconciliation step."""
    decisions: List[GoalReconcileDecisionItem] = Field(..., description="Per-candidate decisions")

    model_config = ConfigDict(extra="allow")


# ─── Flat Goal Synthesis (ablation) ────────────────────────────────────────

class FlatGoalItem(BaseModel):
    """A goal decision from flat goal synthesis."""
    text: str = Field(..., description="Goal text")
    action: str = Field(..., description="create, update, or keep")
    update_goal_id: Optional[int] = Field(default=None, description="Goal ID to update")
    proposition_ids: Optional[List[int]] = Field(default=None, description="Supporting proposition IDs")
    reasoning: Optional[str] = Field(default=None, description="Why this decision")

    model_config = ConfigDict(extra="allow")


class FlatGoalSynthesisResponse(BaseModel):
    """Result of flat goal synthesis LLM call."""
    goals: List[FlatGoalItem] = Field(..., description="Goal decisions")

    model_config = ConfigDict(extra="allow")


# ─── Audit (ablation) ─────────────────────────────────────────────────────

class AuditDecisionResult(BaseModel):
    """Privacy audit decision for an observation."""
    transmit_data: bool = Field(..., description="Whether data should be transmitted")
    data_type: Optional[str] = Field(default=None, description="Type of data detected")
    reasoning: Optional[str] = Field(default=None, description="Why this decision")

    model_config = ConfigDict(extra="allow")


# ─── Schema helper ─────────────────────────────────────────────────────────

def get_schema(json_schema):
    """Return json_object response format (schema ignored).

    Previously passed the full schema for guided generation, but Gemini's
    evolving restrictions on response_schema made this a recurring source
    of 400 errors.  Plain json_object mode is sufficient since prompts
    already describe the expected JSON structure.
    """
    return {"type": "json_object"}

