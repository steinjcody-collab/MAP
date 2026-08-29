#!/usr/bin/env python3
"""
Pulls listing rows from a published Google Sheet (CSV export), geocodes any
address that doesn't already have coordinates, and writes TWO files at the
repo root: listings.json (active for-sale/for-rent listings) and
completed-listings.json (rentals you've marked as filled). Designed to run
once a day via GitHub Actions.

To move a property from the live map to the completed-rentals page, just
set that row's `status` column to "Completed" in the sheet — no code
changes, no separate sheet. Only rent-type rows marked Completed go to the
completed-rentals page; a sale marked Completed just drops off the active
map (it isn't a rental, so it doesn't belong on either page's list).

Data source: a Google Sheet you maintain, published to the web as CSV
(File > Share > Publish to web > select the sheet > CSV). That URL goes in
SHEET_CSV_URL below (or the SHEET_CSV_URL repo variable/secret — see README).
 
This script does NOT touch Homes.com or any MLS site. It only reads the
sheet you control and geocodes addresses using the free US Census Bureau
geocoder (built on official TIGER/Line street data, generally more precise
than OSM interpolation), falling back to OpenStreetMap Nominatim only if
Census can't find a match.
"""
 
import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
 
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTINGS_PATH = os.path.join(REPO_ROOT, "listings.json")
COMPLETED_PATH = os.path.join(REPO_ROOT, "completed-listings.json")
 
# Falls back to the env var / repo variable SHEET_CSV_URL if set, so the
# actual sheet URL doesn't have to live in source control.
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL", "")
 
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
USER_AGENT = "cody-stein-listings-map/1.0 (contact via GitHub repo)"

UNIT_RE = re.compile(
    r"\b(unit|apt|apartment|suite|ste|#)\.?\s*[\w-]+\b", re.IGNORECASE
)


def strip_unit(address):
    """Unit/apartment numbers ('Unit 3', 'Apt 2B', '#4') add noise that can
    throw off a free-text geocoder's street match — strip them before
    geocoding, but keep the original address for display."""
    cleaned = UNIT_RE.sub("", address)
    cleaned = re.sub(r"\s*,\s*,", ",", cleaned)  # collapse a hole left by the strip
    cleaned = re.sub(r"\s+,", ",", cleaned)       # drop space left before a comma
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    return cleaned


