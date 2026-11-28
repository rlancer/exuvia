"""The film's sound, from ElevenLabs: the narration, the music, and the mix.

    python tools/audio.py                    # the narration that is missing or whose text changed, music if missing, the mix
    python tools/audio.py --dry-run          # what that would generate, and how many characters it costs
    python tools/audio.py voice [id ...]     # the narration again (these segments, or every one)
    python tools/audio.py music              # the music again
    python tools/audio.py mix                # only the mix, from the files in audio/

    python tools/audio.py --script help/getting-started/script.json voice     # a help video's narration
    python tools/audio.py --script help/getting-started/script.json --timeline out/help/getting-started/timeline.json mix

Writes audio/voice-<id>.mp3 per segment of script.json, audio/voices.json (the
text each of those was made from, so a run only pays for lines that changed),
audio/music.<ext>, audio/timings.json (each segment's length, which sets the
length of its scene: src/main.js reads it), audio/mix.wav (what
tools/render.mjs puts under the picture) and out/exuvia-overview.srt
(captions to upload with the video).

The narration is ElevenLabs text to speech, the music its music endpoint
(with OpenRouter's Lyria as the fallback when the account has no music). The
API keys are not read here: the network proxy this runs behind adds them, so
the requests carry a placeholder. EXUVIA_VIDEO_VOICE_ID picks the voice
(default Matilda), EXUVIA_VIDEO_VOICE_MODEL the model (default eleven_v4).
A help video (help/<name>/script.json, see help/tools/record.mjs) uses the
same voice: --script names its script, its audio/ folder sits beside it, its
captions go to out/help/<name>.srt, and it has no music. --timeline gives the
moment each line starts in the recording (record.mjs writes it), in place of
laying the lines end to end.

Standard library and ffmpeg only.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

VIDEO = Path(__file__).resolve().parent.parent
argv = sys.argv[1:]


def option(name):
    """Takes `--name value` out of argv and returns the value (None when absent)."""
    if f"--{name}" not in argv:
        return None
    i = argv.index(f"--{name}")
    value = argv[i + 1]
    del argv[i : i + 2]
    return Path(value).resolve()


SCRIPT = option("script") or VIDEO / "script.json"
TIMELINE = option("timeline")
AUDIO = SCRIPT.parent / "audio"
HELP = SCRIPT != VIDEO / "script.json"
OUT = VIDEO / "out" / "help" if HELP else VIDEO / "out"
CAPTIONS = OUT / (f"{SCRIPT.parent.name}.srt" if HELP else "exuvia-overview.srt")
ELEVENLABS = "https://api.elevenlabs.io/v1"
OPENROUTER = "https://openrouter.ai/api/v1"
# The proxy replaces these with the real keys; nothing secret is in this file or its environment.
ELEVENLABS_HEADERS = {"xi-api-key": "proxy-injected"}
OPENROUTER_HEADERS = {"Authorization": "Bearer proxy-injected"}
VOICE = os.environ.get("EXUVIA_VIDEO_VOICE_ID", "XrExE9yKIg1WjnnlVkGX")  # Matilda
VOICE_MODEL = os.environ.get("EXUVIA_VIDEO_VOICE_MODEL", "eleven_v4")
LYRIA = os.environ.get("EXUVIA_VIDEO_MUSIC_MODEL", "google/lyria-3-pro-preview")
MUSIC_PROMPT = (
    "A {seconds} second instrumental underscore for a calm, confident software product film. "
    "Deep-sea atmosphere: warm analog synth pads and a soft sub bass open it; after about eight "
    "seconds a gentle pulsing arpeggio and light, tight electronic percussion come in and build "
    "slowly, with a hopeful melody on felt piano and plucked synth. Modern, minimal, spacious, in "
    "a major key at about 100 bpm. It leaves room for a narrator throughout, lifts in the last ten "
    "seconds and ends on a clean, resolved final chord at about {seconds} seconds. Instrumental "
    "only, no vocals."
)

script = json.loads(SCRIPT.read_text(encoding="utf-8"))
WANTS_MUSIC = script.get("music", True)
segments = script["segments"]
MADE_FROM = AUDIO / "voices.json"


class Refused(Exception):
    """The service answered, but no: the status and what it said."""


def post(url, body, headers, timeout=600):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={**headers, "Content-Type": "application/json"}
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise Refused(f"{url} answered {error.code}: {error.read().decode(errors='replace')[:1500]}") from None


def seconds(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def made_from():
    return json.loads(MADE_FROM.read_text(encoding="utf-8")) if MADE_FROM.exists() else {}


def stale(only):
    """The segments to voice: `only` if given, else those with no file or whose text changed since theirs was made."""
    have = made_from()
    if only:
        return [s for s in segments if s["id"] in only]
    return [s for s in segments if not (AUDIO / f"voice-{s['id']}.mp3").exists() or have.get(s["id"]) != s["text"]]


def voice(todo):
    have = made_from()
    for segment in todo:
        target = AUDIO / f"voice-{segment['id']}.mp3"
        raw = AUDIO / f"raw-{segment['id']}.mp3"
        url = f"{ELEVENLABS}/text-to-speech/{VOICE}?output_format=mp3_44100_128"
        try:
            with post(url, {"text": segment["text"], "model_id": VOICE_MODEL}, ELEVENLABS_HEADERS) as response:
                raw.write_bytes(response.read())
        except Refused as error:
            sys.exit(f"voice {segment['id']}: {error}")
        # Trim the silence the model leaves at either end, so a scene's length is the line's.
        trim = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.03"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", f"{trim},areverse,{trim},areverse", "-b:a", "192k", str(target)],
            check=True,
        )
        raw.unlink()
        have[segment["id"]] = segment["text"]
        MADE_FROM.write_text(json.dumps(have, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{target.name}: {seconds(target):.2f} s ({len(segment['text'])} characters)")


def timeline():
    """Each segment's start and length: where the recording started it (--timeline), else as src/main.js lays the scenes out."""
    if TIMELINE:
        recorded = json.loads(TIMELINE.read_text(encoding="utf-8"))
        starts = {beat["id"]: beat["at"] for beat in recorded["beats"]}
        rows = [
            {"id": s["id"], "at": starts[s["id"]], "duration": seconds(AUDIO / f"voice-{s['id']}.mp3"), "text": s["text"]}
            for s in segments
            if s["id"] in starts
        ]
        return rows, recorded["duration"]
    at, rows = 0.0, []
    for segment in segments:
        length = seconds(AUDIO / f"voice-{segment['id']}.mp3")
        rows.append({"id": segment["id"], "at": at + script["lead"], "duration": length, "text": segment["text"]})
        at += script["lead"] + length + segment["pad"]
    return rows, at


