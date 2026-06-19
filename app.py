#!/usr/bin/env python3.14

import logging
import random
import re
import threading
import time
from datetime import datetime

import plotly.graph_objects as go
import requests
from flask import Flask, render_template, request

import bs_greeks

BASE_URL = "https://api.bybit.com"
REQUEST_TIMEOUT = 10
CACHE_TTL_SECONDS = 60
REQUEST_MAX_ATTEMPTS = 4
REQUEST_BACKOFF_BASE_SECONDS = 0.6
REQUEST_BACKOFF_MAX_SECONDS = 6.0
# Верхняя граница суммарного времени всех попыток запроса к Bybit. Без неё
# 4 попытки × 10с timeout + backoff могут занять ~45с, что приводило к
# WORKER TIMEOUT у gunicorn. Дедлайн гарантирует, что даже фоновый прогрев
# не зависнет надолго.
REQUEST_MAX_TOTAL_SECONDS = 25

# Фоновое кеширование: как часто воркер полностью обновляет все (coin, metric).
CACHE_REFRESH_INTERVAL_SECONDS = 180

COIN_ALIASES = {
    "BTC": {"api_base_coin": "BTC", "symbol_prefixes": {"BTC", "BTCUSDT"}},
    "ETH": {"api_base_coin": "ETH", "symbol_prefixes": {"ETH", "ETHUSDT"}},
    "SOL": {"api_base_coin": "SOL", "symbol_prefixes": {"SOL", "SOLUSDT"}},
    "XRP": {"api_base_coin": "XRP", "symbol_prefixes": {"XRP", "XRPUSDT"}},
    "DOGE": {"api_base_coin": "DOGE", "symbol_prefixes": {"DOGE", "DOGEUSDT"}},
    "XAUTUSDT": {"api_base_coin": "XAUT", "symbol_prefixes": {"XAUT", "XAUTUSDT"}},
}
SUPPORTED_COINS = list(COIN_ALIASES.keys())
MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

DEFAULT_COIN = "BTC"
STRIKE_COVERAGE_RATIO = 0.7

# Безрисковая ставка по умолчанию (крипто-конвенция: r = 0). Пользователь может
# задать свою через query-param ?rate=, значение валидируется и клампится в [0, 1].
DEFAULT_RATE = 0.0
RATE_MIN = 0.0
RATE_MAX = 1.0

