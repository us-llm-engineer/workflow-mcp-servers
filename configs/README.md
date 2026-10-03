# Client configuration templates

Templates for registering the five servers with an MCP client. Replace every `/ABSOLUTE/PATH/TO/` and `<...>` placeholder; no real paths, keys or tokens are stored in this repository.

| File | Client | Notes |
| --- | --- | --- |
| `mcp.json.example` | Claude Code (`.mcp.json` or `claude mcp add`) | All five servers. `data-host-mcp` is an HTTP server, so start it first (see the systemd unit) and sign in with `/mcp`. |
| `codex.config.toml.example` | Codex CLI | stdio servers only. |
| `opencode.json.example` | OpenCode | stdio servers only. |
| `data-host-mcp.service` | systemd (user) | Keeps `data-host-mcp` running on `127.0.0.1:8791`. |
| `data-host-mcp.env.example` | `data-host-mcp` | Names of the secrets the server expects in its env file. |

Full per-machine setup (Google OAuth client, Azure app, Claude development channels, Modal and Scopus keys) is in the root README.

Build the bridge first (`npm ci && npm run build` in `codex-opencode-bridge/`) so `dist/index.js` exists. For Claude Code to *receive* bridged messages, start it with `--dangerously-load-development-channels server:codex-opencode-bridge`.
