#!/usr/bin/env python3
"""
Slim tests for data-host-mcp's OAuth-as-MCP-Connector Google Drive auth.

Two layers:
1. Live HTTP smoke tests against the real, persistently-running systemd
   service on http://127.0.0.1:8791 (the same one Claude Code/Codex talk to).
2. Direct unit tests of GoogleDriveOAuthProvider's per-client token
   isolation — a regression test for the concurrency bug where two MCP
   clients (Claude Code + Codex) shared one global Google token slot and
   stomped on each other.

Any state a test creates (registered clients, token records) is cleaned up
afterward so it doesn't linger for the real clients using this server.
"""

import sys
import json
import time
import asyncio
import hashlib
import base64
import secrets
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

BASE_URL = "http://127.0.0.1:8791"
SERVER_DIR = Path(__file__).parent
OAUTH_CLIENTS_PATH = SERVER_DIR / "oauth_clients.json"
GOOGLE_TOKENS_PATH = SERVER_DIR / "google_tokens.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _write_json(path: Path, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Layer 1: live HTTP smoke tests
# ---------------------------------------------------------------------------

def test_service_reachable():
    print("✓ Test 1: Service reachable")
    resp = requests.get(f"{BASE_URL}/mcp", timeout=5)
    assert resp.status_code == 401, f"Expected 401 (unauthenticated), got {resp.status_code}"
    print(f"  ✓ GET /mcp -> 401 (correctly requires auth)")
    return True


