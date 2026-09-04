import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import cloudscraper
from bs4 import BeautifulSoup


BASE_URL = "https://iledebeaute.ru"
CATALOG_URL = "https://iledebeaute.ru/catalog/tip-has_discount-iz-prom/"
OUTPUT_PATH = "products.jsonl"
HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": f"{BASE_URL}/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
}


def clean_text(value):
    """Убирает лишние пробелы, чтобы текст из HTML был удобен для записи."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).replace("\xa0", " ").strip()


def normalize_name(value):
    """Удаляет технический префикс сайта из названия товара."""
    name = clean_text(value)
    return re.sub(r"^Перейти к товару\s+", "", name, flags=re.IGNORECASE)


def normalize_url(url):
    """Приводит URL к единому виду для защиты от повторного обхода."""
    parsed = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit(parsed._replace(query=query, fragment="")).rstrip("/")


def page_number(url):
    """Возвращает номер страницы из page или PAGEN_1; каталог без параметра считается первой."""
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    for key, value in query.items():
        if key.lower() in {"page", "pagen_1"} and value.isdigit():
            return int(value)
    return 1


def create_session():
    """Создает одну HTTP-сессию с браузерными заголовками для всех запросов."""
    session = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    session.headers.update(HEADERS)
    return session


def fetch_html(session, url):
    """Загружает страницу и останавливает скрапер при HTTP-ошибке."""
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def parse_catalog_page(html):
    """Извлекает уникальные названия и URL товаров с одной страницы каталога."""
    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen_urls = set()

    for link in soup.select("a[href*='/product/']"):
        href = link.get("href")
        if not href:
            continue

        product_url = urljoin(BASE_URL, href)
        normalized_product_url = normalize_url(product_url)
        if normalized_product_url in seen_urls:
            continue

        name = normalize_name(
            link.get("aria-label")
            or link.get("title")
            or link.get_text(" ", strip=True)
        )
        if not name:
            image = link.select_one("img[alt]")
            name = normalize_name(image.get("alt") if image else "")
        if not name:
            continue

        seen_urls.add(normalized_product_url)
        products.append({"name": name, "url": product_url})

    return products


def extract_product_area(soup):
    """Выбирает ближайший к h1 блок, где находятся цена и данные товара."""
    title = soup.select_one("h1")
    if not title:
        return soup

    for parent in title.parents:
        if parent.name in {"body", "html"}:
            break
        text = clean_text(parent.get_text(" ", strip=True))
        if parent.select_one('[itemprop="price"]') or re.search(r"\d[\d\s]*¤", text):
            return parent

    return title.parent or soup


def parse_price_value(element):
    """Читает цену из content или видимого текста и возвращает целое число рублей."""
    if not element:
        return None

    value = element.get("content") or element.get_text(" ", strip=True)
    digits = re.sub(r"\D", "", clean_text(value))
    return int(digits) if digits else None


def extract_current_price(area):
    """Извлекает текущую цену из семантического атрибута itemprop=price."""
    return parse_price_value(area.select_one('[itemprop="price"]'))


def extract_old_price(area, current_price):
    """Извлекает старую цену из перечеркнутого или старого ценового блока."""
    selectors = [
        ".css-1nvlaef",
        "[class*='old-price']",
        "[class*='oldPrice']",
        "del",
        "s",
    ]
    for selector in selectors:
        value = parse_price_value(area.select_one(selector))
        if value is not None and value != current_price:
            return value
    return None


def parse_product_page(session, product):
    """Открывает товар и возвращает только name, url, current_price и old_price."""
    soup = BeautifulSoup(fetch_html(session, product["url"]), "html.parser")
    area = extract_product_area(soup)
    title = soup.select_one("h1")
    current_price = extract_current_price(area)
    old_price = extract_old_price(area, current_price)

    return {
        "name": normalize_name(title.get_text(" ", strip=True)) if title else product["name"],
        "url": product["url"],
        "current_price": current_price,
        "old_price": old_price,
    }


def find_next_page(soup, current_url, visited_pages):
    """Находит следующую непосещенную страницу по кнопке или номеру пагинации."""
    current_number = page_number(current_url)
    candidates = []
    for link in soup.select("a[href]"):
        href = link.get("href")
        if not href:
            continue
        text = clean_text(link.get_text(" ", strip=True)).lower()
        if not (
            "показать еще" in text
            or "показать ещё" in text
            or "pagen_1" in href.lower()
            or "page=" in href.lower()
        ):
            continue

        candidate = urljoin(current_url, href)
        normalized = normalize_url(candidate)
        if (
            page_number(candidate) > current_number
            and normalized not in visited_pages
        ):
            candidates.append((page_number(candidate), candidate))

    if candidates:
        return min(candidates, key=lambda item: item[0])[1]
    return None


def save_jsonl(products, path=OUTPUT_PATH):
    """Сохраняет каждую готовую запись отдельной JSON-строкой."""
    with open(path, "w", encoding="utf-8") as file:
        for product in products:
            file.write(json.dumps(product, ensure_ascii=False) + "\n")


def scrape_catalog():
    """Обходит каталог и товары без дублей, затем сохраняет JSONL."""
    session = create_session()
    current_url = CATALOG_URL
    visited_pages = set()
    seen_products = set()
    products = []

    while current_url:
        normalized_page = normalize_url(current_url)
        if normalized_page in visited_pages:
            break
        visited_pages.add(normalized_page)

        print(f"Каталог {len(visited_pages)}: {current_url}")
        soup = BeautifulSoup(fetch_html(session, current_url), "html.parser")
        catalog_products = parse_catalog_page(str(soup))
        print(f"Найдено ссылок: {len(catalog_products)}")

        for index, product in enumerate(catalog_products, start=1):
            product_key = normalize_url(product["url"])
            if product_key in seen_products:
                continue
            seen_products.add(product_key)
            print(f"Товар {index}/{len(catalog_products)}: {product['name']}")
            products.append(parse_product_page(session, product))

        current_url = find_next_page(soup, current_url, visited_pages)

    save_jsonl(products)
    print(f"Готово: {len(products)} товаров, страниц: {len(visited_pages)}")
    return products


if __name__ == "__main__":
    scrape_catalog()
