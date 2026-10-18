import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_telegram_worker_node_suite_passes():
    tests = sorted(str(path.relative_to(REPO)) for path in (REPO / "cloudflare").glob("*.test.js"))
    assert tests, "no cloudflare worker tests found"
    result = subprocess.run(
        ["node", "--test", *tests],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
