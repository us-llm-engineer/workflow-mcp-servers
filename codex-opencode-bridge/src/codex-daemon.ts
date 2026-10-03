import { randomUUID } from "node:crypto";
import { stat } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import WebSocket from "ws";
import { BridgeError } from "./errors.js";
import { execFile } from "./exec.js";

/**
 * Client for the shared Codex app-server daemon (`codex app-server daemon start`).
 *
 * A Codex TUI started while the daemon runs attaches to it instead of running a
 * private in-process server. Other local processes can then add user input to
 * that TUI's thread through the daemon's control socket, which speaks JSON-RPC
 * over WebSocket on a Unix socket. Verified against Codex CLI 0.154.
 */

const RPC_TIMEOUT_MS = 10_000;

function codexHome(): string {
  return process.env.CODEX_HOME || join(homedir(), ".codex");
}

function codexBin(): string {
  return process.env.CODEX_BIN || "codex";
}

export function codexDaemonSocketPath(): string {
  return join(codexHome(), "app-server-control", "app-server-control.sock");
}

async function socketExists(): Promise<boolean> {
  try {
    return (await stat(codexDaemonSocketPath())).isSocket();
  } catch {
    return false;
  }
}

/**
 * Starts the daemon unless it already answers. Safe to call repeatedly:
 * `codex app-server daemon start` is a no-op when the daemon is running.
 */
export async function ensureCodexDaemon(): Promise<void> {
  if (await socketExists() && await daemonAnswers()) return;
  if (process.env.CODEX_OPENCODE_BRIDGE_NO_CODEX_DAEMON_START === "1") {
    throw new BridgeError("TARGET_NOT_RUNNING", `The shared Codex app-server daemon is not running (no socket at ${codexDaemonSocketPath()})`);
  }
  await execFile(codexBin(), ["app-server", "daemon", "start"], { timeoutMs: 60_000, maxOutputBytes: 1024 * 1024 });
  if (!(await socketExists())) {
    throw new BridgeError("TARGET_NOT_RUNNING", `codex app-server daemon start did not create ${codexDaemonSocketPath()}`);
  }
}

async function daemonAnswers(): Promise<boolean> {
  try {
    await withCodexDaemon(async () => undefined);
    return true;
  } catch {
    return false;
  }
}

export type DaemonCall = (method: string, params: unknown) => Promise<unknown>;

/** Opens one initialized control connection, runs `work`, and always closes it. */
export async function withCodexDaemon<T>(work: (call: DaemonCall) => Promise<T>): Promise<T> {
  // permessage-deflate must be off: the daemon rejects the extension header.
  const ws = new WebSocket(`ws+unix://${codexDaemonSocketPath()}:/`, { perMessageDeflate: false, handshakeTimeout: 5_000 });
  const pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }>();
  let nextId = 1;
  let closedError: Error | null = null;

  const failAll = (error: Error) => {
    closedError ??= error;
    for (const { reject, timer } of pending.values()) {
      clearTimeout(timer);
      reject(error);
    }
    pending.clear();
  };

  ws.on("message", (data) => {
    let message: { id?: number; result?: unknown; error?: { message?: string } };
    try {
      message = JSON.parse(data.toString());
    } catch {
      return;
    }
    if (typeof message.id !== "number") return; // notifications are not needed
    const entry = pending.get(message.id);
    if (!entry) return;
    pending.delete(message.id);
    clearTimeout(entry.timer);
    if (message.error) entry.reject(new BridgeError("COMMAND_FAILED", `Codex app-server: ${message.error.message ?? "request failed"}`));
    else entry.resolve(message.result);
  });
  ws.on("close", () => failAll(new BridgeError("COMMAND_FAILED", "Codex app-server closed the control connection")));
  ws.on("error", (error) => failAll(new BridgeError("TARGET_NOT_RUNNING", `Cannot reach the Codex app-server daemon: ${error.message}`)));

  await new Promise<void>((resolve, reject) => {
    ws.once("open", () => resolve());
    ws.once("error", (error) => reject(new BridgeError("TARGET_NOT_RUNNING", `Cannot reach the Codex app-server daemon: ${error.message}`)));
  });

  const call: DaemonCall = (method, params) => new Promise((resolve, reject) => {
    if (closedError) return reject(closedError);
    const id = nextId++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new BridgeError("COMMAND_TIMEOUT", `Codex app-server did not answer ${method} within ${RPC_TIMEOUT_MS / 1000} s`));
    }, RPC_TIMEOUT_MS);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params }));
  });

  try {
    await call("initialize", {
      clientInfo: { name: "codex-opencode-bridge", title: "codex-opencode-bridge", version: "0.1.0" },
      capabilities: { experimentalApi: true },
    });
    ws.send(JSON.stringify({ method: "initialized" }));
    return await work(call);
  } finally {
    ws.close();
  }
}

/** Thread ids the daemon currently has loaded (open in a TUI now or earlier in its lifetime). */
export async function daemonLoadedThreads(call: DaemonCall): Promise<Set<string>> {
  const ids = new Set<string>();
  let cursor: string | null = null;
  do {
    const page = await call("thread/loaded/list", cursor ? { cursor } : {}) as { data?: string[]; nextCursor?: string | null };
    for (const id of page.data ?? []) ids.add(id);
    cursor = page.nextCursor ?? null;
  } while (cursor);
  return ids;
}

