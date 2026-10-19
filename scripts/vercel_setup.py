"""Local-only setup page: paste the Vercel AI Gateway key once and store it as the GitHub secret AI_GATEWAY_API_KEY.

    python scripts/vercel_setup.py            # then open the printed LOCAL_URL

The server listens on 127.0.0.1 only. The key travels browser -> this process -> ``gh secret set`` standard input.
It is never written to a file, argument list, log, cookie or web storage, and it is never shown again.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SECRET_NAME = "AI_GATEWAY_API_KEY"
TEST_WORKFLOW = "vercel-image-connection-test.yml"
MAX_BODY = 4096
RESULT_LINE = re.compile(r"\b(VERCEL_AUTH|SUNBURST_MODEL|VERCEL_ERROR)=([A-Z_]+)\b")

MSG_SAVED = "✅ تم حفظ مفتاح Vercel بنجاح في GitHub Secrets."
MSG_NO_GH = "GitHub CLI غير مثبت."
ACTION_NO_GH = "OWNER_ACTION: ثبّتي GitHub CLI من https://cli.github.com ثم أعيدي تشغيل هذه الصفحة."
MSG_NO_AUTH = "GitHub CLI يحتاج تسجيل دخول."
ACTION_NO_AUTH = "OWNER_ACTION: شغّلي الأمر gh auth login في Terminal ثم أعيدي المحاولة هنا."
MSG_BAD_KEY = "المفتاح فارغ أو بصيغة غير صالحة. ألصقيه كما هو دون مسافات."
MSG_SAVE_FAILED = "تعذّر حفظ المفتاح في GitHub Secrets. تأكدي من صلاحيات الحساب على المستودع ثم أعيدي المحاولة."


REMOTE_PATTERN = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def repo_slug(runner=subprocess.run) -> str | None:
    """owner/repo of this checkout's ``origin``; the secret is set on exactly this repository, never on a guess."""
    result = runner(["git", "remote", "get-url", "origin"], capture_output=True, text=True, cwd=ROOT, check=False, shell=False)
    match = REMOTE_PATTERN.search((result.stdout or "").strip()) if result.returncode == 0 else None
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _repo_args(runner) -> list[str]:
    slug = repo_slug(runner)
    return ["--repo", slug] if slug else []


def valid_key_shape(key: object) -> bool:
    return isinstance(key, str) and 8 <= len(key) <= 512 and key.isascii() and key.isprintable() and not any(c.isspace() for c in key)


def gh_state(which=shutil.which, runner=subprocess.run) -> str:
    """'missing', 'unauthenticated' or 'ready'; exit codes only, output is never read."""
    if not which("gh"):
        return "missing"
    result = runner(["gh", "auth", "status"], capture_output=True, cwd=ROOT, check=False, shell=False)
    return "ready" if result.returncode == 0 else "unauthenticated"


def save_secret(key: object, which=shutil.which, runner=subprocess.run) -> dict:
    """Store the key as the repository secret through ``gh secret set`` reading standard input."""
    if not valid_key_shape(key):
        return {"ok": False, "message": MSG_BAD_KEY}
    state = gh_state(which, runner)
    if state == "missing":
        return {"ok": False, "message": MSG_NO_GH, "owner_action": ACTION_NO_GH}
    if state == "unauthenticated":
        return {"ok": False, "message": MSG_NO_AUTH, "owner_action": ACTION_NO_AUTH}
    payload = bytearray(str(key).encode("ascii"))
    try:
        repo = _repo_args(runner)
        result = runner(["gh", "secret", "set", SECRET_NAME, *repo], input=payload, capture_output=True, cwd=ROOT,
                        check=False, shell=False)
    finally:
        for index in range(len(payload)):  # best-effort scrub of our own copy
            payload[index] = 0
        del payload
    # only the exit code is used: the CLI output is never printed or returned
    if result.returncode != 0:
        return {"ok": False, "message": MSG_SAVE_FAILED}
    where = f"\nالمستودع: {repo[1]}" if repo else ""
    return {"ok": True, "message": MSG_SAVED + where}


def run_connection_test(which=shutil.which, runner=subprocess.run, sleep=time.sleep, timeout: int = 240) -> dict:
    """Dispatch the Actions smoke test (GET /v1/models only) so the *stored* secret is what gets proven."""
    state = gh_state(which, runner)
    if state == "missing":
        return {"ok": False, "message": MSG_NO_GH, "owner_action": ACTION_NO_GH}
    if state == "unauthenticated":
        return {"ok": False, "message": MSG_NO_AUTH, "owner_action": ACTION_NO_AUTH}
    started = time.time()
    if runner(["gh", "workflow", "run", TEST_WORKFLOW, *_repo_args(runner)], capture_output=True, cwd=ROOT, check=False, shell=False).returncode:
        return {"ok": False, "message": "تعذّر تشغيل اختبار الاتصال في GitHub Actions (هل دُمج الـ workflow في main؟)."}
    run_id = None
    while time.time() - started < timeout:
        sleep(5)
        listing = runner(["gh", "run", "list", f"--workflow={TEST_WORKFLOW}", "--limit", "1", "--json",
                          "databaseId,status,conclusion,createdAt", *_repo_args(runner)], capture_output=True, text=True, cwd=ROOT,
                         check=False, shell=False)
        try:
            latest = json.loads(listing.stdout or "[]")[0]
        except (ValueError, IndexError):
            continue
        if latest.get("status") == "completed":
            run_id = latest["databaseId"]
            break
    if run_id is None:
        return {"ok": False, "message": "انتهت مهلة اختبار الاتصال دون نتيجة."}
    log = runner(["gh", "run", "view", str(run_id), "--log", *_repo_args(runner)], capture_output=True, text=True, cwd=ROOT,
                 check=False, shell=False).stdout or ""
    found = dict(RESULT_LINE.findall(log))  # the workflow prints status codes only, never the key
    auth, model = found.get("VERCEL_AUTH", "FAIL"), found.get("SUNBURST_MODEL", "NOT_TESTED")
    ok = auth == "PASS" and model == "AVAILABLE"
    text = f"VERCEL_AUTH={auth} · SUNBURST_MODEL={model}" + (f" · {found['VERCEL_ERROR']}" if "VERCEL_ERROR" in found else "")
    return {"ok": ok, "message": ("✅ " if ok else "❌ ") + text}


