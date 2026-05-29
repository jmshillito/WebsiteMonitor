#!/usr/bin/env python3
"""Generate a 3-page ICT365 website activity report PDF."""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
from PIL import Image as PILImage
from PIL import ImageChops
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas


SHEET_ID = "109tHI2m1olk6oXMZbV_OOneUT7apMbRJCQkYxXldA1I"
OUTPUT_PREFIX = "website_activity_report"
LOGO_PATH = Path("assets/ict365-logo.png")

HEADER_COLOR = colors.HexColor("#006E6D")
HEADER_TEXT = colors.white
HEADER_SUBTITLE = colors.HexColor("#D9F3F7")
PAGE_BG = colors.HexColor("#F5F8FC")
CARD_BORDER = colors.HexColor("#D7E1ED")
CARD_BG = colors.white
LIGHT_BAR = colors.HexColor("#E6EDF4")
TEAL = colors.HexColor("#3DB7CC")
GOLD = colors.HexColor("#F6C85F")
TEAL_SOFT = colors.HexColor("#E9F7F7")
GOLD_SOFT = colors.HexColor("#F8D98A")
SLATE = colors.HexColor("#5E7897")
NAVY_TEXT = colors.HexColor("#2F3E52")
MUTED = colors.HexColor("#6C757D")
GREEN = colors.HexColor("#1A936F")
RED = colors.HexColor("#C0392B")
GREY = colors.HexColor("#9AA6B2")

CANONICAL_SCHOOLS = [
    "CHHS",
    "CIFEC",
    "EEPS",
    "EMPS",
    "JACPS",
    "JCPS",
    "JGHS",
    "LSHS",
    "LHS",
    "MMPS",
    "PPS",
    "RBPS",
    "SBPS",
    "TMPS",
    "WEPS",
]

SCHOOL_ALIASES = {
    "LHSS": "LSHS",
    "LSHS": "LSHS",
    "PPPS": "PPS",
    "PPS": "PPS",
}

VIEW_GROWTH_ALIASES = {
    "views growth %": "Views Growth",
    "growth views": "Views Growth",
    "views growth": "Views Growth",
    "clicks growth %": "Clicks Growth",
    "growth clicks": "Clicks Growth",
    "clicks growth": "Clicks Growth",
}

POST_DETAIL_ALIASES = {
    "school": "School",
    "ga4 school": "School",
    "post date": "Post date",
    "post_date": "Post date",
    "post title": "Post title",
    "post_title": "Post title",
    "views on post day": "Views on post day",
    "views_on_post_day": "Views on post day",
    "avg views 3 days before": "3 days before",
    "avg_views_3_days_before": "3 days before",
    "avg views 3 days after": "3 days after",
    "avg_views_3_days_after": "3 days after",
    "view change after post pct": "Change",
    "view_change_after_post_pct": "Change",
    "view change after post": "Change",
    "view_change_after_post": "Change",
    "views increased after post": "Impact",
    "views_increased_after_post": "Impact",
}

POST_COLUMNS = [
    "School",
    "Post date",
    "Post title",
    "Views on post day",
    "3 days before",
    "3 days after",
    "Change",
    "Impact",
]


def load_sheet(tab_name: str) -> pd.DataFrame:
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq"
        f"?tqx=out:csv&sheet={tab_name.replace(' ', '%20')}"
    )
    print(f"Loading: {tab_name}")
    return pd.read_csv(url)


def normalize_school_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return SCHOOL_ALIASES.get(text, text)


def is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip().lower()
        return text == "" or text in {"nan", "none", "null"}
    return bool(pd.isna(value))


def format_int(value: object, default: str = "No data") -> str:
    if is_missing(value):
        return default
    try:
        number = float(value)
    except Exception:
        return str(value)
    if math.isnan(number):
        return default
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.1f}"


def format_decimal(value: object, default: str = "No data") -> str:
    if is_missing(value):
        return default
    try:
        number = float(value)
    except Exception:
        return str(value)
    if math.isnan(number):
        return default
    return f"{number:,.1f}"


