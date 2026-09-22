import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
import { CoreClient, CoreClientError } from "./core-client.js";
import { loadConnectorConfig } from "./config.js";
import type { RefreshData, SessionsData } from "./types.js";

const COMMAND = "wecom-context";

function client(ctx: ExtensionCommandContext): CoreClient {
  const config = loadConnectorConfig(ctx.cwd);
  return new CoreClient({
    cwd: ctx.cwd,
    pythonPath: config.pythonPath,
    sidecarEntry: config.sidecarEntry,
    coreConfigPath: config.coreConfigPath,
  });
}

function errorMessage(error: unknown): string {
  if (error instanceof CoreClientError) return `${error.code}: ${error.message}`;
  return "WeCom Context Core 操作失败";
}

async function showStatus(ctx: ExtensionCommandContext): Promise<void> {
  const data = await client(ctx).request<Record<string, unknown>>("status", {}, ctx.signal);
  const snapshot = data.snapshot as Record<string, unknown> | undefined;
  ctx.ui.notify(
    [
      `数据集：${data.dataset ? "可用" : "不可用"}`,
      `快照：${snapshot?.available ? `约 ${Math.round(Number(snapshot.age_minutes ?? 0))} 分钟前` : "不可用"}`,
      `活动快照：${snapshot?.active ? "是" : "否"}`,
    ].join("\n"),
    "info",
  );
}

async function chooseSession(ctx: ExtensionCommandContext): Promise<void> {
  if (!ctx.hasUI) {
    ctx.ui.notify("sessions 需要交互式 Pi TUI", "warning");
    return;
  }
  const data = await client(ctx).request<SessionsData>("sessions", {}, ctx.signal);
  const conversational = data.sessions.filter((session) => session.kind === "单聊" || session.kind === "群聊");
  if (!conversational.length) {
    ctx.ui.notify("没有可用会话", "warning");
    return;
  }
  const options = conversational.map((session, index) => `[${index + 1}] ${session.display_name} · ${session.kind}`);
  const choice = await ctx.ui.select("选择企业微信会话", options);
  if (!choice) return;
  const index = options.indexOf(choice);
  const selected = conversational[index];
  if (!selected) throw new CoreClientError("SESSION_NOT_FOUND", "会话选择无效");
  await client(ctx).request("select_session", { session_key: selected.session_key }, ctx.signal);
  ctx.ui.setStatus(COMMAND, `WeCom: ${selected.display_name}`);
  ctx.ui.notify(`已选择会话：${selected.display_name}`, "info");
}

async function clearSession(ctx: ExtensionCommandContext): Promise<void> {
  await client(ctx).request("clear_session", {}, ctx.signal);
  ctx.ui.setStatus(COMMAND, "WeCom: 未选择");
  ctx.ui.notify("已清除当前企业微信会话选择", "info");
}

async function refresh(ctx: ExtensionCommandContext): Promise<void> {
  if (!ctx.hasUI) {
    ctx.ui.notify("refresh 需要交互式 Pi TUI", "warning");
    return;
  }
  const confirmed = await ctx.ui.confirm("刷新企业微信快照", "将只读本地数据库并创建新私有快照，不会覆盖旧快照。继续？");
  if (!confirmed) return;
  const result = await client(ctx).request<RefreshData>("refresh_snapshot", { confirmed: true }, ctx.signal);
  const invalidated = result.invalidated_session_keys?.length ?? 0;
  ctx.ui.notify(
    invalidated > 0
      ? `刷新成功：${result.decrypted_database_count} 个数据库；${invalidated} 个会话的旧快照绑定已失效，请重新选择来源`
      : `刷新成功：${result.decrypted_database_count} 个数据库`,
    "info",
  );
}

export function registerCommands(pi: ExtensionAPI): void {
  pi.registerCommand(COMMAND, {
    description: "管理本地企业微信上下文",
    handler: async (args, ctx) => {
      const command = args.trim().split(/\s+/)[0]?.toLowerCase() || "status";
      try {
        if (command === "status") return await showStatus(ctx);
        if (command === "sessions" || command === "use") return await chooseSession(ctx);
        if (command === "clear") return await clearSession(ctx);
        if (command === "refresh") return await refresh(ctx);
        ctx.ui.notify("用法：/wecom-context status | sessions | use | clear | refresh", "warning");
      } catch (error) {
        ctx.ui.notify(errorMessage(error), "error");
      }
    },
  });
}
