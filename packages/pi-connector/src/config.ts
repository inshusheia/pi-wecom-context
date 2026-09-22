import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export interface ConnectorConfig {
  coreConfigPath: string;
  sidecarEntry: string;
  pythonPath: string;
  maxMessages: number;
}

const CONFIG_PATH = join(homedir(), ".pi", "agent", "extensions", "pi-wecom-context", "config.json");

/** sidecar 只认显式身份，会话 key 必须是 16 位十六进制。 */
export const SESSION_KEY_PATTERN = /^[0-9a-f]{16}$/;

/**
 * 读取 Pi TUI `/wecom-context use` 写入的默认会话。
 * sidecar 的 read_context 不回退全局会话，因此由 Connector 把它作为显式身份传入；
 * 该 key 仍必须在 session_bindings 中有绑定，否则 sidecar 返回 SESSION_NOT_ALLOWED。
 */
export function readCoreSelectedSessionKey(coreConfigPath: string): string | undefined {
  try {
    const parsed: unknown = JSON.parse(readFileSync(coreConfigPath, "utf8"));
    if (!parsed || typeof parsed !== "object") return undefined;
    const key = (parsed as Record<string, unknown>).selected_session_key;
    return typeof key === "string" && SESSION_KEY_PATTERN.test(key) ? key : undefined;
  } catch {
    return undefined;
  }
}

export function loadConnectorConfig(cwd: string): ConnectorConfig {
  let value: Record<string, unknown> = {};
  try {
    const parsed = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
    if (parsed && typeof parsed === "object") value = parsed as Record<string, unknown>;
  } catch {
    // Use safe defaults; the core sidecar will return a structured error if unavailable.
  }
  return {
    coreConfigPath: typeof value.coreConfigPath === "string"
      ? value.coreConfigPath
      : join(homedir(), "Library", "Application Support", "WeCom Context", "config.json"),
    sidecarEntry: typeof value.sidecarEntry === "string" ? value.sidecarEntry : join(cwd, "sidecar", "wecom_context_core", "protocol.py"),
    pythonPath: typeof value.pythonPath === "string" ? value.pythonPath : "python3",
    maxMessages: typeof value.maxMessages === "number" && Number.isInteger(value.maxMessages)
      ? Math.min(Math.max(value.maxMessages, 1), 100)
      : 30,
  };
}
