# codex-opencode-bridge: implementation

This document describes how the bridge works inside. For setup and tool usage see [README.md](README.md).

The server is a TypeScript (ESM, Node >= 22, tested on Node 26) STDIO MCP server of about 2,700 lines built on `@modelcontextprotocol/sdk`, `ws` and `zod`. It exposes two tools, `send` and `watch_transcripts`, and has one job: let any of three local coding agents (OpenCode, Codex CLI, Claude Code) type a message into another one's live session and read that session's history, without screen scraping.

## 1. Module map

| File | Responsibility |
| --- | --- |
| `src/index.ts` | MCP server, the two tool definitions, input validation, startup side effects (plugin install, Codex daemon, Claude channel receiver). |
| `src/session-id.ts` | Parses `opencode:` / `codex:` / `claude:` prefixes; canonicalises `folder_path` with `realpath`. |
| `src/types.ts` | `ResolvedSession`, `Delivery`, `OlderWindow`, `TranscriptItem` shared types. |
| `src/errors.ts` | `BridgeError` with a closed set of error codes; every failure reaches the caller as `{error:{code,message}}`. |
| `src/exec.ts` | `execFile` wrapper: no shell, hard timeout, output cap, optional stdout-to-file capture. |
| `src/procfs.ts` | `/proc` readers (cmdline, comm, controlling tty, cwd, environ, open files, start time). |
| `src/tmux.ts` | pid -> tty -> tmux socket -> pane discovery, and bracketed-paste delivery. |
| `src/opencode.ts` | OpenCode session resolution and delivery through the OpenCode plugin. |
| `integrations/opencode/codex-opencode-bridge.js` | The plugin that runs *inside* the OpenCode TUI process (protocol 4). |
| `src/install-plugin.ts` | Copies that plugin into `~/.config/opencode/plugins/` atomically. |
| `src/codex.ts` | Codex session resolution (rollout files + SQLite state DB) and delivery routing. |
| `src/codex-daemon.ts` | JSON-RPC-over-WebSocket client for the shared Codex app-server daemon. |
| `src/claude.ts` | Claude Code session resolution from its session registry and transcripts, delivery routing. |
| `src/claude-channel.ts` | Per-session loopback receiver that turns a POST into a `notifications/claude/channel` event. |
| `src/transcripts.ts` | Normalises three transcript formats into one item model; previews, redaction, pagination. |

## 2. Request lifecycle

Both tools go through the same front half (`index.ts`):

1. `parseSessionId` splits `session_id` with `^(opencode|codex|claude):(.+)$`. The reference is trimmed, must be 1-256 characters, and may not contain control characters.
2. `resolveFolder` demands an absolute path, `realpath`s it, and checks it is a directory. Every later comparison uses this canonical folder, so symlinked paths cannot cause false mismatches.
3. The matching `resolve*Session` function turns the reference (native id or human name) into a `ResolvedSession { tool, sessionId, folder, name, transcriptPath, olderSessionIds? }`.
4. `send` additionally validates the message (non-empty, no NUL byte, at most 65,536 UTF-8 bytes) and hands it to `sendToOpenCode`, `sendToCodex` or `sendToClaude`. `watch_transcripts` reads and paginates the transcript.

Results are JSON text. `send` returns `status: "submitted"`, the resolved id, byte count, transport (`opencode-tui`, `codex-app-server`, `claude-channel` or `tmux`), pid/tty/pane, and optional `older_session_ids`, `older_windows`, `queue` and `note`. `submitted` means the text reached the interface; it says nothing about the agent finishing.

### Same-name sessions

Names are not unique. Each resolver collects every match inside the folder and ranks by recency: OpenCode by `updated`/`created`, Codex by `updated_at_ms` from its state database (ties broken by the time-ordered UUID), Claude Code by the registry's `updatedAt`/`startedAt` or transcript mtime. The winner is used, the rest are returned as `older_session_ids`, and live windows of the losers (and older duplicate windows of the winner) are reported as `older_windows` with a reason of `older_session_with_same_name` or `duplicate_window`. The bridge never closes a window.

## 3. OpenCode (no tmux)

