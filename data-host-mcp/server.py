#!/usr/bin/env python3
"""
Data Host MCP Server

- get_file: generate OneDrive URLs (delegated Azure refresh-token auth,
  managed internally by this server since personal OneDrive accounts don't
  support app-only access).

- host_to_drive: upload a local file/folder to Google Drive. Authentication
  is handled as native MCP Connector OAuth (same as the AlphaXiv/HuggingFace
  connectors) rather than a custom tool: this server acts as its own OAuth
  Authorization Server that proxies to Google. Running `/mcp` in Claude Code
  triggers the standard browser sign-in; Claude Code stores and refreshes the
  resulting token itself from then on. The bearer token Claude Code attaches
  to each request IS the live Google access token (pass-through design), so
  tool code just reads it from the request context — no separate token cache
  or manual refresh logic needed on this side.

Runs over streamable-http (not stdio), since MCP OAuth only exists on the
HTTP transport. Must be kept running as a persistent local service (see the
systemd --user unit alongside this file) rather than spawned per session.
"""

import os
import re
import time
import json
import secrets
import mimetypes
from pathlib import Path
from urllib.parse import urlencode

import requests
from starlette.requests import Request
from starlette.responses import Response, HTMLResponse, RedirectResponse

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationParams,
    AuthorizationCode,
    RefreshToken,
    AccessToken,
    AuthorizeError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.auth.middleware.auth_context import get_access_token as get_mcp_access_token
from mcp.shared.auth import OAuthToken, OAuthClientInformationFull

# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------

MCP_SERVER_DIR = Path(__file__).parent
ENV_PATH = os.getenv("AZURE_ENV_PATH", str(MCP_SERVER_DIR / ".env"))

# OneDrive (Azure) — unrelated to the Google OAuth connector below.
AZURE_CLIENT_ID = os.getenv("AZURE_APPLICATION_ID")
AZURE_TENANT_ID = "consumers"  # personal Microsoft accounts must use "consumers"

# Google Drive connector.
GOOGLE_OAUTH_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_CALLBACK_PATH = "/google/callback"

MCP_SERVER_HOST = "127.0.0.1"
MCP_SERVER_PORT = 8791
ISSUER_URL = f"http://{MCP_SERVER_HOST}:{MCP_SERVER_PORT}"

GOOGLE_TOKENS_PATH = MCP_SERVER_DIR / "google_tokens.json"
OAUTH_CLIENTS_PATH = MCP_SERVER_DIR / "oauth_clients.json"

_token_cache = {"access_token": None, "timestamp": 0}  # Azure/OneDrive only


# ---------------------------------------------------------------------------
# Shared .env helpers
# ---------------------------------------------------------------------------

def _read_env_value(key: str) -> str | None:
    if not os.path.exists(ENV_PATH):
        return None
    with open(ENV_PATH) as f:
        for line in f:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return None


def _write_env_value(key: str, value: str):
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            lines = [l for l in f.readlines() if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}\n")
    with open(ENV_PATH, "w") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Google token persistence (survives server restarts)
# ---------------------------------------------------------------------------

def _load_google_tokens() -> dict | None:
    try:
        if GOOGLE_TOKENS_PATH.exists():
            with open(GOOGLE_TOKENS_PATH) as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return None


def _save_google_tokens(tokens: dict):
    try:
        with open(GOOGLE_TOKENS_PATH, "w") as f:
            json.dump(tokens, f, indent=2)
        os.chmod(GOOGLE_TOKENS_PATH, 0o600)
    except IOError as e:
        print(f"Warning: could not save Google tokens: {e}")


def _load_oauth_clients() -> dict[str, OAuthClientInformationFull]:
    if not OAUTH_CLIENTS_PATH.exists():
        return {}
    try:
        with open(OAUTH_CLIENTS_PATH) as f:
            raw = json.load(f)
        return {cid: OAuthClientInformationFull.model_validate(info) for cid, info in raw.items()}
    except (json.JSONDecodeError, IOError):
        return {}


def _save_oauth_clients(clients: dict[str, OAuthClientInformationFull]):
    try:
        raw = {cid: json.loads(info.model_dump_json()) for cid, info in clients.items()}
        with open(OAUTH_CLIENTS_PATH, "w") as f:
            json.dump(raw, f, indent=2)
        os.chmod(OAUTH_CLIENTS_PATH, 0o600)
    except IOError as e:
        print(f"Warning: could not save registered OAuth clients: {e}")


