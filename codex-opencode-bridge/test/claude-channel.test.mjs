import test from "node:test"
import assert from "node:assert/strict"
import { spawn } from "node:child_process"
import { createServer } from "node:http"
import { chmod, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

const root = await mkdtemp(join(tmpdir(), "claude-channel-"))
process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR = join(root, "runtime")
process.env.CLAUDE_CONFIG_DIR = join(root, "claude")
await mkdir(join(root, "runtime"), { recursive: true })
await mkdir(join(root, "claude", "sessions"), { recursive: true })

const { channelEnabledInCmdline, pushToClaudeChannel, startClaudeChannelReceiver } = await import("../dist/claude-channel.js")
const { sendToClaude } = await import("../dist/claude.js")

const spawned = []
test.after(async () => {
  for (const child of spawned) { try { child.kill("SIGKILL") } catch {} }
  await rm(root, { recursive: true, force: true })
})

async function startTime(pid) {
  const stat = await readFile(`/proc/${pid}/stat`, "utf8")
  return stat.slice(stat.lastIndexOf(")") + 1).trim().split(/\s+/)[19]
}

/** A long-lived process whose command line looks like a Claude Code launch. */
async function fakeClaude(args) {
  const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 100000)", "--", ...args], { stdio: "ignore" })
  spawned.push(child)
  await new Promise((resolve) => child.once("spawn", resolve))
  return child.pid
}

async function registerSession(pid, sessionId) {
  await writeFile(join(root, "claude", "sessions", `${pid}.json`), JSON.stringify({
    pid, sessionId, cwd: root, procStart: await startTime(pid), kind: "interactive", name: "fake",
  }))
}

function recordingReceiver() {
  const requests = []
  const server = createServer((req, res) => {
    const chunks = []
    req.on("data", (c) => chunks.push(c))
    req.on("end", () => {
      requests.push({ path: req.url, token: req.headers["x-bridge-token"], body: JSON.parse(Buffer.concat(chunks).toString()) })
      res.writeHead(200, { "content-type": "application/json" })
      res.end("true")
    })
  })
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, requests, port: server.address().port })))
}

async function registerReceiver(claudePid, bridgePid, port, token) {
  await writeFile(join(root, "runtime", `claude-channel-${claudePid}-${bridgePid}.json`), JSON.stringify({
    protocol: 1, claudePid, bridgePid, serverUrl: `http://127.0.0.1:${port}/`, token,
  }))
}

test("channelEnabledInCmdline recognizes the development and regular channel flags", () => {
  assert.equal(channelEnabledInCmdline(["claude", "--dangerously-load-development-channels", "server:codex-opencode-bridge"]), true)
  assert.equal(channelEnabledInCmdline(["claude", "--resume", "x", "--dangerously-load-development-channels", "server:other server:codex-opencode-bridge"]), true)
  assert.equal(channelEnabledInCmdline(["claude", "--dangerously-load-development-channels=server:codex-opencode-bridge"]), true)
  assert.equal(channelEnabledInCmdline(["claude", "--channels", "server:codex-opencode-bridge", "--resume", "x"]), true)
  assert.equal(channelEnabledInCmdline(["claude", "--resume", "x"]), false)
  assert.equal(channelEnabledInCmdline(["claude", "--dangerously-load-development-channels", "server:other"]), false)
  assert.equal(channelEnabledInCmdline(["claude", "--dangerously-load-development-channels", "server:other", "server:codex-opencode-bridge-x"]), false)
  assert.equal(channelEnabledInCmdline(["claude", "--dangerously-load-development-channels", "server:other", "--resume", "server:codex-opencode-bridge"]), false)
})

test("receiver under a Claude Code parent forwards authenticated messages as notifications", async () => {
  // Make this test process look like a bridge started by Claude Code.
  await registerSession(process.ppid, "parent-session")
  const notified = []
  assert.equal(await startClaudeChannelReceiver(async (content, meta) => { notified.push({ content, meta }) }), true)

  const name = (await readdir(join(root, "runtime"))).find((n) => n.startsWith(`claude-channel-${process.ppid}-${process.pid}`))
  assert.ok(name, "receiver registry written")
  const registry = JSON.parse(await readFile(join(root, "runtime", name), "utf8"))
  const post = (headers, body) => fetch(new URL("/claude/channel", registry.serverUrl), { method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body) })

  assert.equal((await post({}, { content: "x" })).status, 403)
  assert.equal((await post({ "x-bridge-token": "wrong" }, { content: "x" })).status, 403)
  assert.equal((await post({ "x-bridge-token": registry.token }, { content: "" })).status, 400)
  const ok = await post({ "x-bridge-token": registry.token }, { content: "hello claude", meta: { from: "opencode", "bad-key": "dropped", from_folder: "/f" } })
  assert.equal(ok.status, 200)
  assert.deepEqual(notified, [{ content: "hello claude", meta: { from: "opencode", from_folder: "/f" } }])

  // The parent's command line has no channel flag, so senders must not claim delivery.
  assert.equal(await pushToClaudeChannel(process.ppid, "hi"), "not-enabled")
})

