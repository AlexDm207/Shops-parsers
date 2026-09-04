# 🛒 Cosmetic Shops Parsers

> **Единый сервис скрапинга и мониторинга цен** для ведущих интернет-магазинов косметики: **Иль де Ботэ**, **Рив Гош** и **Подружка**.

Модуль предназначен для регулярного автоматизированного сбора данных об акциях, скидках и ассортименте. Проект генерирует единый поток сырых данных (`raw data`), готовый к интеграции с аналитическим пайплайном и базой данных (`promotion-scrapper`).

---

## 🛠 Технологический стек

| Технология | Назначение |
| :--- | :--- |
| **Python 3.10+** | Основной язык разработки |
| **cloudscraper / requests** | HTTP-запросы и работа со страницами, защищёнными базовыми WAF-механизмами |
| **BeautifulSoup4** | Разбор и извлечение данных из HTML и JSON-элементов |
| **JSONL** | Единый построчный формат сохранения результатов (`products.jsonl`) |

Для браузерного fallback парсера «Рив Гош» дополнительно используется Playwright/Chromium. Он нужен только если обычный HTTP-запрос получает отказ.

---

## 📂 Структура репозитория

```text
Cosmetic-shops-parsers/
├── requirements.txt          # Зависимости проекта
├── normalaze.py              # Модуль нормализации данных и отправки в pipeline
├── scraper_iledebeaute.py    # Парсер «Иль де Ботэ»
├── scraper_rivegauche.py     # Парсер «Рив Гош»
├── scraper_podrygka.py       # Парсер «Подружка»
├── README.md                 # Общая документация проекта
├── README-iledebote.md       # Инструкция по «Иль де Ботэ»
├── README-rivgosh.md         # Инструкция по «Рив Гош»
└── README-podrygka.md        # Инструкция по «Подружке»
```

## 🚀 Быстрый старт

### 1. Клонирование и установка зависимостей

```bash
git clone <repository-url>
cd Cosmetic-shops-parsers
python -m pip install -r requirements.txt
```

Для browser fallback парсера «Рив Гош» установите Playwright и Chromium:

```bash
python -m pip install playwright
python -m playwright install chromium
```

### 2. Запуск готовых скриптов

```bash
python scraper_rivegauche.py
python scraper_podrygka.py
python scraper_iledebeaute.py
```

Результатом работы является файл `products.jsonl`. Каждый товар записывается отдельной строкой JSON.

### 3. Пример использования в коде

Парсеры «Рив Гош» и «Подружка» имеют унифицированный интерфейс `run()` и `save_to_jsonl()`. Парсер «Иль де Ботэ» запускается через функцию `scrape_catalog()`.

```python
from scraper_rivegauche import RiveGaucheScraper
from scraper_podrygka import PodrygkaScraper
from scraper_iledebeaute import scrape_catalog

rivegauche = RiveGaucheScraper(
    start_url="https://rivegauche.ru/tags/sale?currentPage=1",
    max_pages=1,
)
rivegauche_products = rivegauche.run()
rivegauche.save_to_jsonl("rivegauche-products.jsonl")

podrygka = PodrygkaScraper(
    start_url="https://www.podrygka.ru/catalog/?page=1",
    max_pages=1,
)
podrygka_products = podrygka.run()
podrygka.save_to_jsonl("podrygka-products.jsonl")

# Каталог «Иль де Ботэ» сохраняется функцией в products.jsonl.
iledebeaute_products = scrape_catalog()
```

Чтобы собирать все страницы, не передавайте `max_pages` или установите его в `None` там, где это поддерживается. Соблюдайте разумную задержку между запросами и правила сайтов.

## 📊 Единый формат данных (Output Data Schema)

Все записи предназначены для передачи во внешний pipeline. Скраперы сохраняют значения цен в исходном виде и добавляют время сбора:

```json
{
  "product_name": "FREDERIC MALLE Portrait of a Lady",
  "product_url": "https://rivegauche.ru/product/portrait-of-a-lady",
  "price_current": "9400",
  "price_old": "11463",
  "discount_label": "-18%",
  "parsed_at": "2026-09-04T12:00:00Z"
}
```

Названия полей могут отражать исходный адаптер: например, парсер «Рив Гош» использует `raw_title`, а парсер «Иль де Ботэ» сохраняет `name`, `url`, `current_price` и `old_price`. Нормализатор приводит такие записи к контракту downstream-пайплайна.

## 🔗 Интеграция с пайплайном обработки

Скраперы возвращают сырые объекты (`raw data`). Очистка цен, приведение типов, сопоставление идентичности товара и сохранение в хранилище (ClickHouse / CSV) выполняются внешним нормализатором.

```python
from normalaze import normalize_product

raw_data = scraper.run()
clean_data = [normalize_product(item, cache) for item in raw_data]
```

`cache` в этом примере является Redis-кэшем, который используется нормализатором для сопоставления товаров. Полный поток проекта:

```text
scraper -> raw products.jsonl / Kafka -> normalizer -> ClickHouse / CSV
```

Модуль `normalaze.py` также поддерживает работу через Kafka и экспорт метрик Prometheus. Для запуска этого слоя необходимы переменные окружения `KAFKA_BOOTSTRAP_SERVERS`, `RAW_PRODUCTS_TOPIC`, `NORMALIZED_PRODUCTS_TOPIC`, `REDIS_URL` и `METRICS_PORT`, а также зависимости нормализатора (`redis`, `confluent-kafka`, `prometheus-client`, `product_identity`).

## ⚠️ Ограничения и рекомендации

- Используйте только общедоступные страницы.
- Не обходите CAPTCHA, авторизацию и другие средства контроля доступа.
- Устанавливайте задержки между запросами и учитывайте `robots.txt` и условия использования сайтов.
- Цены в raw data намеренно не преобразуются в копейки или числа: это ответственность нормализатора.
