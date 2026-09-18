from __future__ import annotations

import re
from typing import Any, Callable

TABLE = "convapparel_products"
IMAGE_TABLE = "convapparel_product_images"
CATEGORIES = ("tops", "footwear", "outerwear", "bottoms")
PRODUCT_COLUMNS = "id, title, description, features, category, image_url"
GEO_COLUMNS = f"{PRODUCT_COLUMNS}, lat, lon"
# Geo search pins the shopper in New York; the dump scatters products roughly +/-150 km around it.
GEO_LAT, GEO_LON = 40.7128, -74.0060
GEO_RADIUS_KM = 10.0
GEO_RADIUS_MAX_KM = 25.0
# Map dots cover the whole visible map, which reaches past the widest search radius.
GEO_POINTS_RADIUS_MAX_KM = 100.0
# Manticore returns at most max_matches (1000 by default) rows.
GEO_POINTS_MAX = 1000
KNN_CANDIDATES = 100
SIMILAR_LIMIT = 8
AUTOCOMPLETE_LIMIT = 6
# The SQL shown to people keeps a few of the 512 vector numbers and 100 ids; the executed query has all of them.
PREVIEW_VALUES = 3
# Cosine distance never exceeds 2; hybrid rows found only by keywords report FLT_MAX instead.
MAX_COSINE_DISTANCE = 2.0

Quote = Callable[[str], str]


def query_words(query: str) -> list[str]:
    # Only words reach MATCH(): full-text operators typed by users would be syntax errors.
    return re.findall(r"\w+", query.lower())


def category_condition(categories: list[str], quote: Quote) -> str:
    return f"categories IN ({', '.join(map(quote, categories))})"


def build_search_sql(query: str, mode: str, categories: list[str] | None, limit: int, fuzzy: bool, quote: Quote) -> str:
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
    if categories:
        conditions.append(category_condition(categories, quote))

    sql = f"SELECT {', '.join(columns)} FROM {TABLE} WHERE {' AND '.join(conditions)} LIMIT {limit}"
    if options:
        sql += f" OPTION {', '.join(options)}"
    if mode == "fulltext":
        # KNN candidates make facet counts meaningless, and hybrid search rejects FACET outright.
        sql += " FACET categories ORDER BY COUNT(*) DESC"
    return sql


def build_count_sql(query: str, categories: list[str] | None, fuzzy: bool, quote: Quote) -> str:
    # Facet counts overlap for products in several categories, and fuzzy search runs through Buddy,
    # which answers neither SHOW META nor a second statement, so the total needs its own query.
    conditions = [f"MATCH({quote(' '.join(query_words(query)))})"]
    if categories:
        conditions.append(category_condition(categories, quote))
    sql = f"SELECT COUNT(*) FROM {TABLE} WHERE {' AND '.join(conditions)}"
    return f"{sql} OPTION fuzzy=1" if fuzzy else sql


def build_geo_sql(
    lat: float, lon: float, radius_km: float, categories: list[str] | None, limit: int, quote: Quote, nearest: bool = False
) -> str:
    geodist = f"GEODIST({lat:.6f}, {lon:.6f}, lat, lon, {{in=degrees, out=km}})"
    conditions = [f"distance_km <= {radius_km:g}"]
    if categories:
        conditions.append(category_condition(categories, quote))
    # Order by id, not distance: coordinates are a hash of id, so id order is a deterministic sample that
    # spreads across the whole circle. Nearest-first would pack every result into the inner core,
    # making the map blob and the list look identical at every radius. The client sorts by distance.
    # A short list with no map, like the homepage's top three, asks for the nearest products instead.
    order = "distance_km" if nearest else "id"
    return (
        f"SELECT {GEO_COLUMNS}, {geodist} AS distance_km FROM {TABLE} "
        f"WHERE {' AND '.join(conditions)} ORDER BY {order} ASC LIMIT {limit}"
    )


def build_geo_points_sql(lat: float, lon: float, radius_km: float, limit: int) -> str:
    # Id order samples evenly across the circle, as in build_geo_sql.
    return (
        f"SELECT lat, lon, GEODIST({lat:.6f}, {lon:.6f}, lat, lon, {{in=degrees, out=km}}) AS distance_km FROM {TABLE} "
        f"WHERE distance_km <= {radius_km:g} ORDER BY id ASC LIMIT {limit}"
    )


def build_suggest_sql(word: str, quote: Quote) -> str:
    return f"CALL QSUGGEST({quote(word)}, '{TABLE}', 1 AS limit, 2 AS max_edits)"


def build_autocomplete_sql(word: str, quote: Quote) -> str:
    # By default the last word also expands as an infix, and matches like "print" for "int" crowd
    # real completions out of the ten rows AUTOCOMPLETE returns.
    return f"CALL AUTOCOMPLETE({quote(word)}, '{TABLE}', 0 AS fuzziness, 1 AS append, 0 AS prepend)"


def build_similar_sql(product_id: int) -> str:
    return (
        f"SELECT {PRODUCT_COLUMNS}, knn_dist() AS distance FROM {TABLE} "
        f"WHERE knn(embedding_vector, {SIMILAR_LIMIT}, {product_id}) LIMIT {SIMILAR_LIMIT}"
    )


def sql_list(values: list[Any], preview: bool = False) -> str:
    shown = values[:PREVIEW_VALUES] if preview else values
    more = ", …" if preview and len(values) > PREVIEW_VALUES else ""
    return f"({', '.join(map(str, shown))}{more})"


def build_image_knn_sql(vector_sql: str) -> str:
    return (
        f"SELECT id, knn_dist() AS distance FROM {IMAGE_TABLE} "
        f"WHERE knn(image_vector, {KNN_CANDIDATES}, {vector_sql}) LIMIT {KNN_CANDIDATES}"
    )


def build_similar_photo_sql(product_id: int) -> str:
    return (
        f"SELECT id, knn_dist() AS distance FROM {IMAGE_TABLE} "
        f"WHERE knn(image_vector, {SIMILAR_LIMIT}, {product_id}) LIMIT {SIMILAR_LIMIT}"
    )


def build_products_sql(ids_sql: str, categories: list[str] | None, quote: Quote) -> str:
    conditions = [f"id IN {ids_sql}"]
    if categories:
        conditions.append(category_condition(categories, quote))
    return f"SELECT {PRODUCT_COLUMNS} FROM {TABLE} WHERE {' AND '.join(conditions)} LIMIT {KNN_CANDIDATES}"


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
    return [{"value": row["categories"], "count": row["count(*)"]} for row in facet_rows]


def complete_query(query: str, suggestions: list[str]) -> list[str]:
    last_word = re.search(r"\w+$", query)
    if not last_word:
        return []
    prefix, typed = query[: last_word.start()], last_word.group().lower()
    # Infix matches like "dashiki" for "hik" read as noise in a search box.
    completions = [word for word in suggestions if word.startswith(typed) and word != typed]
    return [prefix + word for word in completions[:AUTOCOMPLETE_LIMIT]]