PAGE = """<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>إعداد Vercel للصور</title>
<style>
body{font-family:Segoe UI,Tahoma,sans-serif;background:#f4efe5;color:#4f4035;max-width:560px;margin:48px auto;padding:0 20px;line-height:1.9}
h1{font-size:1.5rem}.card{background:#fffaf0;border:1px solid #cec7b6;border-radius:16px;padding:24px}
.row{display:flex;gap:8px;margin:16px 0}input{flex:1;padding:12px;font-size:1rem;border:1px solid #cec7b6;border-radius:10px;direction:ltr}
button{padding:11px 18px;font-size:1rem;border:0;border-radius:10px;background:#826d5c;color:#fff;cursor:pointer}
button.secondary{background:#e6dfd0;color:#4f4035}button:disabled{opacity:.5;cursor:wait}#status{margin-top:16px;min-height:2em;white-space:pre-wrap}
</style></head><body><div class="card">
<h1>إعداد Vercel للصور</h1>
<p>ألصقي مفتاح Vercel AI Gateway هنا. سيتم حفظه بأمان لاستخدام GPT Image 2.5 Sunburst في إنشاء صور القناة.</p>
<div class="row"><input id="key" type="password" autocomplete="off" spellcheck="false" placeholder="AI Gateway API key">
<button id="toggle" class="secondary" type="button">إظهار</button></div>
<div class="row"><button id="save" type="button">حفظ المفتاح</button><button id="test" class="secondary" type="button">اختبار الاتصال</button></div>
<div id="status" role="status"></div></div>
<script nonce="__NONCE__">
const TOKEN = "__TOKEN__";
const key = document.getElementById("key"), statusBox = document.getElementById("status");
const saveBtn = document.getElementById("save"), testBtn = document.getElementById("test");
document.getElementById("toggle").addEventListener("click", (e) => {
  const hidden = key.type === "password"; key.type = hidden ? "text" : "password"; e.target.textContent = hidden ? "إخفاء" : "إظهار";
});
async function call(path, body, button, waiting) {
  button.disabled = true; statusBox.textContent = waiting;
  try {
    const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Setup-Token": TOKEN}, body: JSON.stringify(body)});
    const result = await response.json();
    statusBox.textContent = result.message + (result.owner_action ? "\\n" + result.owner_action : "");
    return result;
  } catch (error) { statusBox.textContent = "تعذّر الاتصال بالصفحة المحلية."; }
  finally { button.disabled = false; }
}
saveBtn.addEventListener("click", async () => {
  const value = key.value; key.value = ""; key.type = "password"; document.getElementById("toggle").textContent = "إظهار";
  await call("/api/save", {key: value}, saveBtn, "جارٍ الحفظ…");
});
testBtn.addEventListener("click", () => call("/api/test", {}, testBtn, "جارٍ اختبار الاتصال عبر GitHub Actions… قد يستغرق دقيقة."));
</script></body></html>
"""


def make_handler(token: str, port: int, save=save_secret, test=run_connection_test):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "VercelSetup"

        def log_message(self, *args):  # never log requests
            pass

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _local(self) -> bool:
            origin = self.headers.get("Origin")
            return self.headers.get("Host", "") in allowed_hosts and (origin is None or origin in allowed_origins)

        def do_GET(self):
            if not self._local():
                return self._json(403, {"message": "Forbidden"})
            if self.path.split("?")[0] != "/":
                return self._json(404, {"message": "Not found"})
            nonce = secrets.token_urlsafe(16)
            page = PAGE.replace("__NONCE__", nonce).replace("__TOKEN__", html.escape(token))
            csp = f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'"
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": csp})

        def do_POST(self):
            if not self._local() or self.headers.get("X-Setup-Token") != token:
                return self._json(403, {"message": "Forbidden"})
            if not (self.headers.get("Content-Type", "").startswith("application/json")):
                return self._json(415, {"message": "Unsupported"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                return self._json(413, {"message": "Too large"})
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                body = {}
            finally:
                del raw
            if self.path == "/api/save":
                value = body.get("key") if isinstance(body, dict) else None
                result = save(value)
                value = None
                return self._json(200, result)
            if self.path == "/api/test":
                return self._json(200, test())
            return self._json(404, {"message": "Not found"})

    return Handler


def make_server(port: int = DEFAULT_PORT, save=save_secret, test=run_connection_test) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((HOST, port), lambda *a, **k: None)  # placeholder, replaced below
    actual_port = server.server_address[1]
    server.RequestHandlerClass = make_handler(token, actual_port, save, test)
    server.setup_token = token
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    server = make_server(args.port)
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"LOCAL_URL={url}", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
