/**
 * MCP Agent Service — chat-based agent with tool call visualization
 */

import { Injectable, NgZone } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject, Observable, catchError, tap, throwError } from 'rxjs';
import { environment } from '../../environments/environment';
import {
  McpToolDefinition,
  McpToolCallRequest,
  McpToolCallResponse,
  McpAgentChatRequest,
  McpAgentChatResponse,
  McpAgentToolStep,
  McpChatMessage,
  McpChatSession,
  TokenUsage,
  CopilotModelInfo,
  CopilotConfigResponse,
  CopilotChatRequest,
  CopilotChatResponse,
  DeviceFlowResponse,
  DeviceFlowPollResponse,
} from '../models/mcp-agent.models';

/**
 * RFC 4122 v4 UUID — works on insecure contexts (plain http:// on a LAN IP).
 * The built-in randomUUID is only exposed in secure contexts (https or
 * localhost), so we fall back to `crypto.getRandomValues` + bit-twiddling
 * for the value/version/variant fields when it isn't available.
 *
 * Both code paths use the platform CSPRNG; we never use Math.random for
 * identifiers.
 */
function safeUUID(): string {
  const c: Crypto | undefined =
    typeof crypto !== 'undefined' ? crypto : undefined;
  if (c && typeof (c as any).randomUUID === 'function') {
    return (c as any).randomUUID();
  }
  if (c && typeof c.getRandomValues === 'function') {
    const bytes = new Uint8Array(16);
    c.getRandomValues(bytes);
    // Per RFC 4122 §4.4: set version (4) and variant (10).
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex: string[] = [];
    for (let i = 0; i < 16; i++) {
      hex.push((bytes[i] + 0x100).toString(16).slice(1));
    }
    return (
      hex.slice(0, 4).join('') +
      '-' +
      hex.slice(4, 6).join('') +
      '-' +
      hex.slice(6, 8).join('') +
      '-' +
      hex.slice(8, 10).join('') +
      '-' +
      hex.slice(10, 16).join('')
    );
  }
  // Should never happen in any browser shipped this decade.
  throw new Error('No Web Crypto API available — cannot generate UUID');
}

@Injectable({
  providedIn: 'root',
})
export class McpAgentService {
  private apiUrl = environment.apiUrl;

  /** Available tools (cached for reference panel) */
  private _tools$ = new BehaviorSubject<McpToolDefinition[]>([]);
  tools$ = this._tools$.asObservable();

  /** Chat messages */
  private _messages$ = new BehaviorSubject<McpChatMessage[]>([]);
  messages$ = this._messages$.asObservable();

  /** Loading state */
  private _loading$ = new BehaviorSubject<boolean>(false);
  loading$ = this._loading$.asObservable();

  /** Agent-side chat session id (for multi-turn context) */
  private _chatSessionId: string | null = null;

  /** Copilot session id */
  private _copilotSessionId: string | null = null;

  /** Copilot models */
  private _copilotModels$ = new BehaviorSubject<CopilotModelInfo[]>([]);
  copilotModels$ = this._copilotModels$.asObservable();

  /** Copilot config */
  private _copilotConfig$ = new BehaviorSubject<CopilotConfigResponse | null>(null);
  copilotConfig$ = this._copilotConfig$.asObservable();

  /** Chat sessions (history) */
  private _sessions$ = new BehaviorSubject<McpChatSession[]>([]);
  sessions$ = this._sessions$.asObservable();

  /** Active session ID */
  private _activeSessionId: string | null = null;

  /** Cumulative token usage across all sessions */
  private _totalTokenUsage$ = new BehaviorSubject<{ totalTokens: number; totalCost: number; queryCount: number }>({
    totalTokens: 0, totalCost: 0, queryCount: 0
  });
  totalTokenUsage$ = this._totalTokenUsage$.asObservable();

  private static readonly SESSIONS_KEY = 'mcp_agent_sessions';
  private static readonly MAX_SESSIONS = 50;

  constructor(private http: HttpClient, private ngZone: NgZone) {
    this._loadSessionsFromStorage();
  }

  // ── Session History Management ──────────────────────────────────

