#!/usr/bin/env python3.14
"""
Адаптер Binance European Options (eapi).

Публичный эндпоинт без авторизации:
    GET https://eapi.binance.com/eapi/v1/mark
возвращает bare JSON-массив (без обёртки) с mark IV, греками и markPrice.

Spot/index: ``GET /eapi/v1/index?underlying=BTCUSDT`` → ``indexPrice``
(требуется underlying в виде ``BTCUSDT``, не ``BTC``).

Нюансы Binance (см. проверку API):
  * ``markIV`` ЕСТЬ и это decimal fraction (1.135 = 113.5%);
  * греки ``delta``/``gamma``/``theta``/``vega`` также есть в ответе —
    передаём их в доменный слой (по общей конвенции «API где есть, BS где нет»);
  * ``bidIV``/``askIV`` используют сентинел ``-1.0`` при отсутствии —
    игнорируем (markIV есть напрямую);
  * ``markPrice`` — премия в USDT;
  * forward в bulk нет → underlying_price = None → implied rate r = 0
    (при желании можно использовать riskFreeInterest, но оставляем консистентно
    с другими биржами без forward).

Symbol: ``BTC-260626-140000-C`` → YYMMDD, целочисленный страйк, C/P.
"""

import re
import threading
import time
from datetime import datetime

import requests

from exchanges.base import DataSource, NormalizedOption
import net

BINANCE_BASE_URL = "https://eapi.binance.com"

#: UI-монета → underlying для /index (Binance требует вид BTCUSDT).
COIN_ALIASES = {
    "BTC": {"underlying": "BTCUSDT"},
    "ETH": {"underlying": "ETHUSDT"},
}

# Symbol: <COIN>-YYMMDD-STRIKE-C/P
_SYMBOL_RE = re.compile(r"^([A-Z]+)-(\d{6})-(\d+(?:\.\d+)?)-([CP])$")


def parse_symbol(symbol):
    """``BTC-260626-140000-C`` → (base_coin, strike, option_type, expiry_dt)."""
    m = _SYMBOL_RE.match(symbol)
    if not m:
        return None
    base_coin = m.group(1)
    date_str = m.group(2)
    try:
        expiry_dt = datetime.strptime(date_str, "%y%m%d")
        strike = float(m.group(3))
    except ValueError:
        return None
    option_type = "Call" if m.group(4) == "C" else "Put"
    return base_coin, strike, option_type, expiry_dt


def _to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# TTL-кеш /mark (единый список для всех монет — фильтруем локально).
_mark_cache = {}
_mark_cache_lock = threading.Lock()
_MARK_CACHE_TTL = 60


class BinanceAdapter(DataSource):
    label = "Binance"

    @property
    def supported_coins(self) -> list[str]:
        return list(COIN_ALIASES.keys())

    def _get_mark(self):
        now_ts = time.time()
        with _mark_cache_lock:
            cached = _mark_cache.get("all")
            if cached and (now_ts - cached["ts"]) < _MARK_CACHE_TTL:
                return cached["records"]

        url = f"{BINANCE_BASE_URL}/eapi/v1/mark"
        try:
            payload = net.get_json(url)
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError("Не удалось загрузить опционы Binance (/eapi/v1/mark).") from exc
        if not isinstance(payload, list):
            raise RuntimeError("Binance /eapi/v1/mark вернул неожиданный формат.")

        with _mark_cache_lock:
            _mark_cache["all"] = {"ts": now_ts, "records": payload}
        return payload

    def _get_index(self, underlying):
        url = f"{BINANCE_BASE_URL}/eapi/v1/index"
        params = {"underlying": underlying}
        try:
            payload = net.get_json(url, params=params)
        except (requests.RequestException, ValueError):
            return None
        return _to_float(payload.get("indexPrice"))

    def fetch(self, coin):
        config = COIN_ALIASES.get(coin)
        if config is None:
            raise ValueError(f"Binance не поддерживает монету {coin}")

        records = self._get_mark()
        options: list[NormalizedOption] = []
        for rec in records:
            symbol = rec.get("symbol")
            if not symbol:
                continue
            parsed = parse_symbol(symbol)
            if parsed is None:
                continue
            base_coin, strike, option_type, expiry_dt = parsed

            options.append(
                NormalizedOption(
                    symbol=symbol,
                    base_coin=base_coin,
                    strike=strike,
                    option_type=option_type,
                    expiry_dt=expiry_dt,
                    mark_iv=_to_float(rec.get("markIV")),
                    mark_price=_to_float(rec.get("markPrice")),
                    delta=_to_float(rec.get("delta")),
                    gamma=_to_float(rec.get("gamma")),
                    theta=_to_float(rec.get("theta")),
                    vega=_to_float(rec.get("vega")),
                    underlying_price=None,  # forward в bulk нет → r = 0
                )
            )

        spot_price = self._get_index(config["underlying"])
        return spot_price, None, options
