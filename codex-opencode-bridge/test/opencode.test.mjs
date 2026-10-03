import test from "node:test"
import assert from "node:assert/strict"
import { spawn } from "node:child_process"
import { createServer } from "node:http"
import { chmod, mkdir, mkdtemp, readFile, readdir, readlink, realpath, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

const runtimeDir = await mkdtemp(join(tmpdir(), "bridge-runtime-"))
process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR = runtimeDir
process.env.CODEX_OPENCODE_BRIDGE_NO_PLUGIN_INSTALL = "1"

const scratch = await mkdtemp(join(tmpdir(), "bridge-opencode-"))
const folderA = await realpath(await mkdirReturn(join(scratch, "folderA")))
const folderB = await realpath(await mkdirReturn(join(scratch, "folderB")))
const folderEmpty = await realpath(await mkdirReturn(join(scratch, "folderEmpty")))
const folderStale = await realpath(await mkdirReturn(join(scratch, "folderStale")))

async function mkdirReturn(path) {
  await mkdir(path, { recursive: true })
  return path
}

const sessions = [
  { id: "ses_alpha", title: "alpha", directory: folderA },
  { id: "ses_dup1", title: "dup", directory: folderA, created: 1, updated: 100 },
  { id: "ses_dup2", title: "dup", directory: folderA, created: 2, updated: 900 },
  { id: "ses_b", title: "beta", directory: folderB },
]

const binDir = await mkdtemp(join(tmpdir(), "bridge-bin-"))
const binPath = join(binDir, "opencode")
const script = `#!/usr/bin/env node
const sessions = ${JSON.stringify(sessions)}
const args = process.argv.slice(2)
if (args[0] === "session" && args[1] === "list") {
  process.stdout.write(JSON.stringify(sessions))
} else if (args[0] === "export") {
  const fs = require("node:fs")
  const file = process.env.FAKE_OPENCODE_EXPORT
  process.stdout.write(file && fs.existsSync(file) ? fs.readFileSync(file, "utf8") : JSON.stringify({ messages: [] }))
} else {
  process.exitCode = 1
}
`
await writeFile(binPath, script, "utf8")
await chmod(binPath, 0o755)
process.env.OPENCODE_BIN = binPath

const { resolveOpenCodeSession, sendToOpenCode, pickOpenCodeTui } = await import("../dist/opencode.js")

function hasCode(code) {
  return (error) => error && error.code === code
}

test("resolveOpenCodeSession finds a session by id", async () => {
  const session = await resolveOpenCodeSession(folderA, "ses_alpha")
  assert.deepEqual(session, { tool: "opencode", sessionId: "ses_alpha", folder: folderA, name: "alpha", transcriptPath: null })
})

test("resolveOpenCodeSession finds a session by title", async () => {
  const session = await resolveOpenCodeSession(folderA, "alpha")
  assert.equal(session.sessionId, "ses_alpha")
})

test("resolveOpenCodeSession picks the most recently updated session when titles collide", async () => {
  const session = await resolveOpenCodeSession(folderA, "dup")
  assert.equal(session.sessionId, "ses_dup2")
  assert.deepEqual(session.olderSessionIds, ["ses_dup1"])
})

test("resolveOpenCodeSession reports FOLDER_MISMATCH for an id that belongs elsewhere", async () => {
  await assert.rejects(resolveOpenCodeSession(folderA, "ses_b"), hasCode("FOLDER_MISMATCH"))
})

test("resolveOpenCodeSession reports SESSION_NOT_FOUND when nothing matches", async () => {
  await assert.rejects(resolveOpenCodeSession(folderA, "nonexistent"), hasCode("SESSION_NOT_FOUND"))
})

const spawned = []
const foundPids = []

function spawnFakeTui(cwd, sessionArg) {
  const launch = sessionArg ? `exec -a opencode sh -c 'sleep 60; true' -s ${sessionArg}` : "exec -a opencode sleep 60"
  const child = spawn("script", ["-qfc", `bash -c "${launch}"`, "/dev/null"], {
    cwd,
    stdio: "ignore",
  })
  spawned.push(child)
  return child
}

async function findOpenCodePid(cwd) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const entries = await readdir("/proc")
    for (const entry of entries) {
      if (!/^\d+$/.test(entry)) continue
      try {
        const cmdline = (await readFile(`/proc/${entry}/cmdline`, "utf8")).split("\0").filter(Boolean)
        if (cmdline[0] !== "opencode") continue
        const procCwd = await realpath(await readlink(`/proc/${entry}/cwd`))
        if (procCwd !== cwd) continue
        foundPids.push(Number(entry))
        return Number(entry)
      } catch {
        // Process can exit while scanning.
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  throw new Error(`No fake opencode TUI found in ${cwd}`)
}

function startRecordingServer() {
  const requests = []
  const server = createServer((req, res) => {
    const chunks = []
    req.on("data", (chunk) => chunks.push(chunk))
    req.on("end", () => {
      requests.push({
        method: req.method,
        path: req.url,
        headers: req.headers,
        body: chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : null,
      })
      res.writeHead(200, { "content-type": "application/json" })
      res.end("true")
    })
  })
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, requests, port: server.address().port }))
  })
}

