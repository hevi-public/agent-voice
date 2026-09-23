import pytest

from agent_voice import cli, skill


def home_with(tmp_path, *folders):
    for folder in folders:
        (tmp_path / folder).mkdir()
    return tmp_path


def relative(targets, base):
    return [t.skill_file.relative_to(base).as_posix() for t in targets]


def test_skill_has_the_frontmatter_both_harnesses_require():
    text = skill.skill_text()
    assert text.startswith("---\nname: agent-voice\ndescription: ")


def test_personal_install_covers_claude_and_copilot(tmp_path):
    home = home_with(tmp_path, ".claude", ".copilot")
    targets = skill.targets(list(skill.HARNESSES), home=home)
    assert relative(targets, home) == [
        ".claude/skills/agent-voice/SKILL.md",
        ".copilot/skills/agent-voice/SKILL.md",
    ]
    assert [r.status for r in skill.install(targets)] == ["installed", "installed"]
    assert [r.status for r in skill.install(targets)] == ["unchanged", "unchanged"]


def test_project_install_uses_repo_folders(tmp_path):
    targets = skill.targets(list(skill.HARNESSES), project=tmp_path)
    assert relative(targets, tmp_path) == [
        ".claude/skills/agent-voice/SKILL.md",
        ".github/skills/agent-voice/SKILL.md",
    ]


def test_a_missing_harness_folder_is_skipped_not_created(tmp_path):
    home = home_with(tmp_path, ".claude")
    results = skill.install(skill.targets(list(skill.HARNESSES), home=home))
    assert [r.status for r in results] == ["installed", "no-harness"]
    assert not (home / ".copilot").exists()


def test_no_harness_folder_at_all_is_an_error_and_writes_nothing(tmp_path):
    with pytest.raises(skill.NoHarnessError, match="mkdir .*\\.copilot"):
        skill.install(skill.targets(list(skill.HARNESSES), home=tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_asking_only_for_a_missing_harness_is_an_error(tmp_path):
    home = home_with(tmp_path, ".claude")
    with pytest.raises(skill.NoHarnessError):
        skill.install(skill.targets(["copilot"], home=home))
    assert not (home / ".copilot").exists()


def test_edited_skill_is_kept_unless_forced(tmp_path):
    targets = skill.targets(["claude"], home=home_with(tmp_path, ".claude"))
    skill.install(targets)
    path = targets[0].skill_file
    path.write_text("my own edits")
    assert skill.install(targets)[0].status == "differs"
    assert path.read_text() == "my own edits"
    assert skill.install(targets, force=True)[0].status == "updated"
    assert path.read_text() == skill.skill_text()


def test_uninstall_removes_only_the_skill_folder(tmp_path):
    targets = skill.targets(["copilot"], home=home_with(tmp_path, ".copilot"))
    skill.install(targets)
    folder = targets[0].skill_file.parent
    (folder.parent / "other-skill").mkdir()
    assert skill.uninstall(targets)[0].status == "removed"
    assert not folder.exists()
    assert (folder.parent / "other-skill").exists()
    assert skill.uninstall(targets)[0].status == "absent"


def test_cli_install_skill_into_a_project(tmp_path, capsys):
    project = home_with(tmp_path, ".github")
    assert cli.main(["install-skill", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert (project / ".github/skills/agent-voice/SKILL.md").exists()
    assert not (project / ".claude").exists()
    assert f"skipped    {project / '.claude'} (not found)" in out
    assert skill.INSTRUCTION in out


def test_cli_install_skill_fails_without_any_harness(tmp_path, capsys):
    assert cli.main(["install-skill", "--project", str(tmp_path)]) == 1
    assert "no agent folder found" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
