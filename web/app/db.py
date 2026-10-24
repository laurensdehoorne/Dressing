"""SQLite storage. Clothing details live in a JSON column so fields can grow without migrations."""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
DB_PATH = DATA_DIR / "dressing.db"
PHOTO_DIR = DATA_DIR / "photos"
TMP_DIR = DATA_DIR / "tmp"
BACKUP_DIR = DATA_DIR / "backups"

# The kinds of clothing, in the order the app shows them. The AI picks one of these keys.
CATEGORIES = {
    "top": "Tops & hemden",
    "trui": "Truien & vesten",
    "broek": "Broeken & shorts",
    "rok": "Rokken",
    "kleed": "Kleedjes & jumpsuits",
    "blazer": "Blazers & pakken",
    "jas": "Jassen",
    "schoenen": "Schoenen",
    "accessoire": "Accessoires & tassen",
    "sport": "Sport & loungewear",
}
SEASONS = ["lente", "zomer", "herfst", "winter"]
# Where a piece is: only what is in the dressing can be part of an outfit today.
STATUSES = {"dressing": "In de dressing", "was": "In de was", "stomerij": "Bij de stomerij", "uitgeleend": "Uitgeleend",
            "opgeborgen": "Opgeborgen (ander seizoen)"}
FORMALITY = ["sportief", "casual", "smart casual", "zakelijk", "feestelijk/formeel"]  # 1 to 5
WARMTH = ["heel licht", "licht", "gemiddeld", "warm", "heel warm"]  # 1 to 5

# Fields the client may set on a piece (everything else is managed by the server).
ITEM_FIELDS = {
    "name", "category", "kind", "colors", "pattern", "material", "brand", "size", "fit", "seasons", "warmth",
    "formality", "occasions", "style_tags", "description", "care", "location", "status", "notes", "favorite",
    "purchase_date", "price",
}
LIST_FIELDS = {"colors", "seasons", "occasions", "style_tags"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def init() -> None:
    for d in (DATA_DIR, PHOTO_DIR, TMP_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                settings TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                device TEXT NOT NULL DEFAULT '',
                last_seen TEXT,
                data_key TEXT
            );
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                date_added TEXT NOT NULL,
                has_photo INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS items_user ON items (user_id);
            -- An outfit that was worn (item_ids: the pieces, a JSON list in data), with how it went.
            CREATE TABLE IF NOT EXISTS worn (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                date TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS worn_user ON worn (user_id, date);
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
                user_id TEXT, ip TEXT NOT NULL DEFAULT '', via TEXT NOT NULL DEFAULT '', device TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS audit_log_at ON audit_log (at);
            CREATE TABLE IF NOT EXISTS login_failures (
                at REAL NOT NULL, user_key TEXT NOT NULL, ip TEXT NOT NULL, via TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS login_failures_user ON login_failures (user_key, at);
            CREATE INDEX IF NOT EXISTS login_failures_ip ON login_failures (ip, at);
            -- What each AI analysis cost (costs.py): its calls, and the sum as an estimate.
            CREATE TABLE IF NOT EXISTS ai_usage (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), at TEXT NOT NULL, task TEXT NOT NULL,
                status TEXT NOT NULL, failed INTEGER NOT NULL DEFAULT 0, models TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0, cost_eur REAL NOT NULL DEFAULT 0, calls TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS ai_usage_user ON ai_usage (user_id, at);
            -- A save the app sent with its own id, and its answer: the same id again gets that answer (main._once).
            CREATE TABLE IF NOT EXISTS request_keys (
                user_id TEXT NOT NULL, key TEXT NOT NULL, at TEXT NOT NULL, result TEXT, PRIMARY KEY (user_id, key)
            );
            -- A phone with notifications on (push.py).
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), data TEXT NOT NULL,
                created_at TEXT NOT NULL, device TEXT NOT NULL DEFAULT ''
            );
            """
        )


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA secure_delete = ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _photo_url(name: str) -> str:
    """Photo URL with a version suffix, so a replaced photo isn't served from the browser cache."""
    file = PHOTO_DIR / name
    version = int(file.stat().st_mtime) if file.exists() else 0
    return f"/photos/{name}?v={version}"


def item_row(row: sqlite3.Row) -> dict:
    return {
        **json.loads(row["data"]),
        "id": row["id"],
        "date_added": row["date_added"],
        "photo": _photo_url(f"{row['id']}.jpg") if row["has_photo"] else None,
    }


def worn_row(row: sqlite3.Row) -> dict:
    return {**json.loads(row["data"]), "id": row["id"], "date": row["date"]}


def _level(value) -> int | None:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


def _words(value, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))[:limit]


def clean_item(data: dict) -> dict:
    """What the client (or the AI) sent, limited to known fields with sane values."""
    out = {k: v for k, v in data.items() if k in ITEM_FIELDS}
    for key in LIST_FIELDS & out.keys():
        out[key] = _words(out[key])
    if "seasons" in out:
        out["seasons"] = [s for s in (w.lower() for w in out["seasons"]) if s in SEASONS]
    for key in ("warmth", "formality"):
        if key in out:
            out[key] = _level(out[key])
    if "category" in out and out["category"] not in CATEGORIES:
        out["category"] = "accessoire" if out["category"] else None
    if "status" in out and out["status"] not in STATUSES:
        out["status"] = "dressing"
    if "favorite" in out:
        out["favorite"] = bool(out["favorite"])
    for key in ("name", "kind", "pattern", "material", "brand", "size", "fit", "description", "care", "location",
                "notes", "purchase_date"):
        if key in out:
            out[key] = str(out[key] or "").strip()[:2000]
    if "price" in out:
        try:
            out["price"] = round(float(out["price"]), 2) if out["price"] not in (None, "") else None
        except (TypeError, ValueError):
            out["price"] = None
    return out
