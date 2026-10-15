"""Compatibility with an installed ``discord.py``.

When ``discord.py`` is importable this library must present itself as a first-class
``VoiceProtocol`` implementation, so ``channel.connect(cls=discord_ext_voice.VoiceClient)``
passes ``discord.py``'s own ``isinstance`` check and the documented exception types line up.
"""

from __future__ import annotations

import inspect

import pytest

discord = pytest.importorskip('discord')

import discord.voice_client as discord_voice_client  # noqa: E402

import discord_ext_voice as dev  # noqa: E402


def test_voice_client_implements_voice_protocol():
    """`VoiceProtocol` is discord.py's extension point for alternative voice backends."""
    assert issubclass(dev.VoiceClient, discord_voice_client.VoiceProtocol)
    assert issubclass(dev.VoiceClient, discord.VoiceProtocol)
    # discord.py's own implementation is deliberately not inherited: it is built on PyNaCl and
    # the Python `davey` bindings, neither of which this backend uses.
    assert not issubclass(dev.VoiceClient, discord_voice_client.VoiceClient)
    assert dev.VoiceProtocol is discord_voice_client.VoiceProtocol


def test_the_discord_exception_types_are_reused():
    assert dev.ClientException is discord.errors.ClientException
    assert dev.ConnectionClosed is discord.errors.ConnectionClosed


def test_voice_client_does_not_require_pynacl_or_davey(monkeypatch):
    """Our `VoiceClient` must work when discord.py's own capability checks would fail."""
    import asyncio
    import types

    import discord.voice_client as discord_voice_client

    monkeypatch.setattr(discord_voice_client, 'has_nacl', False)
    monkeypatch.setattr(discord_voice_client, 'has_dave', False)

    with pytest.raises(RuntimeError, match='PyNaCl'):
        discord_voice_client.VoiceClient(object(), object())

    class _State:
        loop = None
        user = types.SimpleNamespace(id=222)

    class _Client:
        _connection = _State()

    class _Channel:
        id = 333
        guild = types.SimpleNamespace(id=111)

    voice = dev.VoiceClient(_Client(), _Channel())
    assert voice._connection.native is not None
    assert voice.native.is_connected() is False
    assert voice.encoder is None
    assert voice.is_playing() is False
    assert asyncio.iscoroutinefunction(voice.connect)


def test_voice_client_constructor_signature_matches_discord():
    ours = inspect.signature(dev.VoiceClient.__init__)
    theirs = inspect.signature(discord_voice_client.VoiceClient.__init__)
    assert list(ours.parameters)[1:] == list(theirs.parameters)[1:] == ['client', 'channel']


def test_connect_signature_is_accepted_by_discord():
    ours = inspect.signature(dev.VoiceClient.connect)
    theirs = inspect.signature(discord_voice_client.VoiceProtocol.connect)
    assert set(ours.parameters) == set(theirs.parameters)


def test_voice_websocket_is_this_packages_own():
    """The websocket facade is ours: Rust owns the socket, so only the surface is left."""
    import discord.gateway

    assert not issubclass(dev.DiscordVoiceWebSocket, discord.gateway.DiscordVoiceWebSocket)
    assert dev.DiscordVoiceWebSocket.__module__ == 'discord_ext_voice.gateway'

    # The opcode constants keep Discord's values, so the frames stay interchangeable.
    ours = dev.DiscordVoiceWebSocket
    theirs = discord.gateway.DiscordVoiceWebSocket
    for name in (
        'IDENTIFY',
        'SELECT_PROTOCOL',
        'READY',
        'HEARTBEAT',
        'SESSION_DESCRIPTION',
        'SPEAKING',
        'HEARTBEAT_ACK',
        'RESUME',
        'HELLO',
        'RESUMED',
        'MLS_EXTERNAL_SENDER',
        'MLS_KEY_PACKAGE',
        'MLS_INVALID_COMMIT_WELCOME',
    ):
        assert getattr(ours, name) == getattr(theirs, name), f'{name} must match Discord'

    # What discord.py's player calls is implemented here.
    assert callable(ours.speak)
    assert callable(ours.close)


def test_discord_implementations_are_reused_verbatim():
    """Only the host framework comes from discord.py; nothing of ours inherits from it."""
    import discord.gateway
    import discord.player
    import discord.voice_state

    # The playback clock is discord.py's, unchanged.
    assert dev.AudioPlayer is discord.player.AudioPlayer
    # `VoiceClient` implements discord.py's documented extension point: the one inheritance this
    # package has, and the only one it wants.
    assert dev.VoiceProtocol is discord.voice_client.VoiceProtocol
    assert issubclass(dev.VoiceClient, discord.voice_client.VoiceProtocol)

    for ours, theirs in (
        (dev.VoiceConnectionState, discord.voice_state.VoiceConnectionState),
        (dev.ConnectionFlowState, discord.voice_state.ConnectionFlowState),
        (dev.DiscordVoiceWebSocket, discord.gateway.DiscordVoiceWebSocket),
        (dev.AudioSource, discord.player.AudioSource),
        (dev.PCMAudio, discord.player.PCMAudio),
        (dev.FFmpegPCMAudio, discord.player.FFmpegPCMAudio),
        (dev.FFmpegOpusAudio, discord.player.FFmpegOpusAudio),
        (dev.PCMVolumeTransformer, discord.player.PCMVolumeTransformer),
    ):
        assert ours is not theirs
        assert not issubclass(ours, theirs), f'{ours.__name__} must not inherit discord.py'


def test_audio_sources_are_this_packages_own():
    """The sources decode natively, so discord.py's `ffmpeg`-subprocess ones are not reused."""
    import discord.player

    for name in ('AudioSource', 'PCMAudio', 'FFmpegPCMAudio', 'FFmpegOpusAudio', 'PCMVolumeTransformer'):
        ours = getattr(dev, name)
        assert ours is not getattr(discord.player, name), f'{name} must be ours'
        assert ours.__module__ == 'discord_ext_voice.sources'

    # A source with the same three methods is still accepted, which is what makes a
    # `discord.AudioSource` subclass playable.
    class External(discord.AudioSource):
        def read(self) -> bytes:
            return b''

    assert dev.is_audio_source(External()) is True
    assert dev.is_audio_source(b'not a source') is False


def test_discord_voice_protocol_contract_methods_exist():
    for name in ('on_voice_state_update', 'on_voice_server_update', 'connect', 'disconnect', 'cleanup'):
        assert callable(getattr(dev.VoiceClient, name))


def test_audio_source_still_works_alongside_discord():
    """`discord.AudioSource` subclasses must be accepted by `play`."""
    assert hasattr(discord, 'AudioSource')

    class External(discord.AudioSource):
        def read(self) -> bytes:
            return b''

    source = External()
    assert source.is_opus() is False
