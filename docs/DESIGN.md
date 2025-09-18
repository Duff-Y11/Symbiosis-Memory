# Symbiosis-Memory v0.2 — Design

This document specifies the minimal yet extensible design for Symbiosis‑Memory v0.2.
The goal is to deliver a working memory system with short-/mid-/long‑term layers,
time‑decay forgetting, optimistic extraction, and both CLI and local HTTP API interfaces.

## Goals
- Working memory layers: short (FIFO buffer), mid (promote/demote/delete), long (stable).
- Forgetting policies: time decay, capacity cleanup, and manual forget.
- Extraction: optimistic heuristic rules with optional LLM enhancement (hybrid mode).
- Interfaces: CLI + local HTTP API backed by the same core services.
- Storage: SQLite with FTS5 for full‑text search over memories.

## Non‑Goals (v0.2)
- Semantic vector search and external vector DBs (planned v0.3).
- Multi‑user tenancy, permissions, and audit trail (planned v0.4).
- Complex summarization/compaction pipelines (early versions in v0.3).

---

## Architecture

Components (single process):
- Core Services
  - MemoryOrchestrator: scoring, promotion/demotion, short‑term buffer enforcement.
  - ContextService: builds conversation context from short‑term + top‑K mid‑term.
  - GCService: decay scoring pass and cleanup (delete/archive) and promotions.
  - Extractor: pluggable heuristic and optional LLM augmentation; dedupe/merge.
- Interfaces
  - CLI: operational commands (add turn, remember, list, gc, explain).
  - HTTP API (local): programmatic access for apps/tests.
- Storage
  - SQLite (WAL mode recommended), with FTS5 virtual table for full‑text querying.

Data flow (typical):
1) POST /turns or `sm add-turn` inserts a user turn → optional auto extract.
2) Extractor emits candidate memories → write to mid‑term (or merge into existing).
3) Context requests return short‑term window + top‑K mid‑term by score.
4) GC pass updates scores (time decay), promotes/demotes, and cleans capacity.

---

## Data Model & Schema

Tables (singular user for v0.2):

```sql
-- Turn-level short-term context; short-term window is enforced per session.
CREATE TABLE IF NOT EXISTS turns (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT NOT NULL,
  ts           DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
  role         TEXT NOT NULL CHECK (role IN ('user','assistant')),
  text         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session_ts ON turns(session_id, ts);

-- Consolidated memories across layers (mid/long). Short-term is derived from turns.
CREATE TABLE IF NOT EXISTS memories (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  layer         TEXT NOT NULL CHECK (layer IN ('mid','long')),
  content       TEXT NOT NULL,
  created_at    DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
  last_seen_at  DATETIME,
  hits          INTEGER NOT NULL DEFAULT 0,
  score         REAL NOT NULL DEFAULT 0.0,
  importance    INTEGER NOT NULL DEFAULT 0, -- 0/1 flag, boosts score
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived','deleted')),
  tags_json     TEXT -- JSON array of strings
);
CREATE INDEX IF NOT EXISTS idx_mem_layer_score ON memories(layer, score DESC);
CREATE INDEX IF NOT EXISTS idx_mem_last_seen ON memories(last_seen_at);

-- Traceability from memories to their source turns.
CREATE TABLE IF NOT EXISTS memory_links (
  memory_id   INTEGER NOT NULL,
  turn_id     INTEGER NOT NULL,
  reason      TEXT,
  PRIMARY KEY (memory_id, turn_id),
  FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id)   REFERENCES turns(id)    ON DELETE CASCADE
);

-- Meta/config + schema versioning.
CREATE TABLE IF NOT EXISTS meta (
  key          TEXT PRIMARY KEY,
  value_json   TEXT
);

-- FTS5: full-text search over memories.content
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content,
  content='memories',
  content_rowid='id',
  tokenize='unicode61'
);

-- Keep FTS in sync with base table
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
```

Notes
- Short‑term is not duplicated in `memories`; it is windowed from `turns` by session.
- For deployments lacking FTS5 support, the system will gracefully degrade to LIKE search.

---

## Scoring & Policies

Score combines frequency, recency, and importance:

```
score = w_freq * ln(1 + hits)
      + w_recency * exp(-lambda * age_days)
      + w_importance * importance

defaults: w_freq=1.0, w_recency=1.0, w_importance=2.0, lambda=0.05
```

Policies
- Promote mid → long: hits ≥ 3 AND last_seen within 7 days.
- Delete/Archive mid: score < 0.5 OR age_days > 30 (delete in v0.2; archive optional).
- Short‑term window: keep last N turns per session (default N=100).

GC pass
1) Recompute score for all active memories.
2) Promote eligible mid‑term to long‑term.
3) Remove low‑score/expired mid‑term to enforce capacity.

---

## Optimistic Extraction

Trigger: on new user turn (unless `no_extract=true`).

Heuristic rules (examples)
- Identity/preferences: `\b(I am|I'm|my name is|I like|prefer|love|hate)\b`
- State changes: `\b(no longer|don’t anymore|changed to|now)\b`
- Time/place/events: simple date, city, and plan patterns (regex + keywords).

Dedup & merge
- Normalize (lowercase, trim punctuation), compare by Jaccard token overlap.
- If similar to an existing memory: increment hits, update last_seen_at; otherwise insert new.

