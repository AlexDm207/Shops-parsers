"""Raw product scraper for the Podrygka online catalogue.

The module deliberately does not normalize prices, deduplicate products, or
write to a database.  Those operations belong to the project's downstream
normalizer and sink modules.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:  # pragma: no cover - exercised only when the optional dependency is absent
    cloudscraper = None

import requests


LOGGER = logging.getLogger(__name__)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class PodrygkaScraper:
    """Collect raw product dictionaries from Podrygka catalogue pages."""

    def __init__(
        self,
        start_url: str,
        max_pages: int | None = None,
        *,
        request_timeout: float = 30.0,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
        session: requests.Session | None = None,
    ) -> None:
        if not start_url:
            raise ValueError("start_url must not be empty")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be greater than zero")
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("delays must satisfy 0 <= min_delay <= max_delay")

        self.start_url = start_url
        self.max_pages = max_pages
        self.request_timeout = request_timeout
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.session = session or self._create_session()
        self.products: list[dict[str, Any]] = []
        self._requested_pages = 0

    @staticmethod
    def _create_session() -> requests.Session:
        """Create a browser-like session, preferring Cloudflare support."""
        if cloudscraper is not None:
            session = cloudscraper.create_scraper(browser="chrome")
        else:
            session = requests.Session()
        session.headers.update(
            {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )
        return session

    def _page_url(self, page: int) -> str:
        parts = urlsplit(self.start_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["page"] = str(page)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def fetch_page(self, url_or_page: str | int) -> str:
        """Fetch a URL or catalogue page number and return its response body."""
        url = self._page_url(url_or_page) if isinstance(url_or_page, int) else url_or_page
        LOGGER.info("Fetching page: %s", url)
        if self._requested_pages:
            time.sleep(random.uniform(self.min_delay, self.max_delay))
        try:
            response = self.session.get(url, timeout=self.request_timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            LOGGER.exception("Request failed: %s", url)
            raise RuntimeError(f"Unable to fetch Podrygka page: {url}") from exc
        self._requested_pages += 1
        LOGGER.info("Fetched page: status=%s bytes=%s", response.status_code, len(response.content))
        return response.text

    @staticmethod
    def _text(node: Any) -> str | None:
        if node is None:
            return None
        value = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node).strip()
        return value or None

    @staticmethod
    def _first_value(data: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            if data.get(key) is not None:
                return data[key]
        return None

    def _product_from_mapping(self, data: Mapping[str, Any], page_url: str) -> dict[str, Any] | None:
        name = self._first_value(data, "product_name", "name", "title", "productName")
        url = self._first_value(data, "product_url", "url", "link", "detailUrl")
        current = self._first_value(data, "price_current", "currentPrice", "price", "priceFinal")
        old = self._first_value(data, "price_old", "oldPrice", "basePrice", "priceOld")
        discount = self._first_value(data, "discount_label", "discount", "discountLabel", "action")
        pricing = data.get("pricing")
        offers = data.get("offers")
        if isinstance(pricing, Mapping):
            current = current if current is not None else self._first_value(pricing, "totalPrice", "price")
            old = old if old is not None else self._first_value(pricing, "basePrice", "oldPrice")
            discount_percent = pricing.get("discountPercent")
            if discount is None and discount_percent is not None:
                discount = f"-{discount_percent}%"
        if isinstance(offers, Mapping):
            current = current if current is not None else self._first_value(offers, "price", "lowPrice")
            old = old if old is not None else self._first_value(offers, "highPrice", "oldPrice")
        if not name or not url or current is None:
            return None
        return {
            "product_name": str(name).strip(),
            "product_url": urljoin(page_url, str(url).strip()),
            "price_current": current,
            "price_old": old,
            "discount_label": discount,
            "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }

    def _products_from_json(self, value: Any, page_url: str) -> Iterator[dict[str, Any]]:
        if isinstance(value, Mapping):
            product = self._product_from_mapping(value, page_url)
            if product is not None:
                yield product
            for child in value.values():
                yield from self._products_from_json(child, page_url)
        elif isinstance(value, list):
            for child in value:
                yield from self._products_from_json(child, page_url)

    def _parse_json_scripts(self, soup: BeautifulSoup, page_url: str) -> Iterator[dict[str, Any]]:
        for script in soup.select('script[type="application/ld+json"], script[type="application/json"]'):
            try:
                value = json.loads(script.string or script.get_text())
            except (TypeError, json.JSONDecodeError):
                continue
            yield from self._products_from_json(value, page_url)

    def _parse_next_data_scripts(self, soup: BeautifulSoup, page_url: str) -> Iterator[dict[str, Any]]:
        """Parse Podrygka's Next.js streaming payload containing catalogue offers."""
        for script in soup.find_all("script"):
            content = script.string or script.get_text()
            if not content.startswith("self.__next_f.push([1,"):
                continue
            try:
                encoded_payload = content[content.find("[1,") + 3 : -2]
                payload = json.loads(encoded_payload)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                LOGGER.warning("Could not decode Next.js payload: %s", exc)
                continue
            for line in payload.splitlines() if isinstance(payload, str) else ():
                if ":" not in line:
                    continue
                try:
                    value = json.loads(line.split(":", 1)[1])
                except json.JSONDecodeError:
                    continue
                yield from self._products_from_json(value, page_url)

    def _parse_cards(self, soup: BeautifulSoup, page_url: str) -> Iterator[dict[str, Any]]:
        selectors = (
            "[data-product-id]",
            ".product-item",
            ".catalog-item",
            ".product-card",
            "article.product",
        )
        seen_nodes: set[int] = set()
        for selector in selectors:
            for card in soup.select(selector):
                if id(card) in seen_nodes:
                    continue
                seen_nodes.add(id(card))
                link_node = card.select_one("a[href]")
                name_node = card.select_one(
                    "[itemprop='name'], [data-product-name], .product-name, .product-title, a.title"
                ) or link_node
                current_node = card.select_one("[itemprop='price'], [data-price], .price-current, .current-price, .price")
                old_node = card.select_one(
                    "del.text-inherit, [data-old-price], .price-old, .old-price, del, s"
                )
                discount_node = card.select_one(
                    "span.text-white.w-full.text-inherit, [data-discount], .discount, .sale, .badge"
                )
                raw = {
                    "name": self._text(name_node),
                    "url": link_node.get("href") if link_node else None,
                    "price": self._text(current_node),
                    "oldPrice": self._text(old_node),
                    "discount": self._text(discount_node),
                }
                product = self._product_from_mapping(raw, page_url)
                if product is not None:
                    yield product

    def parse_products(self, html_or_json: str | bytes | Mapping[str, Any] | list[Any]) -> list[dict[str, Any]]:
        """Parse product records from HTML or an API JSON payload.

        Values are intentionally returned in their source form.  In particular,
        this method does not strip currency symbols or convert decimal formats.
        """
        page_url = self.start_url
        if isinstance(html_or_json, (Mapping, list)):
            return list(self._products_from_json(html_or_json, page_url))

        text = html_or_json.decode("utf-8", errors="replace") if isinstance(html_or_json, bytes) else html_or_json
        try:
            return list(self._products_from_json(json.loads(text), page_url))
        except (json.JSONDecodeError, TypeError):
            soup = BeautifulSoup(text, "html.parser")
            products = list(self._parse_next_data_scripts(soup, page_url))
            if not products:
                products.extend(self._parse_json_scripts(soup, page_url))
            if not products:
                products.extend(self._parse_cards(soup, page_url))
            return products

    def run(self) -> list[dict[str, Any]]:
        """Fetch pages in order until ``max_pages`` or an empty page is reached."""
        self.products = []
        page = 1
        while self.max_pages is None or page <= self.max_pages:
            try:
                body = self.fetch_page(page)
            except RuntimeError as exc:
                LOGGER.warning("%s", exc)
                break
            page_products = self.parse_products(body)
            LOGGER.info("Parsed page %s: products=%s", page, len(page_products))
            if not page_products:
                LOGGER.warning("No products found on page %s; stopping pagination", page)
                break
            self.products.extend(page_products)
            page += 1
        return self.products

    def save_to_jsonl(self, filename: str = "products.jsonl") -> None:
        """Write collected raw records as UTF-8 JSON Lines."""
        LOGGER.info("Saving %s products to %s", len(self.products), filename)
        with open(filename, "w", encoding="utf-8", newline="\n") as output:
            for product in self.products:
                output.write(json.dumps(product, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    scraper = PodrygkaScraper(
        "https://www.podrygka.ru/catalog/?page=1",
        max_pages=3,
    )
    scraper.run()
    scraper.save_to_jsonl("products.jsonl")