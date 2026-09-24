"""Serves the Atlas on this machine, lets any agent steer it, and answers the page's Ask box.

    uv run python atlas/serve.py              # then open http://127.0.0.1:7788

Agents that speak MCP steer it through atlas/mcp_server.py. Anything that can
run a shell command can drive the page too — Natter, a script:

    curl -s 127.0.0.1:7788/state -H 'content-type: application/json' \\
         -d '{"focus": "hooks.handle", "level": 2, "note": "Every hook event lands here."}'
    curl -s 127.0.0.1:7788/viewer             # what the person is looking at now
    curl -s 127.0.0.1:7788/model.json         # every node id, edge and tour

The page applies each posted state with an animated transition. The state
fields are described in atlas/README.md.

The Ask box runs a question through `claude -p` or `copilot -p`, whichever is
installed, with the map and the current view in the prompt and the Atlas MCP
server attached, so the agent moves the map as it answers. The agent can read
this repository's files and use the Atlas tools, nothing else.

Only 127.0.0.1 is served, and only to pages it served itself: POSTs must be
JSON (a page elsewhere can't send that cross-origin without a CORS preflight,
which this server never approves), and a Host or Origin header naming another
site is refused, so a DNS-rebinding page can't reach it either.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mapdata

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_PORT = 7788
FILES = {"/": ("atlas.html", "text/html; charset=utf-8"), "/model.json": ("model.json", "application/json")}
MAX_BODY = 64 * 1024
# atlas.html is written for claude.ai, which wraps it in this skeleton when
# it publishes; served here, it gets the same wrapping.
SKELETON_HEAD = (
    b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
    b'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
    b"<style>body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style></head><body>"
)
AGENTS = {"claude": "Claude", "copilot": "Copilot"}
ASK_TIMEOUT = 300  # seconds
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Set by an agent that launched this process (Claude Code refuses to start
# inside what looks like another of its sessions).
PARENT_AGENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

ASK_RULES = """You are the guide inside the agent-voice Atlas, an interactive architecture map open in the viewer's browser. agent-voice is a Python package that speaks coding agents' status updates aloud (Kokoro TTS on Apple Silicon) and chimes when Claude Code or Copilot waits for approval. Its source code is in your working directory.

Walk the viewer through the answer ON THE MAP: call the atlas_show tool once per point you make (2 to 6 calls), each with a short note (one or two sentences) that the page displays while it moves. Write no text before or between the tool calls; the notes are the walkthrough. Use node ids exactly as listed below. {levels}. Use lens to colour a concern across the map. Call atlas_node for a function's calls, tests, I/O and coverage, and read the source files when you need more than the map says.
After the last atlas_show call, answer in 2 to 4 short plain-text paragraphs. Refer to functions by name. Say so when neither the map nor the code answers the question.
{voice}
{index}

