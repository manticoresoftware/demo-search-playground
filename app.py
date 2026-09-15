from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from conversational_search import create_chat_handler, is_uninitialized_error
from search import (
    SIMILAR_LIMIT,
    build_autocomplete_sql,
    build_image_knn_sql,
    build_products_sql,
    build_search_sql,
    build_similar_photo_sql,
    build_similar_sql,
    build_suggest_sql,
    category_counts,
    complete_query,
    query_words,
    sql_list,
    to_hit,
)

BASE_DIR = Path(__file__).parent
MANTICORE_HTTP = "http://manticore:9308"
EMBED_HTTP = "http://embed:8000"
DEFAULT_TABLE = "convapparel_products"
CHAT_DEFAULT_MODEL = "assistant_gpt41mini"
VECTOR_FIELDS = "embedding_vector"
CHAT_MODEL_OPTIONS = {
    "model": "openrouter:openai/gpt-4.1-mini",
    "timeout": 60,
    "retrieval_limit": 5,
    "max_document_length": 0,
}
DEFAULT_CUSTOM_PROMPT = """You are a context-only answer writer for a shopping product search demo.

Answer using only the provided context. Do not use outside knowledge, memory, assumptions, or unsupported facts.

Write concise, helpful shopping recommendations. Prefer product details that are directly supported by the retrieved context.

Citation rules:
- Every recommendation or factual item must end with a citation.
- Never include a reference ID within the item itself.
- At the end of the item, append the reference context ID (`context[].id`) in the format `[ref:<id>]`.
- Do not duplicate the references at the end of the whole answer."""
SUPPORTED_SORTS = {"relevance", "title"}
INIT_MESSAGE = "Manticore is not initialized. Run ./scripts/init_manticore.sh, then reload the app."
MAX_QUERY_LENGTH = 200
MAX_RESULTS = 24
MAX_PHOTO_BYTES = 5 * 1024 * 1024
EMBED_UNAVAILABLE = "The image embedding service is not running. Start it with: docker compose up -d embed"

Category = Literal["tops", "footwear", "outerwear", "bottoms"]

