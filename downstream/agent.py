"""Downstream: the teammate who tells the other teams.

It sits in every team's Switch room. When someone announces a change that
another room depends on, it asks the author (in the thread) whether to tell
them. On "send" it briefs each affected room, then works as the liaison:
questions asked in a briefed room come back to the author's thread, answers go
back out, and it keeps chasing until every affected room has acknowledged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .contracts import Contract, _split_terms, normalize
from .detect import RoomView, Verdict, make_analyzer
from .ledger import Ripple, State, Target
from .switch import Event, SwitchAgent, SwitchError

log = logging.getLogger("downstream.agent")

APPROVE = re.compile(r"^\s*(send|yes|yep|yeah|y|go|go ahead|ship it|notify|do it|ok|okay|sure|please do|👍|:\+1:|:thumbsup:)\b", re.I)
SKIP = re.compile(r"^\s*(skip|no|nope|ignore|not now|false alarm|cancel|don'?t)\b", re.I)
ACK = re.compile(r"^\s*(ack|acked|acknowledged|ok|okay|got it|noted|done|handled|fixed|on it|we'?re good|all good|sounds good|👍|✅|:\+1:|:thumbsup:|:white_check_mark:)\b", re.I)
NOT_US = re.compile(r"\b(not us|not affected|doesn'?t affect us|does not affect us|unaffected|not relevant|n/?a for us)\b", re.I)

HELP = """**Downstream** tells the other teams when you change something they depend on.

