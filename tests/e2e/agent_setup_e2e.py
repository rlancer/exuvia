"""Hand the README's setup prompt to a coding agent and check what it built.

This tests the docs as much as the code: the prompt comes verbatim from the
README's "Set up with your agent" section, and the agent gets no other help.
The prompt sends the agent to fetch its steps from the docs site
(``/agent/setup.txt``); the harness serves this checkout's copy of those steps
on localhost instead, so a run tests the README under test, not the one on
the live site (``--live-steps`` keeps the site's).
A run has two sessions, the way a person would use it:

1. **Setup.** A fresh agent session in an empty folder gets the README
   prompt. Afterwards the harness checks that a project exists, that a
   workbench answers, that the agent ran ``hello``, and that the agent's own
   MCP configuration lists an ``exuvia`` server.
2. **Analysis.** A new session in the same folder (so the MCP server the
   agent added is loaded natively) gets a short request for a simple
   analysis. The harness checks that new nodes exist and ran, and whether
   the agent worked through MCP (runs triggered ``mcp``), over HTTP
   (``api``) or around exuvia (files written directly).

If the workbench from step 1 did not outlive the setup session, the harness
starts it before step 2, as the README tells a person to do.
``--cold-analysis`` stops it instead, for the case where the person forgot:
the agent then has to notice (the MCP tools say how to start it) and start
it itself.

The default agent is the Grok Build CLI (installed as ``agent`` or
``grok``); ``--agent-cmd`` and ``--model`` pick another binary and model.
Only the standard library is needed, plus ``uv`` (on PATH or through mise)::

    python tests/e2e/agent_setup_e2e.py                     # grok-4.5, exuvia from PyPI
    python tests/e2e/agent_setup_e2e.py --source local      # this checkout's code
    python tests/e2e/agent_setup_e2e.py --model grok-4.7 --phase setup

``--source local`` builds a wheel from this checkout with a version above
the published one and points uv at it (``UV_FIND_LINKS``), so the agent's
plain ``uvx exuvia ...`` commands get the code under test. Each run gives uv
an empty cache, as a new user has: uvx would otherwise reuse whatever
exuvia it cached before (``--warm-cache`` keeps yours, for quicker runs).

The agent may change its user-level MCP config (``~/.grok/config.toml`` for
Grok). The harness saves that file first and puts it back at the end;
``--keep-config`` leaves the agent's change in place. Everything else the
run makes stays in the work folder, whose path is printed, with the agent's
transcripts and a ``report.json``. Exits non-zero when a check fails. Not
collected by ``pytest`` (no ``test_`` prefix).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
PROJECT = "my-research"
PORTS = range(7420, 7440)
SITE_STEPS = "https://exuvia.lobster.mp/agent/setup.txt"

ANALYSIS_PROMPT = (
    "Use exuvia to build a simple analysis in my-research: make up a year of "
    "daily sales for three stores, total them by store and month, and rank "
    "the stores by yearly total. Run it and tell me which store sold the most."
)


# -- helpers ------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def readme_section(heading: str) -> str:
    text = README.read_text(encoding="utf-8")
    return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def setup_prompt(steps_url: str | None = None) -> str:
    """The fenced ```text block under '## Set up with your agent', with its steps
    URL pointed at ``steps_url`` when one is given."""
    m = re.search(r"```text\n(.*?)```", readme_section("Set up with your agent"), re.S)
    if not m:
        raise SystemExit("no ```text block under 'Set up with your agent' in README.md")
    prompt = m.group(1).strip()
    if steps_url:
        if SITE_STEPS not in prompt:
            raise SystemExit(f"the README's setup prompt no longer links to {SITE_STEPS}")
        prompt = prompt.replace(SITE_STEPS, steps_url)
    return prompt


def setup_steps() -> str:
    """The ```text file=agent/setup.txt block: what the site serves at /agent/setup.txt."""
    m = re.search(r"```text file=agent/setup\.txt\n(.*?)```", readme_section("Set up with your agent"), re.S)
    if not m:
        raise SystemExit("no ```text file=agent/setup.txt block under 'Set up with your agent' in README.md")
    return m.group(1)


