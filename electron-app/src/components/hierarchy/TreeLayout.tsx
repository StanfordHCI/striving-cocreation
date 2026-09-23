/**
 * The card tree itself: strivings across the top, their activities beneath,
 * actions beneath those, joined by connector lines.
 *
 * Laid out with plain flex rather than absolute positioning — every row centres
 * its children and drops a line to them, which keeps the whole thing printable,
 * scrollable, and correct at any zoom without a layout pass.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import type { HierarchyNode } from '../../api/contracts';
import { ActionCard, ActivityCard, GoalCard } from './Cards';
import { HBar, VLine } from './Primitives';
import { CARD_WIDTH, LEVEL_TOKENS, ROW_GAP } from './tokens';
import type { GoalBranch, Judgment, RelationJudgment } from './types';

// ── Scroll area ──────────────────────────────────────────────────────────────

const ZOOM_MIN = 0.4;
const ZOOM_MAX = 1.5;

export function ScrollableTreeArea({
  children,
  zoom,
  onZoomChange,
}: {
  children: ReactNode;
  zoom: number;
  onZoomChange: (next: number) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(false);

  const checkScroll = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    setCanScrollLeft(element.scrollLeft > 8);
    setCanScrollRight(element.scrollLeft + element.clientWidth < element.scrollWidth - 8);
  }, []);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    checkScroll();
    element.addEventListener('scroll', checkScroll, { passive: true });
    // The tree grows and shrinks as branches open, so watch the content too —
    // resize alone misses a branch expanding within a fixed-width viewport.
    const resizeObserver = new ResizeObserver(checkScroll);
    resizeObserver.observe(element);
    const mutationObserver = new MutationObserver(checkScroll);
    mutationObserver.observe(element, { childList: true, subtree: true });
    return () => {
      element.removeEventListener('scroll', checkScroll);
      resizeObserver.disconnect();
      mutationObserver.disconnect();
    };
  }, [checkScroll]);

  // Pinch (ctrlKey) and Cmd+scroll zoom, matching the trackpad convention.
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    function onWheel(event: WheelEvent) {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const next = zoom - event.deltaY * 0.005;
      onZoomChange(Number(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next)).toFixed(2)));
    }
    element.addEventListener('wheel', onWheel, { passive: false });
    return () => element.removeEventListener('wheel', onWheel);
  }, [zoom, onZoomChange]);

  function scrollBy(direction: number) {
    scrollRef.current?.scrollBy({ left: direction * 320, behavior: 'smooth' });
  }

  return (
    <div className="card-tree-area">
      <button
        type="button"
        className={`tree-scroll-arrow left${canScrollLeft ? '' : ' is-hidden'}`}
        onClick={() => scrollBy(-1)}
        aria-label="Scroll to earlier strivings"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="10 3 5 8 10 13" /></svg>
      </button>
      <button
        type="button"
        className={`tree-scroll-arrow right${canScrollRight ? '' : ' is-hidden'}`}
        onClick={() => scrollBy(1)}
        aria-label="Scroll to later strivings"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="6 3 11 8 6 13" /></svg>
      </button>
      <div className="card-tree-scroll" ref={scrollRef}>
        <div
          className="card-tree-zoom"
          style={zoom === 1 ? undefined : {
            transform: `scale(${zoom})`,
            width: `${100 / zoom}%`,
            minHeight: `${100 / zoom}%`,
          }}
        >
          {children}
        </div>
      </div>
    </div>
  );
}

// ── Tree ─────────────────────────────────────────────────────────────────────

export type TreeLayoutProps = {
  branches: GoalBranch[];
  expanded: Record<string, boolean>;
  selectedId: number | null;
  textOf: (node: HierarchyNode) => string;
  judgments: Record<number, Judgment>;
  relationJudgments: Record<string, RelationJudgment>;
  lockedIds: number[];
  goalMergeSelection: number[];
  activityMergeSelection: number[];
  /** absorbed id -> survivor text, and survivor id -> absorbed text. */
  mergeInto: Record<number, string>;
  mergeFrom: Record<number, string>;
  removedActions: Set<string>;
  highlights: Record<string, 'added' | 'modified'>;
  onToggle: (key: string) => void;
  onSelect: (id: number) => void;
  onJudgeGoal: (id: number, next: Judgment | null) => void;
  onJudgeRelation: (activityId: number, goalId: number, next: RelationJudgment | null) => void;
  onTextEdit: (node: HierarchyNode, next: string) => void;
  onGoalMergeToggle: (id: number) => void;
  onActivityMergeToggle: (id: number) => void;
  onLockToggle: (id: number) => void;
  onRemoveAction: (actionId: number, activityId: number, text: string) => void;
};

