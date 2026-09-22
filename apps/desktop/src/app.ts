import {
  AGENT_MODELS,
  DEFAULT_AGENT_MODEL,
  api,
  onAgentEvent,
  onContextGenerationChanged,
  toCoreError,
} from "./ipc";
import type {
  AccountIdentity,
  AccountSummary,
  AgentConversationSummary,
  AgentModelInfo,
  AgentSourceRequest,
  AgentStatusData,
  ContextPackageData,
  ContextPackageImage,
  CoreError,
  MemoryClearData,
  MemoryContextData,
  MemoryHistoryData,
  MemoryInjectionsData,
  DatasetSummary,
  PreviewData,
  SessionSummary,
  StatusData,
  WorkbenchBootstrap,
} from "./ipc";

const root = document.querySelector<HTMLDivElement>("#app");
if (!root) throw new Error("missing app root");
const appRoot = root;

const MODEL_KEY = "wecom-agent-model";
const THEME_KEY = "wecom-context-theme";

type Theme = "light" | "dark";
type Page = "workbench" | "datasets" | "settings";

function initialTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

interface AgentBubble {
  role: "user" | "assistant" | "system" | "error";
  text: string;
}
interface AssistantState {
  /** 唯一主 Agent 的共享会话；企微会话对象只描述资料来源。 */
  messages: AgentBubble[];
  draft: string;
  model: string;
  historyLoaded: boolean;
  status: AssistantStatus;
  thinking: { since: number; phase: "starting" | "thinking" } | null;
  runId: string | null;
  requestId: string | null;
  memoryEpoch: number;
  error: string | null;
}

type AssistantStatus = "stopped" | "starting" | "idle" | "preparing" | "generating" | "clearing" | "failed";

interface ConversationState {
  /** 资料来源的稳定键：`${datasetId}:${sessionKey}`；不是 Agent 身份。 */
  id: string;
  datasetId: string;
  /** 该资料所属快照；history 模式下是历史快照。 */
  snapshotId: string;
  sessionKey: string;
  /** 企微侧 conversation_id，仅用于资料读取和展示。 */
  conversationId: string;
  name: string;
  kind: string;
  source: "snapshot" | "history" | "both";
  availability: "current" | "history_only" | "gone";
}

/** 资料库中的联系人只描述可读来源，主助手运行态集中在 AssistantState。 */


const ASSISTANT_LIVE_STATUS: Record<AssistantStatus, boolean> = {
  stopped: false,
  starting: true,
  idle: true,
  preparing: true,
  generating: true,
  clearing: true,
  failed: false,
};
const ASSISTANT_BUSY_STATUS: Record<AssistantStatus, boolean> = {
  stopped: false,
  starting: true,
  idle: false,
  preparing: true,
  generating: true,
  clearing: true,
  failed: false,
};
const ASSISTANT_STATUS_TEXT: Record<AssistantStatus, string> = {
  stopped: "未启动",
  starting: "正在连接",
  idle: "空闲",
  preparing: "正在准备资料",
  generating: "正在生成",
  clearing: "正在清空记忆",
  failed: "上次失败",
};

const MAIN_AGENT_CLIENT_TOKEN = "main-assistant";
let assistantSubmissionSeq = 0;


interface DrawerState {
  open: boolean;
  loading: boolean;
  /** 预览指纹：选中的来源集合，用于判断抽屉里的资料包是否还对应当前选择 */
  cacheKey: string | null;
  /** 本轮资料包（多来源图文），替代旧的单会话上下文预览 */
  pkg: ContextPackageData | null;
  /** 图片缩略图：image_id → data URL，按需加载 */
  thumbs: Record<string, string>;
  error: string | null;
}

interface ImageViewerState {
  packageId: string;
  imageId: string;
  loading: boolean;
  dataUrl: string | null;
  error: string | null;
}
type SourceRange = "all" | "today" | "7d" | "custom";

interface SourceOptions {
  range: SourceRange;
  start: string;
  end: string;
  limit: number;
  includeImages: boolean;
  maxImages: number;
  maxTotalBytes: number;
  maxContextTokens: number;
  maxMessageCharacters: number;
}

interface SourcePackageOptions {
  imageOptions: { max_images: number; max_total_bytes: number; max_edge_pixels: number };
  maxContextTokens: number;
  maxMessageCharacters: number;
}

interface PendingSend {
  text: string;
  model: string;
  sources: AgentSourceRequest[];
  names: string[];
  datasetLabels: string[];
  packageKey: string;
  packageOptions: SourcePackageOptions;
  crossEnterprise: boolean;
  crossEnterpriseConfirmed: boolean;
}

const SOURCE_OPTIONS_KEY = "wecom.sourceOptions";
const RAIL_WIDTH_KEY = "wecom.railWidth";
const MIN_RAIL_WIDTH = 280;
const MAX_RAIL_WIDTH = 520;
const DEFAULT_RAIL_WIDTH = 320;

function clampRailWidth(value: number): number {
  return Math.min(MAX_RAIL_WIDTH, Math.max(MIN_RAIL_WIDTH, Math.round(value)));
}

function loadRailWidth(): number {
  try {
    const value = Number(localStorage.getItem(RAIL_WIDTH_KEY));
    return Number.isFinite(value) ? clampRailWidth(value) : DEFAULT_RAIL_WIDTH;
  } catch {
    return DEFAULT_RAIL_WIDTH;
  }
}

function saveRailWidth(): void {
  try {
    localStorage.setItem(RAIL_WIDTH_KEY, String(state.railWidth));
  } catch {
    /* 宽度是界面偏好，无法持久化时仍保持本次拖拽结果 */
  }
}

const DEFAULT_SOURCE_OPTIONS: SourceOptions = {
  range: "all",
  start: "",
  end: "",
  limit: 30,
  includeImages: true,
  maxImages: 8,
  maxTotalBytes: 8 * 1024 * 1024,
  maxContextTokens: 2500,
  maxMessageCharacters: 2000,
};

function loadSourceOptions(): SourceOptions {
  try {
    const parsed = JSON.parse(localStorage.getItem(SOURCE_OPTIONS_KEY) ?? "{}") as Record<string, unknown>;
    const range: SourceRange = parsed.range === "today" || parsed.range === "7d" || parsed.range === "custom" ? parsed.range : "all";
    const integer = (value: unknown, fallback: number, min: number, max: number): number => {
      const number = typeof value === "number" && Number.isFinite(value) ? Math.round(value) : fallback;
      return Math.min(max, Math.max(min, number));
    };
    return {
      range,
      start: typeof parsed.start === "string" ? parsed.start : "",
      end: typeof parsed.end === "string" ? parsed.end : "",
      limit: integer(parsed.limit, DEFAULT_SOURCE_OPTIONS.limit, 1, 500),
      includeImages: parsed.includeImages !== false,
      maxImages: integer(parsed.maxImages, DEFAULT_SOURCE_OPTIONS.maxImages, 0, 32),
      maxTotalBytes: integer(parsed.maxTotalBytes, DEFAULT_SOURCE_OPTIONS.maxTotalBytes, 0, 1 << 30),
      maxContextTokens: integer(parsed.maxContextTokens, DEFAULT_SOURCE_OPTIONS.maxContextTokens, 100, 60000),
      maxMessageCharacters: integer(parsed.maxMessageCharacters, DEFAULT_SOURCE_OPTIONS.maxMessageCharacters, 50, 8000),
    };
  } catch {
    return { ...DEFAULT_SOURCE_OPTIONS };
  }
}

function saveSourceOptions(): void {
  try {
    localStorage.setItem(SOURCE_OPTIONS_KEY, JSON.stringify(state.sourceOptions));
  } catch {
    /* 配置无法持久化时仍保证当前页面的来源草案可用 */
  }
}

type MemoryTab = "context" | "history" | "injections";

interface MemoryState {
  open: boolean;
  tab: MemoryTab;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  confirmingClear: boolean;
  /** 运行态快照：状态、模型、进程与记忆代次 */
  runtime: AgentStatusData | null;
  context: MemoryContextData | null;
  history: MemoryHistoryData | null;
  injections: MemoryInjectionsData | null;
  cleared: string | null;
}
interface EnterpriseState {
  open: boolean;
  mode: "accounts" | "companies";
  loading: boolean;
  datasets: DatasetSummary[];
  accounts: AccountSummary[];
  currentAccount: AccountIdentity | null;
  selectedAccount: AccountSummary | null;
  error: string | null;
}

interface ConfirmState {
  token: string;
  label: string;
}

interface DeepSeekEditorState {
  apiKey: string;
  error: string | null;
  busy: boolean;
}
interface RailItem {
  conv: ConversationState;
  recent: boolean;
  hasHistory: boolean;
  preview: string;
  time: string;
  updatedAt: number;
  lastMessageTime: string;
}

/** 资料库运行状态与主助手状态分离；这里仅说明切换不会打断助手。 */
const KIND_LABEL: Record<string, string> = { private: "单聊", group: "群聊", "单聊": "单聊", "群聊": "群聊" };
const CONVERSATIONAL_KINDS: Record<string, true> = { "单聊": true, "群聊": true };

/* ------------------------------------------------------------------ *
 * 思考计时：一次「发送 → 本轮结束」为一个区间
 * ------------------------------------------------------------------ */

let thinkingTicker: number | null = null;

/** 区间开始：所有发送都属于唯一主 Agent，不再绑定某个企微会话。 */
function armThinking(agent: AssistantState, phase: "starting" | "thinking"): void {
  if (agent.thinking === null) agent.thinking = { since: Date.now(), phase };
  else if (phase === "thinking") agent.thinking.phase = "thinking";
  syncThinkingLabels();
}

/** 区间结束：本轮完成、进程退出、停止、清空或失效都调用。 */
function endThinking(agent: AssistantState): void {
  agent.thinking = null;
}

