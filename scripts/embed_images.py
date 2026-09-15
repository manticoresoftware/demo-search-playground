"""Compute Fashion CLIP vectors for every product photo and save them as SQL for init_manticore.sh.

Run: docker compose run --rm -v "$PWD:/app" app python scripts/embed_images.py
A stopped run resumes where it left off.
"""

from __future__ import annotations

import base64
import hashlib
import json
import lzma
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path

MANTICORE_HTTP = "http://manticore:9308"
EMBED_HTTP = "http://embed:8000"
TABLE = "convapparel_products"
# A separate table: Manticore can't UPDATE a KNN-indexed vector, and Buddy's partial REPLACE fails on some ids.
IMAGE_TABLE = "convapparel_product_images"
IMAGE_TABLE_SCHEMA = f"CREATE TABLE {IMAGE_TABLE} (image_vector float_vector knn_type='hnsw' knn_dims='512' hnsw_similarity='COSINE')"
INSERT_BATCH_SIZE = 200
PAGE_SIZE = 1000
BATCH_SIZE = 64
DOWNLOAD_WORKERS = 32
# One embedding batch keeps about four cores busy, so a few run side by side.
EMBED_WORKERS = 3
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_TIMEOUT_S = 30
EMBED_TIMEOUT_S = 600
# CLIP looks at 224x224 pixels, so bigger downloads only cost bandwidth.
THUMBNAIL_WIDTH = 224
# The same file behind many products is Amazon's "No image available" placeholder.
PLACEHOLDER_MIN_PRODUCTS = 20
VECTOR_DECIMALS = 4
PROGRESS = Path("dumps/image_vectors.progress.jsonl")
OUTPUT = Path("dumps/convapparel_image_vectors.sql.xz")
# Same part size as the product dump, which keeps every file under GitHub's size limit.
PART_BYTES = 25 * 1024 * 1024


def sql(query: str) -> list[dict]:
    body = urllib.parse.urlencode({"query": query}).encode()
    with urllib.request.urlopen(urllib.request.Request(f"{MANTICORE_HTTP}/sql?mode=raw", data=body), timeout=120) as resp:
        result = json.load(resp)[0]
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result.get("data", [])


def products():
    last_id = 0
    while rows := sql(
        f"SELECT id, image_url FROM {TABLE} WHERE id > {last_id} ORDER BY id ASC LIMIT {PAGE_SIZE} OPTION max_matches={PAGE_SIZE}"
    ):
        yield from rows
        last_id = rows[-1]["id"]


def download(url: str) -> bytes | None:
    thumbnail = re.sub(r"\._AC_[A-Z0-9_]+_\.", f"._AC_UL{THUMBNAIL_WIDTH}_.", url)
    for _ in range(DOWNLOAD_ATTEMPTS):
        try:
            with urllib.request.urlopen(thumbnail, timeout=DOWNLOAD_TIMEOUT_S) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            error = exc
    print(f"skipped {thumbnail}: {error}", file=sys.stderr)
    return None


def embed(files: list[bytes]) -> list[list[float]]:
    body = json.dumps({"images": [base64.b64encode(data).decode() for data in files]}).encode()
    request = urllib.request.Request(f"{EMBED_HTTP}/images", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=EMBED_TIMEOUT_S) as resp:
        return json.load(resp)


def embed_batch(files: list[bytes]) -> list[list[float] | None]:
    try:
        return embed(files)
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
    # One unreadable file rejects the whole batch, so find it by embedding the files one by one.
    vectors = []
    for data in files:
        try:
            vectors.append(embed([data])[0])
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
            vectors.append(None)
    return vectors


def embed_rows(batch: list[dict], downloads: ThreadPoolExecutor) -> list[dict]:
    downloaded = [(row, data) for row, data in zip(batch, downloads.map(download, (row["image_url"] for row in batch))) if data]
    vectors = embed_batch([data for _, data in downloaded])
    return [
        {"id": row["id"], "sha1": hashlib.sha1(data).hexdigest(), "vector": [round(x, VECTOR_DECIMALS) for x in vector]}
        for (row, data), vector in zip(downloaded, vectors)
        if vector is not None
    ]


def main() -> None:
    done = {json.loads(line)["id"] for line in PROGRESS.open()} if PROGRESS.exists() else set()
    todo = [row for row in products() if row["id"] not in done]
    print(f"{len(done)} products already embedded, {len(todo)} to go", flush=True)

    batches = [todo[start : start + BATCH_SIZE] for start in range(0, len(todo), BATCH_SIZE)]
    with PROGRESS.open("a") as progress, ThreadPoolExecutor(DOWNLOAD_WORKERS) as downloads, ThreadPoolExecutor(EMBED_WORKERS) as embeds:
        for finished, entries in enumerate(embeds.map(lambda batch: embed_rows(batch, downloads), batches), start=1):
            progress.writelines(json.dumps(entry) + "\n" for entry in entries)
            progress.flush()
            print(f"{len(done) + min(finished * BATCH_SIZE, len(todo))}/{len(done) + len(todo)}", flush=True)

    counts = Counter(json.loads(line)["sha1"] for line in PROGRESS.open())
    placeholders = {sha1 for sha1, count in counts.items() if count >= PLACEHOLDER_MIN_PRODUCTS}
    rows = (json.loads(line) for line in PROGRESS.open())
    values = (f"({entry['id']},({','.join(map(str, entry['vector']))}))" for entry in rows if entry["sha1"] not in placeholders)
    with lzma.open(OUTPUT, "wt") as output:
        output.write(f"DROP TABLE IF EXISTS {IMAGE_TABLE};\n{IMAGE_TABLE_SCHEMA};\n")
        while batch := list(islice(values, INSERT_BATCH_SIZE)):
            output.write(f"INSERT INTO {IMAGE_TABLE} (id, image_vector) VALUES {','.join(batch)};\n")

    for old_part in OUTPUT.parent.glob(f"{OUTPUT.name}.part-*"):
        old_part.unlink()
    with OUTPUT.open("rb") as whole:
        for number, chunk in enumerate(iter(lambda: whole.read(PART_BYTES), b"")):
            Path(f"{OUTPUT}.part-{number:02d}").write_bytes(chunk)
    OUTPUT.unlink()
    skipped = sum(counts[sha1] for sha1 in placeholders)
    print(f"Wrote {OUTPUT}.part-* with {sum(counts.values()) - skipped} vectors; skipped {skipped} placeholder photos")


if __name__ == "__main__":
    main()
