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
from types import SimpleNamespace
from colab_mcp import session
from fastmcp import Client
import pytest
from unittest.mock import patch, AsyncMock, Mock


@pytest.fixture
def mock_wss():
    """Provides a mock ColabWebSocketServer instance."""
    return MockColabWebSocketServer()


class MockColabWebSocketServer:
    def __init__(self):
        self.connection_live = asyncio.Event()
        self.read_stream = Mock()
        self.write_stream = Mock()
        self.token = "test-token"
        self.port = 1234

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class FakeBrowserProxy:
    def __init__(self, port: int, token: str, *, connected: bool = True):
        self.wss = SimpleNamespace(port=port, token=token)
        self.connected = connected
        self.proxy_mcp_client = SimpleNamespace(call_tool=AsyncMock())
        self.await_tools_ready = AsyncMock(return_value=["get_cells"])

    def is_connected(self):
        return self.connected

    async def await_proxy_connection(self):
        return None


class FakeBrowserSession:
    def __init__(self, port: int, token: str, *, connected: bool = True):
        self.wss = None
        self.proxy_client = None
        self._proxy = FakeBrowserProxy(port, token, connected=connected)
        self.cleaned_up = False

    async def start_proxy_server(self):
        self.wss = self._proxy.wss
        self.proxy_client = self._proxy

    async def cleanup(self):
        self.cleaned_up = True


@pytest.fixture(autouse=True)
def clear_browser_sessions():
    import colab_mcp

    colab_mcp._browser_sessions.clear()
    yield
    colab_mcp._browser_sessions.clear()


@pytest.fixture
def mock_proxy_client(mock_wss):
    client = Mock(spec=session.ColabProxyClient)
    client.wss = mock_wss
    client.is_connected.return_value = False
    return client


class TestDirectTools:
    """Tests for the direct tool registration on the mcp server."""

    @pytest.mark.asyncio
    async def test_mcp_has_expected_tools(self):
        from colab_mcp import mcp
        async with Client(mcp) as client:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}
            assert tool_names == {
                "open_browser",
                "get_cells",
                "run_code_cell",
                "load_notebook",
                "sync_local",
                "get_live_system_metrics",
                "mount_drive",
                "run_shell",
            }
            for tool in tools:
                if tool.name != "open_browser":
                    assert "browser_id" in tool.inputSchema["required"]

    @pytest.mark.asyncio
    async def test_open_creates_independent_sessions_and_unique_browser_ids(self, monkeypatch):
        import colab_mcp

        sessions = [
            FakeBrowserSession(31001, "token-one"),
            FakeBrowserSession(31002, "token-two"),
        ]
        opened_urls = []
        monkeypatch.setattr(colab_mcp, "ColabSessionProxy", lambda: sessions.pop(0))
        monkeypatch.setattr(colab_mcp, "_open_url", opened_urls.append)

        first, second = await asyncio.gather(
            colab_mcp.open_browser.fn("one@example.com"),
            colab_mcp.open_browser.fn("two@example.com"),
        )

        browser_ids = [message.split(". ", 1)[0].split(": ", 1)[1] for message in (first, second)]
        assert len(set(browser_ids)) == 2
        assert set(browser_ids) == set(colab_mcp._browser_sessions)
        assert len(opened_urls) == 2
        assert "mcpProxyPort=31001" in opened_urls[0]
        assert "mcpProxyPort=31002" in opened_urls[1]
        assert "authuser=" not in opened_urls[0]
        assert "authuser=" not in opened_urls[1]
        assert "Account: one@example.com" in first
        assert "Account: two@example.com" in second

    @pytest.mark.asyncio
    async def test_open_rejects_empty_account_name_before_starting_session(self, monkeypatch):
        import colab_mcp

        factory = Mock()
        monkeypatch.setattr(colab_mcp, "ColabSessionProxy", factory)
        result = await colab_mcp.open_browser.fn("   ")
        assert "non-empty account label" in result
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_call_routes_only_to_its_browser_id(self):
        import colab_mcp

        first = FakeBrowserSession(31001, "token-one")
        second = FakeBrowserSession(31002, "token-two")
        await first.start_proxy_server()
        await second.start_proxy_server()
        first._proxy.proxy_mcp_client.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="from-first")]
        )
        second._proxy.proxy_mcp_client.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="from-second")]
        )
        colab_mcp._browser_sessions.update({"first": first, "second": second})

        result = await colab_mcp.get_cells.fn("second")

        assert result == "from-second"
        second._proxy.proxy_mcp_client.call_tool.assert_awaited_once_with(
            "get_cells", {}
        )
        first._proxy.proxy_mcp_client.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_browser_id_is_rejected_without_fallback(self):
        import colab_mcp

        result = await colab_mcp.get_cells.fn("does-not-exist")

        assert result == (
            "Unknown browser_id 'does-not-exist'. Call "
            "open_browser to create a browser session."
        )

    @pytest.mark.asyncio
    async def test_failed_connection_cleans_up_its_unusable_browser_id(self, monkeypatch):
        import colab_mcp

        failed_session = FakeBrowserSession(31001, "token-one", connected=False)
        monkeypatch.setattr(colab_mcp, "ColabSessionProxy", lambda: failed_session)
        monkeypatch.setattr(colab_mcp, "_open_url", Mock())

        result = await colab_mcp.open_browser.fn("one@example.com")

        browser_id = result.split(". ", 1)[0].split(": ", 1)[1]
        assert "Connection failed" in result
        assert browser_id not in colab_mcp._browser_sessions
        assert failed_session.cleaned_up is True


