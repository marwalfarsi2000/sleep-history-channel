import json
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError
import youtube_oauth_check as check
import youtube_publish as yp


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    for key in check.FIELDS:
        monkeypatch.setenv(key, "TEST_ONLY_" + key)


def test_missing_refresh_is_blocked_before_network(monkeypatch):
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN")
    with patch.object(yp, "get_granted_scopes") as refresh:
        report = check.check()
    assert report["status"] == "BLOCKED"
    refresh.assert_not_called()


def test_presence_only_is_not_claimed_as_binding_proof():
    with patch.object(yp, "get_granted_scopes") as refresh:
        assert check.check(offline=True)["status"] == "NOT_VERIFIED"
    refresh.assert_not_called()


@pytest.mark.parametrize("error,action", [
    ("invalid_grant: TEST_ONLY_SECRET", "REAUTHORIZE_YOUTUBE_OAUTH"),
    ("unauthorized_client: TEST_ONLY_SECRET", "UPDATE_YOUTUBE_OAUTH_CLIENT_AND_REAUTHORIZE"),
])
def test_refresh_failures_are_sanitized(error, action):
    with patch.object(yp, "get_granted_scopes", side_effect=RefreshError(error)):
        report = check.check()
    assert report["status"] == "BLOCKED" and report["action"] == action
    assert "TEST_ONLY" not in json.dumps(report)


def test_transport_failure_does_not_disclose_token_url():
    with patch.object(yp, "get_granted_scopes", side_effect=RuntimeError("?access_token=TEST_ONLY_SECRET")):
        report = check.check()
    assert report["status"] == "NOT_VERIFIED"
    assert "TEST_ONLY" not in json.dumps(report)


def test_verified_binding_and_capabilities_without_publish():
    with patch.object(yp, "get_granted_scopes", return_value={yp.SCOPE_UPLOAD, yp.SCOPE_READONLY}), \
         patch.object(yp, "publish_mode", return_value=yp.MODE_DIRECT_PUBLIC), \
         patch.object(yp, "build") as build:
        report = check.check()
    assert report["status"] == report["client_refresh_binding"] == "PASS"
    assert report["readback_available"]
    build.assert_not_called()


def test_secret_file_only_loads_youtube_fields(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN")
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)
    path = tmp_path / "fake.env"
    path.write_text('YOUTUBE_REFRESH_TOKEN="TEST_ONLY_REFRESH"\nUNRELATED_SECRET=TEST_ONLY_NO\n')
    check.load_youtube_fields(path)
    import os
    assert os.environ["YOUTUBE_REFRESH_TOKEN"] == "TEST_ONLY_REFRESH"
    assert "UNRELATED_SECRET" not in os.environ


@pytest.mark.parametrize("exception", [OSError, RuntimeError])
def test_scope_lookup_redacts_token_exception(exception):
    credentials = MagicMock(granted_scopes=None, token="TEST_ONLY_ACCESS")
    with patch("urllib.request.urlopen", side_effect=exception("TEST_ONLY_ACCESS")):
        with pytest.raises(RuntimeError) as error:
            yp.get_granted_scopes(credentials)
    assert "TEST_ONLY_ACCESS" not in str(error.value)
    assert error.value.__suppress_context__


def test_tokeninfo_wrong_client_is_denied():
    credentials = MagicMock(granted_scopes=None, token="TEST_ONLY_ACCESS", client_id="client-A")
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "aud": "client-B", "scope": yp.SCOPE_MANAGE}).encode()
    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(RuntimeError, match="BINDING_MISMATCH"):
            yp.get_granted_scopes(credentials)


def test_manual_oauth_workflow_cannot_execute_publish_or_write_state():
    import pathlib, re
    text = (pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/review.yml").read_text(encoding="utf-8")
    oauth, decide = text.split("  oauth-preflight:\n", 1)[1].split("  decide:\n", 1)
    assert "if: inputs.action == 'oauth-preflight'" in oauth
    assert "if: inputs.action != 'oauth-preflight'" in decide
    assert "contents: read" in oauth and "contents: write" not in oauth
    assert "python scripts/youtube_oauth_check.py" in oauth
    assert not any(command in oauth for command in ("cloud_pipeline.py", "state_store.py", "git_sync.py", "upload-artifact"))
    assert set(re.findall(r"secrets\.([A-Z_]+)", oauth)) == set(check.FIELDS)