function startTuiControlServer() {
  const requests = []
  const server = createServer((req, res) => {
    const chunks = []
    req.on("data", (chunk) => chunks.push(chunk))
    req.on("end", () => {
      requests.push({ path: req.url, token: req.headers["x-bridge-token"], body: chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : null })
      res.writeHead(200, { "content-type": "application/json" })
      res.end("{\"data\":null}")
    })
  })
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, requests, port: server.address().port })))
}

test("sendToOpenCode submits straight to the session with the session's last model and agent", async (t) => {
  const folder = await realpath(await mkdirReturn(join(scratch, "folderP4")))
  const exportFile = join(scratch, "export-p4.json")
  await writeFile(exportFile, JSON.stringify({ messages: [
    { info: { role: "user", agent: "plan", model: { providerID: "old", modelID: "old-model" } }, parts: [] },
    { info: { role: "assistant" }, parts: [] },
    { info: { role: "user", agent: "build", model: { providerID: "opencode-go", modelID: "deepseek-v4-flash", variant: "high" } }, parts: [] },
    { info: { role: "assistant" }, parts: [] },
  ] }))
  process.env.FAKE_OPENCODE_EXPORT = exportFile
  t.after(() => { delete process.env.FAKE_OPENCODE_EXPORT })
  spawnFakeTui(folder)
  const pid = await findOpenCodePid(folder)
  const { server, requests, port } = await startTuiControlServer()
  t.after(() => server.close())
  await writeFile(join(runtimeDir, `${pid}.json`), JSON.stringify({ pid, serverUrl: `http://127.0.0.1:${port}/`, directory: folder, token: "tok4", protocol: 4 }))

  const delivery = await sendToOpenCode({ tool: "opencode", sessionId: "ses_target", folder, name: "t", transcriptPath: null }, "hello direct")
  assert.equal(delivery.transport, "opencode-tui")
  assert.equal(delivery.pid, pid)
  assert.equal(delivery.pane, null)
  assert.equal(requests.length, 1, "exactly one request: no select-session, append-prompt, or submit-prompt")
  assert.equal(requests[0].path, "/bridge/request")
  assert.equal(requests[0].token, "tok4")
  assert.deepEqual(requests[0].body, {
    route: "/session/ses_target/prompt_async",
    body: {
      parts: [{ type: "text", text: "hello direct" }],
      model: { providerID: "opencode-go", modelID: "deepseek-v4-flash" },
      variant: "high",
      agent: "build",
    },
  })
})

