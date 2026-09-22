import { describe, expect, test } from "bun:test";
import { CoreClient, CoreClientError, type CoreRunOptions, type CoreRunResult } from "../src/core-client.js";

const success = (data: unknown): CoreRunResult => ({
  stdout: JSON.stringify({ protocol_version: "1", request_id: "test", ok: true, data }),
  code: 0,
  killed: false,
});

function clientWith(
  runner: (command: string, args: readonly string[], options: CoreRunOptions) => Promise<CoreRunResult>,
  options: Partial<ConstructorParameters<typeof CoreClient>[0]> = {},
): CoreClient {
  return new CoreClient({
    cwd: "/tmp/pi-wecom-context-test",
    pythonPath: "python3",
    sidecarEntry: "sidecar/wecom_context_core/protocol.py",
    coreConfigPath: "/tmp/wecom-context-config.json",
    runner,
    ...options,
  });
}

describe("CoreClient", () => {
  test("builds a source sidecar request without a shell", async () => {
    let invocation: { command: string; args: readonly string[]; cwd: string; input: string } | undefined;
    const client = clientWith(async (command, args, options) => {
      invocation = { command, args, cwd: options.cwd, input: options.input };
      return success({ status: "ok" });
    });

    await expect(client.request("status", { limit: 3 })).resolves.toEqual({ status: "ok" });
    expect(invocation).toMatchObject({
      command: "python3",
      args: ["-m", "sidecar.wecom_context_core.protocol", "--config", "/tmp/wecom-context-config.json"],
      cwd: "/tmp/pi-wecom-context-test",
    });
    expect(JSON.parse(invocation?.input.trim() ?? "")).toMatchObject({ protocol_version: "1", action: "status", limit: 3 });
  });

  test("uses the packaged sidecar executable directly", async () => {
    let invocation: { command: string; args: readonly string[] } | undefined;
    const client = clientWith(async (command, args) => {
      invocation = { command, args };
      return success({});
    }, { sidecarEntry: "/Applications/WeCom Context.app/Contents/Resources/wecom-context-core-aarch64-apple-darwin" });

    await client.request("status");
    expect(invocation).toEqual({
      command: "/Applications/WeCom Context.app/Contents/Resources/wecom-context-core-aarch64-apple-darwin",
      args: ["--config", "/tmp/wecom-context-config.json"],
    });
  });

  test("maps process, output, and sidecar errors to stable codes", async () => {
    const failed = clientWith(async () => ({ stdout: "", code: 1, killed: false }));
    await expect(failed.request("status")).rejects.toMatchObject({ code: "CORE_FAILED" });

    const malformed = clientWith(async () => ({ stdout: "not-json\n", code: 0, killed: false }));
    await expect(malformed.request("status")).rejects.toMatchObject({ code: "OUTPUT_INVALID" });

    const sidecarError = clientWith(async () => ({
      stdout: JSON.stringify({
        ok: false,
        error: { code: "SESSION_NOT_ALLOWED", message: "未选择允许读取的会话", retryable: false, details: { session_key: "0123456789abcdef" } },
      }),
      code: 0,
      killed: false,
    }));
    await expect(sidecarError.request("read_context")).rejects.toMatchObject({
      code: "SESSION_NOT_ALLOWED",
      message: "未选择允许读取的会话",
      retryable: false,
      details: { session_key: "0123456789abcdef" },
    });
  });

  test("maps killed processes and pre-aborted signals to timeout errors", async () => {
    const killed = clientWith(async () => ({ stdout: "", code: null, killed: true }));
    await expect(killed.request("refresh_snapshot")).rejects.toMatchObject({ code: "CORE_TIMEOUT", retryable: true });

    const controller = new AbortController();
    controller.abort();
    const unavailable = clientWith(async () => {
      throw new Error("runner must not start");
    });
    await expect(unavailable.request("status", {}, controller.signal)).rejects.toBeInstanceOf(CoreClientError);
    await expect(unavailable.request("status", {}, controller.signal)).rejects.toMatchObject({ code: "CORE_TIMEOUT" });
  });
});
