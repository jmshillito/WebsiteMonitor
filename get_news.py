import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

import requests

OUTPUT_FILE = "/mnt/c/Users/john.shillito/Downloads/WEBSites/REPORTING/NewsPosts.csv"
USER_AGENT = "Mozilla/5.0 NewsPostsReporter/2.0"
REQUEST_TIMEOUT = 30
POSTS_PER_PAGE = 100


@dataclass(frozen=True)
class SchoolSource:
    name: str
    slug: str
    aliases: tuple[str, ...] = ()
    feed_path: str | None = None
    extra_prefixes: tuple[str, ...] = ()

    @property
    def rest_prefixes(self) -> tuple[str, ...]:
        prefixes = (f"https://schools.edu.ky/{self.slug}/",)
        if self.extra_prefixes:
            prefixes += self.extra_prefixes
        return prefixes

    @property
    def feed_slug(self) -> str:
        return self.feed_path or self.slug


SCHOOL_SOURCES = (
    SchoolSource("CHHS", "chhs", aliases=("chhs",), extra_prefixes=("https://schools.edu.ky/blog/",)),
    SchoolSource("CIFEC", "cifec", aliases=("cifec",)),
    SchoolSource("EEPS", "eeps", aliases=("eeps",)),
    SchoolSource("EMPS", "emmps", aliases=("emmps", "emps")),
    SchoolSource("JACPS", "jacps", aliases=("jacps",)),
    SchoolSource("JCPS", "jcps", aliases=("jcps",)),
    SchoolSource("JGHS", "jghs", aliases=("jghs",), extra_prefixes=("https://schools.edu.ky/blog/",)),
    SchoolSource("LHSS", "lhs", aliases=("lhs", "lshs")),
    SchoolSource("LSHS", "lshs", aliases=("lshs",)),
    SchoolSource("MMPS", "mmps", aliases=("mmps",)),
    SchoolSource("PPPS", "pps", aliases=("pps", "ppps")),
    SchoolSource("RBPS", "rbps", aliases=("rbps",)),
    SchoolSource("SBPS", "csbs", aliases=("csbs",), feed_path="csbs/csbs-news"),
    SchoolSource("TMPS", "tmps", aliases=("tmps",)),
    SchoolSource("WEPS", "weps", aliases=("weps",)),
)

