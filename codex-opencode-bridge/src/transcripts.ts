import { createReadStream } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { BridgeError } from "./errors.js";
import { execFile } from "./exec.js";
import type { ResolvedSession, TranscriptItem } from "./types.js";

const invocationTypes = new Set(["custom_tool_call", "function_call", "local_shell_call", "mcp_tool_call"]);
const outputTypes = new Set(["custom_tool_call_output", "function_call_output", "local_shell_call_output", "mcp_tool_call_output"]);

function openCodeBin(): string {
  return process.env.OPENCODE_BIN || "opencode";
}

/** Reads and normalizes the native transcript for a resolved session. */
export async function readTranscript(session: ResolvedSession): Promise<TranscriptItem[]> {
  if (session.tool === "codex") {
    if (!session.transcriptPath) {
      throw new BridgeError("TRANSCRIPT_NOT_FOUND", `No transcript file is known for Codex session ${session.sessionId}`);
    }
    return parseCodexJsonl(session.transcriptPath);
  }
  if (session.tool === "claude") {
    if (!session.transcriptPath) {
      throw new BridgeError("TRANSCRIPT_NOT_FOUND", `No transcript file is known for Claude Code session ${session.sessionId}`);
    }
    return parseClaudeJsonl(session.transcriptPath);
  }
  return readOpenCodeTranscript(session.sessionId);
}

export async function parseCodexJsonl(path: string): Promise<TranscriptItem[]> {
  const items: TranscriptItem[] = [];
  const calls = new Map<string, Extract<TranscriptItem, { kind: "tool" }>>();
  const input = createReadStream(path, { encoding: "utf8" });
  const lines = createInterface({ input, crlfDelay: Infinity });
  let lineNumber = 0;
  let invalidJsonLine: number | null = null;
  try {
    for await (const line of lines) {
      lineNumber += 1;
      if (!line.trim()) continue;
      if (invalidJsonLine !== null) malformed(invalidJsonLine, "invalid JSON before the final line");
      let record: unknown;
      try { record = JSON.parse(line); }
      catch {
        // A broken final write is normal while an agent is still running.
        invalidJsonLine = lineNumber;
        continue;
      }
      consumeCodexRecord(record, items, calls, lineNumber);
    }
  } catch (error) {
    if (error instanceof BridgeError) throw error;
    const message = error instanceof Error ? error.message : "unknown error";
    throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", `Could not read Codex transcript: ${message}`);
  }
  return items;
}

function consumeCodexRecord(
  record: unknown,
  items: TranscriptItem[],
  calls: Map<string, Extract<TranscriptItem, { kind: "tool" }>>,
  lineNumber: number,
): void {
  if (!isObject(record) || record.type !== "response_item" || !isObject(record.payload)) return;
  const payload = record.payload;
  if (payload.type === "message" && (payload.role === "user" || payload.role === "assistant")) {
    if (!Array.isArray(payload.content)) malformed(lineNumber, "message content must be an array");
    const text: string[] = [];
    for (const part of payload.content) {
      if (!isObject(part)) malformed(lineNumber, "message content item must be an object");
      if (part.type === "input_text" || part.type === "output_text") {
        if (typeof part.text !== "string") malformed(lineNumber, "message text must be a string");
        text.push(scrubText(part.text));
      }
    }
    if (text.length) items.push({ kind: "chat", role: payload.role, text: text.join("\n") });
    return;
  }
  if (typeof payload.type !== "string") return;
  if (invocationTypes.has(payload.type)) {
    const callId = getCallId(payload);
    const name = stringAt(payload, ["name", "tool", "tool_name"]);
    if (!callId || !name) malformed(lineNumber, "tool call requires call_id and name");
    if (calls.has(callId)) malformed(lineNumber, `duplicate tool call ${callId}`);
    const item: Extract<TranscriptItem, { kind: "tool" }> = {
      kind: "tool", name, input: scrubValue(toolInput(payload)), status: "pending", output_preview: null, output_truncated: false,
    };
    calls.set(callId, item);
    items.push(item);
  } else if (outputTypes.has(payload.type)) {
    const callId = getCallId(payload);
    if (!callId) malformed(lineNumber, "tool output requires call_id");
    const item = calls.get(callId);
    if (!item) return; // Older records can be outside the retained rollout segment.
    const output = toolOutput(payload);
    const preview = outputPreview(output);
    item.status = inferStatus(payload, output);
    item.output_preview = preview.text;
    item.output_truncated = preview.truncated;
  }
}

