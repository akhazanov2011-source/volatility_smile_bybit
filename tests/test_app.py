"""
Интеграционные тесты app.py: проверяют, что item-словари содержат все новые
ключи греков, метрики нормализуются корректно, ставка валидируется, а на моках
нормализованных опционов данные подготавливаются без обращения к сети.

Сетевой слой (адаптеры бирж, _build_chart_entry) мокается/не вызывается.
Фоновый воркер не запускается в большинстве тестов (мокается
_ensure_worker_started). Греки для бирж без API (Deribit/OKX/Binance)
досчитываются по БС прямо в fetch_and_prepare_data.
"""

import sys
import os
import threading
import time
from datetime import datetime, timedelta

import pytest

# Добавляем корень проекта в sys.path, чтобы можно было импортировать app и bs_greeks
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from exchanges import NormalizedOption


# --------------------------------------------------------------------------------------
# Хелперы: построение NormalizedOption-моков
# --------------------------------------------------------------------------------------

def _make_option(symbol, base_coin="BTC", strike=60000.0, option_type="Call",
                 mark_iv=0.6, mark_price=1000.0, delta=0.55, gamma=0.00002,
                 theta=-5.0, vega=20.0, underlying_price=60000.0,
                 open_interest=None, expiry=None):
    """Конструктор нормализованного опциона для тестов.

    По умолчанию это Call на BTC, экспирация ~90 дней, IV=0.6 (как Bybit
    markIv=0.6, но в долях единицы), forward=spot → implied rate = 0.
    """
    if expiry is None:
        expiry = datetime.now() + timedelta(days=90)
    return NormalizedOption(
        symbol=symbol,
        base_coin=base_coin,
        strike=strike,
        option_type=option_type,
        expiry_dt=expiry,
        mark_iv=mark_iv,
        mark_price=mark_price,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        underlying_price=underlying_price,
        open_interest=open_interest,
    )


def _make_mock_options(base_coin="BTC", strike=60000.0, underlying_price=60000.0,
                       expiry=None):
    """Call + Put на одном страйке — минимальный набор для улыбки.

    Общий expiry гарантирует, что Call и Put попадут в один бакет по дате
    экспирации (важно: datetime.now() имеет микросекунды — два независимых
    вызова дали бы разные ключи).
    """
    if expiry is None:
        expiry = datetime.now() + timedelta(days=90)
    future = expiry.strftime("%d%b%y").upper()
    return [
        _make_option(f"{base_coin}-{future}-{int(strike)}-C-USDT",
                     base_coin=base_coin, strike=strike, option_type="Call",
                     underlying_price=underlying_price, expiry=expiry),
        _make_option(f"{base_coin}-{future}-{int(strike)}-P-USDT",
                     base_coin=base_coin, strike=strike, option_type="Put",
                     underlying_price=underlying_price, expiry=expiry),
    ]


# --------------------------------------------------------------------------------------
# normalize_metric / normalize_exchange
# --------------------------------------------------------------------------------------

def test_normalize_metric_accepts_new_greeks():
    for key in ("delta", "vega", "vanna", "volga", "speed", "charm", "ultima", "open_interest"):
        assert app.normalize_metric(key) == key


def test_normalize_metric_keeps_existing():
    for key in ("iv", "theta", "theta_pct", "mark_price"):
        assert app.normalize_metric(key) == key


def test_normalize_metric_rejects_unknown():
    assert app.normalize_metric("nonexistent") == app.DEFAULT_METRIC
    assert app.normalize_metric(None) == app.DEFAULT_METRIC
    assert app.normalize_metric("") == app.DEFAULT_METRIC


def test_normalize_exchange_defaults():
    assert app.normalize_exchange(None) == app.DEFAULT_EXCHANGE
    assert app.normalize_exchange("") == app.DEFAULT_EXCHANGE
    assert app.normalize_exchange("bogus") == app.DEFAULT_EXCHANGE


def test_normalize_exchange_accepts_all():
    for key in ("bybit", "deribit", "okx", "binance"):
        assert app.normalize_exchange(key) == key
        assert app.normalize_exchange(key.upper()) == key


