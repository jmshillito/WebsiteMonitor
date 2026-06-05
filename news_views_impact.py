#!/usr/bin/env python3
"""Correlate GA4 daily views with news post activity by school."""

from __future__ import annotations

import argparse
import csv
from email.utils import parsedate_to_datetime
from html import unescape
import re
import sys
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from get_news import collect_rows as collect_school_news_rows


GA4_REQUIRED_COLUMNS = {"school", "date", "views"}
DEFAULT_SERVICE_ACCOUNT_FILE = Path("ga4-reporting/keys/service-account.json")
DEFAULT_VIEWS_SHEET_NAME = "Views and Clicks"
DEFAULT_POST_IMPACT_SHEET_NAME = "Post Impact"
DEFAULT_POST_DETAILS_SHEET_NAME = "Post Details"
DEFAULT_NEWS_POSTS_SHEET_NAME = "News Posts"
HEADLINE_NEWS_CATEGORY_ID = 13
HEADLINE_NEWS_CATEGORY_NAME = "Headline News"
WORDPRESS_API_URL = "https://schools.edu.ky/wp-json/wp/v2/posts"
WORDPRESS_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 WebsiteMonitor/1.0",
    "Accept": "application/json,text/plain,*/*",
}
SGCAPTCHA_RE = re.compile(r"sgcaptcha/\?r=([^\"'<> ]+)", re.I)
GA4_SCHOOL_CODES = [
    "CHHS",
    "CIFEC",
    "EEPS",
    "EMPS",
    "JACPS",
    "JCPS",
    "JGHS",
    "LHSS",
    "LSHS",
    "MMPS",
    "PPPS",
    "RBPS",
    "SBPS",
    "TMPS",
    "WEPS",
]


@dataclass(frozen=True)
class InputPaths:
    ga4: Path
    output_dir: Path
    start_date: str | None
    end_date: str | None
    month: str | None
    spreadsheet_id: str | None
    service_account_file: Path
    views_sheet_name: str
    post_impact_sheet_name: str
    post_details_sheet_name: str
    news_posts_sheet_name: str


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def open_url_in_browser(url: str) -> bool:
    candidates = [
        ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"],
        ["pwsh.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"],
        ["xdg-open", url],
    ]
    for command in candidates:
        try:
            result = subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            continue
        if result.returncode == 0:
            return True
    return False


def extract_sgcaptcha_url(response_text: str, response_url: str) -> str | None:
    match = SGCAPTCHA_RE.search(response_text)
    if not match:
        return None

    target = match.group(1)
    if target.startswith("http://") or target.startswith("https://"):
        return target
    if target.startswith("/"):
        return f"https://schools.edu.ky{target}"
    return f"{response_url.rstrip('/')}/{target.lstrip('/')}"


