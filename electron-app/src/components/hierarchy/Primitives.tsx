/** Small shared pieces of the card editor: inline text editing, action rows,
 *  merge checkboxes, and the connector lines that make the tree read as one. */

import { useEffect, useRef, useState, type CSSProperties } from 'react';

// ── Inline text editing ──────────────────────────────────────────────────────

type InlineEditProps = {
  text: string;
  onSave: (next: string) => void;
  disabled?: boolean;
  style?: CSSProperties;
  ariaLabel?: string;
};

/**
 * Click-to-edit label. Commits on blur or Enter, abandons on Escape, and
 * stays quiet when the new text matches the old so a stray click never
 * registers as an edit.
 */
export function InlineEdit({ text, onSave, disabled, style, ariaLabel }: InlineEditProps) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(text);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { setValue(text); }, [text]);
  useEffect(() => { if (editing) inputRef.current?.focus(); }, [editing]);

  function commit() {
    setEditing(false);
    const next = value.trim();
    if (next && next !== text) onSave(next);
    else setValue(text);
  }

  if (disabled) {
    return <span className="card-inline-text is-disabled" style={style}>{text}</span>;
  }

  if (editing) {
    return (
      <textarea
        ref={inputRef}
        className="card-inline-input"
        style={style}
        value={value}
        aria-label={ariaLabel ?? `Edit ${text}`}
        onChange={(event) => setValue(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            commit();
          }
          if (event.key === 'Escape') {
            setValue(text);
            setEditing(false);
          }
        }}
        rows={Math.max(2, Math.ceil(value.length / 40))}
      />
    );
  }

  return (
    <span
      className="card-inline-text"
      style={style}
      role="button"
      tabIndex={0}
      title="Click to edit"
      onClick={() => setEditing(true)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          setEditing(true);
        }
      }}
    >
      {text}
    </span>
  );
}

// ── Judgement buttons ────────────────────────────────────────────────────────

const CROSS = (
  <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
    <line x1="4" y1="4" x2="12" y2="12" /><line x1="12" y1="4" x2="4" y2="12" />
  </svg>
);

const CHECK = (
  <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="3 8.5 6.5 12 13 4" />
  </svg>
);

const ARROW = (
  <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
    <path d="M3 8h10M10 5l3 3-3 3" />
  </svg>
);

type GoalActionsProps = {
  judgment: 'approve' | 'reject' | undefined;
  onJudge: (next: 'approve' | 'reject' | null) => void;
};

/** Accept / remove, on a goal card. Clicking the active choice clears it. */
export function GoalEditActions({ judgment, onJudge }: GoalActionsProps) {
  return (
    <div className="card-actions">
      <button
        type="button"
        className={`card-action approve${judgment === 'approve' ? ' is-active' : ''}`}
        onClick={() => onJudge(judgment === 'approve' ? null : 'approve')}
        title="This striving is right"
      >
        {CHECK} Accept
      </button>
      <button
        type="button"
        className={`card-action reject${judgment === 'reject' ? ' is-active' : ''}`}
        onClick={() => onJudge(judgment === 'reject' ? null : 'reject')}
        title="Remove this striving"
      >
        {CROSS} Remove
      </button>
    </div>
  );
}

type RelationActionsProps = {
  judgment: 'misplaced' | 'detach' | undefined;
  onJudge: (next: 'misplaced' | 'detach' | null) => void;
};

/**
 * Activity cards get two ways to say "not here": the activity is real but
 * filed under the wrong striving, or it does not belong to this striving at
 * all. Both unlink it; the distinction is recorded for the edit log.
 */
export function ActivityEditActions({ judgment, onJudge }: RelationActionsProps) {
  return (
    <div className="card-actions">
      <button
        type="button"
        className={`card-action misplaced${judgment === 'misplaced' ? ' is-active' : ''}`}
        onClick={() => onJudge(judgment === 'misplaced' ? null : 'misplaced')}
        title="Correct activity, but it sits under the wrong striving"
      >
        {ARROW} Wrong striving
      </button>
      <button
        type="button"
        className={`card-action reject${judgment === 'detach' ? ' is-active' : ''}`}
        onClick={() => onJudge(judgment === 'detach' ? null : 'detach')}
        title="Remove this activity from this striving"
      >
        {CROSS} Remove
      </button>
    </div>
  );
}

// ── Merge selection ──────────────────────────────────────────────────────────

export function MergeCheck({
  checked,
  onChange,
  accent = 'var(--level-activity)',
  label,
}: {
  checked: boolean;
  onChange: () => void;
  accent?: string;
  label: string;
}) {
  return (
    <label className="card-merge-check" onClick={(event) => event.stopPropagation()}>
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        aria-label={`Select ${label} for merge`}
        style={{ accentColor: accent }}
      />
    </label>
  );
}

// ── Connectors ───────────────────────────────────────────────────────────────

/** Vertical run from a card down to its children's bar. */
export function VLine({ height = 16, color = 'var(--line)' }: { height?: number; color?: string }) {
  return <div className="card-vline" style={{ height, background: color }} aria-hidden="true" />;
}

/**
 * Horizontal bar spanning a row of sibling cards. Drawn only for genuine
 * branches — a lone child reads better on a straight line down.
 *
 * The bar runs centre-to-centre, so it meets each child's drop line rather than
 * overhanging the outer two. That is `(count - 1)` gaps between centres, each
 * one card plus one gutter wide.
 */
export function HBar({
  count,
  gap,
  childWidth,
  color = 'var(--line)',
}: {
  count: number;
  gap: number;
  childWidth: number;
  color?: string;
}) {
  if (count <= 1) return null;
  return (
    <div
      className="card-hbar"
      aria-hidden="true"
      style={{ width: (count - 1) * (childWidth + gap), background: color }}
    />
  );
}
