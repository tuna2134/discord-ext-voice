"""Scratch: does disconnect work while the player is still running?"""
from __future__ import annotations

import asyncio
import io
import sys

sys.path.insert(0, 'tests')

import pytest

import discord_ext_voice as dev
from fake_gateway import FakeVoiceGateway
from test_voice_client import KEY, join, make_client

pytestmark = pytest.mark.asyncio


async def test_disconnect_while_playing(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    # 10 seconds of audio: playback is definitely still running.
    voice.play(dev.PCMAudio(io.BytesIO(b'\x00' * (dev.FRAME_SIZE * 500))))
    assert voice.is_playing() is True

    await asyncio.wait_for(voice.disconnect(), timeout=10.0)
    assert voice.is_connected() is False
    assert client._connection._get_voice_client(KEY) is None
    await asyncio.sleep(0.2)   # let the player thread notice
    assert voice.is_playing() is False
