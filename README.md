# Manticore Search Playground

Search 82,524 clothing products with full-text, vector, hybrid, image and AI search in Manticore, and see the exact SQL behind every result.
The same app serves the JSON APIs behind the search demo on the manticoresearch.com homepage.

The project entry point is `docker-compose.yml`. It runs Manticore, a Fashion CLIP embedding service and the app on the same Compose network, which matters because the app connects to them through the Compose service names `manticore` and `embed`.

## Dataset

This project uses the `google/ConvApparel` dataset from Hugging Face: conversations between shoppers and an apparel recommendation assistant. The dataset contains product recommendations for footwear, outerwear, tops, and bottoms.

The preparation script builds a deduplicated product corpus from recommendation items:

- `item_id`
- `title`
- `description`
- `features`
- `image_url`
- inferred apparel category

ConvApparel v1 currently yields 82,524 unique product IDs from 175,751 recommendation occurrences.

The repository includes split SQL dumps, so setup doesn't have to compute any embeddings:

- `dumps/convapparel_products_with_embeddings.sql.xz.part-*`: product rows with text embeddings in `embedding_vector`
- `dumps/convapparel_image_vectors.sql.xz.part-*`: Fashion CLIP vectors of the product photos in the `convapparel_product_images` table

Raw downloaded data and locally generated non-embedding SQL are intentionally ignored by git:

- `data/raw/ConvApparel.zip`
- `dumps/convapparel_products.sql.gz`

## Quick Start

Create the environment file and set your OpenRouter key:

```bash
cp .env.example .env
```

Edit `.env`:

```env
OPENROUTER_API_KEY=
```

Initialize Manticore from the checked-in dumps:

```bash
./scripts/init_manticore.sh
```

Start the app and the embedding service:

```bash
docker compose up --build app embed
```

Open: `http://127.0.0.1:8000`

The embedding service downloads the Fashion CLIP model (about 600 MB) on first start. Everything except image search works while it loads.

## Environment

Set in `.env`:

```env
OPENROUTER_API_KEY=
# Optional: host port for the app, which listens on 127.0.0.1 only. Defaults to 8000.
APP_PORT=
```

The key is passed into the `manticore` service and used when the app creates Manticore chat models on demand. Only Ask AI needs it.

## API

Every search endpoint returns the SQL it ran (`sql`) and the same query as bodies for Manticore's JSON `/search` endpoint (`requests`), so the playground and the website can show them next to the results.

- `GET /api/search`
  - Query: `q` (up to 200 characters), `mode` (`fulltext`, `vector`, `hybrid`, `image` or `geo`, default `hybrid`), optional `category` (comma-separated, e.g. `tops,footwear`), `fuzzy` (default `true`), `limit` (1 to 100, default 12). `geo` mode ignores `q` and takes `lat`, `lon` (default New York) and `radius` in kilometers (0.5 to 25, default 10) instead, and `total` shows how many products the radius really matches.
  - fulltext runs `MATCH()` with `OPTION fuzzy=1` and `FACET categories`, `vector` runs `knn()` with the query text, `hybrid` runs both with `OPTION fusion_method='rrf'`, `image` turns the text into a Fashion CLIP vector and searches the product photos, and `geo` filters with `GEODIST()` on the `lat`/`lon` attributes baked into the product dump. Geo orders by `id` — coordinates are a hash of id, so the page shows a deterministic sample spread across the whole circle instead of a cluster of the nearest items; the UI sorts hits by distance for the list. `nearest=1` orders by distance instead, for short lists without a map.
  - Response: `sql`, `requests`, `took_ms` and `hits`. Full-text and geo also return `total`; full-text adds `facets`. When typo tolerance changed a word, `corrected` holds the query found with `CALL QSUGGEST`, and `terms` holds the words to highlight. Image search adds `embed_ms`, the time spent turning the query into a vector; geo hits add `lat`, `lon` and `distance_km`.
- `GET /api/geo/points?lat=&lon=&radius=&limit=` returns only the coordinates of products within `radius` km (up to 100) as `points` (`[[lat, lon], …]`, up to 1,000, sampled in id order), for drawing products as map dots.
- `POST /api/search/image` searches by photo. Send the photo (up to 5 MB) as the request body with an `image/*` `Content-Type`; `category` and `limit` work as above.
- `GET /api/autocomplete?q=` completes the last word with `CALL AUTOCOMPLETE`.
- `GET /api/similar/{id}` returns the products closest to a product, using KNN by document id. Add `?by=photo` to compare product photos instead of descriptions.
- `POST /api/assistant/chat`
  - Body: `message`, optional `conversation_uuid`, optional `custom_prompt`
  - Response includes Manticore `response_with_refs` when available, plus `sources`; the UI turns `[ref:<id>]` markers into numbered links to the source products.
- `POST /api/assistant/conversations/{conversation_uuid}/copy` copies a conversation under a new id and returns `conversation_uuid` and its `turns` (`message`, `search_query`, `response`, `sources`). Body: optional `sources`, the product ids shown with the last answer; otherwise each turn gets the products its answer cites. `CALL CHAT` has no command to read or copy history, so this reads and writes Buddy's `system.chat_history_shopping_assistant` table directly.