# --------------------------------------------------------------------------------------
# SUPPORTED_METRICS — все метрики имеют обязательные поля
# --------------------------------------------------------------------------------------

def test_all_metrics_have_required_fields():
    required = {"label", "axis_title", "value_key", "source", "description"}
    for key, cfg in app.SUPPORTED_METRICS.items():
        assert required.issubset(cfg.keys()), f"Метрика {key} не имеет полей: {required - set(cfg.keys())}"
        assert cfg["value_key"] == key, f"value_key должен совпадать с ключом для {key}"
        assert cfg["source"] in ("api", "bs", "derived"), f"Неизвестный source для {key}"


def test_expected_metric_keys_present():
    expected = {"iv", "theta", "theta_pct", "mark_price", "delta", "vega", "vanna", "volga", "speed", "charm", "ultima", "open_interest"}
    assert expected == set(app.SUPPORTED_METRICS.keys())


# --------------------------------------------------------------------------------------
# fetch_and_prepare_data — на моках NormalizedOption
# --------------------------------------------------------------------------------------

def test_fetch_and_prepare_data_returns_higher_greeks():
    options = _make_mock_options()
    spot = 60000.0
    _, by_expiry, sorted_expiries = app.fetch_and_prepare_data(
        options, 59000.0, 61000.0, spot
    )
    assert len(sorted_expiries) == 1
    expiry = sorted_expiries[0]
    for opt_type in ("Call", "Put"):
        items = by_expiry[expiry][opt_type]
        assert len(items) == 1
        item = items[0]
        # Базовые ключи (из API)
        assert item["delta"] == 0.55
        assert item["vega"] == 20.0
        assert item["gamma"] == 0.00002
        # Высшие греки — должны присутствовать (float или None)
        for key in ("vanna", "volga", "speed", "charm", "ultima"):
            assert key in item, f"Ключ {key} отсутствует в item"
            assert item[key] is not None, f"{key} не должен быть None для валидного опциона"
        # Ключ open_interest обязан присутствовать в точке (None если биржа не отдаёт).
        assert "open_interest" in item
        # implied rate = ln(underlying/spot)/T; при F=S → r=0
        assert item["risk_free_rate"] == 0.0
        assert item["mark_price"] == 1000.0


def test_fetch_and_prepare_data_propagates_open_interest():
    """open_interest из NormalizedOption прокидывается в точку как есть
    (уже нормализован адаптером в единицы базового актива)."""
    # Общая экспирация, чтобы Call/Put попали в один бакет улыбки.
    expiry = datetime.now() + timedelta(days=90)
    options = [
        _make_option("BTC-C-60000", option_type="Call", open_interest=12.5, expiry=expiry),
        _make_option("BTC-P-60000", option_type="Put", open_interest=3.2, expiry=expiry),
    ]
    spot = 60000.0
    _, by_expiry, sorted_expiries = app.fetch_and_prepare_data(
        options, 59000.0, 61000.0, spot
    )
    expiry_key = sorted_expiries[0]
    call = by_expiry[expiry_key]["Call"][0]
    put = by_expiry[expiry_key]["Put"][0]
    assert call["open_interest"] == pytest.approx(12.5)
    assert put["open_interest"] == pytest.approx(3.2)


def test_format_oi():
    assert app.format_oi(None) == "N/A"
    assert app.format_oi("") == "N/A"
    # Большие значения — группировка разрядов.
    assert app.format_oi(1234.5) == "1,234.5"
    # Средние — два знака.
    assert app.format_oi(42.0) == "42.00"
    # Малые (< 1) — :.4g.
    assert app.format_oi(0.05) == "0.05"


