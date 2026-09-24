"""An MCP server that lets any agent steer the Atlas: Claude Code, Copilot CLI, VS Code, anything that speaks MCP.

    python atlas/mcp_server.py --config       # prints the snippet each harness needs

Each harness starts this over stdio. The first one to need the page serves it
on 127.0.0.1:7788 (the same server as atlas/serve.py) and opens it in your
browser; later ones find it running and steer the same page. If the Atlas is
already served by serve.py, every agent steers that.

Standard library only, so any Python 3.10+ runs it. `say` needs agent-voice
(this repo's environment, or `agent-voice` on PATH).
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import mapdata
import serve

HERE = Path(__file__).resolve().parent
VERSION = "0.1.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LENSES = ["security", "privacy", "network", "audio", "files", "process", "resilience", "performance", "agent", "coverage"]
SAY_TIMEOUT = 180  # seconds
REOPEN_AFTER = 30  # seconds: don't open a second tab while the first one loads

INSTRUCTIONS = """The Atlas is an interactive map of the agent-voice code, open in the person's browser. Use it to show them the code while you explain it: call atlas_show once per point you make.
Call atlas_map first for every node id, lens and tour. atlas_viewer tells you what the person is looking at, so "what is this?" needs no further context.
For a spoken walkthrough, pass each step's narration (one to three sentences) as `say`: the call returns once it has been spoken, so call the next step straight away, with no text in between."""

STATE_PROPS = {
    "focus": {"type": "string", "description": "A node id from atlas_map, e.g. hooks.handle, a module (speech) or the outside world (ext.audio). An empty string clears it."},
    "level": {"type": "integer", "enum": [0, 1, 2], "description": mapdata.LEVELS + ". Defaults from the focus."},
    "highlight": {"type": "array", "items": {"type": "string"}, "description": "Node ids to emphasise; the edges between them animate."},
    "lens": {"type": "array", "items": {"type": "string", "enum": LENSES}, "description": "Concerns to colour across the map."},
    "tour": {"type": "string", "description": "A guided tour id from atlas_map; an empty string leaves the tour."},
    "step": {"type": "integer", "minimum": 1, "description": "The tour's 1-based step."},
    "note": {"type": "string", "description": "Text shown in the guide panel: one or two sentences."},
    "title": {"type": "string", "description": "The guide panel's heading, e.g. the question being answered."},
    "reset": {"type": "boolean", "description": "Clear focus, lenses, tour and highlights first."},
    "say": {"type": "string", "description": "Speak this aloud with agent-voice after the map moves; the call returns once it has been spoken."},
}

