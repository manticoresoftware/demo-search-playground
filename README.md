# Manticore Search Playground

Search 82,524 clothing products with full-text, vector, hybrid and AI search in Manticore, and see the exact SQL behind every result.
The same app serves the JSON APIs behind the search demo on the manticoresearch.com homepage.

The project entry point is `docker-compose.yml`. It runs the app and Manticore on the same Compose network, which matters because the app connects to Manticore through the Compose service name `manticore`.

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

The repository includes a split SQL dump with product rows and precomputed `embedding_vector` values:

- `dumps/convapparel_products_with_embeddings.sql.xz.part-*`

Restoring this dump is much faster than downloading ConvApparel and waiting for Manticore to calculate 82k embeddings during setup.

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

Initialize Manticore from the checked-in precomputed dump:

```bash
./scripts/init_manticore.sh
```

Start the API:

```bash
docker compose up --build app
```

Open: `http://127.0.0.1:8000`

## Environment

Set in `.env`:

```env
OPENROUTER_API_KEY=
```

The key is passed into the `manticore` service and used when the app creates Manticore chat models on demand. Only Ask AI needs it; the search endpoints work without it.

## API

Every search endpoint returns the SQL it ran, so the playground and the website can show it next to the results.

- `GET /api/search`
  - Query: `q` (required, up to 200 characters), `mode` (`fulltext`, `vector` or `hybrid`, default `hybrid`), optional `category` (`tops`, `bottoms`, `footwear` or `outerwear`), `fuzzy` (default `true`), `limit` (1 to 24, default 12)
  - `fulltext` runs `MATCH()` with `OPTION fuzzy=1` and `FACET category`, `vector` runs `knn()` with the query text, and `hybrid` runs both with `OPTION fusion_method='rrf'`.
  - Response: `sql`, `took_ms` and `hits`. Full-text also returns `total` and `facets`. When typo tolerance changed a word, `corrected` holds the query found with `CALL QSUGGEST`, and `terms` holds the words to highlight.
  - Each hit has `matched_words` and `similarity`, which tell whether keywords, meaning or both found it.
- `GET /api/autocomplete?q=` completes the last word with `CALL AUTOCOMPLETE`.
- `GET /api/similar/{id}` returns the products closest to a product, using KNN by document id.
- `POST /api/assistant/chat`
  - Body: `message`, optional `conversation_uuid`, optional `custom_prompt`
  - Response includes Manticore `response_with_refs` when available, plus `sources`; the UI turns `[ref:<id>]` markers into numbered links to the source products.

When `custom_prompt` is omitted or blank, the app creates/reuses the default `assistant_gpt41mini` chat model with the built-in prompt. When `custom_prompt` is non-empty, the app calculates a SHA-256 hash prefix for that prompt, creates/reuses `assistant_gpt41mini_<hash>`, and calls that model so repeated prompt variants do not recreate duplicate chat models.

Examples:

```bash
curl "http://127.0.0.1:8000/api/search?q=lether%20jaket&mode=fulltext&limit=3"

curl -X POST "http://127.0.0.1:8000/api/assistant/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"I need waterproof black running shoes for jogging"}'
```

Browsers can call the API from `https://manticoresearch.com` and from `localhost` or `127.0.0.1` on any port.

## Manticore Initialization

The Quick Start runs `./scripts/init_manticore.sh` once before starting the app. That script starts the `manticore` service, removes old orphan services, waits for the MySQL protocol, drops any existing `convapparel_products` table and default `assistant_gpt41mini` chat model, and restores `dumps/convapparel_products_with_embeddings.sql.xz.part-*`.

While restoring, the script adds `min_infix_len='2'` to the table, which fuzzy search, `CALL QSUGGEST` and `CALL AUTOCOMPLETE` need, and makes `category` a string attribute so it can be filtered and faceted.

Run the initialization script again when you need to reset the `convapparel_products` table. Chat models are created by the FastAPI app on demand before `CALL CHAT`, which also lets the UI send a custom prompt per request.

## Tests

```bash
docker compose run --rm --no-deps app python test_search.py
```

The script checks SQL building, escaping of user input, and response shaping.

## Website Integration

The homepage demo in `manticoresoftware/site` reads the API address from the Hugo `playground_api` param. Its `config/development` points to `http://127.0.0.1:8000`, so `hugo server` uses this app when it runs locally. Hugo fetches the first results at build time, and the browser calls the same endpoints for new queries.

## Local Python Development

The Compose setup is the supported way to run the full app because `app.py` connects to Manticore at `http://manticore:9308`. If you run Uvicorn directly on the host, that service name will not resolve unless you provide an equivalent local hostname or adjust the code/configuration for local development.

For app-only iteration after handling Manticore connectivity:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```
