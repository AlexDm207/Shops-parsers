"""Scrape discounted products from all Pyaterochka catalog categories."""

from __future__ import annotations

import json
import logging
import os
import re
import time
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
SHOP = "pyaterochka"
BASE_URL = "https://5ka.ru"
CATEGORY_URLS = (
    f"{BASE_URL}/catalog/pyatyorochka-vyruchaet--251C17045/",
    f"{BASE_URL}/catalog/gotovaya-eda--251C12884/",
    f"{BASE_URL}/catalog/ovoshchi-frukty-orekhi--251C51627/",
    f"{BASE_URL}/catalog/molochnye-produkty-yaytsa--251C51940/",
    f"{BASE_URL}/catalog/khleb-i-vypechka--251C12888/",
    f"{BASE_URL}/catalog/myaso-ptitsa-kolbasy--251C52037/",
    f"{BASE_URL}/catalog/ryba-i-moreprodukty--251C12890/",
    f"{BASE_URL}/catalog/sladosti--251C12900/",
    f"{BASE_URL}/catalog/sneki-i-chipsy--251C12901/",
    f"{BASE_URL}/catalog/bakaleya--251C52954/",
    f"{BASE_URL}/catalog/zamorozhennye-produkty--251C52970/",
    f"{BASE_URL}/catalog/voda-i-napitki--251C12904/",
    f"{BASE_URL}/catalog/zdorovyy-vybor--251C12905/",
    f"{BASE_URL}/catalog/detskie-tovary--251C55979/",
    f"{BASE_URL}/catalog/dlya-zhivotnykh--251C12907/",
    f"{BASE_URL}/catalog/krasota-gigiena-apteka--251C55984/",
    f"{BASE_URL}/catalog/bytovaya-khimiya-uborka--251C55982/",
    f"{BASE_URL}/catalog/kukhnya-dom-dacha--251C55985/",
)


