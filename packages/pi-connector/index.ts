import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerCommands } from "./src/commands.js";
import { registerWecomCliTool, registerWecomSendTool } from "./src/wecom-cli.js";
import { registerReadContextTool } from "./src/tool.js";

export default function piWecomContextConnector(pi: ExtensionAPI): void {
  registerCommands(pi);
  registerWecomCliTool(pi);
  registerWecomSendTool(pi);
  registerReadContextTool(pi);
}
