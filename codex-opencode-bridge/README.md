# codex-opencode-bridge

An STDIO MCP bridge for typing a message into a live OpenCode, Codex, or Claude Code session as if a human typed it, and for reading its native transcript. It does not capture terminal panes.

## Setup

Requires Node.js, the `opencode` and `codex` CLIs on `PATH`, and `tmux` only for Claude Code delivery. Run OpenCode and Codex exactly as you normally do; no tmux is involved.

```sh
npm install
npm run build
```

There are no manual setup steps. On startup the server does two things automatically:

- **Codex:** runs `codex app-server daemon start`. Codex windows opened while that shared daemon runs attach to it, which lets the bridge add user messages to their threads over the daemon's local control socket.
- **OpenCode:** writes its bundled OpenCode plugin to `~/.config/opencode/plugins/codex-opencode-bridge.js` (or `$XDG_CONFIG_HOME/opencode/plugins/...`) automatically, creating the directory if needed. The plugin starts its own loopback control listener inside the OpenCode process and publishes its random port plus a per-process token in a user-only registry file; the TUI's own `--port` setting does not matter and message contents never pass through the registry file. The plugin is a thin, stable pass-through to OpenCode's own `/tui/*` controls, so later server updates do not require restarting OpenCode. The server also drives TUIs that loaded an earlier version of the plugin.

**One-time caveat:** an OpenCode window opened before any bridge plugin was installed, or a Codex window opened before the daemon was first started, runs with a private in-process server that nothing outside can reach. The bridge reports that case with the exact command to reopen it; after reopening once, delivery is automatic from then on.

Codex `~/.codex/config.toml`:

```toml
[mcp_servers.codex-opencode-bridge]
command = "node"
args = ["/absolute/path/to/codex-opencode-bridge/dist/index.js"]
```

OpenCode `opencode.json`:

```json
{
  "mcp": {
    "codex-opencode-bridge": {
      "type": "local",
      "command": ["node", "/absolute/path/to/codex-opencode-bridge/dist/index.js"],
      "enabled": true
    }
  }
}
```

Claude Code:

```sh
claude mcp add codex-opencode-bridge -- node /absolute/path/to/codex-opencode-bridge/dist/index.js
```

## session_id and folder_path

Every tool takes two arguments identifying a session, and nothing else — there are no manual tmux labels or IDs to assign:

- `session_id`: `opencode:<session id or title>`, `codex:<thread UUID or name>`, or `claude:<session UUID or name>`.
- `folder_path`: the absolute working folder the session's agent process was started in.

Discovery is automatic from these two values: OpenCode sessions are looked up with `opencode session list --format json`; Codex and Claude Code sessions are looked up from their own on-disk session/transcript metadata, matched to a live process by folder.

### Several sessions with the same name

If more than one session in the folder shares a name, `send` and `watch_transcripts` use the most recent one. OpenCode and Claude Code compare last-updated times, and Codex compares its own state database. The result lists the skipped ids as `older_session_ids`. On `send`, live windows of those older sessions, and older duplicate windows of the same session, come back as `older_windows` with their pid and terminal. The bridge reports them and does not close them. Windows of different sessions are never treated as duplicates, so a session named in the call is always reached through the window that best matches it.

### How each tool is reached

- **OpenCode** (no tmux): the server finds the OpenCode process running in `folder_path`, then asks its bridge plugin to submit the message straight to the session inside that OpenCode process, using the session's last model and agent. The prompt box is never touched, so a draft you are typing is preserved. A window running an older plugin is refused with a reopen hint instead of being typed into.
- **Codex** (no tmux): the server connects to `~/.codex/app-server-control/app-server-control.sock` and calls `thread/queue/add`, the same input queue the Codex TUI uses when you press Enter. It starts a turn when Codex is idle and waits for the current turn otherwise; every attached Codex window renders it live. Codex stops draining that queue when a human interrupts a turn, so the bridge starts the message itself once the thread is idle, and `send` reports `queue.state` (`started`, `waiting_for_current_turn`, or `queued_not_started`). `watch_transcripts` lists messages Codex has accepted but not started under `queued_messages`. The session must be open in a Codex window. A Codex window opened before the daemon existed is reached through its tmux pane if it has one, and otherwise must be reopened once.
- **Claude Code** (tmux): the server types into the tmux pane the session runs in. Start it with `tmux new -s claude 'cd /abs/project/folder && claude --resume <uuid>'`, or `claude attach <jobId>` inside tmux for a background session.

## Examples

```json
{"folder_path": "/abs/project/folder", "session_id": "opencode:ses_abc123", "message": "Please inspect the failing build."}
```

```json
{"folder_path": "/abs/project/folder", "session_id": "codex:123e4567-e89b-42d3-a456-426614174000", "message": "Run the test suite again."}
```

```json
{"folder_path": "/abs/project/folder", "session_id": "claude:my-refactor-session", "message": "Continue with step 2."}
```

A `send` result with `"status": "submitted"` only means the text and Enter reached the interface, not that the agent finished responding.

```json
{"folder_path": "/abs/project/folder", "session_id": "opencode:ses_abc123", "page": 0}
```

`watch_transcripts` returns normalized native transcript items. Page 0 is the newest two items; page 1 is the previous two, and so on; each returned page is chronological. It returns chat items plus paired tool invocations (including pending tools), with a 150-word output preview.

## Privacy and security

The bridge reads native local transcript exports and can submit supplied messages through a live local interface. OpenCode delivery requires a live TUI process whose working folder matches `folder_path`, discovered from `/proc`, plus that same process's registry entry pointing at a token-protected loopback HTTP listener started by the plugin — the listener checks `x-bridge-token` on every request and never logs message contents. Codex delivery goes through the local Codex app-server control socket (a user-only Unix socket owned by Codex). Claude Code delivery requires a live tmux pane running the matching process. Keep runtime/config/transcript directories private and treat `send` as non-idempotent. No external network requests are made at runtime; all HTTP traffic is loopback-only. Large base64/data/binary-looking content is omitted from transcript output.
