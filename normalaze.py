import json
import os
import re
from datetime import UTC, datetime

import redis
from confluent_kafka import Consumer, Producer
from prometheus_client import Counter, start_http_server

from product_identity import resolve_identity


KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
RAW_PRODUCTS_TOPIC = os.getenv("RAW_PRODUCTS_TOPIC")
NORMALIZED_PRODUCTS_TOPIC = os.getenv("NORMALIZED_PRODUCTS_TOPIC")
REDIS_URL = os.getenv("REDIS_URL")
METRICS_PORT = int(os.getenv("METRICS_PORT"))

MESSAGES_NORMALIZED = Counter("normalizer_messages_total", "")
NORMALIZATION_ERRORS = Counter("normalizer_errors_total", "")
FALLBACK_IDENTITIES = Counter(
    "normalizer_fallback_identities_total",
    "",
)


def price(value):
    x = re.sub(r"[^0-9,.]", "", value).replace(",", ".")
    if not x:
        return None

    a = x.split(".")
    rub = a[0]
    kop = "00"
    if len(a) > 1:
        kop = a[1]
    return int(rub) * 100 + int((kop + "00")[:2])


def normalize_product(product, cache):
    current_price = price(product.get("current_price_text", ""))
    old_price = price(product.get("old_price_text", ""))
    identity = resolve_identity(product, cache)

    if identity.match_method.startswith("fallback"):
        FALLBACK_IDENTITIES.inc()

    return {
        **product,
        "canonical_id": identity.canonical_id,
        "normalized_name": identity.canonical_name,
        "name_match_method": identity.match_method,
        "name_match_confidence": identity.confidence,
        "current_price_kopecks": current_price,
        "old_price_kopecks": old_price,
        "has_promotion": bool(old_price and current_price and old_price > current_price),
        "normalized_at": datetime.now(UTC).isoformat(),
    }


def main():
    start_http_server(METRICS_PORT)
    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
            "group.id": "product-normalizer",
            "auto.offset.reset": "earliest",
        }
    )
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS})
    consumer.subscribe([RAW_PRODUCTS_TOPIC])

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                print(msg.error())
                continue

            try:
                product = normalize_product(json.loads(msg.value()), cache)
                producer.produce(
                    NORMALIZED_PRODUCTS_TOPIC,
                    key=msg.key(),
                    value=json.dumps(product, ensure_ascii=False),
                )
                producer.flush()
                MESSAGES_NORMALIZED.inc()
            except Exception as error:
                NORMALIZATION_ERRORS.inc()
                print(f"Normalization error: {error}")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
