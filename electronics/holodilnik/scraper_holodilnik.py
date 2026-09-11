"""Scrape discounted products from Holodilnik.ru promotion pages."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from requests import Response, Session
from requests.exceptions import RequestException, Timeout

try:
    from .scraper_runtime import (
        REQUEST_ERRORS,
        REQUEST_TIMEOUT_SECONDS,
        ScraperError,
        create_session,
        publish_products,
        raw_product,
        save_jsonl,
    )
except ImportError:
    from scraper_runtime import (
        REQUEST_ERRORS,
        REQUEST_TIMEOUT_SECONDS,
        ScraperError,
        create_session,
        publish_products,
        raw_product,
        save_jsonl,
    )

LOGGER = logging.getLogger(__name__)
SHOP = "holodilnik"
BASE_URL = "https://www.holodilnik.ru"
ACTION_URLS = (
    f"{BASE_URL}/action/washing_machines_skidki/",
    f"{BASE_URL}/action/tv_skidki/",
    f"{BASE_URL}/action/refridgerators_skidki/",
)


class HolodilnikScraper:
    """Walk each configured promotion page and its product pagination."""

    def __init__(
        self,
        action_urls: Iterable[str] = ACTION_URLS,
        max_pages_per_action: int | None = None,
        *,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        delay: float = 1.0,
        max_retries: int = 3,
        session: Session | None = None,
    ) -> None:
        self.action_urls = tuple(action_urls)
        if not self.action_urls:
            raise ValueError("action_urls must not be empty")
        if max_pages_per_action is not None and max_pages_per_action < 1:
            raise ValueError("max_pages_per_action must be greater than zero")
        if request_timeout <= 0 or delay < 0 or max_retries < 0:
            raise ValueError("invalid request settings")
        self.max_pages_per_action = max_pages_per_action
        self.request_timeout = request_timeout
        self.delay = delay
        self.max_retries = max_retries
        self.session = session or create_session()
        self.products: list[dict[str, Any]] = []
        self._last_request = False

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()

    @staticmethod
    def _absolute(url: Any, base_url: str) -> str | None:
        if not url:
            return None
        absolute = urljoin(base_url, str(url).strip())
        host = urlsplit(absolute).netloc.lower().split(":", 1)[0]
        return absolute if host == "holodilnik.ru" or host.endswith(".holodilnik.ru") else None

    @staticmethod
    def _value(data: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            if data.get(key) not in (None, "", []):
                return data[key]
        return None

    @staticmethod
    def _price(value: Any) -> str | None:
        if isinstance(value, Mapping):
            value = value.get("value") or value.get("amount") or value.get("price")
        text = re.sub(r"\s+", "", str(value or "").replace("\xa0", ""))
        match = re.search(r"\d[\d.,]*", text)
        if not match:
            return None
        number = match.group(0)
        separator = max(number.rfind(","), number.rfind("."))
        if separator >= 0 and len(number) - separator - 1 in (1, 2):
            return re.sub(r"[.,]", "", number[:separator]) + "." + number[separator + 1:]
        return re.sub(r"[.,]", "", number)

    def _request(self, url: str, referer: str) -> str:
        for attempt in range(self.max_retries + 1):
            if self._last_request and self.delay:
                time.sleep(self.delay)
            self._last_request = True
            try:
                response: Response = self.session.get(
                    url,
                    headers={
                        "User-Agent": self.session.headers.get("User-Agent", "Mozilla/5.0"),
                        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                        "Accept-Language": "ru-RU,ru;q=0.9",
                        "Referer": referer,
                    },
                    timeout=self.request_timeout,
                )
                if response.status_code not in (429, 503):
                    response.raise_for_status()
                    return response.text
                LOGGER.warning("Temporary HTTP %s for %s", response.status_code, url)
            except Timeout:
                LOGGER.warning("Timeout while fetching %s (attempt %s)", url, attempt + 1)
            except RequestException as error:
                error_response = getattr(error, "response", None)
                if error_response is None or error_response.status_code not in (429, 503):
                    REQUEST_ERRORS.labels(shop=SHOP).inc()
                    raise ScraperError(f"Unable to fetch Holodilnik page: {url}") from error
            if attempt == self.max_retries:
                REQUEST_ERRORS.labels(shop=SHOP).inc()
                raise ScraperError(f"Unable to fetch Holodilnik page after retries: {url}")
            time.sleep(min(2**attempt, 8))
        raise AssertionError("unreachable")

    def _json_scripts(self, soup: BeautifulSoup) -> Iterator[Any]:
        for script in soup.select(
            "script#__NEXT_DATA__, script[type='application/ld+json'], script[type='application/json']"
        ):
            try:
                yield json.loads(script.string or script.get_text())
            except (TypeError, json.JSONDecodeError):
                continue

    def _product_from_mapping(
        self, data: Mapping[str, Any], page_url: str
    ) -> dict[str, Any] | None:
        pricing_value = data.get("pricing") or data.get("priceData") or {}
        pricing = pricing_value if isinstance(pricing_value, Mapping) else {}
        name = self._value(data, "name", "product_name", "productName", "title", "displayName")
        url = self._value(data, "url", "product_url", "productUrl", "link", "canonicalUrl")
        current = self._value(data, "current_price", "price_current", "currentPrice", "salePrice", "price", "finalPrice")
        old = self._value(data, "old_price", "price_old", "oldPrice", "regularPrice", "basePrice")
        current = current if current is not None else self._value(pricing, "current", "sale", "final", "price")
        old = old if old is not None else self._value(pricing, "old", "regular", "base")
        discount = self._value(data, "discount", "discount_label", "discountLabel", "badge")
        product_url = self._absolute(url, page_url)
        current_price = self._price(current)
        old_price = self._price(old)
        if not name or not product_url or current_price is None or (old_price is None and not discount):
            return None
        return raw_product(
            shop=SHOP,
            url=product_url,
            name=self._clean(name),
            current_price=current_price,
            old_price=old_price,
            discount=self._clean(discount),
        )

    def _walk_json(self, value: Any, page_url: str) -> Iterator[dict[str, Any]]:
        if isinstance(value, Mapping):
            product = self._product_from_mapping(value, page_url)
            if product:
                yield product
            for child in value.values():
                yield from self._walk_json(child, page_url)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_json(child, page_url)

    def _parse_products(self, html: str, page_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        products = [
            product
            for payload in self._json_scripts(soup)
            for product in self._walk_json(payload, page_url)
        ]
        if not products:
            cards = soup.select(
                "div.a-prod, div.catalog-item, [data-product-id], [data-product], .product-card, "
                ".product-item, article, li[class*='product'], div[class*='product']"
            )
            for card in cards:
                link = card.select_one(
                    "a.catalog-item__name, a.catalog-item__image-link, a.a-prod-link, "
                    "a[href*='/product/'], a[href]"
                )
                name = card.select_one(
                    ".catalog-item__name, .catalog-item__title, .ap-model, "
                    "[itemprop='name'], [data-product-name], .product-name, "
                    ".product-title, h2, h3"
                )
                current = card.select_one(
                    ".price-value__actual, .ap-price-new, [itemprop='price'], "
                    "[data-current-price], .price-current, "
                    "[data-price], .price, [class*='price']"
                )
                old = card.select_one(
                    ".price-value__old, .ap-price-old, [data-old-price], .price-old, .old-price, "
                    "[class*='old-price'], del, s"
                )
                discount = card.select_one(
                    ".catalog-item__image-labels, .catalog-item__badges, .ap-discount, "
                    "[data-discount], .discount, .badge, "
                    "[class*='discount'], [class*='Discount']"
                )
                product = self._product_from_mapping(
                    {
                        "name": name.get_text(" ", strip=True) if name else None,
                        "url": link.get("href") if link else None,
                        "price": current.get("content") or current.get_text(" ", strip=True) if current else None,
                        "oldPrice": old.get("content") or old.get_text(" ", strip=True) if old else None,
                        "discount": discount.get_text(" ", strip=True) if discount else None,
                    },
                    page_url,
                )
                if product:
                    products.append(product)
        return self._unique(products)

    def _next_page(self, html: str, current_url: str, visited: set[str]) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        current_page = int(dict(parse_qsl(urlsplit(current_url).query)).get("page", "1") or 1)
        candidates: list[tuple[int, str]] = []
        for link in soup.select("a[rel='next'][href], a[aria-label*='След'][href], a[href]"):
            href = str(link.get("href") or "")
            candidate = self._absolute(href, current_url)
            if not candidate or candidate in visited:
                continue
            query = dict(parse_qsl(urlsplit(candidate).query, keep_blank_values=True))
            page = int(query.get("page", "0") or 0)
            text = self._clean(link.get_text(" ", strip=True)).lower()
            if link.get("rel") == ["next"] or "след" in text or "далее" in text:
                candidates.append((page or current_page + 1, candidate))
            elif page == current_page + 1:
                candidates.append((page, candidate))
        return min(candidates)[1] if candidates else None

    def _products_for_action(self, action_url: str) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        visited: set[str] = set()
        page_url: str | None = action_url
        while page_url and page_url not in visited:
            if self.max_pages_per_action is not None and len(visited) >= self.max_pages_per_action:
                break
            visited.add(page_url)
            try:
                html = self._request(page_url, action_url)
            except ScraperError as error:
                LOGGER.warning("Skipping Holodilnik action page %s: %s", page_url, error)
                break
            products.extend(self._parse_products(html, page_url))
            page_url = self._next_page(html, page_url, visited)
        return self._unique(products)

    @staticmethod
    def _unique(products: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for product in products:
            if product["source_url"] not in seen:
                seen.add(product["source_url"])
                result.append(product)
        return result

    def run(self) -> list[dict[str, Any]]:
        self.products = self._unique(
            product
            for action_url in self.action_urls
            for product in self._products_for_action(action_url)
        )
        LOGGER.info("Collected %s products from %s Holodilnik actions", len(self.products), len(self.action_urls))
        return self.products

    def save_to_jsonl(self, filename: str = "holodilnik_products.jsonl") -> int:
        if not self.products:
            self.run()
        save_jsonl(self.products, filename)
        return len(self.products)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = HolodilnikScraper(max_pages_per_action=None)
    scraper.save_to_jsonl("holodilnik_products.jsonl")
    if os.getenv("PUBLISH_TO_KAFKA", "false").lower() == "true":
        publish_products(SHOP, scraper.products)
