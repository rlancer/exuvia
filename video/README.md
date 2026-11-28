# The overview video

The source of exuvia's overview film: about two minutes, 1920x1080, made for
YouTube. Nothing here is part of the package or the website.

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

## Making it

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

## What is where

| | |
|---|---|
| `script.json` | the narration, one segment per scene, with the pause after each |
| `src/` | the page: `index.html` (the scenes), `style.css`, `main.js` (the timeline and every animation) |
| `audio/voices.json` | the text each narration file was made from. Not in git, like the audio |
| `assets/` | the mark, the lobster and its shell, exported from `site/src/lib/brand.ts` and `polysvg.ts`; `sea.jpg`, the backdrop, generated with `tools/gen_image.py` from the site's home card |
| `audio/timings.json` | the length of each narration segment as last generated. The audio itself is not in git |
| `tools/` | `audio.py`, `render.mjs`, `gen_image.py` |
| `out/` | what the tools write; not in git |

## Uploading

Upload `out/exuvia-overview.mp4` and add `out/exuvia-overview.srt` as its
English captions.
