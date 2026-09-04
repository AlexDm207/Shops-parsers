# iledebeaute-scraper

Минимальный стартовый проект для парсинга каталога скидок ИЛЬ ДЕ БОТЭ.

## Установка

```bash
python -m pip install -r requirements.txt
```

## Запуск минимального скрапера

```bash
python scraper_iledebeaute.py
```

Для PowerShell с проектным окружением:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt
python .\scraper_iledebeaute.py
```

Результат записывается в `products.jsonl`. Каждая строка содержит только `name`, `url`, `current_price` и `old_price`.

## Что проверять

- доступность страницы;
- наличие ссылок на карточки товара;
- наличие заголовков, цен, URL товаров;
- при необходимости — добавление User-Agent/headers или `cloudscraper`.

## Структура

- `scraper_iledebeaute.py` — основной минимальный парсер каталога;
- `scraper.py` — старая экспериментальная версия;
- `requirements.txt` — зависимости;
- `README.md` — краткая инструкция.
