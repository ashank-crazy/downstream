"""Deciding whether a message is a change other rooms need to hear about.

Two analyzers, same answer shape:

* `RuleAnalyzer` (default, no API key, deterministic): a change verb plus a
  term another room's contract depends on. Every decision it makes can be
  explained in one line, which is what you want from something that posts
  into other teams' channels.
* `ClaudeAnalyzer` (when ANTHROPIC_API_KEY is set): asks Claude to judge the
  same question semantically ("auth is moving to OAuth2" reaches a room that
  depends on `login tokens`), and falls back to the rules on any failure.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from .contracts import Contract, normalize, term_in

log = logging.getLogger("downstream.detect")

CHANGE = re.compile(
    r"\b("
    r"renam\w*|deprecat\w*|remov\w*|drop(?:s|ping|ped)?|delet\w*|migrat\w*|"
    r"chang\w*|break\w*|sunset\w*|shut(?:ting)? ?down|retir\w*|replac\w*|"
    r"rotat\w*|freez\w*|delay\w*|slip\w*|postpon\w*|moving|move[sd]?|"
    r"upgrad\w*|downtime|maintenance|cut ?over|switch(?:ing)? to|"
    r"rate[ -]?limit\w*|no longer|stop(?:ping)? (?:support\w*|serving|sending)|"
    r"turn(?:ing)? off|disabl\w*|new version|v\d+(?:\.\d+)*|reschedul\w*|"
    r"push(?:ed|ing)? (?:back|out)"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class Impact:
    room_id: str
    terms: list[str]
    reason: str


@dataclass
class Verdict:
    is_change: bool
    summary: str = ""
    impacts: list[Impact] = field(default_factory=list)
    change_words: list[str] = field(default_factory=list)
    engine: str = "rules"


@dataclass
class RoomView:
    room_id: str
    name: str
    contract: Contract


class RuleAnalyzer:
    name = "rules"

    def analyze(
        self, text: str, origin: RoomView, rooms: list[RoomView], *, force: bool = False
    ) -> Verdict:
        words = sorted({m.group(0).lower() for m in CHANGE.finditer(text)})
        if not words and not force:
            return Verdict(False)
        norm = normalize(text)
        owned_here = [t for t in origin.contract.owns if term_in(t, norm)]
        impacts: list[Impact] = []
        for room in rooms:
            if room.room_id == origin.room_id:
                continue
            hit = [t for t in room.contract.depends if term_in(t, norm)]
            if not hit:
                continue
            listed = ", ".join(f"`{t}`" for t in hit)
            reason = f"depends on {listed}"
            if any(normalize(t) in {normalize(o) for o in owned_here} for t in hit):
                reason += f" (owned by #{origin.name})"
            impacts.append(Impact(room.room_id, hit, reason))
        return Verdict(bool(impacts), _first_sentence(text), impacts, words, "rules")


class ClaudeAnalyzer:
    """Semantic matching with Claude, using structured output so the answer
    is always the JSON shape below. Rules remain the fallback."""

    name = "claude"
    SCHEMA = {
        "type": "object",
        "properties": {
            "is_change": {"type": "boolean"},
            "summary": {"type": "string"},
            "impacted": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "room": {"type": "string"},
                        "terms": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                    "required": ["room", "terms", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["is_change", "summary", "impacted"],
        "additionalProperties": False,
    }
    SYSTEM = (
        "You are Downstream, a teammate that watches a company's team chat rooms. "
        "Each room has a contract listing what it owns and what it depends on. "
        "Decide whether a message announces a change (rename, removal, migration, "
        "deadline shift, outage, behaviour change) that would affect another room's "
        "dependencies. Only list rooms whose contract plausibly depends on what is "
        "changing, never the room the message came from. A question, a joke, or a "
        "plan with no concrete change is not a change. summary: one plain sentence "
        "saying what changes and when, written for the affected team."
    )

    def __init__(self, model: str | None = None) -> None:
        import anthropic  # optional dependency

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("DOWNSTREAM_MODEL", "claude-opus-5-5")
        self.rules = RuleAnalyzer()

    def analyze(
        self, text: str, origin: RoomView, rooms: list[RoomView], *, force: bool = False
    ) -> Verdict:
        fallback = self.rules.analyze(text, origin, rooms, force=force)
        try:
            return self._ask(text, origin, rooms) or fallback
        except Exception as exc:  # network, auth, refusal: rules still work
            log.warning("Claude analysis failed, using rules: %s", exc)
            return fallback

    def _ask(self, text: str, origin: RoomView, rooms: list[RoomView]) -> Verdict | None:
        directory = "\n".join(
            f"- #{r.name}: owns {', '.join(r.contract.owns) or 'nothing listed'}; "
            f"depends on {', '.join(r.contract.depends) or 'nothing listed'}"
            for r in rooms
        )
        prompt = (
            f"Rooms and their contracts:\n{directory}\n\n"
            f"Message posted in #{origin.name}:\n<message>\n{text}\n</message>"
        )
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=2000,
            system=self.SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": self.SCHEMA},
            },
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return None
        raw = next(b.text for b in response.content if b.type == "text")
        data = json.loads(raw)
        by_name = {r.name.lower().lstrip("#"): r for r in rooms}
        impacts = []
        for item in data.get("impacted", []):
            room = by_name.get(str(item.get("room", "")).lower().lstrip("#"))
            if room is None or room.room_id == origin.room_id:
                continue
            impacts.append(Impact(room.room_id, list(item.get("terms") or []), item["reason"]))
        is_change = bool(data.get("is_change")) and bool(impacts)
        return Verdict(is_change, data.get("summary", ""), impacts, [], "claude")


def make_analyzer() -> RuleAnalyzer | ClaudeAnalyzer:
    if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("DOWNSTREAM_ENGINE") != "rules":
        try:
            return ClaudeAnalyzer()
        except ImportError:
            log.warning("ANTHROPIC_API_KEY is set but `anthropic` is not installed; "
                        "install with `uv sync --extra claude`. Using rules.")
    return RuleAnalyzer()


def _first_sentence(text: str, limit: int = 220) -> str:
    text = re.sub(r"@\S+", "", text).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text, re.S)
    s = (m.group(1) if m else text).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"
