import { randomBytes } from "node:crypto";
import { unlinkSync } from "node:fs";
import { chmod, mkdir, readdir, readFile, unlink, writeFile } from "node:fs/promises";
import { createServer, type IncomingMessage } from "node:http";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { BridgeError } from "./errors.js";
import { readCmdline, readComm } from "./procfs.js";

/**
 * Claude Code delivery through channels.
 *
 * Every bridge process that Claude Code starts as an MCP server runs a small
 * token-protected loopback receiver and advertises it in a registry keyed by the
 * Claude Code process. A sender posts the message there; the receiver pushes it
 * into its own session as a `notifications/claude/channel` event.
 *
 * Claude Code only accepts those events when the session was started with
 * `--dangerously-load-development-channels server:<name>` (or `--channels`), and
 * it drops them silently otherwise, so senders check the session's command line.
 */

export const CLAUDE_CHANNEL_SERVER_NAME = "codex-opencode-bridge";
const PROTOCOL = 1;
const MAX_BODY_BYTES = 1024 * 1024;

export type ChannelNotifier = (content: string, meta: Record<string, string>) => Promise<void>;

interface ReceiverRegistry {
  protocol: number;
  claudePid: number;
  bridgePid: number;
  serverUrl: string;
  token: string;
}

function runtimeDirectory(): string {
  if (process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR) return process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR;
  const uid = typeof process.getuid === "function" ? process.getuid() : "user";
  return join("/tmp", `codex-opencode-bridge-${uid}`);
}

function claudeConfigDir(): string {
  return process.env.CLAUDE_CONFIG_DIR || join(homedir(), ".claude");
}

/** The configured MCP-server name whose Claude channel we use. */
export function claudeChannelServerName(): string {
  return process.env.CODEX_OPENCODE_BRIDGE_CLAUDE_SERVER_NAME || CLAUDE_CHANNEL_SERVER_NAME;
}

async function parentPid(pid: number): Promise<number | null> {
  try {
    const stat = await readFile(`/proc/${pid}/stat`, "utf8");
    const fields = stat.slice(stat.lastIndexOf(")") + 1).trim().split(/\s+/);
    const ppid = Number(fields[1]);
    return Number.isInteger(ppid) && ppid > 1 ? ppid : null;
  } catch {
    return null;
  }
}

/** The Claude Code process this bridge serves, found through its live session registry. */
export async function findOwningClaudePid(start = process.pid): Promise<number | null> {
  let pid = await parentPid(start);
  for (let depth = 0; pid !== null && depth < 5; depth += 1) {
    try {
      const entry = JSON.parse(await readFile(join(claudeConfigDir(), "sessions", `${pid}.json`), "utf8")) as { pid?: number };
      if (entry.pid === pid) return pid;
    } catch {
      // Not a Claude Code process; keep walking up.
    }
    pid = await parentPid(pid);
  }
  return null;
}

/**
 * Starts the receiver when this bridge runs under Claude Code. Returns false when
 * it does not, so the caller can skip it.
 */