**Resolution.** `opencode session list --format json` is executed with the folder as cwd (30 s timeout, 16 MiB cap). If the CLI hangs and times out, the bridge falls back to a read-only `sqlite3 -readonly -json` query of OpenCode's database (`$XDG_DATA_HOME/opencode/opencode.db`, table `session`), validating every row's shape before use. Matching is by `id` or `title`; an id found only in another folder yields `FOLDER_MISMATCH`.

**Delivery plugin.** A TUI cannot be reached from outside, so the bridge ships a plugin loaded by OpenCode itself. On startup (`ensureOpenCodePlugin`) the server copies it to `$XDG_CONFIG_HOME/opencode/plugins/codex-opencode-bridge.js` through a temp file and `rename`, skipping the write when contents are identical. Inside each TUI process the plugin:

- starts an HTTP listener on `127.0.0.1:0` with a random 24-byte token;
- writes a `0600` registry file `<runtime-dir>/<pid>.json` containing pid, URL, directory, token and `protocol: 4` (runtime dir is `/tmp/codex-opencode-bridge-<uid>`, mode `0700`, overridable with `CODEX_OPENCODE_BRIDGE_RUNTIME_DIR`);
- accepts only `POST /bridge/request` with a matching `x-bridge-token`, a body of at most 1 MiB, and a `route` that matches one of two allow-list patterns: `/tui/<name>` or `/session/ses_<id>/prompt_async`;
- forwards the request through OpenCode's own in-process HTTP client, so the TUI renders the effect live;
- deletes its registry file on exit and `dispose`.

The plugin is deliberately a dumb pass-through; all logic lives in the MCP server, so updating the server never requires restarting a running TUI.

**Sending.** `scanOpenCodeTuis` walks `/proc`, selecting processes whose argv0/argv1 basename is `opencode`, whose fd 0 is a `/dev/pts/*`, and whose cwd equals the session folder; it reads each one's registry entry. A TUI is "capable" only if its registry pid matches, `protocol === 4`, a token exists and the URL is loopback-only (`127.0.0.1`, `localhost`, `[::1]`). Windows with an older plugin are refused with `TUI_CONTROL_UNAVAILABLE` and a reopen hint, because older plugins could only type into the prompt box, which would clobber a draft.

`pickOpenCodeTui` chooses among capable windows by tier: started with `-s <this session>` (best), started with no explicit session (could show anything), started on a different session (worst), then newest start time. If the chosen window was launched on another session a `note` warns that a different window will not update live. The message is POSTed to `/session/<id>/prompt_async` as `{parts:[{type:"text",text}]}`, augmented with the **model, variant and agent of the session's last user message** (read from `opencode export`) so the bridged turn runs with the settings the human last used. The prompt box is never touched.

## 4. Codex (no tmux)

**Resolution.** Codex stores each thread as `~/.codex/sessions/**/rollout-*-<uuid>.jsonl`. The resolver:

1. reads thread names from `$CODEX_HOME/session_index.jsonl` (`{id, thread_name}` records, the last one per id wins) and, for a name, collects every UUID that carries it;
2. finds the rollout file for each UUID (more than one is `AMBIGUOUS_TRANSCRIPT`);
3. reads the working folder from the rollout's session metadata, or, for a thread that has never run a turn (no rollout file is written until the first turn), from `cwd` in `state_5.sqlite` (opened read-only via `node:sqlite`);
4. keeps candidates whose realpath'd cwd equals the folder and sorts by `updated_at_ms`.

**Delivery, preferred path: the shared app-server daemon.** At startup the bridge runs `codex app-server daemon start` (idempotent). A Codex TUI opened while it runs attaches to the daemon, which exposes a control socket at `$CODEX_HOME/app-server-control/app-server-control.sock` speaking JSON-RPC over WebSocket (`ws+unix://`, with `perMessageDeflate` off because the daemon rejects that extension). `withCodexDaemon` opens one connection, performs `initialize` (experimental API enabled) plus the `initialized` notification, runs the work, and always closes. Each call has a 10 s timeout and all pending calls are failed if the socket closes.

`sendToCodex` verifies the thread is loaded (`thread/loaded/list`, paginated), then `queueAndStart`:

