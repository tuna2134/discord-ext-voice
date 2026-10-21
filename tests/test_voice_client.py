"""End-to-end tests of the Python ``VoiceClient`` layer against the fake gateway.

A minimal stand-in for ``discord.py``'s client objects is used so the whole flow —
``channel.connect(cls=VoiceClient)`` -> voice state dispatch -> native handshake ->
``play()`` -> RTP on the wire -> ``disconnect()`` — is exercised.
"""

from __future__ import annotations

import asyncio
import io
import types
from typing import Any

import pytest

import discord_ext_voice as dev
from fake_gateway import SSRC, FakeVoiceGateway

pytestmark = pytest.mark.asyncio

GUILD_ID = 111
USER_ID = 222
CHANNEL_ID = 333
# discord.py keys voice clients by guild id.
KEY = GUILD_ID


class FakeConnectionState:
    """Stands in for ``discord.state.ConnectionState``."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.user = types.SimpleNamespace(id=USER_ID, bot=True)
        self.voice_clients: dict[Any, Any] = {}

    def _get_voice_client(self, key: Any) -> Any:
        return self.voice_clients.get(key)

    def _add_voice_client(self, key: Any, voice: Any) -> None:
        self.voice_clients[key] = voice

    def _remove_voice_client(self, key: Any) -> None:
        self.voice_clients.pop(key, None)


class FakeGuild:
    def __init__(self, client: 'FakeClient') -> None:
        self.id = GUILD_ID
        self._client = client
        self.me = types.SimpleNamespace(voice=None)
        self.channels: dict[int, 'FakeChannel'] = {}
        self.voice_state_calls: list[Any] = []

    def get_channel(self, channel_id: int) -> Any:
        return self.channels.get(channel_id)

    async def change_voice_state(self, *, channel: Any, self_deaf: bool = False, self_mute: bool = False) -> None:
        """Pretend to be the main gateway: dispatch the updates in the background."""
        self.voice_state_calls.append((channel, self_deaf, self_mute))
        voice = self._client._connection._get_voice_client(KEY)
        endpoint = self._client.endpoint

        async def dispatch() -> None:
            # A real gateway delivers these as separate events, shortly afterwards.
            await asyncio.sleep(0)
            if voice is None:
                return
            if channel is None:
                await voice.on_voice_state_update({'channel_id': None})
                return
            # Server update first: discord.py's reconnect path (close code 4015) only reaches
            # `ConnectionFlowState.got_both_voice_updates` in that order.
            await voice.on_voice_server_update(
                {'token': 'voice-token', 'guild_id': str(GUILD_ID), 'endpoint': endpoint}
            )
            await voice.on_voice_state_update(
                {'channel_id': str(channel.id), 'session_id': 'session-1'}
            )

        self._dispatch = asyncio.get_running_loop().create_task(dispatch())


class FakeChannel:
    def __init__(self, guild: FakeGuild, channel_id: int = CHANNEL_ID) -> None:
        self.guild = guild
        self.id = channel_id

    def _get_voice_client_key(self) -> tuple[int, str]:
        # Same contract as `discord.abc.Connectable._get_voice_client_key`.
        return GUILD_ID, 'guild_id'


class FakeClient:
    def __init__(self) -> None:
        self._connection = FakeConnectionState()
        self.endpoint: str | None = None
        # `discord.py` schedules coroutines on this loop; the native transport does not use it.
        self.loop = asyncio.get_running_loop()


async def make_client(gateway: FakeVoiceGateway) -> tuple[FakeClient, FakeChannel]:
    client = FakeClient()
    client.endpoint = gateway.endpoint
    guild = FakeGuild(client)
    channel = FakeChannel(guild)
    guild.channels[channel.id] = channel
    return client, channel


async def join(client: FakeClient, channel: FakeChannel, **kwargs: Any) -> Any:
    """`channel.connect(cls=dev.VoiceClient)` without needing real discord.py channel plumbing."""
    voice = dev.VoiceClient(client, channel)
    client._connection._add_voice_client(KEY, voice)
    await voice.connect(timeout=10.0, reconnect=True, **kwargs)
    return voice


# ---------------------------------------------------------------------------- connecting


async def test_connect_establishes_the_native_session(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)

    voice = await join(client, channel)
    try:
        assert voice.is_connected() is True
        assert voice.session_id == 'session-1'
        # discord.py strips only the `wss://` scheme; a plaintext gateway endpoint is kept
        # verbatim and the native transport understands the `ws://` prefix.
        assert voice.endpoint == gateway.endpoint
        assert voice.ssrc == SSRC
        assert voice.mode == 'aead_xchacha20_poly1305_rtpsize'
        # The credentials stay native: the handshake and the RTP encryption use them there.
        assert not hasattr(voice, 'token')
        assert not hasattr(voice, 'secret_key')
        assert len(voice.native.secret_key) == 32
        assert voice.guild.id == GUILD_ID
        assert voice.user.id == USER_ID
        assert voice.timeout == 10.0
    finally:
        await voice.disconnect()


async def test_connect_asks_the_guild_to_change_voice_state(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)

    voice = await join(client, channel, self_deaf=True, self_mute=True)
    try:
        assert channel.guild.voice_state_calls[0] == (channel, True, True)
    finally:
        await voice.disconnect()


async def test_native_connection_is_exposed(gateway_factory):
    """The transport lives in Rust; Python only holds a handle plus discord.py's state."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)
    try:
        assert voice.native is voice._connection.native
        assert voice.native.is_connected() is True
        assert voice._connection.supported_modes == dev.VoiceClient.supported_modes
        assert voice._connection.max_dave_protocol_version == dev.DAVE_PROTOCOL_VERSION
    finally:
        await voice.disconnect()


