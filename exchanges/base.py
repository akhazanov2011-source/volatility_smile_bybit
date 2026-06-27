#!/usr/bin/env python3.14
"""
Базовые типы слоя адаптеров бирж.

``NormalizedOption`` — единая доменная модель опциона, в которую каждый
адаптер (Bybit/Deribit/OKX/Binance) переводит сырой ответ своей биржи.
Доменная логика приложения (app.py) работает только с этой моделью и не
знает о формате конкретной биржи.

``DataSource`` — интерфейс адаптера: ``fetch(coin)`` возвращает spot/forward
и список нормализованных опционов; ``supported_coins`` — список монет,
которые реально торгуются опционами на данной бирже.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class NormalizedOption:
    """Единое представление опциона для всех бирж.

    Поля греков (delta/gamma/theta/vega) равны ``None``, если биржа не
    отдаёт их в bulk-эндпоинте (Deribit/OKX/Binance) — в этом случае
    доменный слой считает их локально по модели Блэка — Шоулза.

    Конвенции (адаптер приводит к ним до возврата):
      * ``mark_iv`` — в долях единицы (0.6 = 60%); Deribit отдаёт процент
        (35.22) и адаптер делит на 100;
      * ``mark_price`` — приведён к USDT для сопоставимости между биржами
        (Deribit котирует премию в BTC и конвертирует по spot; Bybit/Binance
        изначально в USDT; OKX не отдаёт mark_price в opt-summary → None,
        доменный слой досчитает его по модели Блэка — Шоулза);
      * ``underlying_price`` — цена базового актива (перпетуала/форварда)
        для per-option implied rate; ``None`` если биржа не отдаёт;
      * ``open_interest`` — открытый интерес, приведённый к количеству единиц
        базового актива (BTC/ETH/…), а не контрактов: Bybit отдаёт контракты
        (1 контракт = CONTRACT_SIZE монет) и адаптер умножает; Deribit отдаёт
        сразу в монетах (контракт = 1 базовой единицы); OKX отдаёт готовое
        поле ``oiCcy``; Binance не отдаёт OI доступным эндпоинтом → ``None``.
    """

    symbol: str
    base_coin: str            # "BTC", "ETH" — базовый актив
    strike: float
    option_type: str          # "Call" / "Put"
    expiry_dt: datetime
    mark_iv: float | None     # в долях единицы (0.6 = 60%)
    mark_price: float | None
    delta: float | None       # None → считается по BS в доменном слое
    gamma: float | None
    theta: float | None
    vega: float | None
    underlying_price: float | None  # цена базового (перпа/форварда) для implied rate
    open_interest: float | None = None  # OI в единицах базового актива; None если биржа не отдаёт


class DataSource:
    """Абстрактный адаптер биржи. Конкретные адаптеры переопределяют
    ``fetch`` и ``supported_coins``."""

    #: Человекочитаемое название биржи (для UI и логов).
    label: str = "Exchange"

    @property
    def supported_coins(self) -> list[str]:
        raise NotImplementedError

    def fetch(self, coin: str) -> tuple[float, float | None, list[NormalizedOption]]:
        """Загрузить опционный рынок по монете.

        Возвращает кортеж ``(spot_price, forward_price, options)``:
          * spot_price — спот/индекс базового актива;
          * forward_price — цена базового (перпа/форварда) для расчёта
            implied rate, либо ``None``, если биржа не отдаёт её в bulk
            (тогда доменный слой использует r = 0);
          * options — список нормализованных опционов.
        """
        raise NotImplementedError
