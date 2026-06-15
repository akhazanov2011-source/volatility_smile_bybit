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
from apscheduler.schedulers.background import BackgroundScheduler

BASE_URL = "https://api.bybit.com"
REQUEST_TIMEOUT = 10
CACHE_TTL_SECONDS = 60
REQUEST_MAX_ATTEMPTS = 4
REQUEST_BACKOFF_BASE_SECONDS = 0.6
REQUEST_BACKOFF_MAX_SECONDS = 6.0

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
SUPPORTED_METRICS = {
    "iv": {
        "label": "Implied Volatility",
        "axis_title": "Implied Volatility (%)",
        "value_key": "iv",
    },
    "theta": {
        "label": "Theta",
        "axis_title": "Theta",
        "value_key": "theta",
    },
    "theta_pct": {
        "label": "Theta / Mark Price",
        "axis_title": "Theta / Mark Price (%)",
        "value_key": "theta_pct",
    },
}
DEFAULT_METRIC = "iv"

app = Flask(__name__)
scheduler = BackgroundScheduler()
cache = {}

@app.before_first_request
def init_scheduler():
    scheduler.add_job(fetch_data, 'interval', minutes=3)
    scheduler.start()

def fetch_data():
    # Получаем данные с Bybit API
    try:
        base_coin = DEFAULT_COIN
        tickers = get_tickers(base_coin)
        spot_price = get_spot_price(tickers)
        if spot_price is None:
            raise ValueError("Не удалось определить spot цену для выбранной монеты.")
        
        strikes = collect_strikes(tickers, base_coin)
        min_strike, max_strike, displayed_strikes = get_strike_window(strikes, spot_price)
        _, by_expiry, sorted_expiries = fetch_and_prepare_data(
            base_coin, tickers, min_strike, max_strike, spot_price
        )
        
        if not by_expiry:
            raise ValueError("Нет данных для построения графика в выбранном диапазоне.")
        
        fig = build_figure(
            base_coin,
            spot_price,
            by_expiry,
            sorted_expiries,
            min_strike,
            max_strike,
            DEFAULT_METRIC
        )
        fig.update_layout(width=None, height=720, margin=dict(l=50, r=50, t=100, b=50))
        chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn")
        
        cache['volatility_data'] = {
            'timestamp': time.time(),
            'data': chart_html
        }
        print("Данные успешно кешированы")
    except Exception as exc:
        logger.exception(
            "Unhandled error while fetching data: %s",
            exc
        )
        cache['volatility_data'] = {
            'timestamp': time.time(),
            'data': 'Ошибка загрузки данных. Попробуйте обновить страницу.'
        }
        print("Ошибка загрузки данных")

@app.route('/')
def index():
    if 'volatility_data' in cache:
        return f"Последние данные: {cache['volatility_data']['data']}"
    return "Данные не найдены в кэше"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("volatility_smile_bybit")

_tickers_cache = {}
_tickers_cache_lock = threading.Lock()


def _requests_get_with_retry(url, *, params, timeout):
    last_exc = None
    for attempt in range(1, REQUEST_MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= REQUEST_MAX_ATTEMPTS:
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


def fetch_and_prepare_data(selected_coin, tickers, min_strike, max_strike, spot_price):
    raw_by_expiry = {}

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
        width=1200,
        height=700,
        margin=dict(l=60, r=200, t=100, b=60),
    )
    return fig


def build_hover_text(item, label_date, days_to_expiry, selected_metric):
    option_type = "Call" if "-C-" in item["symbol"] else "Put"
    delta_str = f"{float(item['delta']):.4f}" if item["delta"] != "N/A" else "N/A"
    mark_str = f"{float(item['mark_price']):.2f}" if item["mark_price"] != "N/A" else "N/A"
    gamma_str = f"{float(item['gamma']):.6f}" if item["gamma"] != "N/A" else "N/A"
    theta_str = f"{float(item['theta']):.2f}" if item["theta"] != "N/A" else "N/A"
    vega_str = f"{float(item['vega']):.2f}" if item["vega"] != "N/A" else "N/A"
    metric_title = SUPPORTED_METRICS[selected_metric]["label"]
    metric_value = parse_numeric(item.get(SUPPORTED_METRICS[selected_metric]["value_key"]))
    metric_str = f"{metric_value:.2f}" if metric_value is not None else "N/A"
    theta_pct_value = parse_numeric(item.get("theta_pct"))
    theta_pct_str = f"{theta_pct_value:.2f}%" if theta_pct_value is not None else "N/A"
    return (
        f"<b>{item['symbol']}</b><br>"
        f"Экспирация: {label_date} ({days_to_expiry}д)<br>"
        f"Тип: {option_type}<br>"
        f"Страйк: {format_strike(item['strike'])}<br>"
        f"{metric_title}: {metric_str}<br>"
        f"IV: {item['iv']:.2f}%<br>"
        f"Theta / Mark Price: {theta_pct_str}<br>"
        f"Mark Price: {mark_str}<br>"
        f"Delta: {delta_str}<br>"
        f"Gamma: {gamma_str}<br>"
        f"Theta: {theta_str}<br>"
        f"Vega: {vega_str}"
    )


@app.get("/healthz")
def healthcheck():
    return {"status": "ok"}


@app.get("/")
def index():
    selected_coin = request.args.get("coin", DEFAULT_COIN).upper()
    if selected_coin not in SUPPORTED_COINS:
        selected_coin = DEFAULT_COIN
    selected_metric = normalize_metric(request.args.get("metric"))

    chart_html = None
    error = None
    spot_price = None
    expiries_count = 0
    min_strike = None
    max_strike = None
    displayed_strikes = 0
    total_strikes = 0

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
            selected_coin, tickers, min_strike, max_strike, spot_price
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
        fig.update_layout(width=None, height=720, margin=dict(l=50, r=50, t=100, b=50))
        chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn")
    except Exception as exc:
        logger.exception(
            "Unhandled error while rendering index (coin=%s metric=%s): %s",
            selected_coin,
            selected_metric,
            exc,
        )
        error = "Не удалось загрузить данные Bybit. Попробуйте обновить страницу через минуту."

    return render_template(
        "index.html",
        coins=SUPPORTED_COINS,
        selected_coin=selected_coin,
        metrics=SUPPORTED_METRICS,
        selected_metric=selected_metric,
        chart_html=chart_html,
        error=error,
        spot_price=spot_price,
        expiries_count=expiries_count,
        min_strike=min_strike,
        max_strike=max_strike,
        displayed_strikes=displayed_strikes,
        total_strikes=total_strikes,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
