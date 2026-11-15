"""Offline regression tests for secure YouTube OAuth and cloud connection checks."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

import youtube_oauth_local as local


def test_web_oauth_uses_only_registered_local_callback(tmp_path: Path) -> None:
    file = tmp_path / "client.json"
    file.write_text(json.dumps({"web": {
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "fake",
        "redirect_uris": [local.PREFERRED_REDIRECT, "https://example.com/callback"],
    }}), encoding="utf-8")
    client, kind, redirect = local.load_client(file)
    assert kind == "web" and client["client_id"].startswith("test.")
    assert redirect == local.PREFERRED_REDIRECT
    with pytest.raises(ValueError, match="registered"):
        local.load_client(file, "http://localhost:8765/callback")


def test_installed_client_allows_explicit_local_redirect(tmp_path: Path) -> None:
    file = tmp_path / "client.json"
    file.write_text(json.dumps({"installed": {
        "client_id": "desktop", "client_secret": "fake", "redirect_uris": ["http://localhost"],
    }}), encoding="utf-8")
    _, kind, redirect = local.load_client(file, "http://localhost:8765/callback")
    assert kind == "installed" and redirect.endswith(":8765/callback")


@pytest.mark.parametrize("url", [
    "https://localhost:5679/callback",
    "http://example.com:5679/callback",
    "http://localhost:5679/callback?code=bad",
    "http://user:pass@localhost:5679/callback",
    "http://localhost/callback",
    "http://localhost:0/callback",
])
def test_rejects_nonlocal_or_unsafe_callback(url: str) -> None:
    assert local.allowed_loopback(url) is False


def test_pkce_uses_sha256_url_safe_challenge() -> None:
    verifier, challenge = local.pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    assert challenge == expected
    assert len(verifier) >= 43


def test_github_action_cannot_receive_authorization_code() -> None:
    path = Path(__file__).resolve().parents[2] / ".github/workflows/youtube-reauth.yml"
    workflow = path.read_text(encoding="utf-8")
    assert "auth_code:" not in workflow
    assert "github.event.inputs.auth_code" not in workflow
    assert "GH_SECRETS_PAT" not in workflow
    assert "scripts/youtube_connection_check.py" in workflow
    assert "YOUTUBE_EXPECTED_CHANNEL_ID" in workflow
