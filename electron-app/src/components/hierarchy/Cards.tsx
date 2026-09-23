/**
 * The three card types the hierarchy is edited through.
 *
 * Each card carries its own state on its face — pending judgment, staged
 * merge, compile highlight — so the user can read the whole markup off the
 * tree without opening a panel.
 */

import { useState } from 'react';
import type { HierarchyNode } from '../../api/contracts';
import {
  ActivityEditActions,
  GoalEditActions,
  InlineEdit,
  MergeCheck,
} from './Primitives';
import { CARD_WIDTH, HIGHLIGHT, JUDGMENT_TINT, LEVEL_TOKENS, MERGE_TINT } from './tokens';
import type { Judgment, RelationJudgment } from './types';

type Highlight = 'added' | 'modified' | undefined;

/** Resolve the border/glow a card wears: merge tint first, then compile. */
function decorate(highlight: Highlight, mergeRelated: boolean, absorbed: boolean) {
  if (mergeRelated) {
    return {
      border: `2px ${absorbed ? 'dashed' : 'solid'} ${MERGE_TINT.border}`,
      boxShadow: `0 0 12px ${MERGE_TINT.glow}`,
    };
  }
  if (highlight) {
    const tint = HIGHLIGHT[highlight];
    return { border: `2px solid ${tint.border}`, boxShadow: `0 0 12px ${tint.glow}` };
  }
  return null;
}

function truncate(text: string, max = 60): string {
  return text.length > max ? `${text.slice(0, max - 3)}...` : text;
}

// ── Goal ─────────────────────────────────────────────────────────────────────

export type GoalCardProps = {
  node: HierarchyNode;
  text: string;
  selected: boolean;
  expanded: boolean;
  childCount: number;
  judgment?: Judgment;
  locked: boolean;
  highlight?: Highlight;
  mergeChecked: boolean;
  /** Text of the goal this one is being folded into, if any. */
  mergingInto?: string;
  /** Text of the goal being folded into this one, if any. */
  mergingFrom?: string;
  onSelect: () => void;
  onToggle: () => void;
  onJudge: (next: Judgment | null) => void;
  onTextEdit: (next: string) => void;
  onMergeToggle: () => void;
  onLockToggle: () => void;
};

