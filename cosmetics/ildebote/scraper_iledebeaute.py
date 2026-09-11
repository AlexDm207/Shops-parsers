"""Synchronous Il de Beaute raw-product scraper."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

COMMON_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COMMON_ROOT))
from scraper_common import (  # noqa: E402
    REQUEST_TIMEOUT_SECONDS,
    ScraperError,
    create_session,
    publish_products,
    raw_product,
    save_jsonl,
)

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://iledebeaute.ru"
DEFAULT_CATALOG_URL = f"{BASE_URL}/catalog/tip-has_discount-iz-prom/"


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).replace("\xa0", " ").strip()


def normalize_name(value: Any) -> str:
    return re.sub(r"^Перейти к товару\s+", "", clean_text(value), flags=re.IGNORECASE)


def normalize_url(url: str) -> str:
    parsed = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit(parsed._replace(query=query, fragment="")).rstrip("/")


class IleDeBeauteScraper:
    """Collect canonical raw records from the catalogue and product pages."""

    def __init__(
        self,
        start_url: str = DEFAULT_CATALOG_URL,
        max_pages: int | None = None,
        *,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        delay: float = 0.0,
        session=None,
    ) -> None:
        if not start_url:
            raise ValueError("start_url must not be empty")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be greater than zero")
        if delay < 0:
            raise ValueError("delay must not be negative")
        self.start_url = start_url
        self.max_pages = max_pages
        self.request_timeout = request_timeout
        self.delay = delay
        self.session = session or create_session()
        self.products: list[dict[str, Any]] = []

    def fetch_page(self, url: str) -> str:
        try:
            response = self.session.get(url, timeout=self.request_timeout)
            response.raise_for_status()
            return response.text
        except Exception as error:
            LOGGER.exception("Request failed: %s", url)
            raise ScraperError(f"Unable to fetch Il de Beaute page: {url}") from error

    @staticmethod
    def _price_value(element) -> str:
        if not element:
            return ""
        value = element.get("content") or element.get_text(" ", strip=True)
        return re.sub(r"\D", "", clean_text(value))

    def _product_links(self, html: str) -> list[dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        products = []
        seen = set()
        for link in soup.select("a[href*='/product/']"):
            url = normalize_url(urljoin(BASE_URL, link.get("href", "")))
            name = normalize_name(
                link.get("aria-label") or link.get("title") or link.get_text(" ", strip=True)
            )
            if not name:
                image = link.select_one("img[alt]")
                name = normalize_name(image.get("alt") if image else "")
            if name and url and url not in seen:
                seen.add(url)
                products.append({"name": name, "url": url})
        return products

    def _product_record(self, product: dict[str, str]) -> dict[str, Any] | None:
        soup = BeautifulSoup(self.fetch_page(product["url"]), "html.parser")
        title = soup.select_one("h1")
        area = soup
        if title:
            for parent in title.parents:
                if parent.name in {"body", "html"}:
                    break
                text = clean_text(parent.get_text(" ", strip=True))
                if parent.select_one('[itemprop="price"]') or re.search(r"\d[\d\s]*¤", text):
                    area = parent
                    break
        current = self._price_value(area.select_one('[itemprop="price"]'))
        old = ""
        for selector in ("[class*='old-price']", "[class*='oldPrice']", "del", "s"):
            value = self._price_value(area.select_one(selector))
            if value and value != current:
                old = value
                break
        if not current:
            return None
        return raw_product(
            shop="iledebeaute",
            url=product["url"],
            name=normalize_name(title.get_text(" ", strip=True)) if title else product["name"],
            current_price=current,
            old_price=old,
        )

    @staticmethod
    def _next_page(soup: BeautifulSoup, current_url: str, visited: set[str]) -> str | None:
        current_query = dict(parse_qsl(urlsplit(current_url).query, keep_blank_values=True))
        current_number = int(next((v for k, v in current_query.items() if k.lower() in {"page", "pagen_1"} and v.isdigit()), "1"))
        candidates = []
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            text = clean_text(link.get_text(" ", strip=True)).lower()
            if not ("показать" in text or "pagen_1" in href.lower() or "page=" in href.lower()):
                continue
            candidate = urljoin(current_url, href)
            query = dict(parse_qsl(urlsplit(candidate).query, keep_blank_values=True))
            number = int(next((v for k, v in query.items() if k.lower() in {"page", "pagen_1"} and v.isdigit()), "1"))
            normalized = normalize_url(candidate)
            if number > current_number and normalized not in visited:
                candidates.append((number, candidate))
        return min(candidates)[1] if candidates else None

    def run(self) -> list[dict[str, Any]]:
        self.products = []
        current_url = self.start_url
        visited_pages: set[str] = set()
        seen_products: set[str] = set()
        while current_url and (self.max_pages is None or len(visited_pages) < self.max_pages):
            normalized_page = normalize_url(current_url)
            if normalized_page in visited_pages:
                break
            visited_pages.add(normalized_page)
            try:
                html = self.fetch_page(current_url)
            except ScraperError as error:
                LOGGER.warning("Stopping pagination: %s", error)
                break
            soup = BeautifulSoup(html, "html.parser")
            for product in self._product_links(html):
                product_key = normalize_url(product["url"])
                if product_key in seen_products:
                    continue
                seen_products.add(product_key)
                try:
                    record = self._product_record(product)
                except ScraperError as error:
                    LOGGER.warning("Skipping product %s: %s", product["url"], error)
                    continue
                if record:
                    self.products.append(record)
            current_url = self._next_page(soup, current_url, visited_pages)
        return self.products

    def save_to_jsonl(self, filename: str = "products.jsonl") -> None:
        save_jsonl(self.products, filename)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    scraper = IleDeBeauteScraper(max_pages=int(os.getenv("MAX_PAGES", "0")) or None)
    products = scraper.run()
    scraper.save_to_jsonl("products.jsonl")
    if os.getenv("PUBLISH_TO_KAFKA", "false").lower() == "true":
        publish_products("iledebeaute", products)
