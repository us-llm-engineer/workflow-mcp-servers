import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import colab_mcp


class FakeProxy:
    def __init__(self, connected=True):
        self.connected = connected
        self.proxy_mcp_client = SimpleNamespace(call_tool=AsyncMock())

    def is_connected(self):
        return self.connected


class FakeSession:
    def __init__(self, connected=True):
        self.proxy_client = FakeProxy(connected)


@pytest.fixture(autouse=True)
def clear_state():
    colab_mcp._browser_sessions.clear()
    colab_mcp._notebook_bindings.clear()
    colab_mcp._notebook_owners.clear()
    yield
    colab_mcp._browser_sessions.clear()
    colab_mcp._notebook_bindings.clear()
    colab_mcp._notebook_owners.clear()


def result(text):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def notebook(tmp_path, cells):
    path = tmp_path / "source.ipynb"
    path.write_text(json.dumps({"cells": cells}), encoding="utf-8")
    return str(path)


def attach(browser_id="b1"):
    session = FakeSession()
    colab_mcp._browser_sessions[browser_id] = session
    return session


@pytest.mark.asyncio
async def test_load_only_binds_and_never_touches_browser(tmp_path):
    session = attach()
    path = notebook(tmp_path, [{"cell_type": "code", "source": "x = 1"}])

    reply = await colab_mcp.load_notebook.fn("b1", path)

    assert "Bound local notebook" in reply
    session.proxy_client.proxy_mcp_client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_binding_is_one_to_one(tmp_path):
    attach("one")
    attach("two")
    path = notebook(tmp_path, [])
    assert "Bound local notebook" in await colab_mcp.load_notebook.fn("one", path)

    reply = await colab_mcp.load_notebook.fn("two", path)

    assert "already bound" in reply
    assert "one" in reply


@pytest.mark.asyncio
async def test_stale_disconnected_owner_does_not_block_rebinding(tmp_path):
    first = attach("one")
    attach("two")
    path = notebook(tmp_path, [])
    await colab_mcp.load_notebook.fn("one", path)
    first.proxy_client.connected = False

    reply = await colab_mcp.load_notebook.fn("two", path)

    assert "Bound local notebook" in reply
    assert "one" not in colab_mcp._notebook_bindings
    assert colab_mcp._notebook_owners[next(iter(colab_mcp._notebook_owners))] == "two"


@pytest.mark.asyncio
async def test_sync_requires_a_binding():
    attach()
    assert "Call load_notebook first" in await colab_mcp.sync_local.fn("b1")


