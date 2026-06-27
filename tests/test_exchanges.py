"""
Тесты адаптеров бирж (exchanges/*).

Каждый адаптер тестируется на моках JSON-ответов (без обращения к сети):
проверяется парсинг symbol/instrument_name, нормализация mark_iv (конвенции
процент/доля единицы), извлечение spot/forward, заполнение греков None там,
где биржа их не отдаёт. Сетевой слой net.get_json подменяется.
"""

import sys
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exchanges
from exchanges.bybit import BybitAdapter, parse_symbol as bybit_parse_symbol
from exchanges.deribit import DeribitAdapter, parse_instrument_name as deribit_parse
from exchanges.okx import OkxAdapter
from exchanges.binance import BinanceAdapter, parse_symbol as binance_parse_symbol


# --------------------------------------------------------------------------------------
# Реестр
# --------------------------------------------------------------------------------------

def test_registry_has_all_exchanges():
    assert set(exchanges.EXCHANGES.keys()) == {"bybit", "deribit", "okx", "binance"}
    assert exchanges.DEFAULT_EXCHANGE == "bybit"


def test_all_pairs_covers_every_coin():
    pairs = exchanges.all_exchange_coin_pairs()
    # Каждая (exchange, coin) из supported_coins должна быть в all_pairs.
    for ex_key, cfg in exchanges.EXCHANGES.items():
        for coin in cfg.adapter.supported_coins:
            assert (ex_key, coin) in pairs


def test_supported_coins_per_exchange():
    assert exchanges.supported_coins("bybit") == ["BTC", "ETH", "SOL", "XRP", "DOGE", "XAUTUSDT"]
    assert exchanges.supported_coins("deribit") == ["BTC", "ETH"]
    assert exchanges.supported_coins("okx") == ["BTC", "ETH"]
    assert exchanges.supported_coins("binance") == ["BTC", "ETH", "SOL", "XRP", "DOGE"]


def test_labels_human_readable():
    labels = {cfg.label for cfg in exchanges.EXCHANGES.values()}
    assert labels == {"Bybit", "Deribit", "OKX", "Binance"}


# --------------------------------------------------------------------------------------
# Bybit
# --------------------------------------------------------------------------------------

def test_bybit_parse_symbol_valid():
    parsed = bybit_parse_symbol("BTC-28MAR26-100000-C-USDT")
    assert parsed["base_coin"] == "BTC"
    assert parsed["strike"] == 100000.0
    assert parsed["option_type"] == "Call"
    assert parsed["expiry_dt"] == datetime(2026, 3, 28)


def test_bybit_parse_symbol_put_and_usdt_prefix():
    parsed = bybit_parse_symbol("ETH-31DEC25-2000-P-USDT")
    assert parsed["option_type"] == "Put"
    assert parsed["strike"] == 2000.0


def test_bybit_parse_symbol_invalid_returns_none():
    assert bybit_parse_symbol("garbage") is None
    assert bybit_parse_symbol("BTC-99XXX26-100000-C-USDT") is None
    assert bybit_parse_symbol("BTC-29FEB25-100000-C") is None  # < 5 частей


def test_bybit_adapter_normalizes_response(monkeypatch):
    """Bybit: markIv — доля единицы (как есть); греки берутся из ответа;
    spot = indexPrice; underlying_price = underlyingPrice."""
    bybit_resp = {
        "result": {
            "list": [
                {
                    "symbol": "BTC-28MAR26-100000-C-USDT",
                    "markIv": "0.6",
                    "markPrice": "1200",
                    "delta": "0.55",
                    "gamma": "0.00002",
                    "theta": "-5.0",
                    "vega": "20.0",
                    "indexPrice": "64000",
                    "underlyingPrice": "64500",
                    "openInterest": "1000",
                },
                {
                    "symbol": "BTC-28MAR26-100000-P-USDT",
                    "markIv": "0.65",
                    "markPrice": "800",
                    "delta": "-0.45",
                    "gamma": "0.00002",
                    "theta": "-4.0",
                    "vega": "18.0",
                    "indexPrice": "",
                    "underlyingPrice": "64500",
                    "openInterest": "250",
                },
                {"symbol": "garbage"},  # пропускается
            ]
        }
    }
    adapter = BybitAdapter()
    monkeypatch.setattr(adapter, "get_tickers", lambda base_coin: bybit_resp["result"]["list"])

    spot, forward, options = adapter.fetch("BTC")
    assert spot == 64000.0
    assert forward is None  # forward per-option (underlying_price)
    assert len(options) == 2
    call = next(o for o in options if o.option_type == "Call")
    put = next(o for o in options if o.option_type == "Put")
    assert call.base_coin == "BTC"
    assert call.strike == 100000.0
    assert call.mark_iv == 0.6
    assert call.mark_price == 1200.0
    assert call.delta == 0.55
    assert call.underlying_price == 64500.0
    assert call.expiry_dt == datetime(2026, 3, 28)
    # Open interest приводится к монетам: контракты × CONTRACT_SIZE (BTC=0.01).
    assert call.open_interest == pytest.approx(1000 * 0.01)
    assert put.open_interest == pytest.approx(250 * 0.01)


