import io

import pytest

from agent_voice import cli, speech


@pytest.fixture
def spoken(monkeypatch):
    said = []

    def fake_say(text, **options):
        said.append((text, options))
        return "kokoro"

    monkeypatch.setattr(speech, "say", fake_say)
    return said


def test_arguments_are_joined(spoken):
    assert cli.speak_main(["All", "done."]) == 0
    assert spoken[0][0] == "All done."


def test_text_comes_from_stdin_when_no_arguments(spoken, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('It\'s "quoted" and costs $5!\n'))
    assert cli.speak_main([]) == 0
    assert spoken[0][0] == 'It\'s "quoted" and costs $5!\n'


def test_options_are_passed_through(spoken):
    cli.speak_main(["hi", "--voice", "bm_george", "--speed", "1.2", "--no-fallback"])
    assert spoken[0][1] == {"voice": "bm_george", "speed": 1.2, "fallback": False, "verbose": False}


def test_nothing_to_say_is_a_usage_error(spoken, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.speak_main([]) == 2
    assert spoken == []


def test_failure_exits_nonzero_with_a_message(monkeypatch, capsys):
    def broken(text, **options):
        raise RuntimeError("no audio device")

    monkeypatch.setattr(speech, "say", broken)
    assert cli.speak_main(["hi"]) == 1
    assert "no audio device" in capsys.readouterr().err


def test_agent_voice_say_matches_speak(spoken):
    assert cli.main(["say", "hello"]) == 0
    assert spoken[0][0] == "hello"
