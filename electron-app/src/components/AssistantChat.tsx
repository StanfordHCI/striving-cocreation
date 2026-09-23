import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ArrowUp, LoaderCircle } from 'lucide-react';
import type { ChatFocus, ChatHistory, ChatIntent, ChatMessage, ChatReply, ChatSendRequest } from '../api/assistantContracts';
import './AssistantChat.css';

export type AssistantChatHandle = {
  open: (focus: ChatFocus, intent?: ChatIntent) => void;
  newChat: () => void;
  loadThread: (id: string) => void;
};
type Props = {
  enabled: boolean;
  visible: boolean;
  contextId: string | null;
  sendTurn: (request: ChatSendRequest, signal: AbortSignal) => Promise<ChatReply>;
  loadHistory: (sessionId: string, signal: AbortSignal) => Promise<ChatHistory>;
  onSelectEntity: (id: number) => void;
  onOpen: () => void;
  onSaved: () => void;
  onStateChange: (sessionId: string | null, busy: boolean) => void;
};
type DisplayMessage = ChatMessage & { refs: number[]; failed?: boolean };
const OVERVIEW: ChatFocus = { kind: 'overview', title: 'Your goals and recent activity', detail: '', entity_ids: [] };
const SESSION_KEY = 'tempo.assistant.chat';
function rememberSession(id: string | null) {
  try { if (id) localStorage.setItem(SESSION_KEY, id); else localStorage.removeItem(SESSION_KEY); } catch { /* History remains on the local server. */ }
}

function MessageText({ content, refs, onSelectEntity }: { content: string; refs: number[]; onSelectEntity: (id: number) => void }) {
  const pieces: ReactNode[] = [];
  const pattern = /\[entity:([1-9][0-9]*):([^\[\]:\n]{1,100})\]/g;
  let cursor = 0;
  for (const match of content.matchAll(pattern)) {
    const start = match.index;
    const id = Number(match[1]);
    pieces.push(content.slice(cursor, start));
    pieces.push(refs.includes(id)
      ? <button type="button" className="chat-entity-link" key={`${start}-${id}`} onClick={() => onSelectEntity(id)}>{match[2]}</button>
      : match[0]);
    cursor = start + match[0].length;
  }
  pieces.push(content.slice(cursor));
  return <>{pieces}</>;
}

