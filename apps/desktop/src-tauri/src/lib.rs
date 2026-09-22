mod main_agent;
mod memory;
mod provider_config;
mod sidecar;
mod uninstall;


use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use tauri::{AppHandle, Manager};

use sidecar::CoreError;

/// 每个联系人一个 Agent 工作目录，并按企业（数据集）分片：
/// `<app_data>/agents/<dataset_id>/<session_key>/`。
/// 对话记录与 Agent 工作目录同址，从而严格保持「一个企业的一个联系人一个对话」。
const CONVERSATION_FILE: &str = "conversation.jsonl";
const CONVERSATION_META_FILE: &str = "conversation.json";
const PREVIEW_CHARACTERS: usize = 80;
/// 迁移时无法确定企业归属的历史目录落在这里，且永不进入任何列表。
const UNASSIGNED_DATASET_DIR: &str = "_unassigned";
const LEGACY_HISTORY_UNASSIGNED: &str = "LEGACY_HISTORY_UNASSIGNED";
const LEGACY_HISTORY_MIGRATION_FAILED: &str = "LEGACY_HISTORY_MIGRATION_FAILED";

/// 唯一主 Agent 的状态与进程生命周期在 `main_agent` 模块内维护（见 `main_agent.rs`）。
/// 本文件只保留旧布局的只读历史读取、配置读取与 Tauri 命令。
fn agent_key(dataset_id: &str, session_key: &str) -> String {
    format!("{dataset_id}:{session_key}")
}

pub(crate) fn new_run_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    // 毫秒 + 进程 id + 进程内自增，保证同一进程内单调不重复。
    format!(
        "{}-{}-{}",
        now_millis(),
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    )
}

pub(crate) fn validate_model(model: &str) -> Result<(), CoreError> {
    let parts: Vec<&str> = model.split('/').collect();
    let valid = parts.len() == 2
        && parts[0] == "deepseek"
        && parts[1].len() >= 2
        && parts[1].chars().all(|c| {
            c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '-' || c == '_'
        });
    if valid {
        Ok(())
    } else {
        Err(CoreError::local("INVALID_REQUEST", "模型标识无效", false))
    }
}

pub(crate) fn now_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as u64)
        .unwrap_or(0)
}

fn is_hex_token(value: &str, min: usize, max: usize) -> bool {
    value.len() >= min && value.len() <= max && value.chars().all(|c| c.is_ascii_hexdigit())
}

/// 会话哈希固定 16 位十六进制。
fn valid_session_key(session_key: &str) -> bool {
    is_hex_token(session_key, 16, 16)
}

/// 数据集 id 是本机数据目录的短哈希（实测 12 位）；只约束十六进制与长度区间，
/// 这样 `_unassigned` 之类的辅助目录天然被排除。
fn valid_dataset_id(dataset_id: &str) -> bool {
    is_hex_token(dataset_id, 8, 64)
}

fn valid_account_id(account_id: &str) -> bool {
    !account_id.is_empty()
        && account_id.len() <= 128
        && !account_id
            .chars()
            .any(|character| character == '/' || character == '\0')
}

#[derive(Serialize, Deserialize)]
struct HistoryEntry {
    role: String,
    text: String,
    ts: u64,
}

#[derive(Serialize, Deserialize, Default, Clone)]
struct ConversationMeta {
    #[serde(default)]
    session_name: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    dataset_id: String,
    #[serde(default)]
    session_key: String,
    #[serde(default)]
    conversation_id: String,
    #[serde(default)]
    snapshot_id: String,
}

#[derive(Serialize)]
struct ConversationSummary {
    conversation_id: String,
    session_key: String,
    dataset_id: String,
    snapshot_id: String,
    session_name: String,
    model: String,
    updated_at: u64,
    message_count: usize,
    preview: String,
}

#[cfg(test)]
fn append_history_at(
    path: &std::path::Path,
    role: &str,
    text: &str,
    ts: u64,
) -> std::io::Result<()> {
    let entry = HistoryEntry {
        role: role.to_string(),
        text: text.to_string(),
        ts,
    };
    let mut line = serde_json::to_string(&entry)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    line.push('\n');
    // O_APPEND 下单次 write 为原子追加：用户消息（命令线程）与回复（读取线程）可并发。
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?
        .write_all(line.as_bytes())
}

fn load_history_at(path: &std::path::Path) -> Vec<HistoryEntry> {
    let Ok(contents) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    contents
        .lines()
        .filter_map(|line| serde_json::from_str::<HistoryEntry>(line.trim()).ok())
        .collect()
}

fn load_meta_at(path: &std::path::Path) -> ConversationMeta {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|contents| serde_json::from_str(&contents).ok())
        .unwrap_or_default()
}

fn save_meta_at(path: &std::path::Path, meta: &ConversationMeta) -> std::io::Result<()> {
    std::fs::write(path, serde_json::to_vec(meta)?)
}

fn preview_of(text: &str) -> String {
    let flat = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= PREVIEW_CHARACTERS {
        return flat;
    }
    let mut preview: String = flat.chars().take(PREVIEW_CHARACTERS).collect();
    preview.push('…');
    preview
}

fn display_name_of(session_key: &str, meta: &ConversationMeta) -> String {
    if meta.session_name.is_empty() {
        format!("会话 {}", &session_key[..6])
    } else {
        meta.session_name.clone()
    }
}

/// 复合身份（前端 conversationId），永远可由 dataset_id + session_key 重建。
fn composite_conversation_id(dataset_id: &str, session_key: &str) -> String {
    agent_key(dataset_id, session_key)
}

fn conversation_root(app: &AppHandle) -> Result<PathBuf, CoreError> {
    Ok(app
        .path()
        .app_data_dir()
        .map_err(|error| CoreError::internal(error.to_string()))?
        .join("agents"))
}

/// 旧布局的平铺目录：`<agents>/<session_key>/`，且目录内确有对话文件。
/// 只有「16 位十六进制名字 + 内含 conversation 文件」才算，避免把数据集目录误判成会话目录。
fn is_legacy_session_dir(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    valid_session_key(name)
        && path.is_dir()
        && (path.join(CONVERSATION_FILE).is_file() || path.join(CONVERSATION_META_FILE).is_file())
}

/// 把旧平铺目录搬进两级布局并补齐元数据。目标已存在时合并追加，绝不覆盖既有内容；
/// 只有目标写成功后才删除原目录，失败保留原目录由下次重试。
fn move_conversation_dir(
    source: &Path,
    destination: &Path,
    dataset_id: Option<&str>,
) -> std::io::Result<()> {
    if !source.is_dir() {
        return Ok(());
    }
    let session_key = destination
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default()
        .to_string();
    if let Some(parent) = destination.parent() {
        std::fs::create_dir_all(parent)?;
    }
    if destination.is_dir() {
        let source_meta = load_meta_at(&source.join(CONVERSATION_META_FILE));
        let source_history =
            std::fs::read_to_string(source.join(CONVERSATION_FILE)).unwrap_or_default();
        if !source_history.is_empty() {
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(destination.join(CONVERSATION_FILE))?;
            file.write_all(source_history.as_bytes())?;
        }
        let mut meta = if destination.join(CONVERSATION_META_FILE).is_file() {
            load_meta_at(&destination.join(CONVERSATION_META_FILE))
        } else {
            source_meta.clone()
        };
        if meta.session_name.is_empty() {
            meta.session_name = source_meta.session_name;
        }
        if meta.model.is_empty() {
            meta.model = source_meta.model;
        }
        if meta.conversation_id.is_empty() {
            meta.conversation_id = source_meta.conversation_id;
        }
        if meta.snapshot_id.is_empty() {
            meta.snapshot_id = source_meta.snapshot_id;
        }
        save_meta_at(&destination.join(CONVERSATION_META_FILE), &meta)?;
    } else {
        std::fs::rename(source, destination)?;
    }
    let mut meta = load_meta_at(&destination.join(CONVERSATION_META_FILE));
    if let Some(dataset_id) = dataset_id {
        meta.dataset_id = dataset_id.to_string();
    }
    meta.session_key = session_key;
    if meta.conversation_id.is_empty() {
        meta.conversation_id = composite_conversation_id(
            if meta.dataset_id.is_empty() {
                UNASSIGNED_DATASET_DIR
            } else {
                &meta.dataset_id
            },
            &meta.session_key,
        );
    }
    save_meta_at(&destination.join(CONVERSATION_META_FILE), &meta)?;
    if source.exists() {
        std::fs::remove_dir_all(source)?;
    }
    Ok(())
}