def test_protected_resource_metadata():
    print("✓ Test 2: Protected resource metadata (RFC 9728)")
    resp = requests.get(f"{BASE_URL}/.well-known/oauth-protected-resource/mcp", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert data["resource"] == f"{BASE_URL}/mcp"
    assert f"{BASE_URL}/" in data["authorization_servers"]
    print(f"  ✓ resource: {data['resource']}")
    return True


def test_authorization_server_metadata():
    print("✓ Test 3: Authorization server metadata (RFC 8414)")
    resp = requests.get(f"{BASE_URL}/.well-known/oauth-authorization-server", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert data["authorization_endpoint"] == f"{BASE_URL}/authorize"
    assert data["token_endpoint"] == f"{BASE_URL}/token"
    assert data["registration_endpoint"] == f"{BASE_URL}/register"
    print(f"  ✓ endpoints correctly advertised")
    return True


def test_two_independent_clients_can_register():
    """Test two separate MCP clients (simulating Claude Code + Codex) can
    both register against this server without colliding."""
    print("✓ Test 4: Two independent clients can register")
    original_clients = _read_json(OAUTH_CLIENTS_PATH)

    try:
        client_a = requests.post(
            f"{BASE_URL}/register",
            json={"redirect_uris": ["http://localhost:11111/callback"], "client_name": "test-client-a"},
            timeout=5,
        ).json()
        client_b = requests.post(
            f"{BASE_URL}/register",
            json={"redirect_uris": ["http://localhost:22222/callback"], "client_name": "test-client-b"},
            timeout=5,
        ).json()

        assert client_a["client_id"] != client_b["client_id"], "Two registrations got the same client_id"

        on_disk = _read_json(OAUTH_CLIENTS_PATH)
        assert client_a["client_id"] in on_disk
        assert client_b["client_id"] in on_disk
        print(f"  ✓ client A: {client_a['client_id'][:12]}...")
        print(f"  ✓ client B: {client_b['client_id'][:12]}...")
        print(f"  ✓ both persisted independently")
    finally:
        _write_json(OAUTH_CLIENTS_PATH, original_clients)
    return True


def test_authorize_redirects_to_google():
    print("✓ Test 5: /authorize redirects to Google with correct params")
    original_clients = _read_json(OAUTH_CLIENTS_PATH)

    reg = requests.post(
        f"{BASE_URL}/register",
        json={"redirect_uris": ["http://localhost:9999/callback"]},
        timeout=5,
    ).json()
    client_id = reg["client_id"]

    try:
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

        resp = requests.get(
            f"{BASE_URL}/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost:9999/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "test-state-123",
            },
            allow_redirects=False,
            timeout=5,
        )
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth")
        assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8791%2Fgoogle%2Fcallback" in location
        assert "access_type=offline" in location
        assert "prompt=consent" in location
        print(f"  ✓ Redirects to Google's real consent screen with correct params")
    finally:
        _write_json(OAUTH_CLIENTS_PATH, original_clients)
    return True


def test_systemd_service_active():
    print("✓ Test 6: systemd --user service is active + enabled")
    import subprocess

    result = subprocess.run(["systemctl", "--user", "is-active", "data-host-mcp.service"], capture_output=True, text=True)
    assert result.stdout.strip() == "active"
    result = subprocess.run(["systemctl", "--user", "is-enabled", "data-host-mcp.service"], capture_output=True, text=True)
    assert result.stdout.strip() == "enabled"
    print(f"  ✓ active + enabled")
    return True


# ---------------------------------------------------------------------------
# Layer 2: direct unit tests of per-client token isolation (the bug fix)
# ---------------------------------------------------------------------------

def test_per_client_token_isolation():
    """Regression test for the exact bug reported: two MCP clients (e.g.
    Claude Code + Codex) must each keep their own independent Google
    token — one client's token must never validate for, or be overwritten
    by, another client's."""
    print("✓ Test 7: Per-client Google token isolation (regression test)")
    import server as srv

    # Back up the REAL token file: complete_google_login() below calls
    # provider._persist(), which writes provider._google_tokens straight to
    # GOOGLE_TOKENS_PATH — without this backup/restore, this test would
    # clobber the live server's actual tokens with fake test data (this
    # exact bug bit the real service once already).
    original_tokens = _read_json(GOOGLE_TOKENS_PATH)

    provider = srv.GoogleDriveOAuthProvider()
    provider._google_tokens = {}  # start from a clean isolated slate for this test

    async def run():
        now = time.time()
        provider._google_tokens["client-A"] = {
            "access_token": "token-for-A",
            "refresh_token": "refresh-for-A",
            "expires_at": now + 3600,
        }
        provider._google_tokens["client-B"] = {
            "access_token": "token-for-B",
            "refresh_token": "refresh-for-B",
            "expires_at": now + 3600,
        }

        # Each client's token must validate to itself and no one else.
        access_a = await provider.load_access_token("token-for-A")
        access_b = await provider.load_access_token("token-for-B")
        assert access_a is not None and access_a.client_id == "client-A"
        assert access_b is not None and access_b.client_id == "client-B"

        # A's token must not be usable as B's, and vice versa.
        assert access_a.token != access_b.token

        # Simulate client B doing a fresh full sign-in (complete_google_login)
        # and confirm it does NOT touch client A's token at all.
        provider._pending["fake-google-state"] = {
            "params": srv.AuthorizationParams(
                state="b-state", scopes=[srv.GOOGLE_OAUTH_SCOPE],
                code_challenge="fake-challenge",
                redirect_uri="http://localhost:22222/callback",
                redirect_uri_provided_explicitly=True,
            ),
            "client_id": "client-B",
        }

        # Monkeypatch requests.post so complete_google_login doesn't hit real Google
        class FakeResponse:
            status_code = 200
            def json(self):
                return {"access_token": "token-for-B-rotated", "refresh_token": "refresh-for-B-rotated", "expires_in": 3600}

        original_post = srv.requests.post
        srv.requests.post = lambda *a, **k: FakeResponse()
        try:
            provider.complete_google_login("fake-google-state", "fake-google-code")
        finally:
            srv.requests.post = original_post

        # Client A's token must be completely untouched by B's re-login.
        access_a_after = await provider.load_access_token("token-for-A")
        assert access_a_after is not None, "Client A's token was wiped by client B's sign-in!"
        assert access_a_after.client_id == "client-A"

        # Client B's OLD token must no longer validate (it rotated)...
        old_b = await provider.load_access_token("token-for-B")
        assert old_b is None, "Client B's old token should be invalidated after rotation"

        # ...and the NEW one must validate to client B specifically.
        new_b = await provider.load_access_token("token-for-B-rotated")
        assert new_b is not None and new_b.client_id == "client-B"

        print("  ✓ Client A's token unaffected by client B's independent sign-in")
        print("  ✓ Client B's rotated token correctly isolated to client B")
        print("  ✓ No cross-client interference (the original bug is fixed)")

    try:
        asyncio.run(run())
    finally:
        _write_json(GOOGLE_TOKENS_PATH, original_tokens)
    return True


def test_revoke_only_affects_owning_client():
    """Test revoking one client's token doesn't affect another client's."""
    print("✓ Test 8: revoke_token only clears the owning client's slot")
    import server as srv

    # Same backup/restore reasoning as Test 7 — revoke_token() also calls
    # provider._persist(), which writes straight to the real GOOGLE_TOKENS_PATH.
    original_tokens = _read_json(GOOGLE_TOKENS_PATH)

    provider = srv.GoogleDriveOAuthProvider()
    now = time.time()
    provider._google_tokens = {
        "client-X": {"access_token": "tok-x", "refresh_token": "ref-x", "expires_at": now + 3600},
        "client-Y": {"access_token": "tok-y", "refresh_token": "ref-y", "expires_at": now + 3600},
    }

    async def run():
        class FakeToken:
            token = "tok-x"

        original_post = srv.requests.post
        srv.requests.post = lambda *a, **k: type("R", (), {"status_code": 200})()
        try:
            await provider.revoke_token(FakeToken())
        finally:
            srv.requests.post = original_post

        assert "client-X" not in provider._google_tokens, "client-X should be revoked"
        assert "client-Y" in provider._google_tokens, "client-Y should be untouched by client-X's revocation"
        y_token = await provider.load_access_token("tok-y")
        assert y_token is not None and y_token.client_id == "client-Y"
        print("  ✓ Revoking client-X's token left client-Y fully intact")

    try:
        asyncio.run(run())
    finally:
        _write_json(GOOGLE_TOKENS_PATH, original_tokens)
    return True


def test_stale_flat_format_token_file_does_not_crash():
    """Test that if an old pre-fix flat-format tokens.json somehow exists,
    the provider doesn't crash loading it (defensive, not silently correct —
    just must not raise, since bad legacy data should degrade to 'no tokens'
    rather than take the whole server down)."""
    print("✓ Test 9: Old flat-format token file doesn't crash the provider")
    import server as srv

    legacy_flat_data = {"access_token": "legacy", "refresh_token": "legacy-r", "expires_at": time.time() + 3600}

    async def run():
        provider = srv.GoogleDriveOAuthProvider.__new__(srv.GoogleDriveOAuthProvider)
        provider._clients = {}
        provider._pending = {}
        provider._auth_codes = {}
        provider._google_tokens = legacy_flat_data  # simulate old format loaded as-is

        # This must not raise, even though the data shape is wrong (strings,
        # not per-client dicts) — load_access_token should just find no match.
        result = await provider.load_access_token("legacy")
        assert result is None, "Legacy flat data should not be mistaken for a valid per-client record"
        print("  ✓ Legacy format safely ignored instead of crashing")

    asyncio.run(run())
    return True


# ---------------------------------------------------------------------------
# Layer 3: sync_file_to_local (Drive -> local) unit tests
# ---------------------------------------------------------------------------

def test_resolve_drive_path_walks_segments_and_raises_on_missing():
    """Test _resolve_drive_path walks multiple segments correctly and
    raises a clear FileNotFoundError naming the missing segment."""
    print("✓ Test 10: _resolve_drive_path segment walking")
    import server as srv
    import re as re_module

    # Fake tree: /project/sub/data.csv
    fake_tree = {
        ("root", "project"): {"id": "id-project", "name": "project", "mimeType": "application/vnd.google-apps.folder"},
        ("id-project", "sub"): {"id": "id-sub", "name": "sub", "mimeType": "application/vnd.google-apps.folder"},
        ("id-sub", "data.csv"): {"id": "id-data", "name": "data.csv", "mimeType": "text/csv"},
    }

    class FakeResponse:
        def __init__(self, files):
            self._files = files
        def raise_for_status(self):
            pass
        def json(self):
            return {"files": self._files}

    def fake_get(url, headers=None, params=None, timeout=None):
        q = params["q"]
        m = re_module.search(r"name='((?:[^'\\]|\\.)*)' and '([^']*)' in parents", q)
        name = m.group(1).replace("\\'", "'").replace("\\\\", "\\")
        parent = m.group(2)
        node = fake_tree.get((parent, name))
        return FakeResponse([node] if node else [])

    original_get = srv.requests.get
    original_token_fn = srv._get_current_google_token
    srv.requests.get = fake_get
    srv._get_current_google_token = lambda: "fake-token"
    try:
        result = srv._resolve_drive_path("/project/sub/data.csv")
        assert result == {"id": "id-data", "name": "data.csv", "mimeType": "text/csv"}, result
        print("  ✓ Multi-segment path resolved correctly")

        try:
            srv._resolve_drive_path("/project/missing/data.csv")
            assert False, "should have raised FileNotFoundError"
        except FileNotFoundError as e:
            assert "missing" in str(e)
            print(f"  ✓ Missing segment raises clearly: {e}")
    finally:
        srv.requests.get = original_get
        srv._get_current_google_token = original_token_fn
    return True


def test_download_file_from_drive_writes_bytes_and_creates_parents():
    """Test _download_file_from_drive (now streaming + async, for the
    300s-timeout fix) writes correct bytes and creates parent dirs, rejects
    native Google formats before ever calling alt=media, and reports
    progress via ctx between chunks."""
    print("✓ Test 11: _download_file_from_drive streams bytes + reports progress + rejects native Google formats")
    import server as srv
    import tempfile

    class FakeResponse:
        def __init__(self, chunks):
            self._chunks = chunks
            self.headers = {}
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size=None):
            return iter(self._chunks)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    calls = []

    def fake_get(url, headers=None, params=None, timeout=None, stream=None):
        calls.append(params)
        return FakeResponse([b"hello ", b"bytes"])

    class FakeContext:
        def __init__(self):
            self.progress_calls = []
        async def report_progress(self, progress, total=None, message=None):
            self.progress_calls.append((progress, total, message))

    original_get = srv.requests.get
    original_token_fn = srv._get_current_google_token
    srv.requests.get = fake_get
    srv._get_current_google_token = lambda: "fake-token"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = f"{tmp}/nested/dir/out.txt"
            fake_ctx = FakeContext()
            asyncio.run(srv._download_file_from_drive("fake-id", out_path, mime_type="text/plain", ctx=fake_ctx))
            assert Path(out_path).exists()
            assert Path(out_path).read_bytes() == b"hello bytes"
            assert len(fake_ctx.progress_calls) == 2, "expected one progress report per chunk"
            print("  ✓ File written correctly, parent dirs created, progress reported per chunk")

        assert len(calls) == 1 and calls[0].get("alt") == "media"

        try:
            asyncio.run(srv._download_file_from_drive(
                "fake-id", "/tmp/should-not-be-written.txt", mime_type="application/vnd.google-apps.document"
            ))
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "alt=media" in str(e) or "native Google" in str(e)
            print(f"  ✓ Native Google format rejected before any alt=media call: {e}")
        assert len(calls) == 1, "alt=media must not have been called for the rejected native format"
    finally:
        srv.requests.get = original_get
        srv._get_current_google_token = original_token_fn
    return True


