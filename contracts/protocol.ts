export type ProtocolErrorCode =
  | "INVALID_REQUEST"
  | "CONFIG_INVALID"
  | "PYTHON_UNAVAILABLE"
  | "VAULT_CLI_UNAVAILABLE"
  | "DATASET_UNAVAILABLE"
  | "DATASET_NOT_FOUND"
  | "DATASET_CHANGED"
  | "KEY_UNAVAILABLE"
  | "KEY_PERMISSION_UNSAFE"
  | "KEY_DATASET_MISMATCH"
  | "KEY_DATASET_READ_BLOCKED"
  | "CAPTURE_FAILED"
  | "SNAPSHOT_UNAVAILABLE"
  | "SNAPSHOT_INVALID"
  | "SNAPSHOT_CHANGED"
  | "SESSION_NOT_FOUND"
  | "SESSION_NOT_ALLOWED"
  | "STALE_SESSION_MAP"
  | "SESSION_GONE"
  | "SEND_DISABLED"
  | "SEND_TARGET_UNSUPPORTED"
  | "REFRESH_LOCKED"
  | "REFRESH_CANCELLED"
  | "REFRESH_FAILED"
  | "OUTPUT_INVALID"
  | "CONTEXT_UNAVAILABLE"
  | "PACKAGE_EXPIRED"
  | "MODEL_IMAGE_UNSUPPORTED"
  | "AGENT_BUSY"
  | "AGENT_WRITE_FAILED"
  | "PI_UNAVAILABLE"
  | "SIDECAR_TIMEOUT"
  | "STALE_REQUEST"
  | "MEMORY_CLEARING"
  | "MEMORY_CLEAR_FAILED"
  | "INTERNAL_ERROR";

export interface ProtocolError {
  code: ProtocolErrorCode;
  message: string;
  retryable: boolean;
  details?: Record<string, unknown>;
}

export interface ProtocolSuccess<T> {
  protocol_version: "1";
  request_id: string;
  ok: true;
  data: T;
}

export interface ProtocolFailure {
  protocol_version: "1";
  request_id: string;
  ok: false;
  error: ProtocolError;
}

export type ProtocolResponse<T> = ProtocolSuccess<T> | ProtocolFailure;

export interface SessionSummary {
  session_key: string;
  display_name: string;
  kind: string;
  last_message_time: string | null;
  selected: boolean;
  conversation_id?: string;
  dataset_id?: string;
  snapshot_id?: string;
}

export interface SessionsData {
  count: number;
  sessions: SessionSummary[];
  dataset_id?: string;
  snapshot_id?: string;
  snapshot_created_at?: string;
}

export interface ReadContextData {
  content: string;
  details: {
    snapshot_created_at: string;
    snapshot_age_minutes: number;
    message_count: number;
    estimated_tokens: number;
    truncated: boolean;
    stale: boolean;
    session_name: string;
    original_message_count?: number;
    retained_message_count?: number;
    redactions?: { email: number; credential: number; control: number };
    history_snapshot_id?: string;
    read_only_history?: boolean;
  };
}

export type BindingMode = "active" | "history";
export type BindRecovery = "none" | "rebuilt_mapping" | "from_conversation_id" | "history_snapshot";
export type BootstrapReadiness = "ready" | "needs_dataset" | "needs_key" | "refresh_failed";
export type SelectionReason = "kept" | "auto_adopted" | "single_dataset" | "ambiguous" | "none";

export interface SessionBinding {
  session_key: string;
  conversation_id: string;
  dataset_id: string;
  snapshot_id: string;
  snapshot_path: string;
  mode: BindingMode;
  bound_at: string;
}

export interface BootstrapWarning {
  code: string;
  message: string;
}

export interface BootstrapDataset {
  dataset_id: string;
  display_name: string;
  kind: "current" | "backup" | "unknown";
  key_available: boolean;
  active: boolean;
}

export interface BootstrapSnapshot {
  snapshot_id: string;
  created_at: string;
  age_minutes: number;
  refreshed: boolean;
  degraded: boolean;
}

export interface BootstrapSession {
  session_key: string;
  conversation_id: string;
  display_name: string;
  kind: "单聊" | "群聊";
  last_message_time: string | null;
  dataset_id: string;
  snapshot_id: string;
}

export interface BootstrapData {
  readiness: BootstrapReadiness;
  dataset: BootstrapDataset | null;
  snapshot: BootstrapSnapshot | null;
  sessions: BootstrapSession[];
  selection_reason: SelectionReason;
  warnings: BootstrapWarning[];
}

export interface BindSessionData {
  bound: true;
  session_key: string;
  conversation_id: string;
  dataset_id: string;
  snapshot_id: string;
  mode: BindingMode;
  recovered: BindRecovery;
  history_snapshot_id: string | null;
  history_created_at: string | null;
}

export interface SelectSessionData extends BindSessionData {
  selected: true;
}

export interface RefreshData {
  created: boolean;
  decrypted_database_count: number;
  failed_database_count: number;
  active_snapshot_created_at: string;
  session_binding: "preserved" | "cleared" | "none";
  invalidated_session_keys: string[];
  preserved_session_keys: string[];
  generation: { dataset_id: string; snapshot_id: string };
}

/* ------------------------------------------------------------------ *
 * 升级契约：单 Agent + 多来源图文注入 + 记忆管理
 * 与 contracts/prepare-context.schema.json、agent-status.schema.json、
 * memory.schema.json 一一对应（schema 是权威，此处是类型镜像）。
 * ------------------------------------------------------------------ */

export type AgentRuntimeState =
  | "stopped"
  | "starting"
  | "idle"
  | "preparing"
  | "generating"
  | "clearing"
  | "failed";

