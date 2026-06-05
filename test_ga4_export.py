from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
import tempfile
from unittest import mock

google_module = types.ModuleType("google")
api_core_module = types.ModuleType("google.api_core")
exceptions_module = types.ModuleType("google.api_core.exceptions")


class _GoogleAPIError(Exception):
    pass


exceptions_module.GoogleAPIError = _GoogleAPIError
api_core_module.exceptions = exceptions_module
google_module.api_core = api_core_module

analytics_module = types.ModuleType("google.analytics")
data_module = types.ModuleType("google.analytics.data_v1beta")
types_module = types.ModuleType("google.analytics.data_v1beta.types")
data_module.BetaAnalyticsDataClient = object
types_module.DateRange = object
types_module.Dimension = object
types_module.Metric = object
types_module.RunReportRequest = object
analytics_module.data_v1beta = data_module
data_module.types = types_module
google_module.analytics = analytics_module

oauth_module = types.ModuleType("google.oauth2")
service_account_module = types.ModuleType("google.oauth2.service_account")
credentials_module = types.ModuleType("google.oauth2.credentials")
transport_module = types.ModuleType("google.auth.transport.requests")
transport_module.Request = object


class _Credentials:
    @classmethod
    def from_service_account_file(cls, path, scopes=None):
        instance = cls()
        instance.path = Path(path)
        instance.scopes = scopes
        return instance


service_account_module.Credentials = _Credentials
credentials_module.Credentials = _Credentials
oauth_module.service_account = service_account_module
oauth_module.credentials = credentials_module

googleapiclient_module = types.ModuleType("googleapiclient")
discovery_module = types.ModuleType("googleapiclient.discovery")
errors_module = types.ModuleType("googleapiclient.errors")
discovery_module.build = lambda *args, **kwargs: object()


class _HttpError(Exception):
    pass


errors_module.HttpError = _HttpError
googleapiclient_module.discovery = discovery_module
googleapiclient_module.errors = errors_module

authlib_module = types.ModuleType("google_auth_oauthlib")
flow_module = types.ModuleType("google_auth_oauthlib.flow")
flow_module.InstalledAppFlow = object
authlib_module.flow = flow_module

sys.modules.update(
    {
        "google": google_module,
        "google.api_core": api_core_module,
        "google.api_core.exceptions": exceptions_module,
        "google.analytics": analytics_module,
        "google.analytics.data_v1beta": data_module,
        "google.analytics.data_v1beta.types": types_module,
        "google.oauth2": oauth_module,
        "google.oauth2.service_account": service_account_module,
        "google.oauth2.credentials": credentials_module,
        "google.auth.transport.requests": transport_module,
        "googleapiclient": googleapiclient_module,
        "googleapiclient.discovery": discovery_module,
        "googleapiclient.errors": errors_module,
        "google_auth_oauthlib": authlib_module,
        "google_auth_oauthlib.flow": flow_module,
    }
)

import test_ga4 as exporter


class SearchConsoleFallbackTests(unittest.TestCase):
    def test_uses_exact_property_without_wrong_aliases(self) -> None:
        calls: list[str] = []

        def fake_iter_search_console_rows_for_type(client, site_url, start_date, end_date, search_type, page_filter=None, dimensions=None):
            calls.append(site_url)
            return iter(
                [
                    {
                        "date": "2026-05-01",
                        "clicks": "7",
                        "impressions": "70",
                        "ctr": "0.1",
                        "position": "3.2",
                    }
                ]
            )

        with mock.patch.object(exporter, "iter_search_console_rows_for_type", side_effect=fake_iter_search_console_rows_for_type):
            rows = list(
                exporter.iter_search_console_rows_with_fallback(
                    client=object(),
                    school="EMPS",
                    site_url="https://schools.edu.ky/emmps/",
                    start_date="2026-05-01",
                    end_date="2026-05-31",
                )
            )

        self.assertEqual(calls, ["https://schools.edu.ky/emmps/"])
        self.assertEqual(rows, [{"date": "2026-05-01", "clicks": "7", "impressions": "70", "ctr": "0.1", "position": "3.2"}])

    def test_search_console_uses_oauth_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client_secret_file = Path(tmpdir) / "client_secret.json"
            client_secret_file.write_text("{}", encoding="utf-8")
            token_file = Path(tmpdir) / "gsc_oauth_token.json"
            token_file.write_text("{}", encoding="utf-8")

            with mock.patch.object(exporter, "load_oauth_credentials", return_value=_Credentials()) as mocked:
                creds = exporter.load_search_console_credentials(client_secret_file, token_file)

        self.assertIsInstance(creds, _Credentials)
        mocked.assert_called_once_with(client_secret_file, token_file, None)

    def test_site_candidates_include_trailing_slash_variants(self) -> None:
        self.assertEqual(
            exporter.iter_search_console_site_candidates("https://schools.edu.ky/chhs", "CHHS"),
            ["https://schools.edu.ky/chhs", "https://schools.edu.ky/chhs/"],
        )

    def test_page_filter_is_derived_from_site_url_path(self) -> None:
        self.assertEqual(exporter.derive_page_filter("https://schools.edu.ky/chhs/"), "/chhs")
        self.assertEqual(exporter.derive_page_filter("https://schools.edu.ky/"), None)

    def test_root_fallback_with_page_filter_is_used_when_school_property_is_empty(self) -> None:
        calls: list[tuple[str, str, tuple[str, ...] | None]] = []

        def fake_iter_search_console_rows_for_type(client, site_url, start_date, end_date, search_type, page_filter=None, dimensions=None):
            calls.append((site_url, search_type, tuple(dimensions) if dimensions else None))
            if site_url == "https://schools.edu.ky/chhs/" and search_type == "web":
                return iter(
                    [
                        {
                            "date": "2026-05-01",
                            "clicks": "0",
                            "impressions": "10",
                            "ctr": "0",
                            "position": "12.0",
                        }
                    ]
                )
            if site_url == "https://schools.edu.ky/" and search_type == "discover":
                return iter(
                    [
                        {
                            "date": "2026-05-01",
                            "page": "https://schools.edu.ky/CHHS/welcome",
                            "clicks": "249",
                            "impressions": "1000",
                            "ctr": "0.249",
                            "position": "1.0",
                        }
                    ]
                )
            return iter([])

        with mock.patch.object(exporter, "iter_search_console_rows_for_type", side_effect=fake_iter_search_console_rows_for_type):
            rows = list(
                exporter.iter_search_console_rows_with_fallback(
                    client=object(),
                    school="CHHS",
                    site_url="https://schools.edu.ky/chhs/",
                    start_date="2026-05-01",
                    end_date="2026-05-31",
                )
            )

        self.assertEqual(
            calls[0],
            ("https://schools.edu.ky/chhs/", "web", None),
        )
        self.assertIn(("https://schools.edu.ky/", "discover", ("date", "page")), calls)
        self.assertEqual(rows, [{"date": "2026-05-01", "clicks": 249, "impressions": 1000, "ctr": "", "position": ""}])


if __name__ == "__main__":
    unittest.main()
