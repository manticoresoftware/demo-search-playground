"""Run: docker compose run --rm --no-deps app python test_search.py"""

from app import sql_quote
from search import build_geo_sql, build_image_knn_sql, build_products_sql, build_search_sql, category_counts, complete_query, sql_list, to_hit

# User input never reaches MATCH() as operators, and quotes stay escaped in the KNN text.
sql = build_search_sql("it's a \"t-shirt\" | -sale", "hybrid", "tops", 5, True, sql_quote)
assert "MATCH('it s a t shirt sale')" in sql, sql
assert "knn(embedding_vector, 100, 'it\\'s a \"t-shirt\" | -sale')" in sql, sql
assert "REGEX(category, 'tops')" in sql and "OPTION fuzzy=1, fusion_method='rrf'" in sql, sql
assert "FACET" not in sql, sql

# The frontend sends several checked categories as one comma-separated value.
sql = build_search_sql("boots", "fulltext", "footwear|outerwear", 3, False, sql_quote)
assert "REGEX(category, 'footwear|outerwear')" in sql, sql

sql = build_search_sql("boots", "fulltext", None, 3, False, sql_quote)
assert sql.endswith("LIMIT 3 FACET category ORDER BY COUNT(*) DESC"), sql
assert "OPTION" not in sql and "knn(" not in sql, sql

sql = build_search_sql("boots", "vector", None, 3, True, sql_quote)
assert "MATCH(" not in sql and "fuzzy" not in sql and "FACET" not in sql, sql

assert category_counts([
    {"category": "footwear", "count(*)": 5},
    {"category": "bottoms, footwear", "count(*)": 2},
]) == [{"value": "footwear", "count": 7}, {"value": "bottoms", "count": 2}]

row = {"id": 2**62, "title": "", "description": "", "features": "", "category": "tops", "image_url": ""}
keyword_only = to_hit({**row, "text_score": 1500, "distance": 3.4e38})
assert keyword_only["id"] == str(2**62) and keyword_only["matched_words"] and keyword_only["similarity"] is None
knn_only = to_hit({**row, "text_score": 1, "distance": 0.25})
assert not knn_only["matched_words"] and knn_only["similarity"] == 0.75

assert sql_list([1, 2, 3, 4], preview=True) == "(1, 2, 3, …)" and sql_list([1, 2]) == "(1, 2)"
assert "knn(image_vector, 100, (0.1, 0.2))" in build_image_knn_sql("(0.1, 0.2)")
sql = build_products_sql("(1, 2)", "tops", sql_quote)
assert "WHERE id IN (1, 2) AND REGEX(category, 'tops') LIMIT 100" in sql, sql

assert complete_query("waterproof hik", ["hiking", "dashiki", "hik"]) == ["waterproof hiking"]
assert complete_query("boots ", ["boots"]) == []

sql = build_geo_sql(40.7128, -74.006, 10, "footwear", 12, sql_quote)
assert sql == (
    "SELECT id, title, description, features, category, image_url, lat, lon, "
    "GEODIST(40.712800, -74.006000, lat, lon, {in=degrees, out=km}) AS distance_km "
    "FROM convapparel_products WHERE distance_km <= 10 AND REGEX(category, 'footwear') "
    "ORDER BY id ASC LIMIT 12 FACET category ORDER BY COUNT(*) DESC"
), sql
sql = build_geo_sql(40.7, -74.0, 0.5, None, 24, sql_quote)
assert "REGEX" not in sql and "distance_km <= 0.5" in sql and sql.endswith("ORDER BY id ASC LIMIT 24 FACET category ORDER BY COUNT(*) DESC"), sql

print("ok")
