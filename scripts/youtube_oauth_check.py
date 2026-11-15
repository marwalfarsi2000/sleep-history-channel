"""Read-only OAuth preflight. Never uploads, changes videos, or prints credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from google.auth.exceptions import RefreshError

import youtube_publish as yp

FIELDS = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")


def load_youtube_fields(path: Path) -> None:
    """Load only the three YouTube fields from an existing external secret file."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in FIELDS:
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def check(*, offline: bool = False) -> dict:
    present = {key: bool(os.environ.get(key, "").strip()) for key in FIELDS}
    report = {"credential_fields_set": present, "publishes_video": False,
              "client_refresh_binding": "NOT_VERIFIED"}
    if not all(present.values()):
        return {**report, "status": "BLOCKED", "action": "PROVIDE_MATCHING_YOUTUBE_OAUTH_FIELDS"}
    if offline:
        return {**report, "status": "NOT_VERIFIED", "action": "RUN_READ_ONLY_OAUTH_PREFLIGHT"}
    try:
        mode = yp.publish_mode()
        granted = yp.get_granted_scopes(yp.credentials())
        gap = yp.capability_gap(mode, granted)
    except RefreshError as exc:
        blocked = yp.oauth_refresh_block(exc)
        return {**report, "status": "BLOCKED", "action": (
            blocked["action"] if blocked else "REVIEW_OAUTH_PROVIDER_FAILURE")}
    except Exception:
        # A provider exception can contain a token-bearing URL or request body.
        return {**report, "status": "NOT_VERIFIED", "action": "REVIEW_OAUTH_PREFLIGHT_CONNECTIVITY_OR_BINDING"}
    return {**report, "status": "BLOCKED" if gap else "PASS",
            "client_refresh_binding": "PASS", "publish_mode": mode,
            "granted_scopes": sorted(granted),
            "readback_available": bool(granted & yp.READ_SCOPES),
            "action": gap["action"] if gap else "NONE"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", type=Path,
                        default=Path(os.environ.get("SLEEP_HISTORY_SECRETS_FILE") or
                                     Path.home() / "AppData/Local/SleepHistoryAutomation/secrets.env"))
    parser.add_argument("--offline", action="store_true", help="Presence only; does not prove client/token compatibility")
    args = parser.parse_args()
    load_youtube_fields(args.credentials_file)
    report = check(offline=args.offline)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
