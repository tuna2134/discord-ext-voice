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


def test_voice_client_is_a_discord_voice_protocol():
    assert issubclass(dev.VoiceClient, discord_voice_client.VoiceProtocol)
    assert issubclass(dev.VoiceClient, discord.VoiceProtocol)


def test_client_exception_is_also_a_discord_client_exception():
    assert issubclass(dev.ClientException, discord.errors.ClientException)
    assert issubclass(dev.ClientException, dev.VoiceError)


def test_voice_client_constructor_signature_matches_discord():
    ours = inspect.signature(dev.VoiceClient.__init__)
    theirs = inspect.signature(discord_voice_client.VoiceClient.__init__)
    assert list(ours.parameters)[1:] == list(theirs.parameters)[1:] == ['client', 'channel']


def test_connect_signature_is_accepted_by_discord():
    ours = inspect.signature(dev.VoiceClient.connect)
    theirs = inspect.signature(discord_voice_client.VoiceProtocol.connect)
    assert set(ours.parameters) == set(theirs.parameters)


def test_discord_speaking_state_is_used():
    assert dev.gateway.SpeakingState is discord.SpeakingState
    assert int(dev.gateway.SpeakingState.voice) == 1
    assert int(dev.gateway.SpeakingState.priority) == 4


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
