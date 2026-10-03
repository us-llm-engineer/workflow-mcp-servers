# Changelog

All notable changes to this fork of [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) are documented here.

This fork follows the upstream `1.0.x` baseline and tags fork-specific work
with the date of the change. Upstream-merged work keeps its own commit
history.

## 2026-09-29 — Local-first synchronization surface

### Changed
- Replaced `open_colab_browser_connection()` with
  `open_browser(account_name)`. `account_name` is session metadata while every
  call still receives an isolated browser ID and WebSocket proxy.
- Changed `load_notebook(browser_id, path)` from an immediate destructive load
  into a local-only one-to-one binding operation.
- Added `sync_local(browser_id)`, which compares the bound local notebook with
  Colab and applies only the required add/update/move/delete operations. The
  local notebook is authoritative; browser outputs and edits are not written
  back locally.
- Removed add, text-add, update, delete, and move from the public MCP surface.
  They remain private bridge operations used only by synchronization and
  temporary cells.

### Added
- `get_live_system_metrics(browser_id)` for GPU RAM, CPU RAM, and disk usage
  percentages.
- `mount_drive(browser_id)` using the standard `google.colab.drive` mount code.
- `run_shell(browser_id, command)` for arbitrary single- or multi-line shell
  input via `%%bash`.
- Drive mounting, metrics, and shell execution use temporary cells that are
  deleted in a `finally` path after execution.

### Fixed
- Stopped passing account emails as Colab's `authuser` query selector. This
  could select a stale signed-out profile and loop the browser through sign-in.

The older 2026-09-28 `load_notebook` entry below documents the superseded
clear-and-recreate implementation for historical context.

## 2026-09-28 — Removed change_runtime and OAuth support

Live-tested the actual "Change runtime type" flow end-to-end with browser
traffic capture (Reconnect to a CPU runtime, then dropdown next to
Connect/Reconnect > Change runtime type > T4 > Save > Disconnect-and-reassign
confirmation > back to CPU). Both the initial connect and the type change hit
the same endpoint, `POST https://colab.clients6.google.com/$rpc/google.internal.colab.v1.RuntimeService/CreateAssignment`,
followed by periodic `KeepAliveAssignment` polls. The resources panel also
showed a real, UI-surfaced "You have 1 active session" / "Manage sessions"
concurrency limit, corroborating the concurrent-session exhaustion this
fork's `change_runtime` stub already warned about.

The traffic-capture tool available in this session only exposes URL, method,
and status code — not request/response bodies — so the actual payload (in
particular, the notebook identity/hash the browser sends) could not be
inspected. Without that, there was no way to verify whether `client.py`'s
prior `ColabClient.assign()` attempt failed specifically because it used a
random `notebook_hash` (as its own code comment speculated) versus some other
reason. Attempting a "fix" on that unverified guess risked repeating the
exact orphaned-VM/quota-exhaustion incident already documented in git
history for this tool — so no REST-based fix was attempted.

### Removed
- **`change_runtime` tool** — it could only ever explain the limitation, not
  work around it; keeping it around invited an agent to call it and get a
  wall of text instead of a GPU.
- **`--client-oauth-config` CLI flag** and the OAuth init block in
  `main_async()` — had no remaining consumer.
- **`src/colab_mcp/auth.py`** and **`src/colab_mcp/client.py`** — deleted
  entirely (dead code with `change_runtime` gone). Dropped the
  `google-auth-oauthlib` and `requests` dependencies from `pyproject.toml`
  since they were only imported by these two files.

### Added
- `open_colab_browser_connection`'s docstring and returned message now tell
  the calling agent explicitly: if the task needs a GPU or TPU, stop and ask
  a human to select it in the browser (dropdown next to
  "Connect"/"Reconnect" > "Change runtime type" > select accelerator > Save)
  — this server cannot do it programmatically.

### Noted, not acted on
- The real "Change runtime type" dialog offers CPU, T4 GPU, L4 GPU, A100
  GPU, G4 GPU, H100 GPU (greyed out/unavailable), v5e-1 TPU, and v6e-1 TPU —
  broader than the removed tool's old docstring (`NONE, T4, L4, A100`). Moot
  now that the tool is gone, but useful if a future contributor revisits
  browser-driven (not REST) runtime automation.