  private _loadSessionsFromStorage(): void {
    try {
      const raw = localStorage.getItem(McpAgentService.SESSIONS_KEY);
      if (raw) {
        let sessions: McpChatSession[] = JSON.parse(raw).map((s: any) => ({
          ...s,
          createdAt: new Date(s.createdAt),
          lastActiveAt: new Date(s.lastActiveAt),
          messages: (s.messages || []).map((m: any) => ({ ...m, timestamp: new Date(m.timestamp) })),
        }));
        // Remove stale empty sessions (keep at most one empty one at the top)
        let foundEmpty = false;
        sessions = sessions.filter(s => {
          if (s.messageCount === 0 && s.messages.length === 0) {
            if (foundEmpty) return false;
            foundEmpty = true;
          }
          return true;
        });
        this._sessions$.next(sessions);
        this._updateTotalTokenUsage(sessions);
        // Auto-load the most recent session so chat is restored on page load
        if (sessions.length > 0) {
          this._activeSessionId = sessions[0].id;
          this._messages$.next([...sessions[0].messages]);
          this._copilotSessionId = sessions[0].copilotSessionId || null;
        }
      }
    } catch {
      // ignore corrupt data
    }
  }

  private _saveSessionsToStorage(): void {
    try {
      const sessions = this._sessions$.value;
      localStorage.setItem(McpAgentService.SESSIONS_KEY, JSON.stringify(sessions));
    } catch {
      // ignore storage errors
    }
  }

  private _updateTotalTokenUsage(sessions: McpChatSession[]): void {
    let totalTokens = 0, totalCost = 0, queryCount = 0;
    for (const s of sessions) {
      totalTokens += s.totalTokens || 0;
      totalCost += s.totalCost || 0;
      queryCount += s.queryCount || 0;
    }
    this._totalTokenUsage$.next({ totalTokens, totalCost, queryCount });
  }

  createSession(mode: 'copilot', model: string): McpChatSession {
    const session: McpChatSession = {
      id: safeUUID(),
      title: 'New Chat',
      mode,
      model,
      createdAt: new Date(),
      lastActiveAt: new Date(),
      messageCount: 0,
      messages: [],
      totalTokens: 0,
      totalCost: 0,
      totalTimeMs: 0,
      queryCount: 0,
    };
    const sessions = [session, ...this._sessions$.value].slice(0, McpAgentService.MAX_SESSIONS);
    this._sessions$.next(sessions);
    this._activeSessionId = session.id;
    this._messages$.next([]);
    this._chatSessionId = null;
    this._copilotSessionId = null;
    this._saveSessionsToStorage();
    return session;
  }

  loadSession(sessionId: string): void {
    const session = this._sessions$.value.find(s => s.id === sessionId);
    if (session) {
      this._activeSessionId = sessionId;
      this._messages$.next([...session.messages]);
      // Restore backend session IDs from saved session
      this._chatSessionId = null;
      this._copilotSessionId = session.copilotSessionId || null;
    }
  }

  getActiveSession(): McpChatSession | null {
    if (!this._activeSessionId) return null;
    return this._sessions$.value.find(s => s.id === this._activeSessionId) || null;
  }

  getActiveSessionId(): string | null {
    return this._activeSessionId;
  }

  deleteSession(sessionId: string): void {
    const sessions = this._sessions$.value.filter(s => s.id !== sessionId);
    this._sessions$.next(sessions);
    if (this._activeSessionId === sessionId) {
      this._activeSessionId = null;
      this._messages$.next([]);
    }
    this._updateTotalTokenUsage(sessions);
    this._saveSessionsToStorage();
  }

  renameSession(sessionId: string, newTitle: string): void {
    const sessions = this._sessions$.value.map(s =>
      s.id === sessionId ? { ...s, title: newTitle } : s
    );
    this._sessions$.next(sessions);
    this._saveSessionsToStorage();
  }

  clearAllSessions(): void {
    this._sessions$.next([]);
    this._activeSessionId = null;
    this._messages$.next([]);
    this._chatSessionId = null;
    this._copilotSessionId = null;
    this._totalTokenUsage$.next({ totalTokens: 0, totalCost: 0, queryCount: 0 });
    this._saveSessionsToStorage();
  }

