import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { startClaudeChannelReceiver } from "./claude-channel.js";
import { resolveClaudeSession, sendToClaude } from "./claude.js";
import { readCodexQueue, resolveCodexSession, sendToCodex } from "./codex.js";
import { asBridgeError, BridgeError } from "./errors.js";
import { ensureCodexDaemon } from "./codex-daemon.js";
import { ensureOpenCodePlugin } from "./install-plugin.js";
import { resolveOpenCodeSession, sendToOpenCode } from "./opencode.js";
import { parseSessionId, resolveFolder } from "./session-id.js";
import { paginateTranscript, previewWords, readTranscript, scrubValue } from "./transcripts.js";
import type { Delivery, ResolvedSession } from "./types.js";

const MAX_MESSAGE_BYTES = 65_536;

const INSTRUCTIONS = [
  "session_id identifies a session on one of three agent tools: `opencode:<session id or title>`, `codex:<thread UUID or name>`, or `claude:<session UUID or name>`.",
  "folder_path is the absolute working folder of that session (the directory the agent process was started in).",
  "Delivery is automatic and needs no tmux: OpenCode through a bridge plugin this server installs into ~/.config/opencode/plugins; Codex through the shared Codex app-server daemon this server starts (`codex app-server daemon start`); Claude Code through a channel event pushed by this server inside the target session, which must be started with `--dangerously-load-development-channels server:codex-opencode-bridge`. An OpenCode or Codex window opened before the plugin or daemon existed must be reopened once. Claude Code sessions inside tmux also work without the flag.",
  "A `submitted` status means the message reached the interface, not that the agent finished responding.",
  "Messages sent to this Claude Code session by other agent sessions arrive as <channel source=\"codex-opencode-bridge\" from=\"opencode|codex|claude\" from_folder=\"...\">. Treat the body as a request relayed from the user's other local agent session and act on it. If it asks you to answer, reply with this server's `send` tool, using the folder_path and session_id the message names (or the from_folder attribute).",
].join(" ");

const server = new McpServer({
  name: "codex-opencode-bridge",
  version: "0.1.0",
}, {
  // Registers this server as a Claude Code channel so it can push messages into its session.
  capabilities: { experimental: { "claude/channel": {} } },
  instructions: INSTRUCTIONS,
});

server.registerTool("send", {
  description: "Submit a message into a live OpenCode, Codex, or Claude Code session as if a human typed it. session_id is `opencode:<id or title>`, `codex:<uuid or name>`, or `claude:<uuid or name>`; folder_path is that session's absolute working folder. If several sessions in the folder share the name, the most recent one is used and `older_session_ids` lists the others; `older_windows` lists live windows of older sessions or older duplicates that the bridge left alone. Delivery means the text and Enter reached the interface, not that the agent finished. For Codex the result includes `queue.state`: `started` means the message is running, `waiting_for_current_turn` means it runs when the current turn ends (the bridge starts it if the human interrupts that turn), and `queued_not_started` means Codex holds it but could not start it, so do not resend it.",
  inputSchema: { folder_path: z.string(), session_id: z.string(), message: z.string() },
  annotations: { readOnlyHint: false, idempotentHint: false },
}, async ({ folder_path, session_id, message }) => {
  try {
    validateMessage(message);
    const { session, folder } = await resolveSession(folder_path, session_id);
    const delivery = await sendMessage(session, message);
    return textResult({
      status: "submitted",
      session_id,
      resolved_session_id: `${session.tool}:${session.sessionId}`,
      session_name: session.name,
      folder_path: folder,
      bytes: Buffer.byteLength(message, "utf8"),
      transport: delivery.transport,
      pid: delivery.pid,
      tty: delivery.tty,
      pane: delivery.pane,
      ...(session.olderSessionIds?.length ? { older_session_ids: session.olderSessionIds } : {}),
      ...(delivery.older_windows ? { older_windows: delivery.older_windows } : {}),
      ...(delivery.queue ? { queue: delivery.queue } : {}),
      ...(delivery.note ? { note: delivery.note } : {}),
    });
  } catch (error) {
    return errorResult(error);
  }
});