def format_growth(value: object, default: str = "No data") -> str:
    if is_missing(value):
        return default
    text = str(value).strip()
    if not text:
        return default
    if text.lower() in {"nan", "none", "null"}:
        return default
    if text.endswith("%"):
        return text if text.startswith(("+", "-")) else f"+{text}"
    try:
        number = float(text)
    except Exception:
        return text
    if math.isnan(number):
        return default
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.1f}%"


def growth_color(value: object) -> colors.Color:
    if is_missing(value):
        return GREY
    text = str(value).strip().lower()
    if not text or text in {"nan", "none", "null", "no data"}:
        return GREY
    if text.startswith("-"):
        return RED
    if text in {"0", "0.0", "0.0%"}:
        return GREY
    return GREEN


def display_text(value: object, default: str = "No data") -> str:
    if is_missing(value):
        return default
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return default
    return text


def load_views_clicks() -> tuple[pd.DataFrame, str]:
    raw = load_sheet("Views and Clicks")
    raw.columns = [str(column).strip() for column in raw.columns]

    rename_map: dict[str, str] = {}
    for column in raw.columns:
        key = column.strip().lower()
        if key in VIEW_GROWTH_ALIASES:
            rename_map[column] = VIEW_GROWTH_ALIASES[key]
    raw = raw.rename(columns=rename_map)
    raw = raw.loc[:, ~raw.columns.duplicated()]

    school_column = next((c for c in raw.columns if c.strip().lower() == "school"), None)
    if school_column is None:
        raise ValueError("Views and Clicks sheet is missing a School column")

    raw["School"] = raw[school_column].map(normalize_school_code)
    if school_column != "School":
        raw = raw.drop(columns=[school_column])

    for column in ["Views", "Clicks", "Users", "Posts"]:
        if column in raw.columns:
            raw[column] = pd.to_numeric(raw[column], errors="coerce")

    for column in ["Views Growth", "Clicks Growth"]:
        if column in raw.columns:
            raw[column] = raw[column].astype(object).where(~raw[column].isna(), "")

    if "Date" in raw.columns:
        parsed_dates = pd.to_datetime(raw["Date"], errors="coerce")
        raw["Date"] = parsed_dates.dt.strftime("%Y-%m-%d")
        start = parsed_dates.min()
        end = parsed_dates.max()
        period = f"{start:%b %d, %Y} - {end:%b %d, %Y}" if pd.notna(start) and pd.notna(end) else ""
    elif "date" in raw.columns:
        parsed_dates = pd.to_datetime(raw["date"], errors="coerce")
        raw["Date"] = parsed_dates.dt.strftime("%Y-%m-%d")
        start = parsed_dates.min()
        end = parsed_dates.max()
        period = f"{start:%b %d, %Y} - {end:%b %d, %Y}" if pd.notna(start) and pd.notna(end) else ""
    else:
        period = ""

    aggregate_map: dict[str, object] = {
        "Views": "sum" if "Views" in raw.columns else "first",
        "Clicks": "sum" if "Clicks" in raw.columns else "first",
    }
    if "Users" in raw.columns:
        aggregate_map["Users"] = "sum"
    if "Posts" in raw.columns:
        aggregate_map["Posts"] = "sum"
    if "Views Growth" in raw.columns:
        aggregate_map["Views Growth"] = "first"
    if "Clicks Growth" in raw.columns:
        aggregate_map["Clicks Growth"] = "first"

    summary = raw.groupby("School", as_index=False).agg(aggregate_map)
    if "Views" in summary.columns:
        summary["Views"] = pd.to_numeric(summary["Views"], errors="coerce").fillna(0)
    if "Clicks" in summary.columns:
        summary["Clicks"] = pd.to_numeric(summary["Clicks"], errors="coerce").fillna(0)
    if "Users" in summary.columns:
        summary["Users"] = pd.to_numeric(summary["Users"], errors="coerce").fillna(0)
    if "Posts" in summary.columns:
        summary["Posts"] = pd.to_numeric(summary["Posts"], errors="coerce").fillna(0)

    for column in ["Views Growth", "Clicks Growth"]:
        if column not in summary.columns:
            summary[column] = ""
        else:
            summary[column] = summary[column].replace({pd.NA: "", float("nan"): ""})

    summary = summary.sort_values(["Views", "Clicks", "School"], ascending=[False, False, True]).reset_index(drop=True)
    return summary, period


