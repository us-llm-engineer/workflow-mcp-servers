import { createReadStream, existsSync } from "node:fs";
import { readdir, readFile, realpath } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { DatabaseSync } from "node:sqlite";
import { daemonLoadedThreads, daemonQueueList, ensureCodexDaemon, queueAndStart, watchQueuedSubmission, withCodexDaemon, type QueueOutcome, type QueuedMessage } from "./codex-daemon.js";
import { BridgeError } from "./errors.js";
import { listOpenFiles, listPids, readCmdline, readComm, readStartTime, readStdinTty } from "./procfs.js";
import { locateTmuxPane, pasteAndSubmit } from "./tmux.js";
import type { Delivery, OlderWindow, ResolvedSession } from "./types.js";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function codexHome(): string {
  return process.env.CODEX_HOME || join(homedir(), ".codex");
}

/**
 * A brand-new Codex thread (created but never given a first turn, e.g. by the
 * VS Code extension) has a name and a `cwd` recorded in Codex's local state
 * database, but no rollout file yet: Codex only writes that lazily on the
 * first turn. This reads `cwd` from there so such a thread still resolves.
 */
function cwdFromStateDb(home: string, uuid: string): string | null {
  const path = join(home, "state_5.sqlite");
  if (!existsSync(path)) return null;
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return null;
  }
  try {
    const row = db.prepare("select cwd from threads where id = ?").get(uuid) as { cwd?: unknown } | undefined;
    return typeof row?.cwd === "string" && row.cwd ? row.cwd : null;
  } catch {
    return null;
  } finally {
    db.close();
  }
}

/** When Codex last touched the thread, from its own state database, or null. */
function updatedMsFromStateDb(home: string, uuid: string): number | null {
  const path = join(home, "state_5.sqlite");
  if (!existsSync(path)) return null;
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return null;
  }
  try {
    const row = db.prepare("select updated_at_ms, updated_at from threads where id = ?").get(uuid) as
      | { updated_at_ms?: unknown; updated_at?: unknown }
      | undefined;
    if (typeof row?.updated_at_ms === "number") return row.updated_at_ms;
    return typeof row?.updated_at === "number" ? row.updated_at * 1000 : null;
  } catch {
    return null;
  } finally {
    db.close();
  }
}

interface RolloutCandidate {
  uuid: string;
  /** null when the thread has no rollout file yet (no turn has run). */
  path: string | null;
  cwd: string | null;
}

/** Resolves a Codex session reference (name or UUID) to one native session in one folder. */
export async function resolveCodexSession(folder: string, ref: string): Promise<ResolvedSession> {
  const home = codexHome();
  const names = await latestThreadNames(home);

  const candidateUuids = UUID_RE.test(ref)
    ? [ref]
    : [...names.entries()].filter(([, name]) => name === ref).map(([uuid]) => uuid);

  const found: RolloutCandidate[] = [];
  for (const uuid of candidateUuids) {
    const files = await findRolloutsForUuid(join(home, "sessions"), uuid);
    if (files.length > 1) {
      throw new BridgeError("AMBIGUOUS_TRANSCRIPT", `Multiple Codex rollout files found for session ${uuid}`);
    }
    let cwd: string | null;
    let path: string | null;
    if (files.length === 1) {
      path = files[0];
      cwd = (await readSessionMeta(path))?.cwd ?? null;
    } else {
      // No turn has run yet, so no rollout file exists on disk: fall back to
      // the folder Codex recorded when the thread was created.
      path = null;
      cwd = cwdFromStateDb(home, uuid);
      if (cwd === null) continue;
    }
    if (cwd) {
      try {
        cwd = await realpath(cwd);
      } catch {
        // Fall back to the raw string recorded in the transcript or database.
      }
    }
    found.push({ uuid, path, cwd });
  }

  const matching = found.filter((candidate) => candidate.cwd === folder);

  if (matching.length === 0) {
    if (UUID_RE.test(ref) && found.length > 0) {
      const cwds = [...new Set(found.map((candidate) => candidate.cwd ?? "an unknown folder"))].join(", ");
      throw new BridgeError("FOLDER_MISMATCH", `Codex session ${ref} belongs to ${cwds}, not ${folder}`);
    }
    throw new BridgeError("SESSION_NOT_FOUND", `No Codex session named or identified as ${ref} in ${folder}`);
  }

  // Several threads can share a name: the one Codex touched most recently wins
  // (thread ids are time-ordered, which settles any tie).
  const recency = (uuid: string) => updatedMsFromStateDb(home, uuid) ?? 0;
  const uuids = [...new Set(matching.map((candidate) => candidate.uuid))].sort(
    (x, y) => recency(y) - recency(x) || (y > x ? 1 : -1),
  );
  const chosen = matching.find((candidate) => candidate.uuid === uuids[0]) as RolloutCandidate;
  return {
    tool: "codex",
    sessionId: chosen.uuid,
    folder,
    name: names.get(chosen.uuid) ?? null,
    transcriptPath: chosen.path,
    ...(uuids.length > 1 ? { olderSessionIds: uuids.slice(1) } : {}),
  };
}

