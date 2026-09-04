# RIV GOSH Promotion Scraper

A Python loader for collecting raw product data from the RIV GOSH sale section. The module is designed for the `promotion-scrapper` pipeline:

```text
loader -> normalizer -> storage
```

This project contains only the loader. Price normalization, deduplication, and persistence to ClickHouse or CSV are handled by downstream modules.

## Features

- Fetches public sale pages with `cloudscraper` or `requests`.
- Falls back to Playwright/Chromium when direct HTTP requests are refused or reset.
- Parses JSON responses and rendered HTML product cards.
- Supports `currentPage` pagination.
- Adds a UTC ISO-8601 collection timestamp to every raw record.
- Writes optional JSONL output.

## Raw record format

Each output line contains one JSON object:

```json
{
  "raw_title": "Payot Source Hydra+ Adaptogen Moisturising Gel",
  "product_url": "https://rivegauche.ru/product/example",
  "price_current": "2 650 ₽",
  "price_old": "5 300 ₽",
  "discount_label": "-50%",
  "parsed_at": "2026-09-04T08:54:53.020424Z"
}
```

Prices remain raw strings or values from the source. They are intentionally not normalized here.

## Installation in PowerShell

Run these commands from the project directory. Install packages into the same Python environment that will run the scraper.

### Using the project virtual environment

```powershell
cd "C:\Users\79295\Desktop\Git_work\rivgosh"

.\.venv\Scripts\Activate.ps1
python -m pip install cloudscraper beautifulsoup4 requests playwright
python -m playwright install chromium
```

If PowerShell blocks script activation, use the process-scoped policy for the current terminal only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### Using Anaconda

```powershell
& "C:\Users\79295\anaconda3\python.exe" -m pip install cloudscraper beautifulsoup4 requests playwright
& "C:\Users\79295\anaconda3\python.exe" -m playwright install chromium
```

## Run

```powershell
python .\scraper_rivegauche.py
```

With Anaconda:

```powershell
& "C:\Users\79295\anaconda3\python.exe" .\scraper_rivegauche.py
```

The standalone example writes results to `products.jsonl` and prints the number of collected products.

The default example uses:

- URL: `https://rivegauche.ru/tags/sale?currentPage=1`
- `max_pages=None`: continue until an empty page is reached
- `delay=1.5`: wait between page requests

For a short test run, set `max_pages=1` in the `__main__` block.

## Python API

```python
from scraper_rivegauche import RiveGaucheScraper

scraper = RiveGaucheScraper(
    start_url="https://rivegauche.ru/tags/sale?currentPage=1",
    max_pages=1,
    delay=1.5,
)

products = scraper.fetch_all_products()
scraper.save_to_jsonl("products.jsonl")
```

Available methods:

- `fetch_page(url_or_page)` loads one page.
- `parse_products(data_or_html)` parses raw JSON or HTML.
- `run()` collects paginated products.
- `fetch_all_products()` is a pipeline-friendly alias for `run()`.
- `save_to_jsonl(filename)` writes raw records as JSONL and returns the record count.

## Network and access notes

The site may return HTTP `503`, `ProxyError`, or `ERR_CONNECTION_RESET` for direct requests. The loader then tries Chromium through Playwright. If the network route used by PowerShell is blocked, the browser fallback can also fail even when the VS Code browser can open the site.

The scraper uses only publicly available pages. Do not bypass CAPTCHA, authentication, or access controls. Keep delays reasonable and follow the website's terms and `robots.txt`.