async def test_handshake_close_is_translated_to_discord_connection_closed(gateway_factory):
    """Regression: a 4006 used to escape as an ``AttributeError`` instead of a retryable error.

    ``discord.py`` retries a connect only for :class:`discord.ConnectionClosed`, so the code
    and the reason have to survive the trip through the native connection.
    """
    gateway = await gateway_factory(reject_identifies=1)
    client, channel = await make_client(gateway)

    voice = dev.VoiceClient(client, channel)
    client._connection._add_voice_client(KEY, voice)

    try:
        with pytest.raises(dev.ConnectionClosed) as info:
            await voice.connect(timeout=10.0, reconnect=False)

        assert info.value.code == 4006
        assert voice._connection.native.close_code == 4006
        assert voice.is_connected() is False
    finally:
        await voice.disconnect()


async def test_connect_times_out_waiting_for_the_gateway(gateway_factory):
    """A gateway that never sends the voice updates must time out, not hang.

    This is the native flow wait (`ConnectionFlow.wait_for_state`): the timeout has to reach
    `_wrap_connect` as an `asyncio.TimeoutError`, and the cancelled wait must not leave the flow
    wedged.
    """
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)

    voice = dev.VoiceClient(client, channel)
    client._connection._add_voice_client(KEY, voice)

    async def never(*args, **kwargs):
        return None

    # The fake main gateway dispatches through these, so nothing ever reaches the flow.
    voice.on_voice_server_update = never
    voice.on_voice_state_update = never

    try:
        with pytest.raises(asyncio.TimeoutError):
            await voice.connect(timeout=0.2, reconnect=False)
        assert voice.is_connected() is False
        assert voice._connection.state is dev.ConnectionFlowState.disconnected
    finally:
        await voice.disconnect()


async def test_connect_retries_after_a_rejected_session(gateway_factory):
    """A 4006 is recoverable: the retry gets a fresh session id and the connection comes up."""
    gateway = await gateway_factory(reject_identifies=1)
    client, channel = await make_client(gateway)

    voice = await join(client, channel)
    try:
        assert voice.is_connected() is True
        # Two IDENTIFYs: the rejected one and the one that completed the handshake.
        identifies = [frame for frame in gateway.received_json if frame.get('op') == 0]
        assert len(identifies) == 2
        assert voice.native.close_code is None
    finally:
        await voice.disconnect()


# ---------------------------------------------------------------------------- playback