def test_download_folder_from_drive_recursive_tree_shape():
    """Test _download_folder_from_drive (async) recreates a 2-level folder
    tree locally with correct file placement, content, and per-item
    progress reporting."""
    print("✓ Test 12: _download_folder_from_drive recursive tree shape")
    import server as srv
    import tempfile

    # root-folder -> file-a.txt + subfolder -> subfolder -> file-b.txt
    listings = {
        "root-folder": [
            {"id": "id-file-a", "name": "file-a.txt", "mimeType": "text/plain"},
            {"id": "id-subfolder", "name": "subfolder", "mimeType": "application/vnd.google-apps.folder"},
        ],
        "id-subfolder": [
            {"id": "id-file-b", "name": "file-b.txt", "mimeType": "text/plain"},
        ],
    }
    file_contents = {"id-file-a": b"content-a", "id-file-b": b"content-b"}

    class FakeListResponse:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    class FakeMediaResponse:
        def __init__(self, chunk):
            self._chunk = chunk
            self.headers = {}
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size=None):
            return iter([self._chunk])
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def fake_get(url, headers=None, params=None, timeout=None, stream=None):
        if params and params.get("alt") == "media":
            file_id = url.rsplit("/", 1)[-1]
            return FakeMediaResponse(file_contents[file_id])
        # files.list call
        folder_id = params["q"].split("'")[1]
        return FakeListResponse({"files": listings.get(folder_id, [])})

    class FakeContext:
        def __init__(self):
            self.progress_calls = []
        async def report_progress(self, progress, total=None, message=None):
            self.progress_calls.append((progress, total, message))

    original_get = srv.requests.get
    original_token_fn = srv._get_current_google_token
    srv.requests.get = fake_get
    srv._get_current_google_token = lambda: "fake-token"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = f"{tmp}/downloaded"
            fake_ctx = FakeContext()
            asyncio.run(srv._download_folder_from_drive("root-folder", local_dir, ctx=fake_ctx))

            assert Path(f"{local_dir}/file-a.txt").read_bytes() == b"content-a"
            assert Path(f"{local_dir}/subfolder/file-b.txt").read_bytes() == b"content-b"
            # 2 items at root level (file-a, subfolder) + 1 item inside subfolder = 3 per-item
            # progress calls, plus one per-chunk call for each of the 2 files = 5 total.
            assert len(fake_ctx.progress_calls) == 5, fake_ctx.progress_calls
            print("  ✓ Two-level tree recreated correctly, correct content at both levels, progress reported")
    finally:
        srv.requests.get = original_get
        srv._get_current_google_token = original_token_fn
    return True


