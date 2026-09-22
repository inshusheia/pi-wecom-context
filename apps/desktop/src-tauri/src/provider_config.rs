//! DeepSeek 官方 API 配置。
//!
//! 唯一事实源是 App 配置 `config.json` 的 `custom_providers` 数组；
//! 本模块负责验证用户 API Key、拉取官方模型目录，并把唯一的 DeepSeek
//! provider 渲染到 pi 的 `~/.pi/agent/models.json`。
//!
//! 配置成功后只写入 DeepSeek 官方 endpoint 与该 API Key 返回的模型，
//! 不保留 OpenCode、Codex 或其它供应商条目。

use crate::sidecar::CoreError;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::{Read as _, Write as _};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const THINKING_LEVELS: &[&str] = &["off", "minimal", "low", "medium", "high", "xhigh", "max"];
const DEFAULT_CONTEXT_WINDOW: u64 = 128_000;
const DEFAULT_MAX_TOKENS: u64 = 16_384;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CustomModel {
    pub id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub reasoning: bool,
    #[serde(default)]
    pub input_images: bool,
    #[serde(default)]
    pub context_window: Option<u64>,
    #[serde(default)]
    pub max_tokens: Option<u64>,
    #[serde(default)]
    pub thinking_level_map: Option<BTreeMap<String, Option<String>>>,
    #[serde(default)]
    pub compat: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CustomProvider {
    pub provider_id: String,
    pub display_name: String,
    pub base_url: String,
    pub api: String,
    pub api_key: String,
    #[serde(default)]
    pub auth_header: bool,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    #[serde(default)]
    pub enabled: bool,
    pub models: Vec<CustomModel>,
    #[serde(default)]
    pub updated_at: String,
}

pub fn is_valid_model_id(model_id: &str) -> bool {
    let bytes = model_id.as_bytes();
    bytes.len() >= 2
        && bytes.len() <= 96
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || matches!(*byte, b'.' | b'-' | b'_')
        })
}

/// 校验 DeepSeek provider 配置；错误信息直接面向 UI 展示。
pub fn validate_provider(provider: &CustomProvider) -> Result<(), String> {
    if provider.provider_id != DEEPSEEK_PROVIDER_ID {
        return Err("只支持 DeepSeek 官方 provider".to_string());
    }
    if provider.display_name.trim().is_empty() {
        return Err("display_name 不能为空".to_string());
    }
    if provider.base_url != DEEPSEEK_BASE_URL {
        return Err("base_url 必须是 DeepSeek 官方地址".to_string());
    }
    if provider.api != "openai-completions" {
        return Err("DeepSeek 仅支持 openai-completions".to_string());
    }
    if provider.api_key.trim().is_empty() {
        return Err("api_key 不能为空".to_string());
    }
    if provider.models.is_empty() {
        return Err("至少需要一个模型".to_string());
    }
    let mut seen = std::collections::HashSet::new();
    for model in &provider.models {
        if !is_valid_model_id(&model.id) {
            return Err(format!(
                "模型 id「{}」无效：2-96 位小写字母/数字/点/短横线/下划线",
                model.id
            ));
        }
        if !seen.insert(model.id.clone()) {
            return Err(format!("模型 id「{}」重复", model.id));
        }
        if let Some(map) = &model.thinking_level_map {
            for key in map.keys() {
                if !THINKING_LEVELS.contains(&key.as_str()) {
                    return Err(format!(
                        "thinking_level_map 键「{key}」无效，仅支持 {}",
                        THINKING_LEVELS.join("/")
                    ));
                }
            }
        }
    }
    Ok(())
}