export async function startClaudeChannelReceiver(notify: ChannelNotifier): Promise<boolean> {
  const claudePid = await findOwningClaudePid();
  if (claudePid === null) return false;

  const token = randomBytes(24).toString("hex");
  const server = createServer(async (req, res) => {
    const reply = (status: number, value: unknown) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(value));
    };
    if (req.method !== "POST" || new URL(req.url ?? "/", "http://127.0.0.1").pathname !== "/claude/channel") return reply(404, { error: "not found" });
    if (req.headers["x-bridge-token"] !== token) return reply(403, { error: "forbidden" });
    let body: { content?: unknown; meta?: unknown };
    try {
      body = JSON.parse(await readBody(req));
    } catch {
      return reply(400, { error: "invalid request body" });
    }
    if (typeof body.content !== "string" || !body.content) return reply(400, { error: "content is required" });
    const meta: Record<string, string> = {};
    if (body.meta && typeof body.meta === "object") {
      for (const [key, value] of Object.entries(body.meta as Record<string, unknown>)) {
        if (/^[A-Za-z0-9_]+$/.test(key) && typeof value === "string") meta[key] = value;
      }
    }
    try {
      await notify(body.content, meta);
      reply(200, true);
    } catch (error) {
      reply(502, { error: error instanceof Error ? error.message : String(error) });
    }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  server.unref();

  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  const root = runtimeDirectory();
  await mkdir(root, { recursive: true, mode: 0o700 });
  await chmod(root, 0o700);
  const registryPath = join(root, `claude-channel-${claudePid}-${process.pid}.json`);
  const registry: ReceiverRegistry = { protocol: PROTOCOL, claudePid, bridgePid: process.pid, serverUrl: `http://127.0.0.1:${port}/`, token };
  await writeFile(registryPath, JSON.stringify(registry), { mode: 0o600 });
  const cleanup = () => { try { unlinkSync(registryPath); } catch { /* already gone */ } };
  process.once("exit", cleanup);
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"] as const) {
    process.once(signal, () => { cleanup(); process.exit(0); });
  }
  return true;
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("payload too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

/** Whether a Claude Code command line enables this bridge as a channel. */
export function channelEnabledInCmdline(argv: string[], name = claudeChannelServerName()): boolean {
  const wanted = new Set([`server:${name}`]);
  let collecting = false;
  for (const arg of argv) {
    if (arg === "--dangerously-load-development-channels" || arg === "--channels") {
      collecting = true;
      continue;
    }
    const inline = /^--(?:dangerously-load-development-channels|channels)=(.*)$/.exec(arg);
    if (inline) {
      if (inline[1].split(/[\s,]+/).some((entry) => wanted.has(entry))) return true;
      collecting = false;
      continue;
    }
    if (arg.startsWith("-")) {
      collecting = false;
      continue;
    }
    if (collecting && arg.split(/[\s,]+/).some((entry) => wanted.has(entry))) return true;
  }
  return false;
}

/** Live receivers registered for a Claude Code process, newest bridge first. */
async function receiversFor(claudePid: number): Promise<ReceiverRegistry[]> {
  const root = runtimeDirectory();
  let names: string[];
  try {
    names = await readdir(root);
  } catch {
    return [];
  }
  const prefix = `claude-channel-${claudePid}-`;
  const found: ReceiverRegistry[] = [];
  for (const name of names) {
    if (!name.startsWith(prefix) || !name.endsWith(".json")) continue;
    try {
      const entry = JSON.parse(await readFile(join(root, name), "utf8")) as ReceiverRegistry;
      if (entry.claudePid !== claudePid || entry.protocol !== PROTOCOL) continue;
      if ((await readCmdline(entry.bridgePid)) === null) {
        await unlink(join(root, name)).catch(() => {});
        continue;
      }
      found.push(entry);
    } catch {
      // Unreadable or half-written entry.
    }
  }
  return found.sort((a, b) => b.bridgePid - a.bridgePid);
}

/** Describes who is sending, for the `<channel>` tag attributes. */
export async function senderMeta(): Promise<Record<string, string>> {
  const meta: Record<string, string> = { from_folder: process.cwd() };
  let pid = await parentPid(process.pid);
  for (let depth = 0; pid !== null && depth < 5; depth += 1) {
    const argv0 = basename((await readCmdline(pid))?.[0] ?? "");
    const comm = (await readComm(pid)) ?? "";
    const tool = ["opencode", "codex", "claude"].find((name) => argv0 === name || comm.startsWith(name));
    if (tool) {
      meta.from = tool;
      break;
    }
    pid = await parentPid(pid);
  }
  return meta;
}

export type ChannelOutcome = "delivered" | "no-receiver" | "not-enabled";

/**
 * Pushes `content` into the Claude Code process `claudePid` through its bridge
 * receiver. Returns why it could not, so the caller can choose a fallback.
 */
export async function pushToClaudeChannel(claudePid: number, content: string): Promise<ChannelOutcome> {
  const receivers = await receiversFor(claudePid);
  if (receivers.length === 0) return "no-receiver";
  if (!channelEnabledInCmdline((await readCmdline(claudePid)) ?? [])) return "not-enabled";

  const meta = await senderMeta();
  let lastError: unknown = null;
  for (const receiver of receivers) {
    try {
      const response = await fetch(new URL("/claude/channel", receiver.serverUrl), {
        method: "POST",
        headers: { "content-type": "application/json", "x-bridge-token": receiver.token },
        body: JSON.stringify({ content, meta }),
        signal: AbortSignal.timeout(10_000),
      });
      if (response.ok) return "delivered";
      lastError = new Error(`HTTP ${response.status}: ${(await response.text().catch(() => "")).slice(0, 300)}`);
    } catch (error) {
      lastError = error;
    }
  }
  throw new BridgeError(
    "COMMAND_FAILED",
    `Could not push into Claude Code (pid ${claudePid}) through its bridge: ${lastError instanceof Error ? lastError.message : String(lastError)}`,
  );
}
