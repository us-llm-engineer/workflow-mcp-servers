# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
from collections.abc import AsyncIterator
import contextlib
from contextlib import AsyncExitStack
import logging
import os
import subprocess
from fastmcp import Client
from fastmcp.client.transports import ClientTransport
from mcp.client.session import ClientSession
import webbrowser

from colab_mcp.websocket_server import ColabWebSocketServer

logger = logging.getLogger(__name__)

WINDOWS_CHROME_PATHS = (
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
)

UI_CONNECTION_TIMEOUT = 60.0  # secs
TOOLS_READY_TIMEOUT = 10.0  # secs
TOOLS_READY_POLL_INTERVAL = 0.5  # secs

class ColabTransport(ClientTransport):
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss

    @contextlib.asynccontextmanager
    async def connect_session(self, **session_kwargs) -> AsyncIterator[ClientSession]:
        # mcp.shared.session.BaseSession._receive_loop does
        # `async with (self._read_stream, self._write_stream):`, so exiting a
        # ClientSession - for ANY reason, including a timeout-induced
        # cancellation - always closes whatever streams it was given. wss's
        # read/write streams are a single permanent pair reused across every
        # reconnect attempt for the life of the server process, so handing
        # them in directly means the first ClientSession to exit closes them
        # forever, and every later attempt fails instantly with
        # "Server session was closed unexpectedly" (anyio.ClosedResourceError)
        # without ever touching the socket. clone() hands out an independent
        # closeable handle to the same underlying stream: closing one clone
        # doesn't affect the others still open, so each connection attempt
        # can freely close its own handle on exit.
        async with ClientSession(
            self.wss.read_stream.clone(), self.wss.write_stream.clone(), **session_kwargs
        ) as session:
            yield session

    def __repr__(self) -> str:
        return "<ColabSessionProxyTransport>"


class ColabProxyClient:
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss
        self.proxy_mcp_client: Client | None = None
        self._exit_stack = AsyncExitStack()
        self._start_task = None

    def is_connected(self):
        return self.wss.connection_live.is_set() and self.proxy_mcp_client is not None

    async def await_proxy_connection(self):
        # _start_task is a one-shot task: once it finishes (success, timeout-
        # induced cancellation, or error) it can never transition back to
        # "in progress" on its own. Re-gathering an already-done task here is
        # what causes retries to hang - a task cancelled by a prior timeout
        # raises CancelledError immediately on the next gather, which isn't
        # caught by the TimeoutError suppression below, so the tool call
        # never returns. Restart it whenever it isn't actively running.
        #
        # A task that's merely *unfinished* (still mid-handshake from a prior
        # call) must be cancelled and fully awaited - not just abandoned -
        # before starting a new one. Both attempts read from the same
        # single, permanent wss.read_stream/write_stream (they're bound once
        # per server process, reused across every browser tab), so leaving a
        # half-torn-down ClientSession running would let it silently steal
        # the initialize response meant for the new attempt, causing the new
        # attempt to hang waiting for a reply that already went elsewhere.
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
            with contextlib.suppress(BaseException):
                await self._start_task
        if self._start_task is None or self._start_task.done():
            self._start_task = asyncio.create_task(self._start_proxy_client())

        with contextlib.suppress(asyncio.TimeoutError):
            # wait for the connection to be live and for the proxy client to fully initialize
            connection_tasks = asyncio.gather(
                self.wss.connection_live.wait(), self._start_task
            )
            await asyncio.wait_for(
                connection_tasks,
                timeout=UI_CONNECTION_TIMEOUT,
            )

    async def await_tools_ready(self) -> list[str]:
        """Poll the proxy client until remote tools are available."""
        if not self.is_connected():
            return []
        elapsed = 0.0
        while elapsed < TOOLS_READY_TIMEOUT:
            try:
                tools = await self.proxy_mcp_client.list_tools()
                if tools:
                    return [t.name for t in tools]
            except Exception:
                pass
            await asyncio.sleep(TOOLS_READY_POLL_INTERVAL)
            elapsed += TOOLS_READY_POLL_INTERVAL
        return []

    async def _start_proxy_client(self):
        # blocks until a websocket connection is made successfully
        self.proxy_mcp_client = await self._exit_stack.enter_async_context(
            Client(ColabTransport(self.wss))
        )

    async def __aenter__(self):
        self._start_task = asyncio.create_task(self._start_proxy_client())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._start_task:
            self._start_task.cancel()
        await self._exit_stack.aclose()


def _open_url(url: str) -> None:
    """Open `url` in a browser, preferring Windows Chrome when running under WSL.

    Under WSL, Python's `webbrowser` module resolves to whatever browser is
    installed inside the Linux subsystem (often a Linux Chrome/Chromium via
    WSLg), which is noticeably slower than the host Windows browser. Launch
    the Windows executable directly instead of routing the URL through
    ``cmd.exe start``: Colab's URL fragment contains ``&mcpProxyPort=...``,
    and cmd.exe interprets an unquoted ``&`` as a command separator. That
    opened a tab with the token but without its proxy port, so no browser
    session could connect. If Windows Chrome isn't available (for example on
    a non-WSL host), fall back to the normal cross-platform browser launcher.
    """
    for chrome_path in WINDOWS_CHROME_PATHS:
        if not os.path.isfile(chrome_path):
            continue
        try:
            subprocess.Popen(
                [chrome_path, url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            logger.warning("Failed to launch Windows Chrome directly; falling back", exc_info=True)
    webbrowser.open_new(url)


class ColabSessionProxy:
    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self.proxy_client: ColabProxyClient | None = None
        self.wss: ColabWebSocketServer | None = None

    async def start_proxy_server(self):
        self.wss = await self._exit_stack.enter_async_context(ColabWebSocketServer())
        self.proxy_client = await self._exit_stack.enter_async_context(
            ColabProxyClient(self.wss)
        )

    async def cleanup(self):
        await self._exit_stack.aclose()