/// 渲染成 pi models.json 的 provider 条目（字段语义对齐 provider-composer.js `modelFromJson`）。
pub fn provider_entry(provider: &CustomProvider) -> Value {
    let models: Vec<Value> = provider
        .models
        .iter()
        .map(|model| {
            let mut entry = serde_json::Map::new();
            entry.insert("id".into(), Value::String(model.id.clone()));
            let name = if model.name.trim().is_empty() {
                model.id.clone()
            } else {
                model.name.clone()
            };
            entry.insert("name".into(), Value::String(name));
            entry.insert("api".into(), Value::String(provider.api.clone()));
            entry.insert("baseUrl".into(), Value::String(provider.base_url.clone()));
            entry.insert("reasoning".into(), Value::Bool(model.reasoning));
            let mut input = vec![Value::String("text".into())];
            if model.input_images {
                input.push(Value::String("image".into()));
            }
            entry.insert("input".into(), Value::Array(input));
            entry.insert(
                "contextWindow".into(),
                Value::from(model.context_window.unwrap_or(DEFAULT_CONTEXT_WINDOW)),
            );
            entry.insert(
                "maxTokens".into(),
                Value::from(model.max_tokens.unwrap_or(DEFAULT_MAX_TOKENS)),
            );
            match &model.thinking_level_map {
                Some(map) => {
                    // 非推理模型且未显式配置：写全 null，显式禁用思考（spawn 固定 --thinking max）。
                    let value = if model.reasoning {
                        Value::Object(
                            map.iter()
                                .map(|(k, v)| {
                                    (
                                        k.clone(),
                                        v.clone().map(Value::String).unwrap_or(Value::Null),
                                    )
                                })
                                .collect(),
                        )
                    } else {
                        Value::Object(
                            THINKING_LEVELS
                                .iter()
                                .map(|level| (level.to_string(), Value::Null))
                                .collect(),
                        )
                    };
                    entry.insert("thinkingLevelMap".into(), value);
                }
                // 无显式映射：推理模型走 pi 默认映射；非推理模型显式全 null。
                None => {
                    if !model.reasoning {
                        let mut all_null = serde_json::Map::new();
                        for level in THINKING_LEVELS {
                            all_null.insert(level.to_string(), Value::Null);
                        }
                        entry.insert("thinkingLevelMap".into(), Value::Object(all_null));
                    }
                }
            }
            if let Some(compat) = &model.compat {
                if compat.is_object() && !compat.as_object().is_some_and(|map| map.is_empty()) {
                    entry.insert("compat".into(), compat.clone());
                }
            }
            Value::Object(entry)
        })
        .collect();
    let mut entry = serde_json::Map::new();
    entry.insert("name".into(), Value::String(provider.display_name.clone()));
    entry.insert("baseUrl".into(), Value::String(provider.base_url.clone()));
    entry.insert("apiKey".into(), Value::String(provider.api_key.clone()));
    entry.insert("api".into(), Value::String(provider.api.clone()));
    if !provider.headers.is_empty() {
        entry.insert(
            "headers".into(),
            Value::Object(
                provider
                    .headers
                    .iter()
                    .map(|(k, v)| (k.clone(), Value::String(v.clone())))
                    .collect(),
            ),
        );
    }
    entry.insert("authHeader".into(), Value::Bool(provider.auth_header));
    entry.insert("models".into(), Value::Array(models));
    Value::Object(entry)
}

/// 合并结果：`None` 表示既有文件存在但损坏，调用方应先备份。
pub fn merge_models_json(existing: Option<&str>, providers: &[CustomProvider]) -> (Value, bool) {
    let mut corrupt = false;
    let mut providers_map = match existing {
        Some(text) if !text.trim().is_empty() => match serde_json::from_str::<Value>(text) {
            Ok(Value::Object(root)) => {
                let map = root
                    .get("providers")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                map
            }
            _ => {
                corrupt = true;
                serde_json::Map::new()
            }
        },
        _ => serde_json::Map::new(),
    };
    let managed_ids: Vec<&str> = providers
        .iter()
        .map(|provider| provider.provider_id.as_str())
        .collect();
    // 先移除本应用管理的全部 id（enabled 的随后重新写入）。
    for id in &managed_ids {
        providers_map.remove(*id);
    }
    for provider in providers.iter().filter(|provider| provider.enabled) {
        providers_map.insert(provider.provider_id.clone(), provider_entry(provider));
    }
    (
        Value::Object(serde_json::Map::from_iter([(
            "providers".to_string(),
            Value::Object(providers_map),
        )])),
        corrupt,
    )
}

/// 原子写 models.json：temp + rename，unix 上 0600。
pub fn write_models_json(path: &Path, value: &Value) -> Result<(), CoreError> {
    let parent = path
        .parent()
        .ok_or_else(|| CoreError::internal("models.json 缺少父目录"))?;
    std::fs::create_dir_all(parent).map_err(|error| CoreError::internal(error.to_string()))?;
    let body =
        serde_json::to_vec_pretty(value).map_err(|error| CoreError::internal(error.to_string()))?;
    let temp = parent.join(format!(".models.json.tmp-{}", std::process::id()));
    std::fs::write(&temp, &body).map_err(|error| CoreError::internal(error.to_string()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&temp, std::fs::Permissions::from_mode(0o600));
    }
    std::fs::rename(&temp, path).map_err(|error| CoreError::internal(error.to_string()))?;
    Ok(())
}

/// 把损坏的 models.json 备份成 `<path>.corrupt-<毫秒时间戳>`；返回备份路径。
pub fn backup_corrupt(path: &Path) -> Result<Option<PathBuf>, CoreError> {
    if !path.is_file() {
        return Ok(None);
    }
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis())
        .unwrap_or(0);
    let target = path.with_extension(format!("json.corrupt-{stamp}"));
    std::fs::rename(path, &target).map_err(|error| CoreError::internal(error.to_string()))?;
    Ok(Some(target))
}

/* ------------------------------------------------------------------ *
 * pi --list-models 输出解析
 * ------------------------------------------------------------------ */