function formatElapsed(ms: number): string {
  const seconds = Math.max(0, ms) / 1000;
  return seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${String(Math.floor(seconds % 60)).padStart(2, "0")} 秒`;
}

/** 只改文本与显隐，不重排 DOM：避免打断输入框焦点、草稿与滚动位置。 */
function syncThinkingLabels(): void {
  const turn = state.assistant.thinking;
  const armed = turn !== null && state.assistant.status !== "clearing";
  if (armed && thinkingTicker === null) thinkingTicker = window.setInterval(syncThinkingLabels, 100);
  if (!armed && thinkingTicker !== null) {
    window.clearInterval(thinkingTicker);
    thinkingTicker = null;
  }
  const meter = document.getElementById("thinking-meter");
  if (meter) meter.hidden = !armed;
  if (!armed || turn === null) return;
  const text = `${turn.phase === "thinking" ? "已思考" : "正在连接"} ${formatElapsed(Date.now() - turn.since)}`;
  for (const node of document.querySelectorAll<HTMLElement>("[data-thinking-text]")) node.textContent = text;
}

const CONFIRM_LABELS: Record<string, string> = {
  refresh: "更新聊天记录会重新读取本机企业微信聊天",
  "toggle-send": "切换写回企业微信权限",
  clear: "清除当前会话绑定",
  "reset-conversation": "重置当前对话并清除本机记忆",
  "install-connector": "安装或更新 Pi Connector",
  "uninstall-application": "卸载应用将删除本应用保存的聊天记录、密钥、快照、配置、对话和托管 Connector；不会删除企业微信原始聊天",
  "remove-account": "彻底删除该账户在本应用内的全部数据：本地聊天记录、对话和连接信息；企业微信原始聊天不会被删除",
  "remove-dataset": "清理该公司在本应用内的数据：本地聊天记录、对话和连接信息；企业微信原始聊天不会被删除",
};
/** 会改变会话数据的错误码：按码分支，绝不解析中文文案。 */
const GONE_CODES: Record<string, true> = { SESSION_GONE: true, SESSION_NOT_FOUND: true, SESSION_NOT_ALLOWED: true };
const GENERATION_CODES: Record<string, true> = { DATASET_CHANGED: true, SNAPSHOT_CHANGED: true };

const EMPTY_DRAWER: DrawerState = { open: false, loading: false, cacheKey: null, pkg: null, thumbs: {}, error: null };

/** 本轮选中的资料来源（会话 id 集合）。与「当前打开的会话」是两件事：
 *  打开只影响界面，勾选才决定下一轮注入什么。 */
const SOURCES_KEY = "wecom.selectedSources";
const SOURCE_SNAPSHOTS_KEY = "wecom.selectedSourceSnapshots";

function loadSelectedSources(): string[] {
  try {
    const raw = localStorage.getItem(SOURCES_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? [...new Set(parsed.filter((item): item is string => typeof item === "string"))].slice(0, 20)
      : [];
  } catch {
    return [];
  }
}

function loadSelectedSourceSnapshots(): Record<string, string> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(SOURCE_SNAPSHOTS_KEY) ?? "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).filter((entry): entry is [string, string] => typeof entry[0] === "string" && typeof entry[1] === "string"));
  } catch {
    return {};
  }
}

function saveSelectedSources(): void {
  try {
    localStorage.setItem(SOURCES_KEY, JSON.stringify(state.selectedSources));
    localStorage.setItem(SOURCE_SNAPSHOTS_KEY, JSON.stringify(state.selectedSourceSnapshots));
  } catch {
    /* 存储不可用时只影响下次启动的记忆，不影响本轮选择 */
  }
}
function clearRoundContextStorage(): void {
  try {
    for (const key of [SOURCES_KEY, SOURCE_SNAPSHOTS_KEY, SOURCE_OPTIONS_KEY]) localStorage.removeItem(key);
  } catch {
    /* 本轮资料状态不是普通对话的必要条件 */
  }
}

function localDate(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function resolvedSourceWindow(options: SourceOptions = state.sourceOptions): { start?: string; end?: string } {
  if (options.range === "custom") {
    const start = /^\d{4}-\d{2}-\d{2}$/.test(options.start) ? `${options.start}T00:00:00` : undefined;
    let end: string | undefined;
    if (/^\d{4}-\d{2}-\d{2}$/.test(options.end)) {
      const date = new Date(`${options.end}T00:00:00`);
      date.setDate(date.getDate() + 1);
      end = `${localDate(date)}T00:00:00`;
    }
    return { start, end };
  }
  if (options.range === "all") return {};
  const endDate = new Date();
  endDate.setHours(0, 0, 0, 0);
  endDate.setDate(endDate.getDate() + 1);
  const startDate = new Date(endDate);
  startDate.setDate(startDate.getDate() - (options.range === "today" ? 1 : 7));
  return { start: `${localDate(startDate)}T00:00:00`, end: `${localDate(endDate)}T00:00:00` };
}

function sourcesCacheKey(): string {
  return JSON.stringify({
    sources: selectedSourcesRequest(),
    options: state.sourceOptions,
  });
}

clearRoundContextStorage();

const state: {
  theme: Theme;
  page: Page;
  bootstrap: WorkbenchBootstrap | null;
  bootstrapLoading: boolean;
  sessions: Record<string, SessionSummary>;
  summaries: Record<string, AgentConversationSummary>;
  conversations: Record<string, ConversationState>;
  assistant: AssistantState;
  historyLoading: Record<string, true>;
  drawer: DrawerState;
  imageViewer: ImageViewerState | null;
  imageDownloads: Record<string, true>;
  memory: MemoryState;
  selectedSources: string[];
  selectedSourceSnapshots: Record<string, string>;
  sourceOptions: SourceOptions;
  pendingSend: PendingSend | null;
  enterprise: EnterpriseState;
  query: string;
  kind: "all" | "private" | "group";
  busy: string | null;
  error: CoreError | null;
  notice: string | null;
  accessibilityRequired: boolean;
  warningsDismissed: Record<string, true>;
  pendingConfirm: ConfirmState | null;
  epochs: { bootstrap: number; history: number; memory: number; preview: number };
  status: StatusData | null;
  railWidth: number;
  connectorInstalled: boolean | null;
  piEnvironment: { version: string; auth_available: boolean; api_key_available?: boolean; proxy_configured: boolean; proxy_reachable: boolean; transport: string } | null;
  deepseekConfigured: boolean;
  deepseekEditor: DeepSeekEditorState | null;
  ignoredDatasets: DatasetSummary[];
  modelCatalog: AgentModelInfo[] | null;
  modelsWarning: string | null;
} = {
  theme: initialTheme(),
  bootstrap: null,
  page: "workbench",
  bootstrapLoading: false,
  sessions: {},
  summaries: {},
  conversations: {},
  assistant: {
    messages: [],
    draft: "",
    model: initialModel(),
    historyLoaded: false,
    status: "stopped",
    thinking: null,
    runId: null,
    requestId: null,
    memoryEpoch: 1,
    error: null,
  },
  historyLoading: {},
  drawer: { ...EMPTY_DRAWER },
  imageViewer: null,
  imageDownloads: {},
  memory: { open: false, tab: "context", loading: false, loadingMore: false, error: null, confirmingClear: false, runtime: null, context: null, history: null, injections: null, cleared: null },
  selectedSources: loadSelectedSources(),
  selectedSourceSnapshots: loadSelectedSourceSnapshots(),
  enterprise: { open: false, mode: "accounts", loading: false, datasets: [], accounts: [], currentAccount: null, selectedAccount: null, error: null },
  sourceOptions: loadSourceOptions(),
  pendingSend: null,
  query: "",
  kind: "all",
  busy: null,
  error: null,
  notice: null,
  accessibilityRequired: false,
  warningsDismissed: {},
  pendingConfirm: null,
  railWidth: loadRailWidth(),
  epochs: { bootstrap: 0, history: 0, memory: 0, preview: 0 },
  status: null,
  connectorInstalled: null,
  piEnvironment: null,
  deepseekConfigured: false,
  deepseekEditor: null,
  ignoredDatasets: [],
  modelCatalog: null,
  modelsWarning: null,
};

/* ------------------------------------------------------------------ *
 * 基础工具
 * ------------------------------------------------------------------ */

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 当前可用模型：只接受 DeepSeek，模型目录由官方 API Key 配置后动态加载。 */
function knownModel(model: string): boolean {
  if (state.modelCatalog) {
    return state.modelCatalog.some((item) => item.provider === "deepseek" && item.id === model);
  }
  return AGENT_MODELS.some((group) => group.models.some((item) => `${group.provider}/${item}` === model));
}

/** 旧版本可能保存了其它供应商模型；升级后统一回到 DeepSeek。 */
function initialModel(): string {
  const stored = localStorage.getItem(MODEL_KEY);
  if (!stored || !stored.startsWith("deepseek/")) {
    localStorage.setItem(MODEL_KEY, DEFAULT_AGENT_MODEL);
    return DEFAULT_AGENT_MODEL;
  }
  return stored;
}

function formatStamp(ms: number): string {
  if (!ms) return "";
  const date = new Date(ms);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function compositeKey(datasetId: string | undefined, sessionKey: string): string {
  return `${datasetId ?? ""}:${sessionKey}`;
}

function warningKey(warning: { code: string; message: string }): string {
  return `${warning.code}|${warning.message}`;
}





function historyCandidateIds(error: CoreError): string[] {
  const raw = error.details?.history_candidates;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object" && typeof (item as { snapshot_id?: unknown }).snapshot_id === "string") return (item as { snapshot_id: string }).snapshot_id;
      return "";
    })
    .filter((item) => item.length > 0);
}

function setError(error: unknown): void {
  state.busy = null;
  state.error = toCoreError(error);
  render();
}

function syncText(): string {
  const bootstrap = state.bootstrap;
  if (!bootstrap) return "正在识别聊天记录…";
  if (state.bootstrapLoading || state.busy === "refresh" || state.busy === "switch") return "正在更新…";
  if (bootstrap.readiness === "refresh_failed") return "更新失败 · 重试";
  if (bootstrap.readiness === "needs_key") return "需要连接";
  if (!bootstrap.dataset) return "尚未选择";
  if (!bootstrap.snapshot) return "暂无记录";
  if (bootstrap.snapshot.degraded) return "使用上次记录";
  return `已更新 ${Math.max(0, Math.round(bootstrap.snapshot.age_minutes))} 分钟前`;
}

function banners(): string {
  const parts: string[] = [];
  if (state.pendingConfirm) {
    parts.push(`<div class="banner pending">再次点击以确认：${escapeHtml(state.pendingConfirm.label)}<button data-action="dismiss-pending" aria-label="取消确认">×</button></div>`);
  }
  if (state.notice) {
    const accessibilityAction = state.accessibilityRequired
      ? `<button data-action="open-accessibility-settings">打开隐私与安全性设置</button>`
      : "";
    parts.push(`<div class="banner notice">${escapeHtml(state.notice)}${accessibilityAction}<button data-action="dismiss-notice" aria-label="关闭">×</button></div>`);
  }
  if (state.error) {
    parts.push(`<div class="banner error"><span class="err-code">${escapeHtml(state.error.code)}</span><span>${escapeHtml(state.error.message)}</span><button data-action="dismiss-error" aria-label="关闭">×</button></div>`);
  }
  return parts.join("");
}

function loading(text = "加载中…"): string {
  return `<div class="loading"><span class="spinner"></span>${escapeHtml(text)}</div>`;
}

type ScrollSnapshot = { selector: string; index: number; top: number; left: number };

const PRESERVED_SCROLL_SELECTORS = ["#transcript", ".rail-list", ".chat-body", ".content", ".drawer-body", ".modal-body", ".image-viewer-body"];

function captureScrollPositions(): ScrollSnapshot[] {
  const snapshots: ScrollSnapshot[] = [];
  for (const selector of PRESERVED_SCROLL_SELECTORS) {
    document.querySelectorAll<HTMLElement>(selector).forEach((element, index) => {
      if (element.scrollHeight <= element.clientHeight && element.scrollWidth <= element.clientWidth) return;
      snapshots.push({ selector, index, top: element.scrollTop, left: element.scrollLeft });
    });
  }
  return snapshots;
}

function renderKeepingScrollPosition(): void {
  const snapshots = captureScrollPositions();
  render();
  requestAnimationFrame(() => {
    for (const snapshot of snapshots) {
      const element = document.querySelectorAll<HTMLElement>(snapshot.selector)[snapshot.index];
      if (!element) continue;
      element.scrollTop = snapshot.top;
      element.scrollLeft = snapshot.left;
    }
  });
}

/**
 * 两步确认（WKWebView 不实现 window.confirm，必须内联）。
 * 返回 true 表示这是第二次点击，可以执行。
 */
function requireConfirm(token: string, label: string): boolean {
  if (state.pendingConfirm?.token === token) {
    state.pendingConfirm = null;
    state.notice = null;
    renderKeepingScrollPosition();
    return true;
  }
  state.pendingConfirm = { token, label };
  state.notice = null;
  renderKeepingScrollPosition();
  return false;
}

/* ------------------------------------------------------------------ *
 * 列表模型
 * ------------------------------------------------------------------ */

function railItems(): RailItem[] {
  const query = state.query.trim().toLocaleLowerCase();
  const browsingDatasetId = state.bootstrap?.dataset?.dataset_id ?? "";
  const items: RailItem[] = [];
  for (const conv of Object.values(state.conversations)) {
    if (browsingDatasetId && conv.datasetId !== browsingDatasetId) continue;
    if (conv.availability === "gone") continue;
    if (state.kind !== "all" && conv.kind !== (state.kind === "private" ? "单聊" : "群聊")) continue;
    if (query && !conv.name.toLocaleLowerCase().includes(query)) continue;
    const summary = state.summaries[conv.id];
    const session = state.sessions[conv.id];
    const hasHistory = conv.source !== "snapshot" || (summary?.message_count ?? 0) > 0;
    const lastMessageTime = session?.last_message_time ?? "";
    items.push({
      conv,
      recent: hasHistory,
      hasHistory,
      preview: summary?.preview ?? "",
      time: summary?.updated_at ? formatStamp(summary.updated_at) : lastMessageTime,
      updatedAt: summary?.updated_at ?? 0,
      lastMessageTime,
    });
  }
  items.sort((left, right) => {
    if (left.recent !== right.recent) return left.recent ? -1 : 1;
    if (left.updatedAt !== right.updatedAt) return right.updatedAt - left.updatedAt;
    return left.lastMessageTime < right.lastMessageTime ? 1 : -1;
  });
  return items;
}

function railListHtml(): string {
  const items = railItems();
  if (!items.length) {
    return `<div class="rail-empty">${state.bootstrap?.dataset ? "没有匹配的聊天" : "正在读取聊天记录…"}</div>`;
  }
  let html = "";
  let group: "recent" | "snapshot" | null = null;
  for (const item of items) {
    const next = item.recent ? "recent" : "snapshot";
    if (next !== group) {
      html += `<div class="rail-group-title">${next === "recent" ? "已有聊天记录" : "企业微信聊天"}</div>`;
      group = next;
    }
    html += convRowHtml(item);
  }
  return html;
}

function convRowHtml(item: RailItem): string {
  const conv = item.conv;
  const secondary = item.preview || `${KIND_LABEL[conv.kind] || conv.kind || "其他"}${item.time ? ` · ${item.time}` : ""}`;
  const picked = state.selectedSources.includes(conv.id);
  const actionLabel = picked ? `移除 ${conv.name} 本次添加` : `添加 ${conv.name} 到本次提问`;
  const canDownload = conv.availability === "current" && Boolean(conv.snapshotId && conv.sessionKey);
  const downloading = state.imageDownloads[conv.id] === true;
  const downloadLabel = canDownload ? `下载 ${conv.name} 的全部图片（缺失时由企业微信补齐）` : "当前快照不可用，无法下载图片";
  return `<div class="conv-line ${picked ? "picked" : ""}">
    <div class="conv-row">
      <span class="avatar small">${escapeHtml(conv.name.slice(0, 1) || "会")}</span>
      <span class="conv-main"><strong>${escapeHtml(conv.name)}</strong><small>${escapeHtml(secondary)}</small></span>
      <span class="conv-side">
        ${conv.availability === "history_only" ? `<span class="status-pill neutral">只读历史</span>` : ""}
        <span class="conv-time">${escapeHtml(item.time)}</span>
        <button class="conv-download${downloading ? " busy" : ""}" type="button" data-action="download-images" data-conversation-id="${escapeHtml(conv.id)}" aria-label="${escapeHtml(downloadLabel)}" title="${escapeHtml(downloadLabel)}" ${canDownload && !downloading ? "" : "disabled"}>${downloading ? "下载中" : "图"}</button>
      </span>
      <button class="conv-pick" type="button" data-source-toggle="${escapeHtml(conv.id)}" aria-label="${escapeHtml(actionLabel)}" title="${escapeHtml(actionLabel)}">${picked ? "✓" : "+"}</button>
    </div>
  </div>`;
}

function renderRailList(): void {
  const container = document.getElementById("rail-list");
  if (container) container.innerHTML = railListHtml();
}

async function downloadConversationImages(conversationId: string): Promise<void> {
  const conversation = state.conversations[conversationId];
  if (!conversation || conversation.availability !== "current") return;
  if (state.imageDownloads[conversationId]) return;
  state.imageDownloads[conversationId] = true;
  state.notice = null;
  state.error = null;
  state.accessibilityRequired = false;
  render();
  try {
    const result = await api.downloadImages(
      conversation.datasetId,
      conversation.snapshotId,
      conversation.sessionKey,
      true,
    );
    state.accessibilityRequired = result.client_fetch_error_code === "CLIENT_ACCESSIBILITY_REQUIRED" || result.client_fetch_error_code === "CLIENT_AUTOMATION_REQUIRED";
    const omitted = result.omitted_count ? `，${result.omitted_count} 张无法从本地缓存恢复` : "";
    const repeated = result.skipped_existing ? `，${result.skipped_existing} 张已存在` : "";
    const clientFetched = result.client_fetched ? `，企业微信客户端补齐 ${result.client_fetched} 张` : "";
    const clientMissing = result.client_fetch_missing ? `，客户端仍缺失 ${result.client_fetch_missing} 张` : "";
    const clientAttempts = result.client_fetch_attempts > 1 ? `，客户端已重试 ${result.client_fetch_attempts} 次` : "";
    const clientError = result.client_fetch_error ? `，客户端补齐失败：${result.client_fetch_error}` : "";
    state.notice = `「${conversation.name}」已下载 ${result.downloaded} / ${result.total_images} 张图片${clientFetched}${repeated}${omitted}${clientMissing}${clientAttempts}${clientError}。保存位置：${result.directory}`;
  } catch (error) {
    setError(error);
  } finally {
    delete state.imageDownloads[conversationId];
    render();
  }
}

/* ------------------------------------------------------------------ *
 * 左栏
 * ------------------------------------------------------------------ */

/** 左栏列表是本轮上下文选择器；它不打开历史对话。 */
function selectionBarHtml(): string {
  const count = state.selectedSources.length;
  if (!count) {
    return `<div class="source-bar empty"><span>本次不添加新聊天，继续当前对话</span></div>`;
  }
  const names = selectedSourceConversations().map((conv) => conv.name).slice(0, 3).join("、");
  return `<div class="source-bar">
    <span>本次添加 ${count} 个聊天：${escapeHtml(names)}${count > 3 ? "…" : ""}</span>
    <span class="source-bar-actions">
      <button class="button ghost small" data-action="preview-sources">调整本次资料</button>
      <button class="button ghost small" data-action="clear-sources">移除全部</button>
    </span>
  </div>`;
}

/** 勾选的来源会话（按选择顺序去重，缺失的会话自动跳过）。 */
function selectedSourceConversations(): ConversationState[] {
  return state.selectedSources
    .map((id) => state.conversations[id])
    .filter((conv): conv is ConversationState => Boolean(conv));
}
function sourceDatasetLabel(conv: ConversationState): string {
  if (conv.datasetId === state.bootstrap?.dataset?.dataset_id) return state.bootstrap?.dataset?.display_name ?? "当前企业";
  return state.enterprise.datasets.find((dataset) => dataset.dataset_id === conv.datasetId)?.display_name ?? conv.datasetId.slice(0, 8);
}

function sourceDraftHtml(): string {
  const sources = selectedSourceConversations();
  if (!sources.length) {
    return `<div class="source-draft empty"><span>未添加新聊天；助手会继续参考当前对话</span><button class="button ghost small" type="button" data-action="open-drawer">添加聊天</button></div>`;
  }
  const chips = sources.map((conv) => {
    const crossEnterprise = crossEnterpriseLabel(conv);
    const label = crossEnterprise ? `${sourceDatasetLabel(conv)} · ${conv.name}` : conv.name;
    const stale = sourceIsStale(conv);
    const displayLabel = `${label}${stale ? " · 需要更新" : ""}`;
    return `<button class="source-chip ${stale ? "stale" : ""}" type="button" data-source-remove="${escapeHtml(conv.id)}" aria-label="移除本次添加的聊天 ${escapeHtml(label)}" title="移除 ${escapeHtml(label)}"><span>${escapeHtml(displayLabel)}</span><b aria-hidden="true">×</b></button>`;
  }).join("");
  return `<div class="source-draft"><span class="source-draft-label">本次添加</span><div class="source-chips">${chips}</div><button class="button ghost small" type="button" data-action="open-drawer">调整范围</button></div>`;
}

function crossEnterpriseLabel(conv: ConversationState): boolean {
  return selectedSourceConversations().some((item) => item.datasetId !== conv.datasetId);
}

function renderAppNav(): string {
  const items: Array<{ page: Page; icon: string; label: string; hint: string }> = [
    { page: "workbench", icon: "⌂", label: "工作台", hint: "提问与选择聊天" },
    { page: "datasets", icon: "▦", label: "聊天记录", hint: "企业与聊天记录" },
    { page: "settings", icon: "⚙", label: "设置", hint: "连接与模型" },
  ];
  const dataset = state.bootstrap?.dataset;
  const sync = syncText();
  const tone = state.error || state.bootstrap?.readiness === "refresh_failed" ? "bad" : state.busy || state.bootstrapLoading ? "warn" : "ok";
  return `<aside class="app-nav" aria-label="应用导航">
    <button class="app-nav-brand" data-page="workbench" aria-label="返回工作台" title="WeCom Context">
      <span class="brand-mark">W</span>
    </button>
    <div class="app-nav-divider"></div>
    <nav class="app-nav-main" aria-label="主要页面">
      ${items.map((item) => `<button class="app-nav-item ${state.page === item.page ? "active" : ""}" data-page="${item.page}" title="${item.hint}" aria-label="${item.label}">
        <span class="app-nav-icon" aria-hidden="true">${item.icon}</span>
        <span>${item.label}</span>
      </button>`).join("")}
    </nav>
    <div class="app-nav-spacer"></div>
    <div class="app-nav-status">
      <button class="app-nav-status-button" data-action="open-enterprise" title="切换企业和聊天记录">
        <span class="app-nav-status-dot ${tone}" aria-hidden="true"></span>
        <span>${escapeHtml(dataset?.display_name ?? "未选择企业")}</span>
      </button>
      <button class="app-nav-item app-nav-item-compact" data-action="open-memory" title="查看本对话记录" aria-label="对话记录">
        <span class="app-nav-icon" aria-hidden="true">◌</span>
        <span>对话记录</span>
      </button>
      <button class="app-nav-item app-nav-item-compact" data-action="toggle-theme" title="${state.theme === "dark" ? "切换浅色主题" : "切换深色主题"}" aria-label="${state.theme === "dark" ? "浅色主题" : "深色主题"}">
        <span class="app-nav-icon" aria-hidden="true">${state.theme === "dark" ? "☀" : "☾"}</span>
        <span>${state.theme === "dark" ? "浅色" : "深色"}</span>
      </button>
    </div>
  </aside>`;
}

function pageTitle(): string {
  if (state.page === "datasets") return "企业与聊天记录";
  if (state.page === "settings") return "连接与设置";
  return "对话助手";
}

function pageSubtitle(): string {
  if (state.page === "datasets") return "管理企业和本机保存的聊天记录";
  if (state.page === "settings") return "模型、连接器与本地权限";
  return "选择聊天后提问，也可以继续当前对话";
}

function renderRail(): string {
  const name = state.bootstrap?.dataset?.display_name ?? "未选择企业";
  const snapshot = state.bootstrap?.snapshot?.snapshot_id;
  const sync = syncText();
  const tone = state.error || state.bootstrap?.readiness === "refresh_failed" ? "bad" : state.busy || state.bootstrapLoading ? "warn" : "ok";
  const syncAction = state.bootstrap?.readiness === "refresh_failed" ? "retry-refresh" : "open-enterprise";
  return `<aside class="rail">
    <div class="rail-head">
      <div class="rail-kicker">当前聊天记录</div>
      <button class="rail-enterprise" data-action="open-enterprise">${escapeHtml(name)}</button>
      <div class="rail-meta"><span>${escapeHtml(state.bootstrap?.selected_account?.account_name ?? state.enterprise.selectedAccount?.account_name ?? "未选择账户")}</span></div>
      <div class="rail-meta">
        <button class="rail-sync ${tone}" data-action="${syncAction}"><span class="row-dot ${tone === "bad" ? "error" : tone === "warn" ? "thinking" : "ready"}"></span>${escapeHtml(sync)}</button>
        <span class="rail-snapshot">${snapshot ? "聊天记录已就绪" : "暂无聊天记录"}</span>
      </div>
    </div>
    <div class="rail-tools">
      <label class="search"><span>⌕</span><input data-search placeholder="搜索联系人或群聊" value="${escapeHtml(state.query)}"></label>
      <div class="segmented">
        <button class="segment ${state.kind === "all" ? "active" : ""}" data-kind="all">全部</button>
        <button class="segment ${state.kind === "private" ? "active" : ""}" data-kind="private">单聊</button>
        <button class="segment ${state.kind === "group" ? "active" : ""}" data-kind="group">群聊</button>
      </div>
    </div>
    <div class="rail-list" id="rail-list">${railListHtml()}</div>
    <div class="rail-foot">
      <button class="rail-foot-row" data-action="open-enterprise"><span class="dot ${tone === "bad" ? "bad" : "ok"}"></span><span>${escapeHtml(name)} · ${escapeHtml(sync)}</span></button>
      <button class="rail-foot-row" data-action="open-memory"><span aria-hidden="true">◌</span><span>对话记录</span></button>
      <button class="rail-foot-row" data-page="settings"><span aria-hidden="true">⚙</span><span>设置</span></button>
    </div>
  </aside>`;
}


/* ------------------------------------------------------------------ *
 * 聊天区
 * ------------------------------------------------------------------ */

/** 旧版工具调用遗留的标记文本：显示为已忽略，而不是把乱码当回复展示。 */
const TOOL_MARKUP = /<\|\s*\|?\s*DSML|invoke\s+name=|invoke\s+name="|bash">/;

/** 思考中的占位气泡：动态点 + 已思考时长，由 syncThinkingLabels 就地刷新文本。 */
const THINKING_BUBBLE_HTML = `<div class="agent-bubble assistant thinking"><span class="agent-bubble-role">助手</span><span class="thinking-line"><span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span><span data-thinking-text></span></span></div>`;

function renderMarkdown(text: string): string {
  const lines = escapeHtml(text).split("\n");
  const output: string[] = [];
  let inCode = false;
  let inList = false;
  const closeList = () => {
    if (inList) {
      output.push("</ul>");
      inList = false;
    }
  };
  const inline = (value: string): string => value
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      closeList();
      if (inCode) output.push("</code></pre>");
      else output.push("<pre><code>");
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      output.push(`${line}\n`);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    const listItem = line.match(/^\s*[-*]\s+(.+)$/);
    if (listItem) {
      if (!inList) {
        output.push("<ul>");
        inList = true;
      }
      output.push(`<li>${inline(listItem[1])}</li>`);
      continue;
    }
    closeList();
    if (!line.trim()) continue;
    output.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  if (inCode) output.push("</code></pre>");
  return output.join("");
}

function renderBubble(bubble: AgentBubble, thinking = false): string {
  const role = bubble.role;
  if (thinking) return THINKING_BUBBLE_HTML;
  const label = role === "user" ? "你" : role === "assistant" ? "助手" : role === "error" ? "错误" : "系统";
  if (role === "assistant" && TOOL_MARKUP.test(bubble.text)) {
    return `<div class="agent-bubble system"><span class="agent-bubble-role">系统</span><div>旧版输出包含工具调用标记，已忽略（当前版本无工具，直接基于上下文回复）。</div></div>`;
  }
  return `<div class="agent-bubble ${role}"><span class="agent-bubble-role">${label}</span><div class="${role === "assistant" ? "markdown" : ""}">${role === "assistant" ? renderMarkdown(bubble.text) : escapeHtml(bubble.text)}</div></div>`;
}

function transcriptHtml(): string {
  const agent = state.assistant;
  if (!agent.messages.length) {
    if (state.historyLoading["main-agent"]) return loading("正在恢复对话记录…");
    if (agent.thinking !== null) return THINKING_BUBBLE_HTML;
    return `<div class="empty-state compact"><strong>可以开始提问</strong><span>直接输入问题；需要参考企业微信聊天时，先从左侧添加聊天。</span></div>`;
  }
  const last = agent.messages[agent.messages.length - 1];
  const pendingAssistant = agent.thinking !== null && last.role === "assistant" && !last.text;
  const html = agent.messages
    .map((bubble, index) => renderBubble(bubble, pendingAssistant && index === agent.messages.length - 1))
    .join("");
  return agent.thinking !== null && !pendingAssistant ? html + THINKING_BUBBLE_HTML : html;
}

function modelOptionsHtml(model: string): string {
  if (!state.deepseekConfigured) {
    return `<option value="" selected disabled>请先连接 AI</option>`;
  }
  const catalog = (state.modelCatalog ?? []).filter((item) => item.provider === "deepseek");
  if (!catalog.length) {
    return `<option value="" selected disabled>正在加载 DeepSeek 模型…</option>`;
  }
  const options = catalog.map((item) => {
    const label = item.images ? `${item.name || item.model}（支持图片）` : item.name || item.model;
    return `<option value="${escapeHtml(item.id)}" ${item.id === model ? "selected" : ""}>${escapeHtml(label)}</option>`;
  }).join("");
  if (model && !knownModel(model)) {
    return `<option value="${escapeHtml(model)}" selected disabled>${escapeHtml(model)}（不可用）</option>${options}`;
  }
  return options;
}

function accountBannerHtml(): string {
  const selected = state.bootstrap?.selected_account ?? state.enterprise.selectedAccount;
  const current = state.bootstrap?.current_account ?? state.enterprise.currentAccount;
  if (!selected && !current) {
    return `<div class="chat-account-banner warn"><strong>尚未选择企业微信账户</strong><span>进入应用时请选择账户；之后只显示该账户对应的公司数据集。</span><button class="button ghost small" data-action="open-enterprise">选择账户</button></div>`;
  }
  const selectedLabel = selected ? selected.account_name : "未选择";
  const currentLabel = current ? current.account_name : "无法确认";
  return `<div class="chat-account-banner"><div><strong>当前使用账户</strong><span>${escapeHtml(selectedLabel)}</span></div><div class="chat-account-live"><small>企业微信当前登录</small><span>${escapeHtml(currentLabel)}</span></div><button class="button ghost small" data-action="open-enterprise">切换账户</button></div>`;
}

function chatHeadHtml(): string {
  const agent = state.assistant;
  const stopVisible = agent.status === "starting" || agent.status === "preparing" || agent.status === "generating";
  return `<header class="chat-head" id="chat-head">
    <div class="chat-head-main">
      <strong>对话助手</strong>
      <span class="chat-sub"><span>可以继续追问；本对话已使用的资料会保留</span><span id="chat-status-text">${escapeHtml(ASSISTANT_STATUS_TEXT[agent.status])}</span><span class="thinking-meter" id="thinking-meter" ${agent.thinking !== null ? "" : "hidden"}><span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span><span data-thinking-text></span></span></span>
    </div>
    <div class="chat-actions">
      <span class="row-dot ${agent.status === "failed" ? "error" : agent.status === "idle" ? "ready" : agent.status === "stopped" ? "stale" : "thinking"}" id="chat-status" aria-label="${escapeHtml(ASSISTANT_STATUS_TEXT[agent.status])}"></span>
      <label class="model-field">模型<select data-model aria-label="选择 DeepSeek 模型">${modelOptionsHtml(knownModel(agent.model) ? agent.model : DEFAULT_AGENT_MODEL)}</select></label>
      <button class="button ghost small" data-action="open-deepseek-config">${state.deepseekConfigured ? "更新 AI 设置" : "连接 AI"}</button>
      <button class="button ghost" data-action="new-conversation" ${agent.status === "clearing" ? "disabled" : ""}>新对话</button>
      <button class="button danger" id="chat-stop" data-action="stop-agent" ${stopVisible ? "" : "hidden"}>停止</button>
    </div>
  </header>${accountBannerHtml()}`;
}

const QUICK_TASKS: Record<string, { label: string; prompt: string }> = {
  summary: {
    label: "总结聊天",
    prompt: "请总结已提供的企业微信聊天，提炼关键结论、分歧和下一步行动。",
  },
  todos: {
    label: "提取待办",
    prompt: "请从已提供的企业微信聊天中提取待办事项，按负责人、截止时间和状态整理；未知信息标为待确认。",
  },
  reply: {
    label: "起草回复",
    prompt: "请根据已提供的企业微信聊天起草一份合适的回复，只给出可编辑草稿，不要发送或写回企业微信。",
  },
};

function quickTasksHtml(): string {
  const available = selectedSourceConversations().length > 0 || state.assistant.messages.length > 0;
  const buttons = Object.entries(QUICK_TASKS)
    .map(([task, item]) => `<button class="quick-task" type="button" data-action="quick-task" data-task="${task}" ${available ? "" : "disabled"}>${item.label}</button>`)
    .join("");
  return `<div class="quick-tasks"><span class="quick-tasks-label">常用任务</span>${buttons}${available ? "" : '<span class="quick-tasks-hint">先添加聊天记录</span>'}</div>`;
}

function applyQuickTask(task: string): void {
  const item = QUICK_TASKS[task];
  if (!item || ASSISTANT_BUSY_STATUS[state.assistant.status] || state.assistant.status === "clearing") return;
  state.assistant.draft = item.prompt;
  state.notice = "已填入问题，确认内容后再发送";
  render();
  requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>("[data-draft]")?.focus());
}

function composerHtml(): string {
  const agent = state.assistant;
  const locked = state.busy === "switch" || agent.status === "clearing";
  const sendDisabled = locked || !state.deepseekConfigured || ASSISTANT_BUSY_STATUS[agent.status] || !agent.draft.trim();
  const hint = state.deepseekConfigured
    ? "直接发送问题；已添加的聊天会作为本次参考资料"
    : "请先点击上方「连接 AI」并输入 API Key";
  return `<form class="composer" id="composer">
    ${sourceDraftHtml()}
    ${quickTasksHtml()}
    <div class="composer-main"><textarea data-draft rows="3" placeholder="输入问题，Enter 发送（Shift+Enter 换行）…" aria-label="发送给对话助手的问题" ${locked ? "disabled" : ""}>${escapeHtml(agent.draft)}</textarea><span class="composer-hint">${hint}</span></div>
    <button class="button primary" type="submit" ${sendDisabled ? "disabled" : ""}>发送</button>
  </form>`;
}

function datasetRowHtml(dataset: DatasetSummary): string {
  const selected = state.bootstrap?.dataset?.dataset_id === dataset.dataset_id;
  const pending = state.pendingConfirm?.token === `dataset:${dataset.dataset_id}`;
  const capturePending = state.pendingConfirm?.token === `capture-key:${dataset.dataset_id}`;
  const tags = [
    `<span class="status-pill ${dataset.key_available ? "ok" : "warn"}">${dataset.key_available ? "已连接" : "需要连接"}</span>`,
    dataset.active ? `<span class="status-pill neutral">当前登录</span>` : "",
    selected ? `<span class="status-pill ok">当前使用</span>` : "",
    pending ? `<span class="status-pill warn">再次点击确认</span>` : "",
  ].join("");
  const removePending = state.pendingConfirm?.token === `remove-dataset:${dataset.dataset_id}`;
  const captureButton = !dataset.key_available
    ? `<button class="button secondary" data-action="capture-key" data-dataset-id="${escapeHtml(dataset.dataset_id)}" ${state.busy ? "disabled" : ""}>${capturePending ? "再次点击确认" : "连接聊天记录"}</button>`
    : "";
  const removeButton = `<button class="button danger" data-action="remove-dataset" data-dataset-id="${escapeHtml(dataset.dataset_id)}" ${state.busy ? "disabled" : ""}>${removePending ? "再次点击确认删除" : "删除"}</button>`;
  return `<div class="dataset-row ${selected ? "selected" : ""}" data-dataset-id="${escapeHtml(dataset.dataset_id)}">
    <span class="avatar small">${escapeHtml(dataset.display_name.slice(0, 1) || "企")}</span>
    <span class="dataset-main"><strong>${escapeHtml(dataset.display_name)}</strong><small>数据库 ${dataset.database_count} · 加密 ${dataset.encrypted_database_count} · WAL ${dataset.wal_count}</small></span>
    <span class="dataset-tags">${tags}${captureButton}${removeButton}</span>
  </div>`;
}

function datasetsBodyHtml(): string {
  const enterprise = state.enterprise;
  if (enterprise.error) {
    return `<div class="inline-error"><strong>无法读取企业列表</strong><span>${escapeHtml(enterprise.error)}</span>${scanButtonHtml("secondary", "重试")}</div>`;
  }
  if (!enterprise.datasets.length) {
    return enterprise.loading
      ? loading("正在发现本机企业微信数据…")
      : `<div class="empty-state compact"><strong>没有发现可用企业</strong><span>请确认企业微信已在本机登录过。</span></div>`;
  }
  return `<div class="dataset-list">${enterprise.datasets.map(datasetRowHtml).join("")}</div>
    <p class="panel-copy">尚未连接的企业需要完成一次本机连接。过程中会临时重启企业微信并进行只读扫描，通常需要 2–3 分钟；企业微信原始聊天不会被删除。</p>`;
}

/** 资料库异常只影响资料入口，不遮盖主助手对话。 */
function readinessCardHtml(): string {
  const bootstrap = state.bootstrap;
  if (!bootstrap) return `<div class="chat-readiness muted"><span class="spinner"></span><span>正在识别企业微信聊天记录；对话助手仍可直接使用。</span></div>`;
  if (bootstrap.readiness === "needs_dataset") {
    return `<div class="chat-readiness"><strong>尚未选择企业微信聊天</strong><span>对话助手仍可直接使用；需要参考聊天时再选择企业和聊天记录。</span><button class="button ghost small" data-action="open-enterprise">选择聊天</button></div>`;
  }
  if (bootstrap.readiness === "needs_key") {
    return `<div class="chat-readiness"><strong>还没有连接聊天记录</strong><span>对话助手仍可直接使用；需要参考企业微信聊天时，再完成一次本机连接。</span><button class="button ghost small" data-action="open-enterprise">连接聊天</button></div>`;
  }
  if (bootstrap.readiness === "refresh_failed") {
    return `<div class="chat-readiness"><strong>聊天记录更新失败</strong><span>可继续普通对话；仍可使用上次记录，稍后可以重试更新。</span><button class="button ghost small" data-action="retry-refresh">重试更新</button></div>`;
  }
  if (!state.deepseekConfigured) {
    return `<div class="chat-readiness"><strong>还差一步：连接 AI</strong><span>连接 DeepSeek 后即可提问；API Key 只保存在本机。</span><button class="button ghost small" data-action="open-deepseek-config">连接 AI</button></div>`;
  }
  return "";
}

/** 常驻警告条：与 banners 同级，任何页面（含 ready 态工作台）都能看到 bootstrap warnings。 */
function warningStrip(): string {
  const warnings = (state.bootstrap?.warnings ?? []).filter((warning) => state.warningsDismissed[warningKey(warning)] !== true);
  if (!warnings.length) return "";
  return `<div class="warning-strip" role="status"><ul class="warning-list">${warnings.map((warning) => `<li><strong>${escapeHtml(warning.code)}</strong><span class="warning-text">${escapeHtml(warning.message)}</span><button class="warning-dismiss" data-warning-dismiss="${escapeHtml(warningKey(warning))}" aria-label="忽略该提示">×</button></li>`).join("")}</ul></div>`;
}

function chatBodyHtml(): string {
  return `${chatHeadHtml()}${readinessCardHtml()}<div class="chat-body" id="transcript">${transcriptHtml()}</div>${composerHtml()}`;
}

function scrollTranscript(): void {
  const container = document.getElementById("transcript");
  if (container) container.scrollTop = container.scrollHeight;
}

function syncSendButton(): void {
  const agent = state.assistant;
  const send = document.querySelector<HTMLButtonElement>("#composer button[type=submit]");
  if (send) send.disabled = state.busy === "switch" || !state.deepseekConfigured || agent.status === "clearing" || ASSISTANT_BUSY_STATUS[agent.status] || !agent.draft.trim();
}

/** 局部刷新：只动 transcript / 主助手状态 / 发送按钮，不重排输入框。 */
function renderTranscript(): void {
  const container = document.getElementById("transcript");
  if (container) container.innerHTML = transcriptHtml();
  const agent = state.assistant;
  syncSendButton();
  const stop = document.getElementById("chat-stop");
  if (stop) stop.hidden = !(agent.status === "starting" || agent.status === "preparing" || agent.status === "generating");
  const dot = document.getElementById("chat-status");
  if (dot) dot.className = `row-dot ${agent.status === "failed" ? "error" : agent.status === "idle" ? "ready" : agent.status === "stopped" ? "stale" : "thinking"}`;
  const statusText = document.getElementById("chat-status-text");
  if (statusText) statusText.textContent = ASSISTANT_STATUS_TEXT[agent.status];
  syncThinkingLabels();
  const textarea = document.querySelector<HTMLTextAreaElement>("[data-draft]");
  if (textarea && textarea.value !== agent.draft) textarea.value = agent.draft;
  scrollTranscript();
  renderRailList();
}

/* ------------------------------------------------------------------ *
 * 抽屉与浮层
 * ------------------------------------------------------------------ */

function imageStatusLabel(image: ContextPackageImage): string {
  switch (image.status) {
    case "original":
      return "可发送原图";
    case "thumbnail":
      return "可发送图片分片";
    case "missing":
      return "图片未找到";
    case "unsupported":
      return "格式不支持";
    case "too_large":
      return "超过本轮图片限制";
    case "failed":
      return "读取失败";
    default:
      return "图片不可用";
  }
}

function imageCardHtml(image: ContextPackageImage): string {
  const key = image.image_id.startsWith("img_") ? image.image_id : "";
  const thumb = key ? state.drawer.thumbs[key] : undefined;
  const visual = key
    ? thumb
      ? `<button class="image-open" type="button" data-image-view="${escapeHtml(key)}" aria-label="打开${imageStatusLabel(image)}"><img class="thumb" src="${thumb}" alt="${imageStatusLabel(image)}"></button>`
      : `<div class="thumb placeholder">${thumb === undefined ? "加载中…" : "缩略图不可用"}</div>`
    : `<div class="thumb placeholder">未发送</div>`;
  const technical = [
    `图片标识：${image.image_id}`,
    `MIME：${image.mime_type ?? "—"}`,
    `大小：${image.bytes ? `${Math.round(image.bytes / 1024)} KB` : "—"}`,
    `尺寸：${image.width && image.height ? `${image.width}×${image.height}` : "—"}`,
    `SHA-256：${image.sha256 ?? "—"}`,
  ].join(" · ");
  return `<figure class="shot ${key ? "" : "omitted"}">
    ${visual}
    <figcaption><strong>${imageStatusLabel(image)}</strong>
      ${image.reason ? `<span class="muted">${escapeHtml(image.reason)}</span>` : ""}
      <details class="technical-inline"><summary>技术详情</summary><span>${escapeHtml(technical)}</span></details>
    </figcaption>
  </figure>`;
}
function renderImageViewer(): string {
  const viewer = state.imageViewer;
  if (!viewer) return "";
  const body = viewer.loading
    ? loading("正在读取原尺寸图片…")
    : viewer.error
      ? `<div class="inline-error"><strong>图片暂不可用</strong><span>${escapeHtml(viewer.error)}</span></div>`
      : viewer.dataUrl
        ? `<img class="image-viewer-image" src="${viewer.dataUrl}" alt="资料原尺寸图片">`
        : `<p class="muted">没有可显示的图片。</p>`;
  return `<div class="overlay image-viewer-overlay" data-action="close-image-viewer">
    <section class="image-viewer" role="dialog" aria-modal="true" aria-label="图片查看器">
      <header class="modal-head"><div><h2>图片预览</h2><p class="muted">按原比例显示；长图可滚动查看。</p></div><button class="drawer-close" data-action="close-image-viewer" aria-label="关闭">×</button></header>
      <div class="image-viewer-body">${body}</div>
    </section>
  </div>`;
}

async function openImageViewer(imageId: string): Promise<void> {
  const packageId = state.drawer.pkg?.package_id;
  if (!packageId || !imageId) return;
  state.imageViewer = { packageId, imageId, loading: true, dataUrl: null, error: null };
  render();
  try {
    const result = await api.packageImageData(packageId, imageId);
    if (!state.imageViewer || state.imageViewer.packageId !== packageId || state.imageViewer.imageId !== imageId) return;
    state.imageViewer = { packageId, imageId, loading: false, dataUrl: result.data_url, error: null };
  } catch (error) {
    if (!state.imageViewer || state.imageViewer.packageId !== packageId || state.imageViewer.imageId !== imageId) return;
    state.imageViewer = { packageId, imageId, loading: false, dataUrl: null, error: toCoreError(error).message };
  }
  render();
}

function sourceRangeText(): string {
  const options = state.sourceOptions;
  if (options.range === "today") return "今天（准备时冻结）";
  if (options.range === "7d") return "最近 7 天（准备时冻结）";
  if (options.range === "custom") return `${options.start || "起始不限"} 至 ${options.end || "结束不限"}`;
  return "可用聊天记录";
}

function sourceConfigHtml(): string {
  const options = state.sourceOptions;
  const disabled = state.pendingSend || state.drawer.loading ? "disabled" : "";
  return `<section class="source-config">
    <div class="source-config-head"><div><strong>本次读取范围</strong><span class="muted">${escapeHtml(sourceRangeText())}</span></div><span class="muted">发送前可查看实际内容</span></div>
    <div class="source-config-grid">
      <label>时间范围<select data-source-option="range" ${disabled}>
        <option value="all" ${options.range === "all" ? "selected" : ""}>可用聊天记录</option>
        <option value="today" ${options.range === "today" ? "selected" : ""}>今天</option>
        <option value="7d" ${options.range === "7d" ? "selected" : ""}>最近 7 天</option>
        <option value="custom" ${options.range === "custom" ? "selected" : ""}>自定义日期</option>
      </select></label>
      <label>每个聊天最多读取<input type="number" min="1" max="500" step="1" value="${options.limit}" data-source-option="limit" ${disabled}></label>
      <label class="check-line"><input type="checkbox" data-source-option="includeImages" ${options.includeImages ? "checked" : ""} ${disabled}>包含图片（图片也会发送）</label>
    </div>
    ${options.range === "custom" ? `<div class="source-config-grid custom-range">
      <label>开始日期<input type="date" data-source-option="start" value="${escapeHtml(options.start)}" ${disabled}></label>
      <label>结束日期（含当天）<input type="date" data-source-option="end" value="${escapeHtml(options.end)}" ${disabled}></label>
    </div>` : ""}
    <details class="advanced-limits"><summary>更多限制</summary>
      <div class="source-config-grid">
        <label>全包图片数上限<input type="number" min="0" max="32" step="1" value="${options.maxImages}" data-source-option="maxImages" ${disabled}></label>
        <label>图片总字节上限<input type="number" min="0" max="${1 << 30}" step="1048576" value="${options.maxTotalBytes}" data-source-option="maxTotalBytes" ${disabled}></label>
        <label>资料内容上限<input type="number" min="100" max="60000" step="100" value="${options.maxContextTokens}" data-source-option="maxContextTokens" ${disabled}></label>
        <label>单消息字符上限<input type="number" min="50" max="8000" step="50" value="${options.maxMessageCharacters}" data-source-option="maxMessageCharacters" ${disabled}></label>
      </div>
    </details>
  </section>`;
}

function selectedModelInfo(model: string): AgentModelInfo | null {
  return state.modelCatalog?.find((item) => item.id === model) ?? null;
}

function imageCapability(model: string): "supported" | "unsupported" | "unknown" {
  if (!state.modelCatalog || state.modelsWarning) return "unknown";
  const info = selectedModelInfo(model);
  return info ? (info.images ? "supported" : "unsupported") : "unknown";
}

function pendingSendHtml(pkg: ContextPackageData): string {
  const pending = state.pendingSend;
  if (!pending) {
    return `<div class="drawer-actions"><span class="muted">预览只读取本机记录，不会发送问题。</span><button class="button secondary" data-action="complete-preview">完成预览</button></div>`;
  }
  const current = state.drawer.cacheKey === pending.packageKey;
  if (!current) return `<div class="inline-warn"><strong>资料已变化</strong><span>请重新预览后再确认发送。</span></div>`;
  const capability = imageCapability(pending.model);
  const imageBlocked = pkg.stats.image_count > 0 && capability !== "supported";
  const imageNotice = pkg.stats.image_count > 0
    ? `<div class="inline-warn"><strong>图片隐私提示</strong><span>图片可能包含未脱敏姓名、聊天文字或其他信息；确认后会与问题一起发送给所选模型。</span></div>`
    : "";
  const compatibilityNotice = imageBlocked
    ? `<div class="inline-warn"><strong>${capability === "unsupported" ? "当前模型不支持图片" : "暂时无法确认模型是否支持图片"}</strong><span>不能静默丢图；请更换已确认支持图片的模型，或显式改为仅文字后重新预览。</span><button class="button secondary" data-action="disable-source-images">改为仅文字并重新预览</button></div>`
    : "";
  const crossNotice = pending.crossEnterprise
    ? `<label class="cross-enterprise-confirm"><input type="checkbox" data-cross-enterprise-confirm ${pending.crossEnterpriseConfirmed ? "checked" : ""}>我确认将这些企业的资料共同发送给该模型：${escapeHtml(pending.datasetLabels.join("、"))}</label>`
    : "";
  const canConfirm = !imageBlocked && (!pending.crossEnterprise || pending.crossEnterpriseConfirmed);
  return `<section class="send-confirmation">
    <h3>确认发送本次内容</h3>
    <p class="muted">确认后会把问题、${pkg.stats.message_count} 条聊天记录和 ${pkg.stats.image_count} 张图片发送给所选模型。已发送内容无法通过删除本地对话撤回。</p>
    <div class="confirm-question">${escapeHtml(pending.text)}</div>
    <div class="confirm-meta">模型：${escapeHtml(pending.model)} · 聊天：${pending.names.length} 个 · 资料约 ${pkg.stats.estimated_tokens} tokens</div>
    ${imageNotice}
    ${compatibilityNotice}
    ${crossNotice}
    <div class="drawer-actions"><button class="button secondary" data-action="cancel-send">取消发送</button><button class="button primary" data-action="confirm-send" ${canConfirm ? "" : "disabled"}>确认发送</button></div>
  </section>`;
}
function updateSourceOption(field: string, target: HTMLInputElement | HTMLSelectElement): void {
  if (state.pendingSend) return;
  const next = { ...state.sourceOptions };
  if (field === "range" && (target.value === "all" || target.value === "today" || target.value === "7d" || target.value === "custom")) {
    next.range = target.value;
  } else if (field === "start" || field === "end") {
    next[field] = target.value;
  } else if (field === "includeImages" && target instanceof HTMLInputElement) {
    next.includeImages = target.checked;
  } else {
    const bounds: Record<string, [number, number]> = {
      limit: [1, 500],
      maxImages: [0, 32],
      maxTotalBytes: [0, 1 << 30],
      maxContextTokens: [100, 60000],
      maxMessageCharacters: [50, 8000],
    };
    const bound = bounds[field];
    if (!bound) return;
    const value = Number(target.value);
    if (!Number.isFinite(value)) return;
    next[field as keyof SourceOptions] = Math.min(bound[1], Math.max(bound[0], Math.round(value))) as never;
  }
  state.sourceOptions = next;
  saveSourceOptions();
  invalidatePackagePreview();
  render();
}


/** 本轮资料预览：来源、图片、告警与容量，全部来自同一份不可变资料包。 */
function packageHtml(pkg: ContextPackageData): string {
  const sourceRows = pkg.sources.map((source) => `<div class="source-row">
    <div><strong>${escapeHtml(source.display_name)}</strong>
      <span class="muted">${escapeHtml(source.kind)} · 记录时间 ${escapeHtml(source.snapshot_created_at || "未知")}${source.read_only_history ? " · 仅可读取历史" : ""}</span></div>
    <div class="muted">实际使用 ${source.retained_message_count}/${source.original_message_count} 条聊天 · 图片 ${source.image_count}${source.omitted_image_count ? `（未发送 ${source.omitted_image_count}）` : ""} · 内容量约 ${source.estimated_tokens}${source.truncated ? " · 内容已截断" : ""}</div>
  </div>`).join("");
  const images = pkg.images.length
    ? `<div class="thumb-grid">${pkg.images.map(imageCardHtml).join("")}</div>`
    : `<p class="muted">本轮没有图片。</p>`;
  const warnings = pkg.warnings.length
    ? `<div class="inline-warn"><strong>注意</strong>${pkg.warnings.map((item) => `<span>${escapeHtml(item.message)}</span>`).join("")}</div>`
    : "";
  return `${sourceConfigHtml()}
    <h3>本次资料（${pkg.stats.source_count} 个聊天）</h3>
    <p class="drawer-meta">生成于 ${escapeHtml(new Date(pkg.created_at).toLocaleTimeString())} · <details class="technical-inline"><summary>查看技术详情</summary><span>资料包 ${escapeHtml(pkg.package_id)}</span></details></p>
    <div class="card-grid four">
      <div class="stat"><span>聊天消息</span><strong>${pkg.stats.message_count}</strong></div>
      <div class="stat"><span>发送图片</span><strong>${pkg.stats.image_count}</strong></div>
      <div class="stat"><span>未发送图片</span><strong>${pkg.stats.omitted_image_count}</strong></div>
      <div class="stat"><span>内容量估算</span><strong>${pkg.stats.estimated_tokens}</strong></div>
    </div>
    ${warnings}
    <div class="drawer-flags">${sourceRows}</div>
    <h3>图片（${pkg.stats.image_count} 张，含长截图分片）</h3>
    ${images}
    <p class="muted">图片里的文字不会自动脱敏；长截图按 2048 像素高无损分片，分片之间可能有重叠。</p>
    <details class="text-preview"><summary>查看发给模型的资料正文（完整）</summary><pre>${escapeHtml(pkg.text)}</pre></details>
    <div class="drawer-flags">
      <div class="flag-row"><span>发送时会复用该资料包</span><strong>${state.drawer.cacheKey === sourcesCacheKey() ? "是" : "否（来源已变化）"}</strong></div>
    </div>
    ${pendingSendHtml(pkg)}`;
}


function renderDrawer(): string {
  const drawer = state.drawer;
  let body: string;
  if (drawer.loading) body = loading("正在生成本轮资料…");
  else if (drawer.error) body = `${sourceConfigHtml()}<div class="inline-error"><strong>资料预览暂不可用</strong><span>${escapeHtml(drawer.error)}</span><button class="button secondary" data-action="retry-preview">重试</button></div>`;
  else if (drawer.pkg) body = packageHtml(drawer.pkg);
  else if (!state.selectedSources.length) body = `${sourceConfigHtml()}<div class="empty-state compact"><strong>本轮未选来源</strong><span>在左侧点会话前的「+」把它加入本轮资料；不选就只做普通对话。</span></div>`;
  else body = `${sourceConfigHtml()}<div class="empty-state compact"><strong>尚未预览</strong><span>点击「生成预览」确认本轮会发送哪些资料。</span><button class="button secondary" data-action="retry-preview">生成预览</button></div>`;
  return `<div class="drawer-overlay" data-action="close-drawer">
    <aside class="drawer">
      <header class="drawer-head"><h2>本轮资料</h2><button class="drawer-close" data-action="close-drawer" aria-label="关闭">×</button></header>
      <div class="drawer-body">${body}</div>
    </aside>
  </div>`;
}

/* ------------------------------------------------------------------ *
 * 记忆：当前有效上下文 / 完整历史 / 已注入资料，以及清空
 * ------------------------------------------------------------------ */

const EFFECTIVENESS_TEXT: Record<string, string> = {
  active: "仍在上下文中",
  summarized: "只剩摘要",
  evicted: "已移出上下文",
  unknown: "无法判定",
};

function memoryRows(messages: { role: string; text: string; images?: unknown[] }[], emptyText: string, flags?: (message: { in_effective_context?: boolean; summarized?: boolean }) => string): string {
  if (!messages.length) return `<p class="muted">${escapeHtml(emptyText)}</p>`;
  return `<div class="memory-list">${messages
    .map((message) => {
      const role = message.role === "assistant" ? "Agent" : message.role === "user" ? "你" : "系统";
      const flag = flags ? flags(message as { in_effective_context?: boolean; summarized?: boolean }) : "";
      const images = message.images?.length ? ` · 图片 ${message.images.length}` : "";
      const preview = message.text.slice(0, 400);
      const text = message.text.length > 400
        ? `<span>${escapeHtml(preview)}…</span><details class="memory-full-text"><summary>查看完整内容</summary><pre>${escapeHtml(message.text)}</pre></details>`
        : escapeHtml(message.text);
      return `<div class="memory-row ${escapeHtml(message.role)}"><span class="memory-role">${role}${flag}</span><span class="memory-text">${text}${images}</span></div>`;
    })
    .join("")}</div>`;
}

function applyAgentRuntime(runtime: AgentStatusData): void {
  const agent = state.assistant;
  agent.status = runtime.state;
  agent.memoryEpoch = runtime.memory_epoch;
  agent.runId = runtime.run_id;
  agent.requestId = runtime.request_id;
  if (runtime.model) agent.model = runtime.model;
  if (agent.status !== "starting" && agent.status !== "preparing" && agent.status !== "generating") endThinking(agent);
}

async function loadAssistantRuntime(): Promise<void> {
  try {
    const runtime = await api.agentStatus();
    // 启动查询不能覆盖用户已经发起的请求或输入中的草稿。
    if (state.assistant.status !== "stopped" || state.assistant.messages.length || state.assistant.draft) return;
    state.memory.runtime = runtime;
    applyAgentRuntime(runtime);
    render();
  } catch {
    /* Agent 尚未启动时状态查询失败不阻断资料库加载。 */
  }
}

function contextPercentLabel(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return value > 0 && value < 0.1 ? "<0.1%" : `${value.toFixed(value < 1 ? 2 : 1)}%`;
}
function memoryBodyHtml(): string {
  const memory = state.memory;
  if (memory.loading) return loading("正在读取记忆…");
  if (memory.error) return `<div class="inline-error"><strong>记忆暂不可用</strong><span>${escapeHtml(memory.error)}</span></div>`;
  if (memory.cleared && memory.tab === "context") return `<div class="inline-ok"><strong>已清空</strong><span>${escapeHtml(memory.cleared)}</span><span class="muted">企业微信源数据、快照与模型配置未受影响。</span></div>`;
  if (memory.tab === "context") {
    const ctx = memory.context;
    if (!ctx) return `<p class="muted">点「刷新」读取当前有效上下文。</p>`;
    const compactions = ctx.compactions.length
      ? ctx.compactions.map((item) => `<div class="memory-row system"><span class="memory-role">对话摘要</span><span class="memory-text">${escapeHtml(item.summary.slice(0, 400))}${item.tokens_after ? `（摘要后约 ${item.tokens_after} 内容量）` : ""}</span></div>`).join("")
      : `<p class="muted">还没有生成过对话摘要。</p>`;
    return `<div class="card-grid four">
      <div class="stat"><span>对话轮次</span><strong>${ctx.memory_epoch}</strong></div>
      <div class="stat"><span>当前内容量</span><strong>${ctx.context_tokens ?? "—"}</strong></div>
      <div class="stat"><span>使用比例</span><strong>${contextPercentLabel(ctx.context_percent)}</strong></div>
      <div class="stat"><span>其中图片</span><strong>${ctx.images_in_context}</strong></div>
    </div>
    <p class="muted">模型：${escapeHtml(ctx.model ?? "—")}${ctx.streaming ? " · 正在回答" : ""}</p>
    <h3>当前对话</h3>
    ${memoryRows(ctx.messages, "当前对话为空。")}
    <h3>压缩摘要</h3>
    ${compactions}`;
  }
  if (memory.tab === "history") {
    const history = memory.history;
    if (!history) return `<p class="muted">点「刷新」读取完整对话。</p>`;
    const more = history.has_more
      ? `<div class="memory-more"><button class="button secondary" data-action="load-more-memory" data-memory-tab="history" ${memory.loadingMore ? "disabled" : ""}>${memory.loadingMore ? "加载中…" : "加载更早记录"}</button></div>`
      : "";
    return `<p class="muted">共 ${history.total} 条；较早内容可能已被摘要或不再参与当前回答。</p>
      ${memoryRows(
        history.messages,
        "还没有对话记录。",
        (message) => `<span class="memory-flag">${message.in_effective_context ? "当前可参考" : message.summarized ? "已摘要" : "较早记录"}</span>`,
      )}
      ${more}`;
  }
  const injections = memory.injections;
  if (!injections) return `<p class="muted">点「刷新」读取已使用的聊天。</p>`;
  if (!injections.entries.length && !injections.has_more) return `<p class="muted">还没有在对话中使用过企业微信聊天。</p>`;
  const more = injections.has_more
    ? `<div class="memory-more"><button class="button secondary" data-action="load-more-memory" data-memory-tab="injections" ${memory.loadingMore ? "disabled" : ""}>${memory.loadingMore ? "加载中…" : "加载更早注入记录"}</button></div>`
    : "";
  return `<p class="muted">共使用过 ${injections.total} 次聊天记录。详情用于核对哪些内容进入过本机对话。</p>
    ${injections.entries
      .map(
        (entry) => `<div class="injection-card">
        <div class="injection-head"><strong>${escapeHtml(entry.question || "（未记录问题）")}</strong><span class="status-pill neutral">${escapeHtml(EFFECTIVENESS_TEXT[entry.effectiveness] ?? entry.effectiveness)}</span></div>
        <div class="muted">${entry.sources.map((source) => `${escapeHtml(source.display_name)}（${escapeHtml(source.kind)} · 消息 ${source.message_count} · 图片 ${source.image_count}）`).join("；")}</div>
        <div class="muted">保留 ${entry.retained.message_count} 条消息 · ${entry.retained.image_count} 张图片${entry.package_id ? ` · 资料包 ${escapeHtml(entry.package_id)}` : ""}</div>
      </div>`,
      )
      .join("")}
    ${more}`;
}

function memoryRuntimeHtml(): string {
  const runtime = state.memory.runtime;
  if (!runtime) return "";
  const labels: Record<string, string> = {
    stopped: "未启动",
    starting: "启动中",
    idle: "空闲",
    preparing: "准备聊天资料",
    generating: "回答中",
    clearing: "清理中",
    failed: "异常",
  };
  return `<p class="muted">助手${escapeHtml(labels[runtime.state] ?? runtime.state)} · 模型 ${escapeHtml(runtime.model ?? "—")}${runtime.unfinished_clear_epoch ? " · 上次清理未完成，重启后将自动继续" : ""}</p>`;
}

function renderMemory(): string {
  if (!state.memory.open) return "";
  const tabs: MemoryTab[] = ["context", "history", "injections"];
  const labels: Record<MemoryTab, string> = { context: "当前对话", history: "完整记录", injections: "已使用聊天" };
  const clearing = state.assistant.status === "clearing";
  const clearControls = clearing
    ? `<span class="muted">正在清空主助手记忆…</span>`
    : state.memory.confirmingClear
      ? `<div class="memory-confirmation" role="alert">
          <strong>确认清空主助手记忆？</strong>
          <span>此操作会删除对话、摘要、已使用聊天记录和相关图片，不能恢复；企业微信源数据、已保存聊天记录和模型配置不受影响。</span>
          <span class="memory-confirmation-actions"><button class="button secondary" data-action="cancel-memory-clear">取消</button><button class="button danger" data-action="confirm-memory-clear">确认清空</button></span>
        </div>`
      : `<button class="button secondary" data-action="clear-memory">清空对话</button>`;
  return `<div class="overlay" data-action="close-memory">
    <section class="modal memory-modal">
      <header class="modal-head">
        <div><h2>本对话记录</h2><p class="muted">这里查看助手当前能参考的内容、完整对话和已经使用过的聊天。清空会删除本机保存的对话记忆。</p></div>
        <button class="drawer-close" data-action="close-memory" aria-label="关闭">×</button>
      </header>
      ${memoryRuntimeHtml()}
      <nav class="memory-tabs">${tabs
        .map((tab) => `<button class="segment ${state.memory.tab === tab ? "active" : ""}" data-memory-tab="${tab}" ${clearing ? "disabled" : ""}>${labels[tab]}</button>`)
        .join("")}</nav>
      <div class="modal-body">${memoryBodyHtml()}</div>
      <footer class="modal-foot">
        <button class="button secondary" data-action="reload-memory" ${clearing ? "disabled" : ""}>刷新</button>
        ${clearControls}
        ${!state.memory.confirmingClear && !clearing ? `<span class="muted">清空会删除本对话、已使用聊天和相关图片；企业微信源数据与配置不动。</span>` : ""}
      </footer>
    </section>
  </div>`;
}

async function openMemory(tab: MemoryTab = "context"): Promise<void> {
  state.drawer.open = false;
  state.imageViewer = null;
  state.pendingSend = null;
  state.epochs.preview += 1;
  const epoch = ++state.epochs.memory;
  state.memory = { ...state.memory, open: true, tab, loading: true, loadingMore: false, error: null, confirmingClear: false, cleared: null };
  render();
  try {
    const runtime = await api.agentStatus();
    if (epoch === state.epochs.memory) {
      state.memory.runtime = runtime;
      applyAgentRuntime(runtime);
    }
  } catch {
    if (epoch === state.epochs.memory) state.memory.runtime = null;
  }
  await loadMemoryTab(tab);
}

async function loadMemoryTab(tab: MemoryTab): Promise<void> {
  const epoch = ++state.epochs.memory;
  state.memory = { ...state.memory, open: true, tab, loading: true, loadingMore: false, error: null, cleared: null };
  render();
  try {
    if (tab === "context") {
      const context = await api.memoryContext();
      if (epoch !== state.epochs.memory) return;
      state.memory.context = context;
    } else if (tab === "history") {
      const history = await api.memoryHistory(0, 50);
      if (epoch !== state.epochs.memory) return;
      state.memory.history = history;
    } else {
      const injections = await api.memoryInjections(0, 20);
      if (epoch !== state.epochs.memory) return;
      state.memory.injections = injections;
    }
    if (epoch !== state.epochs.memory) return;
    state.memory.loading = false;
  } catch (error) {
    if (epoch !== state.epochs.memory) return;
    state.memory.loading = false;
    state.memory.error = toCoreError(error).message;
  }
  render();
}

async function loadMoreMemory(tab: MemoryTab): Promise<void> {
  if (state.memory.loading || state.memory.loadingMore || state.assistant.status === "clearing") return;
  const history = tab === "history" ? state.memory.history : null;
  const injections = tab === "injections" ? state.memory.injections : null;
  const hasMore = tab === "history" ? history?.has_more === true : injections?.has_more === true;
  if (!hasMore) return;
  const offset = tab === "history" ? history?.messages.length ?? 0 : injections?.entries.length ?? 0;
  const epoch = ++state.epochs.memory;
  state.memory = { ...state.memory, tab, loadingMore: true, error: null, cleared: null };
  render();
  try {
    if (tab === "history") {
      const next = await api.memoryHistory(offset, 50);
      if (epoch !== state.epochs.memory) return;
      const current = state.memory.history;
      state.memory.history = current
        ? { ...next, offset: current.offset, messages: [...current.messages, ...next.messages] }
        : next;
    } else {
      const next = await api.memoryInjections(offset, 20);
      if (epoch !== state.epochs.memory) return;
      const current = state.memory.injections;
      state.memory.injections = current
        ? { ...next, offset: current.offset, entries: [...current.entries, ...next.entries] }
        : next;
    }
    if (epoch !== state.epochs.memory) return;
    state.memory.loadingMore = false;
  } catch (error) {
    if (epoch !== state.epochs.memory) return;
    state.memory.loadingMore = false;
    state.memory.error = toCoreError(error).message;
  }
  render();
}

async function clearMemory(): Promise<void> {
  if (state.assistant.status === "clearing") return;
  assistantSubmissionSeq += 1;
  const epoch = ++state.epochs.memory;
  const agent = state.assistant;
  agent.status = "clearing";
  agent.runId = null;
  agent.requestId = null;
  endThinking(agent);
  state.memory = { ...state.memory, confirmingClear: false, loading: true, loadingMore: false, error: null, cleared: null };
  render();
  try {
    const result: MemoryClearData = await api.memoryClear(true);
    // 清空成功后，前端不得继续显示旧 transcript、草稿、资料选择或旧预览。
    state.assistant = {
      ...state.assistant,
      messages: [],
      draft: "",
      historyLoaded: true,
      status: "stopped",
      thinking: null,
      runId: null,
      requestId: null,
      memoryEpoch: result.memory_epoch,
      error: null,
    };
    state.historyLoading = {};
    state.selectedSources = [];
    state.selectedSourceSnapshots = {};
    saveSelectedSources();
    state.drawer = { ...EMPTY_DRAWER };
    state.imageViewer = null;
    state.epochs.preview += 1;
    state.pendingConfirm = null;
    state.notice = "当前对话已重置；本次聊天选择和历史记忆已清除";
    if (epoch === state.epochs.memory) {
      state.memory = {
        ...state.memory,
        loading: false,
        context: null,
        history: null,
        injections: null,
        cleared: `删除会话 ${result.removed.session ? "是" : "否"} · 消息 ${result.removed.messages} 条 · 注入记录 ${result.removed.injections} 条 · 资料包 ${result.removed.packages} 个 · 图片 ${result.removed.images} 张；记忆代次 ${result.previous_epoch} → ${result.memory_epoch}`,
      };
    }
    try {
      const runtime = await api.agentStatus();
      state.memory.runtime = runtime;
      applyAgentRuntime(runtime);
    } catch {
      state.memory.runtime = null;
    }
  } catch (error) {
    if (epoch === state.epochs.memory) {
      state.memory.loading = false;
      state.memory.error = toCoreError(error).message;
    }
    agent.status = "failed";
    agent.error = toCoreError(error).code;
  }
  render();
}
async function startNewConversation(): Promise<void> {
  const agent = state.assistant;
  if (state.busy || agent.status === "clearing") return;
  if (ASSISTANT_BUSY_STATUS[agent.status]) {
    state.notice = "当前回答尚未完成，请稍后再开始新对话";
    render();
    return;
  }
  state.busy = "new-conversation";
  state.error = null;
  render();
  try {
    await api.agentNewConversation(knownModel(agent.model) ? agent.model : DEFAULT_AGENT_MODEL);
    assistantSubmissionSeq += 1;
    endThinking(agent);
    state.assistant = {
      ...agent,
      messages: [],
      draft: "",
      historyLoaded: true,
      status: "idle",
      thinking: null,
      runId: null,
      requestId: null,
      error: null,
    };
    state.historyLoading = {};
    state.selectedSources = [];
    state.selectedSourceSnapshots = {};
    saveSelectedSources();
    state.drawer = { ...EMPTY_DRAWER };
    state.imageViewer = null;
    state.pendingSend = null;
    state.memory = { ...state.memory, open: false, loading: false, loadingMore: false, error: null, context: null, history: null, injections: null, cleared: null };
    state.epochs.preview += 1;
    state.epochs.memory += 1;
    state.notice = "已开始新对话；旧对话仍保存在本机，未被删除";
  } catch (error) {
    setError(error);
  }
  state.busy = null;
  render();
}

function requestConversationReset(): void {
  if (state.assistant.status === "clearing") return;
  if (requireConfirm("reset-conversation", CONFIRM_LABELS["reset-conversation"])) void clearMemory();
}
function accountCardHtml(account: AccountSummary): string {
  const selected = state.enterprise.selectedAccount?.account_id === account.account_id;
  const current = state.enterprise.currentAccount?.account_id === account.account_id || account.current;
  const removePending = state.pendingConfirm?.token === `remove-account:${account.account_id}`;
  return `<article class="dataset-row ${selected ? "selected" : ""}">
    <span class="avatar small">${escapeHtml(account.account_name.slice(0, 1) || "人")}</span>
    <span class="dataset-main"><strong>${escapeHtml(account.account_name)}</strong><small>名下公司数据集 ${account.dataset_count} · 数据库 ${account.database_count}</small></span>
    <span class="dataset-tags">
      ${current ? '<span class="status-pill ok">当前登录</span>' : ""}
      ${account.key_available ? '<span class="status-pill ok">已连接</span>' : '<span class="status-pill warn">需要连接</span>'}
      <button class="button secondary" data-action="select-account" data-account-id="${escapeHtml(account.account_id)}">${selected ? "选择名下公司" : "使用此账户"}</button>
      <button class="button danger" data-action="remove-account" data-account-id="${escapeHtml(account.account_id)}">${removePending ? "再次点击确认" : "彻底删除"}</button>
    </span>
  </article>`;
}

function companyCardHtml(dataset: DatasetSummary): string {
  const selected = state.bootstrap?.dataset?.dataset_id === dataset.dataset_id;
  const capturePending = state.pendingConfirm?.token === `capture-key:${dataset.dataset_id}`;
  const captureButton = !dataset.key_available
    ? `<button class="button secondary" data-action="capture-key" data-dataset-id="${escapeHtml(dataset.dataset_id)}" ${state.busy ? "disabled" : ""}>${capturePending ? "再次点击确认" : "连接聊天记录"}</button>`
    : "";
  return `<article class="dataset-row ${selected ? "selected" : ""}">
    <span class="avatar small">${escapeHtml((dataset.company_name || "企").slice(0, 1))}</span>
    <span class="dataset-main"><strong>${escapeHtml(dataset.company_name || dataset.display_name)}</strong><small>${escapeHtml(dataset.display_name)} · 数据库 ${dataset.database_count}</small></span>
    <span class="dataset-tags">
      ${dataset.active ? '<span class="status-pill ok">当前数据</span>' : ""}
      ${dataset.key_available ? '<span class="status-pill ok">已连接</span>' : '<span class="status-pill warn">需要连接</span>'}
      ${captureButton}
      <button class="button secondary" data-action="select-company" data-dataset-id="${escapeHtml(dataset.dataset_id)}">${selected ? "继续使用" : "选择公司"}</button>
    </span>
  </article>`;
}

function renderEnterprisePanel(): string {
  const enterprise = state.enterprise;
  const account = enterprise.selectedAccount;
  const body = enterprise.loading
    ? loading(enterprise.mode === "accounts" ? "正在读取企业微信账户…" : "正在读取账户名下的公司…")
    : enterprise.error
      ? `<div class="inline-error"><strong>无法读取企业微信数据</strong><span>${escapeHtml(enterprise.error)}</span>${scanButtonHtml("secondary", "重试扫描")}</div>`
      : enterprise.mode === "accounts"
        ? enterprise.accounts.length
          ? `<div class="dataset-list">${enterprise.accounts.map(accountCardHtml).join("")}</div>`
          : `<div class="empty-state compact"><strong>没有发现企业微信账户</strong><span>请确认企业微信已在本机登录过。</span></div>`
        : enterprise.datasets.length
          ? `<div class="dataset-list">${enterprise.datasets.map(companyCardHtml).join("")}</div>`
          : `<div class="empty-state compact"><strong>该账户没有可用公司数据</strong><span>请返回账户列表重新选择。</span></div>`;
  const title = enterprise.mode === "accounts" ? "第 1 步：选择企业微信账户" : "第 2 步：选择公司";
  const subtitle = enterprise.mode === "accounts"
    ? "先选择要使用的企业微信账户；下一步选择该账户名下的公司。"
    : `账户：${account?.account_name ?? "未命名账户"} · 选择公司后即可连接聊天记录。`;
  return `<div class="overlay" data-action="close-enterprise">
    <section class="modal">
      <header class="modal-head">
        <div><h2>${title}</h2><p class="muted">${escapeHtml(subtitle)}</p></div>
        <button class="drawer-close" data-action="close-enterprise" aria-label="关闭">×</button>
      </header>
      <div class="modal-body">${body}<p class="panel-copy">删除只会清理本应用保存的聊天记录、对话和连接信息，不会删除企业微信原始聊天。下次扫描仍可能重新发现这些记录。</p></div>
      <footer class="modal-foot">${enterprise.mode === "accounts" ? scanButtonHtml("ghost", "重新扫描") : '<button class="button ghost" data-action="back-account-picker">返回账户列表</button>'}<span class="muted">${enterprise.mode === "accounts" ? "当前登录账户会标记为“当前登录”。" : "下一步：连接聊天记录；通常需要 2–3 分钟。"}</span></footer>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------ *
 * 页面
 * ------------------------------------------------------------------ */

function renderWorkbench(): string {
  return `<div class="workbench ${state.busy === "switch" ? "locked" : ""}" style="--rail-width: ${state.railWidth}px">
      ${renderRail()}
      <div class="rail-resizer" data-rail-resizer role="separator" tabindex="0" aria-label="调整会话选择器宽度" aria-orientation="vertical"></div>
      <section class="chat">${chatBodyHtml()}</section>
    </div>`;
}

function renderDatasetsPage(): string {
  return `<section class="page-head"><div><p class="eyebrow">CHAT RECORDS</p><h1>企业与聊天记录</h1><p class="muted">只列出各企业的当前聊天记录（不含备份）。切换企业只改变可浏览的聊天，不会清空当前对话。</p></div><button class="button secondary" data-page="workbench">返回工作台</button></section>
    <section class="panel">${datasetsBodyHtml()}</section>
    <section class="panel"><p class="panel-copy">选择企业不会删除企业微信原始记录或本机已保存的对话；需要时可重新连接聊天记录并更新。</p></section>`;
}

function renderSettings(): string {
  const connector = state.connectorInstalled == null ? "检查中…" : state.connectorInstalled ? "已安装" : "未安装";
  const pi = state.piEnvironment;
  const dataset = state.bootstrap?.dataset;
  const snapshot = state.bootstrap?.snapshot;
  const warnings = state.bootstrap?.warnings ?? [];
  const modelCount = state.modelCatalog?.length ?? 0;
  return `<section class="page-head"><div><p class="eyebrow">SETTINGS</p><h1>连接与权限</h1><p class="muted">聊天记录默认只在本机读取；发送问题时，所选聊天和必要的对话内容会发送给 DeepSeek，不会自动上传全部聊天库。模型 Key 只保存于本机。</p></div><button class="button secondary" data-page="workbench">返回工作台</button></section>
    <section class="settings-list">
      <article class="setting-row"><div><strong>企业与聊天记录</strong><span>${dataset ? `${escapeHtml(dataset.display_name)}${snapshot ? " · 已有本机记录" : " · 尚无记录"}` : "尚未选择企业"}</span></div><div class="setting-actions"><span class="status-pill ${state.bootstrap?.readiness === "ready" ? "ok" : "warn"}">${escapeHtml(state.bootstrap?.readiness ?? "读取中")}</span><button class="button secondary" data-page="datasets">管理聊天记录</button></div></article>
      <article class="setting-row"><div><strong>Pi Connector</strong><span>仅安装固定扩展文件，不复制聊天数据。</span></div><div class="setting-actions"><span class="status-pill ${state.connectorInstalled ? "ok" : "neutral"}">${connector}</span><button class="button secondary" data-action="install-connector">安装 / 更新</button><button class="button danger" data-action="uninstall-connector">卸载</button></div></article>
      <article class="setting-row"><div><strong>DeepSeek AI</strong><span>${state.deepseekConfigured ? `已连接 · 已准备 ${modelCount} 个模型` : "尚未连接 AI 服务"}</span></div><div class="setting-actions"><span class="status-pill ${state.deepseekConfigured ? "ok" : "warn"}">${state.deepseekConfigured ? "已连接" : "未连接"}</span><button class="button secondary" data-action="open-deepseek-config">${state.deepseekConfigured ? "更新 AI 设置" : "连接 AI"}</button></div></article>
      <article class="setting-row"><div><strong>Pi 运行环境</strong><span>${pi ? `版本 ${escapeHtml(pi.version)} · transport ${escapeHtml(pi.transport)}` : "检查中…"}</span></div><span class="status-pill ${pi?.auth_available && pi?.proxy_reachable ? "ok" : "neutral"}">${pi ? (pi.auth_available && pi.proxy_reachable ? "可启动" : "需检查") : "检查中…"}</span></article>
      <article class="setting-row"><div><strong>外部 Pi 会话</strong><span>在 iTerm 中打开 Pi，读取同一份本地快照上下文。</span></div><button class="button secondary" data-action="launch-pi">打开 Pi</button></article>
      <article class="setting-row"><div><strong>写回企业微信</strong><span>允许 Agent 把结果发回会话；默认关闭，开启后每次发送仍会记录确认时间。</span></div><div class="setting-actions"><span class="status-pill ${state.status?.allow_send_to_wecom ? "warn" : "ok"}">${state.status?.allow_send_to_wecom ? "已开启" : "已关闭"}</span><button class="button ${state.status?.allow_send_to_wecom ? "danger" : "secondary"}" data-action="toggle-send">${state.status?.allow_send_to_wecom ? "关闭写回" : "开启写回"}</button></div></article>
      <article class="setting-row"><div><strong>诊断信息</strong><span>本机会话 ${Object.keys(state.conversations).length} · 最近对话 ${Object.keys(state.summaries).length} · 选择依据 ${escapeHtml(state.bootstrap?.selection_reason || "—")}</span></div><span class="status-pill ${warnings.length ? "warn" : "ok"}">${warnings.length ? `警告 ${warnings.length}` : "正常"}</span></article>
      <article class="setting-row danger-zone"><div><strong>卸载 WeCom Context</strong><span>删除本应用保存的聊天记录、快照、密钥、配置、导出文件和托管 Connector；不会删除企业微信原始聊天数据库。</span></div><button class="button danger" data-action="uninstall-application">${state.pendingConfirm?.token === "uninstall-application" ? "再次点击确认卸载" : "卸载应用"}</button></article>
      <article class="setting-row"><div><strong>清除会话绑定</strong><span>清除服务端绑定和本地恢复记录，不删除任何源数据或模型配置。</span></div><button class="button danger" data-action="clear">清除</button></article>
    </section>`;
}
function renderDeepSeekConfig(): string {
  const editor = state.deepseekEditor;
  if (!editor) return "";
  return `<div class="overlay" data-action="close-deepseek-config">
    <section class="modal">
      <header class="modal-head">
        <div><h2>连接 AI</h2><p class="muted">${state.deepseekConfigured ? "当前已连接。输入新的 API Key 可覆盖现有配置。" : "输入 DeepSeek API Key，验证成功后即可开始提问。"}</p></div>
        <button class="drawer-close" data-action="close-deepseek-config" aria-label="关闭">×</button>
      </header>
      <form class="modal-body" id="deepseek-form">
        <label class="field-label">DeepSeek API Key<input data-deepseek-api-key type="password" autocomplete="off" value="${escapeHtml(editor.apiKey)}" placeholder="sk-…"></label>
        <p class="muted">API Key 只保存到本机配置，不会显示在界面或日志中。</p>
        ${editor.error ? `<div class="inline-error"><strong>配置失败</strong><span>${escapeHtml(editor.error)}</span></div>` : ""}
      </form>
      <footer class="modal-foot">
        <button class="button ghost" type="button" data-action="close-deepseek-config">取消</button>
        <button class="button primary" type="submit" form="deepseek-form" ${editor.busy ? "disabled" : ""}>${editor.busy ? "验证并连接中…" : "验证并连接"}</button>
      </footer>
    </section>
  </div>`;
}

function openDeepSeekConfig(): void {
  if (ASSISTANT_BUSY_STATUS[state.assistant.status]) {
    state.notice = "请先停止当前 DeepSeek 请求，再更新 API Key";
    render();
    return;
  }
  state.deepseekEditor = { apiKey: "", error: null, busy: false };
  render();
}

async function submitDeepSeekConfig(): Promise<void> {
  const editor = state.deepseekEditor;
  if (!editor || editor.busy) return;
  const apiKey = editor.apiKey.trim();
  if (apiKey.length < 8) {
    editor.error = "请输入有效的 DeepSeek API Key";
    render();
    return;
  }
  editor.busy = true;
  editor.error = null;
  render();
  try {
    const result = await api.configureDeepSeek(apiKey);
    state.deepseekConfigured = result.configured;
    state.modelCatalog = result.models.filter((model) => model.provider === "deepseek");
    const selected = state.modelCatalog.some((model) => model.id === state.assistant.model)
      ? state.assistant.model
      : result.default_model;
    state.assistant.model = selected;
    localStorage.setItem(MODEL_KEY, selected);
    state.deepseekEditor = null;
    state.notice = `DeepSeek 配置成功，已拉取 ${state.modelCatalog.length} 个可用模型`;
  } catch (error) {
    const current = state.deepseekEditor;
    if (current) {
      current.busy = false;
      current.error = toCoreError(error).message;
      render();
      return;
    }
    setError(error);
  }
  render();
}


async function loadAgentModels(): Promise<void> {
  const result = await api.listAgentModels();
  state.deepseekConfigured = result.configured;
  state.modelCatalog = result.models.filter((model) => model.provider === "deepseek");
  state.modelsWarning = result.warning;
  if (state.deepseekConfigured && state.modelCatalog.length && !knownModel(state.assistant.model)) {
    state.assistant.model = state.modelCatalog[0].id;
    localStorage.setItem(MODEL_KEY, state.assistant.model);
  }
  render();
}


/**
 * 进行中按钮的统一反馈：按当前忙态令牌给对应按钮加 `.busy`（CSS 画转圈）、禁用并换成进行体文案。
 * 单一事实源——按钮只要用相同的 data-action，就自动获得反馈，不必在每个模板里再写一遍判断。
 */
const BUSY_BUTTONS: Record<string, { actions: string[]; label: string }> = {
  refresh: { actions: ["refresh", "retry-refresh"], label: "刷新中…" },
  "capture-key": { actions: ["capture-key"], label: "取钥中…（约 2-3 分钟）" },
  "remove-dataset": { actions: ["remove-dataset"], label: "删除中…" },
  "restore-dataset": { actions: ["restore-dataset"], label: "恢复中…" },
  "uninstall-application": { actions: ["uninstall-application"], label: "卸载中…" },
  "install-connector": { actions: ["install-connector"], label: "安装中…" },
  "uninstall-connector": { actions: ["uninstall-connector"], label: "卸载中…" },
  "toggle-send": { actions: ["toggle-send"], label: "切换中…" },
  clear: { actions: ["clear"], label: "清除中…" },
  "launch-pi": { actions: ["launch-pi"], label: "打开中…" },
  "datasets-scan": { actions: ["reload-datasets"], label: "正在扫描…" },
  "datasets-reload": { actions: ["reload-bootstrap"], label: "检测中…" },
};

/** 当前忙态令牌 + 作用范围（`capture-key:<dataset_id>` 只作用于那一行）。 */
function activeBusyState(): { token: string; scope: string | null } | null {
  if (state.busy) {
    const separator = state.busy.indexOf(":");
    return separator < 0
      ? { token: state.busy, scope: null }
      : { token: state.busy.slice(0, separator), scope: state.busy.slice(separator + 1) };
  }
  if (state.enterprise.loading) return { token: "datasets-scan", scope: null };
  if (state.bootstrapLoading) return { token: "datasets-reload", scope: null };
  return null;
}

function applyBusyFeedback(): void {
  const busy = activeBusyState();
  if (!busy) return;
  const target = BUSY_BUTTONS[busy.token];
  if (!target) return;
  for (const element of appRoot.querySelectorAll<HTMLButtonElement>("[data-action]")) {
    if (!target.actions.includes(element.dataset.action ?? "")) continue;
    if (busy.scope && element.dataset.datasetId !== busy.scope) continue;
    element.classList.add("busy");
    element.disabled = true;
    element.textContent = target.label;
  }
}

function render(): void {
  const body = state.page === "workbench"
    ? renderWorkbench()
    : state.page === "datasets"
      ? `<main class="content">${renderDatasetsPage()}</main>`
      : `<main class="content">${renderSettings()}</main>`;
  const dataset = state.bootstrap?.dataset;
  appRoot.innerHTML = `<div class="shell">
      ${renderAppNav()}
      <div class="app-stage">
        <header class="topbar">
          <div class="topbar-leading">
            <div class="topbar-heading"><span class="topbar-kicker">WECom CONTEXT</span><strong>${pageTitle()}</strong><span>${pageSubtitle()}</span></div>
            ${dataset ? `<span class="topbar-context"><span class="dot ok"></span>${escapeHtml(dataset.display_name)}</span>` : ""}
          </div>
          <div class="topbar-actions"><span class="privacy-badge">本机读取；发送时上传所选内容</span><button class="button ghost" data-action="open-memory">对话记录</button><button class="button secondary" data-action="refresh" ${state.busy === "refresh" ? "disabled" : ""}>更新聊天记录</button></div>
        </header>
        ${banners()}
        ${warningStrip()}
        ${body}
        ${state.enterprise.open ? renderEnterprisePanel() : ""}
        ${renderMemory()}
        ${state.deepseekEditor ? renderDeepSeekConfig() : ""}
        ${renderImageViewer()}
      </div>
    </div>`;
  // 忙态反馈必须在整树重建之后施加：DOM 是新的，模板里不再重复写进行体文案。
  applyBusyFeedback();
  syncThinkingLabels();
  scrollTranscript();
}

function switchPage(page: Page): void {
  state.page = page;
  state.pendingConfirm = null;
  if (page !== "workbench") {
    state.drawer.open = false;
    state.imageViewer = null;
    state.pendingSend = null;
    state.epochs.preview += 1;
    state.enterprise.open = false;
    state.deepseekEditor = null;
  }
  render();
  if (page === "datasets") void loadDatasets();
  if (page === "settings") void loadSettingsInfo();
}

/* ------------------------------------------------------------------ *
 * 加载
 * ------------------------------------------------------------------ */

function upsertConversations(result: WorkbenchBootstrap): void {
  const datasetId = result.dataset?.dataset_id ?? "";
  const snapshotId = result.snapshot?.snapshot_id ?? "";
  const next: Record<string, ConversationState> = Object.fromEntries(
    Object.entries(state.conversations).filter(([, conv]) => conv.datasetId !== datasetId),
  );
  let snapshotBindingsChanged = false;
  for (const session of result.sessions) {
    if (CONVERSATIONAL_KINDS[session.kind] !== true) continue;
    const sid = session.dataset_id ?? datasetId;
    if (sid !== datasetId) continue;
    const id = compositeKey(sid, session.session_key);
    const existing = state.conversations[id];
    const summary = result.conversations.find((item) => item.session_key === session.session_key && item.dataset_id === sid);
    const source: ConversationState["source"] = summary ? "both" : "snapshot";
    if (state.selectedSources.includes(id) && !state.selectedSourceSnapshots[id] && snapshotId) {
      state.selectedSourceSnapshots[id] = snapshotId;
      snapshotBindingsChanged = true;
    }
    if (existing) {
      existing.name = session.display_name || existing.name;
      existing.kind = session.kind || existing.kind;
      if (session.conversation_id) existing.conversationId = session.conversation_id;
      if (snapshotId && existing.snapshotId !== snapshotId) {
        existing.snapshotId = snapshotId;
      }
      existing.source = source;
      existing.availability = "current";
      next[id] = existing;
      continue;
    }
    next[id] = {
      id,
      datasetId: sid,
      snapshotId,
      sessionKey: session.session_key,
      conversationId: session.conversation_id ?? "",
      name: session.display_name,
      kind: session.kind,
      source,
      availability: "current",
    };
  }
  for (const summary of result.conversations) {
    const sid = summary.dataset_id || datasetId;
    if (sid !== datasetId) continue;
    const id = compositeKey(sid, summary.session_key);
    const inSnapshot = next[id];
    if (inSnapshot) {
      if (inSnapshot.source === "snapshot") inSnapshot.source = "both";
      continue;
    }
    const existing = state.conversations[id];
    if (existing) {
      existing.name = summary.session_name || existing.name;
      existing.source = "history";
      existing.availability = "history_only";
      next[id] = existing;
      continue;
    }
    next[id] = {
      id,
      datasetId: sid,
      snapshotId: summary.snapshot_id || snapshotId,
      conversationId: "",
      sessionKey: summary.session_key,
      name: summary.session_name || summary.session_key,
      kind: "",
      source: "history",
      availability: "history_only",
    };
  }
  for (const [id, conv] of Object.entries(state.conversations)) {
    if (next[id] || conv.datasetId !== datasetId) continue;
    conv.availability = "gone";
    next[id] = conv;
  }
  if (snapshotBindingsChanged) saveSelectedSources();
  state.conversations = next;
}

function applyBootstrap(result: WorkbenchBootstrap): void {
  const datasetId = result.dataset?.dataset_id ?? "";
  state.bootstrap = result;
  state.warningsDismissed = {};
  const sessions: Record<string, SessionSummary> = {};
  for (const session of result.sessions) {
    if (CONVERSATIONAL_KINDS[session.kind] !== true) continue;
    sessions[compositeKey(session.dataset_id ?? datasetId, session.session_key)] = session;
  }
  state.sessions = sessions;
  const summaries: Record<string, AgentConversationSummary> = {};
  for (const summary of result.conversations) {
    if ((summary.dataset_id || datasetId) !== datasetId) continue;
    summaries[compositeKey(summary.dataset_id || datasetId, summary.session_key)] = summary;
  }
  state.summaries = summaries;
  upsertConversations(result);
  // 左栏没有独立的“当前联系人”；选中态只代表本轮注入来源。
}

async function loadAccountPicker(): Promise<void> {
  state.enterprise.open = true;
  state.enterprise.mode = "accounts";
  await loadDatasets();
}

async function loadBootstrap(): Promise<void> {
  const epoch = ++state.epochs.bootstrap;
  state.bootstrapLoading = true;
  render();
  let result: WorkbenchBootstrap;
  try {
    result = await api.bootstrapWorkbench();
  } catch (error) {
    if (epoch !== state.epochs.bootstrap) return;
    state.bootstrapLoading = false;
    setError(error);
    return;
  }
  if (epoch !== state.epochs.bootstrap) return;
  state.bootstrapLoading = false;
  state.error = null;
  applyBootstrap(result);
  if (state.busy === "refresh") {
    state.busy = null;
    state.notice = "聊天记录已更新；本地对话与草稿已保留";
  }
  render();
  if (result.readiness === "needs_dataset") {
    state.enterprise.open = true;
    state.enterprise.mode = "accounts";
    void loadDatasets();
  }
  if (result.readiness === "ready") void loadAssistantHistory();
}

async function loadDatasets(): Promise<void> {
  state.enterprise.loading = true;
  render();
  try {
    const result = await api.datasets();
    state.enterprise.datasets = result.datasets.filter((item) => item.kind !== "backup");
    state.enterprise.accounts = result.accounts ?? [];
    state.enterprise.currentAccount = result.current_account ?? null;
    state.enterprise.selectedAccount = result.selected_account ?? null;
    state.ignoredDatasets = (result.ignored ?? []).filter((item) => item.kind !== "backup");
    state.enterprise.error = null;
    if (result.scan_deferred) {
      state.notice = "容器扫描超时，本次企业列表来自本地缓存；稍后再点「重新扫描」试试";
    }
  } catch (error) {
    state.enterprise.error = toCoreError(error).message;
  }
  state.enterprise.loading = false;
  render();
}
async function selectAccount(accountId: string): Promise<void> {
  const account = state.enterprise.accounts.find((item) => item.account_id === accountId);
  if (!account?.dataset_id) return;
  state.busy = "account-select";
  state.error = null;
  state.pendingConfirm = null;
  render();
  try {
    await api.selectDataset(account.dataset_id);
    state.enterprise.selectedAccount = account;
    state.enterprise.mode = "companies";
    state.enterprise.open = true;
    state.imageViewer = null;
    state.pendingSend = null;
    state.drawer = { ...EMPTY_DRAWER };
    state.epochs.bootstrap += 1;
    state.epochs.preview += 1;
    state.notice = `已选择账户「${account.account_name}」；请选择该账户名下的公司`;
    await loadDatasets();
  } catch (error) {
    setError(error);
  }
  state.busy = null;
  render();
}

async function selectCompany(datasetId: string): Promise<void> {
  const dataset = state.enterprise.datasets.find((item) => item.dataset_id === datasetId);
  if (!dataset) return;
  state.busy = "company-select";
  state.error = null;
  render();
  try {
    await api.selectDataset(dataset.dataset_id);
    state.enterprise.open = !dataset.key_available;
    state.enterprise.mode = "companies";
    state.imageViewer = null;
    state.pendingSend = null;
    state.drawer = { ...EMPTY_DRAWER };
    state.epochs.bootstrap += 1;
    state.epochs.preview += 1;
    state.notice = dataset.key_available
      ? `已选择公司「${dataset.company_name || dataset.display_name}」`
      : `已选择公司「${dataset.company_name || dataset.display_name}」；完成连接后即可读取聊天`;
    await loadBootstrap();
  } catch (error) {
    setError(error);
  }
  state.busy = null;
  render();
}
async function removeAccount(accountId: string, companyName: string): Promise<void> {
  state.busy = `remove-account:${accountId}`;
  state.error = null;
  state.pendingConfirm = null;
  render();
  try {
    const result = await api.removeAccount(accountId);
    state.enterprise.accounts = result.accounts ?? [];
    state.enterprise.selectedAccount = result.selected_account ?? null;
    state.ignoredDatasets = result.ignored ?? [];
    state.notice = `已彻底删除「${companyName}」在本应用内的数据：${result.deleted_datasets} 个数据集、${result.deleted_snapshots} 个快照、${result.deleted_keys} 个密钥；企业微信原始数据库未改动`;
    if (state.bootstrap?.selected_account?.account_id === accountId) {
      state.bootstrap = null;
      state.sessions = {};
      state.summaries = {};
      state.conversations = {};
    }
  } catch (error) {
    setError(error);
  }
  state.busy = null;
  await loadDatasets();
  render();
}

/** 「重新扫描」按钮：扫描期间由 applyBusyFeedback 加转圈与进行体文案，这里只做禁用。 */
function scanButtonHtml(variant: string, label: string): string {
  return `<button class="button ${variant}" data-action="reload-datasets" ${state.enterprise.loading ? "disabled" : ""}>${label}</button>`;
}

async function loadStatus(): Promise<void> {
  try {
    state.status = await api.status();
  } catch {
    /* 诊断信息不可用不影响工作台主流程 */
    state.status = null;
  }
  if (state.page === "settings" || state.drawer.open) render();
}

async function loadSettingsInfo(): Promise<void> {
  const jobs: Array<Promise<void>> = [];
  if (state.connectorInstalled == null) {
    jobs.push(api.connector().then((result) => { state.connectorInstalled = result.installed; }).catch(() => { state.connectorInstalled = false; }));
  }
  if (state.piEnvironment == null) {
    jobs.push(api.piEnvironment().then((result) => { state.piEnvironment = result; }).catch(() => { state.piEnvironment = null; }));
  }
  if (state.modelCatalog == null) {
    jobs.push(loadAgentModels().catch(() => {}));
  }
  await Promise.all(jobs);
  await loadStatus();
  if (state.page === "settings") render();
}

/** 资料源失效只更新来源元数据，不把联系人历史写进主助手 transcript。 */
function markSourceHistoryOnly(conv: ConversationState, error: CoreError): boolean {
  if (GONE_CODES[error.code] !== true || !historyCandidateIds(error).length) return false;
  conv.availability = "history_only";
  if (conv.source === "both") conv.source = "history";
  state.notice = `「${conv.name}」当前记录不可用，已标记为仅可查看历史；它不会自动进入本对话`;
  return true;
}

/** 主助手历史以 Pi session 为事实源；打开资料行不会读取或切换它。 */
async function loadAssistantHistory(): Promise<void> {
  const agent = state.assistant;
  if (agent.historyLoaded || state.historyLoading["main-agent"]) return;
  state.historyLoading["main-agent"] = true;
  const epoch = ++state.epochs.history;
  renderTranscript();
  try {
    const history = await api.memoryHistory(0, 200);
    if (epoch !== state.epochs.history) return;
    const target = state.assistant;
    if (!target.messages.length) {
      target.messages = history.messages.map((message) => ({ role: message.role, text: message.text }));
    }
    target.memoryEpoch = history.memory_epoch;
    target.historyLoaded = true;
    target.error = null;
  } catch (error) {
    if (epoch !== state.epochs.history) return;
    state.assistant.error = toCoreError(error).code;
    state.assistant.historyLoaded = true;
  } finally {
    delete state.historyLoading["main-agent"];
    if (epoch === state.epochs.history) renderTranscript();
  }
}

/* ------------------------------------------------------------------ *
 * 会话与消息
 * ------------------------------------------------------------------ */


/**
 * 本轮资料来源：来源自身携带企业、快照、会话身份；浏览中的当前企业不能覆盖它。
 */
function sourceIsStale(conv: ConversationState): boolean {
  const selectedSnapshotId = state.selectedSourceSnapshots[conv.id];
  return Boolean(selectedSnapshotId && conv.snapshotId && selectedSnapshotId !== conv.snapshotId);
}

function sourceRequestFor(conv: ConversationState, snapshotId: string, options: SourceOptions = state.sourceOptions): AgentSourceRequest | null {
  if (!conv.datasetId || !snapshotId || !/^[0-9a-f]{16}$/.test(conv.sessionKey)) return null;
  const window = resolvedSourceWindow(options);
  return {
    dataset_id: conv.datasetId,
    snapshot_id: snapshotId,
    session_key: conv.sessionKey,
    limit: options.limit,
    ...(window.start ? { start: window.start } : {}),
    ...(window.end ? { end: window.end } : {}),
    include_images: options.includeImages && conv.availability !== "history_only",
  };
}

function sourceFor(conv: ConversationState, options: SourceOptions = state.sourceOptions): AgentSourceRequest[] {
  const source = sourceRequestFor(conv, state.selectedSourceSnapshots[conv.id] ?? conv.snapshotId, options);
  return source ? [source] : [];
}


async function sendMessage(): Promise<void> {
  const agent = state.assistant;
  const text = agent.draft.trim();
  if (!state.deepseekConfigured) {
    state.notice = "请先配置 DeepSeek API Key";
    render();
    return;
  }
  if (!text || ASSISTANT_BUSY_STATUS[agent.status] || state.busy === "switch") return;
  const model = knownModel(agent.model) ? agent.model : DEFAULT_AGENT_MODEL;
  const selected = selectedSourceConversations();
  const sources = selectedSourcesRequest();
  await submitAgentMessage({
    text,
    model,
    sources,
    names: selected.map((conv) => conv.name),
    datasetLabels: selected.map(sourceDatasetLabel),
    packageKey: "",
    packageOptions: sourcePackageOptions(),
    crossEnterprise: selected.some((conv) => conv.datasetId !== selected[0]?.datasetId),
    crossEnterpriseConfirmed: true,
  });
}

async function confirmPendingSend(): Promise<void> {
  const pending = state.pendingSend;
  if (!pending) return;
  if (pending.crossEnterprise && !pending.crossEnterpriseConfirmed) return;
  if (state.drawer.cacheKey !== pending.packageKey || !state.drawer.pkg) {
    state.pendingSend = null;
    state.notice = "资料预览已失效，请重新配置与预览";
    render();
    return;
  }
  state.pendingSend = null;
  state.drawer = { ...state.drawer, open: false };
  render();
  await submitAgentMessage(pending);
}

async function submitAgentMessage(request: PendingSend): Promise<void> {
  const agent = state.assistant;
  if (ASSISTANT_BUSY_STATUS[agent.status] || state.busy === "switch") return;
  const submission = ++assistantSubmissionSeq;
  let sourcesConsumed = false;
  agent.error = null;
  agent.status = request.sources.length ? "preparing" : "starting";
  agent.runId = null;
  agent.requestId = null;
  armThinking(agent, "starting");
  renderTranscript();
  try {
    const context = request.sources.length
      ? {
        sources: request.sources,
        packageId: state.drawer.pkg?.package_id,
        ...request.packageOptions,
      }
      : {};
    const result = await api.agentSendMessage({
      clientToken: MAIN_AGENT_CLIENT_TOKEN,
      model: request.model,
      text: request.text,
      ...context,
    });
    if (submission !== assistantSubmissionSeq) return;
    if (result.accepted === false) {
      throw { code: "AGENT_NOT_ACCEPTED", message: "助手没有接受本次请求，请稍后重试", retryable: true };
    }
    if (agent.draft === request.text) agent.draft = "";
    agent.messages.push({ role: "user", text: request.text });
    agent.requestId = result.request_id;
    agent.runId = result.run_id || null;
    agent.memoryEpoch = result.memory_epoch;
    agent.status = "generating";
    if (request.sources.length) {
      const images = result.image_count ?? 0;
      const omitted = result.omitted_image_count ?? 0;
      const imageNote = omitted ? `，另有 ${omitted} 张未找到或超出限制` : "";
      agent.messages.push({
        role: "system",
        text: `本次使用聊天：${request.names.join("、") || "已选聊天"}（图片 ${images} 张${imageNote}）；已加入当前对话，可继续追问`,
      });
      // 来源是一次性草案：本轮接受后消费，助手已记住的资料由记忆面板单独查看。
      state.selectedSources = [];
      state.selectedSourceSnapshots = {};
      saveSelectedSources();
      invalidatePackagePreview();
      sourcesConsumed = true;
    }
  } catch (error) {
    if (submission !== assistantSubmissionSeq) return;
    handleSendFailure(agent, toCoreError(error), request.text);
  }
  if (submission === assistantSubmissionSeq) {
    if (sourcesConsumed) render();
    else renderTranscript();
  }
}

function handleSendFailure(agent: AssistantState, error: CoreError, text: string): void {
  agent.messages.push({ role: "error", text: `${error.message}${error.retryable ? "（可重试）" : ""}` });
  agent.status = "failed";
  agent.runId = null;
  agent.requestId = null;
  agent.error = error.code;
  endThinking(agent);
  // 用户可能在异步请求期间继续编辑；只有草稿仍为空时才恢复失败请求。
  if (!agent.draft.trim()) agent.draft = text;
  if (GENERATION_CODES[error.code] === true) {
    state.notice = error.message || "资料版本已变化，请重新配置与预览";
    void loadBootstrap();
  }
}

async function stopAssistant(): Promise<void> {
  const agent = state.assistant;
  if (!ASSISTANT_LIVE_STATUS[agent.status] || agent.status === "idle") return;
  assistantSubmissionSeq += 1;
  agent.status = "stopped";
  agent.runId = null;
  agent.requestId = null;
  endThinking(agent);
  agent.messages.push({ role: "system", text: "已停止当前回答；下次发送会继续使用本对话记录。" });
  renderTranscript();
  try {
    await api.agentStop();
  } catch {
    /* 进程可能已退出 */
  }
}

async function changeModel(model: string): Promise<void> {
  const agent = state.assistant;
  if (!knownModel(model) || agent.model === model) return;
  if (ASSISTANT_BUSY_STATUS[agent.status]) {
    state.notice = "助手正在处理请求，请先停止后再切换模型";
    render();
    return;
  }
  if (state.pendingSend) {
    invalidatePackagePreview();
    state.notice = "模型已变化，旧资料预览确认已失效，请重新预览";
  }
  agent.model = model;
  localStorage.setItem(MODEL_KEY, model);
  agent.error = null;
  render();
}

/** 重试说明只保留一条（同一条消息就地更新），不随重试次数刷屏。 */
const RETRY_NOTE_PREFIX = "模型重试：";

function replaceRetryNote(agent: AssistantState, text: string | null, role: "system" | "error" = "system"): void {
  const index = agent.messages.findIndex((message) => message.text.startsWith(RETRY_NOTE_PREFIX));
  if (text === null) {
    if (index >= 0) agent.messages.splice(index, 1);
    return;
  }
  const note = { role, text: `${RETRY_NOTE_PREFIX}${text}` } as const;
  if (index >= 0) agent.messages[index] = { role: note.role, text: note.text };
  else agent.messages.push({ role: note.role, text: note.text });
}

function eventText(message: unknown): string {
  if (typeof message === "string") return message;
  if (Array.isArray(message)) {
    return message.map((item) => eventText(item)).filter(Boolean).join("");
  }
  if (!message || typeof message !== "object") return "";
  const record = message as Record<string, unknown>;
  if (typeof record.text === "string") return record.text;
  if (typeof record.content === "string") return record.content;
  if (Array.isArray(record.content)) return eventText(record.content);
  if (record.delta !== undefined && record.delta !== message) return eventText(record.delta);
  if (record.message !== undefined && record.message !== message) return eventText(record.message);
  return "";
}

function normaliseAgentEvent(raw: unknown): Record<string, unknown> | null {
  if (typeof raw === "string") {
    try {
      const parsed: unknown = JSON.parse(raw);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null;
    } catch {
      return null;
    }
  }
  return raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : null;
}

function handleAgentEvent(payload: { conversation_id?: string; run_id?: string; request_id?: string; memory_epoch?: number; event?: unknown }): void {
  const agent = state.assistant;
  if (payload.memory_epoch !== undefined && payload.memory_epoch !== agent.memoryEpoch) return;
  const event = normaliseAgentEvent(payload.event);
  if (!event) return;
  const runId = payload.run_id ?? null;
  const requestId = payload.request_id ?? null;
  if (runId && agent.runId === null && (agent.status === "starting" || agent.status === "preparing" || agent.status === "generating")) {
    agent.runId = runId;
  }
  if (runId && agent.runId !== runId) return;
  if (requestId && agent.requestId === null && (agent.status === "starting" || agent.status === "preparing" || agent.status === "generating")) {
    agent.requestId = requestId;
  }
  if (requestId && agent.requestId !== requestId) return;
  const type = String(event.type ?? "");
  if (type === "agent_start" || type === "turn_start") {
    agent.status = "generating";
    armThinking(agent, "thinking");
  } else if (type === "message_start") {
    const message = event.message as { role?: string } | undefined;
    if (message?.role === "assistant") {
      const last = agent.messages[agent.messages.length - 1];
      if (!last || last.role !== "assistant" || last.text) agent.messages.push({ role: "assistant", text: "" });
      agent.status = "generating";
      armThinking(agent, "thinking");
    }
  } else if (type === "message_update" || type === "message_delta") {
    const delta = eventText(event.delta ?? event.message ?? event.content);
    if (delta) {
      const last = agent.messages[agent.messages.length - 1];
      if (last?.role === "assistant") last.text += delta;
      else agent.messages.push({ role: "assistant", text: delta });
      agent.status = "generating";
      armThinking(agent, "thinking");
    }
  } else if (type === "message_end") {
    const message = event.message as { role?: string; content?: unknown; errorMessage?: string } | undefined;
    if (message?.role === "assistant") {
      const text = [message.errorMessage ? `⚠ ${message.errorMessage}` : "", eventText(message)].filter(Boolean).join("\n");
      const last = agent.messages[agent.messages.length - 1];
      if (last?.role === "assistant") {
        if (text && !last.text) last.text = text;
      } else if (text) {
        agent.messages.push({ role: "assistant", text });
      }
      agent.status = "generating";
    }
  } else if (type === "agent_end" || type === "turn_end") {
    agent.status = "generating";
  } else if (type === "agent_settled") {
    agent.status = "idle";
    agent.runId = null;
    agent.requestId = null;
    endThinking(agent);
    replaceRetryNote(agent, null);
  } else if (type === "auto_retry_start") {
    const attempt = Number(event.attempt ?? 0);
    const maxAttempts = Number(event.maxAttempts ?? 0);
    const reason = String(event.errorMessage ?? "模型请求失败");
    agent.status = "generating";
    armThinking(agent, "thinking");
    replaceRetryNote(agent, `模型请求失败，正在重试（${attempt}/${maxAttempts}）：${reason}`);
  } else if (type === "auto_retry_end") {
    if (event.success === false) {
      const reason = String(event.finalError ?? "模型请求多次失败");
      agent.status = "failed";
      agent.runId = null;
      endThinking(agent);
      replaceRetryNote(agent, `模型请求失败：${reason}`, "error");
    } else {
      replaceRetryNote(agent, null);
    }
  } else if (type === "process_exit") {
    const detail = String(event.stderr ?? "").trim();
    agent.runId = null;
    agent.requestId = null;
    agent.status = "failed";
    endThinking(agent);
    agent.messages.push({ role: "error", text: detail ? `Agent 进程已退出：\n${detail}` : "Agent 进程已退出" });
  } else {
    return;
  }
  renderTranscript();
}

/** 快照换代只影响资料库身份；不得停止或重置唯一主助手。 */
function handleGenerationChanged(payload: { dataset_id?: string; snapshot_id?: string; invalidated?: string[] }): void {
  const datasetId = payload.dataset_id ?? "";
  const snapshotId = payload.snapshot_id ?? "";
  const invalidated = new Set(payload.invalidated ?? []);
  const affected = Object.values(state.conversations).some((conv) =>
    conv.datasetId === datasetId && (invalidated.has(conv.id) || invalidated.has(conv.sessionKey) || invalidated.has(conv.conversationId)),
  );
  state.drawer = { ...EMPTY_DRAWER };
  state.imageViewer = null;
  state.pendingSend = null;
  state.epochs.preview += 1;
  if (affected) {
    state.notice = snapshotId
      ? "聊天记录已更新；已选择的聊天需要重新查看，本对话和已使用内容不受影响"
      : "聊天记录已变化；请重新选择本次要添加的聊天";
  }
  render();
}

/* ------------------------------------------------------------------ *
 * 操作
 * ------------------------------------------------------------------ */


/** 从应用移除企业：只删除该企业的应用侧快照/会话记录与独占密钥；不停止主助手、不删除企业微信数据库。 */
async function removeDataset(datasetId: string, displayName: string): Promise<void> {
  state.busy = `remove-dataset:${datasetId}`;
  state.error = null;
  state.pendingConfirm = null;
  render();
  try {
    const result = await api.removeDataset(datasetId);
    state.ignoredDatasets = result.ignored ?? [];
    state.notice = `已从应用移除「${displayName}」：删除本机保存的聊天记录和连接信息；本对话记忆未删除，企业微信原始聊天未改动`;
  } catch (error) {
    setError(error);
  }
  state.busy = null;
  await loadDatasets();
  state.epochs.bootstrap += 1;
  await loadBootstrap();
}

async function restoreDataset(datasetId: string): Promise<void> {
  state.busy = `restore-dataset:${datasetId}`;
  state.error = null;
  render();
  try {
    const result = await api.restoreDataset(datasetId);
    state.ignoredDatasets = result.ignored ?? [];
    state.notice = "已重新发现本机聊天记录；需要重新连接并更新后才能使用";
  } catch (error) {
    setError(error);
  }
  state.busy = null;
  await loadDatasets();
}

/** 一键取钥：sidecar 内完成重启企微→签名副本→只读扫描→写密钥→验证→恢复原版；成功后自动刷新快照。 */
async function runCaptureKey(datasetId: string): Promise<void> {
  state.busy = `capture-key:${datasetId}`;
  state.error = null;
  state.pendingConfirm = null;
  render();
  try {
    const result = await api.captureKey(datasetId);
    if (result.already) {
      state.notice = result.message ?? "该企业已连接，无需重复操作";
    } else {
      state.notice = "聊天记录连接成功，正在更新本机记录…";
    }
    state.enterprise.open = false;
    await refreshSnapshot();
    return;
  } catch (error) {
    setError(error);
  }
  state.busy = null;
  render();
}

async function refreshSnapshot(): Promise<void> {
  state.busy = "refresh";
  state.error = null;
  state.pendingConfirm = null;
  render();
  try {
    await api.refresh();
    state.pendingSend = null;
    // 后端会发出 context-generation-changed；它只作废旧资料包，不停止主助手。
    // 这里只重新取 bootstrap，messages / draft / 本轮来源一律保留。
    state.epochs.bootstrap += 1;
    await loadBootstrap();
  } catch (error) {
    setError(error);
  }
}

async function switchEnterprise(dataset: DatasetSummary): Promise<void> {
  state.busy = "switch";
  state.error = null;
  state.pendingConfirm = null;
  render();
  try {
    await api.selectDataset(dataset.dataset_id);
    state.imageViewer = null;
    state.pendingSend = null;
    state.drawer = { ...EMPTY_DRAWER };
    state.epochs.bootstrap += 1;
    state.epochs.preview += 1;
    state.enterprise.open = false;
    state.busy = null;
    state.notice = `已切换到「${dataset.display_name}」并刷新资料库；主助手、当前记忆与本轮来源已保留`;
    await loadBootstrap();
  } catch (error) {
    state.busy = null;
    setError(error);
  }
}

async function pickDataset(datasetId: string): Promise<void> {
  const dataset = state.enterprise.datasets.find((item) => item.dataset_id === datasetId);
  if (!dataset) return;
  if (state.bootstrap?.dataset?.dataset_id === datasetId) {
    state.enterprise.open = false;
    state.notice = `「${dataset.display_name}」已经是当前企业`;
    render();
    return;
  }
  await switchEnterprise(dataset);
}


/** 点「配置与预览」：始终预览本轮勾选的来源；没勾选就说明可直接普通对话。 */
async function openDrawer(): Promise<void> {
  await openPackageDrawer();
}

/** 切换一个来源的勾选状态；选择变化立即作废抽屉里已预览的资料包。 */
function toggleSource(conversationId: string): void {
  const conv = state.conversations[conversationId];
  if (!conv) return;
  const index = state.selectedSources.indexOf(conversationId);
  if (index >= 0) {
    state.selectedSources.splice(index, 1);
    delete state.selectedSourceSnapshots[conversationId];
  } else if (state.selectedSources.length >= 20) {
    state.notice = "本轮最多选择 20 个资料来源；可先移除一个再加入";
    render();
    return;
  } else {
    state.selectedSources.push(conversationId);
    state.selectedSourceSnapshots[conversationId] = conv.snapshotId;
  }
  saveSelectedSources();
  invalidatePackagePreview();
  render();
}

function toggleAllSources(): void {
  state.selectedSources = [];
  state.selectedSourceSnapshots = {};
  saveSelectedSources();
  invalidatePackagePreview();
  state.notice = "已清空本轮来源：之后的对话不会注入企微资料";
  render();
}

function invalidatePackagePreview(): void {
  // 所有来源/配置变化都推进版本，迟到的本地包结果不能覆盖新草案。
  state.epochs.preview += 1;
  state.pendingSend = null;
  state.drawer = {
    ...state.drawer,
    cacheKey: null,
    pkg: null,
    thumbs: {},
    error: null,
  };
}


/** 由勾选来源构造本轮 sources；身份不全的会话直接跳过（宁可少注入也不猜企业与快照）。 */
function selectedSourcesRequest(): AgentSourceRequest[] {
  return selectedSourceConversations().flatMap((conv) => sourceFor(conv));
}
function sourcePackageOptions(): SourcePackageOptions {
  return {
    imageOptions: {
      max_images: state.sourceOptions.maxImages,
      max_total_bytes: state.sourceOptions.maxTotalBytes,
      max_edge_pixels: 2048,
    },
    maxContextTokens: state.sourceOptions.maxContextTokens,
    maxMessageCharacters: state.sourceOptions.maxMessageCharacters,
  };
}

async function openPackageDrawer(): Promise<void> {
  if (state.memory.open) {
    state.epochs.memory += 1;
    state.memory = { ...state.memory, open: false, confirmingClear: false };
  }
  if (!state.selectedSources.length) {
    state.drawer = { open: true, loading: false, cacheKey: null, pkg: null, thumbs: {}, error: null };
    render();
    return;
  }
  const stale = selectedSourceConversations().filter((conv) => sourceIsStale(conv));
  if (stale.length) {
    state.pendingSend = null;
    state.drawer = {
      open: true,
      loading: false,
      cacheKey: null,
      pkg: null,
      thumbs: {},
      error: `已有 ${stale.length} 个来源使用旧快照，请先在左侧点击「改用当前快照」后再预览`,
    };
    render();
    return;
  }
  if (state.drawer.cacheKey === sourcesCacheKey() && state.drawer.pkg) {
    state.drawer = { ...state.drawer, open: true };
    render();
    return;
  }
  const sources = selectedSourcesRequest();
  if (!sources.length) {
    state.drawer = { open: true, loading: false, cacheKey: null, pkg: null, thumbs: {}, error: "所选会话还没有企业或快照身份，请先刷新快照" };
    render();
    return;
  }
  const epoch = ++state.epochs.preview;
  state.drawer = { open: true, loading: true, cacheKey: null, pkg: null, thumbs: {}, error: null };
  render();
  try {
    const pkg = await api.previewContextPackage({ sources, ...sourcePackageOptions() });
    if (epoch !== state.epochs.preview) return;
    state.drawer = { open: true, loading: false, cacheKey: sourcesCacheKey(), pkg, thumbs: {}, error: null };
    render();
    void loadThumbnails(pkg);
  } catch (error) {
    if (epoch !== state.epochs.preview) return;
    const core = toCoreError(error);
    if (core.code === "SNAPSHOT_CHANGED" || core.code === "DATASET_CHANGED" || core.code === "SESSION_GONE") {
      for (const conv of selectedSourceConversations()) markSourceHistoryOnly(conv, core);
    }
    state.drawer = { open: true, loading: false, cacheKey: null, pkg: null, thumbs: {}, error: core.message };
    render();
  }
}

/** 缩略图按需加载：失败不阻塞预览，只留占位说明。 */
async function loadThumbnails(pkg: ContextPackageData): Promise<void> {
  const usable = pkg.images.filter((image) => image.image_id.startsWith("img_"));
  for (const image of usable) {
    try {
      const data = await api.packageImageData(pkg.package_id, image.image_id);
      const activePackageId = state.drawer.pkg?.package_id;
      if (activePackageId !== pkg.package_id) return;
      state.drawer.thumbs[image.image_id] = data.data_url;
    } catch {
      const activePackageId = state.drawer.pkg?.package_id;
      if (activePackageId !== pkg.package_id) return;
      state.drawer.thumbs[image.image_id] = "";
    }
    if (state.drawer.open) render();
  }
}

async function runAction(action: string, label: string): Promise<void> {
  state.busy = action;
  state.error = null;
  try {
    if (action === "clear") {
      await api.clearSession();
      state.notice = "已清除会话绑定与本地恢复记录";
    } else if (action === "launch-pi") {
      await api.launchPi();
      state.notice = "已在 iTerm 打开 Pi";
    } else if (action === "install-connector") {
      await api.installConnector();
      state.connectorInstalled = true;
      state.notice = "Pi Connector 安装完成";
    } else if (action === "uninstall-connector") {
      await api.uninstallConnector();
      state.connectorInstalled = false;
      state.notice = "Pi Connector 已卸载";
    } else if (action === "uninstall-application") {
      await api.uninstallApplication();
      state.notice = "卸载已开始，应用即将退出";
    } else if (action === "toggle-send") {
      const next = !state.status?.allow_send_to_wecom;
      await api.setSendPermission(next);
      await loadStatus();
    } else {
      throw new Error(`未知操作 ${label || action}`);
    }
  } catch (error) {
    setError(error);
    return;
  }
  state.busy = null;
  render();
}

function handleAction(element: HTMLElement, target: HTMLElement): void {
  const action = element.dataset.action ?? "";
  if (action === "dismiss-error") { state.error = null; render(); return; }
  if (action === "dismiss-notice") { state.notice = null; state.accessibilityRequired = false; render(); return; }
  if (action === "open-accessibility-settings") {
    void api.openAccessibilitySettings().catch(setError);
    return;
  }
  if (action === "dismiss-pending") { state.pendingConfirm = null; render(); return; }
  if (action === "reload-datasets") { void loadDatasets(); return; }
  if (action === "reload-bootstrap") { void loadBootstrap(); return; }
  if (action === "toggle-theme") {
    state.theme = state.theme === "dark" ? "light" : "dark";
    try { localStorage.setItem(THEME_KEY, state.theme); } catch { /* 本地存储不可用时仅保持当前会话主题。 */ }
    document.documentElement.dataset.theme = state.theme;
    render();
    return;
  }
  if (action === "open-enterprise") { state.enterprise.open = true; state.pendingConfirm = null; render(); void loadAccountPicker(); return; }
  if (action === "close-enterprise") {
    if (element.classList.contains("overlay") && target.closest(".modal")) return;
    state.enterprise.open = false;
    state.pendingConfirm = null;
    render();
    return;
  }
  if (action === "refresh-source") {
    const id = element.dataset.conversationId ?? "";
    const conv = state.conversations[id];
    if (conv) {
      state.selectedSourceSnapshots[id] = conv.snapshotId;
      saveSelectedSources();
      invalidatePackagePreview();
      state.notice = `已将「${conv.name}」改用当前快照，请重新预览`;
      render();
    }
    return;
  }
  if (action === "download-images") {
    void downloadConversationImages(element.dataset.conversationId ?? "");
    return;
  }
  if (action === "close-image-viewer") {
    state.imageViewer = null;
    render();
    return;
  }
  if (action === "close-drawer") {
    if (element.classList.contains("drawer-overlay") && target.closest(".drawer")) return;
    state.pendingSend = null;
    state.imageViewer = null;
    state.drawer.open = false;
    state.epochs.preview += 1;
    render();
    return;
  }
  if (action === "open-drawer") { void openDrawer(); return; }
  if (action === "quick-task") {
    applyQuickTask(element.dataset.task ?? "");
    return;
  }
  if (action === "new-conversation") {
    void startNewConversation();
    return;
  }
  if (action === "open-memory") { void openMemory(); return; }
  if (action === "close-memory") {
    if (element.classList.contains("overlay") && target.closest(".modal")) return;
    state.epochs.memory += 1;
    state.memory = { ...state.memory, open: false, confirmingClear: false };
    render();
    return;
  }
  if (action === "reload-memory") {
    if (state.assistant.status !== "clearing") void loadMemoryTab(state.memory.tab);
    return;
  }
  if (action === "load-more-memory") {
    const tab = element.dataset.memoryTab;
    if (tab === "history" || tab === "injections") void loadMoreMemory(tab);
    return;
  }
  if (action === "clear-memory") {
    if (state.assistant.status !== "clearing") {
      state.memory.confirmingClear = true;
      state.memory.error = null;
      render();
    }
    return;
  }
  if (action === "cancel-memory-clear") {
    state.memory.confirmingClear = false;
    render();
    return;
  }
  if (action === "confirm-memory-clear") {
    void clearMemory();
    return;
  }
  if (action === "reset-conversation") {
    requestConversationReset();
    return;
  }
  if (action === "disable-source-images") {
    state.pendingSend = null;
    state.sourceOptions = { ...state.sourceOptions, includeImages: false };
    saveSourceOptions();
    invalidatePackagePreview();
    state.notice = "已改为仅文字；请重新生成预览后再确认发送";
    render();
    return;
  }
  if (action === "confirm-send") { void confirmPendingSend(); return; }
  if (action === "cancel-send") {
    state.pendingSend = null;
    render();
    return;
  }
  if (action === "complete-preview") {
    state.drawer.open = false;
    render();
    return;
  }
  if (action === "preview-sources") { void openPackageDrawer(); return; }
  if (action === "clear-sources") { toggleAllSources(); return; }
  if (action === "retry-preview") { void openPackageDrawer(); return; }
  if (action === "retry-refresh") { void refreshSnapshot(); return; }
  if (action === "stop-agent") { void stopAssistant(); return; }
  if (action === "refresh") {
    if (requireConfirm("refresh", CONFIRM_LABELS.refresh)) void refreshSnapshot();
    return;
  }
  if (action === "toggle-send") {
    if (requireConfirm("toggle-send", CONFIRM_LABELS["toggle-send"])) void runAction("toggle-send", "切换写回企业微信权限");
    return;
  }
  if (action === "clear") {
    if (requireConfirm("clear", CONFIRM_LABELS.clear)) void runAction("clear", "清除当前会话绑定");
    return;
  }
  if (action === "install-connector") {
    if (requireConfirm("install-connector", CONFIRM_LABELS["install-connector"])) void runAction("install-connector", "安装或更新 Pi Connector");
    return;
  }
  if (action === "uninstall-application") {
    if (requireConfirm("uninstall-application", CONFIRM_LABELS["uninstall-application"])) {
      void runAction("uninstall-application", "卸载 WeCom Context");
    }
    return;
  }
  if (action === "uninstall-connector") {
    if (requireConfirm("uninstall-connector", CONFIRM_LABELS["uninstall-connector"])) void runAction("uninstall-connector", "卸载 Pi Connector");
    return;
  }
  if (action === "launch-pi") void runAction("launch-pi", "打开 Pi");
  if (action === "capture-key") {
    const datasetId = element.dataset.datasetId ?? "";
    if (requireConfirm(`capture-key:${datasetId}`, CONFIRM_LABELS["capture-key"])) void runCaptureKey(datasetId);
    return;
  }
  if (action === "select-account") {
    void selectAccount(element.dataset.accountId ?? "");
    return;
  }
  if (action === "select-company") {
    void selectCompany(element.dataset.datasetId ?? "");
    return;
  }
  if (action === "back-account-picker") {
    state.enterprise.mode = "accounts";
    state.enterprise.open = true;
    void loadDatasets();
    return;
  }
  if (action === "remove-account") {
    const accountId = element.dataset.accountId ?? "";
    const account = state.enterprise.accounts.find((item) => item.account_id === accountId);
    if (accountId && account && requireConfirm(`remove-account:${accountId}`, CONFIRM_LABELS["remove-account"])) {
      void removeAccount(accountId, account.account_name);
    }
    return;
  }
  if (action === "remove-dataset") {
    const datasetId = element.dataset.datasetId ?? "";
    const dataset = state.enterprise.datasets.find((item) => item.dataset_id === datasetId);
    if (datasetId && requireConfirm(`remove-dataset:${datasetId}`, CONFIRM_LABELS["remove-dataset"])) {
      void removeDataset(datasetId, dataset?.display_name ?? datasetId);
    }
    return;
  }
  if (action === "restore-dataset") {
    void restoreDataset(element.dataset.datasetId ?? "");
    return;
  }
  if (action === "open-deepseek-config") { openDeepSeekConfig(); return; }
  if (action === "close-deepseek-config") {
    if (element.classList.contains("overlay") && target.closest(".modal")) return;
    state.deepseekEditor = null;
    render();
    return;
  }
}

let railResize: { startX: number; startWidth: number; pointerId: number } | null = null;

function applyRailWidth(width: number): void {
  state.railWidth = clampRailWidth(width);
  document.querySelector<HTMLElement>(".workbench")?.style.setProperty("--rail-width", `${state.railWidth}px`);
}

function finishRailResize(target?: HTMLElement): void {
  if (!railResize) return;
  railResize = null;
  target?.classList.remove("dragging");
  document.body.classList.remove("rail-resizing");
  saveRailWidth();
  render();
}

appRoot.addEventListener("pointerdown", (event) => {
  const target = (event.target as HTMLElement | null)?.closest<HTMLElement>("[data-rail-resizer]");
  if (!target || window.matchMedia("(max-width: 700px)").matches) return;
  railResize = { startX: event.clientX, startWidth: state.railWidth, pointerId: event.pointerId };
  target.classList.add("dragging");
  target.setPointerCapture?.(event.pointerId);
  document.body.classList.add("rail-resizing");
  event.preventDefault();
});

appRoot.addEventListener("pointermove", (event) => {
  if (!railResize || event.pointerId !== railResize.pointerId) return;
  applyRailWidth(railResize.startWidth + event.clientX - railResize.startX);
});

appRoot.addEventListener("pointerup", (event) => {
  if (!railResize || event.pointerId !== railResize.pointerId) return;
  finishRailResize((event.target as HTMLElement | null)?.closest<HTMLElement>("[data-rail-resizer]") ?? undefined);
});

appRoot.addEventListener("pointercancel", (event) => {
  if (!railResize || event.pointerId !== railResize.pointerId) return;
  finishRailResize((event.target as HTMLElement | null)?.closest<HTMLElement>("[data-rail-resizer]") ?? undefined);
});

/* ------------------------------------------------------------------ *
 * 事件绑定
 * ------------------------------------------------------------------ */

appRoot.addEventListener("click", (event) => {
  const target = event.target as HTMLElement | null;
  if (!target) return;
  const pageButton = target.closest<HTMLElement>("[data-page]");
  if (pageButton) {
    switchPage(pageButton.dataset.page as Page);
    return;
  }
  const imageView = target.closest<HTMLElement>("[data-image-view]");
  if (imageView) {
    void openImageViewer(imageView.dataset.imageView ?? "");
    return;
  }
  const sourceRemove = target.closest<HTMLElement>("[data-source-remove]");
  if (sourceRemove) {
    toggleSource(sourceRemove.dataset.sourceRemove ?? "");
    return;
  }
  const sourceToggle = target.closest<HTMLElement>("[data-source-toggle]");
  if (sourceToggle) {
    toggleSource(sourceToggle.dataset.sourceToggle ?? "");
    return;
  }
  const dismissWarning = target.closest<HTMLElement>("[data-warning-dismiss]");
  if (dismissWarning) {
    const key = dismissWarning.dataset.warningDismiss;
    if (key) {
      state.warningsDismissed[key] = true;
      render();
    }
    return;
  }
  const memoryTab = target.closest<HTMLElement>("[data-memory-tab]");
  if (memoryTab) {
    void loadMemoryTab((memoryTab.dataset.memoryTab ?? "context") as MemoryTab);
    return;
  }
  const kindButton = target.closest<HTMLElement>("[data-kind]");
  if (kindButton) {
    const kind = kindButton.dataset.kind;
    state.kind = kind === "private" || kind === "group" ? kind : "all";
    render();
    return;
  }
  // 数据集行里既有「点击整行选择企业」，也有行内按钮（取钥 / 删除），
  // 行外还套着 data-action="close-enterprise" 的 overlay：
  // 只有「行内按钮比整行更靠近点击目标」时才走按钮，否则走整行选择；
  // 两者都没有时才落到更外层的 action（如 overlay 关闭）。
  const actionButton = target.closest<HTMLElement>("[data-action]");
  const datasetButton = target.closest<HTMLElement>("[data-dataset-id]");
  if (datasetButton && !(actionButton && datasetButton.contains(actionButton))) {
    if (state.busy !== "switch") void pickDataset(datasetButton.dataset.datasetId ?? "");
    return;
  }
  if (actionButton) handleAction(actionButton, target);
});

appRoot.addEventListener("input", (event) => {
  const input = event.target as HTMLInputElement | HTMLTextAreaElement | null;
  if (!input) return;
  if (input.matches("[data-search]")) {
    state.query = input.value;
    renderRailList();
    return;
  }
  if (input.matches("[data-draft]")) {
    state.assistant.draft = input.value;
    syncSendButton();
    return;
  }
  if (input.matches("[data-deepseek-api-key]") && state.deepseekEditor) {
    state.deepseekEditor.apiKey = input.value;
    return;
  }
});

appRoot.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    if (state.imageViewer) {
      state.imageViewer = null;
    } else if (state.memory.open) {
      state.epochs.memory += 1;
      state.memory = { ...state.memory, open: false, confirmingClear: false };
    } else if (state.deepseekEditor) {
      state.deepseekEditor = null;
      state.pendingConfirm = null;
    } else if (state.enterprise.open) {
      state.enterprise.open = false;
      state.pendingConfirm = null;
    } else if (state.drawer.open) {
      state.pendingSend = null;
      state.drawer.open = false;
      state.epochs.preview += 1;
    }
    render();
    return;
  }
  const railHandle = (event.target as HTMLElement | null)?.closest<HTMLElement>("[data-rail-resizer]");
  if (railHandle && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    const next = event.key === "Home"
      ? MIN_RAIL_WIDTH
      : event.key === "End"
        ? MAX_RAIL_WIDTH
        : state.railWidth + (event.key === "ArrowRight" ? 16 : -16);
    applyRailWidth(next);
    saveRailWidth();
    render();
    event.preventDefault();
    return;
  }
  const textarea = event.target as HTMLTextAreaElement | null;
  if (!textarea?.matches("[data-draft]") || event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  void sendMessage();
});

appRoot.addEventListener("change", (event) => {
  const target = event.target as HTMLSelectElement | HTMLInputElement | null;
  if (!target) return;
  const sourceOption = target.dataset.sourceOption;
  if (sourceOption) {
    updateSourceOption(sourceOption, target);
    return;
  }
  if (target.matches("[data-cross-enterprise-confirm]")) {
    if (state.pendingSend && target instanceof HTMLInputElement) {
      state.pendingSend = { ...state.pendingSend, crossEnterpriseConfirmed: target.checked };
      render();
    }
    return;
  }
  if (!target.matches("[data-model]")) return;
  void changeModel((target as HTMLSelectElement).value);
});

appRoot.addEventListener("submit", (event) => {
  const form = event.target as HTMLElement | null;
  if (form?.matches("#deepseek-form")) {
    event.preventDefault();
    void submitDeepSeekConfig();
    return;
  }
  if (!form?.matches("#composer")) return;
  event.preventDefault();
  void sendMessage();
});

onAgentEvent(handleAgentEvent);
onContextGenerationChanged(handleGenerationChanged);

// 应用启动时读取用户已配置的 DeepSeek API Key 对应模型目录。
void loadAgentModels().catch(() => {});
void loadBootstrap();
void loadStatus();
