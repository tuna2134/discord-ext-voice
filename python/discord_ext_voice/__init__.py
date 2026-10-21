"""``discord_ext_voice`` — a Rust voice backend for ``discord.py``.

Quick start::

    import discord
    import discord_ext_voice as dev

    voice = await channel.connect(cls=dev.VoiceClient)
    voice.play(dev.FFmpegPCMAudio('song.mp3'), after=lambda error: print('done', error))

The Python layer is a thin extension of ``discord.py``: :class:`VoiceClient` implements
``discord.VoiceProtocol`` and is hosted by discord.py's connection machinery. Everything
voice-specific is this package's own and happens in Rust:

======================  ==========================================================
Concern                 Rust crate
======================  ==========================================================
Voice websocket         ``tokio`` + ``tokio-rustls`` + ``tokio-tungstenite``
Voice gateway payloads  ``voice-model``
RTP encryption          ``xsalsa20poly1305`` / ``chacha20poly1305``
End-to-end encryption   ``davey`` (DAVE / MLS)
Opus codec              ``oprs`` (pure-Rust libopus, used natively)
Audio sources           :mod:`discord_ext_voice.sources` (no binary, no C library)
Audio decode/resample   ``symphonia`` + ``rubato``
Connection flow         :mod:`discord_ext_voice.voice_state` + ``_native.ConnectionFlow``
Playback clock          :mod:`discord_ext_voice.player` (own thread)
Python bindings         ``pyo3`` + ``pyo3-async-runtimes`` (maturin)
======================  ==========================================================
"""

from __future__ import annotations

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
from ._native import OPUS_SILENCE
from .player import AudioPlayer
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
#: Read from the native module, so the Rust and Python sides cannot drift (the test suite also
#: asserts they match ``discord.opus.Encoder``).
SAMPLES_PER_FRAME: int = _native.SAMPLES_PER_FRAME
FRAME_LENGTH: int = _native.FRAME_LENGTH
SAMPLE_SIZE: int = _native.SAMPLE_SIZE
FRAME_SIZE: int = _native.FRAME_SIZE
SAMPLING_RATE: int = _native.SAMPLING_RATE
CHANNELS: int = _native.CHANNELS

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
