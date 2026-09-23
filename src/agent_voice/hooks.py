"""Announces tool-approval prompts through the agents' own hooks.

Claude Code: a `PermissionRequest` hook. It fires the moment an approval
dialog appears, and only then: an already-allowed tool fires `PreToolUse`
alone (observed with Claude Code 2.1.267). Its payload names the tool.

Copilot CLI: a `notification` hook, filtered to `notification_type:
permission_prompt`. Its own `permissionRequest` event is no use here: it fires
before the rules engine, so for every tool call, prompted or not.

VS Code's Copilot has neither — no approval event, and `PreToolUse` cannot tell
whether VS Code will ask — so it gets no hook.

What is said is a summons, never the command: "Your approval is needed to run
a shell command in natter." The answer happens on screen, where the whole
command is; reading part of it aloud would make the unread part sound safe.
Only names the harness or the user chose are spoken — a tool kind looked up
from Claude's own tool names, and the project's folder name — nothing the
model wrote. The hook keeps no state between calls.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import server, speech

HARNESSES = ("claude", "copilot")

TOOL_ACTIONS = {
    "Bash": "run a shell command",
    "PowerShell": "run a shell command",
    "Edit": "edit a file",
    "MultiEdit": "edit a file",
    "Write": "write a file",
    "NotebookEdit": "edit a notebook",
    "Read": "read a file",
    "Glob": "search your files",
    "Grep": "search your files",
    "WebFetch": "fetch a web page",
    "WebSearch": "search the web",
    "Task": "start a subagent",
    "Agent": "start a subagent",
    "Skill": "use a skill",
    "ExitPlanMode": "go ahead with its plan",
}


# MARK: - what to say


def _tool_action(tool_name: str) -> str:
    if tool_name in TOOL_ACTIONS:
        return TOOL_ACTIONS[tool_name]
    if tool_name.startswith("mcp__"):
        # Server names are often ids or plugin-prefixed strings that do not
        # read aloud; the screen says which one.
        return "use an external tool"
    return "use a tool"


def _worktree_main(folder: Path, git_file: Path) -> Path | None:
    """The main checkout behind a git worktree, whose `.git` is a file reading
    `gitdir: <main>/.git/worktrees/<name>`; None for anything else (a submodule's
    `.git` file points into `.git/modules/` instead)."""
    try:
        first = git_file.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = (folder / first[len("gitdir:"):].strip()).resolve()
    if gitdir.parent.name != "worktrees":
        return None
    common = gitdir.parent.parent  # <main>/.git, or a bare repository
    return common.parent if common.name == ".git" else common


def project_folder(cwd: Path) -> Path:
    """The folder a session's project is named after: the repository's top
    level — the main checkout's, for a worktree — or `cwd` outside git."""
    for folder in (cwd, *cwd.parents):
        git = folder / ".git"
        if git.is_dir():
            return folder
        if git.is_file():
            return _worktree_main(folder, git) or folder
    return cwd


def _project(cwd: object) -> str:
    """The project's name, made speakable; "" when there is none worth saying."""
    if not isinstance(cwd, str) or not cwd:
        return ""
    name = project_folder(Path(cwd)).name.removesuffix(".git")
    name = re.sub(r"[-_.]+", " ", name).strip()
    return name if re.fullmatch(r"[A-Za-z0-9 ]{1,40}", name) else ""


def announcement(harness: str, event: dict) -> str | None:
    """The sentence for an approval prompt, or None for any other event.
    Worded to fit any agent: nothing says which one is asking."""
    if harness == "claude" and event.get("hook_event_name") == "PermissionRequest":
        tool = event.get("tool_name")
        what = f" to {_tool_action(tool)}" if isinstance(tool, str) and tool else ""
    elif harness == "copilot" and event.get("notification_type") == "permission_prompt":
        what = ""  # Copilot's notification does not say which tool
    else:
        return None
    project = _project(event.get("cwd"))
    where = f" in {project}" if project else ""
    return f"Your approval is needed{what}{where}."


