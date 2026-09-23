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
    assert cli.main(["say", "All", "done."]) == 0
    assert spoken[0][0] == "All done."


def test_text_comes_from_stdin_when_no_arguments(spoken, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('It\'s "quoted" and costs $5!\n'))
    assert cli.main(["say"]) == 0
    assert spoken[0][0] == 'It\'s "quoted" and costs $5!\n'


def test_options_are_passed_through(spoken):
    cli.main(["say", "hi", "--voice", "bm_george", "--speed", "1.2", "--no-fallback"])
    assert spoken[0][1] == {"voice": "bm_george", "speed": 1.2, "fallback": False, "verbose": False}


def test_nothing_to_say_is_a_usage_error(spoken, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["say"]) == 2
    assert spoken == []


def test_failure_exits_nonzero_with_a_message(monkeypatch, capsys):
    def broken(text, **options):
        raise RuntimeError("no audio device")

    monkeypatch.setattr(speech, "say", broken)
    assert cli.main(["say", "hi"]) == 1
    assert "no audio device" in capsys.readouterr().err



def test_say_turns_hugging_face_offline(spoken, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    cli.main(["say", "hi"])
    import os

    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_say_goes_through_the_voice_server_when_there_is_one(monkeypatch, spoken, capsys):
    from agent_voice import server

    sent = []
    monkeypatch.setattr(server, "speak", lambda text, *a, **kw: sent.append(text) or {"ok": True, "engine": "say", "warning": "Kokoro unavailable (x); using macOS say"})
    assert cli.main(["say", "hello"]) == 0
    assert sent == ["hello"] and spoken == []
    assert "Kokoro unavailable" in capsys.readouterr().err


def test_say_speaks_in_process_without_a_server(monkeypatch, spoken):
    from agent_voice import server

    monkeypatch.setattr(server, "speak", lambda *a, **kw: None)
    assert cli.main(["say", "hello"]) == 0
    assert spoken[0][0] == "hello"


def test_a_server_error_is_reported(monkeypatch, spoken, capsys):
    from agent_voice import server

    monkeypatch.setattr(server, "speak", lambda *a, **kw: {"ok": False, "error": "no model"})
    assert cli.main(["say", "--no-fallback", "hello"]) == 1
    assert "no model" in capsys.readouterr().err
