from __future__ import annotations

import inspect

import pipeline


def test_audio_qc_tokens_remove_diacritics_and_tatweel_only():
    assert pipeline._audio_qc_tokens("إِنَّ هٰذَا نَصٌّ عَرَبِيٌّ ـ وَاضِحٌ") == [
        "إن", "هذا", "نص", "عربي", "واضح"
    ]


def test_audio_qc_diff_is_bounded_and_uses_full_repetitive_stream():
    source = ["في"] * 250 + ["تلمسان", "صباغة", "الصوف"] + ["من"] * 250
    transcript = list(source)
    transcript[251] = "النسيج"

    diff = pipeline._audio_qc_diff(source, transcript)
    assert diff
    assert diff[0]["tag"] == "replace"
    assert "صباغة" in diff[0]["source"]
    assert "النسيج" in diff[0]["transcript"]


def test_validate_audio_disables_sequence_matcher_autojunk_and_persists_failure_transcript():
    source = inspect.getsource(pipeline.validate_audio)
    assert "autojunk=False" in source
    assert "audio_qc_failure_" in source
    assert '"transcript": transcript' in source
    assert '"diff": _audio_qc_diff' in source
