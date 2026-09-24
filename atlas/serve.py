"""Serves the Atlas on this machine and lets any agent steer it.

    uv run python atlas/serve.py              # then open http://127.0.0.1:7788

Anything that can run a shell command can drive the page — Claude Code,
Copilot, Natter, a script:

    curl -s 127.0.0.1:7788/state -H 'content-type: application/json' \\
         -d '{"focus": "hooks.handle", "level": 2, "note": "Every hook event lands here."}'
    curl -s 127.0.0.1:7788/viewer             # what the person is looking at now
    curl -s 127.0.0.1:7788/model.json         # every node id, edge and tour

The page applies each posted state with an animated transition. The state
fields are described in atlas/README.md.

Only 127.0.0.1 is served. POSTs must be JSON: a web page elsewhere in your
browser can't send that cross-origin without a CORS preflight, which this
server never approves.
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = {"/": ("atlas.html", "text/html; charset=utf-8"), "/model.json": ("model.json", "application/json")}
MAX_BODY = 64 * 1024
# atlas.html is written for claude.ai, which wraps it in this skeleton when
# it publishes; served here, it gets the same wrapping.
SKELETON_HEAD = (
    b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
    b'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
    b"<style>body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style></head><body>"
)


class Hub:
    """The latest guide state and viewer report, and the pages listening for states."""

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
            for listener in self.listeners:
                listener.put(self.state)
            return self.state

    def listen(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.listeners.append(q)
        return q

    def forget(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)


class Handler(BaseHTTPRequestHandler):
    hub: Hub

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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in FILES:
            name, kind = FILES[path]
            body = (HERE / name).read_bytes()
            if name.endswith(".html"):
                body = SKELETON_HEAD + body + b"</body></html>"
            self._send(200, body, kind)
        elif path == "/state":
            self._json(200, self.hub.state or {})
        elif path == "/viewer":
            self._json(200, self.hub.viewer)
        elif path == "/events":
            self._events()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
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
            self._json(200, self.hub.publish(data))
        elif path == "/viewer":
            self.hub.viewer = data
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def _events(self):
        """Server-sent events: the current state at once, then every new one."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = self.hub.listen()
        try:
            if self.hub.state:
                q.put(self.hub.state)
            while True:
                try:
                    state = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(state)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.hub.forget(q)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the agent-voice Atlas locally.")
    parser.add_argument("--port", type=int, default=7788)
    parser.add_argument("--open", action="store_true", help="open it in your browser")
    args = parser.parse_args()
    Handler.hub = Hub()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Atlas at {url}  (steer it: POST JSON to {url}state)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
