import math
from datetime import datetime, timezone
from typing import Dict, Tuple

import sqlite3

from ..storage import iso_now
from ..models import GCResult


def _parse_dt(s: str) -> datetime:
    # Stored as UTC ISO-like string
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)


def _age_days(since_iso: str) -> float:
    try:
        dt = _parse_dt(since_iso)
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


def compute_score(hits: int, created_at: str, last_seen_at: str | None, cfg: Dict) -> float:
    s_cfg = cfg.get("scoring", {})
    w_freq = float(s_cfg.get("w_freq", 1.0))
    w_rec = float(s_cfg.get("w_recency", 1.0))
    w_imp = float(s_cfg.get("w_importance", 2.0))
    lam = float(s_cfg.get("lambda", 0.05))

    # recency measured from last_seen if available, else created_at
    when = last_seen_at or created_at
    age = _age_days(when)
    freq_term = w_freq * math.log1p(max(0, hits))
    rec_term = w_rec * math.exp(-lam * age)
    # importance is applied separately by caller (we need importance value)
    return freq_term, rec_term


def recompute_scores(conn: sqlite3.Connection, cfg: Dict) -> int:
    cur = conn.execute(
        "SELECT id, hits, created_at, last_seen_at, importance FROM memories WHERE status='active'"
    )
    rows = cur.fetchall()
    updated = 0
    w_imp = float(cfg.get("scoring", {}).get("w_importance", 2.0))
    for r in rows:
        freq_term, rec_term = compute_score(
            int(r["hits"]), r["created_at"], r["last_seen_at"], cfg
        )
        score = freq_term + rec_term + w_imp * int(r["importance"])
        conn.execute("UPDATE memories SET score=? WHERE id=?", (score, r["id"]))
        updated += 1
    return updated


def _promote_mid_to_long(conn: sqlite3.Connection, cfg: Dict) -> int:
    promote_hits = int(cfg.get("mid", {}).get("promote_hits", 3))
    cur = conn.execute(
        "SELECT id, hits, last_seen_at FROM memories WHERE layer='mid' AND status='active' AND hits>=?",
        (promote_hits,),
    )
    rows = cur.fetchall()
    promoted = 0
    for r in rows:
        last_seen = r["last_seen_at"]
        recent = _age_days(last_seen or "1970-01-01 00:00:00.000000") <= 7.0
        if recent:
            conn.execute("UPDATE memories SET layer='long' WHERE id=?", (r["id"],))
            promoted += 1
    return promoted


def _delete_stale_mid(conn: sqlite3.Connection, cfg: Dict) -> int:
    threshold = float(cfg.get("mid", {}).get("delete_score_threshold", 0.5))
    max_age = float(cfg.get("mid", {}).get("demote_age_days", 30))
    cur = conn.execute(
        "SELECT id, score, created_at, last_seen_at FROM memories WHERE layer='mid' AND status='active'"
    )
    deleted = 0
    for r in cur.fetchall():
        when = r["last_seen_at"] or r["created_at"]
        if (r["score"] < threshold) or (_age_days(when) > max_age):
            conn.execute("DELETE FROM memory_links WHERE memory_id=?", (r["id"],))
            conn.execute("DELETE FROM memories WHERE id=?", (r["id"],))
            deleted += 1
    return deleted


def _enforce_mid_capacity(conn: sqlite3.Connection, cfg: Dict) -> int:
    cap = int(cfg.get("mid", {}).get("capacity", 500))
    cur = conn.execute(
        "SELECT id FROM memories WHERE layer='mid' AND status='active' ORDER BY score ASC, id ASC"
    )
    ids = [r[0] for r in cur.fetchall()]
    overflow = max(0, len(ids) - cap)
    if overflow <= 0:
        return 0
    to_delete = ids[:overflow]
    for mid in to_delete:
        conn.execute("DELETE FROM memory_links WHERE memory_id=?", (mid,))
        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
    return overflow


def _prune_short_term(conn: sqlite3.Connection, cfg: Dict) -> int:
    keep = int(cfg.get("short_term", {}).get("size", 100))
    pruned = 0
    # Find all sessions
    sessions = [r[0] for r in conn.execute("SELECT DISTINCT session_id FROM turns").fetchall()]
    for sid in sessions:
        cur = conn.execute(
            "SELECT id FROM turns WHERE session_id=? ORDER BY ts DESC, id DESC",
            (sid,),
        )
        ids = [r[0] for r in cur.fetchall()]
        overflow = max(0, len(ids) - keep)
        if overflow > 0:
            to_delete = ids[-overflow:]
            conn.executemany("DELETE FROM turns WHERE id=?", [(i,) for i in to_delete])
            pruned += overflow
    return pruned


def garbage_collect(conn: sqlite3.Connection, cfg: Dict) -> GCResult:
    recomputed = recompute_scores(conn, cfg)
    promoted = _promote_mid_to_long(conn, cfg)
    deleted = _delete_stale_mid(conn, cfg)
    deleted += _enforce_mid_capacity(conn, cfg)
    pruned_turns = _prune_short_term(conn, cfg)
    return GCResult(recomputed=recomputed, promoted=promoted, deleted=deleted, pruned_turns=pruned_turns)


def touch_memory(conn: sqlite3.Connection, mem_id: int) -> None:
    conn.execute("UPDATE memories SET last_seen_at=? WHERE id=?", (iso_now(), mem_id))

