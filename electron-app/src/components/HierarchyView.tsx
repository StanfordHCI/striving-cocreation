import { useEffect, useState } from 'react';
import api from '../api/client';
import type { HierarchyResponse } from '../api/contracts';
import CardEditor from './hierarchy/CardEditor';
import HierarchyTable from './hierarchy/HierarchyTable';

type HierarchyViewProps = {
  refreshKey: number;
  onSelectEntity: (id: number | null) => void;
  selectedEntityId: number | null;
  /** True while the pipeline is recording — a compile cannot be accepted then. */
  recording: boolean;
};

type EditorMode = 'cards' | 'table';

type Notice = { kind: 'success' | 'error'; text: string };

export default function HierarchyView({
  refreshKey,
  onSelectEntity,
  selectedEntityId,
  recording,
}: HierarchyViewProps) {
  const [hierarchy, setHierarchy] = useState<HierarchyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [mode, setMode] = useState<EditorMode>('cards');
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    api.getHierarchy(controller.signal).then((result) => {
      setHierarchy(result);
      setLoading(false);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) {
        setNotice({ kind: 'error', text: reason instanceof Error ? reason.message : 'Unable to load hierarchy' });
        setLoading(false);
      }
    });
    return () => controller.abort();
  }, [refreshKey, reloadKey]);

  const modeSwitch = (
    <div className="hierarchy-mode-switch" role="group" aria-label="Hierarchy view">
      <button
        type="button"
        className={mode === 'cards' ? 'is-active' : undefined}
        onClick={() => setMode('cards')}
        aria-pressed={mode === 'cards'}
        title="Mark up the whole tree, then compile"
      >
        Cards
      </button>
      <button
        type="button"
        className={mode === 'table' ? 'is-active' : undefined}
        onClick={() => setMode('table')}
        aria-pressed={mode === 'table'}
        title="Inspect labels, IDs, parents, and capture times"
      >
        Table
      </button>
    </div>
  );

  const heading = (
    <div className="view-heading hierarchy-heading">
      <div>
        <h1>Hierarchy</h1>
        <p>
          {mode === 'cards'
            ? 'Review and correct the goals, activities, and actions Tempo identified.'
            : 'Find items by label or ID, and inspect their parents and capture times.'}
        </p>
      </div>
      <div className="hierarchy-heading-actions">
        {hierarchy && <span className="count-chip">{hierarchy.count} nodes</span>}
        {modeSwitch}
      </div>
    </div>
  );

  return (
    <div className="hierarchy-view is-cards">
      {heading}
      {notice && <div className={`hierarchy-notice ${notice.kind}`} role="status">{notice.text}</div>}

      {loading && !hierarchy ? (
        <div className="panel-state">Loading hierarchy…</div>
      ) : (
        <>
          {/* Keep card edits and table filters when switching views. */}
          <div className="hierarchy-mode-panel" hidden={mode !== 'cards'}>
            <CardEditor
              roots={hierarchy?.roots ?? []}
              active={mode === 'cards'}
              recording={recording}
              selectedId={selectedEntityId}
              onSelectEntity={onSelectEntity}
              onCompiled={() => setReloadKey((current) => current + 1)}
            />
          </div>
          <div className="hierarchy-mode-panel" hidden={mode !== 'table'}>
            <HierarchyTable
              roots={hierarchy?.roots ?? []}
              selectedId={selectedEntityId}
              onSelectEntity={onSelectEntity}
            />
          </div>
        </>
      )}
    </div>
  );
}
