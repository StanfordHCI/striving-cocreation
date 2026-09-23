/**
 * The card-based hierarchy editor.
 *
 * The user marks up the whole tree — accepting, rewriting, merging, rejecting —
 * and then compiles: Tempo copies the database, applies the markup, re-runs the
 * model with that feedback, and shows what changed before anything is kept.
 *
 * Nothing here writes to the server until Compile. That is the point: the model
 * sees the edits as one coherent revision rather than a drip of patches, and
 * the user can undo freely up to the moment they commit.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import api, { TempoApiError } from '../../api/client';
import type { CompileResult, HierarchyNode } from '../../api/contracts';
import { CompilePreviewModal, CompileProgress, ContextNoteModal, EditHistoryPanel, MergeTextModal } from './Panels';
import { OnboardingOverlay, onboardingSteps } from './Onboarding';
import { ScrollableTreeArea, TreeLayout } from './TreeLayout';
import { hasStagedEdits, toBranches, toCompileRequest, type Judgment, type RelationJudgment } from './types';
import { useCardEditor } from './useCardEditor';
import './cards.css';

const ONBOARDING_SEEN_KEY = 'tempo.hierarchy.onboarding.v1';

/** A destructive edit waiting on an optional "why". */
type PendingNote =
  | { kind: 'goal'; label: string; id: number; judgment: Judgment }
  | { kind: 'relation'; label: string; activityId: number; goalId: number; judgment: RelationJudgment }
  | { kind: 'action'; label: string; actionId: number; activityId: number; text: string };

/** A merge waiting on the user to choose the surviving wording. */
type PendingMerge = {
  level: 'goal' | 'activity';
  survivorId: number;
  absorbedId: number;
  textA: string;
  textB: string;
};

export type CardEditorProps = {
  roots: HierarchyNode[];
  active?: boolean;
  /** True while the pipeline is recording — blocks accepting a compile. */
  recording: boolean;
  onSelectEntity: (id: number | null) => void;
  selectedId: number | null;
  onCompiled: () => void;
};

