#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "feedparser>=6.0",
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
# ]
# ///
"""
Fetch food-aid resources and write them to ../food_resources.json.

v1: foodpantries.org RSS only.

Usage:
    uv run scripts/fetch_resources.py

Idempotent: rerunning merges by id and writes the same JSON if nothing changed.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import feedparser
import httpx
from bs4 import BeautifulSoup

UA = "SharedPot/1.0 (+https://sharedpot.github.io)"
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
OUTPUT = ROOT / "food_resources.json"
GEOCODE_CACHE = SCRIPT_DIR / ".geocode_cache.json"
FETCH_CACHE = SCRIPT_DIR / ".fetch_cache.json"

# Permissive US street-address regex: number + street name + suffix + ", ST 12345"
ADDRESS_RE = re.compile(
    r"(\d{1,6}\s+[\w\.\-'’ ]+?(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|"
    r"Lane|Ln|Drive|Dr|Way|Court|Ct|Place|Pl|Parkway|Pkwy|Highway|Hwy|"
    r"Trail|Trl|Circle|Cir|Square|Sq|Terrace|Ter)\.?"
    r"(?:[\w\.\-'’ ,#]*?)"
    r",\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?)",
    re.IGNORECASE,
)


class Cache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = json.loads(path.read_text()) if path.exists() else {}

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True))
        tmp.replace(self.path)


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:80] or "entry"


def extract_address(text: str) -> str | None:
    if not text:
        return None
    m = ADDRESS_RE.search(text)
    return m.group(1).strip(" ,") if m else None


def html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


class FoodPantriesOrg:
    name = "FoodPantries.org"
    home = "https://www.foodpantries.org/"
    feed_url = "https://www.foodpantries.org/feed/"
    category = "pantry"
    id_prefix = "fp"

    def fetch_items(self, http: httpx.Client) -> list:
        resp = http.get(
            self.feed_url,
            headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"Failed to parse RSS: {feed.bozo_exception!r}")
        return feed.entries

    def parse_entry(
        self, item, http: httpx.Client, fetch_cache: Cache
    ) -> tuple[str | None, str]:
        """Return (address, description_text) for one feed item."""
        desc_html = item.get("description") or item.get("summary") or ""
        addr = extract_address(html_to_text(desc_html))
        if addr:
            return addr, desc_html
        link = (item.get("link") or "").strip()
        if not link:
            return None, desc_html
        cached = fetch_cache.get(link)
        if cached is None:
            time.sleep(1)  # polite to foodpantries.org
            r = http.get(link, headers={"User-Agent": UA})
            r.raise_for_status()
            cached = r.text
            fetch_cache.set(link, cached)
        addr = extract_address(html_to_text(cached))
        return addr, desc_html


def geocode(address: str, cache: Cache, http: httpx.Client) -> dict | None:
    cached = cache.get(address)
    if cached is not None:
        return cached or None
    time.sleep(1.1)  # Nominatim policy: <= 1 req/sec
    r = http.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1, "addressdetails": 0},
        headers={"User-Agent": UA, "Accept-Language": "en"},
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        cache.set(address, False)
        return None
    result = {"lat": round(float(data[0]["lat"]), 5), "lng": round(float(data[0]["lon"]), 5)}
    cache.set(address, result)
    return result


def truncate(text: str, n: int = 280) -> str:
    if len(text) <= n:
        return text
    cut = text[: n - 3].rsplit(" ", 1)[0]
    return cut + "..."


def main() -> int:
    fetch_cache = Cache(FETCH_CACHE)
    geocode_cache = Cache(GEOCODE_CACHE)

    existing = []
    if OUTPUT.exists():
        existing = json.loads(OUTPUT.read_text())
    by_id = {e["id"]: e for e in existing}

    sources = [FoodPantriesOrg()]

    with httpx.Client(timeout=60.0, follow_redirects=True) as http:
        for source in sources:
            print(f"\n=== {source.name} ===", flush=True)
            try:
                items = source.fetch_items(http)
            except Exception as e:
                print(f"  fetch failed: {e}", file=sys.stderr)
                return 2

            print(f"  feed items: {len(items)}")
            for i, item in enumerate(items[:3]):
                print(f"  sample[{i}]: {item.get('title')!r} -> {item.get('link')!r}")

            added = 0
            kept = 0
            skipped_no_addr = 0
            skipped_no_geo = 0
            for item in items:
                title = (item.get("title") or "").strip()
                link = (item.get("link") or "").strip()
                if not title or not link:
                    continue
                try:
                    addr, desc_html = source.parse_entry(item, http, fetch_cache)
                except httpx.HTTPError as e:
                    print(f"  [warn: detail page failed] {title}: {e}", file=sys.stderr)
                    addr, desc_html = None, item.get("description") or ""
                if not addr:
                    skipped_no_addr += 1
                    continue
                geo = geocode(addr, geocode_cache, http)
                if not geo:
                    skipped_no_geo += 1
                    continue

                entry_id = f"{source.id_prefix}-{slugify(title)}"
                description = truncate(html_to_text(desc_html))
                entry = {
                    "id": entry_id,
                    "category": source.category,
                    "name": title,
                    "description": description,
                    "url": link,
                    "lat": geo["lat"],
                    "lng": geo["lng"],
                    "address": addr,
                    "source": {"name": source.name, "url": source.home},
                }
                if entry_id in by_id:
                    kept += 1
                else:
                    added += 1
                by_id[entry_id] = entry

            print(
                f"  added: {added}, updated/kept: {kept}, "
                f"skipped (no address): {skipped_no_addr}, "
                f"skipped (no geocode): {skipped_no_geo}"
            )

    merged = sorted(by_id.values(), key=lambda e: e["id"])
    OUTPUT.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(merged)} entries to {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
