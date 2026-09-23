import { useCallback, useEffect, useState } from 'react';
import { Circle, GitBranch, MessageCircle, Play, Search, Settings, Share2, Square } from 'lucide-react';
import api from './api/client';
import type { TempoStatus } from './api/contracts';
import GraphView from './components/GraphView';
import AssistantView from './components/AssistantView';
import type { ChatFocus } from './api/assistantContracts';
import HierarchyView from './components/HierarchyView';
import Inspector from './components/Inspector';
import OnboardingForm from './components/OnboardingForm';
import RecordView from './components/RecordView';
import SearchView from './components/SearchView';
import TempoMark from './components/TempoMark';
import useRecording from './hooks/useRecording';
import './styles/main.css';

type View = 'record' | 'hierarchy' | 'graph' | 'search' | 'assistant';

const INITIAL_STATUS: TempoStatus = {
  running: false,
  error: null,
  message: 'Connecting…',
};

const NAV_ITEMS: Array<{ id: View; label: string; shortcut: string; icon: typeof Circle }> = [
  { id: 'record', label: 'Record', shortcut: '1', icon: Circle },
  { id: 'hierarchy', label: 'Hierarchy', shortcut: '2', icon: GitBranch },
  { id: 'graph', label: 'Graph', shortcut: '3', icon: Share2 },
  { id: 'search', label: 'Search', shortcut: '4', icon: Search },
  { id: 'assistant', label: 'Assistant', shortcut: '5', icon: MessageCircle },
];