/// 首次运行时把旧的平铺历史迁入两级布局；幂等，可安全重复调用。
fn migrate_legacy_history(root: &Path, selected_dataset_id: Option<&str>) -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    let selected_dataset_id = selected_dataset_id.filter(|dataset_id| valid_dataset_id(dataset_id));
    let mut warnings: Vec<String> = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !is_legacy_session_dir(&path) {
            continue;
        }
        let session_key = entry.file_name().to_string_lossy().to_string();
        let destination = match selected_dataset_id {
            Some(dataset_id) => root.join(dataset_id).join(&session_key),
            None => root.join(UNASSIGNED_DATASET_DIR).join(&session_key),
        };
        match move_conversation_dir(&path, &destination, selected_dataset_id) {
            Ok(()) => {
                if selected_dataset_id.is_none()
                    && !warnings
                        .iter()
                        .any(|warning| warning == LEGACY_HISTORY_UNASSIGNED)
                {
                    warnings.push(LEGACY_HISTORY_UNASSIGNED.to_string());
                }
            }
            Err(_) => {
                if !warnings
                    .iter()
                    .any(|warning| warning == LEGACY_HISTORY_MIGRATION_FAILED)
                {
                    warnings.push(LEGACY_HISTORY_MIGRATION_FAILED.to_string());
                }
            }
        }
    }
    warnings
}

fn sharded_conversation_dir(root: &Path, dataset_id: &str, session_key: &str) -> PathBuf {
    root.join(dataset_id).join(session_key)
}

struct ConversationDir {
    dataset_id: String,
    session_key: String,
    path: PathBuf,
}

/// 只认两级合法目录，`_unassigned` 与任何非十六进制目录一律忽略。
fn conversation_dirs(root: &Path) -> Vec<ConversationDir> {
    let Ok(datasets) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    let mut dirs = Vec::new();
    for dataset_entry in datasets.flatten() {
        let dataset_id = dataset_entry.file_name().to_string_lossy().to_string();
        if !valid_dataset_id(&dataset_id) {
            continue;
        }
        let Ok(sessions) = std::fs::read_dir(dataset_entry.path()) else {
            continue;
        };
        for session_entry in sessions.flatten() {
            let session_key = session_entry.file_name().to_string_lossy().to_string();
            if !valid_session_key(&session_key) {
                continue;
            }
            let path = session_entry.path();
            if !path.is_dir() {
                continue;
            }
            dirs.push(ConversationDir {
                dataset_id: dataset_id.clone(),
                session_key,
                path,
            });
        }
    }
    dirs
}

/// 扫描两级工作目录；没有消息记录的会话与非法目录一律忽略。
fn summarize_conversations(root: &Path) -> Vec<ConversationSummary> {
    let mut summaries = Vec::new();
    for dir in conversation_dirs(root) {
        let messages = load_history_at(&dir.path.join(CONVERSATION_FILE));
        let Some(last) = messages.last() else {
            continue;
        };
        let meta = load_meta_at(&dir.path.join(CONVERSATION_META_FILE));
        summaries.push(ConversationSummary {
            conversation_id: composite_conversation_id(&dir.dataset_id, &dir.session_key),
            session_name: display_name_of(&dir.session_key, &meta),
            dataset_id: dir.dataset_id,
            session_key: dir.session_key,
            snapshot_id: meta.snapshot_id,
            model: meta.model,
            updated_at: last.ts,
            message_count: messages.len(),
            preview: preview_of(&last.text),
        });
    }
    summaries.sort_by(|left, right| right.updated_at.cmp(&left.updated_at));
    summaries
}

/// 解析对话目录：复合身份 → 唯一 session_key（兼容裸键）→ 元数据记录的 conversation_id。
fn resolve_conversation_dir(root: &Path, conversation_id: &str) -> Option<ConversationDir> {
    if let Some((dataset_id, session_key)) = conversation_id.split_once(':') {
        if valid_dataset_id(dataset_id) && valid_session_key(session_key) {
            let path = sharded_conversation_dir(root, dataset_id, session_key);
            if path.is_dir() {
                return Some(ConversationDir {
                    dataset_id: dataset_id.to_string(),
                    session_key: session_key.to_string(),
                    path,
                });
            }
        }
    }
    let dirs = conversation_dirs(root);
    let mut by_session: Vec<ConversationDir> = Vec::new();
    let mut by_meta: Vec<ConversationDir> = Vec::new();
    for dir in dirs {
        if valid_session_key(conversation_id) && dir.session_key == conversation_id {
            by_session.push(ConversationDir {
                dataset_id: dir.dataset_id.clone(),
                session_key: dir.session_key.clone(),
                path: dir.path.clone(),
            });
        }
        let meta = load_meta_at(&dir.path.join(CONVERSATION_META_FILE));
        if !meta.conversation_id.is_empty() && meta.conversation_id == conversation_id {
            by_meta.push(dir);
        }
    }
    for candidates in [by_session, by_meta] {
        if candidates.len() == 1 {
            return candidates.into_iter().next();
        }
    }
    None
}

fn read_config_value(app: &AppHandle) -> Option<Value> {
    let dir = sidecar::config_dir(app).ok()?;
    let contents = std::fs::read_to_string(dir.join("config.json")).ok()?;
    serde_json::from_str(&contents).ok()
}

/// 当前选中的数据集（仅接受合法十六进制 id）。
fn selected_dataset_id(app: &AppHandle) -> Option<String> {
    read_config_value(app)?
        .get("selected_dataset_id")
        .and_then(Value::as_str)
        .filter(|dataset_id| valid_dataset_id(dataset_id))
        .map(str::to_string)
}

/// 扫描前准备：确保目录存在，并把遗留平铺历史迁移到位（幂等）。
fn history_root_ready(app: &AppHandle) -> Result<(PathBuf, Vec<String>), CoreError> {
    let root = conversation_root(app)?;
    std::fs::create_dir_all(&root).map_err(|error| CoreError::internal(error.to_string()))?;
    let warnings = migrate_legacy_history(&root, selected_dataset_id(app).as_deref());
    Ok((root, warnings))
}

fn local_conversations(
    app: &AppHandle,
) -> Result<(Vec<ConversationSummary>, Vec<String>), CoreError> {
    let (root, warnings) = history_root_ready(app)?;
    Ok((summarize_conversations(&root), warnings))
}

pub(crate) fn packaged_pi_binary_path(app: &AppHandle) -> Result<Option<PathBuf>, CoreError> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| CoreError::internal(error.to_string()))?;
    let candidates = [
        resource_dir.join("resources").join("pi-runtime").join("pi"),
        resource_dir.join("pi-runtime").join("pi"),
    ];
    Ok(candidates
        .into_iter()
        .find(|candidate| candidate.is_file())
        .map(|candidate| candidate.to_path_buf()))
}

pub(crate) fn pi_binary_path() -> Result<String, CoreError> {
    if let Ok(path) = std::env::var("WECOM_CONTEXT_PI_BIN") {
        if std::path::Path::new(&path).is_file() {
            return Ok(path);
        }
    }
    let output = Command::new("/bin/zsh")
        .args(["-lc", "export NVM_DIR=\"$HOME/.nvm\"; [ -s \"$NVM_DIR/nvm.sh\" ] && . \"$NVM_DIR/nvm.sh\"; whence -p pi"])
        .output()
        .map_err(|error| CoreError::local("PI_UNAVAILABLE", format!("无法定位 Pi 命令: {error}"), false))?;
    let path = String::from_utf8_lossy(&output.stdout)
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(str::to_string)
        .ok_or_else(|| CoreError::local("PI_UNAVAILABLE", "找不到可用的 Pi 命令", false))?;
    if !output.status.success() || !std::path::Path::new(&path).is_file() {
        return Err(CoreError::local(
            "PI_UNAVAILABLE",
            "找不到可用的 Pi 命令",
            false,
        ));
    }
    Ok(path)
}

pub(crate) fn agent_proxy_env() -> Vec<(String, String)> {
    omp_proxy()
        .map(|(host, port)| {
            let proxy = format!("http://{host}:{port}");
            vec![
                ("PI_PROXY".to_string(), proxy.clone()),
                ("HTTP_PROXY".to_string(), proxy.clone()),
                ("HTTPS_PROXY".to_string(), proxy.clone()),
                ("ALL_PROXY".to_string(), proxy),
            ]
        })
        .unwrap_or_default()
}

pub(crate) fn agent_trace(app: &AppHandle, message: &str) {
    let Ok(dir) = sidecar::config_dir(app) else {
        return;
    };
    let _ = std::fs::create_dir_all(&dir);
    let path = dir.join("agent-debug.log");
    if let Ok(metadata) = std::fs::metadata(&path) {
        if metadata.len() > 128 * 1024 {
            let _ = std::fs::remove_file(&path);
        }
    }
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        use std::io::Write as _;
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let _ = writeln!(file, "{stamp} {message}");
    }
}

#[tauri::command]
async fn allow_session(app: AppHandle, session_key: String) -> Result<Value, CoreError> {
    invoke_core_blocking(app, "allow_session", json!({ "session_key": session_key })).await
}

#[tauri::command]
async fn set_send_permission(app: AppHandle, enabled: bool) -> Result<Value, CoreError> {
    invoke_core_blocking(app, "set_send_permission", json!({ "enabled": enabled })).await
}

#[tauri::command]
async fn bind_session(app: AppHandle, payload: Value) -> Result<Value, CoreError> {
    invoke_core_blocking(app, "bind_session", payload).await
}

/// 一键取钥：sidecar 内完成重启企业微信→签名副本→只读扫描→写密钥→验证→恢复。
#[tauri::command]
async fn capture_key(app: AppHandle, dataset_id: String) -> Result<Value, CoreError> {
    invoke_core_blocking(app, "capture_key", json!({ "dataset_id": dataset_id })).await
}