export async function parseClaudeJsonl(path: string): Promise<TranscriptItem[]> {
  const items: TranscriptItem[] = [];
  const calls = new Map<string, Extract<TranscriptItem, { kind: "tool" }>>();
  const input = createReadStream(path, { encoding: "utf8" });
  const lines = createInterface({ input, crlfDelay: Infinity });
  let lineNumber = 0;
  let invalidJsonLine: number | null = null;
  try {
    for await (const line of lines) {
      lineNumber += 1;
      if (!line.trim()) continue;
      if (invalidJsonLine !== null) malformed(invalidJsonLine, "invalid JSON before the final line");
      let record: unknown;
      try { record = JSON.parse(line); }
      catch {
        // A broken final write is normal while an agent is still running.
        invalidJsonLine = lineNumber;
        continue;
      }
      consumeClaudeRecord(record, items, calls, lineNumber);
    }
  } catch (error) {
    if (error instanceof BridgeError) throw error;
    const message = error instanceof Error ? error.message : "unknown error";
    throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", `Could not read Claude transcript: ${message}`);
  }
  return items;
}

function consumeClaudeRecord(
  record: unknown,
  items: TranscriptItem[],
  calls: Map<string, Extract<TranscriptItem, { kind: "tool" }>>,
  lineNumber: number,
): void {
  if (!isObject(record)) return;
  if (record.isSidechain === true || record.isMeta === true) return;
  if (record.type !== "user" && record.type !== "assistant") return;
  if (!isObject(record.message)) malformed(lineNumber, "message must be an object");
  const role = record.type;
  const content = record.message.content;

  if (role === "user") {
    if (typeof content === "string") {
      const text = scrubText(content);
      if (text.trim()) items.push({ kind: "chat", role: "user", text });
      return;
    }
    if (!Array.isArray(content)) malformed(lineNumber, "user message content must be a string or array");
    for (const part of content) {
      if (!isObject(part)) malformed(lineNumber, "user content item must be an object");
      if (part.type === "text") {
        if (typeof part.text !== "string") malformed(lineNumber, "text block must have string text");
        const text = scrubText(part.text);
        if (text.trim()) items.push({ kind: "chat", role: "user", text });
      } else if (part.type === "tool_result") {
        const toolUseId = stringAt(part, ["tool_use_id"]);
        if (!toolUseId) malformed(lineNumber, "tool_result requires tool_use_id");
        const item = calls.get(toolUseId);
        if (!item) continue; // Unknown tool_use_id: ignore.
        const preview = outputPreview(claudeToolResultText(part.content));
        item.status = part.is_error === true ? "error" : "completed";
        item.output_preview = preview.text;
        item.output_truncated = preview.truncated;
      }
      // Other block types are ignored.
    }
    return;
  }

  // assistant
  if (!Array.isArray(content)) malformed(lineNumber, "assistant message content must be an array");
  for (const part of content) {
    if (!isObject(part)) malformed(lineNumber, "assistant content item must be an object");
    if (part.type === "text") {
      if (typeof part.text !== "string") malformed(lineNumber, "text block must have string text");
      const text = scrubText(part.text);
      if (text.trim()) items.push({ kind: "chat", role: "assistant", text });
    } else if (part.type === "tool_use") {
      const id = stringAt(part, ["id"]);
      const name = stringAt(part, ["name"]);
      if (!id || !name) malformed(lineNumber, "tool_use requires id and name");
      if (calls.has(id)) malformed(lineNumber, `duplicate tool_use ${id}`);
      const item: Extract<TranscriptItem, { kind: "tool" }> = {
        kind: "tool", name, input: scrubValue(part.input ?? null), status: "pending", output_preview: null, output_truncated: false,
      };
      calls.set(id, item);
      items.push(item);
    }
    // `thinking` and other block types are ignored.
  }
}

function claudeToolResultText(content: unknown): string | null {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const texts = content
      .filter((block): block is { type: string; text: string } => isObject(block) && block.type === "text" && typeof block.text === "string")
      .map((block) => block.text);
    return texts.length ? texts.join("\n") : null;
  }
  return null;
}

