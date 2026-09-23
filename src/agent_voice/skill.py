"""Installs the agent-voice skill where Claude Code and GitHub Copilot look for skills."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

SKILL_NAME = "agent-voice"

# Personal skill folders, by harness. Claude Code reads only ~/.claude/skills;
# Copilot CLI reads ~/.copilot/skills (and ~/.agents/skills) but not
# ~/.claude/skills. VS Code's Copilot reads all three, so a VS Code user with
# both installed sees the skill twice — same file, same name, harmless.
USER_DIRS = {
    "claude": Path(".claude/skills"),
    "copilot": Path(".copilot/skills"),
}

# Project skill folders: every harness above reads both of these in a repo.
PROJECT_DIRS = {
    "claude": Path(".claude/skills"),
    "copilot": Path(".github/skills"),
}

HARNESSES = tuple(USER_DIRS)

INSTRUCTION = (
    "When you finish a task, need my input, or hit a failure I should know about, "
    'announce it aloud with the agent-voice skill (`agent-voice say "..."`).'
)


@dataclass(frozen=True)
class Result:
    path: Path
    status: str  # "installed", "updated", "unchanged", "skipped (differs; use --force)", "removed", "absent"


def skill_text() -> str:
    return resources.files("agent_voice").joinpath("skill/SKILL.md").read_text(encoding="utf-8")


def targets(harnesses: list[str], project: Path | None = None, home: Path | None = None) -> list[Path]:
    """The SKILL.md paths for `harnesses`, personal (under `home`) or in `project`."""
    if project is not None:
        base, table = project, PROJECT_DIRS
    else:
        base, table = home or Path.home(), USER_DIRS
    return [base / table[name] / SKILL_NAME / "SKILL.md" for name in harnesses]


def install(paths: list[Path], force: bool = False) -> list[Result]:
    text = skill_text()
    results = []
    for path in paths:
        if not path.exists():
            status = "installed"
        elif path.read_text(encoding="utf-8") == text:
            results.append(Result(path, "unchanged"))
            continue
        elif force:
            status = "updated"
        else:
            # A differing file may carry the user's own edits.
            results.append(Result(path, "skipped (differs; use --force)"))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        results.append(Result(path, status))
    return results


def uninstall(paths: list[Path]) -> list[Result]:
    results = []
    for path in paths:
        folder = path.parent
        if folder.name != SKILL_NAME or not folder.exists():
            results.append(Result(path, "absent"))
            continue
        shutil.rmtree(folder)
        results.append(Result(path, "removed"))
    return results