def test_bybit_open_interest_uses_contract_size_per_coin(monkeypatch):
    """open_interest = контракты × CONTRACT_SIZE[coin]. Для DOGE (size=1000)
    множитель отличен от BTC (0.01) — проверяем, что он берётся по UI-монете."""
    bybit_resp = {
        "result": {
            "list": [
                {"symbol": "DOGE-28MAR26-0.2-C-USDT", "markIv": "0.6", "markPrice": "0.01",
                 "delta": "0.5", "gamma": "0.001", "theta": "-1", "vega": "0.5",
                 "indexPrice": "0.18", "underlyingPrice": "0.18", "openInterest": "5"},
            ]
        }
    }
    adapter = BybitAdapter()
    monkeypatch.setattr(adapter, "get_tickers", lambda base_coin: bybit_resp["result"]["list"])

    _, _, options = adapter.fetch("DOGE")
    assert len(options) == 1
    # 5 контрактов × 1000 (DOGE contract size) = 5000 DOGE.
    assert options[0].open_interest == pytest.approx(5000.0)


def test_bybit_unsupported_coin_raises():
    adapter = BybitAdapter()
    with pytest.raises(ValueError):
        adapter.fetch("NOTACOIN")


def test_bybit_gold_option_base_coin_matches_ui_name(monkeypatch):
    """Регрессия: золото торгуется на Bybit под baseCoin=XAUT, поэтому
    символы начинаются с 'XAUT', а UI-имя монеты — 'XAUTUSDT'. Адаптер
    обязан класть в option именно UI-имя ('XAUTUSDT'), иначе collect_strikes
    в app.py (фильтрует по UI-имени) отбросит все золото-опционы → 0 страйков
    → 'Не удалось загрузить данные с Bybit'.

    Воспроизводит баг XAUTUSDT→XAUT из #gold-broken.
    """
    bybit_resp = {
        "result": {
            "list": [
                {
                    "symbol": "XAUT-28MAR26-4200-C-USDT",
                    "markIv": "0.35",
                    "markPrice": "120",
                    "delta": "0.5",
                    "gamma": "0.0001",
                    "theta": "-2.0",
                    "vega": "10.0",
                    "indexPrice": "4180",
                    "underlyingPrice": "4190",
                },
                {
                    "symbol": "XAUT-28MAR26-4200-P-USDT",
                    "markIv": "0.38",
                    "markPrice": "80",
                    "delta": "-0.5",
                    "gamma": "0.0001",
                    "theta": "-1.5",
                    "vega": "9.0",
                    "indexPrice": "4180",
                    "underlyingPrice": "4190",
                },
            ]
        }
    }
    adapter = BybitAdapter()
    monkeypatch.setattr(adapter, "get_tickers", lambda base_coin: bybit_resp["result"]["list"])

    spot, forward, options = adapter.fetch("XAUTUSDT")
    assert spot == 4180.0
    assert forward is None
    assert len(options) == 2
    # Ключевая проверка регрессии: base_coin = UI-имя, а не префикс символа.
    assert all(o.base_coin == "XAUTUSDT" for o in options)
    # Имитируем фильтр collect_strikes из app.py — должны пройти все опционы.
    strikes = sorted({o.strike for o in options if o.base_coin == "XAUTUSDT"})
    assert strikes == [4200.0]


# --------------------------------------------------------------------------------------
# Deribit
# --------------------------------------------------------------------------------------

def test_deribit_parse_instrument_name():
    parsed = deribit_parse("BTC-31JUL26-69000-C")
    assert parsed == ("BTC", 69000.0, "Call", datetime(2026, 7, 31))


def test_deribit_parse_put():
    parsed = deribit_parse("ETH-28MAR26-3000-P")
    assert parsed[2] == "Put"
    assert parsed[1] == 3000.0


