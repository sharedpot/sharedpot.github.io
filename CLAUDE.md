# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**SharedPot** — a static, dependency-free directory of community chat groups (Telegram / WhatsApp / Signal) plotted on a map. Visitors browse, search, and use "find near me" to sort by distance, then click through to the group's invite link. No backend, no accounts, no build step. Hosted on GitHub Pages.

## File layout

- `index.html` — landing page: header, search/locate controls, Leaflet map, group list.
- `app.js` — fetches `groups.json`, renders markers and list, search filter, geolocation + Haversine distance sort.
- `styles.css` — layout grid + platform badge colours; Simple.css from CDN handles base typography.
- `groups.json` — array of group entries (the directory data).
- `submit.html` — page with an embedded Google Form for community submissions.

## Group entry schema

```json
{
  "id": "kebab-case-unique",
  "name": "Display name",
  "description": "Short blurb.",
  "platform": "telegram | whatsapp | signal",
  "url": "https://...",
  "lat": 00.0000,
  "lng": 00.0000,
  "address": "Area, City, Country"
}
```

Conventions:
- `lat` / `lng` are pre-computed (geocode via nominatim.openstreetmap.org), 4 decimals.
- `id` is unique, lowercase, kebab-case.
- `platform` must be one of the three known values; entries with other platforms or non-finite coordinates are filtered out at load time.
- Keep `groups.json` valid JSON (no trailing commas, no comments).

## Submissions flow

1. Visitor fills the Google Form embedded on `submit.html`.
2. Responses land in a Google Sheet.
3. Owner reviews, geocodes the address, appends to `groups.json`, commits and pushes — GitHub Pages redeploys automatically.

## Run locally

```bash
python3 -m http.server 8000
```

Open <http://localhost:8000>. (`file://` won't work — geolocation and `fetch()` need an HTTP origin.)

## Manual test checklist

- Map loads with markers; clicking a marker shows a popup with the chat link.
- Typing in the search box filters list and markers in sync.
- "Find near me" prompts for permission, drops a marker at the user's location, sorts the list by distance.
- Radius selector hides groups beyond the chosen distance.
- Clicking a list item pans the map and opens the popup.
- "Open chat" links open the invite URL in a new tab.

## Deploy

GitHub Pages: **Settings → Pages → Source: `main` / root**. Every push to `main` redeploys. No build step, no Actions workflow needed.
