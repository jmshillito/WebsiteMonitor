#!/usr/bin/env python3
"""Export GA4 views and Search Console clicks by school into CSV."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Iterable

from google.api_core.exceptions import GoogleAPIError
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
from googleapiclient.errors import HttpError
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow


DEFAULT_OUTPUT = Path("imports/ga4_daily.csv")
DEFAULT_KEYS_DIR = Path("ga4-reporting/keys")
DEFAULT_CLIENT_SECRET = DEFAULT_KEYS_DIR / "client_secret_463400512765-k9faff977lqifvpiqt69263r8c9dnr6e.apps.googleusercontent.com.json"
DEFAULT_TOKEN_FILE = DEFAULT_KEYS_DIR / "oauth_token.json"
DEFAULT_GSC_TOKEN_FILE = DEFAULT_KEYS_DIR / "gsc_oauth_token.json"
DEFAULT_SERVICE_ACCOUNT_FILE = DEFAULT_KEYS_DIR / "service-account.json"
DEFAULT_PROPERTY_MAP = Path("ga4-reporting/property_ids.tsv")
SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
]
AUTH_MODES = {"oauth", "service-account"}
DEBUG_GSC = os.getenv("GA4_DEBUG_GSC", "0") == "1"
IGNORE_GSC_DATE_RANGE = os.getenv("GA4_IGNORE_GSC_DATE_RANGE", "1") == "1"
SEARCH_CONSOLE_TYPES = ["web", "discover", "googleNews", "news", "image", "video"]
SEARCH_CONSOLE_SITE_ALIASES = {
    "EMPS": ["https://schools.edu.ky/emmps/"],
    "LHSS": ["https://schools.edu.ky/lhs/", "https://schools.edu.ky/lshs/"],
    "SBPS": ["https://schools.edu.ky/csbs/"],
    "PPPS": ["https://schools.edu.ky/pps/"],
}


@dataclass(frozen=True)
class SchoolProperties:
    school: str
    ga4_property_id: str
    search_console_property: str | None


@dataclass(frozen=True)
class Args:
    property_map: Path
    start_date: str
    end_date: str
    output: Path
    client_secret: Path
    token_file: Path
    gsc_token_file: Path
    service_account_file: Path
    auth_mode: str
    auth_code: str | None
    gsc_only: bool
    gsc_site_url: str | None
    gsc_search_type: str


def parse_args() -> Args:
    parser = argparse.ArgumentParser(description="Export GA4 and Search Console daily metrics into CSV.")
    parser.add_argument(
        "--property-map",
        type=Path,
        default=DEFAULT_PROPERTY_MAP,
        help="TSV or CSV file with school, ga4_property_id, and search_console_property columns.",
    )
    parser.add_argument("--start-date", default=None, help="Start date YYYY-MM-DD. Defaults to first day of month.")
    parser.add_argument("--end-date", default=None, help="End date YYYY-MM-DD. Defaults to today.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="CSV output path. Defaults to imports/ga4_daily.csv",
    )
    parser.add_argument(
        "--client-secret",
        type=Path,
        default=DEFAULT_CLIENT_SECRET,
        help="OAuth client secret JSON path.",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=DEFAULT_TOKEN_FILE,
        help="OAuth token cache JSON path.",
    )
    parser.add_argument(
        "--gsc-token-file",
        type=Path,
        default=DEFAULT_GSC_TOKEN_FILE,
        help="OAuth token cache JSON path used for Search Console.",
    )
    parser.add_argument(
        "--service-account-file",
        type=Path,
        default=DEFAULT_SERVICE_ACCOUNT_FILE,
        help="Service account JSON path used for silent authentication.",
    )
    parser.add_argument(
        "--auth-mode",
        choices=sorted(AUTH_MODES),
        default="service-account",
        help="Authentication mode. service-account is silent; oauth can prompt for authorization.",
    )
    parser.add_argument(
        "--auth-code",
        type=str,
        default=None,
        help="Authorization code returned by the Google consent screen.",
    )
    parser.add_argument(
        "--gsc-only",
        action="store_true",
        help="Only probe Search Console clicks for a single property and exit.",
    )
    parser.add_argument(
        "--gsc-site-url",
        type=str,
        default=None,
        help="Search Console site URL for --gsc-only mode.",
    )
    parser.add_argument(
        "--gsc-search-type",
        type=str,
        default="web",
        help="Search Console search type for --gsc-only mode.",
    )

    parsed = parser.parse_args()
    today = date.today()
    month_start = today.replace(day=1)
    return Args(
        property_map=parsed.property_map,
        start_date=parsed.start_date or month_start.isoformat(),
        end_date=parsed.end_date or today.isoformat(),
        output=parsed.output,
        client_secret=parsed.client_secret,
        token_file=parsed.token_file,
        gsc_token_file=parsed.gsc_token_file,
        service_account_file=parsed.service_account_file,
        auth_mode=parsed.auth_mode,
        auth_code=parsed.auth_code,
        gsc_only=parsed.gsc_only,
        gsc_site_url=parsed.gsc_site_url,
        gsc_search_type=parsed.gsc_search_type,
    )


def load_oauth_credentials(client_secret_path: Path, token_file_path: Path, auth_code: str | None = None) -> Credentials:
    creds = None
    if token_file_path.exists():
        creds = Credentials.from_authorized_user_file(token_file_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_secret_path.exists():
                raise FileNotFoundError(f"OAuth client secret file not found: {client_secret_path}")
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            flow.redirect_uri = "http://localhost"
            auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
            print("Open this URL in a browser and sign in with the reporting account:", file=sys.stderr)
            print(auth_url, file=sys.stderr)
            if not auth_code:
                if not sys.stdin.isatty():
                    raise ValueError(
                        "missing authorization code; rerun with --auth-code or set GA4_AUTH_CODE after opening the URL above"
                    )
                try:
                    auth_code = input("Paste the authorization code here: ")
                except EOFError as exc:
                    raise ValueError("missing authorization code") from exc
            code = auth_code.strip()
            if not code:
                raise ValueError("missing authorization code")
            flow.fetch_token(code=code)
            creds = flow.credentials

        token_file_path.parent.mkdir(parents=True, exist_ok=True)
        token_file_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def load_service_account_credentials(service_account_path: Path) -> Credentials:
    if not service_account_path.exists():
        raise FileNotFoundError(f"Service account file not found: {service_account_path}")
    return service_account.Credentials.from_service_account_file(service_account_path, scopes=SCOPES)


def resolve_auth_credentials_path(
    auth_mode: str,
    client_secret_path: Path,
    service_account_path: Path,
) -> Path:
    if auth_mode == "service-account":
        return service_account_path
    if auth_mode == "oauth":
        return client_secret_path
    raise ValueError(f"unsupported auth mode: {auth_mode}")


def load_credentials(
    client_secret_path: Path,
    token_file_path: Path,
    service_account_path: Path,
    auth_mode: str,
    auth_code: str | None = None,
) -> Credentials:
    credential_path = resolve_auth_credentials_path(auth_mode, client_secret_path, service_account_path)
    print(f"Using GA4 auth mode: {auth_mode}", file=sys.stderr)
    print(f"Using GA4 credentials: {credential_path}", file=sys.stderr)

    if auth_mode == "service-account":
        return load_service_account_credentials(credential_path)
    if auth_mode == "oauth":
        return load_oauth_credentials(credential_path, token_file_path, auth_code)
    raise ValueError(f"unsupported auth mode: {auth_mode}")


def load_property_map(path: Path) -> list[SchoolProperties]:
    if not path.exists():
        raise FileNotFoundError(f"Property map file not found: {path}")

    with path.open("r", encoding="utf-8", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
        except csv.Error:
            dialect = csv.excel_tab
        reader = csv.DictReader(f, dialect=dialect)
        field_map = {str(field or "").strip().lower().replace(" ", "_"): field for field in (reader.fieldnames or [])}

        def pick(*names: str) -> str | None:
            for name in names:
                if name in field_map:
                    return field_map[name]
            return None

        school_key = pick("school")
        ga4_key = pick("ga4_property_id", "ga4_property", "ga4_id", "property_id")
        sc_key = pick(
            "search_console_property",
            "search_console_site_url",
            "search_console_site",
            "gsc_property",
            "site_url",
            "site",
        )
        if not school_key or not ga4_key:
            raise ValueError(
                f"{path} must contain at least school and ga4_property_id columns; found {reader.fieldnames}"
            )

        rows: list[SchoolProperties] = []
        for row in reader:
            school = str(row.get(school_key, "")).strip().upper()
            ga4_property_id = str(row.get(ga4_key, "")).strip()
            search_console_property = str(row.get(sc_key, "")).strip() if sc_key else ""
            if not school or not ga4_property_id:
                continue
            rows.append(
                SchoolProperties(
                    school=school,
                    ga4_property_id=ga4_property_id,
                    search_console_property=search_console_property or None,
                )
            )

    if not rows:
        raise ValueError(f"No valid school/property rows found in {path}")
    return rows


def load_ga4_client(credentials: Credentials) -> BetaAnalyticsDataClient:
    return BetaAnalyticsDataClient(credentials=credentials)


def load_search_console_credentials(
    client_secret_path: Path,
    token_file_path: Path,
    auth_code: str | None = None,
) -> Credentials:
    credential_path = client_secret_path
    print(f"Using GSC OAuth credentials: {credential_path}", file=sys.stderr)
    return load_oauth_credentials(client_secret_path, token_file_path, auth_code)


def load_search_console_client(credentials: Credentials):
    return build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


def load_search_console_sites(client) -> set[str]:
    try:
        sites_resource = client.sites()
    except Exception:
        return set()

    try:
        response = sites_resource.list().execute()
    except Exception as exc:
        print(f"warning: unable to list Search Console sites: {exc}", file=sys.stderr)
        return set()

    sites: set[str] = set()
    for entry in response.get("siteEntry", []):
        site_url = str(entry.get("siteUrl", "")).strip()
        if site_url:
            sites.add(site_url)
    if sites:
        print(f"Search Console accessible sites: {sorted(sites)}", file=sys.stderr)
    return sites


def debug_print_gsc_rows(label: str, site_url: str, rows: list[dict[str, str]]) -> None:
    if not DEBUG_GSC:
        return
    print(f"GSC DEBUG {label} siteUrl={site_url} rows={len(rows)}", file=sys.stderr)
    for row in rows[:10]:
        print(f"GSC DEBUG {label} row={row}", file=sys.stderr)


def iter_search_console_rows_for_type(
    client,
    site_url: str | None,
    start_date: str,
    end_date: str,
    search_type: str,
    page_filter: str | None = None,
    dimensions: list[str] | None = None,
) -> Iterable[dict[str, str]]:
    if not site_url:
        return []

    if IGNORE_GSC_DATE_RANGE:
        start_date = "2000-01-01"
        end_date = date.today().isoformat()

    request_body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions or ["date"],
        "rowLimit": 25000,
        "type": search_type,
    }
    if page_filter:
        request_body["dimensionFilterGroups"] = [
            {
                "filters": [
                    {
                        "dimension": "page",
                        "operator": "contains",
                        "expression": page_filter,
                    }
                ]
            }
        ]
    response = client.searchanalytics().query(siteUrl=site_url, body=request_body).execute()
    for row in response.get("rows", []):
        keys = row.get("keys", [])
        if not keys:
            continue
        try:
            day = datetime.strptime(keys[0], "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        yield {
            "date": day,
            "page": row.get("keys", [None, None])[1] if len(row.get("keys", [])) > 1 else "",
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": row.get("ctr", 0),
            "position": row.get("position", 0),
        }


def read_search_console_clicks_only(
    client,
    site_url: str,
    start_date: str | None = None,
    end_date: str | None = None,
    search_type: str = "web",
) -> list[dict[str, str]]:
    if start_date is None or end_date is None:
        start_date = "2000-01-01"
        end_date = date.today().isoformat()
    rows = list(
        iter_search_console_rows_for_type(
            client,
            site_url,
            start_date,
            end_date,
            search_type=search_type,
        )
    )
    total_clicks = sum(float(row.get("clicks", 0) or 0) for row in rows)
    print(
        json.dumps(
            {
                "site_url": site_url,
                "search_type": search_type,
                "start_date": start_date,
                "end_date": end_date,
                "rows": len(rows),
                "clicks": total_clicks,
            }
        ),
        file=sys.stderr,
    )
    return rows


def iter_search_console_site_candidates(site_url: str | None, school: str) -> list[str]:
    candidates: list[str] = []
    if site_url:
        normalized = site_url.strip()
        if normalized:
            candidates.append(normalized)
            if normalized.endswith("/"):
                trimmed = normalized.rstrip("/")
                if trimmed:
                    candidates.append(trimmed)
            else:
                candidates.append(f"{normalized}/")
    candidates.extend(SEARCH_CONSOLE_SITE_ALIASES.get(school, []))
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def derive_page_filter(site_url: str | None) -> str | None:
    if not site_url:
        return None
    parsed = urlparse(site_url.strip())
    path = parsed.path.strip()
    if not path or path == "/":
        return None
    return path.lower().rstrip("/")


def build_page_matchers(site_url: str | None, school: str) -> list[str]:
    matchers: list[str] = []
    page_filter = derive_page_filter(site_url)
    if page_filter:
        base = page_filter.lstrip("/")
        variants = {
            page_filter,
            f"/{base.lower()}",
            f"/{base.upper()}",
        }
        matchers.extend(sorted(variants))

    school_aliases = {
        "CHHS": ["chhs"],
        "EMPS": ["emmps", "emps"],
        "PPPS": ["pps", "ppps"],
        "SBPS": ["csbs", "sbps"],
        "LHSS": ["lhs", "lshs"],
    }.get(school, [])
    for alias in school_aliases:
        for candidate in (alias.lower(), alias.upper()):
            matcher = f"/{candidate}"
            if matcher not in matchers:
                matchers.append(matcher)
    return matchers


def iter_ga4_rows(
    client: BetaAnalyticsDataClient,
    property_id: str,
    start_date: str,
    end_date: str,
) -> Iterable[dict[str, str]]:
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="screenPageViews"), Metric(name="totalUsers")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        limit=100000,
    )
    response = client.run_report(request)

    for row in response.rows:
        values = [value.value for value in row.dimension_values]
        metrics = [value.value for value in row.metric_values]
        if not values:
            continue
        try:
            day = datetime.strptime(values[0], "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        yield {
            "date": day,
            "views": metrics[0] if metrics else "0",
            "users": metrics[1] if len(metrics) > 1 else "0",
        }


def iter_search_console_rows(
    client,
    site_url: str | None,
    start_date: str,
    end_date: str,
    page_filter: str | None = None,
    dimensions: list[str] | None = None,
) -> Iterable[dict[str, str]]:
    yield from iter_search_console_rows_for_type(
        client,
        site_url,
        start_date,
        end_date,
        search_type="web",
        page_filter=page_filter,
        dimensions=dimensions,
    )


def iter_search_console_rows_with_fallback(
    client,
    school: str,
    site_url: str | None,
    start_date: str,
    end_date: str,
) -> Iterable[dict[str, str]]:
    last_exc: Exception | None = None
    page_filter = derive_page_filter(site_url)
    accessible_sites = load_search_console_sites(client)

    for candidate in iter_search_console_site_candidates(site_url, school):
        for search_type in SEARCH_CONSOLE_TYPES:
            try:
                rows = list(iter_search_console_rows_for_type(client, candidate, start_date, end_date, search_type))
            except (GoogleAPIError, HttpError) as exc:
                last_exc = exc
                continue
            debug_print_gsc_rows(f"{school} candidate type={search_type}", candidate, rows)
            if rows:
                total_clicks = sum(float(row.get("clicks", 0) or 0) for row in rows)
                if total_clicks > 0 or not page_filter:
                    yield from rows
                    return
                print(
                    f"warning: GSC candidate {candidate} type={search_type} returned rows but zero clicks for {school}; trying fallback",
                    file=sys.stderr,
                )
                continue

    if page_filter:
        root_candidates = [
            candidate
            for candidate in ("sc-domain:schools.edu.ky", "https://schools.edu.ky/")
            if not accessible_sites or candidate in accessible_sites
        ]
        if not root_candidates:
            root_candidates = ["https://schools.edu.ky/"]

        for root_site_url in root_candidates:
            for search_type in SEARCH_CONSOLE_TYPES:
                try:
                    rows = list(
                        iter_search_console_rows_for_type(
                            client,
                            root_site_url,
                            start_date,
                            end_date,
                            search_type,
                            dimensions=["date", "page"],
                        )
                    )
                except (GoogleAPIError, HttpError) as exc:
                    last_exc = exc
                    continue
                debug_print_gsc_rows(f"{school} root type={search_type}", root_site_url, rows)

                matchers = build_page_matchers(site_url, school)
                filtered_rows = [
                    row
                    for row in rows
                    if any(matcher.lower() in str(row.get("page", "")).lower() for matcher in matchers)
                ]
                if filtered_rows:
                    print(
                        f"Using GSC root fallback for {school}: siteUrl={root_site_url} type={search_type} matched pages {matchers}",
                        file=sys.stderr,
                    )
                    if DEBUG_GSC:
                        print(f"GSC DEBUG {school} matchers={matchers}", file=sys.stderr)
                    by_day: dict[str, dict[str, float | int | str]] = defaultdict(
                        lambda: {"date": "", "clicks": 0, "impressions": 0}
                    )
                    for row in filtered_rows:
                        day = str(row.get("date", "")).strip()
                        if not day:
                            continue
                        bucket = by_day[day]
                        bucket["date"] = day
                        bucket["clicks"] = int(float(bucket["clicks"])) + int(float(row.get("clicks", 0) or 0))
                        bucket["impressions"] = int(float(bucket["impressions"])) + int(float(row.get("impressions", 0) or 0))
                    for row in sorted(by_day.values(), key=lambda item: item["date"]):
                        yield {
                            "date": row["date"],
                            "clicks": row["clicks"],
                            "impressions": row["impressions"],
                            "ctr": "",
                            "position": "",
                        }
                    return

    if last_exc is not None:
        raise last_exc


def build_daily_rows(
    ga4_client: BetaAnalyticsDataClient,
    search_console_client,
    property_map: list[SchoolProperties],
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    daily: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {"views": 0, "users": 0, "clicks": 0, "impressions": 0, "ctr": None, "position": None}
    )

    for school_properties in property_map:
        try:
            for row in iter_ga4_rows(ga4_client, school_properties.ga4_property_id, start_date, end_date):
                try:
                    views = int(float(row["views"]))
                except ValueError:
                    continue
                daily[(school_properties.school, row["date"])]["views"] += views
                try:
                    users = int(float(row.get("users", 0)))
                except ValueError:
                    users = 0
                daily[(school_properties.school, row["date"])]["users"] += users
        except GoogleAPIError as exc:
            print(
                f"warning: skipping GA4 property {school_properties.ga4_property_id} for "
                f"{school_properties.school}: {exc.message if hasattr(exc, 'message') else exc}",
                file=sys.stderr,
            )

        try:
            for row in iter_search_console_rows_with_fallback(
                search_console_client,
                school_properties.school,
                school_properties.search_console_property,
                start_date,
                end_date,
            ):
                key = (school_properties.school, row["date"])
                daily[key]["clicks"] += int(float(row.get("clicks", 0) or 0))
                daily[key]["impressions"] += int(float(row.get("impressions", 0) or 0))
                daily[key]["ctr"] = float(row.get("ctr", 0) or 0)
                daily[key]["position"] = float(row.get("position", 0) or 0)
        except (GoogleAPIError, HttpError) as exc:
            print(
                f"warning: skipping Search Console property {school_properties.search_console_property} "
                f"for {school_properties.school}: {exc.message if hasattr(exc, 'message') else exc}",
                file=sys.stderr,
            )

    output_rows = []
    for (school, date_value), values in sorted(daily.items()):
        output_rows.append(
            {
                "school": school,
                "date": date_value,
                "views": values["views"],
                "users": values["users"],
                "clicks": values["clicks"],
                "impressions": values["impressions"],
                "ctr": values["ctr"] if values["ctr"] is not None else "",
                "position": values["position"] if values["position"] is not None else "",
            }
        )
    return output_rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["school", "date", "views", "users", "clicks", "impressions", "ctr", "position"],
        )
        writer.writeheader()
        writer.writerows(rows)


def write_archive_copy(path: Path, rows: list[dict[str, object]], start_date: str, end_date: str) -> None:
    stamp = f"{start_date}_to_{end_date}"
    archive_path = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
    write_csv(archive_path, rows)


def main() -> int:
    args = parse_args()
    if getattr(args, "gsc_only", False):
        if not args.gsc_site_url:
            raise ValueError("--gsc-site-url is required with --gsc-only")
        search_console_credentials = load_search_console_credentials(
            args.client_secret,
            args.gsc_token_file,
            args.auth_code,
        )
        search_console_client = load_search_console_client(search_console_credentials)
        rows = read_search_console_clicks_only(
            search_console_client,
            args.gsc_site_url,
            None,
            None,
            args.gsc_search_type,
        )
        print(json.dumps(rows, indent=2))
        return 0
    credentials = load_credentials(
        args.client_secret,
        args.token_file,
        args.service_account_file,
        args.auth_mode,
        args.auth_code,
    )
    ga4_client = load_ga4_client(credentials)
    search_console_credentials = load_search_console_credentials(
        args.client_secret,
        args.gsc_token_file,
        args.auth_code,
    )
    search_console_client = load_search_console_client(search_console_credentials)
    property_map = load_property_map(args.property_map)
    rows = build_daily_rows(ga4_client, search_console_client, property_map, args.start_date, args.end_date)
    write_csv(args.output, rows)
    write_archive_copy(args.output, rows, args.start_date, args.end_date)
    print(json.dumps({"output": str(args.output), "rows": len(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
