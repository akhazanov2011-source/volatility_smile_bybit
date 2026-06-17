"""
Тесты модуля bs_greeks.

Главная проверка — верификация замкнутых формул высших греков через центральные
конечные разности (finite differences) от нижестоящих производных. Это ловит
ошибки знака и формулы, которые невозможно поймать на эталонных значениях.
"""

import math

import pytest

import bs_greeks as bs


# --------------------------------------------------------------------------------------
# Нормальное распределение
# --------------------------------------------------------------------------------------

def test_norm_cdf_at_zero():
    assert bs.norm_cdf(0.0) == pytest.approx(0.5)


def test_norm_cdf_symmetry():
    # N(-x) = 1 - N(x)
    for x in (0.5, 1.0, 1.96, 2.5, 3.0):
        assert bs.norm_cdf(-x) == pytest.approx(1.0 - bs.norm_cdf(x))


def test_norm_cdf_known_values():
    assert bs.norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
    assert bs.norm_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)
    assert bs.norm_cdf(3.0) == pytest.approx(0.99865, abs=1e-4)


def test_norm_pdf_at_zero():
    # n(0) = 1 / sqrt(2π) ≈ 0.39894
    assert bs.norm_pdf(0.0) == pytest.approx(1.0 / math.sqrt(2.0 * math.pi))


def test_norm_pdf_integrates_to_one():
    # Простой численный интеграл n(x) от -5 до 5 ≈ 1
    n = 10000
    a, b = -5.0, 5.0
    h = (b - a) / n
    total = 0.0
    for i in range(n + 1):
        x = a + i * h
        weight = 0.5 if i == 0 or i == n else 1.0
        total += weight * bs.norm_pdf(x) * h
    assert total == pytest.approx(1.0, abs=1e-4)


# --------------------------------------------------------------------------------------
# Базовые греки против конечных разностей цены BS
# --------------------------------------------------------------------------------------

PARAMS = dict(spot=60000.0, strike=60000.0, time_to_expiry=0.25, risk_free_rate=0.03, sigma=0.6)
H = dict(spot=1.0, sigma=1e-4, t=1e-4)


def test_bs_delta_matches_fd():
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["spot"]
    up = bs.bs_call_price(S + h, K, T, r, sigma)
    down = bs.bs_call_price(S - h, K, T, r, sigma)
    fd_delta = (up - down) / (2 * h)
    assert bs.bs_delta(S, K, T, r, sigma) == pytest.approx(fd_delta, rel=1e-3)


def test_bs_gamma_matches_fd():
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["spot"]
    up = bs.bs_call_price(S + h, K, T, r, sigma)
    mid = bs.bs_call_price(S, K, T, r, sigma)
    down = bs.bs_call_price(S - h, K, T, r, sigma)
    fd_gamma = (up - 2 * mid + down) / (h * h)
    assert bs.bs_gamma(S, K, T, r, sigma) == pytest.approx(fd_gamma, rel=1e-2)


def test_bs_vega_matches_fd():
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["sigma"]
    up = bs.bs_call_price(S, K, T, r, sigma + h)
    down = bs.bs_call_price(S, K, T, r, sigma - h)
    fd_vega = (up - down) / (2 * h)
    # Vega в BS-конвенции — за единичное изменение σ (доли единицы), что и считаем
    assert bs.bs_vega(S, K, T, r, sigma) == pytest.approx(fd_vega, rel=1e-3)


def test_bs_theta_matches_fd():
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["t"]
    up = bs.bs_call_price(S, K, T + h, r, sigma)
    down = bs.bs_call_price(S, K, T - h, r, sigma)
    # Theta = dPrice/dt, но сигма нашей theta — за год, и по классической конвенции
    # Theta = ∂P/∂t где t — календарное время (T = T_expiry − t). Поэтому ∂P/∂t = −∂P/∂T.
    fd_theta = -(up - down) / (2 * h)
    assert bs.bs_theta(S, K, T, r, sigma) == pytest.approx(fd_theta, rel=1e-2)


# --------------------------------------------------------------------------------------
# Высшие греки против конечных разностей нижестоящих производных
# --------------------------------------------------------------------------------------

def test_vanna_matches_fd_of_delta_wrt_sigma():
    """Vanna = ∂Delta/∂σ."""
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["sigma"]
    up = bs.bs_delta(S, K, T, r, sigma + h)
    down = bs.bs_delta(S, K, T, r, sigma - h)
    fd = (up - down) / (2 * h)
    assert bs.vanna(S, K, T, r, sigma) == pytest.approx(fd, rel=1e-2)


