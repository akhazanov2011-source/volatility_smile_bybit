#!/usr/bin/env python3.14
"""
Реестр поддерживаемых бирж.

``EXCHANGES`` — упорядоченный словарь ``{key: ExchangeConfig}``. Доменный
слой (app.py) и UI работают с ключами бирж (``bybit``, ``deribit``, …),
получая через реестр экземпляр адаптера и список поддерживаемых монет.

Добавить новую биржу = создать модуль-адаптер (наследник ``DataSource``) и
внести запись в ``EXCHANGES``.
"""

from dataclasses import dataclass

from exchanges.base import DataSource, NormalizedOption
from exchanges.bybit import BybitAdapter
from exchanges.deribit import DeribitAdapter
from exchanges.okx import OkxAdapter
from exchanges.binance import BinanceAdapter


@dataclass(frozen=True)
class ExchangeConfig:
    key: str           # URL/внутренний ключ: "bybit"
    label: str         # Человекочитаемое название: "Bybit"
    adapter: DataSource


# Экземпляры адаптеров — без состояния (stateless); кеши живут на уровне
# модулей адаптеров. Безопасно переиспользовать один экземпляр на процесс.
_ADAPTERS: dict[str, DataSource] = {
    "bybit": BybitAdapter(),
    "deribit": DeribitAdapter(),
    "okx": OkxAdapter(),
    "binance": BinanceAdapter(),
}

#: Реестр бирж. Порядок определяет отображение в селекторе UI.
EXCHANGES: dict[str, ExchangeConfig] = {
    key: ExchangeConfig(key=key, label=adapter.label, adapter=adapter)
    for key, adapter in _ADAPTERS.items()
}

DEFAULT_EXCHANGE = "bybit"


def get_exchange(key: str) -> ExchangeConfig:
    return EXCHANGES[key]


def supported_coins(key: str) -> list[str]:
    """Список монет, торгуемых опционами на конкретной бирже."""
    return EXCHANGES[key].adapter.supported_coins


def all_exchange_coin_pairs() -> list[tuple[str, str]]:
    """Декартово произведение (exchange, coin) по всем биржам — для фонового
    прогрева кеша."""
    return [
        (ex_key, coin)
        for ex_key, cfg in EXCHANGES.items()
        for coin in cfg.adapter.supported_coins
    ]


__all__ = [
    "DataSource",
    "NormalizedOption",
    "ExchangeConfig",
    "EXCHANGES",
    "DEFAULT_EXCHANGE",
    "get_exchange",
    "supported_coins",
    "all_exchange_coin_pairs",
]
