"""Adds deterministic lat/lon attributes to the product dump (stdin -> stdout).

The playground pretends every product is stocked around New York City, so the
Geo demo needs coordinates. Manticore can't join another table, so they are
baked into `convapparel_products` itself: a SHA-256 hash of the product id maps
to one point scattered about +/-150 km around the city center, which keeps the
coordinates stable across re-bakes.

    cat dumps/convapparel_products_with_embeddings.sql.xz.part-* \
      | xz -cd | python3 scripts/bake_coordinates.py \
      | xz -T0 | split -b 26214400 -d - dumps/convapparel_products_with_embeddings.sql.xz.part-

Run ./scripts/init_manticore.sh afterwards to load the new dump.
"""

from __future__ import annotations

import hashlib
import re
import sys

# Midtown Manhattan; the products scatter roughly +/-150 km around it.
CENTER_LAT = 40.7128
CENTER_LON = -74.0060
SPAN_LAT = 2.7
SPAN_LON = 3.55

ROW_ID = re.compile(r"^\((\d+),")
# Tuples end with two closing parens (the embedding vector list) and a comma or semicolon.
# The match must stop before the trailing newline, or the replacement eats it and merges every row into one line.
ROW_TAIL = re.compile(r"\)\)([,;])[ \t]*$")
# Rows from a previous bake already end with "),lat,lon)"; a re-bake replaces those coordinates.
ROW_BAKED = re.compile(r"\),(-?\d+\.\d+),(-?\d+\.\d+)\)([,;])[ \t]*$")


def coordinates(product_id: int) -> tuple[float, float]:
    digest = hashlib.sha256(f"geo:{product_id}".encode()).digest()
    unit_lat = int.from_bytes(digest[:4], "big") / 2**32
    unit_lon = int.from_bytes(digest[4:8], "big") / 2**32
    return CENTER_LAT + (unit_lat - 0.5) * SPAN_LAT, CENTER_LON + (unit_lon - 0.5) * SPAN_LON


def main() -> None:
    rows = 0
    for line in sys.stdin:
        id_match = ROW_ID.match(line)
        if not id_match:
            if line in ("`lat` float,\n", "`lon` float,\n"):
                # Dropped and re-added next to `source`, so re-baking a baked dump stays idempotent.
                continue
            if line.startswith("`source` text,"):
                line = "`source` text,\n`lat` float,\n`lon` float,\n"
            elif line.startswith("INSERT INTO `convapparel_products` ("):
                line = line.replace("`embedding_vector`) VALUES", "`embedding_vector`, `lat`, `lon`) VALUES")
            sys.stdout.write(line)
            continue
        lat, lon = coordinates(int(id_match.group(1)))
        if ROW_BAKED.search(line):
            line = ROW_BAKED.sub(lambda m: f"),{lat:.6f},{lon:.6f}){m.group(3)}", line)
        else:
            line = ROW_TAIL.sub(lambda m: f"),{lat:.6f},{lon:.6f}){m.group(1)}", line)
        sys.stdout.write(line)
        rows += 1
    sys.stderr.write(f"Added coordinates to {rows} product rows\n")


if __name__ == "__main__":
    main()
