import re
import sqlite3
import string
from typing import Dict, List

from ..models import ExtractCandidate
from ..storage import iso_now


PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def normalize(text: str) -> str:
    return " ".join(text.lower().translate(PUNCT_TABLE).split())


def heuristic_extract(text: str) -> List[ExtractCandidate]:
    cands: List[ExtractCandidate] = []
    t = text.strip()
    if not t:
        return cands

    # Basic preference patterns
    pref_pat = re.compile(r"\b(i like|i love|i hate|i prefer)\b\s+([^\.\!\?]+)", re.IGNORECASE)
    for m in pref_pat.finditer(t):
        phrase = m.group(0)
        obj = m.group(2).strip()
        content = f"User {m.group(1).lower()} {obj}".strip()
        cands.append(ExtractCandidate(content=content, importance=0, tags=["preference"]))

    # Identity patterns
    name_pat = re.compile(r"\b(my name is|i am|im)\b\s+([A-Za-z][A-Za-z0-9_-]{1,31})", re.IGNORECASE)
    for m in name_pat.finditer(t):
        name = m.group(2)
        content = f"User name is {name}"
        cands.append(ExtractCandidate(content=content, importance=1, tags=["identity"]))

    # Negative / state-change
    neg_pat = re.compile(r"\b(no longer|dont anymore|do not anymore|changed to|now)\b[^\.\!\?]*", re.IGNORECASE)
    for m in neg_pat.finditer(t):
        phrase = m.group(0).strip()
        cands.append(ExtractCandidate(content=f"State change: {phrase}", importance=0, tags=["state"]))

    # Basic date mentions (very naive)
    date_pat = re.compile(r"\b(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
    for m in date_pat.finditer(t):
        date_s = m.group(1)
        cands.append(ExtractCandidate(content=f"Date mentioned: {date_s}", importance=0, tags=["time"]))

    # Deduplicate within this turn by normalized content
    uniq: Dict[str, ExtractCandidate] = {}
    for c in cands:
        key = normalize(c.content)
        uniq[key] = c
    return list(uniq.values())


def apply_extraction(conn: sqlite3.Connection, session_id: str, turn_id: int, text: str, cfg: Dict) -> List[Dict]:
    mode = str(cfg.get("extractor", {}).get("mode", "hybrid")).lower()
    # For v0.2, we implement heuristic only; LLM integration can be added later.
    cands = heuristic_extract(text)

    results: List[Dict] = []
    now = iso_now()
    for c in cands:
        norm = normalize(c.content)
        # Try to find existing active memory with same normalized content
        row = conn.execute(
            "SELECT id, content, hits FROM memories WHERE status='active' AND (lower(replace(content, '.', '')) = ?) LIMIT 1",
            (norm,),
        ).fetchone()
        if row:
            mem_id = int(row["id"])
            conn.execute(
                "UPDATE memories SET hits=hits+1, last_seen_at=? WHERE id=?",
                (now, mem_id),
            )
            action = "update"
        else:
            cur = conn.execute(
                "INSERT INTO memories(layer, content, created_at, last_seen_at, hits, score, importance, status, tags_json)"
                " VALUES('mid',?,?,?,?,0.0,?, 'active', ?)",
                (c.content, now, now, 1, c.importance, (None if not c.tags else json_dumps(c.tags))),
            )
            mem_id = cur.lastrowid
            action = "create"

        conn.execute(
            "INSERT OR REPLACE INTO memory_links(memory_id, turn_id, reason) VALUES(?,?,?)",
            (mem_id, turn_id, "extracted"),
        )
        results.append({"id": mem_id, "content": c.content, "action": action})

    return results


def json_dumps(val) -> str:
    # local import to avoid circular
    import json

    return json.dumps(val, ensure_ascii=False)

