"""Opus codec access, mirroring :mod:`discord.opus`.

Encoding and decoding are implemented in Rust with ``audiopus`` (libopus), so unlike
``discord.py`` there is never a "the opus library could not be loaded" failure. ``load_opus``
and ``is_loaded`` are kept so existing code that calls them keeps working.
"""

from __future__ import annotations

from typing import Literal

from ._native import (
    CHANNELS,
    FRAME_LENGTH,
    OPUS_SILENCE,
    SAMPLES_PER_FRAME,
    SAMPLING_RATE,
    OpusDecoder,
    OpusEncoder,
    OpusError,
)
from .errors import OpusNotLoaded

__all__ = (
    'Encoder',
    'Decoder',
    'OpusError',
    'OpusNotLoaded',
    'load_opus',
    'is_loaded',
    'OPUS_SILENCE',
    'SAMPLE_SIZE',
    'FRAME_SIZE',
    'SAMPLES_PER_FRAME',
    'FRAME_LENGTH',
    'SAMPLING_RATE',
    'CHANNELS',
    'APPLICATION_CTL',
    'BAND_CTL',
    'SIGNAL_CTL',
    'APPLICATION_AUDIO',
    'APPLICATION_VOIP',
    'APPLICATION_LOWDELAY',
)

# These remain strings for backwards compatibility, exactly like discord.py.
APPLICATION_AUDIO = 'audio'
APPLICATION_VOIP = 'voip'
APPLICATION_LOWDELAY = 'lowdelay'

#: Bytes per stereo sample pair (`struct.calcsize('h') * CHANNELS`).
SAMPLE_SIZE = CHANNELS * 2
#: Bytes in one 20ms frame of 48kHz stereo signed 16 bit PCM.
FRAME_SIZE = SAMPLES_PER_FRAME * SAMPLE_SIZE

APPLICATION_CTL = Literal['audio', 'voip', 'lowdelay']
BAND_CTL = Literal['narrow', 'medium', 'wide', 'superwide', 'full']
SIGNAL_CTL = Literal['auto', 'voice', 'music']

#: The native encoder; its constructor and ``encode`` match ``discord.opus.Encoder``.
Encoder = OpusEncoder

#: The native decoder.
Decoder = OpusDecoder


def load_opus(name: str | None = None) -> None:
    """No-op: Opus is linked into the native extension.

    Parameters
    ----------
    name: Optional[:class:`str`]
        Ignored. Present so that ``discord.opus.load_opus`` call sites keep working.
    """


def is_loaded() -> bool:
    """Whether the Opus library is available. Always ``True`` here."""
    return True
