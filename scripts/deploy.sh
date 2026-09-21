#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

volume_name="${WARDROBE_VOLUME_NAME:-}"
if [ -z "$volume_name" ] && [ -f .env ]; then
    volume_name="$(sed -n 's/^WARDROBE_VOLUME_NAME=//p' .env | tail -n 1 | tr -d '\r\"')"
fi
volume_name="${volume_name:-style1_wardrobe_data}"
case "$volume_name" in
    *[!A-Za-z0-9_.-]*|'')
        echo "Некорректное имя Docker volume: $volume_name" >&2
        exit 1
        ;;
esac

if ! docker volume inspect "$volume_name" >/dev/null 2>&1; then
    docker volume create "$volume_name" >/dev/null
    echo "Создан постоянный Docker volume: $volume_name"
fi

if [ -n "$(docker compose ps -q bot)" ]; then
    ./scripts/backup.sh
fi

docker compose up -d --build --force-recreate
docker compose ps
