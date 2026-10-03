import { readdir, readFile, realpath, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";
import { CLAUDE_CHANNEL_SERVER_NAME, channelEnabledInCmdline, claudeChannelServerName, pushToClaudeChannel, type ChannelOutcome } from "./claude-channel.js";
import { BridgeError } from "./errors.js";
import { execFile } from "./exec.js";
import { listPids, readCmdline, readStartTime, readStdinTty } from "./procfs.js";
import { locateTmuxPane, pasteAndSubmit } from "./tmux.js";
import type { Delivery, OlderWindow, ResolvedSession } from "./types.js";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function claudeConfigDir(): string {
  return process.env.CLAUDE_CONFIG_DIR || join(homedir(), ".claude");
}

interface RegistryEntry {
  pid: number;
  sessionId: string;
  cwd: string;
  procStart?: string;
  kind?: string;
  jobId?: string;
  name?: string;
  status?: string;
  startedAt?: number;
  updatedAt?: number;
}

/** Resolves a Claude Code session reference (sessionId, name, or jobId) to one session in one folder. */
export async function resolveClaudeSession(folder: string, ref: string): Promise<ResolvedSession> {
  const configDir = claudeConfigDir();
  const registry = await readRegistry(configDir);
  const liveEntries: RegistryEntry[] = [];
  for (const entry of registry) {
    if (await isLive(entry)) liveEntries.push(entry);
  }

  const matching = liveEntries.filter((e) => e.sessionId === ref || e.name === ref || e.jobId === ref);
  if (matching.length > 0) {
    const resolvedCwds = await Promise.all(
      matching.map(async (entry) => ({ entry, cwd: await realpathOrRaw(entry.cwd) })),
    );
    const inFolder = resolvedCwds.filter((m) => m.cwd === folder);
    if (inFolder.length === 0) {
      const cwds = [...new Set(resolvedCwds.map((m) => m.cwd))].join(", ");
      throw new BridgeError("FOLDER_MISMATCH", `Claude Code session ${ref} belongs to ${cwds}, not ${folder}`);
    }
    // Several sessions can share a name: the most recently active one wins.
    const recency = (entry: RegistryEntry) => entry.updatedAt ?? entry.startedAt ?? 0;
    const bySession = new Map<string, RegistryEntry>();
    for (const { entry } of inFolder) {
      const seen = bySession.get(entry.sessionId);
      if (!seen || recency(entry) > recency(seen)) bySession.set(entry.sessionId, entry);
    }
    const [chosen, ...older] = [...bySession.values()].sort((x, y) => recency(y) - recency(x));
    return {
      tool: "claude",
      sessionId: chosen.sessionId,
      folder,
      name: chosen.name ?? null,
      transcriptPath: await findTranscriptPath(configDir, folder, chosen.sessionId),
      ...(older.length ? { olderSessionIds: older.map((entry) => entry.sessionId) } : {}),
    };
  }

  // No live match: fall back to on-disk transcripts.
  const projectDir = join(configDir, "projects", escapeFolder(folder));
  if (UUID_RE.test(ref)) {
    const direct = join(projectDir, `${ref}.jsonl`);
    if (await fileExists(direct)) {
      return { tool: "claude", sessionId: ref, folder, name: await lastCustomTitle(direct), transcriptPath: direct };
    }
  }

  let files: string[];
  try {
    files = (await readdir(projectDir)).filter((f) => f.endsWith(".jsonl"));
  } catch {
    files = [];
  }
  const candidates: Array<{ sessionId: string; path: string }> = [];
  for (const file of files) {
    const path = join(projectDir, file);
    const title = await lastCustomTitle(path);
    if (title === ref) candidates.push({ sessionId: file.slice(0, -".jsonl".length), path });
  }

  if (candidates.length >= 1) {
    // Several transcripts can carry the same title: the most recently written one wins.
    const dated = await Promise.all(candidates.map(async (c) => ({ ...c, mtime: (await stat(c.path)).mtimeMs })));
    const [chosen, ...older] = dated.sort((x, y) => y.mtime - x.mtime);
    return {
      tool: "claude",
      sessionId: chosen.sessionId,
      folder,
      name: ref,
      transcriptPath: chosen.path,
      ...(older.length ? { olderSessionIds: older.map((c) => c.sessionId) } : {}),
    };
  }
  throw new BridgeError("SESSION_NOT_FOUND", `No Claude Code session named or identified as ${ref} in ${folder}`);
}

/**
 * Delivers a message into a live Claude Code session.
 *
 * Preferred: a channel event pushed by the bridge server running inside that
 * session (needs the session started with the development-channels flag naming
 * this server). Fallback: typing into the tmux pane the session runs in.
 */
export async function sendToClaude(session: ResolvedSession, message: string): Promise<Delivery> {
  const delivery = await deliverToClaude(session, message);
  const older = await olderClaudeWindows(session, delivery.pid);
  return older.length ? { ...delivery, older_windows: older } : delivery;
}

/** Live windows of older same-named sessions, and older duplicate windows of this session. */
async function olderClaudeWindows(session: ResolvedSession, deliveredPid: number): Promise<OlderWindow[]> {
  const found: OlderWindow[] = [];
  const ids = new Set([session.sessionId, ...(session.olderSessionIds ?? [])]);
  for (const entry of await readRegistry(claudeConfigDir())) {
    if (!ids.has(entry.sessionId) || entry.kind === "bg" || entry.pid === deliveredPid || !(await isLive(entry))) continue;
    found.push({
      pid: entry.pid,
      tty: await readStdinTty(entry.pid),
      session_id: entry.sessionId,
      reason: entry.sessionId === session.sessionId ? "duplicate_window" : "older_session_with_same_name",
    });
  }
  return found;
}

async function deliverToClaude(session: ResolvedSession, message: string): Promise<Delivery> {
  const configDir = claudeConfigDir();
  const registry = await readRegistry(configDir);
  const liveEntries: RegistryEntry[] = [];
  for (const entry of registry) {
    if (entry.sessionId === session.sessionId && (await isLive(entry))) liveEntries.push(entry);
  }
  const relaunch = `claude --dangerously-load-development-channels server:${CLAUDE_CHANNEL_SERVER_NAME} --resume ${session.sessionId}`;

  if (liveEntries.length === 0) {
    throw new BridgeError(
      "TARGET_NOT_RUNNING",
      `Claude Code session ${session.sessionId} is not running. Start it in ${session.folder} with: ${relaunch}`,
    );
  }

  // Interactive sessions first, then background ones; each is a Claude Code process.
  const newest = (x: RegistryEntry, y: RegistryEntry) => (y.startedAt ?? 0) - (x.startedAt ?? 0);
  const ordered = [
    ...liveEntries.filter((e) => e.kind !== "bg").sort(newest),
    ...liveEntries.filter((e) => e.kind === "bg").sort(newest),
  ];
  const outcomes: Array<{ entry: RegistryEntry; outcome: ChannelOutcome }> = [];
  for (const entry of ordered) {
    const outcome = await pushToClaudeChannel(entry.pid, message);
    if (outcome === "delivered") {
      return { transport: "claude-channel", pid: entry.pid, tty: await readStdinTty(entry.pid), pane: null };
    }
    outcomes.push({ entry, outcome });
  }

  // A channel-enabled session can be launched from a nested project that has
  // not inherited this bridge's MCP entry. Repair that ordinary setup gap, then
  // retry the same message only after a live receiver appears.
  if (outcomes.some(({ outcome }) => outcome === "no-receiver") && await anyChannelEnabled(ordered)) {
    await installClaudeChannelBridge(session.folder);
    const recovered = await waitForClaudeChannel(ordered, message);
    if (recovered) {
      return {
        transport: "claude-channel",
        pid: recovered.pid,
        tty: await readStdinTty(recovered.pid),
        pane: null,
        note: "The bridge registered itself with Claude Code and retried this message after the target channel became available.",
      };
    }
  }

  // No channel available: type into a tmux pane if the session has one.
  for (const entry of ordered) {
    const pane = await tmuxPaneForEntry(entry, liveEntries);
    if (pane) {
      await pasteAndSubmit(pane.socket, pane.pane, message);
      return { transport: "tmux", pid: pane.pid, tty: pane.tty, pane: pane.pane };
    }
  }

  const first = outcomes[0];
  if (first.outcome === "not-enabled") {
    throw new BridgeError(
      "CHANNEL_NOT_ENABLED",
      `Claude Code session ${session.sessionId} (pid ${first.entry.pid}) was started without channels enabled for ${CLAUDE_CHANNEL_SERVER_NAME}, so it would silently drop pushed messages. Restart it once in ${session.folder} with: ${relaunch}`,
    );
  }
  throw new BridgeError(
    "CHANNEL_NOT_ENABLED",
    `Claude Code session ${session.sessionId} (pid ${first.entry.pid}) has no ${CLAUDE_CHANNEL_SERVER_NAME} MCP server connected, so nothing can push into it. Add the server to Claude Code (claude mcp add ${CLAUDE_CHANNEL_SERVER_NAME} -- node <path>/dist/index.js) and start the session with: ${relaunch}`,
  );
}

async function anyChannelEnabled(entries: RegistryEntry[]): Promise<boolean> {
  const argv = await Promise.all(entries.map((entry) => readCmdline(entry.pid)));
  return argv.some((args) => channelEnabledInCmdline(args ?? []));
}

/**
 * Makes the bridge available to future and dynamically refreshed Claude Code
 * sessions. Failure is deliberately non-fatal: the original delivery error is
 * more actionable when Claude itself cannot update its configuration.
 */
async function installClaudeChannelBridge(folder: string): Promise<void> {
  const entryPoint = fileURLToPath(new URL("./index.js", import.meta.url));
  try {
    await execFile(process.env.CLAUDE_BIN || "claude", [
      "mcp", "add", "--scope", "user", claudeChannelServerName(), "--", "node", entryPoint,
    ], { cwd: folder, timeoutMs: 30_000, maxOutputBytes: 1024 * 1024 });
  } catch {
    // It may already be registered, or Claude may decline to modify config.
    // Either way, the short receiver poll below is still safe and useful.
  }
}

async function waitForClaudeChannel(entries: RegistryEntry[], message: string): Promise<RegistryEntry | null> {
  // Claude Code can refresh MCP configuration without a session restart. Avoid
  // re-submitting while no receiver exists: exactly one post is made once one
  // reports itself live.
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await new Promise<void>((resolve) => setTimeout(resolve, 250));
    for (const entry of entries) {
      if (!(await isLive(entry))) continue;
      if (await pushToClaudeChannel(entry.pid, message) === "delivered") return entry;
    }
  }
  return null;
}

