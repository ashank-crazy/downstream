"""The whole ripple, against a fake Switch: propose, send, relay both ways,
ack, not-us, nudge, close."""

import itertools

import pytest

from downstream.agent import Downstream
from downstream.demo import ROOMS
from downstream.ledger import State


class FakeSwitch:
    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.ids = itertools.count(1)
        self.rooms = {f"r-{n}": (n, d, c) for n, d, c in ROOMS}
        self.docs: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []

    async def list_rooms(self):
        return [{"room_id": k, "name": v[0], "description": v[1]} for k, v in self.rooms.items()]

    async def room_detail(self, room_id):
        n, d, c = self.rooms[room_id]
        return {"name": n, "description": d, "instructions": c,
                "connected_user_names": [f"{n}-person"]}

    async def post(self, room_id, body, thread_id=None):
        eid = f"e{next(self.ids)}"
        self.posts.append({"room": room_id, "body": body, "thread": thread_id, "id": eid, "to": []})
        return eid

    async def post_to(self, room_id, body, names, thread_id=None):
        eid = await self.post(room_id, body, thread_id)
        self.posts[-1]["to"] = names
        return eid

    async def op(self, op, /, *, bind=None, **args):
        if op == "create_room_document":
            self.docs[f"{bind}:{args['name']}"] = args["content"]
            return {"document_id": f"doc-{bind}-{args['name']}"}
        if op == "update_room_document":
            self.docs[args["document_id"]] = args["content"]
            return {"status": "ok"}
        if op == "link_rooms":
            self.links.append((args["source_room_id"], args["target_room_id"]))
            return {}
        raise AssertionError(op)

    def last(self, room: str) -> dict:
        return [p for p in self.posts if p["room"] == room][-1]


def msg(body, mid, sender="alice", thread=None, addressed=False):
    return {"body": body, "message_id": mid, "sender": f"@{sender}:x", "sender_name": sender,
            "thread_id": thread, "addressed": addressed}


@pytest.fixture
async def bot(tmp_path):
    sw = FakeSwitch()
    b = Downstream(sw, State(tmp_path / "state.json"))
    await b.refresh_rooms()
    sw.posts.clear()
    return b, sw


async def test_full_ripple(bot):
    b, sw = bot
    api, mobile, data, billing = "r-api-platform", "r-mobile-app", "r-data-insights", "r-billing"

    await b.on_message(api, msg("Anyone know why the user_id index got slow?", "m0"))
    assert sw.posts == []  # a question is not a change

    await b.on_message(api, msg("Heads up: in the orders-api v2 response we're renaming "
                                "`user_id` to `account_id`. Shipping Tuesday.", "m1"))
    proposal = sw.last(api)
    assert proposal["thread"] == "m1" and "ripple #1" in proposal["body"]
    assert proposal["to"] == ["alice"]
    for room in ("#mobile-app", "#data-insights", "#billing"):
        assert room in proposal["body"]
    assert "#infra" not in proposal["body"]

    await b.on_message(api, msg("send", "m2", thread="m1"))
    heads = {r: sw.last(r) for r in (mobile, data, billing)}
    assert all("Heads-up from #api-platform" in h["body"] for h in heads.values())
    assert heads[mobile]["to"] == ["mobile-app-person"]
    assert "Briefed" in sw.last(api)["body"]
    assert (api, mobile) in sw.links

    # A question in mobile's thread reaches Alice's thread...
    await b.on_message(mobile, msg("Will user_id stay as an alias?", "m3", "bob",
                                   thread=heads[mobile]["id"]))
    relay = sw.last(api)
    assert relay["thread"] == "m1" and "#mobile-app" in relay["body"] and "bob" in relay["body"]

    # ...and her answer goes back out, to every briefed room.
    await b.on_message(api, msg("Yes, until v2.3.", "m4", thread="m1"))
    back = sw.last(mobile)
    assert back["thread"] == heads[mobile]["id"] and "Yes, until v2.3." in back["body"]

    await b.on_message(mobile, msg("ack, moving in 2.3", "m5", "bob", thread=heads[mobile]["id"]))
    assert "✅ **#mobile-app** acknowledged (bob)" in [p for p in sw.posts if p["room"] == api][-1]["body"]

    await b.on_message(billing, msg("not us, totals only", "m6", "dan", thread=heads[billing]["id"]))
    assert "not affected" in sw.last(api)["body"]
    assert b.state.ripples[1].status == "sent"

    await b.on_message(data, msg("ack, ETL updated", "m7", "carol", thread=heads[data]["id"]))
    assert b.state.ripples[1].status == "closed"
    assert "closed" in sw.last(api)["body"]
    assert any("downstream-ledger" in k or k.startswith("doc-") for k in sw.docs)


async def test_skip(bot):
    b, sw = bot
    await b.on_message("r-api-platform", msg("We're deprecating auth tokens v1 next month.", "m1"))
    await b.on_message("r-api-platform", msg("skip", "m2", thread="m1"))
    assert b.state.ripples[1].status == "skipped"
    assert all(p["room"] == "r-api-platform" for p in sw.posts)


async def test_send_to_one_room(bot):
    b, sw = bot
    await b.on_message("r-api-platform", msg("We're deprecating auth tokens v1 next month.", "m1"))
    assert set(b.state.ripples[1].targets) == {"r-mobile-app", "r-billing"}
    await b.on_message("r-api-platform", msg("send billing", "m2", thread="m1"))
    assert set(b.state.ripples[1].targets) == {"r-billing"}
    assert not [p for p in sw.posts if p["room"] == "r-mobile-app"]


async def test_contract_editing_and_map(bot):
    b, sw = bot
    await b.on_message("r-infra", msg("@downstream we depend on orders-api", "m1", "erin",
                                      addressed=True))
    assert "orders-api" in b.contract("r-infra").depends
    await b.on_message("r-infra", msg("@downstream map", "m2", "erin", addressed=True))
    m = sw.last("r-infra")["body"]
    assert "**#api-platform** → **#infra** via `orders-api`" in m


async def test_ignores_itself(bot):
    b, sw = bot
    await b.on_message("r-api-platform", msg("We're renaming user_id.", "m1", "downstream"))
    assert sw.posts == []


async def test_nudge(bot, monkeypatch):
    b, sw = bot
    b.nudge_after = 0
    await b.on_message("r-api-platform", msg("We're deprecating auth tokens v1.", "m1"))
    await b.on_message("r-api-platform", msg("send", "m2", thread="m1"))
    import asyncio

    calls = 0
    real_sleep = asyncio.sleep

    async def fast_sleep(_):
        nonlocal calls
        calls += 1
        if calls > 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr("downstream.agent.asyncio.sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await b._nudge_loop()
    assert any("⏰" in p["body"] for p in sw.posts)
    assert any("hasn't answered" in p["body"] for p in sw.posts)