test("sendToOpenCode omits model settings when the session has no user message yet", async (t) => {
  const folder = await realpath(await mkdirReturn(join(scratch, "folderP4new")))
  spawnFakeTui(folder)
  const pid = await findOpenCodePid(folder)
  const { server, requests, port } = await startTuiControlServer()
  t.after(() => server.close())
  await writeFile(join(runtimeDir, `${pid}.json`), JSON.stringify({ pid, serverUrl: `http://127.0.0.1:${port}/`, directory: folder, token: "tok4", protocol: 4 }))

  await sendToOpenCode({ tool: "opencode", sessionId: "ses_new", folder, name: null, transcriptPath: null }, "first")
  assert.deepEqual(requests[0].body, { route: "/session/ses_new/prompt_async", body: { parts: [{ type: "text", text: "first" }] } })
})

for (const protocol of [undefined, 2, 3]) {
  test(`sendToOpenCode refuses a protocol-${protocol ?? 1} plugin instead of typing into the prompt box`, async (t) => {
    const folder = await realpath(await mkdirReturn(join(scratch, `folderOld${protocol ?? 1}`)))
    spawnFakeTui(folder)
    const pid = await findOpenCodePid(folder)
    const { server, requests, port } = await startTuiControlServer()
    t.after(() => server.close())
    await writeFile(join(runtimeDir, `${pid}.json`), JSON.stringify({ pid, serverUrl: `http://127.0.0.1:${port}/`, directory: folder, token: "tok", ...(protocol ? { protocol } : {}) }))

    await assert.rejects(
      sendToOpenCode({ tool: "opencode", sessionId: "ses_target", folder, name: null, transcriptPath: null }, "hi"),
      (e) => e.code === "TUI_CONTROL_UNAVAILABLE" && /Reopen that OpenCode once/.test(e.message),
    )
    assert.equal(requests.length, 0, "nothing is typed into the prompt box")
  })
}

test("sendToOpenCode reports TUI_CONTROL_UNAVAILABLE when the plugin endpoint is unreachable", async () => {
  const folder = await realpath(await mkdirReturn(join(scratch, "folderGone")))
  spawnFakeTui(folder)
  const pid = await findOpenCodePid(folder)
  await writeFile(join(runtimeDir, `${pid}.json`), JSON.stringify({ pid, serverUrl: "http://127.0.0.1:1/", directory: folder, token: "tok", protocol: 4 }))
  await assert.rejects(
    sendToOpenCode({ tool: "opencode", sessionId: "ses_x", folder, name: null, transcriptPath: null }, "hi"),
    (e) => e.code === "TUI_CONTROL_UNAVAILABLE",
  )
})

test("sendToOpenCode reports TARGET_NOT_RUNNING when no TUI is live in the folder", async () => {
  const session = { tool: "opencode", sessionId: "ses_x", folder: folderEmpty, name: null, transcriptPath: null }
  await assert.rejects(sendToOpenCode(session, "hi"), hasCode("TARGET_NOT_RUNNING"))
})

test.after(() => {
  for (const pid of foundPids) {
    try { process.kill(pid, "SIGKILL") } catch {}
  }
  for (const child of spawned) {
    try { child.kill("SIGKILL") } catch {}
  }
})

test("pickOpenCodeTui ranks windows started on the session, then on none, then on another; the newest wins a tie", () => {
  const win = (pid, startTime, ...args) => ({ pid, startTime, cmdline: ["opencode", ...args] })
  // The reported case: one window on the first session, one plain window; the target is a third session.
  assert.equal(pickOpenCodeTui([win(245350, 300, "-s", "ses_first"), win(166143, 100)], "ses_second").pid, 166143)
  assert.equal(pickOpenCodeTui([win(1, 100, "-s", "ses_second"), win(2, 200)], "ses_second").pid, 1, "started on the session beats a newer plain window")
  assert.equal(pickOpenCodeTui([win(1, 100, "--session=ses_second"), win(2, 200, "-c")], "ses_second").pid, 1)
  assert.equal(pickOpenCodeTui([win(1, 100), win(2, 200), win(3, 150)], "ses_second").pid, 2, "newest plain window")
  assert.equal(pickOpenCodeTui([win(1, 100, "-s", "ses_a"), win(2, 200, "-s", "ses_b")], "ses_second").pid, 2, "no better window: newest")
  assert.equal(pickOpenCodeTui([win(7, 100)], "ses_second").pid, 7)
})

