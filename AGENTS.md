# AGENTS

Instructions for coding agents working in this repo. `README.md` is the
reference for what exuvia is and how it is built, run and deployed.

## Every change ships as a PR against `main`

- Start a feature branch from current `origin/main`, commit there, push it
  and open a pull request with `gh pr create --base main`. Never push
  directly to `main`.
- **Don't ask first.** Committing, pushing and opening the PR are part of
  finishing the task, not a separate decision: when a change is ready, open
  the PR (or push to the task's open PR) without stopping to ask "commit?",
  "push?" or "open a PR?". A task is not done while its changes sit
  uncommitted or on a branch with no PR.
- **Don't merge.** Leave the PR open for the maintainer to review and merge,
  unless they ask you to merge it. A push to `main` deploys the website
  (`.github/workflows/deploy-site.yml`), and a `v*` tag publishes to PyPI
  (`publish.yml`).
- Report the PR link when you finish, with what you tested and what you
  did not.