def test_deribit_parse_invalid():
    assert deribit_parse("nope") is None
    assert deribit_parse("BTC-99ZZZ26-100-C") is None


def test_deribit_adapter_normalizes_iv_and_converts_mark_price_to_usdt(monkeypatch):
    """Deribit: mark_iv в ПРОЦЕНТАХ (35.22) → нормализуем /100; mark_price
    котируется в BTC → конвертируется в USDT (× spot); греков нет → None."""
    summary = {
        "result": [
            {
                "instrument_name": "BTC-31JUL26-69000-C",
                "mark_iv": 35.22,
                "mark_price": 0.0199,
                "bid_price": 0.0195,
                "ask_price": 0.0205,
                "volume_usd": 8837.58,
                "underlying_index": "BTC-31JUL26",
                "open_interest": 41.6,
            },
            {"instrument_name": "BTC-31JUL26-69000-P", "mark_iv": 40.0,
             "mark_price": 0.05, "bid_price": 0.04, "ask_price": 0.06,
             "open_interest": 322.8},
            {"instrument_name": "garbage"},  # пропускается
        ]
    }

    adapter = DeribitAdapter()
    spot_usd = 64086.08
    monkeypatch.setattr(adapter, "_get_book_summary", lambda currency: summary["result"])
    monkeypatch.setattr(adapter, "_get_index_price", lambda name: spot_usd)

    spot, forward, options = adapter.fetch("BTC")
    assert spot == spot_usd
    assert forward is None
    assert len(options) == 2
    call = next(o for o in options if o.option_type == "Call")
    put = next(o for o in options if o.option_type == "Put")
    assert call.mark_iv == pytest.approx(0.3522)  # 35.22 / 100
    # mark_price приводится к USDT: 0.0199 BTC × 64086.08 ≈ 1275.31 USDT
    assert call.mark_price == pytest.approx(0.0199 * spot_usd)
    assert put.mark_price == pytest.approx(0.05 * spot_usd)
    # Греков в Deribit bulk нет
    assert call.delta is None
    assert call.gamma is None
    assert call.theta is None
    assert call.vega is None
    assert call.underlying_price is None
    assert call.expiry_dt == datetime(2026, 7, 31)
    # open_interest Deribit уже в единицах базового актива (контракт = 1 BTC).
    assert call.open_interest == pytest.approx(41.6)
    assert put.open_interest == pytest.approx(322.8)


def test_deribit_adapter_mark_price_none_when_spot_missing(monkeypatch):
    """Если spot недоступен, mark_price нельзя сконвертировать → None."""
    summary = {
        "result": [
            {"instrument_name": "BTC-31JUL26-69000-C", "mark_iv": 35.22,
             "mark_price": 0.0199},
        ]
    }
    adapter = DeribitAdapter()
    monkeypatch.setattr(adapter, "_get_book_summary", lambda currency: summary["result"])
    monkeypatch.setattr(adapter, "_get_index_price", lambda name: None)

    spot, forward, options = adapter.fetch("BTC")
    assert spot is None
    assert options[0].mark_price is None  # нельзя конвертировать без spot


# --------------------------------------------------------------------------------------
# OKX
# --------------------------------------------------------------------------------------