def music_file():
    return next(iter(sorted(AUDIO.glob("music.*"))), None)


def keep_music(data, kind):
    for old in AUDIO.glob("music.*"):
        old.unlink()
    target = AUDIO / f"music.{kind}"
    target.write_bytes(data)
    print(f"{target.name}: {seconds(target):.2f} s")


def music_elevenlabs(total):
    body = {
        "prompt": MUSIC_PROMPT.format(seconds=round(total + 4)),
        "music_length_ms": round((total + 4) * 1000),
        "force_instrumental": True,
    }
    with post(f"{ELEVENLABS}/music?output_format=mp3_44100_128", body, ELEVENLABS_HEADERS, timeout=900) as response:
        keep_music(response.read(), "mp3")


def music_lyria(total):
    """Lyria through OpenRouter: audio comes back as a stream of base64 pieces, beside the model's notes."""
    body = {
        "model": LYRIA,
        "modalities": ["text", "audio"],
        "stream": True,
        "messages": [{"role": "user", "content": MUSIC_PROMPT.format(seconds=round(total + 4))}],
    }
    pieces, kind = [], None
    with post(f"{OPENROUTER}/chat/completions", body, OPENROUTER_HEADERS, timeout=900) as response:
        for line in response:
            line = line.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            event = json.loads(line[6:])
            if "error" in event:
                raise Refused(f"music: {json.dumps(event['error'])[:1500]}")
            for choice in event.get("choices", []):
                audio = (choice.get("delta") or {}).get("audio") or {}
                if audio.get("data"):
                    pieces.append(audio["data"])
                kind = audio.get("format") or kind
    if not pieces:
        raise Refused("music: the answer held no audio")
    data = b"".join(base64.b64decode(piece) for piece in pieces)
    keep_music(data, kind or ("wav" if data[:4] == b"RIFF" else "mp3"))


