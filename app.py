#!/usr/bin/env python3.14

import logging
import threading
import time
from datetime import datetime

import plotly.graph_objects as go
from flask import Flask, render_template, request

import bs_greeks
from exchanges import (
    EXCHANGES,
    DEFAULT_EXCHANGE,
    NormalizedOption,
    all_exchange_coin_pairs,
    supported_coins,
)

# Фоновое кеширование: как часто воркер полностью обновляет все (exchange, coin, metric).
# Поднято с 180 до 300с: с несколькими биржами комбинаций стало заметно больше,
# и более редкий прогрев снижает нагрузку на публичные API и риск rate-limit.
CACHE_REFRESH_INTERVAL_SECONDS = 300

DEFAULT_COIN = "BTC"
STRIKE_COVERAGE_RATIO = 0.7

# Безрисковая ставка считается автоматически для каждого опциона как implied
# cost-of-carry: r = ln(forward/spot) / T. FALLBACK_RATE используется при
# вырожденных входах (пустой forward, T → 0 и т.п.) — крипто-конвенция r = 0.
# Ставка применима только к биржам, чьи адаптеры отдают forward (Bybit —
# per-option underlying_price, OKX — fwdPx); Deribit/Binance forward не
# отдают, для них r = 0.
RATE_CLAMP = 0.5
FALLBACK_RATE = 0.0