/**
 * Delivers a message into a live Codex session as if typed into its TUI.
 *
 * 1. A Codex TUI started while the shared app-server daemon runs is attached to
 *    it; the message goes into that thread's input queue through the daemon and
 *    every attached TUI renders it. No terminal access is needed.
 * 2. A Codex TUI started before the daemon existed runs a private in-process
 *    server and owns the thread itself (it holds the rollout and writer lock).
 *    Nothing outside can reach that server; typing into its tmux pane is the
 *    only fallback, otherwise it must be reopened once.
 */
export async function sendToCodex(session: ResolvedSession, message: string): Promise<Delivery> {
  const uuid = session.sessionId;
  const reopen = `codex resume ${uuid}`;

  const owners = (await privateServerOwners(uuid)).sort((x, y) => y.startTime - x.startTime || y.pid - x.pid);
  if (owners.length >= 1) {
    const { pid, tty } = owners[0];
    let located;
    try {
      located = await locateTmuxPane(pid, `Codex session ${uuid}`, reopen);
    } catch (error) {
      if (error instanceof BridgeError && error.code === "NOT_IN_TMUX") {
        throw new BridgeError(
          "TUI_CONTROL_UNAVAILABLE",
          `Codex (pid ${pid} on ${tty}) opened session ${uuid} before the shared Codex app-server was running, so it uses a private server nothing else can reach. The bridge has started the shared server; quit that Codex once and reopen the session (${reopen} in ${session.folder}) and messages will arrive automatically from then on.`,
        );
      }
      throw error;
    }
    await pasteAndSubmit(located.socket, located.pane, message);
    const older = await olderCodexWindows(session, pid, owners.slice(1).map((o) => ({ pid: o.pid, tty: o.tty, session_id: uuid, reason: "duplicate_window" as const })));
    return { transport: "tmux", pid, tty, pane: located.pane, ...(older.length ? { older_windows: older } : {}) };
  }

  await ensureCodexDaemon();
  let queue: QueueOutcome | null = null;
  const delivered = await withCodexDaemon(async (call) => {
    if (!(await daemonLoadedThreads(call)).has(uuid)) return false;
    queue = await queueAndStart(call, uuid, message);
    return true;
  });
  if (!delivered || !queue) {
    throw new BridgeError(
      "TARGET_NOT_RUNNING",
      `Codex session ${uuid} is not open in any Codex window. Open it once (${reopen} in ${session.folder}) and retry.`,
    );
  }
  const outcome: QueueOutcome = queue;
  // A message behind a running turn is rescued if the human interrupts that turn.
  if (outcome.state === "waiting_for_current_turn" && outcome.submission_id) watchQueuedSubmission(uuid, outcome.submission_id);
  const older = await olderCodexWindows(session, null, []);
  return { transport: "codex-app-server", pid: await daemonPid(), tty: null, pane: null, queue: outcome, ...(older.length ? { older_windows: older } : {}) };
}

/**
 * Messages Codex has accepted for the session but not started yet. Read-only,
 * and empty when the shared Codex server is not running.
 */
export async function readCodexQueue(uuid: string): Promise<QueuedMessage[]> {
  try {
    return await withCodexDaemon((call) => daemonQueueList(call, uuid));
  } catch {
    return [];
  }
}

/** Interactive Codex processes that own the thread through their own in-process server. */
async function privateServerOwners(uuid: string): Promise<Array<{ pid: number; tty: string; startTime: number }>> {
  const rolloutSuffix = `-${uuid}.jsonl`;
  const lockName = `${uuid}.lock`;
  const results = await Promise.all(
    (await listPids()).map(async (pid): Promise<{ pid: number; tty: string; startTime: number } | null> => {
      try {
        const [cmdline, comm] = await Promise.all([readCmdline(pid), readComm(pid)]);
        const looksLikeCodex =
          (cmdline !== null && (isCodexExecutable(cmdline[0]) || isCodexExecutable(cmdline[1]))) ||
          (comm !== null && comm.startsWith("codex"));
        if (!looksLikeCodex || cmdline?.includes("app-server")) return null;
        const owns = (await listOpenFiles(pid)).some((file) => {
          const name = basename(file);
          return (name.startsWith("rollout-") && name.endsWith(rolloutSuffix)) ||
            (name === lockName && basename(dirname(file)) === "thread-writer-locks");
        });
        if (!owns) return null;
        const tty = await readStdinTty(pid);
        return tty ? { pid, tty, startTime: Number(await readStartTime(pid)) || 0 } : null;
      } catch {
        return null;
      }
    }),
  );
  return dedupeByTty(results.filter((r): r is { pid: number; tty: string; startTime: number } => r !== null));
}