export default function CardEditor({
  roots,
  active = true,
  recording,
  onSelectEntity,
  selectedId,
  onCompiled,
}: CardEditorProps) {
  const editor = useCardEditor();
  const branches = useMemo(() => toBranches(roots), [roots]);

  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [zoom, setZoom] = useState(1);
  const [pendingNote, setPendingNote] = useState<PendingNote | null>(null);
  const [pendingMerge, setPendingMerge] = useState<PendingMerge | null>(null);

  const [compiling, setCompiling] = useState<
    { step: string; detail: string; llmCalls?: number; elapsedSeconds?: number } | null
  >(null);
  const [compileResult, setCompileResult] = useState<CompileResult | null>(null);
  const [compileBusy, setCompileBusy] = useState(false);
  const [compileError, setCompileError] = useState<string | null>(null);

  const [onboardingStep, setOnboardingStep] = useState<number | null>(null);
  const steps = useMemo(() => onboardingSteps(), []);

  // Show the walkthrough once, the first time there is a tree to explain.
  useEffect(() => {
    if (branches.length === 0) return;
    let seen = true;
    try {
      seen = window.localStorage.getItem(ONBOARDING_SEEN_KEY) === '1';
    } catch {
      // Private browsing or blocked storage — treat the tour as already seen
      // rather than showing it on every mount.
      seen = true;
    }
    if (!seen) setOnboardingStep(0);
  }, [branches.length]);

  const dismissOnboarding = useCallback(() => {
    setOnboardingStep(null);
    try {
      window.localStorage.setItem(ONBOARDING_SEEN_KEY, '1');
    } catch {
      // Nothing to do — the tour simply reappears next session.
    }
  }, []);

  // ⌘Z undo, but not while a modal owns the keyboard.
  useEffect(() => {
    if (!active) return;
    function onKey(event: KeyboardEvent) {
      const blocked = pendingNote || pendingMerge || compileResult || compiling || onboardingStep !== null;
      if (blocked) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        editor.undo();
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [active, editor, pendingNote, pendingMerge, compileResult, compiling, onboardingStep]);

  // ── text resolution ────────────────────────────────────────────────────

  const textOf = useCallback(
    (node: HierarchyNode) => editor.edits.textOverrides[node.id] ?? node.text,
    [editor.edits.textOverrides],
  );

  const textById = useMemo(() => {
    const map = new Map<number, string>();
    const walk = (node: HierarchyNode) => {
      map.set(node.id, editor.edits.textOverrides[node.id] ?? node.text);
      node.children.forEach(walk);
    };
    roots.forEach(walk);
    return map;
  }, [roots, editor.edits.textOverrides]);

  const mergeIntoText = useMemo(() => {
    const out: Record<number, string> = {};
    for (const [absorbed, survivor] of Object.entries(editor.mergeInto)) {
      out[Number(absorbed)] = textById.get(survivor) ?? '';
    }
    return out;
  }, [editor.mergeInto, textById]);

  const mergeFromText = useMemo(() => {
    const out: Record<number, string> = {};
    for (const [survivor, absorbed] of Object.entries(editor.mergeFrom)) {
      out[Number(survivor)] = textById.get(absorbed) ?? '';
    }
    return out;
  }, [editor.mergeFrom, textById]);

  // ── handlers ───────────────────────────────────────────────────────────

  const toggle = useCallback((key: string) => {
    setExpanded((current) => ({ ...current, [key]: !current[key] }));
  }, []);

  const expandAll = useCallback(() => {
    const next: Record<string, boolean> = {};
    for (const branch of branches) {
      next[`goal-${branch.goal.id}`] = true;
      for (const activity of branch.activities) next[`activity-${activity.id}`] = true;
    }
    setExpanded(next);
  }, [branches]);

  const handleJudgeGoal = useCallback((id: number, judgment: Judgment | null) => {
    // Only removals are worth interrupting for a reason.
    if (judgment === 'reject') {
      setPendingNote({ kind: 'goal', label: 'Removing this striving', id, judgment });
      return;
    }
    editor.judgeGoal(id, judgment);
  }, [editor]);

  const handleJudgeRelation = useCallback((
    activityId: number,
    goalId: number,
    judgment: RelationJudgment | null,
  ) => {
    if (judgment === null) {
      editor.judgeRelation(activityId, goalId, null);
      return;
    }
    setPendingNote({
      kind: 'relation',
      label: judgment === 'misplaced'
        ? 'This activity sits under the wrong striving'
        : 'Removing this activity from this striving',
      activityId,
      goalId,
      judgment,
    });
  }, [editor]);

  const handleRemoveAction = useCallback((actionId: number, activityId: number, text: string) => {
    if (editor.removedActionSet.has(`${actionId}:${activityId}`)) {
      editor.removeAction(actionId, activityId, text); // toggles it back on
      return;
    }
    setPendingNote({ kind: 'action', label: 'Removing this action', actionId, activityId, text });
  }, [editor]);

  const submitNote = useCallback((note: string) => {
    const pending = pendingNote;
    setPendingNote(null);
    if (!pending) return;
    const trimmed = note.trim() || undefined;

    if (pending.kind === 'goal') editor.judgeGoal(pending.id, pending.judgment, trimmed);
    if (pending.kind === 'relation') {
      editor.judgeRelation(pending.activityId, pending.goalId, pending.judgment, trimmed);
    }
    if (pending.kind === 'action') {
      editor.removeAction(pending.actionId, pending.activityId, pending.text, trimmed);
    }
  }, [pendingNote, editor]);

  // Two checked cards at the same level is the signal to merge.
  const openMerge = useCallback((level: 'goal' | 'activity') => {
    const selection = level === 'goal'
      ? editor.edits.goalMergeSelection
      : editor.edits.activityMergeSelection;
    if (selection.length !== 2) return;
    const [survivorId, absorbedId] = selection;
    setPendingMerge({
      level,
      survivorId,
      absorbedId,
      textA: textById.get(survivorId) ?? '',
      textB: textById.get(absorbedId) ?? '',
    });
  }, [editor.edits.goalMergeSelection, editor.edits.activityMergeSelection, textById]);

  const confirmMerge = useCallback((mergedText: string) => {
    const pending = pendingMerge;
    setPendingMerge(null);
    if (!pending) return;
    editor.confirmMerge(pending.level, pending.survivorId, pending.absorbedId, mergedText);
  }, [pendingMerge, editor]);

  // ── compile ────────────────────────────────────────────────────────────

  const runCompile = useCallback(async () => {
    setCompileError(null);
    setCompiling({ step: 'snapshot', detail: 'Starting…' });
    try {
      const result = await api.compileHierarchy(
        toCompileRequest(editor.edits),
        (event) => {
          if (event.type === 'progress') {
            setCompiling({
              step: event.step,
              detail: event.detail,
              llmCalls: event.llm_calls,
              elapsedSeconds: event.elapsed_seconds,
            });
          }
        },
      );
      setCompileResult(result);
    } catch (error) {
      setCompileError(
        error instanceof TempoApiError ? error.message : 'Compile failed. Please try again.',
      );
    } finally {
      setCompiling(null);
    }
  }, [editor.edits]);

  const acceptCompile = useCallback(async () => {
    if (!compileResult) return;
    setCompileBusy(true);
    setCompileError(null);
    try {
      await api.acceptCompile(compileResult.token);
      setCompileResult(null);
      editor.reset();
      onCompiled();
    } catch (error) {
      setCompileError(
        error instanceof TempoApiError ? error.message : 'Could not apply the compiled hierarchy.',
      );
    } finally {
      setCompileBusy(false);
    }
  }, [compileResult, editor, onCompiled]);

  const revertCompile = useCallback(async () => {
    if (!compileResult) return;
    setCompileBusy(true);
    try {
      await api.revertCompile(compileResult.token);
    } catch {
      // The draft is disposable; a failed cleanup is swept server-side and
      // must not block the user from getting back to their edits.
    } finally {
      setCompileResult(null);
      setCompileBusy(false);
    }
  }, [compileResult]);

  // ── render ─────────────────────────────────────────────────────────────

  const staged = hasStagedEdits(editor.edits);
  const goalMergeReady = editor.edits.goalMergeSelection.length === 2;
  const activityMergeReady = editor.edits.activityMergeSelection.length === 2;

  if (branches.length === 0) {
    return (
      <div className="card-editor is-empty">
        <p>No strivings yet.</p>
        <p className="card-editor-hint">
          Record for a while and Tempo will infer what you are working toward.
        </p>
      </div>
    );
  }

  return (
    <div className="card-editor">
      <div className="card-editor-toolbar">
        <div className="card-editor-toolbar-left">
          <button type="button" onClick={expandAll}>Expand all</button>
          <button type="button" onClick={() => setExpanded({})}>Collapse all</button>
          <span className="card-editor-zoom">
            <button type="button" onClick={() => setZoom((z) => Math.max(0.4, +(z - 0.1).toFixed(2)))} aria-label="Zoom out">−</button>
            <span>{Math.round(zoom * 100)}%</span>
            <button type="button" onClick={() => setZoom((z) => Math.min(1.5, +(z + 0.1).toFixed(2)))} aria-label="Zoom in">+</button>
          </span>
        </div>

        <div className="card-editor-toolbar-right">
          {goalMergeReady && (
            <button type="button" className="merge-cta" onClick={() => openMerge('goal')}>
              ⨆ Merge 2 strivings
            </button>
          )}
          {activityMergeReady && (
            <button type="button" className="merge-cta" onClick={() => openMerge('activity')}>
              ⨆ Merge 2 activities
            </button>
          )}
          <button
            type="button"
            className="help"
            onClick={() => setOnboardingStep(0)}
            aria-label="Show the editor walkthrough"
            title="How this works"
          >
            ?
          </button>
          <button
            type="button"
            className="compile-cta"
            onClick={runCompile}
            disabled={!staged || compiling !== null}
            title={staged ? 'Apply your edits and re-run the model' : 'Make an edit first'}
          >
            Compile
          </button>
        </div>
      </div>

      {compileError && !compileResult && (
        <p className="card-editor-error" role="alert">{compileError}</p>
      )}

      <div className="card-editor-body">
        <ScrollableTreeArea zoom={zoom} onZoomChange={setZoom}>
          <TreeLayout
            branches={branches}
            expanded={expanded}
            selectedId={selectedId}
            textOf={textOf}
            judgments={editor.edits.judgments}
            relationJudgments={editor.edits.relationJudgments}
            lockedIds={editor.edits.locked}
            goalMergeSelection={editor.edits.goalMergeSelection}
            activityMergeSelection={editor.edits.activityMergeSelection}
            mergeInto={mergeIntoText}
            mergeFrom={mergeFromText}
            removedActions={editor.removedActionSet}
            highlights={{}}
            onToggle={toggle}
            onSelect={onSelectEntity}
            onJudgeGoal={handleJudgeGoal}
            onJudgeRelation={handleJudgeRelation}
            onTextEdit={(node, next) => editor.editText(node, next, textOf(node))}
            onGoalMergeToggle={(id) => editor.toggleMergeSelection(id, 'goal')}
            onActivityMergeToggle={(id) => editor.toggleMergeSelection(id, 'activity')}
            onLockToggle={editor.toggleLock}
            onRemoveAction={handleRemoveAction}
          />
        </ScrollableTreeArea>

        <aside className="card-editor-side">
          <EditHistoryPanel entries={editor.log} onUndo={editor.canUndo ? editor.undo : undefined} />
        </aside>
      </div>

      {pendingNote && (
        <ContextNoteModal
          actionLabel={pendingNote.label}
          onSubmit={submitNote}
          onCancel={() => setPendingNote(null)}
        />
      )}

      {pendingMerge && (
        <MergeTextModal
          textA={pendingMerge.textA}
          textB={pendingMerge.textB}
          onConfirm={confirmMerge}
          onCancel={() => setPendingMerge(null)}
        />
      )}

      {compiling && (
        <CompileProgress
          step={compiling.step}
          detail={compiling.detail}
          llmCalls={compiling.llmCalls}
          elapsedSeconds={compiling.elapsedSeconds}
        />
      )}

      {compileResult && (
        <CompilePreviewModal
          result={compileResult}
          busy={compileBusy}
          error={compileError}
          canAccept={!recording}
          onAccept={acceptCompile}
          onRevert={revertCompile}
        />
      )}

      {onboardingStep !== null && (
        <OnboardingOverlay
          steps={steps}
          step={onboardingStep}
          onNext={() => {
            if (onboardingStep >= steps.length - 1) dismissOnboarding();
            else setOnboardingStep(onboardingStep + 1);
          }}
          onBack={() => setOnboardingStep(Math.max(0, onboardingStep - 1))}
          onSkip={dismissOnboarding}
        />
      )}
    </div>
  );
}
