import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    "short_term": {"size": 100},
    "mid": {"capacity": 500, "promote_hits": 3, "demote_age_days": 30},
    "scoring": {"w_freq": 1.0, "w_recency": 1.0, "w_importance": 2.0, "lambda": 0.05},
    "extractor": {"mode": "hybrid"},
    "llm": {
        "provider": "openai",
        "endpoint": "",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
        "timeout_s": 8,
    },
    "fts": {"enabled": True},
}


def get_default_db_path() -> str:
    base = os.getcwd()
    data_dir = os.path.join(base, "data")
    return os.path.join(data_dir, "symbiosis.db")


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    if not db_path:
        db_path = get_default_db_path()
    _ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Pragmas for reliability
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        rows = conn.execute("PRAGMA compile_options;").fetchall()
        return any("FTS5" in r[0].upper() for r in rows)
    except Exception:
        return False


BOOTSTRAP_SQL_BASE = r"""
CREATE TABLE IF NOT EXISTS turns (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT NOT NULL,
  ts           DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
  role         TEXT NOT NULL CHECK (role IN ('user','assistant')),
  text         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session_ts ON turns(session_id, ts);

CREATE TABLE IF NOT EXISTS memories (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  layer         TEXT NOT NULL CHECK (layer IN ('mid','long')),
  content       TEXT NOT NULL,
  created_at    DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
  last_seen_at  DATETIME,
  hits          INTEGER NOT NULL DEFAULT 0,
  score         REAL NOT NULL DEFAULT 0.0,
  importance    INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived','deleted')),
  tags_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_layer_score ON memories(layer, score DESC);
CREATE INDEX IF NOT EXISTS idx_mem_last_seen ON memories(last_seen_at);

CREATE TABLE IF NOT EXISTS memory_links (
  memory_id   INTEGER NOT NULL,
  turn_id     INTEGER NOT NULL,
  reason      TEXT,
  PRIMARY KEY (memory_id, turn_id),
  FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value_json TEXT
);
"""


BOOTSTRAP_SQL_FTS = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content,
  content='memories',
  content_rowid='id',
  tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS trg_mem_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
  INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def bootstrap(conn: sqlite3.Connection) -> Dict[str, Any]:
    conn.executescript(BOOTSTRAP_SQL_BASE)
    fts_supported = has_fts5(conn)
    if fts_supported:
        conn.executescript(BOOTSTRAP_SQL_FTS)
    # initialize config if missing
    cur = conn.execute("SELECT value_json FROM meta WHERE key='config'")
    row = cur.fetchone()
    if row is None:
        cfg = DEFAULT_CONFIG.copy()
        if not fts_supported:
            cfg = json.loads(json.dumps(cfg))  # deep copy
            cfg["fts"]["enabled"] = False
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value_json) VALUES('config',?)",
            (json.dumps(cfg),),
        )
    else:
        cfg = json.loads(row[0])
        if not fts_supported:
            cfg["fts"]["enabled"] = False
            conn.execute(
                "UPDATE meta SET value_json=? WHERE key='config'",
                (json.dumps(cfg),),
            )
    conn.commit()
    return cfg


def get_config(conn: sqlite3.Connection) -> Dict[str, Any]:
    cur = conn.execute("SELECT value_json FROM meta WHERE key='config'")
    row = cur.fetchone()
    if not row:
        return DEFAULT_CONFIG
    try:
        return json.loads(row[0])
    except Exception:
        return DEFAULT_CONFIG


def set_config(conn: sqlite3.Connection, cfg: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value_json) VALUES('config', ?)",
        (json.dumps(cfg),),
    )
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection):
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def iso_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
