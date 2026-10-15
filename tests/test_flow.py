"""The native connection flow and the Python view of it.

The transition table itself is unit-tested in Rust (`src/flow.rs`); what matters here is the
Python side of the boundary: that the state round-trips, that the waiting primitives behave, and
that every action the native flow can return has a name in Python.
"""

from __future__ import annotations

import asyncio

import pytest

import discord_ext_voice as dev
from discord_ext_voice import _native
from discord_ext_voice.voice_state import (
    CloseAction,
    VoiceServerAction,
    VoiceStateAction,
)
from test_voice_client import join, make_client

pytestmark = pytest.mark.asyncio


def flow_of(guild_id: int = 1, user_id: int = 2, channel_id: int = 3) -> _native.ConnectionFlow:
    """A flow with no connection behind it: enough for the policy and the phase."""
    return _native.VoiceConnection(guild_id, user_id, channel_id).flow


# ---------------------------------------------------------------------------- the phase


async def test_the_flow_state_round_trips_every_state():
    """The Python enum and the native phase are the same nine numbers."""
    flow = flow_of()
    for state in dev.ConnectionFlowState:
        flow.set_state(state.value)
        assert flow.state == state.value
        assert dev.ConnectionFlowState(flow.state) is state

    with pytest.raises(ValueError):
        flow.set_state(9)


async def test_set_state_returns_the_previous_state():
    flow = flow_of()
    assert flow.set_state(dev.ConnectionFlowState.connected.value) == 0
    assert flow.set_state(dev.ConnectionFlowState.connected.value) == 8
    assert flow.state == 8


async def test_the_python_state_property_is_the_native_phase(gateway_factory):
    """`state` is a view of the transport's phase, not a second copy of it."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    try:
        connection = voice._connection
        assert connection._flow.state == connection.state.value == 8
        assert voice.native.phase == 8

        connection.state = dev.ConnectionFlowState.disconnected
        assert connection._flow.state == 0
        # The transport follows the flow: it only accepts audio when the phase is connected.
        assert voice.native.phase == 0
    finally:
        await voice.disconnect()


# ---------------------------------------------------------------------------- waiting


async def test_wait_for_state_returns_when_the_phase_arrives():
    flow = flow_of()
    waiter = asyncio.ensure_future(flow.wait_for_state([8], 5.0))
    # The phase can arrive before or after the waiter registers: both must be caught.
    flow.set_state(8)
    assert await waiter is True


async def test_wait_for_state_times_out():
    flow = flow_of()
    assert await flow.wait_for_state([8], 0.05) is False


async def test_wait_for_state_rejects_an_unknown_state():
    flow = flow_of()
    with pytest.raises(ValueError):
        await flow.wait_for_state([9], 0.05)


async def test_wait_connected_times_out():
    """The blocking wait is what `VoiceClient.wait_until_connected` uses."""
    flow = flow_of()
    assert await asyncio.to_thread(flow.wait_connected, 0.05) is False


async def test_wait_connected_returns_once_connected(gateway_factory):
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = await join(client, channel)

    try:
        assert await asyncio.to_thread(voice._connection.wait, 5.0) is True
    finally:
        await voice.disconnect()


async def test_the_flow_wait_raises_asyncio_timeout(gateway_factory):
    """`_wait_for_state` keeps discord.py's `asyncio.TimeoutError` contract."""
    gateway = await gateway_factory()
    client, channel = await make_client(gateway)
    voice = dev.VoiceClient(client, channel)

    with pytest.raises(asyncio.TimeoutError):
        await voice._connection._wait_for_state(
            dev.ConnectionFlowState.connected, timeout=0.05
        )


# ---------------------------------------------------------------------------- actions


async def test_every_native_action_has_a_python_name():
    """A new action in Rust has to be named in Python before it can be dispatched on."""
    families = (
        (VoiceStateAction, ('VOICE_STATE_',)),
        (VoiceServerAction, ('VOICE_SERVER_',)),
        (CloseAction, ('CLOSE_',)),
    )
    native = _native.ConnectionFlow
    for enum, prefixes in families:
        declared = {
            value
            for name, value in vars(native).items()
            if isinstance(value, int) and name.startswith(prefixes)
        }
        assert declared == {int(member) for member in enum}, enum.__name__


async def test_the_close_code_policy_reaches_python():
    flow = flow_of()
    assert CloseAction(flow.close_action(1000, False, True)) is CloseAction.normal
    assert CloseAction(flow.close_action(4014, True, True)) is CloseAction.by_discord
    assert CloseAction(flow.close_action(4014, False, True)) is CloseAction.potential_reconnect
    assert CloseAction(flow.close_action(4021, False, True)) is CloseAction.rate_limited
    assert CloseAction(flow.close_action(4015, False, True)) is CloseAction.resume
    assert CloseAction(flow.close_action(4000, False, True)) is CloseAction.reconnect
    assert CloseAction(flow.close_action(4000, False, False)) is CloseAction.raise_

    flow.expecting_disconnect = True
    assert CloseAction(flow.close_action(1000, False, True)) is CloseAction.quiet


async def test_the_gateway_event_transitions_reach_python():
    flow = flow_of()

    flow.set_state(dev.ConnectionFlowState.set_guild_voice_state.value)
    assert (
        VoiceStateAction(flow.voice_state_update(3, 3)) is VoiceStateAction.ignore
    )
    assert flow.state == dev.ConnectionFlowState.got_voice_state_update.value
    assert flow.joined is True
    assert (
        VoiceStateAction(flow.voice_state_update(99, 3))
        is VoiceStateAction.move_during_handshake
    )

    flow.set_state(dev.ConnectionFlowState.set_guild_voice_state.value)
    assert (
        VoiceServerAction(flow.voice_server_update(True, True, False))
        is VoiceServerAction.ignore
    )
    assert flow.state == dev.ConnectionFlowState.got_voice_server_update.value

    flow.set_state(dev.ConnectionFlowState.connected.value)
    assert (
        VoiceServerAction(flow.voice_server_update(True, True, True))
        is VoiceServerAction.close_old_websocket
    )
    assert flow.state == dev.ConnectionFlowState.got_voice_server_update.value


async def test_a_disconnect_we_did_not_ask_for_is_reported(gateway_factory):
    flow = flow_of()
    flow.set_state(dev.ConnectionFlowState.connected.value)
    assert (
        VoiceStateAction(flow.voice_state_update(None, 3)) is VoiceStateAction.disconnect
    )

    flow.expecting_disconnect = True
    assert VoiceStateAction(flow.voice_state_update(None, None)) is VoiceStateAction.ignore
    assert flow.expecting_disconnect is False


async def test_the_retry_policy_is_native():
    assert _native.ConnectionFlow.MAX_ATTEMPTS == 5
    assert [_native.ConnectionFlow.retry_wait(i) for i in range(5)] == [1.0, 3.0, 5.0, 7.0, 9.0]
