#!/usr/bin/env python3
"""One-time Microsoft device-code sign-in for data-host-mcp's OneDrive tool.

Reads AZURE_APPLICATION_ID (your Azure app registration's client id) from the
environment, asks Microsoft for a device code, prints the URL and code to open
in any browser, waits for you to approve, and stores the resulting refresh
token as AZURE_REFRESH_TOKEN in the same key=value file server.py reads
(AZURE_ENV_PATH, default: ./.env next to this script).

    AZURE_APPLICATION_ID=<client id> python3 device_auth.py
"""

import os
import sys
import time
from pathlib import Path

import requests

TENANT = "consumers"  # personal Microsoft accounts
SCOPE = "Files.Read offline_access"
BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"
ENV_PATH = Path(os.getenv("AZURE_ENV_PATH", Path(__file__).parent / ".env"))


def write_env_value(key: str, value: str) -> None:
    lines = []
    if ENV_PATH.exists():
        lines = [line for line in ENV_PATH.read_text().splitlines(keepends=True) if not line.startswith(f"{key}=")]
    lines.append(f"{key}={value}\n")
    ENV_PATH.write_text("".join(lines))
    os.chmod(ENV_PATH, 0o600)


def main() -> int:
    client_id = os.getenv("AZURE_APPLICATION_ID")
    if not client_id:
        print("Set AZURE_APPLICATION_ID to your Azure app registration's client id.", file=sys.stderr)
        return 2

    response = requests.post(f"{BASE}/devicecode", data={"client_id": client_id, "scope": SCOPE}, timeout=15)
    response.raise_for_status()
    device = response.json()
    print(device.get("message") or f"Open {device['verification_uri']} and enter code {device['user_code']}")

    interval = int(device.get("interval", 5))
    deadline = time.time() + int(device.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        poll = requests.post(
            f"{BASE}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device["device_code"],
            },
            timeout=15,
        )
        payload = poll.json()
        if poll.status_code == 200 and payload.get("refresh_token"):
            write_env_value("AZURE_REFRESH_TOKEN", payload["refresh_token"])
            print(f"Signed in. Refresh token saved to {ENV_PATH}")
            return 0
        error = payload.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        print(f"Sign-in failed: {payload.get('error_description', error)}", file=sys.stderr)
        return 1
    print("Sign-in timed out; run the script again.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
