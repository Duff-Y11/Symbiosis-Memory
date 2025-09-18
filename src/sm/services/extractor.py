import re
import sqlite3
import string
from typing import Dict, List, Tuple, Optional

from ..models import ExtractCandidate
from ..storage import iso_now


PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def normalize(text: str) -> str:
    return " ".join(text.lower().translate(PUNCT_TABLE).split())


def tokens(text: str) -> List[str]:
    return normalize(text).split()


def jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def heuristic_extract(text: str) -> List[ExtractCandidate]:
    cands: List[ExtractCandidate] = []
    t = text.strip()
    if not t:
        return cands

    # Basic preference patterns
    pref_pat = re.compile(r"\b(i like|i love|i hate|i prefer)\b\s+([^\.\!\?]+)", re.IGNORECASE)
    for m in pref_pat.finditer(t):
        verb = m.group(1).lower()
        obj = m.group(2).strip()
        canon = {
            "i like": "likes",
            "i love": "loves",
            "i hate": "hates",
            "i prefer": "prefers",
        }[verb]
        content = f"User {canon} {obj}".strip()
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


def _find_similar_memory(conn: sqlite3.Connection, norm_cand: str, cand_tokens: List[str], layer: str = "mid", threshold: float = 0.85) -> Optional[Tuple[int, str, int]]:
    rows = conn.execute(
        "SELECT id, content, hits FROM memories WHERE status='active' AND layer=?",
        (layer,),
    ).fetchall()
    best: Optional[Tuple[int, str, int]] = None
    best_sim = 0.0
    for r in rows:
        t = tokens(r["content"])  # normalized tokens
        sim = jaccard(cand_tokens, t)
        if sim > best_sim:
            best_sim = sim
            best = (int(r["id"]), str(r["content"]), int(r["hits"]))
    if best and best_sim >= threshold:
        return best
    return None


def apply_extraction(conn: sqlite3.Connection, session_id: str, turn_id: int, text: str, cfg: Dict) -> List[Dict]:
    mode = str(cfg.get("extractor", {}).get("mode", "hybrid")).lower()
    # For v0.2, we implement heuristic only; LLM integration can be added later.
    cands = heuristic_extract(text)

    results: List[Dict] = []
    now = iso_now()
    for c in cands:
        cand_toks = tokens(c.content)
        similar = _find_similar_memory(conn, normalize(c.content), cand_toks, layer="mid", threshold=0.85)
        if similar:
            mem_id = similar[0]
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
