# agent-voice Atlas

An interactive map of this repository: the agents and the machine around it,
its modules, every function, the calls between them, and the places the code
meets the outside world (I/O seams). Zoom from the system down to a single
function, highlight a concern across the map (security, network, audio, test
coverage …), or take a guided tour that answers a question step by step.

It is built to be steered by an agent as much as by a person.

Everything runs on your machine: the page, the server that steers it, and the
agents that answer questions about it. Nothing depends on claude.ai.

## Run it locally

```bash
uv run python atlas/serve.py --open
```

It serves on `127.0.0.1:7788`.

## Steer it from an agent (MCP)

`atlas/mcp_server.py` is an MCP server (stdio, standard library only) with four
tools:

| Tool | What it does |
|---|---|
| `atlas_show` | Moves the map: the state fields below, plus `say` to speak the step with agent-voice. With `say`, it returns once the sentence has been spoken, so a walkthrough plays step after step without gaps. |
| `atlas_map` | Every node id, edge, lens and tour, as text. |
| `atlas_node` | One node: file and lines, I/O, calls and callers, tests, coverage. |
| `atlas_viewer` | What you are looking at right now. |

Register it once per harness. This prints the exact snippet for each, with
this checkout's paths filled in:

```bash
uv run python atlas/mcp_server.py --config
```

- **Claude Code**: run the `claude mcp add …` line it prints.
- **Copilot CLI**: add the `atlas` entry to `mcpServers` in `~/.copilot/mcp-config.json`.
- **VS Code (Copilot Chat)**: add the `atlas` entry to `servers` in `.vscode/mcp.json` or your user `mcp.json`.

Then ask, from any directory: *"walk me through how a voice is generated on the
atlas"*. If nothing is serving the page yet, the first `atlas_show` serves it
from the MCP server's own process and opens it in your browser. Later agents,
in other harnesses, steer the same page. That page closes with the agent
session that served it, so run `serve.py` yourself when you want it to last.

## Steer it with curl

Anything that can run a shell command can drive the page too: Natter, a script.

```bash
curl -s 127.0.0.1:7788/state -H 'content-type: application/json' \
     -d '{"focus": "hooks.handle", "level": 2, "note": "Every hook event lands here.", "by": "Claude"}'
curl -s 127.0.0.1:7788/viewer        # what the person is looking at now
curl -s 127.0.0.1:7788/model.json    # every node id, edge and tour
```

## The guide state

One JSON object, used by every way of steering (`atlas_show`, POST /state,
the claude.ai artifact's `guide/state` document, and `atlas.go()` in the
browser). Fields
that are present replace what is on screen; absent ones stay as they are.

| Field | Meaning |
|---|---|
| `focus` | A node id: `hooks.handle`, `server.Voice._speak`, a module (`speech`) or the outside world (`ext.audio`). `null` clears it. |
| `level` | `0` system, `1` modules, `2` functions. Defaults from the focus. |
| `lens` | Concerns to highlight: `security`, `privacy`, `network`, `audio`, `files`, `process`, `resilience`, `performance`, `agent`, `coverage`. |
| `tour`, `step` | A guided tour (`voice`, `approval`, `speak`, `settings`, `network`, `fallback`, `skill`, `lifecycle`) and its 1-based step. |
| `highlight` | Node ids to emphasise; edges between them animate. |
| `note`, `title` | Narration shown in the guide panel. |
| `by` | Who is guiding, shown as the guide's badge ("Claude", "Natter"). |
| `reset` | `true` clears everything first. |

The page reports what the person is looking at in the same shape, to
`GET /viewer` locally or the `guide/viewer` document on claude.ai, so an agent
can answer "what is this?" without being told.

## The Ask box

With the page served locally, the Ask box sends your question to `claude -p`
or `copilot -p`, whichever is on your PATH (a picker appears when both are).
The agent gets the whole map, what you are looking at and the conversation so
far in its prompt, runs in this repository, and has the Atlas MCP server
attached, so it moves the map while it answers. The answer streams into the
guide panel; Stop ends the agent.

What that agent can do is limited to reading and steering:

- **Claude** runs with `--restricted --tools Read,Grep,Glob --permission-mode dontAsk`.
  It can read files in this repository and use the Atlas tools, nothing else.
  Your settings and hooks are not loaded.
- **Copilot** runs with `--allow-tool atlas --deny-tool shell --deny-tool write`.

Each question is one agent run on your own subscription, started by you. An
answer is stopped after five minutes.

The server only answers pages it served itself. POSTs must be JSON, which a
page on another site can't send without a CORS preflight that is never
approved. A request whose `Host` or `Origin` names another site is refused,
which stops DNS rebinding too.

## Rebuild the model

`model.json` is generated. Line numbers and call edges come from the source,
coverage from pytest-cov, history from git; descriptions, tags, I/O and tours
are curated in `build_model.py`. The build refuses to run when a function has
no annotation or an annotation names a function that no longer exists.

```bash
uv run --with pytest-cov pytest -q --cov=agent_voice --cov-report=json:atlas/coverage.json
uv run python atlas/build_model.py
```
