"""`downstream reset`: clear the demo rooms for a fresh run.

Deletes every Mattermost post in the five demo channels except Downstream's
introductions, and forgets Downstream's ripples (contracts and room documents
stay). Run it while Downstream is stopped, since a running Downstream holds its
ripples in memory and would write them back.
"""

from __future__ import annotations

import json
from pathlib import Path

from .demo import ROOMS, Mattermost, _env


async def reset(home: Path, stack_env: Path) -> None:
    env = _env(stack_env)
    mm = Mattermost(f"http://localhost:{env.get('MATTERMOST_HOST_PORT', '8065')}")
    await mm.login(env.get("MATTERMOST_ADMIN_USER", "admin"), env["MATTERMOST_ADMIN_PASSWORD"])
    team = await mm.call("GET", f"/teams/name/{env.get('MATTERMOST_TEAM_NAME', 'switch')}")
    names = {r[0] for r in ROOMS}
    removed = 0
    for ch in await mm.call("GET", f"/users/me/teams/{team['id']}/channels"):
        if ch.get("display_name") not in names:
            continue
        page = await mm.call("GET", f"/channels/{ch['id']}/posts", params={"per_page": 200})
        for p in page["posts"].values():
            if p.get("type") or "I'm **Downstream**" in p.get("message", ""):
                continue
            r = await mm.http.delete(f"/posts/{p['id']}")
            removed += r.status_code == 200  # a reply goes with its root: 404 is fine
    await mm.close()

    state = home / "state.json"
    if state.exists():
        d = json.loads(state.read_text())
        d.update({"next_id": 1, "ripples": {}, "threads": {}})
        state.write_text(json.dumps(d, indent=2))
    print(f"Removed {removed} posts from the demo rooms and cleared Downstream's ripples.")
