"""Exception types raised by :mod:`discord_ext_voice`.

The native core raises ``VoiceError`` subclasses; this module lines them up with the names
``discord.py`` uses so that ``except discord.ClientException`` style code keeps working when
the library is plugged into a real ``discord.py`` bot.
"""

from __future__ import annotations

from ._native import (
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

try:  # pragma: no cover - depends on whether discord.py is installed
    from discord.errors import ClientException as _DiscordClientException
except Exception:  # pragma: no cover
    _DiscordClientException = None


if _DiscordClientException is not None:

    class ClientException(_DiscordClientException, VoiceError):  # type: ignore[misc]
        """Mirrors ``discord.ClientException`` so both ``except`` clauses catch it."""

else:

    class ClientException(VoiceError):  # type: ignore[no-redef]
        """Raised for the same reasons ``discord.ClientException`` would be."""


class OpusNotLoaded(ClientException):
    """Raised when a PCM source is used but no Opus encoder is available.

    The native core always links libopus, so this is only raised for API compatibility.
    """


class FFmpegProcessError(ClientException):
    """Raised when the ``ffmpeg`` fallback process exits with an error."""


__all__ = (
    'VoiceError',
    'ClientException',
    'NotConnected',
    'AlreadyConnected',
    'HandshakeError',
    'ConnectionClosedError',
    'ProtocolError',
    'EncryptionError',
    'DaveError',
    'OpusError',
    'OpusNotLoaded',
    'FFmpegError',
    'FFmpegProcessError',
)
