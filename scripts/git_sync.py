"""Helper for non-blocking git state persistence in cloud environment."""

import subprocess
import sys


import os

def sync_git_catalogue() -> bool:
    try:
        subprocess.run(["git", "config", "user.name", "sleep-history-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "marwalfarsi2000@gmail.com"], check=False)
        subprocess.run([
            "git", "add",
            "config/topics_history.json",
            "config/topic_candidate_bank.json",
            "config/archive_index.json",
            "config/tts_usage.json",
            "config/tts_quota_state.json",
            "config/pronunciation_dictionary.json"
        ], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update sleep history catalogue"], check=False)
            target_branch = os.environ.get("GITHUB_REF_NAME", "").strip() or "main"
            for attempt in range(1, 4):
                rebase = subprocess.run(["git", "pull", "--rebase", "origin", target_branch])
                if rebase.returncode != 0:
                    subprocess.run(["git", "rebase", "--abort"], check=False)
                    print(f"WARNING: Git rebase failed on attempt {attempt}, aborted rebase.", file=sys.stderr)
                    continue
                push = subprocess.run(["git", "push", "origin", f"HEAD:{target_branch}"])
                if push.returncode == 0:
                    return True
            print("WARNING: Git state persistence push failed after retries, but production result is preserved.", file=sys.stderr)
            return False
        return True
    except Exception as exc:
        print(f"WARNING: Exception during git catalogue sync: {exc}", file=sys.stderr)
        return False


def main() -> int:
    return 0 if sync_git_catalogue() else 1


if __name__ == "__main__":
    raise SystemExit(main())