app = FastAPI(title="Manticore Search Playground", version="0.2.0")
# manticoresearch.com, its Cloudflare Pages previews and its local Hugo server call the APIs directly from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://manticoresearch\.com|https://([a-z0-9-]+\.)?site-aqr\.pages\.dev|http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def sql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def manticore_sql(query: str) -> dict[str, Any] | list[Any]:
    payload = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        f"{MANTICORE_HTTP.rstrip('/')}/sql?mode=raw",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            raise RuntimeError(f"Manticore HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(INIT_MESSAGE) from exc


def run_sql(query: str) -> list[dict[str, Any]]:
    try:
        payload = manticore_sql(query)
    except RuntimeError as exc:
        raise HTTPException(status_code=503 if str(exc) == INIT_MESSAGE else 502, detail=str(exc)) from exc
    results = payload if isinstance(payload, list) else [payload]
    for result in results:
        # Buddy leaves out "data" when a CALL finds nothing, and keys KNN-by-id rows by position instead of a list.
        data = result.setdefault("data", [])
        if isinstance(data, dict):
            result["data"] = list(data.values())
        if result.get("error"):
            detail = str(result["error"])
            if is_uninitialized_error(detail):
                raise HTTPException(status_code=503, detail=INIT_MESSAGE)
            raise HTTPException(status_code=502, detail=detail)
    return results


def elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def timed_sql(query: str) -> tuple[list[dict[str, Any]], int]:
    started = time.perf_counter()
    results = run_sql(query)
    return results, elapsed_ms(started)


def suggest_word(word: str) -> str:
    rows = run_sql(build_suggest_sql(word, sql_quote))[0]["data"]
    return rows[0]["suggest"] if rows else word


def embed(path: str, payload: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        f"{EMBED_HTTP}{path}", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise HTTPException(status_code=400, detail=json.load(exc)["detail"]) from exc
        raise HTTPException(status_code=502, detail=f"The image embedding service answered HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=503, detail=EMBED_UNAVAILABLE) from exc


def ranked_products(distances: dict[int, float], category: str | None, limit: int) -> tuple[list[dict[str, Any]], str, int]:
    """Loads the products behind photo matches, closest first: hits, the SQL to show, and its time."""
    ids = list(distances)
    if not ids:
        return [], "", 0
    (products,), took_ms = timed_sql(build_products_sql(sql_list(ids), category, sql_quote))
    ranked = sorted(products["data"], key=lambda row: distances[row["id"]])
    hits = [to_hit({**row, "distance": distances[row["id"]]}) for row in ranked[:limit]]
    return hits, build_products_sql(sql_list(ids, preview=True), category, sql_quote), took_ms


def search_by_vector(query: str, vector: list[float], embed_ms: int, category: str | None, limit: int) -> dict[str, Any]:
    (neighbors,), knn_ms = timed_sql(build_image_knn_sql(sql_list(vector)))
    distances = {row["id"]: row["distance"] for row in neighbors["data"]}
    hits, products_sql, products_ms = ranked_products(distances, category, limit)
    shown_vector = sql_list([round(value, 4) for value in vector], preview=True)
    return {
        "query": query,
        "mode": "image",
        "category": category,
        "fuzzy": False,
        "sql": ";\n".join(filter(None, [build_image_knn_sql(shown_vector), products_sql])),
        "took_ms": knn_ms + products_ms,
        "embed_ms": embed_ms,
        "total": None,
        "corrected": None,
        "terms": [],
        "facets": None,
        "hits": hits,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/search")
def search(
    q: str = Query(min_length=1, max_length=MAX_QUERY_LENGTH),
    mode: Literal["fulltext", "vector", "hybrid", "image"] = "hybrid",
    category: Optional[Category] = None,
    fuzzy: bool = True,
    limit: int = Query(12, ge=1, le=MAX_RESULTS),
) -> dict[str, Any]:
    query = q.strip()
    words = query_words(query)
    if not words:
        raise HTTPException(status_code=400, detail="q must contain at least one word")
    if mode == "image":
        started = time.perf_counter()
        vector = embed("/text", {"text": query})
        return search_by_vector(query, vector, elapsed_ms(started), category, limit)

    sql = build_search_sql(query, mode, category, limit, fuzzy, sql_quote)
    (hits, *facets), took_ms = timed_sql(sql)
    facet_rows = facets[0]["data"] if facets else None
    terms = [] if mode == "vector" else words
    if fuzzy and terms:
        terms = [suggest_word(word) for word in words]

    return {
        "query": query,
        "mode": mode,
        "category": category,
        "fuzzy": fuzzy,
        "sql": sql,
        "took_ms": took_ms,
        "total": sum(row["count(*)"] for row in facet_rows) if facet_rows is not None else None,
        "corrected": " ".join(terms) if terms != words and terms else None,
        "terms": terms,
        "facets": category_counts(facet_rows) if facet_rows is not None else None,
        "hits": [to_hit(row) for row in hits["data"]],
    }


@app.post("/api/search/image")
async def search_photo(
    request: Request,
    category: Optional[Category] = None,
    limit: int = Query(12, ge=1, le=MAX_RESULTS),
) -> dict[str, Any]:
    if not request.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(status_code=415, detail="Send the photo as the request body with an image/* Content-Type")
    photo = bytearray()
    async for chunk in request.stream():
        photo.extend(chunk)
        if len(photo) > MAX_PHOTO_BYTES:
            raise HTTPException(status_code=413, detail="Photos can be up to 5 MB")
    if not photo:
        raise HTTPException(status_code=400, detail="The request body has no photo")

    started = time.perf_counter()
    (vector,) = await run_in_threadpool(embed, "/images", {"images": [base64.b64encode(photo).decode("ascii")]})
    return await run_in_threadpool(search_by_vector, "", vector, elapsed_ms(started), category, limit)


@app.get("/api/autocomplete")
def autocomplete(q: str = Query(min_length=1, max_length=MAX_QUERY_LENGTH)) -> dict[str, Any]:
    last_word = re.search(r"\w{2,}$", q)
    if not last_word:
        return {"sql": None, "suggestions": []}
    sql = build_autocomplete_sql(last_word.group().lower(), sql_quote)
    rows = run_sql(sql)[0]["data"]
    return {"sql": sql, "suggestions": complete_query(q, [row["query"] for row in rows])}


@app.get("/api/similar/{product_id}")
def similar(product_id: int = PathParam(ge=1), by: Literal["description", "photo"] = "description") -> dict[str, Any]:
    if by == "photo":
        knn_sql = build_similar_photo_sql(product_id)
        (neighbors,), knn_ms = timed_sql(knn_sql)
        distances = {row["id"]: row["distance"] for row in neighbors["data"]}
        hits, products_sql, products_ms = ranked_products(distances, None, SIMILAR_LIMIT)
        return {"sql": ";\n".join(filter(None, [knn_sql, products_sql])), "took_ms": knn_ms + products_ms, "hits": hits}

    sql = build_similar_sql(product_id)
    (hits,), took_ms = timed_sql(sql)
    return {"sql": sql, "took_ms": took_ms, "hits": [to_hit(row) for row in hits["data"]]}


app.post("/api/assistant/chat")(
    create_chat_handler(
        manticore_sql=manticore_sql,
        sql_quote=sql_quote,
        default_table=DEFAULT_TABLE,
        default_model=CHAT_DEFAULT_MODEL,
        vector_fields=VECTOR_FIELDS,
        init_message=INIT_MESSAGE,
        default_prompt=DEFAULT_CUSTOM_PROMPT,
        chat_model_options=CHAT_MODEL_OPTIONS,
    )
)
