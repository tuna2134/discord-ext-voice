"""Opus encoding and decoding.

The :class:`Encoder` and :class:`Decoder` here are the native ones (`oprs`, a pure-Rust port of
libopus 1.6.1), which means they work without ``discord.opus.load_opus`` and without a C library.
The parameter type aliases come from :mod:`discord.opus` so the two implementations can never
disagree, and the silence packet is the native constant the player sends.
"""

from __future__ import annotations

from discord.opus import (  # noqa: F401  (re-exported)
    APPLICATION_AUDIO,
    APPLICATION_LOWDELAY,
    APPLICATION_VOIP,
)

from ._native import OPUS_SILENCE
from ._native import OpusDecoder as Decoder
from ._native import OpusEncoder as Encoder
from ._native import OpusError

__all__ = (
    'Encoder',
    'Decoder',
    'OpusError',
    'OPUS_SILENCE',
    'APPLICATION_AUDIO',
    'APPLICATION_VOIP',
    'APPLICATION_LOWDELAY',
)
