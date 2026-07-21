/**
 * MCP Agent Component — Copilot-style chat with MCP tool visualization
 *
 * Chat interface where the user types natural language questions,
 * the LLM agent orchestrates MCP tool calls, and the full tool-call
 * trace is shown inline.
 */

import {
  Component,
  OnInit,
  OnDestroy,
  ViewChild,
  ElementRef,
  Input,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subject, takeUntil } from 'rxjs';

// Material
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatBadgeModule } from '@angular/material/badge';
import { MatMenuModule } from '@angular/material/menu';
import { MatDividerModule } from '@angular/material/divider';

import { McpAgentService } from '../../services/mcp-agent.service';
import {
  McpToolDefinition,
  McpChatMessage,
  McpChatSession,
  McpAgentToolStep,
  CopilotModelInfo,
} from '../../models/mcp-agent.models';

@Component({
  selector: 'app-mcp-agent',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatIconModule,
    MatButtonModule,
    MatTooltipModule,
    MatProgressSpinnerModule,
    MatSnackBarModule,
    MatBadgeModule,
    MatMenuModule,
    MatDividerModule,
  ],
  templateUrl: './mcp-agent.component.html',
  styleUrls: ['./mcp-agent.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class McpAgentComponent implements OnInit, OnDestroy {
  @ViewChild('chatContainer') chatContainer?: ElementRef;
  @ViewChild('messageInput') messageInput?: ElementRef;

  /** DB session ID from the main app (passed via input binding) */
  @Input() sessionId = '';

  // Chat
  messages: McpChatMessage[] = [];
  inputText = '';
  isLoading = false;

  // Tool reference panel
  tools: McpToolDefinition[] = [];
  showToolPanel = false;
  toolSearch = '';

  // Per-step expand state (keyed by msg.id + step index)
  expandedSteps = new Set<string>();

  // Input focus state
  inputFocused = false;

  // ── History Sidebar ─────────────────────────────────────────────
  showHistory = true;
  sessions: McpChatSession[] = [];
  editingSessionId: string | null = null;
  editingSessionName = '';
  totalTokens = 0;
  totalCost = 0;

  // ── Copilot Mode ────────────────────────────────────────────────
  /** OpenAI Codex chat is the only mode. */
  activeMode: 'copilot' = 'copilot';

  /** Copilot models */
  copilotModels: CopilotModelInfo[] = [];
  selectedModel = 'gpt-5.6-sol';
  copilotConfigured = false;
  hasOpenAIToken = false;
  codexEmail = '';
  codexDisplayName = '';

  /** Grouped models by vendor (collapsed sections) */
  groupedModels: { vendor: string; models: CopilotModelInfo[]; expanded: boolean }[] = [];

  /** Config dialog state */
  showConfigDialog = false;

  /** OAuth Device Flow state */
  deviceFlowActive = false;
  deviceFlowCode = '';
  deviceFlowUri = '';
  deviceFlowPolling = false;
  deviceFlowStatus = '';
  private _deviceFlowPollTimer: any = null;

  private destroy$ = new Subject<void>();

  constructor(
    private mcpAgent: McpAgentService,
    private snackBar: MatSnackBar,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    this.mcpAgent.loadTools().subscribe({
      next: (tools) => {
        this.tools = tools;
        this.cdr.markForCheck();
      },
    });

    this.mcpAgent.messages$.pipe(takeUntil(this.destroy$)).subscribe((msgs) => {
      this.messages = msgs;
      this.cdr.markForCheck();
      setTimeout(() => this._scrollToBottom(), 50);
    });

    this.mcpAgent.loading$.pipe(takeUntil(this.destroy$)).subscribe((loading) => {
      this.isLoading = loading;
      this.cdr.markForCheck();
    });

    // Session history
    this.mcpAgent.sessions$.pipe(takeUntil(this.destroy$)).subscribe((sessions) => {
      this.sessions = sessions;
      this.cdr.markForCheck();
    });

    this.mcpAgent.totalTokenUsage$.pipe(takeUntil(this.destroy$)).subscribe((usage) => {
      this.totalTokens = usage.totalTokens;
      this.totalCost = usage.totalCost;
      this.cdr.markForCheck();
    });

    // Load OpenAI Codex config + models
    this.mcpAgent.loadCopilotConfig().subscribe({
      next: (cfg) => {
        this.copilotConfigured = cfg.has_codex_token;
        this.hasOpenAIToken = cfg.has_codex_token;
        this.codexEmail = cfg.codex_email || '';
        this.codexDisplayName = cfg.codex_display_name || '';
        if (cfg.default_model) {
          this.selectedModel = cfg.default_model;
        }
        this.reloadModels();
        this.cdr.markForCheck();
      },
    });

    this.mcpAgent.loadCopilotModels().subscribe({
      next: (models) => {
        this.copilotModels = models;
        // Use the first chat-capable model if the saved selection is unavailable.
        if (!models.some(m => m.id === this.selectedModel && m.supported_in_api !== false)) {
          this.selectedModel = models.find(m => m.supported_in_api !== false)?.id || this.selectedModel;
        }
        this._buildGroupedModels();
        this.cdr.markForCheck();
      },
    });


    // Register saved connections for cross-database support
    this._registerSavedConnections();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
    this._stopDeviceFlowPolling();
  }

  /** Send saved connections to the backend so the agent can switch databases. */
  private _registerSavedConnections(): void {
    try {
      const raw = localStorage.getItem('savedConnections');
      if (!raw) return;
      const connections = JSON.parse(raw) as any[];
      if (!connections?.length) return;
      this.mcpAgent.registerConnections(connections).subscribe({
        next: () => console.log(`Registered ${connections.length} connections for cross-DB`),
        error: (err: any) => console.warn('Could not register connections:', err),
      });
    } catch {
      // ignore parse errors
    }
  }

  // ── Chat ────────────────────────────────────────────────────────

  sendMessage(): void {
    const text = this.inputText.trim();
    if (!text || this.isLoading) return;

    this.inputText = '';

    // Auto-create a session if none is active
    if (!this.mcpAgent.getActiveSessionId()) {
      this.mcpAgent.createSession(this.activeMode, this.selectedModel);
    }

    if (!this.copilotConfigured) {
      this.snackBar.open('Sign in with OpenAI Codex first (click the settings icon)', 'OK', { duration: 4000 });
      return;
    }
    // Copilot works independently — it can auto-connect via switch_database.
    // Use streaming for real-time tool progress.
    this.mcpAgent.sendCopilotMessageStream(
      text,
      this.sessionId || undefined,
      this.selectedModel
    );
  }

  onInputKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.sendMessage();
    }
  }

  autoResizeInput(): void {
    const el = this.messageInput?.nativeElement;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }

  private _buildGroupedModels(): void {
    const map = new Map<string, CopilotModelInfo[]>();
    // Preferred vendor order
    const vendorOrder = ['OpenAI'];
    for (const m of this.copilotModels) {
      const vendor = m.vendor || 'Other';
      if (!map.has(vendor)) map.set(vendor, []);
      map.get(vendor)!.push(m);
    }
    // Sort vendors: preferred order first, then alphabetical
    const sortedVendors = [...map.keys()].sort((a, b) => {
      const ai = vendorOrder.indexOf(a);
      const bi = vendorOrder.indexOf(b);
      if (ai !== -1 && bi !== -1) return ai - bi;
      if (ai !== -1) return -1;
      if (bi !== -1) return 1;
      return a.localeCompare(b);
    });
    this.groupedModels = sortedVendors.map(vendor => ({
      vendor,
      models: map.get(vendor)!,
      expanded: map.get(vendor)!.some(m => m.id === this.selectedModel),
    }));
  }

  toggleVendorGroup(group: { vendor: string; models: CopilotModelInfo[]; expanded: boolean }): void {
    group.expanded = !group.expanded;
  }

  getSelectedModelInGroup(group: { vendor: string; models: CopilotModelInfo[]; expanded: boolean }): string | null {
    const match = group.models.find(m => m.id === this.selectedModel);
    return match ? match.name : null;
  }

  clearChat(): void {
    this.mcpAgent.clearChat();
    this.expandedSteps.clear();
  }

  sendQuickAction(text: string): void {
    this.inputText = text;
    this.sendMessage();
  }

  // ── History Sidebar ─────────────────────────────────────────────

  toggleHistory(): void {
    this.showHistory = !this.showHistory;
    this.cdr.markForCheck();
  }

  startNewChat(): void {
    // If current session is already empty, just reuse it
    const current = this.mcpAgent.getActiveSession();
    if (current && current.messageCount === 0) {
      this.expandedSteps.clear();
      this.cdr.markForCheck();
      return;
    }
    this.mcpAgent.createSession(this.activeMode, this.selectedModel);
    this.expandedSteps.clear();
    this.cdr.markForCheck();
  }

  selectSession(sessionId: string): void {
    this.mcpAgent.loadSession(sessionId);
    this.expandedSteps.clear();
    this.cdr.markForCheck();
  }

  isActiveSession(sessionId: string): boolean {
    return this.mcpAgent.getActiveSessionId() === sessionId;
  }

  startEditingSession(sessionId: string, currentTitle: string): void {
    this.editingSessionId = sessionId;
    this.editingSessionName = currentTitle;
    this.cdr.markForCheck();
  }

  saveSessionName(sessionId: string): void {
    if (this.editingSessionName.trim()) {
      this.mcpAgent.renameSession(sessionId, this.editingSessionName.trim());
    }
    this.editingSessionId = null;
    this.cdr.markForCheck();
  }

  cancelEditingSession(): void {
    this.editingSessionId = null;
    this.cdr.markForCheck();
  }

  onDeleteSession(sessionId: string): void {
    this.mcpAgent.deleteSession(sessionId);
    this.cdr.markForCheck();
  }

  clearAllHistory(): void {
    this.mcpAgent.clearAllSessions();
    this.cdr.markForCheck();
  }

  formatSessionDate(date: Date): string {
    if (!date) return '';
    const d = new Date(date);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays < 7) return `${diffDays}d ago`;
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }

  formatTokenCount(tokens: number): string {
    if (!tokens) return '0';
    if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
    if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
    return tokens.toString();
  }

  formatTime(ms: number): string {
    if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
    return `${Math.round(ms)}ms`;
  }

  formatCostDisplay(cost: number): string {
    if (!cost) return '$0.00';
    if (cost < 0.01) return `$${cost.toFixed(4)}`;
    return `$${cost.toFixed(2)}`;
  }

  // ── Tool Panel ──────────────────────────────────────────────────

  toggleToolPanel(): void {
    this.showToolPanel = !this.showToolPanel;
    this.cdr.markForCheck();
  }

  get filteredTools(): McpToolDefinition[] {
    if (!this.toolSearch.trim()) return this.tools;
    const q = this.toolSearch.toLowerCase();
    return this.tools.filter(
      (t) => t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)
    );
  }

  get toolCategories(): { name: string; tools: McpToolDefinition[] }[] {
    const catMap = new Map<string, McpToolDefinition[]>();
    for (const t of this.filteredTools) {
      const cat = t.category || 'General';
      if (!catMap.has(cat)) catMap.set(cat, []);
      catMap.get(cat)!.push(t);
    }
    return Array.from(catMap.entries()).map(([name, tools]) => ({ name, tools }));
  }

  // ── Message UI helpers ──────────────────────────────────────────

  toggleToolSteps(msg: McpChatMessage): void {
    msg._toolsExpanded = !msg._toolsExpanded;
    this.cdr.markForCheck();
  }

  toggleSql(msg: McpChatMessage): void {
    msg._sqlExpanded = !msg._sqlExpanded;
    this.cdr.markForCheck();
  }

  toggleResults(msg: McpChatMessage): void {
    msg._resultsExpanded = !msg._resultsExpanded;
    this.cdr.markForCheck();
  }

  isStepExpanded(msgId: string, stepIndex: number): boolean {
    return this.expandedSteps.has(`${msgId}-${stepIndex}`);
  }

  toggleStep(msgId: string, stepIndex: number): void {
    const key = `${msgId}-${stepIndex}`;
    if (this.expandedSteps.has(key)) {
      this.expandedSteps.delete(key);
    } else {
      this.expandedSteps.add(key);
    }
    this.cdr.markForCheck();
  }

  formatJson(obj: any): string {
    try {
      return JSON.stringify(obj, null, 2);
    } catch {
      return String(obj);
    }
  }

  /** Convert basic markdown to HTML for display. */
  renderMarkdown(text: string): string {
    if (!text) return '';

    // --- Phase 0: Extract fenced code blocks (```lang ... ```) before any other processing ---
    const codeBlocks: string[] = [];
    const escForCode = (s: string) =>
      s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    let processed = text.replace(
      /```[ \t]*([a-zA-Z0-9_+-]*)[ \t]*\r?\n([\s\S]*?)```/g,
      (_m, lang: string, code: string) => {
        const langClass = lang ? ` language-${lang.toLowerCase()}` : '';
        const langLabel = lang
          ? `<span class="code-lang">${escForCode(lang)}</span>`
          : '';
        const placeholder = `%%CODEBLOCK_${codeBlocks.length}%%`;
        codeBlocks.push(
          `<div class="code-block">${langLabel}<pre><code class="hljs${langClass}">${escForCode(
            code.replace(/\n$/, '')
          )}</code></pre></div>`
        );
        return placeholder;
      }
    );

    // --- Phase 1: Extract markdown tables before escaping ---
    const tableBlocks: string[] = [];
    const esc = (s: string) =>
      s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    processed = processed.replace(
      /((?:^\|.+\|[ \t]*\n){2,})/gm,
      (block) => {
        const rows = block.trim().split('\n').filter((r) => r.trim());
        if (rows.length < 2) return block;

        // Check for separator row (| --- | --- |)
        const sepIdx = rows.findIndex((r) => /^\|[\s:-]+\|$/.test(r.trim()));
        const dataStart = sepIdx > 0 ? sepIdx + 1 : 1;

        const parseCells = (row: string) =>
          row.split('|').slice(1, -1).map((c) => c.trim());

        const headerCells = parseCells(rows[0]);
        let thead = '<thead><tr>';
        headerCells.forEach((c) => (thead += `<th>${esc(c)}</th>`));
        thead += '</tr></thead>';

        let tbody = '<tbody>';
        for (let i = dataStart; i < rows.length; i++) {
          const cells = parseCells(rows[i]);
          tbody += '<tr>';
          cells.forEach((c) => (tbody += `<td>${esc(c)}</td>`));
          tbody += '</tr>';
        }
        tbody += '</tbody>';

        const placeholder = `%%TABLE_${tableBlocks.length}%%`;
        tableBlocks.push(
          `<div class="md-table-wrap"><table class="md-table">${thead}${tbody}</table></div>`
        );
        return placeholder;
      }
    );

    // --- Phase 2: Standard markdown transforms ---
    let html = processed
      // Escape HTML entities
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      // Bold: **text** or __text__
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/__(.*?)__/g, '<strong>$1</strong>')
      // Italic: *text* or _text_
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/(?<!\w)_(.*?)_(?!\w)/g, '<em>$1</em>')
      // Inline code: `text`
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      // Headers: ### text
      .replace(/^#### (.+)$/gm, '<h5>$1</h5>')
      .replace(/^### (.+)$/gm, '<h4>$1</h4>')
      .replace(/^## (.+)$/gm, '<h3>$1</h3>')
      .replace(/^# (.+)$/gm, '<h2>$1</h2>')
      // Horizontal rule
      .replace(/^---+$/gm, '<hr>');

    // Process bullet lists (- item or * item)
    html = html.replace(
      /((?:^[\t ]*[-*] .+$\n?)+)/gm,
      (block) => {
        const items = block
          .split('\n')
          .filter((l) => l.trim())
          .map((l) => `<li>${l.replace(/^[\t ]*[-*] /, '')}</li>`)
          .join('');
        return `<ul>${items}</ul>`;
      }
    );

    // Process numbered lists (1. item)
    html = html.replace(
      /((?:^[\t ]*\d+\. .+$\n?)+)/gm,
      (block) => {
        const items = block
          .split('\n')
          .filter((l) => l.trim())
          .map((l) => `<li>${l.replace(/^[\t ]*\d+\.\s*/, '')}</li>`)
          .join('');
        return `<ol>${items}</ol>`;
      }
    );

    // Line breaks: double newline = paragraph, single = <br>
    html = html
      .split(/\n{2,}/)
      .map((p) => p.trim())
      .filter((p) => p)
      .map((p) => {
        if (
          p.startsWith('<h') ||
          p.startsWith('<ul') ||
          p.startsWith('<ol') ||
          p.startsWith('<hr') ||
          p.startsWith('%%TABLE_') ||
          p.startsWith('%%CODEBLOCK_')
        )
          return p;
        return `<p>${p.replace(/\n/g, '<br>')}</p>`;
      })
      .join('');

    // --- Phase 3: Re-insert table HTML ---
    tableBlocks.forEach((tbl, i) => {
      html = html.replace(`%%TABLE_${i}%%`, tbl);
    });

    // --- Phase 4: Re-insert fenced code blocks ---
    codeBlocks.forEach((cb, i) => {
      html = html.replace(`%%CODEBLOCK_${i}%%`, cb);
    });

    return html;
  }

  toolStepSummary(step: McpAgentToolStep): string {
    if (!step.success) return step.error || 'Failed';
    const r = step.result;
    if (!r) return 'OK';
    if (r.extensions) return `${r.extensions.length} extension${r.extensions.length !== 1 ? 's' : ''}`;
    if (r.rows) return `${r.row_count ?? r.rows.length} row${(r.row_count ?? r.rows.length) !== 1 ? 's' : ''}`;
    if (r.tables) return `${r.tables.length} table${r.tables.length !== 1 ? 's' : ''}`;
    if (r.columns) return `${r.columns.length} column${r.columns.length !== 1 ? 's' : ''}`;
    if (r.records) return `${r.row_count ?? r.records.length} row${(r.row_count ?? r.records.length) !== 1 ? 's' : ''}`;
    if (r.session_id) return 'Connected';
    if (r.sql) return 'SQL generated';
    if (r.valid !== undefined) return r.valid ? 'Valid' : 'Invalid';
    if (r.relationships) return `${r.relationships.length} relationship${r.relationships.length !== 1 ? 's' : ''}`;
    return 'OK';
  }

  /** Generate human-readable description of what the tool step is doing */
  toolStepThinking(step: McpAgentToolStep): string {
    const args = step.arguments || {};
    switch (step.tool_name) {
      case 'search_tables':
        return `Searching for tables related to "${args['query'] || args['search_term'] || '...'}"`;
      case 'search_columns':
        return `Looking up columns${args['query'] ? ' matching "' + args['query'] + '"' : ''}${args['table_name'] ? ' in table ' + args['table_name'] : ''}`;
      case 'check_relationships':
        return `Checking relationships between ${args['table1'] || '?'} and ${args['table2'] || '?'}`;
      case 'introspect_schema':
        return `Inspecting schema for table ${args['table_name'] || '...'}`;
      case 'discover_join_paths':
        return `Finding join paths between ${args['source_table'] || '?'} and ${args['target_table'] || '?'}`;
      case 'preview_data':
        return `Previewing data from ${args['table_name'] || '...'}`;
      case 'sample_column_values':
        return `Sampling values from ${args['table_name'] ? args['table_name'] + '.' : ''}${args['column_name'] || '...'}`;
      case 'generate_sql':
        return `Generating SQL query for: "${(args['question'] || args['query'] || '...').substring(0, 80)}"`;
      case 'validate_sql':
        return 'Validating the generated SQL query';
      case 'execute_sql':
        return 'Executing SQL query against the database';
      case 'explain_sql':
        return 'Analyzing query execution plan';
      case 'fix_sql':
        return 'Fixing SQL query based on error feedback';
      case 'detect_extensions':
        return 'Detecting database extensions (PostGIS, pgvector, ...)';
      case 'semantic_data_search':
        return `Semantic search for "${(args['query_text'] || '...').toString().substring(0, 60)}" in ${args['table'] || '...'}`;
      case 'switch_database':
        return `Switching to database "${args['connection_name'] || '...'}"`;
      case 'list_available_databases':
        return 'Listing available database connections';
      case 'connect_database':
        return 'Connecting to database';
      default:
        return `Running ${step.tool_name}`;
    }
  }

  getToolIcon(name: string): string {
    const map: Record<string, string> = {
      search_tables: 'search',
      search_columns: 'view_column',
      check_relationships: 'account_tree',
      introspect_schema: 'schema',
      discover_join_paths: 'merge',
      preview_data: 'table_chart',
      sample_column_values: 'data_array',
      generate_sql: 'code',
      validate_sql: 'check_circle',
      execute_sql: 'play_arrow',
      explain_sql: 'help_outline',
      fix_sql: 'build',
      detect_extensions: 'extension',
      semantic_data_search: 'travel_explore',
      connect_database: 'power',
      get_conversation_context: 'history',
    };
    return map[name] || 'extension';
  }

  getCategoryIcon(name: string): string {
    const map: Record<string, string> = {
      'Schema Discovery': 'search',
      'Data Access': 'table_chart',
      'SQL Generation': 'code',
      'SQL Execution': 'play_arrow',
      'SQL Assistance': 'build',
      Connection: 'power',
      Context: 'history',
    };
    return map[name] || 'extension';
  }

  trackMessage(_index: number, msg: McpChatMessage): string {
    return msg.id;
  }

  trackToolStep(index: number, _step: McpAgentToolStep): number {
    return index;
  }

  /** Get the last assistant message (used for inline loading indicator) */
  getLastAssistantMessage(): McpChatMessage | null {
    for (let i = this.messages.length - 1; i >= 0; i--) {
      if (this.messages[i].role === 'assistant') return this.messages[i];
    }
    return null;
  }

  // ── Copilot Config ──────────────────────────────────────────────

  openConfigDialog(): void {
    this.showConfigDialog = true;
    this.cdr.markForCheck();
  }

  selectModel(model: CopilotModelInfo): void {
    if (model.supported_in_api === false) return;
    this.selectedModel = model.id;
  }

  reloadModels(): void {
    this.copilotConfigured = this.hasOpenAIToken;
    this.mcpAgent.loadCopilotModels().subscribe({
      next: (models) => {
        this.copilotModels = models;
        if (!models.some(model => model.id === this.selectedModel && model.supported_in_api !== false)) {
          this.selectedModel = models.find(model => model.supported_in_api !== false)?.id || this.selectedModel;
        }
        this._buildGroupedModels();
        this.cdr.markForCheck();
      },
      error: () => this.cdr.markForCheck(),
    });
  }

  closeConfigDialog(): void {
    this.showConfigDialog = false;
    this.cdr.markForCheck();
  }

  // ── OpenAI Codex OAuth Device Flow ──────────────────────────────

  startCodexSignIn(): void {
    this.deviceFlowActive = true;
    this.deviceFlowStatus = 'Starting...';
    this.cdr.markForCheck();

    this.mcpAgent.startDeviceFlow().subscribe({
      next: (res) => {
        this.deviceFlowCode = res.user_code;
        this.deviceFlowUri = res.verification_uri;
        this.deviceFlowStatus = '';
        this.cdr.markForCheck();

        // Open the OpenAI Codex device authorization page.
        window.open(res.verification_uri, '_blank');

        // Start polling for approval
        this._startDeviceFlowPolling(res.interval || 5);
      },
      error: (err) => {
        this.deviceFlowActive = false;
        this.deviceFlowStatus = '';
        const detail = err?.message || 'Failed to start sign-in';
        this.snackBar.open(detail, 'Dismiss', { duration: 6000 });
        this.cdr.markForCheck();
      },
    });
  }

  cancelDeviceFlow(): void {
    this._stopDeviceFlowPolling();
    this.deviceFlowActive = false;
    this.deviceFlowCode = '';
    this.deviceFlowStatus = '';
    this.cdr.markForCheck();
  }

  signOutOpenAI(): void {
    this.mcpAgent.disconnectOpenAI().subscribe({
      next: (cfg) => {
        this.hasOpenAIToken = false;
        this.copilotConfigured = cfg.configured;
        this.copilotModels = [];
        this.groupedModels = [];
        this.snackBar.open('OpenAI credentials removed', 'OK', { duration: 3000 });
        this.cdr.markForCheck();
      },
      error: (err) => this.snackBar.open(err?.message || 'Sign out failed', 'Dismiss', { duration: 5000 }),
    });
  }

  copyDeviceCode(): void {
    if (this.deviceFlowCode) {
      navigator.clipboard.writeText(this.deviceFlowCode);
      this.snackBar.open('Code copied!', '', { duration: 1500 });
    }
  }

  private _deviceFlowIntervalSec = 5;

  private _startDeviceFlowPolling(intervalSec: number): void {
    this.deviceFlowPolling = true;
    this._deviceFlowIntervalSec = intervalSec;
    this.deviceFlowStatus = 'Waiting for authorization...';
    this.cdr.markForCheck();
    this._schedulePoll();
  }

  private _schedulePoll(): void {
    this._deviceFlowPollTimer = setTimeout(() => {
      if (!this.deviceFlowPolling) return;
      this.mcpAgent.pollDeviceFlow().subscribe({
        next: (res) => {
          // Honor a server-requested slower polling interval.
          if (res.interval && res.interval > this._deviceFlowIntervalSec) {
            this._deviceFlowIntervalSec = res.interval;
          }
          if (res.status === 'complete') {
            this._stopDeviceFlowPolling();
            this.deviceFlowActive = false;
            this.copilotConfigured = true;
            if (res.default_model) {
              this.selectedModel = res.default_model;
            }
            this.showConfigDialog = false;
            this.hasOpenAIToken = true;
            this.snackBar.open('OpenAI Codex connected!', 'OK', { duration: 3000 });

            // Reload models
            this.mcpAgent.loadCopilotModels().subscribe({
              next: (models) => {
                this.copilotModels = models;
                this.cdr.markForCheck();
              },
            });
            this.cdr.markForCheck();
          } else if (res.status === 'expired') {
            this._stopDeviceFlowPolling();
            this.deviceFlowActive = false;
            this.deviceFlowStatus = '';
            this.snackBar.open('Code expired. Please try again.', 'Dismiss', { duration: 5000 });
            this.cdr.markForCheck();
          } else if (res.status === 'denied') {
            this._stopDeviceFlowPolling();
            this.deviceFlowActive = false;
            this.deviceFlowStatus = '';
            this.snackBar.open('Authorization denied.', 'Dismiss', { duration: 5000 });
            this.cdr.markForCheck();
          } else if (res.status === 'error') {
            this._stopDeviceFlowPolling();
            this.deviceFlowActive = false;
            this.deviceFlowStatus = '';
            this.snackBar.open(res.error || 'OAuth error', 'Dismiss', { duration: 5000 });
            this.cdr.markForCheck();
          } else {
            // 'pending' → schedule next poll
            this._schedulePoll();
          }
        },
        error: () => {
          // Network error — retry with current interval
          this._schedulePoll();
        },
      });
    }, this._deviceFlowIntervalSec * 1000) as any;
  }

  private _stopDeviceFlowPolling(): void {
    if (this._deviceFlowPollTimer) {
      clearTimeout(this._deviceFlowPollTimer);
      this._deviceFlowPollTimer = null;
    }
    this.deviceFlowPolling = false;
  }

  getModelVendorIcon(vendor: string): string {
    const map: Record<string, string> = {
      OpenAI: 'auto_awesome',
      Microsoft: 'window',
      Meta: 'groups',
      'Mistral AI': 'air',
      DeepSeek: 'psychology',
    };
    return map[vendor] || 'model_training';
  }

  private _scrollToBottom(): void {
    if (this.chatContainer?.nativeElement) {
      const el = this.chatContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }
}
