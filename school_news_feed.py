#!/usr/bin/env python3
"""Fetch DES school news feeds and write a dated latest-post summary per school."""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import shutil
import subprocess

from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class School:
    name: str
    slug: str
    feed_path: str | None = None

    @property
    def feed_slug(self) -> str:
        return self.feed_path or self.slug


SCHOOLS = [
    School("CHHS", "chhs"),
    School("CIFEC", "cifec"),
    School("EEPS", "eeps"),
    School("EMMPS", "emmps"),
    School("JACPS", "jacps"),
    School("JCPS", "jcps"),
    School("JGHS", "jghs"),
    School("LHS", "lhs"),
    School("LSHS", "lshs"),
    School("MMPS", "mmps"),
    School("PPPS", "pps"),
    School("RBPS", "rbps"),
    School("CSBS", "csbs", "csbs/csbs-news"),
    School("TMPS", "tmps"),
    School("WEPS", "weps"),
]

GA4_SCHOOL_ALIASES = {
    "EMMPS": "EMPS",
    "LHS": "LHSS",
    "CSBS": "SBPS",
}

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = BASE_DIR / "last_seen_posts.json"
USER_AGENT = "Mozilla/5.0 (compatible; WebsiteMonitor/1.0)"


def load_state(state_file: Path) -> dict[str, list[str]]:
    if not state_file.exists():
        return {}

    try:
        with state_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    state: dict[str, list[str]] = {}
    for school, seen_ids in data.items():
        if isinstance(seen_ids, list):
            state[str(school)] = [str(item) for item in seen_ids if item]
    return state


