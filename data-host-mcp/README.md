# data-host-mcp

A single-file (`server.py`, about 950 lines) MCP server that moves data between a local machine and two cloud drives, and acts as its own **OAuth 2.1 authorization server** so MCP clients can sign in to Google Drive with the standard connector flow. It runs as a long-lived local HTTP service (streamable HTTP on `127.0.0.1:8791`, endpoint `/mcp`), not as a per-session stdio subprocess, because MCP OAuth only exists on the HTTP transport.

## Tools

| Tool | What it does |
| --- | --- |
| `get_file(path)` | OneDrive. For a file returns a pre-authenticated download URL (valid about an hour; no auth header needed). For a folder returns the Microsoft Graph `.../children` URL, which does need a Bearer token. |
| `host_to_drive(file_path)` | Uploads a local file, or recursively a folder, to the root of Google Drive. Returns `{id, name, type, webViewLink}`. |
| `sync_file_to_local(files)` | One-way Drive -> local copy of a batch of `[drive_path, local_path]` pairs (files or whole folders). Returns per-pair results, so one bad pair does not stop the rest. |

## Requirements and setup

```bash
pip install -r requirements.txt          # mcp, requests
cp ../configs/data-host-mcp.env.example .env   # then fill in the values
AZURE_APPLICATION_ID=<client id> python3 device_auth.py   # one-time OneDrive sign-in
python3 server.py                         # serves http://127.0.0.1:8791/mcp
```

Values the server reads (names only; the repository contains no credentials):

| Name | Where | Purpose |
| --- | --- | --- |
| `AZURE_APPLICATION_ID` | process environment | Client id of your Azure app registration (personal accounts, tenant `consumers`). |
| `AZURE_ENV_PATH` | process environment | Path of the key=value file below (default `./.env` next to `server.py`). |
| `AZURE_REFRESH_TOKEN` | the `.env` file | OneDrive delegated refresh token, rotated and rewritten by the server on every refresh. Obtain the first one with `python3 device_auth.py` (Microsoft device-code sign-in, scope `Files.Read offline_access`; included here). |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | the `.env` file | Your Google OAuth client (Drive scope). Its redirect URI must be `http://127.0.0.1:8791/google/callback`. |

A systemd user unit template is in [`../configs/data-host-mcp.service`](../configs/data-host-mcp.service). In Claude Code run `/mcp` and sign in once; the client then stores and refreshes its own token.

Runtime files created next to the script (`google_tokens.json`, `oauth_clients.json`, mode 0600) hold live tokens and registered clients and must never be committed; the repository `.gitignore` excludes them.

## Implementation

### Google Drive auth is a pass-through OAuth bridge

`GoogleDriveOAuthProvider` implements the MCP SDK's `OAuthAuthorizationServerProvider`. The server advertises itself as the issuer (`http://127.0.0.1:8791`) with dynamic client registration enabled, one scope (`https://www.googleapis.com/auth/drive`), and `/mcp` as the protected resource. The flow:

1. **Registration.** Each MCP client (Claude Code, Codex, anything else) registers itself once; the record is persisted to `oauth_clients.json`.
2. **Authorize.** `authorize()` stores the client's request parameters under a random `state` and redirects the browser to Google's consent screen (`access_type=offline`, `prompt=consent`) with this server's `/google/callback` as redirect URI.
3. **Callback.** Google redirects to the custom route `/google/callback`. `complete_google_login` exchanges the code for tokens (and insists on a refresh token, with an explanatory error if Google does not return one), stores them **under the MCP client's id**, mints a short-lived (300 s) MCP authorization code carrying the client's PKCE challenge, and redirects back to the client's own redirect URI.
4. **Token exchange.** `exchange_authorization_code` hands the client Google's *own* access and refresh tokens as its MCP bearer tokens. This is the pass-through design: the Bearer token a client attaches to each request is the live Google access token, so tool code just reads it from the request context (`_get_current_google_token`) and no second token cache exists.
5. **Refresh and revoke.** `exchange_refresh_token` proxies Google's refresh grant and updates the stored record; `revoke_token` calls Google's revoke endpoint and drops the record. `load_access_token` validates a presented token by looking up which client owns it and checking expiry.

Tokens are stored **per client id**. An earlier single shared slot meant a second client's sign-in silently overwrote the first client's token; keying by client makes concurrent clients independent (covered by a regression test).

### Large transfers do not time out

MCP clients abandon a request that produces no traffic for about 300 s. Both directions therefore report progress:

- **Upload** (`_upload_file_to_drive`) uses Drive's *resumable* protocol: one POST starts a session, then the file is PUT in 8 MiB chunks with `Content-Range` headers (HTTP 308 means "send more"), and `ctx.report_progress` fires after each chunk. Zero-byte files use a single multipart POST because the range protocol has no clean empty case. Folders (`_upload_folder_to_drive`) create the Drive folder, recurse, and report progress after each child.
- **Download** (`_download_file_from_drive`) streams the response in 8 MiB chunks to disk with progress after each, creating parent directories. Native Google formats (Docs, Sheets, ...) cannot be fetched with `alt=media`, so they fail up front with a clear message instead of Google's opaque 400. Folders are mirrored recursively (`_download_folder_from_drive`).

### Path resolution

`_resolve_drive_path` walks a `/`-separated path one segment at a time with `files.list` queries (`name='...' and '<parent>' in parents and trashed=false`, with quote and backslash escaping), starting at `root`. Known limitations, documented in the code: Drive allows duplicate names, in which case the first match wins; folder listings fetch a single page (Drive's default page size) and do not paginate.

### OneDrive side

`get_azure_access_token` exchanges the stored refresh token (scope `Files.Read offline_access`, tenant `consumers`), caches the access token for 3,000 s, and **writes back the rotated refresh token** to the `.env` file. `get_file` first reads the item's metadata from Graph (`/me/drive/root:{path}`); folders return the children URL, files issue a request to `.../content` with redirects disabled and return the `Location` header, which is the pre-authenticated download URL.

## Tests

`test_mcp.py` is a script-style suite with two layers: live smoke tests against a running instance of the service (reachability, OAuth discovery metadata, client registration, redirect to Google, systemd unit state), and direct tests of the provider (per-client token isolation, revoke only affecting its owner, tolerance of a stale token-file format, path resolution, file and folder download, batch failure isolation). The live layer needs the service running with your own configuration, and one optional round-trip test touches real Drive data. Run it with `python3 test_mcp.py`.
