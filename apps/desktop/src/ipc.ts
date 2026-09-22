import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

/* ------------------------------------------------------------------ *
 * 结构化错误：所有命令失败时 reject { code, message, retryable, details? }
 * ------------------------------------------------------------------ */

export interface CoreError {
  code: string;
  message: string;
  retryable: boolean;
  details?: Record<string, unknown>;
}

/** 仅用于「命令只回了字符串」时的兜底判断；结构化错误自带 retryable。 */
const RETRYABLE_CODES: Record<string, true> = {
  DATASET_UNAVAILABLE: true,
  REFRESH_LOCKED: true,
  REFRESH_FAILED: true,
  AGENT_BUSY: true,
  AGENT_WRITE_FAILED: true,
  PI_UNAVAILABLE: true,
  SIDECAR_TIMEOUT: true,
};

const LEADING_CODE = /^([A-Z][A-Z0-9_]{2,})\b/;

export function toCoreError(error: unknown): CoreError {
  if (error && typeof error === "object") {
    const candidate = error as { code?: unknown; message?: unknown; retryable?: unknown; details?: unknown };
    if (typeof candidate.code === "string" && candidate.code) {
      return {
        code: candidate.code,
        message: typeof candidate.message === "string" && candidate.message ? candidate.message : candidate.code,
        retryable: typeof candidate.retryable === "boolean" ? candidate.retryable : RETRYABLE_CODES[candidate.code] === true,
        details: candidate.details && typeof candidate.details === "object" ? candidate.details as Record<string, unknown> : undefined,
      };
    }
    if (typeof candidate.message === "string") return fromMessage(candidate.message);
  }
  if (typeof error === "string") return fromMessage(error);
  if (error instanceof Error) return fromMessage(error.message);
  return { code: "INTERNAL_ERROR", message: String(error ?? "未知错误"), retryable: false };
}

function fromMessage(message: string): CoreError {
  const trimmed = message.trim();
  if (trimmed.startsWith("{")) {
    try {
      return toCoreError(JSON.parse(trimmed));
    } catch {
      /* 不是 JSON，继续按文本处理 */
    }
  }
  const match = trimmed.match(LEADING_CODE);
  const code = match ? match[1] : "INTERNAL_ERROR";
  return { code, message: trimmed || code, retryable: RETRYABLE_CODES[code] === true };
}

/* ------------------------------------------------------------------ *
 * 数据类型（字段名与冻结契约一致）
 * ------------------------------------------------------------------ */

export interface StatusData {
  dataset: { available: boolean; database_count: number; encrypted_database_count: number; scan_deferred?: boolean };
  allow_send_to_wecom?: boolean;
  key: { available: boolean; validated_database_count: number };
  snapshot: { available: boolean; created_at: string | null; age_minutes: number | null; active: boolean };
  selected_session: { available: boolean; display_name: string | null; kind: string | null };
  dataset_selection_required: boolean;
}

export interface SessionSummary {
  session_key: string;
  /** 企微侧 conversation_id（不是前端复合键） */
  conversation_id?: string;
  dataset_id?: string;
  snapshot_id?: string;
  display_name: string;
  kind: string;
  /** `YYYY-MM-DD HH:MM` 或 null，只做展示与字符串排序 */
  last_message_time: string | null;
  selected?: boolean;
}

export interface AccountIdentity {
  account_id: string;
  account_name: string;
  dataset_id: string;
  recent_write_minutes?: number | null;
  confidence?: "live" | "cached" | "selected";
}

export interface AccountSummary extends AccountIdentity {
  dataset_count: number;
  database_count: number;
  key_available: boolean;
  current: boolean;
}

export interface DatasetSummary {
  active?: boolean;
  recent_write_minutes?: number | null;
  account_id: string;
  account_key?: string;
  account_name?: string;
  company_name: string;
  dataset_id: string;
  kind: "current" | "backup" | "unknown";
  display_name: string;
  database_count: number;
  encrypted_database_count: number;
  wal_count: number;
  key_available: boolean;
}

export interface DatasetsData {
  count: number;
  ignored?: DatasetSummary[];
  selected_dataset_id: string | null;
  datasets: DatasetSummary[];
  accounts: AccountSummary[];
  current_account: AccountIdentity | null;
  selected_account: AccountSummary | null;
  /** true 表示本次扫描超时、列表来自本地缓存（容器目录读取被 sandboxd 拖慢时会出现） */
  scan_deferred?: boolean;
}
/*
 * 新字段由 sidecar 提供；旧缓存/旧 sidecar 仍由 normalizeDatasets 兜底为空值，
 * 防止升级过程中前端因单个字段缺失而白屏。
 */

export interface PreviewData {
  session_name: string;
  snapshot_created_at: string;
  snapshot_age_minutes: number;
  original_message_count: number;
  retained_message_count: number;
  estimated_tokens: number;
  truncated: boolean;
  redactions: { email: number; credential: number; control: number };
}

export interface AgentHistoryMessage {
  role: "user" | "assistant" | "system";
  text: string;
  ts: number;
}
export interface AgentConversationSummary {
  /** 本地复合键 `datasetId:sessionKey` */
  conversation_id: string;
  session_key: string;
  dataset_id: string;
  snapshot_id: string;
  session_name: string;
  model: string;
  message_count: number;
  preview: string;
  updated_at: number;
}

export interface AgentConversationHistory {
  conversation_id?: string;
  session_key: string;
  session_name: string;
  model: string;
  messages: AgentHistoryMessage[];
  dataset_id?: string;
  snapshot_id?: string;
}

export interface WorkbenchDataset {
  dataset_id: string;
  account_id: string;
  company_name: string;
  display_name: string;
  kind: string;
  key_available: boolean;
  active: boolean;
}

export interface WorkbenchSnapshot {
  snapshot_id: string;
  created_at: string;
  age_minutes: number;
  refreshed: boolean;
  degraded: boolean;
}

export interface WorkbenchWarning {
  code: string;
  message: string;
}

export interface WorkbenchBootstrap {
  readiness: "ready" | "needs_dataset" | "needs_key" | "refresh_failed";
  dataset: WorkbenchDataset | null;
  snapshot: WorkbenchSnapshot | null;
  sessions: SessionSummary[];
  selection_reason: string;
  warnings: WorkbenchWarning[];
  conversations: AgentConversationSummary[];
  current_account: AccountIdentity | null;
  selected_account: AccountSummary | null;
}

export interface BindSessionParams {
  sessionKey: string;
  conversationId?: string;
  datasetId?: string;
  snapshotId?: string;
  allowHistory?: boolean;
}

export interface BindSessionResult {
  bound: true;
  session_key: string;
  /** 企微侧 conversation_id */
  conversation_id: string;
  dataset_id: string;
  snapshot_id: string;
  mode: "active" | "history";
  recovered: string;
  history_snapshot_id: string | null;
  history_created_at: string | null;
}

