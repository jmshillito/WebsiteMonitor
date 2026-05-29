#!/usr/bin/env python3
"""Correlate GA4 daily views with news post activity by school."""

from __future__ import annotations

import argparse
from datetime import datetime
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


GA4_REQUIRED_COLUMNS = {"school", "date", "views"}
RSS_REQUIRED_COLUMNS = {"school"}
DEFAULT_SERVICE_ACCOUNT_FILE = Path("ga4-reporting/keys/service-account.json")
DEFAULT_VIEWS_SHEET_NAME = "Views and Clicks"
DEFAULT_POST_IMPACT_SHEET_NAME = "Post Impact"
DEFAULT_POST_DETAILS_SHEET_NAME = "Post Details"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
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
    rss: Path
    output_dir: Path
    start_date: str | None
    end_date: str | None
    month: str | None
    spreadsheet_id: str | None
    service_account_file: Path
    views_sheet_name: str
    post_impact_sheet_name: str
    post_details_sheet_name: str


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def parse_args() -> InputPaths:
    parser = argparse.ArgumentParser(
        description="Correlate GA4 daily views with news posts and write Looker Studio friendly CSVs."
    )
    parser.add_argument("--ga4", type=Path, required=True, help="Path to the GA4 daily report CSV.")
    parser.add_argument("--rss", type=Path, required=True, help="Path to the RSS/news posts CSV.")
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

    args = parser.parse_args()
    spreadsheet_id = None
    if args.spreadsheet_id is not None:
        spreadsheet_id = str(args.spreadsheet_id).strip() or None
    return InputPaths(
        ga4=args.ga4,
        rss=args.rss,
        output_dir=args.output_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        month=args.month,
        spreadsheet_id=spreadsheet_id,
        service_account_file=args.service_account_file,
        views_sheet_name=args.views_sheet_name,
        post_impact_sheet_name=args.post_impact_sheet_name,
        post_details_sheet_name=args.post_details_sheet_name,
    )


def parse_date_range(
    month: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str | None, pd.Timestamp | None, pd.Timestamp | None]:
    if month is not None:
        try:
            month_start = pd.Timestamp(f"{month}-01")
        except ValueError as exc:
            raise ValueError("--month must use YYYY-MM format") from exc

        month_end = month_start + pd.offsets.MonthEnd(0)
        return month, month_start.normalize(), month_end.normalize()

    if start_date is None and end_date is None:
        return None, None, None
    if start_date is None or end_date is None:
        raise ValueError("--start-date and --end-date must be provided together")

    try:
        parsed_start = pd.Timestamp(start_date)
        parsed_end = pd.Timestamp(end_date)
    except ValueError as exc:
        raise ValueError("--start-date and --end-date must use YYYY-MM-DD format") from exc

    if parsed_end < parsed_start:
        raise ValueError("--end-date must be on or after --start-date")

    return f"{parsed_start:%Y-%m-%d}_to_{parsed_end:%Y-%m-%d}", parsed_start.normalize(), parsed_end.normalize()


def load_csv(path: Path, label: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"{label} file not found: {path}")
    return pd.read_csv(path)


def normalize_school_series(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.upper()


def normalize_date_series(series: pd.Series, label: str) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", utc=True).dt.tz_convert(None)
    missing = parsed.isna().sum()
    if missing:
        warn(f"{label}: {missing} row(s) could not be parsed as dates and will be dropped.")
    return parsed.dt.normalize()


def prepare_ga4(df: pd.DataFrame, month_start: pd.Timestamp | None, month_end: pd.Timestamp | None) -> pd.DataFrame:
    missing = GA4_REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"GA4 CSV is missing required columns: {sorted(missing)}")

    ga4 = df.copy()
    ga4.columns = [str(col).strip().lower() for col in ga4.columns]
    ga4["school"] = normalize_school_series(ga4["school"])
    ga4["date"] = normalize_date_series(ga4["date"], "GA4")
    ga4["views"] = pd.to_numeric(ga4["views"], errors="coerce")
    if "users" not in ga4.columns:
        ga4["users"] = pd.NA
    ga4["users"] = pd.to_numeric(ga4["users"], errors="coerce")

    if ga4["views"].isna().any():
        warn("GA4: some views values could not be converted to numbers and will be dropped.")

    ga4 = ga4.dropna(subset=["school", "date", "views"]).copy()
    if ga4["views"].dropna().mod(1).eq(0).all():
        ga4["views"] = ga4["views"].astype("Int64")
    else:
        ga4["views"] = ga4["views"].astype(float)
    if ga4["users"].dropna().mod(1).eq(0).all():
        ga4["users"] = ga4["users"].fillna(0).astype("Int64")
    else:
        ga4["users"] = ga4["users"].fillna(0).astype(float)

    if month_start is not None and month_end is not None:
        ga4 = ga4[(ga4["date"] >= month_start) & (ga4["date"] <= month_end)].copy()

    return ga4