export function GoalCard(props: GoalCardProps) {
  const {
    node, text, selected, expanded, childCount, judgment, locked, highlight,
    mergeChecked, mergingInto, mergingFrom,
    onSelect, onToggle, onJudge, onTextEdit, onMergeToggle, onLockToggle,
  } = props;

  const absorbed = Boolean(mergingInto);
  const mergeRelated = absorbed || Boolean(mergingFrom);
  const decoration = decorate(highlight, mergeRelated, absorbed);
  const tokens = LEVEL_TOKENS.goal;

  return (
    <div className="card-row">
      <div
        className={`hierarchy-card goal${selected ? ' is-selected' : ''}${absorbed ? ' is-absorbed' : ''}`}
        style={{
          width: CARD_WIDTH.goal,
          background: mergeRelated ? MERGE_TINT.wash : tokens.background,
          ...(decoration ?? {}),
        }}
        onClick={onSelect}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => { if (event.key === 'Enter') onSelect(); }}
        data-entity-id={node.id}
        data-entity-type="goal"
      >
        <div className="card-label-row">
          <span
            className="card-dot"
            style={{ background: mergeRelated ? MERGE_TINT.border : tokens.accent }}
          />
          <span
            className="card-kind"
            style={{ color: mergeRelated ? MERGE_TINT.ink : tokens.accent }}
          >
            {mergeRelated ? 'Merging' : 'Striving'}
          </span>

          {locked && <span className="card-badge lock" title="Pinned — synthesis will not alter this">pinned</span>}

          {judgment && !absorbed && (
            <span
              className="card-badge"
              style={{
                color: JUDGMENT_TINT[judgment].ink,
                background: JUDGMENT_TINT[judgment].wash,
              }}
            >
              {JUDGMENT_TINT[judgment].label}
            </span>
          )}

          {highlight && !judgment && !absorbed && (
            <span
              className="card-badge"
              style={{ color: HIGHLIGHT[highlight].ink, background: HIGHLIGHT[highlight].wash }}
            >
              {highlight === 'added' ? 'new' : 'updated'}
            </span>
          )}
        </div>

        <div className="card-text goal-text" onClick={(event) => event.stopPropagation()}>
          {absorbed ? text : (
            <InlineEdit text={text} onSave={onTextEdit} disabled={locked} ariaLabel={`Edit striving ${text}`} />
          )}
        </div>

        {mergingInto && (
          <div className="card-merge-note">↔ merging with: <em>{truncate(mergingInto)}</em></div>
        )}
        {mergingFrom && (
          <div className="card-merge-note">↔ merging with: <em>{truncate(mergingFrom)}</em></div>
        )}

        {!absorbed && (
          <div className="card-actions-slot" onClick={(event) => event.stopPropagation()}>
            <GoalEditActions judgment={judgment} onJudge={onJudge} />
            <button
              type="button"
              className={`card-pin${locked ? ' is-active' : ''}`}
              onClick={onLockToggle}
              title={locked ? 'Unpin — allow synthesis to revise this' : 'Pin — keep this exactly as written'}
            >
              {locked ? '⚑ Pinned' : '⚐ Pin'}
            </button>
          </div>
        )}

        {childCount !== 0 && !absorbed && (
          <button
            type="button"
            className={`card-expand${expanded ? ' is-open' : ''}`}
            onClick={(event) => { event.stopPropagation(); onToggle(); }}
            aria-expanded={expanded}
          >
            <span className="card-expand-caret">▶</span>
            {expanded ? 'collapse' : `${childCount} ${childCount === 1 ? 'activity' : 'activities'}`}
          </button>
        )}
      </div>

      {!absorbed && (
        <MergeCheck checked={mergeChecked} onChange={onMergeToggle} accent="var(--level-goal)" label={text} />
      )}
    </div>
  );
}

// ── Activity ─────────────────────────────────────────────────────────────────

export type ActivityCardProps = {
  node: HierarchyNode;
  text: string;
  selected: boolean;
  expanded: boolean;
  childCount: number;
  relationJudgment?: RelationJudgment;
  highlight?: Highlight;
  mergeChecked: boolean;
  mergingInto?: string;
  mergingFrom?: string;
  /**
   * True when this activity already rendered under an earlier goal. Activities
   * can belong to several strivings; showing the full card each time would
   * imply duplicates, so repeats render as a muted reference.
   */
  ghost: boolean;
  onSelect: () => void;
  onToggle: () => void;
  onJudge: (next: RelationJudgment | null) => void;
  onTextEdit: (next: string) => void;
  onMergeToggle: () => void;
};

