# colab-proxy-mcp: implementation

This server is a local-first fork of Google's [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) (Apache-2.0, original copyright headers and `LICENSE` retained). The fork's changes are listed in [CHANGELOG.md](CHANGELOG.md); this document explains how the whole thing works. Python >= 3.13, FastMCP 2.14.5, `websockets`, `anyio`, about 1,130 lines in `src/colab_mcp/`.

## 1. The core idea: the browser is the server's client

Colab exposes no remote API for editing a notebook. The Colab web page, however, can open a WebSocket to a local MCP server and act as an **MCP server itself** (it advertises notebook tools: `add_code_cell`, `get_cells`, `run_code_cell`, ...). So the roles invert: this process starts a WebSocket server, opens Colab in a browser tab, waits for the tab to dial in, and then uses an MCP *client* over that socket to call the tab's tools. The process is simultaneously

- an MCP **server** towards your agent (stdio by default, or authenticated streamable HTTP), and
- an MCP **client** towards the Colab tab (over the WebSocket).

```
agent --MCP--> colab_mcp (FastMCP) --MCP client--> ColabWebSocketServer <--ws-- Colab tab
```

## 2. Module map

| File | Role |
| --- | --- |
| `__init__.py` | FastMCP app, the 8 public tools, private forwarding helpers, notebook binding table, CLI, transports. |
| `session.py` | `ColabSessionProxy` (one per browser session), `ColabProxyClient`, `ColabTransport`, browser launching. |
| `websocket_server.py` | `ColabWebSocketServer`: the single-client, origin-checked, token-checked WebSocket endpoint. |
| `notebook_file.py` | Pure-stdlib `.ipynb` parser (`load_ipynb_cells`). |
| `process_registry.py` | Cross-platform registry of running servers, with liveness checks and stale cleanup. |

## 3. Opening a session (`open_browser`)

1. A fresh `browser_id = "colab_<uuid4 hex>"` and a fresh `ColabSessionProxy` are created. **Every browser session has its own WebSocket server, port and token**, so several Colab accounts or tabs are isolated from each other.
2. `ColabWebSocketServer.__aenter__` binds `127.0.0.1` with port 0 and generates `secrets.token_urlsafe(16)`. It then asserts that every bound socket shares one port; binding `localhost` can create IPv4 and IPv6 listeners on *different* ephemeral ports, and the tab would hit the wrong one and show "Disconnected from the local Colab MCP server".
3. `_open_url` opens `https://colab.research.google.com/notebooks/empty.ipynb?p=<port>#mcpProxyToken=<token>&mcpProxyPort=<port>`. Under WSL it launches the Windows Chrome executable directly (not through `cmd.exe start`, which treats the `&` in the URL fragment as a command separator and drops the port) and falls back to `webbrowser.open_new`.
4. `ColabProxyClient.await_proxy_connection` waits up to 60 s for the tab to connect and for the MCP `initialize` handshake to finish, then `await_tools_ready` polls `list_tools` (10 s) so the tool returns the list of notebook tools the tab exposes. On any failure the session is removed and cleaned up and the tool reports the failure rather than leaving a half-open session.

`account_name` is validated as non-empty and stored only as caller metadata; an email address is not a valid Colab `authuser` selector, so the tab uses whatever profile Chrome is signed into.

## 4. The WebSocket endpoint

`websockets.serve(..., subprotocols=["mcp"], origins=[colab.research.google.com, colab.google.com], process_request=..., process_response=...)`.

- **Auth:** an `Upgrade: websocket` request must carry either `access_token=<token>` in the path or `Authorization: Bearer <token>`; missing token -> 401, malformed -> 400, wrong -> 403.
- **Private Network Access:** a public HTTPS page connecting to `localhost` is a "private network request" in Chrome, which first sends an `OPTIONS` preflight and re-checks the upgrade response. Non-upgrade requests therefore get `204` with `Access-Control-Allow-Private-Network: true` plus the usual CORS headers, and the same headers are added to the `101` upgrade response (`process_response`). Without both, Chrome silently cancels the upgrade.
- **Single client:** an `asyncio.Lock` allows one live connection; a second one is closed with code 1013 ("Server is busy"). `connection_live` (an `asyncio.Event`) is set while connected.
- **Stream bridge:** two zero-buffer anyio memory streams adapt WebSocket frames to the MCP SDK's `SessionMessage` objects (`_read_from_socket` validates each frame as `JSONRPCMessage`; `_write_to_socket` serialises replies). The streams are created once per server and reused across reconnects.