def parse_args() -> InputPaths:
    parser = argparse.ArgumentParser(
        description="Correlate GA4 daily views with news posts and write Looker Studio friendly CSVs."
    )
    parser.add_argument("--ga4", type=Path, required=True, help="Path to the GA4 daily report CSV.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Directory where the output CSV files will be written.",
    )
    parser.add_argument(
        "--month",
        type=str,
        default=None,
        help="Optional YYYY-MM month filter applied to both inputs before analysis.",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Optional YYYY-MM-DD start date applied to both inputs before analysis.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Optional YYYY-MM-DD end date applied to both inputs before analysis.",
    )
    parser.add_argument(
        "--spreadsheet-id",
        type=str,
        default=None,
        help="Optional Google Sheets spreadsheet ID to upload the report into.",
    )
    parser.add_argument(
        "--service-account-file",
        type=Path,
        default=DEFAULT_SERVICE_ACCOUNT_FILE,
        help="Service account JSON file used to write to Google Sheets.",
    )
    parser.add_argument(
        "--views-sheet-name",
        type=str,
        default=DEFAULT_VIEWS_SHEET_NAME,
        help="Worksheet name for the daily Views and Clicks data.",
    )
    parser.add_argument(
        "--post-impact-sheet-name",
        type=str,
        default=DEFAULT_POST_IMPACT_SHEET_NAME,
        help="Worksheet name for the post impact summary.",
    )
    parser.add_argument(
        "--post-details-sheet-name",
        type=str,
        default=DEFAULT_POST_DETAILS_SHEET_NAME,
        help="Worksheet name for the per-post detail table.",
    )
    parser.add_argument(
        "--news-posts-sheet-name",
        type=str,
        default=DEFAULT_NEWS_POSTS_SHEET_NAME,
        help="Worksheet name for the WordPress Headline News export.",
    )

    args = parser.parse_args()
    spreadsheet_id = None if args.spreadsheet_id is None else str(args.spreadsheet_id).strip() or None
    return InputPaths(
        ga4=args.ga4,
        output_dir=args.output_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        month=args.month,
        spreadsheet_id=spreadsheet_id,
        service_account_file=args.service_account_file,
        views_sheet_name=args.views_sheet_name,
        post_impact_sheet_name=args.post_impact_sheet_name,
        post_details_sheet_name=args.post_details_sheet_name,
        news_posts_sheet_name=args.news_posts_sheet_name,
    )


def parse_iso_date(value: str, label: str) -> date:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} is empty")

    candidates = [
        text,
        text.replace("Z", "+00:00"),
    ]
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    try:
        return parsedate_to_datetime(text).date()
    except Exception:
        pass

    raise ValueError(f"{label} must use a recognizable date format: {value!r}")


def parse_date_range(
    month: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str | None, date | None, date | None]:
    if month is not None:
        try:
            month_start = datetime.strptime(f"{month}-01", "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("--month must use YYYY-MM format") from exc

        if month_start.month == 12:
            month_end = date(month_start.year, 12, 31)
        else:
            month_end = date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)
        return month, month_start, month_end

    if start_date is None and end_date is None:
        return None, None, None
    if start_date is None or end_date is None:
        raise ValueError("--start-date and --end-date must be provided together")

    parsed_start = parse_iso_date(start_date, "--start-date")
    parsed_end = parse_iso_date(end_date, "--end-date")

    if parsed_end < parsed_start:
        raise ValueError("--end-date must be on or after --start-date")

    return f"{parsed_start:%Y-%m-%d}_to_{parsed_end:%Y-%m-%d}", parsed_start, parsed_end


def load_csv(path: Path, label: str) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"{label} file not found: {path}")

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows


def normalize_school(value: Any) -> str:
    return str(value or "").strip().upper()


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_wp_text(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def to_number(value: Any) -> float | int | None:
    text = normalize_text(value)
    if text == "":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number.is_integer():
        return int(number)
    return number


def format_number(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return normalize_text(value)


def format_output_date(value: date | None) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d")


def infer_school_from_headline_news(post: dict[str, Any]) -> str:
    link = normalize_text(post.get("link")).lower()
    slug = normalize_text(post.get("slug")).lower()
    title = normalize_wp_text(post.get("title")).lower()

    alias_map = [
        (("schools.edu.ky/csbs/", "csbs"), "SBPS"),
        (("schools.edu.ky/emmps/", "emmps"), "EMPS"),
        (("schools.edu.ky/lhs/", "lhs"), "LHSS"),
        (("schools.edu.ky/lshs/", "lshs"), "LSHS"),
        (("schools.edu.ky/pps/", "pps"), "PPPS"),
        (("schools.edu.ky/ppps/", "ppps"), "PPPS"),
        (("schools.edu.ky/chhs/", "chhs"), "CHHS"),
        (("schools.edu.ky/cifec/", "cifec"), "CIFEC"),
        (("schools.edu.ky/eeps/", "eeps"), "EEPS"),
        (("schools.edu.ky/jacps/", "jacps"), "JACPS"),
        (("schools.edu.ky/jcps/", "jcps"), "JCPS"),
        (("schools.edu.ky/jghs/", "jghs"), "JGHS"),
        (("schools.edu.ky/mmps/", "mmps"), "MMPS"),
        (("schools.edu.ky/rbps/", "rbps"), "RBPS"),
        (("schools.edu.ky/tmps/", "tmps"), "TMPS"),
        (("schools.edu.ky/weps/", "weps"), "WEPS"),
    ]
    for needles, school in alias_map:
        if any(needle in link or needle in slug or needle in title for needle in needles):
            return school
    return ""


def fetch_headline_news_posts(
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in collect_school_news_rows():
        date_text = normalize_text(row.get("Date"))
        if not date_text:
            continue

        try:
            post_date = parse_iso_date(date_text, "News post date")
        except ValueError:
            continue

        rows.append(
            {
                "date": post_date,
                "school": normalize_school(row.get("School")),
                "title": normalize_wp_text(row.get("Title")),
                "url": normalize_text(row.get("URL")),
                "slug": normalize_text(row.get("Slug")),
                "category": HEADLINE_NEWS_CATEGORY_NAME,
            }
        )

    return rows


def filter_news_posts_by_date(
    news_posts: list[dict[str, Any]],
    month_start: date | None,
    month_end: date | None,
) -> list[dict[str, Any]]:
    if month_start is None or month_end is None:
        return list(news_posts)
    return [row for row in news_posts if isinstance(row.get("date"), date) and month_start <= row["date"] <= month_end]


def safe_mean(values: list[float | int]) -> float | None:
    if not values:
        return None
    return float(mean(values))


def safe_pct_change(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return ((float(numerator) - float(denominator)) / float(denominator)) * 100.0


def prepare_ga4(rows: list[dict[str, str]], month_start: date | None, month_end: date | None) -> list[dict[str, Any]]:
    if not rows:
        return []

    columns = {str(col).strip().lower() for col in rows[0].keys()}
    missing = GA4_REQUIRED_COLUMNS - columns
    if missing:
        raise ValueError(f"GA4 CSV is missing required columns: {sorted(missing)}")

    prepared: list[dict[str, Any]] = []
    for raw in rows:
        row = {str(key).strip().lower(): value for key, value in raw.items()}
        school = normalize_school(row.get("school"))
        date_value = row.get("date")
        views = to_number(row.get("views"))
        if not school or not date_value or views is None:
            continue

        parsed_date = parse_iso_date(date_value, "GA4 date")
        if month_start is not None and month_end is not None and not (month_start <= parsed_date <= month_end):
            continue

        prepared.append(
            {
                "school": school,
                "date": parsed_date,
                "views": views,
                "users": to_number(row.get("users")),
                "clicks": to_number(row.get("clicks")),
                "impressions": to_number(row.get("impressions")),
                "ctr": to_number(row.get("ctr")),
                "position": to_number(row.get("position")),
            }
        )

    if any(row["views"] is None for row in prepared):
        warn("GA4: some views values could not be converted to numbers and were dropped.")

    return prepared


def build_news_daily_summary(news_posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, date], int] = defaultdict(int)
    for row in news_posts:
        if not row.get("school") or not isinstance(row.get("date"), date):
            continue
        counts[(row["school"], row["date"])] += 1

    return [
        {"school": school, "date": post_date, "news_posts_count": count}
        for (school, post_date), count in sorted(counts.items(), key=lambda item: (item[0][0], item[0][1]))
    ]


def add_rolling_post_flags(
    merged: list[dict[str, Any]],
    news_daily: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    post_dates_by_school: dict[str, list[date]] = defaultdict(list)
    for row in news_daily:
        post_dates_by_school[row["school"]].append(row["date"])

    for school in post_dates_by_school:
        post_dates_by_school[school].sort()

    output: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in merged:
        grouped[row["school"]].append(row)

    for school in sorted(grouped):
        rows = sorted(grouped[school], key=lambda row: row["date"])
        post_dates = post_dates_by_school.get(school, [])
        post_index = 0
        current_last_post: date | None = None

        for row in rows:
            while post_index < len(post_dates) and post_dates[post_index] <= row["date"]:
                current_last_post = post_dates[post_index]
                post_index += 1

            enriched = dict(row)
            enriched["last_post_date"] = format_output_date(current_last_post)
            if current_last_post is None:
                enriched["days_since_last_post"] = None
                enriched["within_1_day_post"] = False
                enriched["within_3_days_post"] = False
                enriched["within_7_days_post"] = False
            else:
                delta_days = (row["date"] - current_last_post).days
                enriched["days_since_last_post"] = delta_days
                enriched["within_1_day_post"] = delta_days <= 1
                enriched["within_3_days_post"] = delta_days <= 3
                enriched["within_7_days_post"] = delta_days <= 7

            enriched["is_post_day"] = int(row.get("news_posts_count", 0)) > 0
            output.append(enriched)

    return sorted(output, key=lambda row: (row["school"], row["date"]))


def build_school_summary(merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_school: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in merged:
        by_school[row["school"]].append(row)

    records: list[dict[str, Any]] = []
    for school in GA4_SCHOOL_CODES:
        rows = by_school.get(school, [])
        total_posts = sum(int(row.get("news_posts_count", 0) or 0) for row in rows)
        avg_views_all_days = safe_mean([float(row["views"]) for row in rows])

        post_days = [row for row in rows if row["is_post_day"]]
        recent_1 = [row for row in rows if row.get("days_since_last_post") is not None and row["days_since_last_post"] <= 1]
        recent_3 = [row for row in rows if row.get("days_since_last_post") is not None and row["days_since_last_post"] <= 3]
        recent_7 = [row for row in rows if row.get("days_since_last_post") is not None and row["days_since_last_post"] <= 7]
        no_recent = [row for row in rows if row.get("days_since_last_post") is None or row["days_since_last_post"] > 7]

        avg_views_on_post_days = safe_mean([float(row["views"]) for row in post_days])
        avg_views_within_1_day = safe_mean([float(row["views"]) for row in recent_1])
        avg_views_within_3_days = safe_mean([float(row["views"]) for row in recent_3])
        avg_views_within_7_days = safe_mean([float(row["views"]) for row in recent_7])
        avg_views_no_recent_post = safe_mean([float(row["views"]) for row in no_recent])

        records.append(
            {
                "school": school,
                "total_posts": total_posts,
                "no_news_posts_in_period": total_posts == 0,
                "avg_views_all_days": avg_views_all_days,
                "avg_views_on_post_days": avg_views_on_post_days,
                "avg_views_within_1_day": avg_views_within_1_day,
                "avg_views_within_3_days": avg_views_within_3_days,
                "avg_views_within_7_days": avg_views_within_7_days,
                "avg_views_no_recent_post": avg_views_no_recent_post,
                "view_lift_on_post_days_pct": safe_pct_change(avg_views_on_post_days, avg_views_no_recent_post),
                "view_lift_within_1_day_pct": safe_pct_change(avg_views_within_1_day, avg_views_no_recent_post),
                "view_lift_within_3_days_pct": safe_pct_change(avg_views_within_3_days, avg_views_no_recent_post),
                "view_lift_within_7_days_pct": safe_pct_change(avg_views_within_7_days, avg_views_no_recent_post),
            }
        )

    return records


def build_event_impact(news_posts: list[dict[str, Any]], merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    views_lookup: dict[tuple[str, date], list[float]] = defaultdict(list)
    for row in merged:
        views_lookup[(row["school"], row["date"])].append(float(row["views"]))

    school_dates: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for row in merged:
        school_dates[row["school"]].append((row["date"], float(row["views"])))
    for school in school_dates:
        school_dates[school].sort(key=lambda item: item[0])

    event_rows: list[dict[str, Any]] = []
    for post in sorted(news_posts, key=lambda row: (row["school"], row["date"], row["title"])):
        school = post["school"]
        post_date = post["date"]
        rows = school_dates.get(school, [])

        views_on_post_day = safe_mean(views_lookup.get((school, post_date), []))
        before_window = [views for row_date, views in rows if post_date - timedelta(days=3) <= row_date < post_date]
        after_window = [views for row_date, views in rows if post_date < row_date <= post_date + timedelta(days=3)]

        avg_before = safe_mean(before_window)
        avg_after = safe_mean(after_window)
        change = None if avg_before is None or avg_after is None else avg_after - avg_before
        change_pct = safe_pct_change(avg_after, avg_before)
        increased = avg_before is not None and avg_after is not None and avg_after > avg_before

        event_rows.append(
            {
                "school": school,
                "post_date": post_date,
                "post_title": post["title"],
                "post_url": post["url"],
                "views_on_post_day": views_on_post_day,
                "avg_views_3_days_before": avg_before,
                "avg_views_3_days_after": avg_after,
                "view_change_after_post": change,
                "view_change_after_post_pct": change_pct,
                "views_increased_after_post": increased,
            }
        )

    return event_rows


def warn_school_mismatches(ga4: list[dict[str, Any]], news_posts: list[dict[str, Any]]) -> None:
    ga4_schools = {row["school"] for row in ga4}
    news_schools = {row["school"] for row in news_posts if row.get("school")}
    unexpected_news = sorted(news_schools - ga4_schools)
    if unexpected_news:
        warn(f"News post school codes not found in GA4 data: {unexpected_news}")


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        return format_number(value)
    return normalize_text(value)


def output_rows_from_dicts(rows: list[dict[str, Any]], columns: list[str]) -> list[list[str]]:
    return [columns] + [[format_cell(row.get(column)) for column in columns] for row in rows]


def build_views_and_clicks_sheet(merged: list[dict[str, Any]], month: str | None) -> list[list[str]]:
    rows = []
    for row in merged:
        month_value = month or row["date"].strftime("%Y-%m")
        rows.append(
            {
                "Date": row["date"],
                "Month": month_value,
                "School": row["school"],
                "Views": row["views"],
                "Clicks": row.get("clicks"),
                "Users": row.get("users"),
                "Posts": row.get("news_posts_count", 0),
                "Notes": "",
            }
        )

    rows.sort(key=lambda row: (row["School"], row["Date"]))
    columns = ["Date", "Month", "School", "Views", "Clicks", "Users", "Posts", "Notes"]
    return output_rows_from_dicts(rows, columns)


def build_post_impact_sheet(summary: list[dict[str, Any]]) -> list[list[str]]:
    rows = [dict(row) for row in sorted(summary, key=lambda row: row["school"])]
    columns = [
        "school",
        "total_posts",
        "no_news_posts_in_period",
        "avg_views_all_days",
        "avg_views_on_post_days",
        "avg_views_within_1_day",
        "avg_views_within_3_days",
        "avg_views_within_7_days",
        "avg_views_no_recent_post",
        "view_lift_on_post_days_pct",
        "view_lift_within_1_day_pct",
        "view_lift_within_3_days_pct",
        "view_lift_within_7_days_pct",
    ]
    if rows and "month" in rows[0]:
        columns.append("month")

    headers = [column.replace("_", " ").title() if column != "month" else "Month" for column in columns]
    return [headers] + [[format_cell(row.get(column)) for column in columns] for row in rows]


def build_post_details_sheet(events: list[dict[str, Any]]) -> list[list[str]]:
    rows = sorted(events, key=lambda row: (row["school"], row["post_date"], row["post_title"]))
    columns = [
        "school",
        "post_date",
        "post_title",
        "post_url",
        "views_on_post_day",
        "avg_views_3_days_before",
        "avg_views_3_days_after",
        "view_change_after_post",
        "view_change_after_post_pct",
        "views_increased_after_post",
    ]
    if rows and "month" in rows[0]:
        columns.append("month")

    headers = [column.replace("_", " ").title() if column != "month" else "Month" for column in columns]
    return [headers] + [[format_cell(row.get(column)) for column in columns] for row in rows]


def build_news_posts_sheet(news_posts: list[dict[str, Any]]) -> list[list[str]]:
    rows = sorted(news_posts, key=lambda row: (row["date"], row["title"]), reverse=True)
    columns = ["date", "school", "title", "url", "slug", "category"]
    return [columns] + [[format_cell(row.get(column)) for column in columns] for row in rows]


def build_news_posts_raw_rows(news_posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = sorted(news_posts, key=lambda row: (row["date"], row["title"]), reverse=True)
    return [
        {
            "date": row.get("date"),
            "title": row.get("title"),
            "link": row.get("url"),
            "slug": row.get("slug"),
            "school": row.get("school"),
            "category": row.get("category"),
        }
        for row in rows
    ]


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: format_cell(row.get(column)) for column in columns})


def write_table_csv(path: Path, table: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(table)


def maybe_upload_to_sheets(
    spreadsheet_id: str,
    service_account_file: Path,
    views_sheet_name: str,
    post_impact_sheet_name: str,
    post_details_sheet_name: str,
    news_posts_sheet_name: str,
    merged: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    events: list[dict[str, Any]],
    news_posts: list[dict[str, Any]],
    month: str | None,
) -> bool:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception:
        warn("Google Sheets upload skipped because google client libraries are not installed.")
        return False

    if not service_account_file.exists():
        raise FileNotFoundError(f"Service account file not found: {service_account_file}")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = service_account.Credentials.from_service_account_file(service_account_file, scopes=scopes)
    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)

    def sheet_range(sheet_name: str) -> str:
        escaped = sheet_name.replace("'", "''")
        return f"'{escaped}'!A:Z"

    def quoted_sheet_name(sheet_name: str) -> str:
        return sheet_range(sheet_name).split("!")[0]

    spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()
    existing = {
        str(sheet.get("properties", {}).get("title", "")).strip()
        for sheet in spreadsheet.get("sheets", [])
    }
    missing = [
        sheet_name
        for sheet_name in [views_sheet_name, post_impact_sheet_name, post_details_sheet_name, news_posts_sheet_name]
        if sheet_name not in existing
    ]
    if missing:
        requests = [{"addSheet": {"properties": {"title": sheet_name}}} for sheet_name in missing]
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()

    sheets_payload = [
        (views_sheet_name, build_views_and_clicks_sheet(merged, month)),
        (post_impact_sheet_name, build_post_impact_sheet(summary)),
        (post_details_sheet_name, build_post_details_sheet(events)),
        (news_posts_sheet_name, build_news_posts_sheet(news_posts)),
    ]
    for sheet_name, rows in sheets_payload:
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=sheet_range(sheet_name),
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{quoted_sheet_name(sheet_name)}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()

    print(
        f"Uploaded {views_sheet_name}, {post_impact_sheet_name}, {post_details_sheet_name}, and {news_posts_sheet_name} "
        f"to spreadsheet {spreadsheet_id}"
    )
    return True


def write_outputs(
    output_dir: Path,
    merged: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    events: list[dict[str, Any]],
    news_posts: list[dict[str, Any]],
    news_posts_raw: list[dict[str, Any]],
    run_stamp: str,
    spreadsheet_id: str | None = None,
    service_account_file: Path | None = None,
    views_sheet_name: str = DEFAULT_VIEWS_SHEET_NAME,
    post_impact_sheet_name: str = DEFAULT_POST_IMPACT_SHEET_NAME,
    post_details_sheet_name: str = DEFAULT_POST_DETAILS_SHEET_NAME,
    news_posts_sheet_name: str = DEFAULT_NEWS_POSTS_SHEET_NAME,
    month: str | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = output_dir / "looker_studio_dataset.csv"
    dataset_dated_path = output_dir / f"looker_studio_dataset_{run_stamp}.csv"
    merged_path = output_dir / "merged_news_views_daily.csv"
    summary_path = output_dir / "news_views_school_summary.csv"
    events_path = output_dir / "news_post_event_impact.csv"
    news_posts_path = output_dir / "news_posts.csv"
    news_posts_compat_path = output_dir / "NewsPosts.csv"
    news_posts_raw_path = output_dir / "news_posts_rest_raw.csv"
    merged_dated_path = output_dir / f"merged_news_views_daily_{run_stamp}.csv"
    summary_dated_path = output_dir / f"news_views_school_summary_{run_stamp}.csv"
    events_dated_path = output_dir / f"news_post_event_impact_{run_stamp}.csv"
    news_posts_dated_path = output_dir / f"news_posts_{run_stamp}.csv"
    news_posts_compat_dated_path = output_dir / f"NewsPosts_{run_stamp}.csv"
    news_posts_raw_dated_path = output_dir / f"news_posts_rest_raw_{run_stamp}.csv"

    merged_columns = [
        "school",
        "date",
        "views",
        "users",
        "clicks",
        "impressions",
        "ctr",
        "position",
        "news_posts_count",
        "last_post_date",
        "days_since_last_post",
        "within_1_day_post",
        "within_3_days_post",
        "within_7_days_post",
        "is_post_day",
        "month",
    ]
    if merged and "month" not in merged[0]:
        merged_columns = [column for column in merged_columns if column != "month"]

    summary_columns = [
        "school",
        "total_posts",
        "no_news_posts_in_period",
        "avg_views_all_days",
        "avg_views_on_post_days",
        "avg_views_within_1_day",
        "avg_views_within_3_days",
        "avg_views_within_7_days",
        "avg_views_no_recent_post",
        "view_lift_on_post_days_pct",
        "view_lift_within_1_day_pct",
        "view_lift_within_3_days_pct",
        "view_lift_within_7_days_pct",
        "month",
    ]
    if summary and "month" not in summary[0]:
        summary_columns = [column for column in summary_columns if column != "month"]

    events_columns = [
        "school",
        "post_date",
        "post_title",
        "post_url",
        "views_on_post_day",
        "avg_views_3_days_before",
        "avg_views_3_days_after",
        "view_change_after_post",
        "view_change_after_post_pct",
        "views_increased_after_post",
        "month",
    ]
    if events and "month" not in events[0]:
        events_columns = [column for column in events_columns if column != "month"]

    news_posts_columns = ["date", "school", "title", "url", "slug", "category"]

    if merged:
        write_csv(dataset_path, merged, merged_columns)
        write_csv(dataset_dated_path, merged, merged_columns)
        write_csv(merged_path, merged, merged_columns)
        write_csv(merged_dated_path, merged, merged_columns)
    else:
        for path in [dataset_path, dataset_dated_path, merged_path, merged_dated_path]:
            write_csv(path, [], merged_columns)

    if summary:
        write_csv(summary_path, summary, summary_columns)
        write_csv(summary_dated_path, summary, summary_columns)
    else:
        for path in [summary_path, summary_dated_path]:
            write_csv(path, [], summary_columns)

    if events:
        write_csv(events_path, events, events_columns)
        write_csv(events_dated_path, events, events_columns)
    else:
        for path in [events_path, events_dated_path]:
            write_csv(path, [], events_columns)

    if news_posts:
        write_csv(news_posts_path, news_posts, news_posts_columns)
        write_csv(news_posts_compat_path, news_posts, news_posts_columns)
        write_csv(news_posts_dated_path, news_posts, news_posts_columns)
        write_csv(news_posts_compat_dated_path, news_posts, news_posts_columns)
    else:
        for path in [news_posts_path, news_posts_compat_path, news_posts_dated_path, news_posts_compat_dated_path]:
            write_csv(path, [], news_posts_columns)

    raw_rows = build_news_posts_raw_rows(news_posts_raw)
    raw_columns = ["date", "title", "link", "slug", "school", "category"]
    if raw_rows:
        write_csv(news_posts_raw_path, raw_rows, raw_columns)
        write_csv(news_posts_raw_dated_path, raw_rows, raw_columns)
    else:
        for path in [news_posts_raw_path, news_posts_raw_dated_path]:
            write_csv(path, [], raw_columns)

    print(f"Wrote {dataset_path}")
    print(f"Wrote {dataset_dated_path}")
    print(f"Wrote {merged_path}")
    print(f"Wrote {summary_path}")
    print(f"Wrote {events_path}")
    print(f"Wrote {news_posts_path}")
    print(f"Wrote {news_posts_compat_path}")
    print(f"Wrote {news_posts_raw_path}")
    print(f"Wrote {merged_dated_path}")
    print(f"Wrote {summary_dated_path}")
    print(f"Wrote {events_dated_path}")
    print(f"Wrote {news_posts_dated_path}")
    print(f"Wrote {news_posts_compat_dated_path}")
    print(f"Wrote {news_posts_raw_dated_path}")

    if spreadsheet_id:
        uploaded = maybe_upload_to_sheets(
            spreadsheet_id,
            service_account_file or DEFAULT_SERVICE_ACCOUNT_FILE,
            views_sheet_name,
            post_impact_sheet_name,
            post_details_sheet_name,
            news_posts_sheet_name,
            merged,
            summary,
            events,
            news_posts,
            month,
        )
        if not uploaded:
            warn("Spreadsheet upload skipped.")


def main() -> int:
    try:
        args = parse_args()
        run_label, month_start, month_end = parse_date_range(args.month, args.start_date, args.end_date)

        ga4_raw = load_csv(args.ga4, "GA4")
        ga4 = prepare_ga4(ga4_raw, month_start, month_end)

        if not ga4:
            warn("GA4 data is empty after filtering.")

        try:
            news_posts_raw = fetch_headline_news_posts()
        except Exception as exc:
            warn(f"Headline News REST fetch failed: {exc}")
            news_posts_raw = []

        if not news_posts_raw:
            warn("Headline News REST data is empty after fetch.")

        news_posts_in_range = filter_news_posts_by_date(news_posts_raw, month_start, month_end)

        warn_school_mismatches(ga4, news_posts_in_range)

        news_daily = build_news_daily_summary(news_posts_in_range)
        news_daily_lookup = {(row["school"], row["date"]): row["news_posts_count"] for row in news_daily}

        merged: list[dict[str, Any]] = []
        for row in ga4:
            merged.append(
                {
                    "school": row["school"],
                    "date": row["date"],
                    "views": row["views"],
                    "users": row.get("users"),
                    "clicks": row.get("clicks"),
                    "impressions": row.get("impressions"),
                    "ctr": row.get("ctr"),
                    "position": row.get("position"),
                    "news_posts_count": news_daily_lookup.get((row["school"], row["date"]), 0),
                }
            )

        merged = add_rolling_post_flags(merged, news_daily)
        summary = build_school_summary(merged)
        events = build_event_impact(news_posts_in_range, merged)

        if run_label is not None:
            for row in merged:
                row["month"] = run_label
            for row in summary:
                row["month"] = run_label
            for row in events:
                row["month"] = run_label

        run_stamp = run_label or date.today().strftime("%Y-%m-%d")
        write_outputs(
            args.output_dir,
            merged,
            summary,
            events,
            news_posts_in_range,
            news_posts_raw,
            run_stamp,
            spreadsheet_id=args.spreadsheet_id,
            service_account_file=args.service_account_file,
            views_sheet_name=args.views_sheet_name,
            post_impact_sheet_name=args.post_impact_sheet_name,
            post_details_sheet_name=args.post_details_sheet_name,
            news_posts_sheet_name=args.news_posts_sheet_name,
            month=args.month,
        )
        return 0
    except Exception as exc:
        warn(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