def test_aggregate_open_interest_sums_call_plus_put_per_strike():
    """На каждом страйке OI суммируется по Call+Put в одну точку."""
    calls = [
        {"strike": 100, "option_type": "Call", "open_interest": 12.5, "symbol": "C-100"},
        {"strike": 110, "option_type": "Call", "open_interest": 5.0, "symbol": "C-110"},
    ]
    puts = [
        {"strike": 100, "option_type": "Put", "open_interest": 3.2, "symbol": "P-100"},
        {"strike": 120, "option_type": "Put", "open_interest": 8.0, "symbol": "P-120"},
    ]
    agg = app._aggregate_open_interest_points(calls, puts)
    # Три страйка с OI (100, 110, 120), отсортированы.
    assert [a["strike"] for a in agg] == [100, 110, 120]
    strike100 = next(a for a in agg if a["strike"] == 100)
    assert strike100["open_interest"] == pytest.approx(15.7)
    assert strike100["oi_call"] == pytest.approx(12.5)
    assert strike100["oi_put"] == pytest.approx(3.2)
    # Страйк, где есть только Call → oi_put остаётся None.
    strike110 = next(a for a in agg if a["strike"] == 110)
    assert strike110["oi_call"] == pytest.approx(5.0)
    assert strike110["oi_put"] is None
    assert strike110["open_interest"] == pytest.approx(5.0)


def test_aggregate_open_interest_skips_none_and_sorts():
    """Опционы с None OI не дают точки. Результат отсортирован по страйку."""
    calls = [
        {"strike": 110, "option_type": "Call", "open_interest": None, "symbol": "C-110"},
        {"strike": 100, "option_type": "Call", "open_interest": 1.0, "symbol": "C-100"},
    ]
    puts = [{"strike": 105, "option_type": "Put", "open_interest": 2.0, "symbol": "P-105"}]
    agg = app._aggregate_open_interest_points(calls, puts)
    # C-110 с None пропущен → остаются 100 и 105.
    assert [a["strike"] for a in agg] == [100, 105]


def test_aggregate_open_interest_empty_when_all_none():
    """Если весь OI None (Binance) — агрегат пустой, точек на графике не будет."""
    calls = [{"strike": 100, "option_type": "Call", "open_interest": None, "symbol": "C"}]
    puts = [{"strike": 100, "option_type": "Put", "open_interest": None, "symbol": "P"}]
    assert app._aggregate_open_interest_points(calls, puts) == []


def test_build_oi_hover_text_shows_total_and_split():
    agg = {
        "strike": 60000,
        "open_interest": 15.7,
        "oi_call": 12.5,
        "oi_put": 3.2,
        "symbols": ["BTC-C-60000", "BTC-P-60000"],
    }
    text = app.build_oi_hover_text(agg, "28 MAR 2026", 90)
    assert "60,000" in text
    assert "Call+Put" in text
    # Суммарный и разбивка.
    assert "15.70" in text  # total (format_oi < 1000 → :.2f)
    assert "Call OI: 12.50" in text
    assert "Put OI: 3.20" in text


def test_build_oi_hover_text_none_side():
    """Если на страйке только Call (Put нет или его OI=None) — Put OI: N/A."""
    agg = {"strike": 110, "open_interest": 5.0, "oi_call": 5.0, "oi_put": None, "symbols": ["C-110"]}
    text = app.build_oi_hover_text(agg, "28 MAR 2026", 90)
    assert "Put OI: N/A" in text
    assert "Call OI: 5.00" in text


def test_fetch_and_prepare_data_higher_greeks_change_with_rate():
    """Высшие греки должны зависеть от безрисковой ставки, которая выводится
    из underlying_price (implied cost-of-carry)."""
    spot = 60000.0
    # Общий expiry для обоих наборов, чтобы сравнивать греки в одном бакете.
    expiry = datetime.now() + timedelta(days=90)
    opts_flat = _make_mock_options(underlying_price=60000.0, expiry=expiry)
    opts_premium = _make_mock_options(underlying_price=63000.0, expiry=expiry)

    _, by1, _ = app.fetch_and_prepare_data(opts_flat, 59000.0, 61000.0, spot)
    _, by2, _ = app.fetch_and_prepare_data(opts_premium, 59000.0, 61000.0, spot)

    expiry_key = list(by1.keys())[0]
    item_r0 = by1[expiry_key]["Call"][0]
    item_r1 = by2[expiry_key]["Call"][0]

    assert item_r0["risk_free_rate"] == 0.0
    assert item_r1["risk_free_rate"] > 0.0, "implied rate должен быть положительным при F > S"
    assert item_r0["charm"] != item_r1["charm"], "Charm должен зависеть от ставки r"


