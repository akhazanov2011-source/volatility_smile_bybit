# Volatility Smile — Bybit

Проект строит интерактивную улыбку волатильности по опционам Bybit и работает как веб-сервис.

Вся Python-логика находится в одном файле:

- `app.py` — Flask-приложение, загрузка данных Bybit, автоматический выбор страйкового окна и построение Plotly-графика для `IV`, `theta` или `theta / mark price`

HTML-интерфейс находится в `templates/index.html`.

Используется публичный Bybit API V5, авторизация не требуется.

## Локальный запуск

```bash
python3.14 -m pip install -r requirements.txt
python3.14 app.py
```

После запуска сервис будет доступен по адресу `http://127.0.0.1:8000`.

Проверка живости:

```bash
curl http://127.0.0.1:8000/healthz
```

## Docker

```bash
docker build -t volatility-smile-bybit .
docker run --rm -p 8000:8000 volatility-smile-bybit
```

После этого сервис откроется на `http://127.0.0.1:8000`.

## Docker Compose

В проект добавлен `docker-compose.yml` с авторестартом и пробросом порта `8000`.
Для совместимости со старым `docker-compose` в Linux используется формат `version: "3.3"`.

```bash
docker compose up -d --build
docker-compose up -d --build
```

Остановка:

```bash
docker compose down
docker-compose down
```

## Что показывает график

- `X` — strike
- `Y` — выбранная метрика: `implied volatility` (`markIv`), `theta` или `theta / mark price (%)`
- Каждая экспирация — отдельная серия
- Для каждой экспирации улыбка строится одной сплошной линией
- В интерфейсе можно переключать режим диаграммы между `IV`, `theta` и `theta / mark price (%)`
- `theta / mark price (%)` считается по формуле `theta / mark price * 100`
- В тултипах доступны выбранная метрика, `IV`, `theta / mark price`, `mark price`, `delta`, `gamma`, `theta`, `vega`
- Диапазон страйков выбирается автоматически: показываются 70% ближайших к `spot` страйков

## Поддерживаемые активы

- `BTC`
- `ETH`
- `SOL`
- `XRP`
- `DOGE`
- `XAUTUSDT`

## Быстрая проверка после изменений

```bash
python3.14 -m py_compile app.py
python3.14 -c "import app"
```
