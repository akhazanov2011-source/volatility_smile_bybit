"""
Интеграционные тесты app.py: проверяют, что item-словари содержат все новые
ключи греков, метрики нормализуются корректно, ставка валидируется, а на моках
тикеров данные подготавливаются без обращения к сети.

Сетевой слой (get_tickers, _build_chart_entry) мокается/не вызывается.
Фоновый воркер не запускается, т.к. pytest импортирует app, а функция
_ensure_worker_started() в маршруте не вызывается напрямую в этих тестах.
"""

import sys
import os
import threading
import time
from datetime import datetime, timedelta

import pytest
import requests

# Добавляем корень проекта в sys.path, чтобы можно было импортировать app и bs_greeks
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


# --------------------------------------------------------------------------------------
# normalize_metric / normalize_rate
# --------------------------------------------------------------------------------------

def test_normalize_metric_accepts_new_greeks():
    for key in ("delta", "vega", "vanna", "volga", "speed", "charm", "ultima"):
        assert app.normalize_metric(key) == key


def test_normalize_metric_keeps_existing():
    for key in ("iv", "theta", "theta_pct", "mark_price"):
        assert app.normalize_metric(key) == key


def test_normalize_metric_rejects_unknown():
    assert app.normalize_metric("nonexistent") == app.DEFAULT_METRIC
    assert app.normalize_metric(None) == app.DEFAULT_METRIC
    assert app.normalize_metric("") == app.DEFAULT_METRIC


def test_normalize_rate_default():
    assert app.normalize_rate(None) == 0.0
    assert app.normalize_rate("") == 0.0
    assert app.normalize_rate("garbage") == 0.0


def test_normalize_rate_parses_float():
    assert app.normalize_rate("0.045") == 0.045
    assert app.normalize_rate("0.1") == 0.1


def test_normalize_rate_clamps_to_range():
    assert app.normalize_rate("-0.5") == app.RATE_MIN
    assert app.normalize_rate("2.0") == app.RATE_MAX
    assert app.normalize_rate("1.5") == app.RATE_MAX


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
    expected = {"iv", "theta", "theta_pct", "mark_price", "delta", "vega", "vanna", "volga", "speed", "charm", "ultima"}
    assert expected == set(app.SUPPORTED_METRICS.keys())


# --------------------------------------------------------------------------------------
# fetch_and_prepare_data — на моках тикеров
# --------------------------------------------------------------------------------------

def _make_ticker(symbol, mark_iv="0.6", mark_price="1000", delta="0.55",
                 gamma="0.00002", theta="-5.0", vega="20.0", index_price="60000"):
    return {
        "symbol": symbol,
        "markIv": mark_iv,
        "markPrice": mark_price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "indexPrice": index_price,
    }


def _make_mock_tickers():
    """Один Call + один Put на страйке 60000, экспирация через ~90 дней."""
    future = (datetime.now() + timedelta(days=90)).strftime("%d%b%y").upper()
    return [
        _make_ticker(f"BTC-{future}-60000-C-USDT"),
        _make_ticker(f"BTC-{future}-60000-P-USDT"),
    ]


def test_fetch_and_prepare_data_returns_higher_greeks():
    tickers = _make_mock_tickers()
    spot = 60000.0
    _, by_expiry, sorted_expiries = app.fetch_and_prepare_data(
        "BTC", tickers, 59000.0, 61000.0, spot, risk_free_rate=0.0
    )
    assert len(sorted_expiries) == 1
    expiry = sorted_expiries[0]
    for opt_type in ("Call", "Put"):
        items = by_expiry[expiry][opt_type]
        assert len(items) == 1
        item = items[0]
        # Базовые ключи (из API)
        assert item["delta"] == "0.55"
        assert item["vega"] == "20.0"
        assert item["gamma"] == "0.00002"
        # Новые высшие греки — должны присутствовать (float или None)
        for key in ("vanna", "volga", "speed", "charm", "ultima"):
            assert key in item, f"Ключ {key} отсутствует в item"
            # При T > 0, σ > 0, S > 0 — должны быть числом, не None
            assert item[key] is not None, f"{key} не должен быть None для валидного опциона"
        assert item["mark_price"] == "1000"


