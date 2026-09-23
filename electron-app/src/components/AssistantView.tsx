import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowUpRight, Check, LoaderCircle, MessageCircle, Plus, X } from 'lucide-react';
import api from '../api/client';
import type { AssistantEvidence, AssistantFeed, AssistantResponse, ChatFocus, ChatIntent, ChatThread } from '../api/assistantContracts';
import AssistantChat, { type AssistantChatHandle } from './AssistantChat';
import './AssistantView.css';

type Props = {
  active: boolean;
  refreshKey: number;
  onSelectEntity: (id: number) => void;
  onReviewGoals: () => void;
  requestedFocus: ChatFocus | null;
  onFocusHandled: () => void;
};
type Option = AssistantResponse['options'][number];
const sendChat = api.sendAssistantChat.bind(api);
const loadHistory = api.getAssistantChat.bind(api);

function EvidenceLinks({ ids, evidence, onSelectEntity, onChat }: {
  ids: number[]; evidence: AssistantEvidence[]; onSelectEntity: (id: number) => void;
  onChat: (focus: ChatFocus, intent?: ChatIntent) => void;
}) {
  const items = evidence.filter((item) => ids.includes(item.id));
  if (!items.length) return null;
  return <details className="assistant-evidence">
    <summary>Why this suggestion?</summary>
    <ul>{items.map((item) => <li key={item.id}>
      <button type="button" onClick={() => onSelectEntity(item.id)}>
        <span><small>{item.type} · {new Date(item.timestamp_end ?? item.timestamp_start).toLocaleString()}</small>{item.text}</span>
        <ArrowUpRight size={15} aria-hidden="true" />
      </button>
      <button type="button" className="assistant-evidence-chat" onClick={() => onChat({ kind: item.type, title: item.text, detail: '', entity_ids: [item.id] })}>Discuss this {item.type}</button>
    </li>)}</ul>
  </details>;
}