  private _syncMessageToSession(msg?: McpChatMessage): void {
    if (!this._activeSessionId) return;
    const messages = this._messages$.value;
    const sessions = this._sessions$.value.map(s => {
      if (s.id !== this._activeSessionId) return s;

      // Auto-generate title from first user message
      let title = s.title;
      if (title === 'New Chat' && messages.length > 0) {
        const firstUser = messages.find(m => m.role === 'user');
        if (firstUser) {
          title = firstUser.content.substring(0, 60) + (firstUser.content.length > 60 ? '...' : '');
        }
      }

      // Accumulate token usage from the new message
      let totalTokens = s.totalTokens;
      let totalCost = s.totalCost;
      let totalTimeMs = s.totalTimeMs;
      let queryCount = s.queryCount;
      if (msg?.tokenUsage) {
        totalTokens += msg.tokenUsage.total_tokens;
        totalCost += msg.tokenUsage.estimated_cost;
      }
      if (msg?.totalTimeMs) {
        totalTimeMs += msg.totalTimeMs;
      }
      if (msg?.sql) {
        queryCount += 1;
      }

      return {
        ...s,
        title,
        lastActiveAt: new Date(),
        messageCount: messages.length,
        messages: [...messages],
        totalTokens,
        totalCost,
        totalTimeMs,
        queryCount,
        copilotSessionId: this._copilotSessionId || s.copilotSessionId,
      };
    });
    this._sessions$.next(sessions);
    this._updateTotalTokenUsage(sessions);
    this._saveSessionsToStorage();
  }

  // ── Tool Discovery ──────────────────────────────────────────────

  loadTools(): Observable<McpToolDefinition[]> {
    return this.http.get<McpToolDefinition[]>(`${this.apiUrl}/api/mcp/tools`).pipe(
      tap((tools) => this._tools$.next(tools)),
      catchError(this._handleError)
    );
  }

  // ── Agent Chat (main flow) ──────────────────────────────────────

  sendMessage(message: string, dbSessionId: string): Observable<McpAgentChatResponse> {
    // Add user message immediately
    const userMsg: McpChatMessage = {
      id: safeUUID(),
      role: 'user',
      content: message,
      timestamp: new Date(),
    };
    this._addMessage(userMsg);
    this._loading$.next(true);

    const req: McpAgentChatRequest = {
      message,
      db_session_id: dbSessionId,
      chat_session_id: this._chatSessionId || undefined,
    };

    return this.http
      .post<McpAgentChatResponse>(`${this.apiUrl}/api/mcp/agent`, req)
      .pipe(
        tap((res) => {
          // Track session for multi-turn
          if (res.chat_session_id) {
            this._chatSessionId = res.chat_session_id;
          }

          // Build assistant message
          const assistantMsg: McpChatMessage = {
            id: safeUUID(),
            role: 'assistant',
            content: res.response_message || (res.error ? `Error: ${res.error}` : ''),
            timestamp: new Date(),
            toolSteps: res.tool_steps,
            sql: res.sql,
            records: res.records,
            rowCount: res.row_count,
            columns: res.columns,
            totalTimeMs: res.total_time_ms,
            confidence: res.confidence,
            error: res.error,
            _toolsExpanded: false, // Collapsed by default
            _sqlExpanded: !!res.sql,
            _resultsExpanded: (res.records?.length ?? 0) > 0,
          };
          this._addMessage(assistantMsg);
          this._syncMessageToSession(assistantMsg);
          this._loading$.next(false);
        }),
        catchError((err) => {
          const errorMsg: McpChatMessage = {
            id: safeUUID(),
            role: 'assistant',
            content: '',
            timestamp: new Date(),
            error: err?.error?.detail || err?.message || 'Request failed',
          };
          this._addMessage(errorMsg);
          this._loading$.next(false);
          return throwError(() => err);
        })
      );
  }

  // ── Direct tool call (for manual invocation from reference panel) ──

  callToolDirect(toolName: string, args: Record<string, any>): Observable<McpToolCallResponse> {
    return this.http
      .post<McpToolCallResponse>(`${this.apiUrl}/api/mcp/call`, {
        tool_name: toolName,
        arguments: args,
      } as McpToolCallRequest)
      .pipe(catchError(this._handleError));
  }

  // ── Session Management ──────────────────────────────────────────

  clearChat(): void {
    this._messages$.next([]);
    this._chatSessionId = null;
    this._copilotSessionId = null;
    this._activeSessionId = null;
  }

  get messages(): McpChatMessage[] {
    return this._messages$.value;
  }

  // ── OpenAI Codex models and OAuth configuration ─────────────────

