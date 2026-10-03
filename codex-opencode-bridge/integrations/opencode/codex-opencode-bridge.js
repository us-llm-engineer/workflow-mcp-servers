// Stable OpenCode bridge plugin (protocol 4).
//
// This file is deliberately a thin, token-protected pass-through from a
// loopback port to OpenCode's own in-process server routes. Requests run inside
// the TUI process, so the TUI renders their effects live. All delivery logic
// lives in the MCP server, so updating the server never requires restarting a
// running TUI. Do not add behaviour here unless the wire protocol must change.
import { unlinkSync } from "node:fs"
import { chmod, mkdir, unlink, writeFile } from "node:fs/promises"
import { randomBytes } from "node:crypto"
import { createServer } from "node:http"
import { join } from "node:path"

// Allowed in-process routes: TUI controls, and submitting a message straight to a
// session (which never touches the prompt box the human is typing in).
const ALLOWED_ROUTES = [
  /^\/tui\/[a-z][a-z-]*$/,
  /^\/session\/ses_[A-Za-z0-9]+\/prompt_async$/,
]

function runtimeDirectory() {
  if (process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR) return process.env.CODEX_OPENCODE_BRIDGE_RUNTIME_DIR
  const uid = typeof process.getuid === "function" ? process.getuid() : "user"
  return join("/tmp", `codex-opencode-bridge-${uid}`)
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on("data", (chunk) => {
      size += chunk.length
      if (size > limit) {
        reject(new Error("payload too large"))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on("end", () => resolve(Buffer.concat(chunks)))
    req.on("error", (error) => reject(error))
  })
}

function sendJson(res, status, value) {
  res.writeHead(status, { "content-type": "application/json" })
  res.end(JSON.stringify(value))
}

export const CodexOpenCodeBridgeDiscovery = async ({ client, directory }) => {
  const token = randomBytes(24).toString("hex")

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1")
    if (req.method !== "POST" || url.pathname !== "/bridge/request") return sendJson(res, 404, { error: "not found" })
    if (req.headers["x-bridge-token"] !== token) return sendJson(res, 403, { error: "forbidden" })

    let request
    try {
      request = JSON.parse((await readBody(req, 1024 * 1024)).toString("utf8"))
    } catch {
      return sendJson(res, 400, { error: "invalid request body" })
    }
    if (typeof request?.route !== "string" || !ALLOWED_ROUTES.some((pattern) => pattern.test(request.route))) {
      return sendJson(res, 400, { error: "route is not allowed" })
    }

    const raw = client?._client ?? client?.tui?._client
    if (typeof raw?.post !== "function") {
      return sendJson(res, 502, { error: "OpenCode client does not expose a raw HTTP client" })
    }
    try {
      const result = await raw.post({
        url: request.route,
        ...(request.body === undefined ? {} : { body: request.body, headers: { "Content-Type": "application/json" } }),
        throwOnError: true,
      })
      sendJson(res, 200, { data: result?.data ?? null })
    } catch (error) {
      sendJson(res, 502, { error: error instanceof Error ? error.message : String(error) })
    }
  })

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  server.unref()

  const root = runtimeDirectory()
  await mkdir(root, { recursive: true, mode: 0o700 })
  await chmod(root, 0o700)
  const registry = join(root, `${process.pid}.json`)
  await writeFile(registry, JSON.stringify({
    pid: process.pid,
    serverUrl: `http://127.0.0.1:${server.address().port}/`,
    directory,
    token,
    protocol: 4,
  }), { mode: 0o600 })

  process.on("exit", () => {
    try { unlinkSync(registry) } catch {}
  })

  return {
    dispose: async () => {
      server.close()
      await unlink(registry).catch(() => {})
    },
  }
}