test("sendToOpenCode uses the plain window, not the window started on another session (the reported failure)", async (t) => {
  const folder = await realpath(await mkdirReturn(join(scratch, "folderTwoWindows")))
  spawnFakeTui(folder, "ses_first")
  spawnFakeTui(folder)
  await new Promise((resolve) => setTimeout(resolve, 400))
  const pids = new Map()
  for (let attempt = 0; attempt < 40 && pids.size < 2; attempt += 1) {
    for (const entry of await readdir("/proc")) {
      if (!/^\d+$/.test(entry)) continue
      try {
        const cmdline = (await readFile(`/proc/${entry}/cmdline`, "utf8")).split("\0").filter(Boolean)
        if (cmdline[0] !== "opencode" || await realpath(await readlink(`/proc/${entry}/cwd`)) !== folder) continue
        pids.set(cmdline.includes("ses_first") ? "first" : "plain", Number(entry)); foundPids.push(Number(entry))
      } catch { /* the process may exit while scanning */ }
    }
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  assert.equal(pids.size, 2, "both fake windows are running")
  const servers = {}
  for (const [name, pid] of pids) {
    servers[name] = await startTuiControlServer()
    t.after(() => servers[name].server.close())
    await writeFile(join(runtimeDir, `${pid}.json`), JSON.stringify({ pid, serverUrl: `http://127.0.0.1:${servers[name].port}/`, directory: folder, token: `tok-${name}`, protocol: 4 }))
  }

  const delivery = await sendToOpenCode({ tool: "opencode", sessionId: "ses_second", folder, name: "second", transcriptPath: null }, "hello second")
  assert.equal(delivery.pid, pids.get("plain"))
  assert.equal(delivery.note, undefined, "a plain window is a fair guess, so no warning")
  assert.equal(servers.plain.requests.length, 1)
  assert.equal(servers.first.requests.length, 0, "the window on the other session is left alone")
})

test("sendToOpenCode reports windows of older same-named sessions and leaves them running", async (t) => {
  const folder = await realpath(await mkdirReturn(join(scratch, "folderOlder")))
  spawnFakeTui(folder, "ses_old")
  spawnFakeTui(folder, "ses_new")
  const pids = new Map()
  for (let attempt = 0; attempt < 60 && pids.size < 2; attempt += 1) {
    for (const entry of await readdir("/proc")) {
      if (!/^\d+$/.test(entry)) continue
      try {
        const cmdline = (await readFile(`/proc/${entry}/cmdline`, "utf8")).split("\0").filter(Boolean)
        if (cmdline[0] !== "opencode" || await realpath(await readlink(`/proc/${entry}/cwd`)) !== folder) continue
        pids.set(cmdline.includes("ses_old") ? "old" : "new", Number(entry)); foundPids.push(Number(entry))
      } catch { /* the process may exit while scanning */ }
    }
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  assert.equal(pids.size, 2, "both fake windows are running")
  const servers = {}
  for (const [name, pid] of pids) {
    servers[name] = await startTuiControlServer()
    t.after(() => servers[name].server.close())
    await writeFile(join(runtimeDir, `${pid}.json`), JSON.stringify({ pid, serverUrl: `http://127.0.0.1:${servers[name].port}/`, directory: folder, token: `tok-${name}`, protocol: 4 }))
  }

  const delivery = await sendToOpenCode({ tool: "opencode", sessionId: "ses_new", folder, name: "n", transcriptPath: null, olderSessionIds: ["ses_old"] }, "hello")
  assert.equal(delivery.pid, pids.get("new"))
  assert.equal(servers.new.requests.length, 1)
  assert.equal(servers.old.requests.length, 0)
  assert.deepEqual(delivery.older_windows.map(({ pid, session_id, reason }) => ({ pid, session_id, reason })), [
    { pid: pids.get("old"), session_id: "ses_old", reason: "older_session_with_same_name" },
  ])
  process.kill(pids.get("old"), 0) // still running: the bridge reports older windows and never closes them
})
