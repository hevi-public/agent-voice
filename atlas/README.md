# agent-voice Atlas

An interactive map of this repository: the agents and the machine around it,
its modules, every function, the calls between them, and the places the code
meets the outside world (I/O seams). Zoom from the system down to a single
function, highlight a concern across the map (security, network, audio, test
coverage …), or take a guided tour that answers a question step by step.

It is built to be steered by an agent as much as by a person.

## Run it locally

```bash
uv run python atlas/serve.py --open
```

It serves on `127.0.0.1:7788`. Any agent that can run a shell command can then
drive the page: Claude Code, Copilot, Natter, a script.

```bash
curl -s 127.0.0.1:7788/state -H 'content-type: application/json' \
     -d '{"focus": "hooks.handle", "level": 2, "note": "Every hook event lands here.", "by": "Claude"}'
curl -s 127.0.0.1:7788/viewer        # what the person is looking at now
curl -s 127.0.0.1:7788/model.json    # every node id, edge and tour
```

## The guide state

One JSON object, used by every way of steering (POST /state, the claude.ai
artifact's `guide/state` document, and `atlas.go()` in the browser). Fields
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

## Rebuild the model

`model.json` is generated. Line numbers and call edges come from the source,
coverage from pytest-cov, history from git; descriptions, tags, I/O and tours
are curated in `build_model.py`. The build refuses to run when a function has
no annotation or an annotation names a function that no longer exists.

```bash
uv run --with pytest-cov pytest -q --cov=agent_voice --cov-report=json:atlas/coverage.json
uv run python atlas/build_model.py
```
