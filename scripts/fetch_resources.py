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

UA = "Mozilla/5.0 (compatible; SharedPot/1.0; +https://sharedpot.github.io)"
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
OUTPUT = ROOT / "food_resources.json"
GEOCODE_CACHE = SCRIPT_DIR / ".geocode_cache.json"
FETCH_CACHE = SCRIPT_DIR / ".fetch_cache.json"

# Browser-like headers — foodpantries.org returns 406 to bare requests
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
LISTING_TYPES = {"LocalBusiness", "Organization", "Restaurant", "Place", "FoodEstablishment"}


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


def html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def extract_jsonld_listing(html: str) -> dict | None:
    """Find a JSON-LD block describing the listing (LocalBusiness/Organization/etc.)."""
    for raw in JSONLD_RE.findall(html):
        # Their JSON-LD has unescaped newlines/tabs inside string values; collapse whitespace.
        sanitized = re.sub(r"\s+", " ", raw).strip()
        try:
            obj = json.loads(sanitized)
        except json.JSONDecodeError:
            continue
        # @type may be a string or list
        ld_types = obj.get("@type")
        if isinstance(ld_types, str):
            ld_types = [ld_types]
        if not ld_types:
            continue
        if not any(t in LISTING_TYPES for t in ld_types):
            continue
        if isinstance(obj.get("address"), dict):
            return obj
    return None


def format_address(addr_obj: dict) -> str | None:
    if not isinstance(addr_obj, dict):
        return None
    parts = [
        addr_obj.get("streetAddress"),
        addr_obj.get("addressLocality"),
        addr_obj.get("addressRegion"),
        addr_obj.get("postalCode"),
        addr_obj.get("addressCountry") or "USA",
    ]
    parts = [str(p).strip() for p in parts if p]
    if len(parts) < 3:
        return None
    return ", ".join(parts)


class FoodPantriesOrg:
    name = "FoodPantries.org"
    home = "https://www.foodpantries.org/"
    feed_url = "https://www.foodpantries.org/feed/"
    category = "pantry"
    id_prefix = "fp"

    def fetch_items(self, http: httpx.Client) -> list:
        headers = {**BROWSER_HEADERS, "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"}
        resp = http.get(self.feed_url, headers=headers)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"Failed to parse RSS: {feed.bozo_exception!r}")
        return feed.entries

    def parse_entry(
        self, item, http: httpx.Client, fetch_cache: Cache
    ) -> tuple[str | None, str, str | None]:
        """Return (address, description_html, name) — fetched from the listing's JSON-LD."""
        link = (item.get("link") or "").strip()
        rss_desc = item.get("description") or item.get("summary") or ""
        if not link:
            return None, rss_desc, None
        cached = fetch_cache.get(link)
        if cached is None:
            time.sleep(1)  # polite to foodpantries.org
            r = http.get(link, headers=BROWSER_HEADERS)
            r.raise_for_status()
            cached = r.text
            fetch_cache.set(link, cached)
        ld = extract_jsonld_listing(cached)
        if not ld:
            return None, rss_desc, None
        address = format_address(ld.get("address") or {})
        # Prefer JSON-LD's description (HTML) when present — it's richer than RSS summary
        desc_html = ld.get("description") or rss_desc
        name = (ld.get("name") or "").strip() or None
        return address, desc_html, name


def geocode(address: str, cache: Cache, http: httpx.Client) -> dict | None:
    cached = cache.get(address)
    if cached is not None:
        return cached or None
    time.sleep(1.1)  # Nominatim policy: <= 1 req/sec
    r = http.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1, "addressdetails": 0},
        headers={"User-Agent": "SharedPot/1.0 (+https://sharedpot.github.io)", "Accept-Language": "en"},
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
                rss_title = (item.get("title") or "").strip()
                link = (item.get("link") or "").strip()
                if not rss_title or not link:
                    continue
                try:
                    addr, desc_html, ld_name = source.parse_entry(item, http, fetch_cache)
                except httpx.HTTPError as e:
                    print(f"  [warn: detail page failed] {rss_title}: {e}", file=sys.stderr)
                    addr, desc_html, ld_name = None, item.get("description") or "", None
                if not addr:
                    skipped_no_addr += 1
                    continue
                geo = geocode(addr, geocode_cache, http)
                if not geo:
                    skipped_no_geo += 1
                    continue

                title = ld_name or rss_title
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