def test_fetch_and_prepare_data_missing_underlying_price_falls_back():
    """При отсутствии underlying_price ставка откатывается к FALLBACK_RATE (0)."""
    expiry = datetime.now() + timedelta(days=90)
    options = [
        _make_option("BTC-TEST-60000-C-USDT", option_type="Call",
                     underlying_price=None, expiry=expiry),
        _make_option("BTC-TEST-60000-P-USDT", option_type="Put",
                     underlying_price=None, expiry=expiry),
    ]
    spot = 60000.0
    _, by_expiry, _ = app.fetch_and_prepare_data(options, 59000.0, 61000.0, spot)
    expiry_key = list(by_expiry.keys())[0]
    assert by_expiry[expiry_key]["Call"][0]["risk_free_rate"] == app.FALLBACK_RATE


def test_fetch_and_prepare_data_default_rate_works():
    """Без underlying_price используется FALLBACK_RATE и не падает."""
    options = _make_mock_options()
    spot = 60000.0
    _, by_expiry, _ = app.fetch_and_prepare_data(options, 59000.0, 61000.0, spot)
    expiry = list(by_expiry.keys())[0]
    assert by_expiry[expiry]["Call"] and by_expiry[expiry]["Put"]


def test_fetch_and_prepare_data_zero_iv_returns_none_greeks():
    """mark_iv=0 не должен вызывать ZeroDivisionError; опции с IV<=0 пропускаются."""
    expiry = datetime.now() + timedelta(days=90)
    options = [
        _make_option("BTC-TEST-60000-C-USDT", option_type="Call", mark_iv=0.0,
                     delta=0.55, expiry=expiry),
        _make_option("BTC-TEST-60000-P-USDT", option_type="Put", mark_iv=0.0,
                     delta=-0.45, expiry=expiry),
    ]
    spot = 60000.0
    _, by_expiry, sorted_expiries = app.fetch_and_prepare_data(
        options, 59000.0, 61000.0, spot
    )
    # mark_iv<=0 → опция пропускается → данных нет
    assert sorted_expiries == []
    assert by_expiry == {}


def test_fetch_and_prepare_data_fills_greeks_when_api_missing():
    """Для бирж без API-греков (Deribit/OKX) delta/gamma/theta/vega=None
    в опционе → fetch_and_prepare_data досчитывает их по БС."""
    expiry = datetime.now() + timedelta(days=90)
    future = expiry.strftime("%d%b%y").upper()
    options = [
        NormalizedOption(
            symbol=f"BTC-{future}-60000-C", base_coin="BTC", strike=60000.0,
            option_type="Call", expiry_dt=expiry,
            mark_iv=0.6, mark_price=0.02, delta=None, gamma=None,
            theta=None, vega=None, underlying_price=None,
        ),
        NormalizedOption(
            symbol=f"BTC-{future}-60000-P", base_coin="BTC", strike=60000.0,
            option_type="Put", expiry_dt=expiry,
            mark_iv=0.6, mark_price=0.02, delta=None, gamma=None,
            theta=None, vega=None, underlying_price=None,
        ),
    ]
    spot = 60000.0
    _, by_expiry, _ = app.fetch_and_prepare_data(options, 59000.0, 61000.0, spot)
    expiry_key = list(by_expiry.keys())[0]
    call_item = by_expiry[expiry_key]["Call"][0]
    put_item = by_expiry[expiry_key]["Put"][0]
    # BS-греки должны быть числами (не None)
    assert call_item["delta"] is not None and 0 < call_item["delta"] < 1
    assert put_item["delta"] is not None and -1 < put_item["delta"] < 0
    assert call_item["gamma"] is not None and call_item["gamma"] > 0
    assert call_item["vega"] is not None and call_item["vega"] > 0
    assert call_item["theta"] is not None and call_item["theta"] < 0


