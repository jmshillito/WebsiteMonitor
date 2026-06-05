#!/usr/bin/env python3
"""Build a WEPS RSS feed from the public Headline News archive."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests


BASE = "https://schools.edu.ky/weps/"
CATEGORY_URL = "https://schools.edu.ky/weps/category/headline-news/"
OUT = Path("weps_news_feed.xml")

HEADERS = {
    "User-Agent": "Mozilla/5.0 WEPS-News-RSS/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass(frozen=True)
class Post:
    title: str
    link: str
    date: str
    summary: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def get_text(self) -> str:
        text = " ".join(self.parts)
        text = re.sub(r"\s+", " ", text)
        return unescape(text).strip()


def strip_tags(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    return parser.get_text()


def get_page(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def _first_match(patterns: list[str], text: str) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            return match.group(1)
    return None


def extract_posts(page_url: str) -> tuple[list[Post], Optional[str]]:
    html = get_page(page_url)
    posts: list[Post] = []

    for article_html in re.findall(r"<article\b.*?</article>", html, flags=re.I | re.S):
        href = _first_match(
            [
                r'<h2[^>]*>\s*<a[^>]+href=["\']([^"\']+)["\']',
                r'<h3[^>]*>\s*<a[^>]+href=["\']([^"\']+)["\']',
                r'<a[^>]+class=["\'][^"\']*entry-title[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
            ],
            article_html,
        )
        if not href:
            continue

        title = _first_match(
            [
                r'<h2[^>]*>\s*<a[^>]*>(.*?)</a>',
                r'<h3[^>]*>\s*<a[^>]*>(.*?)</a>',
                r'<a[^>]+class=["\'][^"\']*entry-title[^"\']*["\'][^>]*>(.*?)</a>',
            ],
            article_html,
        )
        date = _first_match(
            [
                r'<time[^>]*>(.*?)</time>',
                r'class=["\'][^"\']*posted-on[^"\']*["\'][^>]*>\s*(.*?)\s*<',
                r'class=["\'][^"\']*entry-date[^"\']*["\'][^>]*>\s*(.*?)\s*<',
            ],
            article_html,
        )
        excerpt = _first_match(
            [
                r'class=["\'][^"\']*entry-summary[^"\']*["\'][^>]*>(.*?)</',
                r'class=["\'][^"\']*entry-content[^"\']*["\'][^>]*>(.*?)</',
                r'<p[^>]*>(.*?)</p>',
            ],
            article_html,
        )

        posts.append(
            Post(
                title=strip_tags(title or ""),
                link=urljoin(BASE, href),
                date=strip_tags(date or ""),
                summary=strip_tags(excerpt or ""),
            )
        )

    next_href = _first_match(
        [
            r'<a[^>]+class=["\'][^"\']*next[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
            r'<a[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']',
            r'<a[^>]+class=["\'][^"\']*page-numbers[^"\']*next[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
        ],
        html,
    )
    next_url = urljoin(BASE, next_href) if next_href else None
    return posts, next_url


def collect_all_posts() -> list[Post]:
    url = CATEGORY_URL
    all_posts: list[Post] = []
    seen: set[str] = set()

    while url:
        posts, next_url = extract_posts(url)
        for post in posts:
            if post.link not in seen:
                seen.add(post.link)
                all_posts.append(post)
        url = next_url

    return all_posts


def _normalize_pubdate(date_text: str) -> str:
    if not date_text:
        return ""
    return date_text


def write_rss(posts: list[Post]) -> None:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "West End Primary School News"
    ET.SubElement(channel, "link").text = BASE
    ET.SubElement(channel, "description").text = "WEPS Headline News posts"

    for post in posts:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = post.title
        ET.SubElement(item, "link").text = post.link
        ET.SubElement(item, "guid").text = post.link
        ET.SubElement(item, "description").text = post.summary or post.title
        if post.date:
            ET.SubElement(item, "pubDate").text = _normalize_pubdate(post.date)

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(OUT, encoding="utf-8", xml_declaration=True)


def main() -> int:
    posts = collect_all_posts()
    write_rss(posts)
    print(f"Wrote {OUT}")
    print(f"Posts found: {len(posts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
