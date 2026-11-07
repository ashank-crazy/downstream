"""Room contracts: what a room owns, and what it depends on.

A contract is CODEOWNERS for a conversation. Each room states the things it is
responsible for (`owns`) and the things other teams' changes can break for it
(`depends`). Downstream reads them from the room's instructions or description
the first time it joins, lets people edit them by talking to it, and mirrors
the result into a room document everyone can read.

    Owns: orders-api, order_id, checkout v2
    Depends on: user_id, auth tokens, payments webhook
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LINE = re.compile(
    r"^\s*[-*]?\s*(owns|we own|depends on|depends|we depend on|uses)\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def _split_terms(raw: str) -> list[str]:
    parts = re.split(r"[,;\n]|\band\b", raw)
    out: list[str] = []
    for p in parts:
        t = p.strip().strip("`'\".").strip()
        if t and len(t) <= 60 and t.lower() not in {"nothing", "none", "n/a"}:
            out.append(t)
    return out


def normalize(text: str) -> str:
    """Lower-case and fold the separators people use interchangeably, so
    `user_id`, `user-id` and `user id` all compare equal."""
    text = text.lower()
    text = re.sub(r"[`*_\-/.:]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def term_in(term: str, text_norm: str) -> bool:
    t = normalize(term)
    if not t:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}s?(?![a-z0-9])", text_norm) is not None


@dataclass
class Contract:
    owns: list[str] = field(default_factory=list)
    depends: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str | None) -> Contract:
        c = cls()
        for m in _LINE.finditer(text or ""):
            key = m.group(1).lower()
            terms = _split_terms(m.group(2))
            if key in {"owns", "we own"}:
                c.add_owns(terms)
            else:
                c.add_depends(terms)
        return c

    def add_owns(self, terms: list[str]) -> list[str]:
        return _add(self.owns, terms)

    def add_depends(self, terms: list[str]) -> list[str]:
        return _add(self.depends, terms)

    def remove(self, terms: list[str]) -> list[str]:
        gone = []
        for t in terms:
            for lst in (self.owns, self.depends):
                for existing in list(lst):
                    if normalize(existing) == normalize(t):
                        lst.remove(existing)
                        gone.append(existing)
        return gone

    def is_empty(self) -> bool:
        return not self.owns and not self.depends

    def render(self) -> str:
        owns = ", ".join(f"`{t}`" for t in self.owns) or "_nothing yet_"
        deps = ", ".join(f"`{t}`" for t in self.depends) or "_nothing yet_"
        return f"**Owns:** {owns}\n**Depends on:** {deps}"

    def to_dict(self) -> dict[str, list[str]]:
        return {"owns": list(self.owns), "depends": list(self.depends)}

    @classmethod
    def from_dict(cls, d: dict | None) -> Contract:
        d = d or {}
        return cls(owns=list(d.get("owns") or []), depends=list(d.get("depends") or []))


def _add(lst: list[str], terms: list[str]) -> list[str]:
    added = []
    have = {normalize(t) for t in lst}
    for t in terms:
        if normalize(t) and normalize(t) not in have:
            lst.append(t)
            have.add(normalize(t))
            added.append(t)
    return added