class PyaterochkaScraper:
    """Walk every category and retain only products with an actual promotion."""

    def __init__(
        self,
        category_urls: Iterable[str] = CATEGORY_URLS,
        max_pages_per_category: int | None = None,
        *,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        delay: float = 1.0,
        max_retries: int = 3,
        use_browser_fallback: bool = True,
        session: Session | None = None,
    ) -> None:
        self.category_urls = tuple(dict.fromkeys(category_urls))
        if not self.category_urls:
            raise ValueError("category_urls must not be empty")
        if max_pages_per_category is not None and max_pages_per_category < 1:
            raise ValueError("max_pages_per_category must be greater than zero")
        if request_timeout <= 0 or delay < 0 or max_retries < 0:
            raise ValueError("invalid request settings")
        self.max_pages_per_category = max_pages_per_category
        self.request_timeout = request_timeout
        self.delay = delay
        self.max_retries = max_retries
        self.use_browser_fallback = use_browser_fallback
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
        return absolute if host == "5ka.ru" or host.endswith(".5ka.ru") else None

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

    @staticmethod
    def _has_promotion(data: Mapping[str, Any], discount: Any, old_price: Any) -> bool:
        if discount not in (None, "", False, 0, "0", "0%"):
            return True
        if old_price not in (None, "", False, 0, "0"):
            return True
        for key in ("promo", "promotion", "promotions", "action", "isPromo", "is_promo", "sale"):
            if data.get(key) not in (None, "", False, [], {}):
                return True
        return False

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
                if response.status_code < 400:
                    return response.text
                if response.status_code not in (403, 429, 503):
                    response.raise_for_status()
                LOGGER.warning("Temporary HTTP %s for %s", response.status_code, url)
            except Timeout:
                LOGGER.warning("Timeout while fetching %s (attempt %s)", url, attempt + 1)
            except RequestException as error:
                error_response = getattr(error, "response", None)
                if error_response is None or error_response.status_code not in (403, 429, 503):
                    REQUEST_ERRORS.labels(shop=SHOP).inc()
                    raise ScraperError(f"Unable to fetch Pyaterochka page: {url}") from error
            if attempt == self.max_retries:
                REQUEST_ERRORS.labels(shop=SHOP).inc()
                raise ScraperError(f"Unable to fetch Pyaterochka page after retries: {url}")
            time.sleep(min(2**attempt, 8))
        raise AssertionError("unreachable")

    def _request_in_browser(self, url: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover
            raise ScraperError(
                "5ka.ru requires browser fallback; install playwright and Chromium"
            ) from error

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=self.session.headers.get("User-Agent", "Mozilla/5.0"),
                    locale="ru-RU",
                    extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9", "Referer": BASE_URL},
                )
                page.goto(url, wait_until="domcontentloaded", timeout=int(self.request_timeout * 1000))
                page.wait_for_timeout(2500)
                return page.content()
            finally:
                browser.close()

    def _json_scripts(self, soup: BeautifulSoup) -> Iterator[Any]:
        for script in soup.select(
            "script#__NEXT_DATA__, script[type='application/ld+json'], "
            "script[type='application/json'], script:not([src])"
        ):
            text = script.string or script.get_text()
            if not text or len(text) > 10_000_000:
                continue
            try:
                yield json.loads(text)
            except (TypeError, json.JSONDecodeError):
                continue

    def _product_from_mapping(self, data: Mapping[str, Any], page_url: str) -> dict[str, Any] | None:
        pricing_value = data.get("pricing") or data.get("priceData") or data.get("prices") or {}
        pricing = pricing_value if isinstance(pricing_value, Mapping) else {}
        name = self._value(data, "name", "product_name", "productName", "title", "displayName")
        url = self._value(data, "url", "product_url", "productUrl", "link", "canonicalUrl")
        current = self._value(data, "current_price", "price_current", "currentPrice", "salePrice", "promoPrice", "price", "finalPrice")
        old = self._value(data, "old_price", "price_old", "oldPrice", "regularPrice", "basePrice", "old")
        discount = self._value(data, "discount", "discount_label", "discountLabel", "discountPercent", "promoLabel", "badge")
        current = current if current is not None else self._value(pricing, "current", "sale", "final", "price")
        old = old if old is not None else self._value(pricing, "old", "regular", "base")
        product_url = self._absolute(url, page_url)
        current_price = self._price(current)
        old_price = self._price(old)
        if not name or not product_url or current_price is None or not self._has_promotion(data, discount, old_price):
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
                "[data-product-id], [data-product], .product-card, .product-item, "
                "article, li[class*='product'], div[class*='product']"
            )
            for card in cards:
                link = card.select_one("a[href*='/product/'], a[href*='/catalog/'], a[href]")
                name = card.select_one(
                    "[itemprop='name'], [data-product-name], .product-name, "
                    ".product-title, h2, h3"
                )
                current = card.select_one(
                    "[itemprop='price'], [data-current-price], .price-current, "
                    "[data-price], .price, [class*='price']"
                )
                old = card.select_one(
                    "[data-old-price], .price-old, .old-price, [class*='old-price'], del, s"
                )
                discount = card.select_one(
                    "[data-discount], .discount, .badge, [class*='discount'], [class*='sale'], "
                    "[class*='promo']"
                )
                data = {
                    "name": name.get_text(" ", strip=True) if name else None,
                    "url": link.get("href") if link else None,
                    "price": current.get("content") or current.get_text(" ", strip=True) if current else None,
                    "oldPrice": old.get("content") or old.get_text(" ", strip=True) if old else None,
                    "discount": discount.get_text(" ", strip=True) if discount else None,
                }
                product = self._product_from_mapping(data, page_url)
                if product:
                    products.append(product)
        return self._unique(products)

    def _next_page(self, html: str, current_url: str, visited: set[str]) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        current_page = int(dict(parse_qsl(urlsplit(current_url).query)).get("page", "1") or 1)
        candidates: list[tuple[int, str]] = []
        for link in soup.select("a[rel='next'][href], a[aria-label*='След'][href], a[href], button[data-url]"):
            href = link.get("href") or link.get("data-url")
            candidate = self._absolute(href, current_url)
            if not candidate or candidate in visited:
                continue
            query = dict(parse_qsl(urlsplit(candidate).query, keep_blank_values=True))
            page = int(query.get("page", "0") or 0)
            text = self._clean(link.get_text(" ", strip=True)).lower()
            if page == current_page + 1 or "след" in text or "далее" in text or "показать еще" in text or "показать ещё" in text:
                candidates.append((page or current_page + 1, candidate))
        return min(candidates)[1] if candidates else None

    def _products_for_category(self, category_url: str) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        visited: set[str] = set()
        page_url: str | None = category_url
        browser_required = False
        while page_url and page_url not in visited:
            if self.max_pages_per_category is not None and len(visited) >= self.max_pages_per_category:
                break
            visited.add(page_url)
            try:
                html = self._request(page_url, category_url)
            except ScraperError as error:
                if not self.use_browser_fallback:
                    LOGGER.warning("Skipping Pyaterochka category page %s: %s", page_url, error)
                    break
                browser_required = True
                try:
                    html = self._request_in_browser(page_url)
                except ScraperError as browser_error:
                    LOGGER.warning("Browser fallback failed for %s: %s", page_url, browser_error)
                    break
            if browser_required and not self._parse_products(html, page_url):
                LOGGER.warning("No products found on Pyaterochka page %s", page_url)
            page_products = self._parse_products(html, page_url)
            if not page_products and self.use_browser_fallback and not browser_required:
                try:
                    browser_html = self._request_in_browser(page_url)
                    page_products = self._parse_products(browser_html, page_url)
                    if page_products:
                        html = browser_html
                except ScraperError as browser_error:
                    LOGGER.warning("Browser fallback failed for %s: %s", page_url, browser_error)
            products.extend(page_products)
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
            for category_url in self.category_urls
            for product in self._products_for_category(category_url)
        )
        LOGGER.info("Collected %s promoted products from %s Pyaterochka categories", len(self.products), len(self.category_urls))
        return self.products

    def save_to_jsonl(self, filename: str = "pyaterochka_products.jsonl") -> int:
        if not self.products:
            self.run()
        save_jsonl(self.products, filename)
        return len(self.products)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = PyaterochkaScraper(max_pages_per_category=None)
    scraper.save_to_jsonl("pyaterochka_products.jsonl")
    if os.getenv("PUBLISH_TO_KAFKA", "false").lower() == "true":
        publish_products(SHOP, scraper.products)
