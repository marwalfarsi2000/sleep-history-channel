from __future__ import annotations

import hashlib
import json

import canva_thumbnail
import pipeline


def test_completed_fast_path_rejects_changed_thumbnail_inputs(tmp_path):
    story = tmp_path / "story"
    story.mkdir()

    scene_prompt = "A calm historical market with open title space."
    thumbnail_prompt = "A cinematic historical market."
    thumbnail_text = "Historic Market"

    (story / "08_image_prompt_en.txt").write_text(scene_prompt, encoding="utf-8")
    effective = canva_thumbnail.build_background_prompt(thumbnail_prompt, scene_prompt)

    required_files = {
        "clean_story_sha256": "05_story_clean.md",
        "tts_normalized_sha256": "05b_story_tts_normalized.md",
        "tts_api_sha256": "07_story_tts_api.md",
        "audio_sha256": "12_audio_full.mp3",
        "image_sha256": "10_background.png",
        "thumbnail_sha256": "11_thumbnail_base.jpg",
        "video_sha256": "14_video_final.mp4",
    }
    metadata = {
        "thumbnail_text": thumbnail_text,
        "thumbnail_prompt": thumbnail_prompt,
        "thumbnail_input_sha256": canva_thumbnail.input_identity(thumbnail_text, effective),
        "youtube_packaging_status": "passed",
        "youtube_packaging_validation_status": "passed",
        "thumbnail_text_render_status": "passed",
    }
    for status in (
        "fact_check_status",
        "second_fact_check_status",
        "diacritization_status",
        "tts_normalization_status",
        "speech_safe_validation_status",
        "pronunciation_validation_status",
        "tts_preparation_status",
        "audio_qc_status",
        "image_generation_status",
        "image_accuracy_status",
        "video_qc_status",
        "metadata_validation_status",
    ):
        metadata[status] = "passed"

    for key, filename in required_files.items():
        path = story / filename
        path.write_bytes(filename.encode("utf-8"))
        metadata[key] = hashlib.sha256(path.read_bytes()).hexdigest()

    metadata_path = story / "15_metadata.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert pipeline.completed_artifacts_are_current(story) is True

    metadata["thumbnail_text"] = "Different Historic Market"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert pipeline.completed_artifacts_are_current(story) is False
