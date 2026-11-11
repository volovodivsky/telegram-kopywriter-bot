#!/usr/bin/env bash
# Запуск і оновлення бота на сервері: ./deploy.sh
# Бере свіжий код з GitHub, перезбирає Docker-образ і перезапускає контейнер.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "❌ Немає файлу .env. Спершу: cp .env.example .env && nano .env"
  exit 1
fi

git pull --ff-only
docker build -t kopywriter-bot .
docker rm -f kopywriter-bot >/dev/null 2>&1 || true
mkdir -p data
docker run -d --name kopywriter-bot --restart=always \
  --env-file .env -v "$(pwd)/data:/app/data" kopywriter-bot

sleep 3
echo "✅ Бот перезапущено. Останні рядки логу:"
docker logs --tail 5 kopywriter-bot