export type SourceKind = "单聊" | "群聊";

/** 图片可用状态：只有 original / thumbnail 会真正进入模型请求。 */
export type ImageStatus = "original" | "thumbnail" | "missing" | "unsupported" | "too_large" | "failed";

/** 一轮交互的完成状态：incomplete = 生成被中断，不能当成完整回复。 */
export type InteractionState = "complete" | "incomplete" | "failed" | "cancelled";

/** 已注入资料在当前有效上下文中的处境；unknown 必须如实显示，不得假定仍被记住。 */
export type MemoryEffectiveness = "active" | "summarized" | "evicted" | "unknown";

export interface PrepareSourceRequest {
  dataset_id: string;
  snapshot_id: string;
  session_key: string;
  limit?: number;
  start?: string;
  end?: string;
  /** 默认 false：未显式要求时不读取图片资源 */
  include_images?: boolean;
}

export interface PrepareImageOptions {
  max_images?: number;
  max_total_bytes?: number;
  max_edge_pixels?: number;
}

export interface PrepareContextRequest {
  sources: PrepareSourceRequest[];
  image_options?: PrepareImageOptions;
  max_context_tokens?: number;
  max_message_characters?: number;
}

export interface PackageSource {
  /** `<dataset_id>:<session_key>` */
  source_id: string;
  dataset_id: string;
  snapshot_id: string;
  session_key: string;
  conversation_id: string;
  display_name: string;
  kind: SourceKind;
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

export interface PackageMessage {
  /** message_id + send_time 构成稳定身份，预算裁剪时必须保留 */
  message_id: string;
  source_id: string;
  time: string;
  send_time: number;
  sender: string;
  content_type: number;
  type_name: string;
  content: string;
  image_ids: string[];
  truncated: boolean;
}

export interface PackageImage {
  image_id: string;
  source_id: string;
  message_id: string;
  status: ImageStatus;
  /** status 非 original/thumbnail 时必填，说明不可用原因 */
  reason: string | null;
  /** 仅可用图片有值；路径必须位于资料包目录内 */
  path: string | null;
  mime_type: string | null;
  sha256: string | null;
  bytes: number | null;
  width: number | null;
  height: number | null;
  estimated_tokens: number | null;
}

export interface PackageWarning {
  code: string;
  message: string;
  source_id: string | null;
  image_id: string | null;
}

export interface PackageStats {
  source_count: number;
  message_count: number;
  image_count: number;
  omitted_image_count: number;
  image_bytes: number;
  estimated_tokens: number;
  truncated: boolean;
}

export interface PrepareContextData {
  package_id: string;
  created_at: string;
  /** 资料包目录：Rust 只读该目录内的图片，不接受前端传入的任意路径 */
  dir: string;
  /** 已脱敏、已裁剪的引用数据正文（含「【图片 N】」编号） */
  text: string;
  sources: PackageSource[];
  messages: PackageMessage[];
  /** 数组顺序即模型输入顺序，text 中的「【图片 N】」引用第 N 张 */
  images: PackageImage[];
  warnings: PackageWarning[];
  stats: PackageStats;
}

export interface AgentStatusData {
  state: AgentRuntimeState;
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
  /** 非空表示上次清空未完成，下次启动必须继续完成清空 */
  unfinished_clear_epoch: number | null;
  last_error: { code: string; message: string } | null;
}

export interface MemoryImageRef {
  image_id: string;
  status: ImageStatus;
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

export interface MemoryCompaction {
  ts: number;
  summary: string;
  tokens_after: number | null;
}

/** 当前有效上下文：运行时真正还在用的内容，不是模型内部参数或推理过程。 */
export interface MemoryContextData {
  view: "context";
  memory_epoch: number;
  state: AgentRuntimeState;
  model: string | null;
  thinking: string;
  streaming: boolean;
  system_prompt: string;
  system_prompt_chars: number;
  /** null = 运行时未提供统计，不得伪造精确值 */
  context_tokens: number | null;
  context_percent: number | null;
  context_window: number | null;
  messages: MemoryContextMessage[];
  compactions: MemoryCompaction[];
  images_in_context: number;
  updated_at: string;
}

export interface MemoryHistoryMessage {
  index: number;
  role: "user" | "assistant" | "system";
  text: string;
  ts: number;
  state: InteractionState;
  images: MemoryImageRef[];
  /** false = 已不在有效上下文（可能已被压缩） */
  in_effective_context: boolean;
  /** true = 只剩摘要，原图不再有效 */
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

export interface MemoryInjectionSource {
  source_id: string;
  dataset_id: string;
  session_key: string;
  display_name: string;
  kind: SourceKind;
  snapshot_id: string;
  snapshot_created_at: string;
  message_count: number;
  image_count: number;
}

export interface MemoryInjectionEntry {
  request_id: string;
  at: string;
  question: string;
  package_id: string | null;
  sources: MemoryInjectionSource[];
  retained: { message_count: number; image_count: number };
  effectiveness: MemoryEffectiveness;
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

/** 清空结果：kept 全为 true 表示企微源数据、快照、密钥与模型配置未被清空。 */
export interface MemoryClearData {
  view: "clear";
  cleared: boolean;
  previous_epoch: number;
  memory_epoch: number;
  removed: {
    session: boolean;
    messages: number;
    injections: number;
    packages: number;
    images: number;
  };
  kept: {
    datasets: true;
    snapshots: true;
    keys: true;
    providers: true;
    legacy_archive: true;
  };
  started_at: string;
  finished_at: string;
}

export type MemoryViewData =
  | MemoryContextData
  | MemoryHistoryData
  | MemoryInjectionsData
  | MemoryClearData;