  loadCopilotConfig(): Observable<CopilotConfigResponse> {
    return this.http
      .get<CopilotConfigResponse>(`${this.apiUrl}/api/copilot/config`)
      .pipe(
        tap((cfg) => {
          this._copilotConfig$.next(cfg);
        }),
        catchError(this._handleError)
      );
  }

  loadCopilotModels(): Observable<CopilotModelInfo[]> {
    return this.http
      .get<CopilotModelInfo[]>(`${this.apiUrl}/api/copilot/models`)
      .pipe(
        tap((models) => this._copilotModels$.next(models)),
        catchError(this._handleError)
      );
  }

  sendCopilotMessage(
    message: string,
    dbSessionId?: string,
    model?: string
  ): Observable<CopilotChatResponse> {
    const userMsg: McpChatMessage = {
      id: safeUUID(),
      role: 'user',
      content: message,
      timestamp: new Date(),
    };
    this._addMessage(userMsg);
    this._loading$.next(true);

    const req: CopilotChatRequest = {
      message,
      db_session_id: dbSessionId || '',
      session_id: this._copilotSessionId || undefined,
      model,
    };

    return this.http
      .post<CopilotChatResponse>(`${this.apiUrl}/api/copilot/chat`, req)
      .pipe(
        tap((res) => {
          if (res.session_id) {
            this._copilotSessionId = res.session_id;
          }

          const tokenUsage: TokenUsage | undefined = res.usage?.['total_tokens'] ? {
            prompt_tokens: res.usage['prompt_tokens'] || 0,
            completion_tokens: res.usage['completion_tokens'] || 0,
            total_tokens: res.usage['total_tokens'] || 0,
            model: res.model || '',
            estimated_cost: res.usage['estimated_cost'] || this._estimateCost(res.usage['total_tokens'] || 0, res.model, res.usage['prompt_tokens'], res.usage['completion_tokens']),
          } : undefined;

          const assistantMsg: McpChatMessage = {
            id: safeUUID(),
            role: 'assistant',
            content: res.message || (res.error ? `Error: ${res.error}` : ''),
            timestamp: new Date(),
            toolSteps: res.tool_steps,
            sql: res.sql,
            records: res.records,
            rowCount: res.row_count,
            columns: res.columns,
            totalTimeMs: res.total_time_ms,
            error: res.error,
            activeDatabase: res.active_database || '',
            tokenUsage,
            trustScore: res.trust_score,
            trustLabel: res.trust_label,
            trustChecks: res.trust_checks,
            verification: res.verification,
            groundedSources: res.grounded_sources,
            _toolsExpanded: false, // Collapsed by default
            _sqlExpanded: !!res.sql,
            _resultsExpanded: (res.records?.length ?? 0) > 0,
          };
          this._addMessage(assistantMsg);
          this._syncMessageToSession(assistantMsg);
          this._loading$.next(false);
        }),
        catchError((err) => {
          const errorMsg: McpChatMessage = {
            id: safeUUID(),
            role: 'assistant',
            content: '',
            timestamp: new Date(),
            error: err?.error?.detail || err?.message || 'Request failed',
          };
          this._addMessage(errorMsg);
          this._loading$.next(false);
          return throwError(() => err);
        })
      );
  }

