# Listings map

A static map of active listings, hosted on GitHub Pages, that refreshes itself
once a day from a Google Sheet you maintain.

```
index.html                      the active-listings map (reads listings.json)
completed.html                  the completed-rentals map (reads completed-listings.json)
listings.json                   active listings — rewritten daily
completed-listings.json         completed rentals — rewritten daily, same run
scripts/update_listings.py      pulls the sheet, geocodes new addresses, writes both JSON files
.github/workflows/update-listings.yml   the daily cron job that runs the script
```

Nothing here reads Homes.com or any MLS site directly — Homes.com's terms
don't allow automated scraping, even of your own listings. The sheet is the
source of truth; you (or your brokerage) keep it updated.

## 1. Create the Google Sheet

Make a sheet with these column headers in row 1:

```
address,price,beds,baths,sqft,status,type,photo
```

- `type` is either `sale` or `rent`
- `photo` can be blank — the card just shows a placeholder block
- Add one row per listing. To add a new listing later, just add a row — no code changes needed.

Then: **File → Share → Publish to web**, select the sheet, choose **CSV**,
and copy the resulting URL. It'll look like:

```
https://docs.google.com/spreadsheets/d/e/2PACX-.../pub?output=csv
```

## 2. Create the GitHub repo

1. Create a new repo (public or private, either works with GitHub Pages on a paid plan; public is simplest on the free plan).
2. Push these files to the `main` branch.
3. Go to **Settings → Secrets and variables → Actions → New repository secret**, name it `SHEET_CSV_URL`, and paste the CSV URL from step 1.
   (It's a secret rather than a plain variable only because it's a slightly awkward long URL — the sheet itself isn't sensitive.)

## 3. Turn on GitHub Pages

**Settings → Pages → Source → Deploy from a branch → `main` / `root`.**
Your map will be live at `https://<your-username>.github.io/<repo-name>/`.

## 4. Turn on the daily job

The workflow in `.github/workflows/update-listings.yml` runs automatically
once a day (13:00 UTC ≈ 6am Phoenix time). To run it immediately instead of
waiting:

**Actions tab → "Update listings" → Run workflow.**

It will:
1. Download your sheet as CSV
2. Geocode any address it hasn't seen before (via the free OpenStreetMap
   Nominatim service, one request per second — addresses it already resolved
   are reused, so this stays fast)
3. Commit the refreshed `listings.json` back to the repo, which GitHub Pages
   then serves automatically

## Updating listings going forward

Just edit the Google Sheet — add, remove, or change a row. The site catches
up within a day (or immediately if you run the workflow manually). No need
to touch any code.

## Marking a rental as completed

Change that row's `status` column to `Completed`. On the next run:

- If `type` is `rent`, the row moves from the active map (`index.html`) to
  the completed-rentals map (`completed.html`) automatically.
- If `type` is `sale`, the row just drops off the active map — a sale isn't
  a rental, so it doesn't appear on the completed-rentals page either.

An optional `yearCompleted` column (e.g. `2025`) will show up on the
completed-rentals page if you add it to the sheet; it's not required.

No second sheet, no separate data source — same sheet, same script, same
daily run.

## Local preview

Any static file server works, e.g.:

```
python3 -m http.server 8000
```

then open `http://localhost:8000`.
