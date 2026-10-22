"""The native playback clock, exercised through the Python wrapper.

:class:`discord_ext_voice.AudioPlayer` is a thin wrapper over the Rust player, which owns the
thread, the 20ms clock, pause/resume, the silence frames and the ``after`` callback. These tests
pin that behaviour; the broader end-to-end flow lives in ``test_voice_client.py``.
"""

from __future__ import annotations

import asyncio
import io
import time
from typing import Any, Callable

import pytest

import discord_ext_voice as dev
from test_voice_client import KEY, join, make_client

pytestmark = pytest.mark.asyncio

#: A frame of PCM that is loud enough to never be encoded down to a silence packet.
LOUD_FRAME = bytes(range(256)) * 15


class CountingSource(dev.PCMAudio):
    """A native source that hands out whole frames and records what the player did to it."""

    def __init__(self, frames: int = 3) -> None:
        super().__init__(io.BytesIO(b''))
        self.frames = frames
        self.reads = 0
        self.cleanups = 0
        self.failure: Exception | None = None

    def read(self) -> bytes:
        if self.failure is not None:
            raise self.failure
        self.reads += 1
        if self.reads > self.frames:
            return b''
        return LOUD_FRAME

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.cleanups += 1


def payload_overhead(mode: str) -> int:
    """The RTP header, nonce and authentication tag around an audio payload."""
    return 12 + (4 if 'rtpsize' in mode else 0) + 16


def silence_packet_length(mode: str) -> int:
    """How long an RTP packet carrying only the Opus silence frame is."""
    return payload_overhead(mode) + len(dev.OPUS_SILENCE)


def speaking_states(gateway: Any) -> list[int]:
    return [frame['d']['speaking'] for frame in gateway.received_json if frame.get('op') == 5]


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=timeout)


def completion_recorder() -> tuple[Callable[[Any], None], asyncio.Event, list[Any]]:
    """An `after` callback, the event it sets and the errors it collected."""
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    errors: list[Any] = []

    def after(error: Any) -> None:
        errors.append(error)
        loop.call_soon_threadsafe(done.set)

    return after, done, errors


