# SharedPot

A static, dependency-free directory of local Telegram / WhatsApp / Signal community groups, plotted on a map. No backend, no accounts, no build step.

## What's in here

- `index.html` — landing page with map, search, and "find near me".
- `app.js` — Leaflet init, list rendering, search filter, geolocation + distance sort.
- `styles.css` — layout overrides on top of Simple.css.
- `groups.json` — the directory data; one object per group.
- `submit.html` — page with an embedded Google Form for submissions.

## Run locally

Geolocation and `fetch()` won't work from `file://`, so use a local server:

```bash
python3 -m http.server 8000
```

Open <http://localhost:8000>.

## Add a group

1. Open `groups.json`.
2. Append an object:
   ```json
   {
     "id": "kebab-case-unique",
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
4. `platform` must be `telegram`, `whatsapp`, or `signal`.
5. Commit and push — GitHub Pages redeploys automatically.

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
