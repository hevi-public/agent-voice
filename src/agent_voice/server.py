"""The voice server: keeps Kokoro loaded between announcements.

A cold `agent-voice say` spends about 3.5 s before any sound, nearly all of it
warming Kokoro, spaCy and espeak on the first sentence; a warm sentence takes
about 0.3 s. So the first `say` starts this server in the background, and every
`say` after it sends its text here over a Unix socket in the state folder.

It speaks one request at a time, in order, and quits after IDLE_TIMEOUT
seconds without a request, removing its socket, so the ~800 MB it holds is not
held for good. Requests carry an optional tag, and `cancel` drops every queued
or playing request with that tag (or a tag starting with it and a colon): that
is how an approval announcement is stopped once the agent has moved on.

Protocol: one JSON object per line each way, one request per connection.
  {"op": "say", "text": ..., "voice": ..., "speed": ..., "tag": ..., "wait": true, "fallback": true}
      -> {"ok": true, "engine": "kokoro" | "say" | "cancelled", "warning": ...}   (after playback)
      -> {"ok": true, "queued": true}                                          (wait: false)
  {"op": "cancel", "tag": ...}  -> {"ok": true, "cancelled": n}
  {"op": "ping"}                -> {"ok": true, "pid": ..., "idle": seconds}
  {"op": "stop"}                -> {"ok": true}
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import speech

IDLE_TIMEOUT = 30 * 60
START_WAIT = 10.0
SAY_TIMEOUT = 300.0


def socket_path() -> Path:
    return speech.state_dir() / "voice.sock"


def enabled() -> bool:
    """AGENT_VOICE_SERVER=0 turns the server off: every `say` then loads Kokoro itself."""
    return os.environ.get("AGENT_VOICE_SERVER", "").strip().lower() not in ("0", "false", "no", "off")


def idle_timeout() -> float:
    try:
        return float(os.environ.get("AGENT_VOICE_IDLE", IDLE_TIMEOUT))
    except ValueError:
        return IDLE_TIMEOUT


# MARK: - server


@dataclass
class Job:
    text: str
    voice: str | None = None
    speed: float | None = None
    tag: str | None = None
    fallback: bool = True
    cancelled: bool = False
    engine: str | None = None
    warning: str | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


def _matches(job_tag: str | None, tag: str) -> bool:
    return job_tag is not None and (job_tag == tag or job_tag.startswith(tag + ":"))


class Voice:
    """The queue and the one worker that synthesizes and plays it.

    `synthesize` and `player` are injectable so tests can run the queue
    without Kokoro or speakers.
    """

    def __init__(
        self,
        synthesize: Callable = speech.synthesize,
        player: Callable[[list[str]], subprocess.Popen] | None = None,
        idle: float = IDLE_TIMEOUT,
    ):
        self.synthesize = synthesize
        self.player = player or (lambda argv: subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        self.idle = idle
        self.queue: deque[Job] = deque()
        self.cond = threading.Condition()
        self.current: Job | None = None
        self.playing: subprocess.Popen | None = None
        self.last_activity = time.monotonic()
        self.stopping = False

    def submit(self, job: Job) -> None:
        with self.cond:
            self.queue.append(job)
            self.last_activity = time.monotonic()
            self.cond.notify_all()

    def cancel(self, tag: str) -> int:
        with self.cond:
            dropped = [job for job in self.queue if _matches(job.tag, tag)]
            for job in dropped:
                self.queue.remove(job)
                job.cancelled = True
                job.engine = "cancelled"
                job.done.set()
            count = len(dropped)
            if self.current is not None and _matches(self.current.tag, tag):
                self.current.cancelled = True
                if self.playing is not None:
                    self.playing.terminate()
                count += 1
            return count

    def stop(self) -> None:
        with self.cond:
            self.stopping = True
            self.cond.notify_all()

    def idle_for(self) -> float:
        with self.cond:
            busy = self.current is not None or bool(self.queue)
            return 0.0 if busy else time.monotonic() - self.last_activity

    def _next(self) -> Job | None:
        with self.cond:
            while not self.queue:
                if self.stopping:
                    return None
                remaining = self.idle - (time.monotonic() - self.last_activity)
                if remaining <= 0:
                    return None
                self.cond.wait(timeout=min(remaining, 5.0))
            self.current = self.queue.popleft()
            return self.current

    def run(self) -> None:
        """Works the queue until stopped or idle for `idle` seconds."""
        while (job := self._next()) is not None:
            try:
                self._speak(job)
            except Exception as exc:  # keep serving after a bad request
                job.error = str(exc)
            finally:
                with self.cond:
                    self.current = None
                    self.playing = None
                    self.last_activity = time.monotonic()
                if job.engine is None and job.error is None:
                    job.engine = "cancelled"
                job.done.set()

    def _speak(self, job: Job) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-voice-") as folder:
            try:
                audio, rate = self.synthesize(job.text, job.voice, job.speed)
            except Exception as exc:
                if not job.fallback:
                    raise
                job.warning = f"Kokoro unavailable ({exc}); using macOS say"
                argv, engine = [speech.SAY, job.text], "say"
            else:
                wav = Path(folder) / "speech.wav"
                speech._write_wav(audio, rate, wav)
                argv, engine = [speech.AFPLAY, str(wav)], "kokoro"
            with speech._playback_lock():
                with self.cond:
                    if job.cancelled:
                        job.engine = "cancelled"
                        return
                    self.playing = self.player(argv)
                self.playing.wait()
            job.engine = "cancelled" if job.cancelled else engine


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        voice: Voice = self.server.voice  # type: ignore[attr-defined]
        try:
            request = json.loads(self.rfile.readline())
            reply = self._answer(voice, request)
        except Exception as exc:
            reply = {"ok": False, "error": str(exc)}
        with contextlib.suppress(OSError):
            self.wfile.write((json.dumps(reply) + "\n").encode())

    @staticmethod
    def _answer(voice: Voice, request: dict) -> dict:
        op = request.get("op")
        if op == "say":
            job = Job(
                text=str(request["text"]),
                voice=request.get("voice"),
                speed=request.get("speed"),
                tag=request.get("tag"),
                fallback=bool(request.get("fallback", True)),
            )
            voice.submit(job)
            if not request.get("wait", True):
                return {"ok": True, "queued": True}
            job.done.wait()
            if job.error:
                return {"ok": False, "error": job.error}
            return {"ok": True, "engine": job.engine, "warning": job.warning}
        if op == "cancel":
            return {"ok": True, "cancelled": voice.cancel(str(request["tag"]))}
        if op == "ping":
            return {"ok": True, "pid": os.getpid(), "idle": round(voice.idle_for())}
        if op == "stop":
            voice.stop()
            return {"ok": True}
        return {"ok": False, "error": f"unknown op {op!r}"}


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(idle: float | None = None, voice: Voice | None = None) -> int:
    """Runs the server in this process until it is stopped or goes idle.
    Returns at once if another server already holds the lock. `voice` is for
    tests; the real one warms Kokoro before taking requests."""
    folder = speech.state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    lock = open(folder / "server.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # another server is running
    path = socket_path()
    path.unlink(missing_ok=True)  # we hold the lock, so any socket here is stale
    warm = voice is None
    voice = voice or Voice(idle=idle_timeout() if idle is None else idle)
    server = _Server(str(path), _Handler)
    os.chmod(path, 0o600)
    server.voice = voice  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if warm:  # before the first real request, which queues meanwhile
            with contextlib.suppress(Exception):
                voice.synthesize("Ready.")
        voice.run()  # the main thread: MLX is happiest on one thread
    finally:
        server.shutdown()
        server.server_close()
        path.unlink(missing_ok=True)
        lock.close()
    return 0


# MARK: - client


def request(message: dict, timeout: float = 2.0) -> dict | None:
    """Sends one request; None when no server answers."""
    path = socket_path()
    if not path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            connection.sendall((json.dumps(message) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data += chunk
        return json.loads(data) if data else None
    except (OSError, ValueError):
        return None


# A Unix socket's path must fit in sockaddr_un.sun_path: 104 bytes on macOS.
SOCKET_PATH_LIMIT = 103


def usable() -> bool:
    return enabled() and len(str(socket_path()).encode()) <= SOCKET_PATH_LIMIT


def running() -> dict | None:
    return request({"op": "ping"}, timeout=1.0)


def start() -> bool:
    """Starts a server in the background unless one runs; True once it answers."""
    if running():
        return True
    subprocess.Popen(
        [sys.executable, "-m", "agent_voice", "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + START_WAIT
    while time.monotonic() < deadline:
        if running():
            return True
        time.sleep(0.1)
    return False


def speak(
    text: str,
    voice: str | None = None,
    speed: float | None = None,
    *,
    tag: str | None = None,
    wait: bool = True,
    fallback: bool = True,
) -> dict | None:
    """Has the server speak `text`, starting it if needed. None when there is
    no server to be had (disabled, or it would not start): the caller then
    speaks in-process."""
    if not usable() or not start():
        return None
    message = {"op": "say", "text": text, "voice": voice, "speed": speed, "tag": tag, "wait": wait, "fallback": fallback}
    return request(message, timeout=SAY_TIMEOUT if wait else 5.0)


def cancel(tag: str) -> int:
    """Stops `tag`'s announcements if a server is running; never starts one."""
    reply = request({"op": "cancel", "tag": tag}, timeout=2.0)
    return int(reply.get("cancelled", 0)) if reply else 0


def stop() -> bool:
    return request({"op": "stop"}, timeout=2.0) is not None
