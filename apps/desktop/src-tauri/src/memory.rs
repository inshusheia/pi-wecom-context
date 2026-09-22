//! Agent 记忆的落盘与清空：注入记录、分页、有效性判定、清空序列。
//!
//! 事实源分工：
//! - 对话与压缩摘要的事实源是 **Pi 的持久会话文件**（`session/` 目录），本模块只负责删除。
//! - 「本轮注入了什么」是应用自己的事实，落在 `injections.jsonl`。
//! - 记忆代次 `memory.json` 由 `main_agent` 维护；清空前先写 `unfinished_clear_epoch`，
//!   崩溃后下次启动据此继续完成清空，绝不把没清干净的会话当成新记忆。

use std::io::Write;
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const INJECTIONS_FILE: &str = "injections.jsonl";
const MAX_QUESTION_CHARS: usize = 500;

/// 一条注入记录：本轮把哪些来源的多少内容交给了模型。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct InjectionRecord {
    pub request_id: String,
    pub at: String,
    #[serde(default)]
    pub question: String,
    #[serde(default)]
    pub package_id: Option<String>,
    #[serde(default)]
    pub sources: Vec<InjectionSource>,
    #[serde(default)]
    pub retained: Retained,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct InjectionSource {
    pub source_id: String,
    pub dataset_id: String,
    pub session_key: String,
    pub display_name: String,
    pub kind: String,
    pub snapshot_id: String,
    pub snapshot_created_at: String,
    pub message_count: usize,
    pub image_count: usize,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct Retained {
    pub message_count: usize,
    pub image_count: usize,
}

/// 注入内容在当前有效上下文中的处境。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Effectiveness {
    /// 仍能在当前有效上下文里找到（按资料包标记核对）
    Active,
    /// 已在标记之后发生过压缩：原文可能只剩摘要
    Summarized,
    /// 既找不到、也没有可解释的压缩：已移出上下文
    Evicted,
    /// 无法判定：如实显示，不假定仍被记住
    Unknown,
}

impl Effectiveness {
    pub fn as_str(self) -> &'static str {
        match self {
            Effectiveness::Active => "active",
            Effectiveness::Summarized => "summarized",
            Effectiveness::Evicted => "evicted",
            Effectiveness::Unknown => "unknown",
        }
    }
}

/// 注入文本里的资料包标记：让「仍在上下文中」可被正面证明，而不是靠猜。
pub fn injection_marker(identifier: &str) -> String {
    format!("【本轮资料 {identifier}】")
}

pub fn append_injection(dir: &Path, record: &InjectionRecord) -> std::io::Result<()> {
    let mut payload = record.clone();
    payload.question = payload.question.chars().take(MAX_QUESTION_CHARS).collect();
    let mut line = serde_json::to_string(&payload)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    line.push('\n');
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join(INJECTIONS_FILE))?
        .write_all(line.as_bytes())
}

/// 读取注入记录（按写入顺序），坏行跳过而不是让整个记忆视图失效。
pub fn load_injections(dir: &Path) -> Vec<InjectionRecord> {
    let Ok(contents) = std::fs::read_to_string(dir.join(INJECTIONS_FILE)) else {
        return Vec::new();
    };
    contents
        .lines()
        .filter_map(|line| serde_json::from_str::<InjectionRecord>(line.trim()).ok())
        .collect()
}

/// 分页：返回 (当页条目, 是否还有更多)。offset 超出范围时返回空页。
pub fn paginate<T: Clone>(items: &[T], offset: usize, limit: usize) -> (Vec<T>, bool) {
    let limit = limit.max(1);
    if offset >= items.len() {
        return (Vec::new(), false);
    }
    let end = (offset + limit).min(items.len());
    (items[offset..end].to_vec(), end < items.len())
}

/// 判定一条注入的处境：
/// - 标记仍出现在当前有效上下文 → active
/// - 没有出现，但在注入之后发生过压缩 → summarized（可能只剩摘要）
/// - 没有出现也没有压缩 → evicted
/// 时间缺失（无法比较）时如实返回 unknown。
pub fn classify(
    found_in_context: bool,
    injection_at_ms: Option<u64>,
    last_compaction_ms: Option<u64>,
) -> Effectiveness {
    if found_in_context {
        return Effectiveness::Active;
    }
    match (injection_at_ms, last_compaction_ms) {
        (_, Some(_)) if injection_at_ms.is_none() => Effectiveness::Unknown,
        (Some(at), Some(compacted)) if compacted > at => Effectiveness::Summarized,
        (Some(_), _) => Effectiveness::Evicted,
        (None, _) => Effectiveness::Unknown,
    }
}

