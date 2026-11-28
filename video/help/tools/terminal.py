"""A real terminal for the help videos: bash in a pseudo-terminal, drawn in a page with xterm.js.

    python help/tools/terminal.py --port 7431 --home <dir> [--cwd <dir>]

Serves terminal.html at / and the video's node_modules beside it. The page
reads the shell's output from /stream (server-sent events) and sends what is
typed to /input, so what the recording shows is what bash printed. One shell
per server: start a second server for a second terminal window.

The shell is bash without the user's profile, with HOME set to --home and a
short prompt, in this process's environment (the network proxy and its CA
included), less the variables that would only add noise on camera.
Standard library only.
"""
import argparse
import base64
import fcntl
import json
import os
import pty
import signal
import struct
import termios
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VIDEO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".map": "application/json"}
# Set in this container for its own tools; on camera they would only print warnings.
QUIET = ("UV_NATIVE_TLS",)

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--home", required=True)
parser.add_argument("--cwd")
parser.add_argument("--title", default="Terminal")
args = parser.parse_args()

home = Path(args.home).resolve()
home.mkdir(parents=True, exist_ok=True)
env = {k: v for k, v in os.environ.items() if k not in QUIET}
# uv keeps its cache under HOME; point it at the real one, so a fresh home doesn't download every package again.
env.setdefault("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv"))
env.update(
    HOME=str(home),
    TERM="xterm-256color",
    COLORTERM="truecolor",
    # The prompt: the folder, in the accent colour, then a dollar.
    PS1="\\[\\e[38;2;122;162;247m\\]\\w\\[\\e[0m\\] $ ",
    PROMPT_COMMAND="",
    HISTFILE="/dev/null",
)
pid, fd = pty.fork()
if pid == 0:
    os.chdir(args.cwd or home)
    # Pasted text arrives a line at a time, with bash's continuation prompt, instead of
    # as one bracketed block that readline redraws in place.
    rc = home / ".help-bashrc"
    rc.write_text(f"bind 'set enable-bracketed-paste off'\nPS1='{env['PS1']}'\n")
    os.execvpe("bash", ["bash", "--noprofile", "--rcfile", str(rc), "-i"], env)

lock = threading.Condition()
output = bytearray()  # everything the shell printed, so a page that (re)connects sees all of it


def pump():
    while True:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        with lock:
            if not data:
                output.extend(b"\r\n[exited]\r\n")
                lock.notify_all()
                return
            output.extend(data)
            lock.notify_all()


threading.Thread(target=pump, daemon=True).start()


def resize(cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/stream":
            return self.stream()
        if path == "/":
            file = HERE / "terminal.html"
        else:
            file = (VIDEO / path.lstrip("/")).resolve()
            if not str(file).startswith(str(VIDEO / "node_modules")):
                file = None
        if not file or not file.is_file():
            self.send_error(404)
            return
        body = file.read_bytes()
        if file.name == "terminal.html":
            body = body.replace(b"{{title}}", args.title.encode())
        self.send_response(200)
        self.send_header("content-type", TYPES.get(file.suffix, "application/octet-stream"))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stream(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-store")
        self.end_headers()
        sent = 0
        while True:
            with lock:
                while sent == len(output):
                    lock.wait()
                chunk = bytes(output[sent:])
                sent = len(output)
            try:
                self.wfile.write(b"data: " + base64.b64encode(chunk) + b"\n\n")
                self.wfile.flush()
            except OSError:
                return

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path == "/input":
            os.write(fd, body)
        elif self.path == "/resize":
            size = json.loads(body)
            resize(size["cols"], size["rows"])
        self.send_response(204)
        self.end_headers()


server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)


def stop(*_):
    # The shell and whatever it started (an `exuvia serve`, say) go with the server.
    # bash leads its own session, and each job it starts is a process group in it.
    os.system(f"pkill -TERM -s {pid}")
    os._exit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
print(f"http://127.0.0.1:{args.port}/", flush=True)
server.serve_forever()