async function daemonPid(): Promise<number> {
  for (const pid of await listPids()) {
    const cmdline = await readCmdline(pid);
    if (cmdline && cmdline.includes("app-server") && cmdline.some((arg) => arg.startsWith("unix://"))) return pid;
  }
  return 0;
}

/** Interactive `codex resume <uuid>` windows for one thread. */
export async function codexResumeWindows(uuid: string): Promise<Array<{ pid: number; tty: string; startTime: number }>> {
  const windows: Array<{ pid: number; tty: string; startTime: number }> = [];
  for (const pid of await listPids()) {
    const cmdline = await readCmdline(pid);
    if (!cmdline || !(isCodexExecutable(cmdline[0]) || isCodexExecutable(cmdline[1]))) continue;
    const at = cmdline.indexOf("resume");
    if (at < 0 || cmdline[at + 1] !== uuid) continue;
    const tty = await readStdinTty(pid);
    if (tty) windows.push({ pid, tty, startTime: Number(await readStartTime(pid)) || 0 });
  }
  return windows;
}

/** Windows on older same-named threads, and every duplicate window of this thread except the newest. */
async function olderCodexWindows(session: ResolvedSession, deliveredPid: number | null, known: OlderWindow[]): Promise<OlderWindow[]> {
  const found = [...known];
  const mine = (await codexResumeWindows(session.sessionId)).sort((x, y) => y.startTime - x.startTime || y.pid - x.pid);
  for (const window of mine.slice(deliveredPid === null ? 1 : 0)) {
    if (window.pid !== deliveredPid && !found.some((f) => f.pid === window.pid)) {
      found.push({ pid: window.pid, tty: window.tty, session_id: session.sessionId, reason: "duplicate_window" });
    }
  }
  for (const id of session.olderSessionIds ?? []) {
    for (const window of await codexResumeWindows(id)) {
      found.push({ pid: window.pid, tty: window.tty, session_id: id, reason: "older_session_with_same_name" });
    }
  }
  return found;
}

function isCodexExecutable(arg: string | undefined): boolean {
  if (arg === undefined) return false;
  return /^codex(\.js)?$/.test(basename(arg));
}

function dedupeByTty<T extends { pid: number; tty: string }>(items: T[]): T[] {
  const byTty = new Map<string, T>();
  for (const item of items) {
    const existing = byTty.get(item.tty);
    if (!existing || item.pid < existing.pid) byTty.set(item.tty, item);
  }
  return [...byTty.values()];
}

async function findRolloutsForUuid(root: string, uuid: string): Promise<string[]> {
  const found: string[] = [];
  const pattern = new RegExp(`^rollout-.*-${uuid}\\.jsonl$`);
  async function visit(dir: string): Promise<void> {
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const child = join(dir, entry.name);
      if (entry.isDirectory()) await visit(child);
      else if (entry.isFile() && pattern.test(entry.name)) found.push(child);
    }
  }
  await visit(root);
  return found;
}

/** Reads only the first line of a rollout file: the (possibly huge) session_meta record. */
async function readSessionMeta(path: string): Promise<{ cwd: string | null } | null> {
  const input = createReadStream(path, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });
  try {
    for await (const line of rl) {
      if (!line.trim()) continue;
      let record: unknown;
      try {
        record = JSON.parse(line);
      } catch {
        return null;
      }
      if (isObject(record) && record.type === "session_meta" && isObject(record.payload)) {
        const cwd = typeof record.payload.cwd === "string" ? record.payload.cwd : null;
        return { cwd };
      }
      return null;
    }
    return null;
  } catch {
    return null;
  } finally {
    rl.close();
    input.destroy();
  }
}

/** Maps uuid -> latest thread_name from session_index.jsonl (last line for an id wins). */
async function latestThreadNames(home: string): Promise<Map<string, string>> {
  const map = new Map<string, string>();
  let content: string;
  try {
    content = await readFile(join(home, "session_index.jsonl"), "utf8");
  } catch {
    return map;
  }
  for (const line of content.split(/\r?\n/)) {
    if (!line.trim()) continue;
    let record: unknown;
    try {
      record = JSON.parse(line);
    } catch {
      continue;
    }
    if (isObject(record) && typeof record.id === "string" && typeof record.thread_name === "string") {
      map.set(record.id, record.thread_name);
    }
  }
  return map;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
