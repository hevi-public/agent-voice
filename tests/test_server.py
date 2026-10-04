"""The voice server's queue and socket protocol, with a fake voice and a fake speaker."""

import shutil
import tempfile
import threading
import time
import warnings

import numpy as np
import pytest
import soundfile as sf

from agent_voice import server, speech


class FakeSynth:
    """Stands in for Kokoro. A line's audio is 240 samples plus the number in its
    text, so the fake speaker can tell which line a wav holds. Records when each
    rendering starts and ends, and on which threads; can be slowed (`delay`) or
    held until a test lets it go (`hold`)."""

    def __init__(self):
        self.started = []  # texts, in the order their rendering began
        self.finished = []  # texts, in the order it ended
        self.threads = set()
        self.holds = {}
        self.delay = 0.0
        self.active = 0
        self.overlapped = False  # did two renderings ever run at once?
        self.lock = threading.Lock()

    def hold(self, text):
        """Makes rendering `text` wait for the returned event."""
        gate = self.holds[text] = threading.Event()
        return gate

    def __call__(self, text, voice=None, speed=None):
        with self.lock:
            self.started.append(text)
            self.threads.add(threading.get_ident())
            self.active += 1
            self.overlapped = self.overlapped or self.active > 1
        try:
            if text in self.holds:
                self.holds[text].wait(5)
            time.sleep(self.delay)
            if text == "broken":
                raise RuntimeError("no model")
            return np.zeros(240 + (int(text) if text.isdigit() else 0), dtype=np.float32), 24000
        finally:
            with self.lock:
                self.active -= 1
                self.finished.append(text)


class FakePlayer:
    """Stands in for afplay/say. A playback lasts `duration` seconds; with `auto`
    off it lasts until the test ends it with `finish(n)`; `terminate` ends it
    early either way."""

    def __init__(self):
        self.played = []  # argv of each playback, in the order they began
        self.texts = []  # the line each one speaks
        self.processes = []
        self.starts = []
        self.ends = []
        self.threads = set()
        self.auto = True
        self.duration = 0.0

    def finish(self, n):
        """Ends the n-th playback (the first is 0)."""
        self.processes[n].finished.set()

    def __call__(self, argv):
        player = self

        class Process:
            terminated = False

            def __init__(self):
                self.finished = threading.Event()

            def wait(self):
                self.finished.wait(player.duration if player.auto else 5)
                player.ends.append(time.monotonic())
                return 0

            def terminate(self):
                self.terminated = True
                self.finished.set()

        process = Process()
        self.starts.append(time.monotonic())
        self.threads.add(threading.get_ident())
        # `say` is given the text itself; afplay a wav made by FakeSynth, as long as its number says
        self.texts.append(argv[1] if argv[0] == speech.SAY else str(sf.info(argv[1]).frames - 240))
        self.played.append(argv)
        self.processes.append(process)
        return process


def wait_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


@pytest.fixture
def player():
    return FakePlayer()


@pytest.fixture
def synth():
    return FakeSynth()


@pytest.fixture
def voice(synth, player):
    return server.Voice(synthesize=synth, player=player, idle=60)


@pytest.fixture
def leftovers(monkeypatch, tmp_path):
    """The temp folders a voice has left behind. They go in a folder of our own:
    a real voice server keeps its wavs in the system temp folder. A folder that is
    cleaned up only because it was garbage collected counts as left behind too."""
    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield lambda: sorted(folder.glob("agent-voice-*"))
    implicit = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning) and "Implicitly cleaning up" in str(w.message)]
    assert implicit == []


def run_in_background(voice):
    thread = threading.Thread(target=voice.run, daemon=True)
    thread.start()
    return thread


def in_flight(voice, player, playing, ahead):
    """Waits until `playing` is the line playing and `ahead` is synthesized and waiting for its turn."""

    def so():
        waiting = voice.ready
        return player.texts[-1:] == [playing] and waiting is not None and waiting.job.text == ahead

    assert wait_until(so)


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


def test_goes_idle_and_returns(synth, player):
    voice = server.Voice(synthesize=synth, player=player, idle=0.2)
    thread = run_in_background(voice)
    thread.join(2)
    assert not thread.is_alive()


# MARK: - synthesizing ahead


def test_lines_play_in_the_order_they_were_submitted(voice, player, synth):
    synth.delay, player.duration = 0.01, 0.02
    jobs = [server.Job(str(n)) for n in range(1, 8)]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    assert jobs[-1].done.wait(5)
    assert player.texts == [str(n) for n in range(1, 8)]
    assert synth.started == synth.finished == player.texts
    assert all(job.engine == "kokoro" for job in jobs)
    voice.stop()


