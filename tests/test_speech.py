"""The engine's decisions — fallback, mute, voice validation — without loading the model."""

import subprocess

import numpy as np
import pytest

from agent_voice import speech


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path / "state"))
    for key in ("AGENT_VOICE_MUTE", "AGENT_VOICE_VOICE", "AGENT_VOICE_SPEED"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def commands(monkeypatch):
    """Records afplay/say invocations instead of making noise."""
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, check: calls.append(argv))
    return calls


def failing_synthesize(*args, **kwargs):
    raise RuntimeError("no model here")


def test_kokoro_output_is_played(monkeypatch, commands):
    monkeypatch.setattr(speech, "synthesize", lambda *a, **k: (np.zeros(2400, dtype=np.float32), 24000))
    assert speech.say("hello") == "kokoro"
    assert [argv[0] for argv in commands] == [speech.AFPLAY]


def test_falls_back_to_say_when_kokoro_fails(monkeypatch, commands, capsys):
    monkeypatch.setattr(speech, "synthesize", failing_synthesize)
    assert speech.say("hello") == "say"
    assert commands == [[speech.SAY, "hello"]]
    assert "no model here" in capsys.readouterr().err


def test_no_fallback_raises_the_original_error(monkeypatch, commands):
    monkeypatch.setattr(speech, "synthesize", failing_synthesize)
    with pytest.raises(RuntimeError, match="no model here"):
        speech.say("hello", fallback=False)
    assert commands == []


def test_save_fallback_writes_a_wave_file(monkeypatch, commands, tmp_path):
    monkeypatch.setattr(speech, "synthesize", failing_synthesize)
    out = tmp_path / "x.wav"
    assert speech.save("hello", out) == "say"
    assert commands[0][:3] == [speech.SAY, "-o", str(out)]


def test_empty_text_is_an_error_not_a_fallback(commands):
    with pytest.raises(speech.VoiceError, match="nothing to say"):
        speech.say("   ")
    assert commands == []


@pytest.mark.parametrize("how", ["flag", "env"])
def test_muted_says_nothing(monkeypatch, commands, how):
    monkeypatch.setattr(speech, "synthesize", failing_synthesize)
    if how == "flag":
        speech.set_muted(True)
    else:
        monkeypatch.setenv("AGENT_VOICE_MUTE", "1")
    assert speech.say("hello") == "muted"
    assert commands == []


def test_unmute_removes_the_flag():
    speech.set_muted(True)
    speech.set_muted(False)
    assert not speech.is_muted()


@pytest.mark.parametrize("voice, code", [("af_heart", "a"), ("bm_george", "b")])
def test_english_voices_map_to_their_language(voice, code):
    assert speech.lang_code(voice) == code


def test_non_english_voice_is_rejected():
    with pytest.raises(speech.VoiceError, match="English only"):
        speech.lang_code("jf_alpha")


def test_bad_speed_env_is_reported(monkeypatch):
    monkeypatch.setenv("AGENT_VOICE_SPEED", "fast")
    with pytest.raises(speech.VoiceError, match="AGENT_VOICE_SPEED"):
        speech.default_speed()


def test_speaking_never_downloads_the_model(monkeypatch, commands, capsys):
    """Only prefetch may contact Hugging Face; a missing model means falling back, not downloading."""
    import huggingface_hub
    import mlx_audio.utils
    from huggingface_hub.errors import LocalEntryNotFoundError

    def cache_only(repo_id, *, local_files_only=False, **kwargs):
        assert local_files_only, "speaking reached for the network"
        raise LocalEntryNotFoundError("not cached")

    def download(*args, **kwargs):
        raise AssertionError("speaking downloaded the model")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", cache_only)
    monkeypatch.setattr(mlx_audio.utils, "get_model_path", download)
    monkeypatch.setattr(speech, "_model", None)
    assert speech.say("hello") == "say"
    assert "agent-voice prefetch" in capsys.readouterr().err


def test_an_install_too_deep_for_espeak_falls_back_instead_of_dying(monkeypatch, commands, capsys):
    """espeak-ng exits the process on a data path over 159 bytes; catch it before it loads."""
    import espeakng_loader

    deep = "/Users/someone/" + "d" * 200 + "/espeak-ng-data"
    monkeypatch.setattr(espeakng_loader, "get_data_path", lambda: deep)
    monkeypatch.setattr(speech, "_load_model", lambda verbose=False: pytest.fail("reached the model"))
    assert speech.say("hello") == "say"
    assert "too deep for espeak-ng" in capsys.readouterr().err


def test_a_normal_install_path_is_fine(monkeypatch):
    import espeakng_loader

    normal = "/Users/someone/.local/share/uv/tools/agent-voice/lib/python3.12/site-packages/espeakng_loader/espeak-ng-data"
    monkeypatch.setattr(espeakng_loader, "get_data_path", lambda: normal)
    assert speech.espeak_path_problem() is None
