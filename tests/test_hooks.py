import io
import json
import os
import subprocess
import time

import pytest

from agent_voice import cli, hooks

PROGRAM = "/Users/me/.local/bin/agent-voice"

# Trimmed from real Claude Code 2.1.267 payloads: the dialog opens...
CLAUDE_REQUEST = {
    "session_id": "ce3b6ff5-4889",
    "cwd": "/Users/me/Projects/speech_test",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf build && git push --force", "description": "i'll push this up now yeah?"},
}
# ...and is still open six seconds later.
CLAUDE_WAITING = {
    "session_id": "ce3b6ff5-4889",
    "cwd": "/Users/me/Projects/speech_test",
    "hook_event_name": "Notification",
    "message": "Claude needs your permission",
    "notification_type": "permission_prompt",
}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path / "state"))


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))
    return calls


def said(calls):
    return [argv[-1] for argv, _ in calls if "say" in argv]


# MARK: - what is said


def test_the_wording_names_no_agent():
    assert hooks.announcement(CLAUDE_WAITING, "Bash") == (
        "Your approval is needed to run a shell command in speech test."
    )
    assert hooks.announcement(CLAUDE_WAITING) == "Your approval is needed in speech test."


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
    assert action in hooks.announcement(CLAUDE_WAITING, tool)


def test_an_unspeakable_folder_name_is_left_out():
    event = {**CLAUDE_WAITING, "cwd": "/tmp/claude-501/7b0a1c31-fa7e-4225-ad1d-46955db3bbb2/scratchpad/ツール"}
    assert hooks.announcement(event, "Bash") == "Your approval is needed to run a shell command."


def test_other_notifications_are_ignored():
    assert hooks.announcement({**CLAUDE_WAITING, "notification_type": "idle_prompt"}) is None


def test_copilot_permission_prompt():
    event = {"cwd": "/Users/me/work/billing-api", "notification_type": "permission_prompt", "message": "…"}
    assert hooks.announcement(event) == "Your approval is needed in billing api."


# MARK: - the hook command


def test_claude_dialog_is_noted_silently_then_announced_when_still_waiting(spawned):
    assert hooks.handle("claude", json.dumps(CLAUDE_REQUEST)) is None
    assert said(spawned) == []
    hooks.handle("claude", json.dumps(CLAUDE_WAITING))
    assert said(spawned) == ["Your approval is needed to run a shell command in speech test."]
    assert spawned[-1][1]["start_new_session"] is True


def notes_folder(tmp_path):
    return tmp_path / "state" / "pending" / CLAUDE_REQUEST["session_id"]


def test_the_announcement_clears_the_sessions_notes(spawned, tmp_path):
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    assert notes_folder(tmp_path).is_dir()
    hooks.handle("claude", json.dumps(CLAUDE_WAITING))
    assert not notes_folder(tmp_path).exists()


def test_every_note_schedules_its_own_deletion(spawned, tmp_path):
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    [(argv, kwargs)] = spawned
    [note] = notes_folder(tmp_path).iterdir()
    assert argv[:2] == ["/bin/sh", "-c"] and argv[3:] == [str(hooks.NOTE_LIFETIME), str(note), str(note.parent)]
    assert kwargs["start_new_session"] is True


def test_a_dialog_answered_in_time_leaves_nothing_behind(monkeypatch, tmp_path):
    """The real `sleep; rm`, with the lifetime cut to zero."""
    monkeypatch.setattr(hooks, "NOTE_LIFETIME", 0)
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    deadline = time.time() + 5
    while notes_folder(tmp_path).exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not notes_folder(tmp_path).exists()


def test_a_note_older_than_its_lifetime_is_never_used(spawned, tmp_path):
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    [note] = notes_folder(tmp_path).iterdir()
    old = time.time() - hooks.NOTE_LIFETIME - 1
    os.utime(note, (old, old))
    hooks.handle("claude", json.dumps(CLAUDE_WAITING))
    assert said(spawned) == ["Your approval is needed in speech test."]


def test_the_newest_dialog_wins(spawned):
    hooks.handle("claude", json.dumps({**CLAUDE_REQUEST, "tool_use_id": "toolu_1"}))
    time.sleep(0.01)
    hooks.handle("claude", json.dumps({**CLAUDE_REQUEST, "tool_use_id": "toolu_2", "tool_name": "Edit"}))
    hooks.handle("claude", json.dumps(CLAUDE_WAITING))
    assert said(spawned) == ["Your approval is needed to edit a file in speech test."]


def test_nothing_the_model_wrote_is_spoken(spawned):
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    hooks.handle("claude", json.dumps(CLAUDE_WAITING))
    for leaked in ("rm", "push", "force", "yeah", "Claude"):
        assert leaked not in said(spawned)[0]


def test_the_noted_tool_is_used_once(spawned):
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    hooks.handle("claude", json.dumps(CLAUDE_WAITING))
    hooks.handle("claude", json.dumps(CLAUDE_WAITING))
    assert said(spawned)[1] == "Your approval is needed in speech test."


def test_sessions_do_not_share_their_tool(spawned):
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    hooks.handle("claude", json.dumps({**CLAUDE_WAITING, "session_id": "other"}))
    assert said(spawned) == ["Your approval is needed in speech test."]


@pytest.mark.parametrize("field", ["session_id", "tool_use_id"])
def test_ids_are_never_used_as_paths(spawned, tmp_path, field):
    hooks.handle("claude", json.dumps({**CLAUDE_REQUEST, field: "../../escape"}))
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "state" / "escape").exists()


def test_copilot_notification_is_announced(spawned):
    event = {"sessionId": "s1", "cwd": "/w/billing-api", "notification_type": "permission_prompt"}
    hooks.handle("copilot", json.dumps(event))
    assert said(spawned) == ["Your approval is needed in billing api."]


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", json.dumps({"hook_event_name": "Stop"})])
def test_hook_ignores_anything_else(spawned, raw, tmp_path):
    assert hooks.handle("claude", raw) is None
    assert spawned == []
    assert not (tmp_path / "state").exists()


def test_cli_hook_is_silent_and_always_succeeds(monkeypatch, capsys):
    def explode(harness, raw):
        raise OSError("no python")

    monkeypatch.setattr(hooks, "handle", explode)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(CLAUDE_WAITING)))
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
    ours = {"type": "command", "command": f"{PROGRAM} hook --from claude", "async": True, "timeout": 10}
    groups = data["hooks"]["PermissionRequest"]
    assert groups == [existing["hooks"]["PermissionRequest"][0], {"hooks": [ours]}]
    assert data["hooks"]["Notification"] == [{"matcher": "permission_prompt", "hooks": [ours]}]
    assert json.loads((home / ".claude/settings.json.agent-voice-backup").read_text()) == existing

    assert hooks.install(["claude"], home=home, program=PROGRAM)[0].status == "unchanged"
    assert hooks.install(["claude"], home=home, program="/elsewhere/agent-voice")[0].status == "updated"
    after_update = json.loads(settings.read_text())["hooks"]
    assert len(after_update["PermissionRequest"]) == 2 and len(after_update["Notification"]) == 1

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
