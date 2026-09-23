from agent_voice import cli, skill


def test_skill_has_the_frontmatter_both_harnesses_require():
    text = skill.skill_text()
    assert text.startswith("---\nname: agent-voice\ndescription: ")


def test_personal_install_covers_claude_and_copilot(tmp_path):
    paths = skill.targets(list(skill.HARNESSES), home=tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in paths] == [
        ".claude/skills/agent-voice/SKILL.md",
        ".copilot/skills/agent-voice/SKILL.md",
    ]
    assert [r.status for r in skill.install(paths)] == ["installed", "installed"]
    assert [r.status for r in skill.install(paths)] == ["unchanged", "unchanged"]


def test_project_install_uses_repo_folders(tmp_path):
    paths = skill.targets(list(skill.HARNESSES), project=tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in paths] == [
        ".claude/skills/agent-voice/SKILL.md",
        ".github/skills/agent-voice/SKILL.md",
    ]


def test_edited_skill_is_kept_unless_forced(tmp_path):
    [path] = skill.targets(["claude"], home=tmp_path)
    skill.install([path])
    path.write_text("my own edits")
    assert skill.install([path])[0].status.startswith("skipped")
    assert path.read_text() == "my own edits"
    assert skill.install([path], force=True)[0].status == "updated"
    assert path.read_text() == skill.skill_text()


def test_uninstall_removes_only_the_skill_folder(tmp_path):
    [path] = skill.targets(["copilot"], home=tmp_path)
    skill.install([path])
    (path.parent.parent / "other-skill").mkdir()
    assert skill.uninstall([path])[0].status == "removed"
    assert not path.parent.exists()
    assert (path.parent.parent / "other-skill").exists()
    assert skill.uninstall([path])[0].status == "absent"


def test_cli_install_skill_into_a_project(tmp_path, capsys):
    assert cli.main(["install-skill", "--project", str(tmp_path), "--for", "copilot"]) == 0
    assert (tmp_path / ".github/skills/agent-voice/SKILL.md").exists()
    assert not (tmp_path / ".claude").exists()
    assert skill.INSTRUCTION in capsys.readouterr().out