def test_the_next_line_is_synthesized_while_this_one_plays(voice, player, synth):
    player.auto = False
    first, second = server.Job("1"), server.Job("2")
    voice.submit(first)
    voice.submit(second)
    run_in_background(voice)
    in_flight(voice, player, playing="1", ahead="2")
    assert synth.finished == ["1", "2"] and not first.done.is_set()
    player.finish(0)
    assert first.done.wait(2) and wait_until(lambda: player.texts == ["1", "2"])
    player.finish(1)
    assert second.done.wait(2)
    assert (first.engine, second.engine) == ("kokoro", "kokoro")
    voice.stop()


def test_only_one_line_is_synthesized_ahead(voice, player, synth):
    player.auto = False
    jobs = [server.Job(str(n)) for n in (1, 2, 3)]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    in_flight(voice, player, playing="1", ahead="2")
    time.sleep(0.2)  # long enough for a third rendering to start, were it going to
    assert synth.started == ["1", "2"]
    player.finish(0)  # line 2 now plays, which frees the place ahead for line 3
    assert wait_until(lambda: player.texts == ["1", "2"])
    in_flight(voice, player, playing="2", ahead="3")
    assert synth.started == ["1", "2", "3"] and not jobs[2].done.is_set()
    player.finish(1)
    assert wait_until(lambda: player.texts == ["1", "2", "3"])
    player.finish(2)
    assert jobs[2].done.wait(2)
    assert [job.engine for job in jobs] == ["kokoro"] * 3
    voice.stop()


def test_synthesis_stays_on_one_thread_and_playback_on_another(voice, player, synth):
    synth.delay, player.duration = 0.01, 0.02
    jobs = [server.Job(str(n)) for n in range(1, 7)]
    for job in jobs:
        voice.submit(job)
    thread = run_in_background(voice)
    assert jobs[-1].done.wait(5)
    assert synth.threads == {thread.ident}  # the thread that called run(): in the server, the main one
    assert len(player.threads) == 1 and player.threads != synth.threads
    assert not synth.overlapped
    voice.stop()


def test_the_say_fallback_is_pipelined_too(voice, player, synth):
    player.auto = False
    broken, after = server.Job("broken"), server.Job("1")
    voice.submit(broken)
    voice.submit(after)
    run_in_background(voice)
    in_flight(voice, player, playing="broken", ahead="1")  # Kokoro renders line 2 while `say` speaks line 1
    player.finish(0)
    assert wait_until(lambda: player.texts == ["broken", "1"])
    player.finish(1)
    assert after.done.wait(2)
    assert (broken.engine, after.engine) == ("say", "kokoro") and "no model" in broken.warning
    voice.stop()


def test_look_ahead_can_be_switched_off(synth, player):
    voice = server.Voice(synthesize=synth, player=player, idle=60, lookahead=False)
    player.auto = False
    jobs = [server.Job(str(n)) for n in (1, 2)]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    assert wait_until(lambda: player.texts == ["1"])
    time.sleep(0.2)
    assert synth.started == ["1"]  # line 2 waits for line 1 to end, as it used to
    player.finish(0)
    assert wait_until(lambda: player.texts == ["1", "2"])
    player.finish(1)
    assert jobs[1].done.wait(2) and synth.started == ["1", "2"]
    voice.stop()


@pytest.mark.parametrize("value, enabled", [("", True), ("1", True), ("yes", True), ("0", False), ("Off", False), (" no ", False)])
def test_look_ahead_is_on_unless_the_environment_says_off(monkeypatch, value, enabled):
    monkeypatch.setenv("AGENT_VOICE_LOOKAHEAD", value)
    assert server.lookahead_enabled() is enabled


def timeline(lookahead, lines=3, render=0.2, play=0.2):
    """Speaks `lines` lines that take `render` seconds to synthesize and `play` to
    play. Returns the silences between the lines, and the seconds until the last ends."""
    synth, player = FakeSynth(), FakePlayer()
    synth.delay, player.duration = render, play
    voice = server.Voice(synthesize=synth, player=player, idle=60, lookahead=lookahead)
    jobs = [server.Job(str(n)) for n in range(lines)]
    for job in jobs:
        voice.submit(job)
    began = time.monotonic()
    run_in_background(voice)
    assert jobs[-1].done.wait(10)
    voice.stop()
    return [start - end for start, end in zip(player.starts[1:], player.ends)], player.ends[-1] - began