def parse_parts(address):
    """Split 'street, city, state[, zip]' into components for a structured
    (rather than free-text) geocoder query."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    street = strip_unit(parts[0]) if parts else ""
    city = parts[1] if len(parts) > 1 else ""
    # last part might be "AZ" or "AZ 86005" — split off a trailing zip if present
    state, postalcode = "", ""
    if len(parts) > 2:
        tail = parts[2].split()
        state = tail[0] if tail else ""
        postalcode = tail[1] if len(tail) > 1 else ""
    return street, city, state, postalcode


def extract_housenumber(text):
    m = re.match(r"\s*(\d+)", text or "")
    return m.group(1) if m else None


def pick_best_census_match(matches, expected_hn):
    """Census can return more than one candidate for an ambiguous address.
    Prefer whichever one's house number actually matches what we asked
    for, instead of always trusting the first one back."""
    if not matches:
        return None
    if expected_hn:
        for m in matches:
            if extract_housenumber(m.get("matchedAddress", "")) == expected_hn:
                return m
    return matches[0]


def pick_best_nominatim_match(results, expected_hn):
    """Same idea for Nominatim: prefer an exact house-number match among
    the returned candidates, then break ties by Nominatim's own
    'importance' score rather than list order."""
    if not results:
        return None

    def importance(r):
        try:
            return float(r.get("importance", 0))
        except (TypeError, ValueError):
            return 0.0

    if expected_hn:
        exact = [r for r in results if r.get("address", {}).get("house_number") == expected_hn]
        if exact:
            exact.sort(key=importance, reverse=True)
            return exact[0]

    return sorted(results, key=importance, reverse=True)[0]


def geocode_census(address):
    """US Census Bureau geocoder — built on official TIGER/Line street
    centerline data. Free, no API key, and generally far more precise for
    US street addresses than OSM-derived interpolation (which is what can
    cause e.g. a '4th St' address to land on '5th St')."""
    params = urllib.parse.urlencode({
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    })
    url = f"{CENSUS_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  Census geocode error for '{address}': {e}", file=sys.stderr)
        return None
    matches = data.get("result", {}).get("addressMatches", [])
    best = pick_best_census_match(matches, extract_housenumber(address))
    if not best:
        return None
    coords = best["coordinates"]
    return float(coords["y"]), float(coords["x"])


def geocode_nominatim(address):
    params = urllib.parse.urlencode({
        "q": address,
        "format": "json",
        "limit": 5,
        "countrycodes": "us",
        "addressdetails": 1,
    })
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            results = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  Nominatim geocode error for '{address}': {e}", file=sys.stderr)
        return None
    best = pick_best_nominatim_match(results, extract_housenumber(address))
    if not best:
        return None
    return float(best["lat"]), float(best["lon"])


def geocode_nominatim_structured(street, city, state, postalcode):
    """Structured query — separate street/city/state/zip fields, which
    Nominatim's parser tends to match more reliably than one free-text
    string, especially for short/ambiguous street names."""
    if not street or not city:
        return None
    params = {
        "street": street,
        "city": city,
        "state": state or "AZ",
        "country": "USA",
        "format": "json",
        "limit": 5,
        "countrycodes": "us",
        "addressdetails": 1,
    }
    if postalcode:
        params["postalcode"] = postalcode
    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            results = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  Nominatim (structured) geocode error for '{street}, {city}': {e}", file=sys.stderr)
        return None
    best = pick_best_nominatim_match(results, extract_housenumber(street))
    if not best:
        return None
    return float(best["lat"]), float(best["lon"])


def geocode(address):
    """Try every combination we reasonably can, across both geocoders,
    before giving up on an address. Order is chosen so the free (no rate
    limit) Census lookups run first, and the rate-limited Nominatim calls
    — which need a sleep between each — run last."""
    stripped = strip_unit(address)
    street, city, state, postalcode = parse_parts(address)

    attempts = [
        ("Census (as written)", lambda: geocode_census(address)),
        ("Census (no unit)", lambda: geocode_census(stripped) if stripped != address else None),
    ]

    for label, fn in attempts:
        result = fn()
        if result:
            print(f"  matched via {label}")
            return result

    # Nominatim calls are rate-limited to 1/sec — only reached if Census
    # couldn't resolve the address at all.
    nominatim_attempts = [
        ("Nominatim (structured)", lambda: geocode_nominatim_structured(street, city, state, postalcode)),
        ("Nominatim (no unit)", lambda: geocode_nominatim(stripped)),
        ("Nominatim (as written)", lambda: geocode_nominatim(address) if address != stripped else None),
    ]
    for label, fn in nominatim_attempts:
        time.sleep(1)
        result = fn()
        if result:
            print(f"  matched via {label}")
            return result

    return None
 
 
def fetch_csv_rows(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    return [row for row in reader if row.get("address", "").strip()]
 
 
def load_existing_coords():
    """Reuse lat/lng already stored in either output file so we don't
    re-geocode an address we've already resolved on a previous run — including
    ones that have since moved from listings.json to completed-listings.json
    (or vice versa) as their status changed."""
    coords = {}
    for path in (LISTINGS_PATH, COMPLETED_PATH):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for l in data.get("listings", []):
            if l.get("lat") is not None and l.get("lng") is not None:
                coords[l["address"].strip().lower()] = (l["lat"], l["lng"])
    return coords
 
 
def to_number(value, default=0):
    # Google Sheets often exports formatted numbers like "1,500,000.00" or
    # "$2,300" depending on the cell's display format. Strip anything that
    # isn't a digit, minus sign, or decimal point before parsing, so
    # formatting choices in the sheet can't silently zero out a price.
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    if not cleaned:
        return default
    try:
        return float(cleaned) if "." in cleaned else int(cleaned)
    except (ValueError, TypeError):
        return default


def get_field(row, name, default=""):
    """Look up a CSV column by name without caring about capitalization —
    Google Sheets headers are easy to type as 'Deal' vs 'deal', and
    csv.DictReader matches column names exactly, so a mismatch here
    otherwise fails silently (the field just comes back empty)."""
    for key, value in row.items():
        if key and key.strip().lower() == name.lower():
            return str(value).strip()
    return default
 
 
def main():
    if not SHEET_CSV_URL:
        print("SHEET_CSV_URL is not set — nothing to do. See README.", file=sys.stderr)
        sys.exit(1)
 
    rows = fetch_csv_rows(SHEET_CSV_URL)
    existing_coords = load_existing_coords()
 
    active_listings = []
    completed_listings = []
    newly_geocoded = 0
 
    for row in rows:
        address = row["address"].strip().strip(",").strip()
        key = address.lower()
        status = row.get("status", "Active").strip()
        listing_type = row.get("type", "sale").strip().lower()
        is_completed = status.lower().startswith("complet")
 
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
 
        if is_completed and listing_type == "rent":
            # Goes to the completed-rentals page instead of the active map.
            # Price/status/url/deal don't matter once it's a past rental —
            # keep the fields that page actually displays.
            completed_listings.append({
                "address": address,
                "beds": to_number(row.get("beds", 0)),
                "baths": to_number(row.get("baths", 0)),
                "sqft": to_number(row.get("sqft", 0)),
                "photo": row.get("photo", "").strip(),
                "yearCompleted": get_field(row, "yearcompleted", ""),
                "lat": lat,
                "lng": lng,
            })
            continue

        if is_completed:
            # A completed SALE isn't a rental and isn't "active" either —
            # it just drops off the map entirely (nothing to do here).
            continue

        active_listings.append({
            "address": address,
            "price": to_number(row.get("price", 0)),
            "beds": to_number(row.get("beds", 0)),
            "baths": to_number(row.get("baths", 0)),
            "sqft": to_number(row.get("sqft", 0)),
            "status": status,
            "type": listing_type,
            "photo": row.get("photo", "").strip(),
            "url": row.get("url", "").strip(),
            "deal": get_field(row, "deal", ""),
            "lat": lat,
            "lng": lng,
        })
 
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
 
    with open(LISTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "listings": active_listings}, f, indent=2)
        f.write("\n")
 
    with open(COMPLETED_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "listings": completed_listings}, f, indent=2)
        f.write("\n")
 
    print(f"Wrote {len(active_listings)} active listings to listings.json")
    print(f"Wrote {len(completed_listings)} completed rentals to completed-listings.json")
    print(f"({newly_geocoded} addresses newly geocoded this run)")
 
 
if __name__ == "__main__":
    main()
 
