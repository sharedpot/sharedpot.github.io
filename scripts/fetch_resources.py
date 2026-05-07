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


US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def state_matches(returned_addr: dict | None, expected: str | None) -> bool:
    """True if Nominatim's returned address.state matches the expected state abbr."""
    if not expected:
        return True
    expected = expected.strip().upper()
    expected_full = US_STATE_NAMES.get(expected)
    if not expected_full:
        return True  # not a US state we know — give it a pass
    if not isinstance(returned_addr, dict):
        return False
    state = (returned_addr.get("state") or "").strip()
    iso = (returned_addr.get("ISO3166-2-lvl4") or "").strip().upper()
    if state.lower() == expected_full.lower():
        return True
    if iso == f"US-{expected}":
        return True
    return False


def geocode_query(
    cache: Cache,
    http: httpx.Client,
    cache_key: dict,
    api_params: dict,
    expected_state: str | None,
) -> dict | None:
    """Run a Nominatim query with cache and state validation.

    Cache value: {"lat", "lng"} on accepted hit, False on miss/rejection.
    Returns None on transient network error (does NOT cache the failure).
    """
    key = json.dumps(cache_key, sort_keys=True)
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    params = {"format": "jsonv2", "limit": 3, "addressdetails": 1, **api_params}
    last_err: Exception | None = None
    for attempt in range(3):
        time.sleep(NOMINATIM_DELAY * (1 + attempt))  # 1.1s, 2.2s, 3.3s
        try:
            r = http.get(NOMINATIM, params=params, headers=NOMINATIM_HEADERS)
            r.raise_for_status()
            data = r.json() or []
            for hit in data:
                if state_matches(hit.get("address"), expected_state):
                    result = {"lat": round(float(hit["lat"]), 5), "lng": round(float(hit["lon"]), 5)}
                    cache.set(key, result)
                    return result
            cache.set(key, False)
            return None
        except (httpx.HTTPError, httpx.RemoteProtocolError) as e:
            last_err = e
            continue
    print(f"  [warn: geocode failed after retries] {api_params}: {last_err}", file=sys.stderr)
    return None  # transient — don't cache the failure


def geocode_address(addr_obj: dict, full_str: str, cache: Cache, http: httpx.Client) -> tuple[dict, str] | None:
    """Cascade: full street → ZIP+state (structured) → city+state (structured).
    State match is enforced on every hit; results in the wrong state are rejected.
    Returns (geo, precision) where precision is 'address' | 'postal' | 'city'."""
    state = addr_obj.get("addressRegion")
    postal = addr_obj.get("postalCode")
    locality = addr_obj.get("addressLocality")

    # 1. Full street address (free-form). Cache key matches the v1 schema so the
    # ~4k entries that already resolved cleanly hit the cache on rerun.
    geo = geocode_query(
        cache, http,
        cache_key={"q": full_str},
        api_params={"q": full_str, "countrycodes": "us"},
        expected_state=state,
    )
    if geo:
        return geo, "address"

    # 2. ZIP code (structured). We TRUST the ZIP — sometimes the source's claimed
    # state is wrong but the ZIP is right (e.g. "Antioch, CO, 94509" — 94509 is in
    # California, the state field is the data error). Don't validate against the
    # claimed state here; let the ZIP win.
    if postal:
        geo = geocode_query(
            cache, http,
            cache_key={"postalcode": postal, "v": 2},
            api_params={"postalcode": postal, "country": "US"},
            expected_state=None,
        )
        if geo:
            return geo, "postal"

    # 3. City + state (structured)
    if locality and state:
        geo = geocode_query(
            cache, http,
            cache_key={"city": locality, "state": state, "v": 2},
            api_params={"city": locality, "state": state, "country": "US"},
            expected_state=state,
        )
        if geo:
            return geo, "city"

    return None


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
                        geo, precision = {"lat": 0.0, "lng": 0.0}, "address"
                    else:
                        result = geocode_address(ld.get("address") or {}, address, geocode_cache, http)
                        if not result:
                            skipped_no_geo += 1
                            continue
                        geo, precision = result

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
                        "geo_precision": precision,
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