def load_post_details() -> pd.DataFrame:
    raw = load_sheet("Post Details")
    raw.columns = [str(column).strip() for column in raw.columns]

    rename_map: dict[str, str] = {}
    for column in raw.columns:
        key = column.strip().lower()
        if key in POST_DETAIL_ALIASES:
            rename_map[column] = POST_DETAIL_ALIASES[key]
    raw = raw.rename(columns=rename_map)
    raw = raw.loc[:, ~raw.columns.duplicated()]

    for column in POST_COLUMNS:
        if column not in raw.columns:
            raw[column] = ""

    raw["School"] = raw["School"].map(normalize_school_code)
    raw["Post date"] = pd.to_datetime(raw["Post date"], errors="coerce").dt.strftime("%Y-%m-%d")
    raw["Post title"] = raw["Post title"].fillna("").astype(str)

    for column in ["Views on post day", "3 days before", "3 days after", "Change"]:
        raw[column] = raw[column].replace("", pd.NA)

    raw = raw.sort_values(["School", "Post date", "Post title"], ascending=[True, True, True]).reset_index(drop=True)
    return raw[POST_COLUMNS]


def derive_impact(row: pd.Series) -> str:
    change = row.get("Change", "")
    if not is_missing(change):
        text = str(change).strip().replace("%", "")
        try:
            number = float(text)
            if number > 0:
                return "Higher"
            if number < 0:
                return "Lower"
            return "Flat"
        except Exception:
            pass

    before = row.get("3 days before", pd.NA)
    after = row.get("3 days after", pd.NA)
    if not is_missing(before) and not is_missing(after):
        try:
            before_num = float(before)
            after_num = float(after)
            if after_num > before_num:
                return "Higher"
            if after_num < before_num:
                return "Lower"
            return "Flat"
        except Exception:
            pass

    impact = row.get("Impact", "")
    if not is_missing(impact):
        text = str(impact).strip().lower()
        if text in {"higher", "lower", "flat"}:
            return text.title()
        if text in {"true", "yes", "1"}:
            return "Higher"
        if text in {"false", "no", "0"}:
            return "Lower"
    return "No data"


def get_canvas_dimensions() -> tuple[float, float]:
    return landscape(letter)


def rounded_rect(c: canvas.Canvas, x: float, y: float, w: float, h: float, fill: colors.Color, stroke: colors.Color | None = None, radius: float = 8, line_width: float = 1) -> None:
    c.setFillColor(fill)
    c.setStrokeColor(stroke if stroke else fill)
    c.setLineWidth(line_width)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1 if stroke else 0)


def label(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    size: float = 9,
    color: colors.Color = NAVY_TEXT,
    bold: bool = False,
    align: str = "left",
) -> None:
    c.setFillColor(color)
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    if align == "center":
        c.drawCentredString(x, y, text)
    elif align == "right":
        c.drawRightString(x, y, text)
    else:
        c.drawString(x, y, text)


def draw_wrapped_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    size: float,
    color: colors.Color,
    bold: bool = False,
    leading: float | None = None,
) -> float:
    lines = simpleSplit(text, "Helvetica-Bold" if bold else "Helvetica", size, width)
    line_height = leading or (size * 1.2)
    for idx, line in enumerate(lines):
        label(c, line, x, y - idx * line_height, size=size, color=color, bold=bold)
    return y - len(lines) * line_height


def draw_logo(c: canvas.Canvas, W: float, H: float) -> None:
    if not LOGO_PATH.exists():
        return
    try:
        logo = PILImage.open(LOGO_PATH)
        ratio = logo.width / logo.height
        logo_h = 1.15 * cm
        logo_w = logo_h * ratio
        max_w = 7.2 * cm
        if logo_w > max_w:
            logo_w = max_w
            logo_h = logo_w / ratio
        x = W - 1.2 * cm - logo_w
        y = H - 1.7 * cm
        c.drawImage(ImageReader(str(LOGO_PATH)), x, y, width=logo_w, height=logo_h, mask="auto")
    except Exception:
        return


