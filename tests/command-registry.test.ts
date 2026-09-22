import { describe, expect, test } from "bun:test";

/**
 * 前端 invoke 的命令必须出现在 Rust 的 generate_handler! 里，
 * 否则运行时只会得到一个 INTERNAL_ERROR「Command xxx not found」。
 * remove_dataset / restore_dataset 曾经漏注册，导致删除企业按钮直接报错。
 */
const ROOT = new URL("../", import.meta.url).pathname;

const ipcSource = await Bun.file(`${ROOT}apps/desktop/src/ipc.ts`).text();
const libSource = await Bun.file(`${ROOT}apps/desktop/src-tauri/src/lib.rs`).text();

const invoked = [...ipcSource.matchAll(/invoke(?:<[^>]*>)?\(\s*"([a-z0-9_]+)"/g)].map((match) => match[1]);
const handlerBlock = libSource.match(/generate_handler!\[(.*?)\]\)/s)?.[1] ?? "";
const registered = new Set(
  [...handlerBlock.matchAll(/(?:(\w+)::)?(\w+),/g)].map((match) => match[2]),
);

describe("Tauri command registry", () => {
  test("registry parses a plausible command list", () => {
    expect(invoked.length).toBeGreaterThan(20);
    expect(registered.size).toBeGreaterThan(20);
  });

  test("every invoked command is registered", () => {
    const missing = [...new Set(invoked)].filter((command) => !registered.has(command));
    expect(missing).toEqual([]);
  });
});
