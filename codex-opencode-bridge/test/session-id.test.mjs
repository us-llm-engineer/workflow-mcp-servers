import test from "node:test"
import assert from "node:assert/strict"
import { mkdtemp, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR ??= await mkdtemp(join(tmpdir(), "bridge-runtime-"))
process.env.CODEX_OPENCODE_BRIDGE_NO_PLUGIN_INSTALL = "1"

const { parseSessionId, resolveFolder } = await import("../dist/session-id.js")

function hasCode(code) {
  return (error) => error && error.code === code
}

test("parseSessionId accepts opencode/codex/claude prefixes", () => {
  assert.deepEqual(parseSessionId("opencode:ses_abc123"), { tool: "opencode", ref: "ses_abc123" })
  assert.deepEqual(parseSessionId("codex:123e4567-e89b-42d3-a456-426614174000"), {
    tool: "codex",
    ref: "123e4567-e89b-42d3-a456-426614174000",
  })
  assert.deepEqual(parseSessionId("claude:my session name"), { tool: "claude", ref: "my session name" })
})

test("parseSessionId trims the reference", () => {
  assert.deepEqual(parseSessionId("opencode:  padded  "), { tool: "opencode", ref: "padded" })
})

test("parseSessionId rejects unknown tool prefixes", () => {
  assert.throws(() => parseSessionId("cursor:abc"), hasCode("INVALID_ID"))
})

test("parseSessionId rejects a missing colon", () => {
  assert.throws(() => parseSessionId("opencode-abc"), hasCode("INVALID_ID"))
})

test("parseSessionId rejects an empty ref", () => {
  assert.throws(() => parseSessionId("opencode:"), hasCode("INVALID_ID"))
  assert.throws(() => parseSessionId("opencode:   "), hasCode("INVALID_ID"))
})

test("parseSessionId rejects a too-long ref", () => {
  assert.throws(() => parseSessionId(`opencode:${"a".repeat(257)}`), hasCode("INVALID_ID"))
})

test("parseSessionId accepts a 256-char ref", () => {
  const ref = "a".repeat(256)
  assert.deepEqual(parseSessionId(`opencode:${ref}`), { tool: "opencode", ref })
})

test("parseSessionId rejects control characters", () => {
  assert.throws(() => parseSessionId("opencode:bad\x01id"), hasCode("INVALID_ID"))
  assert.throws(() => parseSessionId("opencode:bad\nid"), hasCode("INVALID_ID"))
})

test("resolveFolder rejects a relative path", async () => {
  await assert.rejects(resolveFolder("relative/path"), hasCode("INVALID_FOLDER"))
})

test("resolveFolder rejects a nonexistent path", async () => {
  await assert.rejects(resolveFolder("/definitely/does/not/exist/xyz"), hasCode("INVALID_FOLDER"))
})

test("resolveFolder rejects a file", async () => {
  const dir = await mkdtemp(join(tmpdir(), "bridge-folder-"))
  const file = join(dir, "file.txt")
  await writeFile(file, "hi")
  await assert.rejects(resolveFolder(file), hasCode("INVALID_FOLDER"))
})

test("resolveFolder returns the realpath of a directory", async () => {
  const dir = await mkdtemp(join(tmpdir(), "bridge-folder-"))
  const resolved = await resolveFolder(dir)
  assert.equal(typeof resolved, "string")
  assert.ok(resolved.length > 0)
})