## 5. Making reconnects safe (`session.py`)

`mcp.shared.session.BaseSession` closes the streams it is given when a session exits. Handing the server's single permanent stream pair to each `ClientSession` would let the first failed attempt close them forever, after which every retry fails instantly with `ClosedResourceError`. `ColabTransport.connect_session` therefore passes `clone()`d stream handles, so each attempt closes only its own. `await_proxy_connection` also cancels **and awaits** any unfinished start task before creating a new one, because two overlapping handshakes on the same stream would let the old one steal the new one's `initialize` response, and a finished task is never re-gathered (a cancelled task would raise immediately).

## 6. The public tools

| Tool | Behaviour |
| --- | --- |
| `open_browser(account_name)` | See section 3. |
| `load_notebook(browser_id, path)` | Parses the local `.ipynb` *to validate it*, checks the browser is connected, and records a **one-to-one binding** (`_notebook_bindings`, `_notebook_owners`): one notebook can belong to only one live browser session. A stale owner (disconnected) is evicted. Colab is not touched. |
| `sync_local(browser_id)` | Diffs the bound local notebook against `get_cells` and applies the minimal edit script (below). The local file is authoritative; Colab-side edits and outputs are never written back. Guarded by a per-binding `asyncio.Lock`. |
| `get_cells(browser_id)` | Raw cell state (ids, sources, outputs) for diagnosis. |
| `run_code_cell(browser_id, cellId)` | Runs an existing cell by id. |
| `run_shell(browser_id, command)` | Wraps the command in `%%bash` inside a temporary cell. |
| `mount_drive(browser_id)` | Runs the standard `drive.mount('/content/drive')` snippet in a temporary cell and polls `get_cells` (1 s interval, 300 s cap) until the cell leaves `running/pending/queued/busy`. |
| `get_live_system_metrics(browser_id)` | Runs a small script (parse `/proc/meminfo`, `shutil.disk_usage('/')`, `nvidia-smi --query-gpu=memory.used,memory.total`) and returns JSON `gpu_ram_percent` (null without a GPU), `cpu_ram_percent`, `disk_percent`. |

Add/update/delete/move cell are **not** public tools. They are private operations (`_forward_or_error`) used only by `sync_local` and by temporary cells.

### The sync algorithm

`load_ipynb_cells` returns normalised code/markdown cells (raw cells are counted as `skipped_raw`), plus the notebook language from `kernelspec.language`, else `language_info.name`, else `python`. For each desired cell at index `i`:

1. same type and source already at `i` -> `unchanged`;
2. an identical cell later in the remote list -> `move_cell` it to `i`;
3. a cell of the same type at `i` -> `update_cell` its source;
4. otherwise -> add a code or text cell at `i`.

Leftover remote cells past the end of the local list are deleted in reverse order. On the first transport error the tool stops and reports `partially applied: <counts>` so the caller knows exactly where it stopped. The result is a count of `unchanged/added/updated/moved/deleted`.

### Temporary cells

`_run_temporary_code` adds a cell at index 0, runs it, optionally polls for completion, and **always deletes it in a `finally`** (a failed cleanup is only logged), so shell commands and metric probes do not accumulate in the notebook.

### Error convention

Helpers return strings. A result starting with `Unknown browser_id`, `Browser session` or `Error calling` is a transport failure (`_is_transport_error`); the stale browser binding is released before returning so a dead session cannot hold a notebook hostage.

## 7. Process registry and transports

`process_registry` keeps a JSON list of running servers (pid, port, host, start time) in a per-OS location, checks liveness with stdlib-only code (including Windows), and offers `--list-running` and `--kill-stale`. Stale entries are pruned at startup and the process unregisters itself on exit; on exit every browser session is also cleaned up and the binding tables are cleared.

Transports: `MCP_TRANSPORT=stdio` (default) or `streamable-http`, which requires `MCP_SHARED_SECRET`; a raw ASGI middleware compares the `Authorization` header with `Bearer <secret>` and answers 401 otherwise. Host and port come from `MCP_HTTP_HOST` (default `127.0.0.1`) and `MCP_HTTP_PORT` (default 8767).

## 8. Tests

`uv run --group dev pytest` runs 51 tests: WebSocket server behaviour (origin and token rules, CORS preflight, single-client lock), session lifecycle and reconnects, `.ipynb` parsing, and `load_notebook`/`sync_local` logic against fake browser sessions. `scripts/e2e_smoke.py` and `scripts/manual_browser_test.py` drive a real Colab tab and need a signed-in browser.
