import test from "node:test"
import assert from "node:assert/strict"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR ??= await mkdtemp(join(tmpdir(), "bridge-runtime-"))
process.env.CODEX_OPENCODE_BRIDGE_NO_PLUGIN_INSTALL = "1"

const { parseOpenCodeExport, readOpenCodeExport, readTranscript } = await import("../dist/transcripts.js")
const { execFile } = await import("../dist/exec.js")

function hasCode(code) {
  return (error) => error && error.code === code
}

function claudeSession(transcriptPath) {
  return { tool: "claude", sessionId: "sess-1", folder: "/tmp/x", name: null, transcriptPath }
}

function codexSession(transcriptPath) {
  return { tool: "codex", sessionId: "123e4567-e89b-42d3-a456-426614174000", folder: "/tmp/x", name: null, transcriptPath }
}

async function writeFixture(lines) {
  const dir = await mkdtemp(join(tmpdir(), "bridge-transcript-"))
  const path = join(dir, "transcript.jsonl")
  await writeFile(path, lines.join("\n") + "\n", "utf8")
  return path
}

test("readTranscript(claude) parses chat and tool items, skipping sidechain/meta/ignored lines", async () => {
  const lines = [
    JSON.stringify({ type: "user", isSidechain: true, message: { content: "should be skipped" } }),
    JSON.stringify({ type: "assistant", isMeta: true, message: { content: [{ type: "text", text: "skip meta" }] } }),
    JSON.stringify({ type: "custom-title", title: "Some title" }),
    JSON.stringify({ type: "user", message: { content: "Hello from user" } }),
    JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "Hello from assistant" }] } }),
    JSON.stringify({ type: "assistant", message: { content: [{ type: "tool_use", id: "tool_1", name: "bash", input: { cmd: "ls" } }] } }),
    JSON.stringify({ type: "user", message: { content: [{ type: "tool_result", tool_use_id: "tool_1", content: "file1\nfile2", is_error: false }] } }),
    JSON.stringify({ type: "assistant", message: { content: [{ type: "tool_use", id: "tool_2", name: "bash", input: { cmd: "badcmd" } }] } }),
    JSON.stringify({ type: "user", message: { content: [{ type: "tool_result", tool_use_id: "tool_2", content: [{ type: "text", text: "command not found" }], is_error: true }] } }),
    '{"type":"user","message":{"conte', // truncated final line: tolerated like a partial write
  ]
  const path = await writeFixture(lines)
  const items = await readTranscript(claudeSession(path))

  assert.deepEqual(items, [
    { kind: "chat", role: "user", text: "Hello from user" },
    { kind: "chat", role: "assistant", text: "Hello from assistant" },
    { kind: "tool", name: "bash", input: { cmd: "ls" }, status: "completed", output_preview: "file1 file2", output_truncated: false },
    { kind: "tool", name: "bash", input: { cmd: "badcmd" }, status: "error", output_preview: "command not found", output_truncated: false },
  ])
})

test("readTranscript(claude) ignores tool_result for an unknown tool_use_id", async () => {
  const lines = [
    JSON.stringify({ type: "user", message: { content: [{ type: "tool_result", tool_use_id: "ghost", content: "x" }] } }),
    JSON.stringify({ type: "user", message: { content: "still here" } }),
  ]
  const path = await writeFixture(lines)
  const items = await readTranscript(claudeSession(path))
  assert.deepEqual(items, [{ kind: "chat", role: "user", text: "still here" }])
})

test("readTranscript(claude) throws TRANSCRIPT_SCHEMA_UNSUPPORTED for an invalid line before the final line", async () => {
  const lines = [
    JSON.stringify({ type: "user", message: { content: "first" } }),
    "{not valid json",
    JSON.stringify({ type: "user", message: { content: "third" } }),
  ]
  const path = await writeFixture(lines)
  await assert.rejects(readTranscript(claudeSession(path)), hasCode("TRANSCRIPT_SCHEMA_UNSUPPORTED"))
})

test("readTranscript(claude) throws TRANSCRIPT_NOT_FOUND when transcriptPath is null", async () => {
  await assert.rejects(readTranscript(claudeSession(null)), hasCode("TRANSCRIPT_NOT_FOUND"))
})

test("readTranscript(codex) parses chat and paired tool items from a rollout fixture", async () => {
  const lines = [
    JSON.stringify({
      type: "response_item",
      payload: { type: "message", role: "user", content: [{ type: "input_text", text: "List files" }] },
    }),
    JSON.stringify({
      type: "response_item",
      payload: { type: "function_call", call_id: "call_1", name: "shell", arguments: JSON.stringify({ command: ["ls"] }) },
    }),
    JSON.stringify({
      type: "response_item",
      payload: { type: "function_call_output", call_id: "call_1", output: "file1\nfile2" },
    }),
    JSON.stringify({
      type: "response_item",
      payload: { type: "message", role: "assistant", content: [{ type: "output_text", text: "Done." }] },
    }),
  ]
  const path = await writeFixture(lines)
  const items = await readTranscript(codexSession(path))

  assert.deepEqual(items, [
    { kind: "chat", role: "user", text: "List files" },
    { kind: "tool", name: "shell", input: { command: ["ls"] }, status: "completed", output_preview: "file1 file2", output_truncated: false },
    { kind: "chat", role: "assistant", text: "Done." },
  ])
})

