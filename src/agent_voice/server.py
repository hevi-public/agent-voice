"""The voice server: keeps Kokoro loaded between announcements.

A cold `agent-voice say` spends about 3.5 s before any sound, nearly all of it
warming Kokoro, spaCy and espeak on the first sentence; a warm sentence takes
about 0.3 s. So the first `say` starts this server in the background, and every
`say` after it sends its text here over a Unix socket in the state folder.

It speaks one request at a time, in order, but does not wait for one to finish
before rendering the next: while a line plays, the next one is synthesized
(one line ahead, never more, so the audio held in memory stays one line's
worth). It quits after IDLE_TIMEOUT seconds with nothing queued, synthesizing
or playing, removing its socket, so the ~800 MB it holds is not held for good.
Requests carry an optional tag, and `cancel` drops every request with that
tag (or a tag starting with it and a colon) wherever it is: queued, being
synthesized, synthesized ahead or playing. That is how an approval
announcement is stopped once the agent has moved on.

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


def lookahead_enabled() -> bool:
    """AGENT_VOICE_LOOKAHEAD=0 makes the server finish each line before it starts
    on the next, as it did before it synthesized ahead."""
    return os.environ.get("AGENT_VOICE_LOOKAHEAD", "").strip().lower() not in ("0", "false", "no", "off")


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


@dataclass
class _Ready:
    """A job that is synthesized and waits for its turn to play: what to run,
    which engine that is, and the temp folder holding its wav."""

    job: Job
    argv: list[str]
    engine: str
    folder: tempfile.TemporaryDirectory | None = None

    def discard(self) -> None:
        folder, self.folder = self.folder, None
        if folder is not None:
            with contextlib.suppress(OSError):  # a folder that won't go is not worth stopping the server for
                folder.cleanup()


class Voice:
    """The queue and the two threads that work it.

    One thread synthesizes, one at a time (the thread that calls `run`: the main
    thread, where MLX is happiest). A second plays, one at a time, in the order
    the jobs were submitted. While a line plays the next one is synthesized, so
    speech runs back to back, but only one line ahead: the line after that is not
    started until the one ahead has begun to play.

    A job moves queue -> synthesizing -> ready -> current (playing), and each of
    the last three stages holds at most one job. Every change of stage happens
    under `cond`, and both threads are woken at each one.

    `synthesize` and `player` are injectable so tests can run the queue
    without Kokoro or speakers.
    """

    def __init__(
        self,
        synthesize: Callable = speech.synthesize,
        player: Callable[[list[str]], subprocess.Popen] | None = None,
        idle: float = IDLE_TIMEOUT,
        lookahead: bool = True,
    ):
        self.synthesize = synthesize
        self.player = player or (lambda argv: subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        self.idle = idle
        self.lookahead = lookahead  # off: nothing is synthesized while a line plays
        self.queue: deque[Job] = deque()
        self.cond = threading.Condition()
        self.synthesizing: Job | None = None
        self.ready: _Ready | None = None  # synthesized, waiting for the line playing to end
        self.current: Job | None = None  # playing, or about to
        self.playing: subprocess.Popen | None = None
        self.last_activity = time.monotonic()
        self.stopping = False
        self.closing = False  # synthesis is over: the player ends once it has played what is ready

    def submit(self, job: Job) -> None:
        with self.cond:
            self.queue.append(job)
            self.last_activity = time.monotonic()
            self.cond.notify_all()

    def cancel(self, tag: str) -> int:
        """Drops every job with `tag`, wherever it is. A job being synthesized is
        dropped when its synthesis returns; the one playing is stopped."""
        with self.cond:
            dropped = [job for job in self.queue if _matches(job.tag, tag)]
            for job in dropped:
                self.queue.remove(job)
            ahead = self.ready if self.ready is not None and _matches(self.ready.job.tag, tag) else None
            if ahead is not None:
                self.ready = None
                dropped.append(ahead.job)
            for job in dropped:
                job.cancelled = True
                job.engine = "cancelled"
            count = len(dropped)
            if self.synthesizing is not None and _matches(self.synthesizing.tag, tag):
                self.synthesizing.cancelled = True
                count += 1
            if self.current is not None and _matches(self.current.tag, tag):
                self.current.cancelled = True
                if self.playing is not None:
                    self.playing.terminate()
                count += 1
            self.cond.notify_all()
        if ahead is not None:
            ahead.discard()
        for job in dropped:
            job.done.set()
        return count

    def stop(self) -> None:
        with self.cond:
            self.stopping = True
            self.cond.notify_all()

    def _busy(self) -> bool:
        """Is a job queued, being synthesized, waiting to play or playing? Hold `cond`."""
        return (
            bool(self.queue)
            or self.synthesizing is not None
            or self.ready is not None
            or self.current is not None
        )

    def idle_for(self) -> float:
        with self.cond:
            return 0.0 if self._busy() else time.monotonic() - self.last_activity

    def _next(self) -> Job | None:
        """Takes the next job off the queue once it may be synthesized: when the
        place ahead of playback is free and, with look-ahead off, nothing is
        playing. None once stopped with the queue empty, or once idle (nothing
        queued, synthesizing or playing) for the timeout."""
        with self.cond:
            while True:
                if self.queue and self.ready is None and (self.lookahead or self.current is None):
                    self.synthesizing = self.queue.popleft()
                    return self.synthesizing
                if self.stopping and not self.queue:
                    return None
                wait = 5.0  # every change of stage wakes us; this is only a safety net
                if not self._busy():
                    remaining = self.idle - (time.monotonic() - self.last_activity)
                    if remaining <= 0:
                        return None
                    wait = min(remaining, 5.0)
                self.cond.wait(timeout=wait)

    def run(self) -> None:
        """Works the queue until stopped or idle for `idle` seconds: this thread
        synthesizes, a second one plays. Returns once both are done, so every
        client waiting on a job has been answered."""
        playback = threading.Thread(target=self._play_loop, name="agent-voice-playback", daemon=True)
        playback.start()
        try:
            while (job := self._next()) is not None:
                self._synthesize(job)
        finally:
            with self.cond:
                self.closing = True
                self.cond.notify_all()
            playback.join()

    # MARK: - the synthesis thread

    def _synthesize(self, job: Job) -> None:
        """Renders `job` and puts it in the place ahead of playback, or settles
        it here when it failed or was cancelled meanwhile."""
        ready = None
        try:
            ready = self._prepare(job)
        except Exception as exc:  # keep serving after a bad request
            job.error = str(exc)
        with self.cond:
            self.synthesizing = None
            handed_over = ready is not None and not job.cancelled
            if handed_over:
                self.ready = ready
            else:
                self.last_activity = time.monotonic()
            self.cond.notify_all()
        if not handed_over:
            if ready is not None:
                ready.discard()
            self._finish(job)

    def _prepare(self, job: Job) -> _Ready:
        """Synthesizes `job` into a wav, or picks the macOS `say` fallback. Plays nothing."""
        try:
            audio, rate = self.synthesize(job.text, job.voice, job.speed)
        except Exception as exc:
            if not job.fallback:
                raise
            job.warning = f"Kokoro unavailable ({exc}); using macOS say"
            return _Ready(job, [speech.SAY, job.text], "say")
        folder = tempfile.TemporaryDirectory(prefix="agent-voice-")
        wav = Path(folder.name) / "speech.wav"
        ready = _Ready(job, [speech.AFPLAY, str(wav)], "kokoro", folder)
        try:
            speech._write_wav(audio, rate, wav)
        except BaseException:
            ready.discard()
            raise
        return ready

    # MARK: - the playback thread

    def _play_loop(self) -> None:
        """Plays what the synthesis thread hands over, in order, until `closing`."""
        while (ready := self._take_ready()) is not None:
            try:
                self._play(ready)
            except Exception as exc:  # keep serving after a bad request
                ready.job.error = str(exc)
            finally:
                ready.discard()
                with self.cond:
                    self.current = None
                    self.playing = None
                    self.last_activity = time.monotonic()
                    self.cond.notify_all()
                self._finish(ready.job)

    def _take_ready(self) -> _Ready | None:
        with self.cond:
            while self.ready is None:
                if self.closing:
                    return None
                self.cond.wait()
            ready, self.ready = self.ready, None
            self.current = ready.job
            self.cond.notify_all()  # the place ahead is free: synthesis may go on
            return ready

    def _play(self, ready: _Ready) -> None:
        job = ready.job
        with speech._playback_lock():
            with self.cond:
                if job.cancelled:
                    job.engine = "cancelled"
                    return
                process = self.playing = self.player(ready.argv)
            process.wait()
        job.engine = "cancelled" if job.cancelled else ready.engine

    @staticmethod
    def _finish(job: Job) -> None:
        """Ends a job: cancelled unless it failed or already has an engine. Wakes
        the client waiting on it."""
        if job.engine is None and job.error is None:
            job.engine = "cancelled"
        job.done.set()


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
    voice = voice or Voice(idle=idle_timeout() if idle is None else idle, lookahead=lookahead_enabled())
    server = _Server(str(path), _Handler)
    os.chmod(path, 0o600)
    server.voice = voice  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if warm:  # before the first real request, which queues meanwhile
            with contextlib.suppress(Exception):
                voice.synthesize("Ready.")
        voice.run()  # synthesizes on this, the main thread: MLX is happiest on one; a second plays
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