# Метрики, доступные в выпадающем списке. ``source`` указывает происхождение
# значения для справочника: ``api`` — берётся из ответа Bybit, ``bs`` — считается
# локально по модели Блэка — Шоулза (греки высшего порядка API не отдаются).
SUPPORTED_METRICS = {
    "iv": {
        "label": "Implied Volatility",
        "axis_title": "Implied Volatility (%)",
        "value_key": "iv",
        "source": "api",
        "description": (
            "Подразумеваемая волатильность (поле markIv из API Bybit). Рыночная "
            "оценка ожидаемого разброса цены базового актива до экспирации. "
            "Основа «улыбки волатильности»: чем выше IV, тем дороже опцион."
        ),
    },
    "theta": {
        "label": "Theta",
        "axis_title": "Theta",
        "value_key": "theta",
        "source": "api",
        "description": (
            "Тета — скорость временного распада стоимости опциона (∂P/∂t). "
            "Берётся из API Bybit. Обычно отрицательна для длинной позиции: "
            "с каждым днём опцион теряет премию по мере приближения экспирации."
        ),
    },
    "theta_pct": {
        "label": "Theta / Mark Price",
        "axis_title": "Theta / Mark Price (%)",
        "value_key": "theta_pct",
        "source": "derived",
        "description": (
            "Тета, нормированная на mark price опциона (считается локально как "
            "theta / markPrice × 100). Показывает дневной распад в процентах от "
            "стоимости опциона — удобная метрика для сравнения опционов разной цены."
        ),
    },
    "mark_price": {
        "label": "Mark Price",
        "axis_title": "Mark Price",
        "value_key": "mark_price",
        "source": "api",
        "description": (
            "Маркировочная цена опциона (поле markPrice из API Bybit). "
            "Это биржевая оценка текущей справедливой цены, которую Bybit "
            "транслирует вместе с остальными параметрами тикера. На графике "
            "показываются OTM-премии: Put ниже spot и Call выше spot."
        ),
    },
    "delta": {
        "label": "Delta",
        "axis_title": "Delta",
        "value_key": "delta",
        "source": "api",
        "description": (
            "Дельта (∂P/∂S) — изменение цены опциона при изменении цены базового "
            "актива на единицу. Для Call принимает значения 0…1, для Put −1…0. "
            "Берётся из API Bybit. Также интерпретируется как вероятность экспирации ITM."
        ),
    },
    "vega": {
        "label": "Vega",
        "axis_title": "Vega",
        "value_key": "vega",
        "source": "api",
        "description": (
            "Вега (∂P/∂σ) — изменение цены опциона при росте волатильности на 1 "
            "пункт (1 доля единицы). Показывает чувствительность к IV. Берётся из "
            "API Bybit. Vega всегда положительна для длинной позиции в опционе."
        ),
    },
    "vanna": {
        "label": "Vanna",
        "axis_title": "Vanna",
        "value_key": "vanna",
        "source": "bs",
        "description": (
            "Ванна (∂Delta/∂σ = ∂Vega/∂S) — грек второго порядка: скорость "
            "изменения дельты при росте волатильности. Характеризует «перекос» "
            "(skew) волатильности. Считается локально по модели Блэка — Шоулза, "
            "т.к. Bybit API этот грек не отдаёт."
        ),
    },
    "volga": {
        "label": "Volga / Vomma",
        "axis_title": "Volga (Vomma)",
        "value_key": "volga",
        "source": "bs",
        "description": (
            "Волга / Вомма (∂²P/∂σ²) — вторая производная цены по волатильности, "
            "«выпуклость по веге». Положительная волга означает, что опцион "
            "выигрывает от роста волатильности сильнее, чем предсказывает линейная "
            "вега. Считается локально по модели Блэка — Шоулза."
        ),
    },
    "speed": {
        "label": "Speed",
        "axis_title": "Speed",
        "value_key": "speed",
        "source": "bs",
        "description": (
            "Спид (∂Gamma/∂S) — грек третьего порядка: скорость изменения гаммы "
            "при движении цены базового актива. Важен для динамического "
            "хеджирования крупных позиций. Считается локально по модели "
            "Блэка — Шоулза. Величины очень малые — ось масштабируется автоматически."
        ),
    },
    "charm": {
        "label": "Charm",
        "axis_title": "Charm",
        "value_key": "charm",
        "source": "bs",
        "description": (
            "Чарм (∂Delta/∂t) — скорость изменения дельты по мере течения времени "
            "(дрейф дельты). Помогает понять, как часто нужно ребалансировать "
            "дельта-хедж. Считается локально по модели Блэка — Шоулза. "
            "При q = 0 одинаков для Call и Put."
        ),
    },
    "ultima": {
        "label": "Ultima",
        "axis_title": "Ultima",
        "value_key": "ultima",
        "source": "bs",
        "description": (
            "Ультима (∂Vomma/∂σ = ∂³P/∂σ³) — грек третьего порядка по "
            "волатильности. Характеризует стабильность волги при изменениях IV. "
            "Считается локально по модели Блэка — Шоулза. Полезна при торговле "
            "весьма далёкими от денег опционами (wing options)."
        ),
    },
}
DEFAULT_METRIC = "iv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("volatility_smile_bybit")

app = Flask(__name__)


# --------------------------------------------------------------------------------------
# Сетевой слой
# --------------------------------------------------------------------------------------

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
                    "Bybit request aborted after %.1fs deadline (attempt %s/%s): %s",
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
                "Bybit request failed (attempt %s/%s), backing off %.2fs: %s",
                attempt,
                REQUEST_MAX_ATTEMPTS,
                sleep_for,
                exc,
            )
            time.sleep(sleep_for)
    raise last_exc


# --------------------------------------------------------------------------------------
# Доменная логика
# --------------------------------------------------------------------------------------

def parse_symbol(symbol):
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

    return {
        "base_coin": base_coin,
        "strike": float(strike),
        "option_type": "Call" if option_type == "C" else "Put",
        "expiry_dt": expiry_dt,
    }


def get_coin_config(selected_coin):
    return COIN_ALIASES[selected_coin]


_tickers_cache = {}
_tickers_cache_lock = threading.Lock()


