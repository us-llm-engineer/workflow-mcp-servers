import { readdir, readFile, readlink, realpath } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { BridgeError } from "./errors.js";
import { execFile } from "./exec.js";
import { readStartTime } from "./procfs.js";
import { exportOpenCodeJson } from "./transcripts.js";
import type { Delivery, OlderWindow, ResolvedSession } from "./types.js";

interface OpenCodeListEntry {
  id: string;
  title?: string;
  directory?: string;
  created?: number;
  updated?: number;
  time?: { created?: number; updated?: number };
}

interface RegistryEntry {
  pid: number;
  serverUrl: string;
  directory: string;
  token: string;
  protocol?: number;
}

interface RunningTui {
  pid: number;
  tty: string;
  cwd: string;
  cmdline: string[];
  /** Process start time in clock ticks since boot; larger is newer. */
  startTime: number;
  registry: RegistryEntry | null;
}

function openCodeBin(): string {
  return process.env.OPENCODE_BIN || "opencode";
}

function runtimeDirectory(): string {
  if (process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR) return process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR;
  const uid = typeof process.getuid === "function" ? process.getuid() : "user";
  return join("/tmp", `codex-opencode-bridge-${uid}`);
}

async function canonicalize(path: string): Promise<string> {
  try {
    return await realpath(path);
  } catch {
    return path;
  }
}

/** Resolves an `opencode:<id or title>` reference to one OpenCode session in `folder`. */
export async function resolveOpenCodeSession(folder: string, ref: string): Promise<ResolvedSession> {
  let parsed: unknown;
  try {
    const result = await execFile(openCodeBin(), ["session", "list", "--format", "json"], {
      cwd: folder,
      timeoutMs: 30_000,
      maxOutputBytes: 16 * 1024 * 1024,
    });
    parsed = JSON.parse(result.stdout);
  } catch (error) {
    if (error instanceof BridgeError && error.code === "COMMAND_TIMEOUT") {
      const fallback = await listOpenCodeSessionsFromDatabase();
      if (fallback) return resolveOpenCodeSessionFromList(folder, ref, fallback);
    }
    if (error instanceof BridgeError) throw error;
    throw new BridgeError("COMMAND_FAILED", "opencode session list did not return JSON");
  }
  if (!Array.isArray(parsed)) {
    throw new BridgeError("COMMAND_FAILED", "opencode session list did not return a JSON array");
  }
  return resolveOpenCodeSessionFromList(folder, ref, parsed as OpenCodeListEntry[]);
}

async function resolveOpenCodeSessionFromList(folder: string, ref: string, sessions: OpenCodeListEntry[]): Promise<ResolvedSession> {
  const canonicalDirs = await Promise.all(sessions.map((session) => canonicalize(session.directory ?? "")));

  const matches = sessions
    .map((session, index) => ({ session, directory: canonicalDirs[index] }))
    .filter(({ session }) => session.id === ref || session.title === ref);

  const inFolder = matches.filter(({ directory }) => directory === folder);
  if (inFolder.length >= 1) {
    // Several sessions can share a title: the most recently updated one wins.
    const recency = ({ session }: { session: OpenCodeListEntry }) => session.updated ?? session.created ?? session.time?.updated ?? session.time?.created ?? 0;
    const [{ session }, ...older] = [...inFolder].sort((x, y) => recency(y) - recency(x));
    return {
      tool: "opencode",
      sessionId: session.id,
      folder,
      name: session.title || null,
      transcriptPath: null,
      ...(older.length ? { olderSessionIds: older.map((entry) => entry.session.id) } : {}),
    };
  }
  const idMatchElsewhere = matches.find(({ session }) => session.id === ref);
  if (idMatchElsewhere) {
    throw new BridgeError(
      "FOLDER_MISMATCH",
      `OpenCode session ${ref} belongs to ${idMatchElsewhere.directory}, not ${folder}`,
    );
  }
  throw new BridgeError("SESSION_NOT_FOUND", `No OpenCode session named or identified as ${ref} in ${folder}`);
}

