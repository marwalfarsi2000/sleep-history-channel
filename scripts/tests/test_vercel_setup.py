"""Local Vercel key setup page and the /v1/models connection check."""
import builtins
import http.client
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import vercel_gateway as gateway  # noqa: E402
import vercel_setup as setup  # noqa: E402

KEY = "test-gateway-key-0123456789-not-real"


class Runner:
    """Fake subprocess.run recording argv / stdin; never touches gh."""

    def __init__(self, auth=0, set_code=0, extra=None):
        self.calls, self.auth, self.set_code, self.extra = [], auth, set_code, extra or {}

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        code = 0
        if argv[:3] == ["gh", "auth", "status"]:
            code = self.auth
        elif argv[:3] == ["gh", "secret", "set"]:
            code = self.set_code
        for prefix, result in self.extra.items():
            if " ".join(argv).startswith(prefix):
                return result
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="")


def test_server_binds_to_loopback_only():
    server = setup.make_server(0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
    assert "0.0.0.0" not in (ROOT / "scripts" / "vercel_setup.py").read_text(encoding="utf-8")


def test_page_has_password_field_and_frontend_only_toggle_and_no_browser_storage():
    page = setup.PAGE
    assert 'id="key" type="password"' in page and "إعداد Vercel للصور" in page
    assert "حفظ المفتاح" in page and "اختبار الاتصال" in page and "إظهار" in page
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert forbidden not in page
    assert "key.type = hidden" in page  # the show/hide toggle never leaves the browser
    assert "ألصقي مفتاح Vercel AI Gateway هنا" in page


def test_save_sends_the_key_through_stdin_never_argv_and_never_returns_it():
    runner = Runner()
    result = setup.save_secret(KEY, which=lambda name: "/usr/bin/gh", runner=runner)
    assert result == {"ok": True, "message": setup.MSG_SAVED}
    argv, kwargs = next(call for call in runner.calls if call[0][:3] == ["gh", "secret", "set"])
    assert argv == ["gh", "secret", "set", "AI_GATEWAY_API_KEY"]
    assert KEY not in " ".join(sum((call[0] for call in runner.calls), []))
    assert bytes(kwargs["input"]) == b"\x00" * len(KEY) or bytes(kwargs["input"]) == KEY.encode()  # scrubbed or intact copy
    assert not kwargs.get("shell") and "body" not in " ".join(argv)
    assert KEY not in json.dumps(result)


def test_stdin_really_carries_the_key(monkeypatch):
    seen = {}

    def runner(argv, **kwargs):
        if argv[:3] == ["gh", "secret", "set"]:
            seen["stdin"] = bytes(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    setup.save_secret(KEY, which=lambda name: "gh", runner=runner)
    assert seen["stdin"] == KEY.encode()


def test_missing_gh_gives_one_clear_arabic_owner_action():
    result = setup.save_secret(KEY, which=lambda name: None, runner=Runner())
    assert result["ok"] is False and result["message"] == "GitHub CLI غير مثبت."
    assert result["owner_action"].startswith("OWNER_ACTION:") and KEY not in json.dumps(result)


def test_unauthenticated_gh_gives_login_message_and_sets_nothing():
    runner = Runner(auth=1)
    result = setup.save_secret(KEY, which=lambda name: "gh", runner=runner)
    assert result["message"] == "GitHub CLI يحتاج تسجيل دخول."
    assert not any(call[0][:3] == ["gh", "secret", "set"] for call in runner.calls)


def test_failed_set_reports_failure_without_echoing_cli_output():
    runner = Runner(set_code=1)
    result = setup.save_secret(KEY, which=lambda name: "gh", runner=runner)
    assert result["ok"] is False and KEY not in json.dumps(result)


@pytest.mark.parametrize("bad", ["", "   ", "short", "has space inside-key-1234", "line\nbreak-key-123456", None, 123])
def test_invalid_key_shapes_are_rejected_before_gh_is_called(bad):
    runner = Runner()
    result = setup.save_secret(bad, which=lambda name: "gh", runner=runner)
    assert result["ok"] is False and runner.calls == []


def test_secret_is_never_written_to_a_file_or_printed(monkeypatch, capsys):
    real_open = builtins.open

    def guarded(file, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in "wax+"), f"unexpected write open: {file}"
        return real_open(file, mode, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded)
    setup.save_secret(KEY, which=lambda name: "gh", runner=Runner())
    out = capsys.readouterr()
    assert KEY not in out.out and KEY not in out.err


def _serve(save=None, test=None):
    server = setup.make_server(0, save=save or (lambda key: {"ok": True, "message": "saved"}),
                               test=test or (lambda: {"ok": True, "message": "tested"}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def test_http_round_trip_requires_token_local_host_and_origin(capsys):
    received = []
    server, _ = _serve(save=lambda key: received.append(key) or {"ok": True, "message": "saved"})
    try:
        port = server.server_address[1]
        status, page = _request(server, "GET", "/")
        assert status == 200 and server.setup_token.encode() in page and KEY.encode() not in page
        good = {"Content-Type": "application/json", "X-Setup-Token": server.setup_token}
        body = json.dumps({"key": KEY})
        assert _request(server, "POST", "/api/save", body, good)[0] == 200 and received == [KEY]
        assert _request(server, "POST", "/api/save", body, {**good, "X-Setup-Token": "wrong"})[0] == 403
        assert _request(server, "POST", "/api/save", body, {**good, "Origin": "https://evil.example"})[0] == 403
        assert _request(server, "POST", "/api/save", body, {**good, "Host": "evil.example"})[0] == 403
        assert _request(server, "POST", "/api/save", body, {"X-Setup-Token": server.setup_token})[0] == 415
        assert _request(server, "POST", "/api/save", "x" * 5000, good)[0] == 413
        assert received == [KEY]
        output = capsys.readouterr()
        assert KEY not in output.out and KEY not in output.err
    finally:
        server.shutdown()
        server.server_close()


def test_connection_test_runs_the_stored_secret_smoke_workflow():
    log = "x VERCEL_AUTH=PASS\nx SUNBURST_MODEL=AVAILABLE\n"
    runner = Runner(extra={
        "gh run list": subprocess.CompletedProcess([], 0, stdout=json.dumps([{"databaseId": 7, "status": "completed"}]), stderr=""),
        "gh run view": subprocess.CompletedProcess([], 0, stdout=log, stderr=""),
    })
    result = setup.run_connection_test(which=lambda name: "gh", runner=runner, sleep=lambda s: None)
    assert result["ok"] is True and "VERCEL_AUTH=PASS" in result["message"] and "SUNBURST_MODEL=AVAILABLE" in result["message"]
    assert ["gh", "workflow", "run", "vercel-image-connection-test.yml"] in [call[0] for call in runner.calls]


def test_connection_test_reports_failure_codes():
    runner = Runner(extra={
        "gh run list": subprocess.CompletedProcess([], 0, stdout=json.dumps([{"databaseId": 7, "status": "completed"}]), stderr=""),
        "gh run view": subprocess.CompletedProcess([], 0, stdout="VERCEL_AUTH=FAIL\nVERCEL_ERROR=VERCEL_AUTH_FAILED\n", stderr=""),
    })
    result = setup.run_connection_test(which=lambda name: "gh", runner=runner, sleep=lambda s: None)
    assert result["ok"] is False and "VERCEL_AUTH_FAILED" in result["message"]


# --- /v1/models connection check -----------------------------------------------------------------------------------

class Net:
    def __init__(self, status=200, models=None):
        self.calls, self.status = [], status
        self.models = models if models is not None else [gateway.MODEL, "openai/other"]

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        if self.status != 200:
            return self.status, json.dumps({"error": {"message": "nope"}}).encode()
        return 200, json.dumps({"data": [{"id": m} for m in self.models]}).encode()


def test_connection_check_uses_get_models_only_and_creates_no_image(monkeypatch):
    net = Net()
    monkeypatch.setattr(gateway, "transport", net)
    result = gateway.check_connection(KEY)
    assert result == {"VERCEL_AUTH": "PASS", "SUNBURST_MODEL": "AVAILABLE"}
    assert [(m, u) for m, u, _, _ in net.calls] == [("GET", "https://ai-gateway.vercel.sh/v1/models")]
    assert net.calls[0][2]["Authorization"] == f"Bearer {KEY}" and net.calls[0][3] is None


def test_wrong_key_is_a_clear_auth_failure(monkeypatch):
    monkeypatch.setattr(gateway, "transport", Net(status=401))
    result = gateway.check_connection(KEY)
    assert result["VERCEL_AUTH"] == "FAIL" and result["VERCEL_ERROR"] == "VERCEL_AUTH_FAILED"


def test_exact_model_id_is_required(monkeypatch):
    monkeypatch.setattr(gateway, "transport", Net(models=["openai/gpt-image-2.5-sunburst-edit", "openai/gpt-image-2"]))
    result = gateway.check_connection(KEY)
    assert result["VERCEL_AUTH"] == "PASS" and result["SUNBURST_MODEL"] == "UNAVAILABLE"


def test_cli_prints_only_status_codes(monkeypatch, capsys):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", KEY)
    monkeypatch.setattr(gateway, "transport", Net())
    assert gateway.main(["check"]) == 0
    out = capsys.readouterr().out
    assert out.split() == ["VERCEL_AUTH=PASS", "SUNBURST_MODEL=AVAILABLE"] and KEY not in out
    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    assert gateway.main(["check"]) == 1


def test_smoke_workflow_is_dispatch_only_reads_the_secret_and_prints_nothing_else():
    text = (ROOT / ".github" / "workflows" / "vercel-image-connection-test.yml").read_text(encoding="utf-8")
    assert re.search(r"^on:\n  workflow_dispatch:\n", text, re.M) and "push:" not in text and "schedule:" not in text
    assert "AI_GATEWAY_API_KEY: ${{ secrets.AI_GATEWAY_API_KEY }}" in text
    assert "python scripts/vercel_gateway.py check" in text
    for forbidden in ("curl", "env |", "printenv", "set -x", "-v ", "Authorization"):
        assert forbidden not in text


def test_gitignore_covers_secret_files_and_env_example_is_empty():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", ".env.local", ".env.*", "!.env.example", "*.secret", "secrets/"):
        assert pattern in ignore
    assert "AI_GATEWAY_API_KEY=" in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()


def test_running_the_flows_leaves_the_working_tree_untouched():
    try:
        before = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    setup.save_secret(KEY, which=lambda name: "gh", runner=Runner())
    after = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert before == after


def test_secret_is_set_on_the_explicit_origin_repository_and_the_page_says_which():
    origin = subprocess.CompletedProcess([], 0, stdout="https://github.com/marwalfarsi2000/sleep-history-channel.git\n", stderr="")
    runner = Runner(extra={"git remote get-url": origin})
    result = setup.save_secret(KEY, which=lambda name: "gh", runner=runner)
    argv = next(call[0] for call in runner.calls if call[0][:3] == ["gh", "secret", "set"])
    assert argv == ["gh", "secret", "set", "AI_GATEWAY_API_KEY", "--repo", "marwalfarsi2000/sleep-history-channel"]
    assert result["ok"] and "marwalfarsi2000/sleep-history-channel" in result["message"] and KEY not in json.dumps(result)


@pytest.mark.parametrize("url,slug", [("git@github.com:owner/repo.git", "owner/repo"), ("https://github.com/owner/repo", "owner/repo"),
                                      ("https://example.com/x/y.git", None), ("", None)])
def test_repo_slug_parsing(url, slug):
    runner = Runner(extra={"git remote get-url": subprocess.CompletedProcess([], 0, stdout=url, stderr="")})
    assert setup.repo_slug(runner) == slug
