import argparse
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import re

import sqlite3

from .storage import connect, bootstrap, get_config, get_default_db_path, iso_now
from .services.context import get_context as svc_get_context
from .services.orchestrator import garbage_collect, compute_score
from .services import extractor


G_CONN: sqlite3.Connection | None = None


def json_response(handler: BaseHTTPRequestHandler, code: int, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: BaseHTTPRequestHandler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except Exception:
        length = 0
    data = handler.rfile.read(length) if length > 0 else b""
    if not data:
        return {}
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quieter logging; override if needed
        return

    @property
    def conn(self) -> sqlite3.Connection:
        assert G_CONN is not None
        return G_CONN

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/turns":
            body = read_json(self)
            if body is None:
                return json_response(self, 400, {"error": "invalid JSON"})
            session_id = body.get("session_id")
            role = body.get("role", "user")
            text = body.get("text", "")
            auto_extract = bool(body.get("auto_extract", True))
            if not session_id or role not in ("user", "assistant") or not isinstance(text, str):
                return json_response(self, 400, {"error": "missing/invalid fields"})
            cur = self.conn.execute(
                "INSERT INTO turns(session_id, ts, role, text) VALUES(?,?,?,?)",
                (session_id, iso_now(), role, text),
            )
            turn_id = cur.lastrowid
            extracted = []
            cfg = get_config(self.conn)
            if role == "user" and auto_extract:
                extracted = extractor.apply_extraction(self.conn, session_id, turn_id, text, cfg)
            self.conn.commit()
            return json_response(self, 200, {"turn_id": turn_id, "extracted_memories": extracted})

        elif parsed.path == "/memories":
            body = read_json(self)
            if body is None:
                return json_response(self, 400, {"error": "invalid JSON"})
            content = body.get("content")
            importance = int(body.get("importance", 0))
            tags = body.get("tags")
            tags_json = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else None
            if not isinstance(content, str) or not content:
                return json_response(self, 400, {"error": "content required"})
            cur = self.conn.execute(
                "INSERT INTO memories(layer, content, created_at, last_seen_at, hits, score, importance, status, tags_json)"
                " VALUES('mid',?,?,?,?,0.0,?, 'active', ?)",
                (content, iso_now(), None, 0, importance, tags_json),
            )
            mem_id = cur.lastrowid
            self.conn.commit()
            return json_response(self, 200, {"id": mem_id, "content": content})

        elif parsed.path == "/gc":
            cfg = get_config(self.conn)
            res = garbage_collect(self.conn, cfg)
            self.conn.commit()
            return json_response(
                self,
                200,
                {
                    "recomputed": res.recomputed,
                    "promoted": res.promoted,
                    "deleted": res.deleted,
                    "pruned_turns": res.pruned_turns,
                },
            )

        # PATCH via POST override: /memories/{id}?_method=PATCH also allowed
        m = re.fullmatch(r"/memories/(\d+)", parsed.path)
        if m and (self.command == "POST" and parse_qs(parsed.query).get("_method", [""])[0].upper() == "PATCH"):
            return self._handle_patch_memory(int(m.group(1)))

        return json_response(self, 404, {"error": "not found"})

    def do_PATCH(self):
        parsed = urlparse(self.path)
        m = re.fullmatch(r"/memories/(\d+)", parsed.path)
        if m:
            return self._handle_patch_memory(int(m.group(1)))
        return json_response(self, 404, {"error": "not found"})

    def _handle_patch_memory(self, mem_id: int):
        body = read_json(self)
        if body is None:
            return json_response(self, 400, {"error": "invalid JSON"})
        fields = {}
        if "status" in body:
            fields["status"] = body["status"]
        if "content" in body:
            fields["content"] = body["content"]
        if "tags" in body:
            fields["tags_json"] = json.dumps(body["tags"], ensure_ascii=False) if isinstance(body["tags"], list) else None
        if not fields:
            return json_response(self, 400, {"error": "no updatable fields"})
        sets = ",".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [mem_id]
        self.conn.execute(f"UPDATE memories SET {sets} WHERE id=?", vals)
        self.conn.commit()
        return json_response(self, 200, {"ok": True, "id": mem_id})

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/context":
            session_id = qs.get("session_id", [None])[0]
            if not session_id:
                return json_response(self, 400, {"error": "session_id required"})
            try:
                k = int(qs.get("k", ["20"])[0])
            except Exception:
                k = 20
            cfg = get_config(self.conn)
            ctx = svc_get_context(self.conn, session_id, cfg, k=k)
            return json_response(self, 200, ctx)

        if parsed.path == "/memories":
            layer = qs.get("layer", ["mid"])[0]
            q = qs.get("q", [None])[0]
            try:
                limit = int(qs.get("limit", ["50"])[0])
            except Exception:
                limit = 50
            rows = []
            if q:
                cfg = get_config(self.conn)
                if cfg.get("fts", {}).get("enabled", False):
                    rows = self.conn.execute(
                        "SELECT m.id, m.layer, m.content, m.hits, m.score, m.importance, m.status "
                        "FROM memories m JOIN memories_fts f ON m.id=f.rowid "
                        "WHERE m.layer=? AND m.status='active' AND f.memories_fts MATCH ? "
                        "ORDER BY m.score DESC, m.id DESC LIMIT ?",
                        (layer, q, limit),
                    ).fetchall()
                else:
                    like = f"%{q}%"
                    rows = self.conn.execute(
                        "SELECT id, layer, content, hits, score, importance, status FROM memories "
                        "WHERE layer=? AND status='active' AND content LIKE ? ORDER BY score DESC, id DESC LIMIT ?",
                        (layer, like, limit),
                    ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, layer, content, hits, score, importance, status FROM memories WHERE layer=? AND status='active' ORDER BY score DESC, id DESC LIMIT ?",
                    (layer, limit),
                ).fetchall()
            out = [dict(r) for r in rows]
            return json_response(self, 200, out)

        m = re.fullmatch(r"/memories/(\d+)/why", parsed.path)
        if m:
            mem_id = int(m.group(1))
            r = self.conn.execute(
                "SELECT id, hits, created_at, last_seen_at, importance, score FROM memories WHERE id=?",
                (mem_id,),
            ).fetchone()
            if not r:
                return json_response(self, 404, {"error": "not found"})
            cfg = get_config(self.conn)
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
            return json_response(self, 200, exp)

        return json_response(self, 404, {"error": "not found"})


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Symbiosis-Memory HTTP API (no external deps)")
    parser.add_argument("--db", help="Path to SQLite DB (default: ./data/symbiosis.db)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)

    db_path = args.db or get_default_db_path()
    global G_CONN
    G_CONN = connect(db_path)
    bootstrap(G_CONN)

    httpd = HTTPServer((args.host, args.port), Handler)
    print(f"[sm.api] listening on http://{args.host}:{args.port} using db={db_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if G_CONN:
            G_CONN.close()


if __name__ == "__main__":
    main()