def prepare_rss(df: pd.DataFrame, month_start: pd.Timestamp | None, month_end: pd.Timestamp | None) -> pd.DataFrame:
    rss = df.copy()
    rss.columns = [str(col).strip().lower() for col in rss.columns]

    # Accept both the requested input schema and the current RSS script output schema.
    rename_map: dict[str, str] = {}
    if "post_date" not in rss.columns and "last date" in rss.columns:
        rename_map["last date"] = "post_date"
    if "post_title" not in rss.columns and "title" in rss.columns:
        rename_map["title"] = "post_title"
    if "post_url" not in rss.columns and "link" in rss.columns:
        rename_map["link"] = "post_url"
    if "school" not in rss.columns and "ga4 school" in rss.columns:
        rename_map["ga4 school"] = "school"
    rss = rss.rename(columns=rename_map)

    missing = RSS_REQUIRED_COLUMNS - set(rss.columns)
    if missing:
        raise ValueError(f"RSS CSV is missing required columns: {sorted(missing)}")

    if "post_date" not in rss.columns:
        raise ValueError("RSS CSV is missing required column: post_date")
    if "post_title" not in rss.columns:
        rss["post_title"] = ""
    if "post_url" not in rss.columns:
        rss["post_url"] = ""

    if "ga4 school" in rss.columns:
        rss["school"] = normalize_school_series(rss["ga4 school"])
    else:
        rss["school"] = normalize_school_series(rss["school"])

    rss = rss[~rss["school"].str.startswith("UPDATED ON", na=False)].copy()

    rss["post_date"] = normalize_date_series(rss["post_date"], "RSS")
    rss["post_title"] = rss["post_title"].fillna("").astype(str)
    rss["post_url"] = rss["post_url"].fillna("").astype(str)

    rss = rss.dropna(subset=["school", "post_date"]).copy()

    if month_start is not None and month_end is not None:
        rss = rss[(rss["post_date"] >= month_start) & (rss["post_date"] <= month_end)].copy()

    return rss


def build_news_daily_summary(rss: pd.DataFrame) -> pd.DataFrame:
    daily = (
        rss.groupby(["school", "post_date"], as_index=False)
        .agg(news_posts_count=("post_title", "size"))
        .rename(columns={"post_date": "date"})
    )
    daily["date"] = pd.to_datetime(daily["date"]).dt.normalize()
    return daily


def add_rolling_post_flags(merged: pd.DataFrame, news_daily: pd.DataFrame) -> pd.DataFrame:
    merged = merged.sort_values(["school", "date"]).copy()

    merged["last_post_date"] = ""
    merged["days_since_last_post"] = pd.NA
    merged["within_1_day_post"] = False
    merged["within_3_days_post"] = False
    merged["within_7_days_post"] = False

    post_dates_by_school: dict[str, list[pd.Timestamp]] = {}
    for school, group in news_daily.sort_values(["school", "date"]).groupby("school", sort=False):
        post_dates_by_school[school] = [pd.Timestamp(value) for value in group["date"].tolist()]

    for school, group in merged.groupby("school", sort=False):
        post_dates = post_dates_by_school.get(school, [])
        post_index = 0
        current_last_post: pd.Timestamp | pd.NaT = pd.NaT
        last_post_dates: list[str] = []
        days_since_last: list[int | pd.NA] = []
        within_1: list[bool] = []
        within_3: list[bool] = []
        within_7: list[bool] = []

        for _, row in group.iterrows():
            row_date = pd.Timestamp(row["date"])

            while post_index < len(post_dates) and post_dates[post_index] <= row_date:
                current_last_post = post_dates[post_index]
                post_index += 1

            last_post_dates.append(current_last_post.strftime("%Y-%m-%d") if pd.notna(current_last_post) else "")
            if pd.isna(current_last_post):
                days_since_last.append(pd.NA)
                within_1.append(False)
                within_3.append(False)
                within_7.append(False)
                continue

            delta_days = int((row_date - current_last_post).days)
            days_since_last.append(delta_days)
            within_1.append(delta_days <= 1)
            within_3.append(delta_days <= 3)
            within_7.append(delta_days <= 7)

        merged.loc[group.index, "last_post_date"] = last_post_dates
        merged.loc[group.index, "days_since_last_post"] = days_since_last
        merged.loc[group.index, "within_1_day_post"] = within_1
        merged.loc[group.index, "within_3_days_post"] = within_3
        merged.loc[group.index, "within_7_days_post"] = within_7

    merged["is_post_day"] = merged["news_posts_count"] > 0
    merged["date"] = pd.to_datetime(merged["date"]).dt.strftime("%Y-%m-%d")
    merged["last_post_date"] = merged["last_post_date"].fillna("")
    merged["days_since_last_post"] = merged["days_since_last_post"].astype("Int64")
    merged["news_posts_count"] = merged["news_posts_count"].fillna(0).astype(int)

    return merged


