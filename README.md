# agent-voice

Spoken status updates for coding agents. Claude Code or GitHub Copilot runs
`agent-voice say "The tests pass and the branch is pushed."` when it
finishes, gets stuck or needs you, so you hear it while you're working in another window.

The voice is [Kokoro](https://huggingface.co/mlx-community/Kokoro-82M-bf16), a
small neural TTS model that runs locally on Apple Silicon through
[mlx-audio](https://github.com/Blaizzy/mlx-audio). Nothing is sent to a cloud
service. If Kokoro can't run, macOS `say` speaks instead, so an agent session
never breaks because of it.

**Requires:** an Apple Silicon Mac and [uv](https://docs.astral.sh/uv/)
(`brew install uv`).

## Install

```bash
uv tool install git+https://github.com/hevi-public/agent-voice
agent-voice prefetch        # one-time model download (~340 MB) and a test sentence
agent-voice install-skill   # teach Claude Code and Copilot to use it
agent-voice install-hooks   # chime when an agent needs your approval or answer (optional)
agent-voice say "Hello from agent-voice."
```

`uv tool install` puts `agent-voice` in `~/.local/bin`, in its own isolated
environment. If uv warns that this folder isn't on your PATH, run
`uv tool update-shell` and open a new terminal. The first install downloads
about 1 GB of Python packages (MLX, PyTorch, spaCy). It needs no admin rights.

Upgrade with `uv tool upgrade agent-voice`, then run
`agent-voice install-skill --force` to pick up any change to the skill's
instructions. It only overwrites agent-voice's own `SKILL.md` files.

**Before `install-hooks`, back up `~/.claude/settings.json`.** It's the one
file agent-voice edits rather than creates; see below.

## Files it touches

Everything agent-voice writes, and whether it's a file of yours that it
edits (**back it up first**) or a file that belongs to agent-voice alone.

| Path | Written by | What happens |
|---|---|---|
| `~/.claude/settings.json` | `install-hooks`, `uninstall-hooks` | **Edited: back it up first.** One `PermissionRequest` hook entry is added (or removed); with `--speak`, also `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit` and `Stop`. Your other settings and hooks are kept. The file is rewritten with 2-space indentation, so its formatting may change. On the first edit a copy is saved next to it as `settings.json.agent-voice-backup` (left in place by `uninstall-hooks`; delete it when you no longer need it), but don't rely on that as your only backup. If the file isn't valid JSON, agent-voice stops and changes nothing. |
| `~/.copilot/hooks/agent-voice.json` | `install-hooks`, `uninstall-hooks` | agent-voice's own file, created and deleted whole. |
| `~/.claude/skills/agent-voice/SKILL.md`, `~/.copilot/skills/agent-voice/SKILL.md` | `install-skill`, `uninstall-skill` | agent-voice's own folders. A `SKILL.md` you've edited is kept unless you pass `--force`. |
| `<repo>/.claude/skills/agent-voice/`, `<repo>/.github/skills/agent-voice/` | `install-skill --project <repo>` | Same, inside that repo, where they'd be committed with it. |
| `~/.agent-voice/` | speaking, `mute` | `playback.lock` (so two agents take turns), `server.lock` (so only one voice server runs), `voice.sock` (the server's socket, only you can use it; removed when the server quits) and `muted` (only while muted). `hook-log-on` and `hooks.log` exist only while `agent-voice hook-log on` is active. Nothing else accumulates. Move it with `AGENT_VOICE_HOME`. |
| `~/.cache/huggingface/hub/` | `prefetch` | The voice model, about 340 MB, in the standard Hugging Face cache. |
| `~/.local/bin/agent-voice`, `~/.local/share/uv/tools/agent-voice/` | `uv tool install` | The command and its Python environment (about 1 GB). `uv tool uninstall agent-voice` removes both. |

The installers never create `~/.claude` or `~/.copilot`, and never touch
`CLAUDE.md`, `copilot-instructions.md` or any other instruction file:
`install-skill` prints the line to add, and you add it yourself.

## Agent setup

`agent-voice install-skill` writes one `SKILL.md` to:

| Folder | Read by |
|---|---|
| `~/.claude/skills/agent-voice/` | Claude Code, VS Code Copilot |
| `~/.copilot/skills/agent-voice/` | Copilot CLI, VS Code Copilot |

It only writes into agent folders that already exist: without `~/.copilot`,
say, it skips Copilot rather than create the folder. If neither folder exists,
it writes nothing and fails. If you use Copilot only in VS Code and have no
`~/.copilot` yet, create it first (`mkdir ~/.copilot`).

VS Code reads both folders, so it lists the skill twice. Both copies are the
same file, so that's harmless. To install for one harness only, pass
`--for claude` or `--for copilot`. `--project DIR` installs into a repo's
`.claude/skills/` and `.github/skills/` instead, so everyone who clones it gets
the skill.

**Make it speak every session.** An agent loads a skill only when it decides
the skill is relevant, and it may not decide that every time. For reliable
announcements, add an always-on instruction (`install-skill` prints these
too):

- Claude Code: add this line to `~/.claude/CLAUDE.md`.
- Copilot: save it as `~/.copilot/instructions/agent-voice.instructions.md`,
  with `applyTo` frontmatter. Copilot only loads files in that folder whose
  names end in `.instructions.md`.

```markdown
---
applyTo: "**"
---
Use the agent-voice skill to speak to me (`agent-voice say "..."`): announce aloud when you finish a task, need my input, or hit a failure I should know about. I can switch you to speaking every reply with /agent-voice conversational, and back with /agent-voice announcer.
```

(For `CLAUDE.md`, leave out the three frontmatter lines.)

## Modes

The skill has two modes, switched from the chat:

| Command | Mode |
|---|---|
| `/agent-voice announcer` | The default. Speaks when a task is done, when the agent needs you, or when something failed. |
| `/agent-voice conversational` | Speaks every reply, for talking to the agent by voice (your agent's speech-to-text input). Short answers are spoken whole, long ones as their gist, and the agent asks one question at a time. It reads speech-to-text mistakes charitably, but asks aloud when a misheard word would change what it does. |

The agent confirms the switch aloud and keeps the mode for the rest of the
session. The mode lives in the conversation, not in a file, so a new session
starts in announcer mode. Both Claude Code and Copilot pass the word after
`/agent-voice` on to the skill.

**Skip the approval prompt.** Agents ask before running shell commands. To let
`agent-voice` run without asking:

- Claude Code, in `~/.claude/settings.json`:
  `"permissions": { "allow": ["Bash(agent-voice:*)"] }`
- VS Code Copilot, in settings.json:
  `"chat.tools.terminal.autoApprove": { "agent-voice": true }`
- Copilot CLI: `copilot --allow-tool='shell(agent-voice)'`

**Network:** only `agent-voice prefetch` contacts Hugging Face, to download the
model. Speaking reads the local cache and never touches the network. If the
model isn't downloaded yet, `say` falls back to macOS `say` and tells you to
run `prefetch`.

## The voice server

Loading Kokoro and speaking a first sentence takes about 3.5 seconds; a
sentence from an already-loaded Kokoro starts in about 0.4. So the first
`agent-voice say` starts a small background server that keeps Kokoro loaded,
and later calls hand it their text. It speaks one request at a time, so two
agents still take turns.

- It holds about 800 MB of memory while it runs, and quits by itself after 30
  minutes without speaking (`AGENT_VOICE_IDLE`, in seconds, changes that).
- `agent-voice stop` ends it now; `agent-voice doctor` shows whether it runs.
- `AGENT_VOICE_SERVER=0` turns it off: every `say` then loads Kokoro itself,
  as before.
- If it can't start, `say` speaks in-process, and falls back to macOS `say`
  if Kokoro fails, as always.

The Python library (`from agent_voice import say`) doesn't use the server; it
keeps Kokoro loaded in your own process instead.

## Approval and question alerts

`agent-voice install-hooks` makes a sound when an agent is waiting for you:
**Glass** when a tool call needs your approval, **Ping** when the agent asks
you a question through its question tool. You have to come to the keyboard to
answer either way, so a chime says enough and interrupts less than words.

| Agent | Hook | Written to |
|---|---|---|
| Claude Code | `PermissionRequest`, which fires as the dialog opens (its question tool goes through it too) | one entry merged into `~/.claude/settings.json` |
| Copilot CLI | `notification`: `permission_prompt` for an approval, `elicitation_dialog` for a question | its own file, `~/.copilot/hooks/agent-voice.json` |
| Copilot in VS Code | none | VS Code has no approval event, so it can't be announced |

The hook returns at once and plays in the background, so the agent never
waits, and it keeps no state between prompts. It follows the same folder rule
as `install-skill`, and `agent-voice uninstall-hooks` removes exactly what it
added. Mute silences the chimes too.

### Spoken instead: `install-hooks --speak`

With `--speak`, the alert is a sentence instead of a chime: *"Your approval is
needed to run a shell command in billing api."* or *"A question is waiting for
you in billing api."* It names the kind of tool (from the agent's own tool
names; Copilot's notification doesn't carry one, so there it's just *"Your
approval is needed in billing api."*) and the project: the repository's name,
even from a subfolder or a git worktree, or the folder's name outside git. It
never says the command or anything else the model wrote: you approve on
screen, where the whole command is, and reading part of it aloud could make
the rest sound safe.

A sentence takes a few seconds, so `--speak` also installs hooks that stop it
once you've answered. Neither agent reports that a prompt was answered, so
agent-voice watches for the next thing that happens instead: the tool
finishing, you typing, or the turn ending (Claude Code: `PostToolUse`,
`PostToolUseFailure`, `UserPromptSubmit`, `Stop`; Copilot: `postToolUse`,
`postToolUseFailure`, `userPromptSubmitted`, `agentStop`). In Claude Code the
stop is for that tool; Copilot's notification doesn't say which tool it's
about, so there it's for the whole session. It can't help with a long-running
command you approved (a test suite, say): nothing signals until it finishes.
The stop hooks run after every tool call and take a few hundredths of a second.

Run `install-hooks` again, with or without `--speak`, to switch; it replaces
the old entries.

### Diagnosing an agent

Copilot's docs don't say which event fires when it asks you a question, so
its question alert is a best guess. To see what an agent actually sends, run
`agent-voice hook-log on`, use the agent, then `agent-voice hook-log show`. It
records event and tool names only, never a command, a question or any other
content. `agent-voice hook-log off` stops it and deletes the log.

## Commands

```text
agent-voice say "text"                   say it; blocks until finished
agent-voice say < file.txt               text from stdin (safe with quotes, $ and !)
agent-voice say --voice bm_george "..."  another voice (agent-voice voices lists them)
agent-voice say --speed 1.2 "..."        faster
agent-voice say --out hi.wav "..."       write a WAV file instead of playing it

agent-voice mute | unmute                silence every agent, e.g. during a meeting
agent-voice doctor                       check the install
agent-voice voices                       list the English voices
agent-voice uninstall-skill              remove the skill again
agent-voice stop                         stop the voice server now (it restarts on the next say)
agent-voice hook-log on | show | off     record which hook events arrive (names only)
agent-voice install-hooks [--speak] | uninstall-hooks  chime (or speak) when an agent waits for you, or stop
```

Environment variables: `AGENT_VOICE_VOICE` (default `af_heart`),
`AGENT_VOICE_SPEED` (default `1.0`), `AGENT_VOICE_MUTE=1` (mutes that shell
only), `AGENT_VOICE_SERVER=0` (no voice server), `AGENT_VOICE_IDLE` (seconds
before the server quits, default 1800), and `AGENT_VOICE_HOME` (where the
state files live, default `~/.agent-voice`).

When two agents speak at once, the second waits for the first to finish.

## As a library

```bash
uv add git+https://github.com/hevi-public/agent-voice
```

```python
from agent_voice import say, save, synthesize

say("Deploy finished.")                      # returns "kokoro", "say" or "muted"
save("Deploy finished.", "done.wav")
audio, sample_rate = synthesize("Deploy finished.", voice="bf_emma")  # numpy samples
```

Call `prefetch()` once (or run `agent-voice prefetch`) before the first `say`.
The model loads once per process: the first call takes a couple of seconds,
and later calls take only generation time. `say(..., fallback=False)` raises
an error instead of falling back to macOS `say`.

## Why it isn't on PyPI

Kokoro's English pronunciation goes through misaki, and misaki needs spaCy's
`en_core_web_sm` model. That model isn't on PyPI, so it can't be a normal
dependency. If the model is missing, misaki tries to run `spacy download` at
runtime. That command calls pip, which uv-built environments don't have: it
reports success, installs nothing, and every sentence then fails with spaCy
error E050.

This package therefore depends on the model's wheel by direct URL. PyPI
rejects packages with direct-URL dependencies, so agent-voice installs from
git.

## Troubleshooting

- **`agent-voice prefetch` can't reach Hugging Face** (a corporate proxy, for
  example): download the model on another network, or set `HF_ENDPOINT` to
  your company's mirror. The model is cached in `~/.cache/huggingface/hub`.
- **You hear the robotic macOS voice:** Kokoro failed, and the reason is
  printed on stderr. Run `agent-voice doctor`, or `agent-voice say -v "test"`
  to see mlx-audio's own output.
- **`prefetch` or `doctor` says the install is "too deep for espeak-ng":**
  espeak-ng, which pronounces words outside the dictionary, can't handle a
  data folder whose path is longer than 159 characters. A default install is
  about 100, so this only happens with a long custom `UV_TOOL_DIR`. Reinstall
  somewhere shorter. Until then you'll hear the macOS voice.
- **Silence, no error:** check `agent-voice doctor` for `muted: yes`, and make
  sure `AGENT_VOICE_MUTE` isn't set in that shell.

## Development

```bash
uv sync
uv run pytest
```

The tests don't load the model. To check the real voice, run
`uv run agent-voice say "test"`.

To find your way around the code, open the Atlas, an interactive map of it
that any agent can walk you through: `uv run python atlas/serve.py --open`.
See [atlas/README.md](atlas/README.md).

## License

MIT. See [LICENSE](LICENSE).
