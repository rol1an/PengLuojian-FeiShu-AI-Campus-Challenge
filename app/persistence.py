"""SQLite-backed persistence for pushed_events and qa_context.

All functions are synchronous (sqlite3 is fast for these tiny payloads).
The DB file lives at <project_root>/data/state.db and is created on first use.
"""
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / "data" / "state.db"


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pushed_events (
                key TEXT PRIMARY KEY,
                start_time TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qa_context (
                open_id    TEXT PRIMARY KEY,
                event_json TEXT NOT NULL,
                brief_json TEXT NOT NULL,
                history_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_index (
                node_token    TEXT PRIMARY KEY,
                title         TEXT NOT NULL DEFAULT '',
                url           TEXT NOT NULL DEFAULT '',
                excerpt       TEXT NOT NULL DEFAULT '',
                space_name    TEXT NOT NULL DEFAULT '',
                owner         TEXT NOT NULL DEFAULT '',
                obj_type      TEXT NOT NULL DEFAULT '',
                obj_edit_time TEXT NOT NULL DEFAULT '',
                updated_at    REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_embeddings (
                node_token  TEXT NOT NULL,
                chunk_idx   INTEGER NOT NULL,
                text_hash   TEXT NOT NULL,
                embedding   BLOB NOT NULL,
                model       TEXT NOT NULL,
                dims        INTEGER NOT NULL,
                created_at  REAL NOT NULL,
                PRIMARY KEY (node_token, chunk_idx)
            )
        """)
    logger.info("Persistence DB ready at %s", _DB_PATH)


# ── pushed_events ─────────────────────────────────────────────────────────────

def pushed_events_load() -> dict[str, str]:
    """Return all rows as {key: start_time_iso_str}."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT key, start_time FROM pushed_events").fetchall()
    return {key: st for key, st in rows}


def pushed_events_put(key: str, start_time_iso: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pushed_events (key, start_time) VALUES (?, ?)",
            (key, start_time_iso),
        )


def pushed_events_delete(keys: list[str]) -> None:
    if not keys:
        return
    with _get_conn() as conn:
        conn.executemany("DELETE FROM pushed_events WHERE key = ?", [(k,) for k in keys])


# ── qa_context ────────────────────────────────────────────────────────────────

def qa_context_load_all() -> list[tuple[str, str, str, str, float]]:
    """Return all rows as list of (open_id, event_json, brief_json, history_json, created_at)."""
    with _get_conn() as conn:
        return conn.execute(
            "SELECT open_id, event_json, brief_json, history_json, created_at FROM qa_context"
        ).fetchall()


def qa_context_put(open_id: str, event_json: str, brief_json: str, created_at: float) -> None:
    with _get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO qa_context
               (open_id, event_json, brief_json, history_json, created_at)
               VALUES (?, ?, ?, '[]', ?)""",
            (open_id, event_json, brief_json, created_at),
        )


def qa_context_update_history(open_id: str, history_json: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE qa_context SET history_json = ? WHERE open_id = ?",
            (history_json, open_id),
        )


def qa_context_delete(open_ids: list[str]) -> None:
    if not open_ids:
        return
    with _get_conn() as conn:
        conn.executemany("DELETE FROM qa_context WHERE open_id = ?", [(k,) for k in open_ids])


# ── doc_index ─────────────────────────────────────────────────────────────────

def doc_index_put_many(rows: list[tuple]) -> None:
    """Upsert rows of (node_token, title, url, excerpt, space_name, owner, obj_type, obj_edit_time, updated_at)."""
    if not rows:
        return
    with _get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO doc_index
               (node_token, title, url, excerpt, space_name, owner, obj_type, obj_edit_time, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def doc_index_get_by_tokens(tokens: list[str]) -> dict[str, dict]:
    """Return {node_token: metadata_dict} for matching tokens."""
    if not tokens:
        return {}
    placeholders = ",".join("?" * len(tokens))
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT node_token, title, url, excerpt, space_name, owner, obj_type, obj_edit_time "
            f"FROM doc_index WHERE node_token IN ({placeholders})",
            tokens,
        ).fetchall()
    return {
        row[0]: {
            "node_token": row[0], "title": row[1], "url": row[2],
            "excerpt": row[3], "space_name": row[4], "owner": row[5],
            "obj_type": row[6], "obj_edit_time": row[7],
            "obj_token": row[0], "creator": row[5],
        }
        for row in rows
    }


def doc_index_get_all_tokens() -> list[str]:
    """Return all node_tokens in doc_index."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT node_token FROM doc_index").fetchall()
    return [row[0] for row in rows]


def doc_index_count() -> int:
    with _get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM doc_index").fetchone()[0]


# ── doc_embeddings ────────────────────────────────────────────────────────────

def embedding_get_by_tokens(tokens: list[str]) -> dict[str, list[tuple]]:
    """Return {node_token: [(chunk_idx, text_hash, blob, model, dims), ...]}."""
    if not tokens:
        return {}
    placeholders = ",".join("?" * len(tokens))
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT node_token, chunk_idx, text_hash, embedding, model, dims "
            f"FROM doc_embeddings WHERE node_token IN ({placeholders})",
            tokens,
        ).fetchall()
    result: dict[str, list[tuple]] = {}
    for node_token, chunk_idx, text_hash, blob, model, dims in rows:
        result.setdefault(node_token, []).append((chunk_idx, text_hash, blob, model, dims))
    return result


def embedding_put_chunks(rows: list[tuple]) -> None:
    """Upsert rows of (node_token, chunk_idx, text_hash, blob, model, dims, created_at)."""
    if not rows:
        return
    with _get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO doc_embeddings
               (node_token, chunk_idx, text_hash, embedding, model, dims, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def embedding_delete_expired(cutoff_ts: float) -> None:
    """Delete embedding rows where created_at < cutoff_ts."""
    with _get_conn() as conn:
        conn.execute("DELETE FROM doc_embeddings WHERE created_at < ?", (cutoff_ts,))


def doc_index_delete_stale(before_ts: float) -> int:
    """Delete doc_index rows not refreshed since before_ts (i.e. not touched in last rebuild).
    Returns the number of rows deleted."""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM doc_index WHERE updated_at < ?", (before_ts,))
        return cur.rowcount


def doc_embeddings_delete_orphans() -> int:
    """Delete doc_embeddings rows whose node_token no longer exists in doc_index.
    Returns the number of rows deleted."""
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM doc_embeddings WHERE node_token NOT IN "
            "(SELECT node_token FROM doc_index)"
        )
        return cur.rowcount