def get_tickers(base_coin):
    cache_key = base_coin
    now_ts = time.time()
    with _tickers_cache_lock:
        cached = _tickers_cache.get(cache_key)
        if cached and (now_ts - cached["ts"]) < CACHE_TTL_SECONDS:
            return cached["tickers"]

    url = f"{BASE_URL}/v5/market/tickers"
    params = {"category": "option", "baseCoin": base_coin}
    try:
        response = _requests_get_with_retry(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Не удалось загрузить опционные тикеры Bybit для {base_coin} после нескольких попыток."
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Bybit вернул невалидный JSON-ответ.") from exc

    tickers = payload.get("result", {}).get("list")
    if not isinstance(tickers, list):
        raise RuntimeError("Bybit вернул неожиданный формат данных для списка тикеров.")

    with _tickers_cache_lock:
        _tickers_cache[cache_key] = {"ts": now_ts, "tickers": tickers}
    return tickers


def get_spot_price(tickers):
    for ticker in tickers:
        index_price = ticker.get("indexPrice")
        if index_price and index_price != "":
            return float(index_price)
    return None


def matches_selected_coin(parsed_base_coin, selected_coin):
    return parsed_base_coin in get_coin_config(selected_coin)["symbol_prefixes"]


def format_strike(strike):
    if strike >= 100:
        return f"{strike:,.0f}"
    if strike == int(strike):
        return f"{strike:.0f}"
    return f"{strike:.2f}"


def parse_numeric(value):
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_metric(value):
    metric = (value or DEFAULT_METRIC).lower()
    if metric not in SUPPORTED_METRICS:
        return DEFAULT_METRIC
    return metric


def normalize_rate(value):
    """Парсит и клампит безрисковую ставку из query-param. Возвращает float в [RATE_MIN, RATE_MAX]."""
    if value is None or value == "":
        return DEFAULT_RATE
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RATE
    return max(RATE_MIN, min(RATE_MAX, rate))


def calculate_theta_pct(theta_value, mark_price_value):
    theta_numeric = parse_numeric(theta_value)
    mark_price_numeric = parse_numeric(mark_price_value)
    if theta_numeric is None or mark_price_numeric in (None, 0):
        return None
    return theta_numeric / mark_price_numeric * 100


def collect_strikes(tickers, selected_coin):
    strikes = set()
    for ticker in tickers:
        if not ticker.get("markIv"):
            continue
        parsed = parse_symbol(ticker["symbol"])
        if parsed is None or not matches_selected_coin(parsed["base_coin"], selected_coin):
            continue
        strikes.add(parsed["strike"])
    return sorted(strikes)


def get_strike_window(strikes, spot_price):
    if not strikes:
        raise ValueError("Нет доступных страйков для расчёта диапазона.")

    target_count = max(1, round(len(strikes) * STRIKE_COVERAGE_RATIO))
    nearest_strikes = sorted(
        strikes, key=lambda strike: (abs(strike - spot_price), strike)
    )[:target_count]
    return min(nearest_strikes), max(nearest_strikes), target_count


def fetch_and_prepare_data(selected_coin, tickers, min_strike, max_strike, spot_price, risk_free_rate=DEFAULT_RATE):
    raw_by_expiry = {}
    now_dt = datetime.now()

    for ticker in tickers:
        symbol = ticker["symbol"]
        iv_str = ticker.get("markIv")
        if not iv_str:
            continue

        parsed = parse_symbol(symbol)
        if parsed is None or not matches_selected_coin(parsed["base_coin"], selected_coin):
            continue

        strike = parsed["strike"]
        if strike < min_strike or strike > max_strike:
            continue

        opt_type = parsed["option_type"]
        expiry = parsed["expiry_dt"]
        iv = float(iv_str) * 100
        mark_price = ticker.get("markPrice", "N/A")
        theta = ticker.get("theta", "N/A")

        # Время до экспирации в годах и волатильность в долях единицы — для локального
        # расчёта высших греков по модели Блэка — Шоулза.
        seconds_to_expiry = (expiry - now_dt).total_seconds()
        time_to_expiry = seconds_to_expiry / bs_greeks.SECONDS_PER_YEAR if seconds_to_expiry > 0 else 0.0
        sigma = iv / 100.0

        # Высшие греки (Bybit API их не отдаёт). При вырожденных входах получим None.
        vanna_val = bs_greeks.vanna(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
        volga_val = bs_greeks.volga(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
        speed_val = bs_greeks.speed(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
        charm_val = bs_greeks.charm(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
        ultima_val = bs_greeks.ultima(spot_price, strike, time_to_expiry, risk_free_rate, sigma)

        if expiry not in raw_by_expiry:
            raw_by_expiry[expiry] = {"Call": [], "Put": []}

        raw_by_expiry[expiry][opt_type].append(
            {
                "strike": strike,
                "iv": iv,
                "delta": ticker.get("delta", "N/A"),
                "mark_price": mark_price,
                "gamma": ticker.get("gamma", "N/A"),
                "theta": theta,
                "theta_pct": calculate_theta_pct(theta, mark_price),
                "vega": ticker.get("vega", "N/A"),
                "vanna": vanna_val,
                "volga": volga_val,
                "speed": speed_val,
                "charm": charm_val,
                "ultima": ultima_val,
                "symbol": symbol,
                "is_otm": (
                    (opt_type == "Call" and strike > spot_price)
                    or (opt_type == "Put" and strike < spot_price)
                ),
            }
        )

    by_expiry = {}
    for expiry, data in raw_by_expiry.items():
        by_expiry[expiry] = {"Call": [], "Put": []}

        for opt_type in ["Call", "Put"]:
            items = data[opt_type]
            otm = [item for item in items if item["is_otm"]]
            itm = [item for item in items if not item["is_otm"]]
            by_expiry[expiry][opt_type] = otm[:]

            if itm:
                nearest = min(itm, key=lambda item: abs(item["strike"] - spot_price))
                existing_strikes = {
                    item["strike"] for item in by_expiry[expiry][opt_type]
                }
                if nearest["strike"] not in existing_strikes:
                    by_expiry[expiry][opt_type].append(nearest)

            by_expiry[expiry][opt_type].sort(key=lambda item: item["strike"])

    sorted_expiries = sorted(by_expiry.keys())
    return spot_price, by_expiry, sorted_expiries


def build_figure(
    coin,
    spot_price,
    by_expiry,
    sorted_expiries,
    min_strike,
    max_strike,
    selected_metric,
):
    fig = go.Figure()
    metric_config = SUPPORTED_METRICS[selected_metric]
    metric_key = metric_config["value_key"]
    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
        "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
        "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5",
    ]

    for idx, expiry in enumerate(sorted_expiries):
        color = colors[idx % len(colors)]
        label_date = expiry.strftime("%d %b %Y")
        days_to_expiry = (expiry - datetime.now()).days
        legend_group = str(idx)

        calls = sorted(by_expiry[expiry]["Call"], key=lambda item: item["strike"])
        puts = sorted(by_expiry[expiry]["Put"], key=lambda item: item["strike"])
        smile_points = sorted(calls + puts, key=lambda item: item["strike"])
        if selected_metric == "mark_price":
            smile_points = [item for item in smile_points if item["is_otm"]]
        unique_points = []
        seen = set()
        for item in smile_points:
            point_key = (item["strike"], item["symbol"])
            if point_key in seen:
                continue
            metric_value = parse_numeric(item.get(metric_key))
            if metric_value is None:
                continue
            seen.add(point_key)
            unique_points.append(item)

        if not unique_points:
            continue

        fig.add_trace(
            go.Scatter(
                x=[item["strike"] for item in unique_points],
                y=[parse_numeric(item[metric_key]) for item in unique_points],
                mode="lines+markers",
                name=f"{label_date} ({days_to_expiry}д)",
                legendgroup=legend_group,
                legendgrouptitle_text=label_date,
                showlegend=True,
                line=dict(color=color, width=2, dash="solid"),
                marker=dict(size=7, symbol="circle", line=dict(width=2, color="white")),
                hovertext=[
                    build_hover_text(item, label_date, days_to_expiry, selected_metric)
                    for item in unique_points
                ],
                hoverinfo="text",
            )
        )

    spot_str = format_strike(spot_price)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    fig.update_layout(
        shapes=[
            dict(
                type="line",
                x0=spot_price,
                x1=spot_price,
                y0=0,
                y1=1,
                yref="paper",
                line=dict(color="red", width=2, dash="dash"),
                layer="below",
            )
        ],
        annotations=[
            dict(
                x=spot_price,
                y=1.02,
                xref="x",
                yref="paper",
                text=f"Spot: {spot_str}",
                showarrow=False,
                font=dict(size=11, color="red"),
                bgcolor="rgba(255,255,255,0.8)",
            )
        ],
        title={
            "text": (
                f"{metric_config['label']} Smile — {coin}  |  Spot: {spot_str}  |  {now_str}<br>"
                f"Диапазон: ±{format_strike((max_strike - min_strike) / 2)} "
                f"({format_strike(min_strike)} — {format_strike(max_strike)})"
            ),
            "x": 0.5,
            "xanchor": "center",
            "font": {"size": 16, "weight": "bold"},
        },
        xaxis_title="Strike",
        yaxis_title=metric_config["axis_title"],
        xaxis=dict(
            range=[min_strike, max_strike],
            gridcolor="rgba(176, 184, 196, 0.24)",
            zeroline=False,
        ),
        yaxis=dict(gridcolor="rgba(176, 184, 196, 0.24)"),
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        font=dict(family="Helvetica, Arial, sans-serif", color="#1c1e21"),
        hoverlabel=dict(
            bgcolor="rgba(255,255,255,0.98)",
            bordercolor="#7f8ea3",
            font=dict(size=13, family="Helvetica, Arial, sans-serif", color="#111827"),
        ),
        legend=dict(
            orientation="v",
            yanchor="top",
            y=1,
            xanchor="left",
            x=1.02,
            bgcolor="rgba(255,255,255,0.96)",
            bordercolor="rgba(207,214,228,0.8)",
            borderwidth=1,
        ),
        autosize=True,
        width=None,
        height=None,
        margin=dict(l=60, r=200, t=100, b=60),
    )
    return fig


def _format_greek(value, precision=4):
    """Форматирует значение грека. None → 'N/A'. Адаптивно: большие числа — обычный формат,
    очень малые (speed/ultima) — экспоненциальный через :.4g."""
    num = parse_numeric(value)
    if num is None:
        return "N/A"
    if abs(num) < 1e-3 and num != 0:
        return f"{num:.4g}"
    return f"{num:.{precision}f}"


def build_hover_text(item, label_date, days_to_expiry, selected_metric):
    option_type = "Call" if "-C-" in item["symbol"] else "Put"
    delta_str = f"{float(item['delta']):.4f}" if item["delta"] != "N/A" else "N/A"
    mark_str = f"{float(item['mark_price']):.2f}" if item["mark_price"] != "N/A" else "N/A"
    gamma_str = f"{float(item['gamma']):.6f}" if item["gamma"] != "N/A" else "N/A"
    theta_str = f"{float(item['theta']):.2f}" if item["theta"] != "N/A" else "N/A"
    vega_str = f"{float(item['vega']):.2f}" if item["vega"] != "N/A" else "N/A"
    metric_title = SUPPORTED_METRICS[selected_metric]["label"]
    metric_value = parse_numeric(item.get(SUPPORTED_METRICS[selected_metric]["value_key"]))
    metric_str = _format_greek(metric_value)
    theta_pct_value = parse_numeric(item.get("theta_pct"))
    theta_pct_str = f"{theta_pct_value:.2f}%" if theta_pct_value is not None else "N/A"
    return (
        f"<b>{item['symbol']}</b><br>"
        f"Экспирация: {label_date} ({days_to_expiry}д)<br>"
        f"Тип: {option_type}<br>"
        f"Страйк: {format_strike(item['strike'])}<br>"
        f"<b>Mark Price: {mark_str}</b><br>"
        f"<b>{metric_title}: {metric_str}</b><br>"
        f"IV: {item['iv']:.2f}%<br>"
        f"Theta / Mark Price: {theta_pct_str}<br>"
        f"Delta: {delta_str}<br>"
        f"Gamma: {gamma_str}<br>"
        f"Theta: {theta_str}<br>"
        f"Vega: {vega_str}<br>"
        f"Vanna: {_format_greek(item['vanna'])}<br>"
        f"Volga: {_format_greek(item['volga'])}<br>"
        f"Speed: {_format_greek(item['speed'])}<br>"
        f"Charm: {_format_greek(item['charm'])}<br>"
        f"Ultima: {_format_greek(item['ultima'])}"
    )


# --------------------------------------------------------------------------------------
# Фоновое кеширование
# --------------------------------------------------------------------------------------
#
# Воркер-демон раз в CACHE_REFRESH_INTERVAL_SECONDS обновляет отрендеренный график
# (chart_html + сводная статистика) для каждой пары (coin, metric) и кладёт в _cache.
# Маршрут index() читает кеш; при холодном старте/промахе делает синхронный refresh
# именно этой пары. Сетевой слой get_tickers имеет собственный TTL-кеш (60 с), поэтому
# фоновый проход делает по одному сетевому запросу на монету, а не на метрику.

_cache = {}
_cache_lock = threading.Lock()

_worker_started = False
_worker_start_lock = threading.Lock()

# Тройки (coin, metric, rate), которые прямо сейчас греются в фоне.
# Предотвращает дублирование параллельных фоновых запросов одной и той же пары
# при множественных cache-miss (например, несколько пользователей одновременно).
_refreshing = set()
_refreshing_lock = threading.Lock()


def _all_combos():
    # Фоновый воркер греет только дефолтную ставку; нестандартные ставки
    # считаются по запросу (cache-miss → синхронный refresh_combo).
    return [
        (coin, metric, DEFAULT_RATE)
        for coin in SUPPORTED_COINS
        for metric in SUPPORTED_METRICS
    ]


def _cache_get(coin, metric, rate):
    with _cache_lock:
        entry = _cache.get((coin, metric, rate))
        return dict(entry) if entry is not None else None


def _cache_set(coin, metric, rate, entry):
    with _cache_lock:
        _cache[(coin, metric, rate)] = entry


def _build_chart_entry(selected_coin, selected_metric, risk_free_rate=DEFAULT_RATE):
    """Сетевой запрос + рендер. Никогда не бросает исключение наружу."""
    try:
        api_base_coin = get_coin_config(selected_coin)["api_base_coin"]
        tickers = get_tickers(api_base_coin)
        spot_price = get_spot_price(tickers)
        if spot_price is None:
            raise ValueError("Не удалось определить spot цену для выбранной монеты.")

        strikes = collect_strikes(tickers, selected_coin)
        total_strikes = len(strikes)
        min_strike, max_strike, displayed_strikes = get_strike_window(strikes, spot_price)
        _, by_expiry, sorted_expiries = fetch_and_prepare_data(
            selected_coin, tickers, min_strike, max_strike, spot_price, risk_free_rate
        )
        if not by_expiry:
            raise ValueError("Нет данных для построения графика в выбранном диапазоне.")

        expiries_count = len(sorted_expiries)
        fig = build_figure(
            selected_coin,
            spot_price,
            by_expiry,
            sorted_expiries,
            min_strike,
            max_strike,
            selected_metric,
        )
        fig.update_layout(autosize=True, width=None, height=None, margin=dict(l=50, r=50, t=60, b=40))
        chart_html = fig.to_html(
            full_html=False,
            include_plotlyjs="cdn",
            config={"responsive": True},
            default_width="100%",
            default_height="100%",
        )

        return {
            "chart_html": chart_html,
            "error": None,
            "spot_price": spot_price,
            "expiries_count": expiries_count,
            "min_strike": min_strike,
            "max_strike": max_strike,
            "displayed_strikes": displayed_strikes,
            "total_strikes": total_strikes,
            "updated_at": time.time(),
            "status": "live",
        }
    except Exception as exc:
        logger.exception(
            "Error building chart (coin=%s metric=%s): %s",
            selected_coin,
            selected_metric,
            exc,
        )
        return {
            "chart_html": None,
            "error": "Не удалось загрузить данные Bybit. Попробуйте обновить страницу через минуту.",
            "spot_price": None,
            "expiries_count": 0,
            "min_strike": None,
            "max_strike": None,
            "displayed_strikes": 0,
            "total_strikes": 0,
            "updated_at": time.time(),
            "status": "error",
        }


def refresh_combo(selected_coin, selected_metric, risk_free_rate=DEFAULT_RATE):
    """Собирает график для пары и кладёт в кеш. Возвращает запись кеша."""
    entry = _build_chart_entry(selected_coin, selected_metric, risk_free_rate)
    _cache_set(selected_coin, selected_metric, risk_free_rate, entry)
    return entry


def _warming_entry():
    """Заглушка для cache-miss: мгновенно возвращается в маршруте index(),
    чтобы запрос не висел на синхронном сетевом фетче. Шаблон рисует спиннер
    и статус «Кеш: прогревается» для cache_status == 'warming'."""
    return {
        "chart_html": None,
        "error": None,
        "spot_price": None,
        "expiries_count": 0,
        "min_strike": None,
        "max_strike": None,
        "displayed_strikes": 0,
        "total_strikes": 0,
        "updated_at": None,
        "status": "warming",
    }


def _maybe_refresh_async(selected_coin, selected_metric, risk_free_rate):
    """Запускает прогрев пары в fire-and-forget daemon-потоке ровно один раз:
    если пара уже греется, ничего не делает. Это гарантирует, что поток
    запросов не блокируется на сети — только ставит задачу в фон."""
    key = (selected_coin, selected_metric, risk_free_rate)
    with _refreshing_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def _runner():
        try:
            refresh_combo(selected_coin, selected_metric, risk_free_rate)
        except Exception:
            logger.exception(
                "Async refresh unexpectedly failed for %s/%s", selected_coin, selected_metric
            )
        finally:
            with _refreshing_lock:
                _refreshing.discard(key)

    thread = threading.Thread(
        target=_runner,
        name=f"volatility-refresh-{selected_coin}-{selected_metric}",
        daemon=True,
    )
    thread.start()


def _background_worker_loop():
    while True:
        for coin, metric, rate in _all_combos():
            try:
                refresh_combo(coin, metric, rate)
            except Exception:
                logger.exception(
                    "Background refresh unexpectedly failed for %s/%s", coin, metric
                )
        time.sleep(CACHE_REFRESH_INTERVAL_SECONDS)


def _ensure_worker_started():
    """Запускает фоновый воркер кеша ровно один раз на процесс."""
    global _worker_started
    with _worker_start_lock:
        if _worker_started:
            return
        thread = threading.Thread(
            target=_background_worker_loop,
            name="volatility-cache-worker",
            daemon=True,
        )
        thread.start()
        _worker_started = True
        logger.info(
            "Background cache worker started (interval=%ss, combos=%s)",
            CACHE_REFRESH_INTERVAL_SECONDS,
            len(_all_combos()),
        )


# --------------------------------------------------------------------------------------
# Маршруты
# --------------------------------------------------------------------------------------

@app.get("/healthz")
def healthcheck():
    return {"status": "ok"}


@app.get("/")
def index():
    selected_coin = request.args.get("coin", DEFAULT_COIN).upper()
    if selected_coin not in SUPPORTED_COINS:
        selected_coin = DEFAULT_COIN
    selected_metric = normalize_metric(request.args.get("metric"))
    selected_rate = normalize_rate(request.args.get("rate"))

    _ensure_worker_started()

    entry = _cache_get(selected_coin, selected_metric, selected_rate)
    if entry is None:
        # Cache-miss / холодный старт: НЕ виснем на синхронном фетче (~45с в
        # худшем случае → приводило к WORKER TIMEOUT). Мгновенно отдаём
        # страницу-заглушку «прогрев кеша», а саму пару греем в фоне.
        _maybe_refresh_async(selected_coin, selected_metric, selected_rate)
        entry = _warming_entry()

    updated_at = entry.get("updated_at")
    updated_str = (
        datetime.utcfromtimestamp(updated_at).strftime("%H:%M:%S")
        if updated_at
        else "—"
    )

    return render_template(
        "index.html",
        coins=SUPPORTED_COINS,
        selected_coin=selected_coin,
        metrics=SUPPORTED_METRICS,
        selected_metric=selected_metric,
        selected_rate=selected_rate,
        chart_html=entry.get("chart_html"),
        error=entry.get("error"),
        spot_price=entry.get("spot_price"),
        expiries_count=entry.get("expiries_count"),
        min_strike=entry.get("min_strike"),
        max_strike=entry.get("max_strike"),
        displayed_strikes=entry.get("displayed_strikes"),
        total_strikes=entry.get("total_strikes"),
        cache_updated_str=updated_str,
        cache_status=entry.get("status"),
    )


# Запускаем воркер при импорте модуля, чтобы кеш прогревался даже до первого запроса
# (работает и под gunicorn, и под flask run). Демон-поток не блокирует завершение
# процесса и стартует ровно один раз за счёт _ensure_worker_started().
_ensure_worker_started()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