test("sendToClaude pushes through the session's channel receiver when the flag is set", async (t) => {
  const pid = await fakeClaude(["--dangerously-load-development-channels", "server:codex-opencode-bridge"])
  await registerSession(pid, "11111111-1111-4111-8111-111111111111")
  const { server, requests, port } = await recordingReceiver()
  t.after(() => server.close())
  await registerReceiver(pid, process.pid, port, "tok-channel")

  const delivery = await sendToClaude({ tool: "claude", sessionId: "11111111-1111-4111-8111-111111111111", folder: root, name: "fake", transcriptPath: null }, "message from opencode")
  assert.equal(delivery.transport, "claude-channel")
  assert.equal(delivery.pid, pid)
  assert.equal(requests.length, 1)
  assert.equal(requests[0].path, "/claude/channel")
  assert.equal(requests[0].token, "tok-channel")
  assert.equal(requests[0].body.content, "message from opencode")
  assert.equal(typeof requests[0].body.meta.from_folder, "string")
})

test("sendToClaude reports CHANNEL_NOT_ENABLED for a session started without the flag", async (t) => {
  const pid = await fakeClaude(["--resume", "22222222-2222-4222-8222-222222222222"])
  await registerSession(pid, "22222222-2222-4222-8222-222222222222")
  const { server, requests, port } = await recordingReceiver()
  t.after(() => server.close())
  await registerReceiver(pid, process.pid, port, "tok")

  await assert.rejects(
    sendToClaude({ tool: "claude", sessionId: "22222222-2222-4222-8222-222222222222", folder: root, name: null, transcriptPath: null }, "hi"),
    (e) => e.code === "CHANNEL_NOT_ENABLED" && /--dangerously-load-development-channels server:codex-opencode-bridge --resume 22222222/.test(e.message),
  )
  assert.equal(requests.length, 0, "nothing is pushed into a session that would drop it")
})

test("sendToClaude reports CHANNEL_NOT_ENABLED when the session has no bridge server connected", async () => {
  const pid = await fakeClaude(["--dangerously-load-development-channels", "server:codex-opencode-bridge"])
  await registerSession(pid, "33333333-3333-4333-8333-333333333333")
  await assert.rejects(
    sendToClaude({ tool: "claude", sessionId: "33333333-3333-4333-8333-333333333333", folder: root, name: null, transcriptPath: null }, "hi"),
    (e) => e.code === "CHANNEL_NOT_ENABLED" && /no codex-opencode-bridge MCP server connected/.test(e.message),
  )
})

test("sendToClaude configures a channel-enabled session without a receiver, then retries once", async (t) => {
  const pid = await fakeClaude(["--dangerously-load-development-channels", "server:codex-opencode-bridge"])
  await registerSession(pid, "44444444-4444-4444-8444-444444444444")
  const { server, requests, port } = await recordingReceiver()
  t.after(() => server.close())

  const fakeClaudeBin = join(root, "fake-claude-mcp.mjs")
  const invocation = join(root, "mcp-add.json")
  await writeFile(fakeClaudeBin, [
    "#!/usr/bin/env node",
    'import { writeFileSync } from "node:fs"',
    'const [,, ...args] = process.argv',
    'writeFileSync(process.env.TEST_MCP_ADD_LOG, JSON.stringify(args))',
    'writeFileSync(`${process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR}/claude-channel-${process.env.TEST_TARGET_PID}-${process.ppid}.json`, JSON.stringify({ protocol: 1, claudePid: Number(process.env.TEST_TARGET_PID), bridgePid: process.ppid, serverUrl: `http://127.0.0.1:${process.env.TEST_RECEIVER_PORT}/`, token: "recovered-token" }))',
  ].join("\n"))
  await chmod(fakeClaudeBin, 0o755)
  const previous = process.env.CLAUDE_BIN
  Object.assign(process.env, {
    CLAUDE_BIN: fakeClaudeBin,
    TEST_MCP_ADD_LOG: invocation,
    TEST_TARGET_PID: String(pid),
    TEST_RECEIVER_PORT: String(port),
  })
  t.after(() => {
    if (previous === undefined) delete process.env.CLAUDE_BIN
    else process.env.CLAUDE_BIN = previous
    delete process.env.TEST_MCP_ADD_LOG
    delete process.env.TEST_TARGET_PID
    delete process.env.TEST_RECEIVER_PORT
  })

  const delivery = await sendToClaude({ tool: "claude", sessionId: "44444444-4444-4444-8444-444444444444", folder: root, name: null, transcriptPath: null }, "retry me")
  assert.equal(delivery.transport, "claude-channel")
  assert.match(delivery.note, /registered itself/)
  assert.equal(requests.length, 1, "the message is posted once, after the receiver appears")
  assert.equal(requests[0].body.content, "retry me")
  const args = JSON.parse(await readFile(invocation, "utf8"))
  assert.deepEqual(args.slice(0, 6), ["mcp", "add", "--scope", "user", "codex-opencode-bridge", "--"])
  assert.equal(args[6], "node")
  assert.match(args[7], /dist\/index\.js$/)
})
