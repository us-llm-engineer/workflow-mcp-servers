# Copyright 2026 Google Inc.
# Licensed under the Apache License, Version 2.0.

import argparse
import asyncio
import datetime
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlencode
import uuid

from fastmcp import FastMCP
from fastmcp.utilities import logging as fastmcp_logger
from starlette.middleware import Middleware

from colab_mcp import process_registry
from colab_mcp.notebook_file import NormalizedCell, load_ipynb_cells
from colab_mcp.session import ColabSessionProxy, _open_url
from colab_mcp.websocket_server import COLAB, SCRATCH_PATH

mcp = FastMCP(name="ColabMCP")
_browser_sessions: dict[str, ColabSessionProxy] = {}


@dataclass
class _NotebookBinding:
    path: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_notebook_bindings: dict[str, _NotebookBinding] = {}
_notebook_owners: dict[Path, str] = {}
MOUNT_DRIVE_TIMEOUT_SECONDS = 300.0
POLL_INTERVAL_SECONDS = 1.0


class _BearerAuthMiddleware:
    def __init__(self, app, secret: str):
        self.app, self.secret = app, secret

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        auth = dict(scope.get("headers", [])).get(b"authorization", b"").decode("latin-1")
        if auth != f"Bearer {self.secret}":
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def _get_browser_session(browser_id: str) -> ColabSessionProxy | None:
    return _browser_sessions.get(browser_id)


def _release_binding(browser_id: str) -> None:
    binding = _notebook_bindings.pop(browser_id, None)
    if binding is not None and _notebook_owners.get(binding.path) == browser_id:
        _notebook_owners.pop(binding.path, None)


async def _forward_or_error(browser_id: str, tool_name: str, arguments: dict) -> str:
    """Private bridge for sync and ephemeral-cell operations."""
    session = _get_browser_session(browser_id)
    if session is None or session.proxy_client is None:
        _release_binding(browser_id)
        return f"Unknown browser_id {browser_id!r}. Call open_browser to create a browser session."
    if not session.proxy_client.is_connected():
        _release_binding(browser_id)
        return f"Browser session {browser_id!r} is not connected. Call open_browser to create a new browser session."
    try:
        result = await session.proxy_client.proxy_mcp_client.call_tool(tool_name, arguments)
        if hasattr(result, "content"):
            return "\n".join(c.text for c in result.content if hasattr(c, "text"))
        return str(result)
    except Exception as exc:
        return f"Error calling {tool_name} for browser_id {browser_id!r}: {exc}."


def _is_transport_error(result: str) -> bool:
    return result.startswith(("Unknown browser_id", "Browser session", "Error calling"))


def _normalise_remote_cell(cell: dict) -> dict:
    source = cell.get("source", "")
    source = "".join(str(part) for part in source) if isinstance(source, list) else str(source)
    return {"id": cell["id"], "cell_type": cell.get("cell_type", "code"), "source": source}


def _parse_cells(result: str) -> list[dict]:
    try:
        cells = json.loads(result)["cells"]
        if not isinstance(cells, list):
            raise ValueError("cells is not a list")
        return [_normalise_remote_cell(cell) for cell in cells if isinstance(cell, dict) and "id" in cell]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"get_cells returned an unexpected response: {result!r}") from exc


def _new_cell_id(result: str) -> str | None:
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("newCellId", "cellId", "id"):
        if isinstance(payload.get(key), str) and payload[key]:
            return payload[key]
    return None


async def _add_remote_cell(
    browser_id: str, cell: NormalizedCell, index: int, language: str = "python"
) -> tuple[str | None, str]:
    if cell.cell_type == "code":
        result = await _forward_or_error(
            browser_id,
            "add_code_cell",
            {"code": cell.source, "cellIndex": index, "language": language},
        )
    else:
        result = await _forward_or_error(browser_id, "add_text_cell", {"content": cell.source, "cellIndex": index})
    return _new_cell_id(result), result