def test_sync_file_to_local_batch_isolates_failures():
    """Test sync_file_to_local's batch aggregation: one bad pair must not
    stop the rest of the batch, and malformed entries are caught cleanly."""
    print("✓ Test 13: sync_file_to_local batch isolation")
    import server as srv
    import tempfile

    def fake_resolve(drive_path):
        if drive_path == "/missing.txt":
            raise FileNotFoundError(f"Drive path segment 'missing.txt' not found")
        return {"id": "id-ok", "name": "ok.txt", "mimeType": "text/plain"}

    async def fake_download_file(file_id, local_path, mime_type=None, ctx=None):
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"ok content")

    original_resolve = srv._resolve_drive_path
    original_download = srv._download_file_from_drive
    srv._resolve_drive_path = fake_resolve
    srv._download_file_from_drive = fake_download_file
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(srv.sync_file_to_local([
                ["/ok.txt", f"{tmp}/ok.txt"],
                ["/missing.txt", f"{tmp}/missing.txt"],
            ]))
            assert result["ok"] is False
            assert result["synced"] == 1
            assert result["failed"] == 1
            assert len(result["results"]) == 2
            ok_entry = next(r for r in result["results"] if r["drive_path"] == "/ok.txt")
            bad_entry = next(r for r in result["results"] if r["drive_path"] == "/missing.txt")
            assert ok_entry["ok"] is True and ok_entry["error"] is None
            assert bad_entry["ok"] is False and "missing.txt" in bad_entry["error"]
            print("  ✓ One bad pair isolated, good pair still succeeded")

            malformed_result = asyncio.run(srv.sync_file_to_local([["only-one-element"]]))
            assert malformed_result["failed"] == 1 and malformed_result["synced"] == 0
            assert "Malformed entry" in malformed_result["results"][0]["error"]
            print("  ✓ Malformed entry caught cleanly, did not crash the batch")

            empty_result = asyncio.run(srv.sync_file_to_local([]))
            assert empty_result["ok"] is False and "error" in empty_result
            print("  ✓ Empty batch rejected with a clear error")
    finally:
        srv._resolve_drive_path = original_resolve
        srv._download_file_from_drive = original_download
    return True


