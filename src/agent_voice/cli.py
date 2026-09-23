"""The `agent-voice` command.

There is deliberately no bare `speak` alias: Homebrew's espeak-ng installs a
`speak`, and /opt/homebrew/bin usually precedes ~/.local/bin on PATH, so an
agent running `speak` would get espeak's voice instead of this one.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import sys
from pathlib import Path

from . import __version__, hooks, skill, speech


def _add_say_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("text", nargs="*", help="what to say; read from stdin when omitted")
    parser.add_argument("--voice", help=f"Kokoro voice (default: $AGENT_VOICE_VOICE or {speech.DEFAULT_VOICE})")
    parser.add_argument("--speed", type=float, help="speaking rate, 1.0 is normal (default: $AGENT_VOICE_SPEED or 1.0)")
    parser.add_argument("--out", type=Path, help="write a WAV file instead of playing")
    parser.add_argument("--no-fallback", action="store_true", help="fail instead of falling back to macOS say")
    parser.add_argument("-v", "--verbose", action="store_true", help="show mlx-audio's own output")


def _read_text(args: argparse.Namespace) -> str:
    if args.text:
        return " ".join(args.text)
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def _run_say(args: argparse.Namespace) -> int:
    # Speaking needs nothing from Hugging Face; this makes huggingface_hub and
    # transformers refuse to try. Set before either is imported (speech imports
    # them lazily), since both read it at import time.
    os.environ["HF_HUB_OFFLINE"] = "1"
    text = _read_text(args)
    if not text.strip():
        print("agent-voice say: nothing to say (pass text as arguments or on stdin)", file=sys.stderr)
        return 2
    options = {"voice": args.voice, "speed": args.speed, "fallback": not args.no_fallback, "verbose": args.verbose}
    try:
        if args.out:
            speech.save(text, args.out, **options)
            print(f"wrote {args.out}")
        else:
            speech.say(text, **options)
    except Exception as exc:
        print(f"agent-voice say: {exc}", file=sys.stderr)
        return 1
    return 0


# MARK: - agent-voice subcommands


def _prefetch(args: argparse.Namespace) -> int:
    print(f"fetching {speech.MODEL_REPO} (about 340 MB, first time only) and testing it…")
    path = speech.prefetch(verbose=args.verbose)
    print(f"ready: {path}")
    return 0


def _voices(args: argparse.Namespace) -> int:
    current = speech.default_voice()
    for voice in speech.english_voices():
        accent = "American" if voice.startswith("a") else "British"
        gender = "female" if voice[1] == "f" else "male"
        marker = "  (default)" if voice == current else ""
        print(f"{voice:<14} {accent} {gender}{marker}")
    return 0


def _harnesses(args: argparse.Namespace) -> list[str]:
    return args.harness or list(skill.HARNESSES)


def _install_skill(args: argparse.Namespace) -> int:
    if args.print:
        print(skill.skill_text(), end="")
        return 0
    results = skill.install(skill.targets(_harnesses(args), project=args.project), force=args.force)
    for result in results:
        print(_describe(result))
    installed = [r.target.harness for r in results if r.status != "no-harness"]
    print(
        "\nAn agent loads a skill only when it judges it relevant. To have every session\n"
        "use it, add an always-on instruction yourself:"
    )
    for where, text in skill.instruction_steps(installed, project=args.project):
        print(f"\n- {where}:\n")
        print("\n".join(f"    {line}" for line in text.splitlines()))
    return 1 if any(r.status == "differs" for r in results) else 0


def _describe(result: skill.Result) -> str:
    target = result.target
    if result.status == "no-harness":
        return f"{'skipped':<10} {target.root} (not found)"
    if result.status == "differs":
        return f"{'kept':<10} {target.skill_file} (yours differs; use --force)"
    return f"{result.status:<10} {target.skill_file}"


def _uninstall_skill(args: argparse.Namespace) -> int:
    for result in skill.uninstall(skill.targets(_harnesses(args), project=args.project)):
        print(f"{result.status:<10} {result.target.skill_file.parent}")
    return 0


def _hook(args: argparse.Namespace) -> int:
    # Always 0 and silent on stdout: the agent reads a hook's exit code and
    # output as a verdict on the tool call, and this hook only observes.
    try:
        hooks.handle(args.harness, sys.stdin.read())
    except Exception:
        pass
    return 0


def _install_hooks(args: argparse.Namespace) -> int:
    for result in hooks.install(args.harness or list(hooks.HARNESSES)):
        if result.status == "no-harness":
            print(f"{'skipped':<10} {result.path} (not found)")
        else:
            print(f"{result.status:<10} {result.path}")
    return 0


def _uninstall_hooks(args: argparse.Namespace) -> int:
    for result in hooks.uninstall(args.harness or list(hooks.HARNESSES)):
        print(f"{result.status:<10} {result.path}")
    return 0


def _mute(args: argparse.Namespace) -> int:
    speech.set_muted(True)
    print("muted — `agent-voice say` stays silent until: agent-voice unmute")
    return 0


def _unmute(args: argparse.Namespace) -> int:
    speech.set_muted(False)
    print("unmuted")
    if os.environ.get("AGENT_VOICE_MUTE"):
        print("note: AGENT_VOICE_MUTE is set in this shell and still mutes it")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[bool, str]] = []
    apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
    checks.append((apple_silicon, f"Apple Silicon Mac ({sys.platform}/{platform.machine()})"))
    checks.append((os.access(speech.AFPLAY, os.X_OK), f"{speech.AFPLAY} present"))
    espeak = speech.espeak_path_problem()
    checks.append((espeak is None, espeak or "install path short enough for espeak-ng"))
    spacy_model = importlib.util.find_spec("en_core_web_sm") is not None
    checks.append((spacy_model, "spaCy English model (en_core_web_sm) installed"))
    try:
        path = speech.model_path(download=False)
        checks.append((True, f"Kokoro model cached ({path})"))
    except Exception as exc:
        checks.append((False, f"Kokoro model cached — {exc}"))
    for ok, label in checks:
        print(f"{'ok ' if ok else 'NO '} {label}")
    print(f"    muted: {'yes' if speech.is_muted() else 'no'}")
    installed = [t.skill_file.parent for t in skill.targets(list(skill.HARNESSES)) if t.skill_file.exists()]
    print(f"    skill installed: {', '.join(map(str, installed)) or 'nowhere (run: agent-voice install-skill)'}")
    print(f"    approval hooks: {', '.join(hooks.installed()) or 'none (run: agent-voice install-hooks)'}")
    return 0 if all(ok for ok, _ in checks) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-voice", description="Spoken status updates for coding agents.")
    parser.add_argument("--version", action="version", version=f"agent-voice {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    say = commands.add_parser("say", help="say something aloud; blocks until finished")
    _add_say_arguments(say)
    say.set_defaults(run=_run_say)

    prefetch = commands.add_parser("prefetch", help="download the voice model and check it works")
    prefetch.add_argument("-v", "--verbose", action="store_true")
    prefetch.set_defaults(run=_prefetch)

    commands.add_parser("voices", help="list the English voices").set_defaults(run=_voices)

    for name, run, verb in (
        ("install-skill", _install_skill, "install"),
        ("uninstall-skill", _uninstall_skill, "remove"),
    ):
        command = commands.add_parser(name, help=f"{verb} the agent skill for Claude Code and GitHub Copilot")
        command.add_argument(
            "--for", dest="harness", action="append", choices=skill.HARNESSES,
            help="only this harness (repeatable; default: all)",
        )
        command.add_argument(
            "--project", type=Path, metavar="DIR",
            help="use DIR's .claude/skills and .github/skills instead of your personal folders",
        )
        if name == "install-skill":
            command.add_argument("--force", action="store_true", help="overwrite a SKILL.md that differs")
            command.add_argument("--print", action="store_true", help="print the SKILL.md and exit")
        command.set_defaults(run=run)

    for name, run, verb in (
        ("install-hooks", _install_hooks, "announce"),
        ("uninstall-hooks", _uninstall_hooks, "stop announcing"),
    ):
        command = commands.add_parser(name, help=f"{verb} tool-approval prompts (Claude Code, Copilot CLI)")
        command.add_argument(
            "--for", dest="harness", action="append", choices=hooks.HARNESSES,
            help="only this harness (repeatable; default: all)",
        )
        command.set_defaults(run=run)

    hook = commands.add_parser("hook", help="entry point the installed hooks call; reads the event on stdin")
    hook.add_argument("--from", dest="harness", required=True, choices=hooks.HARNESSES)
    hook.set_defaults(run=_hook)

    commands.add_parser("mute", help="silence every agent (e.g. for a meeting)").set_defaults(run=_mute)
    commands.add_parser("unmute", help="undo mute").set_defaults(run=_unmute)
    commands.add_parser("doctor", help="check the install").set_defaults(run=_doctor)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except Exception as exc:
        print(f"agent-voice: {exc}", file=sys.stderr)
        return 1
