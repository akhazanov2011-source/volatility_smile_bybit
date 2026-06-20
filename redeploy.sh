#!/usr/bin/env bash
#
# redeploy.sh — передеплой приложения volatility_smile_bybit
#   1. Остановить и зачистить docker-compose
#   2. Удалить образ проекта
#   3. git pull
#   4. Запустить docker-compose

set -euo pipefail

# Авто-определение команды: v2 плагин или v1 standalone
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "Ошибка: docker compose не найден" >&2
  exit 1
fi

# Корень проекта = каталог скрипта
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE_NAME="volatility_smile_bybit-web"

echo "==> [1/4] Останавливаю и зачищаю docker-compose..."
"${DC[@]}" down --remove-orphans

echo "==> [2/4] Удаляю образ проекта ($IMAGE_NAME)..."
docker rmi -f "$IMAGE_NAME" 2>/dev/null || echo "    образ не найден, пропускаю"

echo "==> [3/4] git pull..."
git pull --ff-only

echo "==> [4/4] Запускаю docker-compose..."
"${DC[@]}" up -d --build

echo "==> Готово."