export interface AgentSendParams {
  /**
   * 界面关联令牌：Rust 会把它原样放进每条 `agent-event` 的 `conversation_id`，
   * 只用于把事件回给发起的那条对话，**不参与 Agent 身份**（只有一个主 Agent）。
   */
  clientToken: string;
  model: string;
  text: string;
  /** 本轮要注入的企微来源；为空表示本轮不带企微资料，纯对话。 */
  sources?: AgentSourceRequest[];
  /** 已预览过的资料包：来源与当前选择一致时复用它，保证「预览的就是发送的」。 */
  packageId?: string;
  imageOptions?: { max_images?: number; max_total_bytes?: number; max_edge_pixels?: number };
  maxContextTokens?: number;
  maxMessageCharacters?: number;
}

/** 一个来源的显式身份：企业 + 快照 + 会话，由 sidecar 逐项校验归属。 */
export interface AgentSourceRequest {
  dataset_id: string;
  snapshot_id: string;
  session_key: string;
  limit?: number;
  start?: string;
  end?: string;
  include_images?: boolean;
}

/** 本轮资料包（与 contracts/prepare-context.schema.json 对齐）。 */
export interface ContextPackageSource {
  source_id: string;
  dataset_id: string;
  snapshot_id: string;
  session_key: string;
  conversation_id: string;
  display_name: string;
  kind: string;
  snapshot_created_at: string;
  snapshot_age_minutes: number;
  stale: boolean;
  read_only_history: boolean;
  original_message_count: number;
  retained_message_count: number;
  image_count: number;
  omitted_image_count: number;
  estimated_tokens: number;
  truncated: boolean;
  redactions: { email: number; credential: number; control: number };
}

/** 图片可用状态：只有 img_ 前缀（original/thumbnail）会真正发给模型。 */
export interface ContextPackageImage {
  image_id: string;
  source_id: string;
  message_id: string;
  status: "original" | "thumbnail" | "missing" | "unsupported" | "too_large" | "failed";
  reason: string | null;
  path: string | null;
  mime_type: string | null;
  sha256: string | null;
  bytes: number | null;
  width: number | null;
  height: number | null;
  estimated_tokens: number | null;
}

export interface ContextPackageWarning {
  code: string;
  message: string;
  source_id: string | null;
  image_id: string | null;
}

export interface ContextPackageData {
  package_id: string;
  created_at: string;
  dir: string;
  text: string;
  sources: ContextPackageSource[];
  messages: { message_id: string; source_id: string; time: string; sender: string; type_name: string; content: string; image_ids: string[] }[];
  images: ContextPackageImage[];
  warnings: ContextPackageWarning[];
  stats: {
    source_count: number;
    message_count: number;
    image_count: number;
    omitted_image_count: number;
    image_bytes: number;
    estimated_tokens: number;
    truncated: boolean;
  };
}

export interface PackageImageData {
  image_id: string;
  mime_type: string;
  bytes: number;
  data_url: string;
}

export interface ImageDownloadResult {
  directory: string;
  session_name: string;
  total_images: number;
  downloaded: number;
  skipped_existing: number;
  omitted_count: number;
  omitted: Array<{ message_id: string; time: string; reason: string }>;
  client_fetch_attempted: boolean;
  client_fetch_attempts: number;
  client_fetched: number;
  client_fetch_missing: number;
  client_fetch_error_code: string | null;
  client_fetch_error: string | null;
}

/* ---- 记忆视图（与 contracts/memory.schema.json 对齐） ---- */

export interface MemoryImageRef {
  image_id: string;
  status: string;
  mime_type: string | null;
  bytes: number | null;
}

export interface MemoryContextMessage {
  index: number;
  role: "user" | "assistant" | "system";
  text: string;
  ts: number;
  images: MemoryImageRef[];
}

export interface MemoryContextData {
  view: "context";
  memory_epoch: number;
  state: string;
  model: string | null;
  thinking: string;
  streaming: boolean;
  system_prompt: string;
  system_prompt_chars: number;
  context_tokens: number | null;
  context_percent: number | null;
  context_window: number | null;
  messages: MemoryContextMessage[];
  compactions: { ts: number; summary: string; tokens_after: number | null }[];
  images_in_context: number;
  updated_at: string;
}

export interface MemoryHistoryMessage {
  index: number;
  role: "user" | "assistant" | "system";
  text: string;
  ts: number;
  state: "complete" | "incomplete" | "failed" | "cancelled";
  images: MemoryImageRef[];
  in_effective_context: boolean;
  summarized: boolean;
}

export interface MemoryHistoryData {
  view: "history";
  memory_epoch: number;
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  messages: MemoryHistoryMessage[];
  updated_at: string;
}

export interface MemoryInjectionEntry {
  request_id: string;
  at: string;
  question: string;
  package_id: string | null;
  sources: { source_id: string; dataset_id: string; session_key: string; display_name: string; kind: string; snapshot_id: string; snapshot_created_at: string; message_count: number; image_count: number }[];
  retained: { message_count: number; image_count: number };
  effectiveness: "active" | "summarized" | "evicted" | "unknown";
}

export interface MemoryInjectionsData {
  view: "injections";
  memory_epoch: number;
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  entries: MemoryInjectionEntry[];
  updated_at: string;
}

export interface MemoryClearData {
  view: "clear";
  cleared: boolean;
  previous_epoch: number;
  memory_epoch: number;
  removed: { session: boolean; messages: number; injections: number; packages: number; images: number };
  kept: { datasets: true; snapshots: true; keys: true; providers: true; legacy_archive: true };
  started_at: string;
  finished_at: string;
}

export interface PreviewPackageParams {
  sources: AgentSourceRequest[];
  imageOptions?: { max_images?: number; max_total_bytes?: number; max_edge_pixels?: number };
  maxContextTokens?: number;
  maxMessageCharacters?: number;
}

/** 唯一主 Agent 的发送结果：`accepted` 只表示写入/接受成功，不代表模型已回答。 */
export interface AgentSendResult {
  run_id: string;
  request_id: string;
  started: boolean;
  memory_epoch: number;
  accepted: boolean;
  /** 本轮资料包标识（未注入资料时为 null） */
  package_id?: string | null;
  /** 本轮实际发送的图片数量（长截图分片按片计） */
  image_count?: number;
  /** 资料包中未能恢复或因限制未发送的图片数量 */
  omitted_image_count?: number;
}

/** `agent_status` 的运行态（与 contracts/agent-status.schema.json 对齐）。 */
export interface AgentStatusData {
  state: "stopped" | "starting" | "idle" | "preparing" | "generating" | "clearing" | "failed";
  memory_epoch: number;
  run_id: string | null;
  request_id: string | null;
  package_id: string | null;
  model: string | null;
  thinking: string;
  pi_pid: number | null;
  session_ready: boolean;
  started_at: string | null;
  last_event_at: string | null;
  unfinished_clear_epoch: number | null;
  last_error: { code: string; message: string } | null;
  busy: boolean;
}

export interface PreviewContextParams {
  sessionKey: string;
  datasetId?: string;
  snapshotId?: string;
  limit?: number;
}

export interface PiEnvironment {
  version: string;
  auth_available: boolean;
  api_key_available?: boolean;
  proxy_configured: boolean;
  proxy_reachable: boolean;
  transport: string;
}

/* ------------------------------------------------------------------ *
 * 事件
 * ------------------------------------------------------------------ */