function openCodeDatabasePath(): string {
  return join(process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"), "opencode", "opencode.db");
}

/** Read-only timeout fallback for OpenCode CLI hangs while its local database remains available. */
async function listOpenCodeSessionsFromDatabase(): Promise<OpenCodeListEntry[] | null> {
  const query = "SELECT id, title, directory, time_created, time_updated FROM session";
  try {
    const result = await execFile("sqlite3", ["-readonly", "-json", openCodeDatabasePath(), query], {
      timeoutMs: 3_000,
      maxOutputBytes: 16 * 1024 * 1024,
    });
    const rows: unknown = JSON.parse(result.stdout);
    if (!Array.isArray(rows)) return null;
    const sessions: OpenCodeListEntry[] = [];
    for (const row of rows) {
      if (!isOpenCodeDatabaseRow(row)) return null;
      sessions.push({ id: row.id, title: row.title, directory: row.directory, created: row.time_created, updated: row.time_updated });
    }
    return sessions;
  } catch {
    return null;
  }
}

function isOpenCodeDatabaseRow(value: unknown): value is {
  id: string; title: string; directory: string; time_created: number; time_updated: number;
} {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  return typeof row.id === "string" && typeof row.title === "string" && typeof row.directory === "string"
    && typeof row.time_created === "number" && typeof row.time_updated === "number";
}

const PLUGIN_PROTOCOL = 4;

/**
 * Delivers `message` into an OpenCode session without touching any prompt box.
 *
 * The bridge plugin inside the OpenCode TUI process submits the message straight
 * to the session through that process's own server, so the TUI renders it live
 * while whatever the human is typing stays untouched. Earlier plugin versions
 * could only type into the prompt box, so they are refused rather than used.
 */
export async function sendToOpenCode(session: ResolvedSession, message: string): Promise<Delivery> {
  const tuis = await scanOpenCodeTuis();
  const inFolder = tuis.filter((tui) => tui.cwd === session.folder);
  if (inFolder.length === 0) {
    throw new BridgeError(
      "TARGET_NOT_RUNNING",
      `No OpenCode TUI is running in ${session.folder}. Start one: cd ${session.folder} && opencode -s ${session.sessionId}`,
    );
  }

  const capable = inFolder.filter((tui) => hasCurrentPlugin(tui));
  if (capable.length === 0) {
    const tui = inFolder[0];
    throw new BridgeError(
      "TUI_CONTROL_UNAVAILABLE",
      `OpenCode (pid ${tui.pid}) in ${session.folder} was opened before the current bridge plugin was installed, and older plugins can only deliver by typing into your prompt box. Reopen that OpenCode once and messages will go straight to the session without touching what you type.`,
    );
  }
  const { tui: target, note } = chooseTui(capable, session);
  const registry = target.registry as RegistryEntry;
  const serverUrl = validateLoopbackUrl(registry.serverUrl);

  const body: Record<string, unknown> = { parts: [{ type: "text", text: message }] };
  Object.assign(body, await lastPromptSettings(session.sessionId));
  await postControl(target, serverUrl, registry.token, { route: `/session/${session.sessionId}/prompt_async`, body });
  const older = olderWindows(inFolder, session, target.pid);
  return {
    transport: "opencode-tui",
    pid: target.pid,
    tty: target.tty,
    pane: null,
    ...(note ? { note } : {}),
    ...(older.length ? { older_windows: older } : {}),
  };
}

/** Windows started on an older same-named session, or older duplicates started on this session. */
function olderWindows(windows: RunningTui[], session: ResolvedSession, deliveredPid: number): OlderWindow[] {
  const found: OlderWindow[] = [];
  for (const tui of windows) {
    if (tui.pid === deliveredPid) continue;
    const started = selectedSession(tui.cmdline);
    if (started === undefined) continue;
    if (session.olderSessionIds?.includes(started)) {
      found.push({ pid: tui.pid, tty: tui.tty, session_id: started, reason: "older_session_with_same_name" });
    } else if (started === session.sessionId) {
      found.push({ pid: tui.pid, tty: tui.tty, session_id: started, reason: "duplicate_window" });
    }
  }
  return found;
}

function hasCurrentPlugin(tui: RunningTui): boolean {
  const registry = tui.registry;
  if (!registry || registry.pid !== tui.pid || registry.protocol !== PLUGIN_PROTOCOL) return false;
  if (typeof registry.token !== "string" || !registry.token) return false;
  try {
    validateLoopbackUrl(registry.serverUrl);
    return true;
  } catch {
    return false;
  }
}

/**
 * Picks the OpenCode window that should carry a message for `sessionId`.
 *
 * A window's launch command is the only reliable hint to the session it shows:
 *  - started on this session (`-s <id>`): best;
 *  - started on no particular session (plain `opencode`, `-c`): may be showing
 *    any session, this one included;
 *  - started on a different session: most likely showing that one.
 * Windows on different sessions are not duplicates, so this never fails: the best
 * group wins and its newest window is used.
 */
export function pickOpenCodeTui<T extends { pid: number; cmdline: string[]; startTime: number }>(candidates: T[], sessionId: string): T {
  const tier = (tui: T) => {
    const selected = selectedSession(tui.cmdline);
    return selected === sessionId ? 0 : selected === undefined ? 1 : 2;
  };
  return [...candidates].sort((a, b) => tier(a) - tier(b) || b.startTime - a.startTime || b.pid - a.pid)[0];
}

function chooseTui(capable: RunningTui[], session: ResolvedSession): { tui: RunningTui; note?: string } {
  const tui = pickOpenCodeTui(capable, session.sessionId);
  const selected = selectedSession(tui.cmdline);
  if (selected !== undefined && selected !== session.sessionId) {
    return {
      tui,
      note: `No OpenCode window in ${session.folder} was started on this session, so it went through the window on ${tui.tty} (pid ${tui.pid}, started on ${selected}). If you view the session in another window, that window will not update live.`,
    };
  }
  return { tui };
}

/**
 * The agent, model, and variant of the session's latest user message, so a
 * bridged message runs with the same settings the human last used there.
 */
async function lastPromptSettings(sessionId: string): Promise<Record<string, unknown>> {
  try {
    const root = await exportOpenCodeJson(sessionId);
    const messages = Array.isArray(root) ? root : (root as { messages?: unknown[] })?.messages;
    if (!Array.isArray(messages)) return {};
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const info = (messages[index] as { info?: Record<string, unknown> })?.info;
      if (info?.role !== "user") continue;
      const settings: Record<string, unknown> = {};
      const model = info.model as { providerID?: unknown; modelID?: unknown; variant?: unknown } | undefined;
      if (typeof model?.providerID === "string" && typeof model?.modelID === "string") {
        settings.model = { providerID: model.providerID, modelID: model.modelID };
        if (typeof model.variant === "string") settings.variant = model.variant;
      }
      if (typeof info.agent === "string") settings.agent = info.agent;
      return settings;
    }
  } catch {
    // Fall back to OpenCode's defaults for the session.
  }
  return {};
}

