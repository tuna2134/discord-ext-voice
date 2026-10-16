"""A live check of the path `examples/ytdl.py`'s ``!stream`` uses.

Nothing here runs by default: it needs the network, YouTube and the ``yt-dlp`` package, and the
URL has to be given explicitly, so it is opt-in::

    DISCORD_EXT_VOICE_LIVE_URL='https://www.youtube.com/watch?v=...' pytest tests/test_stream_live.py

What it proves is the part a fake server cannot: that the extractor's own headers are what make
a CDN URL playable, through the example's `from_url(..., stream=True)` and this package's URL
reader.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import time

import pytest

import discord_ext_voice as dev
from test_voice_client import join, make_client

pytestmark = pytest.mark.asyncio

URL = os.environ.get('DISCORD_EXT_VOICE_LIVE_URL')

pytest.importorskip('yt_dlp')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason='set DISCORD_EXT_VOICE_LIVE_URL to run this'),
]

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / 'examples'


def load_ytdl_example():
    """The example's `YTDLSource`, so the test drives exactly what `!stream` drives."""
    spec = importlib.util.spec_from_file_location('example_ytdl', EXAMPLES / 'ytdl.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_live_stream_url_plays(gateway_factory):
    example = load_ytdl_example()

    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    loop = asyncio.get_running_loop()
    errors: list[object] = []
    finished = asyncio.Event()

    def after(error):
        errors.append(error)
        loop.call_soon_threadsafe(finished.set)

    try:
        player = await example.YTDLSource.from_url(URL, stream=True)
        assert player.title, 'the extractor reported no title'

        started = time.monotonic()
        voice.play(player, after=after)
        packets = await gateway.wait_for_rtp(count=25, timeout=30.0)
        elapsed = time.monotonic() - started

        assert len(packets) >= 25
        # 25 frames of 20ms are 0.5s of audio; the reader must keep up with that.
        assert elapsed < 10.0, elapsed
        assert errors == []
        assert voice.is_playing() is True

        voice.stop()
        await asyncio.wait_for(finished.wait(), timeout=10.0)
    finally:
        await voice.disconnect()
