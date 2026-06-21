#!/usr/bin/env python3.14
"""
Адаптер OKX.

OKX отдаёт опционные данные двумя публичными эндпоинтами (без авторизации),
которые мёрджятся по ``instId``:
  1. ``GET /api/v5/public/opt-summary?uly=BTC-USD`` — markVol (IV), fwdPx
     (forward), realVol (всегда 0, не используется);
  2. ``GET /api/v5/public/instruments?instType=OPTION&uly=BTC-USD`` — stk
     (страйк), expTime (ms epoch), optType (C/P).

Spot/index: ``GET /api/v5/market/index-tickers?instId=BTC-USD`` → ``idxPx``.

Нюансы OKX (см. проверку API):
  * ``markVol`` — decimal fraction (0.448 = 44.8%); нормализация НЕ нужна;
  * значения приходят строками → float();
  * ``bidVol``/``askVol`` могут быть пустой строкой ``""``;
  * греков в bulk НЕТ → delta/gamma/theta/vega = None (считает BS);
  * forward есть (fwdPx per-record) → underlying_price = fwdPx (для implied rate);
  * ``instType=OPTION`` (полное слово), не ``OPT``.

instId: ``BTC-USD_UM-260626-61000-C`` (сегмент семейства ``_UM`` обязателен).
"""

import threading
import time
from datetime import datetime, timezone

import requests

from exchanges.base import DataSource, NormalizedOption
import net

OKX_BASE_URL = "https://www.okx.com"

#: UI-монета → uly (underlying) OKX.
COIN_ALIASES = {
    "BTC": {"uly": "BTC-USD"},
    "ETH": {"uly": "ETH-USD"},
}


def _to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms_to_datetime(ms_str):
    """ms-epoch (строка/число) → datetime (UTC). ``None`` при ошибке."""
    if ms_str in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(ms_str) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


# TTL-кеш (summary, instruments) по uly. Обновляются вместе, т.к. оба нужны
# для построения одной модели.
_market_cache = {}
_market_cache_lock = threading.Lock()
_MARKET_CACHE_TTL = 60


class OkxAdapter(DataSource):
    label = "OKX"

    @property
    def supported_coins(self) -> list[str]:
        return list(COIN_ALIASES.keys())

    def _get_market(self, uly):
        """Возвращает кеш (summary_by_id, instruments_by_id)."""
        now_ts = time.time()
        with _market_cache_lock:
            cached = _market_cache.get(uly)
            if cached and (now_ts - cached["ts"]) < _MARKET_CACHE_TTL:
                return cached["summary"], cached["instruments"]

        summary = self._fetch_summary(uly)
        instruments = self._fetch_instruments(uly)

        with _market_cache_lock:
            _market_cache[uly] = {
                "ts": now_ts,
                "summary": summary,
                "instruments": instruments,
            }
        return summary, instruments

    def _fetch_summary(self, uly):
        url = f"{OKX_BASE_URL}/api/v5/public/opt-summary"
        params = {"uly": uly}
        try:
            payload = net.get_json(url, params=params)
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"Не удалось загрузить opt-summary OKX для {uly}.") from exc
        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError("OKX opt-summary вернул неожиданный формат.")
        return {rec.get("instId"): rec for rec in data if rec.get("instId")}

    def _fetch_instruments(self, uly):
        url = f"{OKX_BASE_URL}/api/v5/public/instruments"
        params = {"instType": "OPTION", "uly": uly}
        try:
            payload = net.get_json(url, params=params)
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"Не удалось загрузить instruments OKX для {uly}.") from exc
        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError("OKX instruments вернул неожиданный формат.")
        return {rec.get("instId"): rec for rec in data if rec.get("instId")}

    def _get_spot(self, uly):
        url = f"{OKX_BASE_URL}/api/v5/market/index-tickers"
        params = {"instId": uly}
        try:
            payload = net.get_json(url, params=params)
        except (requests.RequestException, ValueError):
            return None
        data = payload.get("data") or []
        if not data:
            return None
        return _to_float(data[0].get("idxPx"))

    def fetch(self, coin):
        config = COIN_ALIASES.get(coin)
        if config is None:
            raise ValueError(f"OKX не поддерживает монету {coin}")

        uly = config["uly"]
        summary_by_id, instruments_by_id = self._get_market(uly)

        options: list[NormalizedOption] = []
        for inst_id, summ in summary_by_id.items():
            instr = instruments_by_id.get(inst_id)
            if instr is None:
                # Нет описания инструмента → нельзя определить страйк/экспирацию.
                continue

            expiry_dt = _ms_to_datetime(instr.get("expTime"))
            strike = _to_float(instr.get("stk"))
            opt_type_raw = instr.get("optType")
            if expiry_dt is None or strike is None or opt_type_raw not in ("C", "P"):
                continue
            option_type = "Call" if opt_type_raw == "C" else "Put"
            base_coin = coin

            # markVol — decimal fraction (0.448); остаётся как есть.
            mark_iv = _to_float(summ.get("markVol"))
            # fwdPx — forward per-maturity для implied rate.
            forward_price = _to_float(summ.get("fwdPx"))

            options.append(
                NormalizedOption(
                    symbol=inst_id,
                    base_coin=base_coin,
                    strike=strike,
                    option_type=option_type,
                    expiry_dt=expiry_dt,
                    mark_iv=mark_iv,
                    mark_price=None,  # в opt-summary markPx нет
                    delta=None,       # греков в bulk нет → BS
                    gamma=None,
                    theta=None,
                    vega=None,
                    underlying_price=forward_price,
                )
            )

        spot_price = self._get_spot(uly)
        return spot_price, None, options
