import { z } from 'zod';
import {
  actionResponseSchema,
  compileDispositionSchema,
  compileEventSchema,
  onboardingSchema,
  onboardingSaveSchema,
  observerDiagnosticsSchema,
  deleteResponseSchema,
  entitiesResponseSchema,
  entitySchema,
  entityMutationResponseSchema,
  envConfigSchema,
  exclusionSettingsSchema,
  hierarchyResponseSchema,
  installedAppsSchema,
  mergeResponseSchema,
  reparentResponseSchema,
  screenshotsResponseSchema,
  searchResponseSchema,
  serverEventSchema,
  statsSchema,
  splitResponseSchema,
  tempoStatusSchema,
  timelineResponseSchema,
  type CompileEvent,
  type CompileResult,
  type EntityType,
  type EntityCreateRequest,
  type HierarchyCompileRequest,
  type OnboardingSaveRequest,
  type EntityDeleteRequest,
  type EntityMergeRequest,
  type EntityReparentRequest,
  type EntitySplitRequest,
  type EntityUpdateRequest,
  type ExclusionSettings,
  type ServerEvent,
  type StartRequest,
} from './contracts';
import { getServerBaseUrl } from '../platform/shell';
import {
  assistantFeedSchema, chatReplySchema, chatHistorySchema, chatThreadsSchema,
  type AssistantRefineRequest, type AssistantFeedbackRequest, type ChatSendRequest,
} from './assistantContracts';

export class TempoApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = 'TempoApiError';
  }
}

class TempoApiClient {
  readonly baseUrl: string;
  private socket: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<(event: ServerEvent) => void>();

