from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from conversational_search import create_chat_handler, history_turns, is_uninitialized_error
from search import (
    CATEGORIES,
    GEO_LAT,
    GEO_LON,
    GEO_POINTS_MAX,
    GEO_POINTS_RADIUS_MAX_KM,
    GEO_RADIUS_KM,
    GEO_RADIUS_MAX_KM,
    SIMILAR_LIMIT,
    build_autocomplete_sql,
    build_count_sql,
    build_geo_points_sql,
    build_geo_sql,
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
CHAT_DEFAULT_MODEL = "shopping_assistant"
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
- Cite a product right where you describe it: put its reference `[ref:<id>]`, using the context ID (`context[].id`), at the end of that sentence or list item.
- Each sentence or list item carries only the references of the products it describes.
- Never collect references at the end of the answer."""
# CALL CHAT keeps each conversation's messages in this Buddy table; it has no SQL command to read or copy them.
CHAT_HISTORY_TABLE = f"system.chat_history_{CHAT_DEFAULT_MODEL}"
CHAT_HISTORY_COLUMNS = (
    "conversation_uuid, model_name, created_at, role, message, tokens_used, intent, search_query, exclude_query, excluded_ids, ttl"
)
# Buddy reads at most this many messages of a conversation.
CHAT_HISTORY_LIMIT = 100
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
SUPPORTED_SORTS = {"relevance", "title"}
INIT_MESSAGE = "Manticore is not initialized. Run ./scripts/init_manticore.sh, then reload the app."
MAX_QUERY_LENGTH = 200
MAX_RESULTS = 100
MAX_PHOTO_BYTES = 5 * 1024 * 1024
EMBED_UNAVAILABLE = "The image embedding service is not running. Start it with: docker compose up -d embed"

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


def ranked_products(distances: dict[int, float], categories: list[str] | None, limit: int) -> tuple[list[dict[str, Any]], str, int]:
    """Loads the products behind photo matches, closest first: hits, the SQL to show, and its time."""
    ids = list(distances)
    if not ids:
        return [], "", 0
    (products,), took_ms = timed_sql(build_products_sql(sql_list(ids), categories, sql_quote))
    ranked = sorted(products["data"], key=lambda row: distances[row["id"]])
    hits = [to_hit({**row, "distance": distances[row["id"]]}) for row in ranked[:limit]]
    return hits, build_products_sql(sql_list(ids, preview=True), categories, sql_quote), took_ms


def search_by_vector(query: str, vector: list[float], embed_ms: int, category: str | None, limit: int) -> dict[str, Any]:
    (neighbors,), knn_ms = timed_sql(build_image_knn_sql(sql_list(vector)))
    distances = {row["id"]: row["distance"] for row in neighbors["data"]}
    hits, products_sql, products_ms = ranked_products(distances, category_filter(category), limit)
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
    q: str = Query("", max_length=MAX_QUERY_LENGTH),
    mode: Literal["fulltext", "vector", "hybrid", "image", "geo"] = "hybrid",
    category: Optional[str] = None,
    fuzzy: bool = True,
    lat: float = Query(GEO_LAT, ge=-90, le=90),
    lon: float = Query(GEO_LON, ge=-180, le=180),
    radius: float = Query(GEO_RADIUS_KM, ge=0.5, le=GEO_RADIUS_MAX_KM),
    nearest: bool = False,
    limit: int = Query(12, ge=1, le=MAX_RESULTS),
) -> dict[str, Any]:
    query = q.strip()
    words = query_words(query)
    if not words and mode != "geo":
        raise HTTPException(status_code=400, detail="q must contain at least one word")
    selected = category_filter(category)
    if mode == "geo":
        sql = build_geo_sql(lat, lon, radius, selected, limit, sql_quote, nearest)
        # Manticore rejects inline GEODIST in a COUNT query, so the total comes from the search itself.
        (hits, meta), took_ms = timed_sql(f"{sql}; SHOW META LIKE 'total_found'")
        return {
            "query": query,
            "mode": mode,
            "category": category,
            "fuzzy": False,
            "sql": sql,
            "took_ms": took_ms,
            "total": int(meta["data"][0]["Value"]),
            "corrected": None,
            "terms": [],
            "facets": None,
            "hits": [
                {**to_hit(row), "lat": row["lat"], "lon": row["lon"], "distance_km": round(row["distance_km"], 2)}
                for row in hits["data"]
            ],
        }
    if mode == "image":
        started = time.perf_counter()
        vector = embed("/text", {"text": query})
        return search_by_vector(query, vector, elapsed_ms(started), category, limit)

    sql = build_search_sql(query, mode, selected, limit, fuzzy, sql_quote)
    (hits, *facets), took_ms = timed_sql(sql)
    facet_rows = facets[0]["data"] if facets else None
    total = None
    if mode == "fulltext":
        (count,) = run_sql(build_count_sql(query, selected, fuzzy, sql_quote))
        total = count["data"][0]["count(*)"]
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
        "total": total,
        "corrected": " ".join(terms) if terms != words and terms else None,
        "terms": terms,
        "facets": category_counts(facet_rows) if facet_rows is not None else None,
        "hits": [to_hit(row) for row in hits["data"]],
    }


def category_filter(category: str | None) -> list[str] | None:
    """Comma-separated category names become the list the SQL builders quote."""
    if not category:
        return None
    names = category.split(",")
    unknown = [name for name in names if name not in CATEGORIES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown categories: {', '.join(unknown)}")
    return names


@app.get("/api/geo/points")
def geo_points(
    lat: float = Query(GEO_LAT, ge=-90, le=90),
    lon: float = Query(GEO_LON, ge=-180, le=180),
    radius: float = Query(ge=0.5, le=GEO_POINTS_RADIUS_MAX_KM),
    limit: int = Query(ge=1, le=GEO_POINTS_MAX),
) -> dict[str, Any]:
    """Only coordinates, so a map can draw hundreds of products as dots without loading their details."""
    sql = build_geo_points_sql(lat, lon, radius, limit)
    (rows,), took_ms = timed_sql(sql)
    return {"sql": sql, "took_ms": took_ms, "points": [[row["lat"], row["lon"]] for row in rows["data"]]}


@app.post("/api/search/image")
async def search_photo(
    request: Request,
    category: Optional[str] = None,
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


class CopyConversationRequest(BaseModel):
    # The products the page showed with the last answer, in its numbering; otherwise the cited ones.
    sources: Optional[list[int]] = None


@app.post("/api/assistant/conversations/{conversation_uuid}/copy")
def copy_conversation(
    conversation_uuid: str = PathParam(pattern=UUID_PATTERN), req: Optional[CopyConversationRequest] = None
) -> dict[str, Any]:
    """Copies a conversation under a new id, so a visitor can continue an answer that other visitors were shown
    too without their follow-ups reaching each other. Returns the new id and the turns to show."""
    rows = run_sql(
        f"SELECT {CHAT_HISTORY_COLUMNS} FROM {CHAT_HISTORY_TABLE} WHERE conversation_uuid = {sql_quote(conversation_uuid)} "
        f"ORDER BY created_at ASC, id ASC LIMIT {CHAT_HISTORY_LIMIT}"
    )[0]["data"]
    turns = history_turns(rows)
    if not turns:
        raise HTTPException(status_code=404, detail="This conversation is no longer available. Ask your question again.")

    copy_uuid = str(uuid.uuid4())
    values = ", ".join(
        "(" + ", ".join(
            sql_quote(copy_uuid) if column == "conversation_uuid" else str(row[column]) if isinstance(row[column], int) else sql_quote(row[column])
            for column in CHAT_HISTORY_COLUMNS.split(", ")
        ) + ")"
        for row in rows
    )
    run_sql(f"INSERT INTO {CHAT_HISTORY_TABLE} ({CHAT_HISTORY_COLUMNS}) VALUES {values}")

    if req and req.sources:
        turns[-1]["source_ids"] = req.sources
    ids = list({product_id for turn in turns for product_id in turn["source_ids"]})
    products = {}
    if ids:
        (found,) = run_sql(build_products_sql(sql_list(ids), None, sql_quote))
        products = {row["id"]: to_hit(row) for row in found["data"]}
    return {
        "conversation_uuid": copy_uuid,
        "turns": [
            {
                "message": turn["message"],
                "search_query": turn["search_query"],
                "response": turn["response"],
                # Answers can cite ids that are not products, and callers send ids of their own.
                "sources": [products[product_id] for product_id in turn["source_ids"] if product_id in products],
            }
            for turn in turns
        ],
    }


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
