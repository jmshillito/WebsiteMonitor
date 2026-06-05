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
from urllib.parse import urljoin
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
INVALID_XML_CHARS = re.compile(
    "["
    "\x00-\x08"
    "\x0b"
    "\x0c"
    "\x0e-\x1f"
    "]"
)


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


def sanitize_xml_text(text: str) -> str:
    text = INVALID_XML_CHARS.sub("", text)
    text = text.replace("\ufeff", "")
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[A-Za-z][A-Za-z0-9]+;)", "&amp;", text)
    return text


def clean_html_text(value: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", value).strip())


def strip_html_keep_spacing(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?i)</h[1-6]\s*>", "\n", text)
    text = re.sub(r"(?i)</li\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def archive_url_for_slug(feed_slug: str) -> str:
    school_slug = feed_slug.split("/", 1)[0]
    return f"https://schools.edu.ky/{school_slug}/{school_slug}-news/"


def month_archive_url_for_slug(feed_slug: str, when: datetime | None = None) -> str:
    school_slug = feed_slug.split("/", 1)[0]
    moment = when or datetime.now()
    return f"https://schools.edu.ky/{school_slug}/{moment:%Y/%m}/"


def blog_category_url(category_slug: str) -> str:
    return f"https://schools.edu.ky/blog/category/{category_slug}/"


def blog_month_url(when: datetime | None = None) -> str:
    moment = when or datetime.now()
    return f"https://schools.edu.ky/blog/{moment:%Y/%m}/"


def category_feed_url(category_slug: str, school_slug: str) -> list[str]:
    return [
        f"https://schools.edu.ky/{school_slug}/category/{category_slug}/feed/",
        f"https://schools.edu.ky/blog/category/{category_slug}/feed/",
    ]


def parse_rss_items(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for block in re.findall(r"<item\b.*?</item>", text, flags=re.S | re.I):
        id_match = re.search(r"<guid[^>]*>(.*?)</guid>", block, flags=re.S | re.I)
        if not id_match:
            id_match = re.search(r"<link[^>]*>(.*?)</link>", block, flags=re.S | re.I)
        if not id_match:
            continue

        title_match = re.search(r"<title[^>]*>(.*?)</title>", block, flags=re.S | re.I)
        link_match = re.search(r"<link[^>]*>(.*?)</link>", block, flags=re.S | re.I)
        date_match = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", block, flags=re.S | re.I)
        if not link_match:
            continue

        entries.append(
            {
                "id": clean_html_text(id_match.group(1)),
                "title": clean_html_text(title_match.group(1)) if title_match else "No title",
                "link": clean_html_text(link_match.group(1)),
                "date": clean_html_text(date_match.group(1)) if date_match else "No date",
            }
        )

    return entries


def parse_elementor_news_page(html: str, url: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for block in re.findall(r'(<article\b[^>]*class="[^"]*elementor-post[^"]*"[^>]*>.*?</article>)', html, flags=re.S | re.I):
        link_match = re.search(r'href=[\"\'](https://schools\.edu\.ky/[^\"\']+)[\"\']', block, flags=re.I)
        if not link_match:
            link_match = re.search(r'href=[\"\']([^\"\']+)[\"\']', block, flags=re.I)
        if not link_match:
            continue

        link = urljoin(url, link_match.group(1))
        if not link.startswith("https://schools.edu.ky/"):
            continue

        title_match = re.search(
            r'class="[^"]*elementor-post__title[^"]*"[^>]*>\s*<a[^>]*>\s*(.*?)\s*</a>',
            block,
            flags=re.S | re.I,
        )
        if not title_match:
            title_match = re.search(r'<a[^>]*href="[^"]+"[^>]*>\s*(.*?)\s*</a>', block, flags=re.S | re.I)

        date_match = re.search(
            r'class="[^"]*elementor-post-date[^"]*"[^>]*>\s*(.*?)\s*</span>',
            block,
            flags=re.S | re.I,
        )

        title = clean_html_text(title_match.group(1)) if title_match else "No title"
        if not title:
            continue

        entries.append(
            {
                "id": link,
                "title": title,
                "link": link,
                "date": normalize_csbs_date(clean_html_text(date_match.group(1))) if date_match else "No date",
            }
        )

    return entries


def parse_news_listing_page(html: str, url: str, school_slug: str) -> list[dict[str, str]]:
    prefix = f"https://schools.edu.ky/{school_slug}/"
    seen: set[str] = set()
    entries: list[dict[str, str]] = []
    skip_markers = (
        f"/{school_slug}/category/",
        f"/{school_slug}/feed/",
        f"/{school_slug}/page/",
        f"/{school_slug}/tag/",
        f"/{school_slug}/author/",
        f"/{school_slug}/wp-json/",
    )
    date_pattern = re.compile(r"(?:Published\s*)?(\d{1,2}[/-]\d{1,2}[/-]\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})")
    title_pattern = re.compile(r"<a[^>]*href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>", flags=re.S | re.I)

    for match in title_pattern.finditer(html):
        raw_link = unescape(match.group(1))
        link = urljoin(url, raw_link)
        if not link.startswith(prefix):
            continue
        if any(marker in link for marker in skip_markers):
            continue
        if link in seen:
            continue

        title_html = match.group(2)
        title = clean_html_text(title_html)
        if not title:
            continue
        lowered = title.lower()
        if lowered in {"read more", "read more »", "read more >", "more", "home"}:
            continue

        context_start = max(0, match.start() - 600)
        context_end = min(len(html), match.end() + 900)
        context = html[context_start:context_end]
        date_match = date_pattern.search(context)
        date_text = date_match.group(1) if date_match else "No date"

        entries.append(
            {
                "id": link,
                "title": title,
                "link": link,
                "date": normalize_csbs_date(date_text),
            }
        )
        seen.add(link)

    if entries:
        entries.sort(key=lambda row: row["date"], reverse=True)
        return entries

    text = strip_html_keep_spacing(html)
    text_lines = [line.strip() for line in text.splitlines() if line.strip()]
    for idx, line in enumerate(text_lines):
        date_match = date_pattern.search(line)
        if not date_match:
            continue
        date_text = date_match.group(1)
        title = ""
        for back in range(idx - 1, max(-1, idx - 21), -1):
            candidate = text_lines[back].strip(" #\t\r\n-–—")
            lowered = candidate.lower()
            if lowered in {"read more", "no comments"}:
                continue
            if date_pattern.search(candidate):
                continue
            if lowered.startswith(("published ", "news from", "visit ", "home", "search")):
                continue
            if len(candidate) < 3:
                continue
            title = candidate
            break

        if not title:
            for forward in range(idx + 1, min(len(text_lines), idx + 6)):
                candidate = text_lines[forward].strip(" #\t\r\n-–—")
                lowered = candidate.lower()
                if lowered in {"read more", "no comments"}:
                    continue
                if date_pattern.search(candidate):
                    continue
                if lowered.startswith(("published ", "news from", "visit ", "home", "search")):
                    continue
                if len(candidate) < 3:
                    continue
                title = candidate
                break

        if not title:
            continue

        slug_hint = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        if not slug_hint:
            continue
        link = f"{prefix}{slug_hint}/"
        if link in seen:
            continue
        entries.append(
            {
                "id": link,
                "title": title,
                "link": link,
                "date": normalize_csbs_date(date_text),
            }
        )
        seen.add(link)

    entries.sort(key=lambda row: row["date"], reverse=True)
    return entries


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


def normalize_wp_date(date_text: str) -> str:
    if not date_text:
        return "No date"

    candidates = [date_text, date_text.replace("Z", "+00:00")]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    return date_text


def parse_meta_content(html: str, names: list[str]) -> str:
    for name in names:
        pattern = re.compile(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
            flags=re.I,
        )
        match = pattern.search(html)
        if match:
            return clean_html_text(match.group(1))
    return ""


def parse_page_title(html: str) -> str:
    meta_title = parse_meta_content(html, ["og:title", "twitter:title"])
    if meta_title:
        return meta_title

    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
    if match:
        return clean_html_text(match.group(1))

    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.S | re.I)
    if match:
        return clean_html_text(match.group(1))

    return ""


def parse_page_date(html: str) -> str:
    meta_date = parse_meta_content(
        html,
        [
            "article:published_time",
            "article:modified_time",
            "og:updated_time",
            "date",
        ],
    )
    if meta_date:
        return normalize_wp_date(meta_date)

    match = re.search(
        r"(?:Published\s*)?(\d{1,2}[/-]\d{1,2}[/-]\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})",
        html,
        flags=re.I,
    )
    if match:
        return normalize_csbs_date(match.group(1))

    return "No date"


def fetch_wordpress_sitemap_posts(school_slug: str) -> tuple[list[dict[str, str]], str]:
    sitemap_index_url = "https://schools.edu.ky/wp-sitemap.xml"
    sitemap_index_text = download_url(sitemap_index_url).decode("utf-8", errors="replace")

    sitemap_urls = re.findall(r"<loc>([^<]+)</loc>", sitemap_index_text, flags=re.I)
    post_sitemap_urls = [url for url in sitemap_urls if "post" in url and url.endswith(".xml")]
    if not post_sitemap_urls:
        post_sitemap_urls = [sitemap_index_url]

    prefix = f"https://schools.edu.ky/{school_slug}/"
    excluded_parts = {
        f"/{school_slug}/category/",
        f"/{school_slug}/feed/",
        f"/{school_slug}/page/",
        f"/{school_slug}/tag/",
        f"/{school_slug}/author/",
        f"/{school_slug}/wp-json/",
        f"/{school_slug}/newsletters/",
        f"/{school_slug}/our-history/",
        f"/{school_slug}/contact/",
        f"/{school_slug}/school-policies/",
        f"/{school_slug}/important-documents/",
    }

    entries: list[dict[str, str]] = []
    seen: set[str] = set()

    for sitemap_url in post_sitemap_urls:
        try:
            sitemap_text = download_url(sitemap_url).decode("utf-8", errors="replace")
        except (HTTPError, URLError, ValueError):
            continue

        for post_url in re.findall(r"<loc>([^<]+)</loc>", sitemap_text, flags=re.I):
            if not post_url.startswith(prefix):
                continue
            if any(part in post_url for part in excluded_parts):
                continue
            if post_url in seen:
                continue

            try:
                page_html = download_url(post_url).decode("utf-8", errors="replace")
            except (HTTPError, URLError, ValueError):
                continue

            title = parse_page_title(page_html)
            if not title:
                slug = post_url.rstrip("/").rsplit("/", 1)[-1]
                title = clean_html_text(slug.replace("-", " ").title())

            if not title:
                continue

            entries.append(
                {
                    "id": post_url,
                    "title": title,
                    "link": post_url,
                    "date": parse_page_date(page_html),
                }
            )
            seen.add(post_url)

    entries.sort(key=lambda row: row["date"], reverse=True)
    return entries, sitemap_index_url


def fetch_wordpress_rest_posts(
    school_slug: str,
    category_slugs: list[str] | None = None,
    allowed_prefixes: list[str] | None = None,
) -> tuple[list[dict[str, str]], str]:
    endpoint_candidates = [
        f"https://schools.edu.ky/{school_slug}/wp-json/wp/v2/posts?per_page=100&orderby=date&order=desc&_fields=date,link,slug,title,categories",
        f"https://schools.edu.ky/wp-json/wp/v2/posts?per_page=100&orderby=date&order=desc&_fields=date,link,slug,title,categories",
    ]
    prefixes = allowed_prefixes or [f"https://schools.edu.ky/{school_slug}/"]

    for url in endpoint_candidates:
        try:
            payload = json.loads(download_url(url).decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, HTTPError, URLError, ValueError):
            continue

        if not isinstance(payload, list):
            continue

        category_filter_ids: set[int] = set()
        if category_slugs:
            for category_slug in category_slugs:
                for category_url in (
                    f"https://schools.edu.ky/{school_slug}/wp-json/wp/v2/categories?slug={category_slug}&per_page=100",
                    f"https://schools.edu.ky/wp-json/wp/v2/categories?slug={category_slug}&per_page=100",
                ):
                    try:
                        category_payload = json.loads(download_url(category_url).decode("utf-8", errors="replace"))
                    except (json.JSONDecodeError, HTTPError, URLError, ValueError):
                        continue
                    if isinstance(category_payload, list) and category_payload:
                        category_id = category_payload[0].get("id")
                        if isinstance(category_id, int):
                            category_filter_ids.add(category_id)
                            break

        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for post in payload:
            if not isinstance(post, dict):
                continue
            link = str(post.get("link") or "")
            if not any(link.startswith(prefix) for prefix in prefixes):
                continue
            if category_filter_ids:
                post_categories = post.get("categories")
                if not isinstance(post_categories, list) or not any(int(cat) in category_filter_ids for cat in post_categories if isinstance(cat, int)):
                    continue
            if link in seen:
                continue

            title_data = post.get("title")
            if isinstance(title_data, dict):
                title = clean_html_text(str(title_data.get("rendered", "")))
            else:
                title = clean_html_text(str(title_data or ""))
            if not title:
                slug = link.rstrip("/").rsplit("/", 1)[-1]
                title = clean_html_text(slug.replace("-", " ").title())
            if not title:
                continue

            entries.append(
                {
                    "id": link,
                    "title": title,
                    "link": link,
                    "date": normalize_wp_date(str(post.get("date") or "No date")),
                }
            )
            seen.add(link)

        if entries:
            entries.sort(key=lambda row: row["date"], reverse=True)
            return entries, url

    return [], endpoint_candidates[0]


def fetch_school_rest_posts_from_categories(
    school_slug: str,
    category_slugs: list[str],
    allowed_prefixes: list[str],
) -> tuple[list[dict[str, str]], str]:
    base_candidates = [
        f"https://schools.edu.ky/{school_slug}/wp-json/wp/v2",
        "https://schools.edu.ky/wp-json/wp/v2",
    ]
    prefix_set = allowed_prefixes

    for base in base_candidates:
        category_ids: set[int] = set()
        for category_slug in category_slugs:
            try:
                category_payload = json.loads(
                    download_url(f"{base}/categories?slug={category_slug}&per_page=100").decode("utf-8", errors="replace")
                )
            except (json.JSONDecodeError, HTTPError, URLError, ValueError):
                continue
            if isinstance(category_payload, list) and category_payload:
                category_id = category_payload[0].get("id")
                if isinstance(category_id, int):
                    category_ids.add(category_id)

        if not category_ids:
            continue

        try:
            payload = json.loads(
                download_url(
                    f"{base}/posts?per_page=100&orderby=date&order=desc&_fields=date,link,slug,title,categories"
                ).decode("utf-8", errors="replace")
            )
        except (json.JSONDecodeError, HTTPError, URLError, ValueError):
            continue

        if not isinstance(payload, list):
            continue

        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for post in payload:
            if not isinstance(post, dict):
                continue
            link = str(post.get("link") or "")
            if not link or not any(link.startswith(prefix) for prefix in prefix_set):
                continue
            post_categories = post.get("categories")
            if not isinstance(post_categories, list) or not any(isinstance(cat, int) and cat in category_ids for cat in post_categories):
                continue
            if link in seen:
                continue
            title_data = post.get("title")
            if isinstance(title_data, dict):
                title = clean_html_text(str(title_data.get("rendered", "")))
            else:
                title = clean_html_text(str(title_data or ""))
            if not title:
                slug = link.rstrip("/").rsplit("/", 1)[-1]
                title = clean_html_text(slug.replace("-", " ").title())
            if not title:
                continue
            entries.append(
                {
                    "id": link,
                    "title": title,
                    "link": link,
                    "date": normalize_wp_date(str(post.get("date") or "No date")),
                }
            )
            seen.add(link)

        if entries:
            entries.sort(key=lambda row: row["date"], reverse=True)
            return entries, base

    return [], base_candidates[0]


def fetch_blog_category_posts(category_slug: str, school_slug: str, link_prefix: str | None = None) -> tuple[list[dict[str, str]], str]:
    url_candidates = [
        blog_category_url(category_slug),
        blog_category_url(school_slug),
        blog_month_url(),
    ]
    prefix = link_prefix or f"https://schools.edu.ky/{school_slug}/"
    for url in url_candidates:
        try:
            html = download_url(url).decode("utf-8", errors="replace")
        except (HTTPError, URLError, ValueError):
            continue
        entries = [entry for entry in parse_news_listing_page(html, url, school_slug) if entry["link"].startswith(prefix)]
        if entries:
            return entries, url
    return [], url_candidates[0]


def fetch_category_feed_posts(category_slugs: list[str], school_slug: str, link_prefixes: list[str]) -> tuple[list[dict[str, str]], str]:
    for category_slug in category_slugs:
        for url in category_feed_url(category_slug, school_slug):
            try:
                entries, _ = fetch_feed_from_url(url)
            except Exception:
                continue
            if entries:
                if link_prefixes:
                    filtered = [entry for entry in entries if any(entry["link"].startswith(prefix) for prefix in link_prefixes)]
                    if filtered:
                        return filtered, url
                else:
                    return entries, url
    return [], category_feed_url(category_slugs[0], school_slug)[0]


def ga4_school_name(school_name: str) -> str:
    """Return the school label used by the GA4 report."""
    return GA4_SCHOOL_ALIASES.get(school_name, school_name)


def fetch_feed(feed_slug: str) -> tuple[list[dict[str, str]], str]:
    url = f"https://schools.edu.ky/{feed_slug}/feed/"
    return fetch_feed_from_url(url)


def fetch_feed_from_url(url: str) -> tuple[list[dict[str, str]], str]:
    data = download_url(url)
    text = data.decode("utf-8", errors="replace")
    cleaned_text = sanitize_xml_text(text)

    try:
        root = ET.fromstring(cleaned_text)
    except ET.ParseError as exc:
        entries = parse_rss_items(cleaned_text)
        if entries:
            return entries, url
        return [], url

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
    entries = [entry for entry in parse_news_listing_page(html, url, "csbs") if entry["link"].startswith("https://schools.edu.ky/csbs/")]
    return entries, url


def fetch_wordpress_category_posts(category_slug_candidates: list[str]) -> tuple[list[dict[str, str]], str]:
    base = "https://schools.edu.ky/wp-json/wp/v2"

    for category_slug in category_slug_candidates:
        category_url = f"{base}/categories?slug={category_slug}&per_page=100"
        try:
            category_payload = json.loads(download_url(category_url).decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, HTTPError, URLError, ValueError):
            continue

        if not isinstance(category_payload, list) or not category_payload:
            continue

        category_id = category_payload[0].get("id")
        if category_id is None:
            continue

        posts_url = (
            f"{base}/posts?categories={category_id}"
            "&per_page=100&orderby=date&order=desc&_fields=date,link,slug,title"
        )
        try:
            posts_payload = json.loads(download_url(posts_url).decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, HTTPError, URLError, ValueError):
            continue

        if not isinstance(posts_payload, list):
            continue

        entries: list[dict[str, str]] = []
        for post in posts_payload:
            if not isinstance(post, dict):
                continue
            title_data = post.get("title")
            if isinstance(title_data, dict):
                title = clean_html_text(str(title_data.get("rendered", "")))
            else:
                title = clean_html_text(str(title_data or ""))
            link = str(post.get("link") or "")
            date_text = str(post.get("date") or "No date")
            if not title or not link:
                continue
            entries.append(
                {
                    "id": link,
                    "title": title,
                    "link": link,
                    "date": normalize_wp_date(date_text),
                }
            )

        if entries:
            return entries, posts_url

    return [], ""


def fetch_school_archive(feed_slug: str) -> tuple[list[dict[str, str]], str]:
    school_slug = feed_slug.split("/", 1)[0]
    category_candidates = [school_slug]
    if school_slug == "emmps":
        category_candidates.insert(0, "emps")

    url_candidates = [month_archive_url_for_slug(feed_slug)]
    url_candidates.extend(f"https://schools.edu.ky/{school_slug}/category/{category_slug}/" for category_slug in category_candidates)
    url_candidates.append(archive_url_for_slug(feed_slug))
    url_candidates.append(f"https://schools.edu.ky/{school_slug}/")

    prefix = f"https://schools.edu.ky/{school_slug}/"
    for url in url_candidates:
        html = download_url(url).decode("utf-8", errors="replace")
        entries = [entry for entry in parse_news_listing_page(html, url, school_slug) if entry["link"].startswith(prefix)]
        if entries:
            return entries, url

    return [], url_candidates[0]


def check_feeds(state_file: Path, baseline: bool = False) -> tuple[list[dict[str, str]], list[str]]:
    state = load_state(state_file)
    summary_rows: list[dict[str, str]] = []
    errors: list[str] = []
    next_state: dict[str, list[str]] = dict(state)

    for school in SCHOOLS:
        try:
            school_slug = school.feed_slug.split("/", 1)[0]
            if school.name == "JGHS":
                entries, feed_url = fetch_category_feed_posts(
                    ["jghs", "headline-news"],
                    school_slug,
                    [],
                )
            elif school.name == "CHHS":
                entries, feed_url = fetch_wordpress_rest_posts(
                    school_slug,
                    ["chhs", "headline-news"],
                    ["https://schools.edu.ky/blog/", "https://schools.edu.ky/chhs/"],
                )
            else:
                entries, feed_url = fetch_wordpress_rest_posts(school_slug)

            if not entries:
                entries, feed_url = fetch_wordpress_sitemap_posts(school_slug)

            if not entries:
                blog_category_candidates = {
                    "CHHS": "chhs",
                    "CIFEC": "cifec",
                    "EEPS": "eeps",
                    "EMMPS": "emmps",
                    "JACPS": "jacps",
                    "JCPS": "jcps",
                    "JGHS": "jghs",
                    "LHS": "lhs",
                    "LSHS": "lshs",
                    "MMPS": "mmps",
                    "PPPS": "pps",
                    "RBPS": "rbps",
                    "CSBS": "csbs",
                    "TMPS": "tmps",
                    "WEPS": "weps",
                }
                blog_category = blog_category_candidates.get(school.name)
                if blog_category:
                    if school.name == "JGHS":
                        entries, feed_url = fetch_blog_category_posts(blog_category, school.slug, "https://schools.edu.ky/blog/")
                    else:
                        entries, feed_url = fetch_blog_category_posts(blog_category, school.slug)

            if not entries:
                category_candidates = [school.slug]
                if school.name == "EMMPS":
                    category_candidates.insert(0, "emps")
                elif school.name == "LHS":
                    category_candidates.insert(0, "lhs")
                elif school.name == "PPPS":
                    category_candidates.insert(0, "pps")
                elif school.name == "CSBS":
                    category_candidates.insert(0, "csbs")

                entries, feed_url = fetch_wordpress_category_posts(category_candidates)

            if not entries:
                entries, feed_url = fetch_feed(school.feed_slug)

            if not entries:
                try:
                    if school.name == "CSBS":
                        entries, feed_url = fetch_csbs_archive()
                    else:
                        entries, feed_url = fetch_school_archive(school.feed_slug)
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
        except (HTTPError, URLError, ValueError) as exc:
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