def draw_header(c: canvas.Canvas, W: float, H: float, header_title: str, subtitle: str) -> None:
    c.setFillColor(HEADER_COLOR)
    c.rect(0, H - 2.2 * cm, W, 2.2 * cm, fill=1, stroke=0)
    label(c, header_title, 1.2 * cm, H - 1.0 * cm, 22, HEADER_TEXT, True)
    label(c, subtitle, 1.2 * cm, H - 1.55 * cm, 10, HEADER_SUBTITLE)
    draw_logo(c, W, H)


def build_takeaways(schools: pd.DataFrame) -> list[str]:
    takeaways: list[str] = []
    if schools.empty:
        return ["No school data available.", "No school data available.", "No school data available."]

    top_views = schools.sort_values("Views", ascending=False).iloc[0]
    top_clicks = schools.sort_values("Clicks", ascending=False).iloc[0]
    takeaways.append(
        f"{top_views['School']} leads views ({format_int(top_views['Views'])}) and {top_clicks['School']} leads clicks ({format_int(top_clicks['Clicks'])})."
    )

    click_growth_school = ""
    click_growth_value = None
    if "Clicks Growth" in schools.columns:
        growth_rows = []
        for _, row in schools.iterrows():
            text = format_growth(row.get("Clicks Growth", ""))
            if text == "No data":
                continue
            try:
                growth_rows.append((float(str(text).replace("%", "").replace("+", "")), row["School"], text))
            except Exception:
                continue
        if growth_rows:
            growth_rows.sort(reverse=True)
            _, click_growth_school, click_growth_value = growth_rows[0]

    if click_growth_school and click_growth_value:
        takeaways.append(f"{click_growth_school} has the strongest reported click growth at {click_growth_value}.")
    else:
        takeaways.append("Growth data is incomplete for some schools.")

    positive_growth = []
    if "Clicks Growth" in schools.columns:
        for _, row in schools.iterrows():
            text = format_growth(row.get("Clicks Growth", ""))
            try:
                value = float(str(text).replace("%", "").replace("+", ""))
            except Exception:
                continue
            if value > 0:
                positive_growth.append((value, row["School"]))
    positive_growth.sort(reverse=True)
    if len(positive_growth) >= 2:
        takeaways.append(f"{positive_growth[0][1]} and {positive_growth[1][1]} show consistently strong growth trends.")
    elif positive_growth:
        takeaways.append(f"{positive_growth[0][1]} shows consistently strong growth trends.")
    else:
        takeaways.append("Several schools continue to show steady engagement trends.")
    return takeaways[:3]