THE VIEWER IS LOOKING AT: {viewer}
{history}
THE QUESTION: {question}"""
VOICE_RULE = "The viewer is listening: pass each note as `say` too, word for word, so it is spoken aloud. atlas_show returns once it has been spoken.\n"
VOICE_TAG = "atlas"  # what mcp_server.py tags its speech with, so Stop can cut it off


class Hub:
    """The latest guide state and viewer report, and the pages listening for news."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.state: dict | None = None
        self.viewer: dict = {}
        self.listeners: list[queue.Queue] = []

    def publish(self, state: dict) -> dict:
        with self.lock:
            self.seq += 1
            self.state = {**state, "seq": self.seq, "at": int(time.time() * 1000)}
            self._send("state", self.state)
            return self.state

    def broadcast(self, event: str, data: dict) -> None:
        with self.lock:
            self._send(event, data)

    def _send(self, event: str, data: dict) -> None:
        for listener in self.listeners:
            listener.put((event, data))

    def listen(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.listeners.append(q)
        return q

    def forget(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    @property
    def pages(self) -> int:
        with self.lock:
            return len(self.listeners)


def installed_agents() -> list[str]:
    return [name for name in AGENTS if shutil.which(name)]


def voice_available() -> bool:
    return importlib.util.find_spec("agent_voice") is not None or shutil.which("agent-voice") is not None


def ask_prompt(question: str, viewer: dict, history: list, voice: bool = False) -> str:
    turns = []
    for turn in history[-6:]:
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
            who = "VIEWER" if turn["role"] == "user" else "YOU"
            turns.append(f"{who}: {str(turn.get('content', ''))[:1500]}")
    earlier = "EARLIER IN THIS CONVERSATION:\n" + "\n".join(turns) + "\n" if turns else ""
    return ASK_RULES.format(
        levels=mapdata.LEVELS,
        voice=VOICE_RULE if voice else "",
        index=mapdata.index(mapdata.load()),
        viewer=json.dumps(viewer or {}),
        history=earlier,
        question=question,
    )


def ask_command(agent: str, port: int, voice: bool = False) -> list[str]:
    """The agent's command line. The prompt goes to stdin (claude) or after -p (copilot)."""
    server = {
        "command": sys.executable,
        "args": [str(HERE / "mcp_server.py"), "--port", str(port)],
        # Steps are spoken only when the viewer ticked Speak.
        "env": {"ATLAS_BY": AGENTS[agent], "ATLAS_VOICE": "1" if voice else "0"},
    }
    if agent == "claude":
        # --restricted ignores your settings (so no hooks fire), confines the
        # file tools to this repo and removes every tool that runs code.
        return [
            "claude", "-p", "--restricted", "--tools", "Read,Grep,Glob",
            "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {"atlas": server}}),
            "--allowedTools", "mcp__atlas", "--permission-mode", "dontAsk", "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        ]
    server = {"type": "local", **server, "tools": ["*"]}
    return [
        "copilot", "--additional-mcp-config", json.dumps({"mcpServers": {"atlas": server}}),
        "--allow-tool", "atlas", "--deny-tool", "shell", "--deny-tool", "write", "--silent", "-p",
    ]


class Asker:
    """Runs one Ask at a time and streams its answer to the pages as `answer` events."""

    def __init__(self, hub: Hub, port: int):
        self.hub = hub
        self.port = port
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.current: str | None = None

    def ask(self, question: str, agent: str, viewer: dict, history: list, voice: bool = False) -> str:
        prompt = ask_prompt(question, viewer, history, voice)
        argv = ask_command(agent, self.port, voice)
        if agent == "copilot":
            argv.append(prompt)
        env = {k: v for k, v in os.environ.items() if k not in PARENT_AGENT_ENV}
        self.stop()
        ask_id = uuid.uuid4().hex[:12]
        proc = subprocess.Popen(
            argv, cwd=REPO, env=env, text=True, bufsize=1, start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with self.lock:
            self.proc, self.current = proc, ask_id
        threading.Thread(target=self._feed, args=(proc, prompt if agent == "claude" else ""), daemon=True).start()
        threading.Thread(target=self._read, args=(ask_id, agent, proc), daemon=True).start()
        return ask_id

    def stop(self, ask_id: str | None = None) -> bool:
        with self.lock:
            proc = self.proc
            if proc is None or (ask_id and ask_id != self.current):
                return False
        _kill(proc)
        _hush()
        return True

    @staticmethod
    def _feed(proc: subprocess.Popen, prompt: str) -> None:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass

    def _read(self, ask_id: str, agent: str, proc: subprocess.Popen) -> None:
        timed_out = threading.Event()
        timer = threading.Timer(ASK_TIMEOUT, lambda: (timed_out.set(), _kill(proc)))
        timer.start()
        stderr: list[str] = []
        threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True).start()
        text, error = "", None
        for line in proc.stdout:
            if agent == "claude":
                text, error = _claude_event(line, text, error)
            else:
                text += ANSI.sub("", line)
            self._answer(ask_id, text)
        code = proc.wait()
        timer.cancel()
        with self.lock:
            if self.current == ask_id:
                self.proc = None
        if code and not error:
            if timed_out.is_set():
                error = f"No answer after {ASK_TIMEOUT // 60} minutes, so it was stopped."
            elif code < 0:
                error = "Stopped."
            else:
                tail = "".join(stderr).strip().splitlines()[-3:]
                error = f"{agent} exited with status {code}" + (": " + " ".join(tail) if tail else "")
        self._answer(ask_id, text.strip(), done=True, error=error)

    def _answer(self, ask_id: str, text: str, done: bool = False, error: str | None = None) -> None:
        self.hub.broadcast("answer", {"id": ask_id, "text": text, "done": done, "error": error})


def _claude_event(line: str, text: str, error: str | None) -> tuple[str, str | None]:
    """Folds one line of `claude -p --output-format stream-json` into the answer so far.

    The answer is the latest message's text: each new message starts it over,
    so the page keeps showing the last atlas_show note until new words arrive.
    """
    try:
        event = json.loads(line)
    except ValueError:
        return text, error
    if event.get("type") == "stream_event":
        inner = event.get("event", {})
        if inner.get("type") == "message_start":
            text = ""
        elif inner.get("type") == "content_block_delta" and inner.get("delta", {}).get("type") == "text_delta":
            text += inner["delta"].get("text", "")
    elif event.get("type") == "result" and event.get("is_error"):
        error = str(event.get("result") or event.get("subtype") or "claude failed")
    return text, error


def _hush() -> None:
    """Cuts off a step the stopped agent is still speaking."""
    try:
        from agent_voice import server as voice_server
    except ImportError:
        return
    try:
        voice_server.cancel(VOICE_TAG)
    except OSError:
        pass


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


class Handler(BaseHTTPRequestHandler):
    server: "AtlasServer"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, (json.dumps(data) + "\n").encode())

    def _trusted(self) -> bool:
        """Only this machine's own pages: no other Host (DNS rebinding) or Origin."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host", "") in hosts and (origin is None or origin.removeprefix("http://") in hosts)

    def do_GET(self):
        if not self._trusted():
            return self._json(403, {"error": "only pages served by the Atlas itself"})
        path = self.path.split("?", 1)[0]
        hub = self.server.hub
        if path in FILES:
            name, kind = FILES[path]
            body = (HERE / name).read_bytes()
            if name.endswith(".html"):
                body = SKELETON_HEAD + body + b"</body></html>"
            self._send(200, body, kind)
        elif path == "/state":
            self._json(200, hub.state or {})
        elif path == "/viewer":
            self._json(200, hub.viewer)
        elif path == "/status":
            self._json(200, {"atlas": True, "pages": hub.pages, "agents": installed_agents(), "voice": voice_available()})
        elif path == "/events":
            self._events()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._trusted():
            return self._json(403, {"error": "only pages served by the Atlas itself"})
        path = self.path.split("?", 1)[0]
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json(415, {"error": "send JSON: -H 'content-type: application/json'"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "body too large"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as exc:
            return self._json(400, {"error": f"not JSON: {exc}"})
        if not isinstance(data, dict):
            return self._json(400, {"error": "send a JSON object"})
        if path == "/state":
            self._json(200, self.server.hub.publish(data))
        elif path == "/viewer":
            self.server.hub.viewer = data
            self._json(200, {"ok": True})
        elif path == "/ask":
            self._ask(data)
        elif path == "/ask/stop":
            self._json(200, {"stopped": self.server.asker.stop(data.get("id"))})
        else:
            self._json(404, {"error": "not found"})

    def _ask(self, data: dict) -> None:
        question = str(data.get("question") or "").strip()[:2000]
        installed = installed_agents()
        agent = data.get("agent") or (installed[0] if installed else None)
        if not question:
            return self._json(400, {"error": "ask a question"})
        if agent not in installed:
            return self._json(400, {"error": "neither claude nor copilot is on PATH" if not installed else f"{agent} is not installed"})
        viewer = data.get("viewer") if isinstance(data.get("viewer"), dict) else self.server.hub.viewer
        history = data.get("history") if isinstance(data.get("history"), list) else []
        try:
            ask_id = self.server.asker.ask(question, agent, viewer, history, voice=bool(data.get("voice")))
        except OSError as exc:
            return self._json(500, {"error": f"could not start {agent}: {exc}"})
        self._json(200, {"id": ask_id, "agent": agent})

    def _events(self):
        """Server-sent events: the current state at once, then every new state and answer."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        hub = self.server.hub
        q = hub.listen()
        try:
            if hub.state:
                q.put(("state", hub.state))
            while True:
                try:
                    event, data = q.get(timeout=15)
                    head = "" if event == "state" else f"event: {event}\n"
                    self.wfile.write(f"{head}data: {json.dumps(data)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            hub.forget(q)


class AtlasServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int):
        super().__init__(("127.0.0.1", port), Handler)
        self.hub = Hub()
        self.asker = Asker(self.hub, self.server_address[1])


def make_server(port: int = DEFAULT_PORT) -> AtlasServer:
    return AtlasServer(port)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the agent-voice Atlas locally.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="open it in your browser")
    args = parser.parse_args()
    server = make_server(args.port)
    url = f"http://127.0.0.1:{args.port}/"
    agents = installed_agents()
    print(f"Atlas at {url}  (steer it: atlas/mcp_server.py, or POST JSON to {url}state)")
    print(f"Ask box: {', '.join(agents) if agents else 'off — install claude or copilot'}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.asker.stop()


if __name__ == "__main__":
    main()
