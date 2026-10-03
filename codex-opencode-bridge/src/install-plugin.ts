import { randomBytes } from "node:crypto";
import { readFile, mkdir, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Copies the bundled OpenCode plugin into OpenCode's global plugin directory
 * so a live TUI can pick it up on its next start. Never throws: failures are
 * reported to stderr only, since stdout carries the MCP protocol stream.
 */
export async function ensureOpenCodePlugin(): Promise<void> {
  if (process.env.CODEX_OPENCODE_BRIDGE_NO_PLUGIN_INSTALL === "1") return;
  try {
    const sourceUrl = new URL("../integrations/opencode/codex-opencode-bridge.js", import.meta.url);
    const source = await readFile(fileURLToPath(sourceUrl), "utf8");
    const configHome = process.env.XDG_CONFIG_HOME || join(homedir(), ".config");
    const destDir = join(configHome, "opencode", "plugins");
    const dest = join(destDir, "codex-opencode-bridge.js");

    let current: string | null = null;
    try {
      current = await readFile(dest, "utf8");
    } catch {
      current = null;
    }
    if (current === source) return;

    await mkdir(destDir, { recursive: true });
    const tempPath = join(destDir, `.codex-opencode-bridge.${randomBytes(6).toString("hex")}.tmp`);
    await writeFile(tempPath, source, "utf8");
    await rename(tempPath, dest);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`codex-opencode-bridge: could not install OpenCode plugin: ${message}\n`);
  }
}