def test_fetch_and_prepare_data_fills_mark_price_when_api_missing():
    """mark_price=None (как у OKX — opt-summary не отдаёт markPx) →
    fetch_and_prepare_data досчитывает его по модели Блэка — Шоулза, и значение
    совпадает с прямой BS-ценой от тех же аргументов. Также theta_pct
    становится не-None, т.к. теперь делится на вычисленную цену."""
    import bs_greeks

    expiry = datetime.now() + timedelta(days=90)
    strike = 60000.0
    spot = 60000.0
    sigma = 0.6
    # forward = spot → implied rate = 0, как у _make_option по умолчанию.
    options = [
        NormalizedOption(
            symbol="BTC-OKX-60000-C", base_coin="BTC", strike=strike,
            option_type="Call", expiry_dt=expiry,
            mark_iv=sigma, mark_price=None, delta=None, gamma=None,
            theta=None, vega=None, underlying_price=spot,
        ),
        NormalizedOption(
            symbol="BTC-OKX-60000-P", base_coin="BTC", strike=strike,
            option_type="Put", expiry_dt=expiry,
            mark_iv=sigma, mark_price=None, delta=None, gamma=None,
            theta=None, vega=None, underlying_price=spot,
        ),
    ]
    _, by_expiry, _ = app.fetch_and_prepare_data(options, 59000.0, 61000.0, spot)
    expiry_key = list(by_expiry.keys())[0]
    call_item = by_expiry[expiry_key]["Call"][0]
    put_item = by_expiry[expiry_key]["Put"][0]

    # Считаем T тем же способом, что и в fetch_and_prepare_data.
    now_dt = datetime.now()
    seconds_to_expiry = (expiry - now_dt).total_seconds()
    time_to_expiry = seconds_to_expiry / bs_greeks.SECONDS_PER_YEAR

    expected_call = bs_greeks.bs_call_price(spot, strike, time_to_expiry, 0.0, sigma)
    expected_put = bs_greeks.bs_put_price(spot, strike, time_to_expiry, 0.0, sigma)

    assert call_item["mark_price"] is not None
    assert call_item["mark_price"] == pytest.approx(expected_call)
    assert put_item["mark_price"] == pytest.approx(expected_put)
    # theta_pct теперь вычислим — не None, т.к. mark_price больше не None.
    assert call_item["theta_pct"] is not None



def test_build_hover_text_shows_bold_mark_price_after_strike():
    item = {
        "symbol": "BTC-TEST-60000-C-USDT",
        "strike": 60000.0,
        "option_type": "Call",
        "mark_price": 123.45,
        "iv": 60.0,
        "delta": 0.55,
        "gamma": 0.00002,
        "theta": -5.0,
        "theta_pct": -4.05,
        "vega": 20.0,
        "vanna": 0.1,
        "volga": 0.2,
        "speed": 0.3,
        "charm": 0.4,
        "ultima": 0.5,
        "risk_free_rate": 0.05,
    }

    hover_text = app.build_hover_text(item, "01 Jan 26", 30, "iv")

    strike_idx = hover_text.index("Страйк: 60,000<br>")
    mark_price_idx = hover_text.index("<b>Mark Price: 123.45</b><br>")
    iv_idx = hover_text.index("IV: 60.00%<br>")

    assert strike_idx < mark_price_idx < iv_idx
    assert hover_text.count("Mark Price: 123.45") == 1
    assert "Risk-free Rate: 5.00%" in hover_text
    assert "Тип: Call" in hover_text


