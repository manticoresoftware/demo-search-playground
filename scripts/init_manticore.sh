#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TABLE_DUMP="${TABLE_DUMP:-dumps/convapparel_products_with_embeddings.sql.xz.part-*}"
IMAGE_DUMP="${IMAGE_DUMP:-dumps/convapparel_image_vectors.sql.xz.part-*}"
TABLE_DUMP_MEMBER="${TABLE_DUMP_MEMBER:-}"
SERVICE_NAME="${MANTICORE_SERVICE:-manticore}"
TABLE_NAME="${TABLE_NAME:-convapparel_products}"

shopt -s nullglob
table_dump_parts=($TABLE_DUMP)
shopt -u nullglob
if (( ${#table_dump_parts[@]} == 0 )); then
  echo "Missing table dump: $TABLE_DUMP" >&2
  exit 1
fi

docker compose up -d --remove-orphans "$SERVICE_NAME"

container_id="$(docker compose ps -q "$SERVICE_NAME")"
if [[ -z "$container_id" ]]; then
  echo "Manticore container is not running" >&2
  exit 1
fi

echo "Waiting for Manticore MySQL protocol..."
until docker exec "$container_id" sh -c 'exec mysql -e "SELECT 1"' >/dev/null 2>&1; do
  sleep 1
done

echo "Dropping existing $TABLE_NAME table and default chat models if present..."
docker exec "$container_id" sh -c "exec mysql -e \"DROP TABLE IF EXISTS $TABLE_NAME\"" >/dev/null
docker exec "$container_id" sh -c 'exec mysql -e "DROP CHAT MODEL IF EXISTS assistant"' >/dev/null 2>&1 || true
docker exec "$container_id" sh -c 'exec mysql -e "DROP CHAT MODEL IF EXISTS assistant_gpt41mini"' >/dev/null 2>&1 || true

dump_sql() {
  case "${table_dump_parts[0]}" in
    *.sql.xz.part-*) cat "${table_dump_parts[@]}" | xz -cd ;;
    *.sql.gz.part-*) cat "${table_dump_parts[@]}" | gzip -cd ;;
    *.part-*) cat "${table_dump_parts[@]}" | tar -xOzf - "$TABLE_DUMP_MEMBER" ;;
    *.tar.gz|*.tgz) tar -xOzf "${table_dump_parts[0]}" "$TABLE_DUMP_MEMBER" ;;
    *.gz) gzip -cd "${table_dump_parts[0]}" ;;
    *) cat "${table_dump_parts[0]}" ;;
  esac
}

# Fuzzy search and autocomplete need infixes; category facets need a string attribute.
patch_schema() {
  sed -e '1,/^);$/{' \
    -e 's/^`category` text,$/`category` string attribute indexed,/' \
    -e "s/^);\$/) min_infix_len='2';/" \
    -e '}'
}

echo "Restoring $TABLE_DUMP..."
dump_sql | patch_schema | docker exec -i "$container_id" sh -c 'exec mysql'

shopt -s nullglob
image_dump_parts=($IMAGE_DUMP)
shopt -u nullglob
if (( ${#image_dump_parts[@]} == 0 )); then
  echo "No $IMAGE_DUMP found, so image search stays off. Create it with scripts/embed_images.py." >&2
else
  echo "Restoring $IMAGE_DUMP..."
  cat "${image_dump_parts[@]}" | xz -cd | docker exec -i "$container_id" sh -c 'exec mysql'
fi

echo "Manticore initialization complete."
