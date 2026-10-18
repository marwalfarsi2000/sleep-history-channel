import json
import unittest

import thumbnail_hook as hook

TITLE = "صناعة الورق في سمرقند العصر الساماني"


def cand(text, **scores):
    base = {key: 4 for key in hook.SCORE_KEYS}
    base.update(scores)
    return {"text": text, "scores": base}


class HookRejectionTests(unittest.TestCase):
    def test_generic_scene_labels_rejected(self):
        for label in ("ورش صناعة الورق", "أسواق سمرقند القديمة", "سواقي النيل", "حياة الورّاقين"):
            self.assertTrue(hook.hook_rejection(label, TITLE, []), label)

    def test_acceptable_directions_pass(self):
        for label in ("سرّ ورق سمرقند", "كيف صنعوا الورق؟", "من التوت إلى ورق"):
            self.assertEqual(hook.hook_rejection(label, TITLE, []), "", label)

    def test_title_repetition_clickbait_recent_and_length_rejected(self):
        self.assertIn("title", hook.hook_rejection("الورق في سمرقند الساماني", TITLE, []))
        self.assertIn("clickbait", hook.hook_rejection("لن تصدق ورق سمرقند", TITLE, []))
        self.assertIn("recent", hook.hook_rejection("سرّ ورق سمرقند", TITLE, ["سر ورق سمرقند"]))
        self.assertIn("words", hook.hook_rejection("سر ورق سمرقند الذي غير وجه العالم كله", TITLE, []))
        self.assertIn("words", hook.hook_rejection("ورق", TITLE, []))


class ChooseHookTests(unittest.TestCase):
    def test_generic_never_wins_even_with_top_scores(self):
        text, rejected = hook.choose_hook(
            [cand("ورش صناعة الورق", curiosity=5, clarity=5), cand("سرّ ورق سمرقند", curiosity=3)], TITLE, []
        )
        self.assertEqual(text, "سرّ ورق سمرقند")
        self.assertEqual(rejected[0]["text"], "ورش صناعة الورق")

    def test_fifth_word_needs_to_be_clearly_stronger(self):
        five = cand("كيف صنعوا ورق سمرقند اللامع", curiosity=5)
        four = cand("من التوت إلى ورق", curiosity=5)
        self.assertEqual(hook.choose_hook([five, four], TITLE, [])[0], "من التوت إلى ورق")

    def test_none_viable(self):
        self.assertEqual(hook.choose_hook([cand("ورش صناعة الورق")], TITLE, [])[0], "")


class WriteHookTests(unittest.TestCase):
    def test_write_hook_picks_best_and_retries(self):
        replies = iter([
            json.dumps({"candidates": [cand("أسواق بغداد القديمة")] * 4}),
            json.dumps({"candidates": [cand("ورش صناعة الورق"), cand("سرّ ورق سمرقند"), cand("من التوت إلى ورق", curiosity=5), cand("كيف صنعوا الورق؟")]}),
        ])
        gemini = lambda *a, **k: (next(replies), {})
        self.assertEqual(hook.write_hook(gemini, json.loads, {}, TITLE, []), "من التوت إلى ورق")

    def test_write_hook_empty_when_nothing_acceptable(self):
        gemini = lambda *a, **k: (json.dumps({"candidates": [cand("ورش صناعة الورق")] * 4}), {})
        self.assertEqual(hook.write_hook(gemini, json.loads, {}, TITLE, []), "")


if __name__ == "__main__":
    unittest.main()
