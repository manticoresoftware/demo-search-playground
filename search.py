from __future__ import annotations

import re
from typing import Any, Callable

TABLE = "convapparel_products"
CATEGORIES = ("tops", "footwear", "outerwear", "bottoms")
PRODUCT_COLUMNS = "id, title, description, features, category, image_url"
KNN_CANDIDATES = 100
SIMILAR_LIMIT = 8
AUTOCOMPLETE_LIMIT = 6
# Cosine distance never exceeds 2; hybrid rows found only by keywords report FLT_MAX instead.
MAX_COSINE_DISTANCE = 2.0

Quote = Callable[[str], str]


def query_words(query: str) -> list[str]:
    # Only words reach MATCH(): full-text operators typed by users would be syntax errors.
    return re.findall(r"\w+", query.lower())


def build_search_sql(query: str, mode: str, category: str | None, limit: int, fuzzy: bool, quote: Quote) -> str:
    columns = [PRODUCT_COLUMNS]
    conditions = []
    options = []
    if mode != "vector":
        columns.append("weight() AS text_score")
        conditions.append(f"MATCH({quote(' '.join(query_words(query)))})")
        if fuzzy:
            options.append("fuzzy=1")
    if mode != "fulltext":
        columns.append("knn_dist() AS distance")
        conditions.append(f"knn(embedding_vector, {KNN_CANDIDATES}, {quote(query)})")
    if mode == "hybrid":
        options.append("fusion_method='rrf'")
    if category:
        conditions.append(f"REGEX(category, {quote(category)})")

    sql = f"SELECT {', '.join(columns)} FROM {TABLE} WHERE {' AND '.join(conditions)} LIMIT {limit}"
    if options:
        sql += f" OPTION {', '.join(options)}"
    if mode == "fulltext":
        # KNN candidates make facet counts meaningless, and hybrid search rejects FACET outright.
        sql += " FACET category ORDER BY COUNT(*) DESC"
    return sql


def build_suggest_sql(word: str, quote: Quote) -> str:
    return f"CALL QSUGGEST({quote(word)}, '{TABLE}', 1 AS limit, 2 AS max_edits)"


def build_autocomplete_sql(word: str, quote: Quote) -> str:
    return f"CALL AUTOCOMPLETE({quote(word)}, '{TABLE}', 0 AS fuzziness, 1 AS append)"


def build_similar_sql(product_id: int) -> str:
    return (
        f"SELECT {PRODUCT_COLUMNS}, knn_dist() AS distance FROM {TABLE} "
        f"WHERE knn(embedding_vector, {SIMILAR_LIMIT}, {product_id}) LIMIT {SIMILAR_LIMIT}"
    )


def to_hit(row: dict[str, Any]) -> dict[str, Any]:
    distance = row.get("distance")
    return {
        # Ids exceed JavaScript's safe integer range.
        "id": str(row["id"]),
        "title": row["title"],
        "description": row["description"],
        "features": row["features"],
        "category": row["category"],
        "image_url": row["image_url"],
        # Hybrid rows found only by KNN still get weight 1.
        "matched_words": row.get("text_score", 0) > 1,
        "similarity": round(1 - distance, 3) if distance is not None and distance <= MAX_COSINE_DISTANCE else None,
    }


def category_counts(facet_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Products can belong to several categories, stored as "bottoms, footwear".
    counts = dict.fromkeys(CATEGORIES, 0)
    for row in facet_rows:
        for name in row["category"].split(", "):
            counts[name] += row["count(*)"]
    ranked = sorted(counts.items(), key=lambda item: -item[1])
    return [{"value": name, "count": count} for name, count in ranked if count]


def complete_query(query: str, suggestions: list[str]) -> list[str]:
    last_word = re.search(r"\w+$", query)
    if not last_word:
        return []
    prefix, typed = query[: last_word.start()], last_word.group().lower()
    # Infix matches like "dashiki" for "hik" read as noise in a search box.
    completions = [word for word in suggestions if word.startswith(typed) and word != typed]
    return [prefix + word for word in completions[:AUTOCOMPLETE_LIMIT]]
