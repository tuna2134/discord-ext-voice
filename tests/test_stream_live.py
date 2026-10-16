"""Diagnostic: play a live yt-dlp stream URL through the whole stack."""

from __future__ import annotations

import asyncio
import time

import pytest

import discord_ext_voice as dev
from test_voice_client import join, make_client

pytestmark = pytest.mark.asyncio

VIDEO = 'https://www.youtube.com/watch?v=Wl61TvVuXY4'


async def test_stream_a_live_url(gateway_factory):
    yt_dlp = pytest.importorskip('yt_dlp')

    opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True, 'noplaylist': True}
    with yt_dlp.YoutubeDL(opts) as ytdl:
        info = await asyncio.get_running_loop().run_in_executor(
            None, lambda: ytdl.extract_info(VIDEO, download=False)
        )
    url = info['url']
    print('protocol:', info.get('protocol'), 'ext:', info.get('ext'))

    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    errors = []
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    def after(error):
        errors.append(error)
        loop.call_soon_threadsafe(finished.set)

    try:
        source = dev.PCMVolumeTransformer(dev.FFmpegPCMAudio(url), 0.5)
        started = time.monotonic()
        voice.play(source, after=after)
        packets = await gateway.wait_for_rtp(count=25, timeout=30.0)
        print('packets:', len(packets), 'in', round(time.monotonic() - started, 2), 's')
        print('errors so far:', errors)
        assert voice.is_playing() is True
    finally:
        await voice.disconnect()
