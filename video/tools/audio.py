"""The film's sound, through OpenRouter: the narration (ElevenLabs), the music (Lyria), and the mix.

    OPENROUTER_API_KEY=... python tools/audio.py            # whatever is missing, then the mix
    python tools/audio.py voice [id ...]                     # the narration again (all of it, or these segments)
    python tools/audio.py music                              # the music again
    python tools/audio.py mix                                # only the mix, from the files in audio/

Writes audio/voice-<id>.mp3 per segment of script.json, audio/music.<ext>,
audio/timings.json (each segment's length, which sets the length of its
scene: src/main.js reads it), audio/mix.wav (what tools/render.mjs puts under
the picture) and out/exuvia-overview.srt (captions to upload with the video).

EXUVIA_VIDEO_VOICE picks one of the voice model's voices (default "matilda").
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
AUDIO = VIDEO / "audio"
OUT = VIDEO / "out"
API = "https://openrouter.ai/api/v1"
VOICE = os.environ.get("EXUVIA_VIDEO_VOICE", "matilda")
VOICE_MODEL = os.environ.get("EXUVIA_VIDEO_VOICE_MODEL", "elevenlabs/eleven-v4")
MUSIC_MODEL = os.environ.get("EXUVIA_VIDEO_MUSIC_MODEL", "google/lyria-3-pro-preview")
MUSIC_PROMPT = (
    "A {seconds} second instrumental underscore for a calm, confident software product film. "
    "Deep-sea atmosphere: warm analog synth pads and a soft sub bass open it; after about eight "
    "seconds a gentle pulsing arpeggio and light, tight electronic percussion come in and build "
    "slowly, with a hopeful melody on felt piano and plucked synth. Modern, minimal, spacious, in "
    "a major key at about 100 bpm. It leaves room for a narrator throughout, lifts in the last ten "
    "seconds and ends on a clean, resolved final chord at about {seconds} seconds. Instrumental "
    "only, no vocals."
)

script = json.loads((VIDEO / "script.json").read_text(encoding="utf-8"))
segments = script["segments"]


def request(path, body):
    return urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"},
    )


def call(path, body):
    try:
        with urllib.request.urlopen(request(path, body), timeout=600) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        sys.exit(f"{path} answered {error.code}: {error.read().decode()[:1500]}")


def seconds(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def voice(only):
    for segment in segments:
        target = AUDIO / f"voice-{segment['id']}.mp3"
        if only is not None and segment["id"] not in only and target.exists():
            continue
        if only is None and target.exists():
            continue
        body = {"model": VOICE_MODEL, "input": segment["text"], "voice": VOICE, "response_format": "mp3"}
        raw = AUDIO / f"raw-{segment['id']}.mp3"
        raw.write_bytes(call("/audio/speech", body))
        # Trim the silence the model leaves at either end, so a scene's length is the line's.
        trim = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.03"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", f"{trim},areverse,{trim},areverse", "-b:a", "192k", str(target)],
            check=True,
        )
        raw.unlink()
        print(f"{target.name}: {seconds(target):.2f} s")


def timeline():
    """Each segment's start and length, as src/main.js lays the scenes out."""
    at, rows = 0.0, []
    for segment in segments:
        length = seconds(AUDIO / f"voice-{segment['id']}.mp3")
        rows.append({"id": segment["id"], "at": at + script["lead"], "duration": length, "text": segment["text"]})
        at += script["lead"] + length + segment["pad"]
    return rows, at


def music_file():
    return next(iter(sorted(AUDIO.glob("music.*"))), None)


def music(total):
    """Audio comes back as a stream of base64 pieces, with the transcript (the model's notes) beside it."""
    body = {
        "model": MUSIC_MODEL,
        "modalities": ["text", "audio"],
        "stream": True,
        "messages": [{"role": "user", "content": MUSIC_PROMPT.format(seconds=round(total + 4))}],
    }
    pieces, kind = [], None
    try:
        with urllib.request.urlopen(request("/chat/completions", body), timeout=900) as response:
            for line in response:
                line = line.decode().strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                if "error" in event:
                    sys.exit(f"music: {json.dumps(event['error'])[:1500]}")
                for choice in event.get("choices", []):
                    audio = (choice.get("delta") or {}).get("audio") or {}
                    if audio.get("data"):
                        pieces.append(audio["data"])
                    kind = audio.get("format") or kind
    except urllib.error.HTTPError as error:
        sys.exit(f"music answered {error.code}: {error.read().decode()[:1500]}")
    if not pieces:
        sys.exit("music: the answer held no audio")
    data = b"".join(base64.b64decode(piece) for piece in pieces)
    if kind is None:
        kind = "wav" if data[:4] == b"RIFF" else "mp3"
    for old in AUDIO.glob("music.*"):
        old.unlink()
    target = AUDIO / f"music.{kind}"
    target.write_bytes(data)
    print(f"{target.name}: {seconds(target):.2f} s")


def stamp(t):
    ms = round(t * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def mix(rows, total):
    (AUDIO / "timings.json").write_text(
        json.dumps({row["id"]: {"duration": round(row["duration"], 3)} for row in rows}, indent=1) + "\n", encoding="utf-8"
    )
    OUT.mkdir(exist_ok=True)
    (OUT / "exuvia-overview.srt").write_text(
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
    if music_file():
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
step, *ids = sys.argv[1:] or ["all"]
if step in ("all", "voice"):
    voice(None if step == "all" else set(ids) or {segment["id"] for segment in segments})
rows, total = timeline()
if step == "music" or (step == "all" and not music_file()):
    music(total)
mix(rows, total)
