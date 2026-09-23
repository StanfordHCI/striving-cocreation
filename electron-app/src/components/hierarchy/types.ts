/**
 * Types for the card editor's staged-edit model.
 *
 * Nothing the user does in the editor writes to the server. Every action lands
 * in `StagedEdits`, is narrated in the edit log, and only reaches the database
 * when they compile — which is what makes the whole markup undoable and lets
 * the model see the edits as one coherent batch rather than a drip of patches.
 */

import type { HierarchyCompileRequest, HierarchyNode } from '../../api/contracts';

/** What the user decided about a goal. */
export type Judgment = 'approve' | 'reject';

/** What the user decided about an activity's place under a particular goal. */
export type RelationJudgment = 'misplaced' | 'detach';

export const EDIT_TYPE = {
  TEXT_EDIT: 'text_edit',
  APPROVE: 'approve',
  REJECT: 'reject',
  UNDO_JUDGMENT: 'undo_judgment',
  MERGE_GOALS: 'merge_goals',
  MERGE_ACTIVITIES: 'merge_activities',
  MISPLACE_ACTIVITY: 'misplace_activity',
  DETACH_ACTIVITY: 'detach_activity',
  REMOVE_ACTION: 'remove_action',
  LOCK: 'lock',
  ANNOTATE: 'annotate',
} as const;

export type EditType = (typeof EDIT_TYPE)[keyof typeof EDIT_TYPE];

export type EditLogEntry = {
  id: string;
  type: EditType;
  timestamp: string;
  entityId: number;
  /** Second party to a merge, or the parent an activity was detached from. */
  relatedId?: number;
  oldText?: string;
  newText?: string;
  note?: string;
};

export const EDIT_LABELS: Record<EditType, { icon: string; label: string; color: string }> = {
  [EDIT_TYPE.TEXT_EDIT]:         { icon: '✎', label: 'Edited',              color: '#8B7355' },
  [EDIT_TYPE.APPROVE]:           { icon: '✓', label: 'Accepted',            color: '#2E7D32' },
  [EDIT_TYPE.REJECT]:            { icon: '✕', label: 'Removed',             color: '#C62828' },
  [EDIT_TYPE.UNDO_JUDGMENT]:     { icon: '↶', label: 'Undid judgment',      color: '#888888' },
  [EDIT_TYPE.MERGE_GOALS]:       { icon: '⨆', label: 'Merged strivings',    color: '#1a1a1a' },
  [EDIT_TYPE.MERGE_ACTIVITIES]:  { icon: '⨆', label: 'Merged activities',   color: '#8B7355' },
  [EDIT_TYPE.MISPLACE_ACTIVITY]: { icon: '↔', label: 'Wrong striving',      color: '#d97706' },
  [EDIT_TYPE.DETACH_ACTIVITY]:   { icon: '✂', label: 'Detached from goal',  color: '#C62828' },
  [EDIT_TYPE.REMOVE_ACTION]:     { icon: '−', label: 'Removed action',      color: '#C62828' },
  [EDIT_TYPE.LOCK]:              { icon: '⚑', label: 'Pinned',              color: '#3b82f6' },
  [EDIT_TYPE.ANNOTATE]:          { icon: '“', label: 'Added context',       color: '#8B7355' },
};

/** Everything the user has staged but not yet compiled. */
export type StagedEdits = {
  /** entity id -> the label the user wants kept. */
  textOverrides: Record<number, string>;
  /** goal id -> accept or reject. */
  judgments: Record<number, Judgment>;
  /** `${activityId}:${goalId}` -> what is wrong with that pairing. */
  relationJudgments: Record<string, RelationJudgment>;
  /** Goal ids the user pinned against re-synthesis. */
  locked: number[];
  /** Goal ids checked for merging; pairs form in check order. */
  goalMergeSelection: number[];
  activityMergeSelection: number[];
  /** Confirmed merges as [survivor, absorbed]. */
  goalMerges: Array<[number, number]>;
  activityMerges: Array<[number, number]>;
  /** `${actionId}:${activityId}` for actions dropped from one parent. */
  removedActions: string[];
  /** Free-text context attached to an entity while editing. */
  annotations: Array<{ entity_id: number; type: string; text: string }>;
};

export const EMPTY_EDITS: StagedEdits = {
  textOverrides: {},
  judgments: {},
  relationJudgments: {},
  locked: [],
  goalMergeSelection: [],
  activityMergeSelection: [],
  goalMerges: [],
  activityMerges: [],
  removedActions: [],
  annotations: [],
};

export function hasStagedEdits(edits: StagedEdits): boolean {
  return (
    Object.keys(edits.textOverrides).length > 0
    || Object.keys(edits.judgments).length > 0
    || Object.keys(edits.relationJudgments).length > 0
    || edits.locked.length > 0
    || edits.goalMerges.length > 0
    || edits.activityMerges.length > 0
    || edits.removedActions.length > 0
    || edits.annotations.length > 0
  );
}

/**
 * Translate the editor's staging model into the compile request.
 *
 * The two vocabularies differ on purpose: the editor thinks in what the user
 * *said* about a card, while the server thinks in what to *do* to the graph.
 * A rejected goal becomes a rejection; an activity flagged as sitting under the
 * wrong striving becomes a detach, because the fix is for re-synthesis to find
 * it a better parent rather than for us to guess one.
 */
export function toCompileRequest(edits: StagedEdits): HierarchyCompileRequest {
  const rejected: number[] = [];
  for (const [id, judgment] of Object.entries(edits.judgments)) {
    if (judgment === 'reject') rejected.push(Number(id));
  }

  const removedActionRelations = [...edits.removedActions];
  for (const [key, judgment] of Object.entries(edits.relationJudgments)) {
    const [activityId, goalId] = key.split(':');
    if (judgment === 'detach' || judgment === 'misplaced') {
      // Both readings mean "not under this goal". Dropping the link lets
      // synthesis re-home the activity instead of us inventing a parent.
      removedActionRelations.push(`${activityId}:${goalId}`);
    }
  }

  return {
    text_overrides: Object.fromEntries(
      Object.entries(edits.textOverrides).map(([id, text]) => [Number(id), text]),
    ),
    rejected_ids: rejected,
    locked_ids: [...edits.locked],
    goal_merges: [...edits.goalMerges],
    activity_merges: [...edits.activityMerges],
    removed_action_relations: removedActionRelations,
    annotations: [...edits.annotations],
  };
}

/** Flattened view of one goal subtree, as the card layout wants it. */
export type GoalBranch = {
  goal: HierarchyNode;
  activities: HierarchyNode[];
};

export function toBranches(roots: HierarchyNode[]): GoalBranch[] {
  return roots
    .filter((node) => node.type === 'goal' || node.type === 'activity')
    .map((node) => ({
      goal: node,
      activities: node.type === 'goal' ? node.children : [],
    }));
}
