"""Generate one image with an OpenRouter image model.

    OPENROUTER_API_KEY=... python tools/gen_image.py out.png "prompt" [--ref ref.png] [--model id] [--aspect 16:9]

A reference image, when given, is sent with the prompt so the result keeps the
brand's look. Standard library only.
"""
import argparse
import base64
import json
import os
import sys
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("out")
parser.add_argument("prompt")
parser.add_argument("--ref", action="append", default=[])
parser.add_argument("--model", default="google/gemini-3-pro-image")
parser.add_argument("--aspect", default="16:9")
parser.add_argument("--size", default="4K")
args = parser.parse_args()

content = [{"type": "text", "text": args.prompt}]
for ref in args.ref:
    with open(ref, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}})

body = {
    "model": args.model,
    "messages": [{"role": "user", "content": content}],
    "modalities": ["image", "text"],
    "image_config": {"aspect_ratio": args.aspect, "image_size": args.size},
}
request = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=600) as response:
        answer = json.load(response)
except urllib.error.HTTPError as error:
    sys.exit(f"{error.code}: {error.read().decode()[:2000]}")

message = answer["choices"][0]["message"]
images = message.get("images") or []
if not images:
    sys.exit(f"no image in the answer: {json.dumps(answer)[:2000]}")
url = images[0]["image_url"]["url"]
with open(args.out, "wb") as f:
    f.write(base64.b64decode(url.split(",", 1)[1]))
print(args.out, answer.get("usage", {}).get("cost"))