/// 删除企业（应用侧）：停掉该企业的 Agent、删除其本机会话记录目录，
/// 再由 sidecar 删除本地快照/独占密钥并加入忽略列表；不动企业微信原始数据。
#[tauri::command]
async fn remove_dataset(app: AppHandle, dataset_id: String) -> Result<Value, CoreError> {
    if !valid_dataset_id(&dataset_id) {
        return Err(CoreError::local("INVALID_REQUEST", "数据集标识无效", false));
    }
    // 唯一 Agent 不绑定企业：删除企业只清本地只读归档，不动主 Agent 与会话记忆。
    let conversations = conversation_root(&app)
        .ok()
        .map(|root| root.join(&dataset_id));
    if let Some(dir) = conversations {
        if dir.is_dir() {
            let _ = std::fs::remove_dir_all(&dir);
        }
    }
    invoke_core_blocking(
        app.clone(),
        "remove_dataset",
        json!({ "dataset_id": dataset_id }),
    )
    .await
}

/// 删除企业微信账户在应用侧的全部数据：快照、密钥、本机会话记录。
/// 不删除企业微信原始数据库；sidecar 返回实际涉及的数据集后再清本地目录。
#[tauri::command]
async fn remove_account(app: AppHandle, account_id: String) -> Result<Value, CoreError> {
    if !valid_account_id(&account_id) {
        return Err(CoreError::local(
            "INVALID_REQUEST",
            "企业微信账户标识无效",
            false,
        ));
    }
    let result = invoke_core_blocking(
        app.clone(),
        "remove_account",
        json!({ "account_id": account_id }),
    )
    .await?;
    if let Some(dataset_ids) = result.get("dataset_ids").and_then(Value::as_array) {
        if let Ok(root) = conversation_root(&app) {
            for dataset_id in dataset_ids.iter().filter_map(Value::as_str) {
                if valid_dataset_id(dataset_id) {
                    let directory = root.join(dataset_id);
                    if directory.is_dir() {
                        let _ = std::fs::remove_dir_all(directory);
                    }
                }
            }
        }
    }
    Ok(result)
}

/// 恢复被移除的企业（仅移出忽略列表）。
#[tauri::command]
async fn restore_dataset(app: AppHandle, dataset_id: String) -> Result<Value, CoreError> {
    if !valid_dataset_id(&dataset_id) {
        return Err(CoreError::local("INVALID_REQUEST", "数据集标识无效", false));
    }
    invoke_core_blocking(app, "restore_dataset", json!({ "dataset_id": dataset_id })).await
}

/// 工作台首屏：一次拿齐 sidecar 的就绪状态、会话列表与本机对话摘要。
#[tauri::command]
async fn bootstrap_workbench(app: AppHandle) -> Result<Value, CoreError> {
    let data =
        invoke_core_blocking(app.clone(), "bootstrap", json!({ "auto_refresh": true })).await?;
    let scan_app = app.clone();
    let (conversations, warnings) =
        tauri::async_runtime::spawn_blocking(move || local_conversations(&scan_app))
            .await
            .map_err(|error| CoreError::internal(format!("本地历史扫描失败: {error}")))??;
    let mut payload = match data {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    payload.insert(
        "conversations".into(),
        serde_json::to_value(&conversations)
            .map_err(|error| CoreError::internal(error.to_string()))?,
    );
    let mut merged: Vec<Value> = payload
        .get("warnings")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for warning in warnings {
        if !merged
            .iter()
            .any(|item| item.as_str() == Some(warning.as_str()))
        {
            merged.push(Value::String(warning));
        }
    }
    payload.insert("warnings".into(), Value::Array(merged));
    for key in [
        "readiness",
        "dataset",
        "snapshot",
        "sessions",
        "selection_reason",
    ] {
        if !payload.contains_key(key) {
            payload.insert(key.to_string(), Value::Null);
        }
    }
    Ok(Value::Object(payload))
}

/// 单个资料来源的显式身份（前端传入，必须通过校验才转发给 sidecar）。
#[derive(serde::Deserialize)]
struct SourceRequest {
    dataset_id: String,
    snapshot_id: String,
    session_key: String,
    #[serde(default)]
    limit: Option<u32>,
    #[serde(default)]
    start: Option<String>,
    #[serde(default)]
    end: Option<String>,
    #[serde(default)]
    include_images: Option<bool>,
}

/// 校验来源并转成 sidecar 请求体；不合法直接拒绝，绝不把可疑身份传下去。
fn sources_payload(sources: &[SourceRequest]) -> Result<Value, CoreError> {
    if sources.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }
    if sources.len() > 20 {
        return Err(CoreError::local(
            "INVALID_REQUEST",
            "资料来源最多 20 个",
            false,
        ));
    }
    let mut payload = Vec::with_capacity(sources.len());
    for source in sources {
        if !valid_dataset_id(&source.dataset_id) {
            return Err(CoreError::local(
                "INVALID_REQUEST",
                "来源的企业标识无效",
                false,
            ));
        }
        if !valid_session_key(&source.session_key) {
            return Err(CoreError::local(
                "INVALID_REQUEST",
                "来源的会话标识无效",
                false,
            ));
        }
        if source.snapshot_id.trim().is_empty() || source.snapshot_id.len() > 128 {
            return Err(CoreError::local(
                "INVALID_REQUEST",
                "来源的快照标识无效",
                false,
            ));
        }
        let mut item = Map::new();
        item.insert("dataset_id".into(), json!(source.dataset_id));
        item.insert("snapshot_id".into(), json!(source.snapshot_id));
        item.insert("session_key".into(), json!(source.session_key));
        item.insert(
            "include_images".into(),
            json!(source.include_images.unwrap_or(false)),
        );
        if let Some(limit) = source.limit {
            item.insert("limit".into(), json!(limit.clamp(1, 500)));
        }
        if let Some(start) = &source.start {
            item.insert("start".into(), json!(start));
        }
        if let Some(end) = &source.end {
            item.insert("end".into(), json!(end));
        }
        payload.push(Value::Object(item));
    }
    Ok(Value::Array(payload))
}

