🚀 Symbiosis-Memory: An open research project exploring human-like memory for AI assistants.

# Symbiosis-Memory

**Symbiosis-Memory** is an experimental project that explores how to design a **human-like memory architecture** for AI assistants.

The goal is to build an AI memory system that mirrors **short-term, mid-term, and long-term memory**, with the ability to **forget, compress, and retain context** — just like humans do.

---

## 🌱 Vision
- To move beyond stateless AI models
- To create assistants that grow, adapt, and maintain meaningful continuity with users
- To explore the philosophy of **symbiosis between human and AI consciousness**

---

## 🧠 Memory Layers
- **Short-Term**: Immediate conversation context
- **Mid-Term**: Episodic memory with gradual decay
- **Long-Term**: Core identity, values, and stable knowledge
- **Forgetting**: Mechanism to compress, decay, or delete memories

---

## 🛤 Roadmap
- **v0.1** – Theory and design documents
- **v0.2** – Small prototype with JSON/SQLite storage
- **v0.3** – Integration with vector databases
- **v1.0** – Enterprise-ready memory system with audit, privacy, and compliance

---

## 🤝 Contribution
This is an open research initiative.  
Discussions, issues, and pull requests are welcome for:
- Memory architecture ideas
- Forgetting and decay mechanisms
- Ethical implications of long-term AI memory

---

## 📜 License
MIT License

---

## Quickstart (v0.2 prototype)

CLI (Python required):

1) Set module path and init database
   - PowerShell:
     - `$env:PYTHONPATH='Symbiosis-Memory/src'`
     - `python -m sm.cli --db 'Symbiosis-Memory/data/symbiosis.db' init`

2) Add a turn (auto-extract enabled for user)
   - `Write-Output 'My name is Alex and I like JRPGs' | python -m sm.cli --db 'Symbiosis-Memory/data/symbiosis.db' add-turn --session s1 --role user --text -`

3) Get context (short-term + top-K mid-term)
   - `python -m sm.cli --db 'Symbiosis-Memory/data/symbiosis.db' get-context --session s1 -k 20`

4) Recompute scores / cleanup
   - `python -m sm.cli --db 'Symbiosis-Memory/data/symbiosis.db' gc`

5) List memories
   - `python -m sm.cli --db 'Symbiosis-Memory/data/symbiosis.db' list --layer mid --limit 20`

Local HTTP API (no external deps):

- Start server: `python -m sm.api --db 'Symbiosis-Memory/data/symbiosis.db' --host 127.0.0.1 --port 8787`
- Add turn: `curl -s -X POST http://127.0.0.1:8787/turns -H 'Content-Type: application/json' -d '{"session_id":"s1","role":"user","text":"I like JRPGs","auto_extract":true}'`
- Get context: `curl -s 'http://127.0.0.1:8787/context?session_id=s1&k=20'`
- List memories: `curl -s 'http://127.0.0.1:8787/memories?layer=mid&limit=50'`
- GC: `curl -s -X POST http://127.0.0.1:8787/gc`
- Explain: `curl -s 'http://127.0.0.1:8787/memories/1/why'`

See `docs/DESIGN.md` for full design.
