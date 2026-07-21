/**
 * MCP Agent models — chat-based agent with tool call visualization
 */

// ── Tool definitions (for reference sidebar) ─────────────────────

export interface McpToolDefinition {
  name: string;
  description: string;
  parameters: McpJsonSchema;
  category: string;
}

export interface McpJsonSchema {
  type: string;
  properties: Record<string, McpPropertySchema>;
  required?: string[];
}

export interface McpPropertySchema {
  type: string;
  description?: string;
  default?: any;
  enum?: string[];
  items?: any;
}

// ── Direct tool call (kept for manual invocation) ────────────────

export interface McpToolCallRequest {
  tool_name: string;
  arguments: Record<string, any>;
}

export interface McpToolCallResponse {
  success: boolean;
  tool_name: string;
  result: any;
  error: string | null;
  execution_time_ms: number;
}

export interface McpChainStepRequest {
  tool_name: string;
  arguments: Record<string, any>;
}

export interface McpChainRequest {
  steps: McpChainStepRequest[];
}

export interface McpChainResponse {
  results: McpToolCallResponse[];
  total_time_ms: number;
}

// ── Agent chat (LLM + MCP tools — Copilot-style) ────────────────

export interface McpAgentChatRequest {
  message: string;
  db_session_id: string;
  chat_session_id?: string;
}

export interface McpAgentToolStep {
  tool_name: string;
  arguments: Record<string, any>;
  result: any;
  success: boolean;
  error: string | null;
  execution_time_ms: number;
  reasoning?: string | null;
  database?: string | null;
}

// ── Verifiable Trust Layer (Phase 1) ─────────────────────────────

/** One earned trust signal computed from the agent's tool trace. */
export interface TrustCheck {
  name: string;
  passed: boolean;
  detail: string;
}

/** Dual-path cross-check of the headline metric (two independent queries). */
export interface TrustVerification {
  primary_value: number;
  check_value: number;
  delta: number;
  agreed: boolean;
  method_primary: string;
  method_check: string;
}

/** Optional provenance attached to a grounded answer. */
export interface GroundedSource {
  title: string;
  source: string;
}

export interface McpAgentChatResponse {
  success: boolean;
  response_message: string;
  sql: string | null;
  records: Record<string, any>[];
  row_count: number;
  columns: string[];
  tool_steps: McpAgentToolStep[];
  total_time_ms: number;
  chat_session_id: string;
  error: string | null;
  confidence: string;
  trust_score?: number;
  trust_label?: string;
  trust_checks?: TrustCheck[];
  verification?: TrustVerification | null;
  grounded_sources?: GroundedSource[];
}

// ── UI-side chat message model ───────────────────────────────────

export interface McpChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;

  // Assistant-only fields
  toolSteps?: McpAgentToolStep[];
  sql?: string | null;
  records?: Record<string, any>[];
  rowCount?: number;
  columns?: string[];
  totalTimeMs?: number;
  confidence?: string;
  error?: string | null;
  activeDatabase?: string;
  tokenUsage?: TokenUsage;

  // ── Verifiable Trust Layer (Phase 1) ──
  trustScore?: number;
  trustLabel?: string;  // 'verified' | 'caution' | 'unverified' | '' (n/a)
  trustChecks?: TrustCheck[];
  verification?: TrustVerification | null;
  groundedSources?: GroundedSource[];

  // UI state
  _toolsExpanded?: boolean;
  _sqlExpanded?: boolean;
  _resultsExpanded?: boolean;
  _thinkingText?: string | null;
  _pendingReasoning?: string | null;
}

/** UI-side tracking of a single tool invocation in the log (kept for direct mode) */
export interface McpToolCallEntry {
  id: string;
  toolName: string;
  arguments: Record<string, any>;
  status: 'pending' | 'success' | 'error';
  result?: any;
  error?: string;
  executionTimeMs?: number;
  timestamp: Date;
  chainId?: string;
  chainIndex?: number;
}

// ── OpenAI Codex Models Integration ───────────────────────────────

export interface CopilotModelInfo {
  id: string;
  name: string;
  vendor: string;
  context_window: number;
  supported_in_api?: boolean;
  visibility?: 'list' | 'hide' | string;
}

export interface CopilotConfigResponse {
  provider: 'openai_codex';
  configured: boolean;
  default_model: string;
  has_token: boolean;
  has_codex_token: boolean;
  codex_email: string;
  codex_display_name: string;
  codex_account_id: string;
}

export interface CopilotSshCredentials {
  ssh_host: string;
  ssh_port?: number;
  ssh_username: string;
  ssh_password: string;
  sudo_password?: string;
  kubeconfig_path?: string;
  use_sudo?: boolean;
}

export interface CopilotChatRequest {
  message: string;
  db_session_id?: string;
  session_id?: string;
  model?: string;
  /**
   * Opt-in: when true the backend agent may fan SELECT queries across ALL
   * saved connections via the `query_across_databases` MCP tool. Capped at
   * 10 pods / 1000 rows-per-pod / 30s-per-pod, SELECT-only.
   */
  cross_pod?: boolean;
  /**
   * Optional SSH credentials enabling the *_via_ssh MCP tools (live cluster
   * discovery + cross-pod SQL fan-out via kubectl exec). The password is
   * sent per-request, never persisted server-side, and injected at MCP
   * dispatch time so the LLM never receives it.
   */
  ssh_credentials?: CopilotSshCredentials;
}

export interface CopilotChatResponse {
  success: boolean;
  message: string;
  sql: string | null;
  records: Record<string, any>[];
  row_count: number;
  columns: string[];
  tool_steps: McpAgentToolStep[];
  total_time_ms: number;
  model: string;
  session_id: string;
  error: string | null;
  usage: Record<string, any>;
  active_database: string;
  trust_score?: number;
  trust_label?: string;
  trust_checks?: TrustCheck[];
  verification?: TrustVerification | null;
  grounded_sources?: GroundedSource[];
}

// ── OpenAI Codex OAuth Device Flow ────────────────────────────────

export interface DeviceFlowResponse {
  user_code: string;
  verification_uri: string;
  expires_in: number;
  interval: number;
}

export interface DeviceFlowPollResponse {
  status: 'pending' | 'complete' | 'expired' | 'denied' | 'error';
  configured?: boolean;
  default_model?: string;
  has_token?: boolean;
  warning?: string;
  error?: string;
  interval?: number;
}

// ── Token / Cost Meta Information ────────────────────────────────

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  model: string;
  estimated_cost: number;
}

// ── MCP Agent Chat Session History ───────────────────────────────

export interface McpChatSession {
  id: string;
  title: string;
  mode: 'copilot';
  model: string;
  createdAt: Date;
  lastActiveAt: Date;
  messageCount: number;
  messages: McpChatMessage[];
  totalTokens: number;
  totalCost: number;
  totalTimeMs: number;
  queryCount: number;
  copilotSessionId?: string;
}
