"""Run: docker compose run --rm --no-deps app python test_search.py"""

from app import sql_quote
from conversational_search import history_turns
from search import (
    build_count_sql,
    build_geo_json,
    build_geo_points_sql,
    build_geo_sql,
    build_image_knn_sql,
    build_products_json,
    build_products_sql,
    build_search_json,
    build_search_sql,
    category_counts,
    complete_query,
    json_list,
    json_text,
    sql_list,
    to_hit,
)

# User input never reaches MATCH() as operators, and quotes stay escaped in the KNN text.
sql = build_search_sql("it's a \"t-shirt\" | -sale", "hybrid", ["tops"], 5, True, sql_quote)
assert "MATCH('it s a t shirt sale')" in sql, sql
assert "knn(embedding_vector, 100, 'it\\'s a \"t-shirt\" | -sale')" in sql, sql
assert "categories IN ('tops')" in sql and "OPTION fuzzy=1, fusion_method='rrf'" in sql, sql
assert "FACET" not in sql, sql

# The JSON body mirrors the SQL: query_string keeps the AND between words, the raw text goes to knn, filters join in a bool.
body = build_search_json("it's a \"t-shirt\" | -sale", "hybrid", ["tops"], 5, True)
assert body["query"] == {"bool": {"must": [{"query_string": "it s a t shirt sale"}, {"in": {"categories": ["tops"]}}]}}, body
assert body["knn"] == {"field": "embedding_vector", "query": "it's a \"t-shirt\" | -sale", "k": 100}, body
assert body["options"] == {"fuzzy": True, "fusion_method": "rrf"} and body["limit"] == 5 and "aggs" not in body, body
body = build_search_json("boots", "fulltext", None, 3, False)
assert body["query"] == {"query_string": "boots"} and "options" not in body and "knn" not in body and "aggs" in body, body
body = build_search_json("boots", "vector", ["footwear"], 3, True)
assert body["query"] == {"in": {"categories": ["footwear"]}} and "options" not in body and body["knn"]["query"] == "boots", body

# The frontend sends several checked categories as one comma-separated value.
sql = build_search_sql("boots", "fulltext", ["footwear", "outerwear"], 3, False, sql_quote)
assert "categories IN ('footwear', 'outerwear')" in sql, sql
assert build_count_sql("boots", ["footwear", "outerwear"], True, sql_quote) == (
    "SELECT COUNT(*) FROM convapparel_products WHERE MATCH('boots') AND categories IN ('footwear', 'outerwear') OPTION fuzzy=1"
)
assert build_count_sql("boots", None, False, sql_quote) == "SELECT COUNT(*) FROM convapparel_products WHERE MATCH('boots')"

sql = build_search_sql("boots", "fulltext", None, 3, False, sql_quote)
assert sql.endswith("LIMIT 3 FACET categories ORDER BY COUNT(*) DESC"), sql
assert "OPTION" not in sql and "knn(" not in sql, sql

sql = build_search_sql("boots", "vector", None, 3, True, sql_quote)
assert "MATCH(" not in sql and "fuzzy" not in sql and "FACET" not in sql, sql

assert category_counts([
    {"categories": "footwear", "count(*)": 7},
    {"categories": "bottoms", "count(*)": 2},
]) == [{"value": "footwear", "count": 7}, {"value": "bottoms", "count": 2}]

row = {"id": 2**62, "title": "", "description": "", "features": "", "category": "tops", "image_url": ""}
keyword_only = to_hit({**row, "text_score": 1500, "distance": 3.4e38})
assert keyword_only["id"] == str(2**62) and keyword_only["matched_words"] and keyword_only["similarity"] is None
knn_only = to_hit({**row, "text_score": 1, "distance": 0.25})
assert not knn_only["matched_words"] and knn_only["similarity"] == 0.75

assert sql_list([1, 2, 3, 4], preview=True) == "(1, 2, 3, …)" and sql_list([1, 2]) == "(1, 2)"
# Dropped values become a bare … in the shown JSON; a real ellipsis string stays quoted.
assert json_text({"a": json_list([1, 2, 3, 4], preview=True), "b": "…"}) == '{\n  "a": [1, 2, 3, …],\n  "b": "…"\n}'
assert json_text(build_products_json(json_list([2**62, 2], preview=True), ["tops"])) == (
    '{\n  "table": "convapparel_products",\n  "query": {"bool": {"must": [{"in": {"id": [4611686018427387904, 2]}}, {"in": {"categories": ["tops"]}}]}},\n'
    '  "limit": 100,\n  "_source": ["title", "description", "features", "category", "image_url"]\n}'
)
assert "knn(image_vector, 100, (0.1, 0.2))" in build_image_knn_sql("(0.1, 0.2)")
sql = build_products_sql("(1, 2)", ["tops"], sql_quote)
assert "WHERE id IN (1, 2) AND categories IN ('tops') LIMIT 100" in sql, sql

assert complete_query("waterproof hik", ["hiking", "dashiki", "hik"]) == ["waterproof hiking"]
assert complete_query("boots ", ["boots"]) == []

sql = build_geo_sql(40.7128, -74.006, 10, ["footwear"], 12, sql_quote)
assert sql == (
    "SELECT id, title, description, features, category, image_url, lat, lon, "
    "GEODIST(40.712800, -74.006000, lat, lon, {in=degrees, out=km}) AS distance_km "
    "FROM convapparel_products WHERE distance_km <= 10 AND categories IN ('footwear') "
    "ORDER BY id ASC LIMIT 12"
), sql
sql = build_geo_sql(40.7, -74.0, 0.5, None, 24, sql_quote)
assert "categories" not in sql and "distance_km <= 0.5" in sql and sql.endswith("ORDER BY id ASC LIMIT 24"), sql
sql = build_geo_points_sql(40.7, -74.0, 37.5, 600)
assert sql == (
    "SELECT lat, lon, GEODIST(40.700000, -74.000000, lat, lon, {in=degrees, out=km}) AS distance_km "
    "FROM convapparel_products WHERE distance_km <= 37.5 ORDER BY id ASC LIMIT 600"
), sql
sql = build_geo_sql(40.7, -74.0, 10, None, 3, sql_quote, nearest=True)
assert sql.endswith("ORDER BY distance_km ASC LIMIT 3"), sql
body = build_geo_json(40.7, -74.0, 2.5, ["tops"], 3, nearest=True)
assert body["query"]["bool"]["must"][0]["geo_distance"]["distance"] == "2.5 km" and body["sort"] == [{"distance_km": "asc"}], body
assert build_geo_json(40.7, -74.0, 10, None, 100)["query"] == {"geo_distance": {"location_anchor": {"lat": 40.7, "lon": -74.0}, "location_source": "lat,lon", "distance": "10 km"}}

turns = history_turns([
    {"role": "user", "message": "comfy sneakers", "search_query": "comfortable sneakers"},
    {"role": "assistant", "message": "Try these [ref:22]. Or [ref:11], like [ref:22].", "search_query": ""},
    {"role": "user", "message": "in white?", "search_query": "white comfortable sneakers"},
])
assert turns == [
    {"message": "comfy sneakers", "search_query": "comfortable sneakers", "response": "Try these [ref:22]. Or [ref:11], like [ref:22].", "source_ids": [22, 11]},
    {"message": "in white?", "search_query": "white comfortable sneakers", "response": "", "source_ids": []},
], turns

print("ok")
