"""Persist runtime state to the automation-state branch. Never pushes to main."""

import sys

import state_store


def sync_git_catalogue() -> bool:
    try:
        return state_store.persist_all()
    except Exception as exc:
        print(f"WARNING: runtime state persistence failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    return 0 if sync_git_catalogue() else 1


if __name__ == "__main__":
    raise SystemExit(main())