# MARK: - stopping an announcement once the agent has moved on
#
# Neither agent has an event for "the prompt was answered". What does fire is
# the tool finishing, the user typing, or the turn ending — each a sign the
# prompt is over — so those stop the session's announcement if it is still
# queued or playing. Claude Code names the tool in both PermissionRequest and
# PostToolUse, so there the stop is for that exact call (a parallel tool
# finishing does not silence another's prompt); Copilot's notification names
# no tool, so there it is per session.

CANCEL_EVENTS = {
    "claude": {"PostToolUse": "tool", "PostToolUseFailure": "tool", "UserPromptSubmit": "session", "Stop": "session"},
    "copilot": {"postToolUse": "session", "postToolUseFailure": "session", "userPromptSubmitted": "session", "agentStop": "session"},
}

_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


def _session(event: dict) -> str | None:
    session = event.get("session_id") or event.get("sessionId")
    return session if isinstance(session, str) and _ID.fullmatch(session) else None


def _tool_key(event: dict) -> str:
    call = json.dumps([event.get("tool_name"), event.get("tool_input")], sort_keys=True, default=str)
    return hashlib.sha1(call.encode()).hexdigest()[:12]


def _speak_in_background(text: str, tag: str | None) -> None:
    """Queues `text` on the voice server without waiting for it; without a
    server, speaks from a detached process. Either way the agent never waits."""
    if speech.is_muted():
        return
    if server.speak(text, tag=tag, wait=False) is not None:
        return
    subprocess.Popen(
        [sys.executable, "-m", "agent_voice", "say", "--", text],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "AGENT_VOICE_SERVER": "0"},  # the server just failed; don't wait on it again
    )


def handle(harness: str, raw: str, event_name: str | None = None) -> str | None:
    """Acts on one hook payload: announces an approval prompt in the
    background, or stops the announcement once the agent has moved on.

    Never raises, never prints: a hook's stdout is read by the agent as a
    decision, and this one only observes. Returns what it said, for tests.
    """
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    name = event_name or event.get("hook_event_name")
    session = _session(event)
    scope = CANCEL_EVENTS.get(harness, {}).get(name)
    if scope is not None:
        if session is not None:
            server.cancel(f"{session}:{_tool_key(event)}" if scope == "tool" else session)
        return None
    text = announcement(harness, event)
    if text is None:
        return None
    tag = None
    if session is not None:
        tag = f"{session}:{_tool_key(event)}" if harness == "claude" else session
    _speak_in_background(text, tag)
    return text


# MARK: - installing


class NoHarnessError(RuntimeError):
    """None of the requested agent folders exists."""


@dataclass(frozen=True)
class Result:
    harness: str
    path: Path
    status: str  # "installed", "updated", "unchanged", "no-harness", "removed", "absent"


def executable() -> str:
    """The absolute path hooks should run. Absolute, because an agent started
    from the Dock may not have ~/.local/bin on its PATH."""
    found = shutil.which("agent-voice")
    return str(Path(found).absolute()) if found else str(Path(sys.argv[0]).absolute())


def hook_command(harness: str, program: str | None = None, event: str | None = None) -> str:
    command = f"{_quote(program or executable())} hook --from {harness}"
    return f"{command} --event {event}" if event else command


def _quote(path: str) -> str:
    return f"'{path}'" if re.search(r"[^\w/.\-~]", path) else path


def _is_ours(command: object, harness: str) -> bool:
    return isinstance(command, str) and bool(re.search(rf"agent-voice'?\s+hook\s+--from\s+{harness}\b", command))


def _paths(home: Path) -> dict[str, tuple[Path, Path]]:
    """harness -> (the agent's folder, the file the hook lives in)."""
    return {
        "claude": (home / ".claude", home / ".claude" / "settings.json"),
        "copilot": (home / ".copilot", home / ".copilot" / "hooks" / "agent-voice.json"),
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".agent-voice-tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"{path} is not valid JSON ({exc}); fix it and rerun — left untouched") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} is not a JSON object; left untouched")
    return data


# Claude Code events our handler is installed under: the prompt, then the
# events that mean it is over.
CLAUDE_EVENTS = ("PermissionRequest", *CANCEL_EVENTS["claude"])
# Every event an agent-voice version ever installed under, so an install or an
# uninstall also clears what an older version left (0.1.0 briefly used the
# delayed Notification too).
CLAUDE_OWNED_EVENTS = (*CLAUDE_EVENTS, "Notification")
COPILOT_EVENTS = ("notification", *CANCEL_EVENTS["copilot"])