def draw_page1(c: canvas.Canvas, data: dict[str, object]) -> None:
    W, H = get_canvas_dimensions()
    schools = data["schools"]  # type: ignore[assignment]
    period = str(data.get("period", "")).strip()
    title = str(data.get("title", "Schools Website Activity Report"))
    draw_header(c, W, H, title, "Visual dashboard" + (f" | Reporting period: {period}" if period else ""))

    total_views = int(round(pd.to_numeric(schools["Views"], errors="coerce").fillna(0).sum()))
    total_clicks = int(round(pd.to_numeric(schools["Clicks"], errors="coerce").fillna(0).sum()))
    total_schools = int(len(schools))
    avg_clicks = int(round(total_clicks / total_schools)) if total_schools else 0
    top_view = schools.sort_values("Views", ascending=False).iloc[0]
    top_click = schools.sort_values("Clicks", ascending=False).iloc[0]

    summary = [
        ("Total schools", str(total_schools), "Included in this report"),
        ("Total views", f"{total_views:,}", f"Highest: {top_view['School']}"),
        ("Total clicks", f"{total_clicks:,}", f"Highest: {top_click['School']}"),
        ("Avg. clicks / school", f"{avg_clicks:,}", "Engagement indicator"),
    ]
    sx = 1.2 * cm
    sy = H - 4.0 * cm
    card_w = (W - 2.4 * cm - 0.9 * cm) / 4
    card_h = 1.35 * cm
    for i, (heading, value, sub) in enumerate(summary):
        x = sx + i * (card_w + 0.3 * cm)
        rounded_rect(c, x, sy, card_w, card_h, CARD_BG, CARD_BORDER, 8)
        label(c, heading, x + 0.25 * cm, sy + 0.95 * cm, 8, MUTED, True)
        label(c, value, x + 0.25 * cm, sy + 0.45 * cm, 18, HEADER_COLOR, True)
        label(c, sub, x + 0.25 * cm, sy + 0.15 * cm, 7.5, MUTED)

    section_y = H - 5.25 * cm
    label(c, "Visibility: views by school", 1.2 * cm, section_y, 13, HEADER_COLOR, True)
    label(c, "Longer bars show stronger website visibility.", 1.2 * cm, section_y - 0.42 * cm, 8.5, MUTED)

    top_sorted = schools.sort_values("Views", ascending=False).reset_index(drop=True)
    max_views = float(pd.to_numeric(top_sorted["Views"], errors="coerce").fillna(0).max() or 1)
    x0 = 1.2 * cm
    y0 = section_y - 1.0 * cm
    bar_w = 11.2 * cm
    row_h = 0.62 * cm
    for i, (_, row) in enumerate(top_sorted.iterrows()):
        y = y0 - i * row_h
        label(c, str(row["School"]), x0, y + 0.09 * cm, 8, NAVY_TEXT, True)
        rounded_rect(c, x0 + 1.45 * cm, y, bar_w, 0.32 * cm, LIGHT_BAR, None, 4)
        filled = bar_w * float(row["Views"]) / max_views if max_views else 0
        rounded_rect(c, x0 + 1.45 * cm, y, filled, 0.32 * cm, TEAL if i < 5 else colors.HexColor("#A7DDE5"), None, 4)
        label(c, format_int(row["Views"]), x0 + 1.45 * cm + bar_w + 0.25 * cm, y + 0.08 * cm, 8, NAVY_TEXT, True)

    right_x = 16.1 * cm
    label(c, "Engagement: clicks by school", right_x, section_y, 13, HEADER_COLOR, True)
    label(c, "Cards highlight click volume and growth for every school.", right_x, section_y - 0.42 * cm, 8.5, MUTED)

    click_sorted = schools.sort_values("Clicks", ascending=False).reset_index(drop=True)
    max_clicks = float(pd.to_numeric(click_sorted["Clicks"], errors="coerce").fillna(0).max() or 1)
    mini_w = 3.75 * cm
    mini_h = 1.44 * cm
    gap_x = 0.28 * cm
    gap_y = 0.20 * cm
    start_y = section_y - 2.35 * cm
    for idx, (_, row) in enumerate(click_sorted.iterrows()):
        col = idx % 3
        row_idx = idx // 3
        x = right_x + col * (mini_w + gap_x)
        y = start_y - row_idx * (mini_h + gap_y)
        fill = TEAL_SOFT if idx < 5 else CARD_BG
        rounded_rect(c, x, y, mini_w, mini_h, fill, CARD_BORDER, 7)
        label(c, str(row["School"]), x + 0.18 * cm, y + 1.08 * cm, 8, HEADER_COLOR, True)
        label(c, format_growth(row.get("Clicks Growth", "")), x + mini_w - 0.18 * cm, y + 1.08 * cm, 6.8, growth_color(row.get("Clicks Growth", "")), False, "right")
        label(c, format_int(row["Clicks"]), x + 0.18 * cm, y + 0.52 * cm, 16, NAVY_TEXT, True)
        label(c, "clicks", x + 0.18 * cm, y + 0.26 * cm, 7, MUTED)
        rounded_rect(c, x + 0.18 * cm, y + 0.12 * cm, mini_w - 0.36 * cm, 0.11 * cm, LIGHT_BAR, None, 3)
        filled = (mini_w - 0.36 * cm) * float(row["Clicks"]) / max_clicks if max_clicks else 0
        rounded_rect(c, x + 0.18 * cm, y + 0.12 * cm, filled, 0.11 * cm, GOLD if idx < 5 else GOLD_SOFT, None, 3)

    bottom_y = 0.95 * cm
    rounded_rect(c, 1.2 * cm, bottom_y, W - 2.4 * cm, 1.25 * cm, CARD_BG, CARD_BORDER, 9)
    label(c, "Key takeaways", 1.55 * cm, bottom_y + 0.82 * cm, 10, HEADER_COLOR, True)
    takeaways = build_takeaways(schools)
    positions = [
        (1.55 * cm, bottom_y + 0.45 * cm),
        (1.55 * cm, bottom_y + 0.10 * cm),
        (W / 2 + 0.4 * cm, bottom_y + 0.45 * cm),
    ]
    for i, text in enumerate(takeaways):
        x, y = positions[i]
        label(c, f"{i + 1}. {text}", x, y, 7.8, NAVY_TEXT)

    label(c, "Source: live Google Sheets tabs", W - 1.2 * cm, 0.35 * cm, 7, MUTED, False, "right")
    c.showPage()


