from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Turn:
    id: int
    session_id: str
    ts: str
    role: str
    text: str


@dataclass
class Memory:
    id: int
    layer: str  # 'mid' | 'long'
    content: str
    created_at: str
    last_seen_at: Optional[str]
    hits: int
    score: float
    importance: int
    status: str  # 'active' | 'archived' | 'deleted'
    tags_json: Optional[str]


@dataclass
class GCResult:
    recomputed: int
    promoted: int
    deleted: int
    pruned_turns: int


@dataclass
class ExtractCandidate:
    content: str
    importance: int = 0
    tags: Optional[List[str]] = None
    action: str = "create"  # create | update | archive


Config = Dict[str, Any]

