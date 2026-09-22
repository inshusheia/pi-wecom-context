use serde_json::{Map, Value};
use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

pub fn config_dir(_app: &AppHandle) -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME").ok_or_else(|| "无法定位用户 Home 目录".to_string())?;
    Ok(PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join("WeCom Context"))
}

const ALLOWED_ACTIONS: &[&str] = &[
    "status",
    "bootstrap",
    "discover_datasets",
    "select_dataset",
    "sessions",
    "select_session",
    "bind_session",
    "allow_session",
    "send_target",
    "send_message",
    "set_send_permission",
    "clear_session",
    "read_context",
    "prepare_context",
    "download_images",
    "refresh_snapshot",
    "capture_key",
    "remove_account",
    "remove_dataset",
    "restore_dataset",
];

const SIDECAR_REFRESH_TIMEOUT_SECONDS: u64 = 150;
const SIDECAR_CAPTURE_TIMEOUT_SECONDS: u64 = 300;
const SIDECAR_REMOVE_TIMEOUT_SECONDS: u64 = 180;

fn sidecar_timeout(action: &str) -> Duration {
    // bootstrap 带 auto_refresh=true，内部可能走完整快照刷新；
    // capture_key 要临时重启企业微信并做 90 秒扫描，给最长窗口。
    if matches!(action, "refresh_snapshot" | "bootstrap") {
        Duration::from_secs(SIDECAR_REFRESH_TIMEOUT_SECONDS)
    } else if action == "capture_key" {
        Duration::from_secs(SIDECAR_CAPTURE_TIMEOUT_SECONDS)
    } else if matches!(
        action,
        "remove_account" | "remove_dataset" | "restore_dataset"
    ) {
        // 删除企业账户要清掉该账户的快照目录（可能数百 MB）。
        Duration::from_secs(SIDECAR_REMOVE_TIMEOUT_SECONDS)
    } else {
        Duration::from_secs(SIDECAR_TIMEOUT_SECONDS)
    }
}

/// sidecar 失败的结构化错误。Tauri 命令统一返回这一种错误类型，
/// 前端按 `code` 分支、按 `retryable` 决定是否给出重试入口，不再解析拼接字符串。
#[derive(Debug, Clone, serde::Serialize)]
pub struct CoreError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Value>,
}

impl CoreError {
    pub fn new(code: impl Into<String>, message: impl Into<String>, retryable: bool) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            retryable,
            details: None,
        }
    }

    pub fn with_details(mut self, details: Option<Value>) -> Self {
        self.details = details;
        self
    }

    /// 本进程内的失败（spawn/超时/协议），与 sidecar 自己抛出的业务错误区分开。
    pub fn local(code: impl Into<String>, message: impl Into<String>, retryable: bool) -> Self {
        Self::new(code, message, retryable)
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self::new("INTERNAL_ERROR", message, false)
    }

    /// sidecar 响应 `ok:false` 时原样映射 `error.{code,message,retryable,details}`。
    fn from_response(response: &Value) -> Self {
        let Some(error) = response.get("error").and_then(Value::as_object) else {
            return Self::local(
                "SIDECAR_PROTOCOL_ERROR",
                "sidecar 返回了无法识别的错误响应",
                false,
            );
        };
        let code = error
            .get("code")
            .and_then(Value::as_str)
            .filter(|code| !code.is_empty())
            .unwrap_or("INTERNAL_ERROR");
        let message = error
            .get("message")
            .and_then(Value::as_str)
            .filter(|message| !message.is_empty())
            .unwrap_or("sidecar 操作失败");
        let retryable = error
            .get("retryable")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        Self::new(code, message, retryable).with_details(error.get("details").cloned())
    }
}

impl std::fmt::Display for CoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for CoreError {}

fn debug_log(message: impl AsRef<str>) {
    if std::env::var("WECOM_CONTEXT_DEBUG").ok().as_deref() == Some("1") {
        eprintln!("[wecom-context] {}", message.as_ref());
    }
}

