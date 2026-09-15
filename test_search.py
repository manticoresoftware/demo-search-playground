"""Run: docker compose run --rm --no-deps app python test_search.py"""

from app import sql_quote
from search import build_search_sql, category_counts, complete_query, to_hit

# User input never reaches MATCH() as operators, and quotes stay escaped in the KNN text.
sql = build_search_sql("it's a \"t-shirt\" | -sale", "hybrid", "tops", 5, True, sql_quote)
assert "MATCH('it s a t shirt sale')" in sql, sql
assert "knn(embedding_vector, 100, 'it\\'s a \"t-shirt\" | -sale')" in sql, sql
assert "REGEX(category, 'tops')" in sql and "OPTION fuzzy=1, fusion_method='rrf'" in sql, sql
assert "FACET" not in sql, sql

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

assert complete_query("waterproof hik", ["hiking", "dashiki", "hik"]) == ["waterproof hiking"]
assert complete_query("boots ", ["boots"]) == []

print("ok")
