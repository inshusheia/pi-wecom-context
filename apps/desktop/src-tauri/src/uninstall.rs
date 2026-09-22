use serde_json::json;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;
use tauri::AppHandle;

const CONNECTOR_MARKER: &str = ".managed-by-wecom-context";

/// 返回本应用拥有的本地路径；绝不包含企业微信原始数据库目录。
pub fn managed_paths(home: &Path, app_bundle: Option<&Path>) -> Vec<PathBuf> {
    let mut paths = vec![
        home.join("Library/Application Support/WeCom Context"),
        home.join("Library/Application Support/wecom-local-vault"),
        home.join(".config/wecom-local-vault.json"),
        home.join("Downloads/WeCom Context"),
        home.join("Library/Caches/com.yangsiyu.wecom-context"),
        home.join("Library/Preferences/com.yangsiyu.wecom-context.plist"),
        home.join("Library/Saved Application State/com.yangsiyu.wecom-context.savedState"),
        home.join("Library/Logs/WeCom Context"),
        home.join("Library/WebKit/com.yangsiyu.wecom-context"),
    ];
    if let Some(bundle) = app_bundle {
        paths.push(bundle.to_path_buf());
    }
    paths
}

pub fn app_bundle_from_executable(executable: &Path) -> Option<PathBuf> {
    executable
        .ancestors()
        .find(|path| path.extension().and_then(|value| value.to_str()) == Some("app"))
        .map(Path::to_path_buf)
}

fn shell_quote(path: &Path) -> String {
    format!("'{}'", path.to_string_lossy().replace('\'', "'\\''"))
}

/// 从 Pi 的共享模型配置中移除本应用写入的 DeepSeek provider，保留其他 provider。
fn remove_managed_model_config(home: &Path) -> Result<(), String> {
    let path = home.join(".pi/agent/models.json");
    let Ok(contents) = fs::read_to_string(&path) else {
        return Ok(());
    };
    let Ok(mut root) = serde_json::from_str::<serde_json::Value>(&contents) else {
        return Ok(());
    };
    let Some(providers) = root.get_mut("providers").and_then(serde_json::Value::as_object_mut) else {
        return Ok(());
    };
    providers.remove("deepseek");
    if providers.is_empty() {
        fs::remove_file(&path).map_err(|error| format!("无法清理 Pi 模型配置: {error}"))?;
    } else {
        let body = serde_json::to_vec_pretty(&root)
            .map_err(|error| format!("无法序列化 Pi 模型配置: {error}"))?;
        fs::write(&path, body).map_err(|error| format!("无法保存 Pi 模型配置: {error}"))?;
    }
    Ok(())
}


fn cleanup_script(paths: &[PathBuf], connector: &Path, script_path: &Path) -> String {
    let mut script = String::from("#!/bin/sh\n/bin/sleep 1\n");
    script.push_str(&format!(
        "if [ -f {} ]; then /bin/rm -rf {}; fi\n",
        shell_quote(&connector.join(CONNECTOR_MARKER)),
        shell_quote(connector),
    ));
    for path in paths {
        script.push_str(&format!("/bin/rm -rf {}\n", shell_quote(path)));
    }
    script.push_str(&format!("/bin/rm -f {}\n", shell_quote(script_path)));
    script
}