SCHOOL_ALIAS_MAP = tuple((source.aliases, source.name) for source in SCHOOL_SOURCES if source.aliases)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, application/rss+xml, text/xml;q=0.9, */*;q=0.8",
}


def normalize_date(date_text: str) -> str:
    if not date_text:
        return ""

    candidates = (date_text, date_text.replace("Z", "+00:00"))
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate).date().isoformat()
        except ValueError:
            pass

    try:
        return parsedate_to_datetime(date_text).astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return date_text[:10]


def clean_text(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", text).strip())


def infer_school(post: dict[str, object]) -> str:
    link = str(post.get("link", "")).lower()
    slug = str(post.get("slug", "")).lower()
    title_value = post.get("title", {})
    if isinstance(title_value, dict):
        title = clean_text(str(title_value.get("rendered", ""))).lower()
    else:
        title = clean_text(str(title_value or "")).lower()

    for needles, school in SCHOOL_ALIAS_MAP:
        if any(needle in link or needle in slug or needle in title for needle in needles):
            return school
    return ""


def fetch_json(url: str, params: dict[str, object] | None = None) -> object:
    response = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        return response.json()
    return json.loads(response.text)


def fetch_wordpress_posts(source: SchoolSource) -> tuple[list[dict[str, str]], str]:
    endpoint_candidates = [
        f"https://schools.edu.ky/{source.slug}/wp-json/wp/v2/posts",
        "https://schools.edu.ky/wp-json/wp/v2/posts",
    ]
    all_entries: list[dict[str, str]] = []
    seen_links: set[str] = set()
    used_source_url = endpoint_candidates[0]

    for endpoint in endpoint_candidates:
        page = 1
        entries_for_endpoint: list[dict[str, str]] = []

        while True:
            try:
                payload = fetch_json(
                    endpoint,
                    params={
                        "per_page": POSTS_PER_PAGE,
                        "page": page,
                        "orderby": "date",
                        "order": "desc",
                        "_fields": "date,title,link,slug",
                    },
                )
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 400:
                    break
                break
            except (requests.RequestException, json.JSONDecodeError, ValueError):
                break

            if not isinstance(payload, list) or not payload:
                break

            for post in payload:
                if not isinstance(post, dict):
                    continue

                link = str(post.get("link") or "")
                if not link or not any(link.startswith(prefix) for prefix in source.rest_prefixes):
                    continue
                if link in seen_links:
                    continue

                inferred_school = infer_school(post) or source.name
                title_value = post.get("title")
                if isinstance(title_value, dict):
                    title = clean_text(str(title_value.get("rendered", "")))
                else:
                    title = clean_text(str(title_value or ""))
                if not title:
                    slug = link.rstrip("/").rsplit("/", 1)[-1]
                    title = clean_text(slug.replace("-", " ").title())
                if not title:
                    continue

                entries_for_endpoint.append(
                    {
                        "Date": normalize_date(str(post.get("date") or "")),
                        "School": inferred_school,
                        "Title": title,
                        "URL": link,
                        "Slug": str(post.get("slug") or link.rstrip("/").rsplit("/", 1)[-1]),
                    }
                )
                seen_links.add(link)

            if len(payload) < POSTS_PER_PAGE:
                break
            page += 1

        if entries_for_endpoint:
            all_entries.extend(entries_for_endpoint)
            used_source_url = endpoint

    return all_entries, used_source_url


def fetch_feed_posts(source: SchoolSource) -> tuple[list[dict[str, str]], str]:
    url = f"https://schools.edu.ky/{source.feed_slug}/feed/"
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException:
        return [], url

    text = response.text
    items = []
    for block in re.findall(r"<item\b.*?</item>", text, flags=re.S | re.I):
        link_match = re.search(r"<link[^>]*>(.*?)</link>", block, flags=re.S | re.I)
        title_match = re.search(r"<title[^>]*>(.*?)</title>", block, flags=re.S | re.I)
        date_match = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", block, flags=re.S | re.I)
        guid_match = re.search(r"<guid[^>]*>(.*?)</guid>", block, flags=re.S | re.I)

        if not link_match:
            continue

        link = unescape(re.sub(r"<[^>]+>", "", link_match.group(1)).strip())
        if not link or not any(link.startswith(prefix) for prefix in source.rest_prefixes):
            continue

        title = clean_text(unescape(re.sub(r"<[^>]+>", "", title_match.group(1)).strip())) if title_match else ""
        if not title:
            slug = link.rstrip("/").rsplit("/", 1)[-1]
            title = clean_text(slug.replace("-", " ").title())
        if not title:
            continue

        items.append(
            {
                "Date": normalize_date(unescape(re.sub(r"<[^>]+>", "", date_match.group(1)).strip())) if date_match else "",
                "School": infer_school({"link": link, "slug": link.rstrip("/").rsplit("/", 1)[-1], "title": {"rendered": title}})
                or source.name,
                "Title": title,
                "URL": link,
                "Slug": clean_text(unescape(re.sub(r"<[^>]+>", "", guid_match.group(1)).strip())) if guid_match else link.rstrip("/").rsplit("/", 1)[-1],
            }
        )

    return items, url


def fetch_school_posts(source: SchoolSource) -> tuple[list[dict[str, str]], str]:
    rows, source_url = fetch_wordpress_posts(source)
    if rows:
        return rows, source_url

    rows, source_url = fetch_feed_posts(source)
    return rows, source_url


def collect_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_links: set[str] = set()

    for source in SCHOOL_SOURCES:
        source_rows, source_url = fetch_school_posts(source)
        print(f"{source.name}: {len(source_rows)} posts from {source_url}")
        for row in source_rows:
            if row["URL"] in seen_links:
                continue
            rows.append(row)
            seen_links.add(row["URL"])

    rows.sort(key=lambda row: (row["Date"], row["School"], row["Title"], row["URL"]), reverse=True)
    return rows


def main() -> int:
    rows = collect_rows()

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "School", "Title", "URL", "Slug"])
        writer.writerows([[row["Date"], row["School"], row["Title"], row["URL"], row["Slug"]] for row in rows])

    print(f"Saved {len(rows)} posts to {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