1. `thread/queue/list` to count messages ahead;
2. `thread/queue/add` with a fresh `clientUserMessageId` - the same queue the TUI feeds when a human presses Enter, so every attached window renders it live;
3. if the message already left the queue the state is `started`;
4. if `thread/read` says the thread is `active` the state is `waiting_for_current_turn`;
5. otherwise the thread is idle with the message still queued (Codex stops draining its queue after a human interrupts a turn), so the bridge calls `thread/queue/start`; an "active or pending turn" refusal is treated as harmless;
6. anything else is `queued_not_started` with the error text, which tells callers **not** to resend.

For `waiting_for_current_turn`, `watchQueuedSubmission` polls (every 2 s, up to 30 min, both tunable) and starts the message if the user interrupts the running turn and the thread goes idle with it still queued.

**Delivery, fallback.** A TUI opened before the daemon existed runs a private in-process server and holds the thread's rollout file and `thread-writer-locks/<uuid>.lock` open. `privateServerOwners` finds such processes by scanning `/proc/*/fd`. If one exists, the only way in is its tmux pane (`locateTmuxPane`); outside tmux the bridge raises `TUI_CONTROL_UNAVAILABLE` telling the user to reopen the session once with `codex resume <uuid>`.

`watch_transcripts` for Codex also lists `queued_messages` from `thread/queue/list`.

## 5. Claude Code

**Resolution.** Live sessions come from the registry files in `$CLAUDE_CONFIG_DIR/sessions/*.json` (`pid`, `sessionId`, `cwd`, `name`, `jobId`, `kind`, `startedAt`, `updatedAt`, `procStart`). An entry is live only if `/proc/<pid>` exists and its recorded `procStart` equals the kernel start time (guards against pid reuse). A reference may be a session UUID, a name or a background job id. With no live match the resolver falls back to on-disk transcripts under `projects/<escaped-folder>/<uuid>.jsonl`, matching a UUID directly or a name through the last `custom-title` record in each file.

**Delivery, preferred path: channels.** Claude Code accepts server-initiated `notifications/claude/channel` events from MCP servers started with `--dangerously-load-development-channels server:<name>`, and drops them silently otherwise. The bridge declares the `experimental["claude/channel"]` capability, and when it finds that its own ancestor (up to five parents) is a registered Claude Code process, `startClaudeChannelReceiver`:

- listens on `127.0.0.1:0` with a random token;
- writes `claude-channel-<claudePid>-<bridgePid>.json` (mode `0600`) into the runtime dir;
- accepts `POST /claude/channel {content, meta}` (1 MiB cap, token checked, meta keys restricted to `[A-Za-z0-9_]+` with string values) and calls `server.notification(...)`, which Claude Code shows to the model as a `<channel source=... from=... from_folder=...>` tag.

A sender (`pushToClaudeChannel`) finds live receivers for the target pid, **checks the target's command line for the channel flag first** (otherwise the event would vanish silently and the call would still look successful), then posts, trying newer bridge processes first. `senderMeta` fills `from` by walking the sender's parents to find an `opencode`, `codex` or `claude` process, and `from_folder` from its cwd; the server instructions tell the receiving agent to answer with `send`.