/// 启动短暂的自删除助手：先退出当前 App，再删除 App、配置、密钥、快照和托管扩展。
pub fn schedule(app: &AppHandle) -> Result<serde_json::Value, String> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "无法定位用户 Home 目录".to_string())?;
    remove_managed_model_config(&home)?;
    let executable = std::env::current_exe().map_err(|error| format!("无法定位当前应用: {error}"))?;
    let app_bundle = app_bundle_from_executable(&executable)
        .ok_or_else(|| "无法定位 WeCom Context.app".to_string())?;
    let connector = home.join(".pi/agent/extensions/pi-wecom-context");
    let paths = managed_paths(&home, Some(&app_bundle));
    let script_path = std::env::temp_dir().join(format!(
        "wecom-context-uninstall-{}.sh",
        std::process::id()
    ));
    fs::write(
        &script_path,
        cleanup_script(&paths, &connector, &script_path),
    )
    .map_err(|error| format!("无法创建卸载助手: {error}"))?;
    let mut permissions = fs::metadata(&script_path)
        .map_err(|error| format!("无法读取卸载助手: {error}"))?
        .permissions();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        permissions.set_mode(0o700);
        fs::set_permissions(&script_path, permissions)
            .map_err(|error| format!("无法设置卸载助手权限: {error}"))?;
    }

    Command::new("/usr/bin/nohup")
        .arg("/bin/sh")
        .arg(&script_path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("无法启动卸载助手: {error}"))?;
    std::thread::sleep(Duration::from_millis(100));
    app.exit(0);
    Ok(json!({ "started": true }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundle_path_is_resolved_from_nested_executable() {
        let executable = Path::new("/Applications/WeCom Context.app/Contents/MacOS/wecom-context-desktop");
        assert_eq!(
            app_bundle_from_executable(executable),
            Some(PathBuf::from("/Applications/WeCom Context.app"))
        );
    }

    #[test]
    fn cleanup_scope_contains_app_owned_data_but_not_wecom_source_data() {
        let home = Path::new("/Users/test");
        let paths = managed_paths(
            home,
            Some(Path::new("/Applications/WeCom Context.app")),
        );
        assert!(paths.contains(&home.join("Library/Application Support/WeCom Context")));
        assert!(paths.contains(&home.join("Library/Application Support/wecom-local-vault")));
        assert!(paths.contains(&home.join(".config/wecom-local-vault.json")));
        assert!(paths.contains(&PathBuf::from("/Applications/WeCom Context.app")));
        assert!(!paths.iter().any(|path| path.to_string_lossy().contains("com.tencent.WeWorkMac")));
        assert!(!paths.iter().any(|path| path.to_string_lossy().contains("Group Containers")));
    }

    #[test]
    fn connector_cleanup_requires_management_marker() {
        let home = Path::new("/Users/test");
        let connector = home.join(".pi/agent/extensions/pi-wecom-context");
        let script = cleanup_script(
            &managed_paths(home, None),
            &connector,
            Path::new("/tmp/uninstall.sh"),
        );
        assert!(script.contains(".managed-by-wecom-context"));
        assert!(script.contains("/bin/rm -rf"));
    }

    #[test]
    fn cleanup_script_removes_owned_files_and_preserves_wecom_source_data() {
        let root = std::env::temp_dir().join(format!("wecom-uninstall-test-{}", std::process::id()));
        let home = root.join("home");
        let app_bundle = root.join("WeCom Context.app");
        let connector = home.join(".pi/agent/extensions/pi-wecom-context");
        let source = home.join("Library/Containers/com.tencent.WeWorkMac");
        let paths = managed_paths(&home, Some(&app_bundle));
        for path in &paths {
            if path.extension().is_some() {
                fs::create_dir_all(path.parent().expect("path parent")).expect("parent");
                fs::write(path, b"owned").expect("owned file");
            } else {
                fs::create_dir_all(path).expect("owned directory");
            }
        }
        fs::create_dir_all(&connector).expect("connector");
        fs::write(connector.join(CONNECTOR_MARKER), b"1\n").expect("marker");
        fs::create_dir_all(&source).expect("source");
        fs::write(source.join("message.db"), b"must stay").expect("source file");
        let script_path = root.join("uninstall.sh");
        fs::create_dir_all(&root).expect("root");
        fs::write(
            &script_path,
            cleanup_script(&paths, &connector, &script_path),
        )
        .expect("script");
        let status = Command::new("/bin/sh")
            .arg(&script_path)
            .status()
            .expect("run script");
        assert!(status.success());
        assert!(paths.iter().all(|path| !path.exists()));
        assert!(!connector.exists());
        assert!(source.join("message.db").is_file());
        let _ = fs::remove_dir_all(root);
    }
}
