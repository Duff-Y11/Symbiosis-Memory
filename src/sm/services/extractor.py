import re
import os
import json
import urllib.request
import urllib.error
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

    # Heuristic first
    cands = heuristic_extract(text)

    # Optionally augment with LLM
    if mode in ("llm", "hybrid"):
        llm_cands = []
        try:
            llm_cands = llm_extract(text, cfg)
        except Exception:
            llm_cands = []
        if mode == "llm":
            cands = llm_cands or cands
        else:  # hybrid
            cands = merge_candidates(cands, llm_cands)

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


def merge_candidates(a: List[ExtractCandidate], b: List[ExtractCandidate]) -> List[ExtractCandidate]:
    out: List[ExtractCandidate] = []
    for src in (a, b):
        for c in src:
            if not any(jaccard(tokens(c.content), tokens(o.content)) >= 0.9 for o in out):
                out.append(c)
    return out


def llm_extract(text: str, cfg: Dict) -> List[ExtractCandidate]:
    provider = str(cfg.get("llm", {}).get("provider", "openai")).lower()
    timeout_s = int(cfg.get("llm", {}).get("timeout_s", 8))
    api_key_env = str(cfg.get("llm", {}).get("api_key_env", "OPENAI_API_KEY"))
    api_key = os.environ.get(api_key_env)
    if not api_key:
        return []

    if provider == "openai":
        endpoint = cfg.get("llm", {}).get("endpoint") or "https://api.openai.com/v1/chat/completions"
        model = cfg.get("llm", {}).get("model") or "gpt-4o-mini"
        payload = {
            "model": model,
            "temperature": 0.2,
            # Ask for JSON array output explicitly; avoid json_object constraints for arrays
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract user memories as a JSON array of objects. "
                        "Each object: {content: string, importance: 0|1, tags: [string], action: 'create'|'update'|'archive'}. "
                        "Output ONLY valid JSON array, no prose."
                    ),
                },
                {"role": "user", "content": text},
            ],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            arr = _parse_json_array(content)
            return [_coerce_candidate(obj) for obj in arr]
        except Exception:
            return []
    # Unsupported provider
    return []


def _parse_json_array(s: str) -> List[Dict]:
    # Handle ```json ... ``` wrapping
    m = re.search(r"```json\s*(\[.*?\])\s*```", s, flags=re.DOTALL)
    if m:
        s = m.group(1)
    # If not found, try to find first [...] segment
    if "[" in s and "]" in s:
        start = s.find("[")
        end = s.rfind("]")
        s = s[start : end + 1]
    try:
        val = json.loads(s)
        if isinstance(val, list):
            return val
    except Exception:
        pass
    return []


def _coerce_candidate(obj: Dict) -> ExtractCandidate:
    content = str(obj.get("content", "")).strip()
    if not content:
        # fallback to string item
        if isinstance(obj, str):
            content = obj.strip()
    importance = int(obj.get("importance", 0)) if isinstance(obj, dict) else 0
    if importance not in (0, 1):
        importance = 0
    tags = obj.get("tags") if isinstance(obj, dict) else None
    if not isinstance(tags, list):
        tags = None
    action = obj.get("action") if isinstance(obj, dict) else "create"
    if action not in ("create", "update", "archive"):
        action = "create"
    return ExtractCandidate(content=content, importance=importance, tags=tags, action=action)