export interface AgentEventPayload {
  /** 发起的界面会话关联令牌（Rust 原样回传，不参与 Agent 身份） */
  conversation_id?: string;
  run_id?: string;
  request_id?: string;
  /** 记忆代次：清空后旧代次的事件必须丢弃 */
  memory_epoch?: number;
  /** 旧字段：多 Agent 时代的企微身份，当前不再由 Rust 填充 */
  dataset_id?: string;
  snapshot_id?: string;
  session_key?: string;
  /** Rust 可能传原始 pi JSON 字符串，也可能传已解析的事件对象。 */
  event?: string | Record<string, unknown>;
}

export interface ContextGenerationPayload {
  dataset_id: string;
  snapshot_id: string;
  /** 被作废的会话复合键 */
  invalidated: string[];
}

export const AGENT_MODELS: Array<{ provider: string; models: string[] }> = [
  { provider: "deepseek", models: ["deepseek-flash", "deepseek-v4-pro"] },
];

export const DEFAULT_AGENT_MODEL = "deepseek/deepseek-flash";


/* ------------------------------------------------------------------ *
 * DeepSeek 官方 API 与动态模型目录
 * ------------------------------------------------------------------ */


export interface AgentModelInfo {
  /** `provider/model` 全 id，供 --model 使用 */
  id: string;
  provider: string;
  model: string;
  /** 面向用户的模型名称；API id 可能是兼容别名。 */
  name: string;
  context: string;
  max_out: string;
  thinking: boolean;
  images: boolean;
}

export interface DeepSeekStatus {
  configured: boolean;
  models: AgentModelInfo[];
  models_json_path?: string;
}

export interface DeepSeekConfigureResult {
  configured: boolean;
  default_model: string;
  models: AgentModelInfo[];
  render?: { models_json_path?: string };
}
export function builtinModelCatalog(): AgentModelInfo[] {
  return AGENT_MODELS.flatMap((group) => group.models.map((item) => ({
    id: `${group.provider}/${item}`,
    provider: group.provider,
    model: item,
    name: item === "deepseek-flash" ? "DeepSeek-V4.1-Flash" : item,
    context: "",
    max_out: "",
    thinking: false,
    images: item === "deepseek-flash",
  })));
}


export interface RemoveDatasetResult {
  removed?: boolean;
  restored?: boolean;
  dataset_id?: string;
  deleted_snapshots?: number;
  deleted_keys?: number;
  removed_agents?: number;
  count: number;
  selected_dataset_id: string | null;
  datasets: DatasetSummary[];
  ignored: DatasetSummary[];
}

export interface RemoveAccountResult {
  removed: boolean;
  account_id: string;
  dataset_ids: string[];
  deleted_datasets: number;
  deleted_snapshots: number;
  deleted_keys: number;
  count: number;
  selected_dataset_id: string | null;
  datasets: DatasetSummary[];
  ignored: DatasetSummary[];
  accounts: AccountSummary[];
  current_account: AccountIdentity | null;
  selected_account: AccountSummary | null;
}
/* ------------------------------------------------------------------ *
 * wire → 前端模型的归一化（后端可能给 null / 字符串 warnings）
 * ------------------------------------------------------------------ */

interface RawBootstrap {
  readiness?: unknown;
  dataset?: unknown;
  snapshot?: unknown;
  sessions?: unknown;
  selection_reason?: unknown;
  warnings?: unknown;
  conversations?: unknown;
  current_account?: unknown;
  selected_account?: unknown;
}