def _our_claude_handlers(data: dict) -> list[dict]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    return [
        handler
        for event in CLAUDE_OWNED_EVENTS
        if isinstance(hooks.get(event), list)
        for group in hooks[event]
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for handler in group["hooks"]
        if isinstance(handler, dict) and _is_ours(handler.get("command"), "claude")
    ]


def _install_claude(settings: Path, program: str | None) -> str:
    """Adds our hooks to Claude Code's settings, keeping everything else."""
    data = _read_settings(settings)
    had_ours = bool(_our_claude_handlers(data))
    wanted = copy.deepcopy(data)
    _remove_claude_hooks(wanted)
    for event in CLAUDE_EVENTS:
        handler = {"type": "command", "command": hook_command("claude", program, event), "async": True, "timeout": 10}
        wanted.setdefault("hooks", {}).setdefault(event, []).append({"hooks": [handler]})
    if wanted == data:
        return "unchanged"
    if settings.exists():
        backup = settings.with_name("settings.json.agent-voice-backup")
        if not backup.exists():
            shutil.copy2(settings, backup)
    _write_json(settings, wanted)
    return "updated" if had_ours else "installed"


def _remove_claude_hooks(data: dict) -> bool:
    """Drops our handlers (and any group or event they leave empty). True if anything went."""
    if not _our_claude_handlers(data):
        return False
    hooks = data["hooks"]
    for event in CLAUDE_OWNED_EVENTS:
        if not isinstance(hooks.get(event), list):
            continue
        kept_groups = []
        for group in hooks[event]:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                kept_groups.append(group)  # not ours to judge
                continue
            kept = [h for h in handlers if not (isinstance(h, dict) and _is_ours(h.get("command"), "claude"))]
            if kept:
                kept_groups.append({**group, "hooks": kept})
            elif not handlers:
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del data["hooks"]
    return True


def _copilot_file(program: str | None) -> dict:
    return {
        "version": 1,
        "hooks": {
            event: [{"type": "command", "bash": hook_command("copilot", program, event), "timeoutSec": 10}]
            for event in COPILOT_EVENTS
        },
    }


def install(harnesses: list[str], home: Path | None = None, program: str | None = None) -> list[Result]:
    """Installs the approval hook for each harness whose folder exists; the
    same rule as the skill: never create an agent's folder, and fail when none exists."""
    paths = _paths(home or Path.home())
    present = [h for h in harnesses if paths[h][0].is_dir()]
    if not present:
        missing = ", ".join(str(paths[h][0]) for h in harnesses)
        raise NoHarnessError(f"no agent folder found ({missing}); set up Claude Code or Copilot CLI first")
    results = []
    for harness in harnesses:
        root, path = paths[harness]
        if harness not in present:
            results.append(Result(harness, root, "no-harness"))
            continue
        if harness == "claude":
            status = _install_claude(path, program)
        else:
            wanted = _copilot_file(program)
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            status = "unchanged" if current == wanted else ("updated" if current else "installed")
            if status != "unchanged":
                _write_json(path, wanted)
        results.append(Result(harness, path, status))
    return results


def uninstall(harnesses: list[str], home: Path | None = None) -> list[Result]:
    paths = _paths(home or Path.home())
    results = []
    for harness in harnesses:
        _, path = paths[harness]
        if not path.exists():
            results.append(Result(harness, path, "absent"))
        elif harness == "claude":
            data = _read_settings(path)
            if _remove_claude_hooks(data):
                _write_json(path, data)
                results.append(Result(harness, path, "removed"))
            else:
                results.append(Result(harness, path, "absent"))
        else:
            path.unlink()
            results.append(Result(harness, path, "removed"))
    return results


def installed(home: Path | None = None) -> list[str]:
    """The harnesses whose hook is in place, for `doctor`."""
    paths = _paths(home or Path.home())
    found = []
    try:
        if _our_claude_handlers(_read_settings(paths["claude"][1])):
            found.append("claude")
    except RuntimeError:
        pass
    if paths["copilot"][1].exists():
        found.append("copilot")
    return found