## 2026-09-28 — Added load_notebook tool

### Added
- **`load_notebook(browser_id, path)`** — parses a local `.ipynb` file with
  stdlib `json` (no new `nbformat` dependency; see
  `src/colab_mcp/notebook_file.py`), unconditionally clears every existing
  cell in the target browser tab via `get_cells` + `delete_cell`, then adds
  the file's code/markdown cells in order via `add_code_cell`/`add_text_cell`.
  Raw cells are skipped and counted in the summary. Never calls
  `run_code_cell` — cells are populated, not executed. `path` is resolved on
  the machine running this MCP server, not inside the browser.

### Assumed, NOT yet empirically confirmed against a real Colab tab
- `cellIndex` passed to `add_code_cell`/`add_text_cell` during a
  `load_notebook` call is a single running 0-based counter shared across
  both cell types, incremented once per cell in file order. This mirrors
  how `move_cell`'s real parameter name (`cellIndex`, not `toIndex`) was
  only confirmed via live E2E on 2026-06-19 below — the *value* semantics of
  `cellIndex` on `add_code_cell`/`add_text_cell` specifically have not had
  the same live confirmation yet. Run
  `uv run python scripts/e2e_smoke.py --connect --load-notebook tests/fixtures/sample_notebook.ipynb`
  against a real tab before trusting this ordering assumption in production.

## 2026-06-19 — E2E validated against real Colab + move_cell signature correction

After driving the full smoke E2E against a real Colab notebook (see
`scripts/e2e_smoke.py --connect`), all 7 notebook tools were verified
end-to-end with cellIds returned by the actual browser. One signature
guess turned out to be wrong:

### Fixed
- **`move_cell` parameter renamed `toIndex` → `cellIndex`**. The browser
  rejected `toIndex` with:
  > Invalid arguments for move_cell: cellIndex Required (received undefined)
  Real signature is `move_cell(cellId: str, cellIndex: int)`.

### Verified against real Colab (E2E)
- `add_code_cell` returns `{"newCellId": "<id>"}` in the result body (not
  in structured_content). The E2E script now parses that and threads the
  real cellId through downstream calls.
- `get_cells` returns `{"cells": [{"cell_type": "code", "id": "<id>",
  "source": [...]}]}`.
- `run_code_cell(cellId)` returns `{"outputs": [{"output_type": "stream",
  "name": "stdout", "text": ["..."]}]}` after execution.
- `update_cell(cellId, content)`, `move_cell(cellId, cellIndex)`,
  `delete_cell(cellId)` all return `{}` on success.

### Tools added
- `scripts/manual_browser_test.py` — starts the WebSocket server, prints
  the Colab URL, and waits up to 5 min for any browser to connect.
  Used to confirm that the server itself works (Edge connects on first
  attempt) and isolate browser-specific blocks (Chrome permission cache).

### Docs
- New README troubleshooting entry for the Chrome "previously blocked"
  permission cache — the most common cause of persistent "Disconnected"
  on Chrome that is not solved by the dual-stack or PNA fixes.

## 2026-06-18 — Private Network Access (PNA) handshake — the REAL real fix

After the dual-stack bind fix (earlier today) the Colab tab still showed
"Disconnected from the local Colab MCP server" with a generic 60s timeout.
DevTools Network tab on the failing tab revealed the actual cause:

- The WebSocket request stayed at "Provisional headers are shown" with
  Connection Start → Stalled "2.3 days".
- Server log only saw `stream ends after 0 bytes` — Chrome was opening
  the TCP socket and then closing it before sending any HTTP bytes.

This is Chrome's [Private Network Access](https://developer.chrome.com/blog/private-network-access-preflight)
behavior. A public origin (`https://colab.research.google.com`) talking
to a local server (`ws://localhost`) is classified as a "private network
request" and Chrome blocks it unless the server confirms acceptance via
specific CORS headers — both on the CORS preflight AND on the final
WebSocket upgrade response.

The upstream `googlecolab/colab-mcp` does not send these headers, which
is why "Disconnected" hits every user who runs Chrome with default
security settings. The earlier theories (orphaned servers, stale tabs)
were correlated symptoms but not the cause.

