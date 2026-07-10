"""Regression tests for search relevance filters (crime + national domains).

Fixture headlines/URLs are Peachtree-market (Georgia) data; the geography-gate
tests are skipped under COMPANY=tc. Published dates are generated relative to
now so the recency gate never goes stale.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from blog_automation.company import get_company_slug, get_profile
from blog_automation.pipeline.search import (
    DEFAULT_MAX_AGE_DAYS,
    _collect_matched_terms,
    _relevance_failure_reason,
)

_INSURANCE_CLUSTER_KEY = get_profile().INSURANCE_CLUSTER_KEY


def _recent_date(days_ago: int = 3) -> str:
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))


ARSON_SOURCE = {
    "title": (
        "Georgia Commissioner's Office Offers $10,000 Reward for Help on String of Arsons "
        "- Insurance Journal"
    ),
    "url": "https://www.insurancejournal.com/news/southeast/2026/06/05/872664.htm",
    "content": (
        "The Georgia Insurance Commissioner's office is offering a reward for information "
        "about a vehicle fire in Cedartown. Investigators determined the fire had been "
        "deliberately set."
    ),
    "published_date": _recent_date(),
    "strategy_cluster": _INSURANCE_CLUSTER_KEY,
}

FOX_NATIONAL_SOURCE = {
    "title": (
        "Late-spring freeze devastates Northeast farms, threatening peach and apple crops "
        "- Fox News"
    ),
    "url": "https://www.foxnews.com/weather/late-april-freeze-new-jersey-farms-crop-losses",
    "content": (
        "#### Tornado Alley shifts to the Southeast, storm shelters become more popular\n"
        "#### Hurricane Milton forces St. Petersburg crane collapse\n"
        "#### Southeast met with dangerous flooding while Northeast braces for snowstorms"
    ),
    "published_date": _recent_date(),
    "strategy_cluster": "county_guides",
}

VALID_GEORGIA_INSURANCE_SOURCE = {
    "title": "Georgia roof insurance claim disputes rise after spring hail storms - Insurance Journal",
    "url": "https://www.insurancejournal.com/news/southeast/2026/06/10/872900.htm",
    "content": (
        "Metro Atlanta homeowners are filing more roof insurance claims after recent hail "
        "damage across Gwinnett and Cobb counties."
    ),
    "published_date": _recent_date(),
    "strategy_cluster": _INSURANCE_CLUSTER_KEY,
}

_IS_PEACHTREE = get_company_slug() == "peachtree"


class SearchRelevanceFilterTests(unittest.TestCase):
    def _failure_reason(self, result: dict) -> str | None:
        matched = _collect_matched_terms(result)
        return _relevance_failure_reason(result, matched, DEFAULT_MAX_AGE_DAYS)

    def test_arson_story_rejected_as_off_topic(self) -> None:
        # Off-topic gate fires before geography — valid for every company profile.
        self.assertEqual(self._failure_reason(ARSON_SOURCE), "off_topic_headline")

    @unittest.skipUnless(
        _IS_PEACHTREE,
        "Fixture geography (Southeast/Georgia terms) only matches the Peachtree profile; "
        "under COMPANY=tc the same story is rejected earlier with a different reason.",
    )
    def test_fox_national_story_rejected_without_local_headline(self) -> None:
        self.assertEqual(
            self._failure_reason(FOX_NATIONAL_SOURCE),
            "national_domain_missing_local_headline",
        )

    @unittest.skipUnless(
        _IS_PEACHTREE,
        "Fixture is a Georgia-market story; under COMPANY=tc 'Georgia' is an "
        "out-of-market state term and the source is correctly rejected.",
    )
    def test_valid_georgia_insurance_story_not_rejected_by_national_filter(self) -> None:
        self.assertIsNone(self._failure_reason(VALID_GEORGIA_INSURANCE_SOURCE))


if __name__ == "__main__":
    unittest.main()
