import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const DEFAULT_CORE_CONFIG = join(homedir(), "Library", "Application Support", "WeCom Context", "config.json");

export interface CoreRunOptions {
  cwd: string;
  timeoutMs: number;
  maxBufferBytes: number;
  input: string;
  signal?: AbortSignal;
}

export interface CoreRunResult {
  stdout: string;
  code: number | null;
  killed: boolean;
}

export type CoreRunner = (
  command: string,
  args: readonly string[],
  options: CoreRunOptions,
) => Promise<CoreRunResult>;

export interface CoreClientConfig {
  cwd: string;
  pythonPath?: string;
  sidecarEntry?: string;
  coreConfigPath?: string;
  timeoutMs?: number;
  maxBufferBytes?: number;
  runner?: CoreRunner;
}

export class CoreClientError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly details?: unknown;

  constructor(code: string, message: string, retryable = false, details?: unknown) {
    super(message);
    this.name = "CoreClientError";
    this.code = code;
    this.retryable = retryable;
    this.details = details;
  }
}

function defaultRunner(command: string, args: readonly string[], options: CoreRunOptions): Promise<CoreRunResult> {
  const { promise, resolve: resolveResult, reject } = Promise.withResolvers<CoreRunResult>();
  const child = spawn(command, [...args], {
    cwd: options.cwd,
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = "";
  let killed = false;
  let settled = false;
  let timeout: ReturnType<typeof setTimeout>;
  const abort = (): void => {
    if (settled) return;
    killed = true;
    child.kill("SIGTERM");
  };
  const finish = (result: CoreRunResult): void => {
    if (settled) return;
    settled = true;
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
    resolveResult(result);
  };
  timeout = setTimeout(abort, options.timeoutMs);
  options.signal?.addEventListener("abort", abort, { once: true });
  child.stdout.on("data", (chunk: Buffer | string) => {
    stdout += chunk.toString();
    if (Buffer.byteLength(stdout, "utf8") > options.maxBufferBytes) abort();
  });
  child.stderr.on("data", () => undefined);
  child.once("error", (error) => {
    if (settled) return;
    settled = true;
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
    reject(error);
  });
  child.once("close", (code) => finish({ stdout, code, killed }));
  child.stdin.end(options.input);
  return promise;
}

export class CoreClient {
  private readonly cwd: string;
  private readonly pythonPath: string;
  private readonly sidecarEntry: string;
  private readonly coreConfigPath: string;
  private readonly timeoutMs: number;
  private readonly maxBufferBytes: number;
  private readonly runner: CoreRunner;

  constructor(options: CoreClientConfig) {
    this.cwd = resolve(options.cwd);
    this.pythonPath = options.pythonPath ?? "python3";
    this.sidecarEntry = resolve(this.cwd, options.sidecarEntry ?? "sidecar/wecom_context_core/protocol.py");
    this.coreConfigPath = options.coreConfigPath ?? DEFAULT_CORE_CONFIG;
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.maxBufferBytes = options.maxBufferBytes ?? 4 * 1024 * 1024;
    this.runner = options.runner ?? defaultRunner;
  }

  async request<T>(action: string, payload: Record<string, unknown> = {}, signal?: AbortSignal): Promise<T> {
    if (signal?.aborted) {
      throw new CoreClientError("CORE_TIMEOUT", "WeCom Context Core 超时或被取消", true);
    }
    const requestId = `pi-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const request = JSON.stringify({ protocol_version: "1", request_id: requestId, action, ...payload });
    const sourceSidecar = this.sidecarEntry.endsWith("/sidecar/wecom_context_core/protocol.py");
    const packagedSidecar = !sourceSidecar && this.sidecarEntry.split("/").at(-1)?.startsWith("wecom-context-core") === true;
    const command = packagedSidecar ? this.sidecarEntry : this.pythonPath;
    const args = sourceSidecar
      ? ["-m", "sidecar.wecom_context_core.protocol", "--config", this.coreConfigPath]
      : packagedSidecar
        ? ["--config", this.coreConfigPath]
        : [this.sidecarEntry, "--config", this.coreConfigPath];
    let result: CoreRunResult;
    try {
      result = await this.runner(command, args, {
        cwd: this.cwd,
        timeoutMs: this.timeoutMs,
        maxBufferBytes: this.maxBufferBytes,
        input: `${request}\n`,
        signal,
      });
    } catch {
      throw new CoreClientError("CORE_UNAVAILABLE", "WeCom Context Core 无法启动", true);
    }
    if (result.killed) throw new CoreClientError("CORE_TIMEOUT", "WeCom Context Core 超时或被取消", true);
    if (result.code !== 0) throw new CoreClientError("CORE_FAILED", "WeCom Context Core 执行失败", true);
    const line = result.stdout.trim().split("\n").at(-1) ?? "";
    let response: unknown;
    try {
      response = JSON.parse(line);
    } catch {
      throw new CoreClientError("OUTPUT_INVALID", "WeCom Context Core 返回格式无效");
    }
    if (!response || typeof response !== "object") {
      throw new CoreClientError("OUTPUT_INVALID", "WeCom Context Core 返回对象无效");
    }
    const value = response as { ok?: unknown; data?: unknown; error?: { code?: unknown; message?: unknown; retryable?: unknown; details?: unknown } };
    if (value.ok !== true) {
      const error = value.error;
      throw new CoreClientError(
        typeof error?.code === "string" ? error.code : "CORE_ERROR",
        typeof error?.message === "string" ? error.message : "WeCom Context Core 操作失败",
        error?.retryable === true,
        error?.details,
      );
    }
    return value.data as T;
  }
}
