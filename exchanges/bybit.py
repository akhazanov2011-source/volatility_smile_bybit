#!/usr/bin/env python3.14
"""
Адаптер Bybit — вынос существующей логики из app.py.

Bybit V5 public endpoint:
    GET https://bybit15.p.rapidapi.com/...
Используется публичный эндпоинт без авторизации. From bulk-ответа берём
mark IV (markIv), markPrice, греки (delta/gamma/theta/vega), а также
indexPrice (спот) и underlyingPrice (перпетуал — для implied rate).
"""

import re
import threading
import time
from datetime import datetime

import requests

from exchanges.base import DataSource, NormalizedOption
import net

BYBIT_BASE_URL = "https://api.bybit.com"

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: UI-имя монеты → Bybit baseCoin и допустимые префиксы symbol.
#: XAUTUSDT → baseCoin=XAUT (Bybit отдаёт золото-опционы под XAUT).
COIN_ALIASES = {
    "BTC": {"api_base_coin": "BTC", "symbol_prefixes": {"BTC", "BTCUSDT"}},
    "ETH": {"api_base_coin": "ETH", "symbol_prefixes": {"ETH", "ETHUSDT"}},
    "SOL": {"api_base_coin": "SOL", "symbol_prefixes": {"SOL", "SOLUSDT"}},
    "XRP": {"api_base_coin": "XRP", "symbol_prefixes": {"XRP", "XRPUSDT"}},
    "DOGE": {"api_base_coin": "DOGE", "symbol_prefixes": {"DOGE", "DOGEUSDT"}},
    "XAUTUSDT": {"api_base_coin": "XAUT", "symbol_prefixes": {"XAUT", "XAUTUSDT"}},
}

#: Размер одного опционного контракта в единицах базового актива. Bybit
#: отдаёт openInterest числом контрактов; для перевода в монеты умножаем.
#: Источник: спецификация опционов Bybit V5 (1 лот BTC-option = 0.01 BTC;
#: ETH = 0.1; SOL = 1; XRP = 100; DOGE = 1000; XAUT = 0.01 тр.унции).
CONTRACT_SIZE = {
    "BTC": 0.01,
    "ETH": 0.1,
    "SOL": 1.0,
    "XRP": 100.0,
    "DOGE": 1000.0,
    "XAUTUSDT": 0.01,
}

_SYMBOL_RE = re.compile(r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:\.\d+)?)-([CP])-(?:[A-Z]+)$")


def parse_symbol(symbol):
    """Парсит Bybit-symbol формата ``COIN-DDMMMYY-STRIKE-C/P-QUOTE``.

    Возвращает ``None`` для нераспознанных / некорректных символов.
    """
    parts = symbol.split("-")
    if len(parts) < 5:
        return None

    base_coin = parts[0]
    expiry_str = parts[1]
    strike = parts[2]
    option_type = parts[3]

    match = re.match(r"(\d+)([A-Z]{3})(\d{2})", expiry_str)
    if not match:
        return None

    day = int(match.group(1))
    month_str = match.group(2)
    year = 2000 + int(match.group(3))
    month = MONTH_MAP.get(month_str)
    if month is None:
        return None

    try:
        expiry_dt = datetime(year, month, day)
    except ValueError:
        return None

    try:
        strike_float = float(strike)
    except ValueError:
        return None

    return {
        "base_coin": base_coin,
        "strike": strike_float,
        "option_type": "Call" if option_type == "C" else "Put",
        "expiry_dt": expiry_dt,
    }


def _to_float(value):
    """Bybit отдаёт числовые поля строками; пустые/None → None."""
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# In-memory TTL-кеш сырых тикеров по baseCoin (как было в app.py).
_tickers_cache = {}
_tickers_cache_lock = threading.Lock()
_TICKERS_CACHE_TTL = 60


class BybitAdapter(DataSource):
    label = "Bybit"

    @property
    def supported_coins(self) -> list[str]:
        return list(COIN_ALIASES.keys())

    def get_coin_config(self, coin):
        return COIN_ALIASES[coin]

    def get_tickers(self, base_coin):
        """Сырые тикеры Bybit (result.list) с TTL-кешем 60с."""
        now_ts = time.time()
        with _tickers_cache_lock:
            cached = _tickers_cache.get(base_coin)
            if cached and (now_ts - cached["ts"]) < _TICKERS_CACHE_TTL:
                return cached["tickers"]

        url = f"{BYBIT_BASE_URL}/v5/market/tickers"
        params = {"category": "option", "baseCoin": base_coin}
        try:
            payload = net.get_json(url, params=params)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Не удалось загрузить опционные тикеры Bybit для {base_coin} "
                f"после нескольких попыток."
            ) from exc
        except ValueError as exc:
            raise RuntimeError("Bybit вернул невалидный JSON-ответ.") from exc

        tickers = payload.get("result", {}).get("list")
        if not isinstance(tickers, list):
            raise RuntimeError("Bybit вернул неожиданный формат данных для списка тикеров.")

        with _tickers_cache_lock:
            _tickers_cache[base_coin] = {"ts": now_ts, "tickers": tickers}
        return tickers

    def fetch(self, coin):
        config = COIN_ALIASES.get(coin)
        if config is None:
            raise ValueError(f"Bybit не поддерживает монету {coin}")

        tickers = self.get_tickers(config["api_base_coin"])

        spot_price = None
        options: list[NormalizedOption] = []
        for ticker in tickers:
            symbol = ticker.get("symbol")
            if not symbol:
                continue
            parsed = parse_symbol(symbol)
            if parsed is None:
                continue
            if parsed["base_coin"] not in config["symbol_prefixes"]:
                continue

            index_price = _to_float(ticker.get("indexPrice"))
            if spot_price is None and index_price is not None:
                spot_price = index_price

            # Open interest: Bybit отдаёт числом контрактов (openInterest);
            # приводим к единицам базового актива через CONTRACT_SIZE.
            oi_contracts = _to_float(ticker.get("openInterest"))
            contract_size = CONTRACT_SIZE.get(coin)
            open_interest = (
                oi_contracts * contract_size
                if oi_contracts is not None and contract_size is not None
                else None
            )

            options.append(
                NormalizedOption(
                    symbol=symbol,
                    # UI-имя монеты (не префикс символа): для XAUTUSDT это
                    # "XAUTUSDT", а не "XAUT" — collect_strikes в app.py
                    # фильтрует именно по UI-имени.
                    base_coin=coin,
                    strike=parsed["strike"],
                    option_type=parsed["option_type"],
                    expiry_dt=parsed["expiry_dt"],
                    mark_iv=_to_float(ticker.get("markIv")),
                    mark_price=_to_float(ticker.get("markPrice")),
                    delta=_to_float(ticker.get("delta")),
                    gamma=_to_float(ticker.get("gamma")),
                    theta=_to_float(ticker.get("theta")),
                    vega=_to_float(ticker.get("vega")),
                    underlying_price=_to_float(ticker.get("underlyingPrice")),
                    open_interest=open_interest,
                )
            )

        # Forward для implied rate: на Bybit это underlyingPrice перпа — он
        # per-option (отдаётся в каждом тикере), поэтому как единый forward
        # отдаём None; доменный слой считает r per-option из underlying_price.
        return spot_price, None, options