def test_fetch_and_prepare_data_higher_greeks_change_with_rate():
    """Высшие греки (кроме Charm при T→0) должны зависеть от безрисковой ставки."""
    tickers = _make_mock_tickers()
    spot = 60000.0
    _, by1, _ = app.fetch_and_prepare_data("BTC", tickers, 59000.0, 61000.0, spot, risk_free_rate=0.0)
    _, by2, _ = app.fetch_and_prepare_data("BTC", tickers, 59000.0, 61000.0, spot, risk_free_rate=0.1)

    expiry = list(by1.keys())[0]
    item_r0 = by1[expiry]["Call"][0]
    item_r1 = by2[expiry]["Call"][0]

    # Vanna/Charm чувствительны к r через d2; проверяем, что расчёт действительно
    # использует ставку (значения должны различаться при заметной разнице r).
    assert item_r0["charm"] != item_r1["charm"], "Charm должен зависеть от ставки r"


def test_fetch_and_prepare_data_default_rate_works():
    """Без явной передачи rate используется DEFAULT_RATE и не падает."""
    tickers = _make_mock_tickers()
    spot = 60000.0
    _, by_expiry, _ = app.fetch_and_prepare_data("BTC", tickers, 59000.0, 61000.0, spot)
    expiry = list(by_expiry.keys())[0]
    assert by_expiry[expiry]["Call"] and by_expiry[expiry]["Put"]


def test_fetch_and_prepare_data_zero_iv_returns_none_greeks():
    """markIv=0 не должен вызывать ZeroDivisionError; BS-метрики — None."""
    future = (datetime.now() + timedelta(days=90)).strftime("%d%b%y").upper()
    tickers = [
        _make_ticker(f"BTC-{future}-60000-C-USDT", mark_iv="0"),
        _make_ticker(f"BTC-{future}-60000-P-USDT", mark_iv="0"),
    ]
    spot = 60000.0
    _, by_expiry, sorted_expiries = app.fetch_and_prepare_data(
        "BTC", tickers, 59000.0, 61000.0, spot, risk_free_rate=0.0
    )
    assert len(sorted_expiries) == 1
    expiry = sorted_expiries[0]
    for opt_type in ("Call", "Put"):
        item = by_expiry[expiry][opt_type][0]
        for key in ("vanna", "volga", "speed", "charm", "ultima"):
            assert item[key] is None, f"{key} должен быть None при markIv=0"


def test_build_hover_text_shows_bold_mark_price_after_strike():
    item = {
        "symbol": "BTC-TEST-60000-C-USDT",
        "strike": 60000.0,
        "mark_price": "123.45",
        "iv": 60.0,
        "delta": "0.55",
        "gamma": "0.00002",
        "theta": "-5.0",
        "theta_pct": -4.05,
        "vega": "20.0",
        "vanna": 0.1,
        "volga": 0.2,
        "speed": 0.3,
        "charm": 0.4,
        "ultima": 0.5,
    }

    hover_text = app.build_hover_text(item, "01 Jan 26", 30, "iv")

    strike_idx = hover_text.index("Страйк: 60,000<br>")
    mark_price_idx = hover_text.index("<b>Mark Price: 123.45</b><br>")
    iv_idx = hover_text.index("IV: 60.00%<br>")

    assert strike_idx < mark_price_idx < iv_idx
    assert hover_text.count("Mark Price: 123.45") == 1