async def test_after_runs_and_the_source_is_cleaned_up(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    source = CountingSource(frames=5)
    after, done, errors = completion_recorder()

    try:
        voice.play(source, after=after)
        assert voice.is_playing() is True

        await asyncio.wait_for(done.wait(), timeout=10.0)
        assert errors == [None]
        assert source.reads == 6  # five frames, then the empty read that ends playback
        assert voice.is_playing() is False
        assert voice.is_paused() is False
        # `after` runs first and `cleanup` straight after it, as in discord.py's player.
        await wait_until(lambda: source.cleanups == 1)
        # Every frame reached the wire, including the five silence ones sent on the way out.
        await wait_until(lambda: len(gateway.rtp_packets) >= 10)
    finally:
        await voice.disconnect()


async def test_a_failing_source_is_reported_to_after(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    source = CountingSource()
    source.failure = dev.FFmpegSourceError('the decoder gave up')
    after, done, errors = completion_recorder()

    try:
        voice.play(source, after=after)
        await asyncio.wait_for(done.wait(), timeout=10.0)

        assert len(errors) == 1
        assert isinstance(errors[0], dev.FFmpegSourceError)
        assert 'the decoder gave up' in str(errors[0])
        assert voice.is_playing() is False
        await wait_until(lambda: source.cleanups == 1)
        # `discord.py`'s player announces the end of the voice state when it fails as well.
        await wait_until(lambda: speaking_states(gateway)[-1] == 0)
    finally:
        await voice.disconnect()


class StallingSource(dev.PCMAudio):
    """A native source whose first read never returns in time, like a dead HTTP stream."""

    def __init__(self, delay: float = 1.0) -> None:
        super().__init__(io.BytesIO(b''))
        self.delay = delay
        self.cleanups = 0

    def read(self) -> bytes:
        time.sleep(self.delay)
        return b''

    def cleanup(self) -> None:
        self.cleanups += 1


async def test_a_stalled_source_is_reported_to_after(gateway_factory):
    """The player must not wait forever for a source that stopped producing frames."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    source = StallingSource(delay=1.0)
    after, done, errors = completion_recorder()
    dev.set_read_stall_timeout(0.2)
    try:
        voice.play(source, after=after)
        await asyncio.wait_for(done.wait(), timeout=5.0)
        assert len(errors) == 1
        assert isinstance(errors[0], dev.FFmpegSourceError)
        assert 'stalled' in str(errors[0])
    finally:
        dev.set_read_stall_timeout(30.0)
        await voice.disconnect()


async def test_frames_are_paced_at_the_frame_delay(gateway_factory):
    """The player must not dump a source as fast as it can read it."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    frames = 25  # 0.5s of audio at 20ms per frame
    source = CountingSource(frames=frames)
    after, done, errors = completion_recorder()

    try:
        started = time.monotonic()
        voice.play(source, after=after)
        await asyncio.wait_for(done.wait(), timeout=10.0)
        elapsed = time.monotonic() - started

        assert errors == [None]
        # Give the scheduler plenty of slack, but a player that ignored its clock would finish
        # this in well under 0.1s.
        assert elapsed >= (frames - 1) * dev.AudioPlayer.DELAY * 0.8, elapsed
        assert elapsed < 10.0, elapsed
    finally:
        await voice.disconnect()


async def test_pause_feeds_silence_and_announces_state(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    silence = silence_packet_length(voice.mode or '')
    source = CountingSource(frames=500)  # far more than the test will play

    try:
        voice.play(source)
        await gateway.wait_for_rtp(count=1, timeout=10.0)

        voice.pause()
        assert voice.is_paused() is True
        assert voice.is_playing() is False

        await wait_until(
            lambda: sum(1 for packet in gateway.rtp_packets if len(packet) == silence) >= 5
        )
        await wait_until(lambda: speaking_states(gateway)[-1] == 0)

        reads_while_paused = source.reads
        await asyncio.sleep(0.1)
        assert source.reads == reads_while_paused, 'a paused player must not consume the source'

        voice.resume()
        assert voice.is_playing() is True
        await wait_until(lambda: speaking_states(gateway)[-1] == 1)
        await wait_until(lambda: source.reads > reads_while_paused)

        voice.stop()
        assert voice.is_playing() is False
        await wait_until(lambda: speaking_states(gateway)[-1] == 0)
    finally:
        await voice.disconnect()


async def test_set_source_switches_what_is_read(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    first = CountingSource(frames=1000)
    second = CountingSource(frames=2)
    after, done, errors = completion_recorder()

    try:
        voice.play(first, after=after)
        await gateway.wait_for_rtp(count=1, timeout=10.0)

        voice.source = second
        assert voice.source is second

        await asyncio.wait_for(done.wait(), timeout=10.0)
        assert errors == [None]
        assert second.reads == 3
        assert first.reads >= 1
        # The swapped-in source is the one that gets cleaned up, like discord.py's player.
        await wait_until(lambda: second.cleanups == 1)
    finally:
        await voice.disconnect()


async def test_stop_is_reported_as_not_playing(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    player = None
    try:
        voice.play(CountingSource(frames=1000))
        player = voice._player
        assert player is not None
        assert player.is_alive() is True

        voice.stop()
        assert player.is_playing() is False
        assert player.is_paused() is False
        # The thread notices the stop and stops nothing (`disconnect` waits for it).
        player.join(timeout=5.0)
        assert player.is_alive() is False
        assert voice.source is None
        assert client._connection._get_voice_client(KEY) is voice
    finally:
        await voice.disconnect()


async def test_an_opus_source_is_passed_through(gateway_factory, opus_file):
    """An Opus source must not be encoded twice: the packets go to the wire untouched."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    # What the file's packets look like, read straight from the native source.
    expected: list[int] = []
    reader = dev.FFmpegOpusAudio(opus_file)
    assert reader.is_opus() is True
    while True:
        packet = reader.read()
        if not packet:
            break
        expected.append(len(packet))
    assert len(expected) >= 10  # 0.5s of 20ms packets

    source = dev.FFmpegOpusAudio(opus_file)
    after, done, errors = completion_recorder()

    try:
        voice.play(source, after=after)

        await asyncio.wait_for(done.wait(), timeout=10.0)
        assert errors == [None]

        overhead = payload_overhead(voice.mode or '')
        payloads = [len(packet) - overhead for packet in gateway.rtp_packets]
        # The trailing silence frames are the player's, not the file's.
        while payloads and payloads[-1] == len(dev.OPUS_SILENCE):
            payloads.pop()
        assert payloads == expected
    finally:
        await voice.disconnect()
