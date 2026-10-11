"""SQLite database — knowledge base storage."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "platform.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_entries (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            title    TEXT NOT NULL,
            content  TEXT NOT NULL,
            tags     TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT NOT NULL,
            url             TEXT DEFAULT '',
            description     TEXT DEFAULT '',
            source_type     TEXT NOT NULL,
            sub_section     TEXT DEFAULT '',
            collection_json TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    # migration: add columns if table pre-dates this schema
    for col, definition in [
        ("sub_section", "TEXT DEFAULT ''"),
        ("url", "TEXT DEFAULT ''"),
        ("collection_json", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE links ADD COLUMN {col} {definition}")
        except Exception:
            pass
    conn.commit()
    conn.close()


# ── CRUD helpers ──────────────────────────────────────────────────────────────

def kb_list() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM knowledge_entries ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def kb_get(entry_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM knowledge_entries WHERE id=?", (entry_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def kb_create(title: str, content: str, tags: str = "") -> dict:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO knowledge_entries (title, content, tags) VALUES (?,?,?)",
        (title, content, tags),
    )
    conn.commit()
    entry_id = cur.lastrowid
    conn.close()
    return kb_get(entry_id)


def kb_update(entry_id: int, title: str, content: str, tags: str = "") -> dict | None:
    conn = get_conn()
    conn.execute(
        "UPDATE knowledge_entries SET title=?, content=?, tags=?, updated_at=datetime('now') WHERE id=?",
        (title, content, tags, entry_id),
    )
    conn.commit()
    conn.close()
    return kb_get(entry_id)


def kb_delete(entry_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM knowledge_entries WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def kb_get_all_context() -> str:
    """Return all knowledge entries as a single context string for Claude."""
    entries = kb_list()
    if not entries:
        return ""
    parts = []
    for e in entries:
        tags = f" [{e['tags']}]" if e['tags'] else ""
        parts.append(f"## {e['title']}{tags}\n{e['content']}")
    return "\n\n".join(parts)


# ── Settings (runtime overrides for env vars) ─────────────────────────────────

def setting_set(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def setting_get(key: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else ""


# ── Links (Postman collections & Confluence pages) ────────────────────────────

def link_list(source_type: str, sub_section: str = "") -> list[dict]:
    conn = get_conn()
    if sub_section:
        rows = conn.execute(
            "SELECT * FROM links WHERE source_type=? AND sub_section=? ORDER BY created_at DESC",
            (source_type, sub_section),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM links WHERE source_type=? ORDER BY created_at DESC", (source_type,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def link_create(title: str, url: str, description: str, source_type: str, sub_section: str = "", collection_json: str = "") -> dict:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO links (title, url, description, source_type, sub_section, collection_json) VALUES (?,?,?,?,?,?)",
        (title, url, description, source_type, sub_section, collection_json),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM links WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


def link_delete(link_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM links WHERE id=?", (link_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0
