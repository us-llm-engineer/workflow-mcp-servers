import { randomBytes } from "node:crypto";
import { BridgeError } from "./errors.js";
import { execFile } from "./exec.js";
import { readEnviron, readStdinTty } from "./procfs.js";

/** Extracts the tmux server socket path from a process's TMUX env var. */
export function tmuxSocketFromEnv(env: Record<string, string> | null): string | null {
  const tmux = env?.TMUX;
  if (!tmux) return null;
  const commaIndex = tmux.indexOf(",");
  if (commaIndex === -1) return null;
  return tmux.slice(0, commaIndex);
}

/** Finds the live (non-dead) pane on a tmux server whose pane_tty matches. */
export async function findPaneByTty(socket: string, tty: string): Promise<string | null> {
  let stdout: string;
  try {
    const result = await execFile(
      "tmux",
      ["-S", socket, "list-panes", "-a", "-F", "#{pane_id}|#{pane_tty}|#{pane_dead}"],
      { timeoutMs: 10_000, maxOutputBytes: 512 * 1024 },
    );
    stdout = result.stdout;
  } catch {
    // The tmux server behind this socket may already be gone.
    return null;
  }
  for (const line of stdout.split(/\r?\n/)) {
    if (!line) continue;
    const fields = line.split("|");
    if (fields.length !== 3) continue;
    const [paneId, paneTty, paneDead] = fields;
    if (paneTty === tty && paneDead === "0") return paneId;
  }
  return null;
}

/** Pastes a message into a pane as one buffered paste, then submits it. */
export async function pasteAndSubmit(socket: string, pane: string, message: string): Promise<void> {
  if (!/^%\d+$/.test(pane)) {
    throw new BridgeError("COMMAND_FAILED", `Invalid tmux pane id: ${pane}`);
  }
  const bufferName = `agent-bridge-${randomBytes(12).toString("hex")}`;
  // The semicolons are separate argv items: tmux command separators, not
  // shell syntax. stdin supplies the message directly to load-buffer,
  // preserving newlines, quotes, and Unicode untouched.
  await execFile(
    "tmux",
    [
      "-S", socket,
      "load-buffer", "-b", bufferName, "-", ";",
      "paste-buffer", "-p", "-d", "-b", bufferName, "-t", pane,
    ],
    { input: message, timeoutMs: 15_000, maxOutputBytes: 64 * 1024 },
  );
  // Give TUIs with paste-burst detection time to settle so Enter is not
  // absorbed as part of the pasted content.
  await delay(200);
  await execFile("tmux", ["-S", socket, "send-keys", "-t", pane, "Enter"], {
    timeoutMs: 10_000,
    maxOutputBytes: 8 * 1024,
  });
}

/**
 * Discovers the tmux pane hosting a live process's terminal, with no human
 * setup required: pid -> controlling terminal -> tmux socket (from the
 * process's own TMUX env var) -> the pane whose pane_tty matches.
 */
export async function locateTmuxPane(
  pid: number,
  label: string,
  relaunchHint: string,
): Promise<{ socket: string; pane: string; tty: string }> {
  const tty = await readStdinTty(pid);
  if (!tty) {
    throw new BridgeError("TARGET_NOT_RUNNING", `${label} (pid ${pid}) has no interactive terminal`);
  }
  const env = await readEnviron(pid);
  const socket = tmuxSocketFromEnv(env);
  const pane = socket ? await findPaneByTty(socket, tty) : null;
  if (!socket || !pane) {
    throw new BridgeError(
      "NOT_IN_TMUX",
      `${label} is running on ${tty} outside tmux, so its terminal cannot be typed into. Restart it inside tmux: ${relaunchHint}`,
    );
  }
  return { socket, pane, tty };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