def test_build_figure_mark_price_uses_otm_points_only():
    """Mark Price не должен склеивать Call/Put ITM-мостик в центре."""
    expiry = datetime.now() + timedelta(days=30)
    by_expiry = {
        expiry: {
            "Call": [
                {
                    "symbol": "BTC-TEST-1000-C-USDT",
                    "option_type": "Call",
                    "strike": 1000.0,
                    "mark_price": 120.0,
                    "iv": 60.0,
                    "delta": 0.8,
                    "gamma": 0.1,
                    "theta": -1.0,
                    "theta_pct": -0.8,
                    "vega": 2.0,
                    "vanna": 0.1,
                    "volga": 0.1,
                    "speed": 0.1,
                    "charm": 0.1,
                    "ultima": 0.1,
                    "is_otm": False,
                },
                {
                    "symbol": "BTC-TEST-1100-C-USDT",
                    "option_type": "Call",
                    "strike": 1100.0,
                    "mark_price": 30.0,
                    "iv": 60.0,
                    "delta": 0.4,
                    "gamma": 0.1,
                    "theta": -1.0,
                    "theta_pct": -3.3,
                    "vega": 2.0,
                    "vanna": 0.1,
                    "volga": 0.1,
                    "speed": 0.1,
                    "charm": 0.1,
                    "ultima": 0.1,
                    "is_otm": True,
                },
            ],
            "Put": [
                {
                    "symbol": "BTC-TEST-1000-P-USDT",
                    "option_type": "Put",
                    "strike": 1000.0,
                    "mark_price": 25.0,
                    "iv": 60.0,
                    "delta": -0.4,
                    "gamma": 0.1,
                    "theta": -1.0,
                    "theta_pct": -4.0,
                    "vega": 2.0,
                    "vanna": 0.1,
                    "volga": 0.1,
                    "speed": 0.1,
                    "charm": 0.1,
                    "ultima": 0.1,
                    "is_otm": True,
                },
                {
                    "symbol": "BTC-TEST-1100-P-USDT",
                    "option_type": "Put",
                    "strike": 1100.0,
                    "mark_price": 115.0,
                    "iv": 60.0,
                    "delta": -0.8,
                    "gamma": 0.1,
                    "theta": -1.0,
                    "theta_pct": -0.9,
                    "vega": 2.0,
                    "vanna": 0.1,
                    "volga": 0.1,
                    "speed": 0.1,
                    "charm": 0.1,
                    "ultima": 0.1,
                    "is_otm": False,
                },
            ],
        }
    }

    fig = app.build_figure(
        "BTC",
        1050.0,
        by_expiry,
        [expiry],
        900.0,
        1200.0,
        "mark_price",
    )

    assert list(fig.data[0].x) == [1000.0, 1100.0]
    assert list(fig.data[0].y) == [25.0, 30.0]


# --------------------------------------------------------------------------------------
# Кеш по тройке (exchange, coin, metric) — без сети
# --------------------------------------------------------------------------------------

def test_cache_keyed_by_exchange_coin_metric():
    """_cache_get/_cache_set различают записи по тройке (exchange, coin, metric)."""
    assert app._cache_get("bybit", "BTC", "iv") is None
    assert app._cache_get("deribit", "BTC", "iv") is None

    app._cache_set("bybit", "BTC", "iv", {"status": "live", "updated_at": 0.0})
    assert app._cache_get("bybit", "BTC", "iv") is not None
    # Другая биржа/метрика — None
    assert app._cache_get("deribit", "BTC", "iv") is None
    assert app._cache_get("bybit", "BTC", "delta") is None


# --------------------------------------------------------------------------------------
# Рендер шаблона index() — селектор биржи, тулбар управления сериями (без сети)
# --------------------------------------------------------------------------------------

def _stub_cache_entry(status="live"):
    return {
        "chart_html": "<div id=\"plot\"></div>",
        "error": None,
        "coin": "BTC",
        "spot_price": 60000.0,
        "expiries_count": 5,
        "min_strike": 55000.0,
        "max_strike": 65000.0,
        "displayed_strikes": 10,
        "total_strikes": 20,
        "updated_at": 0.0,
        "status": status,
    }