def serve_steps() -> str:
    """Serve this checkout's setup steps on localhost; returns their URL."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = setup_steps().encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            ok = self.path == "/agent/setup.txt"
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if ok:
                self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/agent/setup.txt"


def http_json(url: str, timeout: float = 3) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def find_workbench(project_dir: Path) -> str | None:
    """The address of an exuvia serving ``project_dir``, scanning the usual ports."""
    want = project_dir.resolve()
    for port in PORTS:
        base = f"http://127.0.0.1:{port}"
        try:
            snap = http_json(f"{base}/api/project", timeout=1)
        except (OSError, ValueError):
            continue
        root = snap.get("root")
        if root and Path(root).resolve() == want:
            return base
    return None


def free_port() -> int:
    import socket

    for port in PORTS:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("no free port in 7420-7439")


def kill_listener(base: str) -> None:
    """Stop whatever listens on ``base``'s port (the workbench the agent left)."""
    port = base.rsplit(":", 1)[1]
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
        pids = {ln.split()[-1] for ln in out.splitlines() if f":{port} " in ln and "LISTENING" in ln}
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    else:
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
        for pid in out.split():
            subprocess.run(["kill", pid], capture_output=True)


def ensure_uv(env: dict[str, str]) -> None:
    """Put uv/uvx on PATH for the agent the way an installed uv would be."""
    if shutil.which("uvx", path=env["PATH"]):
        return
    mise = shutil.which("mise")
    if mise:
        where = subprocess.run([mise, "where", "uv"], capture_output=True, text=True, cwd=REPO).stdout.strip()
        for d in (Path(where), Path(where) / "bin") if where else ():
            if shutil.which("uvx", path=str(d)):
                env["PATH"] = str(d) + os.pathsep + env["PATH"]
                log(f"uvx from mise: {d}")
                return
    raise SystemExit("uvx is not on PATH (install uv, or mise in this repo)")


def build_local_wheel(out: Path, env: dict[str, str]) -> str:
    """A wheel of this checkout, versioned just above the published release."""
    src = out / "src"
    shutil.copytree(
        REPO, src, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "site", "ui", "__pycache__", ".pytest_cache")
    )
    # The version is the latest release tag (hatch-vcs), plus a local label
    # that sorts above it; the copy has no .git, so it is passed in.
    tags = subprocess.run(["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"], capture_output=True, text=True, cwd=REPO)
    base = (tags.stdout.split() or ["v0.0.0"])[0].removeprefix("v")
    version = f"{base}+e2e{time.strftime('%Y%m%d%H%M%S')}"
    wheels = out / "wheels"
    build_env = {**env, "SETUPTOOLS_SCM_PRETEND_VERSION": version}
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(wheels), str(src)], check=True, env=build_env, cwd=out)
    env["UV_FIND_LINKS"] = str(wheels)
    return version


def is_grok(agent_cmd: str) -> bool:
    return Path(agent_cmd).stem.lower() in ("agent", "grok")


def agent_mcp_list(args: argparse.Namespace, cwd: Path, env: dict[str, str]) -> str:
    r = subprocess.run([args.agent_cmd, "mcp", "list"], capture_output=True, text=True, cwd=cwd, env=env, timeout=60)
    return (r.stdout + r.stderr).strip()


def run_agent(args: argparse.Namespace, name: str, prompt: str, cwd: Path, env: dict[str, str], logs: Path) -> dict[str, Any]:
    """One headless agent session; the transcript goes to ``logs/<name>.*``."""
    (logs / f"{name}.prompt.txt").write_text(prompt, encoding="utf-8")
    cmd = [
        args.agent_cmd, "-p", prompt, "-m", args.model, "--always-approve",
        "--output-format", "streaming-json", "--cwd", str(cwd),
    ]  # fmt: skip
    if args.effort:
        cmd += ["--reasoning-effort", args.effort]
    if is_grok(args.agent_cmd):
        # Headless Grok otherwise waits 600 s for background work at the end
        # of the turn and then stops it, which takes the workbench down.
        cmd += ["--no-wait-for-background"]
    log(f"{name}: {args.agent_cmd} -m {args.model} (timeout {args.timeout}s) ...")
    started = time.time()
    stream = logs / f"{name}.jsonl"
    with stream.open("w", encoding="utf-8") as fh:
        try:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=cwd, env=env, timeout=args.timeout)
            code: int | str = proc.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    seconds = round(time.time() - started)
    text, tools = summarize_stream(stream)
    (logs / f"{name}.final.txt").write_text(text, encoding="utf-8")
    (logs / f"{name}.tools.txt").write_text("\n".join(tools), encoding="utf-8")
    log(f"{name}: exit {code} after {seconds}s, {len(tools)} tool calls")
    print("-" * 60 + f"\n{text[-3000:]}\n" + "-" * 60, flush=True)
    return {"exit": code, "seconds": seconds, "tool_calls": len(tools), "tools": tools}


def summarize_stream(path: Path) -> tuple[str, list[str]]:
    """The agent's final text and a one-line digest per tool call, from NDJSON."""
    chunks: list[str] = []
    tools: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        found = _walk(ev)
        for kind, val in found:
            if kind == "text":
                chunks.append(val)
            elif kind == "tool":
                tools.append(val)
    return "".join(chunks).strip(), tools