async def _poll_cell_completion(browser_id: str, cell_id: str) -> None:
    deadline = asyncio.get_running_loop().time() + MOUNT_DRIVE_TIMEOUT_SECONDS
    while True:
        result = await _forward_or_error(browser_id, "get_cells", {})
        if _is_transport_error(result):
            raise RuntimeError(result)
        try:
            cell = next(cell for cell in json.loads(result).get("cells", []) if cell.get("id") == cell_id)
        except (json.JSONDecodeError, StopIteration, AttributeError):
            return
        state = str(cell.get("execution_state", cell.get("status", cell.get("state", "")))).lower()
        if state not in {"running", "pending", "queued", "busy"}:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Temporary cell did not finish within {MOUNT_DRIVE_TIMEOUT_SECONDS:g} seconds")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _run_temporary_code(browser_id: str, code: str, *, poll: bool = False) -> str:
    cell_id, add_result = await _add_remote_cell(browser_id, NormalizedCell("code", code), 0)
    if cell_id is None:
        return f"Could not create temporary cell: {add_result}"
    try:
        result = await _forward_or_error(browser_id, "run_code_cell", {"cellId": cell_id})
        if not _is_transport_error(result) and poll:
            await _poll_cell_completion(browser_id, cell_id)
        return result
    finally:
        cleanup = await _forward_or_error(browser_id, "delete_cell", {"cellId": cell_id})
        if _is_transport_error(cleanup):
            logging.warning("Temporary cell cleanup failed: %s", cleanup)


@mcp.tool()
async def open_browser(account_name: str) -> str:
    """Open a Colab tab, associating it with a caller-supplied account label."""
    if not isinstance(account_name, str) or not account_name.strip():
        return "account_name must be a non-empty account label."
    account_name = account_name.strip()
    browser_id = f"colab_{uuid.uuid4().hex}"
    session = ColabSessionProxy()
    try:
        await session.start_proxy_server()
        _browser_sessions[browser_id] = session
        assert session.proxy_client is not None and session.wss is not None
        # An email is not a valid authuser selector. Let Chrome/Colab use its
        # active authenticated profile; account_name remains session metadata.
        query = urlencode({"p": session.wss.port})
        _open_url(f"{COLAB}{SCRATCH_PATH}?{query}#mcpProxyToken={session.wss.token}&mcpProxyPort={session.wss.port}")
        await session.proxy_client.await_proxy_connection()
        if session.proxy_client.is_connected():
            tools = await session.proxy_client.await_tools_ready()
            return f"Browser ID: {browser_id}. Account: {account_name}. Connection successful. Available notebook tools: {', '.join(tools) or 'none discovered'}."
    except Exception as exc:
        logging.exception("Failed to create browser session %s", browser_id)
        failure = f" ({exc})"
    else:
        failure = " (the Colab tab did not connect before the timeout)"
    _browser_sessions.pop(browser_id, None)
    await session.cleanup()
    return f"Browser ID: {browser_id}. Connection failed{failure}"


@mcp.tool()
async def get_cells(browser_id: str) -> str:
    """Read the browser notebook state for diagnosis."""
    return await _forward_or_error(browser_id, "get_cells", {})


@mcp.tool()
async def run_code_cell(browser_id: str, cellId: str = "") -> str:
    """Execute an existing code cell by its browser-provided ID."""
    return await _forward_or_error(browser_id, "run_code_cell", {"cellId": cellId})


@mcp.tool()
async def load_notebook(browser_id: str, path: str) -> str:
    """Bind one local .ipynb file to browser_id without changing Colab yet."""
    try:
        load_ipynb_cells(path)
    except FileNotFoundError as exc:
        return f"Cannot load notebook: file not found at {path!r} ({exc})."
    except ValueError as exc:
        return f"Cannot load notebook: {exc}"
    session = _get_browser_session(browser_id)
    if session is None or session.proxy_client is None:
        _release_binding(browser_id)
        return f"Unknown browser_id {browser_id!r}. Call open_browser to create a browser session."
    if not session.proxy_client.is_connected():
        _release_binding(browser_id)
        return f"Browser session {browser_id!r} is not connected. Call open_browser to create a new browser session."
    notebook_path = Path(path).expanduser().resolve()
    owner = _notebook_owners.get(notebook_path)
    if owner is not None and owner != browser_id:
        owner_session = _get_browser_session(owner)
        owner_connected = (
            owner_session is not None
            and owner_session.proxy_client is not None
            and owner_session.proxy_client.is_connected()
        )
        if owner_connected:
            return f"Notebook {str(notebook_path)!r} is already bound to browser_id {owner!r}."
        _release_binding(owner)
    old = _notebook_bindings.get(browser_id)
    if old is not None and old.path != notebook_path:
        _notebook_owners.pop(old.path, None)
    _notebook_bindings[browser_id] = _NotebookBinding(notebook_path)
    _notebook_owners[notebook_path] = browser_id
    return f"Bound local notebook {str(notebook_path)!r} to browser_id={browser_id!r}. Call sync_local to apply its diff to Colab."