Conflicts
- When a new fact contradicts an old one, mark old as `archived` and insert the new one; add `memory_links.reason='conflict'` from new memory to source turn.

LLM augmentation (optional)
- Configured via `extractor.mode=heuristic|llm|hybrid`.
- When `llm|hybrid` and API is available, send the turn text; expect JSON:

```json
[
  { "content": "User likes JRPGs", "importance": 1, "tags": ["interests"], "action": "create" },
  { "content": "Old preference obsolete", "action": "archive" }
]
```

- Timeouts, rate limits, or errors fall back to heuristic extraction; writes must not be blocked by LLM failures.

---

## HTTP API (Local)

Base URL: `http://127.0.0.1:8787`

- POST `/turns`
  - Body: `{ session_id, role: 'user'|'assistant', text, auto_extract=true }`
  - Returns: `{ turn_id, extracted_memories: [ {id?, content, action} ] }`

- GET `/context?session_id=s1&k=20`
  - Returns: `{ short_term: [turn...], mid_term: [memory...], long_term_hint?: [] }`

- POST `/memories`
  - Body: `{ content, importance=0, tags=[] }` → create mid‑term memory.

- GET `/memories?layer=mid&q=&limit=50`
  - FTS query if available; otherwise LIKE; ordered by score DESC by default.

- PATCH `/memories/{id}`
  - Body: partial updates `{ status|content|tags }`.

- POST `/gc`
  - Triggers scoring, promotion, and cleanup; returns summary stats.

- GET `/memories/{id}/why`
  - Returns score breakdown, hits, age_days, rules matched, and last decisions.

Sample curl
```bash
curl -s -X POST http://127.0.0.1:8787/turns \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","role":"user","text":"I like JRPGs and my name is Alex","auto_extract":true}'
```

---

## CLI (Proposed)

Executable name: `sm`

- `sm add-turn --session s1 --role user --text "..." [--no-extract]`
- `sm get-context --session s1 --k 20`
- `sm remember --text "..." [--importance 0|1] [--tags "a,b"]`
- `sm list --layer mid --sort score`
- `sm forget --id 123` or `--text "..."`
- `sm gc`
- `sm explain --id 123`

---

## Configuration

Stored in `meta` table (key=`config`) or `config.json` alongside the DB.

```json
{
  "short_term": { "size": 100 },
  "mid": { "capacity": 500, "promote_hits": 3, "demote_age_days": 30 },
  "scoring": { "w_freq": 1.0, "w_recency": 1.0, "w_importance": 2.0, "lambda": 0.05 },
  "extractor": { "mode": "hybrid" },
  "llm": { "provider": "openai", "endpoint": "", "model": "", "api_key_env": "OPENAI_API_KEY", "timeout_s": 8 },
  "fts": { "enabled": true }
}
```

FTS5 capability check
- On startup, run `PRAGMA compile_options;` to detect `FTS5`. If missing, set `fts.enabled=false` and fall back to LIKE.

---

## Observability & Explainability

Metrics (returned by `/gc` and exposed via CLI `sm stats` in later versions)
- counts: created, promoted, archived, deleted
- averages: score, age_days
- extraction: heuristic vs LLM success rate and latency (if enabled)

Explain (`/memories/{id}/why`)
- Show: hits, age_days, score decomposition (each weight term), last_seen_at, matched rules, last actions (promoted/archived/deleted?)

---

## Failure, Safety, Privacy

- SQLite transactions: writes use `BEGIN IMMEDIATE` to avoid races; single‑process server.
- WAL mode recommended for reliability. Pragmas: `journal_mode=WAL`, `synchronous=NORMAL`.
- LLM failures are non‑blocking; always fall back to heuristics.
- PII tagging: basic detectors (email/phone/address) tag memories with `pii` for optional filtering.
- Manual `forget` performs hard delete in v0.2 (no audit trail yet).

---

## Testing Strategy

- Unit tests: scoring function, extractor heuristics, FTS queries (if available), GC policies.
- Integration: end‑to‑end flow — add turn → extract → context → gc → promotion.
- API tests: route validation, JSON schemas, idempotency for dedupe/merge.

---

## Milestones (Implementation Order)

1) Schema + config loader + storage adapter
2) Core services: scoring, GC, short‑term window, promotion/demotion
3) CLI minimal set: add-turn, get-context, remember, list, gc, explain
4) HTTP API routes and basic tests
5) Heuristic extractor + dedupe/merge; link sources
6) LLM augmentation (provider plug‑in, configurable)
7) FTS5 integration for `/memories?q=` and CLI search
8) Docs and polishing

---

## Notes on Deployment

- Single binary/process (Python app). Suggested layout:

```
src/
  sm/
    __init__.py
    storage.py        # sqlite + migrations + FTS helpers
    models.py         # dataclasses / pydantic models
    services/
      orchestrator.py # scoring + promotions + gc
      context.py
      extractor.py    # heuristic + llm plug‑in
    cli.py            # click/argparse CLI
    api.py            # FastAPI/Flask app (port 8787)
```

- DB location: `./data/symbiosis.db` (configurable). Server starts DB if missing and runs bootstrap SQL above.

---

## Future Work (v0.3+)

- Semantic retrieval with vector DB (Weaviate/Milvus/etc.).
- Summarization/compaction pipelines for mid‑term overflow.
- Multi‑user tenancy, permissions, audit trail and compliance tooling.
- Rich conflict resolution and provenance graphs.