async function readOpenCodeTranscript(sessionId: string): Promise<TranscriptItem[]> {
  const root = await exportOpenCodeJson(sessionId);
  const messages = Array.isArray(root) ? root : isObject(root) && Array.isArray(root.messages) ? root.messages : null;
  if (!messages) throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", "opencode export has no messages array");
  const items: TranscriptItem[] = [];
  for (const message of messages) {
    if (!isObject(message) || !isObject(message.info) || !Array.isArray(message.parts)) {
      throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", "opencode message must contain info and parts");
    }
    const role = message.info.role;
    for (const part of message.parts) {
      if (!isObject(part)) throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", "opencode part must be an object");
      if (part.type === "text") {
        if (role !== "user" && role !== "assistant") continue;
        if (typeof part.text !== "string") throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", "opencode text part has no text");
        items.push({ kind: "chat", role, text: scrubText(part.text) });
      } else if (part.type === "tool" || (typeof part.tool === "string" && ("state" in part || "input" in part))) {
        const name = stringAt(part, ["name", "tool"]);
        if (!name) throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", "opencode tool part has no name");
        const state = isObject(part.state) ? part.state : {};
        const input = "input" in state ? state.input : part.input;
        const output = "output" in state ? state.output : part.output;
        const error = "error" in state ? state.error : part.error;
        const preview = outputPreview(error ?? output);
        const statusValue = stringAt(state, ["status"]) || stringAt(part, ["status"]);
        items.push({
          kind: "tool", name, input: scrubValue(input ?? null),
          status: statusValue || (error !== undefined && error !== null ? "error" : output === undefined ? "pending" : "completed"),
          output_preview: preview.text, output_truncated: preview.truncated,
        });
      }
    }
  }
  return items;
}