export default function App() {
  // Record is the entry point: nothing else has anything to show until
  // recording has run, and it is where the self-description prompt lives.
  const [view, setView] = useState<View>('record');
  const [status, setStatus] = useState<TempoStatus>(INITIAL_STATUS);
  const [connected, setConnected] = useState(false);
  const [hierarchyRefresh, setHierarchyRefresh] = useState(0);
  const [selectedEntityId, setSelectedEntityId] = useState<number | null>(null);
  const [assistantVisited, setAssistantVisited] = useState(false);
  const [chatFocus, setChatFocus] = useState<ChatFocus | null>(null);
  const clearChatFocus = useCallback(() => setChatFocus(null), []);
  useEffect(() => { if (view === 'assistant') setAssistantVisited(true); }, [view]);
  const recording = useRecording(status, connected, setStatus);
  const { dismissContextForm } = recording;
  const backToRecord = useCallback(() => {
    dismissContextForm();
    setView('record');
  }, [dismissContextForm]);

  useEffect(() => {
    const controller = new AbortController();
    // Reconnect the locally enabled assistant after an app restart. Disabled
    // assistants only prepare local context; enabling remains an explicit action.
    void api.getAssistantFeed(controller.signal).catch(() => {});
    api.getStatus(controller.signal).then((next) => {
      setStatus(next);
      setConnected(true);
    }).catch(() => {
      if (!controller.signal.aborted) setConnected(false);
    });
    const unsubscribe = api.subscribe((event) => {
      if (event.type === 'connection') setConnected(event.connected);
      if (event.type === 'status') setStatus(event.data);
      if (event.type === 'hierarchy') setHierarchyRefresh((current) => current + 1);
    });
    return () => {
      controller.abort();
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey)) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      // Search is reached with ⌘K, which is what the search field advertises.
      // The digits belong to the sidebar, in the order they appear in it.
      if (event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setView('search');
        return;
      }
      const target = NAV_ITEMS.find((item) => item.shortcut === event.key);
      if (!target) return;
      event.preventDefault();
      setView(target.id);
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  return (
    <div className="tempo-shell">
      <header className="app-header">
        <div className="header-logo"><TempoMark size={24} /><span>Tempo</span></div>
        <button type="button" className="search-trigger" onClick={() => setView('search')} aria-label="Search your activity">
          <Search size={14} aria-hidden="true" /><span>Search your activity…</span><kbd>⌘K</kbd>
        </button>
        <div className="header-actions">
          <button
            type="button"
            className={status.running ? 'primary-button header-record-button stop' : 'primary-button header-record-button'}
            onClick={() => void recording.toggleRecording()}
            disabled={!connected || recording.configLoading || recording.busy !== null || recording.contextForm !== null}
            aria-busy={recording.busy !== null}
            title={!connected ? 'Waiting for the local server' : undefined}
          >
            {status.running ? <Square size={14} aria-hidden="true" /> : <Play size={15} aria-hidden="true" />}
            {recording.busy === 'stopping' ? 'Stopping…'
              : recording.busy === 'starting' ? 'Starting…'
              : recording.busy === 'preparing' ? 'Preparing…'
              : status.running ? 'Stop recording' : 'Start recording'}
          </button>
          <button type="button" className="settings-button" aria-label="Recording settings" onClick={() => setView('record')}>
            <Settings size={17} aria-hidden="true" />
          </button>
        </div>
      </header>

      {recording.error && <p className="recording-error" role="alert">{recording.error}</p>}

      <div className="shell-body">
        <aside className="shell-sidebar">
          <nav className="shell-nav" aria-label="Primary navigation">
            {NAV_ITEMS.map((item) => (
              <button
                type="button"
                key={item.id}
                className={view === item.id ? 'nav-item active' : 'nav-item'}
                onClick={() => setView(item.id)}
                aria-current={view === item.id ? 'page' : undefined}
                aria-keyshortcuts={`Meta+${item.shortcut} Control+${item.shortcut}`}
              >
                <item.icon size={18} className="nav-icon" aria-hidden="true" />
                <span>{item.label}</span>
                <kbd>⌘{item.shortcut}</kbd>
              </button>
            ))}
          </nav>
          <div className="sidebar-footer">Goals · Activities · Actions · Operations</div>
        </aside>

        <main className="shell-main">
          {view === 'record' && (
            <RecordView status={status} recording={recording} />
          )}
          {view === 'hierarchy' && (
            <HierarchyView
              refreshKey={hierarchyRefresh}
              onSelectEntity={setSelectedEntityId}
              selectedEntityId={selectedEntityId}
              recording={status.running}
            />
          )}
          {view === 'graph' && (
            <div className="hierarchy-view is-cards">
              <div className="view-heading hierarchy-heading">
                <div>
                  <h1>Graph</h1>
                  <p>Explore connections between goals, activities, and actions.</p>
                </div>
              </div>
              <GraphView
                refreshKey={hierarchyRefresh}
                selectedId={selectedEntityId}
                onSelectEntity={setSelectedEntityId}
              />
            </div>
          )}
          {view === 'search' && <SearchView onSelectEntity={setSelectedEntityId} />}
          {(view === 'assistant' || assistantVisited) && <div hidden={view !== 'assistant'} className="assistant-container">
            <AssistantView active={view === 'assistant'} refreshKey={hierarchyRefresh} onSelectEntity={setSelectedEntityId}
              onReviewGoals={() => setView('hierarchy')} requestedFocus={chatFocus} onFocusHandled={clearChatFocus} />
          </div>}
        </main>

        {selectedEntityId !== null && (
          <Inspector
            entityId={selectedEntityId}
            onClose={() => setSelectedEntityId(null)}
            onSelectEntity={setSelectedEntityId}
            onDiscuss={(focus) => { setChatFocus(focus); setView('assistant'); setSelectedEntityId(null); }}
            width={380}
          />
        )}
      </div>

      {recording.contextForm && (
        <OnboardingForm
          mode={recording.contextForm}
          onBack={backToRecord}
          onDone={() => void recording.completeContextForm()}
          onSkip={() => void recording.completeContextForm()}
        />
      )}

      <footer className="shell-status" role="status">
        <span className={connected ? 'connection-dot connected' : 'connection-dot'} />
        <span>{connected ? 'Connected' : 'Connecting…'}</span>
        <span className="status-message">{status.message || (status.running ? 'Recording active' : 'Ready')}</span>
      </footer>
    </div>
  );
}
