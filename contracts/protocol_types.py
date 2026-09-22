from __future__ import annotations

from typing import Literal, NotRequired, TypedDict, TypeVar

ProtocolErrorCode = Literal[
    "INVALID_REQUEST",
    "CONFIG_INVALID",
    "PYTHON_UNAVAILABLE",
    "VAULT_CLI_UNAVAILABLE",
    "DATASET_UNAVAILABLE",
    "DATASET_NOT_FOUND",
    "DATASET_CHANGED",
    "KEY_UNAVAILABLE",
    "KEY_PERMISSION_UNSAFE",
    "KEY_DATASET_MISMATCH",
    "KEY_DATASET_READ_BLOCKED",
    "CAPTURE_FAILED",
    "SNAPSHOT_UNAVAILABLE",
    "SNAPSHOT_INVALID",
    "SNAPSHOT_CHANGED",
    "SESSION_NOT_FOUND",
    "SESSION_NOT_ALLOWED",
    "STALE_SESSION_MAP",
    "SESSION_GONE",
    "SEND_DISABLED",
    "SEND_TARGET_UNSUPPORTED",
    "REFRESH_LOCKED",
    "REFRESH_CANCELLED",
    "REFRESH_FAILED",
    "OUTPUT_INVALID",
    "CONTEXT_UNAVAILABLE",
    "PACKAGE_EXPIRED",
    "MODEL_IMAGE_UNSUPPORTED",
    "AGENT_BUSY",
    "AGENT_WRITE_FAILED",
    "PI_UNAVAILABLE",
    "SIDECAR_TIMEOUT",
    "STALE_REQUEST",
    "MEMORY_CLEARING",
    "MEMORY_CLEAR_FAILED",
    "INTERNAL_ERROR",
]


class ProtocolError(TypedDict):
    code: ProtocolErrorCode
    message: str
    retryable: bool
    details: NotRequired[dict]


T = TypeVar("T")


class ProtocolSuccess(TypedDict):
    protocol_version: Literal["1"]
    request_id: str
    ok: Literal[True]
    data: T


class ProtocolFailure(TypedDict):
    protocol_version: Literal["1"]
    request_id: str
    ok: Literal[False]
    error: ProtocolError


class SessionSummary(TypedDict):
    session_key: str
    display_name: str
    kind: str
    last_message_time: str | None
    selected: bool
    conversation_id: NotRequired[str]
    dataset_id: NotRequired[str]
    snapshot_id: NotRequired[str]


BindingMode = Literal["active", "history"]
BindRecovery = Literal["none", "rebuilt_mapping", "from_conversation_id", "history_snapshot"]
BootstrapReadiness = Literal["ready", "needs_dataset", "needs_key", "refresh_failed"]
SelectionReason = Literal["kept", "auto_adopted", "single_dataset", "ambiguous", "none"]


class SessionBinding(TypedDict):
    session_key: str
    conversation_id: str
    dataset_id: str
    snapshot_id: str
    snapshot_path: str
    mode: BindingMode
    bound_at: str


class BootstrapWarning(TypedDict):
    code: str
    message: str


class BootstrapDataset(TypedDict):
    dataset_id: str
    display_name: str
    kind: Literal["current", "backup", "unknown"]
    key_available: bool
    active: bool


class BootstrapSnapshot(TypedDict):
    snapshot_id: str
    created_at: str
    age_minutes: float
    refreshed: bool
    degraded: bool


class BootstrapSession(TypedDict):
    session_key: str
    conversation_id: str
    display_name: str
    kind: Literal["单聊", "群聊"]
    last_message_time: str | None
    dataset_id: str
    snapshot_id: str


class BootstrapData(TypedDict):
    readiness: BootstrapReadiness
    dataset: BootstrapDataset | None
    snapshot: BootstrapSnapshot | None
    sessions: list[BootstrapSession]
    selection_reason: SelectionReason
    warnings: list[BootstrapWarning]


class BindSessionData(TypedDict):
    bound: Literal[True]
    session_key: str
    conversation_id: str
    dataset_id: str
    snapshot_id: str
    mode: BindingMode
    recovered: BindRecovery
    history_snapshot_id: str | None
    history_created_at: str | None


class SelectSessionData(BindSessionData):
    selected: Literal[True]


class RefreshGeneration(TypedDict):
    dataset_id: str
    snapshot_id: str


class RefreshData(TypedDict):
    created: bool
    decrypted_database_count: int
    failed_database_count: int
    active_snapshot_created_at: str
    session_binding: Literal["preserved", "cleared", "none"]
    invalidated_session_keys: list[str]
    preserved_session_keys: list[str]
    generation: RefreshGeneration


class PreviewDetails(TypedDict):
    session_name: str
    snapshot_created_at: str
    snapshot_age_minutes: float
    original_message_count: int
    retained_message_count: int
    estimated_tokens: int
    truncated: bool
    stale: bool
    redactions: dict[str, int]
    history_snapshot_id: NotRequired[str]
    read_only_history: NotRequired[bool]


# --------------------------------------------------------------------------- #
# 升级契约：单 Agent + 多来源图文注入 + 记忆管理
# 与 contracts/prepare-context.schema.json、agent-status.schema.json、
# memory.schema.json 一一对应（schema 是权威，此处是类型镜像）。
# --------------------------------------------------------------------------- #