def safe_pct_change(numerator: float | int | None, denominator: float | int | None) -> float | pd.NA:
    if numerator is None or denominator is None:
        return pd.NA
    if pd.isna(numerator) or pd.isna(denominator) or denominator == 0:
        return pd.NA
    return ((float(numerator) - float(denominator)) / float(denominator)) * 100.0


def build_school_summary(merged: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, object]] = []

    all_schools = GA4_SCHOOL_CODES
    for school in all_schools:
        school_df = merged[merged["school"] == school].copy()
        total_posts = int(school_df["news_posts_count"].sum())
        avg_views_all_days = school_df["views"].mean()

        post_days = school_df[school_df["is_post_day"]]
        recent_1 = school_df[school_df["days_since_last_post"].between(0, 1, inclusive="both")]
        recent_3 = school_df[school_df["days_since_last_post"].between(0, 3, inclusive="both")]
        recent_7 = school_df[school_df["days_since_last_post"].between(0, 7, inclusive="both")]
        no_recent = school_df[school_df["days_since_last_post"].isna() | (school_df["days_since_last_post"] > 7)]

        avg_views_on_post_days = post_days["views"].mean()
        avg_views_within_1_day = recent_1["views"].mean()
        avg_views_within_3_days = recent_3["views"].mean()
        avg_views_within_7_days = recent_7["views"].mean()
        avg_views_no_recent_post = no_recent["views"].mean()

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

    summary = pd.DataFrame.from_records(records)
    numeric_columns = [
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
    for column in numeric_columns:
        if column in summary.columns:
            summary[column] = pd.to_numeric(summary[column], errors="coerce").round(2)
    return summary


def build_event_impact(rss: pd.DataFrame, merged: pd.DataFrame) -> pd.DataFrame:
    event_rows: list[dict[str, object]] = []
    views_lookup = merged[["school", "date", "views"]].copy()
    views_lookup["date"] = pd.to_datetime(views_lookup["date"])

    for _, post in rss.sort_values(["school", "post_date", "post_title"]).iterrows():
        school = post["school"]
        post_date = pd.Timestamp(post["post_date"])
        school_views = views_lookup[views_lookup["school"] == school].copy()

        on_day_match = school_views[school_views["date"] == post_date]
        views_on_post_day = on_day_match["views"].mean() if not on_day_match.empty else pd.NA

        before_window = school_views[
            (school_views["date"] >= post_date - pd.Timedelta(days=3))
            & (school_views["date"] < post_date)
        ]
        after_window = school_views[
            (school_views["date"] > post_date)
            & (school_views["date"] <= post_date + pd.Timedelta(days=3))
        ]

        avg_views_3_days_before = before_window["views"].mean()
        avg_views_3_days_after = after_window["views"].mean()
        view_change_after_post = (
            avg_views_3_days_after - avg_views_3_days_before
            if pd.notna(avg_views_3_days_after) and pd.notna(avg_views_3_days_before)
            else pd.NA
        )
        view_change_after_post_pct = safe_pct_change(avg_views_3_days_after, avg_views_3_days_before)
        views_increased_after_post = bool(
            pd.notna(avg_views_3_days_after)
            and pd.notna(avg_views_3_days_before)
            and avg_views_3_days_after > avg_views_3_days_before
        )

        event_rows.append(
            {
                "school": school,
                "post_date": post_date.strftime("%Y-%m-%d"),
                "post_title": post["post_title"],
                "post_url": post["post_url"],
                "views_on_post_day": views_on_post_day,
                "avg_views_3_days_before": avg_views_3_days_before,
                "avg_views_3_days_after": avg_views_3_days_after,
                "view_change_after_post": view_change_after_post,
                "view_change_after_post_pct": view_change_after_post_pct,
                "views_increased_after_post": views_increased_after_post,
            }
        )

    return pd.DataFrame.from_records(
        event_rows,
        columns=[
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
        ],
    )


def warn_school_mismatches(ga4: pd.DataFrame, rss: pd.DataFrame) -> None:
    ga4_schools = set(ga4["school"].dropna().unique().tolist())
    rss_schools = set(rss["school"].dropna().unique().tolist())

    unexpected_rss = sorted(rss_schools - ga4_schools)
    if unexpected_rss:
        warn(f"RSS school codes not found in GA4 data: {unexpected_rss}")


def sheet_name_range(sheet_name: str) -> str:
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'!A:Z"


def quoted_sheet_name(sheet_name: str) -> str:
    return sheet_name_range(sheet_name).split("!")[0]


def load_sheets_service(service_account_file: Path):
    if not service_account_file.exists():
        raise FileNotFoundError(f"Service account file not found: {service_account_file}")

    credentials = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=SHEETS_SCOPES,
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def ensure_sheet_tabs(service, spreadsheet_id: str, sheet_names: list[str]) -> None:
    spreadsheet = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
        .execute()
    )
    existing = {
        str(sheet.get("properties", {}).get("title", "")).strip()
        for sheet in spreadsheet.get("sheets", [])
    }
    missing = [sheet_name for sheet_name in sheet_names if sheet_name not in existing]
    if not missing:
        return

    requests = [{"addSheet": {"properties": {"title": sheet_name}}} for sheet_name in missing]
    service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()