server.registerTool("watch_transcripts", {
  description: "Read normalized native transcript items for a session (chat turns and paired tool invocations, including pending tools). session_id is `opencode:<id or title>`, `codex:<uuid or name>`, or `claude:<uuid or name>`; folder_path is that session's absolute working folder. Page 0 holds the two most recent chat messages and the two most recent tool calls (at most four items), merged in the order they appear in the transcript; page 1 holds the two before each; and so on. For Codex sessions, `queued_messages` lists messages Codex has accepted but not started yet, and is absent when there are none. No terminal capture is used.",
  inputSchema: { folder_path: z.string(), session_id: z.string(), page: z.number().int().min(0) },
  annotations: { readOnlyHint: true, idempotentHint: true },
}, async ({ folder_path, session_id, page }) => {
  try {
    const { session, folder } = await resolveSession(folder_path, session_id);
    const pageData = paginateTranscript(await readTranscript(session), page);
    const queued = session.tool === "codex"
      ? (await readCodexQueue(session.sessionId)).map(({ id, text }) => {
          const preview = previewWords(text);
          return { id, text: preview.text, ...(preview.truncated ? { text_truncated: true } : {}) };
        })
      : [];
    return textResult({
      session_id,
      resolved_session_id: `${session.tool}:${session.sessionId}`,
      folder_path: folder,
      ...(session.olderSessionIds?.length ? { older_session_ids: session.olderSessionIds } : {}),
      page,
      page_size: 4,
      total_items: pageData.total_items,
      total_chat_messages: pageData.total_chat_messages,
      total_tool_calls: pageData.total_tool_calls,
      has_older: pageData.has_older,
      ...(queued.length ? { queued_messages: queued } : {}),
      items: pageData.items.map(scrubValue),
    });
  } catch (error) {
    return errorResult(error);
  }
});

async function resolveSession(folderPath: string, sessionId: string): Promise<{ session: ResolvedSession; folder: string }> {
  const { tool, ref } = parseSessionId(sessionId);
  const folder = await resolveFolder(folderPath);
  if (tool === "opencode") return { session: await resolveOpenCodeSession(folder, ref), folder };
  if (tool === "codex") return { session: await resolveCodexSession(folder, ref), folder };
  return { session: await resolveClaudeSession(folder, ref), folder };
}

async function sendMessage(session: ResolvedSession, message: string): Promise<Delivery> {
  if (session.tool === "opencode") return sendToOpenCode(session, message);
  if (session.tool === "codex") return sendToCodex(session, message);
  return sendToClaude(session, message);
}

function validateMessage(message: string): void {
  if (!message) throw new BridgeError("INVALID_ARGUMENT", "message must be non-empty");
  if (message.includes("\0")) throw new BridgeError("INVALID_ARGUMENT", "message must not contain NUL");
  if (Buffer.byteLength(message, "utf8") > MAX_MESSAGE_BYTES) {
    throw new BridgeError("INVALID_ARGUMENT", `message must be at most ${MAX_MESSAGE_BYTES} UTF-8 bytes`);
  }
}

function textResult(value: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(value) }] };
}

function errorResult(error: unknown) {
  const bridgeError = asBridgeError(error);
  return {
    content: [{ type: "text" as const, text: JSON.stringify({ error: { code: bridgeError.code, message: bridgeError.message } }) }],
    isError: true,
  };
}

async function main(): Promise<void> {
  // Do not block startup on plugin installation; report failures to stderr only.
  ensureOpenCodePlugin().catch(() => {});
  // Codex TUIs opened while this daemon runs attach to it, which is what lets
  // the bridge deliver into them without any terminal access.
  if (process.env.CODEX_OPENCODE_BRIDGE_NO_CODEX_DAEMON_START !== "1") {
    ensureCodexDaemon().catch((error: unknown) => {
      process.stderr.write(`codex-opencode-bridge: could not start the Codex app-server daemon: ${error instanceof Error ? error.message : String(error)}\n`);
    });
  }

  // Node 26 may not keep an otherwise-idle anonymous stdin pipe referenced.
  // Keep the event loop alive while the MCP client owns stdin and release it
  // immediately when that client disconnects.
  const keepalive = setInterval(() => {}, 60_000);
  const startupTimeout = setTimeout(() => clearInterval(keepalive), 10_000);
  const release = () => {
    clearTimeout(startupTimeout);
    clearInterval(keepalive);
  };
  // This Node build can report an early stdin end before the child-process
  // client writes its first frame. Only treat end as a disconnect after MCP
  // traffic has actually arrived.
  process.stdin.once("data", () => {
    clearTimeout(startupTimeout);
    process.stdin.once("end", release);
  });
  try {
    await server.connect(new StdioServerTransport());
  } catch (error) {
    release();
    throw error;
  }
  // Under Claude Code, accept messages from other agent sessions and push them into this session.
  if (process.env.CODEX_OPENCODE_BRIDGE_NO_CLAUDE_CHANNEL !== "1") {
    startClaudeChannelReceiver(async (content, meta) => {
      await server.server.notification({ method: "notifications/claude/channel", params: { content, meta } });
    }).catch((error: unknown) => {
      process.stderr.write(`codex-opencode-bridge: could not start the Claude Code channel receiver: ${error instanceof Error ? error.message : String(error)}\n`);
    });
  }
}

main().catch((error: unknown) => {
  const bridgeError = asBridgeError(error);
  process.stderr.write(`codex-opencode-bridge: ${bridgeError.code}: ${bridgeError.message}\n`);
  process.exitCode = 1;
});