/**
 * Adds a user message to the thread's input queue, exactly like pressing Enter in
 * the Codex TUI: it starts a turn when the agent is idle and waits for the
 * current turn otherwise. Every attached TUI renders it live.
 */
export async function daemonQueueUserMessage(call: DaemonCall, threadId: string, text: string): Promise<string | null> {
  const result = await call("thread/queue/add", {
    threadId,
    clientUserMessageId: randomUUID(),
    input: [{ type: "text", text, text_elements: [] }],
  }) as { queuedSubmission?: { id?: string } };
  return result?.queuedSubmission?.id ?? null;
}

/** A message waiting in a thread's Codex queue. */
export interface QueuedMessage {
  id: string;
  text: string;
}

/** Thread state as the daemon reports it: "idle", "active", and so on. */
export async function daemonThreadStatus(call: DaemonCall, threadId: string): Promise<string> {
  const result = await call("thread/read", { threadId, includeTurns: false }) as { thread?: { status?: { type?: string } } };
  return result?.thread?.status?.type ?? "unknown";
}

/** Messages Codex has accepted for the thread but not started yet, oldest first. */
export async function daemonQueueList(call: DaemonCall, threadId: string): Promise<QueuedMessage[]> {
  const items: QueuedMessage[] = [];
  let cursor: string | null = null;
  do {
    const page = await call("thread/queue/list", { threadId, limit: 200, ...(cursor ? { cursor } : {}) }) as {
      data?: Array<{ id?: string; input?: Array<{ type?: string; text?: string }> }>;
      nextCursor?: string | null;
    };
    for (const entry of page.data ?? []) {
      if (typeof entry.id !== "string") continue;
      const text = (entry.input ?? []).filter((part) => part.type === "text").map((part) => part.text ?? "").join("");
      items.push({ id: entry.id, text });
    }
    cursor = page.nextCursor ?? null;
  } while (cursor);
  return items;
}

/**
 * Starts one queued message now. Codex refuses while a turn is running or about
 * to start, which is harmless here: it means the queue is already moving.
 */
async function daemonStartQueued(call: DaemonCall, threadId: string, submissionId: string): Promise<{ started: boolean; error?: string }> {
  try {
    await call("thread/queue/start", { threadId, queuedSubmissionId: submissionId });
    return { started: true };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return /active or pending turn/i.test(message) ? { started: false } : { started: false, error: message };
  }
}

export type QueueState = "started" | "waiting_for_current_turn" | "queued_not_started";

export interface QueueOutcome {
  state: QueueState;
  submission_id: string | null;
  /** Messages that were already queued before this one. */
  ahead: number;
  error?: string;
}

/**
 * Queues a user message and makes sure it actually runs.
 *
 * Codex drains its queue by itself when a turn finishes, but stops draining once
 * the human interrupts a turn: an idle thread then holds queued messages forever
 * and nothing tells anyone. So when the thread is idle and the message is still
 * queued, start it. When a turn is running, the caller should watch it.
 */
export async function queueAndStart(call: DaemonCall, threadId: string, text: string): Promise<QueueOutcome> {
  const ahead = (await daemonQueueList(call, threadId)).length;
  const submissionId = await daemonQueueUserMessage(call, threadId, text);
  if (!submissionId) {
    return { state: "queued_not_started", submission_id: null, ahead, error: "Codex did not return a queue id for the message" };
  }
  const stillQueued = async () => (await daemonQueueList(call, threadId)).some((item) => item.id === submissionId);

  if (!(await stillQueued())) return { state: "started", submission_id: submissionId, ahead };
  if ((await daemonThreadStatus(call, threadId)) === "active") {
    return { state: "waiting_for_current_turn", submission_id: submissionId, ahead };
  }
  const start = await daemonStartQueued(call, threadId, submissionId);
  if (start.started) return { state: "started", submission_id: submissionId, ahead };
  if (start.error) return { state: "queued_not_started", submission_id: submissionId, ahead, error: start.error };
  // A turn began between the checks, so the message is either running or next in line.
  return { state: (await stillQueued()) ? "waiting_for_current_turn" : "started", submission_id: submissionId, ahead };
}

const watching = new Set<string>();

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms).unref());
}

/**
 * Rescues a message queued behind a running turn. If the human interrupts that
 * turn, Codex stops draining its queue, so once the thread is idle with the
 * message still queued, start it. Ends when the message leaves the queue.
 */
export function watchQueuedSubmission(threadId: string, submissionId: string): void {
  if (watching.has(submissionId)) return;
  watching.add(submissionId);
  const pollMs = Number(process.env.CODEX_OPENCODE_BRIDGE_QUEUE_POLL_MS) || 2_000;
  const maxMs = Number(process.env.CODEX_OPENCODE_BRIDGE_QUEUE_WATCH_MS) || 30 * 60_000;
  void (async () => {
    const deadline = Date.now() + maxMs;
    try {
      await withCodexDaemon(async (call) => {
        while (Date.now() < deadline) {
          await sleep(pollMs);
          if (!(await daemonQueueList(call, threadId)).some((item) => item.id === submissionId)) return;
          if ((await daemonThreadStatus(call, threadId)) === "idle") await daemonStartQueued(call, threadId, submissionId);
        }
      });
    } catch {
      // The daemon went away; there is nothing left to rescue.
    } finally {
      watching.delete(submissionId);
    }
  })();
}
