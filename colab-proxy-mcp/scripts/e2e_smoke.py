"""Smoke-test the local-first Colab MCP surface.

By default this verifies tool discovery and disconnected behavior. Pass
``--connect ACCOUNT_EMAIL --notebook PATH`` for the interactive browser flow.
"""

import argparse
import asyncio
import re
from pathlib import Path

from fastmcp import Client

import colab_mcp


EXPECTED_TOOLS = {
    "open_browser",
    "load_notebook",
    "sync_local",
    "get_cells",
    "run_code_cell",
    "get_live_system_metrics",
    "mount_drive",
    "run_shell",
}


def text(result) -> str:
    return "\n".join(item.text for item in result.content if hasattr(item, "text"))


async def disconnected(client: Client) -> int:
    tools = await client.list_tools()
    names = {tool.name for tool in tools}
    failures = 0
    if names != EXPECTED_TOOLS:
        print(f"FAIL tool surface: expected {sorted(EXPECTED_TOOLS)}, got {sorted(names)}")
        failures += 1
    else:
        print("OK exact eight-tool surface")
    result = await client.call_tool("get_cells", {"browser_id": "missing"})
    if "Unknown browser_id" not in text(result):
        print(f"FAIL disconnected diagnostic: {text(result)}")
        failures += 1
    else:
        print("OK disconnected browser diagnostic")
    return failures


async def connected(client: Client, account: str, notebook: str) -> int:
    failures = 0
    opened = text(await client.call_tool("open_browser", {"account_name": account}))
    match = re.search(r"Browser ID: (\S+?)\.", opened)
    if not match or "Connection successful" not in opened:
        print(f"FAIL open_browser: {opened}")
        return 1
    browser_id = match.group(1)
    print(f"OK connected {browser_id} for {account}")

    bound = text(await client.call_tool("load_notebook", {"browser_id": browser_id, "path": notebook}))
    if "Bound local notebook" not in bound:
        print(f"FAIL load_notebook: {bound}")
        failures += 1
    synced = text(await client.call_tool("sync_local", {"browser_id": browser_id}))
    if "sync_local complete" not in synced:
        print(f"FAIL sync_local: {synced}")
        failures += 1
    else:
        print(f"OK {synced}")

    metrics = text(await client.call_tool("get_live_system_metrics", {"browser_id": browser_id}))
    print(f"metrics: {metrics}")
    return failures


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", metavar="ACCOUNT_EMAIL")
    parser.add_argument("--notebook", type=Path)
    args = parser.parse_args()
    if bool(args.connect) != bool(args.notebook):
        parser.error("--connect and --notebook must be supplied together")

    failures = 0
    async with Client(colab_mcp.mcp) as client:
        failures += await disconnected(client)
        if args.connect:
            failures += await connected(client, args.connect, str(args.notebook.resolve()))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
