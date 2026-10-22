"""``discord_ext_voice`` — a Rust voice backend for ``discord.py``.

Quick start::

    import discord
    import discord_ext_voice as dev

    voice = await channel.connect(cls=dev.VoiceClient)
    voice.play(dev.FFmpegPCMAudio('song.mp3'), after=lambda error: print('done', error))

Only two things are Python: :class:`VoiceClient` (the ``discord.py`` boundary and the
orchestration of the connection flow) and :class:`AudioPlayer` (a thin wrapper over the native
playback thread). Everything else — the websocket, the transport, the RTP encryption, the
DAVE/MLS session, the Opus codec, the audio sources, the flow state machine and the connection
state itself — is Rust, exposed through ``discord_ext_voice._native``.
"""

from __future__ import annotations

from discord.errors import ClientException, ConnectionClosed

from . import _native
from ._native import (
    DAVE_PROTOCOL_VERSION,
    MAX_ATTEMPTS,
    OPUS_SILENCE,
    SUPPORTED_MODES,
    AlreadyConnected,
    AudioSource,
    CloseAction,
    ConnectionClosedError,
    ConnectionFlowState,
    DaveError,
    DiscordVoiceWebSocket,
    EncryptionError,
    FFmpegError,
    FFmpegOpusAudio,
    FFmpegPCMAudio,
    FFmpegSourceError,
    HandshakeError,
    NotConnected,
    OpusError,
    PCMAudio,
    PCMVolumeTransformer,
    ProtocolError,
    VoiceConnection,
    VoiceConnectionState,
    VoiceError,
    VoiceServerAction,
    VoiceStateAction,
    __version__,
    is_audio_source,
    probe,
    probe_async,
    set_read_ahead,
    set_read_stall_timeout,
)
from .player import AudioPlayer
from .voice_client import (
    DEFAULT_EXPECTED_PACKET_LOSS,
    DEFAULT_FEC,
    VoiceClient,
    VoiceProtocol,
    has_dave,
    has_nacl,
)

#: The geometry of one Discord voice frame. Read from the native module, so the Rust and Python
#: sides cannot drift (the test suite also asserts they match ``discord.opus.Encoder``).
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
    'VoiceConnection',
    'VoiceConnectionState',
    'ConnectionFlowState',
    'VoiceStateAction',
    'VoiceServerAction',
    'CloseAction',
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
    'probe_async',
    'set_read_ahead',
    'set_read_stall_timeout',
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
    'MAX_ATTEMPTS',
    'has_nacl',
    'has_dave',
    'DEFAULT_FEC',
    'DEFAULT_EXPECTED_PACKET_LOSS',
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
