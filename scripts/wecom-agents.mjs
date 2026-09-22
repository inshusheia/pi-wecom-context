#!/usr/bin/env node
/**
 * wecom-agents — 为企业微信会话启动独立的 Pi Agent，每个会话一份完整上下文。
 *
 * 用法:
 *   node scripts/wecom-agents.mjs list                     列出快照内最近会话（含 session_key）
 *   node scripts/wecom-agents.mjs allow <session_key...>   把会话加入允许列表（不改全局选中）
 *   node scripts/wecom-agents.mjs launch <session_key...>  为每个会话开一个 iTerm2 窗口跑独立 Pi
 *   node scripts/wecom-agents.mjs launch --all [N]         为最近 N（默认 5）个会话各开一个 agent
 *
 * 依赖: 已安装 WeCom Context.app（提供 onedir sidecar），或仓库内源码 sidecar。
 */
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

const ROOT = join(import.meta.dirname, "..");

function coreCandidates() {
  const list = [];
  if (process.platform === "darwin") {
    list.push("/Applications/WeCom Context.app/Contents/Resources/resources/wecom-context-core/wecom-context-core");
  }
  list.push(join(ROOT, "sidecar", "dist", "wecom-context-core", "wecom-context-core"));
  return list;
}

function callCore(action, payload = {}) {
  const configPath = join(homedir(), "Library", "Application Support", "WeCom Context", "config.json");
  const request = JSON.stringify({ protocol_version: "1", request_id: `agent-${Date.now()}`, action, ...payload }) + "\n";
  for (const core of coreCandidates()) {
    const result = spawnSync(core, ["--config", configPath], {
      input: request,
      encoding: "utf8",
      timeout: 30_000,
      env: { ...process.env, WECOM_CONTEXT_APP_DIR: join(homedir(), "Library", "Application Support", "WeCom Context") },
    });
    if (result.status !== 0 && result.error) continue;
    const line = result.stdout?.trim().split("\n").at(-1);
    if (!line) continue;
    const response = JSON.parse(line);
    if (response.ok) return response.data;
    throw new Error(`${response.error?.code}: ${response.error?.message}`);
  }
  throw new Error("找不到 WeCom Context Core sidecar");
}

function agentsDir() {
  const dir = join(tmpdir(), "wecom-agents");
  mkdirSync(dir, { recursive: true });
  return dir;
}

function isConversational(kind) {
  return kind === "单聊" || kind === "群聊";
}

async function listSessions() {
  const data = callCore("sessions", { limit: 50 });
  for (const item of data.sessions.filter((session) => isConversational(session.kind))) {
    console.log(`${item.session_key}  ${item.display_name}  [${item.kind}]  ${item.last_message_time ?? "-"}`);
  }
}

async function allowSessions(keys) {
  for (const key of keys) {
    callCore("allow_session", { session_key: key });
    console.log(`已允许: ${key}`);
  }
}

function agentCommand(key, name) {
  const escapedName = name.replaceAll("'", "");
  const prompt = `你是绑定企业微信会话「${escapedName}」的专属 Agent。调用 read_wecom_context 可读取该会话完整上下文（未传参数时自动读取本绑定会话）。`;
  return `cd '${workdirFor(key)}' && WECOM_CONTEXT_SESSION_KEY='${key}' pi --append-system-prompt '${prompt}'`;
}

function workdirFor(key) {
  const workdir = join(agentsDir(), key);
  mkdirSync(workdir, { recursive: true });
  return workdir;
}

function openAgentWindows(commands) {
  const script = `
    tell application "iTerm2"
      repeat with cmd in {${commands.map((c) => JSON.stringify(c).replaceAll("\\", "\\\\")).join(", ")}}
        create window with default profile
        tell current session of current window
          write text cmd
        end tell
        delay 1
      end repeat
    end tell`;
  const osascript = spawn("osascript", ["-"], { stdio: ["pipe", "ignore", "inherit"] });
  osascript.stdin.end(script);
}

async function launch(keys) {
  const sessions = callCore("sessions", { limit: 100 });
  const byKey = new Map(sessions.sessions.filter((item) => isConversational(item.kind)).map((item) => [item.session_key, item]));
  const commands = [];
  for (const key of keys) {
    const item = byKey.get(key);
    if (!item) throw new Error(`快照中找不到会话 ${key}；先运行 list 获取有效 key`);
    commands.push(agentCommand(key, item.display_name));
    console.log(`准备启动 Agent: ${item.display_name} (${key})`);
  }
  openAgentWindows(commands);
}

const [command, ...rest] = process.argv.slice(2);
if (command === "list") {
  await listSessions();
} else if (command === "allow") {
  await allowSessions(rest.filter((k) => /^[0-9a-f]{16}$/.test(k)));
} else if (command === "launch") {
  let keys = rest.filter((k) => /^[0-9a-f]{16}$/.test(k));
  if (rest[0] === "--all") {
    const n = Number(rest[1] ?? 5);
    const data = callCore("sessions", { limit: Math.max(1, Math.min(n, 20)) });
    for (const item of data.sessions.filter((session) => isConversational(session.kind))) {
      callCore("allow_session", { session_key: item.session_key });
      keys.push(item.session_key);
    }
  }
  if (!keys.length) throw new Error("未指定 session_key；先运行 list 查看");
  await allowSessions([...new Set(keys)]);
  await launch([...new Set(keys)]);
} else {
  console.log("用法: wecom-agents.mjs list | allow <key...> | launch <key...> | launch --all [N]");
}
