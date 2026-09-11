"""Scrape discounted products from M.Video promotion pages."""

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
SHOP = "mvideo"
BASE_URL = "https://www.mvideo.ru"
ACTION_URLS = (
    f"{BASE_URL}/promo/promocatalog",
    f"{BASE_URL}/promo/totalnaya-likvidatsiya",
    f"{BASE_URL}/promo/luchshie-predlojeniya",
    f"{BASE_URL}/promo/skidki-teplo",
    f"{BASE_URL}/promo/novogodnyaya-skidka-na-apple-ipad-i-macbook",
    f"{BASE_URL}/promo/promocatalog?from=marketplace",
    f"{BASE_URL}/promo/skidki-na-tovary-dlya-krasoty",
    f"{BASE_URL}/promo/skidki-do-40-na-uhod-za-odezhdoi-mark200473000",
)


class MVideoScraper:
    """Walk each configured M.Video promotion and its product pages."""

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
        self.action_urls = tuple(dict.fromkeys(action_urls))
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
        return absolute if host == "mvideo.ru" or host.endswith(".mvideo.ru") else None

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
                    raise ScraperError(f"Unable to fetch M.Video page: {url}") from error
            if attempt == self.max_retries:
                REQUEST_ERRORS.labels(shop=SHOP).inc()
                raise ScraperError(f"Unable to fetch M.Video page after retries: {url}")
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

    def _request_in_browser(self, url: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover
            raise ScraperError("M.Video requires Playwright for browser fallback") from error

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
            for card in soup.select("mvid-product-card, div.product-card, [data-product-id]"):
                link = card.select_one(
                    "mvid-gallery a[href*='/products/'], mvid-product-title a[href*='/products/'], a[href*='/products/']"
                )
                name = card.select_one(
                    "mvid-product-title a, .product-mini-card__name a, .product-title, "
                    "[itemprop='name'], h2, h3"
                )
                current = card.select_one(
                    "mvid-sale-price, .price__main-value, .price-current, "
                    "[data-current-price], [itemprop='price']"
                )
                old = card.select_one(
                    "mvid-base-price, .price__old-value, .price-old, del, s, [data-old-price]"
                )
                discount = card.select_one(
                    "mvid-discount, .price__discount, .discount, [data-discount], "
                    "mui-badge.regular-badge, .product-label__text"
                )
                product = self._product_from_mapping(
                    {
                        "name": name.get_text(" ", strip=True) if name else None,
                        "url": link.get("href") if link else None,
                        "price": current.get_text(" ", strip=True) if current else None,
                        "oldPrice": old.get_text(" ", strip=True) if old else None,
                        "discount": discount.get_text(" ", strip=True) if discount else None,
                    },
                    page_url,
                )
                if product:
                    products.append(product)
        if not products:
            for link in soup.select("a[href*='/products/']"):
                card = link.find_parent("article")
                if card is None:
                    for parent in link.parents:
                        text = self._clean(parent.get_text(" ", strip=True))
                        if "₽" in text and len(text) <= 1200:
                            card = parent
                            break
                if card is None:
                    continue
                text = self._clean(card.get_text(" ", strip=True))
                prices = re.findall(r"\d[\d\s]*[,.]?\d*\s*₽", text)
                discount = re.search(r"-\s*\d+\s*%", text)
                product = self._product_from_mapping(
                    {
                        "name": link.get_text(" ", strip=True).replace("Открыть карточку товара", "").strip(),
                        "url": link.get("href"),
                        "price": prices[0] if prices else None,
                        "oldPrice": prices[1] if len(prices) > 1 else None,
                        "discount": discount.group(0) if discount else None,
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
        for link in soup.select("a[href], button[data-href], button[data-url]"):
            href = link.get("href") or link.get("data-href") or link.get("data-url")
            candidate = self._absolute(href, current_url)
            if not candidate or candidate in visited:
                continue
            query = dict(parse_qsl(urlsplit(candidate).query, keep_blank_values=True))
            page = int(query.get("page", "0") or 0)
            text = self._clean(link.get_text(" ", strip=True)).lower()
            if page == current_page + 1 or "показать еще" in text or "показать ещё" in text or "след" in text:
                candidates.append((page or current_page + 1, candidate))
        if candidates:
            return min(candidates)[1]
        if "Показать еще" in html or "Показать ещё" in html:
            parts = urlsplit(current_url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["page"] = str(current_page + 1)
            return urlunsplit(parts._replace(query=urlencode(query)))
        return None

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
                LOGGER.warning("Skipping M.Video promotion page %s: %s", page_url, error)
                try:
                    html = self._request_in_browser(page_url)
                except ScraperError as browser_error:
                    LOGGER.warning("Browser fallback failed for %s: %s", page_url, browser_error)
                    break
            page_products = self._parse_products(html, page_url)
            if not page_products:
                try:
                    browser_html = self._request_in_browser(page_url)
                    page_products = self._parse_products(browser_html, page_url)
                    if page_products:
                        html = browser_html
                except ScraperError as browser_error:
                    LOGGER.warning("Browser fallback failed for %s: %s", page_url, browser_error)
            products.extend(page_products)
            next_page = self._next_page(html, page_url, visited)
            if not page_products and next_page:
                LOGGER.warning("No products found on %s; stopping pagination", page_url)
                break
            page_url = next_page
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
        LOGGER.info("Collected %s products from %s M.Video promotions", len(self.products), len(self.action_urls))
        return self.products

    def save_to_jsonl(self, filename: str = "mvideo_products.jsonl") -> int:
        if not self.products:
            self.run()
        save_jsonl(self.products, filename)
        return len(self.products)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = MVideoScraper(max_pages_per_action=None)
    scraper.save_to_jsonl("mvideo_products.jsonl")
    if os.getenv("PUBLISH_TO_KAFKA", "false").lower() == "true":
        publish_products(SHOP, scraper.products)
