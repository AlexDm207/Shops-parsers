"""Raw product loader for the RIV GOSH promotions page.

The loader deliberately does not normalize prices, deduplicate products, or write
anything except optional JSONL output. Those responsibilities belong to the
pipeline's normalizer and sink modules.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterator, List, Mapping, Optional, Union
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from requests.exceptions import RequestException

try:
    from ..scraper_common import (
        REQUEST_TIMEOUT_SECONDS,
        create_session,
        has_promotion,
        publish_products,
        raw_product,
        save_jsonl,
    )
except ImportError:
    from scraper_common import (
        REQUEST_TIMEOUT_SECONDS,
        create_session,
        has_promotion,
        publish_products,
        raw_product,
        save_jsonl,
    )

# use cloudscraper when available, otherwise use requests.
try:
    import cloudscraper
except ImportError:
    cloudscraper = None
    import requests


PagePayload = Union[str, Mapping[str, Any], List[Any]]


class RiveGaucheScraper:
    def __init__(
        self,
        start_url: str,
        max_pages: Optional[int] = None,
        delay: float = 1.0,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        api_url_template: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        if not start_url:
            raise ValueError("start_url must not be empty")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if delay < 0:
            raise ValueError("delay must not be negative")

        # keep loader settings and create one HTTP session.
        self.start_url = start_url
        self.max_pages = max_pages
        self.delay = delay
        self.timeout = timeout
        self.api_url_template = api_url_template
        self.session = create_session()
        self.session.headers.update(headers or {})
        self.headers = dict(self.session.headers)

    def _page_url(self, page: int) -> str:
        """Return start_url with its currentPage query parameter replaced."""
        # replace only the page parameter and keep the remaining URL.
        parsed = urlparse(self.start_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["currentPage"] = str(page)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _product_url(self, value: Any) -> Optional[str]:
        """Return a same-site product URL or None for an external link."""
        if not value:
            return None
        product_url = urljoin(self.start_url, str(value).strip())
        host = urlparse(product_url).netloc.lower().split(":", 1)[0]
        if host == "rivegauche.ru" or host.endswith(".rivegauche.ru"):
            return product_url
        return None

    def _request(self, url: str) -> PagePayload:
        # detect the payload format from headers and content.
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        text = response.text.strip()
        if "json" in content_type or text.startswith(("{", "[")):
            try:
                return response.json()
            except ValueError:
                pass
        return response.text

    def _request_in_browser(self, url: str) -> str:
        """Load public HTML in Chromium when the direct request is refused."""
        # use a browser fallback when the WAF blocks the HTTP client.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "HTTP-запрос получил 503. Установите браузерный fallback: "
                "python -m pip install playwright; python -m playwright install chromium"
            ) from error

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=self.headers["User-Agent"],
                    locale="ru-RU",
                )
                page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
                # wait for Angular to render product cards.
                try:
                    page.wait_for_selector("product-item", timeout=5000)
                except Exception:
                    # an empty page marks the end of pagination.
                    pass
                page.wait_for_timeout(500)
                # retry while the DOM is still changing.
                for attempt in range(3):
                    try:
                        return page.content()
                    except Exception:
                        if attempt == 2:
                            raise
                        page.wait_for_timeout(500)
            finally:
                browser.close()

    def fetch_page(self, url_or_page: Union[str, int]) -> PagePayload:
        # use the API template when configured, otherwise load the promo page.
        page = url_or_page if isinstance(url_or_page, int) else None
        page_url = self._page_url(page) if page is not None else url_or_page
        if self.api_url_template:
            api_url = self.api_url_template.format(page=page or 1, currentPage=page or 1)
            try:
                payload = self._request(api_url)
                if isinstance(payload, (Mapping, list)):
                    return payload
            except Exception:
                pass
        try:
            return self._request(page_url)
        except RequestException as error:
            # retry the same public page through Chromium after a network error.
            return self._request_in_browser(page_url)

    @staticmethod
    def _first_value(item: Mapping[str, Any], *keys: str) -> Any:
        # return the first non-empty value from the API field candidates.
        for key in keys:
            value = item.get(key)
            if value not in (None, "", []):
                return value
        return None

    @staticmethod
    def _as_text(value: Any) -> Optional[str]:
        # convert nested price or discount values to text.
        if value is None:
            return None
        if isinstance(value, Mapping):
            value = value.get("value") or value.get("amount") or value.get("text")
        return str(value).strip() if value is not None else None

    def _product_from_mapping(self, item: Mapping[str, Any]) -> Optional[dict]:
        # build one raw object without normalizing prices.
        title = self._first_value(item, "raw_title", "title", "name", "productName", "displayName")
        url = self._first_value(item, "product_url", "url", "link", "productUrl", "canonicalUrl")
        current = self._first_value(item, "price_current", "currentPrice", "salePrice", "price", "finalPrice")
        old = self._first_value(item, "price_old", "oldPrice", "regularPrice", "basePrice", "old_price")
        discount = self._first_value(item, "discount_label", "discount", "discountLabel", "badge", "saleLabel")
        if not title or not url or current is None or not has_promotion(current, old, discount):
            return None
        product_url = self._product_url(url)
        if not product_url:
            return None
        return raw_product(
            shop="rivegosh",
            url=product_url,
            name=self._as_text(title) or "",
            current_price=current,
            old_price=old,
            discount=self._as_text(discount),
        )

    def _walk_json(self, value: Any) -> Iterator[dict]:
        # recursively find product objects in nested JSON.
        if isinstance(value, Mapping):
            product = self._product_from_mapping(value)
            if product:
                yield product
            for child in value.values():
                yield from self._walk_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_json(child)

    def _parse_html(self, html: str) -> List[dict]:
        # extract products from common store card layouts.
        soup = BeautifulSoup(html, "html.parser")
        products: List[dict] = []
        seen_urls = set()
        cards = soup.select(
            "[data-product-id], [data-product], .product-card, .product-item, "
            "product-item, article, li[class*='product'], div[class*='product-card']"
        )
        for card in cards:
            link = card.select_one("a[href]")
            if not link:
                continue
            title_node = card.select_one(
                "[data-product-title], .product-title, .product-name, .name, h2, h3"
            )
            prices = card.select("[data-price], .price-container .price, .price, [class*='price']")
            current_node = card.select_one(
                "[data-current-price], .price-current, .price-container .price, [class*='current']"
            )
            old_node = card.select_one("[data-old-price], .price-old, del, s, [class*='old']")
            discount_node = card.select_one(
                "[data-discount], .sc-offer, .discount, [class*='discount'], [class*='badge']"
            )
            current_text = current_node.get_text(" ", strip=True) if current_node else (
                prices[0].get_text(" ", strip=True) if prices else None
            )
            if not title_node or not current_text:
                continue
            product_data = {
                "name": title_node.get_text(" ", strip=True),
                "url": self._product_url(link.get("href", "")),
                "current": current_text,
                "old": old_node.get_text(" ", strip=True) if old_node else None,
                "discount": discount_node.get_text(" ", strip=True) if discount_node else None,
            }
            if not product_data["url"] or not has_promotion(
                product_data["current"], product_data["old"], product_data["discount"]
            ):
                continue
            product = raw_product(
                shop="rivegosh",
                url=product_data["url"],
                name=product_data["name"],
                current_price=product_data["current"],
                old_price=product_data["old"],
                discount=product_data["discount"],
            )
            if product["source_url"] not in seen_urls:
                seen_urls.add(product["source_url"])
                products.append(product)

        # inspect JSON-LD and embedded application state.
        for script in soup.select("script[type='application/ld+json'], script#__NEXT_DATA__, script[type='application/json']"):
            try:
                products.extend(self._walk_json(json.loads(script.string or script.get_text())))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return self._unique(products)

    @staticmethod
    def _unique(products: List[dict]) -> List[dict]:
        # remove duplicate product URLs before sending to the pipeline.
        result = []
        seen = set()
        for product in products:
            if product["source_url"] not in seen:
                seen.add(product["source_url"])
                result.append(product)
        return result

    def parse_products(self, data_or_html: PagePayload) -> List[dict]:
        # choose the JSON or HTML parser from the payload type.
        if isinstance(data_or_html, (Mapping, list)):
            return self._unique(list(self._walk_json(data_or_html)))
        return self._parse_html(data_or_html)

    def run(self) -> List[dict]:
        # load pages until the catalog ends or the limit is reached.
        all_products: List[dict] = []
        page = 1
        while self.max_pages is None or page <= self.max_pages:
            if page > 1 and self.delay:
                time.sleep(self.delay)
            payload = self.fetch_page(page)
            products = self.parse_products(payload)
            if not products:
                break
            all_products.extend(products)
            page += 1
        self.products = self._unique(all_products)
        return self.products

    def fetch_all_products(self) -> List[dict]:
        # keep the familiar method name for the external pipeline.
        return self.run()

    def save_to_jsonl(self, filename: str) -> int:
        # write each raw object as one JSONL line.
        if not getattr(self, "products", None):
            self.run()
        save_jsonl(self.products, filename)
        return len(self.products)


if __name__ == "__main__":
    # provide a standalone command-line entry point.
    scraper = RiveGaucheScraper(
        start_url="https://rivegauche.ru/tags/sale?currentPage=1",
        max_pages=None,
        delay=1.5,
    )
    count = scraper.save_to_jsonl("products.jsonl")
    print(f"Собрано товаров: {count}. Результат: products.jsonl")
    if os.getenv("PUBLISH_TO_KAFKA", "false").lower() == "true":
        publish_products("rivegosh", scraper.run())
