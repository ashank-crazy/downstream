"""Downstream's memory: contracts, ripples, and which thread belongs to which.

Kept as one JSON file so a restart picks up exactly where it left off. The
humans' view of the same facts lives in Switch room documents.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Target:
    room_id: str
    terms: list[str]
    reason: str
    thread_id: str = ""
    status: str = "pending"  # pending | acked | not_affected
    by: str = ""
    sent_at: float = 0.0
    resolved_at: float = 0.0
    nudges: int = 0
    last_nudge: float = 0.0


@dataclass
class Ripple:
    id: int
    origin_room: str
    origin_thread: str
    author: str
    text: str
    summary: str
    engine: str
    status: str = "proposed"  # proposed | sent | closed | skipped
    created_at: float = field(default_factory=time.time)
    targets: dict[str, Target] = field(default_factory=dict)

    def pending(self) -> list[Target]:
        return [t for t in self.targets.values() if t.status == "pending"]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Ripple:
        targets = {k: Target(**v) for k, v in (d.get("targets") or {}).items()}
        return cls(**{**d, "targets": targets})


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.next_id = 1
        self.contracts: dict[str, dict[str, list[str]]] = {}
        self.docs: dict[str, dict[str, str]] = {}
        self.introduced: set[str] = set()
        self.ripples: dict[int, Ripple] = {}
        # "room|thread" -> [ripple id, "origin" | "target"]
        self.threads: dict[str, list[Any]] = {}
        if path.exists():
            self._load()

    def _load(self) -> None:
        d = json.loads(self.path.read_text())
        self.next_id = d.get("next_id", 1)
        self.contracts = d.get("contracts", {})
        self.docs = d.get("docs", {})
        self.introduced = set(d.get("introduced", []))
        self.ripples = {int(k): Ripple.from_dict(v) for k, v in d.get("ripples", {}).items()}
        self.threads = d.get("threads", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "next_id": self.next_id,
            "contracts": self.contracts,
            "docs": self.docs,
            "introduced": sorted(self.introduced),
            "ripples": {str(k): asdict(v) for k, v in self.ripples.items()},
            "threads": self.threads,
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    # ── ripples and threads ──────────────────────────────────────────────

    def new_ripple(self, **kw: Any) -> Ripple:
        r = Ripple(id=self.next_id, **kw)
        self.next_id += 1
        self.ripples[r.id] = r
        self.bind(r.origin_room, r.origin_thread, r.id, "origin")
        return r

    def bind(self, room_id: str, thread_id: str, ripple_id: int, role: str) -> None:
        if thread_id:
            self.threads[f"{room_id}|{thread_id}"] = [ripple_id, role]

    def lookup(self, room_id: str, thread_id: str | None) -> tuple[Ripple, str] | None:
        if not thread_id:
            return None
        hit = self.threads.get(f"{room_id}|{thread_id}")
        if not hit:
            return None
        r = self.ripples.get(int(hit[0]))
        return (r, hit[1]) if r else None

    def involving(self, room_id: str) -> list[Ripple]:
        return [
            r
            for r in self.ripples.values()
            if r.origin_room == room_id or room_id in r.targets
        ]