fn sidecar_path(app: &AppHandle) -> Result<PathBuf, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| error.to_string())?;
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(parent) = current_exe.parent() {
            candidates.push(parent.join("wecom-context-core"));
        }
    }
    candidates.extend([
        resource_dir
            .join("resources")
            .join("wecom-context-core")
            .join("wecom-context-core"),
        resource_dir
            .join("wecom-context-core")
            .join("wecom-context-core"),
    ]);
    candidates
        .into_iter()
        .find(|candidate| candidate.is_file())
        .ok_or_else(|| "WeCom Context Core sidecar 不存在".to_string())
}

pub fn packaged_sidecar_path(app: &AppHandle) -> Result<PathBuf, String> {
    sidecar_path(app)
}

// 冷启动首次访问企微容器目录会被 sandboxd 授权中介拖慢（实测 50-60 秒），
// 因此默认超时留足余量，避免一启动就抛 SIDECAR_TIMEOUT。
const SIDECAR_TIMEOUT_SECONDS: u64 = 90;

pub fn invoke_core(app: &AppHandle, action: &str, payload: Value) -> Result<Value, CoreError> {
    if !ALLOWED_ACTIONS.contains(&action) {
        return Err(CoreError::local(
            "INVALID_REQUEST",
            "不允许的 sidecar action",
            false,
        ));
    }
    let config_dir = config_dir(app).map_err(CoreError::internal)?;
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| CoreError::internal(error.to_string()))?;
    std::fs::create_dir_all(&config_dir).map_err(|error| CoreError::internal(error.to_string()))?;
    let config_path = config_dir.join("config.json");
    let sidecar = sidecar_path(app)
        .map_err(|error| CoreError::local("SIDECAR_SPAWN_FAILED", error, false))?;
    debug_log(format!(
        "action={action} sidecar={} config={}",
        sidecar.display(),
        config_path.display()
    ));
    let mut request = Map::new();
    request.insert("protocol_version".into(), Value::String("1".into()));
    request.insert(
        "request_id".into(),
        Value::String(format!(
            "tauri-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        )),
    );
    request.insert("action".into(), Value::String(action.into()));
    if let Value::Object(fields) = payload {
        for (key, value) in fields {
            request.insert(key, value);
        }
    }
    let input = serde_json::to_vec(&Value::Object(request))
        .map_err(|error| CoreError::internal(error.to_string()))?;

    let mut child = Command::new(&sidecar)
        .arg("--config")
        .arg(&config_path)
        .env("WECOM_CONTEXT_APP_DIR", &config_dir)
        .env("WECOM_CONTEXT_RESOURCE_DIR", &resource_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| {
            CoreError::local(
                "SIDECAR_SPAWN_FAILED",
                format!("sidecar 启动失败: {error}"),
                false,
            )
        })?;
    debug_log(format!("spawned pid={}", child.id()));
    input
        .into_iter()
        .chain(std::iter::once(b'\n'))
        .try_for_each(|byte| {
            child
                .stdin
                .as_mut()
                .expect("stdin configured")
                .write_all(&[byte])
        })
        .map_err(|error| {
            CoreError::local(
                "SIDECAR_SPAWN_FAILED",
                format!("sidecar stdin 写入失败: {error}"),
                false,
            )
        })?;
    drop(child.stdin.take());
    debug_log("stdin closed; waiting for sidecar");
    let stdout_pipe = child
        .stdout
        .take()
        .ok_or_else(|| CoreError::internal("sidecar stdout 未配置"))?;
    let stderr_pipe = child
        .stderr
        .take()
        .ok_or_else(|| CoreError::internal("sidecar stderr 未配置"))?;
    let stdout_reader = thread::spawn(move || {
        let mut output = Vec::new();
        let _ = std::io::BufReader::new(stdout_pipe).read_to_end(&mut output);
        output
    });
    let stderr_reader = thread::spawn(move || {
        let mut output = Vec::new();
        let _ = std::io::BufReader::new(stderr_pipe).read_to_end(&mut output);
        output
    });
    let deadline = Instant::now() + sidecar_timeout(action);
    let status = loop {
        match child
            .try_wait()
            .map_err(|error| CoreError::internal(error.to_string()))?
        {
            Some(status) => break status,
            None => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = stdout_reader.join();
                    let _ = stderr_reader.join();
                    debug_log("sidecar timed out; killed");
                    return Err(CoreError::local(
                        "SIDECAR_TIMEOUT",
                        "sidecar 响应超时（系统文件访问可能被占用）",
                        true,
                    ));
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        }
    };
    let stdout = stdout_reader
        .join()
        .map_err(|_| CoreError::internal("sidecar stdout 读取线程异常"))?;
    let stderr = stderr_reader
        .join()
        .map_err(|_| CoreError::internal("sidecar stderr 读取线程异常"))?;
    debug_log(format!(
        "sidecar exited status={} stdout={} stderr={}",
        status,
        stdout.len(),
        stderr.len()
    ));
    if !status.success() {
        if std::env::var("WECOM_CONTEXT_DEBUG").ok().as_deref() == Some("1") {
            eprintln!(
                "[wecom-context] stderr={}",
                String::from_utf8_lossy(&stderr)
            );
        }
        return Err(CoreError::local(
            "SIDECAR_PROTOCOL_ERROR",
            format!(
                "sidecar 异常退出（{}）",
                status
                    .code()
                    .map(|code| format!("exit {code}"))
                    .unwrap_or_else(|| "signal".to_string())
            ),
            false,
        ));
    }
    let parsed = parse_sidecar_output(&stdout);
    match &parsed {
        Ok(_) => debug_log("returning successful data"),
        Err(error) => debug_log(format!("returning sidecar error code={}", error.code)),
    }
    parsed
}

fn parse_sidecar_output(stdout: &[u8]) -> Result<Value, CoreError> {
    let line = String::from_utf8_lossy(stdout)
        .lines()
        .last()
        .unwrap_or_default()
        .to_string();
    let response: Value = serde_json::from_str(&line)
        .map_err(|_| CoreError::local("SIDECAR_PROTOCOL_ERROR", "sidecar 返回格式无效", false))?;
    if response.get("protocol_version").and_then(Value::as_str) != Some("1") {
        return Err(CoreError::local(
            "SIDECAR_PROTOCOL_ERROR",
            "sidecar 协议版本不符",
            false,
        ));
    }
    if response.get("ok") == Some(&Value::Bool(true)) {
        return Ok(response.get("data").cloned().unwrap_or(Value::Null));
    }
    Err(CoreError::from_response(&response))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn error_response(error: Value) -> Value {
        json!({ "protocol_version": "1", "request_id": "t", "ok": false, "error": error })
    }

    #[test]
    fn sidecar_success_returns_data_payload() {
        let data = parse_sidecar_output(
            br#"{"protocol_version":"1","request_id":"t","ok":true,"data":{"count":2}}"#,
        )
        .expect("success response");
        assert_eq!(data, json!({ "count": 2 }));
        // 成功响应没有 data 时返回 null，而不是把信封当数据交给前端。
        let empty = parse_sidecar_output(br#"{"protocol_version":"1","request_id":"t","ok":true}"#)
            .expect("success response");
        assert_eq!(empty, Value::Null);
    }

    #[test]
    fn prepare_context_action_is_allowed_for_source_injection() {
        assert!(ALLOWED_ACTIONS.contains(&"prepare_context"));
    }

    #[test]
    fn sidecar_failure_response_maps_structured_error() {
        let error = parse_sidecar_output(
            r#"{"protocol_version":"1","request_id":"t","ok":false,"error":{"code":"SESSION_GONE","message":"会话已不存在","retryable":false,"details":{"history_candidates":[{"snapshot_id":"s1"}]}}}"#.as_bytes(),
        )
        .expect_err("failure response");
        assert_eq!(error.code, "SESSION_GONE");
        assert_eq!(error.message, "会话已不存在");
        assert!(!error.retryable);
        assert_eq!(
            error
                .details
                .as_ref()
                .and_then(|details| details.get("history_candidates"))
                .and_then(|candidates| candidates.get(0))
                .and_then(|candidate| candidate.get("snapshot_id"))
                .and_then(Value::as_str),
            Some("s1")
        );
    }

    #[test]
    fn sidecar_garbage_or_wrong_protocol_is_reported_as_protocol_error() {
        for stdout in [
            &b"not json"[..],
            &b""[..],
            br#"{"protocol_version":"2","request_id":"t","ok":true,"data":{}}"#,
        ] {
            let error = parse_sidecar_output(stdout).expect_err("invalid output");
            assert_eq!(error.code, "SIDECAR_PROTOCOL_ERROR");
            assert!(!error.retryable);
        }
    }

    #[test]
    fn core_error_maps_sidecar_error_fields() {
        let error = CoreError::from_response(&error_response(json!({
            "code": "SNAPSHOT_UNAVAILABLE",
            "message": "快照不可用",
            "retryable": true,
            "details": { "history_candidates": [ { "snapshot_id": "abc" } ] }
        })));
        assert_eq!(error.code, "SNAPSHOT_UNAVAILABLE");
        assert_eq!(error.message, "快照不可用");
        assert!(error.retryable);
        assert_eq!(
            error
                .details
                .as_ref()
                .and_then(|details| details.get("history_candidates"))
                .is_some(),
            true
        );
    }

    #[test]
    fn core_error_serialization_omits_absent_details() {
        let with_details = CoreError::new("SESSION_GONE", "会话已不存在", false)
            .with_details(Some(json!({ "history_candidates": [] })));
        let serialized = serde_json::to_value(&with_details).expect("serialize");
        assert_eq!(
            serialized,
            json!({
                "code": "SESSION_GONE",
                "message": "会话已不存在",
                "retryable": false,
                "details": { "history_candidates": [] }
            })
        );

        let without_details = CoreError::new("SESSION_GONE", "会话已不存在", false);
        let serialized = serde_json::to_value(&without_details).expect("serialize");
        assert_eq!(serialized.get("details"), None);
        assert_eq!(
            serialized,
            json!({ "code": "SESSION_GONE", "message": "会话已不存在", "retryable": false })
        );
        assert_eq!(without_details.to_string(), "SESSION_GONE: 会话已不存在");
    }

    #[test]
    fn core_error_defaults_when_sidecar_omits_fields() {
        let error = CoreError::from_response(&error_response(json!({ "code": "REFRESH_FAILED" })));
        assert_eq!(error.code, "REFRESH_FAILED");
        assert_eq!(error.message, "sidecar 操作失败");
        assert!(!error.retryable);
        assert!(error.details.is_none());

        // 没有 error 对象的 ok:false 只能归类为协议错误，不能伪装成业务错误。
        let protocol = CoreError::from_response(&json!({ "protocol_version": "1", "ok": false }));
        assert_eq!(protocol.code, "SIDECAR_PROTOCOL_ERROR");
        assert!(!protocol.retryable);
    }

    #[test]
    fn sidecar_timeout_covers_snapshot_work() {
        assert_eq!(sidecar_timeout("bootstrap"), Duration::from_secs(150));
        assert_eq!(
            sidecar_timeout("refresh_snapshot"),
            Duration::from_secs(150)
        );
        // 冷启动容器首访可停顿 50-60 秒，默认窗口必须覆盖。
        assert_eq!(sidecar_timeout("status"), Duration::from_secs(90));
        assert_eq!(
            sidecar_timeout("discover_datasets"),
            Duration::from_secs(90)
        );
        assert_eq!(sidecar_timeout("capture_key"), Duration::from_secs(300));
        assert_eq!(sidecar_timeout("remove_account"), Duration::from_secs(180));
        assert_eq!(sidecar_timeout("remove_dataset"), Duration::from_secs(180));
    }
}