AgentRuntimeState = Literal["stopped", "starting", "idle", "preparing", "generating", "clearing", "failed"]
SourceKind = Literal["单聊", "群聊"]
ImageStatus = Literal["original", "thumbnail", "missing", "unsupported", "too_large", "failed"]
InteractionState = Literal["complete", "incomplete", "failed", "cancelled"]
MemoryEffectiveness = Literal["active", "summarized", "evicted", "unknown"]


class PrepareSourceRequest(TypedDict):
    dataset_id: str
    snapshot_id: str
    session_key: str
    limit: NotRequired[int]
    start: NotRequired[str]
    end: NotRequired[str]
    include_images: NotRequired[bool]


class PrepareImageOptions(TypedDict):
    max_images: NotRequired[int]
    max_total_bytes: NotRequired[int]
    max_edge_pixels: NotRequired[int]


class PrepareContextRequest(TypedDict):
    sources: list[PrepareSourceRequest]
    image_options: NotRequired[PrepareImageOptions]
    max_context_tokens: NotRequired[int]
    max_message_characters: NotRequired[int]


class PackageSource(TypedDict):
    source_id: str
    dataset_id: str
    snapshot_id: str
    session_key: str
    conversation_id: str
    display_name: str
    kind: SourceKind
    snapshot_created_at: str
    snapshot_age_minutes: float
    stale: bool
    read_only_history: bool
    original_message_count: int
    retained_message_count: int
    image_count: int
    omitted_image_count: int
    estimated_tokens: int
    truncated: bool
    redactions: dict[str, int]


class PackageMessage(TypedDict):
    message_id: str
    source_id: str
    time: str
    send_time: int
    sender: str
    content_type: int
    type_name: str
    content: str
    image_ids: list[str]
    truncated: bool


class PackageImage(TypedDict):
    image_id: str
    source_id: str
    message_id: str
    status: ImageStatus
    reason: str | None
    path: str | None
    mime_type: str | None
    sha256: str | None
    bytes: int | None
    width: int | None
    height: int | None
    estimated_tokens: int | None


class PackageWarning(TypedDict):
    code: str
    message: str
    source_id: str | None
    image_id: str | None


class PackageStats(TypedDict):
    source_count: int
    message_count: int
    image_count: int
    omitted_image_count: int
    image_bytes: int
    estimated_tokens: int
    truncated: bool


class PrepareContextData(TypedDict):
    package_id: str
    created_at: str
    dir: str
    text: str
    sources: list[PackageSource]
    messages: list[PackageMessage]
    images: list[PackageImage]
    warnings: list[PackageWarning]
    stats: PackageStats


class AgentStatusData(TypedDict):
    state: AgentRuntimeState
    memory_epoch: int
    run_id: str | None
    request_id: str | None
    package_id: str | None
    model: str | None
    thinking: str
    pi_pid: int | None
    session_ready: bool
    started_at: str | None
    last_event_at: str | None
    unfinished_clear_epoch: int | None
    last_error: dict | None


class MemoryImageRef(TypedDict):
    image_id: str
    status: ImageStatus
    mime_type: str | None
    bytes: int | None


class MemoryContextMessage(TypedDict):
    index: int
    role: Literal["user", "assistant", "system"]
    text: str
    ts: int
    images: list[MemoryImageRef]


class MemoryCompaction(TypedDict):
    ts: int
    summary: str
    tokens_after: int | None


class MemoryContextData(TypedDict):
    view: Literal["context"]
    memory_epoch: int
    state: AgentRuntimeState
    model: str | None
    thinking: str
    streaming: bool
    system_prompt: str
    system_prompt_chars: int
    context_tokens: int | None
    context_percent: float | None
    context_window: int | None
    messages: list[MemoryContextMessage]
    compactions: list[MemoryCompaction]
    images_in_context: int
    updated_at: str


class MemoryHistoryMessage(TypedDict):
    index: int
    role: Literal["user", "assistant", "system"]
    text: str
    ts: int
    state: InteractionState
    images: list[MemoryImageRef]
    in_effective_context: bool
    summarized: bool


class MemoryHistoryData(TypedDict):
    view: Literal["history"]
    memory_epoch: int
    total: int
    offset: int
    limit: int
    has_more: bool
    messages: list[MemoryHistoryMessage]
    updated_at: str


class MemoryInjectionSource(TypedDict):
    source_id: str
    dataset_id: str
    session_key: str
    display_name: str
    kind: SourceKind
    snapshot_id: str
    snapshot_created_at: str
    message_count: int
    image_count: int


class MemoryInjectionEntry(TypedDict):
    request_id: str
    at: str
    question: str
    package_id: str | None
    sources: list[MemoryInjectionSource]
    retained: dict[str, int]
    effectiveness: MemoryEffectiveness


class MemoryInjectionsData(TypedDict):
    view: Literal["injections"]
    memory_epoch: int
    total: int
    offset: int
    limit: int
    has_more: bool
    entries: list[MemoryInjectionEntry]
    updated_at: str


class MemoryClearData(TypedDict):
    view: Literal["clear"]
    cleared: bool
    previous_epoch: int
    memory_epoch: int
    removed: dict[str, int | bool]
    kept: dict[str, Literal[True]]
    started_at: str
    finished_at: str


MemoryViewData = MemoryContextData | MemoryHistoryData | MemoryInjectionsData | MemoryClearData
