from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Path as PathParam, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from conversational_search import create_chat_handler, is_uninitialized_error
from search import (
    build_autocomplete_sql,
    build_search_sql,
    build_similar_sql,
    build_suggest_sql,
    category_counts,
    complete_query,
    query_words,
    to_hit,
)

BASE_DIR = Path(__file__).parent
MANTICORE_HTTP = "http://manticore:9308"
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

app = FastAPI(title="Manticore Search Playground", version="0.2.0")
# manticoresearch.com and its local Hugo server call the APIs directly from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://manticoresearch\.com|http://(localhost|127\.0\.0\.1)(:\d+)?",
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
        # Buddy leaves out "data" when a CALL finds nothing.
        result.setdefault("data", [])
        if result.get("error"):
            detail = str(result["error"])
            if is_uninitialized_error(detail):
                raise HTTPException(status_code=503, detail=INIT_MESSAGE)
            raise HTTPException(status_code=502, detail=detail)
    return results


def timed_sql(query: str) -> tuple[list[dict[str, Any]], int]:
    started = time.perf_counter()
    results = run_sql(query)
    return results, round((time.perf_counter() - started) * 1000)


def suggest_word(word: str) -> str:
    rows = run_sql(build_suggest_sql(word, sql_quote))[0]["data"]
    return rows[0]["suggest"] if rows else word


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/search")
def search(
    q: str = Query(min_length=1, max_length=MAX_QUERY_LENGTH),
    mode: Literal["fulltext", "vector", "hybrid"] = "hybrid",
    category: Optional[Literal["tops", "footwear", "outerwear", "bottoms"]] = None,
    fuzzy: bool = True,
    limit: int = Query(12, ge=1, le=MAX_RESULTS),
) -> dict[str, Any]:
    query = q.strip()
    words = query_words(query)
    if not words:
        raise HTTPException(status_code=400, detail="q must contain at least one word")

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


@app.get("/api/autocomplete")
def autocomplete(q: str = Query(min_length=1, max_length=MAX_QUERY_LENGTH)) -> dict[str, Any]:
    last_word = re.search(r"\w{2,}$", q)
    if not last_word:
        return {"sql": None, "suggestions": []}
    sql = build_autocomplete_sql(last_word.group().lower(), sql_quote)
    rows = run_sql(sql)[0]["data"]
    return {"sql": sql, "suggestions": complete_query(q, [row["query"] for row in rows])}


@app.get("/api/similar/{product_id}")
def similar(product_id: int = PathParam(ge=1)) -> dict[str, Any]:
    sql = build_similar_sql(product_id)
    (hits,), took_ms = timed_sql(sql)
    # Buddy answers KNN-by-id queries with rows keyed by position instead of a list.
    rows = list(hits["data"].values()) if isinstance(hits["data"], dict) else hits["data"]
    return {"sql": sql, "took_ms": took_ms, "hits": [to_hit(row) for row in rows]}


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