# ---------------------------------------------------------------------------
# OneDrive (Azure) — unchanged delegated-auth helpers
# ---------------------------------------------------------------------------

def _read_azure_refresh_token() -> str | None:
    return _read_env_value("AZURE_REFRESH_TOKEN")


def _save_azure_refresh_token(new_refresh_token: str):
    _write_env_value("AZURE_REFRESH_TOKEN", new_refresh_token)


def get_azure_access_token() -> str:
    """Exchange refresh token for a fresh OneDrive access token, rotating the refresh token."""
    current_time = time.time()
    if _token_cache["access_token"] and (current_time - _token_cache["timestamp"]) < 3000:
        return _token_cache["access_token"]

    refresh_token = _read_azure_refresh_token()
    if not refresh_token:
        raise Exception("No AZURE_REFRESH_TOKEN found. Run device_auth.py first.")

    token_url = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": AZURE_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "Files.Read offline_access",
    }

    response = requests.post(token_url, data=data, timeout=10)
    response.raise_for_status()
    payload = response.json()

    access_token = payload["access_token"]
    new_refresh_token = payload.get("refresh_token")
    if new_refresh_token:
        _save_azure_refresh_token(new_refresh_token)

    _token_cache["access_token"] = access_token
    _token_cache["timestamp"] = current_time
    return access_token


def _get_item_metadata(path: str) -> dict:
    token = get_azure_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:{path}"
    response = requests.get(url, headers=headers, timeout=10)
    if response.status_code != 200:
        raise Exception(f"HTTP {response.status_code}: {response.text}")
    return response.json()


def get_file_url(file_path: str) -> str:
    token = get_azure_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:{file_path}:/content"
    response = requests.get(url, headers=headers, allow_redirects=False, timeout=10)
    if response.status_code == 302:
        download_url = response.headers.get("Location")
        if download_url:
            return download_url
        raise Exception("No Location header in redirect response")
    raise Exception(f"HTTP {response.status_code}: {response.text}")


def get_folder_children_url(folder_path: str) -> str:
    return f"https://graph.microsoft.com/v1.0/me/drive/root:{folder_path}:/children"


# ---------------------------------------------------------------------------
# Google Drive OAuth bridge — this IS the "authentication of the MCP
# Connector". Claude Code discovers this server needs OAuth, does dynamic
# client registration, and drives the browser flow; we proxy the actual
# identity check to Google and hand back Google's own access/refresh tokens
# as the MCP session's bearer tokens.
# ---------------------------------------------------------------------------

class GoogleDriveOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """
    Each registered MCP client (Claude Code, Codex, or anything else pointed
    at this server) gets its OWN independent Google token slot, keyed by MCP
    client_id. Earlier this was a single shared slot, so a second client
    completing its own Google sign-in silently overwrote the first client's
    live token — the first client would keep sending a bearer token that no
    longer matched what the server tracked, and depending on timing that
    could surface either as our own 401 or, worse, as a raw 401 straight
    from Google's API once the mismatch briefly self-resolved. Keying by
    client_id makes concurrent clients fully independent.
    """

    def __init__(self):
        self._clients: dict[str, OAuthClientInformationFull] = _load_oauth_clients()
        self._pending: dict[str, dict] = {}  # google `state` -> {"params": AuthorizationParams, "client_id": str}
        self._auth_codes: dict[str, AuthorizationCode] = {}  # our mcp code -> AuthorizationCode

        # client_id -> {"access_token", "refresh_token", "expires_at"}
        self._google_tokens: dict[str, dict] = _load_google_tokens() or {}

    def _persist(self):
        _save_google_tokens(self._google_tokens)

    def _find_client_id_for_access_token(self, token: str) -> str | None:
        for client_id, record in self._google_tokens.items():
            if isinstance(record, dict) and record.get("access_token") == token:
                return client_id
        return None

    # -- Dynamic client registration (each MCP client registers itself once) --

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        _save_oauth_clients(self._clients)

    # -- Authorization: redirect the browser on to Google's real consent screen --

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        google_state = secrets.token_urlsafe(24)
        self._pending[google_state] = {"params": params, "client_id": client.client_id}

        google_client_id = _read_env_value("GOOGLE_CLIENT_ID")
        if not google_client_id:
            raise AuthorizeError(error="server_error", error_description=f"GOOGLE_CLIENT_ID missing in {ENV_PATH}")

        query = {
            "client_id": google_client_id,
            "redirect_uri": f"{ISSUER_URL}{GOOGLE_CALLBACK_PATH}",
            "response_type": "code",
            "scope": GOOGLE_OAUTH_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": google_state,
        }
        return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(query)}"

    # -- Authorization code exchange: hand back this client's own Google tokens --

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code_obj = self._auth_codes.get(authorization_code)
        if code_obj and code_obj.client_id == client.client_id:
            return code_obj
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)
        record = self._google_tokens.get(client.client_id)
        if not record or not record.get("access_token"):
            raise TokenError(error="invalid_grant", error_description="Google sign-in did not complete")

        return OAuthToken(
            access_token=record["access_token"],
            refresh_token=record.get("refresh_token"),
            expires_in=max(0, int(record.get("expires_at", 0) - time.time())),
            scope=GOOGLE_OAUTH_SCOPE,
        )

    # -- Refresh: proxy straight to Google's refresh grant, for this client only --

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = self._google_tokens.get(client.client_id)
        if record and record.get("refresh_token") == refresh_token:
            return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=[GOOGLE_OAUTH_SCOPE])
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        google_client_id = _read_env_value("GOOGLE_CLIENT_ID")
        google_client_secret = _read_env_value("GOOGLE_CLIENT_SECRET")
        data = {
            "client_id": google_client_id,
            "client_secret": google_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token.token,
        }
        response = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=10)
        if response.status_code != 200:
            raise TokenError(error="invalid_grant", error_description=f"Google refresh failed: {response.text}")

        payload = response.json()
        new_access_token = payload["access_token"]
        new_refresh_token = payload.get("refresh_token", refresh_token.token)
        expires_in = payload.get("expires_in", 3600)

        self._google_tokens[client.client_id] = {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "expires_at": time.time() + expires_in,
        }
        self._persist()

        return OAuthToken(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            expires_in=expires_in,
            scope=GOOGLE_OAUTH_SCOPE,
        )

    # -- Bearer token validation on every tool call --

    async def load_access_token(self, token: str) -> AccessToken | None:
        client_id = self._find_client_id_for_access_token(token)
        if client_id is None:
            return None
        record = self._google_tokens[client_id]
        if time.time() >= record.get("expires_at", 0):
            return None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=[GOOGLE_OAUTH_SCOPE],
            expires_at=int(record["expires_at"]),
        )

    async def revoke_token(self, token) -> None:
        client_id = self._find_client_id_for_access_token(token.token)
        if client_id is None:
            for cid, record in self._google_tokens.items():
                if isinstance(record, dict) and record.get("refresh_token") == token.token:
                    client_id = cid
                    break
        if client_id is None:
            return
        try:
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": token.token},
                timeout=10,
            )
        except requests.RequestException:
            pass
        self._google_tokens.pop(client_id, None)
        self._persist()

    # -- Called by the /google/callback route once Google redirects back --

    def complete_google_login(self, google_state: str, google_code: str) -> str:
        """Exchange Google's code, mint our own MCP auth code, return the
        original client's redirect_uri (with our code attached)."""
        pending = self._pending.pop(google_state, None)
        if not pending:
            raise ValueError("Unknown or expired sign-in session")

        params: AuthorizationParams = pending["params"]
        client_id: str = pending["client_id"]

        google_client_id = _read_env_value("GOOGLE_CLIENT_ID")
        google_client_secret = _read_env_value("GOOGLE_CLIENT_SECRET")
        response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": google_client_id,
                "client_secret": google_client_secret,
                "code": google_code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{ISSUER_URL}{GOOGLE_CALLBACK_PATH}",
            },
            timeout=10,
        )
        if response.status_code != 200:
            raise ValueError(f"Google token exchange failed: {response.text}")

        payload = response.json()
        access_token = payload["access_token"]
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in", 3600)

        if not refresh_token:
            raise ValueError(
                "Google did not return a refresh_token. Revoke prior access at "
                "https://myaccount.google.com/permissions and try again."
            )

        self._google_tokens[client_id] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": time.time() + expires_in,
        }
        self._persist()

        mcp_code = secrets.token_urlsafe(32)
        self._auth_codes[mcp_code] = AuthorizationCode(
            code=mcp_code,
            scopes=params.scopes or [GOOGLE_OAUTH_SCOPE],
            expires_at=time.time() + 300,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
        )

        return construct_redirect_uri(str(params.redirect_uri), code=mcp_code, state=params.state)


