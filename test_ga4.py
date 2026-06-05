#!/usr/bin/env python3
"""Export GA4 views and Search Console clicks by school into CSV."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
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
DEFAULT_SERVICE_ACCOUNT_FILE = DEFAULT_KEYS_DIR / "service-account.json"
DEFAULT_PROPERTY_MAP = Path("ga4-reporting/property_ids.tsv")
SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
]
AUTH_MODES = {"oauth", "service-account"}
SEARCH_CONSOLE_SITE_ALIASES = {
    "EMPS": ["https://schools.edu.ky/emmps/", "https://schools.edu.ky/emps/"],
    "LHSS": ["https://schools.edu.ky/lhs/", "https://schools.edu.ky/lshs/"],
    "SBPS": ["https://schools.edu.ky/csbs/", "https://schools.edu.ky/sbps/"],
    "PPPS": ["https://schools.edu.ky/pps/", "https://schools.edu.ky/ppps/"],
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
    service_account_file: Path
    auth_mode: str
    auth_code: str | None


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
        service_account_file=parsed.service_account_file,
        auth_mode=parsed.auth_mode,
        auth_code=parsed.auth_code,
    )


def load_oauth_credentials(client_secret_path: Path, token_file_path: Path, auth_code: str | None = None) -> Credentials:
    if not client_secret_path.exists():
        raise FileNotFoundError(f"OAuth client secret file not found: {client_secret_path}")

    creds = None
    if token_file_path.exists():
        creds = Credentials.from_authorized_user_file(token_file_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
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


def load_search_console_client(credentials: Credentials):
    return build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


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
) -> Iterable[dict[str, str]]:
    if not site_url:
        return []

    request_body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["date"],
        "rowLimit": 25000,
    }
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
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": row.get("ctr", 0),
            "position": row.get("position", 0),
        }


def iter_search_console_rows_with_fallback(
    client,
    school: str,
    site_url: str | None,
    start_date: str,
    end_date: str,
) -> Iterable[dict[str, str]]:
    candidates: list[str] = []
    if site_url:
        candidates.append(site_url)
    candidates.extend(SEARCH_CONSOLE_SITE_ALIASES.get(school, []))

    seen: set[str] = set()
    last_exc: Exception | None = None

    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            yield from iter_search_console_rows(client, normalized, start_date, end_date)
            return
        except (GoogleAPIError, HttpError) as exc:
            last_exc = exc
            continue

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
    credentials = load_credentials(
        args.client_secret,
        args.token_file,
        args.service_account_file,
        args.auth_mode,
        args.auth_code,
    )
    ga4_client = load_ga4_client(credentials)
    search_console_client = load_search_console_client(credentials)
    property_map = load_property_map(args.property_map)
    rows = build_daily_rows(ga4_client, search_console_client, property_map, args.start_date, args.end_date)
    write_csv(args.output, rows)
    write_archive_copy(args.output, rows, args.start_date, args.end_date)
    print(json.dumps({"output": str(args.output), "rows": len(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