def save_state(state_file: Path, state: dict[str, list[str]]) -> None:
    with state_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def write_csv(csv_file: Path, rows: list[dict[str, str]], updated_on: str) -> None:
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    with csv_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["school", "ga4 school", "title", "last date", "link"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        writer.writerow(
            {
                "school": f"Updated on {updated_on}",
                "ga4 school": "",
                "title": "",
                "last date": "",
                "link": "",
            }
        )


def default_csv_output_path() -> Path:
    date_stamp = datetime.now().strftime("%d-%b-%y")
    return Path(f"news_posts_dated_{date_stamp}.csv")


def download_url(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urlopen(request, timeout=20) as response:
            return response.read()
    except HTTPError as exc:
        if exc.code != 403 or shutil.which("curl") is None:
            raise

    result = subprocess.run(
        ["curl", "-L", "-A", USER_AGENT, "--max-time", "20", url],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout:
        raise ValueError(f"Failed to fetch {url} with curl fallback")
    return result.stdout


def normalize_csbs_date(date_text: str) -> str:
    """Convert CSBS archive dates into the same RFC-style format as the feeds."""
    if not date_text or date_text == "No date":
        return "No date"

    parsed_formats = (
        "%B %d, %Y",
        "%b %d, %Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
    )
    for date_format in parsed_formats:
        try:
            parsed = datetime.strptime(date_text, date_format)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    return date_text


def ga4_school_name(school_name: str) -> str:
    """Return the school label used by the GA4 report."""
    return GA4_SCHOOL_ALIASES.get(school_name, school_name)


def fetch_feed(feed_slug: str) -> tuple[list[dict[str, str]], str]:
    url = f"https://schools.edu.ky/{feed_slug}/feed/"
    data = download_url(url)

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"Failed to parse feed XML: {exc}") from exc

    entries: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        post_id = item.findtext("guid") or item.findtext("link")
        if not post_id:
            continue

        entries.append(
            {
                "id": str(post_id),
                "title": str(item.findtext("title") or "No title"),
                "link": str(item.findtext("link") or ""),
                "date": str(item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or "No date"),
            }
        )

    return entries, url


def fetch_csbs_archive() -> tuple[list[dict[str, str]], str]:
    url = "https://schools.edu.ky/csbs/csbs-news/"
    html = download_url(url).decode("utf-8", errors="replace")

    entries: list[dict[str, str]] = []
    for block in re.findall(r'(<article class="elementor-post.*?</article>)', html, flags=re.S):
        link_match = re.search(
            r'href="(https://schools\.edu\.ky/csbs/[^"]+)"',
            block,
        )
        title_match = re.search(
            r'<h3 class="elementor-post__title">\s*<a[^>]*>\s*(.*?)\s*</a>',
            block,
            flags=re.S,
        )
        date_match = re.search(
            r'<span class="elementor-post-date">\s*(.*?)\s*</span>',
            block,
            flags=re.S,
        )

        if not link_match or not title_match:
            continue

        entries.append(
            {
                "id": link_match.group(1),
                "title": unescape(re.sub(r"<[^>]+>", "", title_match.group(1)).strip()),
                "link": link_match.group(1),
                "date": normalize_csbs_date(
                    unescape(re.sub(r"<[^>]+>", "", date_match.group(1)).strip()) if date_match else "No date"
                ),
            }
        )

    return entries, url


def check_feeds(state_file: Path, baseline: bool = False) -> tuple[list[dict[str, str]], list[str]]:
    state = load_state(state_file)
    summary_rows: list[dict[str, str]] = []
    errors: list[str] = []
    next_state: dict[str, list[str]] = dict(state)

    for school in SCHOOLS:
        try:
            entries, feed_url = fetch_feed(school.feed_slug)
        except (HTTPError, URLError, ValueError) as exc:
            if school.name == "CSBS":
                try:
                    entries, feed_url = fetch_csbs_archive()
                except (HTTPError, URLError, ValueError) as archive_exc:
                    errors.append(f"{school.name} ({school.feed_slug}): {archive_exc}")
                    summary_rows.append(
                        {
                            "school": school.name,
                            "ga4 school": ga4_school_name(school.name),
                            "title": "",
                            "last date": "",
                            "link": "",
                        }
                    )
                    continue
            else:
                errors.append(f"{school.name} ({school.feed_slug}): {exc}")
                summary_rows.append(
                    {
                        "school": school.name,
                        "ga4 school": ga4_school_name(school.name),
                        "title": "",
                        "last date": "",
                        "link": "",
                    }
                )
                continue

        if not entries:
            if school.name == "CSBS":
                try:
                    entries, feed_url = fetch_csbs_archive()
                except (HTTPError, URLError, ValueError) as archive_exc:
                    errors.append(f"{school.name} ({school.feed_slug}): {archive_exc}")
                    summary_rows.append(
                        {
                            "school": school.name,
                            "ga4 school": ga4_school_name(school.name),
                            "title": "",
                            "last date": "",
                            "link": "",
                        }
                    )
                    continue
            else:
                errors.append(f"{school.name} ({school.feed_slug}): no posts found at {feed_url}")
                summary_rows.append(
                    {
                        "school": school.name,
                        "ga4 school": ga4_school_name(school.name),
                        "title": "",
                        "last date": "",
                        "link": "",
                    }
                )
                continue

        if not entries:
            errors.append(f"{school.name} ({school.feed_slug}): no posts found at {feed_url}")
            summary_rows.append(
                {
                    "school": school.name,
                    "ga4 school": ga4_school_name(school.name),
                    "title": "",
                    "last date": "",
                    "link": "",
                }
            )
            continue

        current_ids = [entry["id"] for entry in entries]
        next_state[school.name] = current_ids
        latest_entry = entries[0]
        summary_rows.append(
            {
                "school": school.name,
                "ga4 school": ga4_school_name(school.name),
                "title": latest_entry["title"],
                "last date": latest_entry["date"],
                "link": latest_entry["link"],
            }
        )

    save_state(state_file, next_state)
    return summary_rows, errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track DES school news feed changes.")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Retained for compatibility; summary output is unchanged.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="Path to the JSON file used to store the last seen post IDs.",
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        default=None,
        help="Optional path to write the collected posts as CSV.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, errors = check_feeds(args.state_file, baseline=args.baseline)
    updated_on = datetime.now().strftime("%d-%b-%y")

    csv_output = args.csv_output or default_csv_output_path()
    write_csv(csv_output, rows, updated_on)

    print(f"Wrote school news summary to {csv_output}")
    print(f"Updated on {updated_on}")

    if errors:
        print("\nErrors:\n")
        for error in errors:
            print(f"- {error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
