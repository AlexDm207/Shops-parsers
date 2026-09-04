# Podrygka Product Scraper

A Python scraper for collecting raw product data from the Podrygka online cosmetics catalogue.

The scraper reads the catalogue's server-rendered HTML and Next.js streaming payload, then returns product dictionaries with the current price, original price, and discount percentage when available.

## Features

- Uses `cloudscraper` when installed, with a `requests` fallback.
- Supports catalogue pagination through `max_pages`.
- Uses browser-like request headers and a configurable delay between requests.
- Extracts raw product values without normalizing price formats.
- Writes results as UTF-8 JSON Lines (`.jsonl`).
- Logs requests, response sizes, parsing counts, errors, and output writes.

## Installation

```powershell
python -m pip install requests beautifulsoup4 cloudscraper
```

`cloudscraper` is optional. If it is unavailable, the scraper automatically uses `requests`.

## Usage

Run the included example:

```powershell
python scraper_podrygka.py
```

The example collects up to three catalogue pages and writes the result to `products.jsonl`.

Use the class from another Python module:

```python
from scraper_podrygka import PodrygkaScraper

scraper = PodrygkaScraper(
    "https://www.podrygka.ru/catalog/?page=1",
    max_pages=5,
)
products = scraper.run()
scraper.save_to_jsonl("products.jsonl")
```

For a single page, set `max_pages=1`.

## Output

Each JSONL line contains one raw product dictionary:

```json
{
  "product_name": "Тонер для лица `CELIMAX` ...",
  "product_url": "https://www.podrygka.ru/catalog/...",
  "price_current": 1109,
  "price_old": 2540,
  "discount_label": "-56%",
  "parsed_at": "2026-09-04T12:04:41Z"
}
```

The scraper obtains Podrygka catalogue pricing from fields such as:

- `pricing.totalPrice` -> `price_current`
- `pricing.basePrice` -> `price_old`
- `pricing.discountPercent` -> `discount_label`

Values are kept in their source form. Price cleanup, normalization, deduplication, and persistence to ClickHouse or CSV belong to downstream project components.

## Configuration

`PodrygkaScraper` accepts these options:

- `start_url`: catalogue URL to fetch.
- `max_pages`: maximum number of pages; pagination stops earlier when a page has no products.
- `request_timeout`: request timeout in seconds, default `30`.
- `min_delay` and `max_delay`: delay range between requests, default `1` to `3` seconds.
- `session`: optional configured `requests.Session`-compatible object.

## Logging and troubleshooting

When the module is run as a script, it logs to the terminal. Typical messages include:

```text
INFO scraper_podrygka: Fetching page: ...
INFO scraper_podrygka: Fetched page: status=200 bytes=...
INFO scraper_podrygka: Parsed page 1: products=20
INFO scraper_podrygka: Saving 20 products to products.jsonl
```

To enable the same logging when importing the class:

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
```

Network failures are logged with a traceback and stop the current run safely.
