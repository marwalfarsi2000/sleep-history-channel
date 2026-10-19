"""One live Sunburst generation through Vercel AI Gateway for owner approval (manual workflow, single attempt).

The reference is a clean frame of an already reviewed episode video (read only); nothing is changed or published.
Outputs go to ``sunburst-live-test/`` and are uploaded as a workflow artifact. No secrets are written.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PIL import Image

import sunburst_thumbnail as sunburst
import vercel_gateway as gateway

TEST_PHRASE = "ليالي طرابلس الهادئة"  # a trial phrase, not a real episode hook
OUT = Path("sunburst-live-test")


def main(argv: list[str] | None = None) -> int:
    frame = Path((argv if argv is not None else sys.argv[1:])[0])
    OUT.mkdir(exist_ok=True)
    story = OUT / "story"
    story.mkdir(exist_ok=True)
    shutil.copy2(frame, story / "10_background.png")
    shutil.copy2(frame, OUT / "1_reference_background.png")
    with Image.open(frame) as reference:
        result = {"model": gateway.MODEL, "requested_size": gateway.IMAGE_SIZE, "quality": gateway.IMAGE_QUALITY,
                  "endpoint": f"{gateway.BASE_URL}/images/edits", "phrase": TEST_PHRASE, "attempts_allowed": 1,
                  "reference_size": list(reference.size)}
    try:
        record = sunburst.generate(story, TEST_PHRASE, max_attempts=1)
    except gateway.GatewayError as exc:
        result.update(status="FAIL", error_code=exc.code, error=str(exc)[:300])
        (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"SUNBURST_LIVE_TEST=FAIL {exc.code}")
        return 1
    final = story / sunburst.THUMBNAIL_FILE
    shutil.copy2(final, OUT / "2_sunburst_thumbnail.jpg")
    with Image.open(final) as image:
        size = list(image.size)
    result.update(status="PASS", **record, final_size=size, final_bytes=final.stat().st_size,
                  background_unchanged=(story / "10_background.png").read_bytes() == frame.read_bytes(),
                  text_drawn_by_code=False, paid_fallback_used=False)
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(story, ignore_errors=True)
    print(f"SUNBURST_LIVE_TEST=PASS size={size[0]}x{size[1]} attempts={record['thumbnail_attempts']}")
    return 0 if size == [1920, 1080] else 1


if __name__ == "__main__":
    raise SystemExit(main())
