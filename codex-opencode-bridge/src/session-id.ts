import { realpath, stat } from "node:fs/promises";
import { isAbsolute } from "node:path";
import { BridgeError } from "./errors.js";
import type { AgentTool } from "./types.js";

const SESSION_ID = /^(opencode|codex|claude):(.+)$/s;
const CONTROL_CHARS = /[\x00-\x1f]/;

/** Splits a `session_id` tool argument into its agent tool and native reference. */
export function parseSessionId(value: string): { tool: AgentTool; ref: string } {
  const match = SESSION_ID.exec(value);
  if (!match) {
    throw new BridgeError(
      "INVALID_ID",
      "session_id must be opencode:<id or title>, codex:<uuid or name>, or claude:<uuid or name>",
    );
  }
  const tool = match[1] as AgentTool;
  const ref = match[2].trim();
  if (ref.length < 1 || ref.length > 256 || CONTROL_CHARS.test(ref)) {
    throw new BridgeError(
      "INVALID_ID",
      "session_id must be opencode:<id or title>, codex:<uuid or name>, or claude:<uuid or name>",
    );
  }
  return { tool, ref };
}

/** Resolves and validates a `folder_path` tool argument to a canonical absolute directory. */
export async function resolveFolder(path: string): Promise<string> {
  if (!isAbsolute(path)) {
    throw new BridgeError("INVALID_FOLDER", "folder_path must be an absolute path");
  }
  let real: string;
  try {
    real = await realpath(path);
  } catch {
    throw new BridgeError("INVALID_FOLDER", `folder_path does not exist: ${path}`);
  }
  let info;
  try {
    info = await stat(real);
  } catch {
    throw new BridgeError("INVALID_FOLDER", `folder_path does not exist: ${path}`);
  }
  if (!info.isDirectory()) {
    throw new BridgeError("INVALID_FOLDER", `folder_path is not a directory: ${path}`);
  }
  return real;
}
