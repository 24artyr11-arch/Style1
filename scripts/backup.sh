#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
container_id="$(docker compose ps -q bot)"
if [ -z "$container_id" ]; then
    echo "Контейнер bot не запущен; резервная копия не создана." >&2
    exit 1
fi

mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
inside="/tmp/wardrobe-${stamp}.sqlite3"
outside="backups/wardrobe-${stamp}.sqlite3"

docker compose exec -T bot python -c \
    'import os,sqlite3,sys; path=os.path.join(os.environ.get("DATA_DIR", "/app/data"), "wardrobe.sqlite3"); assert os.path.isfile(path), f"База не найдена: {path}"; source=sqlite3.connect(path); target=sqlite3.connect(sys.argv[1]); source.backup(target); target.close(); source.close()' \
    "$inside"
docker cp "${container_id}:${inside}" "$outside" >/dev/null
docker compose exec -T bot python -c 'import os,sys; os.unlink(sys.argv[1])' "$inside"

echo "Резервная копия: $outside"