_oauth_provider = GoogleDriveOAuthProvider()

mcp = FastMCP(
    "data-host-mcp",
    host=MCP_SERVER_HOST,
    port=MCP_SERVER_PORT,
    streamable_http_path="/mcp",
    auth_server_provider=_oauth_provider,
    auth=AuthSettings(
        issuer_url=ISSUER_URL,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[GOOGLE_OAUTH_SCOPE],
            default_scopes=[GOOGLE_OAUTH_SCOPE],
        ),
        required_scopes=[GOOGLE_OAUTH_SCOPE],
        resource_server_url=f"{ISSUER_URL}/mcp",
    ),
)


@mcp.custom_route(GOOGLE_CALLBACK_PATH, methods=["GET"])
async def google_oauth_callback(request: Request) -> Response:
    """Google redirects here after the user approves/denies access."""
    google_state = request.query_params.get("state")
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if not google_state:
        return HTMLResponse("<h1>Missing state parameter.</h1>", status_code=400)

    if error:
        pending = _oauth_provider._pending.pop(google_state, None)
        if pending:
            params: AuthorizationParams = pending["params"]
            return RedirectResponse(
                construct_redirect_uri(str(params.redirect_uri), error=error, state=params.state),
                status_code=302,
            )
        return HTMLResponse(f"<h1>Google sign-in failed: {error}</h1>", status_code=400)

    if not code:
        return HTMLResponse("<h1>No authorization code from Google.</h1>", status_code=400)

    try:
        redirect_url = _oauth_provider.complete_google_login(google_state, code)
    except ValueError as e:
        return HTMLResponse(f"<h1>Sign-in failed</h1><p>{e}</p>", status_code=400)

    return RedirectResponse(redirect_url, status_code=302)


def _get_current_google_token() -> str:
    """The bearer token Claude Code attached to this request IS the live
    Google access token (pass-through design) — Claude Code refreshes it
    automatically per the OAuth flow, so no local refresh logic is needed."""
    access_token = get_mcp_access_token()
    if access_token is None:
        raise Exception("Not authenticated. Run /mcp in Claude Code and sign in to Google Drive.")
    return access_token.token


async def _upload_file_to_drive(local_file_path: str, parent_folder_id: str = "root", ctx: Context | None = None) -> dict:
    """
    Upload a single file to Google Drive.

    Uses resumable upload (not simple multipart) and reports progress after
    every chunk via ctx.report_progress(). This is the actual fix for "long
    data transmission reports aborted after 300 seconds": a single large
    file previously went through one unary multipart POST with zero network
    activity visible to the MCP client for the entire transfer, which is
    exactly what the client's own ~300s read-timeout (waiting for *any*
    response/notification on that request) kills. Chunked resumable upload
    with a progress notification between every chunk gives a well-behaved
    client periodic liveness signals so it doesn't time out mid-transfer.
    """
    token = _get_current_google_token()
    headers = {"Authorization": f"Bearer {token}"}

    file_path = Path(local_file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {local_file_path}")

    file_name = file_path.name
    mime_type, _ = mimetypes.guess_type(local_file_path)
    if not mime_type:
        mime_type = "application/octet-stream"
    file_size = file_path.stat().st_size

    if file_size == 0:
        # Resumable upload's byte-range protocol doesn't have a clean
        # zero-length case; a plain multipart POST handles it in one shot.
        file_metadata = {"name": file_name, "parents": [parent_folder_id]}
        url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
        files = {
            "data": ("metadata", json.dumps(file_metadata), "application/json"),
            "file": (file_name, b"", mime_type),
        }
        response = requests.post(url, headers=headers, files=files, timeout=30)
        response.raise_for_status()
        return response.json()

    # Step 1: start a resumable upload session, get the session URL.
    file_metadata = {"name": file_name, "parents": [parent_folder_id]}
    init_response = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
        headers={**headers, "Content-Type": "application/json; charset=UTF-8"},
        json=file_metadata,
        timeout=30,
    )
    init_response.raise_for_status()
    session_url = init_response.headers["Location"]

    # Step 2: stream the file content up in chunks, reporting progress between each.
    CHUNK_SIZE = 8 * 1024 * 1024  # 8MB, Drive's resumable upload requires multiples of 256KB
    uploaded = 0
    final_response = None
    with open(file_path, "rb") as f:
        while uploaded < file_size:
            chunk = f.read(CHUNK_SIZE)
            chunk_len = len(chunk)
            range_end = uploaded + chunk_len - 1
            put_response = requests.put(
                session_url,
                headers={
                    "Content-Length": str(chunk_len),
                    "Content-Range": f"bytes {uploaded}-{range_end}/{file_size}",
                },
                data=chunk,
                timeout=120,
            )
            uploaded += chunk_len

            if ctx is not None:
                await ctx.report_progress(uploaded, file_size, message=f"Uploading {file_name}")

            if put_response.status_code in (200, 201):
                final_response = put_response
                break
            elif put_response.status_code != 308:  # 308 = incomplete, keep going
                put_response.raise_for_status()

    if final_response is None:
        raise Exception(f"Resumable upload of {local_file_path!r} did not complete")
    return final_response.json()