def test_lines_play_back_to_back_with_look_ahead():
    """With fake durations (0.2 s to synthesize a line, 0.2 s to play it), taking one
    line at a time leaves 0.2 s of silence before every line after the first;
    synthesizing ahead leaves none, and the three lines end 0.4 s sooner."""
    ahead_gaps, ahead_total = timeline(lookahead=True)
    one_gaps, one_total = timeline(lookahead=False)
    assert max(ahead_gaps) < 0.1, ahead_gaps
    assert min(one_gaps) >= 0.15, one_gaps
    assert one_total - ahead_total > 0.25, (one_total, ahead_total)


# MARK: - cancelling at each stage


def test_cancel_drops_a_line_still_in_the_queue(voice, player, synth):
    player.auto = False
    jobs = [server.Job(str(n), tag=f"s{n}:x") for n in (1, 2, 3)]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    in_flight(voice, player, playing="1", ahead="2")  # line 3 is still queued
    assert voice.cancel("s3") == 1
    assert jobs[2].done.wait(2) and jobs[2].engine == "cancelled"
    player.finish(0)
    assert wait_until(lambda: player.texts == ["1", "2"])
    player.finish(1)
    assert jobs[1].done.wait(2)
    assert synth.started == ["1", "2"] and player.texts == ["1", "2"]  # line 3 was never even rendered
    voice.stop()


def test_cancel_drops_a_line_being_synthesized(voice, player, synth, leftovers):
    player.auto = False
    gate = synth.hold("2")
    jobs = [server.Job(str(n), tag=f"s{n}:x") for n in (1, 2, 3)]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    assert wait_until(lambda: player.texts == ["1"] and synth.started == ["1", "2"])  # 2 is mid-rendering
    assert voice.cancel("s2") == 1
    gate.set()  # its rendering returns, and is thrown away
    assert jobs[1].done.wait(2) and jobs[1].engine == "cancelled"
    in_flight(voice, player, playing="1", ahead="3")  # the queue moves on to line 3
    assert len(leftovers()) == 2  # line 1 playing from its wav, line 3 waiting with its own: line 2's is gone
    player.finish(0)
    assert wait_until(lambda: player.texts == ["1", "3"])
    player.finish(1)
    assert jobs[2].done.wait(2)
    assert player.texts == ["1", "3"] and leftovers() == []
    voice.stop()


def test_cancel_drops_the_line_synthesized_ahead_with_its_wav(voice, player, synth, leftovers):
    player.auto = False
    gate = synth.hold("3")
    jobs = [server.Job(str(n), tag=f"s{n}:x") for n in (1, 2, 3)]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    in_flight(voice, player, playing="1", ahead="2")  # line 3 waits in the queue
    assert len(leftovers()) == 2
    assert voice.cancel("s2") == 1
    assert jobs[1].done.is_set() and jobs[1].engine == "cancelled"  # answered by the cancel itself
    assert wait_until(lambda: synth.started == ["1", "2", "3"])  # the place ahead is free: line 3 is rendered at once
    assert len(leftovers()) == 1  # line 2's wav is gone; line 3's is not made yet
    gate.set()
    in_flight(voice, player, playing="1", ahead="3")
    player.finish(0)
    assert wait_until(lambda: player.texts == ["1", "3"])
    player.finish(1)
    assert jobs[2].done.wait(2)
    assert player.texts == ["1", "3"] and leftovers() == []
    voice.stop()


def test_cancel_stops_the_playing_line_and_the_one_ahead_plays_at_once(voice, player, synth):
    player.auto = False
    jobs = [server.Job(str(n), tag=f"s{n}:x") for n in (1, 2)]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    in_flight(voice, player, playing="1", ahead="2")
    assert voice.cancel("s1") == 1
    assert jobs[0].done.wait(2) and jobs[0].engine == "cancelled" and player.processes[0].terminated
    assert wait_until(lambda: player.texts == ["1", "2"])  # line 2 is not rendered again
    assert synth.started == ["1", "2"]
    player.finish(1)
    assert jobs[1].done.wait(2) and jobs[1].engine == "kokoro"
    voice.stop()


def test_cancel_by_session_reaches_every_stage_and_spares_other_sessions(voice, player, synth, leftovers):
    player.auto = False
    mine = [server.Job(str(n), tag=f"s1:{n}") for n in (1, 2, 3)]  # playing, synthesized ahead, queued
    theirs = server.Job("4", tag="s2:4")
    for job in (*mine, theirs):
        voice.submit(job)
    run_in_background(voice)
    in_flight(voice, player, playing="1", ahead="2")
    assert voice.cancel("s1") == 3
    assert all(job.done.wait(2) and job.engine == "cancelled" for job in mine)
    assert wait_until(lambda: player.texts == ["1", "4"])
    player.finish(1)
    assert theirs.done.wait(2) and theirs.engine == "kokoro"
    assert synth.started == ["1", "2", "4"] and leftovers() == []
    voice.stop()


