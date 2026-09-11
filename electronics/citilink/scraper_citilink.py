"""Citilink promotions scraper integrated with promotion-scrapper-master."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import parse_qsl, urljoin, urlsplit

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
SHOP = "citilink"
ACTIONS_URL = "https://www.citilink.ru/actions/"
ALLOWED_ACTION_TYPES = ("скидка на товар", "скидки на товары", "скидки")
EXCLUDED_ACTION_TYPES = ("бонус", "кешбэк", "кэшбэк", "рассроч", "услуг")


class CitilinkScraper:
    """Discover product-discount subactions and scrape their product pages."""

    def __init__(
        self,
        actions_url: str = ACTIONS_URL,
        max_action_pages: int | None = None,
        *,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        min_delay: float = 1.0,
        max_delay: float = 2.5,
        max_retries: int = 3,
        session: Session | None = None,
    ) -> None:
        if not actions_url:
            raise ValueError("actions_url must not be empty")
        if max_action_pages is not None and max_action_pages < 1:
            raise ValueError("max_action_pages must be greater than zero")
        if request_timeout <= 0 or min_delay < 0 or max_delay < min_delay:
            raise ValueError("invalid timeout or delay settings")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")

        self.actions_url = actions_url
        self.max_action_pages = max_action_pages
        self.request_timeout = request_timeout
        self.min_delay = min_delay
        self.max_delay = max_delay
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
        return absolute if host == "citilink.ru" or host.endswith(".citilink.ru") else None

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
        """Fetch a page with retry handling for rate limiting and transient errors."""
        for attempt in range(self.max_retries + 1):
            if self._last_request:
                time.sleep(self.min_delay if self.min_delay == self.max_delay else self.min_delay)
            self._last_request = True
            try:
                response: Response = self.session.get(
                    url,
                    headers={
                        "User-Agent": self.session.headers.get("User-Agent", "Mozilla/5.0"),
                        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                        "Accept-Language": "ru-RU,ru;q=0.9",
                        "Referer": referer,
                        "Cache-Control": "no-cache",
                    },
                    timeout=self.request_timeout,
                )
                if response.status_code not in (429, 503):
                    response.raise_for_status()
                    return response.text
            except Timeout:
                LOGGER.warning("Timeout while fetching %s (attempt %s)", url, attempt + 1)
            except RequestException as error:
                if getattr(error, "response", None) is not None and error.response.status_code in (429, 503):
                    LOGGER.warning("Temporary HTTP %s for %s", error.response.status_code, url)
                else:
                    REQUEST_ERRORS.labels(shop=SHOP).inc()
                    raise ScraperError(f"Unable to fetch Citilink page: {url}") from error

            if attempt == self.max_retries:
                REQUEST_ERRORS.labels(shop=SHOP).inc()
                raise ScraperError(f"Unable to fetch Citilink page after retries: {url}")
            time.sleep(min(2**attempt, 8))
        raise AssertionError("unreachable")

    def _request_in_browser(self, url: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover
            raise ScraperError(
                "Citilink returned a JavaScript-only page; install playwright and Chromium"
            ) from error

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=self.session.headers.get("User-Agent", "Mozilla/5.0"),
                    locale="ru-RU",
                    extra_http_headers={
                        "Accept-Language": "ru-RU,ru;q=0.9",
                        "Referer": self.actions_url,
                    },
                )
                page.goto(url, wait_until="domcontentloaded", timeout=int(self.request_timeout * 1000))
                page.wait_for_timeout(2000)
                return page.content()
            finally:
                browser.close()

    def _product_from_mapping(self, data: Mapping[str, Any], page_url: str) -> dict[str, Any] | None:
        pricing_value = data.get("pricing") or data.get("priceData")
        pricing = pricing_value if isinstance(pricing_value, Mapping) else {}
        current = self._value(data, "price_current", "currentPrice", "salePrice", "finalPrice", "price")
        old = self._value(data, "price_old", "oldPrice", "regularPrice", "basePrice")
        current = current if current is not None else self._value(pricing, "current", "sale", "final", "price")
        old = old if old is not None else self._value(pricing, "old", "regular", "base")
        name = self._value(data, "product_name", "productName", "name", "title", "displayName")
        url = self._value(data, "product_url", "productUrl", "url", "link", "canonicalUrl")
        discount = self._value(data, "discount_label", "discountLabel", "discount", "promoCode", "badge")
        if discount is None:
            percent = self._value(data, "discountPercent", "discount_percentage")
            if percent is not None:
                discount = f"-{percent}%"
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

    def _walk_products(self, value: Any, page_url: str) -> Iterator[dict[str, Any]]:
        if isinstance(value, Mapping):
            product = self._product_from_mapping(value, page_url)
            if product:
                yield product
            for child in value.values():
                yield from self._walk_products(child, page_url)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_products(child, page_url)

    def _json_scripts(self, soup: BeautifulSoup) -> Iterator[Any]:
        for script in soup.select("script#__NEXT_DATA__, script[type='application/ld+json'], script[type='application/json']"):
            try:
                yield json.loads(script.string or script.get_text())
            except (TypeError, json.JSONDecodeError):
                continue

    def _parse_products(self, html: str, page_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        products = [
            product
            for payload in self._json_scripts(soup)
            for product in self._walk_products(payload, page_url)
        ]
        if not products:
            for card in soup.select("[data-product-id], [data-product], .product-card, .product-item, article"):
                link = card.select_one("a[href*='/product/'], a[href]")
                name = card.select_one("[itemprop='name'], [data-product-name], .product-name, .product-title, h2, h3")
                current = card.select_one("[itemprop='price'], [data-current-price], .price-current, [data-price], .price, [class*='ActualInfo']")
                old = card.select_one("[data-old-price], .price-old, .old-price, [class*='old-price'], del, s")
                discount = card.select_one("[data-discount], .discount, .badge, [class*='discount'], [class*='Discount']")
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

    def _is_product_action(self, text: str) -> bool:
        normalized = self._clean(text).lower()
        if any(excluded in normalized for excluded in EXCLUDED_ACTION_TYPES):
            return False
        return not normalized or any(allowed in normalized for allowed in ALLOWED_ACTION_TYPES)

    def _action_links(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for payload in self._json_scripts(soup):
            links.extend(self._action_links_from_json(payload))
        for link in soup.select("a[href*='/actions/']"):
            href = link.get("href")
            action_url = self._absolute(href, self.actions_url)
            if not action_url or urlsplit(action_url).path.rstrip("/") == "/actions":
                continue
            link_text = self._clean(link.get_text(" ", strip=True))
            if any(excluded in link_text.lower() for excluded in EXCLUDED_ACTION_TYPES):
                continue
            if any(allowed in link_text.lower() for allowed in ALLOWED_ACTION_TYPES):
                links.append(action_url)
                continue

            container = link
            found_type = False
            for _ in range(4):
                container = container.parent
                if container is None or container.name in {"body", "html"}:
                    break
                context = self._clean(container.get_text(" ", strip=True)).lower()
                if any(excluded in context for excluded in EXCLUDED_ACTION_TYPES):
                    continue
                if any(allowed in context for allowed in ALLOWED_ACTION_TYPES):
                    links.append(action_url)
                    found_type = True
                    break
            if not found_type:
                links.append(action_url)
        return list(dict.fromkeys(links))

    def _action_links_from_json(self, value: Any) -> Iterator[str]:
        if isinstance(value, Mapping):
            type_value = self._value(value, "actionType", "promotionType", "category", "type", "tags", "badges")
            type_text = self._clean(type_value).lower()
            action_url = self._absolute(
                self._value(value, "actionUrl", "promotionUrl", "url", "link", "href"),
                self.actions_url,
            )
            if action_url and urlsplit(action_url).path.rstrip("/") != "/actions" and self._is_product_action(type_text):
                yield action_url
            for child in value.values():
                yield from self._action_links_from_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._action_links_from_json(child)

    def _next_page(self, html: str, current_url: str, visited: set[str]) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        current_page = int(dict(parse_qsl(urlsplit(current_url).query)).get("page", "1") or 1)
        candidates: list[tuple[int, str]] = []
        for link in soup.select("a[rel='next'][href], a[aria-label*='След'][href], a[href]"):
            href = str(link.get("href") or "")
            candidate = self._absolute(href, current_url)
            if not candidate or candidate in visited:
                continue
            text = self._clean(link.get_text(" ", strip=True)).lower()
            page = int(dict(parse_qsl(urlsplit(candidate).query)).get("page", "0") or 0)
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
            if self.max_action_pages is not None and len(visited) >= self.max_action_pages:
                break
            visited.add(page_url)
            try:
                html = self._request(page_url, self.actions_url)
            except ScraperError as error:
                LOGGER.warning("HTTP action page failed for %s: %s", page_url, error)
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
        try:
            action_html = self._request(self.actions_url, self.actions_url)
        except ScraperError:
            action_html = self._request_in_browser(self.actions_url)
        action_urls = self._action_links(action_html)
        if not action_urls:
            try:
                browser_html = self._request_in_browser(self.actions_url)
                action_urls = self._action_links(browser_html)
            except ScraperError as browser_error:
                LOGGER.warning("Browser fallback failed for actions page: %s", browser_error)
        LOGGER.info("Discovered %s product-discount subactions", len(action_urls))
        self.products = self._unique(
            product
            for action_url in action_urls
            for product in self._products_for_action(action_url)
        )
        return self.products

    def save_to_jsonl(self, filename: str = "citilink_products.jsonl") -> int:
        if not self.products:
            self.run()
        save_jsonl(self.products, filename)
        return len(self.products)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = CitilinkScraper(max_action_pages=None)
    scraper.save_to_jsonl("citilink_products.jsonl")
    if os.getenv("PUBLISH_TO_KAFKA", "false").lower() == "true":
        publish_products(SHOP, scraper.products)