/// 把 ISO-8601 时间（如 `2026-09-16T03:36:00.367Z`）转成毫秒时间戳。
/// Pi 的会话条目用这种格式；解析失败返回 None，而不是编造时间。
pub fn iso_to_millis(value: &str) -> Option<u64> {
    let bytes = value.as_bytes();
    let number = |start: usize, len: usize| -> Option<i64> {
        let slice = value.get(start..start + len)?;
        slice.parse::<i64>().ok()
    };
    if bytes.len() < 19 || bytes[4] != b'-' || bytes[7] != b'-' || bytes[10] != b'T' {
        return None;
    }
    let (year, month, day) = (number(0, 4)?, number(5, 2)?, number(8, 2)?);
    let (hour, minute, second) = (number(11, 2)?, number(14, 2)?, number(17, 2)?);
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 60
    {
        return None;
    }
    // 小数秒：按实际位数补齐到毫秒（".3" 是 300ms，不是 3ms）。
    let millis = if bytes.get(19) == Some(&b'.') {
        let digits: String = value
            .get(20..)
            .unwrap_or_default()
            .chars()
            .take_while(|c| c.is_ascii_digit())
            .take(3)
            .collect();
        if digits.is_empty() {
            0
        } else {
            let parsed = digits.parse::<u64>().unwrap_or(0);
            parsed * 10u64.pow((3 - digits.len()) as u32)
        }
    } else {
        0
    };
    // civil_from_days（Howard Hinnant）：不引依赖也能算准日期。
    let adjusted_year = if month <= 2 { year - 1 } else { year };
    let era = if adjusted_year >= 0 {
        adjusted_year
    } else {
        adjusted_year - 399
    } / 400;
    let year_of_era = adjusted_year - era * 400;
    let day_of_year = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146_097 + day_of_era - 719_468;
    let seconds = days * 86_400 + hour * 3600 + minute * 60 + second;
    if seconds < 0 {
        return None;
    }
    Some(seconds as u64 * 1000 + millis)
}

#[derive(Debug, Default, PartialEq)]
pub struct ClearOutcome {
    pub messages: usize,
    pub injections: usize,
    pub packages: usize,
    pub images: usize,
    pub session: bool,
}

/// 清空 Agent 记忆：会话文件、注入记录、资料包与其中的图片全部删除，并返回实际删除量。
///
/// 调用方必须**先**把 `unfinished_clear_epoch` 落盘再调用本函数，成功后清除该标记并 +1 代次。
/// 这样任何一步崩溃都不会让旧记忆被当成新会话继续使用。
pub fn clear_memory_files(
    agent_dir: &Path,
    session_dir: &Path,
    packages_root: Option<&Path>,
) -> std::io::Result<ClearOutcome> {
    let mut outcome = ClearOutcome::default();

    // 1) 会话文件：先把可统计的部分算出来，再整体删除。
    if session_dir.is_dir() {
        outcome.session = true;
        if let Ok(entries) = std::fs::read_dir(session_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_file() && path.extension().and_then(|ext| ext.to_str()) == Some("jsonl")
                {
                    outcome.messages += count_message_entries(&path);
                }
            }
        }
        std::fs::remove_dir_all(session_dir)?;
    }

    // 2) 注入记录
    let injections_path = agent_dir.join(INJECTIONS_FILE);
    if injections_path.is_file() {
        outcome.injections = load_injections(agent_dir).len();
        std::fs::remove_file(&injections_path)?;
    }

    // 3) 资料包目录（Agent 专属图片缓存就在这些包里）
    if let Some(root) = packages_root {
        if root.is_dir() {
            if let Ok(entries) = std::fs::read_dir(root) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    if !path.is_dir() {
                        continue;
                    }
                    outcome.packages += 1;
                    outcome.images += count_images(&path);
                    std::fs::remove_dir_all(&path)?;
                }
            }
        }
    }
    Ok(outcome)
}