def draw_page2(c: canvas.Canvas, data: dict[str, object]) -> None:
    W, H = get_canvas_dimensions()
    schools = data["schools"]  # type: ignore[assignment]
    draw_header(c, W, H, "School Scorecard View", "One card per school, preserving the visual dashboard format.")

    cards = schools.sort_values("Views", ascending=False).head(15).reset_index(drop=True)
    max_views = float(pd.to_numeric(cards["Views"], errors="coerce").fillna(0).max() or 1)
    max_clicks = float(pd.to_numeric(cards["Clicks"], errors="coerce").fillna(0).max() or 1)
    cols = 5
    card_w = (W - 2.4 * cm - (cols - 1) * 0.35 * cm) / cols
    card_h = 4.0 * cm
    start_x = 1.2 * cm
    start_y = H - 2.4 * cm - card_h
    for idx, (_, row) in enumerate(cards.iterrows()):
        col = idx % cols
        row_idx = idx // cols
        x = start_x + col * (card_w + 0.35 * cm)
        y = start_y - row_idx * (card_h + 0.42 * cm)
        rounded_rect(c, x, y, card_w, card_h, CARD_BG, CARD_BORDER, 9)
        header_fill = HEADER_COLOR if idx < 5 else SLATE
        c.setFillColor(header_fill)
        c.roundRect(x, y + card_h - 0.75 * cm, card_w, 0.75 * cm, 9, fill=1, stroke=0)
        c.rect(x, y + card_h - 0.38 * cm, card_w, 0.38 * cm, fill=1, stroke=0)
        label(c, str(row["School"]), x + 0.25 * cm, y + card_h - 0.48 * cm, 11, colors.white, True)

        # Keep the metric tiles visually clear of the school header band.
        detail_shift = 0.16 * cm
        label(c, "Views", x + 0.25 * cm, y + 2.55 * cm - detail_shift, 7.5, MUTED, True)
        label(c, format_int(row["Views"]), x + 0.25 * cm, y + 2.1 * cm - detail_shift, 15, NAVY_TEXT, True)
        label(c, format_growth(row.get("Views Growth", "")), x + card_w - 0.25 * cm, y + 2.16 * cm - detail_shift, 7, growth_color(row.get("Views Growth", "")), False, "right")
        rounded_rect(c, x + 0.25 * cm, y + 1.78 * cm - detail_shift, card_w - 0.5 * cm, 0.15 * cm, LIGHT_BAR, None, 3)
        rounded_rect(c, x + 0.25 * cm, y + 1.78 * cm - detail_shift, (card_w - 0.5 * cm) * float(row["Views"]) / max_views, 0.15 * cm, TEAL, None, 3)

        label(c, "Clicks", x + 0.25 * cm, y + 1.20 * cm - detail_shift, 7.5, MUTED, True)
        label(c, format_int(row["Clicks"]), x + 0.25 * cm, y + 0.75 * cm - detail_shift, 15, NAVY_TEXT, True)
        label(c, format_growth(row.get("Clicks Growth", "")), x + card_w - 0.25 * cm, y + 0.81 * cm - detail_shift, 7, growth_color(row.get("Clicks Growth", "")), False, "right")
        rounded_rect(c, x + 0.25 * cm, y + 0.43 * cm - detail_shift, card_w - 0.5 * cm, 0.15 * cm, LIGHT_BAR, None, 3)
        rounded_rect(c, x + 0.25 * cm, y + 0.43 * cm - detail_shift, (card_w - 0.5 * cm) * float(row["Clicks"]) / max_clicks, 0.15 * cm, GOLD, None, 3)

    label(c, "Source: live Google Sheets tabs", W - 1.2 * cm, 0.35 * cm, 7, MUTED, False, "right")
    c.showPage()


