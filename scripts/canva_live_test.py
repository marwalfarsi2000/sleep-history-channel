"""One live Canva thumbnail (n8n: Lucid Origin background -> template autofill -> export) for owner review.

Manual workflow only, single attempt, one generated image. Nothing is changed or published; outputs go to
``canva-live-test/`` and are uploaded as a workflow artifact. No secrets are written.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image

import canva_thumbnail as canva

TEST_PHRASE = "داخل ورشة سمرقند"
TEST_SCENE = (
    "16:9 YouTube thumbnail background, cinematic wide establishing shot of a calm 15th century Samarkand craftsmen "
    "workshop at golden hour, warm soft light through an arched window, hand-woven carpets and blue ceramic tiles, "
    "photorealistic historical reconstruction"
)
OUT = Path("canva-live-test")


def main() -> int:
    OUT.mkdir(exist_ok=True)
    story = OUT / "story"
    story.mkdir(exist_ok=True)
    prompt = canva.build_background_prompt(TEST_SCENE)
    result = {"template": canva.TEMPLATE, "background_model": canva.BACKGROUND_MODEL, "phrase": TEST_PHRASE,
              "prompt": prompt, "attempts_allowed": 1}
    try:
        record = canva.generate(story, TEST_PHRASE, prompt, max_attempts=1)
    except canva.CanvaError as exc:
        result.update(status="FAIL", error_code=exc.code, error=canva.redact(str(exc))[:300])
        (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"CANVA_LIVE_TEST=FAIL {exc.code}")
        print(f"CANVA_LIVE_TEST_ERROR={canva.redact(str(exc))[:300]}")
        return 1
    final = story / canva.THUMBNAIL_FILE
    shutil.copy2(final, OUT / "canva_thumbnail.jpg")
    with Image.open(final) as image:
        size = list(image.size)
    final_bytes = final.stat().st_size  # read before the working folder is removed
    result.update(status="PASS", **record, final_size=size, final_bytes=final_bytes, mime="image/jpeg",
                  text_drawn_by_code=False, fallback_used=False)
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(story, ignore_errors=True)
    print(f"CANVA_LIVE_TEST=PASS size={size[0]}x{size[1]} bytes={final_bytes}")
    return 0 if size == [1280, 720] else 1


if __name__ == "__main__":
    raise SystemExit(main())