def _create_folder_in_drive(folder_name: str, parent_folder_id: str = "root") -> str:
    """Create a folder in Google Drive and return its ID."""
    token = _get_current_google_token()
    headers = {"Authorization": f"Bearer {token}"}

    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_folder_id],
    }

    url = "https://www.googleapis.com/drive/v3/files"
    response = requests.post(url, headers=headers, json=file_metadata, timeout=10)
    response.raise_for_status()
    return response.json()["id"]


async def _upload_folder_to_drive(local_folder_path: str, parent_folder_id: str = "root", ctx: Context | None = None) -> dict:
    """
    Recursively upload a folder and its contents to Google Drive, reporting
    progress after each child (file or subfolder) completes — on top of the
    per-chunk progress _upload_file_to_drive already reports for large
    individual files, this covers the "many files" cumulative-time case.
    """
    folder_path = Path(local_folder_path)
    if not folder_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {local_folder_path}")

    folder_name = folder_path.name
    drive_folder_id = _create_folder_in_drive(folder_name, parent_folder_id)

    children = list(folder_path.iterdir())
    for i, item in enumerate(children):
        if item.is_file():
            await _upload_file_to_drive(str(item), drive_folder_id, ctx=ctx)
        elif item.is_dir():
            await _upload_folder_to_drive(str(item), drive_folder_id, ctx=ctx)
        if ctx is not None:
            await ctx.report_progress(i + 1, len(children), message=f"Uploaded {item.name}")

    return {
        "id": drive_folder_id,
        "name": folder_name,
        "type": "folder",
        "webViewLink": f"https://drive.google.com/drive/folders/{drive_folder_id}",
    }


def _resolve_drive_path(path: str) -> dict:
    """
    Resolve a '/'-separated Drive path, relative to Drive's root (e.g.
    '/project/data.csv' or '/project/dataset-folder'), to that item's Drive
    metadata by walking one path segment at a time via files.list, starting
    from parent_id='root'.

    Returns {"id": ..., "name": ..., "mimeType": ...} for the final segment.

    Known limitation: unlike a POSIX filesystem, Drive allows multiple items
    with the same name in the same parent folder. When a segment name is
    ambiguous, this resolver takes files[0] (the Drive API's own, effectively
    arbitrary, ordering) rather than erroring — matching host_to_drive, which
    never deduplicates names on upload either.
    """
    token = _get_current_google_token()
    headers = {"Authorization": f"Bearer {token}"}

    segments = [s for s in path.split("/") if s]
    if not segments:
        raise FileNotFoundError(f"Empty Drive path: {path!r}")

    parent_id = "root"
    node = None
    walked = []
    for segment in segments:
        escaped = segment.replace("\\", "\\\\").replace("'", "\\'")
        params = {
            "q": f"name='{escaped}' and '{parent_id}' in parents and trashed=false",
            "fields": "files(id,name,mimeType)",
        }
        response = requests.get(
            "https://www.googleapis.com/drive/v3/files", headers=headers, params=params, timeout=10
        )
        response.raise_for_status()
        matches = response.json().get("files", [])
        if not matches:
            raise FileNotFoundError(
                f"Drive path segment {segment!r} not found under '/{'/'.join(walked)}' (full path: {path!r})"
            )
        node = matches[0]
        parent_id = node["id"]
        walked.append(segment)

    return node