/** Reads an OpenCode export through a regular file to avoid its large-pipe truncation bug. */
export async function exportOpenCodeJson(sessionId: string): Promise<unknown> {
  return readOpenCodeExport(sessionId, async () => {
    const directory = await mkdtemp(join(tmpdir(), "codex-opencode-bridge-export-"));
    const outputPath = join(directory, "export.json");
    try {
      const result = await execFile(openCodeBin(), ["export", sessionId], {
        timeoutMs: 30_000,
        maxOutputBytes: 16 * 1024 * 1024,
        stdoutPath: outputPath,
      });
      return result.stdout;
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
}

/**
 * Some OpenCode invocations or wrappers prefix the JSON document with this
 * one-line progress banner. Accept that known framing, but do not silently
 * discard arbitrary non-JSON output.
 */
export function parseOpenCodeExport(output: string, sessionId: string): unknown {
  const banner = `Exporting session: ${sessionId}`;
  let json = output.replace(/^\uFEFF/, "");
  if (json.startsWith(banner)) {
    const newline = json.slice(banner.length).match(/^\r?\n/);
    if (!newline) {
      throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", "opencode export did not return JSON");
    }
    json = json.slice(banner.length + newline[0].length);
  }
  try { return JSON.parse(json); }
  catch { throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", "opencode export did not return JSON"); }
}

/**
 * An active OpenCode session can change while `opencode export` serializes it.
 * The CLI may then exit successfully with an incomplete JSON document, so retry
 * only that transient parse failure; command and schema failures still surface.
 */
export async function readOpenCodeExport(
  sessionId: string,
  readExport: () => Promise<string>,
  retryDelayMs = 100,
): Promise<unknown> {
  let failure: BridgeError | null = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const output = await readExport();
    try {
      return parseOpenCodeExport(output, sessionId);
    } catch (error) {
      if (!(error instanceof BridgeError) || error.code !== "TRANSCRIPT_SCHEMA_UNSUPPORTED") throw error;
      failure = error;
      if (attempt < 2) await new Promise<void>((resolve) => setTimeout(resolve, retryDelayMs));
    }
  }
  throw failure ?? new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", "opencode export did not return JSON");
}

function getCallId(value: Record<string, unknown>): string | null { return stringAt(value, ["call_id", "callId", "id"]); }
function stringAt(value: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) if (typeof value[key] === "string" && value[key]) return value[key] as string;
  return null;
}
function toolInput(value: Record<string, unknown>): unknown {
  const raw = value.arguments ?? value.input ?? value.parameters ?? null;
  if (typeof raw !== "string") return raw;
  try { return JSON.parse(raw) as unknown; } catch { return raw; }
}
function toolOutput(value: Record<string, unknown>): unknown { return value.output ?? value.content ?? value.result ?? null; }
function inferStatus(value: Record<string, unknown>, output: unknown): string {
  const status = stringAt(value, ["status"]);
  return status || (value.error !== undefined ? "error" : output === null ? "completed" : "completed");
}
function malformed(line: number, detail: string): never {
  throw new BridgeError("TRANSCRIPT_SCHEMA_UNSUPPORTED", `Malformed recognized transcript record at line ${line}: ${detail}`);
}
function isObject(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }

/** Previews keep 75 words from each end, or 130 characters from each end for long text. */
const PREVIEW_EDGE_WORDS = 75;
const PREVIEW_EDGE_CHARS = 130;
/** Text longer than this many characters is previewed by characters instead of words. */
const PREVIEW_CHAR_LIMIT = 1_000;

/**
 * Short preview of `text`, always joined with "...":
 * - more than 1,000 characters: "<first 130 characters>...<final 130 characters>"
 * - otherwise, more than 150 words: "<first 75 words>...<final 75 words>"
 * - otherwise the text is returned unchanged.
 */
export function previewWords(text: string): { text: string; truncated: boolean } {
  const chars = Array.from(text.trim());
  if (chars.length > PREVIEW_CHAR_LIMIT) {
    return { text: `${chars.slice(0, PREVIEW_EDGE_CHARS).join("")}...${chars.slice(-PREVIEW_EDGE_CHARS).join("")}`, truncated: true };
  }
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (words.length <= PREVIEW_EDGE_WORDS * 2) return { text, truncated: false };
  return {
    text: `${words.slice(0, PREVIEW_EDGE_WORDS).join(" ")}...${words.slice(-PREVIEW_EDGE_WORDS).join(" ")}`,
    truncated: true,
  };
}

export function outputPreview(value: unknown): { text: string | null; truncated: boolean } {
  if (value === undefined || value === null) return { text: null, truncated: false };
  const rendered = typeof value === "string" ? scrubText(value) : JSON.stringify(scrubValue(value));
  const words = rendered.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return { text: null, truncated: false };
  return previewWords(words.join(" "));
}

export function scrubValue(value: unknown): unknown {
  if (typeof value === "string") return scrubText(value);
  if (Array.isArray(value)) return value.map(scrubValue);
  if (isObject(value)) {
    const copy: Record<string, unknown> = {};
    for (const [key, nested] of Object.entries(value)) {
      // `data` is common ordinary application data. Only redact it when this
      // object is a Buffer-like binary representation; other binary-specific
      // fields (including image_url/audio_url/data_url) are always omitted.
      const isBufferData = key === "data" && value.type === "Buffer" && Array.isArray(nested);
      copy[key] = isBufferData || /^(image|audio|base64|blob)$|(?:image|audio|base64|blob|data_url)/i.test(key)
        ? "[binary content omitted]"
        : scrubValue(nested);
    }
    return copy;
  }
  return value;
}

export function scrubText(text: string): string {
  let result = text.replace(/data:[^\s]{0,80};base64,[A-Za-z0-9+/=\s]+/gi, "[data URL omitted]");
  result = result.replace(/[A-Za-z0-9+/]{256,}={0,2}/g, "[base64 omitted]");
  return /[\x00-\x08\x0B\x0C\x0E-\x1F]/.test(result) ? "[binary content omitted]" : result;
}

/** Applies the 150-word preview limit to chat text; tool items already carry previews. */
function previewChat(item: TranscriptItem): TranscriptItem {
  if (item.kind !== "chat") return item;
  const preview = previewWords(item.text);
  return preview.truncated ? { ...item, text: preview.text, text_truncated: true } : item;
}

export interface TranscriptPage {
  items: TranscriptItem[];
  has_older: boolean;
  total_items: number;
  total_chat_messages: number;
  total_tool_calls: number;
}

/**
 * Page `page` holds the two chat messages and the two tool calls that are
 * `page` pairs back from the newest of each kind (so at most four items),
 * merged back into the order they appear in the transcript.
 */
export function paginateTranscript(items: TranscriptItem[], page: number, perKind = 2): TranscriptPage {
  const chats: Array<{ item: TranscriptItem; index: number }> = [];
  const tools: Array<{ item: TranscriptItem; index: number }> = [];
  items.forEach((item, index) => (item.kind === "chat" ? chats : tools).push({ item, index }));
  const pick = (list: Array<{ item: TranscriptItem; index: number }>) => {
    const end = Math.max(0, list.length - page * perKind);
    const start = Math.max(0, end - perKind);
    return { picked: list.slice(start, end), older: start > 0 };
  };
  const chatPage = pick(chats);
  const toolPage = pick(tools);
  return {
    items: [...chatPage.picked, ...toolPage.picked].sort((a, b) => a.index - b.index).map((entry) => previewChat(entry.item)),
    has_older: chatPage.older || toolPage.older,
    total_items: items.length,
    total_chat_messages: chats.length,
    total_tool_calls: tools.length,
  };
}
