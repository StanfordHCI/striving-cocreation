/**
 * First-run walkthrough for the card editor.
 *
 * Each step pairs a sentence with a small looping demo of the interaction it
 * describes, because "click the expand pill" reads faster as a moving picture
 * than as prose. The demos are self-contained mock-ups — they never touch real
 * data, so the tour works before anything has been recorded.
 */

import { useEffect, useState, type ReactNode } from 'react';

// ── Animation driver ─────────────────────────────────────────────────────────

/** Step a looping phase sequence at a fixed interval. */
function usePhaseLoop(sequence: number[], intervalMs: number): number {
  const [phase, setPhase] = useState(sequence[0] ?? 0);

  useEffect(() => {
    let index = 0;
    const id = setInterval(() => {
      index = (index + 1) % sequence.length;
      setPhase(sequence[index]);
    }, intervalMs);
    return () => clearInterval(id);
    // The sequence is a literal defined at module scope in every caller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs]);

  return phase;
}

function Demo({ caption, children }: { caption?: string; children: ReactNode }) {
  return (
    <div className="onb-demo">
      {caption && <span className="onb-demo-caption">{caption}</span>}
      {children}
    </div>
  );
}

function Dot({ color, size = 6 }: { color: string; size?: number }) {
  return (
    <span
      className="onb-dot"
      style={{ width: size, height: size, background: color }}
      aria-hidden="true"
    />
  );
}

// ── The levels of the hierarchy ──────────────────────────────────────────────

function LevelsVisual() {
  const levels = [
    { color: 'var(--level-goal)', size: 8, label: 'Strivings', note: 'what you’re trying to achieve' },
    { color: 'var(--level-activity)', size: 6, label: 'Activities', note: 'what you’ve been doing toward it' },
    { color: 'transparent', size: 5, label: 'Actions', note: 'specific things the system observed', square: true },
  ];
  return (
    <Demo>
      <div className="onb-levels">
        {levels.map((level) => (
          <div key={level.label} className="onb-level">
            {level.square
              ? <span className="onb-square" aria-hidden="true" />
              : <Dot color={level.color} size={level.size} />}
            <span>
              <strong>{level.label}</strong>
              <span className="onb-muted"> — {level.note}</span>
            </span>
          </div>
        ))}
      </div>
    </Demo>
  );
}

// ── Expanding a branch ───────────────────────────────────────────────────────

const EXPAND_SEQUENCE = [0, 1, 1, 2, 2, 2, 0];

function ExpandVisual() {
  const phase = usePhaseLoop(EXPAND_SEQUENCE, 900);

  return (
    <Demo caption={phase === 0 ? 'click to expand' : phase === 1 ? 'expanded' : 'click again…'}>
      <div className="onb-row">
        <Dot color="var(--level-goal)" size={7} />
        <span className="onb-strong">Staying healthy and fit</span>
      </div>
      <div className="onb-indent-1">
        <span className={`onb-pill${phase >= 1 ? ' is-open' : ''}`}>
          <span className="onb-caret">▶</span>
          {phase >= 1 ? 'collapse' : '2 activities'}
        </span>
      </div>

      <div className="onb-reveal" style={{ maxHeight: phase >= 1 ? 96 : 0, opacity: phase >= 1 ? 1 : 0 }}>
        <div className="onb-row onb-indent-2">
          <Dot color="var(--level-activity)" size={5} />
          <span>Morning exercise routine</span>
        </div>
        <div className="onb-indent-3">
          <span className={`onb-pill soft${phase >= 2 ? ' is-open' : ''}`}>
            <span className="onb-caret">▶</span>
            {phase >= 2 ? 'collapse' : '2 actions'}
          </span>
        </div>
        <div className="onb-row onb-indent-2">
          <Dot color="var(--level-activity)" size={5} />
          <span>Tracking calories</span>
        </div>
      </div>

      <div className="onb-reveal delayed" style={{ maxHeight: phase >= 2 ? 52 : 0, opacity: phase >= 2 ? 1 : 0 }}>
        <div className="onb-row onb-indent-4">
          <span className="onb-square" aria-hidden="true" />
          <span className="onb-faint">Opened workout app</span>
        </div>
        <div className="onb-row onb-indent-4">
          <span className="onb-square" aria-hidden="true" />
          <span className="onb-faint">Started 30-min run</span>
        </div>
      </div>
    </Demo>
  );
}

// ── Evidence ─────────────────────────────────────────────────────────────────

const EVIDENCE_NODES = [
  { color: 'var(--level-goal)', size: 7, label: 'Staying healthy and fit', indent: 0 },
  { color: 'var(--level-activity)', size: 5, label: 'Morning exercise routine', indent: 18 },
  { color: null, size: 5, label: 'Opened workout app', indent: 36 },
];

function EvidenceVisual() {
  const [selected, setSelected] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setSelected((current) => (current + 1) % EVIDENCE_NODES.length), 1800);
    return () => clearInterval(id);
  }, []);

  return (
    <Demo caption="click any card">
      <div className="onb-evidence">
        <div className="onb-evidence-tree">
          {EVIDENCE_NODES.map((node, index) => (
            <div
              key={node.label}
              className={`onb-row${index === selected ? ' is-selected' : ''}`}
              style={{ marginLeft: node.indent }}
            >
              {node.color ? <Dot color={node.color} size={node.size} /> : <span className="onb-square" aria-hidden="true" />}
              <span className={index === selected ? 'onb-strong' : undefined}>{node.label}</span>
            </div>
          ))}
        </div>
        <div className="onb-evidence-panel">
          <span className="onb-evidence-label">Evidence</span>
          <div className="onb-evidence-shots">
            {[0, 1, 2].map((shot) => (
              <div key={shot} className="onb-shot" style={{ animationDelay: `${shot * 90}ms` }} />
            ))}
          </div>
        </div>
      </div>
    </Demo>
  );
}

