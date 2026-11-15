"""Read-only YouTube connection preflight. Never uploads videos or displays tokens."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import urllib.parse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"


def check_connection() -> tuple[str, str]:
    names = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_EXPECTED_CHANNEL_ID")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing GitHub Secrets: " + ", ".join(missing))

    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
    )
    creds.refresh(Request())
    granted = set(creds.granted_scopes or [])
    if not granted:
        # Google's refreshed token response may omit scopes. Query only Google, not a third party.
        query = urllib.parse.urlencode({"access_token": creds.token})
        with urllib.request.urlopen("https://oauth2.googleapis.com/tokeninfo?" + query, timeout=30) as response:
            granted = set(str(json.load(response).get("scope", "")).split())
    if not {UPLOAD_SCOPE, READ_SCOPE}.issubset(granted):
        raise RuntimeError("Missing YouTube upload/read OAuth scopes. Reauthorize with the local tool.")

    request = urllib.request.Request(CHANNEL_URL, headers={"Authorization": f"Bearer {creds.token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        items = json.load(response).get("items", [])
    if not items:
        raise RuntimeError("No YouTube channel returned for the authorized Google account")
    channel = items[0]
    if str(channel["id"]) != os.environ["YOUTUBE_EXPECTED_CHANNEL_ID"]:
        raise RuntimeError("YouTube channel ID does not match the expected channel")
    return str(channel["id"]), str(channel.get("snippet", {}).get("title", ""))


if __name__ == "__main__":
    try:
        # Secret contents must not be logged, even on failure.
        channel_id, channel_title = check_connection()
        print("PASS refresh_token; PASS upload/read scopes; PASS expected channel")
        print(f"Authorized channel: {channel_title} ({channel_id})")
    except Exception as exc:
        name = type(exc).__name__
        # Avoid printing Google exception bodies, headers or submitted credential values.
        print(f"FAIL YouTube connection check: {name}. Inspect credentials, scope and channel selection.", file=sys.stderr)
        sys.exit(1)
