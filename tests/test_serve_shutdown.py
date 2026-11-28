"""Ctrl+C stops `exuvia serve` promptly and quietly while a workbench tab is
open (its event stream would otherwise hold uvicorn's graceful shutdown for
as long as the tab stays open), and the same Ctrl+C delivered twice (as under
`uvx`) does not turn into a force quit full of tracebacks. Once stopped,
it ends by the signal, so the shell knows it was interrupted."""

import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from exuvia.project import Project

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to another process is POSIX")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(tmp_path):
    root = tmp_path / "proj"
    Project(root).write_node("hello", "import exuvia\n\n\n@exuvia.node()\ndef hello():\n    return 1\n")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "exuvia.cli", "serve", str(root), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/project", timeout=2).read()
            break
        except OSError:
            if time.monotonic() > deadline or proc.poll() is not None:
                proc.kill()
                raise AssertionError(proc.stdout.read().decode()) from None
            time.sleep(0.2)
    # An open tab: the event stream, read up to its first event.
    stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/events", timeout=30)
    assert b"hello" in stream.readline()
    return proc, stream


@pytest.mark.parametrize("signals", [1, 2])
def test_ctrl_c_stops_the_server_with_a_tab_open(tmp_path, signals):
    proc, stream = serve(tmp_path)
    try:
        for _ in range(signals):
            proc.send_signal(signal.SIGINT)
            time.sleep(0.2)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("the server kept running with an event stream open") from None
        # It ends by the SIGINT, as an uncaught Ctrl+C would: the shell then
        # starts a new line after ^C, and a script sees status 130.
        assert proc.returncode == -signal.SIGINT
        out = proc.stdout.read().decode()
        assert "Traceback" not in out and "ERROR" not in out, out
        assert stream.read().strip() == b""  # the stream ended rather than being cut
    finally:
        stream.close()
        if proc.poll() is None:
            proc.kill()


def test_a_signal_after_shutdown_is_ignored_until_the_process_ends():
    """uvicorn puts back the handlers it found when it stops. A second copy of
    a forwarded Ctrl+C that arrives after that, before the process has ended
    itself by the first, must not raise a KeyboardInterrupt (which exited 1)."""
    import threading

    import uvicorn

    from exuvia import serving

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers are set from the main thread")
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        server = serving._Server(uvicorn.Config(lambda *a: None), app=None)
        with server.capture_signals():
            assert signal.getsignal(signal.SIGINT) == server.handle_exit
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