class TestAwaitToolsReady:
    """Tests for await_tools_ready polling."""

    @pytest.mark.asyncio
    async def test_returns_tool_names(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        client.proxy_mcp_client = AsyncMock()
        mock_tool = Mock()
        mock_tool.name = "add_code_cell"
        client.proxy_mcp_client.list_tools = AsyncMock(return_value=[mock_tool])

        result = await client.await_tools_ready()
        assert result == ["add_code_cell"]

    @pytest.mark.asyncio
    async def test_polls_until_available(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        client.proxy_mcp_client = AsyncMock()
        mock_tool = Mock()
        mock_tool.name = "run_code_cell"
        client.proxy_mcp_client.list_tools = AsyncMock(
            side_effect=[[], [mock_tool]]
        )

        with patch("colab_mcp.session.TOOLS_READY_POLL_INTERVAL", 0.01):
            result = await client.await_tools_ready()
        assert result == ["run_code_cell"]

    @pytest.mark.asyncio
    async def test_not_connected(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        result = await client.await_tools_ready()
        assert result == []


class TestBrowserLauncher:
    def test_uses_windows_chrome_from_wsl(self, monkeypatch):
        url = "https://colab.research.google.com/drive/test#mcpProxyToken=token&mcpProxyPort=1234"
        mock_popen = Mock()
        monkeypatch.setattr(session.os.path, "isfile", lambda path: path == session.WINDOWS_CHROME_PATHS[0])
        monkeypatch.setattr(session.subprocess, "Popen", mock_popen)

        session._open_url(url)

        mock_popen.assert_called_once_with(
            [session.WINDOWS_CHROME_PATHS[0], url],
            stdin=session.subprocess.DEVNULL,
            stdout=session.subprocess.DEVNULL,
            stderr=session.subprocess.DEVNULL,
        )


class TestColabProxyClient:
    def test_is_connected(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.is_connected() is False
        mock_wss.connection_live.set()
        assert client.is_connected() is False
        client.proxy_mcp_client = Mock()
        assert client.is_connected() is True

    @pytest.mark.asyncio
    async def test_await_proxy_connection(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        client._start_task = asyncio.create_task(asyncio.sleep(0.01))
        client._start_proxy_client = AsyncMock()
        mock_wss.connection_live.set()
        with patch("colab_mcp.session.UI_CONNECTION_TIMEOUT", 0.1):
            await client.await_proxy_connection()
        await client._start_task

    @pytest.mark.asyncio
    @patch("colab_mcp.session.Client")
    @patch("colab_mcp.session.ColabTransport", spec=session.ColabTransport)
    async def test_start_proxy_client(
        self, mock_colab_transport, mock_client, mock_wss
    ):
        mock_client.return_value.__aenter__ = AsyncMock()
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        async with client:
            await client._start_task

        mock_colab_transport.assert_called_once_with(mock_wss)
        mock_client.assert_called_with(mock_colab_transport.return_value)


class TestColabTransport:
    @pytest.mark.asyncio
    @patch("colab_mcp.session.ClientSession")
    async def test_connect_session(self, mock_client_session, mock_wss):
        transport = session.ColabTransport(mock_wss)
        mock_client_session.return_value.__aenter__ = AsyncMock()
        async with transport.connect_session(foo="bar") as client_session:
            assert (
                client_session
                == mock_client_session.return_value.__aenter__.return_value
            )

        mock_wss.read_stream.clone.assert_called_once_with()
        mock_wss.write_stream.clone.assert_called_once_with()
        mock_client_session.assert_called_once_with(
            mock_wss.read_stream.clone.return_value,
            mock_wss.write_stream.clone.return_value,
            foo="bar",
        )


class TestColabSessionProxy:
    @pytest.mark.asyncio
    @patch("colab_mcp.session.ColabWebSocketServer")
    @patch("colab_mcp.session.ColabProxyClient")
    async def test_start_proxy_server(
        self,
        mock_colab_proxy_client,
        mock_colab_web_socket_server,
    ):
        mock_colab_web_socket_server.return_value.__aenter__ = AsyncMock()
        mock_colab_proxy_client.return_value.__aenter__ = AsyncMock()
        proxy = session.ColabSessionProxy()
        await proxy.start_proxy_server()
        mock_colab_proxy_client.assert_called_once()
        assert proxy.proxy_client is not None

    @pytest.mark.asyncio
    async def test_cleanup(self):
        proxy = session.ColabSessionProxy()
        proxy._exit_stack = AsyncMock()
        await proxy.cleanup()
        proxy._exit_stack.aclose.assert_called_once()
