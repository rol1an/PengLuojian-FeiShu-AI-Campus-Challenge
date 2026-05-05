"""Vector search: embedding, indexing, and semantic recall for hybrid wiki search.

Uses Doubao-Embedding-Vision via the multimodal embeddings endpoint:
  POST https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal
  input: [{"type": "text", "text": "..."}]
  response: {"data": {"embedding": [...]}, ...}

Each API call returns exactly one vector — concurrent calls are used for batches.
Vectors are persisted in SQLite (app/persistence.py) as raw float BLOBs.
No numpy required — pure Python struct.pack/unpack + math.
"""
import asyncio
import hashlib
import logging
import math
import struct
import time

import httpx

from app.config import settings
from app.models import WikiDoc
import app.persistence as db
from app.workflow_premeet.chunker import chunk_doc

logger = logging.getLogger(__name__)

_EMBED_CONCURRENCY = 8   # max concurrent embedding API calls
_EMBED_URL = "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"


# ── Embedding API ──────────────────────────────────────────────────────────────

def _embed_headers() -> dict[str, str]:
    if not settings.DOUBAO_EMBEDDING_MODEL:
        raise RuntimeError("DOUBAO_EMBEDDING_MODEL not configured")
    api_key = settings.DOUBAO_EMBEDDING_API_KEY or settings.DOUBAO_API_KEY
    if not api_key:
        raise RuntimeError("DOUBAO_EMBEDDING_API_KEY not configured")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


async def _embed_one(client: httpx.AsyncClient, text: str) -> list[float]:
    """Call the multimodal embedding endpoint for a single text. Returns a float list."""
    payload = {
        "model": settings.DOUBAO_EMBEDDING_MODEL,
        "input": [{"type": "text", "text": text[:3000]}],
    }
    resp = await client.post(_EMBED_URL, headers=_embed_headers(), json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()["data"]["embedding"]


# ── BLOB encoding ─────────────────────────────────────────────────────────────

def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes, dims: int) -> list[float]:
    return list(struct.unpack(f"{dims}f", blob))


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── Similarity ────────────────────────────────────────────────────────────────

def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ── Raw embedding ─────────────────────────────────────────────────────────────

