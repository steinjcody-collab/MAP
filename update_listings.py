#!/usr/bin/env python3
"""
Pulls listing rows from a published Google Sheet (CSV export), geocodes any
address that doesn't already have coordinates, and writes listings.json at
the repo root. Designed to run once a day via GitHub Actions.

Data source: a Google Sheet you maintain, published to the web as CSV
(File > Share > Publish to web > select the sheet > CSV). That URL goes in
SHEET_CSV_URL below (or the SHEET_CSV_URL repo variable/secret — see README).

This script does NOT touch Homes.com or any MLS site. It only reads the
sheet you control and calls the free OpenStreetMap Nominatim geocoder,
which is fine to use for a low-volume job like this one request/day.
"""

import csv
import io
import json
import os
import sys
import time
import urllib.request
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTINGS_PATH = os.path.join(REPO_ROOT, "listings.json")

# Falls back to the env var / repo variable SHEET_CSV_URL if set, so the
# actual sheet URL doesn't have to live in source control.
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL", "")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "cody-stein-listings-map/1.0 (contact via GitHub repo)"


def fetch_csv_rows(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    return [row for row in reader if row.get("address", "").strip()]


def load_existing_coords():
    """Reuse lat/lng already stored in listings.json so we don't re-geocode
    addresses we've already resolved on a previous run."""
    if not os.path.exists(LISTINGS_PATH):
        return {}
    try:
        with open(LISTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    coords = {}
    for l in data.get("listings", []):
        if l.get("lat") is not None and l.get("lng") is not None:
            coords[l["address"].strip().lower()] = (l["lat"], l["lng"])
    return coords


def geocode(address):
    params = urllib.parse.urlencode({
        "q": address,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    })
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            results = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  geocode error for '{address}': {e}", file=sys.stderr)
        return None
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def to_number(value, default=0):
    try:
        return float(value) if "." in str(value) else int(value)
    except (ValueError, TypeError):
        return default


def main():
    if not SHEET_CSV_URL:
        print("SHEET_CSV_URL is not set — nothing to do. See README.", file=sys.stderr)
        sys.exit(1)

    rows = fetch_csv_rows(SHEET_CSV_URL)
    existing_coords = load_existing_coords()

    listings = []
    newly_geocoded = 0

    for row in rows:
        address = row["address"].strip()
        key = address.lower()

        if key in existing_coords:
            lat, lng = existing_coords[key]
        else:
            print(f"Geocoding: {address}")
            result = geocode(address)
            if result is None:
                print(f"  no match, skipping coordinates for '{address}'", file=sys.stderr)
                lat, lng = None, None
            else:
                lat, lng = result
                newly_geocoded += 1
            time.sleep(1)  # Nominatim usage policy: max 1 request/sec

        listings.append({
            "address": address,
            "price": to_number(row.get("price", 0)),
            "beds": to_number(row.get("beds", 0)),
            "baths": to_number(row.get("baths", 0)),
            "sqft": to_number(row.get("sqft", 0)),
            "status": row.get("status", "Active").strip(),
            "type": row.get("type", "sale").strip().lower(),
            "photo": row.get("photo", "").strip(),
            "lat": lat,
            "lng": lng,
        })

    output = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "listings": listings,
    }

    with open(LISTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
        f.write("\n")

    print(f"Wrote {len(listings)} listings ({newly_geocoded} newly geocoded) to listings.json")


if __name__ == "__main__":
    main()
