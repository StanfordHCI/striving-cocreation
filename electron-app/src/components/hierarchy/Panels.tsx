/** Side panel and modals: the edit log, the compile preview, and the two
 *  prompts that collect context while the user marks the tree up. */

import { useEffect, useRef, useState } from 'react';
import type { CompileResult } from '../../api/contracts';
import { EDIT_LABELS, EDIT_TYPE, type EditLogEntry } from './types';

// ── Edit history ─────────────────────────────────────────────────────────────

export function EditHistoryPanel({
  entries,
  onUndo,
}: {
  entries: EditLogEntry[];
  onUndo?: () => void;
}) {
  if (entries.length === 0) {
    return (
      <div className="edit-history is-empty">
        <div className="edit-history-glyph" aria-hidden="true">✎</div>
        <p>No edits yet.</p>
        <p className="edit-history-hint">
          Accept or remove strivings, rewrite their text, or check two to merge.
          Nothing is saved until you compile.
        </p>
      </div>
    );
  }

  return (
    <div className="edit-history">
      <header className="edit-history-header">
        <div>
          <h3>Edit history</h3>
          <p>{entries.length} change{entries.length === 1 ? '' : 's'} staged</p>
        </div>
        {onUndo && (
          <button type="button" className="edit-history-undo" onClick={onUndo} title="Undo last edit (⌘Z)">
            ↶ Undo
          </button>
        )}
      </header>

      <ol className="edit-history-list">
        {/* Newest first — the last thing you did is the thing you are most
            likely to want to check or undo. */}
        {[...entries].reverse().map((entry, index) => {
          const meta = EDIT_LABELS[entry.type];
          const time = new Date(entry.timestamp)
            .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

          return (
            <li key={entry.id} className={`edit-history-item${index === 0 ? ' is-latest' : ''}`}>
              <div className="edit-history-line">
                <span className="edit-history-icon" style={{ color: meta.color }} aria-hidden="true">
                  {meta.icon}
                </span>
                <span className="edit-history-label">{meta.label}</span>
                <span className="edit-history-time">{time}</span>
              </div>

              {entry.type === EDIT_TYPE.TEXT_EDIT && (
                <div className="edit-history-detail">
                  <div className="was">{entry.oldText}</div>
                  <div className="now">{entry.newText}</div>
                </div>
              )}
              {(entry.type === EDIT_TYPE.MERGE_GOALS || entry.type === EDIT_TYPE.MERGE_ACTIVITIES) && (
                <div className="edit-history-detail plain">{entry.newText ?? 'Combined into one'}</div>
              )}
              {entry.type === EDIT_TYPE.REMOVE_ACTION && (
                <div className="edit-history-detail struck">{entry.oldText}</div>
              )}
              {entry.note && <div className="edit-history-note">“{entry.note}”</div>}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// ── Context note ─────────────────────────────────────────────────────────────

/**
 * Optional "why" behind a destructive edit. The note is not bookkeeping — it
 * rides along to the model as an annotation, which is often better signal than
 * the structural change on its own.
 */
export function ContextNoteModal({
  actionLabel,
  onSubmit,
  onCancel,
}: {
  actionLabel: string;
  onSubmit: (note: string) => void;
  onCancel: () => void;
}) {
  const [note, setNote] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => {
    function onKey(event: KeyboardEvent) { if (event.key === 'Escape') onCancel(); }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  return (
    <div className="card-modal-scrim" onClick={onCancel} role="presentation">
      <div
        className="card-modal note"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={actionLabel}
      >
        <h3>{actionLabel}</h3>
        <p className="card-modal-sub">Optionally explain why — the model reads this when it recompiles.</p>
        <textarea
          ref={inputRef}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="e.g. These two are really the same thing…"
          rows={3}
          aria-label="Context note"
        />
        <div className="card-modal-actions">
          <button type="button" className="ghost" onClick={() => onSubmit('')}>Skip</button>
          <button type="button" className="primary" onClick={() => onSubmit(note.trim())}>Save</button>
        </div>
      </div>
    </div>
  );
}

// ── Merge text ───────────────────────────────────────────────────────────────

/** Ask which wording survives a merge — neither original is presumed right. */
export function MergeTextModal({
  textA,
  textB,
  onConfirm,
  onCancel,
}: {
  textA: string;
  textB: string;
  onConfirm: (text: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState(textA);

  useEffect(() => {
    function onKey(event: KeyboardEvent) { if (event.key === 'Escape') onCancel(); }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  return (
    <div className="card-modal-scrim" onClick={onCancel} role="presentation">
      <div
        className="card-modal merge"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Merge strivings"
      >
        <h3>Merge these into one</h3>
        <p className="card-modal-sub">Pick the wording that survives, or write a new one.</p>

        <div className="merge-options">
          <button
            type="button"
            className={`merge-option${text === textA ? ' is-active' : ''}`}
            onClick={() => setText(textA)}
          >
            {textA}
          </button>
          <button
            type="button"
            className={`merge-option${text === textB ? ' is-active' : ''}`}
            onClick={() => setText(textB)}
          >
            {textB}
          </button>
        </div>

        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          rows={3}
          aria-label="Merged text"
        />

        <div className="card-modal-actions">
          <button type="button" className="ghost" onClick={onCancel}>Cancel</button>
          <button
            type="button"
            className="primary"
            disabled={!text.trim()}
            onClick={() => onConfirm(text.trim())}
          >
            Merge
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Compile preview ──────────────────────────────────────────────────────────

const CHANGE_COPY = {
  added: { label: 'new', tone: 'added' },
  modified: { label: 'updated', tone: 'modified' },
  removed: { label: 'removed', tone: 'removed' },
} as const;

/**
 * What the compile produced, before it is allowed anywhere near the live data.
 * Accepting promotes the working copy; reverting throws it away.
 */
export function CompilePreviewModal({
  result,
  busy,
  error,
  canAccept,
  onAccept,
  onRevert,
}: {
  result: CompileResult;
  busy: boolean;
  error: string | null;
  /** False while recording is live — the swap needs the database closed. */
  canAccept: boolean;
  onAccept: () => void;
  onRevert: () => void;
}) {
  const changes = [
    ...result.added.map((change) => ({ ...change, kind: 'added' as const })),
    ...result.modified.map((change) => ({ ...change, kind: 'modified' as const })),
    ...result.removed.map((change) => ({ ...change, kind: 'removed' as const })),
  ];

  return (
    <div className="card-modal-scrim" role="presentation">
      <div className="card-modal compile" role="dialog" aria-modal="true" aria-label="Compile results">
        <header>
          <span className="compile-eyebrow">Compile complete</span>
          <h2>Review changes</h2>
          <p className="card-modal-sub">
            {result.applied.text_edits} text edit{result.applied.text_edits === 1 ? '' : 's'},{' '}
            {result.applied.rejections} removal{result.applied.rejections === 1 ? '' : 's'},{' '}
            {result.applied.merges} merge{result.applied.merges === 1 ? '' : 's'}
            {result.synthesized
              ? ` — then the model re-thought the hierarchy with your feedback across ${result.llm_calls} ${result.llm_calls === 1 ? 'call' : 'calls'}.`
              : ' — applied without re-synthesis (no model configured).'}
          </p>
        </header>

        <div className="compile-changes">
          {changes.length === 0 && (
            <p className="compile-empty">
              Your edits applied cleanly and the hierarchy came back unchanged.
            </p>
          )}
          {changes.map((change) => (
            <article key={`${change.kind}-${change.id}`} className={`compile-change ${change.kind}`}>
              <div className="compile-change-head">
                <span className="compile-change-tag">{CHANGE_COPY[change.kind].label}</span>
                <span className="compile-change-type">{change.type}</span>
              </div>
              {change.kind === 'modified' && change.previous_text && change.previous_text !== change.text ? (
                <>
                  <div className="was">{change.previous_text}</div>
                  <div className="now">{change.text}</div>
                </>
              ) : (
                <div className="compile-change-text">{change.text}</div>
              )}
              {/* A card can change without its text changing — say what moved,
                  or the entry reads as an unexplained "updated". */}
              {change.kind === 'modified' && change.previous_parent_id !== change.parent_id && (
                <div className="compile-change-note">
                  {change.parent_id === null
                    ? 'Left without a parent — it will be re-homed on the next compile.'
                    : change.previous_parent_id === null
                      ? 'Given a parent.'
                      : 'Moved to a different parent.'}
                </div>
              )}
            </article>
          ))}
        </div>

        {error && <p className="compile-error" role="alert">{error}</p>}
        {!canAccept && (
          <p className="compile-warning">
            Stop recording before accepting — the swap needs the database closed.
          </p>
        )}

        <div className="card-modal-actions">
          <button type="button" className="ghost" onClick={onRevert} disabled={busy}>
            Revert
          </button>
          <button
            type="button"
            className="primary"
            onClick={onAccept}
            disabled={busy || !canAccept}
          >
            {busy ? 'Applying…' : 'Accept changes'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Compile progress ─────────────────────────────────────────────────────────

export function CompileProgress({
  step,
  detail,
  llmCalls,
  elapsedSeconds,
}: {
  step: string;
  detail: string;
  llmCalls?: number;
  elapsedSeconds?: number;
}) {
  const STEPS = [
    { id: 'snapshot', label: 'Working copy' },
    { id: 'edits', label: 'Applying your edits' },
    { id: 'synthesize', label: 'Re-thinking the hierarchy' },
    { id: 'diff', label: 'Comparing before and after' },
  ];
  const index = STEPS.findIndex((entry) => entry.id === step);

  // Re-synthesis is effectively the whole runtime, so it is the only step with
  // anything to report while it runs. Everything else completes instantly.
  const inSynthesis = step === 'synthesize';
  const minutes = Math.floor((elapsedSeconds ?? 0) / 60);
  const seconds = Math.floor((elapsedSeconds ?? 0) % 60);
  const elapsed = minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;

  return (
    <div className="card-modal-scrim" role="presentation">
      <div className="card-modal progress" role="dialog" aria-modal="true" aria-label="Compiling">
        <div className="compile-spinner" aria-hidden="true" />
        <h3>Compiling</h3>
        <p className="card-modal-sub">{detail}</p>

        {inSynthesis && (
          <div className="compile-meter" role="status" aria-live="polite">
            <span>
              {llmCalls ? `${llmCalls} model ${llmCalls === 1 ? 'call' : 'calls'}` : 'Starting…'}
            </span>
            <span className="compile-elapsed">{elapsed} elapsed</span>
          </div>
        )}

        <ol className="compile-steps">
          {STEPS.map((entry, position) => (
            <li
              key={entry.id}
              className={position < index ? 'is-done' : position === index ? 'is-active' : ''}
            >
              {entry.label}
            </li>
          ))}
        </ol>

        {inSynthesis && (
          <p className="compile-hint">
            This is the model re-reading your whole hierarchy, so it usually
            takes a minute or two. Your existing data is untouched until you
            accept the result.
          </p>
        )}
      </div>
    </div>
  );
}