TOOLS = [
    {
        "name": "atlas_show",
        "description": "Move the Atlas to make one point: focus a node, set the detail level, highlight related nodes, colour a lens, step through a tour, show a note, and optionally speak it. Opens the Atlas in the browser if no page has it open. Returns what is now on screen.",
        "inputSchema": {"type": "object", "properties": STATE_PROPS, "additionalProperties": False},
    },
    {
        "name": "atlas_map",
        "description": "The whole map as text: every node (id, kind, label, tags, summary), every call edge, the lenses and the guided tours. Call it once before steering.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "atlas_node",
        "description": "One node's details: summary, file and lines, I/O it performs, what it calls and what calls it, its tests and test coverage.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "atlas_viewer",
        "description": "What the person is looking at right now: level, focus, lenses, tour step, highlights and the note on screen.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class ToolError(Exception):
    pass


class Atlas:
    def __init__(self, port: int, by: str | None, voice: bool):
        self.port = port
        self.by = by
        self.voice = voice
        self.model = mapdata.load()
        self.ids = {n["id"] for n in self.model["nodes"]}
        self.tours = {t["id"] for t in self.model["tours"]}
        self.server: serve.AtlasServer | None = None
        self.opened_at = 0.0

    # -- the page server: ours, or one that is already running
    def _call(self, method: str, path: str, data: dict | None = None) -> dict:
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method=method,
            headers={"content-type": "application/json"} if body else {},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read() or b"{}")

    def ensure(self) -> dict:
        """The page server's status, starting it in this process if nothing serves the port."""
        try:
            status = self._call("GET", "/status")
            if status.get("atlas"):
                return status
        except (OSError, ValueError):
            pass
        try:
            self.server = serve.make_server(self.port)
        except OSError as exc:
            time.sleep(0.3)  # another agent may have bound it a moment ago
            try:
                status = self._call("GET", "/status")
                if status.get("atlas"):
                    return status
            except (OSError, ValueError):
                pass
            raise ToolError(f"127.0.0.1:{self.port} is in use by something that isn't the Atlas ({exc}).") from exc
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self._call("GET", "/status")

    def _open_page(self) -> None:
        url = f"http://127.0.0.1:{self.port}/"
        if sys.platform == "darwin":
            subprocess.run(["open", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import webbrowser

            webbrowser.open(url)
        self.opened_at = time.monotonic()

    # -- tools
    def atlas_show(self, args: dict) -> dict:
        state = {k: args[k] for k in STATE_PROPS if k in args and k != "say"}
        for key in ("focus", "tour"):
            if key in state and not state[key]:
                state[key] = None
        if state.get("focus") and state["focus"] not in self.ids:
            raise ToolError(self._unknown(state["focus"], self.ids, "node"))
        if state.get("tour") and state["tour"] not in self.tours:
            raise ToolError(self._unknown(state["tour"], self.tours, "tour"))
        unknown = [x for x in state.get("highlight", []) if x not in self.ids]
        if unknown:
            raise ToolError(self._unknown(unknown[0], self.ids, "node"))
        state["by"] = self.by or "Agent"
        status = self.ensure()
        result: dict = {}
        if not status.get("pages") and time.monotonic() - self.opened_at > REOPEN_AFTER:
            self._open_page()
            result["opened"] = f"No page had the Atlas open, so it was opened in the browser at http://127.0.0.1:{self.port}/"
        shown = self._call("POST", "/state", state)
        result["shown"] = {k: v for k, v in shown.items() if k not in ("seq", "at")}
        if state.get("tour"):
            result["tour_step"] = mapdata.tour_step(self.model, state["tour"], int(state.get("step") or 1))
        if args.get("say"):
            result["spoken"] = self._say(str(args["say"]))
        return result

    def atlas_map(self, args: dict) -> str:
        return mapdata.index(self.model)

    def atlas_node(self, args: dict) -> dict:
        found = mapdata.node(self.model, str(args.get("id", "")))
        if found is None:
            raise ToolError(self._unknown(str(args.get("id", "")), self.ids, "node"))
        return found

    def atlas_viewer(self, args: dict) -> dict:
        status = self.ensure()
        viewer = self._call("GET", "/viewer")
        if not status.get("pages"):
            return {"open": False, "note": "No page has the Atlas open; atlas_show opens it."}
        return {"open": True, **viewer}

    # -- helpers
    def _say(self, text: str) -> bool | str:
        if not self.voice:
            return "voice is off for this agent"
        if importlib.util.find_spec("agent_voice"):
            # Through the voice server with a tag, so the page's Stop can cut it off.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            from agent_voice import server as voice_server, speech

            if speech.is_muted():
                return "agent-voice is muted"
            reply = voice_server.speak(text, tag=serve.VOICE_TAG, wait=True)
            if reply is not None:
                return bool(reply.get("ok"))
            argv = [sys.executable, "-m", "agent_voice", "say", text]
        elif shutil.which("agent-voice"):
            argv = ["agent-voice", "say", text]
        else:
            return "agent-voice is not installed"
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=SAY_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "gave up waiting for the voice"
        return done.returncode == 0

    @staticmethod
    def _unknown(value: str, known: set, what: str) -> str:
        close = difflib.get_close_matches(str(value), sorted(known), n=5, cutoff=0.4)
        return f"No {what} {value!r}." + (f" Did you mean: {', '.join(close)}?" if close else " atlas_map lists them all.")


def harness_name(client: str) -> str | None:
    client = client.lower()
    if "claude" in client:
        return "Claude"
    if "copilot" in client or "vscode" in client or "visual studio code" in client:
        return "Copilot"
    return client or None


class Session:
    """One MCP conversation over stdio: newline-delimited JSON-RPC."""

    def __init__(self, atlas: Atlas, out):
        self.atlas = atlas
        self.out = out

    def send(self, message: dict) -> None:
        self.out.write(json.dumps(message) + "\n")
        self.out.flush()

    def handle(self, message: dict) -> dict | None:
        method, msg_id = message.get("method"), message.get("id")
        if msg_id is None:  # a notification: nothing to answer
            return None
        result = self._dispatch(method, message.get("params") or {})
        if result is None:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"no method {method}"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict) -> dict | None:
        if method == "initialize":
            asked = params.get("protocolVersion")
            if not self.atlas.by:
                self.atlas.by = harness_name((params.get("clientInfo") or {}).get("name", ""))
            return {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "atlas", "version": VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call_tool(params.get("name"), params.get("arguments") or {})
        return None

    def _call_tool(self, name: str, args: dict) -> dict:
        tool = getattr(self.atlas, name, None) if name in {t["name"] for t in TOOLS} else None
        if tool is None:
            return {"content": [{"type": "text", "text": f"No tool {name}."}], "isError": True}
        try:
            value = tool(args)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except OSError as exc:
            return {"content": [{"type": "text", "text": f"The Atlas page server didn't answer: {exc}"}], "isError": True}
        except Exception as exc:  # a broken call must not end the session
            return {"content": [{"type": "text", "text": f"{name} failed: {exc!r}"}], "isError": True}
        text = value if isinstance(value, str) else json.dumps(value, indent=1)
        return {"content": [{"type": "text", "text": text}]}

    def run(self, stream) -> None:
        for raw in stream:
            try:
                message = json.loads(raw)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if isinstance(message, dict):
                reply = self.handle(message)
                if reply is not None:
                    self.send(reply)


def configs(python: str, script: str) -> dict:
    """What each harness needs to start this server."""
    command = {"command": python, "args": [script]}
    return {
        "Claude Code (run once)": f"claude mcp add --scope user atlas -- {python} {script}",
        "Copilot CLI (~/.copilot/mcp-config.json, under mcpServers)": {"atlas": {"type": "local", **command, "tools": ["*"]}},
        "VS Code (.vscode/mcp.json or your user mcp.json, under servers)": {"atlas": {"type": "stdio", **command}},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="The Atlas as an MCP server (stdio).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ATLAS_PORT", serve.DEFAULT_PORT)))
    parser.add_argument("--config", action="store_true", help="print how to register it with Claude Code, Copilot CLI and VS Code")
    args = parser.parse_args()
    if args.config:
        for where, snippet in configs(sys.executable, str(HERE / "mcp_server.py")).items():
            print(f"{where}:\n{snippet if isinstance(snippet, str) else json.dumps(snippet, indent=2)}\n")
        return
    out = sys.stdout
    sys.stdout = sys.stderr  # stdout carries the protocol and nothing else
    atlas = Atlas(args.port, by=os.environ.get("ATLAS_BY"), voice=os.environ.get("ATLAS_VOICE", "1") != "0")
    Session(atlas, out).run(sys.stdin)


if __name__ == "__main__":
    main()