When `custom_prompt` is omitted or blank, the app creates/reuses the default `shopping_assistant` chat model with the built-in prompt. When `custom_prompt` is non-empty, the app calculates a SHA-256 hash prefix for that prompt, creates/reuses `shopping_assistant_<hash>`, and calls that model so repeated prompt variants do not recreate duplicate chat models.

manticoresearch.com links its Ask AI answer to `/?mode=chat&conversation=<uuid>&sources=<ids>`, and its follow-up box adds `&q=<question>`. Every homepage visitor is shown the same answer, so the playground continues a copy of that conversation and then asks `q`, if given, as the next message; follow-ups keep its context without reaching other visitors. If the copy fails, `q` stays in the box unsent.

Examples:

```bash
curl "http://127.0.0.1:8000/api/search?q=lether%20jaket&mode=fulltext&limit=3"

curl -X POST "http://127.0.0.1:8000/api/search/image?limit=3" \
  -H "Content-Type: image/jpeg" \
  --data-binary @photo.jpg

curl -X POST "http://127.0.0.1:8000/api/assistant/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"I need waterproof black running shoes for jogging"}'
```

Browsers can call the API from `https://manticoresearch.com` and from `localhost` or `127.0.0.1` on any port.

## Manticore Initialization

The Quick Start runs `./scripts/init_manticore.sh` once before starting the app. That script starts the `manticore` service, removes old orphan services, waits for the MySQL protocol, drops any existing `convapparel_products` table and default `shopping_assistant` chat model, restores the product dump, and then restores the image vector dump.

While restoring, the script adds `min_infix_len='2'` to the product table, which fuzzy search, `CALL QSUGGEST` and `CALL AUTOCOMPLETE` need, and makes `category` a string attribute. A product can have several categories, stored as `bottoms, footwear`, so the script also fills a `categories` JSON array; search filters it with `categories IN ('footwear', 'tops')` and `FACET categories` counts each category.

Photo vectors live in their own `convapparel_product_images` table: Manticore can't `UPDATE` a vector that has a KNN index, so image search finds the closest photos there and then loads those products by id.

Run the initialization script again when you need to reset the tables. Chat models are created by the FastAPI app on demand before `CALL CHAT`, which also lets the UI send a custom prompt per request.

## Rebuilding Image Vectors

`scripts/embed_images.py` downloads every product photo, turns it into a Fashion CLIP vector with the `embed` service, and writes `dumps/convapparel_image_vectors.sql.xz.part-*`. Photos that many products share, like Amazon's "No image available" placeholder, are left out.

```bash
docker compose up -d manticore embed
docker compose run --rm -v "$PWD:/app" app python scripts/embed_images.py
```

It takes about 40 minutes on a 10-core machine. Progress is saved in `dumps/image_vectors.progress.jsonl`, so a stopped run resumes where it left off. Run `./scripts/init_manticore.sh` afterwards to load the new vectors.

## Tests

```bash
docker compose run --rm --no-deps app python test_search.py
```

The script checks SQL building, escaping of user input, and response shaping.

## Deployment

The app runs the same way on a server; put a reverse proxy with TLS in front of it.

1. Clone the repository, create `.env` with `OPENROUTER_API_KEY` and `APP_PORT`, and run `./scripts/init_manticore.sh`.
2. On a host shared with other services, add a `docker-compose.override.yml` (git ignores it) that restarts the services, caps their resources, and makes the kernel stop the playground first if memory runs out:

   ```yaml
   services:
     manticore:
       restart: unless-stopped
       cpus: 1
       mem_limit: 1200m
       oom_score_adj: 1000
     embed:
       restart: unless-stopped
       cpus: 1
       mem_limit: 900m
       oom_score_adj: 1000
     app:
       restart: unless-stopped
       cpus: 0.5
       mem_limit: 256m
       oom_score_adj: 1000
   ```

   Manticore needs about 0.6 GB with both tables, and the embedding service about 0.7 GB.
3. If the host firewall only trusts known Docker bridges, give the network a fixed bridge name in the same override and trust that interface, or the app can't reach Manticore:

   ```yaml
   networks:
     default:
       driver_opts:
         com.docker.network.bridge.name: playground-br0
   ```

4. Start everything with `docker compose up -d --build`.
5. Proxy your domain to `127.0.0.1:$APP_PORT`. Allow request bodies of at least 6 MB for photo uploads and a read timeout of about 90 seconds for Ask AI. With nginx:

   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8090;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       client_max_body_size 6m;
       proxy_read_timeout 90s;
   }
   ```

To update, pull the new code and run `docker compose up -d --build`. Run `./scripts/init_manticore.sh` again only when the dumps change.

## Website Integration

The homepage demo in `manticoresoftware/site` reads the API address from the Hugo `playground_api` param, which points to the production playground. To test the site against a local copy, run `HUGOxPARAMSxPLAYGROUND_API=http://127.0.0.1:8000 hugo server`. Hugo fetches the first results at build time, and the browser calls the same endpoints for new queries.

The site's Geo section embeds the playground in a frame with `/?mode=geo&embed=1`, which drops the header, footer and inspector and locks out wheel-zoom so it never hijacks the page's scroll.

## Local Python Development

The Compose setup is the supported way to run the full app because `app.py` connects to Manticore at `http://manticore:9308` and to the embedding service at `http://embed:8000`. If you run Uvicorn directly on the host, those service names will not resolve unless you provide equivalent local hostnames or adjust the code/configuration for local development.

For app-only iteration after handling Manticore connectivity:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```
