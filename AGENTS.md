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

## Publishing videos to YouTube

`video/README.md` says how the videos are made. They go to the channel
@lobstermp through YouTube Studio in Chrome (the Claude in Chrome tools).

- **Use the `_4k` files.** Finished renders are in `~/Videos` as
  `<name>_4k.mp4`, `<name>_4k.srt` and `<name>_4k-chapters.txt`. Upload
  those. A file of the same name without `_4k` is an older 1080p take whose
  captions and chapters no longer match the picture.
- **Check the channel before every upload.** The Google account in Chrome
  manages several channels and Studio opens on another one. Open
  `https://studio.youtube.com/channel/UCcV_9kEYeFa5ea-PNuRHRnQ` (the name
  under the avatar reads "Lobster"), or switch with avatar → Switch account
  → "Lobster @lobstermp".
- **Getting the mp4 in.** `file_upload` takes under 10 MB a call, a
  `browser_batch` counts as one call, and Chrome refuses a page's `fetch`
  from `127.0.0.1`, so the video goes in as pieces:
  1. `split -b 7M -d` the mp4 into the session's scratchpad.
  2. With `javascript_tool`, add an `<input type="file">` to the Studio
     page whose `change` handler keeps each `File` in a `window` object by
     name. `file_upload` every piece to it, one piece per call.
  3. Join them: `new File(pieces, '<name>_4k.mp4', {type: 'video/mp4'})`.
     Compare its size and SHA-256 (`crypto.subtle.digest`) with the file on
     disk, inside the page: the tool blanks a hash it is asked to return.
  4. Create → Upload videos, then put the `File` on the dialog's
     `input[name=Filedata]` through a `DataTransfer` and dispatch `change`.
     Stay on the page until then: a navigation loses the pieces.
- **Details.** Title and description as given (the chapters file goes into
  the description unchanged); "No, it's not made for kids"; under Show
  more: the tags, Video language English, Caption certification "This
  content has never aired on television in the U.S.", Category "Science &
  Technology".
- **Subtitles.** Video elements → Subtitles → Add → Upload file → "With
  timing". Continue opens a native file chooser by calling `click()` on
  `#captions-file-loader`: replace that input's `click` with a no-op, press
  Continue, `file_upload` a copy of the `.srt` from the scratchpad to that
  input, then Done.
- **Visibility: Unlisted.** Never Public, scheduled or premiered unless
  asked. Save once the line at the foot of the dialog has moved on from
  "Uploading n%" to "Processing" or "Checks complete"; the processing can
  finish after the save.
- **Leave alone** the channel's settings, monetization, end screens, cards,
  playlists and every other video. Under the description Studio says
  external links need a one-time verification to be clickable: that is the
  owner's to do, and the upload saves without it.
- **Check the watch page** (`https://www.youtube.com/watch?v=<id>`): the
  title, the "Unlisted" badge, English captions playing, and the chapters
  in the progress bar. The chapters lag the save: for more than ten
  minutes the description's timestamps are links while the bar is still
  one piece. Look again later before calling them missing.
- Report each video's title, ID and `youtu.be/<id>` link.