async def _download_file_from_drive(
    file_id: str, local_path: str, mime_type: str | None = None, ctx: Context | None = None
) -> None:
    """
    Download a single Drive file's bytes to local_path, creating parent
    directories as needed. Raises a clear ValueError up front for native
    Google formats (Docs/Sheets/Slides/etc — mimeType starting
    'application/vnd.google-apps.' other than a folder), since those can't
    be fetched via alt=media and would otherwise surface as Google's own
    opaque 400 error. Pass mime_type when already known (e.g. from a prior
    files.list call) to skip an extra files.get lookup.

    Streams the response body in chunks and reports progress between each —
    this is the fix for "long data transmission reports aborted after 300
    seconds": the previous single `response.content` read returned nothing
    to the caller until the *entire* file was buffered in memory, with zero
    signal reaching the MCP client for the whole duration. A client with no
    news for ~300s on a request abandons it; periodic progress notifications
    keep a well-behaved client from doing that.
    """
    token = _get_current_google_token()
    headers = {"Authorization": f"Bearer {token}"}

    if mime_type is None:
        meta_response = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            headers=headers, params={"fields": "mimeType"}, timeout=10,
        )
        meta_response.raise_for_status()
        mime_type = meta_response.json().get("mimeType", "")

    if mime_type.startswith("application/vnd.google-apps.") and mime_type != "application/vnd.google-apps.folder":
        raise ValueError(
            f"Cannot download native Google file (mimeType={mime_type!r}) via the Drive API's "
            f"alt=media download; export it to a standard format in Drive first."
        )

    dest = Path(local_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    CHUNK_SIZE = 8 * 1024 * 1024  # 8MB
    with requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        headers=headers, params={"alt": "media"}, timeout=60, stream=True,
    ) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length", 0)) or None
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if ctx is not None:
                    await ctx.report_progress(downloaded, total, message=f"Downloading {dest.name}")


def _list_drive_folder_children(folder_id: str) -> list[dict]:
    """
    List the immediate children of a Drive folder as
    [{"id", "name", "mimeType"}, ...]. Single files.list call — does not
    paginate; a folder with more than Drive's default page size (100) of
    direct children will only return the first page.
    """
    token = _get_current_google_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "q": f"'{folder_id}' in parents and trashed=false",
        "fields": "files(id,name,mimeType)",
    }
    response = requests.get(
        "https://www.googleapis.com/drive/v3/files", headers=headers, params=params, timeout=10
    )
    response.raise_for_status()
    return response.json().get("files", [])


