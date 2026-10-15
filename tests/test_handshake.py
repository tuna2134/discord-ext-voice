"""End-to-end voice handshake tests against a fake Discord voice gateway.

These exercise the real native path: TCP -> websocket upgrade -> IDENTIFY -> READY ->
UDP IP discovery -> SELECT_PROTOCOL -> SESSION_DESCRIPTION -> heartbeat -> RTP encryption.
"""

from __future__ import annotations

import asyncio

import pytest

import discord_ext_voice as dev
from discord_ext_voice import _native

from fake_gateway import CHANNEL_ID, GUILD_ID, USER_ID, SSRC, SESSION_INVALID, open_connection

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------- handshake


async def test_handshake_completes(gateway_factory):
    gateway = await gateway_factory()
    conn = await open_connection(gateway)

    try:
        assert conn.is_connected() is True
        assert conn.ssrc == SSRC
        assert conn.mode == 'aead_xchacha20_poly1305_rtpsize'
        assert conn.ip == '127.0.0.1'
        assert conn.port > 0
        assert conn.voice_port == gateway._udp_port
        assert len(conn.secret_key) == 32
        assert conn.phase == 8
    finally:
        await conn.close()


async def test_identify_payload_matches_discord_protocol(gateway_factory):
    gateway = await gateway_factory()
    conn = await open_connection(gateway)

    try:
        identify = await gateway.wait_for_op(0)
        assert identify['d'] == {
            'server_id': str(GUILD_ID),
            'user_id': str(USER_ID),
            'session_id': 'session-abc',
            'token': 'voice-token',
            'max_dave_protocol_version': dev.DAVE_PROTOCOL_VERSION,
        }
    finally:
        await conn.close()


async def test_select_protocol_and_ip_discovery(gateway_factory):
    gateway = await gateway_factory()
    conn = await open_connection(gateway)

    try:
        select = await gateway.wait_for_op(1)
        assert select['d']['protocol'] == 'udp'
        assert select['d']['data']['address'] == '127.0.0.1'
        assert select['d']['data']['mode'] == 'aead_xchacha20_poly1305_rtpsize'
        assert select['d']['data']['port'] > 0
        assert gateway.ip_discovery_requests >= 1
    finally:
        await conn.close()


async def test_speaking_none_is_sent_after_the_session_description(gateway_factory):
    gateway = await gateway_factory()
    conn = await open_connection(gateway)

    try:
        speaking = await gateway.wait_for_op(5)
        assert speaking['d']['speaking'] == 0
        assert speaking['d']['ssrc'] == SSRC
    finally:
        await conn.close()


async def test_heartbeat_is_acknowledged_and_latency_is_measured(gateway_factory):
    gateway = await gateway_factory(heartbeat_interval_ms=200.0)
    conn = await open_connection(gateway)

    try:
        await gateway.wait_for_op(3, timeout=10.0)
        for _ in range(50):
            if conn.latency != float('inf'):
                break
            await asyncio.sleep(0.05)
        assert conn.latency != float('inf')
        assert 0.0 <= conn.latency < 5.0
        assert conn.average_latency != float('inf')
        assert conn.seq_ack >= 0
    finally:
        await conn.close()


async def test_resume_handshake(gateway_factory):
    gateway = await gateway_factory()
    conn = await open_connection(gateway)
    await conn.close()

    gateway2 = await gateway_factory()
    # A resume skips the UDP negotiation, so the flow only reaches got_voice_server_update.
    conn2 = await open_connection(gateway2, resume=True)
    try:
        assert conn2.is_connected()
        resume = await gateway2.wait_for_op(7)
        assert resume['d']['token'] == 'voice-token'
        assert 'seq_ack' in resume['d']
    finally:
        await conn2.close()


async def test_close_code_is_reported(gateway_factory):
    gateway = await gateway_factory()
    conn = await open_connection(gateway)
    try:
        await gateway.close(4015, 'voice server crashed')
        code, reason = await asyncio.wait_for(conn.wait_closed(), timeout=10.0)
        assert code == 4015
        assert 'crashed' in reason
        assert conn.is_connected() is False
    finally:
        await conn.close()


async def test_close_during_the_handshake_reports_code_and_reason(gateway_factory):
    """Discord closes a rejected session mid-handshake: Python still needs the close code."""
    gateway = await gateway_factory(reject_identifies=1)
    conn = _native.VoiceConnection(GUILD_ID, USER_ID, CHANNEL_ID)
    conn.set_session_id('stale-session')
    conn.set_server_info(GUILD_ID, 'voice-token', gateway.endpoint)
    # ConnectionFlowState.got_both_voice_updates.
    conn.set_phase(4)

    with pytest.raises(dev.ConnectionClosedError) as info:
        await conn.handshake(resume=False, timeout=5.0)

    # The message carries the code: the interesting part of a voice close is the number.
    assert '4006' in str(info.value)
    assert conn.close_code == SESSION_INVALID[0]
    assert conn.close_reason == SESSION_INVALID[1]
    assert conn.is_connected() is False


async def test_send_audio_packet_before_connect_raises():
    conn = _native.VoiceConnection(GUILD_ID, USER_ID, CHANNEL_ID)
    with pytest.raises(dev.NotConnected):
        conn.send_audio_packet(b'\x00' * 3840, encode=True)


async def test_second_handshake_while_connected_is_rejected(gateway_factory):
    gateway = await gateway_factory()
    conn = await open_connection(gateway)
    try:
        with pytest.raises(dev.AlreadyConnected):
            await conn.handshake(resume=False, timeout=5.0)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------- DAVE


async def test_dave_session_is_created_and_key_package_sent(gateway_factory):
    gateway = await gateway_factory(dave_protocol_version=1)
    conn = await open_connection(gateway)

    try:
        assert conn.dave_protocol_version == 1
        assert conn.has_dave_session() is True

        # The MLS key package is opcode 26 on a binary frame.
        frame = await gateway.wait_for_binary_op(26, timeout=10.0)
        assert frame[0] == 26
        assert len(frame) > 32, 'the serialized MLS KeyPackage looks too small'
    finally:
        await conn.close()


async def test_dave_transition_ready_is_sent(gateway_factory):
    gateway = await gateway_factory(dave_protocol_version=1)
    conn = await open_connection(gateway)

    try:
        await gateway.wait_for_binary_op(26, timeout=10.0)
        await gateway.send_json({'op': 21, 'd': {'transition_id': 7, 'protocol_version': 1}})
        ready = await gateway.wait_for_op(23, timeout=10.0)
        assert ready['d']['transition_id'] == 7

        await gateway.send_json({'op': 22, 'd': {'transition_id': 7}})
        await asyncio.sleep(0.1)
        assert conn.dave_protocol_version == 1
    finally:
        await conn.close()


async def test_dave_epoch_prepare_reissues_a_key_package(gateway_factory):
    gateway = await gateway_factory(dave_protocol_version=1)
    conn = await open_connection(gateway)

    try:
        await gateway.wait_for_binary_op(26, timeout=10.0)
        before = len([frame for frame in gateway.received_binary if frame[:1] == b'\x1a'])

        await gateway.send_json({'op': 24, 'd': {'epoch': 1, 'protocol_version': 1}})

        for _ in range(100):
            after = len([frame for frame in gateway.received_binary if frame[:1] == b'\x1a'])
            if after > before:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail('no new key package was sent after dave_prepare_epoch')
    finally:
        await conn.close()
