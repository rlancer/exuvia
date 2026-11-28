# The videos

This folder makes exuvia's videos: the overview film, below, and the help
videos, short tutorials recorded from the real app ([Help videos](#help-videos)).
Nothing here is part of the package or the website.

## The overview film

The source of exuvia's overview film: about two minutes, 1920x1080, made for
YouTube.

The picture is a web page, `src/index.html`, that is a pure function of time:
`seek(t)` in `src/main.js` puts every element where it belongs at second `t`.
`tools/render.mjs` opens the page in the Chrome or Edge already on the
machine, calls `seek` once per frame and pipes the screenshots into ffmpeg.
The product shots are the website's own (`site/src/assets/shots/`), so the
film shows the same workbench the site does; the graph, the code and the
captions are drawn by the page in the site's colours and typefaces.

The sound comes from ElevenLabs (`tools/audio.py`): the narration from its
text to speech (the Matilda voice, `eleven_v4`), one file per segment of
`script.json`, and the music from its music endpoint, with Google's Lyria
through OpenRouter as the fallback if the account has no music. Each scene
lasts as long as its line takes to say, so the narration is generated first
and the picture follows it. `said(id, phrase)` in `main.js` puts a moment of
a scene on a word of its line.

### Making it

Needs ffmpeg on the PATH and Chrome or Edge (or `CHROME_PATH` naming a
Chromium). The sound needs network access to `api.elevenlabs.io` (and
`openrouter.ai` for the fallback) through a proxy that adds the API keys:
`audio.py` sends a placeholder and reads no key itself. The narration is
billed by the character, about 1,600 for the whole script.

```bash
cd video
bun install                              # playwright-core and the two typefaces
python tools/audio.py --dry-run          # what it would generate, and the characters it costs
python tools/audio.py                    # audio/: narration, music, timings.json, mix.wav; out/exuvia-overview.srt
node tools/render.mjs --fps 60           # out/exuvia-overview.mp4
```

- `node tools/render.mjs --serve` serves the page: open the address it prints
  and the film plays in the browser, without sound. `?t=12` holds the frame
  at 12 seconds, `?from=40` starts playing there.
- `node tools/render.mjs --stills 3,17.5,60` writes single frames to
  `out/stills/`, and `--stills scenes` two per scene.
- `python tools/audio.py` only voices the segments that have no file yet or
  whose text changed since theirs was made (`audio/voices.json` keeps the
  text each was made from), so editing one line pays for one line.
  `voice nodes run` voices those segments again whatever they say; `music`
  makes new music; `mix` only mixes. Render again afterwards: the scene
  lengths follow `audio/timings.json`.
- A scene that needs a product shot uses the site's
  (`site/src/assets/shots/`); one that doesn't exist yet is added to
  `site/tools/capture.mjs` and captured, never drawn by hand.

#### In 4K

`--scale 2` renders the same 1920x1080 layout at 3840x2160 (with
`--stills` too, to check frames at full size). The type, lines and
artwork are drawn at that size; the product shots are pictures, and the
site's (2880x1800) are smaller than a 1600x1000 window at twice the
pixels. So, for 4K, capture the shots the film uses at 3x into
`video/shots/`, beside the site's and not over them. The page uses a shot
from there wherever one of the same name exists, and the site's otherwise:

```bash
# serve the example and a hub as site/tools/capture.mjs describes, then, in site/:
node tools/capture.mjs app http://127.0.0.1:7431 --scale 3 --out ../video/shots
node tools/capture.mjs hub http://127.0.0.1:7432 --scale 3 --out ../video/shots
# in video/:
node tools/render.mjs --fps 60 --scale 2 # out/exuvia-overview.mp4, 3840x2160
```

`app` mode sends the agent a real prompt, and what the agent builds varies.
Check that the highlight rings still sit on what they point at (`main.js`
gives each one's position as a share of its shot). If a take doesn't fit, delete that one
shot from `video/shots/`, and the site's is used for it.

### What is where

| | |
|---|---|
| `script.json` | the narration, one segment per scene, with the pause after each |
| `src/` | the page: `index.html` (the scenes), `style.css`, `main.js` (the timeline and every animation) |
| `audio/voices.json` | the text each narration file was made from. Not in git, like the audio |
| `assets/` | the mark, the lobster and its shell, exported from `site/src/lib/brand.ts` and `polysvg.ts`; `sea.jpg`, the backdrop, generated with `tools/gen_image.py` from the site's home card |
| `audio/timings.json` | the length of each narration segment as last generated. The audio itself is not in git |
| `tools/` | `audio.py`, `render.mjs`, `gen_image.py` |
| `shots/` | the product shots at 3x, for a 4K render; not in git |
| `out/` | what the tools write; not in git |

### Uploading

Upload `out/exuvia-overview.mp4` and add `out/exuvia-overview.srt` as its
English captions.

## Help videos

A help video is one task, shown in the real app, tied to the docs page it
explains (`help/<name>/script.json` names it under `docs`). It is not
animated from screenshots: Playwright drives a real `exuvia` and a real
shell while the camera films them, so when the UI changes, the video is
recorded again by running its script, not by hand.

| | |
|---|---|
| `help/<name>/script.json` | the narration: one segment per beat, in order, with the pause after each |
| `help/<name>/video.mjs` | the actions: `beat(id, fn)` per segment, the clicks and typing that line describes |
| `help/<name>/audio/timings.json` | each line's length as last voiced; the voices themselves are not in git |
| `help/tools/record.mjs` | records a video: runs `video.mjs`, films it, and puts the narration under it |
| `help/tools/studio.mjs` | the camera (Chrome's screencast, one page at a time), the pointer, typing, the beat clock |
| `help/tools/terminal.py`, `terminal.html` | a real terminal: bash in a pseudo-terminal, drawn with xterm.js |
| `help/tools/cursor.js` | the pointer, click rings and keycaps drawn into each page (headless Chrome draws none) |
| `help/tools/card.html` | the title card a video opens and closes on |

How the picture keeps to the voice: `beat(id, fn)` starts a line, runs its
actions while it is said, and ends when both the line (and its pause) and
the actions are done. A run or the agent may take longer than the line; the
picture goes on and the next line waits for it. The moment each beat started
is written to `out/help/<name>/timeline.json`, and `tools/audio.py` lays each
voice file there, so the sound follows the picture. `said(phrase)` waits for
a word of the current line (estimated from where it sits in the text), and
`fast(4, fn)` plays a long wait four times faster, with a badge saying so.

Everything on screen is real. Commands run in bash with a fresh home folder
(`/tmp/exuvia-<name>`, so the paths exuvia prints stay short) and uv's own
cache; `uvx exuvia` there is the release on PyPI, which every merge to
`main` that touches the package publishes. The browser is 1280x720 at 1.5x,
so the recording is 1920x1080 with text that reads on a phone, and dark, as
every help video is. A bug that shows on camera is filed as an issue and
left showing, not hidden by the script.

### Recording one

Needs ffmpeg, a Chrome (or `CHROME_PATH`), `uv` and Python 3, and network
access to PyPI. A video that turns the agent on uses OpenRouter, and the
voice uses ElevenLabs, both through a proxy that adds the keys, as for the
overview.

```bash
cd video
bun install
node help/tools/record.mjs getting-started --draft --stills    # silent, timed from estimates: out/help/getting-started-draft.mp4
python tools/audio.py --script help/getting-started/script.json --dry-run   # the characters the voice would cost
python tools/audio.py --script help/getting-started/script.json voice       # voices the lines that changed
node help/tools/record.mjs getting-started                     # out/help/getting-started.mp4 and .srt
```

`--draft` needs no voice, so the wording and the actions can be settled
before any is paid for. `--stills` also writes a frame per beat to
`out/help/<name>-stills/`. As with the overview, `audio.py voice` only voices
the lines whose text changed.

A new video is a folder in `help/` with a `script.json` and a `video.mjs`
that exports `async function film(studio, { stage })`; `getting-started`
shows the pieces.

### Uploading

Upload `out/help/<name>.mp4` with `out/help/<name>.srt` as its English
captions.
