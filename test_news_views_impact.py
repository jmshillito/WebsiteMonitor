from __future__ import annotations

from datetime import date
import sys
import types
import unittest
from unittest import mock

import news_views_impact as nvi


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"unexpected status {self.status_code}")

    def json(self):
        return self._payload


class NewsViewsImpactTests(unittest.TestCase):
    def test_fetch_headline_news_posts_pages_and_filters(self):
        calls: list[dict[str, object]] = []
        responses = iter(
            [
                FakeResponse(
                    200,
                    [
                        {
                            "date": "2026-05-02T10:30:00",
                            "title": {"rendered": "WEPS wins"},
                            "link": "https://schools.edu.ky/weps/weps-wins/",
                            "slug": "weps-wins",
                        },
                        {
                            "date": "2026-04-30T10:30:00",
                            "title": {"rendered": "Outside range"},
                            "link": "https://schools.edu.ky/news/outside-range/",
                            "slug": "outside-range",
                        },
                    ],
                ),
                FakeResponse(400, []),
            ]
        )

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
            return next(responses)

        fake_requests = types.SimpleNamespace(get=fake_get)
        with mock.patch.dict(sys.modules, {"requests": fake_requests}):
            rows = nvi.fetch_headline_news_posts()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], date(2026, 5, 2))
        self.assertEqual(rows[0]["school"], "WEPS")
        self.assertEqual(rows[0]["title"], "WEPS wins")
        self.assertEqual(rows[0]["url"], "https://schools.edu.ky/weps/weps-wins/")
        self.assertEqual(rows[0]["slug"], "weps-wins")
        self.assertEqual(rows[0]["category"], "Headline News")
        self.assertEqual(rows[1]["school"], "")
        self.assertEqual(calls[0]["params"]["categories"], 13)
        self.assertEqual(calls[0]["params"]["per_page"], 100)

        filtered = nvi.filter_news_posts_by_date(rows, date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["date"], date(2026, 5, 2))

    def test_build_news_posts_sheet_has_expected_columns(self):
        table = nvi.build_news_posts_sheet(
            [
                {
                    "date": date(2026, 5, 2),
                    "school": "WEPS",
                    "title": "WEPS wins",
                    "url": "https://schools.edu.ky/weps/weps-wins/",
                    "slug": "weps-wins",
                    "category": "Headline News",
                }
            ]
        )

        self.assertEqual(table[0], ["date", "school", "title", "url", "slug", "category"])
        self.assertEqual(table[1][0], "2026-05-02")
        self.assertEqual(table[1][1], "WEPS")


if __name__ == "__main__":
    unittest.main()
