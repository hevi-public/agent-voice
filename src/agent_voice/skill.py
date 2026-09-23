"""Installs the agent-voice skill where Claude Code and GitHub Copilot look for skills."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

SKILL_NAME = "agent-voice"

# Each harness's own folder; its skills live in `<folder>/skills/`.
#
# Personal: Claude Code reads only ~/.claude/skills; Copilot CLI reads
# ~/.copilot/skills (and ~/.agents/skills) but not ~/.claude/skills. VS Code's
# Copilot reads all three, so a VS Code user with both installed sees the skill
# twice — same file, same name, harmless.
USER_ROOTS = {
    "claude": Path(".claude"),
    "copilot": Path(".copilot"),
}

# Project: every harness above reads both of these in a repo.
PROJECT_ROOTS = {
    "claude": Path(".claude"),
    "copilot": Path(".github"),
}

HARNESSES = tuple(USER_ROOTS)

INSTRUCTION = (
    'Use the agent-voice skill to speak to me (`agent-voice say "..."`): announce aloud when you '
    "finish a task, need my input, or hit a failure I should know about. I can switch you to "
    "speaking every reply with /agent-voice conversational, and back with /agent-voice announcer."
)


def instruction_steps(harnesses: list[str], project: Path | None = None) -> list[tuple[str, str]]:
    """Where each harness keeps always-on instructions, and what to put there.

    Printed, never written: these are the user's instruction files. Copilot
    only loads modular files named `*.instructions.md`, with `applyTo`
    frontmatter saying which files they cover.
    """
    copilot_file = f'---\napplyTo: "**"\n---\n{INSTRUCTION}'
    if project is not None:
        places = {
            "claude": (f"add this line to {project / 'CLAUDE.md'}", INSTRUCTION),
            "copilot": (f"save this as {project / '.github/instructions/agent-voice.instructions.md'}", copilot_file),
        }
    else:
        places = {
            "claude": ("add this line to ~/.claude/CLAUDE.md", INSTRUCTION),
            "copilot": ("save this as ~/.copilot/instructions/agent-voice.instructions.md", copilot_file),
        }
    return [places[name] for name in harnesses]


class NoHarnessError(RuntimeError):
    """None of the requested harness folders exists, so nothing would load the skill."""


@dataclass(frozen=True)
class Target:
    harness: str
    root: Path  # the harness's folder; never created by this module
    skill_file: Path


@dataclass(frozen=True)
class Result:
    target: Target
    # "installed", "updated", "unchanged", "differs" (kept; --force replaces it),
    # "no-harness" (root folder missing), "removed", "absent"
    status: str


def skill_text() -> str:
    return resources.files("agent_voice").joinpath("skill/SKILL.md").read_text(encoding="utf-8")


def targets(harnesses: list[str], project: Path | None = None, home: Path | None = None) -> list[Target]:
    """Where the skill goes for `harnesses`: personal (under `home`) or in `project`."""
    if project is not None:
        base, roots = project, PROJECT_ROOTS
    else:
        base, roots = home or Path.home(), USER_ROOTS
    return [
        Target(name, base / roots[name], base / roots[name] / "skills" / SKILL_NAME / "SKILL.md")
        for name in harnesses
    ]


def install(targets: list[Target], force: bool = False) -> list[Result]:
    """Writes the skill for each harness whose folder already exists.

    A missing harness folder means that harness is not set up here, and
    creating it would only leave a skill nothing reads — so it is skipped, and
    when every requested folder is missing, nothing is written and this raises.
    """
    present = [t for t in targets if t.root.is_dir()]
    if not present:
        missing = ", ".join(str(t.root) for t in targets)
        hint = next(
            (
                f" If you use Copilot only in VS Code, {t.root} may not exist yet: "
                f"create it (mkdir {t.root}) and rerun."
                for t in targets
                if t.root.name == ".copilot"
            ),
            "",
        )
        raise NoHarnessError(
            f"no agent folder found ({missing}), so nothing here would load the skill."
            f" Set up Claude Code or GitHub Copilot first.{hint}"
        )
    text = skill_text()
    results = []
    for target in targets:
        path = target.skill_file
        if target not in present:
            status = "no-harness"
        elif not path.exists():
            status = "installed"
        elif path.read_text(encoding="utf-8") == text:
            status = "unchanged"
        elif force:
            status = "updated"
        else:
            # A differing file may carry the user's own edits.
            status = "differs"
        if status in ("installed", "updated"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        results.append(Result(target, status))
    return results


def uninstall(targets: list[Target]) -> list[Result]:
    results = []
    for target in targets:
        folder = target.skill_file.parent
        if folder.exists():
            shutil.rmtree(folder)
            results.append(Result(target, "removed"))
        else:
            results.append(Result(target, "absent"))
    return results