async def _download_folder_from_drive(folder_id: str, local_dir: str, ctx: Context | None = None) -> None:
    """
    Recursively download a Drive folder's contents into local_dir,
    mirroring the folder structure. Creates local_dir if it doesn't exist.
    Mirrors _upload_folder_to_drive's shape but in reverse. Reports progress
    after each child completes, on top of the per-chunk progress
    _download_file_from_drive already reports for large individual files.
    """
    dest_dir = Path(local_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    children = _list_drive_folder_children(folder_id)
    for i, child in enumerate(children):
        child_path = dest_dir / child["name"]
        if child["mimeType"] == "application/vnd.google-apps.folder":
            await _download_folder_from_drive(child["id"], str(child_path), ctx=ctx)
        else:
            await _download_file_from_drive(child["id"], str(child_path), mime_type=child["mimeType"], ctx=ctx)
        if ctx is not None:
            await ctx.report_progress(i + 1, len(children), message=f"Downloaded {child['name']}")


@mcp.tool()
def get_file(path: str) -> str:
    """
    Get a public URL for a file OR a folder in OneDrive.

    - If path is a file: returns a pre-authenticated public download URL,
      usable directly with curl/requests, no auth header needed (~1 hour).
    - If path is a folder: returns the Graph API "children" URL for that
      folder. This URL is NOT pre-authenticated — it requires a Bearer
      token to call. Call it with a Bearer token
      to list the files inside, then request each file's path with get_file.

    Args:
        path: Path to a file or folder in OneDrive (e.g., '/hellow-world.txt' or '/test-folder')

    Returns:
        A public download URL (file) or a Graph API children URL (folder)
    """
    metadata = _get_item_metadata(path)
    if "folder" in metadata:
        return get_folder_children_url(path)
    return get_file_url(path)


@mcp.tool()
async def host_to_drive(file_path: str, ctx: Context | None = None) -> dict:
    """
    Upload a local file or folder to Google Drive.

    Requires signing in once via `/mcp` in Claude Code (standard MCP
    Connector OAuth, same as AlphaXiv/HuggingFace) — no separate
    authentication tool call needed.

    - If file_path is a file: uploads it to the root of Google Drive
    - If file_path is a folder: creates a folder structure in Google Drive
      and recursively uploads all contents

    Reports MCP progress during the transfer (per-chunk for a large single
    file, per-item for a folder's contents), so a large upload doesn't go
    silent long enough for the client to time out and abort it.

    Args:
        file_path: Absolute or relative path to a local file or folder

    Returns:
        A dict with upload details including the Google Drive folder/file ID
        and a webViewLink to access it
    """
    local_path = Path(file_path).expanduser().resolve()

    if not local_path.exists():
        raise FileNotFoundError(f"Path does not exist: {file_path}")

    if local_path.is_file():
        result = await _upload_file_to_drive(str(local_path), ctx=ctx)
        return {
            "id": result.get("id"),
            "name": result.get("name"),
            "type": "file",
            "webViewLink": result.get("webViewLink", f"https://drive.google.com/file/d/{result.get('id')}"),
        }
    elif local_path.is_dir():
        return await _upload_folder_to_drive(str(local_path), ctx=ctx)
    else:
        raise ValueError(f"Path is neither file nor directory: {file_path}")


@mcp.tool()
async def sync_file_to_local(files: list, ctx: Context | None = None) -> dict:
    """
    Copy a batch of files/folders from Google Drive down to the local
    filesystem. One-way only: Drive -> local.

    Requires signing in once via `/mcp` in Claude Code (standard MCP
    Connector OAuth, same as AlphaXiv/HuggingFace) — no separate
    authentication tool call needed.

    files: a list of [drive_path, local_path] pairs (or 2-tuples).
    drive_path is a '/'-separated path relative to Drive's root (e.g.
    '/project/data.csv' or '/project/dataset-folder'); local_path is the
    destination on the local filesystem. Works for both individual files
    and whole folders (recursive download). The sync for a given pair only
    takes effect once that pair's download succeeds — a bad path, missing
    path segment, or permission error on one pair is reported in that
    pair's own result and does not stop the rest of the batch from being
    attempted. Parent directories are created as needed on the local side.

    Reports MCP progress during the transfer (per-chunk within a large
    single file, per-item across a batch/folder), so a large sync doesn't
    go silent long enough for the client to time out and abort it.

    Args:
        files: list of [drive_path, local_path] pairs

    Returns:
        {"ok": bool, "synced": int, "failed": int, "results": [
            {"ok": bool, "drive_path": str, "local_path": str, "error": str | None},
            ...
        ]}
    """
    if not files:
        return {
            "ok": False, "synced": 0, "failed": 0, "results": [],
            "error": "files must be a non-empty list of (drive_path, local_path) pairs.",
        }

    results = []
    for i, item in enumerate(files):
        try:
            drive_path, local_path = item
        except (TypeError, ValueError):
            results.append({
                "ok": False, "drive_path": None, "local_path": None,
                "error": f"Malformed entry {item!r}; expected a (drive_path, local_path) pair.",
            })
            continue
        try:
            node = _resolve_drive_path(drive_path)
            if node["mimeType"] == "application/vnd.google-apps.folder":
                await _download_folder_from_drive(node["id"], local_path, ctx=ctx)
            else:
                await _download_file_from_drive(node["id"], local_path, mime_type=node["mimeType"], ctx=ctx)
            results.append({"ok": True, "drive_path": drive_path, "local_path": local_path, "error": None})
        except Exception as e:
            results.append({
                "ok": False, "drive_path": drive_path, "local_path": local_path,
                "error": f"{type(e).__name__}: {e}",
            })

        if ctx is not None:
            await ctx.report_progress(i + 1, len(files), message=f"Processed {drive_path}")

    failed = sum(1 for r in results if not r["ok"])
    return {"ok": failed == 0, "synced": len(results) - failed, "failed": failed, "results": results}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