  /**
   * Streaming version of sendCopilotMessage.
   * Uses SSE to progressively show tool steps as the agent works.
   * Returns the assistant message ID so the component can track it.
   */
  sendCopilotMessageStream(
    message: string,
    dbSessionId?: string,
    model?: string
  ): string {
    const userMsg: McpChatMessage = {
      id: safeUUID(),
      role: 'user',
      content: message,
      timestamp: new Date(),
    };
    this._addMessage(userMsg);
    this._loading$.next(true);

    // Create a placeholder assistant message that will be progressively updated
    const assistantMsgId = safeUUID();
    const assistantMsg: McpChatMessage = {
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      toolSteps: [],
      _toolsExpanded: true,
      _sqlExpanded: false,
      _resultsExpanded: false,
    };
    this._addMessage(assistantMsg);

    const req: CopilotChatRequest = {
      message,
      db_session_id: dbSessionId || '',
      session_id: this._copilotSessionId || undefined,
      model,
    };

    // Use fetch for SSE streaming (Angular HttpClient doesn't support SSE well)
    const fetchHeaders: Record<string, string> = { 'Content-Type': 'application/json' };
    const apiKey = (window as any).__env?.apiKey || environment?.apiKey;
    if (apiKey) {
      fetchHeaders['Authorization'] = `Bearer ${apiKey}`;
    }
    fetch(`${this.apiUrl}/api/copilot/chat/stream`, {
      method: 'POST',
      headers: fetchHeaders,
      body: JSON.stringify(req),
    })
      .then(async (response) => {
        if (!response.ok || !response.body) {
          this._runInAngularZone(() => {
            this._updateMessage(assistantMsgId, {
              error: `HTTP ${response.status}: ${response.statusText}`,
              content: `Error: HTTP ${response.status}`,
            });
            this._loading$.next(false);
          });
          return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let eventType = '';
        const eventDataLines: string[] = [];

        const flushEvent = () => {
          if (!eventType || eventDataLines.length === 0) return;
          const eventData = eventDataLines.join('\n');
          try {
            const data = JSON.parse(eventData);
            this._runInAngularZone(() => {
              this._handleSSEEvent(assistantMsgId, eventType, data);
            });
          } catch (e) {
            console.error('SSE parse error:', e, { eventType, eventData });
          }
          eventType = '';
          eventDataLines.length = 0;
        };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || ''; // Keep incomplete line in buffer

          for (const line of lines) {
            const normalized = line.endsWith('\r') ? line.slice(0, -1) : line;
            if (normalized.startsWith('event:')) {
              flushEvent();
              eventType = normalized.slice(6).trim();
            } else if (normalized.startsWith('data:')) {
              eventDataLines.push(normalized.slice(5).trimStart());
            } else if (normalized === '') {
              // Complete event — process it
              flushEvent();
            }
          }
        }

        buffer += decoder.decode();
        if (buffer) {
          const normalized = buffer.endsWith('\r') ? buffer.slice(0, -1) : buffer;
          if (normalized.startsWith('event:')) {
            flushEvent();
            eventType = normalized.slice(6).trim();
          } else if (normalized.startsWith('data:')) {
            eventDataLines.push(normalized.slice(5).trimStart());
          }
        }
        flushEvent();
        this._runInAngularZone(() => this._loading$.next(false));
      })
      .catch((err) => {
        const streamError = this._formatStreamConnectionError(err);
        this._runInAngularZone(() => {
          this._updateMessage(assistantMsgId, {
            error: streamError,
            content: `Error: ${streamError}`,
          });
          this._loading$.next(false);
        });
      });

    return assistantMsgId;
  }