export default function AssistantView({ active, refreshKey, onSelectEntity, onReviewGoals, requestedFocus, onFocusHandled }: Props) {
  const [feed, setFeed] = useState<AssistantFeed | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [viewedGoalId, setViewedGoalId] = useState<number | null>(null);
  const [pane, setPane] = useState<'steps' | 'chat'>('steps');
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [threadsError, setThreadsError] = useState<string | null>(null);
  const [threadsVersion, setThreadsVersion] = useState(0);
  const [chatBusy, setChatBusy] = useState(true);
  const [chatSessionId, setChatSessionId] = useState<string | null>(null);
  const [situation, setSituation] = useState('');
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());
  const draftChanged = useRef(false);
  const sequence = useRef(0);
  const mutation = useRef<AbortController | null>(null);
  const chat = useRef<AssistantChatHandle | null>(null);
  const showChat = useCallback(() => setPane('chat'), []);
  const chatSaved = useCallback(() => setThreadsVersion((value) => value + 1), []);
  const chatStateChanged = useCallback((id: string | null, pending: boolean) => {
    setChatSessionId(id);
    setChatBusy(pending);
  }, []);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void api.getAssistantThreads(controller.signal).then((response) => {
      if (!controller.signal.aborted) { setThreads(response.threads); setThreadsError(null); }
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setThreadsError(reason instanceof Error ? reason.message : 'Could not load chats.');
    });
    return () => controller.abort();
  }, [active, threadsVersion]);

  const receive = useCallback((response: AssistantFeed) => {
    setFeed(response);
    if (!draftChanged.current) {
      setSelected(response.focus_goal_ids ?? response.lenses.map((lens) => lens.goal_id));
      setSituation(response.user_note);
    }
  }, []);

  useEffect(() => {
    if (!active) return;
    let controller: AbortController | null = null;
    let disposed = false;
    async function refresh() {
      if (mutation.current || controller) return;
      const current = ++sequence.current;
      controller = new AbortController();
      try {
        const response = await api.getAssistantFeed(controller.signal);
        if (!disposed && current === sequence.current) { receive(response); setError(null); }
      } catch (reason: unknown) {
        if (!disposed && !controller.signal.aborted && current === sequence.current) {
          setError(reason instanceof Error ? reason.message : 'Could not load the assistant.');
        }
      } finally { controller = null; }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    const ageTimer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => { disposed = true; controller?.abort(); window.clearInterval(timer); window.clearInterval(ageTimer); };
  }, [active, refreshKey, receive]);

  useEffect(() => () => mutation.current?.abort(), []);

  function openChat(focus: ChatFocus, intent: ChatIntent = 'reflect') {
    chat.current?.open({ ...focus, title: focus.title.slice(0, 300), detail: focus.detail.slice(0, 1500), entity_ids: [...new Set(focus.entity_ids)].slice(0, 8) }, intent);
  }

  useEffect(() => {
    if (requestedFocus && feed?.enabled && active && !chatBusy) {
      chat.current?.open(requestedFocus);
      onFocusHandled();
    }
  }, [requestedFocus, feed?.enabled, active, chatBusy, onFocusHandled]);

  async function change(run: (signal: AbortSignal) => Promise<AssistantFeed>, acceptDraft = false): Promise<boolean> {
    if (mutation.current) return false;
    const controller = new AbortController();
    mutation.current = controller;
    ++sequence.current;
    setBusy(true);
    setError(null);
    try {
      const response = await run(controller.signal);
      if (controller.signal.aborted) return false;
      if (acceptDraft) { draftChanged.current = false; setDirty(false); }
      receive(response);
      return true;
    } catch (reason: unknown) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Could not update the assistant.');
      return false;
    } finally {
      if (mutation.current === controller) { mutation.current = null; setBusy(false); }
    }
  }

  function changeGoal(id: number) {
    setSelected((current) => current.includes(id) ? current.filter((value) => value !== id) : current.length < 3 ? [...current, id] : current);
    draftChanged.current = true;
    setDirty(true);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!feed || !selected.length) return;
    void change((signal) => api.refineAssistant({ context_id: feed.context.id, goal_ids: selected, situation: situation.trim() }, signal), true);
  }

  function discuss(option: Option, intent: ChatIntent = 'reflect') {
    openChat({ kind: 'suggestion', title: option.action, detail: `${option.action}\n\n${option.why}`, entity_ids: [...option.goal_ids, ...option.evidence_ids] }, intent);
  }

  const result = feed?.advice;
  const preparedOptions = result?.options.filter((option) => new Date(option.expires_at).getTime() > now) ?? [];
  const focusGoalIds = feed?.focus_goal_ids ?? feed?.lenses.map((lens) => lens.goal_id) ?? [];
  const viewedGoal = viewedGoalId === null ? null : feed?.goals.find((goal) => goal.id === viewedGoalId) ?? null;
  const options = viewedGoal ? preparedOptions.filter((option) => option.goal_ids.includes(viewedGoal.id)) : preparedOptions;
  const visibleGoals = new Set(preparedOptions.flatMap((option) => option.goal_ids));

  function showGoal(id: number) {
    setViewedGoalId(id);
    setPane('steps');
    // Keep the selected goal's steps in view when the sidebar stacks on mobile.
    window.requestAnimationFrame(() => document.getElementById('assistant-next-steps')?.scrollIntoView({ block: 'nearest' }));
  }

  return <div className="view-scroll assistant-view">
    <div className="view-heading">
      <div><h1>Assistant</h1><p>Next steps and conversations about your goals.</p></div>
      <div className="assistant-header-actions">
        <button type="button" className="secondary-button" onClick={onReviewGoals}>Review goals</button>
      </div>
    </div>
    {(error || feed?.error) && <div className="assistant-error" role="alert"><p>{error ?? feed?.error}</p>
      {feed?.enabled && <button type="button" className="secondary-button" disabled={busy} onClick={() => void change((signal) => api.refreshAssistant(signal))}>Retry suggestions</button>}
    </div>}
    {!feed && !error && <p role="status" className="assistant-loading"><LoaderCircle size={18} className="assistant-spinner" />Loading your context…</p>}
    {feed && !feed.enabled && pane !== 'chat' && <section className="assistant-enable panel-card">
      <h2>Connect your goals to next steps</h2>
      <p>The assistant uses {feed.connection.model} through {feed.connection.label} ({feed.connection.destination}). It shares your onboarding answers, activity summaries, goals, and graph relationships. Chat also shares your messages. Screenshots are not sent.</p>
      <p>When enabled, suggestions update in the background while Tempo is open. You can pause the assistant here.</p>
      <button type="button" className="primary-button" disabled={busy || !feed.connection.configured} onClick={() => void change((signal) => api.setAssistantEnabled(true, feed.connection.id, signal))}>{busy ? 'Connecting…' : 'Enable assistant'}</button>
      {!feed.connection.configured && <p>Configure your AI model in Record settings first.</p>}
    </section>}
    {feed?.enabled && <>
      <div className="assistant-toolbar">
        <span>{feed.busy ? 'Updating suggestions…' : 'Suggestions update as your context changes.'}</span>
        <button type="button" className="assistant-text-button" disabled={busy || feed.busy} onClick={() => void change((signal) => api.refreshAssistant(signal))}>Refresh now</button>
        <button type="button" className="assistant-text-button" disabled={busy} onClick={() => void change((signal) => api.setAssistantEnabled(false, feed.connection.id, signal))}>Pause assistant</button>
      </div>
    </>}
    {feed && <>
      <div className="assistant-layout">
        <aside className="assistant-setup" aria-label="Assistant navigation">
          <button type="button" className="secondary-button assistant-new-chat" disabled={chatBusy} onClick={() => chat.current?.newChat()}><Plus size={16} aria-hidden="true" />New chat</button>
          <button type="button" className="assistant-all-steps" aria-pressed={pane === 'steps' && !viewedGoal} onClick={() => { setViewedGoalId(null); setPane('steps'); }}>All next steps<span>{preparedOptions.length}</span></button>
          <div className="assistant-section-heading"><h2>Goals in focus</h2></div>
          <p className="assistant-hint">Select a goal to see its prepared next steps.</p>
          <div className="assistant-lenses">{focusGoalIds.map((id) => {
            const goal = feed.goals.find((item) => item.id === id);
            const count = preparedOptions.filter((option) => option.goal_ids.includes(id)).length;
            return goal && <div className="assistant-lens" key={id}>
              <button type="button" aria-pressed={pane === 'steps' && viewedGoal?.id === id} aria-controls="assistant-next-steps" onClick={() => showGoal(id)}><span>{goal.text}</span><small>{count ? `${count} prepared ${count === 1 ? 'step' : 'steps'}` : feed.busy ? 'Preparing…' : 'No next steps right now'}</small></button>
            </div>;
          })}</div>
          <nav className="assistant-threads" aria-label="Chat threads"><h2>Recent chats</h2>
            {threadsError && <div role="alert"><p>{threadsError}</p><button type="button" className="assistant-text-button" onClick={() => setThreadsVersion((value) => value + 1)}>Retry loading chats</button></div>}
            {!threads.length && !threadsError && <p className="assistant-hint">Your conversations will appear here.</p>}
            {threads.map((thread) => <button type="button" key={thread.session_id} aria-current={pane === 'chat' && chatSessionId === thread.session_id ? 'page' : undefined} disabled={chatBusy} onClick={() => chat.current?.loadThread(thread.session_id)} title={thread.title}><MessageCircle size={14} aria-hidden="true" /><span>{thread.title}</span></button>)}
          </nav>
          <form onSubmit={submit}>
            <details className="assistant-adjustments">
              <summary>Change goals or add context</summary>
              <fieldset disabled={busy}><legend>Choose up to three goals</legend><div className="assistant-goals">{feed.goals.map((goal) => <label className="assistant-goal" key={goal.id}><input type="checkbox" checked={selected.includes(goal.id)} disabled={!selected.includes(goal.id) && selected.length >= 3} onChange={() => changeGoal(goal.id)} /><span>{goal.text}</span></label>)}</div></fieldset>
              <label className="assistant-situation" htmlFor="assistant-situation">Anything the assistant should know?</label>
              <textarea id="assistant-situation" value={situation} onChange={(event) => { setSituation(event.target.value); draftChanged.current = true; setDirty(true); }} disabled={busy} maxLength={2000} rows={3} placeholder="For example, focus on my application today." />
            </details>
            {dirty && <div className="assistant-form-actions"><button type="submit" className="primary-button" disabled={busy || selected.length === 0}>{busy ? 'Updating…' : 'Update focus'}</button></div>}
            {(feed.focus_goal_ids !== null || feed.user_note || dirty) && <button type="button" className="assistant-text-button" disabled={busy} onClick={() => void change((signal) => api.refineAssistant({ context_id: null, goal_ids: null, situation: '' }, signal), true)}>Reset to automatic suggestions</button>}
          </form>
        </aside>
        <div className="assistant-workspace">
        <section id="assistant-next-steps" hidden={pane !== 'steps'} className="assistant-results" aria-label="Suggestions" aria-busy={feed.busy}>
          <div className="assistant-context"><div><span className="assistant-eyebrow">Latest observation</span><p>{feed.context.summary}</p>{feed.context.observed_at && <small>{new Date(feed.context.observed_at).toLocaleString()}</small>}</div></div>
          <div className="assistant-section-heading"><h2>Next steps</h2>{viewedGoal && <button type="button" className="assistant-text-button" onClick={() => setViewedGoalId(null)}>Show all goals</button>}</div>
          {viewedGoal && <div className="assistant-selected-goal"><p>{viewedGoal.text}</p><div className="assistant-detail-actions"><button type="button" onClick={() => onSelectEntity(viewedGoal.id)}>View goal<ArrowUpRight size={14} aria-hidden="true" /></button><button type="button" onClick={() => openChat({ kind: 'goal', title: viewedGoal.text, detail: '', entity_ids: [viewedGoal.id] })}>Discuss this goal</button></div></div>}
          {result && options.length > 0 && !viewedGoal && <p className="assistant-summary">{result.summary}</p>}
          {options.map((option) => <article className="assistant-option panel-card" key={option.id}>
            <div className="assistant-option-top">
              <div className="assistant-option-goals">{option.goal_ids.map((id) => <button key={id} type="button" className="assistant-goal-link" onClick={() => onSelectEntity(id)}>{result?.goals.find((goal) => goal.id === id)?.text}<ArrowUpRight size={14} aria-hidden="true" /></button>)}</div>
              <button type="button" className="assistant-dismiss" disabled={busy} aria-label={'Dismiss suggestion: ' + option.action} onClick={() => void change((signal) => api.assistantFeedback({ suggestion_id: option.id, status: 'dismissed' }, signal))}><X size={16} aria-hidden="true" /></button>
            </div>
            <h3>{option.action}</h3><p>{option.why}</p>
            <div className="assistant-option-meta"><span title="The model’s assessment of supporting evidence, not a probability.">Confidence {option.confidence}/10</span><span>Until {new Date(option.expires_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</span></div>
            <div className="assistant-detail-actions">
              <button type="button" className="assistant-prepare" disabled={busy || chatBusy} onClick={() => discuss(option, 'prepare')}>Prepare in chat</button>
              <button type="button" disabled={busy || chatBusy} onClick={() => discuss(option)}>Talk this through</button>
              <button type="button" disabled={busy} onClick={() => void change((signal) => api.assistantFeedback({ suggestion_id: option.id, status: 'completed' }, signal))}><Check size={13} aria-hidden="true" />Done</button>
            </div>
            <EvidenceLinks ids={option.evidence_ids} evidence={result?.evidence ?? []} onSelectEntity={onSelectEntity} onChat={openChat} />
          </article>)}
          {result?.tradeoffs.filter((tradeoff) => tradeoff.goal_ids.every((id) => visibleGoals.has(id)) && (!viewedGoal || tradeoff.goal_ids.includes(viewedGoal.id))).map((tradeoff, index) => <article className="assistant-tradeoff" key={index}><h3>Something to weigh</h3><p>{tradeoff.description}</p><div className="assistant-detail-actions"><button type="button" onClick={() => openChat({ kind: 'tradeoff', title: 'The choice between these goals', detail: tradeoff.description, entity_ids: [...tradeoff.goal_ids, ...tradeoff.evidence_ids] })}>Discuss this</button></div></article>)}
          {!options.length && <div className="assistant-placeholder">
            {feed.busy && <LoaderCircle className="assistant-spinner" size={22} aria-hidden="true" />}
            <h3>{feed.busy ? 'Preparing next steps' : viewedGoal ? 'No next steps for this goal right now' : 'No suggestions right now'}</h3>
            <p>{feed.busy ? 'This updates automatically as the assistant considers your recent context.' : 'Suggestions appear when there is current evidence and confidence is at least 8/10. You can still use chat.'}</p>
          </div>}
        </section>
        <AssistantChat ref={chat} visible={pane === 'chat'} enabled={feed.enabled} contextId={feed.context.id} sendTurn={sendChat} loadHistory={loadHistory} onSelectEntity={onSelectEntity} onOpen={showChat} onSaved={chatSaved} onStateChange={chatStateChanged} />
        </div>

      </div>
    </>}
  </div>;
}
