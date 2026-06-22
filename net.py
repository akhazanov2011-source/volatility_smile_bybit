#!/usr/bin/env python3.14
"""
Сетевой слой приложения. Листовой модуль без зависимостей от доменной логики:
используется адаптерами бирж (exchanges/*) и app.py.

Единый GET-клиент с экспоненциальным бэкоффом, джиттером и жёстким общим
дедлайном. Дедлайн гарантирует, что даже фоновый прогрев кеша не зависнет
надолго и не приведёт к WORKER TIMEOUT у gunicorn.
"""

import logging
import random
import time

import requests

REQUEST_TIMEOUT = 10
REQUEST_MAX_ATTEMPTS = 4
REQUEST_BACKOFF_BASE_SECONDS = 0.6
REQUEST_BACKOFF_MAX_SECONDS = 6.0
# Верхняя граница суммарного времени всех попыток запроса. Без неё
# 4 попытки × 10с timeout + backoff могут занять ~45с, что приводило к
# WORKER TIMEOUT у gunicorn. Дедлайн гарантирует ограниченность времени.
REQUEST_MAX_TOTAL_SECONDS = 25

logger = logging.getLogger("volatility_smile_bybit")


def _requests_get_with_retry(url, *, params, timeout):
    last_exc = None
    started = time.monotonic()
    for attempt in range(1, REQUEST_MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= REQUEST_MAX_ATTEMPTS:
                break
            elapsed = time.monotonic() - started
            if elapsed >= REQUEST_MAX_TOTAL_SECONDS:
                logger.warning(
                    "Request to %s aborted after %.1fs deadline (attempt %s/%s): %s",
                    url,
                    elapsed,
                    attempt,
                    REQUEST_MAX_ATTEMPTS,
                    exc,
                )
                break
            backoff = min(
                REQUEST_BACKOFF_MAX_SECONDS,
                REQUEST_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
            )
            jitter = random.uniform(0.0, backoff * 0.2)
            sleep_for = backoff + jitter
            logger.warning(
                "Request to %s failed (attempt %s/%s), backing off %.2fs: %s",
                url,
                attempt,
                REQUEST_MAX_ATTEMPTS,
                sleep_for,
                exc,
            )
            time.sleep(sleep_for)
    raise last_exc


def get_json(url, *, params=None, timeout=REQUEST_TIMEOUT):
    """GET + retry + JSON-декодирование. Бросает requests.RequestException
    при сетевых ошибках и ValueError при невалидном JSON."""
    response = _requests_get_with_retry(url, params=params or {}, timeout=timeout)
    return response.json()
