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
        'cleanup',
        'source',
        'guild',
        'user',
        'session_id',
        'endpoint',
        'ssrc',
        'mode',
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


def test_ffmpeg_sources_have_a_native_signature():
    """The sources take the media plus `seek`; the FFmpeg CLI arguments are gone."""
    for cls in (dev.FFmpegPCMAudio, dev.FFmpegOpusAudio):
        # The native `tp_new` carries the text signature, so the class itself is inspected.
        parameters = inspect.signature(cls).parameters
        assert list(parameters)[0] == 'source'
        assert parameters['seek'].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters['seek'].default is None
        # Anything else (before_options, options, executable, ...) is rejected, which is what
        # the catch-all is for.
        assert any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ), f'{cls.__name__} should reject unknown arguments with a useful message'


def test_sources_are_this_packages_own():
    import discord.player

    for cls in (dev.AudioSource, dev.PCMAudio, dev.FFmpegPCMAudio, dev.FFmpegOpusAudio):
        assert cls.__module__ == 'discord_ext_voice._native'
    assert dev.PCMVolumeTransformer.__module__ == 'discord_ext_voice._native'
    # Nothing in the package inherits from discord.py; only the method contract is shared, which
    # is what keeps a foreign source usable.
    for name in ('AudioSource', 'PCMAudio', 'FFmpegPCMAudio', 'FFmpegOpusAudio'):
        assert not issubclass(getattr(dev, name), getattr(discord.player, name))


def test_frame_geometry_matches_discord_py():
    assert dev.SAMPLES_PER_FRAME == 960
    assert dev.FRAME_LENGTH == 20
    assert dev.FRAME_SIZE == 3840
    # The native constants agree with the geometry discord.py advertises.
    assert dev.SAMPLES_PER_FRAME == discord.opus.Encoder.SAMPLES_PER_FRAME
    assert dev.FRAME_SIZE == discord.opus.Encoder.FRAME_SIZE


def test_exception_types_line_up_with_discord():
    # The exceptions this library can raise at the discord.py boundary are discord.py's own.
    assert dev.ClientException is discord.ClientException
    assert dev.ConnectionClosed is discord.errors.ConnectionClosed

    # A native FFmpeg failure is catchable as our own error type; nothing inherits from
    # discord.py, so `except discord.ClientException` does not catch it.
    assert issubclass(dev.FFmpegSourceError, dev.FFmpegError)
    assert not issubclass(dev.FFmpegSourceError, discord.ClientException)


def test_playback_tuning_is_exposed():
    assert dev.DEFAULT_FEC is True
    assert dev.DEFAULT_EXPECTED_PACKET_LOSS == 0.15
    assert dev.VoiceClient.fec is True
    assert dev.VoiceClient.expected_packet_loss == 0.15
    # The native player's read-ahead window and no-data timeout are adjustable.
    assert callable(dev.set_read_ahead)
    assert callable(dev.set_read_stall_timeout)


def test_connection_flow_state_values():
    # The flow enum is ours, with discord.py's names and values so the numbers keep matching.
    assert [state.name for state in dev.ConnectionFlowState] == [
        'disconnected',
        'set_guild_voice_state',
        'got_voice_state_update',
        'got_voice_server_update',
        'got_both_voice_updates',
        'websocket_connected',
        'got_websocket_ready',
        'got_ip_discovery',
        'connected',
    ]
    assert [state.value for state in dev.ConnectionFlowState] == list(range(9))
    assert dev.ConnectionFlowState.connected.value == 8
    assert dev.ConnectionFlowState is not discord.voice_state.ConnectionFlowState
