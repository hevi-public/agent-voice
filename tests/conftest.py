import pytest


@pytest.fixture(autouse=True)
def no_voice_server(monkeypatch, tmp_path):
    """Tests never start the real voice server or touch ~/.agent-voice."""
    monkeypatch.setenv("AGENT_VOICE_SERVER", "0")
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path / "state"))