  constructor(baseUrl = getServerBaseUrl()) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
  }

  private async request<T>(
    path: string,
    schema: z.ZodType<T>,
    init: RequestInit = {},
  ): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    const response = await fetch(`${this.baseUrl}${path}`, { ...init, headers });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const record = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
      const detail = record.detail ?? record.error;
      throw new TempoApiError(typeof detail === 'string' ? detail : `HTTP ${response.status}`, response.status);
    }
    const parsed = schema.safeParse(payload);
    if (!parsed.success) {
      throw new TempoApiError(`Invalid response from ${path}`, 502);
    }
    return parsed.data;
  }

  getStatus(signal?: AbortSignal) {
    return this.request('/api/status', tempoStatusSchema, { signal });
  }

  getAssistantFeed(signal?: AbortSignal) {
    return this.request('/api/assistant/feed', assistantFeedSchema, { signal });
  }

  setAssistantEnabled(enabled: boolean, connectionId: string, signal?: AbortSignal) {
    return this.request('/api/assistant/settings', assistantFeedSchema, {
      method: 'PUT', body: JSON.stringify({ enabled, connection_id: connectionId }), signal,
    });
  }

  refreshAssistant(signal?: AbortSignal) {
    return this.request('/api/assistant/refresh', assistantFeedSchema, { method: 'POST', signal });
  }

  refineAssistant(request: AssistantRefineRequest, signal?: AbortSignal) {
    return this.request('/api/assistant/refine', assistantFeedSchema, { method: 'POST', body: JSON.stringify(request), signal });
  }

  assistantFeedback(request: AssistantFeedbackRequest, signal?: AbortSignal) {
    return this.request('/api/assistant/feedback', assistantFeedSchema, { method: 'POST', body: JSON.stringify(request), signal });
  }

  sendAssistantChat(request: ChatSendRequest, signal?: AbortSignal) {
    return this.request('/api/assistant/chat', chatReplySchema, { method: 'POST', body: JSON.stringify(request), signal });
  }

  getAssistantChat(sessionId: string, signal?: AbortSignal) {
    return this.request(`/api/assistant/chats/${encodeURIComponent(sessionId)}`, chatHistorySchema, { signal });
  }

  getAssistantThreads(signal?: AbortSignal) {
    return this.request('/api/assistant/chats', chatThreadsSchema, { signal });
  }

  getObserverDiagnostics(signal?: AbortSignal) {
    return this.request('/api/diagnostics/observer', observerDiagnosticsSchema, { signal });
  }

  getEnvConfig(signal?: AbortSignal) {
    return this.request('/api/config/env', envConfigSchema, { signal });
  }

  start(request: StartRequest) {
    return this.request('/api/start', actionResponseSchema, {
      method: 'POST',
      body: JSON.stringify(request),
    });
  }

  stop() {
    return this.request('/api/stop', actionResponseSchema, { method: 'POST' });
  }

  getInstalledApps(signal?: AbortSignal) {
    return this.request('/api/apps/installed', installedAppsSchema, { signal });
  }

  getExclusions(signal?: AbortSignal) {
    return this.request('/api/settings/exclusions', exclusionSettingsSchema, { signal });
  }

  updateExclusions(settings: ExclusionSettings) {
    return this.request('/api/settings/exclusions', exclusionSettingsSchema, {
      method: 'PUT',
      body: JSON.stringify(settings),
    });
  }

  getHierarchy(signal?: AbortSignal) {
    return this.request('/api/hierarchy?max_depth=4', hierarchyResponseSchema, { signal });
  }

  search(query: string, entityType?: EntityType, limit = 20, signal?: AbortSignal) {
    const params = new URLSearchParams({ query, limit: String(limit) });
    if (entityType) params.set('entity_type', entityType);
    return this.request(`/api/graph/search?${params}`, searchResponseSchema, {
      method: 'POST',
      signal,
    });
  }

  getTimeline(days = 7, signal?: AbortSignal) {
    return this.request(`/api/graph/timeline?days=${days}`, timelineResponseSchema, { signal });
  }

  getStats(signal?: AbortSignal) {
    return this.request('/api/graph/stats', statsSchema, { signal });
  }

  getEntitiesByType(
    entityType: EntityType,
    options: { limit?: number; offset?: number; include_relations?: boolean } = {},
  ) {
    return this.request('/api/graph/entities', entitiesResponseSchema, {
      method: 'POST',
      body: JSON.stringify({ entity_type: entityType, ...options }),
    });
  }

  getEntity(entityId: number) {
    return this.request(`/api/graph/entity/${entityId}`, entitySchema);
  }

  getEntityScreenshots(entityId: number, signal?: AbortSignal) {
    return this.request(`/api/graph/entity/${entityId}/screenshots`, screenshotsResponseSchema, { signal });
  }

  updateEntity(entityId: number, request: EntityUpdateRequest) {
    return this.request(`/api/entity/${entityId}`, entityMutationResponseSchema, {
      method: 'PATCH',
      body: JSON.stringify(request),
    });
  }

  reparentEntity(entityId: number, request: EntityReparentRequest) {
    return this.request(`/api/entity/${entityId}/reparent`, reparentResponseSchema, {
      method: 'POST',
      body: JSON.stringify(request),
    });
  }

  mergeEntities(request: EntityMergeRequest) {
    return this.request('/api/entity/merge', mergeResponseSchema, {
      method: 'POST',
      body: JSON.stringify(request),
    });
  }

  splitEntity(entityId: number, request: EntitySplitRequest) {
    return this.request(`/api/entity/${entityId}/split`, splitResponseSchema, {
      method: 'POST',
      body: JSON.stringify(request),
    });
  }

  deleteEntity(entityId: number, request: EntityDeleteRequest) {
    return this.request(`/api/entity/${entityId}`, deleteResponseSchema, {
      method: 'DELETE',
      body: JSON.stringify(request),
    });
  }

  createEntity(request: EntityCreateRequest) {
    return this.request('/api/entity', entityMutationResponseSchema, {
      method: 'POST',
      body: JSON.stringify(request),
    });
  }

  /**
   * Compile staged edits, streaming progress until the preview is ready.
   *
   * The endpoint answers with Server-Sent Events rather than one JSON body
   * because re-synthesis runs the model and can take a while — `onEvent` fires
   * per frame so the UI can narrate the steps. Resolves with the terminal
   * `complete` result, or throws on an `error` frame.
   */
  async compileHierarchy(
    request: HierarchyCompileRequest,
    onEvent?: (event: CompileEvent) => void,
    signal?: AbortSignal,
  ): Promise<CompileResult> {
    const response = await fetch(`${this.baseUrl}/api/hierarchy/compile`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    });

    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => null);
      const record = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
      const detail = record.detail ?? record.error;
      throw new TempoApiError(
        typeof detail === 'string' ? detail : `HTTP ${response.status}`,
        response.status,
      );
    }
    if (!response.body) throw new TempoApiError('Compile returned no stream', 502);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let result: CompileResult | null = null;

    const handleFrame = (frame: string) => {
      // SSE frames are `data: {...}` lines separated by a blank line. Ignore
      // anything that is not a data payload (comments, keep-alives).
      const payloads = frame
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trim())
        .filter(Boolean);
      for (const raw of payloads) {
        let json: unknown;
        try {
          json = JSON.parse(raw);
        } catch {
          continue;
        }
        const parsed = compileEventSchema.safeParse(json);
        if (!parsed.success) continue;
        const event = parsed.data;
        onEvent?.(event);
        if (event.type === 'complete') result = event.data;
        if (event.type === 'error') throw new TempoApiError(event.error, 500);
      }
    };

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let split = buffer.indexOf('\n\n');
        while (split !== -1) {
          handleFrame(buffer.slice(0, split));
          buffer = buffer.slice(split + 2);
          split = buffer.indexOf('\n\n');
        }
      }
      if (buffer.trim()) handleFrame(buffer);
    } finally {
      reader.releaseLock();
    }

    if (!result) throw new TempoApiError('Compile ended without a result', 502);
    return result;
  }

  acceptCompile(token: string) {
    return this.request(`/api/hierarchy/compile/${token}/accept`, compileDispositionSchema, {
      method: 'POST',
    });
  }

  revertCompile(token: string) {
    return this.request(`/api/hierarchy/compile/${token}/revert`, compileDispositionSchema, {
      method: 'POST',
    });
  }

  getOnboarding(signal?: AbortSignal) {
    return this.request('/api/onboarding', onboardingSchema, { signal });
  }

  saveOnboarding(request: OnboardingSaveRequest) {
    return this.request('/api/onboarding', onboardingSaveSchema, {
      method: 'PUT',
      body: JSON.stringify(request),
    });
  }

  subscribe(listener: (event: ServerEvent) => void): () => void {
    this.listeners.add(listener);
    this.ensureSocket();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.closeSocket();
    };
  }

  private ensureSocket(): void {
    if (this.socket || typeof WebSocket === 'undefined') return;
    const url = new URL(this.baseUrl);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.pathname = '/ws';
    url.search = '';
    const socket = new WebSocket(url);
    this.socket = socket;
    socket.onopen = () => {
      this.listeners.forEach((current) => current({ type: 'connection', connected: true }));
    };
    socket.onmessage = (message) => {
      let payload: unknown;
      try {
        payload = JSON.parse(String(message.data));
      } catch {
        return;
      }
      const parsed = serverEventSchema.safeParse(payload);
      if (parsed.success) this.listeners.forEach((current) => current(parsed.data));
    };
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.listeners.forEach((current) => current({ type: 'connection', connected: false }));
      if (this.listeners.size > 0) {
        this.reconnectTimer = setTimeout(() => {
          this.reconnectTimer = null;
          this.ensureSocket();
        }, 3000);
      }
    };
  }

  private closeSocket(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    const socket = this.socket;
    this.socket = null;
    if (socket) socket.close();
  }
}

export const api = new TempoApiClient();
export default api;
