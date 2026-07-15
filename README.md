# Clark County Equity Finder

Finds high-equity multifamily parcels (2-4 unit) in Clark County, OH by scraping the county auditor site. Targets properties whose last real sale was on or before 12/31/2019.

## Install

```bash
pip install playwright beautifulsoup4
playwright install chromium
```

## Run

```bash
python clark_county_equity_scraper.py
```

Output saved to `clark_county_equity_results.csv`.

## Resumable

Progress cached to `parcels.json`, `done_parts.json`, `sales_cache.json`. Re-run after any interruption to resume. Delete those files to start fresh.

## Config

| Variable | Default | Description |
|---|---|---|
| `CUTOFF` | `2019-12-31` | Only keep parcels sold on/before this date |
| `CLASSES` | `["520","530"]` | Property classes (520=duplex, 530=triplex) |
| `BATCH` | `8` | Parcels per browser round-trip |
| `PAUSE` | `0.4` | Seconds between batches |
| `HEADLESS` | `True` | Set False to watch the browser work |

## Output columns

Parcel, Class, Owner, Address, Appraised ($), Last Real Sale ($), Last Real Sale date, Years Held, Equity %, Est. Equity ($), Taxes Due, Deed Type, Valid Sale, Neighborhood, Acres, Note, Parcel URL.

## Why it works

Routes all requests through the page's own `fetch()` so Cloudflare sees a real Chrome session. Auto-partitions queries by tax district and neighborhood to work around the 500-row result cap.