# Метрики, доступные в выпадающем списке. ``source`` указывает происхождение
# значения для справочника: ``api`` — берётся из ответа биржи (если она его
# отдаёт, иначе считается по БС), ``bs`` — всегда считается локально по модели
# Блэка — Шоулза, ``derived`` — нормировка данных API.
SUPPORTED_METRICS = {
    "iv": {
        "label": "Implied Volatility",
        "axis_title": "Implied Volatility (%)",
        "value_key": "iv",
        "source": "api",
        "description": (
            "Подразумеваемая волатильность (mark IV из ответа биржи). Рыночная "
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
            "Берётся из API биржи (если отдаётся), иначе считается по модели "
            "Блэка — Шоулза. Обычно отрицательна для длинной позиции: с каждым "
            "днём опцион теряет премию по мере приближения экспирации."
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
            "Маркировочная цена опциона. Берётся из API биржи (если отдаётся), "
            "иначе считается по модели Блэка — Шоулза (OKX не отдаёт markPx в "
            "opt-summary). Биржевая оценка текущей справедливой цены. На графике "
            "показываются OTM-премии: Put ниже spot и Call выше spot. 注意: у "
            "Deribit премия в базовом активе (BTC), у Bybit/Binance/OKX — в USDT."
        ),
    },
    "delta": {
        "label": "Delta",
        "axis_title": "Delta",
        "value_key": "delta",
        "source": "api",
        "description": (
            "Дельта (∂P/∂S) — изменение цены опциона при изменении цены базового "
            "актива на единицу. Для Call 0…1, для Put −1…0. Берётся из API биржи, "
            "если отдаётся (Bybit/Binance); иначе считается локально по "
            "Блэку — Шоулзу (Deribit/OKX). Также интерпретируется как вероятность "
            "экспирации ITM."
        ),
    },
    "vega": {
        "label": "Vega",
        "axis_title": "Vega",
        "value_key": "vega",
        "source": "api",
        "description": (
            "Вега (∂P/∂σ) — изменение цены опциона при росте волатильности на 1 "
            "пункт. Показывает чувствительность к IV. Берётся из API биржи, если "
            "отдаётся; иначе считается по модели Блэка — Шоулза."
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
            "(skew) волатильности. Считается локально по модели Блэка — Шоулза."
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
# Доменная логика
# --------------------------------------------------------------------------------------

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


def normalize_exchange(value):
    exchange = (value or DEFAULT_EXCHANGE).lower()
    if exchange not in EXCHANGES:
        return DEFAULT_EXCHANGE
    return exchange


def calculate_theta_pct(theta_value, mark_price_value):
    theta_numeric = parse_numeric(theta_value)
    mark_price_numeric = parse_numeric(mark_price_value)
    if theta_numeric is None or mark_price_numeric in (None, 0):
        return None
    return theta_numeric / mark_price_numeric * 100


def collect_strikes(options, base_coin):
    """Уникальные страйки опционов с известным mark_iv выбранной монеты."""
    strikes = set()
    for opt in options:
        if opt.base_coin != base_coin:
            continue
        if opt.mark_iv is None or opt.mark_iv <= 0:
            continue
        strikes.add(opt.strike)
    return sorted(strikes)


def get_strike_window(strikes, spot_price):
    if not strikes:
        raise ValueError("Нет доступных страйков для расчёта диапазона.")

    target_count = max(1, round(len(strikes) * STRIKE_COVERAGE_RATIO))
    nearest_strikes = sorted(
        strikes, key=lambda strike: (abs(strike - spot_price), strike)
    )[:target_count]
    return min(nearest_strikes), max(nearest_strikes), target_count


def fetch_and_prepare_data(options, min_strike, max_strike, spot_price):
    """Трансформирует нормализованные опционы в сгруппированные по экспирации
    точки улыбки. ``options`` — list[NormalizedOption].

    Греки, отсутствующие в API биржи (delta/gamma/theta/vega = None),
    досчитываются локально по модели Блэка — Шоулза. mark_price при отсутствии
    в API (OKX) также считается по БС. Высшие греки (vanna, volga, speed,
    charm, ultima) всегда считаются локально.
    """
    raw_by_expiry = {}
    now_dt = datetime.now()

    for opt in options:
        if opt.mark_iv is None or opt.mark_iv <= 0:
            continue

        strike = opt.strike
        if strike < min_strike or strike > max_strike:
            continue

        opt_type = opt.option_type
        expiry = opt.expiry_dt
        iv = opt.mark_iv * 100  # в проценты для отображения
        sigma = opt.mark_iv     # доля единицы для BS

        # Время до экспирации в годах.
        seconds_to_expiry = (expiry - now_dt).total_seconds()
        time_to_expiry = seconds_to_expiry / bs_greeks.SECONDS_PER_YEAR if seconds_to_expiry > 0 else 0.0

        # Безрисковая ставка per-option из implied cost-of-carry:
        # r = ln(forward/spot)/T. forward = underlying_price (если биржа
        # отдаёт). Deribit/Binance не отдают forward → r = 0.
        forward_price = opt.underlying_price
        r_raw = (
            bs_greeks.implied_rate(spot_price, forward_price, time_to_expiry)
            if forward_price is not None
            else None
        )
        risk_free_rate = FALLBACK_RATE if r_raw is None else max(-RATE_CLAMP, min(RATE_CLAMP, r_raw))

        # Базовые греки: из API, если есть; иначе по БС. opt_type в нижний регистр
        # для bs_greeks (ожидает "call"/"put").
        bs_type = opt_type.lower()
        delta_val = opt.delta if opt.delta is not None else bs_greeks.bs_delta(
            spot_price, strike, time_to_expiry, risk_free_rate, sigma, option_type=bs_type
        )
        gamma_val = opt.gamma if opt.gamma is not None else bs_greeks.bs_gamma(
            spot_price, strike, time_to_expiry, risk_free_rate, sigma
        )
        theta_val = opt.theta if opt.theta is not None else bs_greeks.bs_theta(
            spot_price, strike, time_to_expiry, risk_free_rate, sigma, option_type=bs_type
        )
        vega_val = opt.vega if opt.vega is not None else bs_greeks.bs_vega(
            spot_price, strike, time_to_expiry, risk_free_rate, sigma
        )

        # Высшие греки — всегда локально. При вырожденных входах получим None.
        vanna_val = bs_greeks.vanna(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
        volga_val = bs_greeks.volga(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
        speed_val = bs_greeks.speed(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
        charm_val = bs_greeks.charm(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
        ultima_val = bs_greeks.ultima(spot_price, strike, time_to_expiry, risk_free_rate, sigma)

        if expiry not in raw_by_expiry:
            raw_by_expiry[expiry] = {"Call": [], "Put": []}

        # mark_price: из API, если есть; иначе по БС. OKX не отдаёт markPx в
        # bulk-эндпоинте opt-summary → None, считаем теор. цену локально.
        mark_price_val = opt.mark_price if opt.mark_price is not None else (
            bs_greeks.bs_call_price(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
            if bs_type == "call"
            else bs_greeks.bs_put_price(spot_price, strike, time_to_expiry, risk_free_rate, sigma)
        )

        raw_by_expiry[expiry][opt_type].append(
            {
                "strike": strike,
                "iv": iv,
                "option_type": opt_type,
                "delta": delta_val,
                "mark_price": mark_price_val,
                "gamma": gamma_val,
                "theta": theta_val,
                "theta_pct": calculate_theta_pct(theta_val, mark_price_val),
                "vega": vega_val,
                "vanna": vanna_val,
                "volga": volga_val,
                "speed": speed_val,
                "charm": charm_val,
                "ultima": ultima_val,
                "risk_free_rate": risk_free_rate,
                "symbol": opt.symbol,
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


def _fmt(value, fmt):
    """Форматирует число по шаблону (напр. ':.2f'); None → 'N/A'.
    Малые по модулю величины (< 1e-3, не ноль) показываются через :.4g,
    чтобы speed/ultima читались как экспонента."""
    num = parse_numeric(value)
    if num is None:
        return "N/A"
    if abs(num) < 1e-3 and num != 0:
        return f"{num:.4g}"
    return f"{num:{fmt}}"


def build_hover_text(item, label_date, days_to_expiry, selected_metric):
    option_type = item.get("option_type", "Call")
    delta_str = _fmt(item["delta"], ".4f")
    mark_str = _fmt(item["mark_price"], ".2f")
    gamma_str = _fmt(item["gamma"], ".6f")
    theta_str = _fmt(item["theta"], ".2f")
    vega_str = _fmt(item["vega"], ".2f")
    metric_title = SUPPORTED_METRICS[selected_metric]["label"]
    metric_str = _format_greek(parse_numeric(item.get(SUPPORTED_METRICS[selected_metric]["value_key"])))
    theta_pct_value = parse_numeric(item.get("theta_pct"))
    theta_pct_str = f"{theta_pct_value:.2f}%" if theta_pct_value is not None else "N/A"
    r_value = parse_numeric(item.get("risk_free_rate"))
    r_str = f"{r_value * 100:.2f}%" if r_value is not None else "N/A"
    iv_value = parse_numeric(item.get("iv"))
    iv_str = f"{iv_value:.2f}%" if iv_value is not None else "N/A"
    return (
        f"<b>{item['symbol']}</b><br>"
        f"Экспирация: {label_date} ({days_to_expiry}д)<br>"
        f"Тип: {option_type}<br>"
        f"Страйк: {format_strike(item['strike'])}<br>"
        f"<b>Mark Price: {mark_str}</b><br>"
        f"<b>{metric_title}: {metric_str}</b><br>"
        f"IV: {iv_str}<br>"
        f"Theta / Mark Price: {theta_pct_str}<br>"
        f"Delta: {delta_str}<br>"
        f"Gamma: {gamma_str}<br>"
        f"Theta: {theta_str}<br>"
        f"Vega: {vega_str}<br>"
        f"Vanna: {_format_greek(item['vanna'])}<br>"
        f"Volga: {_format_greek(item['volga'])}<br>"
        f"Speed: {_format_greek(item['speed'])}<br>"
        f"Charm: {_format_greek(item['charm'])}<br>"
        f"Ultima: {_format_greek(item['ultima'])}<br>"
        f"Risk-free Rate: {r_str}"
    )


# --------------------------------------------------------------------------------------
# Фоновое кеширование
# --------------------------------------------------------------------------------------
#
# Воркер-демон раз в CACHE_REFRESH_INTERVAL_SECONDS обновляет отрендеренный график
# (chart_html + сводная статистика) для каждой тройки (exchange, coin, metric) и
# кладёт в _cache. Маршрут index() читает кеш; при холодном старте/промахе делает
# асинхронный refresh именно этой тройки. Адаптеры бирж имеют собственный TTL-кеш
# сырых данных (60с), поэтому фоновый проход делает по одному сетевому запросу на
# монету, а не на метрику.

_cache = {}
_cache_lock = threading.Lock()

_worker_started = False
_worker_start_lock = threading.Lock()

# Тройки (exchange, coin, metric), которые прямо сейчас греются в фоне.
# Предотвращает дублирование параллельных фоновых запросов одной и той же тройки
# при множественных cache-miss (например, несколько пользователей одновременно).
_refreshing = set()
_refreshing_lock = threading.Lock()


def _all_combos():
    # Фоновый воркер греет все тройки (exchange, coin, metric).
    return [
        (exchange, coin, metric)
        for exchange, coin in all_exchange_coin_pairs()
        for metric in SUPPORTED_METRICS
    ]


def _cache_get(exchange, coin, metric):
    with _cache_lock:
        entry = _cache.get((exchange, coin, metric))
        return dict(entry) if entry is not None else None


def _cache_set(exchange, coin, metric, entry):
    with _cache_lock:
        _cache[(exchange, coin, metric)] = entry


def _build_chart_entry(selected_exchange, selected_coin, selected_metric):
    """Сетевой запрос + рендер. Никогда не бросает исключение наружу."""
    try:
        adapter = EXCHANGES[selected_exchange].adapter
        coin = selected_coin
        if coin not in adapter.supported_coins:
            # Выбранная монета не торгуется опционами на этой бирже — берём
            # первую поддерживаемую, чтобы график не падал.
            coin = adapter.supported_coins[0]

        spot_price, forward_price, options = adapter.fetch(coin)
        if spot_price is None:
            raise ValueError("Не удалось определить spot цену для выбранной монеты.")

        # Фильтр страйков идёт по UI-имени монеты: адаптеры кладут в option
        # именно его (Bybit XAUTUSDT, OKX и т.д.), а не префикс символа.
        strikes = collect_strikes(options, coin)
        total_strikes = len(strikes)
        min_strike, max_strike, displayed_strikes = get_strike_window(strikes, spot_price)
        _, by_expiry, sorted_expiries = fetch_and_prepare_data(
            options, min_strike, max_strike, spot_price
        )
        if not by_expiry:
            raise ValueError("Нет данных для построения графика в выбранном диапазоне.")

        expiries_count = len(sorted_expiries)
        fig = build_figure(
            coin,
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
            "coin": coin,
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
            "Error building chart (exchange=%s coin=%s metric=%s): %s",
            selected_exchange,
            selected_coin,
            selected_metric,
            exc,
        )
        return {
            "chart_html": None,
            "error": (
                f"Не удалось загрузить данные с {EXCHANGES[selected_exchange].label}. "
                "Попробуйте обновить страницу через минуту."
            ),
            "coin": selected_coin,
            "spot_price": None,
            "expiries_count": 0,
            "min_strike": None,
            "max_strike": None,
            "displayed_strikes": 0,
            "total_strikes": 0,
            "updated_at": time.time(),
            "status": "error",
        }


def refresh_combo(selected_exchange, selected_coin, selected_metric):
    """Собирает график для тройки и кладёт в кеш. Возвращает запись кеша."""
    entry = _build_chart_entry(selected_exchange, selected_coin, selected_metric)
    _cache_set(selected_exchange, selected_coin, selected_metric, entry)
    return entry


def _warming_entry():
    """Заглушка для cache-miss: мгновенно возвращается в маршруте index(),
    чтобы запрос не висел на синхронном фетче. Шаблон рисует спиннер
    и статус «Кеш: прогревается» для cache_status == 'warming'."""
    return {
        "chart_html": None,
        "error": None,
        "coin": None,
        "spot_price": None,
        "expiries_count": 0,
        "min_strike": None,
        "max_strike": None,
        "displayed_strikes": 0,
        "total_strikes": 0,
        "updated_at": None,
        "status": "warming",
    }


def _maybe_refresh_async(selected_exchange, selected_coin, selected_metric):
    """Запускает прогрев тройки в fire-and-forget daemon-потоке ровно один раз:
    если тройка уже греется, ничего не делает. Это гарантирует, что поток
    запросов не блокируется на сети — только ставит задачу в фон."""
    key = (selected_exchange, selected_coin, selected_metric)
    with _refreshing_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def _runner():
        try:
            refresh_combo(selected_exchange, selected_coin, selected_metric)
        except Exception:
            logger.exception(
                "Async refresh unexpectedly failed for %s/%s/%s",
                selected_exchange,
                selected_coin,
                selected_metric,
            )
        finally:
            with _refreshing_lock:
                _refreshing.discard(key)

    thread = threading.Thread(
        target=_runner,
        name=f"volatility-refresh-{selected_exchange}-{selected_coin}-{selected_metric}",
        daemon=True,
    )
    thread.start()


def _background_worker_loop():
    while True:
        for exchange, coin, metric in _all_combos():
            try:
                refresh_combo(exchange, coin, metric)
            except Exception:
                logger.exception(
                    "Background refresh unexpectedly failed for %s/%s/%s",
                    exchange,
                    coin,
                    metric,
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
    selected_exchange = normalize_exchange(request.args.get("exchange"))
    exchange_coins = supported_coins(selected_exchange)
    selected_coin = request.args.get("coin", DEFAULT_COIN).upper()
    if selected_coin not in exchange_coins:
        selected_coin = exchange_coins[0] if exchange_coins else DEFAULT_COIN
    selected_metric = normalize_metric(request.args.get("metric"))

    _ensure_worker_started()

    entry = _cache_get(selected_exchange, selected_coin, selected_metric)
    if entry is None:
        # Cache-miss / холодный старт: НЕ виснем на синхронном фетче. Мгновенно
        # отдаём страницу-заглушку «прогрев кеша», а саму тройку греем в фоне.
        _maybe_refresh_async(selected_exchange, selected_coin, selected_metric)
        entry = _warming_entry()

    updated_at = entry.get("updated_at")
    updated_str = (
        datetime.utcfromtimestamp(updated_at).strftime("%H:%M:%S")
        if updated_at
        else "—"
    )

    # Если выбранная монета не поддерживается биржей, отрисовываем фактическую.
    effective_coin = entry.get("coin") or selected_coin

    return render_template(
        "index.html",
        exchanges=EXCHANGES,
        selected_exchange=selected_exchange,
        exchange_label=EXCHANGES[selected_exchange].label,
        coins=exchange_coins,
        selected_coin=effective_coin,
        metrics=SUPPORTED_METRICS,
        selected_metric=selected_metric,
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
