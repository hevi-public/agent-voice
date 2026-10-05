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


# MARK: - say --progress, say --tag, cancel


@pytest.fixture
def served(monkeypatch):
    """A voice server stand-in: what `server.speak` was asked, and what it answers
    (`reply`), calling `on_playing` first as the real one does when the sound begins."""
    from agent_voice import server

    class Served:
        asked = []
        reply = {"ok": True, "engine": "kokoro", "warning": None}
        plays = True

    def fake_speak(text, voice=None, speed=None, *, on_playing=None, **kw):
        Served.asked.append({"text": text, "on_playing": on_playing, **kw})
        if on_playing is not None and Served.plays:
            on_playing()
        return Served.reply

    Served.asked = []
    monkeypatch.setattr(server, "speak", fake_speak)
    return Served


def test_progress_prints_playing_once_and_nothing_else_on_stdout(served, capsys):
    assert cli.main(["say", "--progress", "hello"]) == 0
    assert capsys.readouterr().out == "playing\n"
    assert served.asked[0]["on_playing"] is not None


def test_progress_is_printed_before_say_returns(served, capsys, monkeypatch):
    order = []
    monkeypatch.setattr(cli, "_announce_playing", lambda: order.append("playing"))
    assert cli.main(["say", "--progress", "hello"]) == 0
    order.append("exit")
    assert order == ["playing", "exit"]


def test_without_progress_nothing_is_printed_and_no_callback_is_given(served, capsys):
    assert cli.main(["say", "hello"]) == 0
    assert capsys.readouterr().out == ""
    assert served.asked[0]["on_playing"] is None and served.asked[0]["tag"] is None


def test_a_muted_say_prints_no_playing_and_exits_0(served, spoken, capsys):
    speech.set_muted(True)
    assert cli.main(["say", "--progress", "--tag", "t:1", "hello"]) == 0
    assert capsys.readouterr().out == ""
    assert served.asked == [] and spoken == []


def test_the_tag_is_passed_to_the_server(served):
    cli.main(["say", "--tag", "trellis:1:2", "hello"])
    assert served.asked[0]["tag"] == "trellis:1:2"


def test_a_withdrawn_line_exits_3_with_a_message_and_no_playing(served, capsys):
    served.reply = {"ok": True, "engine": "cancelled", "warning": None}
    served.plays = False
    assert cli.main(["say", "--progress", "--tag", "t:1", "hello"]) == cli.EXIT_CANCELLED == 3
    captured = capsys.readouterr()
    assert captured.out == "" and "withdrawn" in captured.err


def test_progress_without_a_server_prints_playing_as_in_process_playback_starts(monkeypatch, capsys):
    from agent_voice import server

    monkeypatch.setattr(server, "speak", lambda *a, **kw: None)

    def fake_say(text, on_start=None, **options):
        assert capsys.readouterr().out == ""  # not before the sound
        on_start()
        return "kokoro"

    monkeypatch.setattr(speech, "say", fake_say)
    assert cli.main(["say", "--progress", "hello"]) == 0
    assert capsys.readouterr().out == "playing\n"


def test_no_extra_options_reach_the_speech_without_progress(spoken):
    cli.main(["say", "--tag", "t:1", "hello"])
    assert "on_start" not in spoken[0][1]


def test_in_process_playback_calls_on_start_before_the_player_runs(monkeypatch):
    import numpy as np

    order = []
    monkeypatch.setattr(speech, "synthesize", lambda *a, **kw: (np.zeros(10, dtype=np.float32), 24000))
    monkeypatch.setattr(speech, "_write_wav", lambda *a, **kw: None)
    monkeypatch.setattr(speech.subprocess, "run", lambda argv, **kw: order.append("play"))
    assert speech.say("hello", on_start=lambda: order.append("start")) == "kokoro"
    assert order == ["start", "play"]


def test_in_process_muted_never_calls_on_start(monkeypatch):
    speech.set_muted(True)
    called = []
    assert speech.say("hello", on_start=lambda: called.append(1)) == "muted" and called == []


def test_help_lists_progress_and_tag(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["say", "--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "--progress" in out and "--tag" in out and "3" in out


def test_cancel_withdraws_only_what_has_not_begun_and_always_exits_0(monkeypatch, capsys):
    from agent_voice import server

    asked = []
    monkeypatch.setattr(server, "cancel", lambda tag, *, playing=True: asked.append((tag, playing)) or 1)
    assert cli.main(["cancel", "trellis:1:2"]) == 0
    assert asked == [("trellis:1:2", False)]
    assert capsys.readouterr().err == ""  # quiet unless -v
    assert cli.main(["cancel", "-v", "trellis:1:2"]) == 0
    assert "withdrew 1 queued line" in capsys.readouterr().err


def test_cancel_of_an_unknown_tag_exits_0_and_says_so_with_v(monkeypatch, capsys):
    from agent_voice import server

    monkeypatch.setattr(server, "cancel", lambda tag, *, playing=True: 0)
    assert cli.main(["cancel", "-v", "nobody"]) == 0
    assert "nothing queued" in capsys.readouterr().err


def test_cancel_without_a_server_exits_0_and_starts_none(monkeypatch):
    from agent_voice import server

    def must_not_start():
        raise AssertionError("cancel started a voice server")

    monkeypatch.setattr(server, "start", must_not_start)
    assert cli.main(["cancel", "trellis:1:2"]) == 0