def write_sheet(service, spreadsheet_id: str, sheet_name: str, rows: list[list[object]]) -> None:
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=sheet_name_range(sheet_name),
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{quoted_sheet_name(sheet_name)}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


def build_views_and_clicks_sheet(merged: pd.DataFrame, month: str | None) -> list[list[object]]:
    sheet = merged.copy()
    sheet["date"] = pd.to_datetime(sheet["date"]).dt.strftime("%Y-%m-%d")
    month_series = pd.to_datetime(sheet["date"]).dt.strftime("%Y-%m")
    if "month" not in sheet.columns:
        sheet["month"] = month_series
    else:
        sheet["month"] = sheet["month"].fillna(month_series)

    if "users" not in sheet.columns:
        sheet["users"] = ""
    sheet["posts"] = sheet["news_posts_count"].fillna(0).astype(int)
    sheet["notes"] = ""

    if month is not None:
        sheet["month"] = month

    output = sheet.rename(
        columns={
            "date": "Date",
            "month": "Month",
            "school": "School",
            "views": "Views",
            "clicks": "Clicks",
            "users": "Users",
            "posts": "Posts",
            "notes": "Notes",
        }
    )
    output = output.sort_values(["School", "Date"])
    columns = ["Date", "Month", "School", "Views", "Clicks", "Users", "Posts", "Notes"]
    return [columns] + output[columns].fillna("").values.tolist()


def build_post_impact_sheet(summary: pd.DataFrame) -> list[list[object]]:
    output = summary.copy().sort_values(["school"])
    if "month" in output.columns:
        columns = [col for col in output.columns if col != "month"] + ["month"]
    else:
        columns = list(output.columns)
    headers = [
        str(column).replace("_", " ").title() if column != "month" else "Month"
        for column in columns
    ]
    return [headers] + output[columns].fillna("").values.tolist()


def build_post_details_sheet(events: pd.DataFrame) -> list[list[object]]:
    output = events.copy()
    sort_columns = [col for col in ["school", "post_date", "post_title"] if col in output.columns]
    if sort_columns:
        output = output.sort_values(sort_columns)
    if "month" in output.columns:
        columns = [col for col in output.columns if col != "month"] + ["month"]
    else:
        columns = list(output.columns)
    headers = [
        str(column).replace("_", " ").title() if column != "month" else "Month"
        for column in columns
    ]
    return [headers] + output[columns].fillna("").values.tolist()


