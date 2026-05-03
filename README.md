# SharedPot

A static, dependency-free directory of local Telegram / WhatsApp / Signal community groups, plotted on a map. No backend, no accounts, no build step.

## What's in here

- `index.html` — landing page with map, search, "find near me", and category-toggle chips.
- `about.html` — why and how to run/join a cooking circle (one page, three sections).
- `submit.html` — embedded Google Form for community submissions.
- `app.js` — Leaflet init, list rendering, search filter, geolocation + distance sort, category toggles persisted to localStorage.
- `styles.css` — layout overrides on top of Simple.css; category colors.
- `groups.json` — hand-curated cooking circles (the editorial heart of the site).
- `food_resources.json` — auto-generated from `scripts/fetch_resources.py`. Don't edit by hand.
- `scripts/fetch_resources.py` — pulls food-aid listings from public RSS feeds, geocodes via Nominatim.

## Run locally

Geolocation and `fetch()` won't work from `file://`, so use a local server:

```bash
python3 -m http.server 8000
```

Open <http://localhost:8000>.

## Add a cooking circle

1. Open `groups.json`.
2. Append an object:
   ```json
   {
     "id": "kebab-case-unique",
     "category": "circle",
     "name": "Friendly name",
     "description": "Short description.",
     "platform": "telegram",
     "url": "https://t.me/+invite",
     "lat": 40.7081,
     "lng": -73.9571,
     "address": "Neighbourhood, City, Country"
   }
   ```
3. Geocode the address at <https://nominatim.openstreetmap.org/> and copy the coordinates (4 decimals is fine).
4. `platform` must be `telegram`, `whatsapp`, or `signal`. `category` must be `circle` for hand-curated entries.
5. Commit and push — GitHub Pages redeploys automatically.

## Refresh food-aid data

`food_resources.json` is generated from public RSS feeds (currently foodpantries.org). To refresh:

```bash
uv run scripts/fetch_resources.py
git add food_resources.json
git commit -m "data: refresh food resources"
git push
```

The script writes `scripts/.geocode_cache.json` and `scripts/.fetch_cache.json` (both gitignored) so reruns are fast. First run is slow because Nominatim allows 1 request/second.

### Future: adding freefood.org

freefood.org has no RSS feed, no sitemap, and aggressive bot filtering — scraping isn't viable. The legitimate path is to email `webmaster@freefood.org` and ask:

> Subject: Mutual aid project asking for a data feed
>
> Hi! I run sharedpot.github.io, a small free directory of communal cooking circles. I'd like to also show food-aid resources from freefood.org as a separate, clearly-attributed category, with a click-through to your listing for full details. I see foodpantries.org has an RSS feed I'm using; do you have something similar — RSS, sitemap, or a CSV/JSON dump I could pull periodically? Happy to credit you and link back prominently.
>
> Thanks for keeping the directory running.

If/when they reply with a feed, add a `FreeFoodOrg` source class to `scripts/fetch_resources.py` modeled on the existing `FoodPantriesOrg`, and add a third chip in `index.html`.

## Submission workflow

1. Create a Google Form with fields: name, description, platform (multiple choice), invite URL, address/area, optional submitter email.
2. In the form, **Send → `<>` (Embed HTML)** → copy the URL.
3. Paste it as the `src` of the iframe in `submit.html`.
4. Form responses go to a Google Sheet you own. Periodically review, geocode addresses, and copy approved entries into `groups.json`.

## Deploy via GitHub Pages

1. Create a GitHub repo and push this folder to `main`.
2. Repo → **Settings → Pages**.
3. **Source: Deploy from a branch**, **Branch: `main` / `(root)`**, save.
4. Wait ~1 minute. Site is live at `https://<user>.github.io/<repo>/`.
5. To update: edit `groups.json` (or anything else), commit, push. Pages redeploys on every push to `main`.

For a custom domain, add a `CNAME` file with your domain and configure DNS per the GitHub Pages docs.

Other static hosts (Netlify, Cloudflare Pages) work the same way — just point them at the repo.
