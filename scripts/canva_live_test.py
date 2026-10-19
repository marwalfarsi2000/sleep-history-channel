"""One live Canva thumbnail (n8n: upload -> autofill -> export) for owner review. Manual workflow only, single attempt.

The reference is a clean frame of an already reviewed episode video (read only); nothing is changed or published.
Outputs go to ``canva-live-test/`` and are uploaded as a workflow artifact. No secrets are written.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

import canva_thumbnail as canva

TEST_PHRASE = "داخل ورشة سمرقند"
OUT = Path("canva-live-test")


def text_region_changed(final: Image.Image, reference: Image.Image) -> float:
    """Mean absolute difference between the export and the plain background in the template's text zone (lower right)."""
    plain = reference.convert("RGB").resize(canva.OUTPUT_SIZE)
    box = (560, 480, 1220, 640)
    return ImageStat.Stat(ImageChops.difference(final.crop(box), plain.crop(box)).convert("L")).mean[0]


def main(argv: list[str] | None = None) -> int:
    frame = Path((argv if argv is not None else sys.argv[1:])[0])
    OUT.mkdir(exist_ok=True)
    story = OUT / "story"
    story.mkdir(exist_ok=True)
    shutil.copy2(frame, story / "10_background.png")
    shutil.copy2(frame, OUT / "1_reference_background.png")
    result = {"template": canva.TEMPLATE, "phrase": TEST_PHRASE, "attempts_allowed": 1}
    try:
        record = canva.generate(story, TEST_PHRASE, max_attempts=1)
    except canva.CanvaError as exc:
        result.update(status="FAIL", error_code=exc.code, error=canva.redact(str(exc))[:300])
        (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"CANVA_LIVE_TEST=FAIL {exc.code}")
        print(f"CANVA_LIVE_TEST_ERROR={canva.redact(str(exc))[:300]}")
        return 1
    final = story / canva.THUMBNAIL_FILE
    shutil.copy2(final, OUT / "2_canva_thumbnail.jpg")
    with Image.open(final) as image, Image.open(frame) as reference:
        size = list(image.size)
        changed = round(text_region_changed(image.convert("RGB"), reference), 2)
    result.update(status="PASS", **record, final_size=size, final_bytes=final.stat().st_size, mime="image/jpeg",
                  text_zone_difference=changed, text_drawn_by_code=False,
                  background_unchanged=(story / "10_background.png").read_bytes() == frame.read_bytes())
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(story, ignore_errors=True)
    print(f"CANVA_LIVE_TEST=PASS size={size[0]}x{size[1]} bytes={final.stat().st_size} text_zone_difference={changed}")
    return 0 if size == [1280, 720] else 1


if __name__ == "__main__":
    raise SystemExit(main())
