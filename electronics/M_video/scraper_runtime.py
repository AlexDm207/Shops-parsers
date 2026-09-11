"""Standalone runtime helpers for the M.Video scraper."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

import requests
from prometheus_client import Counter, Gauge, REGISTRY

try:
    from confluent_kafka import Producer
except ImportError:  # pragma: no cover
    Producer = None

try:
    import cloudscraper
except ImportError:  # pragma: no cover
    cloudscraper = None

USER_AGENT = os.getenv(
    "SCRAPER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
RAW_PRODUCTS_TOPIC = os.getenv("RAW_PRODUCTS_TOPIC", "raw-products")

def _counter(name: str, description: str, labels: list[str]) -> Counter:
    existing = REGISTRY._names_to_collectors.get(name)
    return existing if existing is not None else Counter(name, description, labels)


def _gauge(name: str, description: str, labels: list[str]) -> Gauge:
    existing = REGISTRY._names_to_collectors.get(name)
    return existing if existing is not None else Gauge(name, description, labels)


REQUEST_ERRORS = _counter("scraper_request_errors_total", "Scraper request errors", ["shop"])
PRODUCTS_PUBLISHED = _counter("scraper_products_published_total", "Products published", ["shop"])
LAST_SUCCESSFUL_RUN = _gauge("scraper_last_successful_run_timestamp", "Last successful scraper run", ["shop"])


class ScraperError(RuntimeError):
    """A recoverable scraper failure."""


def create_session() -> requests.Session:
    if cloudscraper is not None:
        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    else:
        session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
    )
    return session


def source_product_id(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or urlsplit(url).netloc


def raw_product(
    *,
    shop: str,
    url: str,
    name: str,
    current_price: Any,
    old_price: Any = None,
    discount: Any = None,
    in_stock: bool | None = None,
    gtin: Any = None,
) -> dict[str, Any]:
    return {
        "shop": shop,
        "source_url": url,
        "source_product_id": source_product_id(url),
        "gtin": str(gtin) if gtin else None,
        "name": str(name).strip(),
        "current_price_text": str(current_price) if current_price is not None else "",
        "old_price_text": str(old_price) if old_price is not None else "",
        "discount_text": str(discount) if discount is not None else "",
        "in_stock": bool(current_price) if in_stock is None else in_stock,
        "collected_at": datetime.now(UTC).isoformat(),
    }


def save_jsonl(products: Iterable[Mapping[str, Any]], filename: str) -> None:
    with Path(filename).open("w", encoding="utf-8", newline="\n") as output:
        for product in products:
            output.write(json.dumps(product, ensure_ascii=False) + "\n")


def publish_products(shop: str, products: Iterable[Mapping[str, Any]]) -> int:
    if Producer is None:
        raise RuntimeError("Kafka publishing requires confluent-kafka")
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS})
    count = 0
    for product in products:
        producer.produce(
            RAW_PRODUCTS_TOPIC,
            key=f"{product['shop']}:{product['source_product_id']}",
            value=json.dumps(product, ensure_ascii=False),
        )
        producer.poll(0)
        PRODUCTS_PUBLISHED.labels(shop=shop).inc()
        count += 1
    producer.flush()
    LAST_SUCCESSFUL_RUN.labels(shop=shop).set_to_current_time()
    return count
