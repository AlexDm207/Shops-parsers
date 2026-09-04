"""Raw product loader for the RIV GOSH promotions page.

The loader deliberately does not normalize prices, deduplicate products, or write
anything except optional JSONL output. Those responsibilities belong to the
pipeline's normalizer and sink modules.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Iterator, List, Mapping, Optional, Union
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from requests.exceptions import RequestException

# Используем cloudscraper, а при его отсутствии переключаемся на requests.
try:
    import cloudscraper
except ImportError:
    cloudscraper = None
    import requests


PagePayload = Union[str, Mapping[str, Any], List[Any]]


class RiveGaucheScraper:
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Cache-Control": "no-cache",
    }

    def __init__(
        self,
        start_url: str,
        max_pages: Optional[int] = None,
        delay: float = 1.0,
        timeout: float = 30.0,
        api_url_template: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        if not start_url:
            raise ValueError("start_url must not be empty")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if delay < 0:
            raise ValueError("delay must not be negative")

        # сохраняем настройки загрузчика и создаем HTTP-сессию.
        self.start_url = start_url
        self.max_pages = max_pages
        self.delay = delay
        self.timeout = timeout
        self.api_url_template = api_url_template
        self.headers = {**self.DEFAULT_HEADERS, **(headers or {})}
        self.session = (
            cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
            if cloudscraper is not None
            else requests.Session()
        )
        self.session.headers.update(self.headers)

    def _page_url(self, page: int) -> str:
        """Return start_url with its currentPage query parameter replaced."""
        # меняем только номер страницы, сохраняя остальные параметры URL.
        parsed = urlparse(self.start_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["currentPage"] = str(page)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _request(self, url: str) -> PagePayload:
        # определяем формат ответа по заголовку и содержимому.
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
        # Браузерный fallback нужен для WAF, который блокирует обычный HTTP-клиент.
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
                # Ждем, пока Angular отрисует карточки товаров.
                try:
                    page.wait_for_selector("product-item", timeout=5000)
                except Exception:
                    # Пустая страница означает конец пагинации.
                    pass
                page.wait_for_timeout(500)
                # Во время смены DOM повторяем чтение HTML несколько раз.
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
        # для API используем шаблон, иначе загружаем обычную страницу акции.
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
            # При сетевой ошибке пробуем получить ту же публичную страницу через Chromium.
            return self._request_in_browser(page_url)

    @staticmethod
    def _first_value(item: Mapping[str, Any], *keys: str) -> Any:
        # берем первое непустое значение из вариантов имени поля API.
        for key in keys:
            value = item.get(key)
            if value not in (None, "", []):
                return value
        return None

    @staticmethod
    def _as_text(value: Any) -> Optional[str]:
        # приводим вложенные значения цены или скидки к строке.
        if value is None:
            return None
        if isinstance(value, Mapping):
            value = value.get("value") or value.get("amount") or value.get("text")
        return str(value).strip() if value is not None else None

    def _product_from_mapping(self, item: Mapping[str, Any]) -> Optional[dict]:
        # собираем единый raw-объект без нормализации цен.
        title = self._first_value(item, "raw_title", "title", "name", "productName", "displayName")
        url = self._first_value(item, "product_url", "url", "link", "productUrl", "canonicalUrl")
        current = self._first_value(item, "price_current", "currentPrice", "salePrice", "price", "finalPrice")
        old = self._first_value(item, "price_old", "oldPrice", "regularPrice", "basePrice", "old_price")
        discount = self._first_value(item, "discount_label", "discount", "discountLabel", "badge", "saleLabel")
        if not title or not url or current is None:
            return None
        return {
            "raw_title": self._as_text(title),
            "product_url": urljoin(self.start_url, str(url)),
            "price_current": current,
            "price_old": old,
            "discount_label": self._as_text(discount),
            "parsed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def _walk_json(self, value: Any) -> Iterator[dict]:
        # рекурсивно ищем товарные объекты во вложенном JSON-ответе.
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
        # извлекаем товары из типовых карточек HTML-магазина.
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
            product = {
                "raw_title": title_node.get_text(" ", strip=True),
                "product_url": urljoin(self.start_url, link.get("href", "")),
                "price_current": current_text,
                "price_old": old_node.get_text(" ", strip=True) if old_node else None,
                "discount_label": discount_node.get_text(" ", strip=True) if discount_node else None,
                "parsed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            if product["product_url"] not in seen_urls:
                seen_urls.add(product["product_url"])
                products.append(product)

        # проверяем JSON-LD и состояние приложения, встроенное в HTML.
        for script in soup.select("script[type='application/ld+json'], script#__NEXT_DATA__, script[type='application/json']"):
            try:
                products.extend(self._walk_json(json.loads(script.string or script.get_text())))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return self._unique(products)

    @staticmethod
    def _unique(products: List[dict]) -> List[dict]:
        # убираем повторения по URL карточки перед передачей в pipeline.
        result = []
        seen = set()
        for product in products:
            if product["product_url"] not in seen:
                seen.add(product["product_url"])
                result.append(product)
        return result

    def parse_products(self, data_or_html: PagePayload) -> List[dict]:
        # выбираем JSON-парсер или HTML-парсер по типу ответа.
        if isinstance(data_or_html, (Mapping, list)):
            return self._unique(list(self._walk_json(data_or_html)))
        return self._parse_html(data_or_html)

    def run(self) -> List[dict]:
        # загружаем страницы по порядку до конца каталога или лимита.
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
        return self._unique(all_products)

    def fetch_all_products(self) -> List[dict]:
        # оставляем привычное имя метода для внешнего pipeline.
        return self.run()

    def save_to_jsonl(self, filename: str) -> int:
        # записываем каждый raw-объект отдельной строкой JSONL.
        products = self.run()
        with open(filename, "w", encoding="utf-8") as output:
            for product in products:
                output.write(json.dumps(product, ensure_ascii=False) + "\n")
        return len(products)


if __name__ == "__main__":
    # пример самостоятельного запуска из командной строки.
    scraper = RiveGaucheScraper(
        start_url="https://rivegauche.ru/tags/sale?currentPage=1",
        max_pages=None,
        delay=1.5,
    )
    count = scraper.save_to_jsonl("products.jsonl")
    print(f"Собрано товаров: {count}. Результат: products.jsonl")
