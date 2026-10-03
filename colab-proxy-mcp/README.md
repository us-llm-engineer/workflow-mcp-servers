# Colab MCP (local-first fork)

An MCP server for synchronizing local Jupyter notebooks into isolated Google
Colab browser sessions. The local `.ipynb` file is authoritative; direct
remote cell editing is intentionally not part of the public API.

## Public tools

| Tool | Purpose |
|---|---|
| `open_browser(account_name)` | Open an isolated Colab session, associate it with an account label, and return its browser ID. |
| `load_notebook(browser_id, path)` | Bind one local `.ipynb` to one browser session without changing Colab. |
| `sync_local(browser_id)` | Compare local and remote cells and minimally update Colab until it matches the local file. |
| `get_cells(browser_id)` | Read cells, IDs, contents, and outputs for diagnosis. |
| `run_code_cell(browser_id, cellId)` | Execute an existing cell by ID. |
| `get_live_system_metrics(browser_id)` | Return GPU RAM, CPU RAM, and disk usage percentages. |
| `mount_drive(browser_id)` | Mount Google Drive through a temporary cell that is deleted after completion. |
| `run_shell(browser_id, command)` | Run any single- or multi-line shell string through a temporary `%%bash` cell. |

The internal Colab bridge still uses add/update/move/delete operations to apply
a sync and to manage temporary cells. Those operations are not MCP tools.

## Local-first workflow

1. Call `open_browser(account_name="person@example.com")`.
2. Use the returned browser ID with `load_notebook(browser_id, "/absolute/notebook.ipynb")`.
3. Edit the notebook locally.
4. Call `sync_local(browser_id)` whenever the local changes should be applied.
5. Use `get_cells` to inspect the live state and `run_code_cell` to execute a cell.

`sync_local` does not execute cells and does not copy Colab edits or outputs
back into the local file. A local path can only be bound to one live browser
ID at a time. Rebinding a browser ID to another notebook releases its previous
path.

## Installation

Install [uv](https://docs.astral.sh/uv/), clone the repository, and configure
your MCP client:

```json
{
  "mcpServers": {
    "colab-proxy-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/colab-mcp", "colab-mcp"],
      "timeout": 30000
    }
  }
}
```

The default transport is stdio. Streamable HTTP remains available through
`MCP_TRANSPORT=streamable-http` and requires `MCP_SHARED_SECRET`.

## Browser and runtime notes

- `account_name` is a caller-visible session label. Chrome and Colab select the
  active signed-in browser profile; the connector deliberately does not force
  an email into Colab's `authuser` selector, which can reopen a stale signed-out
  profile in a sign-in loop.
- Chrome must allow Colab's Local Network Access request so the tab can connect
  to the local WebSocket proxy.
- GPU/TPU runtime selection remains a manual Colab UI operation.
- `get_live_system_metrics` reports `gpu_ram_percent: null` if `nvidia-smi` is
  unavailable.
- `mount_drive` waits up to five minutes for the temporary mount cell.

## Diagnostics

```bash
uv run colab-mcp --list-running
uv run colab-mcp --kill-stale
uv run pytest -q
```

The process registry is stored under the platform-specific user configuration
directory and stale entries are pruned at startup.

## License

Apache 2.0.