def test_index_template_renders_exchange_selector(monkeypatch):
    """Шаблон должен рендерить селектор биржи со всеми биржами."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda ex, c, m: _stub_cache_entry())

    with app.app.test_request_context("/?exchange=bybit&coin=BTC&metric=iv"):
        html = app.index()

    assert 'id="exchange"' in html
    assert 'name="exchange"' in html
    for label in ("Bybit", "Deribit", "OKX", "Binance"):
        assert label in html


def test_index_template_renders_series_toolbar(monkeypatch):
    """Шаблон должен рендерить тулбар серий и кнопки, когда есть chart_html."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda ex, c, m: _stub_cache_entry())

    with app.app.test_request_context("/?exchange=bybit&coin=BTC&metric=iv"):
        html = app.index()

    assert 'class="chart-toolbar"' in html
    assert 'id="show-all-series"' in html
    assert 'id="hide-all-series"' in html
    assert "vsm_hidden_series" in html


def test_index_template_no_toolbar_without_chart(monkeypatch):
    """Когда chart_html отсутствует (ошибка/прогрев), кнопки управления сериями не рендерятся.
    Чипы Hint/Glossary остаются доступными всегда."""
    entry = _stub_cache_entry(status="error")
    entry["chart_html"] = None
    entry["error"] = "Не удалось загрузить данные."
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda ex, c, m: entry)

    with app.app.test_request_context("/?exchange=bybit&coin=BTC&metric=iv"):
        html = app.index()

    assert 'id="show-all-series"' not in html
    assert 'id="hide-all-series"' not in html
    assert 'id="open-hint"' in html
    assert 'id="open-glossary"' in html


def test_index_template_renders_metric_description(monkeypatch):
    """Шаблон должен рендерить описание выбранной метрики над графиком."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda ex, c, m: _stub_cache_entry())

    with app.app.test_request_context("/?exchange=bybit&coin=BTC&metric=iv"):
        html = app.index()

    assert 'class="metric-description"' in html
    assert "Подразумеваемая волатильность" in html


def test_index_template_cache_updated_utc_label(monkeypatch):
    """В кеш-баре должна быть пометка (UTC) рядом со временем обновления."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda ex, c, m: _stub_cache_entry())

    with app.app.test_request_context("/?exchange=bybit&coin=BTC&metric=iv"):
        html = app.index()

    assert "(UTC)" in html


def test_index_template_exchange_label_in_subtitle(monkeypatch):
    """Subtitle должен содержать название выбранной биржи."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda ex, c, m: _stub_cache_entry())

    with app.app.test_request_context("/?exchange=deribit&coin=BTC&metric=iv"):
        html = app.index()

    assert "Deribit options monitor" in html


def test_index_coins_filtered_by_exchange(monkeypatch):
    """При выборе deribit селектор монет должен содержать только BTC/ETH."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda ex, c, m: _stub_cache_entry())

    with app.app.test_request_context("/?exchange=deribit&coin=BTC&metric=iv"):
        html = app.index()

    # Deribit поддерживает BTC/ETH — не должно быть DOGE/XAUTUSDT
    assert "DOGE" not in html
    assert "XAUTUSDT" not in html


# --------------------------------------------------------------------------------------
# Неблокирующий cache-miss: index() не виснет на сети, отдаёт status="warming"
# --------------------------------------------------------------------------------------

def test_index_cache_miss_returns_warming_and_does_not_refresh_sync(monkeypatch):
    """При cache-miss маршрут должен мгновенно вернуть заглушку 'warming' и
    поставить прогрев в фон, а не вызывать refresh_combo синхронно."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda ex, c, m: None)

    refresh_calls = []
    monkeypatch.setattr(app, "refresh_combo", lambda *a, **k: refresh_calls.append(a))

    async_calls = []
    monkeypatch.setattr(
        app, "_maybe_refresh_async",
        lambda ex, c, m: async_calls.append((ex, c, m)),
    )

    with app.app.test_request_context("/?exchange=bybit&coin=BTC&metric=iv"):
        html = app.index()

    assert refresh_calls == [], "refresh_combo не должен вызываться синхронно при cache-miss"
    assert async_calls == [("bybit", "BTC", "iv")], "должен запустить фоновый прогрев тройки"
    assert 'Кеш: прогревается' in html
    assert 'Загружаем данные с Bybit' in html


def test_index_cache_miss_renders_autoreload_script(monkeypatch):
    """При status='warming' в страницу должен встраиваться JS автообновления."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda ex, c, m: None)
    monkeypatch.setattr(app, "_maybe_refresh_async", lambda *a, **k: None)

    with app.app.test_request_context("/?exchange=bybit&coin=BTC&metric=iv"):
        html = app.index()

    assert "window.location.search" in html


