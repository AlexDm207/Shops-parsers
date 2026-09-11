"""Scrape promoted products from every Magnit promo-catalog category."""

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
SHOP = "magnit"
BASE_URL = "https://magnit.ru"
SHOP_CODE = "995010"
CATEGORY_URLS = (
    f"{BASE_URL}/promo-catalog?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/151-skidki-na-kategorii?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/149-novinki?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/27-ovoschi-i-fruktyi?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/22-moloko-syir-yajtsa?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/25-myaso-ptitsa-kolbasyi?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/23-bakaleya-sousyi?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/21-napitki?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/101-chaj-kofe-kakao?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/33-byitovaya-himiya?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/32-kosmetika-i-parfyumeriya?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/26-polufabrikatyi?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/28-sneki-orehi?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/30-konditerskie-izdeliya?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/31-gotovaya-eda?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/34-detyam?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/35-zootovaryi?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/41-raznyie-kategorii?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/122-gigiena?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/81-alkogol?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/143-zdorovoe-pitanie?shopCode={SHOP_CODE}",
    f"{BASE_URL}/promo-catalog/148-ryiba-i-moreproduktyi?shopCode={SHOP_CODE}",
)


class MagnitScraper:
    """Walk all requested catalog pages and retain only product promotions."""

    def __init__(
        self,
        category_urls: Iterable[str] = CATEGORY_URLS,
        max_pages_per_category: int | None = None,
        *,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        delay: float = 1.0,
        max_retries: int = 3,
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
        return absolute if host == "magnit.ru" or host.endswith(".magnit.ru") else None

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

    @classmethod
    def _promotion_fields(cls, text: str) -> tuple[str | None, str | None, str | None]:
        clean = cls._clean(text)
        prices = re.findall(r"\d[\d\s]*[,.]?\d*\s*(?:₽|руб\.?|р\.?)", clean, flags=re.I)
        discount = re.search(r"-\s*\d+\s*%", clean)
        current = cls._price(prices[0]) if prices else None
        old = cls._price(prices[1]) if len(prices) > 1 else None
        return current, old, discount.group(0) if discount else None

    def _request(self, url: str, referer: str) -> str:
        for attempt in range(self.max_retries + 1):
            if self._last_request and self.delay:
                time.sleep(self.delay)
            self._last_request = True
            try:
                response: Response = self.session.get(
                    url,
                    headers={"User-Agent": self.session.headers.get("User-Agent", "Mozilla/5.0"), "Accept-Language": "ru-RU,ru;q=0.9", "Referer": referer},
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
                REQUEST_ERRORS.labels(shop=SHOP).inc()
                raise ScraperError(f"Unable to fetch Magnit page: {url}") from error
            if attempt == self.max_retries:
                REQUEST_ERRORS.labels(shop=SHOP).inc()
                raise ScraperError(f"Unable to fetch Magnit page after retries: {url}")
            time.sleep(min(2**attempt, 8))
        raise AssertionError("unreachable")

    def _json_scripts(self, soup: BeautifulSoup) -> Iterator[Any]:
        for script in soup.select("script#__NUXT_DATA__, script#__NEXT_DATA__, script[type='application/json'], script[type='application/ld+json']"):
            text = script.string or script.get_text()
            if not text or len(text) > 10_000_000:
                continue
            try:
                yield json.loads(text)
            except (TypeError, json.JSONDecodeError):
                continue

    def _product_from_card(self, card: Any, page_url: str) -> dict[str, Any] | None:
        link = card.select_one("a[href*='/promo-product/']")
        if not link:
            return None
        product_url = self._absolute(link.get("href"), page_url)
        if not product_url:
            return None
        name_node = card.select_one("[itemprop='name'], [data-product-name], h2, h3, [class*='title'], [class*='name']")
        text = self._clean(card.get_text(" ", strip=True))
        current, old, discount = self._promotion_fields(text)
        if not name_node:
            name = self._clean(link.get_text(" ", strip=True))
        else:
            name = self._clean(name_node.get("content") or name_node.get_text(" ", strip=True))
        if not name or not current:
            return None
        return raw_product(shop=SHOP, url=product_url, name=name, current_price=current, old_price=old, discount=discount)

    def _parse_products(self, html: str, page_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        cards: list[Any] = []
        for link in soup.select("a[href*='/promo-product/']"):
            card = link.find_parent(["article", "li", "div"])
            if card is not None:
                cards.append(card)
        products = [product for card in cards if (product := self._product_from_card(card, page_url))]
        return self._unique(products)

    def _next_page(self, html: str, current_url: str, visited: set[str]) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        for element in soup.select("a[rel='next'][href], a[href*='page='], button[data-url], [data-next-page-url]"):
            href = element.get("href") or element.get("data-url") or element.get("data-next-page-url")
            candidate = self._absolute(href, current_url)
            if candidate and candidate not in visited:
                return candidate
        if soup.find(string=re.compile(r"показать ещё|показать еще", re.I)):
            parts = urlsplit(current_url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            page = int(query.get("page", "1") or 1) + 1
            query["page"] = str(page)
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        return None

    def _products_for_category(self, category_url: str) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        visited: set[str] = set()
        page_url: str | None = category_url
        while page_url and page_url not in visited:
            if self.max_pages_per_category is not None and len(visited) >= self.max_pages_per_category:
                break
            visited.add(page_url)
            try:
                html = self._request(page_url, category_url)
            except ScraperError as error:
                LOGGER.warning("Skipping Magnit category page %s: %s", page_url, error)
                break
            page_products = self._parse_products(html, page_url)
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
        LOGGER.info("Collected %s promoted products from %s Magnit categories", len(self.products), len(self.category_urls))
        return self.products

    def save_to_jsonl(self, filename: str = "magnit_products.jsonl") -> int:
        if not self.products:
            self.run()
        save_jsonl(self.products, filename)
        return len(self.products)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = MagnitScraper(max_pages_per_category=None)
    scraper.save_to_jsonl("magnit_products.jsonl")
    if os.getenv("PUBLISH_TO_KAFKA", "false").lower() == "true":
        publish_products(SHOP, scraper.products)