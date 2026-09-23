"""Kokoro text-to-speech through mlx-audio, with macOS `say` as the fallback."""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import subprocess
import sys
import tempfile
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

MODEL_REPO = "mlx-community/Kokoro-82M-bf16"
DEFAULT_VOICE = "af_heart"
DEFAULT_SAMPLE_RATE = 24000

# A Kokoro voice's first letter is its language. misaki[en] covers only
# American (a) and British (b) English; the other languages need other extras.
ENGLISH_LANG_CODES = ("a", "b")

AFPLAY = "/usr/bin/afplay"
SAY = "/usr/bin/say"


class VoiceError(RuntimeError):
    """Speech could not be produced."""


# MARK: - settings


def state_dir() -> Path:
    """Where the mute flag and the playback lock live."""
    override = os.environ.get("AGENT_VOICE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".agent-voice"


def _mute_flag() -> Path:
    return state_dir() / "muted"


def is_muted() -> bool:
    if os.environ.get("AGENT_VOICE_MUTE", "").strip().lower() in ("1", "true", "yes"):
        return True
    return _mute_flag().exists()


def set_muted(muted: bool) -> None:
    flag = _mute_flag()
    if muted:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
    else:
        flag.unlink(missing_ok=True)


def default_voice() -> str:
    return os.environ.get("AGENT_VOICE_VOICE") or DEFAULT_VOICE


def default_speed() -> float:
    raw = os.environ.get("AGENT_VOICE_SPEED")
    if not raw:
        return 1.0
    try:
        return float(raw)
    except ValueError:
        raise VoiceError(f"AGENT_VOICE_SPEED={raw!r} is not a number") from None


def lang_code(voice: str) -> str:
    code = voice[:1]
    if code not in ENGLISH_LANG_CODES:
        raise VoiceError(
            f"{voice!r} is not an English voice; agent-voice speaks English only "
            "(voices starting af_, am_, bf_ or bm_ — see: agent-voice voices)"
        )
    return code


# MARK: - model


def model_path(download: bool = False) -> Path:
    """The local snapshot of the Kokoro weights.

    Hugging Face is contacted for one reason only: downloading the model, and
    only when `download` is true — which only `prefetch` passes. Speaking reads
    the cache and never goes to the network, so an announcement can neither wait
    on nor fail behind a proxy, and a missing model is an error that says to
    prefetch rather than a 340 MB download inside an agent's shell command.
    """
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    from mlx_audio.utils import DEFAULT_ALLOW_PATTERNS, get_model_path

    try:
        # mlx-audio downloads only the files matching its patterns; without the
        # same patterns the cache check counts the README and friends as missing.
        return Path(snapshot_download(MODEL_REPO, local_files_only=True, allow_patterns=DEFAULT_ALLOW_PATTERNS))
    except LocalEntryNotFoundError:
        if not download:
            raise VoiceError("the Kokoro model is not downloaded yet; run: agent-voice prefetch") from None
    return Path(get_model_path(MODEL_REPO))


def prefetch(verbose: bool = True) -> Path:
    """Downloads the model if it is not cached yet, then renders one sentence so
    that a broken install (spaCy's model, say) fails here, not at the first
    announcement. The only entry point that uses the network."""
    path = model_path(download=True)
    synthesize("Ready.", verbose=verbose)
    return path


@contextlib.contextmanager
def _quiet(verbose: bool) -> Iterator[None]:
    """mlx-audio narrates every load and sentence on stdout; keep it out of the caller's output."""
    if verbose:
        yield
        return
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield


_model = None
_model_lock = threading.Lock()


def _load_model(verbose: bool = False):
    """Loads Kokoro once per process, so a library caller pays the ~2 s only on the first sentence."""
    global _model
    with _model_lock:
        if _model is None:
            with _quiet(verbose):
                path = model_path()
                from mlx_audio.tts.utils import load_model

                model = load_model(path)
                # Kokoro fetches voice files by repo id, and a model loaded from
                # a local path would otherwise pass that path as the id.
                if getattr(model, "repo_id", None) != MODEL_REPO:
                    model.repo_id = MODEL_REPO
            _model = model
        return _model


def synthesize(
    text: str, voice: str | None = None, speed: float | None = None, *, verbose: bool = False
) -> tuple[np.ndarray, int]:
    """Renders `text` with Kokoro. Returns (mono float samples, sample rate)."""
    import numpy as np

    text = _require_text(text)
    voice = voice or default_voice()
    speed = default_speed() if speed is None else speed
    code = lang_code(voice)
    model = _load_model(verbose)
    with _quiet(verbose):
        segments = list(model.generate(text=text, voice=voice, speed=speed, lang_code=code))
    if not segments:
        raise VoiceError("Kokoro produced no audio")
    audio = np.concatenate([np.asarray(segment.audio).reshape(-1) for segment in segments])
    return audio, getattr(model, "sample_rate", DEFAULT_SAMPLE_RATE)


# MARK: - playback


@contextlib.contextmanager
def _playback_lock() -> Iterator[None]:
    """One voice at a time across processes: two agents finishing together
    take turns instead of talking over each other."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "playback.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_wav(audio: np.ndarray, sample_rate: int, path: str | os.PathLike) -> None:
    import soundfile as sf

    sf.write(os.fspath(path), audio, sample_rate)


def play(audio: np.ndarray, sample_rate: int) -> None:
    with tempfile.TemporaryDirectory(prefix="agent-voice-") as directory:
        wav = Path(directory) / "speech.wav"
        _write_wav(audio, sample_rate, wav)
        with _playback_lock():
            subprocess.run([AFPLAY, str(wav)], check=True)


def _say_fallback(text: str, out: Path | None) -> None:
    if out is None:
        with _playback_lock():
            subprocess.run([SAY, text], check=True)
    else:
        # `say` picks the container from the extension; WAVE needs an explicit sample format.
        subprocess.run([SAY, "-o", str(out), "--file-format=WAVE", "--data-format=LEI16", text], check=True)


# MARK: - public entry points


def _require_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise VoiceError("nothing to say")
    return text


def _warn_fallback(exc: Exception) -> None:
    print(f"agent-voice: Kokoro unavailable ({exc}); using macOS say", file=sys.stderr)


def say(
    text: str,
    voice: str | None = None,
    speed: float | None = None,
    *,
    fallback: bool = True,
    verbose: bool = False,
) -> str:
    """Speaks `text` aloud and blocks until it has finished.

    Returns the engine that spoke: "kokoro", "say" (Kokoro failed and
    `fallback` is on), or "muted". A caller that would rather fail than fall
    back passes `fallback=False` and gets the original exception.
    """
    text = _require_text(text)
    if is_muted():
        return "muted"
    try:
        audio, rate = synthesize(text, voice, speed, verbose=verbose)
    except Exception as exc:
        if not fallback:
            raise
        _warn_fallback(exc)
        _say_fallback(text, None)
        return "say"
    play(audio, rate)
    return "kokoro"


def save(
    text: str,
    path: str | os.PathLike,
    voice: str | None = None,
    speed: float | None = None,
    *,
    fallback: bool = True,
    verbose: bool = False,
) -> str:
    """Writes `text` as a WAV file instead of playing it. Same return values as `say`, minus "muted"."""
    text = _require_text(text)
    out = Path(path)
    try:
        audio, rate = synthesize(text, voice, speed, verbose=verbose)
    except Exception as exc:
        if not fallback:
            raise
        _warn_fallback(exc)
        _say_fallback(text, out)
        return "say"
    _write_wav(audio, rate, out)
    return "kokoro"


def english_voices() -> list[str]:
    """The English voices in the downloaded model, sorted."""
    voices = model_path(download=False) / "voices"
    return sorted(
        file.stem for file in voices.glob("*.safetensors") if file.stem[:1] in ENGLISH_LANG_CODES
    )
