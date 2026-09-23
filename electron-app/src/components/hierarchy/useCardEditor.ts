/**
 * Staged-edit state for the card editor.
 *
 * Every action the user takes produces a new `StagedEdits` value plus a log
 * entry, and the previous pair is pushed onto an undo stack. Snapshotting whole
 * states rather than inverse operations keeps undo honest: merges and
 * judgements interact (rejecting a goal clears its merge, for instance), and
 * replaying an inverse would have to re-derive all of that.
 */

import { useCallback, useMemo, useRef, useState } from 'react';
import type { HierarchyNode } from '../../api/contracts';
import {
  EDIT_TYPE,
  EMPTY_EDITS,
  type EditLogEntry,
  type EditType,
  type Judgment,
  type RelationJudgment,
  type StagedEdits,
} from './types';

type Snapshot = { edits: StagedEdits; log: EditLogEntry[] };

const UNDO_LIMIT = 100;

let entryCounter = 0;
function nextEntryId(): string {
  entryCounter += 1;
  return `edit_${Date.now()}_${entryCounter}`;
}

export function useCardEditor() {
  // Edits and their log move together, in one atomic update. Keeping them in
  // separate state invited nesting one setter inside the other's updater —
  // which React may run more than once, appending the same entry twice.
  const [state, setState] = useState<Snapshot>({ edits: EMPTY_EDITS, log: [] });
  const history = useRef<Snapshot[]>([]);
  // Lets the undo stack read the current state without doing it inside an
  // updater, where a repeat invocation would push a duplicate snapshot.
  const stateRef = useRef(state);
  stateRef.current = state;

  /** Apply one change, recording the prior state so it can be undone. */
  const commit = useCallback((
    change: (current: StagedEdits) => StagedEdits,
    entry: Omit<EditLogEntry, 'id' | 'timestamp'> | null,
  ) => {
    // Built here, not in the updater: an id and timestamp generated inside one
    // would differ between invocations of the same logical edit.
    const logEntry: EditLogEntry | null = entry
      ? { ...entry, id: nextEntryId(), timestamp: new Date().toISOString() }
      : null;

    history.current.push(stateRef.current);
    if (history.current.length > UNDO_LIMIT) history.current.shift();

    setState((current) => ({
      edits: change(current.edits),
      log: logEntry ? [...current.log, logEntry] : current.log,
    }));
  }, []);

  const undo = useCallback(() => {
    const previous = history.current.pop();
    if (!previous) return;
    setState(previous);
  }, []);

  const reset = useCallback(() => {
    history.current = [];
    setState({ edits: EMPTY_EDITS, log: [] });
  }, []);

  const { edits, log } = state;

  // ── text ───────────────────────────────────────────────────────────────

  const editText = useCallback((node: HierarchyNode, next: string, previous: string) => {
    commit(
      (current) => ({ ...current, textOverrides: { ...current.textOverrides, [node.id]: next } }),
      {
        type: EDIT_TYPE.TEXT_EDIT,
        entityId: node.id,
        oldText: previous,
        newText: next,
      },
    );
  }, [commit]);

  // ── judgements ─────────────────────────────────────────────────────────

  const judgeGoal = useCallback((id: number, judgment: Judgment | null, note?: string) => {
    const type: EditType = judgment === 'approve'
      ? EDIT_TYPE.APPROVE
      : judgment === 'reject'
        ? EDIT_TYPE.REJECT
        : EDIT_TYPE.UNDO_JUDGMENT;

    commit(
      (current) => {
        const judgments = { ...current.judgments };
        if (judgment === null) delete judgments[id];
        else judgments[id] = judgment;

        // A goal being removed cannot also be staged for a merge.
        const clearMerge = judgment === 'reject';
        return {
          ...current,
          judgments,
          goalMergeSelection: clearMerge
            ? current.goalMergeSelection.filter((candidate) => candidate !== id)
            : current.goalMergeSelection,
          annotations: note
            ? [...current.annotations, { entity_id: id, type: 'judgment', text: note }]
            : current.annotations,
        };
      },
      { type, entityId: id, note },
    );
  }, [commit]);

  const judgeRelation = useCallback((
    activityId: number,
    goalId: number,
    judgment: RelationJudgment | null,
    note?: string,
  ) => {
    const key = `${activityId}:${goalId}`;
    const type: EditType = judgment === 'misplaced'
      ? EDIT_TYPE.MISPLACE_ACTIVITY
      : judgment === 'detach'
        ? EDIT_TYPE.DETACH_ACTIVITY
        : EDIT_TYPE.UNDO_JUDGMENT;

    commit(
      (current) => {
        const relationJudgments = { ...current.relationJudgments };
        if (judgment === null) delete relationJudgments[key];
        else relationJudgments[key] = judgment;
        return {
          ...current,
          relationJudgments,
          annotations: note
            ? [...current.annotations, { entity_id: activityId, type: 'relation', text: note }]
            : current.annotations,
        };
      },
      { type, entityId: activityId, relatedId: goalId, note },
    );
  }, [commit]);

  // ── merges ─────────────────────────────────────────────────────────────

  const toggleMergeSelection = useCallback((id: number, level: 'goal' | 'activity') => {
    const field = level === 'goal' ? 'goalMergeSelection' : 'activityMergeSelection';
    commit(
      (current) => {
        const selection = current[field];
        return {
          ...current,
          [field]: selection.includes(id)
            ? selection.filter((candidate) => candidate !== id)
            : [...selection, id],
        };
      },
      null, // Ticking a checkbox is not itself an edit worth logging.
    );
  }, [commit]);

  /**
   * Confirm a staged merge. The first-checked card survives and takes the
   * agreed wording; the second is folded into it.
   */
  const confirmMerge = useCallback((
    level: 'goal' | 'activity',
    survivorId: number,
    absorbedId: number,
    mergedText: string,
    note?: string,
  ) => {
    const isGoal = level === 'goal';
    commit(
      (current) => ({
        ...current,
        [isGoal ? 'goalMerges' : 'activityMerges']: [
          ...(isGoal ? current.goalMerges : current.activityMerges),
          [survivorId, absorbedId] as [number, number],
        ],
        textOverrides: { ...current.textOverrides, [survivorId]: mergedText },
        [isGoal ? 'goalMergeSelection' : 'activityMergeSelection']: [],
        annotations: note
          ? [...current.annotations, { entity_id: survivorId, type: 'merge', text: note }]
          : current.annotations,
      }),
      {
        type: isGoal ? EDIT_TYPE.MERGE_GOALS : EDIT_TYPE.MERGE_ACTIVITIES,
        entityId: survivorId,
        relatedId: absorbedId,
        newText: mergedText,
        note,
      },
    );
  }, [commit]);

  const clearMergeSelection = useCallback((level: 'goal' | 'activity') => {
    commit(
      (current) => ({
        ...current,
        [level === 'goal' ? 'goalMergeSelection' : 'activityMergeSelection']: [],
      }),
      null,
    );
  }, [commit]);

  // ── actions and locks ──────────────────────────────────────────────────

  const removeAction = useCallback((
    actionId: number,
    activityId: number,
    text: string,
    note?: string,
  ) => {
    const key = `${actionId}:${activityId}`;
    commit(
      (current) => ({
        ...current,
        removedActions: current.removedActions.includes(key)
          ? current.removedActions.filter((candidate) => candidate !== key)
          : [...current.removedActions, key],
        annotations: note
          ? [...current.annotations, { entity_id: actionId, type: 'action', text: note }]
          : current.annotations,
      }),
      { type: EDIT_TYPE.REMOVE_ACTION, entityId: actionId, relatedId: activityId, oldText: text, note },
    );
  }, [commit]);

  const toggleLock = useCallback((id: number) => {
    commit(
      (current) => ({
        ...current,
        locked: current.locked.includes(id)
          ? current.locked.filter((candidate) => candidate !== id)
          : [...current.locked, id],
      }),
      { type: EDIT_TYPE.LOCK, entityId: id },
    );
  }, [commit]);

  const annotate = useCallback((entityId: number, text: string) => {
    commit(
      (current) => ({
        ...current,
        annotations: [...current.annotations, { entity_id: entityId, type: 'note', text }],
      }),
      { type: EDIT_TYPE.ANNOTATE, entityId, note: text },
    );
  }, [commit]);

  // ── derived lookups the tree needs ─────────────────────────────────────

  const { mergeInto, mergeFrom } = useMemo(() => {
    const into: Record<number, number> = {};
    const from: Record<number, number> = {};
    for (const [survivor, absorbed] of [...edits.goalMerges, ...edits.activityMerges]) {
      into[absorbed] = survivor;
      from[survivor] = absorbed;
    }
    return { mergeInto: into, mergeFrom: from };
  }, [edits.goalMerges, edits.activityMerges]);

  const removedActionSet = useMemo(
    () => new Set(edits.removedActions),
    [edits.removedActions],
  );

  return {
    edits,
    log,
    canUndo: history.current.length > 0,
    mergeInto,
    mergeFrom,
    removedActionSet,
    undo,
    reset,
    editText,
    judgeGoal,
    judgeRelation,
    toggleMergeSelection,
    confirmMerge,
    clearMergeSelection,
    removeAction,
    toggleLock,
    annotate,
  };
}