/// 把 sidecar 资料包转成本轮注入：正文 + 按图片编号顺序排列的可用图片。
fn injection_from_package(data: &Value) -> main_agent::Injection {
    let text = data
        .get("text")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let package_id = data
        .get("package_id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let mut images = Vec::new();
    if let Some(items) = data.get("images").and_then(Value::as_array) {
        // 与契约一致：images 数组顺序即模型输入顺序，正文用「【图片 N】」引用第 N 张。
        for item in items.iter().filter(|item| {
            item.get("image_id")
                .and_then(Value::as_str)
                .is_some_and(|id| id.starts_with("img_"))
        }) {
            let (Some(path), Some(mime)) = (
                item.get("path").and_then(Value::as_str),
                item.get("mime_type").and_then(Value::as_str),
            ) else {
                continue;
            };
            images.push(main_agent::InjectImage {
                path: PathBuf::from(path),
                mime_type: mime.to_string(),
            });
        }
    }
    main_agent::Injection {
        package_id,
        text,
        images,
    }
}

/// 资料包根目录：sidecar 写、Rust 只读，且必须落在配置的 vault_root 之下。
fn packages_root(app: &AppHandle) -> Result<PathBuf, CoreError> {
    let vault_root = read_config_value(app)
        .and_then(|config| {
            config
                .get("vault_root")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .ok_or_else(|| CoreError::local("CONFIG_INVALID", "配置里缺少 vault_root", false))?;
    Ok(PathBuf::from(vault_root).join("packages"))
}

/// 把资料包标识解析成目录：只接受 `pkg-` + 安全字符，防路径穿越。
fn resolve_package_dir(app: &AppHandle, package_id: &str) -> Result<PathBuf, CoreError> {
    let safe = package_id.starts_with("pkg-")
        && package_id.len() <= 128
        && package_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-');
    if !safe {
        return Err(CoreError::local("INVALID_REQUEST", "资料包标识无效", false));
    }
    let dir = packages_root(app)?.join(package_id);
    if !dir.is_dir() {
        return Err(CoreError::local(
            "PACKAGE_EXPIRED",
            "资料包已不存在，请重新预览本轮资料",
            true,
        ));
    }
    Ok(dir)
}

/// 组装 prepare_context 请求体（预览与发送共用，保证两边拿到的是同一份选择）。
fn package_request_payload(
    sources: &[SourceRequest],
    image_options: Option<Value>,
    max_context_tokens: Option<u32>,
    max_message_characters: Option<u32>,
) -> Result<Value, CoreError> {
    let mut payload = Map::new();
    payload.insert("sources".into(), sources_payload(sources)?);
    if let Some(options) = image_options {
        payload.insert("image_options".into(), options);
    }
    if let Some(tokens) = max_context_tokens {
        payload.insert("max_context_tokens".into(), json!(tokens.clamp(100, 60000)));
    }
    if let Some(characters) = max_message_characters {
        payload.insert(
            "max_message_characters".into(),
            json!(characters.clamp(50, 8000)),
        );
    }
    Ok(Value::Object(payload))
}

/// 预览过的资料包是否仍然对应当前选择；不一致就必须重新准备，绝不允许「预览 A 发送 B」。
fn package_matches_sources(package: &Value, requested: &Value) -> bool {
    let fingerprint = |value: &Value| -> Vec<(String, String, String)> {
        let mut items: Vec<(String, String, String)> = value
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|item| {
                let dataset = item.get("dataset_id").and_then(Value::as_str)?;
                let snapshot = item.get("snapshot_id").and_then(Value::as_str)?;
                let session = item.get("session_key").and_then(Value::as_str)?;
                Some((
                    dataset.to_string(),
                    snapshot.to_string(),
                    session.to_string(),
                ))
            })
            .collect();
        items.sort();
        items
    };
    let expected = fingerprint(requested);
    if expected.is_empty() {
        return false;
    }
    fingerprint(package) == expected
}

/// 本轮资料预览：只生成资料包并回给界面，不写入 Agent、不消耗模型调用。
#[tauri::command]
async fn preview_context_package(
    app: AppHandle,
    sources: Vec<SourceRequest>,
    image_options: Option<Value>,
    max_context_tokens: Option<u32>,
    max_message_characters: Option<u32>,
) -> Result<Value, CoreError> {
    if sources.is_empty() {
        return Err(CoreError::local(
            "INVALID_REQUEST",
            "还没有选择资料来源",
            false,
        ));
    }
    let payload = package_request_payload(
        &sources,
        image_options,
        max_context_tokens,
        max_message_characters,
    )?;
    invoke_core_blocking(app, "prepare_context", payload).await
}

/// 把所选企业微信会话中的全部本地图片导出到 Downloads 对话目录。
#[tauri::command]
async fn download_images(
    app: AppHandle,
    dataset_id: String,
    snapshot_id: String,
    session_key: String,
    fetch_via_client: bool,
) -> Result<Value, CoreError> {
    if !valid_dataset_id(&dataset_id)
        || snapshot_id.is_empty()
        || snapshot_id.len() > 128
        || !snapshot_id
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || character == '-' || character == '_')
    {
        return Err(CoreError::local("INVALID_REQUEST", "图片导出的来源标识无效", false));
    }
    if !valid_session_key(&session_key) {
        return Err(CoreError::local("INVALID_REQUEST", "图片导出的会话标识无效", false));
    }
    invoke_core_blocking(
        app,
        "download_images",
        json!({
            "dataset_id": dataset_id,
            "snapshot_id": snapshot_id,
            "session_key": session_key,
            "fetch_via_client": fetch_via_client,
        }),
    )
    .await
}

/// 打开 macOS 隐私与安全性设置，处理辅助功能或自动化授权。
#[tauri::command]
fn open_accessibility_settings() -> Result<(), CoreError> {
    Command::new("/usr/bin/open")
        .arg("x-apple.systempreferences:com.apple.preference.security")
        .status()
        .map_err(|error| CoreError::local("PRIVACY_SETTINGS_FAILED", format!("无法打开隐私与安全性设置: {error}"), true))?
        .success()
        .then_some(())
        .ok_or_else(|| CoreError::local("PRIVACY_SETTINGS_FAILED", "系统未能打开隐私与安全性设置", true))
}

/// 资料包里的单张图片（预览缩略图用）。
/// 只读 packages 根目录下、且图片标识合法的文件，不接受任意路径。
#[tauri::command]
fn package_image_data(
    app: AppHandle,
    package_id: String,
    image_id: String,
) -> Result<Value, CoreError> {
    let digits = image_id
        .strip_prefix("img_")
        .or_else(|| image_id.strip_prefix("unusable_"))
        .unwrap_or_default();
    if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
        return Err(CoreError::local("INVALID_REQUEST", "图片标识无效", false));
    }
    let dir = resolve_package_dir(&app, &package_id)?;
    let mut found: Option<(PathBuf, String)> = None;
    for entry in std::fs::read_dir(&dir).map_err(|error| CoreError::internal(error.to_string()))? {
        let entry = entry.map_err(|error| CoreError::internal(error.to_string()))?;
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        if path.file_stem().and_then(|stem| stem.to_str()) != Some(image_id.as_str()) {
            continue;
        }
        let mime = match path
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default()
        {
            "jpg" | "jpeg" => "image/jpeg",
            "png" => "image/png",
            "webp" => "image/webp",
            "gif" => "image/gif",
            _ => continue,
        };
        found = Some((path, mime.to_string()));
        break;
    }
    let Some((path, mime)) = found else {
        return Err(CoreError::local(
            "PACKAGE_EXPIRED",
            "该图片不在资料包中",
            true,
        ));
    };
    let bytes = std::fs::read(&path).map_err(|error| CoreError::internal(error.to_string()))?;
    Ok(json!({
        "image_id": image_id,
        "mime_type": mime,
        "bytes": bytes.len(),
        "data_url": format!("data:{mime};base64,{}", main_agent::base64_encode(&bytes)),
    }))
}

