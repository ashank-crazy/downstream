"""A small client for the Switch agent protocol.

Down: one server-sent event stream (scope=all, so every room the agent is in).
Up: HTTP operations. Each room gets a *placed session* on the one connection, so
room-scoped operations (threaded replies, room documents) can name their room
with X-Switch-Session-Id instead of opening a connection per room.

Protocol reference: https://docs.switchagents.ai/flintai/switch/internals/agent-protocol
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("downstream.switch")

# What Downstream tells Switch about itself: always connected, exchanges
# messages, nothing mediated. It is a long-running service, not a session
# someone starts, so `always_on` is the honest connection model.
PROFILE: dict[str, Any] = {
    "connection_model": "always_on",
    "message_exchange": True,
    "pre_invocation_mediation": [],
    "post_invocation_mediation": [],
    "event_reporting": [],
}

HEARTBEAT_SECONDS = 2.0
CLIENT_NAME = "downstream"
CLIENT_VERSION = "0.1.0"


class SwitchError(RuntimeError):
    def __init__(self, what: str, response: httpx.Response) -> None:
        super().__init__(f"{what}: HTTP {response.status_code} {response.text[:300]}")
        self.status = response.status_code


@dataclass
class Event:
    """One frame from the stream. Control frames have no sequence."""

    kind: str
    data: dict[str, Any]
    sequence: int | None = None

    @property
    def room_id(self) -> str | None:
        return self.data.get("room_id")

    @property
    def payload(self) -> dict[str, Any]:
        return self.data.get("payload") or {}


async def register(
    api_url: str,
    registration_token: str,
    name: str,
    description: str,
    *,
    display_name: str | None = None,
    overwrite: bool = False,
) -> tuple[str, str]:
    """Register a custom agent once. Returns (agent_id, api_key); the key is
    shown exactly once by Switch, so the caller has to keep it."""
    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "connector_type": "custom",
        "integration_profile": PROFILE,
        "overwrite": overwrite,
        "metadata": {"client": CLIENT_NAME, "client_version": CLIENT_VERSION},
    }
    if display_name:
        body["display_name"] = display_name
    async with httpx.AsyncClient(base_url=api_url.rstrip("/"), timeout=30) as http:
        r = await http.post(
            "/agents",
            headers={"Authorization": f"Bearer {registration_token}"},
            json=body,
        )
    if r.status_code != 200:
        raise SwitchError(f"registering {name}", r)
    b = r.json()
    return b["id"], b["api_key"]


async def open_addressing(api_url: str, email: str, password: str, agent_id: str) -> None:
    """Let anyone in a room address the agent. A newly registered agent only
    takes instructions from its owner, which is right for a personal coding
    agent and wrong for a teammate every team talks to. Clearing the scoped
    policy is the owner's call, so this signs in to the Gateway as them."""
    async with httpx.AsyncClient(base_url=api_url.rstrip("/") + "/gateway", timeout=30) as gw:
        r = await gw.post("/auth/login", json={"email": email, "password": password})
        if r.status_code != 200:
            raise SwitchError("signing in to the Gateway", r)
        cookie = r.cookies.get("switch_auth")
        gw.cookies.clear()  # carried by hand: the cookie may be marked Secure
        r = await gw.put(
            f"/agents/{agent_id}/addressing-policy",
            json={"policy": None},
            headers={"Cookie": f"switch_auth={cookie}"},
        )
        if r.status_code != 200:
            raise SwitchError("opening the addressing policy", r)