def music(total):
    try:
        music_elevenlabs(total)
    except Refused as error:
        print(f"ElevenLabs music: {error}\nfalling back to {LYRIA} through OpenRouter", file=sys.stderr)
        try:
            music_lyria(total)
        except Refused as again:
            sys.exit(str(again))


def stamp(t):
    ms = round(t * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def mix(rows, total):
    (AUDIO / "timings.json").write_text(
        json.dumps({row["id"]: {"duration": round(row["duration"], 3)} for row in rows}, indent=1) + "\n", encoding="utf-8"
    )
    OUT.mkdir(parents=True, exist_ok=True)
    CAPTIONS.write_text(
        "\n".join(f"{i}\n{stamp(row['at'])} --> {stamp(row['at'] + row['duration'])}\n{row['text']}\n" for i, row in enumerate(rows, 1)),
        encoding="utf-8",
    )
    inputs, chains = [], []
    for i, row in enumerate(rows):
        inputs += ["-i", str(AUDIO / f"voice-{row['id']}.mp3")]
        chains.append(f"[{i}]aresample=48000,aformat=channel_layouts=stereo,adelay={round(row['at'] * 1000)}:all=1[v{i}]")
    n = len(rows)
    voices = "".join(f"[v{i}]" for i in range(n))
    graph = ";".join(chains) + f";{voices}amix=inputs={n}:normalize=0,apad=whole_dur={total:.3f},asplit=2[voice][key]"
    if WANTS_MUSIC and music_file():
        inputs += ["-i", str(music_file())]
        # Slowed or sped up by a few percent at most, so that its last chord rings out as the picture fades.
        tempo = max(0.94, min(1.06, seconds(music_file()) / (total + 1.5)))
        # The music sits under the voice and ducks while it speaks; it fades out with the picture.
        graph += (
            f";[{n}]aresample=48000,aformat=channel_layouts=stereo,atempo={tempo:.4f},atrim=0:{total:.3f},volume=0.42,"
            f"afade=t=in:d=1.2,afade=t=out:st={total - 2.5:.3f}:d=2.5[bed]"
            ";[bed][key]sidechaincompress=threshold=0.02:ratio=8:attack=40:release=700[ducked]"
            ";[voice][ducked]amix=inputs=2:normalize=0[sum]"
        )
    else:
        graph += ";[key]anullsink;[voice]anull[sum]"
    graph += ";[sum]loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.89[out]"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[out]", "-t", f"{total:.3f}", "-ar", "48000", str(AUDIO / "mix.wav")],
        check=True,
    )
    print(f"mix.wav: {total:.2f} s")


AUDIO.mkdir(exist_ok=True)
dry = "--dry-run" in argv
step, *ids = [a for a in argv if a != "--dry-run"] or ["all"]
if step not in ("all", "voice", "music", "mix"):
    sys.exit(__doc__)
todo = stale(set(ids) or {s["id"] for s in segments}) if step == "voice" else stale(None) if step == "all" else []
if dry:
    for segment in todo:
        print(f"voice {segment['id']}: {len(segment['text'])} characters")
    print(f"{sum(len(s['text']) for s in todo)} characters of narration in all")
    if step == "music" or (step == "all" and WANTS_MUSIC and not music_file()):
        print("and the music")
    sys.exit(0)
voice(todo)
rows, total = timeline()
if step == "music" or (step == "all" and WANTS_MUSIC and not music_file()):
    music(total)
mix(rows, total)