export function TreeLayout(props: TreeLayoutProps) {
  const {
    branches, expanded, selectedId, textOf, judgments, relationJudgments,
    lockedIds, goalMergeSelection, activityMergeSelection, mergeInto, mergeFrom,
    removedActions, highlights,
    onToggle, onSelect, onJudgeGoal, onJudgeRelation, onTextEdit,
    onGoalMergeToggle, onActivityMergeToggle, onLockToggle, onRemoveAction,
  } = props;

  // An activity can be part of several strivings. The first goal to show it
  // owns the editable card; later goals render a muted reference so the
  // sharing is visible without implying a duplicate entity.
  const primaryGoalFor = new Map<number, number>();
  for (const branch of branches) {
    for (const activity of branch.activities) {
      if (!primaryGoalFor.has(activity.id)) primaryGoalFor.set(activity.id, branch.goal.id);
    }
  }

  return (
    <div className="card-tree">
      <div className="card-tree-row" style={{ gap: ROW_GAP.goal }}>
        {branches.map((branch) => {
          const goal = branch.goal;
          const goalOpen = expanded[`goal-${goal.id}`] ?? false;
          const activities = branch.activities;

          return (
            <div className="card-branch" key={goal.id}>
              <GoalCard
                node={goal}
                text={textOf(goal)}
                selected={selectedId === goal.id}
                expanded={goalOpen}
                childCount={activities.length}
                judgment={judgments[goal.id]}
                locked={lockedIds.includes(goal.id)}
                highlight={highlights[String(goal.id)]}
                mergeChecked={goalMergeSelection.includes(goal.id)}
                mergingInto={mergeInto[goal.id]}
                mergingFrom={mergeFrom[goal.id]}
                onSelect={() => onSelect(goal.id)}
                onToggle={() => onToggle(`goal-${goal.id}`)}
                onJudge={(next) => onJudgeGoal(goal.id, next)}
                onTextEdit={(next) => onTextEdit(goal, next)}
                onMergeToggle={() => onGoalMergeToggle(goal.id)}
                onLockToggle={() => onLockToggle(goal.id)}
              />

              {goalOpen && activities.length === 0 && (
                <>
                  <VLine height={20} color={LEVEL_TOKENS.activity.line} />
                  <div className="card-empty-note">No activities under this striving yet</div>
                </>
              )}

              {goalOpen && activities.length > 0 && (
                <>
                  <VLine height={20} color={LEVEL_TOKENS.activity.line} />
                  <div className="card-tree-row is-branched" style={{ gap: ROW_GAP.activity }}>
                    <HBar
                      count={activities.length}
                      gap={ROW_GAP.activity}
                      childWidth={CARD_WIDTH.activity}
                      color={LEVEL_TOKENS.activity.line}
                    />
                    {activities.map((activity) => {
                      const ghost = primaryGoalFor.get(activity.id) !== goal.id;
                      const activityOpen = !ghost && (expanded[`activity-${activity.id}`] ?? false);
                      const actions = activity.children;
                      const relationKey = `${activity.id}:${goal.id}`;

                      return (
                        <div className="card-branch" key={`${goal.id}-${activity.id}`}>
                          <VLine
                            height={16}
                            color={ghost ? 'var(--line-soft)' : LEVEL_TOKENS.activity.line}
                          />
                          <ActivityCard
                            node={activity}
                            text={textOf(activity)}
                            selected={selectedId === activity.id}
                            expanded={activityOpen}
                            childCount={actions.length}
                            relationJudgment={relationJudgments[relationKey]}
                            highlight={highlights[String(activity.id)]}
                            mergeChecked={activityMergeSelection.includes(activity.id)}
                            mergingInto={mergeInto[activity.id]}
                            mergingFrom={mergeFrom[activity.id]}
                            ghost={ghost}
                            onSelect={() => onSelect(activity.id)}
                            onToggle={() => onToggle(`activity-${activity.id}`)}
                            onJudge={(next) => onJudgeRelation(activity.id, goal.id, next)}
                            onTextEdit={(next) => onTextEdit(activity, next)}
                            onMergeToggle={() => onActivityMergeToggle(activity.id)}
                          />

                          {activityOpen && actions.length > 0 && (
                            <>
                              <VLine height={14} color={LEVEL_TOKENS.action.line} />
                              <div className="card-action-stack">
                                <div
                                  className="card-action-spine"
                                  style={{ background: LEVEL_TOKENS.action.line }}
                                  aria-hidden="true"
                                />
                                {actions.map((action) => (
                                  <ActionCard
                                    key={action.id}
                                    node={action}
                                    selected={selectedId === action.id}
                                    removed={removedActions.has(`${action.id}:${activity.id}`)}
                                    onSelect={() => onSelect(action.id)}
                                    onRemove={() => onRemoveAction(action.id, activity.id, action.text)}
                                  />
                                ))}
                              </div>
                            </>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