async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts concurrently (one API call per text, up to _EMBED_CONCURRENCY parallel)."""
    if not texts:
        return []
    sem = asyncio.Semaphore(_EMBED_CONCURRENCY)

    async def _bounded(client: httpx.AsyncClient, text: str) -> list[float]:
        async with sem:
            return await _embed_one(client, text)

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_bounded(client, t) for t in texts])
    return list(results)


async def embed_query(title: str, description: str, keywords: list[str]) -> list[float]:
    """Embed the meeting query string. Not cached."""
    text = f"{title}\n{description}\n{' '.join(keywords)}"
    vecs = await embed_texts([text])
    return vecs[0]


# ── Index docs (write cache) ──────────────────────────────────────────────────

async def index_docs(
    docs: list[WikiDoc],
    full_contents: dict[str, str] | None = None,
    obj_types: dict[str, str] | None = None,
) -> None:
    """Chunk docs → embed → persist to doc_embeddings + doc_index.

    Already-cached chunks whose text_hash is unchanged are skipped.
    full_contents: {node_token: markdown_text} — if available, enables structural chunking.
    obj_types: {node_token: doc_type} — e.g. "docx"/"sheet"/"slides"; defaults to "docx".
    """
    if not docs:
        return
    full_contents = full_contents or {}
    obj_types = obj_types or {}
    now = time.time()
    cutoff = now - settings.EMBEDDING_CACHE_TTL_DAYS * 86400

    # Evict expired entries
    db.embedding_delete_expired(cutoff)

    # Load existing cache for these docs
    all_tokens = [d.node_token for d in docs if d.node_token]
    existing = db.embedding_get_by_tokens(all_tokens)

    # Build chunk texts and identify which need embedding
    doc_chunks: dict[str, list[str]] = {}       # node_token → chunk texts
    doc_chunk_hashes: dict[str, list[str]] = {}  # node_token → chunk hashes

    for doc in docs:
        if not doc.node_token:
            continue
        content = full_contents.get(doc.node_token, "")
        doc_type = obj_types.get(doc.node_token, "docx")
        chunks = chunk_doc(doc.title, doc.excerpt, doc_type, content)
        doc_chunks[doc.node_token] = chunks
        doc_chunk_hashes[doc.node_token] = [_text_hash(c) for c in chunks]

    # Determine which (token, chunk_idx) pairs need (re-)embedding
    to_embed_texts: list[str] = []
    to_embed_keys: list[tuple[str, int]] = []  # (node_token, chunk_idx)

    for doc in docs:
        tok = doc.node_token
        if not tok:
            continue
        chunks = doc_chunks.get(tok, [])
        hashes = doc_chunk_hashes.get(tok, [])
        cached_by_idx = {ci: (th, blob, mdl, dims) for ci, th, blob, mdl, dims in existing.get(tok, [])}

        for idx, (chunk_text, chunk_hash) in enumerate(zip(chunks, hashes)):
            cached = cached_by_idx.get(idx)
            if cached and cached[0] == chunk_hash and cached[2] == settings.DOUBAO_EMBEDDING_MODEL:
                continue  # cache hit, skip
            to_embed_texts.append(chunk_text)
            to_embed_keys.append((tok, idx))

    if to_embed_texts:
        logger.info("index_docs: embedding %d chunks for %d docs", len(to_embed_texts), len(docs))
        try:
            vectors = await embed_texts(to_embed_texts)
        except Exception as e:
            logger.warning("index_docs: embedding API failed: %s", e)
            return

        chunk_rows: list[tuple] = []
        for (tok, idx), vec in zip(to_embed_keys, vectors):
            chunk_hash = doc_chunk_hashes[tok][idx]
            chunk_rows.append((
                tok, idx, chunk_hash,
                _pack(vec),
                settings.DOUBAO_EMBEDDING_MODEL,
                len(vec),
                now,
            ))
        db.embedding_put_chunks(chunk_rows)
    else:
        logger.debug("index_docs: all %d docs fully cached", len(docs))

    # Update doc_index metadata
    index_rows: list[tuple] = []
    for doc in docs:
        if not doc.node_token:
            continue
        index_rows.append((
            doc.node_token, doc.title, doc.url, doc.excerpt[:500],
            doc.space_name, doc.owner, doc.obj_type, doc.obj_edit_time or "",
            now,
        ))
    db.doc_index_put_many(index_rows)


# ── Vector recall (online) ────────────────────────────────────────────────────

async def vector_recall(
    q_vec: list[float],
    exclude_tokens: set[str],
    top_k: int,
) -> list[dict]:
    """Retrieve top-k semantically similar docs from the pre-built index.

    Uses max-pool over a doc's chunks (best chunk wins).
    Returns list[dict] in the same format as _search_one_raw items,
    so results can be merged directly into raw_candidates.
    """
    all_tokens = db.doc_index_get_all_tokens()
    candidate_tokens = [t for t in all_tokens if t not in exclude_tokens]
    if not candidate_tokens:
        return []

    chunk_data = db.embedding_get_by_tokens(candidate_tokens)

    # Compute max-pool cosine similarity per doc
    scores: list[tuple[float, str]] = []
    dims = settings.EMBEDDING_DIMS
    for tok, chunks_info in chunk_data.items():
        if tok in exclude_tokens:
            continue
        max_sim = 0.0
        for _idx, _hash, blob, _model, stored_dims in chunks_info:
            vec = _unpack(blob, stored_dims or dims)
            sim = cosine_similarity(q_vec, vec)
            if sim > max_sim:
                max_sim = sim
        if max_sim > 0:
            scores.append((max_sim, tok))

    scores.sort(reverse=True)
    top_tokens = [tok for _, tok in scores[:top_k]]
    if not top_tokens:
        return []

    # Rebuild raw_candidate dicts from doc_index
    meta = db.doc_index_get_by_tokens(top_tokens)
    result: list[dict] = []
    for tok in top_tokens:
        m = meta.get(tok)
        if m:
            result.append(m)
    return result
