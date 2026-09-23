"""The voice server's queue and socket protocol, with a fake voice and a fake speaker."""

import shutil
import tempfile
import threading
import time

import numpy as np
import pytest

from agent_voice import server, speech


class FakePlayer:
    """Stands in for afplay/say: 'plays' until released or terminated."""

    def __init__(self):
        self.played = []
        self.release = threading.Event()
        self.auto = True  # finish at once unless a test holds playback

    def __call__(self, argv):
        player = self

        class Process:
            terminated = False

            def wait(self):
                if not player.auto:
                    player.release.wait(5)
                return 0

            def terminate(self):
                self.terminated = True
                player.release.set()

        self.played.append(argv)
        return Process()


def fake_synthesize(text, voice=None, speed=None):
    if text == "broken":
        raise RuntimeError("no model")
    return np.zeros(240, dtype=np.float32), 24000


@pytest.fixture
def player():
    return FakePlayer()


@pytest.fixture
def voice(player):
    return server.Voice(synthesize=fake_synthesize, player=player, idle=60)


def run_in_background(voice):
    thread = threading.Thread(target=voice.run, daemon=True)
    thread.start()
    return thread


# MARK: - the queue


def test_speaks_in_order_and_reports_the_engine(voice, player):
    jobs = [server.Job("one"), server.Job("two")]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    for job in jobs:
        assert job.done.wait(2)
    assert [job.engine for job in jobs] == ["kokoro", "kokoro"]
    assert all(argv[0] == speech.AFPLAY for argv in player.played)
    voice.stop()


def test_kokoro_failure_falls_back_to_say(voice, player):
    job = server.Job("broken")
    voice.submit(job)
    run_in_background(voice)
    assert job.done.wait(2)
    assert job.engine == "say" and "no model" in job.warning
    assert player.played == [[speech.SAY, "broken"]]
    voice.stop()


def test_without_fallback_the_error_is_returned(voice, player):
    job = server.Job("broken", fallback=False)
    voice.submit(job)
    run_in_background(voice)
    assert job.done.wait(2)
    assert job.error == "no model" and player.played == []
    voice.stop()


def test_cancel_drops_queued_announcements_by_tag_and_prefix(voice, player):
    keep = server.Job("keep", tag="other-session:abc")
    exact = server.Job("exact", tag="s1:abc")
    same_session = server.Job("same session", tag="s1:def")
    for job in (exact, same_session, keep):
        voice.submit(job)
    assert voice.cancel("s1:abc") == 1
    assert voice.cancel("s1") == 1
    run_in_background(voice)
    assert keep.done.wait(2)
    assert exact.engine == same_session.engine == "cancelled"
    assert keep.engine == "kokoro" and len(player.played) == 1
    voice.stop()


def test_cancel_stops_what_is_playing(voice, player):
    player.auto = False
    job = server.Job("long", tag="s1:abc")
    voice.submit(job)
    run_in_background(voice)
    deadline = time.time() + 2
    while not player.played and time.time() < deadline:
        time.sleep(0.01)
    assert voice.cancel("s1") == 1
    assert job.done.wait(2)
    assert job.engine == "cancelled"
    voice.stop()


def test_goes_idle_and_returns(player):
    voice = server.Voice(synthesize=fake_synthesize, player=player, idle=0.2)
    thread = run_in_background(voice)
    thread.join(2)
    assert not thread.is_alive()


# MARK: - the socket


@pytest.fixture
def short_home(monkeypatch):
    """A Unix socket path must fit in 104 bytes; pytest's tmp_path doesn't."""
    home = tempfile.mkdtemp(prefix="av-", dir="/tmp")
    monkeypatch.setenv("AGENT_VOICE_HOME", home)
    monkeypatch.setenv("AGENT_VOICE_SERVER", "1")
    yield home
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def running(short_home, voice):
    thread = threading.Thread(target=server.serve, kwargs={"voice": voice}, daemon=True)
    thread.start()
    deadline = time.time() + 2
    while server.running() is None and time.time() < deadline:
        time.sleep(0.02)
    yield thread
    server.stop()
    thread.join(2)


def test_protocol_round_trip(running, player):
    assert server.running()["idle"] >= 0
    assert server.speak("hello")["engine"] == "kokoro"
    assert server.speak("later", tag="s1:x", wait=False) == {"ok": True, "queued": True}
    assert server.cancel("nobody") == 0


def test_stop_shuts_down_and_removes_the_socket(running):
    assert server.stop()
    running.join(2)
    assert not running.is_alive()
    assert not server.socket_path().exists()
    assert server.cancel("s1") == 0  # no server: nothing to do, nothing started


def test_a_second_server_leaves_the_first_alone(running, voice):
    assert server.serve(voice=voice) == 0
    assert server.running() is not None


def test_a_stale_socket_is_replaced(short_home, voice):
    server.socket_path().write_text("left by a crash")
    thread = threading.Thread(target=server.serve, kwargs={"voice": voice}, daemon=True)
    thread.start()
    deadline = time.time() + 2
    while server.running() is None and time.time() < deadline:
        time.sleep(0.02)
    assert server.running() is not None
    server.stop()
    thread.join(2)


def test_disabled_means_no_server(monkeypatch):
    monkeypatch.setenv("AGENT_VOICE_SERVER", "0")
    assert server.speak("hello") is None


def test_a_state_folder_too_deep_for_a_socket_means_no_server(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VOICE_SERVER", "1")
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path / ("d" * 120)))
    assert not server.usable()
    assert server.speak("hello") is None
