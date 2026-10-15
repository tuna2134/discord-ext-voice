"""Exception types.

The native core raises ``VoiceError`` subclasses. Where ``discord.py`` defines an equivalent
error, that class is *raised* (``ClientException`` for the client API, ``ConnectionClosed`` for
the websocket) so ``except discord.ClientException`` keeps working; nothing here inherits from
discord.py, so an ``except`` clause has to name the class it wants to catch.
"""

from __future__ import annotations

from discord.errors import (  # noqa: F401  (re-exported: this library raises both)
    ClientException,
    ConnectionClosed,
)

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
)


class FFmpegSourceError(FFmpegError):
    """FFmpeg could not open, decode or seek an audio source.

    Raised while the source is being constructed, where ``discord.py`` only surfaces the failure
    once its ``ffmpeg`` process exits.
    """