def draw_page3(c: canvas.Canvas, data: dict[str, object]) -> None:
    W, H = get_canvas_dimensions()
    posts = data["news_posts"]  # type: ignore[assignment]
    draw_header(c, W, H, "News Post Impact", "Latest posts during period.")

    if posts.empty:
        label(c, "No post details available.", 1.2 * cm, H - 4.0 * cm, 12, NAVY_TEXT, True)
        label(c, "Source: live Google Sheets tabs", W - 1.2 * cm, 0.35 * cm, 7, MUTED, False, "right")
        c.showPage()
        return

    rows = math.ceil(len(posts) / 3)
    cols = 3
    gap_x = 0.28 * cm
    gap_y = 0.24 * cm
    start_x = 1.2 * cm
    top_y = H - 3.85 * cm
    footer_space = 0.8 * cm
    available_height = top_y - footer_space
    card_h = min(3.05 * cm, max(2.35 * cm, (available_height - (rows - 1) * gap_y) / rows))
    card_w = (W - 2.4 * cm - (cols - 1) * gap_x) / cols

    for idx, (_, row) in enumerate(posts.iterrows()):
        col = idx % cols
        row_idx = idx // cols
        x = start_x + col * (card_w + gap_x)
        y = top_y - (row_idx + 1) * card_h - row_idx * gap_y
        impact = derive_impact(row)
        impact_fill = {"Higher": GREEN, "Lower": RED, "Flat": GREY}.get(impact, GREY)
        impact_text = impact if impact != "No data" else "No data"
        rounded_rect(c, x, y, card_w, card_h, CARD_BG, CARD_BORDER, 8)
        c.setFillColor(HEADER_COLOR)
        c.roundRect(x, y + card_h - 0.62 * cm, card_w, 0.62 * cm, 8, fill=1, stroke=0)
        label(c, str(row["School"]), x + 0.18 * cm, y + card_h - 0.43 * cm, 9.5, colors.white, True)
        pill_w = 1.45 * cm if impact_text != "No data" else 1.65 * cm
        rounded_rect(c, x + card_w - pill_w - 0.18 * cm, y + card_h - 0.50 * cm, pill_w, 0.30 * cm, impact_fill, None, 6)
        label(c, impact_text, x + card_w - 0.18 * cm, y + card_h - 0.40 * cm, 7, colors.white, True, "right")

        title = display_text(row["Post title"])
        title_y = y + card_h - 0.82 * cm
        title_y = draw_wrapped_text(c, title, x + 0.18 * cm, title_y, card_w - 0.36 * cm, 8.2, NAVY_TEXT, True, 9.0)

        metric_top = title_y - 0.10 * cm
        left_x = x + 0.18 * cm
        right_x = x + card_w / 2 + 0.08 * cm
        metric_pairs = [
            ("Post date", display_text(row["Post date"])),
            ("Views on post day", format_decimal(row["Views on post day"])),
            ("3 days before", format_decimal(row["3 days before"])),
            ("3 days after", format_decimal(row["3 days after"])),
            ("Change", format_growth(row["Change"])),
        ]

        # Left column
        label(c, metric_pairs[0][0], left_x, metric_top - 0.18 * cm, 6.8, MUTED, True)
        label(c, metric_pairs[0][1], left_x, metric_top - 0.48 * cm, 8.2, NAVY_TEXT, True)
        label(c, metric_pairs[2][0], left_x, metric_top - 1.02 * cm, 6.8, MUTED, True)
        label(c, metric_pairs[2][1], left_x, metric_top - 1.32 * cm, 8.2, NAVY_TEXT, True)
        label(c, metric_pairs[4][0], left_x, metric_top - 1.86 * cm, 6.8, MUTED, True)
        label(c, metric_pairs[4][1], left_x, metric_top - 2.16 * cm, 8.0, growth_color(row["Change"]), True)

        # Right column
        label(c, metric_pairs[1][0], right_x, metric_top - 0.18 * cm, 6.8, MUTED, True)
        label(c, metric_pairs[1][1], right_x, metric_top - 0.48 * cm, 8.2, NAVY_TEXT, True)
        label(c, metric_pairs[3][0], right_x, metric_top - 1.02 * cm, 6.8, MUTED, True)
        label(c, metric_pairs[3][1], right_x, metric_top - 1.32 * cm, 8.2, NAVY_TEXT, True)
        label(c, "Impact", right_x, metric_top - 1.86 * cm, 6.8, MUTED, True)
        label(c, impact_text, right_x, metric_top - 2.16 * cm, 8.0, colors.white, True)

    label(c, "Source: live Google Sheets tabs", W - 1.2 * cm, 0.35 * cm, 7, MUTED, False, "right")
    c.showPage()


