import test from "node:test"
import assert from "node:assert/strict"
import { createServer } from "node:http"
import { mkdir, mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { WebSocketServer } from "ws"

const home = await mkdtemp(join(tmpdir(), "codex-daemon-home-"))
process.env.CODEX_HOME = home
process.env.CODEX_OPENCODE_BRIDGE_NO_CODEX_DAEMON_START = "1"
process.env.CODEX_OPENCODE_BRIDGE_QUEUE_POLL_MS = "30"
const socketPath = join(home, "app-server-control", "app-server-control.sock")
const { sendToCodex, readCodexQueue } = await import("../dist/codex.js")

const UUID = "01a0a73c-b0ca-7761-85d9-0123456789ab"
const session = { tool: "codex", sessionId: UUID, folder: "/tmp", name: "demo", transcriptPath: null }
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Stand-in for the Codex app-server control socket. `status` is the thread state;
 * `autoStart` models Codex draining its queue on its own. After a human interrupt
 * Codex stops draining, which is `autoStart: false` on an idle thread.
 */
async function startFakeDaemon({ loaded = [UUID], status = "idle", autoStart = true } = {}) {
  await mkdir(join(home, "app-server-control"), { recursive: true })
  const state = { status, autoStart, queue: [], started: [], requests: [], nextId: 1 }
  const server = createServer()
  const wss = new WebSocketServer({ server, perMessageDeflate: false })
  server.on("upgrade", (req, socket) => { if (req.headers["sec-websocket-extensions"]) socket.destroy() })
  wss.on("connection", (ws) => {
    ws.on("message", (raw) => {
      const msg = JSON.parse(raw.toString())
      state.requests.push(msg)
      const reply = (result) => ws.send(JSON.stringify({ id: msg.id, result }))
      const fail = (message) => ws.send(JSON.stringify({ id: msg.id, error: { code: -32600, message } }))
      const run = (item) => { state.started.push(item.id); state.status = "active" }
      switch (msg.method) {
        case "initialize": return reply({ userAgent: "fake" })
        case "initialized": return
        case "thread/loaded/list": return reply({ data: loaded, nextCursor: null })
        case "thread/read": return reply({ thread: { status: { type: state.status } } })
        case "thread/queue/list": return reply({ data: state.queue, nextCursor: null })
        case "thread/queue/add": {
          const item = { id: `q${state.nextId++}`, input: msg.params.input }
          if (state.autoStart && state.status === "idle") run(item)
          else state.queue.push(item)
          return reply({ queuedSubmission: { id: item.id, input: item.input } })
        }
        case "thread/queue/start": {
          if (state.status === "active") return fail("thread already has an active or pending turn")
          const index = state.queue.findIndex((item) => item.id === msg.params.queuedSubmissionId)
          run(state.queue.splice(index < 0 ? 0 : index, 1)[0])
          return reply({ turn: { status: "inProgress" } })
        }
        default: return fail("unknown")
      }
    })
  })
  await new Promise((resolve) => server.listen(socketPath, resolve))
  return {
    state,
    close: () => new Promise((resolve) => {
      for (const client of wss.clients) client.terminate() // a watcher may still hold a connection
      wss.close()
      server.close(resolve)
    }),
  }
}

test("a message on an idle thread runs at once, without an extra start call", async () => {
  const daemon = await startFakeDaemon()
  try {
    const delivery = await sendToCodex(session, "hello \"codex\"\nsecond line")
    assert.equal(delivery.transport, "codex-app-server")
    assert.deepEqual(delivery.queue, { state: "started", submission_id: "q1", ahead: 0 })
    assert.equal(daemon.state.requests.filter((r) => r.method === "thread/queue/start").length, 0)
    const add = daemon.state.requests.find((r) => r.method === "thread/queue/add")
    assert.deepEqual(add.params.input, [{ type: "text", text: "hello \"codex\"\nsecond line", text_elements: [] }])
  } finally {
    await daemon.close()
  }
})

test("after a human interrupt the idle thread holds messages, so the bridge starts them itself", async () => {
  const daemon = await startFakeDaemon({ autoStart: false })
  daemon.state.queue.push({ id: "old", input: [{ type: "text", text: "already stuck" }] })
  try {
    const delivery = await sendToCodex(session, "after the interrupt")
    assert.deepEqual(delivery.queue, { state: "started", submission_id: "q1", ahead: 1 })
    assert.deepEqual(daemon.state.started, ["q1"])
    assert.deepEqual(daemon.state.queue.map((i) => i.id), ["old"], "only the new message is started")
  } finally {
    await daemon.close()
  }
})

test("a message behind a running turn is rescued when the human interrupts that turn", async () => {
  const daemon = await startFakeDaemon({ status: "active", autoStart: false })
  try {
    const delivery = await sendToCodex(session, "queued mid-turn")
    assert.equal(delivery.queue.state, "waiting_for_current_turn")
    assert.deepEqual(daemon.state.started, [], "not started while a turn is running")

    daemon.state.status = "idle" // the human pressed Esc: idle, and Codex will not drain the queue
    for (let i = 0; i < 100 && daemon.state.started.length === 0; i += 1) await wait(20)
    assert.deepEqual(daemon.state.started, ["q1"])
    await wait(150) // let the watcher see the empty queue and finish
  } finally {
    await daemon.close()
  }
})

test("a queue start that races an active turn is not reported as a failure", async () => {
  const daemon = await startFakeDaemon({ autoStart: false })
  daemon.state.requests.push = function (...args) {
    // Simulate a turn beginning right after the idle check.
    if (args[0].method === "thread/queue/start") daemon.state.status = "active"
    return Array.prototype.push.apply(this, args)
  }
  try {
    const delivery = await sendToCodex(session, "raced")
    assert.equal(delivery.queue.state, "waiting_for_current_turn")
    assert.equal(delivery.queue.error, undefined)
    daemon.state.queue.length = 0 // the queued message ran, so the watcher can finish
    await wait(100)
  } finally {
    await daemon.close()
  }
})

test("readCodexQueue lists waiting messages, and is empty when the daemon is not running", async () => {
  const daemon = await startFakeDaemon({ autoStart: false })
  daemon.state.queue.push({ id: "w1", input: [{ type: "text", text: "waiting" }] })
  try {
    assert.deepEqual(await readCodexQueue(UUID), [{ id: "w1", text: "waiting" }])
  } finally {
    await daemon.close()
  }
  await rm(socketPath, { force: true })
  assert.deepEqual(await readCodexQueue(UUID), [])
})

test("reports TARGET_NOT_RUNNING when no Codex window has the thread open", async () => {
  const daemon = await startFakeDaemon({ loaded: ["other"] })
  try {
    await assert.rejects(sendToCodex(session, "hi"), (e) => e.code === "TARGET_NOT_RUNNING" && /not open in any Codex window/.test(e.message))
    assert.equal(daemon.state.requests.filter((r) => r.method === "thread/queue/add").length, 0)
  } finally {
    await daemon.close()
  }
})

test("reports TARGET_NOT_RUNNING when the daemon is not running and may not be started", async () => {
  await rm(socketPath, { force: true })
  await assert.rejects(sendToCodex(session, "hi"), (e) => e.code === "TARGET_NOT_RUNNING")
})

test.after(() => rm(home, { recursive: true, force: true }))