def _walk(ev: Any) -> list[tuple[str, str]]:
    """Text deltas and tool calls from one event: Grok's flat stream
    (``{"type": "text", "data": ...}``) or an ACP ``session/update``."""
    if not isinstance(ev, dict):
        return []
    upd = ev.get("update") or (ev.get("params") or {}).get("update") or ev
    kind = upd.get("sessionUpdate") or upd.get("type")
    if kind == "text" and isinstance(upd.get("data"), str):
        return [("text", upd["data"])]
    if kind in ("agent_message_chunk", "agent_message"):
        content = upd.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            return [("text", content.get("text", ""))]
    if kind == "tool_call":
        title = upd.get("toolName") or upd.get("title") or upd.get("kind") or "?"
        raw = upd.get("rawInput")
        return [("tool", f"{title} {json.dumps(raw)[:300] if raw is not None else ''}".strip())]
    return []


# -- checks -------------------------------------------------------------------
class Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append({"check": name, "ok": ok, "detail": detail})
        log(f"{'PASS' if ok else 'FAIL'}  {name}{f' — {detail}' if detail else ''}")
        return ok

    @property
    def ok(self) -> bool:
        return all(i["ok"] for i in self.items)


def node_states(base: str) -> dict[str, str]:
    return {n["name"]: n.get("state", "?") for n in http_json(f"{base}/api/project")["nodes"]}


def runs(base: str) -> list[dict[str, Any]]:
    return http_json(f"{base}/api/runs")


def start_workbench(project_dir: Path, env: dict[str, str], logs: Path, port: int | None) -> tuple[subprocess.Popen, str]:
    """``uvx exuvia serve`` as the README tells a person to before a later
    session, on the port the agent's MCP entry relays to when there is one."""
    port = port or free_port()
    out = (logs / "serve.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        ["uvx", "exuvia", "serve", str(project_dir), "--port", str(port)],
        stdout=out, stderr=subprocess.STDOUT, env=env, cwd=project_dir.parent,
    )  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            http_json(f"{base}/api/guide")
            return proc, base
        except (OSError, ValueError):
            time.sleep(1)
    proc.kill()
    raise SystemExit(f"exuvia serve did not answer on {base}; see {logs / 'serve.log'}")


