import io
import json
import subprocess

import pytest

from agent_voice import cli, hooks

PROGRAM = "/Users/me/.local/bin/agent-voice"

# Trimmed from a real Claude Code 2.1.267 PermissionRequest payload.
CLAUDE_REQUEST = {
    "session_id": "ce3b6ff5-4889",
    "cwd": "/Users/me/Projects/speech_test",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf build && git push --force", "description": "i'll push this up now yeah?"},
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
    return [argv[-1] for argv, _ in calls]


# MARK: - what is said


def test_claude_prompt_names_the_tool_kind_and_project_but_no_agent():
    assert hooks.announcement("claude", CLAUDE_REQUEST) == (
        "Your approval is needed to run a shell command in speech test."
    )


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
    assert action in hooks.announcement("claude", {**CLAUDE_REQUEST, "tool_name": tool})


def test_an_unspeakable_folder_name_is_left_out():
    event = {**CLAUDE_REQUEST, "cwd": "/tmp/claude-501/7b0a1c31-fa7e-4225-ad1d-46955db3bbb2/scratchpad/ツール"}
    assert hooks.announcement("claude", event) == "Your approval is needed to run a shell command."


def test_other_events_are_ignored():
    assert hooks.announcement("claude", {**CLAUDE_REQUEST, "hook_event_name": "PreToolUse"}) is None
    assert hooks.announcement("claude", {"hook_event_name": "Notification", "notification_type": "permission_prompt"}) is None
    assert hooks.announcement("copilot", {"notification_type": "agent_completed"}) is None


def test_copilot_permission_prompt():
    event = {"cwd": "/Users/me/work/billing-api", "notification_type": "permission_prompt", "message": "…"}
    assert hooks.announcement("copilot", event) == "Your approval is needed in billing api."


# MARK: - the project's name


def make_repo(root):
    (root / ".git" / "worktrees").mkdir(parents=True)
    return root


def make_worktree(main, where, name="recursing-elgamal-3be4c7"):
    gitdir = main / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    where.mkdir(parents=True)
    (where / ".git").write_text(f"gitdir: {gitdir}\n")
    return where


def test_a_worktree_is_named_after_its_main_checkout(tmp_path):
    main = make_repo(tmp_path / "speech_test")
    worktree = make_worktree(main, main / ".claude" / "worktrees" / "recursing-elgamal-3be4c7")
    assert hooks.project_folder(worktree / "Sources" / "natter") == main
    assert hooks._project(str(worktree)) == "speech test"


def test_a_relative_gitdir_is_followed(tmp_path):
    main = make_repo(tmp_path / "billing-api")
    where = tmp_path / "elsewhere" / "wt"
    (main / ".git" / "worktrees" / "wt").mkdir()
    where.mkdir(parents=True)
    (where / ".git").write_text("gitdir: ../../billing-api/.git/worktrees/wt\n")
    assert hooks.project_folder(where) == main


def test_a_bare_repositorys_worktree_drops_the_git_suffix(tmp_path):
    bare = tmp_path / "natter.git"
    (bare / "worktrees" / "main").mkdir(parents=True)
    where = tmp_path / "natter-main"
    where.mkdir()
    (where / ".git").write_text(f"gitdir: {bare / 'worktrees' / 'main'}\n")
    assert hooks._project(str(where)) == "natter"


def test_a_subfolder_is_named_after_its_repository(tmp_path):
    main = make_repo(tmp_path / "speech_test")
    assert hooks.project_folder(main / "Sources" / "NatterSpeech") == main


def test_a_submodule_keeps_its_own_name(tmp_path):
    parent = make_repo(tmp_path / "app")
    sub = parent / "vendor" / "lib"
    (parent / ".git" / "modules" / "lib").mkdir(parents=True)
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/lib\n")
    assert hooks.project_folder(sub) == sub


def test_outside_git_the_folder_itself_is_used(tmp_path):
    assert hooks.project_folder(tmp_path / "notes") == tmp_path / "notes"


# MARK: - the hook command


def test_hook_speaks_at_once_in_a_detached_process(spawned):
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    [(argv, kwargs)] = spawned
    assert argv[-3:] == ["say", "--", "Your approval is needed to run a shell command in speech test."]
    assert kwargs["start_new_session"] is True


def test_nothing_the_model_wrote_is_spoken(spawned):
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    for leaked in ("rm", "push", "force", "yeah", "Claude"):
        assert leaked not in said(spawned)[0]


def test_the_hook_leaves_no_files(spawned, tmp_path):
    hooks.handle("claude", json.dumps(CLAUDE_REQUEST))
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", json.dumps({"hook_event_name": "Stop"})])
def test_hook_ignores_anything_else(spawned, raw):
    assert hooks.handle("claude", raw) is None
    assert spawned == []


def test_cli_hook_is_silent_and_always_succeeds(monkeypatch, capsys):
    def explode(harness, raw):
        raise OSError("no python")

    monkeypatch.setattr(hooks, "handle", explode)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(CLAUDE_REQUEST)))
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
    assert "Notification" not in data["hooks"]
    assert json.loads((home / ".claude/settings.json.agent-voice-backup").read_text()) == existing

    assert hooks.install(["claude"], home=home, program=PROGRAM)[0].status == "unchanged"
    assert hooks.install(["claude"], home=home, program="/elsewhere/agent-voice")[0].status == "updated"
    after_update = json.loads(settings.read_text())["hooks"]
    assert len(after_update["PermissionRequest"]) == 2

    assert hooks.uninstall(["claude"], home=home)[0].status == "removed"
    assert json.loads(settings.read_text()) == existing
    assert hooks.uninstall(["claude"], home=home)[0].status == "absent"


def test_reinstalling_clears_the_delayed_notification_an_older_version_added(tmp_path):
    home = home_with(tmp_path, ".claude")
    ours = {"type": "command", "command": f"{PROGRAM} hook --from claude", "async": True, "timeout": 10}
    theirs = {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "chime.sh"}]}
    (home / ".claude/settings.json").write_text(json.dumps({"hooks": {
        "PermissionRequest": [{"hooks": [ours]}],
        "Notification": [theirs, {"matcher": "permission_prompt", "hooks": [ours]}],
    }}))
    assert hooks.install(["claude"], home=home, program=PROGRAM)[0].status == "updated"
    data = json.loads((home / ".claude/settings.json").read_text())
    assert data["hooks"] == {"PermissionRequest": [{"hooks": [ours]}], "Notification": [theirs]}


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