test("readTranscript(codex) throws TRANSCRIPT_NOT_FOUND when transcriptPath is null", async () => {
  await assert.rejects(readTranscript(codexSession(null)), hasCode("TRANSCRIPT_NOT_FOUND"))
})

test("parseOpenCodeExport accepts OpenCode's export progress banner, but not arbitrary prose", () => {
  const exported = { messages: [{ info: { role: "assistant" }, parts: [{ type: "text", text: "ready" }] }] }
  assert.deepEqual(parseOpenCodeExport(`Exporting session: ses_alpha\n${JSON.stringify(exported)}`, "ses_alpha"), exported)
  assert.throws(
    () => parseOpenCodeExport(`unexpected diagnostic\n${JSON.stringify(exported)}`, "ses_alpha"),
    hasCode("TRANSCRIPT_SCHEMA_UNSUPPORTED"),
  )
})

test("readOpenCodeExport retries a transient truncated export, then preserves a persistent parse error", async () => {
  const exported = { messages: [] }
  let attempts = 0
  const root = await readOpenCodeExport("ses_alpha", async () => {
    attempts += 1
    return attempts === 1 ? '{"messages":[' : JSON.stringify(exported)
  }, 0)
  assert.equal(attempts, 2)
  assert.deepEqual(root, exported)

  let invalidAttempts = 0
  await assert.rejects(
    readOpenCodeExport("ses_alpha", async () => {
      invalidAttempts += 1
      return "not JSON"
    }, 0),
    hasCode("TRANSCRIPT_SCHEMA_UNSUPPORTED"),
  )
  assert.equal(invalidAttempts, 3)
})

test("execFile can capture stdout through a regular file", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "bridge-exec-output-"))
  const outputPath = join(dir, "stdout.txt")
  t.after(() => rm(dir, { recursive: true, force: true }))
  const result = await execFile("printf", ["%s", "regular-file-output"], { stdoutPath: outputPath, maxOutputBytes: 1024 })
  assert.equal(result.stdout, "regular-file-output")
})

test("paginateTranscript returns the two newest chat messages and two newest tool calls per page, in transcript order", async () => {
  const { paginateTranscript } = await import("../dist/transcripts.js")
  const chat = (text) => ({ kind: "chat", role: "assistant", text })
  const tool = (name) => ({ kind: "tool", name, input: null, status: "completed", output_preview: null, output_truncated: false })
  // Transcript order: c1 t1 c2 c3 t2 t3 c4 t4 t5 c5
  const items = [chat("c1"), tool("t1"), chat("c2"), chat("c3"), tool("t2"), tool("t3"), chat("c4"), tool("t4"), tool("t5"), chat("c5")]
  const label = (page) => page.items.map((i) => i.text ?? i.name)

  const p0 = paginateTranscript(items, 0)
  assert.deepEqual(label(p0), ["c4", "t4", "t5", "c5"])
  assert.equal(p0.has_older, true)
  assert.equal(p0.total_items, 10)
  assert.equal(p0.total_chat_messages, 5)
  assert.equal(p0.total_tool_calls, 5)

  assert.deepEqual(label(paginateTranscript(items, 1)), ["c2", "c3", "t2", "t3"])
  const p2 = paginateTranscript(items, 2)
  assert.deepEqual(label(p2), ["c1", "t1"])
  assert.equal(p2.has_older, false)
  assert.deepEqual(label(paginateTranscript(items, 3)), [])

  const chatsOnly = paginateTranscript([chat("a"), chat("b"), chat("c")], 0)
  assert.deepEqual(label(chatsOnly), ["b", "c"])
  assert.equal(chatsOnly.has_older, true)
})

test("previews use 130+130 characters above 1,000 characters, otherwise 75+75 words above 150 words", async () => {
  const { paginateTranscript, outputPreview } = await import("../dist/transcripts.js")
  const words = (n, prefix) => Array.from({ length: n }, (_, i) => `${prefix}${i + 1}`).join(" ")
  const long = words(200, "w")
  const expected = `${words(75, "w")}...${Array.from({ length: 75 }, (_, i) => `w${126 + i}`).join(" ")}`

  assert.deepEqual(outputPreview(long), { text: expected, truncated: true })
  assert.deepEqual(outputPreview(words(150, "w")), { text: words(150, "w"), truncated: false })

  const huge = "a,".repeat(300) + "MIDDLE" + "z,".repeat(300)
  assert.deepEqual(outputPreview(huge), { text: `${huge.slice(0, 130)}...${huge.slice(-130)}`, truncated: true })
  assert.equal(outputPreview("x".repeat(1000)).truncated, false, "exactly 1,000 characters is not cut")

  const page = paginateTranscript([{ kind: "chat", role: "user", text: long }, { kind: "chat", role: "assistant", text: "short\nreply" }], 0)
  assert.deepEqual(page.items[0], { kind: "chat", role: "user", text: expected, text_truncated: true })
  assert.deepEqual(page.items[1], { kind: "chat", role: "assistant", text: "short\nreply" }, "short chat text is left exactly as written")
})