def build_report_data() -> dict[str, object]:
    schools, period = load_views_clicks()
    posts = load_post_details()
    return {
        "title": "Schools Website Activity Report",
        "period": period,
        "source_label": "live Google Sheets tabs",
        "schools": schools,
        "news_posts": posts,
    }


def validate_pdf(pdf_path: Path, expected_pages: int = 3) -> None:
    page_count_cmd = [
        "gs",
        "-q",
        "-dNOSAFER",
        "-dNODISPLAY",
        "-c",
        f"({pdf_path}) (r) file runpdfbegin pdfpagecount = quit",
    ]
    result = subprocess.run(page_count_cmd, capture_output=True, text=True, check=True)
    page_count_text = result.stdout.strip()
    if not page_count_text.isdigit():
        raise ValueError(f"Could not determine page count for {pdf_path}")
    page_count = int(page_count_text)
    if page_count != expected_pages:
        raise ValueError(f"Expected {expected_pages} pages, got {page_count}")

    with tempfile.TemporaryDirectory(prefix="website-report-check-") as tmpdir:
        output_pattern = str(Path(tmpdir) / "page-%d.png")
        render_cmd = [
            "gs",
            "-q",
            "-dNOSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=pngalpha",
            "-r144",
            "-dFirstPage=1",
            f"-dLastPage={expected_pages}",
            f"-sOutputFile={output_pattern}",
            str(pdf_path),
        ]
        subprocess.run(render_cmd, check=True)

        for page in range(1, expected_pages + 1):
            page_image = Path(tmpdir) / f"page-{page}.png"
            if not page_image.exists():
                raise ValueError(f"Missing render output for page {page}")
            img = PILImage.open(page_image).convert("RGB")
            bbox = ImageChops.difference(img, PILImage.new("RGB", img.size, (255, 255, 255))).getbbox()
            if bbox is None:
                raise ValueError(f"Page {page} appears blank after rendering")


def main() -> int:
    try:
        report_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_pdf = Path(f"{OUTPUT_PREFIX}_{report_timestamp}.pdf")
        data = build_report_data()

        c = canvas.Canvas(str(output_pdf), pagesize=landscape(letter))
        draw_page1(c, data)
        draw_page2(c, data)
        draw_page3(c, data)
        c.save()

        validate_pdf(output_pdf, expected_pages=3)
        print(f"Report generated: {output_pdf}")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
