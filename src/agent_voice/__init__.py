"""Spoken status updates for coding agents: Kokoro TTS on Apple Silicon, macOS `say` as the fallback.

    from agent_voice import say
    say("The build is green and the branch is pushed.")
"""

from .speech import (
    DEFAULT_VOICE,
    MODEL_REPO,
    VoiceError,
    english_voices,
    is_muted,
    prefetch,
    save,
    say,
    set_muted,
    synthesize,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_VOICE",
    "MODEL_REPO",
    "VoiceError",
    "english_voices",
    "is_muted",
    "prefetch",
    "save",
    "say",
    "set_muted",
    "synthesize",
]