/** The tmux pane showing a session entry: its own terminal, or a `claude attach` client for a background session. */
async function tmuxPaneForEntry(
  entry: RegistryEntry,
  all: RegistryEntry[],
): Promise<{ pid: number; tty: string; socket: string; pane: string } | null> {
  const tryPid = async (pid: number) => {
    try {
      const located = await locateTmuxPane(pid, "Claude Code", "");
      return { pid, ...located };
    } catch {
      return null;
    }
  };
  if (entry.kind !== "bg") return tryPid(entry.pid);

  const tokens = new Set<string>();
  for (const e of all) {
    if (e.jobId) tokens.add(e.jobId);
    tokens.add(e.sessionId);
    if (e.name) tokens.add(e.name);
  }
  for (const pid of await listPids()) {
    const cmdline = await readCmdline(pid);
    if (!cmdline) continue;
    const attachIndex = cmdline.indexOf("attach");
    if (attachIndex < 1 || !tokens.has(cmdline[attachIndex + 1] ?? "")) continue;
    if (!cmdline.slice(0, attachIndex).some((arg) => basename(arg) === "claude")) continue;
    const found = await tryPid(pid);
    if (found) return found;
  }
  return null;
}

async function isLive(entry: RegistryEntry): Promise<boolean> {
  if (typeof entry.pid !== "number") return false;
  const startTime = await readStartTime(entry.pid);
  if (startTime === null) return false; // /proc/<pid> is gone (or unreadable).
  if (entry.procStart !== undefined && String(entry.procStart) !== startTime) return false;
  return true;
}

