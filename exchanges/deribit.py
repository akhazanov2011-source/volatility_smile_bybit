#!/usr/bin/env python3.14
"""
Адаптер Deribit.

Публичный JSON-RPC эндпоинт без авторизации:
    GET https://www.deribit.com/api/v2/public/get_book_summary_by_currency
        ?currency=BTC&kind=option
возвращает ``result`` — массив сводок по всем опционам валюты.

Нюансы Deribit (см. проверку API):
  * ``mark_iv`` — в ПРОЦЕНТАХ (35.22 = 35.22%); нормализуем /100;
  * ``mark_price`` — премия в BTC (quote_currency = базовый актив);
  * spot/index — отдельным запросом ``get_index_price?index_name=btc_usd``
    (имя индекса в нижнем регистре);
  * греки в bulk-ответе НЕТ → delta/gamma/theta/vega = None (считает BS);
  * forward в bulk нет → underlying_price = None → implied rate r = 0.

Instrument_name: ``BTC-31JUL26-69000-C`` → DDMMMYY, целочисленный страйк, C/P.
"""

import re
import threading
import time
from datetime import datetime, timezone

import requests

from exchanges.base import DataSource, NormalizedOption
import net

DERIBIT_BASE_URL = "https://www.deribit.com"

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: UI-монета → валюта Deribit (currency) и имя индекса для spot.
COIN_ALIASES = {
    "BTC": {"currency": "BTC", "index_name": "btc_usd"},
    "ETH": {"currency": "ETH", "index_name": "eth_usd"},
    "SOL": {"currency": "SOL", "index_name": "sol_usd"},
}

# Instrument: <COIN>-DDMMMYY-STRIKE-C/P
_SYMBOL_RE = re.compile(r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:\.\d+)?)-([CP])$")


def parse_instrument_name(instrument_name):
    """``BTC-31JUL26-69000-C`` → (base_coin, strike, option_type, expiry_dt)."""
    m = _SYMBOL_RE.match(instrument_name)
    if not m:
        return None
    base_coin = m.group(1)
    day = int(m.group(2))
    month = MONTH_MAP.get(m.group(3))
    year = 2000 + int(m.group(4))
    if month is None:
        return None
    try:
        expiry_dt = datetime(year, month, day)
        strike = float(m.group(5))
    except ValueError:
        return None
    option_type = "Call" if m.group(6) == "C" else "Put"
    return base_coin, strike, option_type, expiry_dt


def _to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# TTL-кеш book_summary по currency.
_summary_cache = {}
_summary_cache_lock = threading.Lock()
_SUMMARY_CACHE_TTL = 60


class DeribitAdapter(DataSource):
    label = "Deribit"

    @property
    def supported_coins(self) -> list[str]:
        return list(COIN_ALIASES.keys())

    def _get_book_summary(self, currency):
        now_ts = time.time()
        with _summary_cache_lock:
            cached = _summary_cache.get(currency)
            if cached and (now_ts - cached["ts"]) < _SUMMARY_CACHE_TTL:
                return cached["records"]

        url = f"{DERIBIT_BASE_URL}/api/v2/public/get_book_summary_by_currency"
        params = {"currency": currency, "kind": "option"}
        try:
            payload = net.get_json(url, params=params)
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(
                f"Не удалось загрузить опционы Deribit для {currency}."
            ) from exc

        records = payload.get("result")
        if not isinstance(records, list):
            raise RuntimeError("Deribit вернул неожиданный формат данных.")

        with _summary_cache_lock:
            _summary_cache[currency] = {"ts": now_ts, "records": records}
        return records

    def _get_index_price(self, index_name):
        url = f"{DERIBIT_BASE_URL}/api/v2/public/get_index_price"
        params = {"index_name": index_name}
        try:
            payload = net.get_json(url, params=params)
        except (requests.RequestException, ValueError):
            return None
        result = payload.get("result") or {}
        return _to_float(result.get("index_price"))

    def fetch(self, coin):
        config = COIN_ALIASES.get(coin)
        if config is None:
            raise ValueError(f"Deribit не поддерживает монету {coin}")

        records = self._get_book_summary(config["currency"])

        options: list[NormalizedOption] = []
        for rec in records:
            instrument_name = rec.get("instrument_name")
            if not instrument_name:
                continue
            parsed = parse_instrument_name(instrument_name)
            if parsed is None:
                continue
            base_coin, strike, option_type, expiry_dt = parsed

            mark_iv_pct = _to_float(rec.get("mark_iv"))
            # Deribit отдаёт IV в процентах → нормализуем в доли единицы.
            mark_iv = mark_iv_pct / 100.0 if mark_iv_pct is not None else None

            options.append(
                NormalizedOption(
                    symbol=instrument_name,
                    base_coin=base_coin,
                    strike=strike,
                    option_type=option_type,
                    expiry_dt=expiry_dt,
                    mark_iv=mark_iv,
                    mark_price=_to_float(rec.get("mark_price")),
                    delta=None,   # Deribit bulk не отдаёт греки → BS
                    gamma=None,
                    theta=None,
                    vega=None,
                    underlying_price=None,  # forward в bulk нет → r = 0
                )
            )

        spot_price = self._get_index_price(config["index_name"])
        return spot_price, None, options
