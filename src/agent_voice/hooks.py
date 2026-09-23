"""Announces tool-approval prompts through the agents' own hooks.

Claude Code: a `Notification` hook matched to `permission_prompt`, which fires
only once a dialog has waited about six seconds — approve sooner and nothing is
said. Its payload does not name the tool, so a `PermissionRequest` hook, which
fires the moment the dialog appears (and only then: an already-allowed tool
fires `PreToolUse` alone), silently notes the tool per session for the
notification to use; each note deletes itself within half a minute. All
observed with Claude Code 2.1.267, which also showed that the transcript
cannot stand in for the note: six seconds into an open dialog it does not yet
record the tool call.

Copilot CLI: a `notification` hook, filtered to `notification_type:
permission_prompt`. Its own `permissionRequest` event is no use here: it fires
before the rules engine, so for every tool call, prompted or not.

VS Code's Copilot has neither — no approval event, and `PreToolUse` cannot tell
whether VS Code will ask — so it gets no hook.

What is said is a summons, never the command: "Your approval is needed to run
a shell command in natter." The answer happens on screen, where the whole
command is; reading part of it aloud would make the unread part sound safe.
Only names the harness or the user chose are spoken — a tool kind looked up
from Claude's own tool names, and the project folder's name — nothing the
model wrote.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .speech import state_dir

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


def _project(cwd: object) -> str:
    """The project folder's name, made speakable; "" when there is none worth saying."""
    if not isinstance(cwd, str) or not cwd:
        return ""
    name = re.sub(r"[-_.]+", " ", Path(cwd).name).strip()
    return name if re.fullmatch(r"[A-Za-z0-9 ]{1,40}", name) else ""


def announcement(event: dict, tool_name: str | None = None) -> str | None:
    """The sentence for an approval-prompt notification (Claude Code's and
    Copilot CLI's share the shape), or None for any other event. Worded to fit
    any agent: nothing says which one is asking."""
    if event.get("notification_type") != "permission_prompt":
        return None
    project = _project(event.get("cwd"))
    where = f" in {project}" if project else ""
    what = f" to {_tool_action(tool_name)}" if tool_name else ""
    return f"Your approval is needed{what}{where}."


# MARK: - the tool a prompt is about


# How long a note may wait for its notification. The notification comes about
# six seconds after the dialog opens; a note older than this belongs to an
# earlier dialog and is never used, and each note deletes itself at this age.
NOTE_LIFETIME = 30
# Backstop for notes whose self-deletion never ran (the Mac restarted, say).
SWEEP_AGE = 3600

_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


def _session_notes(event: dict) -> Path | None:
    session = event.get("session_id")
    if not isinstance(session, str) or not _ID.fullmatch(session):
        return None
    return state_dir() / "pending" / session


def _remember_tool(event: dict) -> None:
    """Notes the tool a Claude Code dialog is asking about, for the notification
    that follows if the dialog is still open six seconds later.

    One file per dialog, which a detached `sleep; rm` deletes after
    NOTE_LIFETIME whatever happens — so a dialog answered in time, whose
    notification never comes, leaves nothing behind.
    """
    folder, tool = _session_notes(event), event.get("tool_name")
    if folder is None or not isinstance(tool, str):
        return
    request = event.get("tool_use_id")
    name = request if isinstance(request, str) and _ID.fullmatch(request) else uuid.uuid4().hex
    note = folder / name
    for _ in range(2):  # the folder can vanish between mkdir and write: a sibling's cleanup emptied it
        folder.mkdir(parents=True, exist_ok=True)
        try:
            note.write_text(tool, encoding="utf-8")
            break
        except FileNotFoundError:
            continue
    else:
        return
    subprocess.Popen(
        ["/bin/sh", "-c", 'sleep "$0"; rm -f "$1"; rmdir "$2" 2>/dev/null; exit 0',
         str(NOTE_LIFETIME), str(note), str(folder)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _sweep(folder.parent)


def _sweep(pending: Path) -> None:
    cutoff = time.time() - SWEEP_AGE
    for folder in pending.iterdir():
        for note in folder.iterdir() if folder.is_dir() else ():
            if note.stat().st_mtime < cutoff:
                note.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            folder.rmdir()  # only if empty


def _recall_tool(event: dict) -> str | None:
    """The tool of this session's newest note, if young enough; clears the session's notes."""
    folder = _session_notes(event)
    if folder is None or not folder.is_dir():
        return None
    notes = sorted(folder.iterdir(), key=lambda note: note.stat().st_mtime)
    tool = None
    if notes and time.time() - notes[-1].stat().st_mtime <= NOTE_LIFETIME:
        tool = notes[-1].read_text(encoding="utf-8")
    for note in notes:
        note.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        folder.rmdir()
    return tool


def handle(harness: str, raw: str) -> str | None:
    """Speaks the announcement for one hook payload, in the background.

    Never raises, never prints: a hook's stdout is read by the agent as a
    decision, and this one only observes. Returns what it said, for tests.
    """
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    if harness == "claude" and event.get("hook_event_name") == "PermissionRequest":
        _remember_tool(event)
        return None
    # Copilot CLI's notification has no tool, and nothing records one for it.
    text = announcement(event, _recall_tool(event) if harness == "claude" else None)
    if text is None:
        return None
    # Detached, so the agent is never held up by the seconds it takes to talk.
    subprocess.Popen(
        [sys.executable, "-m", "agent_voice", "say", "--", text],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
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


def hook_command(harness: str, program: str | None = None) -> str:
    return f"{_quote(program or executable())} hook --from {harness}"


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


# Claude Code event -> the matcher group fields our handler sits under.
CLAUDE_EVENTS = {
    "PermissionRequest": {},
    "Notification": {"matcher": "permission_prompt"},
}


def _our_claude_handlers(data: dict) -> list[dict]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    return [
        handler
        for event in CLAUDE_EVENTS
        if isinstance(hooks.get(event), list)
        for group in hooks[event]
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for handler in group["hooks"]
        if isinstance(handler, dict) and _is_ours(handler.get("command"), "claude")
    ]


def _install_claude(settings: Path, command: str) -> str:
    """Adds our two hooks to Claude Code's settings, keeping everything else."""
    data = _read_settings(settings)
    had_ours = bool(_our_claude_handlers(data))
    wanted = copy.deepcopy(data)
    _remove_claude_hooks(wanted)
    handler = {"type": "command", "command": command, "async": True, "timeout": 10}
    for event, fields in CLAUDE_EVENTS.items():
        wanted.setdefault("hooks", {}).setdefault(event, []).append({**fields, "hooks": [dict(handler)]})
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
    for event in CLAUDE_EVENTS:
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


def _copilot_file(command: str) -> dict:
    return {
        "version": 1,
        "hooks": {"notification": [{"type": "command", "bash": command, "timeoutSec": 10}]},
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
        command = hook_command(harness, program)
        if harness == "claude":
            status = _install_claude(path, command)
        else:
            wanted = _copilot_file(command)
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