// ── Editing ──────────────────────────────────────────────────────────────────

const EDITING_SEQUENCE = [0, 0, 1, 1, 0, 2, 3, 4, 4, 5, 5, 6, 6, 0];

function EditingVisual() {
  const phase = usePhaseLoop(EDITING_SEQUENCE, 800);

  const goalRemoved = phase === 1;
  const editing = phase >= 3 && phase <= 5;
  const wrongStriving = phase === 6;
  const activityText = phase === 4
    ? 'Morning exercise rou'
    : phase === 5
      ? 'Morning exercise routine and stretching'
      : 'Morning exercise routine';

  const caption = phase === 1
    ? 'removing striving…'
    : phase >= 3 && phase < 5
      ? 'editing activity…'
      : phase === 5
        ? 'saved'
        : wrongStriving ? 'wrong striving…' : '';

  return (
    <Demo caption={caption}>
      <div className={`onb-card goal${goalRemoved ? ' is-removed' : ''}`}>
        <div className="onb-card-head">
          <Dot color="var(--level-goal)" size={6} />
          <span className="onb-kind">Striving</span>
          {goalRemoved && <span className="onb-tag removed">removed</span>}
        </div>
        <div className="onb-card-text">Managing personal finances</div>
        <span className={`onb-btn reject${goalRemoved ? ' is-active' : ''}`}>✕ Remove</span>
      </div>

      <div className={`onb-card activity${editing ? ' is-editing' : ''}${wrongStriving ? ' is-flagged' : ''}`}>
        <div className="onb-card-head">
          <Dot color="var(--level-activity)" size={5} />
          <span className="onb-kind activity">Activity</span>
          {wrongStriving && <span className="onb-tag flagged">wrong striving</span>}
        </div>
        <div className={`onb-card-text${editing ? ' is-editing' : ''}`}>
          {activityText}
          {phase === 4 && <span className="onb-caret-blink" aria-hidden="true" />}
        </div>
        <div className="onb-btn-row">
          <span className={`onb-btn flag${wrongStriving ? ' is-active' : ''}`}>→ Wrong striving</span>
          <span className="onb-btn">✕ Remove</span>
        </div>
      </div>
    </Demo>
  );
}

// ── Merging ──────────────────────────────────────────────────────────────────

const MERGE_SEQUENCE = [0, 1, 2, 3, 3, 4, 4, 0];

