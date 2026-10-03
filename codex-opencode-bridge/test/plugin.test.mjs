import test from "node:test"
import assert from "node:assert/strict"
import { mkdtemp, readFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

const runtimeDir = await mkdtemp(join(tmpdir(), "bridge-runtime-"))
process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR = runtimeDir
process.env.CODEX_OPENCODE_BRIDGE_NO_PLUGIN_INSTALL = "1"
const registryPath = join(runtimeDir, `${process.pid}.json`)
const { CodexOpenCodeBridgeDiscovery } = await import("../integrations/opencode/codex-opencode-bridge.js")

async function startPlugin(client) {
  const hooks = await CodexOpenCodeBridgeDiscovery({ client, directory: "/tmp/x", serverUrl: new URL("http://localhost:4096") })
  const registry = JSON.parse(await readFile(registryPath, "utf8"))
  const post = (body, headers = { "x-bridge-token": registry.token }, path = "/bridge/request", method = "POST") =>
    fetch(new URL(path, registry.serverUrl), { method, headers: { "content-type": "application/json", ...headers }, body: method === "POST" ? JSON.stringify(body) : undefined })
  return { hooks, registry, post }
}

test("registry advertises protocol 4 on a random loopback port with a token", async () => {
  const { hooks, registry } = await startPlugin({ _client: { post: async () => ({ data: true }) } })
  try {
    assert.equal(registry.pid, process.pid)
    assert.equal(registry.protocol, 4)
    assert.equal(registry.directory, "/tmp/x")
    assert.match(registry.serverUrl, /^http:\/\/127\.0\.0\.1:\d+\/$/)
    assert.notEqual(new URL(registry.serverUrl).port, "4096")
    assert.ok(registry.token.length >= 32)
  } finally {
    await hooks.dispose()
  }
  await assert.rejects(readFile(registryPath), "dispose removes the registry")
})

test("forwards authenticated /tui/* calls to the raw client in arrival order", async () => {
  const calls = []
  const { hooks, post } = await startPlugin({ _client: { post: async (options) => { calls.push(options); return { data: true } } } })
  try {
    assert.equal((await post({ route: "/tui/select-session", body: { sessionID: "ses_1" } })).status, 200)
    assert.equal((await post({ route: "/tui/append-prompt", body: { text: "hello tui" } })).status, 200)
    const submit = await post({ route: "/tui/submit-prompt" })
    assert.equal(submit.status, 200)
    assert.deepEqual(await submit.json(), { data: true })
    assert.deepEqual(calls.map((c) => c.url), ["/tui/select-session", "/tui/append-prompt", "/tui/submit-prompt"])
    assert.deepEqual(calls[0].body, { sessionID: "ses_1" })
    assert.deepEqual(calls[1].body, { text: "hello tui" })
    assert.equal(calls[2].body, undefined)
    assert.ok(calls.every((c) => c.throwOnError === true))
  } finally {
    await hooks.dispose()
  }
})

test("forwards a direct session prompt without any TUI prompt-box route", async () => {
  const calls = []
  const { hooks, post } = await startPlugin({ _client: { post: async (options) => { calls.push(options); return { data: null } } } })
  try {
    const body = { parts: [{ type: "text", text: "hi" }], agent: "build", model: { providerID: "p", modelID: "m" } }
    const res = await post({ route: "/session/ses_Abc123/prompt_async", body })
    assert.equal(res.status, 200)
    assert.deepEqual(calls, [{ url: "/session/ses_Abc123/prompt_async", body, headers: { "Content-Type": "application/json" }, throwOnError: true }])
  } finally {
    await hooks.dispose()
  }
})

test("rejects bad tokens, bad routes, and other paths without calling the client", async () => {
  const calls = []
  const { hooks, post } = await startPlugin({ _client: { post: async (options) => { calls.push(options); return { data: true } } } })
  try {
    assert.equal((await post({ route: "/tui/submit-prompt" }, {})).status, 403)
    assert.equal((await post({ route: "/tui/submit-prompt" }, { "x-bridge-token": "wrong" })).status, 403)
    assert.equal((await post({ route: "/session/abc/delete" })).status, 400)
    assert.equal((await post({ route: "/tui/../session" })).status, 400)
    assert.equal((await post({ route: "/session/ses_abc/prompt_async/../delete" })).status, 400)
    assert.equal((await post({ route: "/session/ses_abc/message" })).status, 400)
    assert.equal((await post({})).status, 400)
    assert.equal((await post({ route: "/tui/submit-prompt" }, undefined, "/bridge/send")).status, 404)
    assert.equal((await post({ route: "/tui/submit-prompt" }, undefined, "/bridge/tui")).status, 404)
    assert.equal((await post(null, undefined, "/bridge/request", "GET")).status, 404)
    assert.equal(calls.length, 0)
  } finally {
    await hooks.dispose()
  }
})

test("falls back to tui._client, reports a missing raw client, and survives client errors", async () => {
  const seen = []
  let plugin = await startPlugin({ tui: { _client: { post: async (options) => { seen.push(options.url); return { data: true } } } } })
  try {
    assert.equal((await plugin.post({ route: "/tui/submit-prompt" })).status, 200)
    assert.deepEqual(seen, ["/tui/submit-prompt"])
  } finally {
    await plugin.hooks.dispose()
  }

  plugin = await startPlugin({ tui: {} })
  try {
    assert.equal((await plugin.post({ route: "/tui/submit-prompt" })).status, 502)
  } finally {
    await plugin.hooks.dispose()
  }

  let fail = true
  plugin = await startPlugin({ _client: { post: async () => { if (fail) { fail = false; throw new Error("tui gone") } return { data: true } } } })
  try {
    const bad = await plugin.post({ route: "/tui/submit-prompt" })
    assert.equal(bad.status, 502)
    assert.match((await bad.json()).error, /tui gone/)
    assert.equal((await plugin.post({ route: "/tui/submit-prompt" })).status, 200)
  } finally {
    await plugin.hooks.dispose()
  }
})
