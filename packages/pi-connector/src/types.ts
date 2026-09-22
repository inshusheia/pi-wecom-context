export interface SessionSummary {
  session_key: string;
  conversation_id: string;
  display_name: string;
  kind: string;
  last_message_time: string | null;
  selected: boolean;
}

export interface SessionsData {
  count: number;
  sessions: SessionSummary[];
  dataset_id: string;
  snapshot_id: string;
  snapshot_created_at: string;
}

/** 工具结果 details（供 Pi 展示与后续轮次引用）。 */
export interface ReadWecomContextDetails {
  package_id: string;
  session_name: string;
  snapshot_created_at: string;
  snapshot_age_minutes: number;
  original_message_count: number;
  retained_message_count: number;
  message_count: number;
  estimated_tokens: number;
  truncated: boolean;
  stale: boolean;
  image_count: number;
  omitted_image_count: number;
  warnings: string[];
}

/** 旧版单会话文本视图的 details：read_context 仍在 sidecar 中保留，供诊断使用。 */
export interface ReadContextDetails {
  snapshot_created_at: string;
  snapshot_age_minutes: number;
  message_count: number;
  estimated_tokens: number;
  truncated: boolean;
  stale: boolean;
  session_name: string;
}

export interface ReadContextData {
  content: string;
  details: ReadContextDetails;
}

/** 本轮资料包（与 contracts/prepare-context.schema.json 对齐）。 */
export interface PackageImage {
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

export interface PackageSource {
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

export interface PackageWarning {
  code: string;
  message: string;
  source_id: string | null;
  image_id: string | null;
}

export interface PrepareContextData {
  package_id: string;
  created_at: string;
  dir: string;
  text: string;
  sources: PackageSource[];
  messages: { message_id: string; source_id: string; time: string; sender: string; type_name: string; content: string; image_ids: string[] }[];
  images: PackageImage[];
  warnings: PackageWarning[];
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

export interface RefreshData {
  created: boolean;
  decrypted_database_count: number;
  failed_database_count: number;
  active_snapshot_created_at: string;
  session_binding: "preserved" | "cleared" | "none";
  active_config_fields: boolean;
  invalidated_session_keys: string[];
  preserved_session_keys: string[];
  generation: { dataset_id: string; snapshot_id: string };
}
