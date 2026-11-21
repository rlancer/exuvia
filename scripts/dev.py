"""Fast local setup for a clone and every git worktree of it.

    python scripts/dev.py sync       # uv sync --extra dev + bun install (ui/)
    python scripts/dev.py ui-build   # the UI and views runtime into exuvia/static
    python scripts/dev.py demo       # ui-build, then exuvia serve examples/markets

`mise run sync`, `mise run ui-build` and `mise run demo` call this. Two
things make a fresh worktree start in seconds rather than minutes:

- uv's and bun's package caches live in the clone's shared git dir
  (`.git/exuvia-cache/`), on the same drive as every worktree, so installs
  hardlink files out of the cache instead of copying them (a cache on
  another drive, the default on Windows when the clone is not on C:, means
  copying ~20k files per worktree).
- The built UI is kept per content of `ui/` (a git tree hash, so uncommitted
  edits count), shared by all worktrees: a worktree that has not touched
  the UI copies the build in instead of running Vite. A build skips `tsc`
  (`cd ui && bun run build` and CI still typecheck) and runs the app and the
  views runtime builds side by side.

Standard library only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
STATIC = ROOT / "exuvia" / "static"
KEEP_BUILDS = 8


def git(*args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def cache_dir() -> Path:
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    path = Path(common) / "exuvia-cache"
    path.mkdir(exist_ok=True)
    return path


def tool_env() -> dict[str, str]:
    cache = cache_dir()
    env = dict(os.environ)
    env.setdefault("UV_CACHE_DIR", str(cache / "uv"))
    env.setdefault("BUN_INSTALL_CACHE_DIR", str(cache / "bun"))
    return env


def run(cmd: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    exe = shutil.which(cmd[0]) or cmd[0]  # bun/uv are .exe shims on Windows
    code = subprocess.run([exe, *cmd[1:]], cwd=cwd, env=env).returncode
    if code:
        sys.exit(code)


def sync() -> None:
    env = tool_env()
    run(["uv", "sync", "--extra", "dev"], env=env)
    run(["bun", "install"], cwd=UI, env=env)


def ui_key(cache: Path) -> str:
    """Tree hash of ui/ as it is on disk (tracked + untracked, minus ignored)."""
    env = dict(os.environ, GIT_INDEX_FILE=str(cache / f"ui-index-{os.getpid()}"))
    try:  # a missing index file is an empty index
        git("add", "--all", "--", "ui", env=env)
        return git("write-tree", env=env)
    finally:
        Path(env["GIT_INDEX_FILE"]).unlink(missing_ok=True)


def build_ui(dest: Path, runtime: Path, env: dict[str, str]) -> None:
    """The app into dest and the views runtime into runtime, side by side;
    the runtime then moves to dest/view-runtime (the app build empties its
    outDir, so the two cannot share one while running)."""
    run(["bun", "install"], cwd=UI, env=env)
    bun = shutil.which("bun") or "bun"
    procs = [
        subprocess.Popen([bun, "run", "vite", "build", *args], cwd=UI, env=env)
        for args in (
            ["--outDir", str(dest), "--emptyOutDir"],
            ["-c", "vite.runtime.config.ts", "--outDir", str(runtime), "--emptyOutDir"],
        )
    ]
    if any([p.wait() for p in procs]):
        sys.exit("ui build failed")
    runtime.rename(dest / "view-runtime")


def ui_build() -> None:
    env = tool_env()
    builds = cache_dir() / "ui"
    builds.mkdir(exist_ok=True)
    key = ui_key(builds.parent)
    cached = builds / key
    if cached.is_dir():
        print(f"ui: reusing the build of this ui/ ({key[:10]})")
    else:
        start = time.monotonic()
        tmp = builds / f"{key}.tmp-{os.getpid()}"
        runtime = builds / f"{key}.tmp-{os.getpid()}-runtime"
        try:
            build_ui(tmp, runtime, env)
            tmp.rename(cached)
        except OSError:  # another worktree built the same ui/ meanwhile
            if not cached.is_dir():
                raise
        finally:
            for leftover in (tmp, runtime):
                shutil.rmtree(leftover, ignore_errors=True)
        print(f"ui: built in {time.monotonic() - start:.0f}s ({key[:10]})")
    os.utime(cached)
    shutil.rmtree(STATIC, ignore_errors=True)
    shutil.copytree(cached, STATIC)
    old = sorted((p for p in builds.iterdir() if p.is_dir() and ".tmp-" not in p.name),
                 key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_BUILDS:]
    for path in old:
        shutil.rmtree(path, ignore_errors=True)


def demo() -> None:
    ui_build()
    run(["uv", "run", "--extra", "dev", "exuvia", "serve", "examples/markets"], env=tool_env())


COMMANDS = {"sync": sync, "ui-build": ui_build, "demo": demo}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: python scripts/dev.py {{{'|'.join(COMMANDS)}}}")
    COMMANDS[sys.argv[1]]()
