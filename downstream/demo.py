"""The demo org, and a replay of the demo conversation.

`seed` creates five demo people in the bundled Mattermost and five team rooms
through Switch (Downstream's own `create_room` operation), each with a contract
in its instructions. `play` then performs the conversation from the README as
those people, through Mattermost's API, so the whole flow can be watched (and
recorded) from one Mattermost window.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from .switch import SwitchAgent, SwitchError

PEOPLE = {
    "alice": ("Alice", "Chen", "api-platform"),
    "bob": ("Bob", "Okafor", "mobile-app"),
    "carol": ("Carol", "Iyer", "data-insights"),
    "dan": ("Dan", "Kowalski", "billing"),
    "erin": ("Erin", "Sato", "infra"),
}

ROOMS = [
    ("api-platform", "Public API and auth. Ships the orders API.",
     "Owns: orders-api, user_id, auth tokens, webhooks v1\nDepends on: postgres cluster"),
    ("mobile-app", "iOS and Android apps.",
     "Owns: ios app, android app\nDepends on: user_id, orders-api, auth tokens, push notifications"),
    ("data-insights", "Analytics, dashboards and the nightly ETL.",
     "Owns: revenue dashboard, nightly etl\nDepends on: user_id, orders table, webhooks v1"),
    ("billing", "Invoices and payments.",
     "Owns: invoices, payments webhook\nDepends on: orders-api, auth tokens"),
    ("infra", "Databases, CI, notifications.",
     "Owns: postgres cluster, push notifications, ci\nDepends on: nothing"),
]


def _env(stack_env: Path) -> dict[str, str]:
    from .__main__ import read_env_file

    return read_env_file(stack_env)


class Mattermost:
    def __init__(self, url: str) -> None:
        self.http = httpx.AsyncClient(base_url=url.rstrip("/") + "/api/v4", timeout=30)
        self.token = ""

    async def login(self, user: str, password: str) -> dict[str, Any]:
        r = await self.http.post("/users/login", json={"login_id": user, "password": password})
        r.raise_for_status()
        self.token = r.headers["Token"]
        self.http.headers["Authorization"] = f"Bearer {self.token}"
        return r.json()

    async def call(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Mattermost {method} {path}: {r.status_code} {r.text[:200]}")
        return r.json() if r.content else None

    async def close(self) -> None:
        await self.http.aclose()


async def seed(sw: SwitchAgent, agent_name: str, home: Path, stack_env: Path) -> None:
    env = _env(stack_env)
    mm_url = f"http://localhost:{env.get('MATTERMOST_HOST_PORT', '8065')}"
    team_name = env.get("MATTERMOST_TEAM_NAME", "switch")
    admin_user = env.get("MATTERMOST_ADMIN_USER", "admin")

    people_file = home / "demo-people.json"
    people: dict[str, str] = json.loads(people_file.read_text()) if people_file.exists() else {}

    mm = Mattermost(mm_url)
    await mm.login(admin_user, env["MATTERMOST_ADMIN_PASSWORD"])
    team = await mm.call("GET", f"/teams/name/{team_name}")
    for username, (first, last, _) in PEOPLE.items():
        password = people.get(username) or ("Demo-" + secrets.token_hex(8))
        try:
            u = await mm.call("POST", "/users", json={
                "username": username, "email": f"{username}@demo.downstream.test",
                "password": password, "first_name": first, "last_name": last,
            })
        except RuntimeError:
            u = await mm.call("GET", f"/users/username/{username}")
            await mm.call("PUT", f"/users/{u['id']}/password", json={"new_password": password})
        people[username] = password
        try:
            await mm.call("POST", f"/teams/{team['id']}/members",
                          json={"team_id": team["id"], "user_id": u["id"]})
        except RuntimeError:
            pass  # already a member
        print(f"  person  {first} {last} (@{username})")
    home.mkdir(parents=True, exist_ok=True)
    people_file.write_text(json.dumps(people, indent=2))
    people_file.chmod(0o600)
    await mm.close()

    existing = {r["name"]: r for r in await sw.op_unbound("list_rooms")}
    for name, description, contract in ROOMS:
        if name in existing:
            print(f"  room    #{name} (exists)")
            continue
        members = [u for u, p in PEOPLE.items() if p[2] == name]
        try:
            res = await sw.op_unbound(
                "create_room", name=name, description=description,
                agent_names=[agent_name], user_names=members + [admin_user],
                channel_type="channel_public", instructions=contract,
            )
        except SwitchError as exc:
            # Older servers or bridges that cannot resolve a demo user: retry
            # without people, so the room exists either way.
            print(f"  room    #{name}: {exc}; retrying without people")
            res = await sw.op_unbound(
                "create_room", name=name, description=description,
                agent_names=[agent_name], instructions=contract,
            )
        print(f"  room    #{name} ({res.get('id')})")
    print(f"\nDemo people's passwords are in {people_file} (local Mattermost only).")
    print(f"Open {mm_url} and sign in as {admin_user} to watch every room.")


# ── the replay ────────────────────────────────────────────────────────────


class Persona:
    def __init__(self, mm: Mattermost, me: dict[str, Any]) -> None:
        self.mm, self.me = mm, me

    async def say(self, channel: dict[str, Any], text: str, root: str | None = None) -> dict:
        body = {"channel_id": channel["id"], "message": text}
        if root:
            body["root_id"] = root
        return await self.mm.call("POST", "/posts", json=body)


async def play(home: Path, stack_env: Path, *, pause: float, step: bool) -> None:
    env = _env(stack_env)
    mm_url = f"http://localhost:{env.get('MATTERMOST_HOST_PORT', '8065')}"
    team_name = env.get("MATTERMOST_TEAM_NAME", "switch")
    people = json.loads((home / "demo-people.json").read_text())

    cast: dict[str, Persona] = {}
    for u in PEOPLE:
        mm = Mattermost(mm_url)
        cast[u] = Persona(mm, await mm.login(u, people[u]))
    watcher = cast["alice"].mm
    team = await watcher.call("GET", f"/teams/name/{team_name}")

    async def channel(room: str) -> dict[str, Any]:
        for who, (_, _, home_room) in PEOPLE.items():
            if home_room != room:
                continue
            chans = await cast[who].mm.call("GET", f"/users/me/teams/{team['id']}/channels")
            for c in chans:
                if c.get("display_name") == room or c.get("name") == room:
                    return c
        raise SystemExit(f"No Mattermost channel for #{room}. Did `downstream seed` run?")

    async def beat(label: str) -> None:
        print(f"\n▶ {label}")
        if step:
            await asyncio.to_thread(input, "  (Enter) ")
        else:
            await asyncio.sleep(pause)

    async def wait_for(who: str, ch: dict, needle: str, *, root: str | None = None,
                       since: float, timeout: float = 120) -> dict[str, Any]:
        mm = cast[who].mm
        end = time.time() + timeout
        while time.time() < end:
            path = f"/posts/{root}/thread" if root else f"/channels/{ch['id']}/posts"
            data = await mm.call("GET", path, params={"per_page": 60})
            posts = sorted(data["posts"].values(), key=lambda p: p["create_at"])
            for p in posts:
                if p["create_at"] / 1000 >= since - 1 and needle in p["message"]:
                    return p
            await asyncio.sleep(1.5)
        raise SystemExit(f"Timed out waiting for “{needle}” in #{ch.get('display_name')}")

    api, mobile, data, billing = [await channel(n) for n in
                                  ("api-platform", "mobile-app", "data-insights", "billing")]
    a, b, c, d = cast["alice"], cast["bob"], cast["carol"], cast["dan"]

    await beat("Small talk with a dependency word in it: Downstream stays quiet")
    await a.say(api, "Anyone know why the user_id index on orders got slow this morning?")

    await beat("Alice announces a breaking change in #api-platform")
    t0 = time.time()
    post = await a.say(api, "Heads up: in the orders-api v2 response we're renaming `user_id` "
                            "to `account_id`. Shipping Tuesday 18:00 IST.")
    root = post["id"]
    await wait_for("alice", api, "ripple #", root=root, since=t0)

    await beat("Alice says send")
    t1 = time.time()
    await a.say(api, "send", root=root)
    heads = {}
    for name, ch, who in (("mobile", mobile, "bob"), ("data", data, "carol"),
                          ("billing", billing, "dan")):
        heads[name] = await wait_for(who, ch, "Heads-up from", since=t1)

    await beat("Bob (#mobile-app) asks a question in the briefing thread")
    t2 = time.time()
    await b.say(mobile, "Will user_id stay as an alias for a release? App Store review takes "
                        "~5 days, we can't ship a fix by Tuesday.", root=heads["mobile"]["id"])
    await wait_for("alice", api, "mobile-app", root=root, since=t2)

    await beat("Alice answers in her own thread; Downstream carries it back")
    t3 = time.time()
    await a.say(api, "Yes, user_id stays as a deprecated alias until v2.3 (Dec 1).", root=root)
    await wait_for("bob", mobile, "api-platform", root=heads["mobile"]["id"], since=t3)

    await beat("Bob acknowledges")
    await b.say(mobile, "ack, we'll move to account_id in the 2.3 app release.",
                root=heads["mobile"]["id"])

    await beat("Dan (#billing) says it doesn't affect them")
    await d.say(billing, "not us, billing only reads order totals.", root=heads["billing"]["id"])

    await beat("#data-insights is quiet. Downstream chases them…")
    await wait_for("carol", data, "⏰", root=heads["data"]["id"], since=t1, timeout=900)

    await beat("Carol acknowledges")
    t4 = time.time()
    await c.say(data, "ack, nightly ETL now reads account_id and falls back to user_id.",
                root=heads["data"]["id"])
    await wait_for("alice", api, "closed", root=root, since=t4)

    await beat("Alice asks for the dependency map")
    await a.say(api, "@downstream map")
    print("\n✔ Demo finished.")
    for p in cast.values():
        await p.mm.close()
