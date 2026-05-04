#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
# ]
# ///
"""
Crawl foodpantries.org and write listings to ../food_resources.json.

Site structure:
  homepage          -> /st/<state-name>          (51 state pages)
  /st/<state>       -> /ci/<state-abbr>-<city>   (cities)
  /ci/<state-city>  -> /li/<listing-slug>        (listings)
  /li/<slug>        -> JSON-LD with PostalAddress

Usage:
    uv run scripts/fetch_resources.py                 # full crawl (slow!)
    uv run scripts/fetch_resources.py --state montana # one state
    uv run scripts/fetch_resources.py --limit 50      # first 50 listings only
    uv run scripts/fetch_resources.py --refresh-html  # ignore HTML cache

Caches HTML in scripts/.fetch_cache.json and geocoded addresses in
scripts/.geocode_cache.json (both gitignored). Re-running merges by id —
idempotent. Resumable: kill it any time, the cache picks up where it left off.

Politeness: 1 second between HTTP requests. Nominatim is throttled to 1.1
seconds. Full US crawl is several hours; one state is minutes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
OUTPUT = ROOT / "food_resources.json"
GEOCODE_CACHE = SCRIPT_DIR / ".geocode_cache.json"
FETCH_CACHE = SCRIPT_DIR / ".fetch_cache.json"

BASE = "https://www.foodpantries.org"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
NOMINATIM_HEADERS = {
    "User-Agent": "SharedPot/1.0 (+https://sharedpot.github.io)",
    "Accept-Language": "en",
}

JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
LISTING_TYPES = {"LocalBusiness", "Organization", "Restaurant", "Place", "FoodEstablishment"}

# 1 sec between requests. Bumped after observing 406s; foodpantries.org seems content with this.
REQ_DELAY = 1.0
NOMINATIM_DELAY = 1.1


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


def truncate(text: str, n: int = 280) -> str:
    if len(text) <= n:
        return text
    cut = text[: n - 3].rsplit(" ", 1)[0]
    return cut + "..."


def extract_jsonld_listing(html: str) -> dict | None:
    for raw in JSONLD_RE.findall(html):
        sanitized = re.sub(r"\s+", " ", raw).strip()
        try:
            obj = json.loads(sanitized)
        except json.JSONDecodeError:
            continue
        types = obj.get("@type")
        if isinstance(types, str):
            types = [types]
        if not types or not any(t in LISTING_TYPES for t in types):
            continue
        if isinstance(obj.get("address"), dict):
            return obj
    return None


def format_address(addr: dict) -> str | None:
    if not isinstance(addr, dict):
        return None
    parts = [
        addr.get("streetAddress"),
        addr.get("addressLocality"),
        addr.get("addressRegion"),
        addr.get("postalCode"),
        addr.get("addressCountry") or "USA",
    ]
    parts = [str(p).strip() for p in parts if p]
    if len(parts) < 3:
        return None
    return ", ".join(parts)


class Fetcher:
    """Caches HTML by URL with a polite delay between live fetches."""

    def __init__(self, http: httpx.Client, cache: Cache, refresh: bool = False) -> None:
        self.http = http
        self.cache = cache
        self.refresh = refresh
        self._last_request = 0.0

    def get(self, url: str) -> str:
        if not self.refresh:
            cached = self.cache.get(url)
            if cached is not None:
                return cached
        elapsed = time.monotonic() - self._last_request
        if elapsed < REQ_DELAY:
            time.sleep(REQ_DELAY - elapsed)
        r = self.http.get(url, headers=BROWSER_HEADERS)
        self._last_request = time.monotonic()
        r.raise_for_status()
        text = r.text
        self.cache.set(url, text)
        return text


def parse_links(html: str, prefix: str) -> list[str]:
    """Return unique absolute URLs whose path starts with the given prefix (e.g. '/st/')."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if " " in href or "\n" in href or "\t" in href:
            continue  # malformed link in source HTML
        absolute = urljoin(BASE, href)
        path = urlparse(absolute).path
        if path.startswith(prefix) and absolute not in seen and absolute.startswith(BASE):
            seen.add(absolute)
            out.append(absolute)
    return out


def listing_id(listing_url: str) -> str:
    """Stable per-listing id from URL slug — each /li/<slug> is unique on foodpantries.org."""
    slug = urlparse(listing_url).path.rsplit("/", 1)[-1]
    return "fp-" + slugify(slug)


def discover_state_urls(fetcher: Fetcher) -> list[str]:
    home = fetcher.get(BASE + "/")
    return parse_links(home, "/st/")


def discover_city_urls(fetcher: Fetcher, state_url: str) -> list[str]:
    html = fetcher.get(state_url)
    return parse_links(html, "/ci/")


def discover_listing_urls(fetcher: Fetcher, city_url: str) -> list[str]:
    html = fetcher.get(city_url)
    return parse_links(html, "/li/")


