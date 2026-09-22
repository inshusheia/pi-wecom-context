import { mkdir, cp, rm } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

const root = resolve(new URL("..", import.meta.url).pathname);
const source = join(root, "packages", "pi-connector");
const target = join(root, "apps", "desktop", "src-tauri", "resources", "pi-connector");
const files = [
  "index.ts",
  "src/commands.ts",
  "src/config.ts",
  "src/core-client.ts",
  "src/tool.ts",
  "src/types.ts",
  "src/wecom-cli.ts",
];

await rm(target, { recursive: true, force: true });
for (const relative of files) {
  const destination = join(target, relative);
  await mkdir(dirname(destination), { recursive: true });
  await cp(join(source, relative), destination);
}
console.log(`staged ${files.length} Connector files`);
