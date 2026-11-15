"""One-time local Google OAuth consent for the Sleep History YouTube channel.

Run on the owner's computer, not inside GitHub Actions. Never upload a Google
authorization code as a workflow_dispatch input. Requires GitHub CLI login and
a Google OAuth client JSON exported from the matching Google Cloud project.

Example:
    python scripts/youtube_oauth_local.py --client-json C:/Users/Me/Downloads/client_secret.json

The downloaded client JSON must remain outside Git and is never modified.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPOSITORY = "marwalfarsi2000/sleep-history-channel"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
SCOPES = (UPLOAD_SCOPE, READ_SCOPE)
PREFERRED_REDIRECT = "http://localhost:5679/rest/oauth2-credential/callback"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"


def load_client(path: Path) -> tuple[dict, str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    kind = "web" if "web" in data else "installed" if "installed" in data else ""
    if not kind:
        raise ValueError("Google client JSON must have a 'web' or 'installed' section")
    client = data[kind]
    if not client.get("client_id") or not client.get("client_secret"):
        raise ValueError("Google client JSON is missing client_id/client_secret")
    uris = client.get("redirect_uris") or []
    # Web clients require an exact registered redirect URI. Do not invent one.
    if kind == "web":
        redirects = [u for u in uris if allowed_loopback(u)]
        if not redirects:
            raise ValueError(
                "The OAuth web client has no registered localhost redirect URI. "
                "Register a loopback callback in Google Cloud or use an installed client."
            )
        redirect = PREFERRED_REDIRECT if PREFERRED_REDIRECT in redirects else redirects[0]
    else:
        redirect = PREFERRED_REDIRECT if PREFERRED_REDIRECT in uris else "http://localhost:5679/"
    if not allowed_loopback(redirect):
        raise ValueError("Refusing a non-local or invalid Google redirect URI")
    return client, kind, redirect


def allowed_loopback(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    try:
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1"}
            and parsed.port is not None
            and parsed.port > 0
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


def post_token(values: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(values).encode("ascii")
    request = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # The response may contain sensitive context; print only the OAuth error code.
        try:
            error = json.loads(exc.read().decode("utf-8")).get("error", "unknown")
        except (ValueError, UnicodeDecodeError):
            error = "unknown"
        raise RuntimeError(f"Google OAuth request failed: {error}") from None


def receive_authorization(redirect: str, client_id: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(redirect)
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    result: dict[str, str] = {}
    callback_path = parsed.path or "/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return  # Never log a URL containing an authorization code.

        def do_GET(self):
            request = urllib.parse.urlsplit(self.path)
            if request.path != callback_path:
                self.send_error(404)
                return
            params = urllib.parse.parse_qs(request.query)
            received_state = (params.get("state") or [""])[0]
            if not hmac.compare_digest(received_state, state):
                result["error"] = "oauth_state_mismatch"
            elif "error" in params:
                result["error"] = "google_consent_denied"
            else:
                result["code"] = (params.get("code") or [""])[0]
                if not result["code"]:
                    result["error"] = "authorization_code_missing"
            self.send_response(200 if result.get("code") else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            message = "تم استلام الموافقة. يمكنك إغلاق هذه الصفحة." if result.get("code") else "تعذر إكمال التفويض. عودي إلى الطرفية."
            self.wfile.write(("<html><meta charset='utf-8'><body><h2>" + message + "</h2></body></html>").encode("utf-8"))

    bind_host = "127.0.0.1"
    try:
        server = HTTPServer((bind_host, parsed.port), Handler)
    except OSError as exc:
        raise RuntimeError(
            f"Port {parsed.port} is busy. Stop the app using it (often n8n) and retry, "
            "or register a different localhost redirect URI for this OAuth client."
        ) from exc
    try:
        server.timeout = 1
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        print("Opening Google consent in the browser. Choose the channel-owning account.")
        if not webbrowser.open(url, new=2):
            print("The browser could not open automatically. Use this one-time consent URL locally:")
            print(url)
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
        if not result:
            raise TimeoutError("No Google callback received within 5 minutes; retry the local command.")
        if result.get("error"):
            raise RuntimeError("Google callback failed: " + result["error"])
        return result["code"], verifier
    finally:
        server.server_close()


def get_channel(access_token: str) -> tuple[str, str]:
    request = urllib.request.Request(CHANNEL_URL, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            items = json.load(response).get("items", [])
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"YouTube channel verification failed (HTTP {exc.code}). Check scopes and API activation.") from None
    if not items:
        raise RuntimeError("The authorized Google account does not expose a YouTube channel")
    return str(items[0]["id"]), str(items[0]["snippet"]["title"])


def github_preflight() -> None:
    if not shutil.which("gh"):
        raise RuntimeError("GitHub CLI (gh) is not installed. Install it and run 'gh auth login' first.")
    checked = subprocess.run(["gh", "auth", "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if checked.returncode:
        raise RuntimeError("GitHub CLI is not authenticated. Run 'gh auth login' locally first.")


def store_github_secret(name: str, value: str) -> None:
    subprocess.run(
        ["gh", "secret", "set", name, "--repo", REPOSITORY],
        input=value.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Secure local Google YouTube reauthorization")
    parser.add_argument("--client-json", type=Path, required=True, help="Google OAuth client JSON, kept outside Git")
    parser.add_argument("--expected-channel-id", help="Fail if a different YouTube channel is authorized")
    args = parser.parse_args()

    github_preflight()  # Before consent, confirm we can actually save the new credentials.
    client, kind, redirect = load_client(args.client_json.expanduser().resolve())
    code, verifier = receive_authorization(redirect, client["client_id"])
    token = post_token({
        "code": code,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    if not token.get("refresh_token") or not token.get("access_token"):
        raise RuntimeError("Google did not issue a refresh token; check Production status and offline consent.")
    scopes = set(str(token.get("scope", "")).split())
    if not set(SCOPES).issubset(scopes):
        raise RuntimeError("Google did not grant both YouTube upload and read permissions.")
    refreshed = post_token({
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    })
    if not refreshed.get("access_token"):
        raise RuntimeError("Google refresh-token verification failed")
    channel_id, channel_title = get_channel(refreshed["access_token"])
    print(f"Verified YouTube channel: {channel_title} ({channel_id})")
    if args.expected_channel_id:
        if channel_id != args.expected_channel_id:
            raise RuntimeError("Authorized channel is NOT the expected channel; no GitHub secret was changed.")
    elif input("Is this your intended channel? Type YES to update GitHub Secrets: ").strip() != "YES":
        raise RuntimeError("Channel confirmation declined; no GitHub secret was changed.")

    # Keep all three fields on the same OAuth client. Do not store the auth code.
    # Refresh token is written last, after the corresponding client fields.
    store_github_secret("YOUTUBE_CLIENT_ID", client["client_id"])
    store_github_secret("YOUTUBE_CLIENT_SECRET", client["client_secret"])
    store_github_secret("YOUTUBE_REFRESH_TOKEN", token["refresh_token"])
    store_github_secret("YOUTUBE_EXPECTED_CHANNEL_ID", channel_id)
    print("PASS: refreshed OAuth access token, verified channel, updated GitHub Secrets.")
    print("Run the GitHub 'YouTube Connection Check' action to verify the cloud configuration.")
    print("Revoke the temporary GH_SECRETS_PAT credential if one was created earlier.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
