#!/usr/bin/env python3.14
"""
Локальный расчёт греков модели Блэка — Шоулза (Black — Scholes).

Модуль используется для высших порядковых греков, которые Bybit API не отдаёт:
Vanna, Volga/Vomma, Speed, Charm, Ultima. Базовые греки (delta, gamma, theta,
vega) в приложении берутся напрямую из API; здесь они реализованы для полноты
и для верификации формул высших греков через тесты конечными разностями.

Конвенции
---------
* Модель без дивидендов (q = 0). Крипто-опционы Bybit — на USDT-перпетуалы.
* Время ``T`` — в годах (долях года) до экспирации.
* Волатильность ``sigma`` — в долях единицы (0.6 = 60%), как и ставка ``r``.
* Высшие греки Vanna/Volga/Speed/Ultima совпадают для Call и Put при q = 0;
  Charm также совпадает (Delta_put = Delta_call − 1, производная по времени
  от константы равна нулю). Поэтому option_type им не нужен.
* При вырожденных входах (T ≤ 0, sigma ≤ 0, S ≤ 0) функции возвращают ``None``,
  чтобы上层 мог показать «N/A».
"""

import math

__all__ = [
    "SECONDS_PER_YEAR",
    "norm_pdf",
    "norm_cdf",
    "bs_d1_d2",
    "bs_call_price",
    "bs_put_price",
    "bs_delta",
    "bs_gamma",
    "bs_vega",
    "bs_theta",
    "vanna",
    "volga",
    "speed",
    "charm",
    "ultima",
]

# Количество секунд в юлианском году — для перевода времени до экспирации в годы.
SECONDS_PER_YEAR = 365 * 24 * 60 * 60  # 31_536_000

_SQRT_2PI = math.sqrt(2.0 * math.pi)
_INV_SQRT_2PI = 1.0 / _SQRT_2PI
_INV_SQRT_2 = 1.0 / math.sqrt(2.0)


def norm_pdf(x):
    """Плотность стандартного нормального распределения n(x)."""
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def norm_cdf(x):
    """Функция распределения стандартного нормального распределения N(x)."""
    return 0.5 * (1.0 + math.erf(x * _INV_SQRT_2))


def bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Возвращает (d1, d2) модели Блэка — Шоулза."""
    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * time_to_expiry) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def bs_call_price(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Цена Call-опциона по модели Блэка — Шоулза."""
    d1, d2 = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    discount = math.exp(-risk_free_rate * time_to_expiry)
    return spot * norm_cdf(d1) - strike * discount * norm_cdf(d2)


