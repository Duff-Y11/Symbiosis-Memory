import json
import sqlite3
from typing import Dict, List


def get_context(conn: sqlite3.Connection, session_id: str, cfg: Dict, k: int = 20) -> Dict:
    keep = int(cfg.get("short_term", {}).get("size", 100))
    # Short-term: last N turns for session (ascending)
    cur = conn.execute(
        "SELECT id, session_id, ts, role, text FROM turns WHERE session_id=? ORDER BY ts DESC, id DESC LIMIT ?",
        (session_id, keep),
    )
    turns_desc = [dict(r) for r in cur.fetchall()]
    short_term = list(reversed(turns_desc))

    # Mid-term: top-K by score
    cur2 = conn.execute(
        "SELECT id, layer, content, created_at, last_seen_at, hits, score, importance, status, tags_json "
        "FROM memories WHERE layer='mid' AND status='active' ORDER BY score DESC, id DESC LIMIT ?",
        (k,),
    )
    mid_term = []
    for r in cur2.fetchall():
        it = dict(r)
        if it.get("tags_json"):
            try:
                it["tags"] = json.loads(it["tags_json"])  # present both
            except Exception:
                it["tags"] = []
        mid_term.append(it)

    return {"short_term": short_term, "mid_term": mid_term}