• Announce changes as usual. If another room depends on what you're changing, I'll ask you (in the thread) whether to brief them.
• `@downstream contract` shows what this room owns and depends on.
• `@downstream we own orders-api, order_id` / `@downstream we depend on user_id` edits it. `@downstream forget user_id` removes a term.
• `@downstream map` shows who depends on whom across every room I'm in.
• `@downstream status` lists the open ripples that involve this room.
• `@downstream <message>` makes me check a message on purpose."""


@dataclass
class Room:
    room_id: str
    name: str
    description: str = ""
    instructions: str = ""
    users: list[str] = field(default_factory=list)


class Downstream:
    def __init__(self, switch: SwitchAgent, state: State, name: str = "downstream") -> None:
        self.sw = switch
        self.state = state
        self.name = name
        self.analyzer = make_analyzer()
        self.rooms: dict[str, Room] = {}
        self.nudge_after = float(os.environ.get("DOWNSTREAM_NUDGE_SECONDS", "600"))
        self.max_nudges = int(os.environ.get("DOWNSTREAM_MAX_NUDGES", "2"))
        self._lock = asyncio.Lock()
        self._refreshing: asyncio.Task | None = None
        self._mention = re.compile(rf"@{re.escape(name)}\b[:,]?", re.I)

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        log.info("Downstream starting (engine: %s)", self.analyzer.name)
        loops = [asyncio.create_task(self._nudge_loop()), asyncio.create_task(self._discover_loop())]
        try:
            await self.sw.run(self.on_event)
        finally:
            for t in loops:
                t.cancel()

    async def _discover_loop(self) -> None:
        """Notice rooms Downstream has been invited to since it started. An
        all-rooms stream delivers their messages but does not announce them, so
        a quiet new room would otherwise go unintroduced."""
        while True:
            await asyncio.sleep(float(os.environ.get("DOWNSTREAM_DISCOVER_SECONDS", "15")))
            try:
                listed = await self.sw.list_rooms()
            except SwitchError as exc:
                log.debug("discovery: %s", exc)
                continue
            for r in listed:
                if r["room_id"] not in self.rooms:
                    async with self._lock:
                        await self.load_room(r["room_id"], r.get("name"), r.get("description"))
                    log.info("joined #%s", self.rooms[r["room_id"]].name)
            # Archived, or Downstream was removed: stop counting it as a room
            # that can be affected. Its contract stays in case it comes back.
            current = {r["room_id"] for r in listed}
            for rid in [rid for rid in self.rooms if rid not in current]:
                log.info("left #%s", self.rooms.pop(rid).name)

    async def on_event(self, ev: Event) -> None:
        if ev.kind in ("connection_state", "subscription_changed"):
            if self._refreshing is None or self._refreshing.done():
                self._refreshing = asyncio.create_task(self.refresh_rooms())
            return
        if ev.kind == "message" and ev.room_id:
            async with self._lock:
                await self.on_message(ev.room_id, ev.payload)

    async def refresh_rooms(self) -> None:
        try:
            listed = await self.sw.list_rooms()
        except SwitchError as exc:
            log.warning("could not list rooms: %s", exc)
            return
        for r in listed:
            await self.load_room(r["room_id"], r.get("name"), r.get("description"))
        log.info("watching %d rooms: %s", len(self.rooms),
                 ", ".join(f"#{r.name}" for r in self.rooms.values()))

    async def load_room(self, room_id: str, name: str | None = None,
                        description: str | None = None) -> Room:
        detail: dict[str, Any] = {}
        try:
            detail = await self.sw.room_detail(room_id) or {}
        except SwitchError as exc:
            log.warning("no detail for %s: %s", room_id, exc)
        room = Room(
            room_id=room_id,
            name=detail.get("name") or name or room_id[:8],
            description=detail.get("description") or description or "",
            instructions=detail.get("instructions") or "",
            users=list(detail.get("connected_user_names") or []),
        )
        self.rooms[room_id] = room
        if room_id not in self.state.contracts:
            c = Contract.parse(f"{room.instructions}\n{room.description}")
            self.state.contracts[room_id] = c.to_dict()
            self.state.save()
        if room_id not in self.state.introduced:
            self.state.introduced.add(room_id)
            self.state.save()
            await self._introduce(room)
        return room

    async def room(self, room_id: str) -> Room:
        return self.rooms.get(room_id) or await self.load_room(room_id)

    def contract(self, room_id: str) -> Contract:
        return Contract.from_dict(self.state.contracts.get(room_id))

    def set_contract(self, room_id: str, c: Contract) -> None:
        self.state.contracts[room_id] = c.to_dict()
        self.state.save()

    # ── messages ──────────────────────────────────────────────────────────

    def _is_self(self, p: dict[str, Any]) -> bool:
        if p.get("sender_kind") == "platform":
            # Switch's own voice. A kickoff it posts for a person carries
            # `on_behalf_of`, and is that person talking; anything else isn't.
            return not p.get("on_behalf_of")
        names = {str(p.get("sender_name") or "").lower(), str(p.get("sender") or "").lower()}
        return any(n == self.name or n.startswith(f"@{self.name}") or
                   f"switch-agent-{self.name}" in n for n in names) or \
            str(p.get("sender_name") or "").lower() in {self.name, self.name.title()}

    async def on_message(self, room_id: str, p: dict[str, Any]) -> None:
        await self.room(room_id)  # a room we have not met yet: learn it first
        if self._is_self(p):
            return
        body = (p.get("body") or "").strip()
        if not body or body.startswith("!"):
            return  # Switch room commands are Switch's to answer
        msg_id = p.get("message_id") or ""
        thread = p.get("thread_id") or msg_id
        who = p.get("sender_name") or "someone"
        if p.get("sender_kind") == "platform" and p.get("on_behalf_of"):
            who = str(p["on_behalf_of"]).lstrip("@").split(":")[0]
        addressed = bool(p.get("addressed")) or bool(self._mention.search(body))
        text = self._mention.sub("", body).strip()

        hit = self.state.lookup(room_id, p.get("thread_id"))
        if hit:
            ripple, role = hit
            if role == "origin":
                await self._in_origin_thread(ripple, who, text, addressed)
            else:
                await self._in_target_thread(ripple, room_id, who, text)
            return

        if addressed:
            await self._command(room_id, thread, who, text)
            return

        await self._screen(room_id, thread, who, body, force=False)

    async def _screen(self, room_id: str, thread: str, who: str, text: str,
                      *, force: bool) -> bool:
        origin = await self.room(room_id)
        views = [RoomView(r.room_id, r.name, self.contract(r.room_id)) for r in self.rooms.values()]
        verdict: Verdict = await asyncio.to_thread(
            self.analyzer.analyze, text, RoomView(room_id, origin.name, self.contract(room_id)),
            views, force=force,
        )
        if not verdict.is_change:
            return False
        ripple = self.state.new_ripple(
            origin_room=room_id, origin_thread=thread, author=who, text=text,
            summary=verdict.summary, engine=verdict.engine,
        )
        for imp in verdict.impacts:
            ripple.targets[imp.room_id] = Target(imp.room_id, imp.terms, imp.reason)
        self.state.save()
        lines = "\n".join(
            f"• **#{self.rooms[t.room_id].name}** {t.reason}" for t in ripple.targets.values()
        )
        await self._say(
            room_id,
            f"🌊 **This looks like a change other teams depend on.**\n{lines}\n\n"
            f"Reply **send** and I'll brief {'that room' if len(ripple.targets) == 1 else 'those rooms'} "
            "and carry their questions back to this thread. Reply **skip** if it doesn't matter. "
            f"_(ripple #{ripple.id}, matched by {verdict.engine})_",
            thread=thread, to=[who],
        )
        log.info("ripple #%d proposed from #%s to %s", ripple.id, origin.name,
                 [self.rooms[t].name for t in ripple.targets])
        return True

    # ── the author's thread ───────────────────────────────────────────────

    async def _in_origin_thread(self, r: Ripple, who: str, text: str, addressed: bool) -> None:
        if r.status == "proposed":
            if APPROVE.search(text):
                only = self._named_rooms(text, r.targets)
                await self._send(r, who, only)
            elif SKIP.search(text):
                r.status = "skipped"
                self.state.save()
                await self._say(r.origin_room, "👌 Skipped. Nobody else was told.",
                                thread=r.origin_thread)
            elif addressed:
                await self._say(r.origin_room, "Reply **send** to brief them or **skip** to drop it.",
                                thread=r.origin_thread)
            return
        if r.status != "sent":
            return
        if addressed and re.match(r"^\s*status\b", text, re.I):
            await self._say(r.origin_room, self._status_line(r), thread=r.origin_thread)
            return
        if addressed and re.match(r"^\s*close\b", text, re.I):
            await self._close(r, f"closed by {who}")
            return
        # Anything else said in the author's thread is an answer for the
        # rooms that were briefed: carry it across.
        targets = self._named_rooms(text, r.targets) or list(r.targets)
        origin = self.rooms[r.origin_room].name
        for rid in targets:
            t = r.targets[rid]
            if t.thread_id:
                if t.status == "pending":
                    t.last_nudge = time.time()  # they just heard from us
                    self.state.save()
                await self._say(rid, f"↩️ **#{origin}** · {who}: {text}", thread=t.thread_id)

    # ── a briefed room's thread ───────────────────────────────────────────

    async def _in_target_thread(self, r: Ripple, room_id: str, who: str, text: str) -> None:
        t = r.targets.get(room_id)
        if t is None or r.status not in ("sent", "closed"):
            return
        here = self.rooms[room_id].name
        if NOT_US.search(text) and t.status == "pending":
            await self._resolve(r, t, "not_affected", who)
            await self._say(r.origin_room,
                            f"➖ **#{here}** says it's not affected ({who}). {self._waiting(r)}",
                            thread=r.origin_thread)
            await self._maybe_finish(r)
            return
        if ACK.search(text) and t.status == "pending":
            await self._resolve(r, t, "acked", who)
            rest = re.sub(ACK, "", text, count=1).strip(" .,-—")
            extra = f" — “{rest}”" if rest else ""
            await self._say(r.origin_room,
                            f"✅ **#{here}** acknowledged ({who}){extra}. {self._waiting(r)}",
                            thread=r.origin_thread)
            await self._say(room_id, "✅ Logged. Thanks!", thread=t.thread_id)
            await self._maybe_finish(r)
            return
        # A room that is talking is not a room to chase: restart its clock.
        t.last_nudge = time.time()
        self.state.save()
        await self._say(r.origin_room, f"💬 **#{here}** · {who}: {text}", thread=r.origin_thread,
                        to=[r.author])

    # ── sending, resolving, finishing ─────────────────────────────────────

    async def _send(self, r: Ripple, approver: str, only: list[str]) -> None:
        if only:
            for rid in list(r.targets):
                if rid not in only:
                    del r.targets[rid]
        origin = self.rooms[r.origin_room]
        r.status = "sent"
        quoted = "\n".join(f"> {line}" for line in r.text.splitlines() if line.strip())
        for rid, t in r.targets.items():
            room = await self.room(rid)
            why = ", ".join(f"`{x}`" for x in t.terms)
            body = (
                f"📣 **Heads-up from #{origin.name}** — ripple #{r.id}\n{quoted}\n"
                f"— {r.author}, in #{origin.name}\n\n"
                f"**Why you:** this room depends on {why}.\n"
                f"Reply in this thread and I'll carry it to #{origin.name}. "
                "Reply **ack** once you've handled it, or **not us** if it doesn't affect you."
            )
            try:
                t.thread_id = await self._say(rid, body, to=room.users)
            except SwitchError as exc:
                log.warning("could not brief #%s: %s", room.name, exc)
                continue
            t.sent_at = time.time()
            self.state.bind(rid, t.thread_id, r.id, "target")
            await self._link(r.origin_room, rid, f"downstream of #{origin.name}: {why}")
        self.state.save()
        names = ", ".join(f"**#{self.rooms[rid].name}**" for rid in r.targets)
        await self._say(
            r.origin_room,
            f"📨 Briefed {names}. {self._waiting(r)}\n"
            "Their questions will land here. Reply in this thread and I'll pass the answer back.",
            thread=r.origin_thread,
        )
        await self._update_ledgers(r)

    async def _resolve(self, r: Ripple, t: Target, status: str, who: str) -> None:
        # The ledger documents are rewritten when a ripple is sent and when it
        # closes, not on every answer: Switch announces each document write in
        # the room, and four rooms × every ack is noise nobody asked for.
        t.status, t.by, t.resolved_at = status, who, time.time()
        self.state.save()

    async def _maybe_finish(self, r: Ripple) -> None:
        if r.status == "sent" and not r.pending():
            await self._close(r, "every briefed room has signed off")

    async def _close(self, r: Ripple, why: str) -> None:
        r.status = "closed"
        self.state.save()
        took = _ago(r.created_at)
        await self._say(
            r.origin_room,
            f"🎉 Ripple #{r.id} closed: {why} (opened {took}).\n{self._status_line(r)}",
            thread=r.origin_thread, to=[r.author],
        )
        await self._update_ledgers(r)

    async def _nudge_loop(self) -> None:
        while True:
            await asyncio.sleep(min(15.0, max(5.0, self.nudge_after / 4)))
            now = time.time()
            for r in list(self.state.ripples.values()):
                if r.status != "sent":
                    continue
                for t in r.pending():
                    last = t.last_nudge or t.sent_at
                    if not t.thread_id or now - last < self.nudge_after or t.nudges >= self.max_nudges:
                        continue
                    async with self._lock:
                        t.nudges += 1
                        t.last_nudge = now
                        self.state.save()
                        room = await self.room(t.room_id)
                        origin = self.rooms[r.origin_room].name
                        try:
                            await self._say(
                                t.room_id,
                                f"⏰ Ripple #{r.id} from #{origin} is still open here "
                                f"(sent {_ago(t.sent_at)}). Reply **ack** once it's handled, "
                                "or **not us** if it doesn't affect you.",
                                thread=t.thread_id, to=room.users,
                            )
                            if t.nudges >= self.max_nudges:
                                await self._say(
                                    r.origin_room,
                                    f"⚠️ **#{room.name}** hasn't answered after {t.nudges} reminders. "
                                    "You may want to reach them directly.",
                                    thread=r.origin_thread, to=[r.author],
                                )
                        except SwitchError as exc:
                            log.warning("nudge failed: %s", exc)

    # ── commands ──────────────────────────────────────────────────────────

    async def _command(self, room_id: str, thread: str, who: str, text: str) -> None:
        room = self.rooms[room_id]
        low = text.lower().strip()
        c = self.contract(room_id)
        if low in ("", "help", "hi", "hello", "?"):
            await self._say(room_id, HELP, thread=thread)
        elif low.startswith("contract"):
            await self._say(room_id, f"📄 **#{room.name}**\n{c.render()}", thread=thread)
        elif m := re.match(r"^(?:we\s+)?owns?\s*:?\s+(.+)$", text, re.I | re.S):
            added = c.add_owns(_split_terms(m.group(1)))
            self.set_contract(room_id, c)
            await self._contract_changed(room_id, thread, f"Added to what #{room.name} owns: "
                                         + (", ".join(f"`{t}`" for t in added) or "nothing new"))
        elif m := re.match(r"^(?:we\s+)?(?:depend|depends|rely|use)\s*(?:on)?\s*:?\s+(.+)$",
                           text, re.I | re.S):
            added = c.add_depends(_split_terms(m.group(1)))
            self.set_contract(room_id, c)
            await self._contract_changed(room_id, thread, f"#{room.name} now depends on "
                                         + (", ".join(f"`{t}`" for t in added) or "nothing new"))
        elif m := re.match(r"^(?:forget|remove|drop)\s+(.+)$", text, re.I | re.S):
            gone = c.remove(_split_terms(m.group(1)))
            self.set_contract(room_id, c)
            await self._contract_changed(room_id, thread, "Removed "
                                         + (", ".join(f"`{t}`" for t in gone) or "nothing (no match)"))
        elif low.startswith(("map", "graph", "who depends")):
            await self._say(room_id, self._map(), thread=thread)
        elif low.startswith(("status", "ledger", "ripples")):
            await self._say(room_id, self._room_status(room_id), thread=thread)
        else:
            found = await self._screen(room_id, thread, who, text, force=True)
            if not found:
                await self._say(
                    room_id,
                    "I don't see another room that depends on that. If one does, someone there can "
                    "tell me with `@downstream we depend on …`.",
                    thread=thread,
                )

    async def _contract_changed(self, room_id: str, thread: str, line: str) -> None:
        await self._say(room_id, f"📝 {line}.\n{self.contract(room_id).render()}", thread=thread)
        await self._write_doc(room_id, "contract")

    # ── what people read ─────────────────────────────────────────────────

    def _named_rooms(self, text: str, among: dict[str, Target]) -> list[str]:
        norm = normalize(text)
        out = []
        for rid in among:
            n = normalize(self.rooms[rid].name)
            if n and re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", norm):
                out.append(rid)
        return out

    def _waiting(self, r: Ripple) -> str:
        pend = [f"⏳ #{self.rooms[t.room_id].name}" for t in r.pending()]
        return ("Waiting on: " + " · ".join(pend)) if pend else "Nobody left to hear from."

    def _status_line(self, r: Ripple) -> str:
        icon = {"pending": "⏳", "acked": "✅", "not_affected": "➖"}
        parts = []
        for t in r.targets.values():
            who = f" ({t.by})" if t.by else ""
            parts.append(f"{icon[t.status]} #{self.rooms[t.room_id].name}{who}")
        return " · ".join(parts) or "no rooms briefed"

    def _room_status(self, room_id: str) -> str:
        rs = [r for r in self.state.involving(room_id) if r.status in ("proposed", "sent")]
        if not rs:
            return "Nothing open involving this room. 🌤️"
        lines = []
        for r in rs:
            origin = self.rooms[r.origin_room].name
            arrow = "outgoing" if r.origin_room == room_id else f"from #{origin}"
            lines.append(f"• **#{r.id}** ({arrow}, {r.status}) {r.summary}\n  {self._status_line(r)}")
        return "**Open ripples**\n" + "\n".join(lines)

    def _map(self) -> str:
        edges: list[str] = []
        for a in self.rooms.values():
            owns = {normalize(t): t for t in self.contract(a.room_id).owns}
            for b in self.rooms.values():
                if a.room_id == b.room_id:
                    continue
                shared = [owns[normalize(t)] for t in self.contract(b.room_id).depends
                          if normalize(t) in owns]
                if shared:
                    edges.append(f"• **#{a.name}** → **#{b.name}** via "
                                 + ", ".join(f"`{t}`" for t in shared))
        orphans = []
        all_owned = {normalize(t) for r in self.rooms.values() for t in self.contract(r.room_id).owns}
        for b in self.rooms.values():
            loose = [t for t in self.contract(b.room_id).depends if normalize(t) not in all_owned]
            if loose:
                orphans.append(f"• **#{b.name}** depends on " + ", ".join(f"`{t}`" for t in loose)
                               + " (no room owns it yet)")
        out = "🗺️ **Who depends on whom**\n" + ("\n".join(edges) or "_No links yet._")
        if orphans:
            out += "\n\n**Unowned dependencies**\n" + "\n".join(orphans)
        return out

    async def _introduce(self, room: Room) -> None:
        c = self.contract(room.room_id)
        if c.is_empty():
            body = (f"👋 I'm **Downstream**. I tell other teams when something they depend on changes "
                    f"here, and bring their questions back. I don't know yet what #{room.name} owns "
                    "or depends on. Tell me: `@downstream we own …` and `@downstream we depend on …`.")
        else:
            body = (f"👋 I'm **Downstream**. I tell other teams when something they depend on changes "
                    f"here, and bring their questions back. Here's what I understand about #{room.name}:\n"
                    f"{c.render()}\nFix it any time with `@downstream we depend on …` or "
                    "`@downstream forget …`. `@downstream help` for the rest.")
        try:
            await self._say(room.room_id, body)
            await self._write_doc(room.room_id, "contract")
        except SwitchError as exc:
            log.warning("could not introduce myself in #%s: %s", room.name, exc)

    async def _say(self, room_id: str, body: str, *, thread: str | None = None,
                   to: list[str] | None = None) -> str:
        to = [n for n in (to or []) if n and n.lower() != self.name]
        try:
            return await self.sw.post_to(room_id, body, to, thread_id=thread)
        except SwitchError as exc:
            if to:  # an unknown name is not worth losing the message over
                log.warning("targeted send failed (%s); posting plainly", exc)
                return await self.sw.post(room_id, body, thread_id=thread)
            raise

    async def _link(self, source: str, target: str, label: str) -> None:
        try:
            await self.sw.op("link_rooms", source_room_id=source, target_room_id=target,
                             label=label[:120])
        except SwitchError as exc:
            log.debug("link_rooms: %s", exc)  # already linked, most likely

    # ── room documents (the humans' copy of the ledger) ───────────────────

    async def _update_ledgers(self, r: Ripple) -> None:
        for rid in {r.origin_room, *r.targets}:
            await self._write_doc(rid, "ledger")

    async def _write_doc(self, room_id: str, kind: str) -> None:
        room = self.rooms.get(room_id)
        if room is None:
            return
        if kind == "contract":
            name, desc = "downstream-contract", f"What #{room.name} owns and depends on"
            instr = ("Downstream's record of this room's contract. Use it to judge whether a "
                     "change announced elsewhere affects this room.")
            content = f"# #{room.name} contract\n\n{self.contract(room_id).render()}\n"
        else:
            name, desc = "downstream-ledger", f"Cross-team changes involving #{room.name}"
            instr = "Every change Downstream relayed into or out of this room, and who signed off."
            content = self._ledger_doc(room_id)
        docs = self.state.docs.setdefault(room_id, {})
        try:
            if kind in docs:
                await self.sw.op("update_room_document", bind=room_id,
                                 document_id=docs[kind], content=content)
            else:
                res = await self.sw.op("create_room_document", bind=room_id, name=name,
                                       description=desc, instructions=instr, content=content)
                docs[kind] = res["document_id"]
                self.state.save()
        except SwitchError as exc:
            log.warning("room document %s in #%s: %s", name, room.name, exc)

    def _ledger_doc(self, room_id: str) -> str:
        room = self.rooms[room_id]
        lines = [f"# Downstream ledger for #{room.name}", "",
                 "| # | Direction | Change | Rooms | Status |", "|---|---|---|---|---|"]
        for r in sorted(self.state.involving(room_id), key=lambda x: x.id, reverse=True):
            origin = self.rooms.get(r.origin_room)
            direction = "out" if r.origin_room == room_id else f"in from #{origin.name if origin else '?'}"
            summary = r.summary.replace("|", "/")
            lines.append(f"| {r.id} | {direction} | {summary} | {self._status_line(r)} | {r.status} |")
        return "\n".join(lines) + "\n"


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 90:
        return f"{s}s ago" if s else "just now"
    if s < 5400:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"