#[derive(Debug, Clone, Serialize)]
pub struct AgentModelInfo {
    pub id: String,
    pub provider: String,
    pub model: String,
    pub name: String,
    pub context: String,
    pub max_out: String,
    pub thinking: bool,
    pub images: bool,
}

/// 解析 `pi --list-models` 的固定列表格；Warning 行（stderr 混入）与空行跳过。
pub fn parse_list_models(output: &str) -> Vec<AgentModelInfo> {
    let mut models = Vec::new();
    for line in output.lines() {
        let tokens: Vec<&str> = line.split_whitespace().collect();
        if tokens.len() != 6 || tokens[0] == "provider" {
            continue;
        }
        models.push(AgentModelInfo {
            id: format!("{}/{}", tokens[0], tokens[1]),
            provider: tokens[0].to_string(),
            model: tokens[1].to_string(),
            name: tokens[1].to_string(),
            context: tokens[2].to_string(),
            max_out: tokens[3].to_string(),
            thinking: tokens[4] == "yes",
            images: tokens[5] == "yes",
        });
    }
    models
}

/// 校验某 provider 的模型是否出现在 `pi --list-models <search>` 输出中。
pub fn provider_models_present(
    output: &str,
    provider_id: &str,
    expected: &[String],
) -> Result<(), String> {
    let listed = parse_list_models(output);
    let expected_ids: Vec<String> = expected
        .iter()
        .map(|model| format!("{provider_id}/{model}"))
        .collect();
    let missing: Vec<&String> = expected_ids
        .iter()
        .filter(|id| !listed.iter().any(|model| &model.id == *id))
        .collect();
    if missing.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "pi --list-models 未列出 {}；models.json 可能存在 schema 或连通性问题",
            missing
                .iter()
                .map(|item| item.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ))
    }
}

pub fn config_custom_providers(config: &Value) -> Vec<CustomProvider> {
    config
        .get("custom_providers")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| serde_json::from_value::<CustomProvider>(item.clone()).ok())
                .collect()
        })
        .unwrap_or_default()
}

/* ------------------------------------------------------------------ *
 * Tauri 命令：读写 config.json、渲染 models.json、pi 连通性验证
 * ------------------------------------------------------------------ */

use crate::pi_binary_path;
use tauri::AppHandle;

const PI_LIST_TIMEOUT_MS: u64 = 20_000;
const DEEPSEEK_PROVIDER_ID: &str = "deepseek";
const DEEPSEEK_BASE_URL: &str = "https://api.deepseek.com";
const DEEPSEEK_MODELS_PATH: &str = "/models";

fn config_path(app: &AppHandle) -> Result<PathBuf, CoreError> {
    crate::sidecar::config_dir(app)
        .map(|dir| dir.join("config.json"))
        .map_err(CoreError::internal)
}

fn load_app_config(path: &Path) -> Value {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|contents| serde_json::from_str(&contents).ok())
        .unwrap_or_else(|| json!({}))
}