def test_maybe_refresh_async_populates_cache(monkeypatch):
    """_maybe_refresh_async должен в фоне записать тройку в _cache.

    Патчим метод fetch на экземпляре адаптера Bybit (ExchangeConfig frozen,
    поэтому менять сам adapter нельзя — мокаем именно его метод).
    """
    from exchanges import EXCHANGES
    bybit_adapter = EXCHANGES["bybit"].adapter

    def _stub_fetch(coin):
        return 60000.0, None, _make_mock_options(base_coin=coin, strike=60000.0)

    monkeypatch.setattr(bybit_adapter, "fetch", _stub_fetch)

    with app._cache_lock:
        app._cache.clear()

    assert app._cache_get("bybit", "BTC", "iv") is None

    app._maybe_refresh_async("bybit", "BTC", "iv")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with app._refreshing_lock:
            busy = ("bybit", "BTC", "iv") in app._refreshing
        if not busy:
            break
        time.sleep(0.02)

    entry = app._cache_get("bybit", "BTC", "iv")
    assert entry is not None
    assert entry["status"] in ("live", "error")


def test_maybe_refresh_async_dedups_concurrent_calls(monkeypatch):
    """Несколько одновременных cache-miss одной тройки не должны запускать
    несколько дублирующих фоновых задач."""
    from exchanges import EXCHANGES
    bybit_adapter = EXCHANGES["bybit"].adapter

    def _stub_fetch(coin):
        return 60000.0, None, _make_mock_options(base_coin=coin, strike=60000.0)

    monkeypatch.setattr(bybit_adapter, "fetch", _stub_fetch)

    with app._cache_lock:
        app._cache.clear()
    with app._refreshing_lock:
        app._refreshing.clear()

    with app._refreshing_lock:
        app._refreshing.add(("bybit", "BTC", "iv"))

    started_threads = []
    original_start = threading.Thread.start

    def counting_start(self):
        started_threads.append(self.name)
        if not self.name.startswith("volatility-refresh-"):
            return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", counting_start)

    app._maybe_refresh_async("bybit", "BTC", "iv")

    assert not any(n.startswith("volatility-refresh-") for n in started_threads), \
        "не должен запускать второй поток для уже греющейся тройки"


# --------------------------------------------------------------------------------------
# Сетевой слой net._requests_get_with_retry — дедлайн и успех
# --------------------------------------------------------------------------------------

def test_requests_get_with_retry_respects_deadline(monkeypatch):
    """Все попытки падают → общий дедлайн обрывает цикл раньше max attempts."""
    import net
    import requests

    monkeypatch.setattr(net, "REQUEST_MAX_ATTEMPTS", 10)
    monkeypatch.setattr(net, "REQUEST_MAX_TOTAL_SECONDS", 0.0)
    monkeypatch.setattr(net, "REQUEST_BACKOFF_BASE_SECONDS", 1.0)
    monkeypatch.setattr(net, "REQUEST_BACKOFF_MAX_SECONDS", 5.0)

    def always_fail(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(net.requests, "get", always_fail)

    started = time.monotonic()
    with pytest.raises(requests.RequestException):
        net._requests_get_with_retry("http://x", params={}, timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 1.5


def test_requests_get_with_retry_succeeds(monkeypatch):
    """Успешный ответ возвращается сразу, без ретраев."""
    import net

    class _FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"result": {"list": []}}

    monkeypatch.setattr(net.requests, "get", lambda *a, **k: _FakeResponse())

    resp = net._requests_get_with_retry("http://x", params={}, timeout=1)
    assert resp.json() == {"result": {"list": []}}
