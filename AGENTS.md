# AGENTS

Instructions for coding agents working in this repo. `README.md` is the
reference for what exuvia is and how it is built, run and deployed.
`ui/AGENTS.md` says how the UI is built (Astryx components and StyleX, and
nothing else) and what its hierarchy is: read it before changing `ui/`.

## Every change ships as a PR against `main`

- Start a feature branch from current `origin/main`, commit there, push it
  and open a pull request with `gh pr create --base main`. Never push
  directly to `main`.
- **Don't ask first.** Committing, pushing and opening the PR are part of
  finishing the task, not a separate decision: when a change is ready, open
  the PR (or push to the task's open PR) without stopping to ask "commit?",
  "push?" or "open a PR?". A task is not done while its changes sit
  uncommitted or on a branch with no PR.
- **Turn on auto-merge.** Right after `gh pr create --base main`, run
  `gh pr merge <number> --auto --squash` so the PR squash-merges by itself
  once CI is green. Don't ask first, and don't merge by hand before the
  checks pass. A merge to `main` deploys the website
  (`.github/workflows/deploy-site.yml`). A merge to `main` that changes the
  package (`exuvia/`, `ui/`, `pyproject.toml`, `uv.lock`, the Dockerfile) is
  a release: `release.yml` tags the next patch version and publishes to
  PyPI (`publish.yml`) and GHCR (`image.yml`), and neither can be undone.
  The version comes from that tag; never write one into a file. A
  `release:minor`, `release:major` or `release:skip` label on the PR
  changes the bump; add one only when asked to. If GitHub says "Auto merge
  is not allowed for this repository", leave the PR open and say so.
- **Fix red CI.** After opening the PR, watch its checks
  (`gh pr checks <number> --watch`). If one fails, read the log, fix it on
  the same branch and push; auto-merge picks the PR up once it is green.
  Fix what the failure points at, nothing more. Stop after 3 failed
  attempts and report which check fails and why.
- Report the PR link when you finish, with whether auto-merge is on, whether
  CI is green, what you tested and what you did not.