def test_vanna_equals_fd_of_vega_wrt_spot():
    """Альтернативная форма: Vanna = ∂Vega/∂S."""
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["spot"]
    up = bs.bs_vega(S + h, K, T, r, sigma)
    down = bs.bs_vega(S - h, K, T, r, sigma)
    fd = (up - down) / (2 * h)
    assert bs.vanna(S, K, T, r, sigma) == pytest.approx(fd, rel=1e-2)


def test_volga_matches_fd_of_vega_wrt_sigma():
    """Volga = ∂²P/∂σ² = ∂Vega/∂σ."""
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["sigma"]
    up = bs.bs_vega(S, K, T, r, sigma + h)
    down = bs.bs_vega(S, K, T, r, sigma - h)
    fd = (up - down) / (2 * h)
    assert bs.volga(S, K, T, r, sigma) == pytest.approx(fd, rel=1e-2)


def test_speed_matches_fd_of_gamma_wrt_spot():
    """Speed = ∂Gamma/∂S."""
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["spot"]
    up = bs.bs_gamma(S + h, K, T, r, sigma)
    down = bs.bs_gamma(S - h, K, T, r, sigma)
    fd = (up - down) / (2 * h)
    assert bs.speed(S, K, T, r, sigma) == pytest.approx(fd, rel=1e-2)


def test_charm_matches_fd_of_delta_wrt_time():
    """Charm = ∂Delta/∂t (календарное время). Delta растёт с T, поэтому ∂Delta/∂t = −∂Delta/∂T."""
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["t"]
    up = bs.bs_delta(S, K, T + h, r, sigma)
    down = bs.bs_delta(S, K, T - h, r, sigma)
    fd = -(up - down) / (2 * h)
    assert bs.charm(S, K, T, r, sigma) == pytest.approx(fd, rel=1e-2)


def test_ultima_matches_fd_of_volga_wrt_sigma():
    """Ultima = ∂Vomma/∂σ = ∂Volga/∂σ."""
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    h = H["sigma"]
    up = bs.volga(S, K, T, r, sigma + h)
    down = bs.volga(S, K, T, r, sigma - h)
    fd = (up - down) / (2 * h)
    assert bs.ultima(S, K, T, r, sigma) == pytest.approx(fd, rel=1e-2)


# --------------------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("fn", [bs.bs_call_price, bs.bs_put_price, bs.bs_delta, bs.bs_gamma,
                                 bs.bs_vega, bs.bs_theta, bs.vanna, bs.volga, bs.speed,
                                 bs.charm, bs.ultima])
def test_zero_time_returns_none(fn):
    assert fn(60000.0, 60000.0, 0.0, 0.0, 0.6) is None


@pytest.mark.parametrize("fn", [bs.bs_call_price, bs.bs_put_price, bs.bs_delta, bs.bs_gamma,
                                 bs.bs_vega, bs.bs_theta, bs.vanna, bs.volga, bs.speed,
                                 bs.charm, bs.ultima])
def test_zero_sigma_returns_none(fn):
    assert fn(60000.0, 60000.0, 0.25, 0.0, 0.0) is None


@pytest.mark.parametrize("fn", [bs.bs_call_price, bs.bs_put_price, bs.bs_gamma, bs.bs_vega,
                                 bs.vanna, bs.volga, bs.speed, bs.charm, bs.ultima])
def test_zero_spot_returns_none(fn):
    assert fn(0.0, 60000.0, 0.25, 0.0, 0.6) is None


@pytest.mark.parametrize("fn", [bs.bs_call_price, bs.bs_put_price, bs.bs_delta, bs.bs_gamma,
                                 bs.bs_vega, bs.bs_theta, bs.vanna, bs.volga, bs.speed,
                                 bs.charm, bs.ultima])
def test_negative_time_returns_none(fn):
    assert fn(60000.0, 60000.0, -0.25, 0.0, 0.6) is None


def test_put_call_delta_parity():
    """Delta_put = Delta_call − 1 (put-call parity при q = 0)."""
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    delta_c = bs.bs_delta(S, K, T, r, sigma, option_type="call")
    delta_p = bs.bs_delta(S, K, T, r, sigma, option_type="put")
    assert delta_c - delta_p == pytest.approx(1.0)


def test_put_call_price_parity():
    """C − P = S − K·e^(−rT)."""
    S, K, T, r, sigma = PARAMS["spot"], PARAMS["strike"], PARAMS["time_to_expiry"], PARAMS["risk_free_rate"], PARAMS["sigma"]
    c = bs.bs_call_price(S, K, T, r, sigma)
    p = bs.bs_put_price(S, K, T, r, sigma)
    assert (c - p) == pytest.approx(S - K * math.exp(-r * T))