/// 读改写 config.json：保留 sidecar 管理的其它键；临时文件 + rename 原子替换。
fn save_app_config(path: &Path, config: &Value) -> Result<(), CoreError> {
    let parent = path
        .parent()
        .ok_or_else(|| CoreError::internal("config.json 缺少父目录"))?;
    std::fs::create_dir_all(parent).map_err(|error| CoreError::internal(error.to_string()))?;
    let body = serde_json::to_vec_pretty(config)
        .map_err(|error| CoreError::internal(error.to_string()))?;
    let temp = parent.join(format!(".config.json.tmp-{}", std::process::id()));
    std::fs::write(&temp, &body).map_err(|error| CoreError::internal(error.to_string()))?;
    std::fs::rename(&temp, path).map_err(|error| CoreError::internal(error.to_string()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
            .map_err(|error| CoreError::internal(error.to_string()))?;
    }
    Ok(())
}
fn models_json_path() -> Result<PathBuf, CoreError> {
    let home =
        std::env::var_os("HOME").ok_or_else(|| CoreError::internal("无法定位用户 Home 目录"))?;
    Ok(PathBuf::from(home)
        .join(".pi")
        .join("agent")
        .join("models.json"))
}

/// 运行 `pi --list-models [search]`，返回 (stdout, stderr)。
/// pi 是 `#!/usr/bin/env node` 脚本：从 GUI 启动时 PATH 需要补 pi 同目录（见 spawn_agent 注释）。
pub fn run_pi(
    args: &[&str],
    timeout_ms: u64,
    agent_dir: Option<&Path>,
) -> Result<(String, String), CoreError> {
    let pi_bin = pi_binary_path()?;
    let search_path = match std::path::Path::new(&pi_bin).parent() {
        Some(dir) => format!(
            "{}:{}",
            dir.display(),
            std::env::var("PATH").unwrap_or_default()
        ),
        None => std::env::var("PATH").unwrap_or_default(),
    };
    let mut command = Command::new(&pi_bin);
    command.args(args);
    command
        .env("PATH", &search_path)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(dir) = agent_dir {
        command.env("PI_CODING_AGENT_DIR", dir);
    }
    let mut child = command.spawn().map_err(|error| {
        CoreError::local("PI_UNAVAILABLE", format!("pi 启动失败: {error}"), false)
    })?;
    let deadline = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);
    loop {
        match child.try_wait() {
            Ok(Some(_status)) => break,
            Ok(None) => {
                if std::time::Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(CoreError::local("PI_UNAVAILABLE", "pi 子进程超时", true));
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(error) => return Err(CoreError::internal(error.to_string())),
        }
    }
    let mut stdout = String::new();
    let mut stderr = String::new();
    if let Some(mut pipe) = child.stdout.take() {
        let _ = pipe.read_to_string(&mut stdout);
    }
    if let Some(mut pipe) = child.stderr.take() {
        let _ = pipe.read_to_string(&mut stderr);
    }
    Ok((stdout, stderr))
}

/// 运行 `pi --list-models [search]`。
pub fn run_pi_list_models(
    search: Option<&str>,
    agent_dir: Option<&Path>,
    offline: bool,
) -> Result<(String, String), CoreError> {
    let mut args: Vec<&str> = vec!["--list-models"];
    if let Some(search) = search {
        args.push(search);
    }
    if offline {
        args.push("--offline");
    }
    run_pi(&args, PI_LIST_TIMEOUT_MS, agent_dir)
}

/// 用临时 PI_CODING_AGENT_DIR 验证候选 models.json：pi 离线启动 + list-models，
/// 成功前不落盘真实路径，失败时把 pi 的告警原样带回。
fn validate_candidate(
    providers: &[CustomProvider],
    provider_id: &str,
    expected_model_ids: &[String],
) -> Result<(), CoreError> {
    let path = models_json_path()?;
    let existing = std::fs::read_to_string(&path).ok();
    let (merged, _) = merge_models_json(existing.as_deref(), providers);
    let temp = std::env::temp_dir().join(format!(
        "wecom-models-check-{}-{}",
        std::process::id(),
        now_millis_value()
    ));
    std::fs::create_dir_all(&temp).map_err(|error| CoreError::internal(error.to_string()))?;
    let candidate = temp.join("models.json");
    let result = (|| -> Result<(), CoreError> {
        std::fs::write(
            &candidate,
            serde_json::to_vec_pretty(&merged)
                .map_err(|error| CoreError::internal(error.to_string()))?,
        )
        .map_err(|error| CoreError::internal(error.to_string()))?;
        let (stdout, stderr) = run_pi_list_models(Some(provider_id), Some(&temp), true)?;
        if let Err(message) = provider_models_present(&stdout, provider_id, expected_model_ids) {
            let warning = stderr.trim();
            return Err(CoreError::local(
                "MODEL_VALIDATION_FAILED",
                if warning.is_empty() {
                    message
                } else {
                    format!("{message}\npi 输出：{warning}")
                },
                true,
            ));
        }
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&temp);
    result
}

fn now_millis_value() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis())
        .unwrap_or(0)
}

/// 未配置 API Key 时返回空目录；已配置时只返回该 Key 的官方 `/models` 结果。
#[tauri::command]
pub async fn agent_list_models(app: AppHandle) -> Result<Value, CoreError> {
    let path = config_path(&app)?;
    let config = load_app_config(&path);
    let Some(provider) = deepseek_provider_from_config(&config) else {
        return Ok(json!({
            "configured": false,
            "models": [],
            "warning": Value::Null,
        }));
    };
    Ok(json!({
        "configured": true,
        "models": deepseek_model_infos(&provider),
        "warning": Value::Null,
    }))
}
fn deepseek_provider_from_config(config: &Value) -> Option<CustomProvider> {
    config_custom_providers(config)
        .into_iter()
        .find(|provider| {
            provider.provider_id == DEEPSEEK_PROVIDER_ID
                && provider.base_url == DEEPSEEK_BASE_URL
                && provider.api == "openai-completions"
                && provider.enabled
                && !provider.api_key.trim().is_empty()
                && validate_provider(provider).is_ok()
        })
        .map(normalize_deepseek_provider)
}

fn deepseek_model_info(model: &CustomModel) -> AgentModelInfo {
    AgentModelInfo {
        id: format!("{DEEPSEEK_PROVIDER_ID}/{}", model.id),
        provider: DEEPSEEK_PROVIDER_ID.to_string(),
        model: model.id.clone(),
        name: if model.name.trim().is_empty() {
            model.id.clone()
        } else {
            model.name.clone()
        },
        context: "128K".to_string(),
        max_out: "16K".to_string(),
        thinking: model.reasoning,
        images: model.input_images,
    }
}

fn deepseek_model_infos(provider: &CustomProvider) -> Vec<AgentModelInfo> {
    provider.models.iter().map(deepseek_model_info).collect()
}