### Fixed
- `_validate_authorization` (now also `process_request`) intercepts any
  non-WebSocket request (no `Upgrade: websocket` header) and responds
  204 No Content with:
  - `Access-Control-Allow-Origin: https://colab.research.google.com`
  - `Access-Control-Allow-Methods: GET, OPTIONS`
  - `Access-Control-Allow-Headers: authorization, content-type, sec-websocket-*`
  - `Access-Control-Allow-Private-Network: true`
  - `Access-Control-Allow-Credentials: true`
  - `Access-Control-Max-Age: 86400`
- New `_augment_handshake_response` (`process_response` callback) adds
  the same headers to the 101 Switching Protocols response. Chrome
  re-checks PNA on the upgrade response itself — preflight alone is not
  sufficient.

### Tests added
- `test_cors_preflight_responds_with_pna_headers` — sends a raw HTTP GET
  (no Upgrade) and asserts the 204 response carries PNA + CORS headers.
- `test_websocket_handshake_response_has_pna_header` — completes a real
  WebSocket handshake and asserts the 101 response carries the PNA
  header.

## 2026-06-18 — Root-cause fix for "Disconnected from the local Colab MCP server"

The real cause of [upstream #84](https://github.com/googlecolab/colab-mcp/discussions/84) was not stale servers or stale tabs — it was an **IPv4/IPv6 dual-stack bind bug** in the WebSocket server. With `host="localhost"` + `port=0`, the `websockets` library binds two sockets on **different ephemeral ports** (one IPv6 `::1:X`, one IPv4 `127.0.0.1:Y`), then we report only one of them. When Chrome resolves `ws://localhost:<reported>` to whichever family lost the lottery, it connects to a port with **no listener**, the TCP connection drops with "stream ends after 0 bytes", and the Colab tab shows "Disconnected from the local Colab MCP server" with no retry. The user then waits 60s for a generic timeout.

This was reproduced and root-caused by reading the server logs during a smoke E2E run: lines `server listening on [::1]:52319` and `server listening on 127.0.0.1:52320` for the SAME server instance.

### Fixed
- `ColabWebSocketServer.__init__` default host changed from `"localhost"` to
  `"127.0.0.1"`, forcing IPv4-only bind. Single socket, single port, no
  ambiguity. Chrome resolves `localhost` to `127.0.0.1` on Windows and most
  desktop OSes, so this is transparent for the Colab tab.
- `__aenter__` now validates that every bound socket shares the same port,
  raising `RuntimeError` with diagnostic detail if not. This is a hard
  guard against future regressions (e.g., someone changing `host` back to
  `"localhost"` or to a `(v4, v6)` tuple without picking a fixed port).
- Startup logging now lists every bound socket address (not just the first
  one) plus the URL the Colab tab will actually use.

### Tests added
- `test_single_socket_single_port` — fails if the server binds more than one
  port (the dual-stack regression).
- `test_default_host_is_ipv4` — fails if the default host is set to anything
  other than `"127.0.0.1"`.

## 2026-06-16 — Stale-tab dedup fix + better timeout diagnostics

Chrome dedupes browser tabs by URL canonical, ignoring the `#fragment`. That
meant calling `open_colab_browser_connection` a second time (after the first
server died) would silently reuse the old Colab tab whose fragment pointed at
the dead port, instead of opening a fresh tab for the live server. The old
tab showed "Disconnected from the local Colab MCP server" and the new server
timed out 60s later with no actionable feedback.

### Fixed
- `open_colab_browser_connection` now appends `?p=<port>` to the Colab URL as
  a query param. Because the port is ephemeral and changes per server
  instance, the URL is unique → Chrome cannot dedupe → a fresh tab opens
  every time. The `#fragment` remains the source of truth for the Colab
  browser-side code (token + port), so this is purely a Chrome-side fix.

### Changed
- The timeout error returned by `open_colab_browser_connection` now lists the
  three actual causes (stale tabs, Local Network Access denied, browser not
  opening) with specific remediation, instead of a generic "make sure you
  have a Colab notebook open" message.

### Docs
- README troubleshooting: new entries for **Chrome reused an old Colab tab**
  and **Chrome asks for "Permission to access other services"** (Local
  Network Access prompt), with explanations of why each is required.

## 2026-06-16 — Pre-register full 7-tool notebook surface

Closes the gap reported in [discussion #69](https://github.com/googlecolab/colab-mcp/discussions/69): the post-connection success message advertised seven notebook tools, but only four were pre-registered as stubs. `get_cells`, `delete_cell`, and `move_cell` were unreachable on clients that snapshot tools at startup (Claude Code, Codex). Without `get_cells` the bridge was effectively write-only — an agent could create cells but never read state back.

### Added
- Three new pre-registered tools matching the browser-side handler names:
  - `get_cells()` — read current notebook state (cells, IDs, contents, outputs).
  - `delete_cell(cellId)` — delete a cell by ID.
  - `move_cell(cellId, toIndex)` — move a cell to a new position.
- Matching stubs in `_make_injected_tools` (`session.py`) so all seven tools are
  visible at startup with `NOT_CONNECTED_MSG` until the browser connects.

### Changed (breaking)
- `execute_cell` → renamed to `run_code_cell` to match the browser-side tool
  name directly (no more wrapper). The old `execute_cell` no longer exists.
  Callers must migrate to `run_code_cell(cellId)`. The `cellIndex` fallback
  argument was removed — pass a `cellId` from `add_code_cell` or `get_cells`.

### Caveat
- Parameter signatures for `delete_cell` and `move_cell` are best-guess (the
  browser-side JS handlers are not in this repo). Signatures follow the
  established `cellId`-first pattern used by `update_cell` and `run_code_cell`.
  Adjust in review if the real browser handlers differ.

## 2026-05-14 — Stale-server detection and cleanup

Adds a process registry that solves the "Disconnected from the local Colab MCP server" issue ([upstream discussion #84](https://github.com/googlecolab/colab-mcp/discussions/84)) caused by orphaned servers from prior Claude Code sessions.

### Added
- `src/colab_mcp/process_registry.py` — cross-platform process registry (stdlib only).
  Tracks `{pid, port, host, started_at}` for each running server in:
  - `%LOCALAPPDATA%\colab-mcp\registry.json` on Windows
  - `~/.colab-mcp/registry.json` on macOS/Linux
- CLI flag `--list-running` — print every currently-running `colab-mcp` server.
- CLI flag `--kill-stale` — terminate orphaned `colab-mcp` servers and exit.
- Automatic registration on server startup (after the WebSocket port is bound)
  and unregistration on clean shutdown.
- Automatic pruning of dead registry entries on every startup.

### Changed
- `open_colab_browser_connection` timeout error now reports this server's port
  *and* the PIDs/ports of any peer `colab-mcp` processes, so users can
  immediately recognize the "old browser tab pointed at a dead port" case
  instead of staring at a generic "timed out" message.

### Docs
- README: new **CLI Reference** section, new troubleshooting entry, new row in
  "What's Different" table.
- Changes from Upstream: stale-server detection bullet added.

### Verification
Verified end-to-end on Windows 10 with `uv run` and `uvx`:
- Module loads under uv-managed venv (no import errors).
- `--list-running` correctly reports no servers when none registered.
- `--kill-stale` removes a synthetic registry entry whose PID is dead
  (`Terminated 1 stale colab-mcp server(s): pid=888888 port=55555`) and
  empties the registry file.
- Real `colab-mcp` server startup logs `Registered colab-mcp pid=X port=Y`.
- Clean shutdown removes the server's own entry (subsequent `--list-running`
  shows none).

## Earlier (upstream + previous fork commits)

See `git log` for the full history. Highlights:

- `f70c00d` — pre-register notebook tools at startup (fixes invisible tools in
  MCP clients that don't support `notifications/tools/list_changed`). Originally
  4 stubs; expanded to 7 in the 2026-06-16 entry above.
- `cae498b` — add `change_runtime` tool with OAuth for programmatic GPU
  assignment.
- `e66ee69` — match real Colab API signatures (language param, cellId,
  run_code_cell).
- `440e3bc` — fix `ColabClient` initialization (missing `Prod()` env arg) +
  change OAuth port to 8085 for Windows.