@dataclass
class SwitchAgent:
    """One agent identity talking to one Switch server."""

    api_url: str
    agent_id: str
    api_key: str
    connection_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    rooms: set[str] = field(default_factory=set)
    cursor: int | None = None
    _placed: dict[str, str] = field(default_factory=dict)  # session id -> room
    _http: httpx.AsyncClient | None = None
    _stream_open: asyncio.Event = field(default_factory=asyncio.Event)

    # ── plumbing ──────────────────────────────────────────────────────────

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.api_url.rstrip("/"),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=httpx.Timeout(30, read=None),
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    @staticmethod
    def session_for(room_id: str) -> str:
        return f"downstream-{room_id}"

    # ── operations ────────────────────────────────────────────────────────

    async def op(self, operation: str, /, *, bind: str | None = None, **args: Any) -> Any:
        """Run one operation. With `bind` (a room id), the call is bound to
        that room through the session Downstream placed there."""
        headers = {"X-Switch-Connection-Id": self.connection_id}
        if bind is not None:
            await self._ensure_placed(bind)
            headers["X-Switch-Session-Id"] = self.session_for(bind)
            headers["X-Switch-Session-Host-Id"] = CLIENT_NAME
            headers["X-Switch-Session-Epoch"] = "1"
        args = {k: v for k, v in args.items() if v is not None}
        r = await self.http.post(
            f"/agents/{self.agent_id}/ops/{operation}", headers=headers, json=args
        )
        if r.status_code != 200:
            raise SwitchError(f"operation {operation}", r)
        return r.json().get("result")

    async def op_unbound(self, operation: str, /, **args: Any) -> Any:
        """An operation with no connection at all (setup scripts)."""
        args = {k: v for k, v in args.items() if v is not None}
        r = await self.http.post(f"/agents/{self.agent_id}/ops/{operation}", json=args)
        if r.status_code != 200:
            raise SwitchError(f"operation {operation}", r)
        return r.json().get("result")

    async def _ensure_placed(self, room_id: str) -> None:
        sid = self.session_for(room_id)
        if self._placed.get(sid) == room_id:
            return
        await self._stream_open.wait()
        wanted = dict(self._placed)
        wanted[sid] = room_id
        await self.place(wanted)

    async def place(self, placements: dict[str, str]) -> None:
        r = await self.http.post(
            f"/agents/{self.agent_id}/connection/placements",
            json={"connection_id": self.connection_id, "placements": placements},
        )
        if r.status_code != 200:
            raise SwitchError("placing sessions", r)
        self._placed = dict(r.json().get("placements") or placements)
        # The server echoes placements as {session: room} or a richer shape;
        # keep our own copy when it is not the plain mapping.
        if not all(isinstance(v, str) for v in self._placed.values()):
            self._placed = dict(placements)

    async def place_all(self) -> None:
        if self.rooms:
            await self.place({self.session_for(r): r for r in sorted(self.rooms)})

    # Convenience wrappers over the operations Downstream uses.

    async def post(self, room_id: str, body: str, thread_id: str | None = None) -> str:
        res = await self.op("post_message", bind=room_id, body=body, thread_id=thread_id)
        return res.get("event_id", "") if isinstance(res, dict) else ""

    async def post_to(
        self,
        room_id: str,
        body: str,
        names: list[str],
        thread_id: str | None = None,
    ) -> str:
        if not names:
            return await self.post(room_id, body, thread_id)
        res = await self.op(
            "send_targeted_message",
            bind=room_id,
            body=body,
            target_names=names,
            thread_id=thread_id,
        )
        return res.get("event_id", "") if isinstance(res, dict) else ""

    async def list_rooms(self) -> list[dict[str, Any]]:
        return await self.op("list_rooms") or []

    async def room_detail(self, room_id: str) -> dict[str, Any]:
        return await self.op("get_room_detail", room_id=room_id)

    # ── the stream ────────────────────────────────────────────────────────

    async def run(self, on_event: Callable[[Event], Awaitable[None]]) -> None:
        """Hold the stream open forever, reconnecting with the same connection
        id (so the connection survives a dropped socket) and beating every two
        seconds while a stream is attached."""
        backoff = 1.0
        while True:
            beat = asyncio.create_task(self._heartbeat())
            try:
                await self._stream_once(on_event)
                backoff = 1.0
            except (httpx.HTTPError, SwitchError) as exc:
                log.warning("stream dropped: %s; reopening in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)
            finally:
                beat.cancel()
                self._stream_open.clear()
                self._placed = {}

    async def _stream_once(self, on_event: Callable[[Event], Awaitable[None]]) -> None:
        params = {
            "connection_id": self.connection_id,
            "scope": "all",
            "filter": "all",
            "start_from": "head" if self.cursor is None else str(self.cursor),
            "client": CLIENT_NAME,
            "client_version": CLIENT_VERSION,
        }
        headers = {"Accept": "text/event-stream"}
        if self.cursor is not None:
            headers["Last-Event-ID"] = str(self.cursor)
        async with self.http.stream(
            "GET", f"/agents/{self.agent_id}/events", params=params, headers=headers
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise SwitchError("opening the event stream", resp)
            kind, data, seq = "message", [], None
            async for line in resp.aiter_lines():
                if line == "":
                    if data:
                        await self._dispatch(kind, "\n".join(data), seq, on_event)
                    kind, data, seq = "message", [], None
                    continue
                if line.startswith(":"):
                    continue  # keepalive comment
                field_name, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field_name == "event":
                    kind = value
                elif field_name == "data":
                    data.append(value)
                elif field_name == "id":
                    try:
                        seq = int(value)
                    except ValueError:
                        seq = None

    async def _dispatch(
        self,
        kind: str,
        raw: str,
        seq: int | None,
        on_event: Callable[[Event], Awaitable[None]],
    ) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("unparseable %s frame: %.200s", kind, raw)
            return
        if kind == "connection_state":
            self.rooms = set(data.get("rooms") or [])
            if data.get("cursor") is not None:
                self.cursor = int(data["cursor"])
            self._stream_open.set()
            log.info("connected: %d rooms, cursor %s", len(self.rooms), self.cursor)
            try:
                await self.place_all()
            except SwitchError as exc:
                log.warning("could not place sessions: %s", exc)
        elif kind == "subscription_changed":
            self.rooms = set(data.get("rooms") or [])
        elif kind == "evicted":
            raise SwitchError("evicted", httpx.Response(409, text=str(data)))
        if seq is not None:
            self.cursor = seq
        try:
            await on_event(Event(kind=kind, data=data, sequence=seq))
        except Exception:  # one bad event must not take the stream down
            log.exception("handler failed on %s", kind)

    async def _heartbeat(self) -> None:
        await self._stream_open.wait()
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                r = await self.http.post(
                    f"/agents/{self.agent_id}/connection/beat",
                    json={"connection_id": self.connection_id, "cursor": self.cursor},
                )
            except httpx.HTTPError as exc:
                log.debug("beat failed: %s", exc)
                continue
            if r.status_code in (404, 409):
                # Both mean reopen, not back off: a dead connection stays dead.
                log.warning("heartbeat says the connection is gone (%s)", r.status_code)
                return