@pytest.mark.asyncio
async def test_sync_preserves_matches_moves_then_updates_and_deletes(tmp_path):
    session = attach()
    path = notebook(tmp_path, [
        {"cell_type": "code", "source": "b"},
        {"cell_type": "code", "source": "changed"},
    ])
    await colab_mcp.load_notebook.fn("b1", path)
    calls = []

    async def browser(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "get_cells":
            return result(json.dumps({"cells": [
                {"id": "a", "cell_type": "code", "source": "a"},
                {"id": "b", "cell_type": "code", "source": "b"},
                {"id": "old", "cell_type": "code", "source": "old"},
            ]}))
        return result("{}")

    session.proxy_client.proxy_mcp_client.call_tool.side_effect = browser
    reply = await colab_mcp.sync_local.fn("b1")

    assert "moved': 1" in reply
    assert "updated': 1" in reply
    assert "deleted': 1" in reply
    assert calls[1:] == [
        ("move_cell", {"cellId": "b", "cellIndex": 0}),
        ("update_cell", {"cellId": "a", "content": "changed"}),
        ("delete_cell", {"cellId": "old"}),
    ]


@pytest.mark.asyncio
async def test_sync_adds_when_local_cell_has_no_remote_match(tmp_path):
    session = attach()
    path = notebook(tmp_path, [{"cell_type": "markdown", "source": "# local"}])
    await colab_mcp.load_notebook.fn("b1", path)

    async def browser(name, arguments):
        if name == "get_cells":
            return result('{"cells": []}')
        assert name == "add_text_cell"
        assert arguments == {"content": "# local", "cellIndex": 0}
        return result('{"newCellId": "new"}')

    session.proxy_client.proxy_mcp_client.call_tool.side_effect = browser
    reply = await colab_mcp.sync_local.fn("b1")
    assert "added': 1" in reply


@pytest.mark.asyncio
async def test_sync_recreates_cell_when_type_changes(tmp_path):
    session = attach()
    path = notebook(tmp_path, [{"cell_type": "markdown", "source": "same text"}])
    await colab_mcp.load_notebook.fn("b1", path)
    calls = []

    async def browser(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "get_cells":
            return result('{"cells": [{"id": "code", "cell_type": "code", "source": "same text"}]}')
        if name == "add_text_cell":
            return result('{"newCellId": "markdown"}')
        return result("{}")

    session.proxy_client.proxy_mcp_client.call_tool.side_effect = browser
    reply = await colab_mcp.sync_local.fn("b1")
    assert "added': 1" in reply and "deleted': 1" in reply
    assert calls[1:] == [
        ("add_text_cell", {"content": "same text", "cellIndex": 0}),
        ("delete_cell", {"cellId": "code"}),
    ]


@pytest.mark.asyncio
async def test_temporary_shell_cell_is_deleted_after_execution_error():
    session = attach()
    calls = []

    async def browser(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "add_code_cell":
            return result('{"newCellId": "tmp"}')
        if name == "run_code_cell":
            raise RuntimeError("boom")
        assert name == "delete_cell"
        return result("{}")

    session.proxy_client.proxy_mcp_client.call_tool.side_effect = browser
    reply = await colab_mcp.run_shell.fn("b1", "echo hello\necho world")
    assert "Error calling run_code_cell" in reply
    assert calls[-1] == ("delete_cell", {"cellId": "tmp"})
    assert calls[0][1] == {
        "code": "%%bash\necho hello\necho world",
        "cellIndex": 0,
        "language": "python",
    }


@pytest.mark.asyncio
async def test_mount_drive_polls_until_complete_then_deletes(monkeypatch):
    session = attach()
    calls = []
    polls = 0

    async def browser(name, arguments):
        nonlocal polls
        calls.append((name, dict(arguments)))
        if name == "add_code_cell":
            assert arguments["code"] == "from google.colab import drive\ndrive.mount('/content/drive')"
            return result('{"newCellId": "mount"}')
        if name == "run_code_cell":
            return result("mount started")
        if name == "get_cells":
            polls += 1
            state = "running" if polls == 1 else "idle"
            return result(json.dumps({"cells": [{"id": "mount", "execution_state": state}]}))
        return result("{}")

    monkeypatch.setattr(colab_mcp.asyncio, "sleep", AsyncMock())
    session.proxy_client.proxy_mcp_client.call_tool.side_effect = browser
    assert await colab_mcp.mount_drive.fn("b1") == "mount started"
    assert polls == 2
    assert calls[-1] == ("delete_cell", {"cellId": "mount"})


@pytest.mark.asyncio
async def test_metrics_parses_json_and_removes_temporary_cell():
    session = attach()
    calls = []

    async def browser(name, arguments):
        calls.append(name)
        if name == "add_code_cell":
            return result('{"newCellId": "tmp"}')
        if name == "run_code_cell":
            return result('log line\n{"gpu_ram_percent": null, "cpu_ram_percent": 33.3, "disk_percent": 12.5}')
        return result("{}")

    session.proxy_client.proxy_mcp_client.call_tool.side_effect = browser
    assert json.loads(await colab_mcp.get_live_system_metrics.fn("b1")) == {
        "cpu_ram_percent": 33.3, "disk_percent": 12.5, "gpu_ram_percent": None
    }
    assert calls == ["add_code_cell", "run_code_cell", "delete_cell"]
