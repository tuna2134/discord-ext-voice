"""``discord_ext_voice`` — a ``discord.py``-compatible voice library in Rust.

Quick start::

    import discord_ext_voice as dev

    voice = await channel.connect(cls=dev.VoiceClient)
    voice.play(dev.FFmpegPCMAudio('song.mp3'), after=lambda e: print('done', e))

The public surface mirrors ``discord.voice_client.VoiceClient``, ``discord.player`` and
``discord.opus``, but the implementations are native:

======================  ==========================================================
Concern                 Rust crate
======================  ==========================================================
Voice websocket         ``tokio`` + ``tokio-rustls`` + ``tokio-tungstenite``
Voice gateway payloads  ``voice-model``
RTP encryption          ``xsalsa20poly1305`` / ``chacha20poly1305``
End-to-end encryption   ``davey`` (DAVE / MLS)
Opus codec              ``audiopus``
Audio decode/resample   ``ac-ffmpeg``
Python bindings         ``pyo3`` + ``pyo3-async-runtimes`` (maturin)
======================  ==========================================================
"""

from __future__ import annotations

from . import opus
from ._native import (
    CHANNELS,
    DAVE_PROTOCOL_VERSION,
    FRAME_LENGTH,
    OPUS_SILENCE,
    SAMPLES_PER_FRAME,
    SAMPLING_RATE,
    SUPPORTED_MODES,
    __version__,
    probe,
)
from .errors import (
    AlreadyConnected,
    ClientException,
    ConnectionClosedError,
    DaveError,
    EncryptionError,
    FFmpegError,
    FFmpegProcessError,
    HandshakeError,
    NotConnected,
    OpusError,
    OpusNotLoaded,
    ProtocolError,
    VoiceError,
)
from .gateway import DiscordVoiceWebSocket
from .oggparse import OggError, OggPage, OggStream
from .opus import FRAME_SIZE, SAMPLE_SIZE
from .player import (
    AudioPlayer,
    AudioSource,
    FFmpegAudio,
    FFmpegOpusAudio,
    FFmpegPCMAudio,
    PCMAudio,
    PCMVolumeTransformer,
)
from .voice_client import VoiceClient, VoiceProtocol, has_dave, has_nacl
from .voice_state import ConnectionFlowState, VoiceConnectionState

__all__ = (
    # clients
    'VoiceClient',
    'VoiceProtocol',
    'VoiceConnectionState',
    'ConnectionFlowState',
    'DiscordVoiceWebSocket',
    # audio
    'AudioSource',
    'AudioPlayer',
    'PCMAudio',
    'FFmpegAudio',
    'FFmpegPCMAudio',
    'FFmpegOpusAudio',
    'PCMVolumeTransformer',
    'OggStream',
    'OggPage',
    'OggError',
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
    'OpusNotLoaded',
    'FFmpegError',
    'FFmpegProcessError',
    '__version__',
)