def test_okx_adapter_merges_summary_and_instruments(monkeypatch):
    """OKX: мёрдж opt-summary + instruments по instId; markVol — доля единицы
    (как есть); fwdPx → forward; expTime (ms) → datetime."""
    inst_id = "BTC-USD_UM-260626-61000-C"
    summary_by_id = {
        inst_id: {
            "instId": inst_id,
            "markVol": "0.448013318450148",
            "fwdPx": "64053.2",
            "realVol": "0",
            "bidVol": "0.4284",
            "askVol": "0.5053",
        },
        "BTC-USD_UM-260626-61000-P": {
            "instId": "BTC-USD_UM-260626-61000-P",
            "markVol": "0.5", "fwdPx": "64053.2",
        },
        "BTC-USD_UM-260626-99999-C": {  # нет в instruments → пропускается
            "instId": "BTC-USD_UM-260626-99999-C", "markVol": "0.6", "fwdPx": "64053.2",
        },
        # Coin-margined (без _UM) — то же семейство страйка/экспирации, но
        # премия в BTC. Должно отбрасываться, иначе дублирует страйк и рвёт
        # улыбку. Регрессия на баг со «рванным» графиком OKX BTC.
        "BTC-USD-260626-61000-C": {
            "instId": "BTC-USD-260626-61000-C", "markVol": "0.9", "fwdPx": "64060.0",
        },
    }
    instruments_by_id = {
        inst_id: {
            "instId": inst_id, "optType": "C", "stk": "61000",
            "expTime": "1782115200000", "uly": "BTC-USD", "state": "live",
        },
        "BTC-USD_UM-260626-61000-P": {
            "instId": "BTC-USD_UM-260626-61000-P", "optType": "P", "stk": "61000",
            "expTime": "1782115200000", "uly": "BTC-USD",
        },
        # Описание coin-margined есть, но он всё равно отбрасывается по фильтру _UM.
        "BTC-USD-260626-61000-C": {
            "instId": "BTC-USD-260626-61000-C", "optType": "C", "stk": "61000",
            "expTime": "1782115200000", "uly": "BTC-USD",
        },
    }

    adapter = OkxAdapter()
    monkeypatch.setattr(adapter, "_get_market", lambda uly: (summary_by_id, instruments_by_id))
    monkeypatch.setattr(adapter, "_get_spot", lambda uly: 64088.4)
    monkeypatch.setattr(
        adapter, "_get_open_interest",
        lambda uly: {"BTC-USD_UM-260626-61000-C": 12.5, "BTC-USD_UM-260626-61000-P": 0.0},
    )

    spot, forward, options = adapter.fetch("BTC")
    assert spot == 64088.4
    assert forward is None  # forward per-record (underlying_price = fwdPx)
    assert len(options) == 2  # coin-margined (BTC-USD-...) и запись без instruments отброшены
    # Ни один остаточный option не должен принадлежать coin-margined семейству.
    assert all(o.symbol.startswith("BTC-USD_UM-") for o in options)
    call = next(o for o in options if o.option_type == "Call")
    assert call.mark_iv == pytest.approx(0.448013318450148)  # доля единицы, без /100
    assert call.strike == 61000.0
    assert call.underlying_price == 64053.2
    # Греков в OKX bulk нет
    assert call.delta is None
    # Open interest из отдельного OI-эндпоинта (oiCcy) — в единицах базового актива.
    assert call.open_interest == pytest.approx(12.5)
    # expTime 1782115200000 ms → 2026-06-23 08:00:00 UTC
    expected_dt = datetime.fromtimestamp(1782115200000 / 1000, tz=timezone.utc).replace(tzinfo=None)
    assert call.expiry_dt == expected_dt