/// 核心发送路径：先按本轮来源准备资料包（可含图片），再写入唯一主 Agent。
/// 只有 stdin 写入成功才返回 accepted；模型是否回答由事件流给出。
#[tauri::command]
async fn agent_send_message(
    app: AppHandle,
    state: tauri::State<'_, main_agent::AgentState>,
    text: String,
    model: String,
    client_token: Option<String>,
    sources: Option<Vec<SourceRequest>>,
    package_id: Option<String>,
    image_options: Option<Value>,
    max_context_tokens: Option<u32>,
    max_message_characters: Option<u32>,
) -> Result<Value, CoreError> {
    // 单 Agent 路径：资料只是「本轮来源」，不再绑定 Agent 身份。
    // `client_token` 仅是界面关联令牌，用于把事件回给发起的那条对话。
    let sources = sources.unwrap_or_default();
    let requested = if sources.is_empty() {
        Value::Array(Vec::new())
    } else {
        sources_payload(&sources)?
    };
    let mut injected_package: Option<Value> = None;
    let injection = if let Some(package) = package_id.filter(|value| !value.is_empty()) {
        // 复用预览过的资料包：来源必须与当前选择完全一致，否则视为预览失效。
        let dir = resolve_package_dir(&app, &package)?;
        let manifest = std::fs::read_to_string(dir.join("package.json")).map_err(|error| {
            CoreError::local("PACKAGE_EXPIRED", format!("资料包不可读取：{error}"), true)
        })?;
        let payload: Value = serde_json::from_str(&manifest).map_err(|error| {
            CoreError::local("PACKAGE_EXPIRED", format!("资料包清单损坏：{error}"), true)
        })?;
        if !package_matches_sources(payload.get("sources").unwrap_or(&Value::Null), &requested) {
            return Err(CoreError::local(
                "PACKAGE_EXPIRED",
                "所选来源已变化，请重新预览本轮资料后再发送",
                true,
            ));
        }
        injected_package = Some(payload.clone());
        Some(injection_from_package(&payload))
    } else if sources.is_empty() {
        None
    } else {
        let payload = package_request_payload(
            &sources,
            image_options,
            max_context_tokens,
            max_message_characters,
        )?;
        let data = invoke_core_blocking(app.clone(), "prepare_context", payload).await?;
        injected_package = Some(data.clone());
        let built = injection_from_package(&data);
        if built.text.trim().is_empty() {
            return Err(CoreError::local(
                "CONTEXT_UNAVAILABLE",
                "所选来源没有可用内容，请先刷新快照或改选其它会话",
                true,
            ));
        }
        Some(built)
    };
    let package = injected_package.clone();
    let omitted_image_count = package
        .as_ref()
        .and_then(|value| value.get("stats"))
        .and_then(|stats| stats.get("omitted_image_count"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let mut result = main_agent::send(
        &app,
        &state,
        &text,
        &model,
        client_token.as_deref().unwrap_or_default(),
        injection,
    )?;
    if let Some(object) = result.as_object_mut() {
        object.insert("omitted_image_count".into(), json!(omitted_image_count));
    }
    if let (Some(record), Some(request_id)) =
        (package, result.get("request_id").and_then(Value::as_str))
    {
        // 只在写入成功后记录：没发出去的这一轮不算注入过。
        main_agent::record_injection(&app, request_id, &text, &record);
    }
    Ok(result)
}

/// 唯一主 Agent 的运行态（`contracts/agent-status.schema.json`）。
/// 记忆查询都是 RPC + 文件读，统一放到阻塞线程，避免卡住 WKWebView 主线程。
async fn memory_task<F>(
    app: AppHandle,
    state: tauri::State<'_, main_agent::AgentState>,
    task: F,
) -> Result<Value, CoreError>
where
    F: FnOnce(&AppHandle, &main_agent::AgentState) -> Result<Value, CoreError> + Send + 'static,
{
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || task(&app, &state))
        .await
        .map_err(|error| CoreError::internal(format!("记忆任务失败: {error}")))?
}

/// 当前有效上下文（模型真正还在用的内容）。
#[tauri::command]
async fn memory_context(
    app: AppHandle,
    state: tauri::State<'_, main_agent::AgentState>,
) -> Result<Value, CoreError> {
    memory_task(app, state, main_agent::memory_context).await
}

/// 完整历史（含已被压缩、不再进入当前上下文的部分）。
#[tauri::command]
async fn memory_history(
    app: AppHandle,
    state: tauri::State<'_, main_agent::AgentState>,
    offset: Option<u32>,
    limit: Option<u32>,
) -> Result<Value, CoreError> {
    let offset = offset.unwrap_or(0) as usize;
    let limit = limit.unwrap_or(50).clamp(1, 200) as usize;
    memory_task(app, state, move |app, state| {
        main_agent::memory_history(app, state, offset, limit)
    })
    .await
}

/// 已注入资料清单与它在当前上下文中的处境。
#[tauri::command]
async fn memory_injections(
    app: AppHandle,
    state: tauri::State<'_, main_agent::AgentState>,
    offset: Option<u32>,
    limit: Option<u32>,
) -> Result<Value, CoreError> {
    let offset = offset.unwrap_or(0) as usize;
    let limit = limit.unwrap_or(20).clamp(1, 100) as usize;
    memory_task(app, state, move |app, state| {
        main_agent::memory_injections(app, state, offset, limit)
    })
    .await
}

/// 清空当前 Agent 记忆（会话、摘要、注入记录与资料包图片）；企微源数据与配置不动。
#[tauri::command]
async fn memory_clear(
    app: AppHandle,
    state: tauri::State<'_, main_agent::AgentState>,
    confirmed: bool,
) -> Result<Value, CoreError> {
    memory_task(app, state, move |app, state| {
        main_agent::memory_clear(app, state, confirmed)
    })
    .await
}

#[tauri::command]
fn agent_status(
    app: AppHandle,
    state: tauri::State<'_, main_agent::AgentState>,
) -> Result<Value, CoreError> {
    Ok(main_agent::status(&app, &state))
}

#[tauri::command]
fn agent_stop(state: tauri::State<'_, main_agent::AgentState>) -> Result<Value, CoreError> {
    Ok(json!({ "stopped": main_agent::stop(&state) }))
}

#[tauri::command]
async fn agent_new_conversation(
    app: AppHandle,
    state: tauri::State<'_, main_agent::AgentState>,
    model: String,
) -> Result<Value, CoreError> {
    memory_task(app, state, move |app, state| {
        main_agent::new_session(app, state, &model)
    })
    .await
}

#[tauri::command]
fn agent_history(app: AppHandle, conversation_id: String) -> Result<Value, CoreError> {
    let (root, _) = history_root_ready(&app)?;
    let target = resolve_conversation_dir(&root, &conversation_id)
        .ok_or_else(|| CoreError::local("SESSION_NOT_FOUND", "找不到该对话记录", false))?;
    let meta = load_meta_at(&target.path.join(CONVERSATION_META_FILE));
    Ok(json!({
        "conversation_id": composite_conversation_id(&target.dataset_id, &target.session_key),
        "dataset_id": target.dataset_id,
        "session_key": target.session_key,
        "snapshot_id": meta.snapshot_id,
        "session_name": display_name_of(&target.session_key, &meta),
        "model": meta.model,
        "messages": load_history_at(&target.path.join(CONVERSATION_FILE)),
    }))
}

#[tauri::command]
fn agent_history_list(app: AppHandle) -> Result<Value, CoreError> {
    let (root, _) = history_root_ready(&app)?;
    Ok(json!({ "conversations": summarize_conversations(&root) }))
}

#[tauri::command]
async fn get_status(app: AppHandle) -> Result<Value, CoreError> {
    invoke_core_blocking(app, "status", json!({})).await
}

#[tauri::command]
async fn discover_datasets(app: AppHandle) -> Result<Value, CoreError> {
    invoke_core_blocking(app, "discover_datasets", json!({})).await
}

#[tauri::command]
async fn select_dataset(app: AppHandle, dataset_id: String) -> Result<Value, CoreError> {
    // 切换企业只改变「可选资料来源」的浏览范围；主 Agent 与会话记忆保持不动。
    invoke_core_blocking(app, "select_dataset", json!({ "dataset_id": dataset_id })).await
}

#[tauri::command]
async fn list_sessions(app: AppHandle, limit: Option<u32>) -> Result<Value, CoreError> {
    invoke_core_blocking(app, "sessions", json!({ "limit": limit.unwrap_or(50) })).await
}

#[tauri::command]
async fn select_session(
    app: AppHandle,
    session_key: String,
    conversation_id: Option<String>,
    dataset_id: Option<String>,
    snapshot_id: Option<String>,
    allow_history: Option<bool>,
) -> Result<Value, CoreError> {
    let mut payload = Map::new();
    payload.insert("session_key".into(), json!(session_key));
    if let Some(conversation_id) = conversation_id {
        payload.insert("conversation_id".into(), json!(conversation_id));
    }
    if let Some(dataset_id) = dataset_id {
        payload.insert("dataset_id".into(), json!(dataset_id));
    }
    if let Some(snapshot_id) = snapshot_id {
        payload.insert("snapshot_id".into(), json!(snapshot_id));
    }
    if let Some(allow_history) = allow_history {
        payload.insert("allow_history".into(), json!(allow_history));
    }
    invoke_core_blocking(app, "select_session", Value::Object(payload)).await
}

#[tauri::command]
async fn clear_session(app: AppHandle) -> Result<Value, CoreError> {
    invoke_core_blocking(app, "clear_session", json!({})).await
}

/// 预览上下文：按显式身份（会话 + 企业 + 快照）转发，不再依赖全局选中的会话。
#[tauri::command]
async fn preview_context(
    app: AppHandle,
    session_key: String,
    dataset_id: Option<String>,
    snapshot_id: Option<String>,
    limit: Option<u32>,
) -> Result<Value, CoreError> {
    let mut payload = Map::new();
    payload.insert("session_key".into(), json!(session_key));
    payload.insert("limit".into(), json!(limit.unwrap_or(30)));
    if let Some(dataset_id) = dataset_id {
        payload.insert("dataset_id".into(), json!(dataset_id));
    }
    if let Some(snapshot_id) = snapshot_id {
        payload.insert("snapshot_id".into(), json!(snapshot_id));
    }
    invoke_core_blocking(app, "preview_context", Value::Object(payload)).await
}

#[tauri::command]
async fn refresh_snapshot(app: AppHandle, confirmed: bool) -> Result<Value, CoreError> {
    if !confirmed {
        return Err(CoreError::local(
            "INVALID_REQUEST",
            "刷新必须经过用户确认",
            false,
        ));
    }
    // 刷新快照不动主 Agent：新一轮读取新快照，旧资料在注入记录里保留旧快照标识。
    invoke_core_blocking(app, "refresh_snapshot", json!({ "confirmed": true })).await
}

async fn invoke_core_blocking(
    app: AppHandle,
    action: &str,
    payload: Value,
) -> Result<Value, CoreError> {
    let action = action.to_string();
    tauri::async_runtime::spawn_blocking(move || sidecar::invoke_core(&app, &action, payload))
        .await
        .map_err(|error| CoreError::internal(format!("sidecar 任务失败: {error}")))?
}

fn copy_connector_files(
    source: &std::path::Path,
    target: &std::path::Path,
) -> Result<(), CoreError> {
    std::fs::create_dir_all(target).map_err(|error| CoreError::internal(error.to_string()))?;
    for entry in
        std::fs::read_dir(source).map_err(|error| CoreError::internal(error.to_string()))?
    {
        let entry = entry.map_err(|error| CoreError::internal(error.to_string()))?;
        let source_path = entry.path();
        let target_path = target.join(entry.file_name());
        if source_path.is_dir() {
            copy_connector_files(&source_path, &target_path)?;
        } else if source_path.is_file() {
            std::fs::copy(&source_path, &target_path)
                .map_err(|error| CoreError::internal(error.to_string()))?;
        }
    }
    Ok(())
}

fn connector_is_valid(connector: &std::path::Path) -> bool {
    connector.join(".managed-by-wecom-context").is_file()
        && [
            "index.ts",
            "src/commands.ts",
            "src/config.ts",
            "src/core-client.ts",
            "src/tool.ts",
            "src/types.ts",
            "src/wecom-cli.ts",
        ]
        .iter()
        .all(|relative| connector.join(relative).is_file())
}

fn install_connector_at(
    source: &std::path::Path,
    target: &std::path::Path,
    connector_config: &Value,
) -> Result<(), CoreError> {
    let parent = target
        .parent()
        .ok_or_else(|| CoreError::local("CONNECTOR_ERROR", "无法定位 Connector 目录", false))?;
    std::fs::create_dir_all(parent).map_err(|error| CoreError::internal(error.to_string()))?;
    let marker = target.join(".managed-by-wecom-context");
    if target.exists() && !marker.is_file() {
        return Err(CoreError::local(
            "CONNECTOR_ERROR",
            "目标 Connector 目录不是由本应用管理",
            false,
        ));
    }
    let suffix = format!("{}", std::process::id());
    let staging = parent.join(format!(".pi-wecom-context-staging-{suffix}"));
    let backup = parent.join(format!(".pi-wecom-context-backup-{suffix}"));
    if staging.exists() {
        std::fs::remove_dir_all(&staging)
            .map_err(|error| CoreError::internal(error.to_string()))?;
    }
    if backup.exists() {
        std::fs::remove_dir_all(&backup).map_err(|error| CoreError::internal(error.to_string()))?;
    }
    copy_connector_files(source, &staging)?;
    std::fs::write(staging.join(".managed-by-wecom-context"), b"1\n")
        .map_err(|error| CoreError::internal(error.to_string()))?;
    std::fs::write(
        staging.join("config.json"),
        serde_json::to_vec_pretty(connector_config)
            .map_err(|error| CoreError::internal(error.to_string()))?,
    )
    .map_err(|error| CoreError::internal(error.to_string()))?;
    if target.exists() {
        std::fs::rename(target, &backup).map_err(|error| {
            let _ = std::fs::remove_dir_all(&staging);
            CoreError::internal(error.to_string())
        })?;
    }
    if let Err(error) = std::fs::rename(&staging, target) {
        if backup.exists() {
            let _ = std::fs::rename(&backup, target);
        }
        let _ = std::fs::remove_dir_all(&staging);
        return Err(CoreError::internal(error.to_string()));
    }
    if backup.exists() {
        std::fs::remove_dir_all(&backup).map_err(|error| CoreError::internal(error.to_string()))?;
    }
    Ok(())
}

#[tauri::command]
fn check_connector(_app: AppHandle) -> Result<Value, CoreError> {
    let home =
        std::env::var_os("HOME").ok_or_else(|| CoreError::internal("无法定位用户 Home 目录"))?;
    let connector = std::path::PathBuf::from(home)
        .join(".pi")
        .join("agent")
        .join("extensions")
        .join("pi-wecom-context");
    Ok(json!({ "installed": connector_is_valid(&connector), "configurable": true }))
}

#[tauri::command]
fn install_connector(app: AppHandle) -> Result<Value, CoreError> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| CoreError::internal(error.to_string()))?;
    let source = [
        resource_dir.join("resources").join("pi-connector"),
        resource_dir.join("pi-connector"),
    ]
    .into_iter()
    .find(|candidate| candidate.join("index.ts").is_file())
    .ok_or_else(|| CoreError::local("CONNECTOR_ERROR", "App 资源中缺少 Pi Connector", false))?;
    let home =
        std::env::var_os("HOME").ok_or_else(|| CoreError::internal("无法定位用户 Home 目录"))?;
    let target = std::path::PathBuf::from(home)
        .join(".pi")
        .join("agent")
        .join("extensions")
        .join("pi-wecom-context");
    let config_dir = sidecar::config_dir(&app).map_err(CoreError::internal)?;
    let core_path = sidecar::packaged_sidecar_path(&app)
        .map_err(|error| CoreError::local("CONNECTOR_ERROR", error, false))?;
    let connector_config = json!({
        "coreConfigPath": config_dir.join("config.json"),
        "sidecarEntry": core_path,
        "pythonPath": "python3",
        "maxMessages": 30
    });
    install_connector_at(&source, &target, &connector_config)?;
    Ok(json!({ "installed": true }))
}