def geocode(address: str, cache: Cache, http: httpx.Client) -> dict | None:
    cached = cache.get(address)
    if cached is not None:
        return cached or None
    time.sleep(NOMINATIM_DELAY)
    r = http.get(
        NOMINATIM,
        params={"q": address, "format": "json", "limit": 1, "addressdetails": 0},
        headers=NOMINATIM_HEADERS,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        cache.set(address, False)
        return None
    result = {"lat": round(float(data[0]["lat"]), 5), "lng": round(float(data[0]["lon"]), 5)}
    cache.set(address, result)
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--state", help="Crawl one state by name (e.g. 'montana'). Default: all states.")
    p.add_argument("--limit", type=int, help="Stop after this many listings (across all states).")
    p.add_argument("--refresh-html", action="store_true", help="Ignore HTML cache; refetch live pages.")
    p.add_argument("--no-geocode", action="store_true", help="Skip geocoding (useful for testing crawl logic).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    fetch_cache = Cache(FETCH_CACHE)
    geocode_cache = Cache(GEOCODE_CACHE)

    existing = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else []
    by_id = {e["id"]: e for e in existing}
    initial = len(by_id)

    with httpx.Client(timeout=60.0, follow_redirects=True) as http:
        fetcher = Fetcher(http, fetch_cache, refresh=args.refresh_html)

        print("Discovering states…", flush=True)
        state_urls = discover_state_urls(fetcher)
        print(f"  found {len(state_urls)} states")
        if args.state:
            wanted = args.state.lower().replace(" ", "_")
            state_urls = [u for u in state_urls if urlparse(u).path.endswith(f"/st/{wanted}")]
            if not state_urls:
                print(f"State '{args.state}' not found", file=sys.stderr)
                return 2
            print(f"  filtered to {state_urls[0]}")

        added = updated = skipped_no_addr = skipped_no_geo = errors = 0
        listings_done = 0

        for state_url in state_urls:
            state_name = urlparse(state_url).path.rsplit("/", 1)[-1]
            print(f"\n=== state: {state_name} ===", flush=True)
            try:
                city_urls = discover_city_urls(fetcher, state_url)
            except httpx.HTTPError as e:
                print(f"  state page failed: {e}", file=sys.stderr)
                errors += 1
                continue
            print(f"  cities: {len(city_urls)}")

            for city_url in city_urls:
                try:
                    listing_urls = discover_listing_urls(fetcher, city_url)
                except httpx.HTTPError as e:
                    print(f"  [city failed] {city_url}: {e}", file=sys.stderr)
                    errors += 1
                    continue

                for listing_url in listing_urls:
                    if args.limit is not None and listings_done >= args.limit:
                        print(f"\nLimit of {args.limit} reached.")
                        break
                    listings_done += 1
                    try:
                        html = fetcher.get(listing_url)
                    except httpx.HTTPError as e:
                        print(f"  [listing failed] {listing_url}: {e}", file=sys.stderr)
                        errors += 1
                        continue

                    ld = extract_jsonld_listing(html)
                    if not ld:
                        skipped_no_addr += 1
                        continue
                    address = format_address(ld.get("address") or {})
                    if not address:
                        skipped_no_addr += 1
                        continue
                    name = (ld.get("name") or "").strip()
                    if not name:
                        skipped_no_addr += 1
                        continue

                    if args.no_geocode:
                        geo = {"lat": 0.0, "lng": 0.0}
                    else:
                        geo = geocode(address, geocode_cache, http)
                        if not geo:
                            skipped_no_geo += 1
                            continue

                    desc_html = ld.get("description") or ""
                    description = truncate(html_to_text(desc_html))
                    entry_id = listing_id(listing_url)
                    entry = {
                        "id": entry_id,
                        "category": "pantry",
                        "name": name,
                        "description": description,
                        "url": listing_url,
                        "lat": geo["lat"],
                        "lng": geo["lng"],
                        "address": address,
                        "source": {"name": "FoodPantries.org", "url": BASE + "/"},
                    }
                    if entry_id in by_id:
                        updated += 1
                    else:
                        added += 1
                    by_id[entry_id] = entry

                if args.limit is not None and listings_done >= args.limit:
                    break

            print(
                f"  running totals — listings: {listings_done}, "
                f"added: {added}, updated: {updated}, "
                f"no-address: {skipped_no_addr}, no-geocode: {skipped_no_geo}, "
                f"errors: {errors}"
            )

            # Persist after every state so a kill -INT doesn't lose progress
            merged = sorted(by_id.values(), key=lambda e: e["id"])
            OUTPUT.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")

            if args.limit is not None and listings_done >= args.limit:
                break

    merged = sorted(by_id.values(), key=lambda e: e["id"])
    OUTPUT.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    print(
        f"\nDone. {len(merged)} entries total ({len(merged) - initial} new this run). "
        f"Wrote {OUTPUT.relative_to(ROOT)}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
