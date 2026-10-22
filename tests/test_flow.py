"""The native connection flow and the Python view of it.

The transition table itself is unit-tested in Rust (`src/flow.rs`); what matters here is the
Python side of the boundary: that the state round-trips, that the waiting primitives behave, and
that every action the native flow can return has a name in the native enums.
"""

from __future__ import annotations

import asyncio
import enum

import pytest

import discord_ext_voice as dev
from discord_ext_voice import _native
from test_voice_client import join, make_client

pytestmark = pytest.mark.asyncio


def connection(guild_id: int = 1, user_id: int = 2, channel_id: int = 3) -> _native.VoiceConnection:
    """A connection with no transport behind it: enough for the policy and the phase."""
    return _native.VoiceConnection(guild_id, user_id, channel_id)


# ---------------------------------------------------------------------------- the phase


async def test_the_flow_state_round_trips_every_state():
    """The Python enum and the native phase are the same nine numbers."""
    conn = connection()
    for state in dev.ConnectionFlowState:
        conn.set_phase(state.value)
        assert conn.phase == state.value
        assert conn.state is state
        assert dev.ConnectionFlowState(conn.state) is state

    with pytest.raises(ValueError):
        conn.set_phase(9)


async def test_set_phase_returns_the_previous_state():
    conn = connection()
    assert conn.set_phase(dev.ConnectionFlowState.connected.value) == 0
    assert conn.set_phase(dev.ConnectionFlowState.connected.value) == 8
    assert conn.phase == 8


async def test_the_python_state_property_is_the_native_phase(gateway_factory):
    """`state` is a view of the transport's phase, not a second copy of it."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    try:
        connection = voice._connection
        assert connection.phase == connection.state.value == 8
        assert voice.native.phase == 8

        connection.state = dev.ConnectionFlowState.disconnected
        assert connection.phase == 0
        # The transport follows the flow: it only accepts audio when the phase is connected.
        assert voice.native.phase == 0
    finally:
        await voice.disconnect()


# ---------------------------------------------------------------------------- waiting


async def test_wait_for_state_returns_when_the_phase_arrives():
    conn = connection()
    waiter = asyncio.ensure_future(conn.wait_for_state([8], 5.0))
    # The phase can arrive before or after the waiter registers: both must be caught.
    conn.set_phase(8)
    assert await waiter is True


async def test_wait_for_state_times_out():
    conn = connection()
    assert await conn.wait_for_state([8], 0.05) is False


async def test_wait_for_state_rejects_an_unknown_state():
    conn = connection()
    with pytest.raises(ValueError):
        await conn.wait_for_state([9], 0.05)


async def test_wait_connected_times_out():
    """The blocking wait is what `VoiceClient.wait_until_connected` uses."""
    conn = connection()
    assert await asyncio.to_thread(conn.wait_connected, 0.05) is False


async def test_wait_connected_returns_once_connected(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    try:
        assert await asyncio.to_thread(voice._connection.wait_connected, 5.0) is True
    finally:
        await voice.disconnect()


async def test_the_wait_raises_asyncio_timeout(gateway_factory):
    """`VoiceClient._wait_for_state` keeps discord.py's `asyncio.TimeoutError` contract."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = dev.VoiceClient(client, channel)

    with pytest.raises(asyncio.TimeoutError):
        await voice._wait_for_state(dev.ConnectionFlowState.connected, timeout=0.05)


# ---------------------------------------------------------------------------- actions


async def test_every_action_is_a_named_native_enum():
    """A new action in Rust has to come back as a member of a Python-visible enum."""
    for enum_class in (dev.VoiceStateAction, dev.VoiceServerAction, dev.CloseAction):
        assert issubclass(enum_class, enum.IntEnum), enum_class.__name__
        assert enum_class.__module__ == 'discord_ext_voice'


async def test_the_close_code_policy_reaches_python():
    conn = connection()
    assert dev.CloseAction(conn.close_action(1000, True)) is dev.CloseAction.normal
    assert dev.CloseAction(conn.close_action(4014, True)) is dev.CloseAction.potential_reconnect
    assert dev.CloseAction(conn.close_action(4021, True)) is dev.CloseAction.rate_limited
    assert dev.CloseAction(conn.close_action(4015, True)) is dev.CloseAction.resume
    assert dev.CloseAction(conn.close_action(4000, True)) is dev.CloseAction.reconnect
    assert dev.CloseAction(conn.close_action(4000, False)) is dev.CloseAction.raise_

    # A gateway-confirmed disconnect means Discord removed us, not that we may have been moved.
    conn.disconnected = True
    assert dev.CloseAction(conn.close_action(4014, True)) is dev.CloseAction.by_discord

    conn.expecting_disconnect = True
    assert dev.CloseAction(conn.close_action(1000, True)) is dev.CloseAction.quiet


async def test_the_gateway_event_transitions_reach_python():
    conn = connection()

    conn.set_phase(dev.ConnectionFlowState.set_guild_voice_state.value)
    assert conn.voice_state_update(3, 3) is dev.VoiceStateAction.ignore
    assert conn.phase == dev.ConnectionFlowState.got_voice_state_update.value
    assert conn.joined is True
    assert (
        conn.voice_state_update(99, 3) is dev.VoiceStateAction.move_during_handshake
    )

    conn.set_phase(dev.ConnectionFlowState.set_guild_voice_state.value)
    assert conn.voice_server_update(True, True, False) is dev.VoiceServerAction.ignore
    assert conn.phase == dev.ConnectionFlowState.got_voice_server_update.value

    conn.set_phase(dev.ConnectionFlowState.connected.value)
    assert (
        conn.voice_server_update(True, True, True)
        is dev.VoiceServerAction.close_old_websocket
    )
    assert conn.phase == dev.ConnectionFlowState.got_voice_server_update.value


async def test_a_disconnect_we_did_not_ask_for_is_reported():
    conn = connection()
    conn.set_phase(dev.ConnectionFlowState.connected.value)
    assert conn.voice_state_update(None, 3) is dev.VoiceStateAction.disconnect

    conn.expecting_disconnect = True
    assert conn.voice_state_update(None, None) is dev.VoiceStateAction.ignore
    assert conn.expecting_disconnect is False


async def test_the_retry_policy_is_native():
    assert _native.MAX_ATTEMPTS == 5
    assert [_native.retry_wait(i) for i in range(5)] == [1.0, 3.0, 5.0, 7.0, 9.0]