def test_okx_get_open_interest_parses_oiCcy(monkeypatch):
    """_get_open_interest тянет отдельный эндпоинт /api/v5/public/open-interest
    и строит dict instId → oiCcy (нативно в единицах базового актива)."""
    payload = {
        "code": "0",
        "data": [
            {"instId": "BTC-USD_UM-260626-61000-C", "oi": "1250", "oiCcy": "12.5", "oiUsd": "800000"},
            {"instId": "BTC-USD_UM-260626-61000-P", "oi": "0", "oiCcy": "0", "oiUsd": "0"},
        ],
    }
    adapter = OkxAdapter()

    captured = {}
    def fake_get_json(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return payload

    monkeypatch.setattr("exchanges.okx.net.get_json", fake_get_json)
    oi = adapter._get_open_interest("BTC-USD")
    assert oi == {"BTC-USD_UM-260626-61000-C": 12.5, "BTC-USD_UM-260626-61000-P": 0.0}
    # instType=OPTION обязателен для эндпоинта.
    assert captured["params"] == {"instType": "OPTION", "uly": "BTC-USD"}


def test_okx_get_open_interest_returns_empty_on_network_error(monkeypatch):
    """При сетевой ошибке OI-эндпоинта метод не падает, а возвращает пустой dict —
    тогда open_interest опционов останется None (graceful degradation)."""
    import requests as _requests
    from exchanges import okx as okx_mod
    # Чистим TTL-кеш, чтобы тест не получил данные от предыдущих запусков.
    okx_mod._oi_cache.clear()
    adapter = OkxAdapter()
    def raise_(*a, **kw):
        raise _requests.RequestException("network down")
    monkeypatch.setattr("exchanges.okx.net.get_json", raise_)
    assert adapter._get_open_interest("BTC-USD") == {}


# --------------------------------------------------------------------------------------
# Binance
# --------------------------------------------------------------------------------------

def test_binance_parse_symbol():
    parsed = binance_parse_symbol("BTC-260626-140000-C")
    assert parsed == ("BTC", 140000.0, "Call", datetime(2026, 6, 26))


def test_binance_parse_put():
    parsed = binance_parse_symbol("ETH-251225-3000-P")
    assert parsed[2] == "Put"
    assert parsed[1] == 3000.0


def test_binance_parse_invalid():
    assert binance_parse_symbol("garbage") is None
    assert binance_parse_symbol("BTC-99-140000-C") is None


def test_binance_adapter_markiv_and_greeks(monkeypatch):
    """Binance: markIV — доля единицы (как есть); греки есть в ответе (передаём);
    spot из /index."""
    mark = [
        {
            "symbol": "BTC-260626-140000-C",
            "markPrice": "0.001", "markIV": "1.135",
            "bidIV": "-1.0", "askIV": "2.15702534",
            "delta": "0.0", "gamma": "0.0", "theta": "-0.00000771", "vega": "0.00000064",
        },
        {
            "symbol": "BTC-260626-140000-P",
            "markPrice": "0.05", "markIV": "0.8",
            "bidIV": "0.7", "askIV": "0.9",
            "delta": "-0.5", "gamma": "0.0001", "theta": "-10", "vega": "50",
        },
        {"symbol": "garbage"},  # пропускается
    ]
    index = {"indexPrice": "64155.76847826", "time": 1782047457994}

    adapter = BinanceAdapter()
    monkeypatch.setattr(adapter, "_get_mark", lambda: mark)
    monkeypatch.setattr(adapter, "_get_index", lambda underlying: 64155.76847826)

    spot, forward, options = adapter.fetch("BTC")
    assert spot == 64155.76847826
    assert forward is None  # forward в bulk нет → r = 0
    assert len(options) == 2
    call = next(o for o in options if o.option_type == "Call")
    assert call.mark_iv == 1.135  # доля единицы, без /100
    assert call.mark_price == 0.001
    # Binance отдаёт греки — они передаются дальше
    assert call.delta == 0.0
    assert call.vega == 0.00000064
    assert call.underlying_price is None
    assert call.expiry_dt == datetime(2026, 6, 26)
    # OI-эндпоинт Binance недоступен → open_interest = None.
    assert call.open_interest is None


def test_binance_parse_decimal_strikes():
    """XRP/DOGE имеют десятичные страйки (1.1, 0.085) — парсер должен их брать."""
    assert binance_parse_symbol("XRP-260626-1.1-C") == ("XRP", 1.1, "Call", datetime(2026, 6, 26))
    assert binance_parse_symbol("DOGE-260626-0.085-P") == ("DOGE", 0.085, "Put", datetime(2026, 6, 26))
    assert binance_parse_symbol("SOL-260626-104-C") == ("SOL", 104.0, "Call", datetime(2026, 6, 26))


def test_binance_adapter_filters_by_coin(monkeypatch):
    """/eapi/v1/mark отдаёт опционы ВСЕХ активов одним списком — fetch должен
    вернуть только опционы запрошенной монеты и её spot."""
    mark = [
        {"symbol": "BTC-260626-100000-C", "markIV": "0.5", "markPrice": "100",
         "delta": "0.5", "gamma": "0.0001", "theta": "-5", "vega": "20"},
        {"symbol": "SOL-260626-104-C", "markIV": "1.3326", "markPrice": "0.0539",
         "delta": "0.0144", "gamma": "0.001", "theta": "-0.1", "vega": "1"},
        {"symbol": "DOGE-260626-0.085-C", "markIV": "0.6118", "markPrice": "1.6093",
         "delta": "0.4051", "gamma": "0.01", "theta": "-0.01", "vega": "0.5"},
        {"symbol": "ETH-260626-3000-C", "markIV": "0.6", "markPrice": "50",
         "delta": "0.5", "gamma": "0.0001", "theta": "-3", "vega": "15"},
    ]

    adapter = BinanceAdapter()
    monkeypatch.setattr(adapter, "_get_mark", lambda: mark)
    monkeypatch.setattr(adapter, "_get_index", lambda underlying: 73.98 if underlying == "SOLUSDT" else 1.0)

    spot, forward, options = adapter.fetch("SOL")
    assert spot == 73.98
    assert forward is None
    # Только SOL-опцион, без BTC/DOGE/ETH
    assert len(options) == 1
    assert all(o.base_coin == "SOL" for o in options)
    sol = options[0]
    assert sol.strike == 104.0
    assert sol.mark_iv == pytest.approx(1.3326)


def test_binance_unsupported_coin_raises():
    adapter = BinanceAdapter()
    with pytest.raises(ValueError):
        adapter.fetch("BNB")  # торгуется на Binance, но не добавлен в COIN_ALIASES