#[tauri::command]
fn uninstall_connector(_app: AppHandle) -> Result<Value, CoreError> {
    let home =
        std::env::var_os("HOME").ok_or_else(|| CoreError::internal("无法定位用户 Home 目录"))?;
    let target = std::path::PathBuf::from(home)
        .join(".pi")
        .join("agent")
        .join("extensions")
        .join("pi-wecom-context");
    if !target.exists() {
        return Ok(json!({ "removed": false }));
    }
    if !target.join(".managed-by-wecom-context").is_file() {
        return Err(CoreError::local(
            "CONNECTOR_ERROR",
            "目标 Connector 目录不是由本应用管理",
            false,
        ));
    }
    std::fs::remove_dir_all(&target).map_err(|error| CoreError::internal(error.to_string()))?;
    Ok(json!({ "removed": true }))
}

/// 启动自删除助手，清理本应用拥有的本地数据后退出当前 App。
#[tauri::command]
fn uninstall_application(app: AppHandle) -> Result<Value, CoreError> {
    uninstall::schedule(&app).map_err(|error| CoreError::local("UNINSTALL_FAILED", error, true))
}


fn escape_applescript(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn parse_loopback_proxy(value: &str) -> Option<(String, u16)> {
    let rest = value.strip_prefix("http://")?;
    if rest.contains('/') || rest.contains('@') {
        return None;
    }
    let (host, port) = rest.rsplit_once(':')?;
    if !matches!(host, "127.0.0.1" | "localhost" | "[::1]") {
        return None;
    }
    let port = port.parse::<u16>().ok()?;
    if port == 0 {
        return None;
    }
    Some((host.to_string(), port))
}

fn omp_proxy() -> Option<(String, u16)> {
    let home = std::env::var_os("HOME")?;
    let path = std::path::PathBuf::from(home)
        .join(".omp")
        .join("agent")
        .join(".env");
    let content = std::fs::read_to_string(path).ok()?;
    let mut fallback = None;
    for line in content.lines() {
        let Some((name, raw_value)) = line.trim().split_once('=') else {
            continue;
        };
        let value = raw_value.trim().trim_matches('"').trim_matches('\'');
        if name == "PI_PROXY" {
            fallback = parse_loopback_proxy(value);
        }
    }
    fallback
}

fn proxy_reachable(host: &str, port: u16) -> bool {
    let address = if host == "localhost" {
        "127.0.0.1"
    } else if host == "[::1]" {
        "::1"
    } else {
        host
    };
    let socket = format!("{address}:{port}");
    let Ok(address) = socket.parse::<std::net::SocketAddr>() else {
        return false;
    };
    std::net::TcpStream::connect_timeout(&address, std::time::Duration::from_millis(500)).is_ok()
}

fn pi_version() -> Result<String, CoreError> {
    let pi_bin = pi_binary_path()?;
    let output = std::process::Command::new(&pi_bin)
        .arg("--version")
        .output()
        .map_err(|error| {
            CoreError::local(
                "PI_UNAVAILABLE",
                format!("无法检查 Pi 版本: {error}"),
                false,
            )
        })?;
    if !output.status.success() {
        return Err(CoreError::local("PI_UNAVAILABLE", "Pi 版本检查失败", false));
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(str::to_string)
        .ok_or_else(|| CoreError::local("PI_UNAVAILABLE", "Pi 版本输出为空", false))
}

#[tauri::command]
fn check_pi_environment(app: AppHandle) -> Result<Value, CoreError> {
    let deepseek = provider_config::deepseek_status(app)?;
    let api_key_available = deepseek.get("configured") == Some(&Value::Bool(true));
    let proxy = omp_proxy();
    let proxy_reachable = proxy
        .as_ref()
        .is_some_and(|(host, port)| proxy_reachable(host, *port));
    Ok(json!({
        "version": pi_version()?,
        "auth_available": api_key_available,
        "api_key_available": api_key_available,
        "proxy_configured": proxy.is_some(),
        "proxy_reachable": proxy_reachable,
        "transport": "sse"
    }))
}

#[tauri::command]
fn launch_pi(app: AppHandle) -> Result<Value, CoreError> {
    let connector = check_connector(app.clone())?;
    if connector.get("installed") != Some(&Value::Bool(true)) {
        return Err(CoreError::local(
            "CONNECTOR_ERROR",
            "请先安装 Pi Connector",
            false,
        ));
    }
    let environment = check_pi_environment(app.clone())?;
    if environment.get("auth_available") != Some(&Value::Bool(true)) {
        return Err(CoreError::local(
            "DEEPSEEK_NOT_CONFIGURED",
            "尚未配置 DeepSeek API Key，请先点击模型旁边的配置按钮",
            false,
        ));
    }
    if environment.get("proxy_reachable") != Some(&Value::Bool(true)) {
        return Err(CoreError::local(
            "PI_UNAVAILABLE",
            "OMP 本地代理不可用",
            true,
        ));
    }
    let cwd = app
        .path()
        .app_data_dir()
        .map_err(|error| CoreError::internal(error.to_string()))?;
    let settings_dir = cwd.join(".pi");
    std::fs::create_dir_all(&settings_dir)
        .map_err(|error| CoreError::internal(error.to_string()))?;
    std::fs::write(
        settings_dir.join("settings.json"),
        b"{\"transport\":\"sse\"}\n",
    )
    .map_err(|error| CoreError::internal(error.to_string()))?;
    let cwd_string = escape_applescript(cwd.to_string_lossy().as_ref());
    let (proxy_host, proxy_port) = omp_proxy()
        .ok_or_else(|| CoreError::local("PI_UNAVAILABLE", "未配置 OMP 本地代理", true))?;
    let proxy = format!("http://{proxy_host}:{proxy_port}");
    let pi_bin = shell_quote(&pi_binary_path()?);
    let model = provider_config::configured_deepseek_model(&app)?
        .unwrap_or_else(|| "deepseek/deepseek-chat".to_string());
    let model = shell_quote(&model);
    let command = format!(
        "cd \"{cwd_string}\" && export PI_PROXY=\"{proxy}\" HTTP_PROXY=\"{proxy}\" HTTPS_PROXY=\"{proxy}\" ALL_PROXY=\"{proxy}\" && PI_BIN={pi_bin} && [ -x \"$PI_BIN\" ] && exec \"$PI_BIN\" --model {model} --thinking max --approve --extension \"$HOME/.pi/agent/extensions/pi-wecom-context/index.ts\" \"/wecom-context status\"; status=$?; printf '\\nPi 启动失败（exit %s），按回车关闭此窗口...\\n' \"$status\"; read",
    );
    let script_command = escape_applescript(&command);
    let script = format!(
        "tell application \"iTerm2\"\nactivate\nset newWindow to (create window with default profile)\ntell current session of newWindow\nwrite text \"{script_command}\"\nend tell\nend tell"
    );
    let status = std::process::Command::new("/usr/bin/osascript")
        .args(["-e", &script])
        .status()
        .map_err(|error| CoreError::internal(format!("无法启动 iTerm: {error}")))?;
    if !status.success() {
        return Err(CoreError::internal("iTerm 启动 Pi 失败"));
    }
    Ok(json!({ "started": true, "version": environment.get("version"), "transport": "sse" }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("wecom-context-{name}-{}", std::process::id()))
    }

    const DATASET_A: &str = "07e6cb301334";
    const DATASET_B: &str = "a4f2fab88be8";
    const SESSION_ONE: &str = "a1b2c3d4e5f60718";
    const SESSION_TWO: &str = "0f0f0f0f0f0f0f0f";

    fn seed_conversation(
        root: &std::path::Path,
        dataset_id: &str,
        session_key: &str,
        session_name: &str,
        model: &str,
        messages: &[(&str, &str, u64)],
    ) {
        let dir = sharded_conversation_dir(root, dataset_id, session_key);
        std::fs::create_dir_all(&dir).expect("conversation dir");
        save_meta_at(
            &dir.join(CONVERSATION_META_FILE),
            &ConversationMeta {
                session_name: session_name.to_string(),
                model: model.to_string(),
                dataset_id: dataset_id.to_string(),
                session_key: session_key.to_string(),
                conversation_id: "a".repeat(32),
                snapshot_id: "snapshot-1".to_string(),
            },
        )
        .expect("conversation meta");
        for (role, text, ts) in messages {
            append_history_at(&dir.join(CONVERSATION_FILE), role, text, *ts)
                .expect("history append");
        }
    }

    /// 旧布局的平铺目录：`<agents>/<session_key>/`。
    fn seed_legacy_conversation(
        root: &std::path::Path,
        session_key: &str,
        session_name: &str,
        model: &str,
        messages: &[(&str, &str, u64)],
    ) {
        let dir = root.join(session_key);
        std::fs::create_dir_all(&dir).expect("legacy conversation dir");
        save_meta_at(
            &dir.join(CONVERSATION_META_FILE),
            &ConversationMeta {
                session_name: session_name.to_string(),
                model: model.to_string(),
                ..ConversationMeta::default()
            },
        )
        .expect("legacy meta");
        for (role, text, ts) in messages {
            append_history_at(&dir.join(CONVERSATION_FILE), role, text, *ts)
                .expect("legacy history append");
        }
    }

    fn create_connector_source(root: &std::path::Path) {
        let files = [
            "index.ts",
            "src/commands.ts",
            "src/config.ts",
            "src/core-client.ts",
            "src/tool.ts",
            "src/types.ts",
            "src/wecom-cli.ts",
        ];
        for relative in files {
            let path = root.join(relative);
            std::fs::create_dir_all(path.parent().expect("source parent"))
                .expect("source directory");
            std::fs::write(path, b"test").expect("source file");
        }
    }

    #[test]
    fn connector_install_stages_complete_managed_tree() {
        let root = temp_root("install");
        let source = root.join("source");
        let target = root.join("target");
        std::fs::create_dir_all(&source).expect("source root");
        create_connector_source(&source);
        install_connector_at(&source, &target, &json!({ "sidecarEntry": "/tmp/core" }))
            .expect("install");
        assert!(connector_is_valid(&target));
        assert!(target.join("config.json").is_file());
        // index.ts 会 import src/wecom-cli.js：缺这个文件的旧扩展必须判为无效。
        std::fs::remove_file(target.join("src/wecom-cli.ts")).expect("remove wecom-cli");
        assert!(!connector_is_valid(&target));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn connector_install_rejects_unmanaged_target() {
        let root = temp_root("unmanaged");
        let source = root.join("source");
        let target = root.join("target");
        std::fs::create_dir_all(&source).expect("source root");
        create_connector_source(&source);
        std::fs::create_dir_all(&target).expect("target root");
        std::fs::write(target.join("README"), b"unmanaged").expect("target marker");
        let error = install_connector_at(&source, &target, &json!({}))
            .expect_err("unmanaged target must fail");
        assert!(error.message.contains("不是由本应用管理"));
        assert!(target.join("README").is_file());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn conversation_history_roundtrips_per_contact() {
        let root = temp_root("history-roundtrip");
        seed_conversation(
            &root,
            DATASET_A,
            SESSION_ONE,
            "示例联系人",
            "deepseek/deepseek-v4-flash",
            &[
                ("user", "你好", 1_700_000_000_000),
                ("assistant", "你好！有什么可以帮你？", 1_700_000_005_000),
            ],
        );
        let dir = sharded_conversation_dir(&root, DATASET_A, SESSION_ONE);
        let loaded = load_history_at(&dir.join(CONVERSATION_FILE));
        assert_eq!(loaded.len(), 2);
        assert_eq!(loaded[0].role, "user");
        assert_eq!(loaded[0].text, "你好");
        assert_eq!(loaded[1].role, "assistant");
        assert_eq!(loaded[1].ts, 1_700_000_005_000);
        let meta = load_meta_at(&dir.join(CONVERSATION_META_FILE));
        assert_eq!(meta.session_name, "示例联系人");
        assert_eq!(meta.model, "deepseek/deepseek-v4-flash");
        assert_eq!(meta.dataset_id, DATASET_A);
        assert_eq!(meta.session_key, SESSION_ONE);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn summary_lists_one_conversation_per_contact_newest_first() {
        let root = temp_root("history-summary");
        seed_conversation(
            &root,
            DATASET_A,
            SESSION_ONE,
            "示例联系人",
            "deepseek/deepseek-v4-flash",
            &[("user", "第一句", 1_000), ("assistant", "回复", 2_000)],
        );
        seed_conversation(
            &root,
            DATASET_A,
            SESSION_TWO,
            "示例项目群",
            "deepseek/deepseek-v4-pro",
            &[("user", "群里的问题", 9_000)],
        );
        // 探针目录、只有元数据没有消息的目录、以及非两级布局都必须被忽略。
        seed_conversation(
            &root,
            "not-hex-dataset",
            SESSION_ONE,
            "无效企业",
            "",
            &[("user", "非法", 99_000)],
        );
        let empty = sharded_conversation_dir(&root, DATASET_A, "1111111111111111");
        std::fs::create_dir_all(&empty).expect("empty dir");
        std::fs::create_dir_all(root.join(UNASSIGNED_DATASET_DIR).join(SESSION_TWO))
            .expect("unassigned dir");
        append_history_at(
            &root
                .join(UNASSIGNED_DATASET_DIR)
                .join(SESSION_TWO)
                .join(CONVERSATION_FILE),
            "user",
            "忽略",
            88_000,
        )
        .expect("unassigned history");

        let summaries = summarize_conversations(&root);
        assert_eq!(summaries.len(), 2);
        assert_eq!(summaries[0].session_key, SESSION_TWO);
        assert_eq!(summaries[0].session_name, "示例项目群");
        assert_eq!(summaries[0].message_count, 1);
        assert_eq!(summaries[0].preview, "群里的问题");
        assert_eq!(summaries[0].dataset_id, DATASET_A);
        assert_eq!(
            summaries[0].conversation_id,
            agent_key(DATASET_A, SESSION_TWO)
        );
        assert_eq!(summaries[1].session_key, SESSION_ONE);
        assert_eq!(summaries[1].message_count, 2);
        assert_eq!(summaries[1].updated_at, 2_000);
        assert_eq!(summaries[1].preview, "回复");
        assert_eq!(summaries[1].model, "deepseek/deepseek-v4-flash");
        assert_eq!(summaries[1].snapshot_id, "snapshot-1");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn summary_falls_back_to_placeholder_name_and_truncates_preview() {
        let root = temp_root("history-preview");
        let long = "很长的回复".repeat(40);
        seed_conversation(
            &root,
            DATASET_A,
            "2222222222222222",
            "",
            "",
            &[("assistant", &long, 500)],
        );
        let summaries = summarize_conversations(&root);
        assert_eq!(summaries.len(), 1);
        assert_eq!(summaries[0].session_name, "会话 222222");
        assert_eq!(summaries[0].preview.chars().count(), PREVIEW_CHARACTERS + 1);
        assert!(summaries[0].preview.ends_with('…'));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn history_skips_corrupt_lines_and_missing_files() {
        let root = temp_root("history-corrupt");
        let dir = sharded_conversation_dir(&root, DATASET_A, "3333333333333333");
        std::fs::create_dir_all(&dir).expect("conversation dir");
        std::fs::write(
            dir.join(CONVERSATION_FILE),
            b"{\"role\":\"user\",\"text\":\"ok\",\"ts\":7}\nnot json\n{\"role\":\"assistant\"}\n",
        )
        .expect("corrupt history");
        let loaded = load_history_at(&dir.join(CONVERSATION_FILE));
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].text, "ok");
        assert!(load_history_at(&dir.join("missing.jsonl")).is_empty());
        assert_eq!(
            load_meta_at(&dir.join(CONVERSATION_META_FILE)).session_name,
            ""
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn summaries_and_history_are_isolated_per_dataset() {
        let root = temp_root("dataset-isolation");
        seed_conversation(
            &root,
            DATASET_A,
            SESSION_ONE,
            "示例企业 A 的联系人",
            "deepseek/deepseek-v4-flash",
            &[("user", "A 企业的消息", 1_000)],
        );
        seed_conversation(
            &root,
            DATASET_B,
            SESSION_ONE,
            "其他企业的同名会话",
            "deepseek/deepseek-v4-flash",
            &[("user", "B 企业的消息", 2_000)],
        );

        // 同一 session_key 在两个企业下互不可见：各自的历史只含自己的消息。
        let history_a = load_history_at(
            &sharded_conversation_dir(&root, DATASET_A, SESSION_ONE).join(CONVERSATION_FILE),
        );
        let history_b = load_history_at(
            &sharded_conversation_dir(&root, DATASET_B, SESSION_ONE).join(CONVERSATION_FILE),
        );
        assert_eq!(history_a.len(), 1);
        assert_eq!(history_a[0].text, "A 企业的消息");
        assert_eq!(history_b.len(), 1);
        assert_eq!(history_b[0].text, "B 企业的消息");

        let summaries = summarize_conversations(&root);
        assert_eq!(summaries.len(), 2);
        let dataset_a = summaries
            .iter()
            .find(|summary| summary.dataset_id == DATASET_A)
            .expect("dataset A summary");
        let dataset_b = summaries
            .iter()
            .find(|summary| summary.dataset_id == DATASET_B)
            .expect("dataset B summary");
        assert_eq!(dataset_a.session_key, dataset_b.session_key);
        assert_eq!(dataset_a.session_name, "示例企业 A 的联系人");
        assert_eq!(dataset_b.session_name, "其他企业的同名会话");
        assert_eq!(dataset_a.conversation_id, agent_key(DATASET_A, SESSION_ONE));
        assert_eq!(dataset_b.conversation_id, agent_key(DATASET_B, SESSION_ONE));

        // 复合身份能唯一定位到所属企业。
        let resolved = resolve_conversation_dir(&root, &agent_key(DATASET_B, SESSION_ONE))
            .expect("resolved conversation dir");
        assert_eq!(resolved.dataset_id, DATASET_B);
        // 裸 session_key 跨企业有歧义时必须拒绝，绝不猜企业。
        assert!(resolve_conversation_dir(&root, SESSION_ONE).is_none());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn legacy_flat_history_migrates_into_selected_dataset_and_is_idempotent() {
        let root = temp_root("legacy-migrate");
        seed_legacy_conversation(
            &root,
            SESSION_ONE,
            "示例联系人",
            "deepseek/deepseek-v4-flash",
            &[("user", "旧布局的消息", 1_000)],
        );

        let warnings = migrate_legacy_history(&root, Some(DATASET_A));
        assert!(warnings.is_empty(), "有唯一企业时不应产生警告");
        assert!(!root.join(SESSION_ONE).exists(), "旧平铺目录必须被迁走");
        let migrated = sharded_conversation_dir(&root, DATASET_A, SESSION_ONE);
        let history = load_history_at(&migrated.join(CONVERSATION_FILE));
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].text, "旧布局的消息");
        let meta = load_meta_at(&migrated.join(CONVERSATION_META_FILE));
        assert_eq!(meta.dataset_id, DATASET_A);
        assert_eq!(meta.session_key, SESSION_ONE);
        assert_eq!(meta.conversation_id, agent_key(DATASET_A, SESSION_ONE));
        assert_eq!(meta.session_name, "示例联系人");
        assert_eq!(meta.model, "deepseek/deepseek-v4-flash");

        // 幂等：再跑一次不产生新目录、也不重复追加历史。
        let before = std::fs::read_to_string(migrated.join(CONVERSATION_FILE)).expect("history");
        let second = migrate_legacy_history(&root, Some(DATASET_A));
        assert!(second.is_empty());
        assert_eq!(
            std::fs::read_to_string(migrated.join(CONVERSATION_FILE)).expect("history"),
            before
        );
        assert_eq!(summarize_conversations(&root).len(), 1);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn legacy_flat_history_merges_into_existing_sharded_conversation() {
        let root = temp_root("legacy-merge");
        // 新布局下已经聊过，旧平铺目录里还留着更早的历史：合并而不是覆盖。
        seed_conversation(
            &root,
            DATASET_A,
            SESSION_ONE,
            "示例联系人",
            "deepseek/deepseek-v4-flash",
            &[("user", "新布局的消息", 3_000)],
        );
        seed_legacy_conversation(
            &root,
            SESSION_ONE,
            "示例联系人",
            "deepseek/deepseek-v4-flash",
            &[("user", "旧布局的消息", 1_000)],
        );

        let warnings = migrate_legacy_history(&root, Some(DATASET_A));
        assert!(warnings.is_empty());
        assert!(!root.join(SESSION_ONE).exists(), "合并后旧目录必须被删除");
        let dir = sharded_conversation_dir(&root, DATASET_A, SESSION_ONE);
        let history = load_history_at(&dir.join(CONVERSATION_FILE));
        assert_eq!(history.len(), 2, "两边的历史都必须保留");
        let texts: Vec<&str> = history.iter().map(|entry| entry.text.as_str()).collect();
        assert!(texts.contains(&"新布局的消息"));
        assert!(texts.contains(&"旧布局的消息"));
        let meta = load_meta_at(&dir.join(CONVERSATION_META_FILE));
        assert_eq!(meta.session_name, "示例联系人");
        assert_eq!(meta.dataset_id, DATASET_A);
        assert_eq!(meta.snapshot_id, "snapshot-1");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn legacy_flat_history_without_dataset_goes_to_unassigned() {
        let root = temp_root("legacy-unassigned");
        seed_legacy_conversation(
            &root,
            SESSION_TWO,
            "未知企业会话",
            "deepseek/deepseek-v4-flash",
            &[("user", "无处安放的消息", 1_500)],
        );

        let warnings = migrate_legacy_history(&root, None);
        assert_eq!(warnings, vec![LEGACY_HISTORY_UNASSIGNED.to_string()]);
        let unassigned = root.join(UNASSIGNED_DATASET_DIR).join(SESSION_TWO);
        assert!(unassigned.join(CONVERSATION_FILE).is_file());
        assert!(!root.join(SESSION_TWO).exists());
        // 未归属企业绝不猜测，也不出现在任何列表里。
        assert!(summarize_conversations(&root).is_empty());
        assert_eq!(migrate_legacy_history(&root, None), Vec::<String>::new());
        let _ = std::fs::remove_dir_all(root);
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            get_status,
            bootstrap_workbench,
            discover_datasets,
            select_dataset,
            list_sessions,
            select_session,
            bind_session,
            clear_session,
            preview_context,
            refresh_snapshot,
            set_send_permission,
            check_connector,
            install_connector,
            uninstall_connector,
            uninstall_application,
            check_pi_environment,
            launch_pi,
            allow_session,
            agent_send_message,
            memory_context,
            memory_history,
            memory_injections,
            memory_clear,
            agent_status,
            agent_stop,
            agent_new_conversation,
            preview_context_package,
            download_images,
            open_accessibility_settings,
            package_image_data,
            remove_account,
            remove_dataset,
            capture_key,
            restore_dataset,
            agent_history,
            agent_history_list,
            provider_config::deepseek_status,
            provider_config::deepseek_configure,
            provider_config::agent_list_models,
        ])
        .manage(main_agent::AgentState::default())
        .setup(|app| {
            if let Ok(Some(path)) = packaged_pi_binary_path(app.handle()) {
                std::env::set_var("WECOM_CONTEXT_PI_BIN", path);
            }
            if let Err(error) = provider_config::cleanup_legacy_model_config(app.handle()) {
                eprintln!("模型配置清理失败：{error:?}");
            }
            // API Key 由用户在应用内配置，不把任何模型密钥打进安装包。
            // 上次清空没做完就继续做完，绝不把没清干净的会话当成新记忆使用。
            main_agent::resume_unfinished_clear(app.handle());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running WeCom Context")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<main_agent::AgentState>() {
                    main_agent::shutdown(&state);
                }
            }
        });
}
