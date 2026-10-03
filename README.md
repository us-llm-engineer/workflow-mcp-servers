# workflow-mcp-servers

Five [Model Context Protocol](https://modelcontextprotocol.io) (MCP) servers that connect local coding agents (Claude Code, Codex CLI, OpenCode) to what a research and engineering workflow keeps needing: **each other**, **a cloud notebook**, **rented GPUs**, **cloud storage** and **a literature index**. Each server is self-contained, has its own tests or documentation, and can be used alone.

| Server | Language / transport | One-line purpose | Jump to |
| --- | --- | --- | --- |
| `codex-opencode-bridge` | TypeScript, stdio | Type a message into another agent's *live* session and read its history. | [section](#1-codex-opencode-bridge) |
| `colab-proxy-mcp` | Python 3.13, stdio or HTTP | Control a Google Colab tab: sync a local notebook into it, run cells and shell, mount Drive. | [section](#2-colab-proxy-mcp) |
| `modal-gpu` | Python, stdio | Rent CPU/GPU sandboxes on Modal with persistent Jupyter kernels and cost guard rails. | [section](#3-modal-gpu) |
| `data-host-mcp` | Python, HTTP + OAuth | OneDrive download URLs; Google Drive upload and download. | [section](#4-data-host-mcp) |
| `scopus-mcp` | Python, stdio or HTTP | Search Scopus, read abstracts, authors and citations, scan recent open-access papers. | [section](#5-scopus-mcp) |

Contents: [How they fit together](#how-they-fit-together) - [Register with a client](#register-the-servers-with-an-agent) - sections 1-5 - [Moving to another machine](#moving-to-another-machine) - [Troubleshooting](#troubleshooting) - [References](#references) - [Provenance](#provenance-and-licences)

## How they fit together

```
 agent A <--send / watch_transcripts--> codex-opencode-bridge <--> agent B  (another live session)

 agent --> colab-proxy-mcp --ws--> Colab browser tab        notebook + free/paid GPU
 agent --> modal-gpu --------https-> Modal sandboxes         pay-per-second CPU/GPU + Jupyter kernels
 agent --> data-host-mcp ----------> OneDrive / Google Drive  datasets and results in and out
 agent --> scopus-mcp -------------> Elsevier Scopus API      papers, authors, citations
```

A typical loop: find recent papers with `scopus-mcp`; stage data with `data-host-mcp`; run the heavy cells on `modal-gpu` or `colab-proxy-mcp`; pull results back to disk and Drive; report progress to a second agent session through `codex-opencode-bridge`.

Shared design choices: no screen scraping (each tool is reached through its native interface); cost and consent are part of the API (`confirm=True`, short default lifetimes); long operations report progress; secrets live in environment variables or git-ignored files; every network-reachable mode requires a shared secret or OAuth; errors carry stable codes and name the command that fixes them.

## Register the servers with an agent

Build or install each server first (per-server steps below), then register it. Templates with placeholders are in [`configs/`](configs): `mcp.json.example` (Claude Code), `codex.config.toml.example`, `opencode.json.example`, `data-host-mcp.service`, `data-host-mcp.env.example`.

```bash
# Claude Code (user scope = available in every project)
claude mcp add --scope user codex-opencode-bridge -- node /ABS/PATH/codex-opencode-bridge/dist/index.js
claude mcp add --scope user colab-proxy-mcp       -- uv run --directory /ABS/PATH/colab-proxy-mcp colab-mcp
claude mcp add --scope user modal-gpu             -- python3 /ABS/PATH/modal-gpu/modal_server.py
claude mcp add --scope user scopus-mcp -e SCOPUS_API_KEY=<key> -- uv run --directory /ABS/PATH/scopus-mcp python -m scopus_mcp.server
claude mcp add --scope user --transport http data-host-mcp http://127.0.0.1:8791/mcp   # then run /mcp inside Claude Code to sign in
```

Codex reads `~/.codex/config.toml` (`[mcp_servers.<name>]` tables) and OpenCode reads `opencode.json` (`"mcp"` object); see the templates. Use absolute paths everywhere.

---

## 1. codex-opencode-bridge

### What it is for
Three terminal agents (OpenCode, Codex CLI, Claude Code) each keep their sessions private. This server lets one agent **submit a message into another agent's live session as if a human typed it**, and **read that session's history**, so agents can hand work to each other, ask for status, or answer a question another session raised. It does not capture terminal panes or screenshots; it uses each product's own control interface.

### What it can do
Two tools:

| Tool | Parameters | Returns |
| --- | --- | --- |
| `send` | `folder_path` (absolute working folder of the target session), `session_id` (`opencode:<id or title>`, `codex:<thread UUID or name>`, `claude:<session UUID, name or job id>`), `message` (1-65,536 UTF-8 bytes, no NUL) | `status:"submitted"`, resolved session id and name, byte count, `transport` (`opencode-tui`, `codex-app-server`, `claude-channel` or `tmux`), pid/tty/pane, and when relevant `older_session_ids`, `older_windows`, `queue` (Codex: `started`, `waiting_for_current_turn`, `queued_not_started`) and a `note`. `submitted` means the text reached the interface, not that the agent finished. |
| `watch_transcripts` | `folder_path`, `session_id`, `page` (0 = newest) | Normalised native history in pages of at most four items (two newest chat turns + two newest tool calls, in transcript order), totals, `has_older`, and for Codex `queued_messages`. Long text is previewed (first/last 75 words, or 130 characters each end over 1,000 characters); base64, data URLs and binary-looking content are removed. |

Behaviour worth knowing:
- **Per-tool delivery.** *OpenCode*: a plugin the server installs submits straight to the session (using the session's last model, variant and agent) without touching the prompt box, so a draft you are typing is preserved. *Codex*: the message is added to the thread's input queue through Codex's shared app-server daemon, exactly like pressing Enter; if the human interrupts a running turn, the bridge starts the queued message itself. *Claude Code*: pushed as a `notifications/claude/channel` event (needs the development-channels flag, see setup); falls back to typing into the session's tmux pane.
- **Duplicate names.** If several sessions in the folder share a name, the most recently updated one is used and the others are listed as `older_session_ids`; live windows of older sessions or duplicate windows are reported (never closed).
- **Replies.** A message arriving in Claude Code appears as `<channel source="codex-opencode-bridge" from="opencode|codex|claude" from_folder="...">`. The server's instructions tell the receiving agent to answer with `send` to that folder.
- **Errors** are `{error:{code,message}}` with codes such as `SESSION_NOT_FOUND`, `FOLDER_MISMATCH`, `TARGET_NOT_RUNNING`, `TUI_CONTROL_UNAVAILABLE`, `CHANNEL_NOT_ENABLED`; each message names the exact command that fixes it (for example the `claude ... --resume <id>` line).

Typical prompts: "Ask the Codex session `refactor` in `/work/api` to rerun its tests and tell me when it is done"; "Show me the last messages of the OpenCode session `ses_abc` in `/work/web`"; "Tell the Claude session `reviewer` that the migration is merged".

### Set up on a new machine
Requirements: **Linux or WSL** (it reads `/proc`; macOS is not supported), Node.js >= 22 (tested on 26), and whichever of the `opencode`, `codex`, `claude` CLIs you want to reach. `tmux` is needed only for the tmux fallback (Claude Code without channels, or a Codex window opened before the daemon existed). `sqlite3` is optional (OpenCode listing fallback).

1. `cd codex-opencode-bridge && npm ci && npm run build && npm test` (66 tests).
2. Register `node /ABS/PATH/codex-opencode-bridge/dist/index.js` with **every agent that should send or be reached**, in that agent's config (commands above and `configs/`).
3. **OpenCode.** Nothing to configure. On startup the server copies its plugin to `$XDG_CONFIG_HOME/opencode/plugins/codex-opencode-bridge.js` (default `~/.config/opencode/plugins/`). **Reopen each OpenCode window once** after the first start so the plugin loads. A window older than the plugin is refused with a hint instead of being typed into.
4. **Codex.** On startup the server runs `codex app-server daemon start`. Codex windows opened *while the daemon runs* attach to it and can receive messages. **Reopen each Codex session once** (`codex resume <uuid>` in the session's folder). The daemon protocol was verified against Codex CLI 0.154 (per the source); the unit tests use a fake daemon, so re-check delivery after upgrading Codex.
5. **Claude Code channels (to receive messages).** Claude Code only accepts server-initiated channel events from MCP servers named at launch, and silently drops them otherwise. Start (or resume) every Claude session that should be reachable with:
   ```bash
   claude --dangerously-load-development-channels server:codex-opencode-bridge --resume <session-uuid>
   ```
   The `server:<name>` must match the name used in `claude mcp add` (`codex-opencode-bridge`, or set `CODEX_OPENCODE_BRIDGE_CLAUDE_SERVER_NAME`). This is a development option that is not listed in `claude --help`; check your Claude Code version's release notes if it changes. A Claude session can *send* without the flag. If the flag is on but the server is not registered in that session's project, the bridge runs `claude mcp add --scope user` for you and retries once. Without the flag, delivery falls back to tmux: run the session inside tmux (`tmux new -s claude 'cd /work/proj && claude --resume <uuid>'`).
6. Verify: from one session call `watch_transcripts` for another, then `send` a harmless message.

Useful switches: `CODEX_OPENCODE_BRIDGE_NO_PLUGIN_INSTALL=1`, `..._NO_CODEX_DAEMON_START=1`, `..._NO_CLAUDE_CHANNEL=1`, `..._RUNTIME_DIR`, `CODEX_HOME`, `CODEX_BIN`, `OPENCODE_BIN`, `CLAUDE_BIN`, `CLAUDE_CONFIG_DIR`.

### Security and limits
Everything is local: loopback HTTP listeners protected by random per-process tokens, `0600` registry files in a `0700` runtime directory (`/tmp/codex-opencode-bridge-<uid>`), a Codex user-only Unix socket, no outbound network. `send` is not idempotent and never retries a possibly accepted delivery. It relies on non-public local interfaces of three fast-moving products, so expect to re-verify after upgrades.

Deep dive: [`codex-opencode-bridge/IMPLEMENTATION.md`](codex-opencode-bridge/IMPLEMENTATION.md) and its [README](codex-opencode-bridge/README.md).

---

## 2. colab-proxy-mcp

### What it is for
Colab has no remote API for editing notebooks. This server starts a local WebSocket endpoint, opens a Colab tab that dials back into it, and then drives the tab's own notebook tools. The result: an agent can keep a **local `.ipynb` as the source of truth**, push it into Colab with minimal edits, run cells and shell commands on Colab's hardware, mount Google Drive and read machine metrics. Several browser sessions (for example different Google accounts) run side by side, each with its own port and token.

### What it can do
| Tool | Parameters | Effect |
| --- | --- | --- |
| `open_browser` | `account_name` (label) | Opens `colab.research.google.com/notebooks/empty.ipynb` with a one-time token and port in the URL fragment, waits up to 60 s for the tab to connect, and returns `Browser ID: colab_<hex>` plus the notebook tools the tab exposes. |
| `load_notebook` | `browser_id`, `path` (local `.ipynb`) | Validates the file and binds it one-to-one to that browser session. Colab is not changed yet. |
| `sync_local` | `browser_id` | Diffs the bound local notebook against Colab and applies only the needed add / update / move / delete operations; returns counts (`unchanged, added, updated, moved, deleted`, `skipped_raw`). Local wins; Colab edits and outputs are not written back. Stops on the first error and says what was applied. |
| `get_cells` | `browser_id` | Raw cells with ids, sources and outputs, for diagnosis. |
| `run_code_cell` | `browser_id`, `cellId` | Executes an existing cell. |
| `run_shell` | `browser_id`, `command` | Runs any single- or multi-line shell text in a temporary `%%bash` cell that is deleted afterwards. |
| `mount_drive` | `browser_id` | Mounts Drive at `/content/drive` through a temporary cell and waits (up to 300 s) for it to finish, so you can approve the Google prompt. |
| `get_live_system_metrics` | `browser_id` | JSON with `gpu_ram_percent` (null without a GPU), `cpu_ram_percent`, `disk_percent`. |

Typical prompts: "Open Colab for my research account, load `train.ipynb`, sync it and run cell 3"; "Run `nvidia-smi` on the Colab session"; "How much GPU memory is in use?"

### Set up on a new machine
1. Install [uv](https://docs.astral.sh/uv/); it will fetch Python >= 3.13 automatically.
2. `cd colab-proxy-mcp && uv run --group dev pytest` (51 tests) confirms the install.
3. Register: `uv run --directory /ABS/PATH/colab-proxy-mcp colab-mcp` (stdio).
4. Use **Chrome (or a Chromium browser) signed in to the Google account you want**; the tab uses whichever profile the browser opens. Under WSL the server launches Windows Chrome from `/mnt/c/Program Files/Google/Chrome/Application/chrome.exe` (and the x86 path) and otherwise falls back to the default browser. A headless server cannot use it: a browser must be able to open `https://colab.research.google.com` and reach `ws://localhost:<port>` on the same machine.
5. First use: call `open_browser`; if the tab shows "Disconnected from the local Colab MCP server", see [Troubleshooting](#troubleshooting).
6. Optional HTTP mode for agents on another machine: `MCP_TRANSPORT=streamable-http MCP_SHARED_SECRET=<long random> MCP_HTTP_PORT=8767 uv run ... colab-mcp`; clients send `Authorization: Bearer <secret>`. The browser must still run on the machine hosting the server. Housekeeping: `colab-mcp --list-running`, `colab-mcp --kill-stale`.

### Security and limits
The WebSocket binds `127.0.0.1` only, accepts only the Colab origins, requires the per-session token, and allows one live client. Chrome's Private Network Access preflight is answered explicitly. One notebook can be bound to one live browser session. `get_cells` returns notebook contents, so treat output as sensitive.

Deep dive: [`colab-proxy-mcp/IMPLEMENTATION.md`](colab-proxy-mcp/IMPLEMENTATION.md), [`CHANGELOG.md`](colab-proxy-mcp/CHANGELOG.md).

---

## 3. modal-gpu

### What it is for
Give an agent a real cloud machine on demand: CPU-only or GPU (T4 up to B200), with either one-shot commands or a **persistent Jupyter kernel** whose variables survive between calls, plus file transfer and a small notebook-cell API. It is designed for quick feasibility experiments and heavy notebook cells, with **per-second billing treated as a first-class concern**.

### What it can do (32 tools)
| Group | Tools and parameters |
| --- | --- |
| Account | `modal_check_auth()` verifies credentials with a live, free call. `modal_list_apps()` shows every Modal app on the account (live/stopped). `modal_stop_app(app_identifier, confirm)` kills an app and everything under it. |
| Plain sandboxes | `modal_create_gpu_sandbox(gpu="T4", python_version="3.11", pip_packages, timeout=600, idle_timeout=120, app_name, confirm)`; `modal_terminate_sandbox(handle, confirm)` (waits for a confirmed exit code); `modal_list_sandboxes()`; `modal_sandbox_status(handle)`; `modal_reconnect_sandbox(object_id, gpu, app_name)` recovers a handle after a server restart. |
| Running code | `modal_run_code(handle, code, timeout=120)` (fresh `python -c` each call); `modal_run_shell(handle, args, timeout)` (argv list, no shell: use `["bash","-c","a \| b"]` for pipes); `modal_pip_install(handle, packages, timeout=300)`; `modal_sync_files(handle, files, direction="download"\|"upload")` copies `[remote, local]` pairs with per-pair results. |
| Persistent kernels | `modal_create_jupyter_kernel(gpu, pip_packages, timeout, idle_timeout, app_name, confirm)`; `modal_run_in_kernel(handle, code)` returns `stdout`, `stderr`, `result`, and structured `error`; `modal_list_kernels()`; `modal_stop_jupyter_kernel(handle, confirm)`; `modal_change_kernel_gpu(handle, gpu, timeout, idle_timeout, confirm)` swaps hardware under the same handle (state is lost, stored cells survive). |
| Notebook cells | `modal_add_code_cell(handle, code, cell_index)`, `modal_add_text_cell(handle, content, cell_index)`, `modal_get_cells`, `modal_run_cell(handle, cell_id)`, `modal_update_cell`, `modal_delete_cell`, `modal_move_cell`. Same vocabulary as the Colab server so a workflow can target either. |
| Infrastructure hosting | `host_infra_sandbox(cpu_cores=2, memory_mib=4096, apt_packages, encrypted_ports, timeout=3600, idle_timeout=900, app_name, confirm)` starts a **VM-runtime** sandbox running `dockerd` with `docker compose` installed, so a compose stack can run (default gVisor sandboxes cannot; needs Modal client >= 1.6.0, no GPU); `modal_upload_dir(handle, local_dir, remote_dir, exclude, include_secrets=False, max_mib=256)` uploads a project tree as one archive and skips secrets, `.git`, virtualenvs and caches by default; `modal_docker_status(handle)` lists containers, per-container memory and disk; `modal_sandbox_tunnels(handle)` lists public URLs of exposed ports. The `confirm=True` refusal states an hourly cost estimate. Details: [modal-gpu/README.md](modal-gpu/README.md#hosting-infrastructure-docker-on-the-vm-runtime). |
| Presets | `host_cpu_sandbox(scaling_factor, memory_mib)` (cores = 0.125 x factor, 0.125-16; `memory_mib` optional, Modal's default is 128 MiB), `host_one_low_tier_gpu(gpu="T4"\|"L4"\|"A10G")`, `host_two_t4()`, `host_one_high_end_gpu(gpu="L40S"\|"A100"\|"H100"\|"H200"\|"B200")`; each boots a kernel-capable sandbox, so every execution and cell tool works on its handle. |

Cost guard rails: create/terminate/switch tools require `confirm=True` and the server instructions tell the agent to state the GPU and hourly rate and get consent first; defaults are 10 minutes lifetime and 2 minutes idle; a failed Jupyter boot terminates its own sandbox; GPU names are validated locally. Jupyter URLs and tokens are never meant to be echoed.

Typical prompts: "Spin up a T4 kernel, train this small CNN for 3 epochs and print validation accuracy, then terminate it"; "Switch the kernel to an L40S and rerun the stored cells"; "List Modal apps and stop anything still running".

### Set up on a new machine
1. `pip install 'mcp>=1.2' 'modal>=1.6.0' websocket-client` (websocket-client is only for kernel tools; `modal>=1.6.0` only for `host_infra_sandbox`). Python 3.10+.
2. Create a Modal account, then authenticate: `modal setup` (writes `~/.modal.toml`) **or** set `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` from a service-user token (Modal dashboard -> Settings -> Tokens). Env vars are the right choice for CI or a headless host.
3. Register: `python3 /ABS/PATH/modal-gpu/modal_server.py` (stdio).
4. Verify: call `modal_check_auth` (free), then a short `host_cpu_sandbox` with `confirm=True` and `modal_run_shell(["nvidia-smi"])` style checks, then terminate.
5. Remember: the sandbox registry is in memory. After a server restart use `modal_list_apps` / `modal_reconnect_sandbox`, or check https://modal.com/apps, so nothing keeps billing.

Deep dive: [`modal-gpu/README.md`](modal-gpu/README.md).

---

## 4. data-host-mcp

### What it is for
Move datasets and results between the local disk and two clouds. **OneDrive**: turn a path into a URL an agent (or a remote sandbox) can `curl`. **Google Drive**: upload a file or folder, and mirror Drive paths back down to disk. The server doubles as its own OAuth 2.1 authorization server, so MCP clients sign in to Google through the standard connector flow and Claude Code stores and refreshes the token itself.

### What it can do
| Tool | Parameters | Returns / behaviour |
| --- | --- | --- |
| `get_file` | `path` (OneDrive path such as `/data/train.csv`) | File: a pre-authenticated download URL valid about an hour. Folder: the Microsoft Graph `.../children` URL (needs a Bearer token to call). |
| `host_to_drive` | `file_path` (file or folder, `~` allowed) | Uploads to Drive's root; folders are recreated recursively. Returns `{id, name, type, webViewLink}`. Uses Drive's resumable protocol in 8 MiB chunks and reports MCP progress after every chunk and every folder child, so multi-gigabyte uploads are not abandoned by client timeouts. |
| `sync_file_to_local` | `files`: list of `[drive_path, local_path]` | One-way Drive -> local for files and whole folders; creates parent directories; streams in 8 MiB chunks with progress; native Google formats (Docs, Sheets) are rejected with a clear message. Returns `{ok, synced, failed, results[]}` with one entry per pair, so a bad pair does not stop the batch. |

Limits: Drive path resolution takes the first match when names are duplicated and folder listings are not paginated beyond Drive's first page; OneDrive access is read-only (`Files.Read`).

Typical prompts: "Upload `./results` to my Drive"; "Download `/datasets/imagenet-sub` from Drive to `/data/imagenet-sub`"; "Give me a download URL for `/papers/notes.pdf` on OneDrive".

### Set up on a new machine
**A. Server**
1. `pip install -r data-host-mcp/requirements.txt` (`mcp`, `requests`), Python 3.10+.
2. Create the env file (outside the repo, `chmod 600`) from [`configs/data-host-mcp.env.example`](configs/data-host-mcp.env.example) with `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and (after step C) `AZURE_REFRESH_TOKEN`. Export `AZURE_ENV_PATH=/path/to/that/file` (default is `data-host-mcp/.env`) and `AZURE_APPLICATION_ID=<azure client id>`.
3. Run `python3 data-host-mcp/server.py`, or install the systemd user unit [`configs/data-host-mcp.service`](configs/data-host-mcp.service) (`systemctl --user enable --now data-host-mcp`; add `loginctl enable-linger $USER` to keep it running when logged out). It listens on `http://127.0.0.1:8791/mcp`.
4. Register the HTTP endpoint with your client (`claude mcp add --transport http data-host-mcp http://127.0.0.1:8791/mcp`), then run `/mcp` in Claude Code and sign in to Google once. Each client (Claude Code, Codex, ...) gets its own independent token slot.

**B. Google Drive: get the client id and secret** (needed by `host_to_drive` and `sync_file_to_local`)
1. In the [Google Cloud console](https://console.cloud.google.com/) create or choose a project and **enable the Google Drive API** (APIs & Services -> Library).
2. Configure the **OAuth consent screen**. For a personal Gmail account choose *External* and add your own account under *Test users*. The server requests the full `https://www.googleapis.com/auth/drive` scope, so Google shows an "unverified app" warning that you accept for your own use.
3. **Credentials -> Create credentials -> OAuth client ID -> Application type: Web application.** Add the authorized redirect URI exactly: `http://127.0.0.1:8791/google/callback`. Copy the client id and secret into the env file.
4. Important: while the consent screen's publishing status is **Testing**, Google expires refresh tokens after 7 days and you must sign in again. Move the app to *In production* (it stays unverified for personal use) or use an Internal app on a Workspace domain to avoid weekly re-login.
5. If Google does not return a refresh token (you already authorised this app once), revoke access at https://myaccount.google.com/permissions and sign in again; the server insists on a refresh token and says so.
6. On a **headless or remote machine** the sign-in browser must reach `127.0.0.1:8791`: forward it, `ssh -L 8791:127.0.0.1:8791 user@host`, and complete the Google consent in your local browser.

**C. OneDrive (personal Microsoft account): get the application id and refresh token** (needed by `get_file`)
1. In the [Azure portal](https://portal.azure.com/) -> Microsoft Entra ID -> App registrations -> **New registration**. Supported account types must include **personal Microsoft accounts** (the server uses the `consumers` tenant).
2. Under *Authentication* enable **Allow public client flows** (device code flow). Under *API permissions* add Microsoft Graph **delegated** `Files.Read` (and `offline_access`).
3. Copy the **Application (client) ID** into `AZURE_APPLICATION_ID`.
4. Run once: `AZURE_APPLICATION_ID=<id> AZURE_ENV_PATH=/path/to/env python3 data-host-mcp/device_auth.py`. Open the printed URL, enter the code, approve; the script stores `AZURE_REFRESH_TOKEN` in the env file. The server rotates and rewrites it on every refresh. (This helper implements the documented device-code flow; it is unit-checked with a mock but you should confirm it against your own tenant.)

**D. Verify**: `curl -s http://127.0.0.1:8791/.well-known/oauth-protected-resource/mcp` returns JSON; `python3 data-host-mcp/test_mcp.py` runs the suite against the live service.

### Security
`google_tokens.json` and `oauth_clients.json` appear next to the script (mode `0600`) and hold live tokens: never commit or share them (they are git-ignored). The service binds to `127.0.0.1`; do not expose it publicly. The Google bearer token a client holds is the real Drive token, so treat MCP client config as a secret.

Deep dive: [`data-host-mcp/README.md`](data-host-mcp/README.md).

---

## 5. scopus-mcp

### What it is for
Literature search against Elsevier's **Scopus** index from an agent, with caching and quota awareness so a research session does not burn the API allowance.

### What it can do
| Tool | Parameters | Returns |
| --- | --- | --- |
| `search_scopus` | `query` (Scopus syntax, e.g. `TITLE-ABS-KEY("graph neural network") AND PUBYEAR > 2022`), `count` (default 5), `sort` (default `coverDate`) | Compact records: Scopus id, title, first author, publication, cover date, DOI, citation count, document type, Scopus link. |
| `get_abstract_details` | `scopus_id` (with or without `SCOPUS_ID:`) | Abstract-retrieval record, compacted. |
| `get_author_profile` | `author_id` | Author profile: id, ORCID, name, current affiliation, document, citation and cited-by counts, Scopus author link. |
| `get_citing_papers` | `scopus_id`, `count`, `sort` | Papers that cite the record (runs `REFEID(<id>)`). |
| `get_quota_status` | none | Latest `X-RateLimit` limit, remaining and reset. |
| `search_recent_years` | `search_content` (exact phrase), `page` (>=1) | JSON grouped by each of the last four calendar years, five results per year per page, always restricted to Engineering/Computer Science, articles and conference papers, English, journals and proceedings, open access; per-year `total_results`, `total_pages`, `has_more_pages`, rich fields (OA label, affiliations, ISSN/ISBN, volume/pages, DOI link). One year failing does not lose the others. |

Two prompts (`research-summary`, `author-analysis`) are also registered. Abstract *text* is not returned by Scopus at standard-tier API keys; the tools return the metadata that is available.

Typical prompts: "Find the five most relevant open-access papers per year on 'retrieval augmented generation' for the last four years"; "Who cites paper 85123456789?"; "Show quota remaining".

### Set up on a new machine
1. Request an API key at the [Elsevier Developer Portal](https://dev.elsevier.com/) (an institutional or educational email is usually required; some keys only work from the institution's network or need an institutional token).
2. `cd scopus-mcp && uv run --with pytest --with pytest-asyncio pytest` (23 tests, no key needed).
3. Provide the key as `SCOPUS_API_KEY` in the client's `env` (preferred), or in a git-ignored `config.json` (copy `config.json.example`). Optional TTLs: `CACHE_TTL_SEARCH`, `CACHE_TTL_ABSTRACT`, `CACHE_TTL_AUTHOR`, `CACHE_TTL_DEFAULT` (defaults 1 h, 30 days, 7 days, 24 h). The cache lives in `~/.cache/scopus-mcp/`.
4. Register `uv run --directory /ABS/PATH/scopus-mcp python -m scopus_mcp.server` (stdio).
5. Network mode for an agent in another VM: `MCP_TRANSPORT=streamable-http MCP_SHARED_SECRET=<long random> MCP_HTTP_PORT=8766`; clients send `Authorization: Bearer <secret>` to `/mcp`. The secret is mandatory because the process holds your API key.

Deep dive: [`scopus-mcp/IMPLEMENTATION.md`](scopus-mcp/IMPLEMENTATION.md).

---

## Moving to another machine

| Item | What to carry over | What not to copy |
| --- | --- | --- |
| Code | `git clone` this repository; rebuild (`npm ci && npm run build`, `uv sync`). | `node_modules`, `.venv`, `dist`. |
| Modal | A new `modal setup`, or the two token env vars. | `~/.modal.toml` from another host (create a fresh token instead). |
| Scopus | The API key as an env var. | Other people's cached results (`~/.cache/scopus-mcp`) are harmless but unnecessary. |
| Google Drive | The same OAuth client id/secret may be reused (the redirect URI `http://127.0.0.1:8791/google/callback` is identical on every machine). Each new machine signs in again. | `google_tokens.json`, `oauth_clients.json` (they are bound to registered clients on the old host). |
| OneDrive | The Azure application id; run `device_auth.py` again on the new host. | `AZURE_REFRESH_TOKEN` from another host (one refresh-token chain per host avoids rotation clashes). |
| Agents | Re-run the registration commands with the new absolute paths. | Config files containing absolute paths from the old machine. |
| Bridge | Reopen OpenCode and Codex windows once; relaunch Claude sessions with the channels flag. | The runtime directory in `/tmp`. |

Platform notes: the bridge needs Linux/WSL; Colab needs a local browser; Modal, Scopus and data-host are cross-platform Python. WSL users: keep the repository inside the Linux filesystem for speed and register absolute Linux paths.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Bridge: `CHANNEL_NOT_ENABLED` | Target Claude session was not started with `--dangerously-load-development-channels server:codex-opencode-bridge`, or the bridge is not registered in it. Relaunch with the command in the error. |
| Bridge: `TUI_CONTROL_UNAVAILABLE` | OpenCode/Codex window predates the plugin/daemon. Reopen it once. |
| Bridge: `NOT_IN_TMUX` | Fallback needed but the agent is not in tmux. Restart it inside tmux or enable channels. |
| Colab tab says "Disconnected from the local Colab MCP server" | Port mismatch or blocked Private Network Access. The server binds IPv4 only and answers the preflight; make sure the browser is on the same machine, no extension blocks localhost, and retry `open_browser`. |
| `open_browser` times out | No browser was launched (headless host), the wrong Google profile is signed in, or a second client already holds the socket (code 1013). |
| Modal: auth-shaped errors | Run `modal_check_auth`; run `modal setup` or set the token env vars. |
| Modal: unknown handle after restart | The registry is in memory. `modal_list_apps`, then `modal_reconnect_sandbox(object_id)` or `modal_stop_app`. |
| Drive: sign-in works but tools say "Not authenticated" | Run `/mcp` in the client and authenticate; tokens are per client. |
| Drive: "Google did not return a refresh_token" | Revoke the app at myaccount.google.com/permissions and sign in again. |
| Drive: signs out after a week | Consent screen is in *Testing*; publish it to *In production*. |
| Drive: redirect_uri_mismatch | The OAuth client's redirect URI must be exactly `http://127.0.0.1:8791/google/callback`. |
| OneDrive: `No AZURE_REFRESH_TOKEN` | Run `device_auth.py`; check `AZURE_ENV_PATH` points at the file the server reads. |
| Scopus: 401 | Invalid or institution-bound key; confirm at the developer portal. |
| Scopus: empty results | Quote phrases and check field codes; `search_recent_years` fixes its own filters. |

## References

**MCP and agent clients**
- Model Context Protocol: https://modelcontextprotocol.io (authorization: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
- Claude Code MCP documentation: https://code.claude.com/docs/en/mcp (`claude mcp add --help` for the CLI; the development-channels flag is a launch option for `claude`, see the setup in section 1)
- Codex CLI: https://github.com/openai/codex (config file `~/.codex/config.toml`; `codex app-server daemon --help`)
- OpenCode: https://opencode.ai/docs/ (plugins: https://opencode.ai/docs/plugins/, MCP servers: https://opencode.ai/docs/mcp-servers/)
- tmux: https://github.com/tmux/tmux/wiki; systemd user units: `man systemd.service`; uv: https://docs.astral.sh/uv/

**Google (Drive and Colab)**
- Google Cloud console, enable Drive API: https://console.cloud.google.com/apis/library/drive.googleapis.com
- OAuth consent screen and credentials: https://console.cloud.google.com/apis/credentials/consent, https://console.cloud.google.com/apis/credentials
- OAuth 2.0 for web server applications: https://developers.google.com/identity/protocols/oauth2/web-server (refresh-token expiry in Testing mode: https://developers.google.com/identity/protocols/oauth2#expiration)
- Drive API v3 and resumable uploads: https://developers.google.com/drive/api/guides/manage-uploads
- Revoke app access: https://myaccount.google.com/permissions
- Colab: https://colab.research.google.com; upstream server: https://github.com/googlecolab/colab-mcp; Chrome Private Network Access: https://developer.chrome.com/blog/private-network-access-preflight

**Microsoft (OneDrive)**
- Register an app: https://learn.microsoft.com/entra/identity-platform/quickstart-register-app
- Device code flow: https://learn.microsoft.com/entra/identity-platform/v2-oauth2-device-code
- Graph driveItem content: https://learn.microsoft.com/graph/api/driveitem-get-content

**Modal**
- Sandboxes: https://modal.com/docs/guide/sandboxes; Jupyter in a sandbox: https://modal.com/docs/examples/jupyter_sandbox; pricing: https://modal.com/pricing; apps dashboard: https://modal.com/apps
- Jupyter kernel messaging protocol: https://jupyter-client.readthedocs.io/en/stable/messaging.html

**Elsevier Scopus**
- Developer portal and API keys: https://dev.elsevier.com/; Scopus search tips (field codes): https://dev.elsevier.com/sc_search_tips.html

**Standards used by data-host-mcp**
- OAuth 2.1 / PKCE (RFC 7636), dynamic client registration (RFC 7591), authorization-server metadata (RFC 8414), protected-resource metadata (RFC 9728).

External consoles change their labels; if a menu name differs, follow the linked guide.

## Provenance and licences
- `codex-opencode-bridge`, `modal-gpu` and `data-host-mcp` are original to this repository.
- `colab-proxy-mcp` is a fork of [googlecolab/colab-mcp](https://github.com/googlecolab/colab-mcp) (Apache-2.0; licence and headers retained), changed to a local-first notebook sync (see its changelog).
- `scopus-mcp` builds on [qwe4559999/scopus-mcp](https://github.com/qwe4559999/scopus-mcp) (MIT; licence retained) and adds `search_recent_years` and an authenticated HTTP mode.
