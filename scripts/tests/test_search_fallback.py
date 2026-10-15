from __future__ import annotations

import io
import unittest
import urllib.error
from unittest import mock

import generate_daily

TOPIC = {
    "topic_ar": "قرطبة",
    "angle_ar": "الحياة اليومية",
    "period_ar": "الخلافة",
    "search_query_en": "daily life Caliphate of Cordoba",
}


def _results(*domains: str) -> list[dict]:
    return [
        {"title": f"Source {index}", "url": f"https://{domain}/page{index}", "excerpt": "evidence"}
        for index, domain in enumerate(domains)
    ]


def _quota_error() -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.exa.ai/search", 429, "Too Many Requests", {}, io.BytesIO(b"{}"))


class SearchFallbackTests(unittest.TestCase):
    def test_exhausted_provider_moves_to_next_in_order(self):
        exa = mock.Mock(side_effect=_quota_error())
        brave = mock.Mock(return_value=_results("museum.org", "university.edu", "unesco.org"))
        firecrawl = mock.Mock()
        providers = (("exa", "EXA_API_KEY", exa), ("brave", "BRAVE_SEARCH_API_KEY", brave), ("firecrawl", "FIRECRAWL_API_KEY", firecrawl))
        secrets = {"EXA_API_KEY": "a", "BRAVE_SEARCH_API_KEY": "b", "FIRECRAWL_API_KEY": "c"}
        with mock.patch.object(generate_daily, "SEARCH_PROVIDERS", providers), \
             mock.patch.object(generate_daily, "load_secrets", return_value=secrets):
            provider, results = generate_daily._collect_sources(TOPIC)
        self.assertEqual(provider, "brave")
        self.assertEqual(len(results), 3)
        firecrawl.assert_not_called()

    def test_provider_without_enough_independent_sources_is_skipped(self):
        exa = mock.Mock(return_value=_results("same.org", "same.org", "same.org"))
        firecrawl = mock.Mock(return_value=_results("a.org", "b.org", "c.org"))
        providers = (("exa", "EXA_API_KEY", exa), ("brave", "BRAVE_SEARCH_API_KEY", mock.Mock()), ("firecrawl", "FIRECRAWL_API_KEY", firecrawl))
        with mock.patch.object(generate_daily, "SEARCH_PROVIDERS", providers), \
             mock.patch.object(generate_daily, "load_secrets", return_value={"EXA_API_KEY": "a", "FIRECRAWL_API_KEY": "c"}):
            provider, _ = generate_daily._collect_sources(TOPIC)
        self.assertEqual(provider, "firecrawl")

    def test_all_providers_failing_stays_fail_closed(self):
        providers = (("exa", "EXA_API_KEY", mock.Mock(side_effect=_quota_error())),)
        with mock.patch.object(generate_daily, "SEARCH_PROVIDERS", providers), \
             mock.patch.object(generate_daily, "load_secrets", return_value={"EXA_API_KEY": "a"}):
            with self.assertRaisesRegex(RuntimeError, "GROUNDED_RESEARCH_UNAVAILABLE"):
                generate_daily._collect_sources(TOPIC)

    def test_fallback_notes_always_carry_direct_source_urls(self):
        results = _results("a.org", "b.org", "c.org")
        with mock.patch.object(generate_daily, "_collect_sources", return_value=("exa", results)), \
             mock.patch.object(generate_daily, "gemini", return_value=("ملاحظات [1] [2]\n## خريطة الادعاءات\n- ادعاء [3]", [])):
            research, sources = generate_daily.research_from_search_providers(TOPIC, "prompt")
        for item in results:
            self.assertIn(item["url"], research)
        self.assertEqual([source["url"] for source in sources], [item["url"] for item in results])

    def test_research_uses_search_providers_when_grounding_is_exhausted(self):
        notes = "ملاحظات https://a.org/1 https://b.org/2 https://c.org/3\n## خريطة الادعاءات"
        sources = [{"title": "t", "url": f"https://{d}/x"} for d in ("a.org", "b.org", "c.org")]
        with mock.patch.object(generate_daily, "production_rules", return_value=""), \
             mock.patch.object(generate_daily, "gemini", side_effect=RuntimeError("GROUNDED_RESEARCH_UNAVAILABLE: Gemini HTTP 429")), \
             mock.patch.object(generate_daily, "research_from_search_providers", return_value=(notes, sources)) as fallback:
            research, valid = generate_daily.research_topic(TOPIC)
        fallback.assert_called_once()
        self.assertEqual(research, notes)
        self.assertEqual(len(valid), 3)


if __name__ == "__main__":
    unittest.main()