async function readRegistry(configDir: string): Promise<RegistryEntry[]> {
  const dir = join(configDir, "sessions");
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return [];
  }
  const out: RegistryEntry[] = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".json")) continue;
    try {
      const raw = await readFile(join(dir, entry.name), "utf8");
      const parsed: unknown = JSON.parse(raw);
      if (
        isObject(parsed) &&
        typeof parsed.pid === "number" &&
        typeof parsed.sessionId === "string" &&
        typeof parsed.cwd === "string"
      ) {
        out.push(parsed as unknown as RegistryEntry);
      }
    } catch {
      // Malformed or vanished file: skip it.
    }
  }
  return out;
}

async function findTranscriptPath(configDir: string, folder: string, sessionId: string): Promise<string | null> {
  const direct = join(configDir, "projects", escapeFolder(folder), `${sessionId}.jsonl`);
  if (await fileExists(direct)) return direct;
  const projectsRoot = join(configDir, "projects");
  let dirs: string[];
  try {
    dirs = (await readdir(projectsRoot, { withFileTypes: true })).filter((e) => e.isDirectory()).map((e) => e.name);
  } catch {
    return null;
  }
  for (const dir of dirs) {
    const candidate = join(projectsRoot, dir, `${sessionId}.jsonl`);
    if (await fileExists(candidate)) return candidate;
  }
  return null;
}

async function lastCustomTitle(path: string): Promise<string | null> {
  let content: string;
  try {
    content = await readFile(path, "utf8");
  } catch {
    return null;
  }
  let last: string | null = null;
  for (const line of content.split(/\r?\n/)) {
    if (!line.includes("custom-title")) continue;
    try {
      const record: unknown = JSON.parse(line);
      if (isObject(record) && record.type === "custom-title" && typeof record.customTitle === "string") {
        last = record.customTitle;
      }
    } catch {
      // Ignore a malformed line.
    }
  }
  return last;
}

async function realpathOrRaw(path: string): Promise<string> {
  try {
    return await realpath(path);
  } catch {
    return path;
  }
}

async function fileExists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

function escapeFolder(folder: string): string {
  return folder.replace(/[^A-Za-z0-9]/g, "-");
}

function dedupeByTty<T extends { pid: number; tty: string }>(items: T[]): T[] {
  const byTty = new Map<string, T>();
  for (const item of items) {
    const existing = byTty.get(item.tty);
    if (!existing || item.pid < existing.pid) byTty.set(item.tty, item);
  }
  return [...byTty.values()];
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