def write_outputs(
    output_dir: Path,
    merged: pd.DataFrame,
    summary: pd.DataFrame,
    events: pd.DataFrame,
    run_stamp: str,
    spreadsheet_id: str | None = None,
    service_account_file: Path | None = None,
    views_sheet_name: str = DEFAULT_VIEWS_SHEET_NAME,
    post_impact_sheet_name: str = DEFAULT_POST_IMPACT_SHEET_NAME,
    post_details_sheet_name: str = DEFAULT_POST_DETAILS_SHEET_NAME,
    month: str | None = None,
) -> None:
    numeric_merged_columns = ["views"]
    for column in numeric_merged_columns:
        if column in merged.columns and pd.api.types.is_numeric_dtype(merged[column]):
            if pd.api.types.is_float_dtype(merged[column]):
                merged[column] = merged[column].round(2)

    for column in [
        "views_on_post_day",
        "avg_views_3_days_before",
        "avg_views_3_days_after",
        "view_change_after_post",
        "view_change_after_post_pct",
    ]:
        if column in events.columns:
            events[column] = pd.to_numeric(events[column], errors="coerce").round(2)

    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = output_dir / "looker_studio_dataset.csv"
    dataset_dated_path = output_dir / f"looker_studio_dataset_{run_stamp}.csv"
    merged_path = output_dir / "merged_news_views_daily.csv"
    summary_path = output_dir / "news_views_school_summary.csv"
    events_path = output_dir / "news_post_event_impact.csv"
    merged_dated_path = output_dir / f"merged_news_views_daily_{run_stamp}.csv"
    summary_dated_path = output_dir / f"news_views_school_summary_{run_stamp}.csv"
    events_dated_path = output_dir / f"news_post_event_impact_{run_stamp}.csv"

    merged.to_csv(dataset_path, index=False)
    merged.to_csv(dataset_dated_path, index=False)
    merged.to_csv(merged_path, index=False)
    summary.to_csv(summary_path, index=False)
    events.to_csv(events_path, index=False)
    merged.to_csv(merged_dated_path, index=False)
    summary.to_csv(summary_dated_path, index=False)
    events.to_csv(events_dated_path, index=False)

    print(f"Wrote {dataset_path}")
    print(f"Wrote {dataset_dated_path}")
    print(f"Wrote {merged_path}")
    print(f"Wrote {summary_path}")
    print(f"Wrote {events_path}")
    print(f"Wrote {merged_dated_path}")
    print(f"Wrote {summary_dated_path}")
    print(f"Wrote {events_dated_path}")

    if spreadsheet_id:
        sheets_service = load_sheets_service(service_account_file or DEFAULT_SERVICE_ACCOUNT_FILE)
        ensure_sheet_tabs(
            sheets_service,
            spreadsheet_id,
            [views_sheet_name, post_impact_sheet_name, post_details_sheet_name],
        )
        write_sheet(
            sheets_service,
            spreadsheet_id,
            views_sheet_name,
            build_views_and_clicks_sheet(merged, month),
        )
        write_sheet(
            sheets_service,
            spreadsheet_id,
            post_impact_sheet_name,
            build_post_impact_sheet(summary),
        )
        write_sheet(
            sheets_service,
            spreadsheet_id,
            post_details_sheet_name,
            build_post_details_sheet(events),
        )
        print(
            f"Uploaded {views_sheet_name}, {post_impact_sheet_name}, and {post_details_sheet_name} "
            f"to spreadsheet {spreadsheet_id}"
        )


def main() -> int:
    try:
        args = parse_args()
        run_label, month_start, month_end = parse_date_range(args.month, args.start_date, args.end_date)

        ga4_raw = load_csv(args.ga4, "GA4")
        rss_raw = load_csv(args.rss, "RSS")

        ga4 = prepare_ga4(ga4_raw, month_start, month_end)
        rss = prepare_rss(rss_raw, month_start, month_end)

        if ga4.empty:
            warn("GA4 data is empty after filtering.")
        if rss.empty:
            warn("RSS data is empty after filtering.")

        warn_school_mismatches(ga4, rss)

        news_daily = build_news_daily_summary(rss)
        merged = ga4.merge(news_daily, on=["school", "date"], how="left")
        merged["news_posts_count"] = merged["news_posts_count"].fillna(0)
        merged = add_rolling_post_flags(merged, news_daily)

        summary = build_school_summary(merged)
        events = build_event_impact(rss, merged)

        if run_label is not None:
            merged["month"] = pd.to_datetime(merged["date"]).dt.strftime("%Y-%m")
            summary["month"] = run_label
            events["month"] = run_label

        run_stamp = run_label or pd.Timestamp.today().strftime("%Y-%m-%d")
        write_outputs(
            args.output_dir,
            merged,
            summary,
            events,
            run_stamp,
            spreadsheet_id=args.spreadsheet_id,
            service_account_file=args.service_account_file,
            views_sheet_name=args.views_sheet_name,
            post_impact_sheet_name=args.post_impact_sheet_name,
            post_details_sheet_name=args.post_details_sheet_name,
            month=args.month,
        )
        return 0
    except Exception as exc:
        warn(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