export function ActivityCard(props: ActivityCardProps) {
  const {
    node, text, selected, expanded, childCount, relationJudgment, highlight,
    mergeChecked, mergingInto, mergingFrom, ghost,
    onSelect, onToggle, onJudge, onTextEdit, onMergeToggle,
  } = props;

  const absorbed = Boolean(mergingInto);
  const mergeRelated = absorbed || Boolean(mergingFrom);
  const decoration = decorate(highlight, mergeRelated, absorbed);
  const tokens = LEVEL_TOKENS.activity;

  return (
    <div className="card-row">
      <div
        className={`hierarchy-card activity${selected ? ' is-selected' : ''}${ghost ? ' is-ghost' : ''}${absorbed ? ' is-absorbed' : ''}`}
        style={{
          width: CARD_WIDTH.activity,
          background: mergeRelated ? MERGE_TINT.wash : tokens.background,
          ...(decoration ?? {}),
        }}
        onClick={onSelect}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => { if (event.key === 'Enter') onSelect(); }}
        data-entity-id={node.id}
        data-entity-type="activity"
      >
        <div className="card-label-row">
          <span
            className="card-dot"
            style={{ background: mergeRelated ? MERGE_TINT.border : tokens.accent }}
          />
          <span
            className="card-kind"
            style={{ color: mergeRelated ? MERGE_TINT.ink : tokens.accent }}
          >
            {ghost ? 'Also here' : 'Activity'}
          </span>

          {relationJudgment && !absorbed && (
            <span
              className="card-badge"
              style={
                relationJudgment === 'misplaced'
                  ? { color: '#92400e', background: '#fef3c7' }
                  : { color: JUDGMENT_TINT.reject.ink, background: JUDGMENT_TINT.reject.wash }
              }
            >
              {relationJudgment === 'misplaced' ? 'wrong striving' : 'removed'}
            </span>
          )}

          {highlight && !relationJudgment && !absorbed && (
            <span
              className="card-badge"
              style={{ color: HIGHLIGHT[highlight].ink, background: HIGHLIGHT[highlight].wash }}
            >
              {highlight === 'added' ? 'new' : 'updated'}
            </span>
          )}
        </div>

        <div className="card-text activity-text" onClick={(event) => event.stopPropagation()}>
          {absorbed || ghost ? text : (
            <InlineEdit text={text} onSave={onTextEdit} ariaLabel={`Edit activity ${text}`} />
          )}
        </div>

        {mergingInto && (
          <div className="card-merge-note">↔ merging with: <em>{truncate(mergingInto)}</em></div>
        )}
        {mergingFrom && (
          <div className="card-merge-note">↔ merging with: <em>{truncate(mergingFrom)}</em></div>
        )}

        {/* A ghost is a reference to the card rendered under the first goal —
            editing belongs there, so it only offers navigation. */}
        {!absorbed && !ghost && (
          <div className="card-actions-slot" onClick={(event) => event.stopPropagation()}>
            <ActivityEditActions judgment={relationJudgment} onJudge={onJudge} />
          </div>
        )}

        {childCount > 0 && !absorbed && !ghost && (
          <button
            type="button"
            className={`card-expand${expanded ? ' is-open' : ''}`}
            onClick={(event) => { event.stopPropagation(); onToggle(); }}
            aria-expanded={expanded}
          >
            <span className="card-expand-caret">▶</span>
            {expanded ? 'collapse' : `${childCount} ${childCount === 1 ? 'action' : 'actions'}`}
          </button>
        )}
      </div>

      {!absorbed && !ghost && (
        <MergeCheck checked={mergeChecked} onChange={onMergeToggle} accent={tokens.accent} label={text} />
      )}
    </div>
  );
}

// ── Action ───────────────────────────────────────────────────────────────────

export type ActionCardProps = {
  node: HierarchyNode;
  selected: boolean;
  removed: boolean;
  onSelect: () => void;
  onRemove: () => void;
};

export function ActionCard({ node, selected, removed, onSelect, onRemove }: ActionCardProps) {
  const [hovered, setHovered] = useState(false);

  return (
    <div
      className={`hierarchy-card action${selected ? ' is-selected' : ''}${removed ? ' is-removed' : ''}`}
      style={{ width: CARD_WIDTH.action }}
      onClick={() => { if (!removed) onSelect(); }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      role="button"
      tabIndex={removed ? -1 : 0}
      onKeyDown={(event) => { if (event.key === 'Enter' && !removed) onSelect(); }}
      data-entity-id={node.id}
      data-entity-type="action"
    >
      <div className="card-label-row">
        <span className="card-square" />
        <span
          className="card-kind"
          style={{ color: removed ? JUDGMENT_TINT.reject.ink : LEVEL_TOKENS.action.accent }}
        >
          {removed ? 'Removed' : 'Action'}
        </span>
        {!removed && hovered && (
          <button
            type="button"
            className="card-remove"
            onClick={(event) => { event.stopPropagation(); onRemove(); }}
            title="Remove this action from the activity"
            aria-label={`Remove action ${node.text}`}
          >
            ✕
          </button>
        )}
      </div>
      <div className="card-text action-text">{node.text}</div>
    </div>
  );
}