# MARK: - clean-up


def test_temp_wavs_are_removed_after_playing(voice, player, leftovers):
    jobs = [server.Job(str(n)) for n in range(1, 5)]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    assert jobs[-1].done.wait(5)
    assert leftovers() == []
    voice.stop()


def test_a_failed_wav_write_leaves_nothing_behind(voice, player, leftovers, monkeypatch):
    def full_disk(audio, rate, path):
        raise OSError("disk full")

    monkeypatch.setattr(speech, "_write_wav", full_disk)
    job = server.Job("1")
    voice.submit(job)
    run_in_background(voice)
    assert job.done.wait(2)
    assert job.error == "disk full" and job.engine is None and player.played == []
    assert leftovers() == []
    voice.stop()


def test_a_temp_folder_that_will_not_go_does_not_stop_the_server(voice, player, leftovers, monkeypatch):
    def stuck(path, *args, **kwargs):
        raise OSError("device busy")

    monkeypatch.setattr(shutil, "rmtree", stuck)
    jobs = [server.Job(str(n)) for n in (1, 2)]
    for job in jobs:
        voice.submit(job)
    run_in_background(voice)
    assert jobs[1].done.wait(2)  # both lines are still spoken and answered
    assert [job.engine for job in jobs] == ["kokoro", "kokoro"] and len(leftovers()) == 2
    voice.stop()


# MARK: - idle


def test_idle_time_does_not_run_while_a_line_plays(synth, player):
    voice = server.Voice(synthesize=synth, player=player, idle=0.2)
    player.auto = False
    job = server.Job("1")
    voice.submit(job)
    thread = run_in_background(voice)
    assert wait_until(lambda: player.texts == ["1"])
    thread.join(0.6)  # three times the timeout, with the line still playing
    assert thread.is_alive() and voice.idle_for() == 0.0
    late = server.Job("2")
    voice.submit(late)  # the server has not quit: it still takes work
    player.finish(0)
    assert wait_until(lambda: player.texts == ["1", "2"])
    player.finish(1)
    assert job.done.wait(2) and late.done.wait(2) and late.engine == "kokoro"
    thread.join(2)  # the timeout runs from the end of the last line
    assert not thread.is_alive()


def test_idle_time_runs_only_when_nothing_is_in_hand(voice, player, synth):
    gate = synth.hold("1")
    player.auto = False
    jobs = [server.Job("1"), server.Job("2")]
    for job in jobs:
        voice.submit(job)
    assert voice.idle_for() == 0.0  # queued
    run_in_background(voice)
    assert wait_until(lambda: synth.started == ["1"])
    assert voice.idle_for() == 0.0  # being synthesized
    gate.set()
    in_flight(voice, player, playing="1", ahead="2")
    assert voice.idle_for() == 0.0  # one playing, one waiting its turn
    player.finish(0)
    assert wait_until(lambda: player.texts == ["1", "2"])
    player.finish(1)
    assert jobs[1].done.wait(2)
    assert wait_until(lambda: voice.idle_for() > 0)  # now it counts
    voice.stop()


# MARK: - stopping


def test_stop_lets_the_lines_in_hand_finish_and_run_returns(voice, player, synth):
    player.auto = False
    jobs = [server.Job(str(n)) for n in (1, 2, 3)]  # one playing, one ahead, one queued
    for job in jobs:
        voice.submit(job)
    thread = run_in_background(voice)
    in_flight(voice, player, playing="1", ahead="2")
    voice.stop()
    for n in range(3):
        assert wait_until(lambda n=n: len(player.played) == n + 1)
        if n == 2:
            thread.join(0.2)
            assert thread.is_alive()  # nothing is left to synthesize, but run() waits for the last line to play
        player.finish(n)
    thread.join(2)
    assert not thread.is_alive()
    assert all(job.done.is_set() for job in jobs) and player.texts == ["1", "2", "3"]
    assert [job.engine for job in jobs] == ["kokoro"] * 3


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


def test_a_waiting_say_returns_after_its_own_playback(running, player, synth):
    player.auto = False
    reply = {}
    caller = threading.Thread(target=lambda: reply.update(server.speak("1")), daemon=True)
    caller.start()
    assert wait_until(lambda: synth.finished == ["1"] and player.texts == ["1"])
    caller.join(0.3)
    assert caller.is_alive() and reply == {}  # rendered and playing, but not done
    assert server.speak("2", wait=False) == {"ok": True, "queued": True}  # a line that doesn't wait answers at once
    player.finish(0)
    caller.join(2)
    assert reply == {"ok": True, "engine": "kokoro", "warning": None}
    assert wait_until(lambda: player.texts == ["1", "2"])
    player.finish(1)


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