function MergeVisual() {
  const phase = usePhaseLoop(MERGE_SEQUENCE, 900);

  const firstChecked = phase >= 1;
  const secondChecked = phase >= 2;
  const merging = phase >= 3;
  const merged = phase >= 4;

  return (
    <Demo caption={merged ? 'merged' : merging ? 'merging…' : secondChecked ? 'ready to merge' : 'check two cards'}>
      {merged ? (
        <div className="onb-card goal is-merged">
          <div className="onb-card-head">
            <Dot color="#a78bfa" size={6} />
            <span className="onb-kind merged">Striving</span>
          </div>
          <div className="onb-card-text">Keeping up with coursework</div>
        </div>
      ) : (
        <>
          <div className={`onb-card goal${merging ? ' is-merging' : ''}`}>
            <div className="onb-card-head">
              <span className={`onb-check${firstChecked ? ' is-checked' : ''}`} aria-hidden="true">
                {firstChecked ? '✓' : ''}
              </span>
              <span className="onb-kind">Striving</span>
            </div>
            <div className="onb-card-text">Staying on top of classes</div>
          </div>
          <div className={`onb-card goal${merging ? ' is-merging' : ''}`}>
            <div className="onb-card-head">
              <span className={`onb-check${secondChecked ? ' is-checked' : ''}`} aria-hidden="true">
                {secondChecked ? '✓' : ''}
              </span>
              <span className="onb-kind">Striving</span>
            </div>
            <div className="onb-card-text">Keeping up with coursework</div>
          </div>
        </>
      )}
      <span className={`onb-btn merge${secondChecked ? ' is-visible' : ''}`}>⨆ Merge 2 strivings</span>
    </Demo>
  );
}

// ── Edit history ─────────────────────────────────────────────────────────────

const HISTORY_SEQUENCE = [0, 1, 2, 2, 3, 4, 4, 0];

const HISTORY_ENTRIES = [
  { icon: '✕', label: 'Removed', color: '#C62828', detail: 'Managing finances' },
  { icon: '✎', label: 'Edited', color: 'var(--level-activity)', detail: 'Working on research → Writing paper' },
  { icon: '⨆', label: 'Merged strivings', color: 'var(--level-goal)', detail: 'Combined into one' },
];

function HistoryVisual() {
  const phase = usePhaseLoop(HISTORY_SEQUENCE, 900);
  const visible = phase <= 0 ? 1 : phase <= 1 ? 2 : 3;
  const undoing = phase >= 3;

  return (
    <Demo>
      <div className="onb-history">
        <div className="onb-history-list">
          <div className="onb-history-tab">Edits ({undoing ? visible - 1 : visible})</div>
          {HISTORY_ENTRIES.slice(0, visible).map((entry, index) => {
            const removing = undoing && index === visible - 1;
            return (
              <div key={entry.label} className={`onb-history-row${removing ? ' is-removing' : ''}`}>
                <span style={{ color: entry.color }} aria-hidden="true">{entry.icon}</span>
                <span className="onb-history-label">{entry.label}</span>
                <span className="onb-history-detail">{entry.detail}</span>
              </div>
            );
          })}
        </div>
        <div className="onb-history-undo">
          <span className={`onb-btn${phase === 3 ? ' is-pressed' : ''}`}>↶ Undo</span>
          <span className="onb-faint">or ⌘Z</span>
        </div>
      </div>
    </Demo>
  );
}

// ── Compile ──────────────────────────────────────────────────────────────────

const DONE_SEQUENCE = [0, 1, 1, 2, 2, 2, 0];

function DoneVisual() {
  const phase = usePhaseLoop(DONE_SEQUENCE, 1000);

  return (
    <Demo>
      <div className="onb-done-head">
        <span className={`onb-compile-btn${phase === 1 ? ' is-pressed' : ''}`}>Compile</span>
        <span className="onb-faint">
          {phase === 0 ? 'click when ready' : phase === 1 ? 'compiling…' : 'updated'}
        </span>
        {phase === 1 && <span className="onb-spinner" aria-hidden="true" />}
      </div>
      <div className="onb-reveal" style={{ maxHeight: phase === 2 ? 64 : 0, opacity: phase === 2 ? 1 : 0 }}>
        <div className="onb-row onb-result added">
          <Dot color="#22c55e" size={5} />
          <span>New: Daily wellness routine</span>
        </div>
        <div className="onb-row onb-result modified">
          <Dot color="#3b82f6" size={5} />
          <span>Updated: Writing research paper</span>
        </div>
      </div>
    </Demo>
  );
}