async def test_play_streams_pcm_over_rtp(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    finished = asyncio.Event()
    errors: list[Any] = []

    def after(error: Any) -> None:
        errors.append(error)
        loop.call_soon_threadsafe(finished.set)

    loop = asyncio.get_running_loop()
    # 0.2s of stereo 48kHz silence.
    pcm = b'\x00' * (dev.FRAME_SIZE * 10)
    voice.play(dev.PCMAudio(io.BytesIO(pcm)), after=after)

    try:
        assert voice.is_playing() is True
        assert voice.is_paused() is False
        assert voice.source is not None

        packets = await gateway.wait_for_rtp(count=3, timeout=10.0)
        assert len(packets) >= 3
        assert packets[0][0] == 0x80  # RTP version 2

        # Discord drops audio from an SSRC it has not been told is speaking, so the player's
        # `speaking: 1` has to arrive as well as the transport's initial `speaking: 0`.
        speaks = [frame for frame in gateway.received_json if frame.get('op') == 5]
        assert [frame['d']['speaking'] for frame in speaks] == [0, 1], speaks

        await asyncio.wait_for(finished.wait(), timeout=10.0)
        assert errors == [None]
        assert voice.is_playing() is False
    finally:
        await voice.disconnect()


async def test_pause_and_resume(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    try:
        pcm = b'\x00' * (dev.FRAME_SIZE * 100)
        voice.play(dev.PCMAudio(io.BytesIO(pcm)))
        await gateway.wait_for_rtp(count=1, timeout=10.0)

        voice.pause()
        assert voice.is_paused() is True
        assert voice.is_playing() is False

        voice.resume()
        assert voice.is_playing() is True

        voice.stop()
        assert voice.is_playing() is False
        assert voice.source is None
    finally:
        await voice.disconnect()


async def test_send_audio_packet_encodes_pcm(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    try:
        voice.send_audio_packet(b'\x00' * dev.FRAME_SIZE, encode=True)
        packets = await gateway.wait_for_rtp(count=1, timeout=10.0)
        # One RTP packet whose payload is a small Opus frame, not the 3840 bytes of PCM.
        assert len(packets[0]) > 12
        assert len(packets[0]) < dev.FRAME_SIZE
    finally:
        await voice.disconnect()


async def test_send_audio_packet_passthrough(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    try:
        voice.send_audio_packet(dev.OPUS_SILENCE, encode=False)
        packets = await gateway.wait_for_rtp(count=1, timeout=10.0)
        assert len(packets[0]) == 12 + len(dev.OPUS_SILENCE) + 16 + 4
    finally:
        await voice.disconnect()


# ---------------------------------------------------------------------------- errors


async def test_play_requires_a_connection():
    client = FakeClient()
    guild = FakeGuild(client)
    channel = FakeChannel(guild)
    voice = dev.VoiceClient(client, channel)

    with pytest.raises(dev.ClientException):
        voice.play(dev.PCMAudio(io.BytesIO(b'')))


async def test_play_rejects_a_non_source(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)
    try:
        with pytest.raises(TypeError):
            voice.play('not an audio source')  # type: ignore[arg-type]
    finally:
        await voice.disconnect()


async def test_play_rejects_a_second_source(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)
    try:
        voice.play(dev.PCMAudio(io.BytesIO(b'\x00' * (dev.FRAME_SIZE * 200))))
        with pytest.raises(dev.ClientException):
            voice.play(dev.PCMAudio(io.BytesIO(b'')))
    finally:
        await voice.disconnect()


async def test_source_setter_swaps_the_stream(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)
    try:
        first = dev.PCMAudio(io.BytesIO(b'\x00' * (dev.FRAME_SIZE * 200)))
        voice.play(first)
        second = dev.PCMAudio(io.BytesIO(b'\x00' * (dev.FRAME_SIZE * 200)))
        voice.source = second
        assert voice.source is second

        with pytest.raises(TypeError):
            voice.source = 'nope'  # type: ignore[assignment]
    finally:
        await voice.disconnect()


async def test_disconnect_while_playing(gateway_factory):
    """`!leave` during playback must not wait for the source to end."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    # 10 seconds of audio: playback is definitely still running.
    voice.play(dev.PCMAudio(io.BytesIO(b'\x00' * (dev.FRAME_SIZE * 500))))
    assert voice.is_playing() is True

    await asyncio.wait_for(voice.disconnect(), timeout=10.0)
    assert voice.is_connected() is False
    assert client._connection._get_voice_client(KEY) is None

    await asyncio.sleep(0.2)  # let the player thread notice the stop
    assert voice.is_playing() is False


async def test_playback_survives_dave_traffic(gateway_factory):
    """Audio and DAVE frames at the same time must not wedge the connection.

    The native side buffers log records until it has released the connection lock. Emitting one
    while the lock is held needs the GIL, which the audio thread holds while it waits for that
    lock, so the two threads would deadlock and the whole client would stop responding.
    """
    gateway = await gateway_factory(dave_protocol_version=1)
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    voice.play(dev.PCMAudio(io.BytesIO(b'\x00' * (dev.FRAME_SIZE * 400))))
    try:
        for _ in range(50):
            # An unparseable frame: the native reader logs and carries on, which is exactly the
            # path that used to log with the connection lock held.
            await gateway.send_binary(31, b'\x00\x01\x02\x03')
            await asyncio.sleep(0.005)

        assert voice.is_playing() is True
        # The event loop has to still be responsive.
        await asyncio.wait_for(voice.disconnect(), timeout=10.0)
        assert voice.is_connected() is False
    finally:
        if voice.is_connected():
            await voice.disconnect()


# ---------------------------------------------------------------------------- disconnect


async def test_disconnect_cleans_up(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    await voice.disconnect()
    assert voice.is_connected() is False
    assert client._connection._get_voice_client(KEY) is None
    assert channel.guild.voice_state_calls[-1][0] is None


async def test_disconnect_leaves_while_the_flow_is_reconnecting(gateway_factory):
    """The poller sets the state to `disconnected` while it reconnects, but we are still in the
    channel: a plain `disconnect()` has to tell the gateway to remove us anyway."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    calls = len(channel.guild.voice_state_calls)
    # Simulate the reconnect window of `_poll_voice_ws`.
    voice._connection.state = dev.ConnectionFlowState.disconnected

    try:
        await voice.disconnect()
        assert any(call[0] is None for call in channel.guild.voice_state_calls[calls:]), (
            'the gateway was never told to remove us'
        )
        assert client._connection._get_voice_client(KEY) is None
    finally:
        if voice.is_connected():
            await voice.disconnect()


async def test_move_to_none_disconnects(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    await voice.move_to(None)
    assert voice.is_connected() is False


async def test_move_to_the_same_channel_is_a_noop(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)
    try:
        calls = len(channel.guild.voice_state_calls)
        await voice.move_to(channel, timeout=1.0)
        assert len(channel.guild.voice_state_calls) == calls
    finally:
        await voice.disconnect()


# ---------------------------------------------------------------------------- reconnect


async def test_resume_after_voice_server_crash(gateway_factory):
    """Close code 4015 must trigger a resume, like discord.py."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    try:
        await gateway.close(4015, 'voice server crashed')

        for _ in range(200):
            if voice.is_connected():
                break
            await asyncio.sleep(0.05)
        assert voice.is_connected() is True

        # The client must have sent RESUME on the new websocket connection.
        await gateway.wait_for_op(7, timeout=10.0)
    finally:
        await voice.disconnect()


async def test_voice_privacy_code_is_none_without_dave(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)
    try:
        assert voice.voice_privacy_code is None
        assert voice._connection.dave_protocol_version == 0
        assert voice._connection.dave_session is None
    finally:
        await voice.disconnect()


async def test_dave_is_negotiated_when_the_server_offers_it(gateway_factory):
    gateway = await gateway_factory(dave_protocol_version=1)
    client, channel = await make_client(gateway)
    voice = await join(client, channel)
    try:
        assert voice._connection.dave_protocol_version == 1
        assert voice._connection.dave_session is not None
        # The MLS group is not ready yet, so payloads stay in plaintext until it is.
        assert voice._connection.can_encrypt is False
        await gateway.wait_for_binary_op(26, timeout=10.0)
    finally:
        await voice.disconnect()