fn count_message_entries(path: &Path) -> usize {
    let Ok(contents) = std::fs::read_to_string(path) else {
        return 0;
    };
    contents
        .lines()
        .filter(|line| {
            serde_json::from_str::<Value>(line.trim())
                .ok()
                .and_then(|value| {
                    value
                        .get("type")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .as_deref()
                == Some("message")
        })
        .count()
}

fn count_images(package_dir: &Path) -> usize {
    let Ok(entries) = std::fs::read_dir(package_dir) else {
        return 0;
    };
    entries
        .flatten()
        .filter(|entry| {
            let path = entry.path();
            let name = path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or_default();
            path.is_file() && name.starts_with("img_")
        })
        .count()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("wecom-memory-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("temp dir");
        dir
    }

    fn record(request_id: &str, package_id: &str) -> InjectionRecord {
        InjectionRecord {
            request_id: request_id.to_string(),
            at: "2026-09-16T12:00:00+08:00".to_string(),
            question: "问题".repeat(400),
            package_id: Some(package_id.to_string()),
            sources: vec![InjectionSource {
                source_id: "07e6cb301334:a1b2c3d4e5f60718".to_string(),
                dataset_id: "07e6cb301334".to_string(),
                session_key: "a1b2c3d4e5f60718".to_string(),
                display_name: "工资条".to_string(),
                kind: "单聊".to_string(),
                snapshot_id: "snap-1".to_string(),
                snapshot_created_at: "2026-09-16T11:04:01+08:00".to_string(),
                message_count: 30,
                image_count: 2,
            }],
            retained: Retained {
                message_count: 30,
                image_count: 2,
            },
        }
    }

    #[test]
    fn injections_roundtrip_and_skip_corrupt_lines() {
        let dir = temp_dir("injections");
        append_injection(&dir, &record("req-1", "pkg-1")).expect("append");
        append_injection(&dir, &record("req-2", "pkg-2")).expect("append");
        std::fs::OpenOptions::new()
            .append(true)
            .open(dir.join(INJECTIONS_FILE))
            .expect("open")
            .write_all(b"not json\n")
            .expect("write corrupt line");
        let loaded = load_injections(&dir);
        assert_eq!(loaded.len(), 2);
        assert_eq!(loaded[0].request_id, "req-1");
        assert_eq!(loaded[1].package_id.as_deref(), Some("pkg-2"));
        // 问题文本被截断，避免记忆视图被超长问题撑爆。
        assert_eq!(loaded[0].question.chars().count(), MAX_QUESTION_CHARS);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn paginate_reports_has_more_and_clamps() {
        let items: Vec<u32> = (0..5).collect();
        assert_eq!(paginate(&items, 0, 2), (vec![0, 1], true));
        assert_eq!(paginate(&items, 4, 2), (vec![4], false));
        assert_eq!(paginate(&items, 5, 2), (Vec::<u32>::new(), false));
        assert_eq!(paginate(&items, 0, 0), (vec![0], true), "limit 至少为 1");
    }

    #[test]
    fn classify_prefers_proof_over_guess() {
        assert_eq!(classify(true, Some(10), Some(20)), Effectiveness::Active);
        assert_eq!(
            classify(false, Some(10), Some(20)),
            Effectiveness::Summarized
        );
        assert_eq!(classify(false, Some(20), Some(10)), Effectiveness::Evicted);
        assert_eq!(classify(false, Some(10), None), Effectiveness::Evicted);
        assert_eq!(classify(false, None, Some(20)), Effectiveness::Unknown);
        assert_eq!(classify(false, None, None), Effectiveness::Unknown);
    }

    #[test]
    fn clear_removes_session_injections_and_packages() {
        let agent = temp_dir("clear-agent");
        let session = agent.join("session");
        std::fs::create_dir_all(&session).expect("session dir");
        std::fs::write(
            session.join("2026-09-16T03-36-00-367Z_wecom-main.jsonl"),
            b"{\"type\":\"message\",\"message\":{\"role\":\"user\"}}\n{\"type\":\"other\"}\n",
        )
        .expect("session file");
        append_injection(&agent, &record("req-1", "pkg-1")).expect("append");
        let packages = temp_dir("clear-packages");
        let pkg = packages.join("pkg-1");
        std::fs::create_dir_all(&pkg).expect("package dir");
        std::fs::write(pkg.join("img_0001.jpg"), b"x").expect("image");
        std::fs::write(pkg.join("package.json"), b"{}").expect("manifest");

        let outcome = clear_memory_files(&agent, &session, Some(&packages)).expect("clear");
        assert!(outcome.session);
        assert_eq!(outcome.messages, 1);
        assert_eq!(outcome.injections, 1);
        assert_eq!(outcome.packages, 1);
        assert_eq!(outcome.images, 1);
        assert!(!session.exists());
        assert!(!agent.join(INJECTIONS_FILE).exists());
        assert_eq!(std::fs::read_dir(&packages).expect("list").count(), 0);

        // 幂等：再清一次不报错，也不产生虚假计数。
        let again = clear_memory_files(&agent, &session, Some(&packages)).expect("clear again");
        assert_eq!(again, ClearOutcome::default());
        let _ = std::fs::remove_dir_all(&agent);
        let _ = std::fs::remove_dir_all(&packages);
    }

    #[test]
    fn iso_timestamps_parse_without_dependencies() {
        assert_eq!(iso_to_millis("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(iso_to_millis("1970-01-02T00:00:00Z"), Some(86_400_000));
        assert_eq!(
            iso_to_millis("2026-09-16T03:36:00.367Z"),
            Some(1_789_529_760_367)
        );
        assert_eq!(
            iso_to_millis("2026-09-16T03:36:00.3Z"),
            Some(1_789_529_760_300)
        );
        // 闰年 2 月 29 日必须算对
        assert_eq!(
            iso_to_millis("2024-02-29T00:00:00Z"),
            Some(1_709_164_800_000)
        );
        assert_eq!(iso_to_millis("not-a-time"), None);
        assert_eq!(iso_to_millis("2026-13-01T00:00:00Z"), None);
    }

    #[test]
    fn marker_is_stable_and_greppable() {
        assert_eq!(injection_marker("pkg-1"), "【本轮资料 pkg-1】");
    }
}
