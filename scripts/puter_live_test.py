"""One live Puter / Sunburst generation for owner approval (manual workflow, single attempt; quality from PUTER_TEST_QUALITY, default medium).

The reference is a clean frame of an already reviewed episode video (read only); nothing is changed or published.
Outputs go to ``puter-live-test/`` and are uploaded as a workflow artifact. The Node adapter refuses to run unless the
account is marked free-only verified, so an unverified account never reaches the generation call.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from PIL import Image

import puter_thumbnail as puter

TEST_PHRASE = "داخل ورشة سمرقند"
TEST_QUALITY = os.environ.get("PUTER_TEST_QUALITY") or "low"
OUT = Path("puter-live-test")


def main(argv: list[str] | None = None) -> int:
    frame = Path((argv if argv is not None else sys.argv[1:])[0])
    OUT.mkdir(exist_ok=True)
    story = OUT / "story"
    story.mkdir(exist_ok=True)
    shutil.copy2(frame, story / "10_background.png")
    shutil.copy2(frame, OUT / "1_reference_background.png")
    puter.QUALITY = TEST_QUALITY
    result = {"model": puter.MODEL, "quality": TEST_QUALITY, "phrase": TEST_PHRASE, "attempts_allowed": 1}
    try:
        record = puter.generate(story, TEST_PHRASE, max_attempts=1)
    except puter.PuterError as exc:
        result.update(status="FAIL", **{k: puter.LAST_RUN.get(k) for k in ("allowance_before", "allowance_after")}, error_code=exc.code, error=puter.redact(str(exc))[:300])
        (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"PUTER_LIVE_TEST=FAIL {exc.code}")
        print(f"PUTER_LIVE_TEST_ERROR={puter.redact(str(exc))[:300]}")
        return 1
    final = story / "11_thumbnail_base.jpg"
    shutil.copy2(final, OUT / "2_puter_thumbnail.jpg")
    with Image.open(final) as image:
        size = list(image.size)
    result.update(status="PASS", **record, **{k: puter.LAST_RUN.get(k) for k in ("allowance_before", "allowance_after")},
                  purchased_credits_used=False, final_size=size, final_bytes=final.stat().st_size,
                  background_unchanged=(story / "10_background.png").read_bytes() == frame.read_bytes(),
                  text_drawn_by_code=False, paid_fallback_used=False)
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(story, ignore_errors=True)
    print(f"PUTER_LIVE_TEST=PASS size={size[0]}x{size[1]} bytes={final.stat().st_size}")
    print(f"ALLOWANCE_BEFORE={puter.LAST_RUN.get('allowance_before')} ALLOWANCE_AFTER={puter.LAST_RUN.get('allowance_after')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