def bs_put_price(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Цена Put-опциона по модели Блэка — Шоулза."""
    d1, d2 = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    discount = math.exp(-risk_free_rate * time_to_expiry)
    return strike * discount * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(spot, strike, time_to_expiry, risk_free_rate, sigma, *, option_type="call"):
    """Δ: ∂Price/∂S. Для Call = N(d1), для Put = N(d1) − 1."""
    if not _is_valid(spot, time_to_expiry, sigma):
        return None
    d1, _ = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    if option_type == "put":
        return norm_cdf(d1) - 1.0
    return norm_cdf(d1)


def bs_gamma(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Γ: ∂²Price/∂S² = n(d1) / (S σ √T). Одинаково для Call и Put."""
    if not _is_valid(spot, time_to_expiry, sigma):
        return None
    d1, _ = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    return norm_pdf(d1) / (spot * sigma * math.sqrt(time_to_expiry))


def bs_vega(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Vega: ∂Price/∂σ = S · n(d1) · √T. Одинаково для Call и Put."""
    if not _is_valid(spot, time_to_expiry, sigma):
        return None
    d1, _ = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    return spot * norm_pdf(d1) * math.sqrt(time_to_expiry)


def bs_theta(spot, strike, time_to_expiry, risk_free_rate, sigma, *, option_type="call"):
    """Θ: ∂Price/∂t (за календарный год). Знак как у классической Θ (отрицательна при росте T)."""
    if not _is_valid(spot, time_to_expiry, sigma):
        return None
    d1, d2 = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    sqrt_t = math.sqrt(time_to_expiry)
    first = -(spot * norm_pdf(d1) * sigma) / (2.0 * sqrt_t)
    discount = math.exp(-risk_free_rate * time_to_expiry)
    if option_type == "put":
        return first + risk_free_rate * strike * discount * norm_cdf(-d2)
    return first - risk_free_rate * strike * discount * norm_cdf(d2)


# --------------------------------------------------------------------------------------
# Греки высшего порядка (не отдаются Bybit API, считаются локально).
# Все совпадают для Call и Put при q = 0.
# --------------------------------------------------------------------------------------

def vanna(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Vanna = ∂Delta/∂σ = ∂Vega/∂S = −n(d1) · d2 / σ."""
    if not _is_valid(spot, time_to_expiry, sigma):
        return None
    d1, d2 = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    return -norm_pdf(d1) * d2 / sigma


def volga(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Volga (Vomma) = ∂²Price/∂σ² = Vega · d1 · d2 / σ."""
    if not _is_valid(spot, time_to_expiry, sigma):
        return None
    d1, d2 = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    v = bs_vega(spot, strike, time_to_expiry, risk_free_rate, sigma)
    return v * d1 * d2 / sigma


def speed(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Speed = ∂Gamma/∂S = −n(d1) / (S² σ √T) · (d1/(σ√T) + 1)."""
    if not _is_valid(spot, time_to_expiry, sigma):
        return None
    sqrt_t = math.sqrt(time_to_expiry)
    d1, _ = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    return -norm_pdf(d1) / (spot * spot * sigma * sqrt_t) * (d1 / (sigma * sqrt_t) + 1.0)


def charm(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Charm = ∂Delta/∂t (дрейф дельты по календарному времени).

    При q = 0 одинаков для Call и Put. Замкнутая форма:
        Charm = −n(d1) · [2rT − d2·σ·√T] / (2 σ T √T).
    """
    if not _is_valid(spot, time_to_expiry, sigma):
        return None
    sqrt_t = math.sqrt(time_to_expiry)
    d1, d2 = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    return -norm_pdf(d1) * (2.0 * risk_free_rate * time_to_expiry - d2 * sigma * sqrt_t) / (
        2.0 * sigma * time_to_expiry * sqrt_t
    )


def ultima(spot, strike, time_to_expiry, risk_free_rate, sigma):
    """Ultima = ∂Vomma/∂σ = ∂³Price/∂σ³ = ∂Volga/∂σ.

    Считается по правилу произведения из Volga = Vega · d1·d2 / σ:
        Ultima = (dVega/dσ)·(d1·d2/σ) + Vega · d(d1·d2/σ)/dσ,
    где dVega/dσ = Vega·d1·(d1/σ − √T),
          dd1/dσ = −d1/σ + √T,
          dd2/dσ = −d1/σ.
    Замкнутая форма через d1, d2 даёт ошибки знака в популярных справочниках,
    поэтому здесь — аккуратный вывод через аналитические производные, верифицированный
    конечными разностями (∂Volga/∂σ) в тестах.
    """
    if not _is_valid(spot, time_to_expiry, sigma):
        return None
    sqrt_t = math.sqrt(time_to_expiry)
    d1, d2 = bs_d1_d2(spot, strike, time_to_expiry, risk_free_rate, sigma)
    vega = spot * norm_pdf(d1) * sqrt_t
    # Производные d1, d2 по σ
    dd1_dsigma = -d1 / sigma + sqrt_t
    dd2_dsigma = -d1 / sigma
    # dVega/dσ = Vega · d1 · (d1/σ − √T)
    dvega_dsigma = vega * d1 * (d1 / sigma - sqrt_t)
    # d(d1·d2/σ)/dσ = [(dd1·d2 + d1·dd2)·σ − d1·d2] / σ²
    d1d2 = d1 * d2
    d_ratio_dsigma = ((dd1_dsigma * d2 + d1 * dd2_dsigma) * sigma - d1d2) / (sigma * sigma)
    return dvega_dsigma * (d1d2 / sigma) + vega * d_ratio_dsigma


def _is_valid(spot, time_to_expiry, sigma):
    """Защита от вырожденных входов: все аргументы должны быть положительными."""
    return spot > 0 and time_to_expiry > 0 and sigma > 0
