"""Command line: register Downstream with a Switch server, run it, seed a demo.

    uv run downstream register      # once: creates the agent, stores its key
    uv run downstream run           # the long-running teammate
    uv run downstream seed          # demo org: five team rooms in Mattermost
    uv run downstream reset         # clear the demo rooms (stop `run` first)
    uv run downstream play          # replay the demo conversation (for recording)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .switch import SwitchAgent, SwitchError, open_addressing, register

ROOT = Path(os.environ.get("DOWNSTREAM_HOME", Path.cwd() / ".downstream"))
CREDS = ROOT / "agent.json"
STACK_ENV = Path(__file__).resolve().parent.parent / "stack" / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def setting(name: str, default: str | None = None, *, stack_key: str | None = None) -> str | None:
    """Environment first, then the bundled stack's .env, then the default."""
    if os.environ.get(name):
        return os.environ[name]
    if stack_key:
        v = read_env_file(STACK_ENV).get(stack_key)
        if v:
            return v
    return default


def api_url() -> str:
    port = read_env_file(STACK_ENV).get("API_HOST_PORT", "8000")
    return setting("SWITCH_API_URL", f"http://localhost:{port}") or ""


def load_agent() -> tuple[SwitchAgent, dict]:
    if not CREDS.exists():
        sys.exit("No agent registered yet. Run `uv run downstream register` first.")
    c = json.loads(CREDS.read_text())
    return SwitchAgent(api_url=c["api_url"], agent_id=c["agent_id"], api_key=c["api_key"]), c


async def cmd_register(args: argparse.Namespace) -> None:
    token = setting("SWITCH_REGISTRATION_TOKEN", stack_key="AGENT_REGISTRATION_TOKEN")
    if not token:
        sys.exit("Set SWITCH_REGISTRATION_TOKEN (a registration key from your Switch Gateway).")
    url = api_url()
    agent_id, key = await register(
        url, token, args.name,
        "Tells other teams when something they depend on changes, relays their questions, "
        "and chases acknowledgements until every affected room has signed off.",
        display_name="Downstream", overwrite=args.overwrite,
    )
    ROOT.mkdir(parents=True, exist_ok=True)
    CREDS.write_text(json.dumps({"api_url": url, "agent_id": agent_id, "api_key": key,
                                 "name": args.name}, indent=2))
    CREDS.chmod(0o600)
    print(f"Registered @{args.name} ({agent_id}) on {url}. Key saved to {CREDS}.")
    email = setting("SWITCH_ADMIN_EMAIL", stack_key="GATEWAY_ADMIN_EMAIL")
    password = setting("SWITCH_ADMIN_PASSWORD", stack_key="GATEWAY_ADMIN_PASSWORD")
    if email and password:
        try:
            await open_addressing(url, email, password, agent_id)
            print("Anyone in a room can now address it (addressing policy: open).")
        except SwitchError as exc:
            print(f"Could not open its addressing policy ({exc}).")
    else:
        print("Set its addressing to 'anyone' in Switch Console, so every team can talk to it.")
    print("Invite it to a room with `!invite-agent @%s`, or run `uv run downstream seed`." % args.name)


async def cmd_run(args: argparse.Namespace) -> None:
    from .agent import Downstream
    from .ledger import State

    sw, c = load_agent()
    bot = Downstream(sw, State(ROOT / "state.json"), name=c.get("name", "downstream"))
    try:
        await bot.run()
    finally:
        await sw.close()


async def cmd_seed(args: argparse.Namespace) -> None:
    from .demo import seed

    sw, c = load_agent()
    try:
        await seed(sw, c.get("name", "downstream"), ROOT, STACK_ENV)
    finally:
        await sw.close()


async def cmd_reset(args: argparse.Namespace) -> None:
    from .reset import reset

    await reset(ROOT, STACK_ENV)


async def cmd_play(args: argparse.Namespace) -> None:
    from .demo import play

    await play(ROOT, STACK_ENV, pause=args.pause, step=args.step)


def main() -> None:
    p = argparse.ArgumentParser(prog="downstream", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="register the agent with Switch (once)")
    r.add_argument("--name", default="downstream")
    r.add_argument("--overwrite", action="store_true", help="re-register, issuing a new key")
    sub.add_parser("run", help="run Downstream")
    sub.add_parser("seed", help="create the demo org: team rooms with contracts")
    sub.add_parser("reset", help="clear the demo rooms for a fresh run (stop `run` first)")
    pl = sub.add_parser("play", help="replay the demo conversation as the demo people")
    pl.add_argument("--pause", type=float, default=6.0, help="seconds between beats")
    pl.add_argument("--step", action="store_true", help="wait for Enter between beats")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    fn = {"register": cmd_register, "run": cmd_run, "seed": cmd_seed, "reset": cmd_reset,
          "play": cmd_play}[args.cmd]
    try:
        asyncio.run(fn(args))
    except SwitchError as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
