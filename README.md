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
agent-voice say "Hello from agent-voice."
```

`uv tool install` puts `agent-voice` on your PATH in its own isolated
environment. The first install downloads about 1 GB of Python packages (MLX,
PyTorch, spaCy). It needs no admin rights.

Upgrade with `uv tool upgrade agent-voice`. After an upgrade, run
`agent-voice install-skill --force` if the skill text changed.

## Agent setup

`agent-voice install-skill` writes one `SKILL.md` to:

| Folder | Read by |
|---|---|
| `~/.claude/skills/agent-voice/` | Claude Code, VS Code Copilot |
| `~/.copilot/skills/agent-voice/` | Copilot CLI, VS Code Copilot |

VS Code reads both folders, so it lists the skill twice. Both copies are the
same file, so that's harmless. To install for one harness only, pass
`--for claude` or `--for copilot`. `--project DIR` installs into a repo's
`.claude/skills/` and `.github/skills/` instead, so everyone who clones it gets
the skill.

**Make it announce every time.** An agent loads a skill only when it decides
the skill is relevant, and it may not decide that every time. For reliable
announcements, add a line to your always-on instructions: `~/.claude/CLAUDE.md`
for Claude Code, `.github/copilot-instructions.md` (or your user instructions)
for Copilot:

> When you finish a task, need my input, or hit a failure I should know about,
> announce it aloud with the agent-voice skill (`agent-voice say "..."`).

**Skip the approval prompt.** Agents ask before running shell commands. To let
`agent-voice` run without asking:

- Claude Code, in `~/.claude/settings.json`:
  `"permissions": { "allow": ["Bash(agent-voice:*)"] }`
- VS Code Copilot, in settings.json:
  `"chat.tools.terminal.autoApprove": { "agent-voice": true }`
- Copilot CLI: `copilot --allow-tool='shell(agent-voice)'`

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
```

Environment variables: `AGENT_VOICE_VOICE` (default `af_heart`),
`AGENT_VOICE_SPEED` (default `1.0`), `AGENT_VOICE_MUTE=1` (mutes that shell
only), and `AGENT_VOICE_HOME` (where the mute flag and the playback lock live,
default `~/.agent-voice`).

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
  After that, speaking never touches the network.
- **You hear the robotic macOS voice:** Kokoro failed, and the reason is
  printed on stderr. Run `agent-voice doctor`, or `agent-voice say -v "test"`
  to see mlx-audio's own output.
- **Silence, no error:** check `agent-voice doctor` for `muted: yes`, and make
  sure `AGENT_VOICE_MUTE` isn't set in that shell.

## Development

```bash
uv sync
uv run pytest
```

The tests don't load the model. To check the real voice, run
`uv run agent-voice say "test"`.