@mcp.tool()
async def sync_local(browser_id: str) -> str:
    """Synchronize the bound local notebook to Colab with minimal cell edits."""
    binding = _notebook_bindings.get(browser_id)
    if binding is None:
        return f"No local notebook is bound to browser_id {browser_id!r}. Call load_notebook first."
    async with binding.lock:
        try:
            local, language, skipped_raw = load_ipynb_cells(str(binding.path))
        except (FileNotFoundError, ValueError) as exc:
            return f"Cannot sync local notebook {str(binding.path)!r}: {exc}"
        result = await _forward_or_error(browser_id, "get_cells", {})
        if _is_transport_error(result):
            return result
        try:
            remote = _parse_cells(result)
        except ValueError as exc:
            return str(exc)
        counts = {"unchanged": 0, "added": 0, "updated": 0, "moved": 0, "deleted": 0}
        for index, desired in enumerate(local):
            if index < len(remote) and remote[index]["cell_type"] == desired.cell_type and remote[index]["source"] == desired.source:
                counts["unchanged"] += 1
                continue
            match = next((i for i in range(index + 1, len(remote)) if remote[i]["cell_type"] == desired.cell_type and remote[i]["source"] == desired.source), None)
            if match is not None:
                cell = remote.pop(match)
                result = await _forward_or_error(browser_id, "move_cell", {"cellId": cell["id"], "cellIndex": index})
                if _is_transport_error(result):
                    return f"sync_local partially applied: {counts}; move failed: {result}"
                remote.insert(index, cell)
                counts["moved"] += 1
            elif index < len(remote) and remote[index]["cell_type"] == desired.cell_type:
                result = await _forward_or_error(browser_id, "update_cell", {"cellId": remote[index]["id"], "content": desired.source})
                if _is_transport_error(result):
                    return f"sync_local partially applied: {counts}; update failed: {result}"
                remote[index]["source"] = desired.source
                counts["updated"] += 1
            else:
                cell_id, result = await _add_remote_cell(browser_id, desired, index, language)
                if cell_id is None:
                    return f"sync_local partially applied: {counts}; add failed: {result}"
                remote.insert(index, {"id": cell_id, "cell_type": desired.cell_type, "source": desired.source})
                counts["added"] += 1
        for cell in reversed(remote[len(local):]):
            result = await _forward_or_error(browser_id, "delete_cell", {"cellId": cell["id"]})
            if _is_transport_error(result):
                return f"sync_local partially applied: {counts}; delete failed: {result}"
            counts["deleted"] += 1
        return f"sync_local complete: {counts}; skipped_raw={skipped_raw}."


@mcp.tool()
async def mount_drive(browser_id: str) -> str:
    """Mount Drive in a temporary cell, wait for completion, then delete the cell."""
    try:
        return await _run_temporary_code(browser_id, "from google.colab import drive\ndrive.mount('/content/drive')", poll=True)
    except (RuntimeError, TimeoutError) as exc:
        return f"mount_drive failed: {exc}"


@mcp.tool()
async def run_shell(browser_id: str, command: str) -> str:
    """Run any shell command or multi-line script in a temporary %%bash cell."""
    if not isinstance(command, str) or not command:
        return "command must be a non-empty shell command string."
    return await _run_temporary_code(browser_id, f"%%bash\n{command}")


