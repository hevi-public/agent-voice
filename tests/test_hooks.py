import io
import json
import subprocess

import pytest

from agent_voice import cli, hooks

PROGRAM = "/Users/me/.local/bin/agent-voice"

# Trimmed from a real Claude Code 2.1.267 PermissionRequest payload.
CLAUDE_BASH = {
    "session_id": "ce3b6ff5",
    "cwd": "/Users/me/Projects/speech_test",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf build && git push --force", "description": "i'll push this up now yeah?"},
}


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))
    return calls


# MARK: - what is said


def test_claude_prompt_names_the_tool_kind_and_project():
    assert hooks.announcement("claude", CLAUDE_BASH) == (
        "Claude needs your approval to run a shell command in speech test."
    )


def test_nothing_the_model_wrote_is_spoken():
    text = hooks.announcement("claude", CLAUDE_BASH)
    for leaked in ("rm", "push", "force", "yeah"):
        assert leaked not in text


@pytest.mark.parametrize(
    "tool, action",
    [
        ("Edit", "edit a file"),
        ("WebFetch", "fetch a web page"),
        ("mcp__9be76459-07f6__issue_write", "use an external tool"),
        ("SomethingNew", "use a tool"),
    ],
)
def test_tool_kinds(tool, action):
    assert action in hooks.announcement("claude", {**CLAUDE_BASH, "tool_name": tool})


def test_an_unspeakable_folder_name_is_left_out():
    event = {**CLAUDE_BASH, "cwd": "/tmp/claude-501/7b0a1c31-fa7e-4225-ad1d-46955db3bbb2/scratchpad/ツール"}
    assert hooks.announcement("claude", event) == "Claude needs your approval to run a shell command."


def test_other_claude_events_are_ignored():
    assert hooks.announcement("claude", {**CLAUDE_BASH, "hook_event_name": "PreToolUse"}) is None


def test_copilot_permission_prompt():
    event = {"cwd": "/Users/me/work/billing-api", "notification_type": "permission_prompt", "message": "…"}
    assert hooks.announcement("copilot", event) == "Copilot needs your approval in billing api."


def test_other_copilot_notifications_are_ignored():
    assert hooks.announcement("copilot", {"notification_type": "agent_completed"}) is None


# MARK: - the hook command


def test_hook_speaks_in_a_detached_process(spawned):
    assert hooks.handle("claude", json.dumps(CLAUDE_BASH))
    [(argv, kwargs)] = spawned
    assert argv[-3:] == ["say", "--", "Claude needs your approval to run a shell command in speech test."]
    assert kwargs["start_new_session"] is True


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", json.dumps({"hook_event_name": "Stop"})])
def test_hook_ignores_anything_else(spawned, raw):
    assert hooks.handle("claude", raw) is None
    assert spawned == []


def test_cli_hook_is_silent_and_always_succeeds(monkeypatch, capsys):
    def explode(harness, raw):
        raise OSError("no python")

    monkeypatch.setattr(hooks, "handle", explode)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(CLAUDE_BASH)))
    assert cli.main(["hook", "--from", "claude"]) == 0
    assert capsys.readouterr().out == ""


# MARK: - installing


def home_with(tmp_path, *folders):
    for folder in folders:
        (tmp_path / folder).mkdir()
    return tmp_path


def test_claude_hook_merges_into_existing_settings(tmp_path):
    home = home_with(tmp_path, ".claude")
    settings = home / ".claude/settings.json"
    existing = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "PermissionRequest": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "notify.sh"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "chime.sh"}]}],
        },
    }
    settings.write_text(json.dumps(existing))

    [result, _] = hooks.install(["claude", "copilot"], home=home, program=PROGRAM)
    assert result.status == "installed"
    data = json.loads(settings.read_text())
    assert data["model"] == "opus" and data["permissions"] == existing["permissions"]
    assert data["hooks"]["Stop"] == existing["hooks"]["Stop"]
    groups = data["hooks"]["PermissionRequest"]
    assert groups[0] == existing["hooks"]["PermissionRequest"][0]
    assert groups[1] == {
        "hooks": [{"type": "command", "command": f"{PROGRAM} hook --from claude", "async": True, "timeout": 10}]
    }
    assert json.loads((home / ".claude/settings.json.agent-voice-backup").read_text()) == existing

    assert hooks.install(["claude"], home=home, program=PROGRAM)[0].status == "unchanged"
    assert hooks.install(["claude"], home=home, program="/elsewhere/agent-voice")[0].status == "updated"
    assert len(json.loads(settings.read_text())["hooks"]["PermissionRequest"]) == 2

    assert hooks.uninstall(["claude"], home=home)[0].status == "removed"
    assert json.loads(settings.read_text()) == existing
    assert hooks.uninstall(["claude"], home=home)[0].status == "absent"


def test_claude_hook_into_a_fresh_settings_file(tmp_path):
    home = home_with(tmp_path, ".claude")
    hooks.install(["claude"], home=home, program=PROGRAM)
    assert hooks.installed(home=home) == ["claude"]
    hooks.uninstall(["claude"], home=home)
    assert json.loads((home / ".claude/settings.json").read_text()) == {}


def test_invalid_settings_are_left_untouched(tmp_path):
    home = home_with(tmp_path, ".claude")
    settings = home / ".claude/settings.json"
    settings.write_text("{ not json")
    with pytest.raises(RuntimeError, match="left untouched"):
        hooks.install(["claude"], home=home, program=PROGRAM)
    assert settings.read_text() == "{ not json"


def test_copilot_hook_gets_its_own_file(tmp_path):
    home = home_with(tmp_path, ".copilot")
    [_, result] = hooks.install(["claude", "copilot"], home=home, program=PROGRAM)
    assert result.status == "installed"
    data = json.loads((home / ".copilot/hooks/agent-voice.json").read_text())
    assert data == {
        "version": 1,
        "hooks": {
            "notification": [{"type": "command", "bash": f"{PROGRAM} hook --from copilot", "timeoutSec": 10}]
        },
    }
    assert not (home / ".claude").exists()
    assert hooks.uninstall(["copilot"], home=home)[0].status == "removed"


def test_no_agent_folder_is_an_error(tmp_path):
    with pytest.raises(hooks.NoHarnessError):
        hooks.install(list(hooks.HARNESSES), home=tmp_path, program=PROGRAM)
    assert list(tmp_path.iterdir()) == []


def test_a_program_path_with_spaces_is_quoted_and_still_recognised():
    command = hooks.hook_command("claude", "/Users/me/My Tools/agent-voice")
    assert command == "'/Users/me/My Tools/agent-voice' hook --from claude"
    assert hooks._is_ours(command, "claude")