Order of attempts in `deliverToClaude`: interactive sessions newest first, then background ones; channel push; if there was a missing receiver but the flag is on, run `claude mcp add --scope user codex-opencode-bridge -- node <dist>/index.js` and poll up to 12 x 250 ms for a receiver to appear (one post only, once it does); finally the tmux fallback (the session's own pane, or the pane of a `claude attach <job>` client for a background job). If everything fails the error is `CHANNEL_NOT_ENABLED` with the exact relaunch command.

## 6. The tmux path

`locateTmuxPane` needs no setup: pid -> fd 0 tty -> the process's own `TMUX` environment variable gives the tmux server socket -> `tmux -S <socket> list-panes -a -F '#{pane_id}|#{pane_tty}|#{pane_dead}'` finds the live pane with that tty. `pasteAndSubmit` loads the message into a uniquely named tmux buffer from stdin (newlines, quotes and Unicode survive), `paste-buffer -p -d` pastes it with bracketed-paste markers, waits 200 ms so TUIs with paste-burst detection do not absorb the Enter, then sends `Enter`. Pane ids must match `^%\d+$`. Arguments are never shell-interpreted anywhere in the project (`execFile` uses `shell: false`).

## 7. Transcripts

`readTranscript` normalises three formats into `TranscriptItem = chat{role,text} | tool{name,input,status,output_preview}`:

- **Codex**: the rollout JSONL is streamed line by line; tool calls and their outputs are paired by call id. A malformed *final* line is tolerated (an agent may be mid-write); a malformed line followed by more data raises `TRANSCRIPT_SCHEMA_UNSUPPORTED`.
- **Claude Code**: the project JSONL; sidechain and meta records are skipped, `tool_use` and `tool_result` blocks are paired by id (a result for an unknown id is ignored, a duplicate `tool_use` id is a schema error) and tool uses without a result stay `pending`. Thinking blocks are ignored.
- **OpenCode**: `opencode export` captured **through a regular file** (the CLI truncates large pipe output), tolerating its progress banner and retrying a transient truncated export; messages are `info` + `parts`.

`paginateTranscript` returns pages of at most four items: the two newest chat messages and two newest tool calls, merged back into transcript order; page `n` steps back `n` pairs. Output safety (`scrubValue`/`scrubText`): keys that look like image/audio/base64/blob/data_url are replaced by `[binary content omitted]`, `data:...;base64` URLs and base64 runs of 256+ characters are removed, and control-character content is dropped. Previews keep the first and last 75 words of text over 150 words, or 130 characters from each end for text over 1,000 characters.

## 8. Failure model and safety properties

- Error codes: `INVALID_ARGUMENT, INVALID_ID, INVALID_FOLDER, SESSION_NOT_FOUND, AMBIGUOUS_SESSION, FOLDER_MISMATCH, TRANSCRIPT_NOT_FOUND, AMBIGUOUS_TRANSCRIPT, AMBIGUOUS_BINDING, TARGET_NOT_RUNNING, NOT_IN_TMUX, CHANNEL_NOT_ENABLED, TUI_CONTROL_UNAVAILABLE, COMMAND_FAILED, COMMAND_TIMEOUT, TRANSCRIPT_SCHEMA_UNSUPPORTED`. Each error message names the exact command the user should run to fix the situation.
- All HTTP is loopback-only and token-protected; registry files are `0600` in a `0700` directory; message bodies never go into registry files or logs.
- `send` is not idempotent. The bridge never retries a delivery that may have been accepted.
- Node 26 can drop an idle anonymous stdin pipe from the event loop, so `index.ts` keeps a keepalive interval until the first MCP frame arrives and releases it on stdin `end`.

## 9. Environment variables

`CODEX_OPENCODE_BRIDGE_RUNTIME_DIR`, `CODEX_OPENCODE_BRIDGE_NO_PLUGIN_INSTALL=1`, `CODEX_OPENCODE_BRIDGE_NO_CODEX_DAEMON_START=1`, `CODEX_OPENCODE_BRIDGE_NO_CLAUDE_CHANNEL=1`, `CODEX_OPENCODE_BRIDGE_CLAUDE_SERVER_NAME`, `CODEX_OPENCODE_BRIDGE_QUEUE_POLL_MS`, `CODEX_OPENCODE_BRIDGE_QUEUE_WATCH_MS`, `CODEX_HOME`, `CODEX_BIN`, `OPENCODE_BIN`, `CLAUDE_BIN`, `CLAUDE_CONFIG_DIR`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`.

## 10. Tests

`npm test` compiles and runs 66 tests with the built-in `node:test` runner (7 files in `test/`): session-id parsing, transcript parsing and pagination against fixtures, the Codex queue logic against a fake daemon (idle start, post-interrupt start, rescue watcher, start/active races, `TARGET_NOT_RUNNING` cases), the OpenCode plugin against a fake client, Claude channel flag detection and delivery with fake Claude processes, and terminal/tmux helpers. Environment switches above keep tests from touching real daemons or plugin directories.
