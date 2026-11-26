"""``exuvia env``: a project's environment variables from a terminal.

    exuvia env list [dir | --url URL]
    exuvia env set NAME [dir | --url URL] [--plain] [--from-file PATH | --stdin]
    exuvia env rm NAME [dir | --url URL]
    exuvia env ... --url HUB --group GROUP
    exuvia env ... --url HUB --hub

``--group`` works on a hub group's variables instead (groups.py), which
every project in the group inherits: ``--url`` is then the hub's address and
GROUP the group's id (on its page in the hub, and in ``/api/groups``).
``--hub`` works on the hub's own variables, which every project on the hub
inherits; only a hub admin (or the hub's API token) can change them.

``set`` never takes the value on the command line, where shell history and
``ps`` would keep it: it asks for it without echoing it, or reads it from a
file (a PEM key) or, when asked to, from stdin. With no terminal to ask on
and neither of those, it refuses, so a script or an agent cannot type a
secret in by accident.

With ``--url`` it talks to a running exuvia (``exuvia serve``, or a hub
project at ``<hub>/p/<project>``) over its API, with ``EXUVIA_API_TOKEN``
(the server's token or your own agent token) as the bearer token, and only
over HTTPS unless the server is on this machine. Without ``--url`` it opens
the project folder's records directly, the way the server does, which is
safe while a server runs the same project: it changes variables and nothing
else.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

ORIGIN = "cli"


def add_parser(sub: Any) -> None:
    p = sub.add_parser(
        "env",
        help="list, set and remove a project's environment variables (secrets are asked for, never typed as arguments)",
    )
    env = p.add_subparsers(dest="env_cmd", required=True)

    def target(q: argparse.ArgumentParser) -> None:
        q.add_argument("dir", nargs="?", default=".", help="the project folder (default: .), when there is no --url")
        q.add_argument("--url", metavar="URL", help="a running exuvia: its address, or <hub>/p/<project>; sends EXUVIA_API_TOKEN")
        q.add_argument("--allow-http", action="store_true", help="send to a plain-http address on another machine anyway")
        q.add_argument("--group", metavar="ID", help="a hub group's variables (with --url HUB): every project in it gets them")
        q.add_argument("--hub", action="store_true",
                       help="the hub's own variables (with --url HUB; admins change them): every project on it gets them")

    q = env.add_parser("list", help="the variables: names, plain values, never a secret's value")
    target(q)
    q = env.add_parser("set", help="set a variable; the value is asked for without echo, or read from a file or stdin")
    q.add_argument("name")
    target(q)
    q.add_argument("--plain", action="store_true", help="not a secret: its value can be read back (a region, a URL)")
    src = q.add_mutually_exclusive_group()
    src.add_argument("--from-file", metavar="PATH", help="read the value from a file, e.g. a PEM key")
    src.add_argument("--stdin", action="store_true", help="read the value from stdin (a pipe)")
    q = env.add_parser("rm", help="remove a variable")
    q.add_argument("name")
    target(q)
    p.set_defaults(fn=main)


# -- the value ----------------------------------------------------------------------
def read_value(args: argparse.Namespace, stdin: Any = None) -> str:
    stdin = sys.stdin if stdin is None else stdin
    if args.from_file:
        path = Path(args.from_file).expanduser()
        try:
            data = path.read_bytes()
        except OSError as e:
            raise SystemExit(f"cannot read {path}: {e.strerror or e}") from e
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise SystemExit(f"{path} is not a text file: put a binary key in as base64 text") from e
    if args.stdin:
        return stdin.read()
    if not stdin.isatty():
        raise SystemExit(
            "no terminal to type the value in: run this in your own terminal, or pass --from-file PATH "
            "or --stdin. Never put the value on the command line."
        )
    value = getpass.getpass(f"Value for {args.name} (not shown; for a PEM key use --from-file): ")
    if not value:
        raise SystemExit("no value given: nothing changed")
    return value


# -- a running exuvia -------------------------------------------------------------
def check_url(url: str, allow_http: bool) -> str:
    """remote.check_url, for the command line: a refused address exits."""
    from .remote import check_url

    try:
        return check_url(url, allow_http)
    except ValueError as e:
        raise SystemExit(str(e)) from None


class Remote:
    def __init__(self, url: str, allow_http: bool, group: str | None = None, hub: bool = False):
        from .remote import connect

        try:
            self.http = connect(url, allow_http, origin=ORIGIN)
        except ValueError as e:
            raise SystemExit(str(e)) from None
        url = self.http.where
        if group:
            self.url, self.base = f"{url} (group {group})", f"/api/groups/{quote(group, safe='')}"
        elif hub:
            self.url, self.base = f"{url} (the hub's variables)", "/api/hub"
        else:
            self.url, self.base = url, "/api"

    def _call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        from .remote import Unreachable

        try:
            r = self.http.request(method, self.base + path, json=body)
        except Unreachable as e:
            raise SystemExit(str(e)) from e
        if r.status_code == 401:
            raise SystemExit(
                f"{self.url} needs a token: set EXUVIA_API_TOKEN (or EXUVIA_API_TOKEN_FILE) to the server's token "
                "or to your own agent token from the workbench's account menu"
            )
        if r.status_code == 404 and "/api/groups/" in str(r.request.url):
            raise SystemExit(f"{self.url}: {_detail(r)} (is --url the hub's address, and the group's id right?)")
        if r.status_code == 404 and "/api/hub/" in str(r.request.url):
            raise SystemExit(f"{self.url}: no hub variables here (is --url a hub's address? An older hub has none: upgrade it)")
        if r.status_code == 404:
            raise SystemExit(f"no exuvia project at {self.url} (or an exuvia without environment variables: upgrade it)")
        if r.status_code >= 400:
            raise SystemExit(f"{self.url}: {_detail(r)}")
        return r.json()

    def state(self) -> dict[str, Any]:
        return self._call("GET", "/env")

    def set(self, name: str, value: str, secret: bool) -> None:
        self._call("PUT", f"/env/{quote(name)}", {"value": value, "secret": secret})

    def rm(self, name: str) -> bool:
        before = {v["name"] for v in self.state()["variables"]}
        self._call("DELETE", f"/env/{quote(name)}")
        return name in before


def _detail(r: Any) -> str:
    try:
        return str(r.json().get("detail", r.text))
    except ValueError:
        return r.text


# -- a project folder ---------------------------------------------------------------
class Local:
    """The project's records opened as the server opens them, without a
    workspace (which would take over the project's runs)."""

    def __init__(self, root: Path):
        from . import envvars
        from .project import Project
        from .store import open_store

        if not (root / "nodes").is_dir():
            raise SystemExit(f"{root} is not an exuvia project (no nodes/ folder): pass its folder, or --url")
        self.where = str(root)
        project = Project(root)
        self.store = open_store(project)
        try:
            self.env = envvars.Env(self.store, envvars.Keyring(project.state_dir))
        except envvars.SecretsKeyError as e:
            raise SystemExit(str(e)) from e

    def state(self) -> dict[str, Any]:
        from . import envvars

        return {"variables": envvars.public(self.env.rows()), "requests": []}

    def set(self, name: str, value: str, secret: bool) -> None:
        try:
            who: str | None = getpass.getuser()
        except Exception:  # noqa: BLE001 — no user name to record
            who = None
        self.env.set(name, value, secret, who)

    def rm(self, name: str) -> bool:
        return self.env.delete(name)


def main(args: argparse.Namespace) -> None:
    from . import envvars

    if args.env_cmd != "list":
        if "=" in args.name:
            raise SystemExit(
                "give the name only, e.g. `exuvia env set FRED_API_KEY`: the value is asked for, "
                "so it never sits in your shell history"
            )
        try:
            envvars.check_name(args.name)
        except envvars.EnvError as e:
            raise SystemExit(str(e)) from e
    if args.group and args.hub:
        raise SystemExit("--group or --hub, not both")
    if args.group and not args.url:
        raise SystemExit("--group is a hub group's variables: give the hub's address with --url")
    if args.hub and not args.url:
        raise SystemExit("--hub is a hub's own variables: give the hub's address with --url")
    if args.url:
        target: Remote | Local = Remote(args.url, args.allow_http, args.group, args.hub)
        where = target.url
    else:
        from .cli import _load_dotenv

        root = Path(args.dir).resolve()
        _load_dotenv(root)  # EXUVIA_SECRETS_KEY may be in the project's .env, as for `exuvia serve`
        target = Local(root)
        where = target.where

    try:
        if args.env_cmd == "list":
            print_state(target.state(), where)
        elif args.env_cmd == "set":
            value = read_value(args)
            target.set(args.name, value, not args.plain)
            kind = "plain" if args.plain else "secret, encrypted"
            print(f"Set {args.name} ({kind}) in {where}. Runs started from now on get it.")
            if args.group:
                print("Every project in the group gets it, unless the project sets the same name itself.")
            if args.hub:
                print("Every project on the hub gets it, unless the project or its group sets the same name.")
        else:
            print(f"Removed {args.name} from {where}." if target.rm(args.name) else f"{args.name} was not set in {where}.")
    except envvars.EnvError as e:
        raise SystemExit(str(e)) from e


def print_state(state: dict[str, Any], where: str) -> None:
    variables = [*state.get("variables", []), *state.get("inherited", [])]
    if not variables:
        print(f"No variables in {where}.")
    width = max((len(v["name"]) for v in variables), default=0)
    for v in variables:
        if v["secret"]:
            shown = "secret (hidden)" if v.get("readable", True) else "secret: CANNOT BE DECRYPTED, set it again"
        else:
            first = (v["value"] or "").splitlines()[0] if v["value"] else ""
            shown = first + (" …" if v["value"] and "\n" in v["value"].rstrip("\n") else "")
        if v.get("source") == "group":
            shown += f"  (from group {v['group']['name']}{', overridden here' if v.get('overridden') else ''})"
        elif v.get("source") == "hub":
            shown += f"  (from the hub{', overridden' if v.get('overridden') else ''})"
        print(f"{v['name']:<{width}}  {shown}")
    for r in state.get("requests", []):
        why = f": {r['reason']}" if r.get("reason") else ""
        print(f"waiting to be set: {r['name']}{why}  (exuvia env set {r['name']} ...)")