  /** Handle a single SSE event and update the assistant message */
  private _handleSSEEvent(msgId: string, event: string, data: any): void {
    const messages = this._messages$.getValue();
    const msgIdx = messages.findIndex((m) => m.id === msgId);
    if (msgIdx === -1) return;
    const msg = messages[msgIdx];

    switch (event) {
      case 'thinking': {
        // Model shared its reasoning — store for the next tool_start
        // AND show it immediately in the UI as a live thinking indicator
        const updated = [...messages];
        updated[msgIdx] = {
          ...msg,
          _thinkingText: data.text,
          _pendingReasoning: data.text,
        } as any;
        this._messages$.next(updated);
        break;
      }

      case 'tool_start': {
        const steps = msg.toolSteps ? [...msg.toolSteps] : [];
        const step: McpAgentToolStep = {
          tool_name: data.tool_name,
          arguments: data.arguments || {},
          result: null,
          success: true, // Optimistic; updated on tool_result
          error: null,
          execution_time_ms: 0,
          reasoning: (msg as any)._pendingReasoning || null,
          database: data.database || null,
        };
        (msg as any)._pendingReasoning = null;
        steps.push(step);
        // Create new message reference for OnPush change detection
        // Auto-expand tool steps during streaming so user sees progress
        const updated = [...messages];
        updated[msgIdx] = { ...msg, toolSteps: steps, _toolsExpanded: true };
        this._messages$.next(updated);
        break;
      }

      case 'tool_result': {
        if (!msg.toolSteps) break;
        const idx = data.index;
        if (idx < msg.toolSteps.length) {
          const updatedSteps = [...msg.toolSteps];
          updatedSteps[idx] = {
            ...updatedSteps[idx],
            success: data.success,
            error: data.error,
            result: data.result !== undefined ? data.result : updatedSteps[idx].result,
            execution_time_ms: data.execution_time_ms,
            database: data.database || updatedSteps[idx].database,
          };
          const updated = [...messages];
          updated[msgIdx] = { ...msg, toolSteps: updatedSteps };
          this._messages$.next(updated);
        }
        break;
      }

      case 'done': {
        this._loading$.next(false);
        if (data.session_id) {
          this._copilotSessionId = data.session_id;
        }
        const usage = data.usage;
        const tokenUsage: TokenUsage | undefined = usage?.total_tokens ? {
          prompt_tokens: usage.prompt_tokens || 0,
          completion_tokens: usage.completion_tokens || 0,
          total_tokens: usage.total_tokens || 0,
          model: data.model || '',
          estimated_cost: usage.estimated_cost || this._estimateCost(usage.total_tokens || 0, data.model, usage.prompt_tokens, usage.completion_tokens),
        } : undefined;

        const finalMsg: McpChatMessage = {
          ...msg,
          content: data.message || '',
          sql: data.sql,
          records: data.records,
          rowCount: data.row_count,
          columns: data.columns,
          totalTimeMs: data.total_time_ms,
          activeDatabase: data.active_database || '',
          tokenUsage,
          trustScore: data.trust_score,
          trustLabel: data.trust_label,
          trustChecks: data.trust_checks,
          verification: data.verification,
          groundedSources: data.grounded_sources,
          _toolsExpanded: false, // Collapse tool steps once response is complete
          _sqlExpanded: !!data.sql,
          _resultsExpanded: (data.records?.length ?? 0) > 0,
          toolSteps: data.tool_steps?.length ? data.tool_steps : msg.toolSteps,
        };
        const updated = [...messages];
        updated[msgIdx] = finalMsg;
        this._messages$.next(updated);
        this._syncMessageToSession(finalMsg);

        // Log to backend for query logs & dashboard
        const userMsg = messages.slice(0, msgIdx).reverse().find(m => m.role === 'user');
        this._logCopilotQueryToBackend(finalMsg, userMsg?.content || '');
        break;
      }

      case 'error': {
        const updated = [...messages];
        updated[msgIdx] = {
          ...msg,
          error: data.error,
          content: msg.content || `Error: ${data.error}`,
        };
        this._messages$.next(updated);
        break;
      }
    }
  }

  private _runInAngularZone<T>(fn: () => T): T {
    return NgZone.isInAngularZone() ? fn() : this.ngZone.run(fn);
  }

  private _formatStreamConnectionError(err: any): string {
    const raw = String(err?.message || err || '').trim();
    if (!raw || /failed to fetch|network|load failed|connection/i.test(raw)) {
      return `Backend connection failed. Make sure FastAPI is running at ${this.apiUrl}, then retry.`;
    }
    return raw;
  }

  /** Update specific fields on an existing message */
  private _updateMessage(msgId: string, updates: Partial<McpChatMessage>): void {
    const messages = this._messages$.getValue();
    const idx = messages.findIndex((m) => m.id === msgId);
    if (idx === -1) return;
    const updated = [...messages];
    updated[idx] = { ...messages[idx], ...updates };
    this._messages$.next(updated);
  }

  // ── OpenAI Codex OAuth device flow ───────────────────────────────

  startDeviceFlow(): Observable<DeviceFlowResponse> {
    return this.http
      .post<DeviceFlowResponse>(`${this.apiUrl}/api/copilot/auth/device-code`, {})
      .pipe(catchError(this._handleError));
  }

  pollDeviceFlow(): Observable<DeviceFlowPollResponse> {
    return this.http
      .post<DeviceFlowPollResponse>(`${this.apiUrl}/api/copilot/auth/poll`, {})
      .pipe(
        tap((res: any) => {
          if (res.status === 'complete') {
            this._copilotConfig$.next(res as CopilotConfigResponse);
          }
        }),
        catchError(this._handleError),
      );
  }

  disconnectOpenAI(): Observable<CopilotConfigResponse> {
    return this.http
      .post<CopilotConfigResponse>(`${this.apiUrl}/api/copilot/auth/disconnect`, {})
      .pipe(tap((cfg) => this._copilotConfig$.next(cfg)), catchError(this._handleError));
  }

  // ── Cross-Database ──────────────────────────────────────────────

  registerConnections(connections: any[]): Observable<any> {
    return this.http
      .post(`${this.apiUrl}/api/copilot/connections`, { connections })
      .pipe(catchError(this._handleError));
  }

