"""``discord_ext_voice`` — a Rust voice backend for ``discord.py``.

Quick start::

    import discord
    import discord_ext_voice as dev

    voice = await channel.connect(cls=dev.VoiceClient)
    voice.play(dev.FFmpegPCMAudio('song.mp3'), after=lambda error: print('done', error))

The Python layer is a thin extension of ``discord.py``: :class:`VoiceClient` implements
``discord.VoiceProtocol`` and reuses its connection flow state machine, gateway opcodes and
audio player thread. The audio sources (:mod:`discord_ext_voice.sources`) and everything
voice-specific are this package's own and happen in Rust:

======================  ==========================================================
Concern                 Rust crate
======================  ==========================================================
Voice websocket         ``tokio`` + ``tokio-rustls`` + ``tokio-tungstenite``
Voice gateway payloads  ``voice-model``
RTP encryption          ``xsalsa20poly1305`` / ``chacha20poly1305``
End-to-end encryption   ``davey`` (DAVE / MLS)
Opus codec              ``audiopus``
Audio sources           :mod:`discord_ext_voice.sources` (``ac-ffmpeg``, no binary)
Audio decode/resample   ``ac-ffmpeg`` + ``libswresample``
Python bindings         ``pyo3`` + ``pyo3-async-runtimes`` (maturin)
======================  ==========================================================
"""

from __future__ import annotations

from discord.opus import OPUS_SILENCE
from discord.player import AudioPlayer  # the playback clock, reused unchanged

from . import opus
from ._native import DAVE_PROTOCOL_VERSION, SUPPORTED_MODES, __version__, probe
from .errors import (
    AlreadyConnected,
    ClientException,
    ConnectionClosed,
    ConnectionClosedError,
    DaveError,
    EncryptionError,
    FFmpegError,
    FFmpegSourceError,
    HandshakeError,
    NotConnected,
    OpusError,
    ProtocolError,
    VoiceError,
)
from .gateway import DiscordVoiceWebSocket
from .sources import (
    AudioSource,
    FFmpegOpusAudio,
    FFmpegPCMAudio,
    PCMAudio,
    PCMVolumeTransformer,
    is_audio_source,
)
from .voice_client import VoiceClient, VoiceProtocol, has_dave, has_nacl
from .voice_state import ConnectionFlowState, VoiceConnectionState

#: The geometry of one Discord voice frame. Taken from the native encoder so the Python and
#: Rust sides cannot drift; the test suite asserts they match ``discord.opus.Encoder``.
SAMPLES_PER_FRAME: int = opus.Encoder.SAMPLES_PER_FRAME
FRAME_LENGTH: int = opus.Encoder.FRAME_LENGTH
SAMPLE_SIZE: int = opus.Encoder.SAMPLE_SIZE
FRAME_SIZE: int = opus.Encoder.FRAME_SIZE
SAMPLING_RATE: int = opus.Encoder.SAMPLING_RATE
CHANNELS: int = opus.Encoder.CHANNELS

__all__ = (
    # clients
    'VoiceClient',
    'VoiceProtocol',
    'VoiceConnectionState',
    'ConnectionFlowState',
    'DiscordVoiceWebSocket',
    # audio
    'AudioPlayer',
    'AudioSource',
    'PCMAudio',
    'FFmpegPCMAudio',
    'FFmpegOpusAudio',
    'PCMVolumeTransformer',
    'is_audio_source',
    'probe',
    # codec
    'opus',
    'OPUS_SILENCE',
    'SAMPLES_PER_FRAME',
    'FRAME_LENGTH',
    'FRAME_SIZE',
    'SAMPLE_SIZE',
    'SAMPLING_RATE',
    'CHANNELS',
    'DAVE_PROTOCOL_VERSION',
    'SUPPORTED_MODES',
    'has_nacl',
    'has_dave',
    # errors
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
    'FFmpegError',
    'FFmpegSourceError',
    'ConnectionClosed',
    '__version__',
)
