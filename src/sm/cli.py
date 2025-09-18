import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict

from .storage import connect, bootstrap, get_config, set_config, get_default_db_path, iso_now
from .services.orchestrator import garbage_collect
from .services.context import get_context
from .services import extractor


def _open_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    bootstrap(conn)
    return conn


def cmd_init(args: argparse.Namespace) -> None:
    db_path = args.db or get_default_db_path()
    conn = _open_db(db_path)
    cfg = get_config(conn)
    print(json.dumps({"db": db_path, "config": cfg}, ensure_ascii=False, indent=2))


def cmd_add_turn(args: argparse.Namespace) -> None:
    db_path = args.db or get_default_db_path()
    conn = _open_db(db_path)
    text = args.text
    if text == "-":
        text = sys.stdin.read()
    role = args.role
    session_id = args.session
    cur = conn.execute(
        "INSERT INTO turns(session_id, ts, role, text) VALUES(?,?,?,?)",
        (session_id, iso_now(), role, text),
    )
    turn_id = cur.lastrowid

    extracted = []
    cfg = get_config(conn)
    if role == "user" and not args.no_extract:
        extracted = extractor.apply_extraction(conn, session_id, turn_id, text, cfg)
    conn.commit()
    print(json.dumps({"turn_id": turn_id, "extracted_memories": extracted}, ensure_ascii=False, indent=2))


def cmd_get_context(args: argparse.Namespace) -> None:
    db_path = args.db or get_default_db_path()
    conn = _open_db(db_path)
    cfg = get_config(conn)
    ctx = get_context(conn, args.session, cfg, k=args.k)
    print(json.dumps(ctx, ensure_ascii=False, indent=2))


def cmd_remember(args: argparse.Namespace) -> None:
    db_path = args.db or get_default_db_path()
    conn = _open_db(db_path)
    content = args.text
    tags_json = None
    if args.tags:
        tags_json = json.dumps([t.strip() for t in args.tags.split(",") if t.strip()], ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO memories(layer, content, created_at, last_seen_at, hits, score, importance, status, tags_json)"
        " VALUES('mid',?,?,?,?,0.0,?, 'active', ?)",
        (content, iso_now(), None, 0, args.importance, tags_json),
    )
    mem_id = cur.lastrowid
    conn.commit()
    print(json.dumps({"id": mem_id, "content": content}, ensure_ascii=False))


def cmd_list(args: argparse.Namespace) -> None:
    db_path = args.db or get_default_db_path()
    conn = _open_db(db_path)
    layer = args.layer
    q = args.q
    rows = []
    if q:
        # Try FTS, else LIKE
        cfg = get_config(conn)
        if cfg.get("fts", {}).get("enabled", False):
            rows = conn.execute(
                "SELECT m.id, m.layer, m.content, m.hits, m.score, m.importance, m.status "
                "FROM memories m JOIN memories_fts f ON m.id=f.rowid "
                "WHERE m.layer=? AND m.status='active' AND f.memories_fts MATCH ? "
                "ORDER BY m.score DESC, m.id DESC LIMIT ?",
                (layer, q, args.limit),
            ).fetchall()
        else:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT id, layer, content, hits, score, importance, status FROM memories "
                "WHERE layer=? AND status='active' AND content LIKE ? ORDER BY score DESC, id DESC LIMIT ?",
                (layer, like, args.limit),
            ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, layer, content, hits, score, importance, status FROM memories WHERE layer=? AND status='active' ORDER BY score DESC, id DESC LIMIT ?",
            (layer, args.limit),
        ).fetchall()

    out = [dict(r) for r in rows]
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_gc(args: argparse.Namespace) -> None:
    db_path = args.db or get_default_db_path()
    conn = _open_db(db_path)
    cfg = get_config(conn)
    res = garbage_collect(conn, cfg)
    conn.commit()
    print(json.dumps({
        "recomputed": res.recomputed,
        "promoted": res.promoted,
        "deleted": res.deleted,
        "pruned_turns": res.pruned_turns,
    }, ensure_ascii=False, indent=2))


def cmd_explain(args: argparse.Namespace) -> None:
    db_path = args.db or get_default_db_path()
    conn = _open_db(db_path)
    cfg = get_config(conn)
    r = conn.execute(
        "SELECT id, hits, created_at, last_seen_at, importance, score FROM memories WHERE id=?",
        (args.id,),
    ).fetchone()
    if not r:
        print(json.dumps({"error": f"memory {args.id} not found"}, ensure_ascii=False))
        return
    from .services.orchestrator import compute_score

    freq_term, rec_term = compute_score(int(r["hits"]), r["created_at"], r["last_seen_at"], cfg)
    w_imp = float(cfg.get("scoring", {}).get("w_importance", 2.0))
    exp = {
        "id": r["id"],
        "hits": int(r["hits"]),
        "created_at": r["created_at"],
        "last_seen_at": r["last_seen_at"],
        "importance": int(r["importance"]),
        "score": float(r["score"]),
        "breakdown": {
            "freq_term": freq_term,
            "recency_term": rec_term,
            "importance_term": w_imp * int(r["importance"]),
        },
    }
    print(json.dumps(exp, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sm", description="Symbiosis-Memory CLI (v0.2)")
    p.add_argument("--db", help="Path to SQLite DB (default: ./data/symbiosis.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Initialize database and print config")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("add-turn", help="Add a conversation turn (use '-' to read text from stdin)")
    sp.add_argument("--session", required=True, help="Session ID")
    sp.add_argument("--role", choices=["user", "assistant"], default="user")
    sp.add_argument("--text", required=True, help="Turn text or '-' to read stdin")
    sp.add_argument("--no-extract", action="store_true", help="Disable auto extraction for this turn")
    sp.set_defaults(func=cmd_add_turn)

    sp = sub.add_parser("get-context", help="Get context for a session (short-term + top-K mid-term)")
    sp.add_argument("--session", required=True)
    sp.add_argument("-k", type=int, default=20)
    sp.set_defaults(func=cmd_get_context)

    sp = sub.add_parser("remember", help="Manually save a memory to mid-term")
    sp.add_argument("--text", required=True)
    sp.add_argument("--importance", type=int, default=0, choices=[0, 1])
    sp.add_argument("--tags", help="Comma-separated tags")
    sp.set_defaults(func=cmd_remember)

    sp = sub.add_parser("list", help="List memories by layer")
    sp.add_argument("--layer", choices=["mid", "long"], default="mid")
    sp.add_argument("--q", help="Search query (FTS5 if available, else LIKE)")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("gc", help="Run garbage collection: scoring, promotions, cleanup")
    sp.set_defaults(func=cmd_gc)

    sp = sub.add_parser("explain", help="Explain a memory's score breakdown")
    sp.add_argument("--id", type=int, required=True)
    sp.set_defaults(func=cmd_explain)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
