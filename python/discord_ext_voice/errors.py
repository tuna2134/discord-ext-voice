"""Exception types.

The native core raises ``VoiceError`` subclasses. Where ``discord.py`` defines an equivalent
error, that class is reused (or subclassed) so ``except discord.ClientException`` keeps
working for code that mixes both clients.
"""

from __future__ import annotations

from discord.errors import (  # noqa: F401  (re-exported)
    ClientException,
    ConnectionClosed,
    DiscordException,
    FFmpegProcessError,
)
from discord.opus import OpusNotLoaded  # noqa: F401  (re-exported)

from ._native import (  # noqa: F401  (re-exported)
    AlreadyConnected,
    ConnectionClosedError,
    DaveError,
    EncryptionError,
    FFmpegError,
    HandshakeError,
    NotConnected,
    OpusError,
    ProtocolError,
    VoiceError,
)

__all__ = (
    # native
    'VoiceError',
    'NotConnected',
    'AlreadyConnected',
    'HandshakeError',
    'ConnectionClosedError',
    'ProtocolError',
    'EncryptionError',
    'DaveError',
    'OpusError',
    'FFmpegError',
    'FFmpegSourceError',
    # discord.py's
    'ClientException',
    'ConnectionClosed',
    'DiscordException',
    'FFmpegProcessError',
    'OpusNotLoaded',
)


class FFmpegSourceError(ClientException, FFmpegError):
    """FFmpeg could not open or decode an audio source.

    Inherits from both :class:`discord.ClientException` (what ``discord.py`` raises for a bad
    source) and the native :class:`FFmpegError`, so either ``except`` clause catches it.
    """
