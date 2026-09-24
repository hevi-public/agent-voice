import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "atlas"))

import mapdata  # noqa: E402
import mcp_server  # noqa: E402
import serve  # noqa: E402


@pytest.fixture
def atlas_server():
    server = serve.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _request(server, path, data=None, headers=None):
    port = server.server_address[1]
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_index_lists_every_node_and_tour():
    model = mapdata.load()
    text = mapdata.index(model)
    assert all(n["id"] in text for n in model["nodes"])
    assert all(t["id"] in text for t in model["tours"])


def test_node_names_its_callers_and_callees():
    found = mapdata.node(mapdata.load(), "cli._run_say")
    assert "cli._say_via_server" in found["calls"]
    assert "ext.claude" in found["called_by"]
    assert mapdata.node(mapdata.load(), "nope") is None


def test_tour_step_is_clamped():
    assert mapdata.tour_step(mapdata.load(), "voice", 99)["step"] == mapdata.tour_step(mapdata.load(), "voice", 99)["steps"]


def test_state_round_trip(atlas_server):
    status, shown = _request(atlas_server, "/state", {"focus": "hooks.handle"}, {"content-type": "application/json"})
    assert status == 200 and shown["focus"] == "hooks.handle" and shown["seq"] == 1
    assert _request(atlas_server, "/state")[1]["focus"] == "hooks.handle"


def test_refuses_other_hosts_and_origins(atlas_server):
    assert _request(atlas_server, "/status", headers={"Host": "evil.example:80"})[0] == 403
    json_from_elsewhere = {"content-type": "application/json", "Origin": "http://evil.example"}
    assert _request(atlas_server, "/state", {"focus": "x"}, json_from_elsewhere)[0] == 403


def test_refuses_non_json_posts(atlas_server):
    assert _request(atlas_server, "/state", b"focus=x", {"content-type": "text/plain"})[0] == 415


def test_ask_needs_an_installed_agent(atlas_server, monkeypatch):
    monkeypatch.setattr(serve.shutil, "which", lambda name: None)
    status, body = _request(atlas_server, "/ask", {"question": "why?"}, {"content-type": "application/json"})
    assert status == 400 and "PATH" in body["error"]


def test_claude_ask_is_locked_down():
    argv = serve.ask_command("claude", 7788)
    assert argv[:3] == ["claude", "-p", "--restricted"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    config = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["atlas"]
    assert config["env"]["ATLAS_VOICE"] == "0"


def test_speak_turns_the_agents_voice_on():
    argv = serve.ask_command("claude", 7788, voice=True)
    assert json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["atlas"]["env"]["ATLAS_VOICE"] == "1"
    assert serve.VOICE_RULE in serve.ask_prompt("Why?", {}, [], voice=True)
    assert serve.VOICE_RULE not in serve.ask_prompt("Why?", {}, [])


def test_copilot_ask_takes_the_prompt_last():
    argv = serve.ask_command("copilot", 7788)
    assert argv[-1] == "-p" and "--deny-tool" in argv


def test_ask_prompt_carries_the_view_and_the_conversation():
    prompt = serve.ask_prompt("And then?", {"focus": "speech.say"}, [{"role": "user", "content": "How is a voice made?"}])
    assert '"focus": "speech.say"' in prompt and "VIEWER: How is a voice made?" in prompt
    assert prompt.endswith("THE QUESTION: And then?")


def test_claude_stream_keeps_only_the_latest_message():
    lines = [
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let me look."}}},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "The answer."}}},
        {"type": "result", "is_error": True, "result": "boom"},
    ]
    text, error = "", None
    for line in lines:
        text, error = serve._claude_event(json.dumps(line), text, error)
    assert text == "The answer." and error == "boom"


def _session(by=None):
    return mcp_server.Session(mcp_server.Atlas(port=1, by=by, voice=False), out=None)


def test_mcp_initialize_negotiates_and_names_the_harness():
    session = _session()
    reply = session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01", "clientInfo": {"name": "claude-code"}}})
    assert reply["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]
    assert session.atlas.by == "Claude"
    assert session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_lists_tools_and_rejects_unknown_methods():
    session = _session()
    names = [t["name"] for t in session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert names == ["atlas_show", "atlas_map", "atlas_node", "atlas_viewer"]
    assert session.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601


def test_mcp_suggests_ids_for_a_typo():
    reply = _session().handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "atlas_show", "arguments": {"focus": "hooks.handel"}}})
    assert reply["result"]["isError"] and "hooks.handle" in reply["result"]["content"][0]["text"]


def test_mcp_show_steers_a_running_atlas(atlas_server):
    atlas = mcp_server.Atlas(port=atlas_server.server_address[1], by="Copilot", voice=False)
    atlas.opened_at = float("inf")  # no page is open; don't launch a browser from a test
    result = atlas.atlas_show({"tour": "voice", "step": 2})
    assert result["shown"]["by"] == "Copilot"
    assert result["tour_step"]["text"] == mapdata.load()["tours"][0]["steps"][1]["text"]
    assert atlas_server.hub.state["tour"] == "voice"