// ── Steps ────────────────────────────────────────────────────────────────────

type Visual = 'levels' | 'expand' | 'evidence' | 'editing' | 'merge' | 'history' | 'done';

type Step = { title: string; body: string; visual?: Visual };

export function onboardingSteps(userName?: string): Step[] {
  return [
    {
      title: userName ? `Welcome, ${userName}` : 'Welcome to the hierarchy editor',
      body: 'Tempo watched your screen activity and inferred what you might be striving toward. This is where you review and refine those inferences.',
    },
    {
      title: 'Your striving tree',
      body: 'Strivings sit at the top — broad things you are trying to achieve. Each branches into activities (what you have been doing) and actions (specific things Tempo observed).',
      visual: 'levels',
    },
    {
      title: 'Expanding the tree',
      body: 'Click the pill on any striving to reveal its activities, and again on an activity to see its actions.',
      visual: 'expand',
    },
    {
      title: 'Screenshot evidence',
      body: 'Select any card to see the screenshots Tempo used to infer that statement, in the inspector beside the tree.',
      visual: 'evidence',
    },
    {
      title: 'Editing',
      body: 'Rewrite any card’s text inline, accept it, or remove it. Activities can also be marked as sitting under the wrong striving. Each change can carry a short note explaining why — that note goes to the model.',
      visual: 'editing',
    },
    {
      title: 'Merging',
      body: 'If two cards at the same level are really one thing, check both and merge them. You choose which wording survives.',
      visual: 'merge',
    },
    {
      title: 'Edit history and undo',
      body: 'Every change is staged, not saved — you can see them all in the edit history and undo the last one at any time with ⌘Z.',
      visual: 'history',
    },
    {
      title: 'Compiling',
      body: 'When you are done, hit Compile. Tempo copies your data, applies your edits, re-runs the model with your feedback, and shows you what changed before anything is kept.',
      visual: 'done',
    },
    {
      title: 'You’re ready',
      body: 'Work through the tree at your own pace. Nothing is written until you compile and accept. Reopen this guide any time with the ? button.',
    },
  ];
}

const VISUALS: Record<Visual, () => ReactNode> = {
  levels: LevelsVisual,
  expand: ExpandVisual,
  evidence: EvidenceVisual,
  editing: EditingVisual,
  merge: MergeVisual,
  history: HistoryVisual,
  done: DoneVisual,
};

// ── Overlay ──────────────────────────────────────────────────────────────────

export function OnboardingOverlay({
  steps,
  step,
  onNext,
  onBack,
  onSkip,
}: {
  steps: Step[];
  step: number;
  onNext: () => void;
  onBack: () => void;
  onSkip: () => void;
}) {
  const current = steps[step];
  const isFirst = step === 0;
  const isLast = step === steps.length - 1;
  const Visual = current.visual ? VISUALS[current.visual] : null;

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onSkip();
      if (event.key === 'ArrowRight' || event.key === 'Enter') onNext();
      if (event.key === 'ArrowLeft' && !isFirst) onBack();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onSkip, onNext, onBack, isFirst]);

  return (
    <div className="card-modal-scrim" role="presentation">
      <div className="card-modal onboarding" role="dialog" aria-modal="true" aria-label={current.title}>
        <div className="onb-dots" aria-hidden="true">
          {steps.map((item, index) => (
            <span
              key={item.title}
              className={`onb-dot-step${index === step ? ' is-current' : index < step ? ' is-done' : ''}`}
            />
          ))}
        </div>

        <span className="onb-eyebrow">
          {isFirst ? 'Getting started' : isLast ? 'All set' : `Step ${step} of ${steps.length - 2}`}
        </span>
        <h2>{current.title}</h2>
        <p className="onb-body">{current.body}</p>

        {Visual && <Visual />}

        <div className="onb-nav">
          <div>
            {!isFirst && (
              <button type="button" className="link" onClick={onBack}>← Back</button>
            )}
          </div>
          <div className="onb-nav-right">
            {!isLast && (
              <button type="button" className="link muted" onClick={onSkip}>Skip</button>
            )}
            <button type="button" className="primary" onClick={onNext}>
              {isLast ? 'Start editing' : 'Next'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