fn deepseek_provider(api_key: String, models: Vec<CustomModel>) -> CustomProvider {
    CustomProvider {
        provider_id: DEEPSEEK_PROVIDER_ID.to_string(),
        display_name: "DeepSeek 官方".to_string(),
        base_url: DEEPSEEK_BASE_URL.to_string(),
        api: "openai-completions".to_string(),
        api_key,
        auth_header: false,
        headers: BTreeMap::new(),
        enabled: true,
        models,
        updated_at: String::new(),
    }
}

fn deepseek_model_display_name(id: &str) -> Option<&'static str> {
    match id {
        "deepseek-flash" | "deepseek-v4-flash" | "deepseek-v4-flash-vision-exp" => {
            Some("DeepSeek-V4.1-Flash")
        }
        "deepseek-v4-pro" => Some("DeepSeek-V4-Pro"),
        _ => None,
    }
}

fn deepseek_model_supports_images(id: &str) -> bool {
    matches!(
        id,
        "deepseek-flash" | "deepseek-v4-flash" | "deepseek-v4-flash-vision-exp"
    )
}

fn normalize_deepseek_model(model: &mut CustomModel) {
    if let Some(name) = deepseek_model_display_name(&model.id) {
        model.name = name.to_string();
    }
    if deepseek_model_supports_images(&model.id) {
        model.input_images = true;
    }
}

fn normalize_deepseek_provider(mut provider: CustomProvider) -> CustomProvider {
    for model in &mut provider.models {
        normalize_deepseek_model(model);
    }
    provider
}

/// 解析 DeepSeek `GET /models` 的官方响应，只接受 DeepSeek 自己的模型 id。
pub fn parse_deepseek_models(body: &str) -> Result<Vec<CustomModel>, String> {
    let value: Value = serde_json::from_str(body)
        .map_err(|error| format!("DeepSeek 模型目录不是有效 JSON：{error}"))?;
    let data = value
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| "DeepSeek 模型目录缺少 data 数组".to_string())?;
    let mut models = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for item in data {
        let Some(id) = item.get("id").and_then(Value::as_str) else {
            continue;
        };
        if !id.starts_with("deepseek-") || !is_valid_model_id(id) || !seen.insert(id) {
            continue;
        }
        let reasoning =
            id.contains("reasoner") || id.contains("reasoning") || id.starts_with("deepseek-r");
        models.push(CustomModel {
            id: id.to_string(),
            name: deepseek_model_display_name(id)
                .unwrap_or(id)
                .to_string(),
            reasoning,
            input_images: deepseek_model_supports_images(id),
            context_window: Some(DEFAULT_CONTEXT_WINDOW),
            max_tokens: Some(DEFAULT_MAX_TOKENS),
            thinking_level_map: None,
            compat: None,
        });
    }
    if models.is_empty() {
        return Err("DeepSeek API 没有返回可用模型".to_string());
    }
    models.sort_by(|left, right| left.id.cmp(&right.id));
    Ok(models)
}

fn curl_config_escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace(['\r', '\n'], "")
}