def test_build_figure_mark_price_uses_otm_points_only():
    """Mark Price не должен склеивать Call/Put ITM-мостик в центре."""
    expiry = datetime.now() + timedelta(days=30)
    by_expiry = {
        expiry: {
            "Call": [
                {
                    "symbol": "BTC-TEST-1000-C-USDT",
                    "strike": 1000.0,
                    "mark_price": "120",
                    "iv": 60.0,
                    "delta": "0.8",
                    "gamma": "0.1",
                    "theta": "-1",
                    "theta_pct": -0.8,
                    "vega": "2",
                    "vanna": 0.1,
                    "volga": 0.1,
                    "speed": 0.1,
                    "charm": 0.1,
                    "ultima": 0.1,
                    "is_otm": False,
                },
                {
                    "symbol": "BTC-TEST-1100-C-USDT",
                    "strike": 1100.0,
                    "mark_price": "30",
                    "iv": 60.0,
                    "delta": "0.4",
                    "gamma": "0.1",
                    "theta": "-1",
                    "theta_pct": -3.3,
                    "vega": "2",
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
                    "strike": 1000.0,
                    "mark_price": "25",
                    "iv": 60.0,
                    "delta": "-0.4",
                    "gamma": "0.1",
                    "theta": "-1",
                    "theta_pct": -4.0,
                    "vega": "2",
                    "vanna": 0.1,
                    "volga": 0.1,
                    "speed": 0.1,
                    "charm": 0.1,
                    "ultima": 0.1,
                    "is_otm": True,
                },
                {
                    "symbol": "BTC-TEST-1100-P-USDT",
                    "strike": 1100.0,
                    "mark_price": "115",
                    "iv": 60.0,
                    "delta": "-0.8",
                    "gamma": "0.1",
                    "theta": "-1",
                    "theta_pct": -0.9,
                    "vega": "2",
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
# Кеш по 3-туплю (без сети) — через mock refresh_combo
# --------------------------------------------------------------------------------------

def test_cache_keyed_by_rate(monkeypatch):
    """_cache_get/_cache_set должны различать записи по ставке."""
    monkeypatch.setattr(app, "get_tickers", lambda base_coin: _make_mock_tickers())
    monkeypatch.setattr(app, "get_spot_price", lambda tickers: 60000.0)

    entry_a = app._cache_get("BTC", "iv", 0.0)
    entry_b = app._cache_get("BTC", "iv", 0.05)
    # До заполнения обе None
    assert entry_a is None and entry_b is None

    # Заполняем одну — вторая остаётся None
    app._cache_set("BTC", "iv", 0.0, {"status": "live", "updated_at": 0.0})
    assert app._cache_get("BTC", "iv", 0.0) is not None
    assert app._cache_get("BTC", "iv", 0.05) is None


# --------------------------------------------------------------------------------------
# Рендер шаблона index() — тулбар управления видимостью серий (без сети)
# --------------------------------------------------------------------------------------

def test_index_template_renders_series_toolbar(monkeypatch):
    """Шаблон должен рендерить тулбар серий и кнопки, когда есть chart_html."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(
        app,
        "_cache_get",
        lambda coin, metric, rate: {
            "chart_html": "<div id=\"plot\"></div>",
            "error": None,
            "spot_price": 60000.0,
            "expiries_count": 5,
            "min_strike": 55000.0,
            "max_strike": 65000.0,
            "displayed_strikes": 10,
            "total_strikes": 20,
            "updated_at": 0.0,
            "status": "live",
        },
    )

    with app.app.test_request_context("/?coin=BTC&metric=iv"):
        html = app.index()

    assert 'class="chart-toolbar"' in html
    assert 'id="show-all-series"' in html
    assert 'id="hide-all-series"' in html
    # JS, отвечающий за сохранение видимости серий в localStorage.
    assert "vsm_hidden_series" in html


def test_index_template_no_toolbar_without_chart(monkeypatch):
    """Когда chart_html отсутствует (ошибка/прогрев), кнопки управления сериями не рендерятся.
    Чипы Hint/Glossary остаются доступными всегда."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(
        app,
        "_cache_get",
        lambda coin, metric, rate: {
            "chart_html": None,
            "error": "Не удалось загрузить данные Bybit.",
            "spot_price": None,
            "expiries_count": 0,
            "min_strike": None,
            "max_strike": None,
            "displayed_strikes": 0,
            "total_strikes": 0,
            "updated_at": 0.0,
            "status": "error",
        },
    )

    with app.app.test_request_context("/?coin=BTC&metric=iv"):
        html = app.index()

    # Кнопок управления сериями нет — графика нет.
    assert 'id="show-all-series"' not in html
    assert 'id="hide-all-series"' not in html
    # Чипы справки доступны в любом случае.
    assert 'id="open-hint"' in html
    assert 'id="open-glossary"' in html


def test_index_template_renders_metric_description(monkeypatch):
    """Шаблон должен рендерить описание выбранной метрики над графиком."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(
        app,
        "_cache_get",
        lambda coin, metric, rate: {
            "chart_html": "<div id=\"plot\"></div>",
            "error": None,
            "spot_price": 60000.0,
            "expiries_count": 5,
            "min_strike": 55000.0,
            "max_strike": 65000.0,
            "displayed_strikes": 10,
            "total_strikes": 20,
            "updated_at": 0.0,
            "status": "live",
        },
    )

    with app.app.test_request_context("/?coin=BTC&metric=iv"):
        html = app.index()

    assert 'class="metric-description"' in html
    # Описание метрики IV из SUPPORTED_METRICS должно быть в HTML.
    assert "Подразумеваемая волатильность" in html


def test_index_template_cache_updated_utc_label(monkeypatch):
    """В кеш-баре должна быть пометка (UTC) рядом со временем обновления."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(
        app,
        "_cache_get",
        lambda coin, metric, rate: {
            "chart_html": "<div id=\"plot\"></div>",
            "error": None,
            "spot_price": 60000.0,
            "expiries_count": 5,
            "min_strike": 55000.0,
            "max_strike": 65000.0,
            "displayed_strikes": 10,
            "total_strikes": 20,
            "updated_at": 0.0,
            "status": "live",
        },
    )

    with app.app.test_request_context("/?coin=BTC&metric=iv"):
        html = app.index()

    assert "(UTC)" in html


# --------------------------------------------------------------------------------------
# Неблокирующий cache-miss: index() не виснет на сети, отдаёт status="warming"
# --------------------------------------------------------------------------------------

def test_index_cache_miss_returns_warming_and_does_not_refresh_sync(monkeypatch):
    """При cache-miss маршрут должен мгновенно вернуть заглушку 'warming' и
    поставить прогрев в фон, а не вызывать refresh_combo синхронно."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda coin, metric, rate: None)

    # refresh_combo ни при каких условиях не должен вызваться синхронно.
    refresh_calls = []
    monkeypatch.setattr(app, "refresh_combo", lambda *a, **k: refresh_calls.append(a))

    # _maybe_refresh_async создаёт реальный поток; подменим на стаб, чтобы
    # тест не зависел от потоков и сети.
    async_calls = []
    monkeypatch.setattr(
        app, "_maybe_refresh_async",
        lambda coin, metric, rate: async_calls.append((coin, metric, rate)),
    )

    with app.app.test_request_context("/?coin=BTC&metric=iv"):
        html = app.index()

    assert refresh_calls == [], "refresh_combo не должен вызываться синхронно при cache-miss"
    assert async_calls == [("BTC", "iv", 0.0)], "должен запустить фоновый прогрев пары"
    # Шаблон рисует спиннер и метку прогрева.
    assert 'Кеш: прогревается' in html
    assert 'Загружаем данные с Bybit' in html


def test_index_cache_miss_renders_autoreload_script(monkeypatch):
    """При status='warming' в страницу должен встраиваться JS автообновления."""
    monkeypatch.setattr(app, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(app, "_cache_get", lambda coin, metric, rate: None)
    monkeypatch.setattr(app, "_maybe_refresh_async", lambda *a, **k: None)

    with app.app.test_request_context("/?coin=BTC&metric=iv"):
        html = app.index()

    assert "window.location.search" in html


def test_maybe_refresh_async_populates_cache(monkeypatch):
    """_maybe_refresh_async должен в фоне записать пару в _cache."""
    monkeypatch.setattr(app, "get_tickers", lambda base_coin: _make_mock_tickers())
    monkeypatch.setattr(app, "get_spot_price", lambda tickers: 60000.0)

    # Чистим кеш от возможных следов других тестов.
    with app._cache_lock:
        app._cache.clear()

    # Пара ещё не в кеше.
    assert app._cache_get("BTC", "iv", 0.0) is None

    app._maybe_refresh_async("BTC", "iv", 0.0)

    # Ждём завершения фоновой задачи (демон-поток). Вешаем мьютекс на
    # _refreshing, чтобы дождаться, пока пара покинет множество «греется».
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with app._refreshing_lock:
            busy = ("BTC", "iv", 0.0) in app._refreshing
        if not busy:
            break
        time.sleep(0.02)

    entry = app._cache_get("BTC", "iv", 0.0)
    assert entry is not None
    assert entry["status"] in ("live", "error")


def test_maybe_refresh_async_dedups_concurrent_calls(monkeypatch):
    """Несколько одновременных cache-miss одной пары не должны запускать
    несколько дублирующих фоновых задач."""
    monkeypatch.setattr(app, "get_tickers", lambda base_coin: _make_mock_tickers())
    monkeypatch.setattr(app, "get_spot_price", lambda tickers: 60000.0)

    with app._cache_lock:
        app._cache.clear()
    with app._refreshing_lock:
        app._refreshing.clear()

    # Искусственно пометим пару как «уже греется».
    with app._refreshing_lock:
        app._refreshing.add(("BTC", "iv", 0.0))

    started_threads = []
    original_start = threading.Thread.start

    def counting_start(self):
        started_threads.append(self.name)
        # Возвращаемся к реальному start только для НЕ-refresh потоков,
        # чтобы тест не порождал фоновую сеть.
        if not self.name.startswith("volatility-refresh-"):
            return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", counting_start)

    app._maybe_refresh_async("BTC", "iv", 0.0)

    assert not any(n.startswith("volatility-refresh-") for n in started_threads), \
        "не должен запускать второй поток для уже греющейся пары"


# --------------------------------------------------------------------------------------
# Дедлайн на ретраи запросов
# --------------------------------------------------------------------------------------

def test_requests_get_with_retry_respects_deadline(monkeypatch):
    """Все попытки падают → общий дедлайн обрывает цикл раньше REQUEST_MAX_ATTEMPTS
    в случае, если бекофы слишком большие. Гарантирует ограниченность времени."""
    # Принудительно сжимаем дедлайн, чтобы тест был быстрым, а бекоф — заметным.
    monkeypatch.setattr(app, "REQUEST_MAX_ATTEMPTS", 10)
    monkeypatch.setattr(app, "REQUEST_MAX_TOTAL_SECONDS", 0.0)
    monkeypatch.setattr(app, "REQUEST_BACKOFF_BASE_SECONDS", 1.0)
    monkeypatch.setattr(app, "REQUEST_BACKOFF_MAX_SECONDS", 5.0)

    def always_fail(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(app.requests, "get", always_fail)

    started = time.monotonic()
    with pytest.raises(app.requests.RequestException):
        app._requests_get_with_retry("http://x", params={}, timeout=1)
    elapsed = time.monotonic() - started

    # Дедлайн 0 + первая попытка падает мгновенно → выходим почти сразу,
    # НЕ делая 10 попыток с бекофами (что заняло бы десятки секунд).
    assert elapsed < 1.5


def test_requests_get_with_retry_succeeds(monkeypatch):
    """Успешный ответ возвращается сразу, без ретраев."""
    class _FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"result": {"list": []}}

    monkeypatch.setattr(app.requests, "get", lambda *a, **k: _FakeResponse())

    resp = app._requests_get_with_retry("http://x", params={}, timeout=1)
    assert resp.json() == {"result": {"list": []}}
