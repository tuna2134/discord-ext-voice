"""The public API must line up with ``discord.py``'s voice surface."""

from __future__ import annotations

import inspect

import discord
import pytest

import discord_ext_voice as dev


def test_module_constants_match_discord_py():
    assert dev.SAMPLES_PER_FRAME == 960
    assert dev.FRAME_LENGTH == 20
    assert dev.SAMPLE_SIZE == 4
    assert dev.FRAME_SIZE == 3840
    assert dev.SAMPLING_RATE == 48000
    assert dev.CHANNELS == 2
    assert dev.OPUS_SILENCE == b'\xf8\xff\xfe'
    assert dev.DAVE_PROTOCOL_VERSION == 1
    assert dev.has_nacl is True
    assert dev.has_dave is True


def test_supported_modes_match_discord_py():
    assert dev.VoiceClient.supported_modes == discord.VoiceClient.supported_modes
    assert list(dev.SUPPORTED_MODES) == list(discord.VoiceClient.supported_modes)


@pytest.mark.parametrize(
    'name',
    [
        'play',
        'stop',
        'pause',
        'resume',
        'is_playing',
        'is_paused',
        'is_connected',
        'disconnect',
        'move_to',
        'send_audio_packet',
        'wait_until_connected',
        'create_connection_state',
        'on_voice_state_update',
        'on_voice_server_update',
        'connect',
        'checked_add',
        'cleanup',
        'source',
        'guild',
        'user',
        'session_id',
        'token',
        'endpoint',
        'ssrc',
        'mode',
        'secret_key',
        'ws',
        'timeout',
        'latency',
        'average_latency',
        'voice_privacy_code',
    ],
)
def test_voice_client_surface(name):
    assert hasattr(dev.VoiceClient, name), f'VoiceClient is missing {name}'


def test_audio_player_signature_matches_discord_py():
    parameters = inspect.signature(dev.AudioPlayer.__init__).parameters
    assert list(parameters) == ['self', 'source', 'client', 'after']
    assert parameters['after'].kind is inspect.Parameter.KEYWORD_ONLY
    assert dev.AudioPlayer.DELAY == 0.02


def test_ffmpeg_pcm_audio_accepts_discord_py_arguments():
    parameters = inspect.signature(dev.FFmpegPCMAudio.__init__).parameters
    for name in ('source', 'executable', 'pipe', 'stderr', 'before_options', 'options'):
        assert name in parameters, f'FFmpegPCMAudio is missing {name}'
    assert parameters['executable'].default == 'ffmpeg'
    assert parameters['pipe'].default is False


def test_opus_encoder_surface():
    encoder = dev.opus.Encoder(application='voip', bitrate=96, fec=True, bandwidth='full')
    assert encoder.SAMPLES_PER_FRAME == 960
    assert encoder.FRAME_LENGTH == 20
    assert encoder.FRAME_SIZE == 3840
    assert callable(encoder.encode)
    # The native encoder agrees with the geometry discord.py advertises.
    assert encoder.SAMPLES_PER_FRAME == discord.opus.Encoder.SAMPLES_PER_FRAME
    assert encoder.FRAME_SIZE == discord.opus.Encoder.FRAME_SIZE


def test_opus_encoder_rejects_bad_parameters():
    with pytest.raises(ValueError):
        dev.opus.Encoder(application='speech')
    with pytest.raises(ValueError):
        dev.opus.Encoder(bitrate=4)
    with pytest.raises(ValueError):
        dev.opus.Encoder(bandwidth='ultra')


def test_gateway_opcode_constants():
    ws = dev.DiscordVoiceWebSocket
    assert (ws.IDENTIFY, ws.SELECT_PROTOCOL, ws.READY, ws.HEARTBEAT) == (0, 1, 2, 3)
    assert (ws.SESSION_DESCRIPTION, ws.SPEAKING, ws.HEARTBEAT_ACK, ws.RESUME) == (4, 5, 6, 7)
    assert (ws.HELLO, ws.RESUMED) == (8, 9)
    assert ws.MLS_EXTERNAL_SENDER == 25
    assert ws.MLS_INVALID_COMMIT_WELCOME == 31


def test_exception_types_line_up_with_discord():
    assert dev.ClientException is discord.ClientException
    assert dev.FFmpegProcessError is discord.errors.FFmpegProcessError
    assert dev.OpusNotLoaded is discord.opus.OpusNotLoaded

    # A native FFmpeg failure is catchable as either flavour.
    assert issubclass(dev.FFmpegSourceError, dev.FFmpegError)
    assert issubclass(dev.FFmpegSourceError, discord.ClientException)


def test_connection_flow_state_values():
    # discord.py's `ConnectionFlowState` is a custom Enum, so compare `.value`.
    assert [state.value for state in dev.ConnectionFlowState] == list(range(9))
    assert dev.ConnectionFlowState.connected.value == 8
    assert dev.ConnectionFlowState is discord.voice_state.ConnectionFlowState
