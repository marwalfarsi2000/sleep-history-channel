from __future__ import annotations

import unittest

import pipeline


class DiacritizationAlignTests(unittest.TestCase):
    def test_dropped_and_added_words_never_change_the_source_text(self):
        source = "ذهب التجار إلى السوق في الصباح الباكر، وكانت المدينة هادئة."
        candidate = "ذَهَبَ التُّجّارُ إلى السُّوقِ في الصَّباحِ، وَكانَتِ المَدِينَةُ هادِئَةً جِدًّا."
        aligned = pipeline.align_source_diacritics(source, candidate)
        self.assertEqual(pipeline.DIACRITICS_RE.sub("", aligned), source)
        self.assertTrue(pipeline.lexical_fidelity(source, aligned)[0])
        self.assertIn("التُّجّارُ", aligned)

    def test_merged_conjunction_keeps_original_spacing(self):
        source = "و كانت القوافل تصل مساء."
        candidate = "وَكانَتِ القَوافِلُ تَصِلُ مَساءً."
        aligned = pipeline.align_source_diacritics(source, candidate)
        self.assertEqual(pipeline.DIACRITICS_RE.sub("", aligned), source)


if __name__ == "__main__":
    unittest.main()