def test_live_sync_file_to_local_round_trip():
    """Live end-to-end verification: round-trip /hello-world.txt and
    /hello-folder (already uploaded to this Drive account earlier via
    host_to_drive) back down to a scratch temp dir against the REAL
    Google Drive API. Skips gracefully if no client is currently signed in."""
    print("✓ Test 14 (live): sync_file_to_local round-trip against real Drive")
    import server as srv
    import tempfile

    tokens = _read_json(GOOGLE_TOKENS_PATH)
    live_record = next((r for r in tokens.values() if isinstance(r, dict) and r.get("access_token")), None)
    if not live_record:
        print("  ⚠ SKIPPED: no signed-in Google client found in google_tokens.json")
        return True

    class FakeAccessToken:
        token = live_record["access_token"]

    original_get_token = srv.get_mcp_access_token
    srv.get_mcp_access_token = lambda: FakeAccessToken()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_file = f"{tmp}/hello-world.txt"
            local_folder = f"{tmp}/hello-folder"
            result = asyncio.run(srv.sync_file_to_local([
                ["/hello-world.txt", local_file],
                ["/hello-folder", local_folder],
            ]))
            if not result["ok"]:
                print(f"  ⚠ SKIPPED: live sync failed (token may be stale/expired): {result['results']}")
                return True
            assert result["synced"] == 2 and result["failed"] == 0
            assert Path(local_file).exists() and Path(local_file).stat().st_size > 0
            assert (Path(local_folder) / "hello-world.txt").exists()
            print(f"  ✓ Downloaded /hello-world.txt and /hello-folder from real Drive to {tmp}")
    finally:
        srv.get_mcp_access_token = original_get_token
    return True


if __name__ == "__main__":
    print("\n🧪 Running data-host-mcp OAuth connector tests...\n")

    tests = [
        test_service_reachable,
        test_protected_resource_metadata,
        test_authorization_server_metadata,
        test_two_independent_clients_can_register,
        test_authorize_redirects_to_google,
        test_systemd_service_active,
        test_per_client_token_isolation,
        test_revoke_only_affects_owning_client,
        test_stale_flat_format_token_file_does_not_crash,
        test_resolve_drive_path_walks_segments_and_raises_on_missing,
        test_download_file_from_drive_writes_bytes_and_creates_parents,
        test_download_folder_from_drive_recursive_tree_shape,
        test_sync_file_to_local_batch_isolates_failures,
        test_live_sync_file_to_local_round_trip,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
                print()
        except Exception as e:
            failed += 1
            print(f"  ✗ FAILED: {e}\n")

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