  // ── Backend Query Logging ───────────────────────────────────────

  /** Log a copilot query to the backend after SSE streaming completes */
  private _logCopilotQueryToBackend(msg: McpChatMessage, userQuery: string): void {
    // Extract tables_used from tool step results
    const tablesUsed = new Set<string>();
    for (const ts of (msg.toolSteps || [])) {
      if (ts.result && typeof ts.result === 'object') {
        // search_tables or generate_sql may return tables
        if (Array.isArray(ts.result?.tables)) {
          ts.result.tables.forEach((t: any) => tablesUsed.add(typeof t === 'string' ? t : t.name || ''));
        }
        if (Array.isArray(ts.result?.tables_used)) {
          ts.result.tables_used.forEach((t: string) => tablesUsed.add(t));
        }
      }
    }

    const payload = {
      session_id: this._copilotSessionId || '',
      user_query: userQuery,
      generated_sql: msg.sql || '',
      total_time_ms: msg.totalTimeMs || 0,
      success: !msg.error,
      row_count: msg.rowCount || 0,
      model: msg.tokenUsage?.model || '',
      tables_used: Array.from(tablesUsed).filter(Boolean),
      db_identity: msg.activeDatabase || '',
      error_message: msg.error || null,
      tool_steps: (msg.toolSteps || []).map(ts => ({
        tool_name: ts.tool_name,
        execution_time_ms: ts.execution_time_ms || 0,
        success: ts.success,
        reasoning: ts.reasoning || null,
      })),
      usage: {
        prompt_tokens: msg.tokenUsage?.prompt_tokens || 0,
        completion_tokens: msg.tokenUsage?.completion_tokens || 0,
        total_tokens: msg.tokenUsage?.total_tokens || 0,
        estimated_cost: msg.tokenUsage?.estimated_cost || 0,
      },
    };

    this.http.post(`${this.apiUrl}/api/copilot/log-query`, payload)
      .subscribe({
        error: (err) => console.warn('Failed to log copilot query:', err),
      });
  }

  // ── Helpers ─────────────────────────────────────────────────────

  private _addMessage(msg: McpChatMessage): void {
    this._messages$.next([...this._messages$.value, msg]);
    // Auto-sync user messages too (for session title generation)
    if (msg.role === 'user') {
      this._syncMessageToSession();
    }
  }

  /** Rough cost estimation per 1K tokens by model family */
  private _estimateCost(totalTokens: number, model?: string, promptTokens?: number, completionTokens?: number): number {
    // Industry-standard pricing per 1M tokens: [input, output]
    // Order matters: more-specific keys before shorter prefixes
    const pricing: Record<string, [number, number]> = {
      // OpenAI
      'gpt-5.5':              [5.00,  30.00],
      'gpt-5.4-mini':         [0.75,  4.50],
      'gpt-5.4-nano':         [0.20,  1.25],
      'gpt-5.4':              [2.50,  15.00],
      'gpt-5-nano':           [0.20,  1.25],
      'gpt-4.1-mini':         [0.40,  1.60],
      'gpt-4.1-nano':         [0.10,  0.40],
      'gpt-4.1':              [2.00,  8.00],
      'gpt-4o-mini':          [0.15,  0.60],
      'gpt-4o':               [2.50,  10.00],
      'gpt-4-turbo':          [10.00, 30.00],
      'gpt-4':                [30.00, 60.00],
      'gpt-3.5-turbo':        [0.50,  1.50],
      'o4-mini':              [1.10,  4.40],
      'o3-mini':              [1.10,  4.40],
      'o1-mini':              [3.00,  12.00],
      'o1':                   [15.00, 60.00],
    };
    const m = (model || '').toLowerCase();
    let rates: [number, number] = [0.15, 0.60]; // default: gpt-4o-mini
    for (const [key, val] of Object.entries(pricing)) {
      if (m.includes(key)) { rates = val; break; }
    }
    // Use separate input/output tokens if available, otherwise split 3:1 (typical ratio)
    const input = promptTokens ?? Math.round(totalTokens * 0.75);
    const output = completionTokens ?? (totalTokens - input);
    return (input / 1_000_000) * rates[0] + (output / 1_000_000) * rates[1];
  }

  private _handleError(err: any) {
    const message = err?.error?.detail || err?.message || 'Unknown error';
    return throwError(() => new Error(message));
  }
}