# -- main ---------------------------------------------------------------------
def main() -> int:
    # Agents' replies carry arrows and dashes; a Windows console or a
    # redirected stdout would otherwise be cp1252 and fail on them.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    default_agent = shutil.which("agent") or shutil.which("grok") or "agent"
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--agent-cmd", default=os.environ.get("E2E_AGENT", default_agent))
    ap.add_argument("--model", default=os.environ.get("E2E_MODEL", "grok-4.5"))
    ap.add_argument("--effort", default=None, help="reasoning effort passed to the agent")
    ap.add_argument("--source", choices=["pypi", "local"], default="pypi", help="exuvia from PyPI or this checkout")
    ap.add_argument("--phase", choices=["setup", "analysis", "both"], default="both")
    ap.add_argument("--workdir", type=Path, help="folder to run in (default: a new temp folder)")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per agent session")
    ap.add_argument("--analysis-prompt", default=ANALYSIS_PROMPT)
    ap.add_argument(
        "--cold-analysis", action="store_true",
        help="stop the workbench before the analysis session: the agent has to start it",
    )  # fmt: skip
    ap.add_argument(
        "--warm-cache", action="store_true",
        help="use your own uv cache instead of an empty one (faster; not with --source local)",
    )  # fmt: skip
    ap.add_argument(
        "--live-steps", action="store_true",
        help="let the agent fetch the setup steps from the live site instead of this checkout's README",
    )  # fmt: skip
    ap.add_argument("--keep-config", action="store_true", help="leave the agent's MCP config change in place")
    ap.add_argument("--keep-server", action="store_true", help="leave the workbench running at the end")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = (args.workdir or Path(os.environ.get("TEMP", "/tmp")) / f"exuvia-agent-e2e-{stamp}").resolve()
    logs = work.parent / f"{work.name}-logs"
    work.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    project_dir = work / PROJECT
    log(f"work folder: {work}\n[e2e] logs:        {logs}")

    env = dict(os.environ)
    ensure_uv(env)
    if not args.warm_cache:
        # A new user's cache is empty, and uvx reuses whatever exuvia it has
        # cached, which would hide the build under test.
        env["UV_CACHE_DIR"] = str(logs / "uv-cache")
    report: dict[str, Any] = {"agent": args.agent_cmd, "model": args.model, "source": args.source, "work": str(work)}
    if args.source == "local":
        report["version_under_test"] = build_local_wheel(logs, env)
        log(f"local wheel {report['version_under_test']} via UV_FIND_LINKS")

    user_cfg = Path.home() / ".grok" / "config.toml"
    saved_cfg = user_cfg.read_bytes() if user_cfg.exists() else None
    checks = Checks()
    serve_proc: subprocess.Popen | None = None
    base: str | None = None
    mcp_url: str | None = None
    try:
        if args.phase in ("setup", "both"):
            steps_url = None if args.live_steps else serve_steps()
            report["setup_steps"] = steps_url or SITE_STEPS
            report["setup"] = run_agent(args, "setup", setup_prompt(steps_url), work, env, logs)
            checks.add("project created", (project_dir / "nodes" / "hello.py").exists(), str(project_dir))
            base = find_workbench(project_dir)
            # Informational: whether a background process outlives a headless
            # session is up to the agent CLI, not to the docs.
            report["workbench_survived_setup"] = base
            log(f"workbench after setup: {base or 'not running'}")
            mcp = agent_mcp_list(args, work, env)
            (logs / "mcp-list.txt").write_text(mcp, encoding="utf-8")
            line = next((ln.strip() for ln in mcp.splitlines() if "exuvia" in ln.lower()), "")
            checks.add("agent's MCP config has an `exuvia` server", bool(line), line or mcp[:200])
            m = re.search(r"--url\s+(\S+)", line)
            mcp_url = m.group(1).rstrip("/") if m else None
            cfg_now = user_cfg.read_bytes() if user_cfg.exists() else None
            report["user_config_changed"] = cfg_now != saved_cfg
            for name in (".grok/config.toml", ".mcp.json", ".cursor/mcp.json"):
                if (work / name).exists():
                    report.setdefault("project_configs", []).append(name)

        port = int(mcp_url.rsplit(":", 1)[1]) if mcp_url and re.search(r":\d+$", mcp_url) else None

        def open_workbench(why: str) -> str | None:
            nonlocal serve_proc
            found = find_workbench(project_dir)
            if found or not project_dir.exists():
                return found
            log(f"starting the workbench ({why})")
            serve_proc, found = start_workbench(project_dir, env, logs, port)
            return found

        base = base or open_workbench("as the README tells a person to before a later session")
        if base:
            cfg = http_json(f"{base}/api/config")
            report["exuvia_version"] = cfg.get("version")
            if args.source == "local":
                checks.add("workbench runs the local build", cfg.get("version") == report["version_under_test"], cfg.get("version", ""))
            if args.phase in ("setup", "both"):
                hello_ok = any(r["node"] == "hello" and r["status"] == "success" for r in runs(base))
                checks.add("agent ran `hello` successfully", hello_ok)

        if args.phase in ("analysis", "both") and base:
            before = set(node_states(base))
            seen_runs = {r["id"] for r in runs(base)}
            if args.cold_analysis:
                # The person forgot to start the workbench: the agent has to
                # notice (the MCP tools say so) and start it itself.
                kill_listener(base)
                if serve_proc:
                    serve_proc.kill()
                    serve_proc = None
                log("workbench stopped before the analysis session (--cold-analysis)")
            report["analysis"] = run_agent(args, "analysis", args.analysis_prompt, work, env, logs)
            agents = find_workbench(project_dir)
            report["analysis"]["workbench_after"] = agents
            if args.cold_analysis:
                log(f"workbench after analysis: {agents or 'not running'}")
            base = agents or open_workbench("to read the results")
            states = node_states(base)
            new = {n: s for n, s in states.items() if n not in before}
            report["analysis"]["nodes"] = states
            checks.add("analysis added nodes", len(new) >= 2, json.dumps(new))
            checks.add("every new node is fresh", bool(new) and all(s == "fresh" for s in new.values()), json.dumps(new))
            fresh_runs = [r for r in runs(base) if r["id"] not in seen_runs]
            triggers: dict[str, int] = {}
            for r in fresh_runs:
                triggers[r.get("trigger") or "?"] = triggers.get(r.get("trigger") or "?", 0) + 1
            report["analysis"]["run_triggers"] = triggers
            checks.add("analysis ran nodes through MCP", triggers.get("mcp", 0) > 0, json.dumps(triggers))
    finally:
        if base and not args.keep_server:
            kill_listener(base)
        if serve_proc:
            serve_proc.kill()
        if not args.keep_config:
            if saved_cfg is None:
                user_cfg.unlink(missing_ok=True)
            else:
                user_cfg.write_bytes(saved_cfg)
        if not args.warm_cache:
            shutil.rmtree(logs / "uv-cache", ignore_errors=True)
        report["checks"] = checks.items
        (logs / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        log(f"report: {logs / 'report.json'}")

    log("all checks passed" if checks.ok else "some checks failed")
    return 0 if checks.ok else 1


if __name__ == "__main__":
    sys.exit(main())