@mcp.tool()
async def get_live_system_metrics(browser_id: str) -> str:
    """Return JSON GPU-RAM, CPU-RAM, and root-disk usage percentages."""
    code = '''import json, shutil, subprocess
values = {}
with open("/proc/meminfo", encoding="utf-8") as handle:
    for line in handle:
        key, value = line.split(":", 1); values[key] = int(value.split()[0])
gpu = None
try:
    used, total = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"], text=True).strip().splitlines()[0].split(",")
    gpu = round(int(used.strip()) / int(total.strip()) * 100, 2)
except (FileNotFoundError, subprocess.CalledProcessError, IndexError, ValueError, ZeroDivisionError):
    pass
disk = shutil.disk_usage("/")
print(json.dumps({"gpu_ram_percent": gpu, "cpu_ram_percent": round((values["MemTotal"] - values["MemAvailable"]) / values["MemTotal"] * 100, 2), "disk_percent": round(disk.used / disk.total * 100, 2)}))'''
    result = await _run_temporary_code(browser_id, code)
    if _is_transport_error(result) or result.startswith("Could not create temporary cell"):
        return result
    match = re.search(r"\{[^\n]*\}", result)
    if match is None:
        return f"Could not parse live system metrics from temporary cell output: {result}"
    try:
        return json.dumps(json.loads(match.group(0)), sort_keys=True)
    except json.JSONDecodeError:
        return f"Could not parse live system metrics from temporary cell output: {result}"


def init_logger(logdir):
    log_filename = datetime.datetime.now().strftime(f"{logdir}/colab-mcp.%Y-%m-%d_%H-%M-%S.log")
    logging.basicConfig(format="%(asctime)s %(levelname)s:%(message)s", datefmt="%m/%d/%Y %I:%M:%S %p", filename=log_filename, level=logging.INFO)
    fastmcp_logger.get_logger("colab-mcp").info("logging to %s", log_filename)


def parse_args(v):
    parser = argparse.ArgumentParser(description="ColabMCP is an MCP server that lets you interact with Colab.")
    parser.add_argument("-l", "--log", action="store", default=tempfile.mkdtemp(prefix="colab-mcp-logs-"))
    parser.add_argument("-p", "--enable-proxy", action="store_true", default=True)
    parser.add_argument("--list-running", action="store_true", default=False)
    parser.add_argument("--kill-stale", action="store_true", default=False)
    return parser.parse_args(v)


def _print_running_servers() -> None:
    entries = process_registry.list_running()
    if not entries:
        print("No colab-mcp servers currently registered as running.")
        return
    print(f"Found {len(entries)} running colab-mcp server(s):")
    for entry in entries:
        started = datetime.datetime.fromtimestamp(entry.started_at).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  pid={entry.pid:<6}  port={entry.port:<6}  host={entry.host}  started={started}")


async def main_async():
    args = parse_args(sys.argv[1:])
    init_logger(args.log)
    if args.list_running:
        _print_running_servers()
        return
    if args.kill_stale:
        removed = process_registry.cleanup_stale(kill=True)
        print(f"Terminated {len(removed)} stale colab-mcp server(s)." if removed else "No stale colab-mcp servers found.")
        return
    dead = process_registry.prune_dead()
    if dead:
        logging.info("Pruned %s stale entries from process registry", dead)
    try:
        transport = os.environ.get("MCP_TRANSPORT", "stdio")
        if transport == "stdio":
            await mcp.run_async()
        elif transport == "streamable-http":
            secret = os.environ.get("MCP_SHARED_SECRET")
            if not secret:
                raise ValueError("MCP_SHARED_SECRET is required for streamable-http mode")
            await mcp.run_http_async(transport="streamable-http", host=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"), port=int(os.environ.get("MCP_HTTP_PORT", "8767")), middleware=[Middleware(_BearerAuthMiddleware, secret=secret)])
        else:
            raise ValueError(f"Unknown MCP_TRANSPORT {transport!r}; use 'stdio' or 'streamable-http'")
    finally:
        sessions = list(_browser_sessions.values())
        _browser_sessions.clear()
        _notebook_bindings.clear()
        _notebook_owners.clear()
        for browser_session in sessions:
            await browser_session.cleanup()
        try:
            process_registry.unregister()
        except Exception as exc:
            logging.warning("Could not unregister process: %s", exc)


def main() -> None:
    asyncio.run(main_async())