const READINESS_VALUES: Record<string, WorkbenchBootstrap["readiness"]> = {
  ready: "ready",
  needs_dataset: "needs_dataset",
  needs_key: "needs_key",
  refresh_failed: "refresh_failed",
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function normalizeWarning(value: unknown): WorkbenchWarning | null {
  if (typeof value === "string" && value) return { code: value, message: value };
  const record = asRecord(value);
  if (!record) return null;
  const code = asString(record.code);
  const message = asString(record.message, code);
  if (!code && !message) return null;
  return { code: code || message, message: message || code };
}

function normalizeSessions(value: unknown): SessionSummary[] {
  const sessions: SessionSummary[] = [];
  for (const item of asArray(value)) {
    const record = asRecord(item);
    if (!record) continue;
    const sessionKey = asString(record.session_key);
    if (!sessionKey) continue;
    sessions.push({
      session_key: sessionKey,
      conversation_id: asString(record.conversation_id) || undefined,
      dataset_id: asString(record.dataset_id) || undefined,
      snapshot_id: asString(record.snapshot_id) || undefined,
      display_name: asString(record.display_name, "未命名会话"),
      kind: asString(record.kind, "其他"),
      last_message_time: typeof record.last_message_time === "string" ? record.last_message_time : null,
    });
  }
  return sessions;
}

function normalizeSummaries(value: unknown): AgentConversationSummary[] {
  const summaries: AgentConversationSummary[] = [];
  for (const item of asArray(value)) {
    const record = asRecord(item);
    if (!record) continue;
    const sessionKey = asString(record.session_key);
    const datasetId = asString(record.dataset_id);
    const conversationId = asString(record.conversation_id) || `${datasetId}:${sessionKey}`;
    if (!sessionKey || !conversationId) continue;
    summaries.push({
      conversation_id: conversationId,
      session_key: sessionKey,
      dataset_id: datasetId,
      snapshot_id: asString(record.snapshot_id),
      session_name: asString(record.session_name, sessionKey),
      model: asString(record.model),
      message_count: typeof record.message_count === "number" ? record.message_count : 0,
      preview: asString(record.preview),
      updated_at: typeof record.updated_at === "number" ? record.updated_at : 0,
    });
  }
  return summaries;
}

function normalizeAccount(value: unknown): AccountSummary | null {
  const record = asRecord(value);
  const accountId = asString(record?.account_id);
  if (!accountId) return null;
  return {
    account_id: accountId,
    account_name: asString(record?.account_name, "未命名账户"),
    dataset_id: asString(record?.dataset_id),
    recent_write_minutes: typeof record?.recent_write_minutes === "number" ? record.recent_write_minutes : null,
    confidence: record?.confidence === "live" || record?.confidence === "cached" || record?.confidence === "selected" ? record.confidence : undefined,
    dataset_count: typeof record?.dataset_count === "number" ? record.dataset_count : 0,
    database_count: typeof record?.database_count === "number" ? record.database_count : 0,
    key_available: record?.key_available === true,
    current: record?.current === true,
  };
}

function normalizeDataset(value: unknown): DatasetSummary | null {
  const record = asRecord(value);
  const datasetId = asString(record?.dataset_id);
  if (!datasetId) return null;
  const kind = asString(record?.kind, "unknown");
  return {
    dataset_id: datasetId,
    account_id: asString(record?.account_id),
    account_key: asString(record?.account_key) || undefined,
    account_name: asString(record?.account_name) || undefined,
    company_name: asString(record?.company_name),
    kind: kind === "current" || kind === "backup" ? kind : "unknown",
    display_name: asString(record?.display_name, "未命名企业"),
    database_count: typeof record?.database_count === "number" ? record.database_count : 0,
    encrypted_database_count: typeof record?.encrypted_database_count === "number" ? record.encrypted_database_count : 0,
    wal_count: typeof record?.wal_count === "number" ? record.wal_count : 0,
    key_available: record?.key_available === true,
    active: record?.active === true,
    recent_write_minutes: typeof record?.recent_write_minutes === "number" ? record.recent_write_minutes : null,
  };
}

function normalizeDatasets(raw: unknown): DatasetsData {
  const record = asRecord(raw) ?? {};
  const datasets = asArray(record.datasets).map(normalizeDataset).filter((item): item is DatasetSummary => item !== null);
  const ignored = asArray(record.ignored).map(normalizeDataset).filter((item): item is DatasetSummary => item !== null);
  const accounts = asArray(record.accounts).map(normalizeAccount).filter((item): item is AccountSummary => item !== null);
  return {
    count: typeof record.count === "number" ? record.count : datasets.length,
    selected_dataset_id: typeof record.selected_dataset_id === "string" ? record.selected_dataset_id : null,
    datasets,
    ignored,
    accounts,
    current_account: normalizeAccount(record.current_account),
    selected_account: normalizeAccount(record.selected_account),
    scan_deferred: record.scan_deferred === true,
  };
}

function normalizeBootstrap(raw: unknown): WorkbenchBootstrap {
  const record = asRecord(raw) ?? {};
  const dataset = asRecord(record.dataset);
  const snapshot = asRecord(record.snapshot);
  const readiness = READINESS_VALUES[asString(record.readiness)] ?? "needs_dataset";
  return {
    readiness,
    dataset: dataset && asString(dataset.dataset_id)
      ? {
        dataset_id: asString(dataset.dataset_id),
        account_id: asString(dataset.account_id),
        company_name: asString(dataset.company_name),
        display_name: asString(dataset.display_name, "未命名企业"),
        kind: asString(dataset.kind, "unknown"),
        key_available: dataset.key_available === true,
        active: dataset.active === true,
      }
      : null,
    snapshot: snapshot && asString(snapshot.snapshot_id)
      ? {
        snapshot_id: asString(snapshot.snapshot_id),
        created_at: asString(snapshot.created_at),
        age_minutes: typeof snapshot.age_minutes === "number" ? snapshot.age_minutes : 0,
        refreshed: snapshot.refreshed === true,
        degraded: snapshot.degraded === true,
      }
      : null,
    sessions: normalizeSessions(record.sessions),
    selection_reason: asString(record.selection_reason),
    warnings: asArray(record.warnings).map(normalizeWarning).filter((warning): warning is WorkbenchWarning => warning !== null),
    conversations: normalizeSummaries(record.conversations),
    current_account: normalizeAccount(record.current_account),
    selected_account: normalizeAccount(record.selected_account),
  };
}

/* ------------------------------------------------------------------ *
 * 浏览器 dev 预览 mock（只在 vite dev + 无 Tauri 时生效）
 * ------------------------------------------------------------------ */
const devPreview = import.meta.env.DEV && !("__TAURI_INTERNALS__" in window);
const mockDatasets: DatasetSummary[] = [
  { dataset_id: "preview-current", account_id: "demo-account", account_key: "示例账户", account_name: "示例账户", company_name: "示例企业", kind: "current", display_name: "示例企业·当前数据", database_count: 19, encrypted_database_count: 19, wal_count: 0, key_available: true, active: true },
  { dataset_id: "preview-lab", account_id: "demo-account", account_key: "示例账户", account_name: "示例账户", company_name: "另一家示例企业", kind: "current", display_name: "另一家示例企业·当前数据", database_count: 16, encrypted_database_count: 16, wal_count: 0, key_available: true, active: false },
  { dataset_id: "preview-nokey", account_id: "secondary-account", account_key: "其他示例账户", account_name: "其他示例账户", company_name: "未连接的示例企业", kind: "current", display_name: "未连接的示例企业·当前数据", database_count: 12, encrypted_database_count: 12, wal_count: 0, key_available: false, active: false },
];
const mockIgnoredAccounts = new Set<string>();

function mockAccountSummaries(): AccountSummary[] {
  const groups = new Map<string, DatasetSummary[]>();
  for (const dataset of mockDatasets) {
    const accountKey = dataset.account_key ?? dataset.account_id;
    if (mockIgnoredAccounts.has(accountKey)) continue;
    const items = groups.get(accountKey) ?? [];
    items.push(dataset);
    groups.set(accountKey, items);
  }
  return [...groups.entries()].map(([account_id, items]) => {
    const preferred = items.find((item) => item.kind === "current") ?? items[0];
    return {
      account_id,
      account_name: preferred.account_name ?? "未命名账户",
      dataset_id: preferred.dataset_id,
      dataset_count: items.length,
      database_count: items.reduce((total, item) => total + item.database_count, 0),
      key_available: items.some((item) => item.key_available),
      current: account_id === "demo-account",
    };
  });
}
function mockScopedDatasets(): DatasetSummary[] {
  const selected = mockDatasets.find((item) => item.dataset_id === mockDatasetId);
  const accountKey = selected?.account_key ?? selected?.account_id;
  return selected && accountKey && !mockIgnoredAccounts.has(accountKey)
    ? mockDatasets.filter((item) => (item.account_key ?? item.account_id) === accountKey)
    : [];
}
let mockDatasetId = "preview-current";
let mockSnapshotId = "snap-20260912-0930";
let mockSnapshotCreatedAt = "2026-09-12T09:30:00Z";
let mockSnapshotAge = 12;
let mockSnapshotSeq = 0;
let mockRunSeq = 0;
let mockAgentSessionSeq = 0;
let mockAgentSessionKey = "0a1b2c3d4e5f6a71";
let mockMemoryEpoch = 1;
let mockMemoryCleared = false;
let mockAllowSend = false;



const compositeKey = (datasetId: string, sessionKey: string) => `${datasetId}:${sessionKey}`;

function mockSessionsFor(datasetId: string): SessionSummary[] {
  if (datasetId === "preview-current") {
    return [
      { session_key: "0a1b2c3d4e5f6a71", conversation_id: "0a1b2c3d4e5f6a71", dataset_id: datasetId, snapshot_id: mockSnapshotId, display_name: "示例联系人", kind: "单聊", last_message_time: "2026-09-12 10:24" },
      { session_key: "0a1b2c3d4e5f6a72", conversation_id: "0a1b2c3d4e5f6a72", dataset_id: datasetId, snapshot_id: mockSnapshotId, display_name: "示例项目群", kind: "群聊", last_message_time: "2026-09-12 09:58" },
      { session_key: "0a1b2c3d4e5f6a73", conversation_id: "0a1b2c3d4e5f6a73", dataset_id: datasetId, snapshot_id: mockSnapshotId, display_name: "产品讨论群", kind: "群聊", last_message_time: "2026-09-11 18:42" },
      { session_key: "0a1b2c3d4e5f6a74", conversation_id: "0a1b2c3d4e5f6a74", dataset_id: datasetId, snapshot_id: mockSnapshotId, display_name: "系统通知", kind: "系统", last_message_time: null },
    ];
  }
  if (datasetId === "preview-lab") {
    return [
      { session_key: "0b1b2c3d4e5f6b71", conversation_id: "0b1b2c3d4e5f6b71", dataset_id: datasetId, snapshot_id: mockSnapshotId, display_name: "另一位联系人", kind: "单聊", last_message_time: "2026-09-10 20:11" },
      { session_key: "0b1b2c3d4e5f6b72", conversation_id: "0b1b2c3d4e5f6b72", dataset_id: datasetId, snapshot_id: mockSnapshotId, display_name: "论文讨论群", kind: "群聊", last_message_time: "2026-09-10 15:03" },
    ];
  }
  return [];
}

interface MockConversation {
  conversation_id: string;
  session_key: string;
  session_name: string;
  dataset_id: string;
  snapshot_id: string;
  model: string;
  messages: AgentHistoryMessage[];
  updated_at: number;
}

const mockHistory: Record<string, MockConversation> = {
  "preview-current:0a1b2c3d4e5f6a71": {
    conversation_id: "preview-current:0a1b2c3d4e5f6a71", session_key: "0a1b2c3d4e5f6a71", session_name: "示例联系人",
    dataset_id: "preview-current", snapshot_id: mockSnapshotId, model: DEFAULT_AGENT_MODEL, updated_at: 1757750060000,
    messages: [
      { role: "user", text: "帮我概括一下最近的聊天", ts: 1757750000000 },
      { role: "assistant", text: "最近三条消息是课程安排、材料清单和一次会议时间确认。", ts: 1757750060000 },
    ],
  },
  "preview-current:0a1b2c3d4e5f6a72": {
    conversation_id: "preview-current:0a1b2c3d4e5f6a72", session_key: "0a1b2c3d4e5f6a72", session_name: "示例项目群",
    dataset_id: "preview-current", snapshot_id: mockSnapshotId, model: DEFAULT_AGENT_MODEL, updated_at: 1757660040000,
    messages: [
      { role: "user", text: "群里在讨论什么？", ts: 1757660000000 },
      { role: "assistant", text: "主要在讨论下周模型评测的排期。", ts: 1757660040000 },
    ],
  },
};

function mockSummaries(datasetId: string): AgentConversationSummary[] {
  return Object.values(mockHistory)
    .filter((item) => item.dataset_id === datasetId)
    .map((item) => {
      const last = item.messages[item.messages.length - 1];
      return {
        conversation_id: item.conversation_id,
        session_key: item.session_key,
        dataset_id: item.dataset_id,
        snapshot_id: item.snapshot_id,
        session_name: item.session_name,
        model: item.model,
        message_count: item.messages.length,
        preview: last?.text.slice(0, 80) ?? "",
        updated_at: item.updated_at,
      };
    });
}

/** 与 Rust `resolve_conversation_dir` 一致：复合键优先，其次唯一的裸 session_key。 */
function mockResolveConversation(root: string): { key: string; store: MockConversation } | null {
  const direct = mockHistory[root];
  if (direct) return { key: direct.conversation_id, store: direct };
  const matches = Object.entries(mockHistory).filter(([, item]) => item.session_key === root);
  if (matches.length !== 1) return null;
  const [key, store] = matches[0];
  return { key, store };
}

function mockEnsureConversation(datasetId: string, sessionKey: string, sessionName: string, model: string): { key: string; store: MockConversation } {
  const key = compositeKey(datasetId, sessionKey);
  const existing = mockHistory[key];
  if (existing) {
    existing.model = model;
    existing.session_name = sessionName || existing.session_name;
    return { key, store: existing };
  }
  const created: MockConversation = {
    conversation_id: key, session_key: sessionKey, session_name: sessionName,
    dataset_id: datasetId, snapshot_id: mockSnapshotId, model, messages: [], updated_at: Date.now(),
  };
  mockHistory[key] = created;
  return { key, store: created };
}

function mockEnsureAgentConversation(clientToken: string, model: string): MockConversation {
  const sessionKey = /^[0-9a-f]{16}$/.test(clientToken) ? clientToken : mockAgentSessionKey;
  return mockEnsureConversation(mockDatasetId, sessionKey, "主 Agent", model).store;
}

function mockWecomConversationId(datasetId: string, sessionKey: string): string {
  const session = mockSessionsFor(datasetId).find((item) => item.session_key === sessionKey);
  return session?.conversation_id ?? sessionKey;
}

const mockAgentHandlers = new Set<(payload: AgentEventPayload) => void>();
const mockGenerationHandlers = new Set<(payload: ContextGenerationPayload) => void>();

function emitMockAgent(payload: AgentEventPayload): void {
  for (const handler of [...mockAgentHandlers]) handler(payload);
}

function emitMockGeneration(payload: ContextGenerationPayload): void {
  for (const handler of [...mockGenerationHandlers]) handler(payload);
}

function mockPreviewStatus(): StatusData {
  const dataset = mockDatasets.find((item) => item.dataset_id === mockDatasetId);
  return {
    dataset: { available: true, database_count: dataset?.database_count ?? 19, encrypted_database_count: dataset?.encrypted_database_count ?? 19 },
    allow_send_to_wecom: mockAllowSend,
    key: { available: dataset?.key_available ?? false, validated_database_count: dataset?.key_available ? dataset.encrypted_database_count : 0 },
    snapshot: { available: true, created_at: mockSnapshotCreatedAt, age_minutes: mockSnapshotAge, active: true },
    selected_session: { available: false, display_name: null, kind: null },
    dataset_selection_required: false,
  };
}

function mockBootstrap(): unknown {
  const dataset = mockDatasets.find((item) => item.dataset_id === mockDatasetId) ?? null;
  const selectedAccount = mockAccountSummaries().find((item) => item.account_id === (dataset?.account_key ?? dataset?.account_id)) ?? null;
  const sessions = dataset ? mockSessionsFor(mockDatasetId).map((session) => ({ ...session, snapshot_id: mockSnapshotId })) : [];
  return {
    readiness: !dataset ? "needs_dataset" : dataset.key_available ? "ready" : "needs_key",
    dataset: dataset ? { dataset_id: dataset.dataset_id, account_id: dataset.account_id, company_name: dataset.company_name, display_name: dataset.display_name, kind: dataset.kind, key_available: dataset.key_available, active: dataset.active === true } : null,
    snapshot: dataset?.key_available
      ? { snapshot_id: mockSnapshotId, created_at: mockSnapshotCreatedAt, age_minutes: mockSnapshotAge, refreshed: false, degraded: false }
      : null,
    sessions,
    selection_reason: "kept",
    warnings: [],
    current_account: { account_id: "demo-account", account_name: "示例账户", dataset_id: "preview-current", recent_write_minutes: 2.4, confidence: "selected" },
    selected_account: selectedAccount,
    conversations: mockSummaries(mockDatasetId),
  };
}

/* ------------------------------------------------------------------ *
 * API
 * ------------------------------------------------------------------ */

export const api = {
  /* 工作台 */
  bootstrapWorkbench: async (): Promise<WorkbenchBootstrap> => {
    const raw = devPreview ? mockBootstrap() : await invoke<unknown>("bootstrap_workbench");
    return normalizeBootstrap(raw);
  },

  /* 会话 */

  /* 数据集与快照 */
  status: (): Promise<StatusData> => devPreview ? Promise.resolve(mockPreviewStatus()) : invoke<StatusData>("get_status"),
  datasets: async (): Promise<DatasetsData> => {
    if (!devPreview) return normalizeDatasets(await invoke<unknown>("discover_datasets"));
    const accounts = mockAccountSummaries();
    const datasets = mockScopedDatasets();
    const selectedAccount = accounts.find((item) => item.account_id === (datasets[0]?.account_key ?? datasets[0]?.account_id)) ?? null;
    return {
      count: datasets.length,
      selected_dataset_id: mockDatasetId,
      datasets,
      ignored: [],
      accounts,
      current_account: { account_id: "demo-account", account_name: "示例账户", dataset_id: "preview-current", recent_write_minutes: 2.4, confidence: "selected" },
      selected_account: selectedAccount,
    };
  },
  removeAccount: (accountId: string): Promise<RemoveAccountResult> => {
    if (!devPreview) return invoke<RemoveAccountResult>("remove_account", { accountId });
    const datasetIds = mockDatasets
      .filter((item) => (item.account_key ?? item.account_id) === accountId)
      .map((item) => item.dataset_id);
    mockIgnoredAccounts.add(accountId);
    if (datasetIds.includes(mockDatasetId)) mockDatasetId = "preview-current";
    const accounts = mockAccountSummaries();
    const datasets = mockScopedDatasets();
    return Promise.resolve({
      removed: true,
      account_id: accountId,
      dataset_ids: datasetIds,
      deleted_datasets: datasetIds.length,
      deleted_snapshots: 1,
      deleted_keys: 1,
      count: datasets.length,
      selected_dataset_id: mockDatasetId,
      datasets,
      ignored: [],
      accounts,
      current_account: { account_id: "demo-account", account_name: "示例账户", dataset_id: "preview-current", recent_write_minutes: 2.4, confidence: "selected" },
      selected_account: accounts.find((item) => item.account_id === (datasets[0]?.account_key ?? datasets[0]?.account_id)) ?? null,
    });
  },
  removeDataset: (datasetId: string): Promise<RemoveDatasetResult> => {
    if (devPreview) {
      return Promise.resolve({ removed: true, dataset_id: datasetId, deleted_snapshots: 1, deleted_keys: 1, removed_agents: 0, count: 0, selected_dataset_id: null, datasets: [], ignored: [] });
    }
    return invoke<RemoveDatasetResult>("remove_dataset", { datasetId });
  },
  restoreDataset: (datasetId: string): Promise<RemoveDatasetResult> => {
    if (devPreview) {
      return Promise.resolve({ restored: true, dataset_id: datasetId, count: mockDatasets.length, selected_dataset_id: mockDatasetId, datasets: mockDatasets, ignored: [] });
    }
    return invoke<RemoveDatasetResult>("restore_dataset", { datasetId });
  },
  selectDataset: (datasetId: string): Promise<unknown> => {
    if (devPreview) {
      mockDatasetId = datasetId;
      return Promise.resolve({ selected: true, dataset_id: datasetId });
    }
    return invoke("select_dataset", { datasetId });
  },
  refresh: (): Promise<unknown> => {
    if (!devPreview) return invoke("refresh_snapshot", { confirmed: true });
    mockSnapshotSeq += 1;
    mockSnapshotId = `snap-mock-${mockSnapshotSeq}`;
    mockSnapshotCreatedAt = new Date().toISOString();
    mockSnapshotAge = 0;
    const invalidated = Object.values(mockHistory).filter((item) => item.dataset_id === mockDatasetId).map((item) => item.conversation_id);
    window.setTimeout(() => emitMockGeneration({ dataset_id: mockDatasetId, snapshot_id: mockSnapshotId, invalidated }), 0);
    return Promise.resolve({ created: true, snapshot_id: mockSnapshotId, invalidated_session_keys: invalidated, preserved_session_keys: [], generation: { dataset_id: mockDatasetId, snapshot_id: mockSnapshotId } });
  },

  /* 本轮资料（多来源图文资料包） */
  previewContextPackage: (params: PreviewPackageParams): Promise<ContextPackageData> => {
    if (!devPreview) return invoke<ContextPackageData>("preview_context_package", { ...params });
    const includeImages = params.sources.some((source) => source.include_images !== false);
    const sources = params.sources.map((source) => ({
      source_id: `${source.dataset_id}:${source.session_key}`,
      dataset_id: source.dataset_id,
      snapshot_id: source.snapshot_id,
      session_key: source.session_key,
      conversation_id: source.session_key,
      display_name: mockSessionsFor(source.dataset_id).find((item) => item.session_key === source.session_key)?.display_name ?? source.session_key,
      kind: "单聊",
      snapshot_created_at: mockSnapshotCreatedAt,
      snapshot_age_minutes: mockSnapshotAge,
      stale: false,
      read_only_history: false,
      original_message_count: 2,
      retained_message_count: 2,
      image_count: includeImages ? 1 : 0,
      omitted_image_count: includeImages ? 1 : 2,
      estimated_tokens: 900,
      truncated: false,
      redactions: { email: 0, credential: 0, control: 0 },
    }));
    return Promise.resolve<ContextPackageData>({
      package_id: `pkg-mock-${++mockRunSeq}`,
      created_at: new Date().toISOString(),
      dir: "/tmp/mock-package",
      text: "（dev 预览）【本轮资料】…",
      sources,
      messages: [
        { message_id: "1", source_id: sources[0]?.source_id ?? "", time: "2026-09-12 10:20", sender: "示例联系人", type_name: "文本", content: "下周模型评测的排期已经更新，请大家查看。", image_ids: includeImages ? ["img_0001"] : [] },
        { message_id: "2", source_id: sources[0]?.source_id ?? "", time: "2026-09-12 10:24", sender: "你", type_name: "文本", content: "收到，我会在周五前补充实验结果。", image_ids: includeImages ? ["unusable_0001"] : [] },
      ],
      images: [
        {
          image_id: "img_0001",
          source_id: sources[0]?.source_id ?? "",
          message_id: "1",
          status: includeImages ? "thumbnail" : "missing",
          reason: includeImages ? "长截图分片 1/2（原图 900×4200）" : "本轮未勾选带入图片",
          path: includeImages ? "/tmp/mock-package/img_0001.jpg" : null,
          mime_type: includeImages ? "image/jpeg" : null,
          sha256: includeImages ? "0".repeat(64) : null,
          bytes: includeImages ? 102400 : null,
          width: includeImages ? 900 : null,
          height: includeImages ? 2048 : null,
          estimated_tokens: null,
        },
        {
          image_id: "unusable_0001",
          source_id: sources[0]?.source_id ?? "",
          message_id: "2",
          status: "missing",
          reason: "本地图片缓存中不存在该图片（未下载或已被清理）",
          path: null,
          mime_type: null,
          sha256: null,
          bytes: null,
          width: null,
          height: null,
          estimated_tokens: null,
        },
      ],
      warnings: [{ code: includeImages ? "IMAGE_UNAVAILABLE" : "IMAGES_DISABLED", message: includeImages ? "1 张图片本次未发送（缺失、格式不支持或超限）" : "本轮已关闭图片，不会发送图片载荷", source_id: sources[0]?.source_id ?? null, image_id: null }],
      stats: { source_count: sources.length, message_count: 2, image_count: includeImages ? 1 : 0, omitted_image_count: includeImages ? 1 : 2, image_bytes: includeImages ? 102400 : 0, estimated_tokens: 900, truncated: false },
    });
  },
  packageImageData: (packageId: string, imageId: string): Promise<PackageImageData> => {
    if (!devPreview) return invoke<PackageImageData>("package_image_data", { packageId, imageId });
    const png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==";
    return Promise.resolve({ image_id: imageId, mime_type: "image/png", bytes: 68, data_url: `data:image/png;base64,${png}` });
  },
  downloadImages: (
    datasetId: string,
    snapshotId: string,
    sessionKey: string,
    fetchViaClient = false,
  ): Promise<ImageDownloadResult> => {
    if (!devPreview) {
      return invoke<ImageDownloadResult>("download_images", {
        datasetId,
        snapshotId,
        sessionKey,
        fetchViaClient,
      });
    }
    return Promise.resolve({
      directory: "~/Downloads/WeCom Context/preview-current/示例联系人",
      session_name: "示例联系人",
      total_images: 2,
      downloaded: 2,
      skipped_existing: 0,
      omitted_count: 0,
      omitted: [],
      client_fetch_attempted: fetchViaClient,
      client_fetch_attempts: fetchViaClient ? 1 : 0,
      client_fetched: fetchViaClient ? 1 : 0,
      client_fetch_missing: 0,
      client_fetch_error_code: null,
      client_fetch_error: null,
    });
  },
  openAccessibilitySettings: (): Promise<void> => {
    if (devPreview) return Promise.resolve();
    return invoke<void>("open_accessibility_settings");
  },

  /* 记忆 */
  memoryContext: (): Promise<MemoryContextData> => devPreview
    ? Promise.resolve<MemoryContextData>({
        view: "context",
        memory_epoch: mockMemoryEpoch,
        state: "idle",
        model: "deepseek/deepseek-flash",
        thinking: "max",
        streaming: false,
        system_prompt: "（dev 预览）你是企业微信资料助手…",
        system_prompt_chars: 320,
        context_tokens: mockMemoryCleared ? 0 : 4321,
        context_percent: mockMemoryCleared ? 0 : 0.4,
        context_window: 1000000,
        messages: mockMemoryCleared ? [] : [
          { index: 0, role: "user", text: "（dev 预览）本轮资料：…", ts: 0, images: [{ image_id: "msg0_img0", status: "original", mime_type: null, bytes: null }] },
          { index: 1, role: "assistant", text: "（dev 预览）收到 4 张图片。", ts: 0, images: [] },
        ],
        compactions: mockMemoryCleared ? [] : [{ ts: 0, summary: "（dev 预览）早前讨论被压缩为摘要", tokens_after: 2100 }],
        images_in_context: mockMemoryCleared ? 0 : 1,
        updated_at: `${Date.now()}`,
      })
    : invoke<MemoryContextData>("memory_context"),
  memoryHistory: (offset = 0, limit = 50): Promise<MemoryHistoryData> => devPreview
    ? Promise.resolve<MemoryHistoryData>({
        view: "history",
        memory_epoch: mockMemoryEpoch,
        total: mockMemoryCleared ? 0 : 3,
        offset,
        limit,
        has_more: false,
        messages: mockMemoryCleared ? [] : [
          { index: 0, role: "user", text: "（dev 预览）第一轮问题", ts: 0, state: "complete", images: [], in_effective_context: false, summarized: true },
          { index: 1, role: "assistant", text: "（dev 预览）第一轮回答", ts: 0, state: "complete", images: [], in_effective_context: false, summarized: true },
          { index: 2, role: "user", text: "（dev 预览）最近一轮问题", ts: 0, state: "complete", images: [], in_effective_context: true, summarized: false },
        ],
        updated_at: `${Date.now()}`,
      })
    : invoke<MemoryHistoryData>("memory_history", { offset, limit }),
  memoryInjections: (offset = 0, limit = 20): Promise<MemoryInjectionsData> => devPreview
    ? Promise.resolve<MemoryInjectionsData>({
        view: "injections",
        memory_epoch: mockMemoryEpoch,
        total: mockMemoryCleared ? 0 : 2,
        offset,
        limit,
        has_more: false,
        entries: mockMemoryCleared ? [] : [
          { request_id: "req-mock-2", at: `${Date.now()}`, question: "（dev 预览）帮我看这两天的重点", package_id: "pkg-mock-2",
            sources: [{ source_id: "preview-current:0a1b2c3d4e5f6a71", dataset_id: "preview-current", session_key: "0a1b2c3d4e5f6a71", display_name: "示例联系人", kind: "单聊", snapshot_id: mockSnapshotId, snapshot_created_at: mockSnapshotCreatedAt, message_count: 20, image_count: 1 }],
            retained: { message_count: 20, image_count: 1 }, effectiveness: "active" },
          { request_id: "req-mock-1", at: `${Date.now() - 3600_000}`, question: "（dev 预览）更早一轮", package_id: "pkg-mock-1",
            sources: [{ source_id: "preview-current:0a1b2c3d4e5f6a71", dataset_id: "preview-current", session_key: "0a1b2c3d4e5f6a71", display_name: "示例联系人", kind: "单聊", snapshot_id: mockSnapshotId, snapshot_created_at: mockSnapshotCreatedAt, message_count: 12, image_count: 0 }],
            retained: { message_count: 12, image_count: 0 }, effectiveness: "summarized" },
        ],
        updated_at: `${Date.now()}`,
      })
    : invoke<MemoryInjectionsData>("memory_injections", { offset, limit }),
  /** 只读归档：旧联系人的本地对话记录。新流程不再用它当记忆，只用于查阅。 */
  agentHistory: (conversationId: string): Promise<AgentConversationHistory> => {
    if (!devPreview) return invoke<AgentConversationHistory>("agent_history", { conversationId });
    const resolved = mockResolveConversation(conversationId);
    if (!resolved) {
      return Promise.reject<AgentConversationHistory>({
        code: "SESSION_NOT_FOUND",
        message: "找不到该对话记录",
        retryable: false,
      });
    }
    const store = resolved.store;
    return Promise.resolve<AgentConversationHistory>({
      conversation_id: store.conversation_id,
      session_key: store.session_key,
      session_name: store.session_name,
      model: store.model,
      messages: store.messages,
      dataset_id: store.dataset_id,
      snapshot_id: store.snapshot_id,
    });
  },
  /** 清除服务端会话绑定（不影响主 Agent 记忆与源数据）。 */
  clearSession: (): Promise<unknown> => devPreview ? Promise.resolve({ cleared: true }) : invoke("clear_session"),

  memoryClear: (confirmed: boolean): Promise<MemoryClearData> => {
    if (!devPreview) return invoke<MemoryClearData>("memory_clear", { confirmed });
    mockMemoryCleared = true;
    return Promise.resolve<MemoryClearData>({
      view: "clear",
      cleared: true,
      previous_epoch: mockMemoryEpoch,
      memory_epoch: ++mockMemoryEpoch,
      removed: { session: true, messages: 3, injections: 2, packages: 1, images: 2 },
      kept: { datasets: true, snapshots: true, keys: true, providers: true, legacy_archive: true },
      started_at: `${Date.now()}`,
      finished_at: `${Date.now()}`,
    });
  },

  /* Agent（唯一主 Agent：与企微会话无关） */
  agentSendMessage: (params: AgentSendParams): Promise<AgentSendResult> => {
    if (!devPreview) return invoke<AgentSendResult>("agent_send_message", { ...params });
    mockMemoryCleared = false;
    const store = mockEnsureAgentConversation(params.clientToken, params.model);
    store.messages.push({ role: "user", text: params.text, ts: Date.now() });
    store.updated_at = Date.now();
    const runId = `mock-run-${++mockRunSeq}`;
    const requestId = `mock-request-${mockRunSeq}`;
    const route: AgentEventPayload = { conversation_id: params.clientToken, run_id: runId, request_id: requestId, memory_epoch: mockMemoryEpoch };
    const reply = `（dev 预览）已收到：${params.text.slice(0, 80)}`;
    window.setTimeout(() => emitMockAgent({ ...route, event: JSON.stringify({ type: "agent_start" }) }), 120);
    window.setTimeout(() => emitMockAgent({ ...route, event: JSON.stringify({ type: "message_start", message: { role: "assistant", content: [] } }) }), 260);
    window.setTimeout(() => {
      store.messages.push({ role: "assistant", text: reply, ts: Date.now() });
      store.updated_at = Date.now();
      emitMockAgent({ ...route, event: JSON.stringify({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text: reply }] } }) });
    }, 900);
    window.setTimeout(() => emitMockAgent({ ...route, event: JSON.stringify({ type: "agent_settled" }) }), 980);
    return Promise.resolve<AgentSendResult>({
      run_id: runId,
      request_id: requestId,
      started: true,
      memory_epoch: mockMemoryEpoch,
      accepted: true,
      package_id: params.sources?.length ? `pkg-mock-${mockRunSeq}` : null,
      image_count: params.sources?.some((source) => source.include_images !== false) && (params.imageOptions?.max_images ?? 8) > 0 ? 1 : 0,
    });
  },
  agentStatus: (): Promise<AgentStatusData> => devPreview
    ? Promise.resolve({
        state: "idle" as const,
        memory_epoch: mockMemoryEpoch,
        run_id: null,
        request_id: null,
        package_id: null,
        model: null,
        thinking: "max",
        pi_pid: null,
        session_ready: false,
        started_at: null,
        last_event_at: null,
        unfinished_clear_epoch: null,
        last_error: null,
        busy: false,
      })
    : invoke<AgentStatusData>("agent_status"),
  agentStop: (): Promise<unknown> => devPreview
    ? Promise.resolve({ stopped: true })
    : invoke("agent_stop"),

  agentNewConversation: (model: string): Promise<unknown> => {
    if (!devPreview) return invoke("agent_new_conversation", { model });
    mockAgentSessionSeq += 1;
    mockAgentSessionKey = `${(0x1000000000000000 + mockAgentSessionSeq).toString(16).slice(-16)}`;
    return Promise.resolve({ session_id: mockAgentSessionKey });
  },

  /* 上下文预览 */

  /* 权限与集成 */
  setSendPermission: (enabled: boolean): Promise<unknown> => {
    if (devPreview) { mockAllowSend = enabled; return Promise.resolve({ enabled }); }
    return invoke("set_send_permission", { enabled });
  },
  connector: (): Promise<{ installed: boolean; configurable: boolean }> => devPreview
    ? Promise.resolve({ installed: true, configurable: true })
    : invoke<{ installed: boolean; configurable: boolean }>("check_connector"),
  installConnector: (): Promise<unknown> => devPreview ? Promise.resolve({ installed: true }) : invoke("install_connector"),
  uninstallConnector: (): Promise<unknown> => devPreview ? Promise.resolve({ removed: true }) : invoke("uninstall_connector"),
  uninstallApplication: (): Promise<unknown> => devPreview
    ? Promise.resolve({ started: true })
    : invoke("uninstall_application"),
  piEnvironment: (): Promise<PiEnvironment> => devPreview
    ? Promise.resolve({ version: "0.85.1", auth_available: true, proxy_configured: true, proxy_reachable: true, transport: "sse" })
    : invoke<PiEnvironment>("check_pi_environment"),
  launchPi: (): Promise<unknown> => devPreview ? Promise.resolve({ started: true }) : invoke("launch_pi"),
  listAgentModels: async (): Promise<{ configured: boolean; models: AgentModelInfo[]; warning: string | null }> => {
    if (devPreview) return Promise.resolve({ configured: true, models: builtinModelCatalog(), warning: null });
    try {
      const raw = await invoke<unknown>("agent_list_models");
      const record = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
      const models = Array.isArray(record.models) ? (record.models as AgentModelInfo[]) : [];
      return {
        configured: record.configured === true,
        models,
        warning: typeof record.warning === "string" ? record.warning : null,
      };
    } catch (error) {
      const core = toCoreError(error);
      return { configured: false, models: [], warning: core.message };
    }
  },
  deepSeekStatus: (): Promise<DeepSeekStatus> => devPreview
    ? Promise.resolve({ configured: true, models: builtinModelCatalog(), models_json_path: "~/.pi/agent/models.json (dev 预览)" })
    : invoke<DeepSeekStatus>("deepseek_status"),
  configureDeepSeek: (apiKey: string): Promise<DeepSeekConfigureResult> => devPreview
    ? Promise.resolve({ configured: true, default_model: DEFAULT_AGENT_MODEL, models: builtinModelCatalog() })
    : invoke<DeepSeekConfigureResult>("deepseek_configure", { apiKey }),
  captureKey: (datasetId: string): Promise<{ captured: boolean; verified: boolean; already?: boolean; dataset_id: string; key_file?: string | null; message?: string; login_note?: string | null }> => {
    if (devPreview) return Promise.resolve({ captured: false, verified: true, already: true, dataset_id: datasetId, message: "该企业已有可用密钥，无需提取" });
    return invoke("capture_key", { datasetId });
  },
};


/* ------------------------------------------------------------------ *
 * 事件订阅（浏览器预览下由 mock 驱动）
 * ------------------------------------------------------------------ */

function subscribe<T>(event: string, mockHandlers: Set<(payload: T) => void>, handler: (payload: T) => void): () => void {
  if (devPreview) {
    mockHandlers.add(handler);
    return () => { mockHandlers.delete(handler); };
  }
  let unlisten: (() => void) | null = null;
  let released = false;
  try {
    void listen<T>(event, (message) => handler(message.payload)).then((fn) => {
      if (released) fn();
      else unlisten = fn;
    });
  } catch {
    /* 事件通道不可用时工作台仍可用，只是没有流式更新 */
  }
  return () => { released = true; unlisten?.(); };
}

export function onAgentEvent(handler: (payload: AgentEventPayload) => void): () => void {
  return subscribe("agent-event", mockAgentHandlers, handler);
}

export function onContextGenerationChanged(handler: (payload: ContextGenerationPayload) => void): () => void {
  return subscribe("context-generation-changed", mockGenerationHandlers, handler);
}