async function postControl(target: RunningTui, serverUrl: URL, token: string, request: unknown): Promise<void> {
  let response: Response;
  try {
    response = await fetch(new URL("/bridge/request", serverUrl), {
      method: "POST",
      headers: { "x-bridge-token": token, "content-type": "application/json" },
      body: JSON.stringify(request),
      signal: AbortSignal.timeout(15_000),
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "unknown connection failure";
    throw new BridgeError(
      "TUI_CONTROL_UNAVAILABLE",
      `Could not reach the bridge plugin of OpenCode TUI (pid ${target.pid}) at ${serverUrl.origin}: ${detail}`,
    );
  }
  const text = await response.text().catch(() => "");
  if (!response.ok) {
    throw new BridgeError("COMMAND_FAILED", `OpenCode TUI (pid ${target.pid}) rejected the message: HTTP ${response.status}${text ? `: ${text.slice(0, 500)}` : ""}`);
  }
}

export async function scanOpenCodeTuis(): Promise<RunningTui[]> {
  const procEntries = await readdir("/proc", { withFileTypes: true });
  const matches: RunningTui[] = [];
  const root = runtimeDirectory();

  await Promise.all(
    procEntries
      .filter((entry) => entry.isDirectory() && /^\d+$/.test(entry.name))
      .map(async (entry) => {
        const pid = Number(entry.name);
        try {
          const cmdline = (await readFile(`/proc/${pid}/cmdline`, "utf8")).split("\0").filter(Boolean);
          const isOpenCode = basename(cmdline[0] ?? "") === "opencode" || basename(cmdline[1] ?? "") === "opencode";
          if (!isOpenCode) return;
          const tty = await readlink(`/proc/${pid}/fd/0`);
          if (!tty.startsWith("/dev/pts/")) return;
          const cwd = await realpath(await readlink(`/proc/${pid}/cwd`));
          let registry: RegistryEntry | null = null;
          try {
            registry = JSON.parse(await readFile(join(root, `${pid}.json`), "utf8")) as RegistryEntry;
          } catch {
            registry = null;
          }
          matches.push({ pid, tty, cwd, cmdline, startTime: Number(await readStartTime(pid)) || 0, registry });
        } catch {
          // Processes can exit or become unreadable while /proc is being scanned.
        }
      }),
  );
  return matches;
}

function selectedSession(argv: string[]): string | undefined {
  for (let index = 1; index < argv.length; index += 1) {
    if ((argv[index] === "-s" || argv[index] === "--session") && argv[index + 1]) return argv[index + 1];
    if (argv[index].startsWith("--session=")) return argv[index].slice("--session=".length);
  }
  return undefined;
}

function validateLoopbackUrl(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new BridgeError("TUI_CONTROL_UNAVAILABLE", "OpenCode TUI registry contains an invalid control URL");
  }
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new BridgeError("TUI_CONTROL_UNAVAILABLE", "OpenCode TUI control URL is not a local HTTP endpoint");
  }
  return url;
}