export default forwardRef<AssistantChatHandle, Props>(function AssistantChat({ enabled, visible, contextId, sendTurn, loadHistory, onSelectEntity, onOpen, onSaved, onStateChange }, ref) {
  const [focus, setFocus] = useState<ChatFocus>(OVERVIEW);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [draftIntent, setDraftIntent] = useState<ChatIntent>('reflect');
  const [busy, setBusy] = useState(false);
  const [hydrating, setHydrating] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const session = useRef<{ id: string | null; revision: number }>({ id: null, revision: 0 });
  const pending = useRef<AbortController | null>(null);
  const failed = useRef<{ request: ChatSendRequest; messageIndex: number } | null>(null);
  const log = useRef<HTMLDivElement | null>(null);
  const input = useRef<HTMLTextAreaElement | null>(null);
  const historyRequest = useRef<AbortController | null>(null);

  async function restore(id: string, controller: AbortController) {
    setHydrating(true);
    setError(null);
    try {
      const history = await loadHistory(id, controller.signal);
      if (controller.signal.aborted) return;
      session.current = { id: history.session_id, revision: history.revision };
      rememberSession(history.session_id);
      setFocus(history.messages.at(-1)?.focus ?? OVERVIEW);
      setMessages(history.messages.map((message) => ({ ...message, refs: message.role === 'assistant' ? [...message.content.matchAll(/\[entity:([1-9][0-9]*):[^\[\]:\n]{1,100}\]/g)].map((match) => Number(match[1])) : [] })));
    } catch (reason: unknown) {
      if (!controller.signal.aborted) {
        session.current = { id: null, revision: 0 };
        setError(reason instanceof Error ? reason.message : 'Could not restore the conversation.');
      }
    } finally { if (!controller.signal.aborted) setHydrating(false); }
  }

  useEffect(() => {
    const controller = new AbortController();
    historyRequest.current = controller;
    let saved: string | null = null;
    try { saved = localStorage.getItem(SESSION_KEY); } catch { /* Storage can be disabled by the browser. */ }
    if (!saved) { setHydrating(false); return () => controller.abort(); }
    void restore(saved, controller);
    return () => controller.abort();
  }, [loadHistory]);

  useEffect(() => () => { pending.current?.abort(); historyRequest.current?.abort(); }, []);
  useEffect(() => { if (visible) input.current?.focus(); }, [visible, hydrating]);
  useEffect(() => { if (log.current) log.current.scrollTop = log.current.scrollHeight; }, [messages, busy, visible]);
  useEffect(() => { onStateChange(session.current.id, busy || hydrating); }, [messages, busy, hydrating, onStateChange]);

  async function deliver(request: ChatSendRequest, retryIndex?: number) {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError(null);
    setNotice(null);
    failed.current = null;
    const messageIndex = retryIndex ?? session.current.revision * 2;
    if (retryIndex === undefined) {
      setMessages((current) => [...current, { role: 'user', content: request.message, timestamp: new Date().toISOString(), focus: request.focus, strategy: null, refs: [] }]);
    } else {
      setMessages((current) => current.map((message, index) => index === retryIndex ? { ...message, failed: false } : message));
    }
    try {
      const reply = await sendTurn(request, controller.signal);
      if (controller.signal.aborted) return;
      session.current = { id: reply.session_id, revision: reply.revision };
      rememberSession(reply.session_id);
      setMessages((current) => [...current, { ...reply.message, refs: reply.entity_refs }]);
      onSaved();
    } catch (reason: unknown) {
      if (!controller.signal.aborted) {
        failed.current = { request, messageIndex };
        setMessages((current) => current.map((message, index) => index === messageIndex ? { ...message, failed: true } : message));
        setError(reason instanceof Error ? reason.message : 'Could not get a reply. Please try again.');
      }
    } finally {
      if (pending.current === controller) { pending.current = null; setBusy(false); }
    }
  }

  function ask(message: string, intent: ChatIntent, selectedFocus: ChatFocus) {
    if (!enabled) { setError('Enable the assistant before sending a message.'); return; }
    if (hydrating) { setDraft(message); setDraftIntent(intent); return; }
    if (failed.current) { setDraft(message); setDraftIntent(intent); setNotice('Retry the previous reply or start a new chat before continuing.'); return; }
    if (pending.current) {
      setDraft(message);
      setDraftIntent(intent);
      setNotice('Your question is ready to send after the current reply.');
      return;
    }
    setDraft('');
    setDraftIntent('reflect');
    void deliver({ request_id: crypto.randomUUID(), session_id: session.current.id, expected_revision: session.current.revision, context_id: contextId, message, intent, focus: selectedFocus });
  }

  useImperativeHandle(ref, () => ({
    open: (selectedFocus, intent = 'reflect') => {
      if (pending.current || hydrating) return;
      if (JSON.stringify(selectedFocus) !== JSON.stringify(focus)) newChat(selectedFocus);
      setFocus(selectedFocus);
      onOpen();
      setNotice(null);
      if (intent === 'suggest') ask('Suggest concrete next steps for this.', 'suggest', selectedFocus);
      if (intent === 'prepare') ask(`Prepare this next step here: ${selectedFocus.detail.split('\n\n')[0] || selectedFocus.title}\n\nWrite a draft or plan I can review and refine with you.`, 'prepare', selectedFocus);
    },
    newChat: () => { if (!pending.current && !hydrating) { newChat(); onOpen(); } },
    loadThread: (id) => {
      if (pending.current) return;
      historyRequest.current?.abort();
      const controller = new AbortController();
      historyRequest.current = controller;
      newChat();
      onOpen();
      void restore(id, controller);
    },
  }));

  function submit(event: FormEvent) { event.preventDefault(); if (draft.trim() && canSend) ask(draft.trim(), draftIntent, focus); }
  function inspect(id: number) { onSelectEntity(id); }
  function newChat(selectedFocus: ChatFocus = OVERVIEW) {
    session.current = { id: null, revision: 0 };
    rememberSession(null);
    setFocus(selectedFocus);
    setMessages([]); setError(null); setNotice(null); setDraft(''); setDraftIntent('reflect'); failed.current = null;
  }
  const canSend = enabled && !busy && !hydrating && failed.current === null;

  if (!visible) return null;
  return <section className="assistant-chat" aria-label="Assistant chat">
    <div className="assistant-chat-focus"><span>In focus · {focus.kind === 'overview' ? 'Your context' : focus.kind}</span><strong>{focus.title}</strong><p>{focus.kind === 'suggestion' ? 'Work on a draft or plan here, then keep refining it together.' : 'Discuss your goals, work on a draft, or make a plan.'}</p></div>
    <div className="assistant-chat-log" ref={log} role="log" aria-label="Conversation" aria-live="polite" aria-relevant="additions text">
      {messages.length === 0 && <div className="assistant-chat-empty"><p>What would you like to think through?</p><small>You can talk about the decision, what feels difficult, or what you want to do next.</small></div>}
      {messages.map((message, index) => <article className={`assistant-chat-message ${message.role}`} key={index}>
        <div className="assistant-chat-message-label">{message.role === 'user' ? 'You' : 'Tempo'}</div>
        {message.focus.kind !== 'overview' && message.focus.title !== focus.title && <div className="assistant-chat-message-focus">About: {message.focus.title}</div>}
        <div className="assistant-chat-message-content"><MessageText content={message.content} refs={message.refs} onSelectEntity={inspect} /></div>
        {message.failed && <small className="assistant-chat-failed">Reply unavailable</small>}
      </article>)}
      {busy && <p className="assistant-chat-thinking" role="status"><LoaderCircle className="assistant-spinner" size={15} aria-hidden="true" />Thinking it through…</p>}
    </div>
    <form className="assistant-chat-composer" onSubmit={submit}>
      {!enabled && <p className="assistant-chat-notice">Assistant paused. Enable it to send messages.</p>}
      {hydrating && <p role="status">Loading conversation…</p>}
      {error && <div className="assistant-chat-error" role="alert"><p>{error}</p>{failed.current && <button type="button" onClick={() => { const previous = failed.current; if (previous) void deliver(previous.request, previous.messageIndex); }} disabled={busy || !enabled}>Retry reply</button>}</div>}
      {notice && <p className="assistant-chat-notice" role="status">{notice}</p>}
      <div className="assistant-chat-input"><textarea ref={input} aria-label="Message Tempo" placeholder="What's on your mind?" value={draft} maxLength={2000} rows={3} onChange={(event) => { setDraft(event.target.value); setDraftIntent('reflect'); }} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (draft.trim() && canSend) ask(draft.trim(), draftIntent, focus); } }} /><button type="submit" disabled={!canSend || !draft.trim()} aria-label="Send message"><ArrowUp size={18} aria-hidden="true" /></button></div>
    </form>
  </section>;
});