fn fetch_deepseek_models(api_key: &str) -> Result<Vec<CustomModel>, CoreError> {
    let mut command = Command::new("/usr/bin/curl");
    command
        .args([
            "--silent",
            "--show-error",
            "--location",
            "--connect-timeout",
            "10",
            "--max-time",
            "30",
            "--config",
            "-",
            "-w",
            "\n%{http_code}",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (name, value) in crate::agent_proxy_env() {
        command.env(name, value);
    }
    let mut child = command.spawn().map_err(|error| {
        CoreError::local(
            "DEEPSEEK_UNAVAILABLE",
            format!("无法启动网络请求：{error}"),
            true,
        )
    })?;
    let config = format!(
        "url = \"{DEEPSEEK_BASE_URL}{DEEPSEEK_MODELS_PATH}\"\nheader = \"Authorization: Bearer {}\"\nheader = \"Accept: application/json\"\n",
        curl_config_escape(api_key)
    );
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(config.as_bytes()).map_err(|error| {
            CoreError::local(
                "DEEPSEEK_UNAVAILABLE",
                format!("发送 DeepSeek 请求失败：{error}"),
                true,
            )
        })?;
    }
    let output = child.wait_with_output().map_err(|error| {
        CoreError::local(
            "DEEPSEEK_UNAVAILABLE",
            format!("等待 DeepSeek 响应失败：{error}"),
            true,
        )
    })?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let Some((body, status)) = stdout.rsplit_once('\n') else {
        return Err(CoreError::local(
            "DEEPSEEK_UNAVAILABLE",
            "DeepSeek API 响应为空",
            true,
        ));
    };
    let status = status.trim().parse::<u16>().unwrap_or(0);
    if !(200..300).contains(&status) {
        let (code, message, retryable) = match status {
            401 | 403 => (
                "DEEPSEEK_AUTH_FAILED",
                "DeepSeek API Key 无效或无权限",
                false,
            ),
            429 => (
                "DEEPSEEK_RATE_LIMITED",
                "DeepSeek API 请求过于频繁，请稍后重试",
                true,
            ),
            500..=599 => (
                "DEEPSEEK_API_ERROR",
                "DeepSeek 服务暂时不可用，请稍后重试",
                true,
            ),
            _ => (
                "DEEPSEEK_API_ERROR",
                "DeepSeek API 请求失败，请检查网络与 API Key",
                true,
            ),
        };
        return Err(CoreError::local(
            code,
            format!("{message}（HTTP {status}）"),
            retryable,
        ));
    }
    parse_deepseek_models(body)
        .map_err(|message| CoreError::local("DEEPSEEK_API_ERROR", message, true))
}

fn render_only_deepseek_models_json(provider: Option<&CustomProvider>) -> Result<Value, CoreError> {
    let path = models_json_path()?;
    let existing = std::fs::read_to_string(&path).ok();
    let corrupt = existing
        .as_deref()
        .is_some_and(|contents| serde_json::from_str::<Value>(contents).is_err());
    if corrupt {
        if let Some(backup) = backup_corrupt(&path)? {
            let _ = backup;
        }
    }
    let mut providers = serde_json::Map::new();
    if let Some(provider) = provider {
        providers.insert(provider.provider_id.clone(), provider_entry(provider));
    }
    let rendered = json!({ "providers": providers });
    write_models_json(&path, &rendered)?;
    Ok(json!({
        "models_json_path": path.to_string_lossy(),
        "corrupt_backup": corrupt,
    }))
}

/// 启动时清除旧版 OpenCode/Codex 配置，只保留 DeepSeek 官方条目。
pub fn cleanup_legacy_model_config(app: &AppHandle) -> Result<(), CoreError> {
    let path = config_path(app)?;
    let mut config = load_app_config(&path);
    let provider = deepseek_provider_from_config(&config);
    let canonical = provider
        .as_ref()
        .map(|provider| serde_json::to_value(std::slice::from_ref(provider)))
        .transpose()
        .map_err(|error| CoreError::internal(error.to_string()))?
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let mut changed = false;
    if let Some(object) = config.as_object_mut() {
        if object.get("custom_providers") != Some(&canonical) {
            object.insert("custom_providers".to_string(), canonical);
            changed = true;
        }
        let desired_default = provider.as_ref().and_then(|provider| {
            object
                .get("default_model")
                .and_then(Value::as_str)
                .filter(|model| {
                    model
                        .strip_prefix("deepseek/")
                        .is_some_and(|id| provider.models.iter().any(|item| item.id == id))
                })
                .map(str::to_string)
                .or_else(|| {
                    provider
                        .models
                        .first()
                        .map(|model| format!("{DEEPSEEK_PROVIDER_ID}/{}", model.id))
                })
        });
        match desired_default {
            Some(default_model) => {
                if object.get("default_model").and_then(Value::as_str) != Some(&default_model) {
                    object.insert("default_model".to_string(), Value::String(default_model));
                    changed = true;
                }
            }
            None => {
                if object.remove("default_model").is_some() {
                    changed = true;
                }
            }
        }
    }
    if changed {
        save_app_config(&path, &config)?;
    }
    render_only_deepseek_models_json(provider.as_ref())?;
    Ok(())
}

#[tauri::command]
pub fn deepseek_status(app: AppHandle) -> Result<Value, CoreError> {
    let path = config_path(&app)?;
    let config = load_app_config(&path);
    let provider = deepseek_provider_from_config(&config);
    Ok(json!({
        "configured": provider.is_some(),
        "models": provider.as_ref().map(deepseek_model_infos).unwrap_or_default(),
        "models_json_path": models_json_path()?.to_string_lossy(),
    }))
}
pub(crate) fn configured_deepseek_model(app: &AppHandle) -> Result<Option<String>, CoreError> {
    let path = config_path(app)?;
    let config = load_app_config(&path);
    let Some(provider) = deepseek_provider_from_config(&config) else {
        return Ok(None);
    };
    let available = |model: &str| {
        model
            .strip_prefix("deepseek/")
            .is_some_and(|id| provider.models.iter().any(|item| item.id == id))
    };
    if let Some(model) = config
        .get("default_model")
        .and_then(Value::as_str)
        .filter(|model| available(model))
    {
        return Ok(Some(model.to_string()));
    }
    Ok(provider
        .models
        .first()
        .map(|model| format!("{DEEPSEEK_PROVIDER_ID}/{}", model.id)))
}

#[tauri::command]
pub fn deepseek_configure(app: AppHandle, api_key: String) -> Result<Value, CoreError> {
    let api_key = api_key.trim().to_string();
    if api_key.len() < 8 || api_key.len() > 512 {
        return Err(CoreError::local(
            "INVALID_REQUEST",
            "DeepSeek API Key 长度无效",
            false,
        ));
    }
    let models = fetch_deepseek_models(&api_key)?;
    let provider = deepseek_provider(api_key, models);
    validate_provider(&provider)
        .map_err(|message| CoreError::local("INVALID_REQUEST", message, false))?;
    let expected: Vec<String> = provider
        .models
        .iter()
        .map(|model| model.id.clone())
        .collect();
    validate_candidate(
        std::slice::from_ref(&provider),
        DEEPSEEK_PROVIDER_ID,
        &expected,
    )?;
    let path = config_path(&app)?;
    let mut config = load_app_config(&path);
    let old_default = config
        .get("default_model")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let default_model = if provider
        .models
        .iter()
        .any(|model| format!("{DEEPSEEK_PROVIDER_ID}/{}", model.id) == old_default)
    {
        old_default.to_string()
    } else {
        format!("{DEEPSEEK_PROVIDER_ID}/{}", provider.models[0].id)
    };
    if let Some(object) = config.as_object_mut() {
        object.insert(
            "custom_providers".to_string(),
            serde_json::to_value(std::slice::from_ref(&provider))
                .map_err(|error| CoreError::internal(error.to_string()))?,
        );
        object.insert(
            "default_model".to_string(),
            Value::String(default_model.clone()),
        );
    }
    save_app_config(&path, &config)?;
    let render = render_only_deepseek_models_json(Some(&provider))?;
    Ok(json!({
        "configured": true,
        "default_model": default_model,
        "models": deepseek_model_infos(&provider),
        "render": render,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn provider(id: &str, models: &[&str], enabled: bool) -> CustomProvider {
        CustomProvider {
            provider_id: id.to_string(),
            display_name: format!("显示 {id}"),
            base_url: "https://api.example.com/v1".to_string(),
            api: "openai-completions".to_string(),
            api_key: "sk-test".to_string(),
            auth_header: false,
            headers: BTreeMap::new(),
            enabled,
            models: models
                .iter()
                .map(|model| CustomModel {
                    id: model.to_string(),
                    name: String::new(),
                    reasoning: true,
                    input_images: false,
                    context_window: None,
                    max_tokens: None,
                    thinking_level_map: None,
                    compat: None,
                })
                .collect(),
            updated_at: String::new(),
        }
    }

    #[test]
    fn merge_preserves_foreign_providers_and_overwrites_managed() {
        let existing = r#"{"providers":{"deepseek":{"models":[]},"hand-made":{"apiKey":"x","baseUrl":"https://x","api":"openai-completions","models":[{"id":"m1","api":"openai-completions","baseUrl":"https://x"}]}}}"#;
        let (merged, corrupt) =
            merge_models_json(Some(existing), &[provider("hand-made", &["m2"], true)]);
        assert!(!corrupt);
        let map = merged.get("providers").unwrap().as_object().unwrap();
        assert!(map.contains_key("deepseek"));
        assert_eq!(
            map.get("hand-made")
                .unwrap()
                .get("apiKey")
                .and_then(Value::as_str),
            Some("sk-test")
        );
        assert_eq!(
            map.get("hand-made")
                .unwrap()
                .get("models")
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn merge_removes_disabled_and_tolerates_corrupt_existing() {
        let (merged, corrupt) =
            merge_models_json(Some("{not json"), &[provider("mine", &["a"], false)]);
        assert!(corrupt);
        let map = merged.get("providers").unwrap().as_object().unwrap();
        assert!(map.is_empty());
    }

    #[test]
    fn merge_from_missing_file_is_empty_object() {
        let (merged, corrupt) = merge_models_json(None, &[]);
        assert!(!corrupt);
        assert!(merged
            .get("providers")
            .unwrap()
            .as_object()
            .unwrap()
            .is_empty());
    }

    #[test]
    fn render_entry_carries_key_and_defaults() {
        let entry = provider_entry(&provider("mine", &["m1"], true));
        assert_eq!(entry.get("apiKey").and_then(Value::as_str), Some("sk-test"));
        let model = &entry.get("models").unwrap().as_array().unwrap()[0];
        assert_eq!(
            model.get("contextWindow").and_then(Value::as_u64),
            Some(128_000)
        );
        assert_eq!(
            model.get("api").and_then(Value::as_str),
            Some("openai-completions")
        );
        assert_eq!(model.get("input").unwrap().as_array().unwrap().len(), 1);
    }

    #[test]
    fn non_reasoning_model_gets_null_thinking_map() {
        let mut provider = provider("mine", &["m"], true);
        provider.models[0].reasoning = false;
        let map = provider_entry(&provider)
            .get("models")
            .unwrap()
            .as_array()
            .unwrap()[0]
            .get("thinkingLevelMap")
            .unwrap()
            .as_object()
            .unwrap()
            .clone();
        assert!(map.values().all(|value| value.is_null()));
        assert_eq!(map.len(), THINKING_LEVELS.len());
    }

    #[test]
    fn validation_requires_official_deepseek_configuration() {
        let mut provider = provider("deepseek", &["deepseek-chat"], true);
        assert!(validate_provider(&provider).is_err());
        provider.base_url = "https://api.deepseek.com".to_string();
        assert!(validate_provider(&provider).is_ok());
        provider.api = "anthropic-messages".to_string();
        assert!(validate_provider(&provider).is_err());
        provider.api = "openai-completions".to_string();
        provider.models.clear();
        assert!(validate_provider(&provider).is_err());
    }

    #[test]
    fn parse_list_models_skips_header_and_garbage() {
        let text = "provider      model            context  max-out  thinking  images\ndeepseek      deepseek-flash   1M       384K     yes       yes\nWarning: something\n";
        let models = parse_list_models(text);
        assert_eq!(models.len(), 1);
        assert_eq!(models[0].id, "deepseek/deepseek-flash");
        assert!(models[0].thinking);
        assert!(models[0].images);
    }

    #[test]
    fn provider_models_present_reports_missing() {
        let text =
            "provider  model  context  max-out  thinking  images\nmine  m1  1M  1K  yes  no\n";
        assert!(provider_models_present(text, "mine", &["m1".to_string()]).is_ok());
        let error = provider_models_present(text, "mine", &["m1".to_string(), "m2".to_string()])
            .unwrap_err();
        assert!(error.contains("mine/m2"));
    }

    #[test]
    fn atomic_write_sets_contents() {
        let dir = std::env::temp_dir().join(format!("wecom-provider-test-{}", std::process::id()));
        let path = dir.join("models.json");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        write_models_json(
            &path,
            &merge_models_json(None, &[provider("mine", &["m"], true)]).0,
        )
        .unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.contains("\"providers\""));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn parse_deepseek_models_filters_foreign_and_duplicates() {
        let body = r#"{"data":[{"id":"deepseek-chat"},{"id":"deepseek-flash"},{"id":"deepseek-reasoner"},{"id":"deepseek-chat"},{"id":"other-model"},{"id":"DeepSeek-invalid"}]}"#;
        let models = parse_deepseek_models(body).unwrap();
        assert_eq!(
            models
                .iter()
                .map(|model| model.id.as_str())
                .collect::<Vec<_>>(),
            vec!["deepseek-chat", "deepseek-flash", "deepseek-reasoner"]
        );
        assert!(!models[0].reasoning);
        assert!(!models[0].input_images);
        assert!(models[1].input_images);
        assert_eq!(models[1].name, "DeepSeek-V4.1-Flash");
        assert!(models[2].reasoning);
    }

    #[test]
    fn parse_deepseek_models_requires_available_data() {
        assert!(parse_deepseek_models(r#"{"data":[]}"#).is_err());
        assert!(parse_deepseek_models(r#"{"error":{"message":"bad key"}}"#).is_err());
    }

    #[test]
    fn deepseek_provider_uses_official_endpoint_and_model_info() {
        let provider = deepseek_provider(
            "sk-test-deepseek".to_string(),
            parse_deepseek_models(r#"{"data":[{"id":"deepseek-chat"}]}"#).unwrap(),
        );
        assert_eq!(provider.provider_id, "deepseek");
        assert_eq!(provider.base_url, "https://api.deepseek.com");
        assert_eq!(
            deepseek_model_infos(&provider)[0].id,
            "deepseek/deepseek-chat"
        );
    }
    #[test]
    fn configured_flash_model_is_normalized_for_vision() {
        let mut provider = provider("deepseek", &["deepseek-flash"], true);
        provider.base_url = "https://api.deepseek.com".to_string();
        provider.models[0].reasoning = false;
        provider.models[0].input_images = false;
        provider.models[0].name = "deepseek-flash".to_string();
        let normalized = normalize_deepseek_provider(provider);
        assert_eq!(normalized.models[0].name, "DeepSeek-V4.1-Flash");
        assert!(normalized.models[0].input_images);
        let info = &deepseek_model_infos(&normalized)[0];
        assert_eq!(info.name, "DeepSeek-V4.1-Flash");
        assert!(info.images);
    }

    #[test]
    fn config_rejects_non_deepseek_provider() {
        let mut provider = provider("deepseek", &["deepseek-chat"], true);
        provider.base_url = "https://api.deepseek.com".to_string();
        let config = json!({
            "custom_providers": [
                serde_json::to_value(&provider).unwrap(),
                { "provider_id": "opencode-go", "api_key": "subscription" }
            ]
        });
        assert!(deepseek_provider_from_config(&config).is_some());
        provider.api = "anthropic-messages".to_string();
        let invalid = json!({
            "custom_providers": [serde_json::to_value(provider).unwrap()]
        });
        assert!(deepseek_provider_from_config(&invalid).is_none());
    }
}
